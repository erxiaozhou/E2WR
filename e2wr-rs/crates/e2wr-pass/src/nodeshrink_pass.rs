//! U10: the task pool and the NodeShrinkPass body (single-node tasks, D-11).
//!
//! Mirrors Python (the post-round-4-cleanup baseline):
//! - `ReduceFrameWork/ASTNodePool.py`: `ToReduceTask`/`OneCFNodeReduceTask`/
//!   `NodeListsReduceTask` (D-11: both `practical_select` call sites have max_=1,
//!   tasks are always single-node — the multi-node aggregation loop and the max_ parameter chain are not replicated; R-23 cleared the container
//!   leftovers; the Rust enum variant holds a single NodeId directly),
//!   `get_node_size_score`/`get_tree_node_lengths`/`from_ast_state`/`push`/
//!   `push_non_empty_subtree_nodes`/`_pop_with_priority_with_drop` (triple
//!   filtering)/`_select_from_p3_heap`/`handle_task_success`/`_find_parents_
//!   after_reduce`;
//! - `ReduceFrameWork/OneNodeReducer.py`: the `NodeReducer.try_one_task` dispatch
//!   (CF node → block shrinking; list → V9) and the `min(rest,900)+60` TimeoutHandler budget
//!   of `_try_non_list_node`;
//! - `ReduceFrameWork/NodeShrinkPass.py reduce()`: the strip pre-step, the epoch loop,
//!   the round-0 function-level orchestration (reusing M8's func_level facilities) and indirect-call replacement,
//!   the P-5 odd-success spot check, the last_pass_case three-way exception recovery, cross-call state,
//!   (total_run_times/last_output_hash/last_run_stop_size_score),
//!   store_exception_case's double swallow (debug artifact; Rust does not write it to disk, see below).
//!
//! Structural difference (Python's single shared tree vs Rust's per-epoch tree + per-task state injection):
//! - Python's ast_state (snapshot + tree) is shared mutable state through which V9 and block shrinking
//!   communicate; Rust rebuilds (Snapshot, Ast) from the current input per epoch, and each task clones
//!   both and injects them into NodeListTrialApplier / NodeRewriter, adopting their evolved
//!   final state afterwards. Equivalence: within an epoch all existing NodeIds are stable (cloning preserves arena indices;
//!   evolution only appends/replaces in place); at epoch boundaries Python also rebuilds the whole tree
//!   (`ASTState.from_path`). The cost of the double clone per task is recorded in the optimizations list.
//! - TimeoutHandler (SIGALRM hard interrupt) → an equivalent soft checkpoint (after a task returns, check the
//!   `min(rest,900)+60` budget; exceeding goes to the recovery path): the block-shrinking internal budget
//!   (allocate, no +60 slack) triggers before the hard budget; the hard interrupt only matters in pathological cases like a single
//!   oracle (≤30 s) hanging; the return-time check and the alarm-time check agree on the "past deadline" judgment.
//!   the "past deadline" judgment.
//! - store_exception_case only writes debug artifacts and prints (recovery is unaffected;
//!   Python's inner and outer try/except swallow its own errors doubly) — Rust writes nothing,
//!   and the double swallow degenerates to a no-op.

use std::collections::BTreeSet;
use std::collections::BinaryHeap;
use std::cmp::Reverse;
use std::path::{Path, PathBuf};
use std::time::{Duration, Instant};

use anyhow::{bail, Context as _, Result};

use e2wr_dd::factory::DdFactory;
use e2wr_dd::oracle::Oracle;
use e2wr_ir::ast::{Ast, ListRole, NodeId, NodeKind};
use e2wr_ir::snapshot::Snapshot;

use crate::common::{DirSystem, ExecResult, ExecStatus};
use crate::func_level::{
    body_inst_num, replace_indirect_calls, run_wasm_strip,
    with_backup_on_err, FuncLevelConfig, FuncLevelCtx,
};
use crate::instseq::shrink_block::{block_shrink, if_shrink};
use crate::instseq::trial::NodeListTrialApplier;
use crate::instseq::v9::{
    build_v6_cfg, OneNodeListReducerV9, OnlyOneInstTask, RNOpParam,
};
use crate::node_rewriter::NodeRewriter;

/// The two live subclasses of Python `ToReduceTask` (D-11 single-node; R-23 container leftovers cleared).
#[derive(Clone, Debug)]
pub enum Task {
    /// Python `OneCFNodeReduceTask` (Block/Loop/If).
    CfNode(NodeId),
    /// Python `NodeListsReduceTask` (always a single-element list; RootNode is also a list;
    /// the Vec container is a leftover of the D-11 single-noding, degenerated to a single NodeId by R-23).
    NodeLists(NodeId),
}

impl Task {
    pub fn first_node(&self) -> NodeId {
        match self {
            Task::CfNode(n) => *n,
            Task::NodeLists(n) => *n,
        }
    }

    pub fn total_inst_num(&self, ast: &Ast) -> u64 {
        let n = match self {
            Task::CfNode(n) => *n,
            Task::NodeLists(n) => *n,
        };
        ast.get_length(n) as u64
    }
}

/// R-16: the shared core of task dispatch — the six fields common to `NodeShrinkPass` (the main-loop path,
/// Python `NodeReducer.try_one_task`) and `FuncLevelNodeReducer` (the function-level path,
/// Python `NodeReducer.try_one_node`); the two original implementations were verbatim-identical,
/// the only real difference being RNOpParam's total_run_times/enable_fast_mode fields
/// (the two Python construction sites pass different values), passed in as parameters.
pub(crate) struct TaskDispatchCore<'a> {
    pub oracle: &'a Oracle,
    pub dd_factory: &'a DdFactory,
    pub dir: &'a DirSystem,
    pub debug: bool,
    pub only_one: OnlyOneInstTask,
    pub v6_whole_dd: bool,
}

#[allow(clippy::too_many_arguments)]
fn dispatch_task(
    core: &TaskDispatchCore<'_>,
    state: &mut PassState,
    task: &Task,
    cur_output_path: &Path,
    cur_epoch_num: i64,
    rest_time: f64,
    total_run_times: i64,
    enable_fast_mode: bool,
) -> Result<(bool, Vec<NodeId>)> {
    match task {
        Task::CfNode(node) => {
            // Python `_try_non_list_node`: allocate = min(rest, 900),
            // Hard budget allocate+60 (TimeoutHandler → soft checkpoint; see the module comment).
            let allocate_rest_time = rest_time.min(900.0);
            let hard_deadline =
                Instant::now() + Duration::from_secs_f64(allocate_rest_time + 60.0);
            let mut rewriter = NodeRewriter::new_with_state(
                state.snapshot.clone(),
                state.ast.clone(),
                core.oracle.clone(),
                core.dir.tmp_used_path.clone(),
                !core.debug,
            );
            rewriter.set_best_path(cur_output_path.to_path_buf());
            let mut rng = rand::thread_rng();
            let (success, new_nodes) = match &state.ast.node(*node).kind {
                NodeKind::Block { .. } | NodeKind::Loop { .. } => {
                    block_shrink(&mut rewriter, *node, Some(allocate_rest_time), &mut rng)?
                }
                NodeKind::If { .. } => {
                    if_shrink(&mut rewriter, *node, Some(allocate_rest_time), &mut rng)?
                }
                _ => bail!("unsupported CF node kind"),
            };
            if Instant::now() > hard_deadline {
                // Python: SIGALRM raises TimeoutError where the budget runs out → exception recovery.
                bail!("task hard timeout (min(rest,900)+60)");
            }
            let (snap, ast) = rewriter.into_state();
            state.snapshot = snap;
            state.ast = ast;
            Ok((success, new_nodes))
        }
        Task::NodeLists(node) => {
            // Python `_try_node_lists`: filter empty lists + remove_empty_
            // node_in_nodelist + V9 (D-11 always single-node → the empty-list filter degenerates to
            // a single-node emptiness test, R-23).
            if state.ast.get_length(*node) == 0 {
                return Ok((false, Vec::new()));
            }
            let children = state.ast.sub_nodes(*node);
            let kept: Vec<NodeId> = children
                .iter()
                .copied()
                .filter(|c| state.ast.get_length(*c) > 0)
                .collect();
            if kept.len() != children.len() {
                state.ast.replace_split_with_new_sub_nodes(
                    *node,
                    0,
                    children.len(),
                    kept,
                );
            }
            let node_list = *node;
            let mut applier = NodeListTrialApplier::new_with_ast(
                node_list,
                state.snapshot.clone(),
                state.ast.clone(),
                core.oracle.clone(),
                core.dir.tmp_used_path.clone(),
                core.debug,
            )?;
            applier.set_best_path(cur_output_path.to_path_buf());
            let op_param = RNOpParam {
                cur_epoch_num: Some(cur_epoch_num),
                total_run_times: Some(total_run_times),
                enable_fast_mode,
            };
            let mut reducer = OneNodeListReducerV9::new(
                core.debug,
                build_v6_cfg(&op_param, core.only_one, core.v6_whole_dd),
            );
            let outcome = reducer.reduce_multi(
                state.snapshot.module(),
                &state.ast,
                node_list,
                &mut applier,
                core.dd_factory,
                rest_time,
            )?;
            let (snap, ast) = applier.into_state();
            state.snapshot = snap;
            state.ast = ast;
            Ok((outcome.final_result, outcome.cur_nodes))
        }
    }
}

/// Python `get_node_size_score`. The try/except → 1 around get_length is a defensive branch
/// (Rust get_length always succeeds), not ported.
pub fn node_size_score(ast: &Ast, node: NodeId, max_length: Option<f64>) -> f64 {
    let length_score = ast.get_length(node) as f64;
    match &ast.node(node).kind {
        NodeKind::List { .. } => 1.0 + length_score,
        NodeKind::Insts { .. } => 0.0,
        _ => match max_length {
            Some(m) if m > 0.0 => length_score / m,
            _ => length_score,
        },
    }
}

fn is_cf_node(ast: &Ast, node: NodeId) -> bool {
    matches!(
        ast.node(node).kind,
        NodeKind::Block { .. } | NodeKind::Loop { .. } | NodeKind::If { .. }
    )
}

fn is_root_list(ast: &Ast, node: NodeId) -> bool {
    matches!(&ast.node(node).kind, NodeKind::List { role: ListRole::Root, .. })
}

/// Python `node_is_removed` (reachability from the root via `is_ancestor_of(root, node)`).
/// The Rust equivalent walks the parent chain upward — `replace_split_with_new_sub_nodes` clears
/// parent=None when detaching a node; a chain breaking before reaching the function root means removed.
pub fn node_is_removed(ast: &Ast, node: NodeId) -> bool {
    let root = ast.root_of_func(ast.node(node).loc.func_idx as usize);
    let mut cur = Some(node);
    while let Some(id) = cur {
        if id == root {
            return false;
        }
        cur = ast.node(id).parent;
    }
    true
}

/// Pre-order collection of nodes with length > 0 (Python `traverse_ast` + `get_all_non_empty_ast_nodes`;
/// the filter does not affect descendant traversal — descendants of a zero-length list/instruction sequence always have length 0).
fn collect_non_empty_preorder(ast: &Ast, id: NodeId, out: &mut Vec<NodeId>) {
    if ast.get_length(id) > 0 {
        out.push(id);
    }
    for c in ast.sub_nodes(id) {
        collect_non_empty_preorder(ast, c, out);
    }
}

/// Heap keys: finite non-positive scores (length + 1.0, or divided by a positive base); NaN cannot occur.
#[derive(Clone, Copy, Debug, PartialEq)]
struct ScoreKey(f64);

impl Eq for ScoreKey {}

impl PartialOrd for ScoreKey {
    fn partial_cmp(&self, other: &Self) -> Option<std::cmp::Ordering> {
        Some(self.cmp(other))
    }
}

impl Ord for ScoreKey {
    fn cmp(&self, other: &Self) -> std::cmp::Ordering {
        self.0.total_cmp(&other.0)
    }
}

/// Python `ASTNodePool` (the main heap + the P3 parent-reprocessing heap).
/// Heap order: a min-heap of (-score, id) — Python breaks ties with id(node) (object address),
/// Rust with NodeId (the Python address order is inherently unreproducible; same class as P-29).
pub struct AstNodePool {
    heap: BinaryHeap<Reverse<(ScoreKey, NodeId)>>,
    in_heap: BTreeSet<NodeId>,
    all_inst_num: Option<f64>,
    reprocess_parents: bool,
    p3_heap: BinaryHeap<Reverse<(u32, NodeId)>>,
    p3_processed: BTreeSet<NodeId>,
    current_source: Option<&'static str>,
}

impl AstNodePool {
    /// Python `from_ast_state` (main-loop arguments: considered=None,
    /// prioritize_node_lists=False (P-1), reprocess_parents=True,
    /// all_inst_num=Some). The M11 scenario passes no all_inst_num/reprocess_parents.
    pub fn from_ast(ast: &Ast, all_inst_num: Option<u64>, reprocess_parents: bool) -> AstNodePool {
        Self::from_ast_for_funcs(ast, None, all_inst_num, reprocess_parents)
    }

    /// The restricted-function-set form of `from_ast` (Python from_ast_state's
    /// considered_func_idxs argument; FuncLevelNodeReducer passes the modified set).
    pub fn from_ast_for_funcs(
        ast: &Ast,
        considered: Option<&BTreeSet<u32>>,
        all_inst_num: Option<u64>,
        reprocess_parents: bool,
    ) -> AstNodePool {
        let mut pool = AstNodePool {
            heap: BinaryHeap::new(),
            in_heap: BTreeSet::new(),
            all_inst_num: all_inst_num.map(|v| v as f64),
            reprocess_parents,
            p3_heap: BinaryHeap::new(),
            p3_processed: BTreeSet::new(),
            current_source: Some("main_heap"),
        };
        let mut nodes = Vec::new();
        for f in 0..ast.func_roots.len() {
            if let Some(set) = considered {
                if !set.contains(&(f as u32)) {
                    continue;
                }
            }
            collect_non_empty_preorder(ast, ast.root_of_func(f), &mut nodes);
        }
        for node in nodes {
            if matches!(ast.node(node).kind, NodeKind::Insts { .. }) {
                continue;
            }
            pool.push(ast, node);
        }
        pool
    }

    /// Whether only the main heap remains (Python `__bool__`: the main heap only, not the P3 heap).
    pub fn is_empty(&self) -> bool {
        self.heap.is_empty()
    }

    /// Python `push` (prioritize_node_lists=False → scoring with max_length=None).
    pub fn push(&mut self, ast: &Ast, node: NodeId) {
        if self.in_heap.contains(&node) {
            return;
        }
        let score = node_size_score(ast, node, None);
        self.heap.push(Reverse((ScoreKey(-score), node)));
        self.in_heap.insert(node);
    }

    fn push_to_p3_heap(&mut self, ast: &Ast, node: NodeId) {
        let score = ast.get_length(node);
        if score > 0 {
            self.p3_heap.push(Reverse((score, node)));
        }
    }

    /// Python `push_non_empty_subtree_nodes`.
    pub fn push_non_empty_subtree_nodes(&mut self, ast: &Ast, new_nodes: &[NodeId]) {
        for n in new_nodes {
            let mut subs = Vec::new();
            collect_non_empty_preorder(ast, *n, &mut subs);
            for sub in subs {
                if ast.get_length(sub) == 0 {
                    continue;
                }
                if matches!(ast.node(sub).kind, NodeKind::Insts { .. }) {
                    continue;
                }
                if is_cf_node(ast, sub) {
                    debug_assert!(ast.node(sub).parent.is_some());
                }
                self.push(ast, sub);
            }
        }
    }

    /// Python `practical_select` (D-11: single-node tasks).
    pub fn practical_select(&mut self, ast: &Ast, max_score: Option<f64>) -> Option<Task> {
        if let Some(task) = self.select_from_main_heap(ast, max_score) {
            self.current_source = Some("main_heap");
            return Some(task);
        }
        if self.reprocess_parents {
            if let Some(task) = self.select_from_p3_heap(ast) {
                self.current_source = Some("p3");
                return Some(task);
            }
        }
        self.current_source = None;
        None
    }

    /// Python `_select_from_main_heap` → `_pop_with_priority_with_drop`
    /// (triple filtering: InstsNode skipped / max_score threshold dropped / length 0 or already deleted
    /// skipped; the threshold test rescores with the **current** length, max_length=all_inst_num).
    fn select_from_main_heap(&mut self, ast: &Ast, max_score: Option<f64>) -> Option<Task> {
        while let Some(Reverse((_, node))) = self.heap.pop() {
            self.in_heap.remove(&node);
            if matches!(ast.node(node).kind, NodeKind::Insts { .. }) {
                continue;
            }
            if let Some(ms) = max_score {
                if node_size_score(ast, node, self.all_inst_num) >= ms {
                    continue;
                }
            }
            if ast.get_length(node) == 0 || node_is_removed(ast, node) {
                continue;
            }
            return Some(if is_cf_node(ast, node) {
                Task::CfNode(node)
            } else {
                Task::NodeLists(node)
            });
        }
        None
    }

    /// Python `_select_from_p3_heap`.
    fn select_from_p3_heap(&mut self, ast: &Ast) -> Option<Task> {
        while let Some(Reverse((_, node))) = self.p3_heap.pop() {
            if self.p3_processed.contains(&node) {
                continue;
            }
            self.p3_processed.insert(node);
            if node_is_removed(ast, node) {
                continue;
            }
            if ast.is_list(node) {
                return Some(Task::NodeLists(node));
            }
            if is_cf_node(ast, node) {
                return Some(Task::CfNode(node));
            }
        }
        None
    }

    /// Python `handle_task_success` (dispatched by _current_source; the main-heap path's
    /// reprocess_parents branch is constant true under the main-loop construction, but the switch semantics are kept).
    pub fn handle_task_success(&mut self, ast: &Ast, task: &Task, new_nodes: &[NodeId]) {
        if self.current_source == Some("p3") {
            for parent in Self::find_parents_after_reduce(ast, task, new_nodes) {
                if !node_is_removed(ast, parent) {
                    self.push_to_p3_heap(ast, parent);
                }
            }
            return;
        }
        self.push_non_empty_subtree_nodes(ast, new_nodes);
        if self.reprocess_parents {
            for parent in Self::find_parents_after_reduce(ast, task, new_nodes) {
                if is_root_list(ast, parent) {
                    continue;
                }
                if !node_is_removed(ast, parent) {
                    self.push_to_p3_heap(ast, parent);
                }
            }
        }
    }

    /// Python `_find_parents_after_reduce` (list tasks take the task node's parent,
    /// CF tasks the new node's parent; deduplicated, order preserved).
    fn find_parents_after_reduce(ast: &Ast, task: &Task, new_nodes: &[NodeId]) -> Vec<NodeId> {
        let mut parents: Vec<NodeId> = Vec::new();
        let mut seen = BTreeSet::new();
        let source: Box<dyn Iterator<Item = NodeId>> = match task {
            Task::NodeLists(n) => Box::new(std::iter::once(*n)),
            Task::CfNode(_) => Box::new(new_nodes.iter().copied()),
        };
        for n in source {
            if let Some(p) = ast.node(n).parent {
                if seen.insert(p) {
                    parents.push(p);
                }
            }
        }
        parents
    }
}

/// Python `NodeShrinkPass`. The three cross-call state fields + input_parser_inst_num
/// (Python's -1 sentinel → Option) stay with the instance.
pub struct NodeShrinkPass<'a> {
    oracle: &'a Oracle,
    dd_factory: DdFactory,
    dir: DirSystem,
    debug: bool,
    use_function_level_removal: bool,
    /// Function-level facilities when to_test_func_name exists (Python instrument_manager +
    /// ty_remove_func_pass; None = Python's instrument_manager=None skip-everything
    /// form).
    fl: Option<FuncLevelCtx<'a>>,
    /// Single-stage mode (the --force_inst_mutation equivalent; default Disable = all stages).
    only_one: OnlyOneInstTask,
    /// The process-level `ONE_V6_CFG.enable_whole_dd` made explicit (the --disable_whole_dd
    /// equivalent; Python's actual default is True).
    v6_whole_dd: bool,
    total_run_times: i64,
    last_output_hash: Option<String>,
    last_run_stop_size_score: Option<f64>,
    input_parser_inst_num: Option<u64>,
}

fn sha256_of(path: &Path) -> Option<String> {
    let bytes = std::fs::read(path).ok()?;
    // Python only uses a hex digest for equality comparison; a simple hex form suffices here (no real hash needed —
    // the semantics are merely "same input as last round's output keeps the continuation score" equality).
    // A length+content prefix/suffix mix would widen the collision surface; full hex encoding used instead.
    Some(format!("{:x}", FastDigest(&bytes)))
}

/// A sufficient equality digest: holding full hex bytes is long, so a dual FNV-128 hash reduces collisions
/// (the difference from Python's sha256 is the theoretical collision rate, with no behavioral impact on the "same-input
/// continuation" test; if byte-exactness is ever needed, swap in the sha2 dependency).
struct FastDigest<'b>(&'b [u8]);

impl std::fmt::LowerHex for FastDigest<'_> {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        let mut h1: u64 = 0xcbf29ce484222325;
        let mut h2: u64 = 0x9e3779b97f4a7c15;
        for &b in self.0 {
            h1 = (h1 ^ b as u64).wrapping_mul(0x100000001b3);
            h2 = (h2 ^ b as u64).wrapping_mul(0x9e3779b97f4a7c15).rotate_left(17);
        }
        write!(f, "{h1:016x}{h2:016x}")
    }
}

impl<'a> NodeShrinkPass<'a> {
    #[allow(clippy::too_many_arguments)]
    pub fn new(
        oracle: &'a Oracle,
        result_dir: &Path,
        to_test_func_name: Option<&str>,
        dd_factory: DdFactory,
        use_function_level_removal: bool,
        debug: bool,
    ) -> NodeShrinkPass<'a> {
        Self::with_stage(
            oracle,
            result_dir,
            to_test_func_name,
            dd_factory,
            use_function_level_removal,
            OnlyOneInstTask::Disable,
            debug,
        )
    }

    /// U11: construction with single-stage semantics (the --force_inst_mutation equivalent; since R-11 expressed
    /// directly via [`OnlyOneInstTask`], `Disable` = all stages).
    #[allow(clippy::too_many_arguments)]
    pub fn with_stage(
        oracle: &'a Oracle,
        result_dir: &Path,
        to_test_func_name: Option<&str>,
        dd_factory: DdFactory,
        use_function_level_removal: bool,
        only_one: OnlyOneInstTask,
        debug: bool,
    ) -> NodeShrinkPass<'a> {
        let fl = to_test_func_name.map(|name| {
            FuncLevelCtx::new(
                oracle,
                result_dir,
                name,
                dd_factory.clone(),
                FuncLevelConfig::default(),
                debug,
            )
        });
        NodeShrinkPass {
            oracle,
            dd_factory,
            dir: DirSystem::new(result_dir),
            debug,
            use_function_level_removal,
            fl,
            only_one,
            v6_whole_dd: true,
            total_run_times: -1,
            last_output_hash: None,
            last_run_stop_size_score: None,
            input_parser_inst_num: None,
        }
    }

    /// M12: the wiring entry for `ONE_V6_CFG.enable_whole_dd=False` (--disable_whole_dd);
    /// chain construction.
    pub fn with_v6_whole_dd(mut self, enable: bool) -> Self {
        self.v6_whole_dd = enable;
        self
    }

    // Z-4 (ruled deleted 2026-09-29): Z-3 once added a with_fl_cfg chain constructor injecting
    // FuncLevelConfig, but SchedulerConfig has no such field — no caller could ever build an instance
    // with a custom config; the zero-call method was removed. fl.cfg's two fields actually always use the defaults
    // 300/30 (matching the Python hardcodes); no behavioral gap; add it back if ever needed.

    /// Python `call_oracle(..., CheckType.SELF_CHECK)`: exceptions treated as not passing.
    fn oracle_check(&self, path: &Path) -> bool {
        match self.oracle.check(path) {
            Ok(v) => v,
            Err(e) => {
                if self.debug {
                    eprintln!("oracle error: {e:#}");
                }
                false
            }
        }
    }

    /// Python `_run_function_level_reduction_to_file` (the real total_run_times;
    /// `last_try_is_proi` is write-only, not ported; the single-iteration loop shell straightened).
    /// R-17: the body goes through the shared base in func_level.rs, keeping the differences here — snapshot-loading
    /// error text, input_parser_inst_num baseline collection, the real total_run_times, no time-cost print.
    fn run_function_level(
        &mut self,
        cur_input_path: &Path,
        cur_output_path: &Path,
    ) -> Result<(bool, Snapshot)> {
        let input_snapshot =
            Snapshot::from_path(cur_input_path).context("load function-level input")?;
        if self.input_parser_inst_num.is_none() {
            self.input_parser_inst_num = Some(body_inst_num(&input_snapshot));
        }
        let is_first_round = self.total_run_times == 0;
        let raw_setting = (self.fl.is_some(), self.total_run_times);
        crate::func_level::run_function_level_shared(
            self.fl.as_mut(),
            input_snapshot,
            cur_input_path,
            cur_output_path,
            is_first_round,
            raw_setting,
            false,
            self.debug,
        )
    }

    /// Python `NodeReducer.try_one_task` dispatch + state adoption.
    /// Returns (success, new_nodes); Err corresponds to a Python exception (entering the recovery path).
    fn try_one_task(
        &mut self,
        state: &mut PassState,
        task: &Task,
        cur_output_path: &Path,
        cur_epoch_num: i64,
        rest_time: f64,
    ) -> Result<(bool, Vec<NodeId>)> {
        // Python `NodeReducer.try_one_task`: total_run_times=instance counter,
        // enable_fast_mode=True.
        let core = TaskDispatchCore {
            oracle: self.oracle,
            dd_factory: &self.dd_factory,
            dir: &self.dir,
            debug: self.debug,
            only_one: self.only_one,
            v6_whole_dd: self.v6_whole_dd,
        };
        dispatch_task(
            &core,
            state,
            task,
            cur_output_path,
            cur_epoch_num,
            rest_time,
            self.total_run_times,
            true,
        )
    }

    /// Python `NodeShrinkPass.reduce` (the @reduce_checker input-oracle check is borne
    /// by the caller/CLI, same convention as FuncLevelPass).
    pub fn reduce(
        &mut self,
        cur_input_path: &Path,
        cur_output_path: &Path,
        timeout: f64,
    ) -> Result<ExecResult> {
        let start_time = Instant::now();
        let original_size = std::fs::metadata(cur_input_path)?.len() as i64;
        std::fs::copy(cur_input_path, cur_output_path)
            .context("copy input to output")?;
        let mut cur_input_path: PathBuf = cur_output_path.to_path_buf();

        let stripped_path = self.dir.tmp_dir.join("stripped_input.wasm");
        // Z-3 wiring: the strip timeout reads FuncLevelConfig (falling back to the default 30 when fl is absent,
        // matching the Python hardcode; behavior unchanged).
        let strip_timeout_s = self
            .fl
            .as_ref()
            .map(|fl| fl.cfg.strip_timeout_s)
            .unwrap_or(FuncLevelConfig::default().strip_timeout_s);
        run_wasm_strip(&cur_input_path, &stripped_path, strip_timeout_s);
        if self.oracle_check(&stripped_path) {
            std::fs::copy(&stripped_path, &cur_input_path)?;
        }
        self.input_parser_inst_num = None;

        let should_stop_time = start_time + Duration::from_secs_f64(timeout);
        self.total_run_times += 1;
        let mut cur_epoch_num: i64 = -1;
        let mut last_exception_score: Option<f64> = None;
        let last_pass_case: PathBuf = self.dir.tmp_dir.join("last_pass_case.wasm");
        if last_pass_case.exists() {
            std::fs::remove_file(&last_pass_case)?;
        }

        let cur_input_hash = sha256_of(&cur_input_path);
        let has_start_info = match (&cur_input_hash, &self.last_output_hash) {
            (Some(a), Some(b)) => a == b,
            _ => false,
        };
        if !has_start_info {
            self.last_run_stop_size_score = None;
        }

        // Python: the try wraps the whole epoch section; after the exception is printed (re-raised only in DEBUG) it falls into
        // the wrap-up. state plays the ast_state role (snapshot + tree).
        let mut final_state: Option<PassState> = None;
        let epoch_result = self.run_epochs(
            &mut cur_input_path,
            cur_output_path,
            &last_pass_case,
            should_stop_time,
            &mut cur_epoch_num,
            &mut last_exception_score,
            &mut final_state,
        );
        if let Err(e) = epoch_result {
            eprintln!("NodeShrinkPass epoch error: {e:#}");
            if self.debug {
                return Err(e);
            }
        }

        if last_pass_case.exists() && !self.oracle_check(cur_output_path) {
            println!("Store last passing case to output path");
            std::fs::copy(&last_pass_case, cur_output_path)?;
        }

        self.last_output_hash = sha256_of(&cur_input_path);
        let Some(state) = final_state else {
            // Python: with timeout<=0 the epoch loop runs zero times and ast_state is unbound, i.e. it
            // crashes (NameError) — the explicit error aligns.
            bail!("NodeShrinkPass.reduce ran no epoch (timeout={timeout}?)");
        };
        let cur_inst_num = body_inst_num(&state.snapshot);
        let current_size = std::fs::metadata(cur_output_path)?.len() as i64;
        Ok(ExecResult {
            exec_status: ExecStatus::Success,
            exec_taken_time: start_time.elapsed().as_secs_f64(),
            reduced_size_num: Some(original_size - current_size),
            reduced_inst_num: self
                .input_parser_inst_num
                .map(|b| b.saturating_sub(cur_inst_num)),
            is_partial_by_timeout: Instant::now() >= should_stop_time,
        })
    }

    /// Python reduce()'s epoch section (the try block's content).
    #[allow(clippy::too_many_arguments)]
    fn run_epochs(
        &mut self,
        cur_input_path: &mut PathBuf,
        cur_output_path: &Path,
        last_pass_case: &Path,
        should_stop_time: Instant,
        cur_epoch_num: &mut i64,
        last_exception_score: &mut Option<f64>,
        final_state: &mut Option<PassState>,
    ) -> Result<()> {
        let mut early_return = false;
        let mut success_times: u64 = 0;
        let mut success_times_at_last_epoch = 0;
        let mut state: Option<PassState> = None;
        while Instant::now() < should_stop_time {
            let mut last_epoch_has_reduced = false;
            *cur_epoch_num += 1;
            if *cur_epoch_num >= 1 {
                self.last_run_stop_size_score = None;
                println!(
                    "Success times at last epoch [{}]: {}",
                    *cur_epoch_num - 1,
                    success_times - success_times_at_last_epoch
                );
                success_times_at_last_epoch = success_times;
            }

            let epoch_snapshot;
            if self.use_function_level_removal && *cur_epoch_num == 0 {
                let (did_reduce, snapshot) =
                    self.run_function_level(cur_input_path, cur_output_path)?;
                *cur_input_path = cur_output_path.to_path_buf();
                if did_reduce {
                    last_epoch_has_reduced = true;
                }
                epoch_snapshot = snapshot;
            } else {
                epoch_snapshot = Snapshot::from_path(cur_input_path)?;
            }
            let mut state_cur = PassState {
                ast: Ast::from_module(epoch_snapshot.module())?,
                snapshot: epoch_snapshot,
            };
            if self.input_parser_inst_num.is_none() {
                self.input_parser_inst_num = Some(body_inst_num(&state_cur.snapshot));
            }
            if cur_input_path.as_path() != last_pass_case {
                std::fs::copy(&*cur_input_path, last_pass_case)?;
            }

            if *cur_epoch_num == 0 {
                let rest_time = (should_stop_time - Instant::now()).as_secs_f64();
                if rest_time > 0.0 {
                    if let Some(fl) = self.fl.as_mut() {
                        // Z-3 wiring: the cap reads FuncLevelConfig (default 300, matching the
                        // Python hardcode; behavior unchanged).
                        let dd_timeout = rest_time.min(fl.cfg.indirect_dd_timeout_cap_s);
                        let snapshot_ref = state_cur.snapshot.clone();
                        let in_path = cur_input_path.clone();
                        let (_new_snapshot, replaced_indirect_idxs) = with_backup_on_err(
                            fl,
                            "replace_call_indirect_bak.wasm",
                            &in_path,
                            cur_output_path,
                            || {
                                replace_indirect_calls(
                                    fl,
                                    &in_path,
                                    cur_output_path,
                                    &snapshot_ref,
                                    Some(dd_timeout),
                                )
                            },
                        )?;
                        *cur_input_path = cur_output_path.to_path_buf();
                        if !replaced_indirect_idxs.is_empty() {
                            state_cur.snapshot = _new_snapshot;
                            state_cur.ast =
                                Ast::from_module(state_cur.snapshot.module())?;
                            self.input_parser_inst_num =
                                Some(body_inst_num(&state_cur.snapshot));
                            if cur_input_path.as_path() != last_pass_case {
                                std::fs::copy(&*cur_input_path, last_pass_case)?;
                            }
                        }
                    }
                }
            }
            let mut pool =
                AstNodePool::from_ast(&state_cur.ast, self.input_parser_inst_num, true);

            // ---- Task loop ----
            loop {
                let last_pass_size = std::fs::metadata(&*cur_input_path)?.len() as i64;
                let max_score = min_of_two_optional(
                    self.last_run_stop_size_score,
                    *last_exception_score,
                );
                let Some(task) = pool.practical_select(&state_cur.ast, max_score) else {
                    break;
                };
                let node = task.first_node();
                // Python's rest_time is a float that can go negative; timeout breaks. Rust's Instant
                // subtraction saturates at 0, so `< 0.0` is never true; compare instants explicitly (found by the bench).
                let rest_time = (should_stop_time - Instant::now()).as_secs_f64();
                if Instant::now() > should_stop_time {
                    early_return = true;
                    if *cur_epoch_num == 0 {
                        let max_length = self
                            .input_parser_inst_num
                            .expect("input_parser_inst_num set before task loop")
                            as f64;
                        self.last_run_stop_size_score =
                            Some(node_size_score(&state_cur.ast, node, Some(max_length)));
                    }
                    break;
                }

                let task_inst_num_before = task.total_inst_num(&state_cur.ast);
                let task_result = self.try_one_task(
                    &mut state_cur,
                    &task,
                    cur_output_path,
                    *cur_epoch_num,
                    rest_time,
                );
                match task_result {
                    Ok((success_, new_nodes)) => {
                        // P-5 spot check: on odd successes (the pre-increment value), re-verify the oracle.
                        let audit_fail = success_
                            && !self.oracle_check(cur_output_path)
                            && success_times % 2 == 1;
                        if audit_fail {
                            // Python raises UnStableCaaseException → the same except branch's
                            // recovery path (unstable is not re-raised even in DEBUG).
                            self.recover_from_exception(
                                node,
                                cur_input_path,
                                cur_output_path,
                                last_pass_case,
                                last_exception_score,
                                &mut state_cur,
                                &mut early_return,
                            );
                            break;
                        }
                        if success_ {
                            let counts_as_reduce = match &task {
                                Task::NodeLists(_) => {
                                    let after: u64 =
                                        new_nodes.iter().map(|n| state_cur.ast.get_length(*n) as u64).sum();
                                    success_ && task_inst_num_before > after
                                }
                                Task::CfNode(_) => success_,
                            };
                            if counts_as_reduce {
                                last_epoch_has_reduced = true;
                            }
                            pool.handle_task_success(&state_cur.ast, &task, &new_nodes);
                            success_times += 1;
                            std::fs::copy(cur_output_path, last_pass_case)?;
                            *cur_input_path = last_pass_case.to_path_buf();
                        }
                        let cur_pass_size = std::fs::metadata(&*cur_input_path)?.len() as i64;
                        println!(
                            "[Success Times: {success_times}][{}] CurSize: {cur_pass_size} Last task success: {success_} RS: {}",
                            *cur_epoch_num,
                            last_pass_size - cur_pass_size
                        );
                    }
                    Err(e) => {
                        if self.debug {
                            return Err(e);
                        }
                        // Python: the DEBUG=False except Exception branch.
                        self.recover_from_exception(
                            node,
                            cur_input_path,
                            cur_output_path,
                            last_pass_case,
                            last_exception_score,
                            &mut state_cur,
                            &mut early_return,
                        );
                        break;
                    }
                }
            }
            if early_return {
                println!("early_return is True, break");
                state = Some(state_cur);
                break;
            }
            state = Some(state_cur);
            if pool.is_empty() {
                *last_exception_score = None;
                if !last_epoch_has_reduced
                    && (*cur_epoch_num > 0 || self.total_run_times > 0)
                {
                    println!(
                        "Pool is empty and last_epoch_has_reduced? {last_epoch_has_reduced}, break"
                    );
                    break;
                }
            }
        }
        *final_state = state;
        Ok(())
    }

    /// The three-step recovery of Python's except branch: exception-score threshold → (store_exception_case
    /// double-swallowed, nothing written) → the last_pass_case three-way branch.
    #[allow(clippy::too_many_arguments)]
    fn recover_from_exception(
        &mut self,
        node: NodeId,
        cur_input_path: &mut PathBuf,
        cur_output_path: &Path,
        last_pass_case: &Path,
        last_exception_score: &mut Option<f64>,
        state: &mut PassState,
        early_return: &mut bool,
    ) {
        let max_length = self
            .input_parser_inst_num
            .expect("input_parser_inst_num set before task loop")
            as f64;
        *last_exception_score =
            Some(node_size_score(&state.ast, node, Some(max_length)) - 0.00001);
        println!(
            "Process UnStableCaaseException ..., next last_exception_score is: {:?}",
            *last_exception_score
        );
        // store_exception_case: debug artifact, double-swallowed → no-op.
        if !last_pass_case.exists() {
            println!("No last passing case found, stop reducing");
            *early_return = true;
        } else if self.oracle_check(last_pass_case) {
            let _ = std::fs::copy(last_pass_case, cur_output_path);
            *cur_input_path = last_pass_case.to_path_buf();
            if let Ok(snap) = Snapshot::from_path(last_pass_case) {
                if let Ok(new_ast) = Ast::from_module(snap.module()) {
                    state.snapshot = snap;
                    state.ast = new_ast;
                }
            }
        } else {
            println!("last_pass_case not exists or does not pass oracle, will stop reducing");
            *early_return = true;
        }
    }
}

/// Per-epoch mutable state (Python ast_state: snapshot + tree).
struct PassState {
    snapshot: Snapshot,
    ast: Ast,
}

/// Python `_get_min_of_two_optional_float`.
fn min_of_two_optional(a: Option<f64>, b: Option<f64>) -> Option<f64> {
    match (a, b) {
        (None, x) => x,
        (x, None) => x,
        (Some(a), Some(b)) => Some(a.min(b)),
    }
}

// ---------------------------------------------------------------------------
// FuncLevelNodeReducer (M11; the follow-up step of FinalPolishPass.polish_return_type)
// ---------------------------------------------------------------------------

/// Python `ReduceFrameWork/FuncLevelNodeReducer.py`: repeatedly performs "within-function node-level
/// reduction" (block shrinking + V9) on a given function set, the epoch loop running until timeout or a zero-success round.
///
/// Differences from the NodeShrinkPass main loop (same-source Python differences, copied verbatim):
/// - the task pool only contains nodes of the considered functions (from_ast_state's
///   considered_func_idxs); no reprocess_parents (the P3 heap is off),
///   no all_inst_num, no max_score threshold filtering;
/// - no function-level preprocessing/strip/P-5 spot check/last_pass_case — on a task exception, state is rebuilt from
///   cur_snapshot (the snapshot of the last success) and the epoch's task loop breaks,
///   the outer epoch loop deciding on another round via last_epoch_has_reduced;
/// - RNOpParam: total_run_times=0, enable_fast_mode=False, cur_epoch_num
///   this structure's own counter (from -1, the first epoch being 0);
/// - after success, `ast_state.to_file(cur_output_path)` — the trial commit already wrote the artifact
///   to best_path (same bytes); no duplicate write.
pub struct FuncLevelNodeReducer<'a> {
    oracle: &'a Oracle,
    dd_factory: DdFactory,
    dir: DirSystem,
    debug: bool,
    /// The process-level globals (--force_inst_mutation / --disable_whole_dd) made explicit;
    /// the Python side reads them via `build_v6_cfg` (M12 wiring).
    only_one: OnlyOneInstTask,
    v6_whole_dd: bool,
}

impl<'a> FuncLevelNodeReducer<'a> {
    pub fn new(
        oracle: &'a Oracle,
        dd_factory: DdFactory,
        result_dir: &Path,
        debug: bool,
    ) -> FuncLevelNodeReducer<'a> {
        FuncLevelNodeReducer {
            oracle,
            dd_factory,
            dir: DirSystem::new(result_dir),
            debug,
            only_one: OnlyOneInstTask::Disable,
            v6_whole_dd: true,
        }
    }

    /// M12: the wiring entry for --force_inst_mutation / --disable_whole_dd; chain construction.
    pub fn with_v6(mut self, only_one: OnlyOneInstTask, v6_whole_dd: bool) -> Self {
        self.only_one = only_one;
        self.v6_whole_dd = v6_whole_dd;
        self
    }

    /// Python `FuncLevelNodeReducer.reduce`. Err can only come from I/O/encoding internals
    /// (the corresponding Python exception goes to the except-rebuild branch — task-level errors are already caught here; an Err going up
    /// means the epoch rebuild itself failed).
    pub fn reduce(
        &self,
        cur_output_path: &Path,
        input_snapshot: &Snapshot,
        considered_func_idxs: &BTreeSet<u32>,
        timeout: f64,
    ) -> Result<Snapshot> {
        println!("Start FuncLevelNodeReducer, timeout: {timeout}");
        let should_stop_time = Instant::now() + Duration::from_secs_f64(timeout);
        let mut cur_snapshot = input_snapshot.full_copy();
        let mut cur_epoch_num: i64 = -1;
        let mut success_times: u64 = 0;

        let mut early_return = false;
        'outer: while Instant::now() < should_stop_time {
            cur_epoch_num += 1;
            let mut state = PassState {
                ast: Ast::from_module(cur_snapshot.module())?,
                snapshot: cur_snapshot.full_copy(),
            };
            let mut pool =
                AstNodePool::from_ast_for_funcs(&state.ast, Some(considered_func_idxs), None, false);
            let mut last_epoch_has_reduced = false;
            loop {
                let Some(task) = pool.practical_select(&state.ast, None) else {
                    break;
                };
                let node = task.first_node();
                // Python's rest_time is a float that can go negative; timeout breaks. Rust's Instant
                // subtraction saturates at 0, so `< 0.0` is never true; compare instants explicitly (found by the bench).
                let rest_time = (should_stop_time - Instant::now()).as_secs_f64();
                if Instant::now() > should_stop_time {
                    early_return = true;
                    break;
                }
                // Python `_is_to_reduce_node`'s double check (the pool selection already filtered; behaviorally equivalent).
                if state.ast.get_length(node) == 0 || node_is_removed(&state.ast, node) {
                    continue;
                }
                match self.try_one_node(&mut state, &task, cur_epoch_num, rest_time, cur_output_path) {
                    Ok((success_, new_nodes)) => {
                        if success_ {
                            last_epoch_has_reduced = true;
                            pool.push_non_empty_subtree_nodes(&state.ast, &new_nodes);
                            success_times += 1;
                            cur_snapshot = state.snapshot.full_copy();
                            println!(
                                "[FL][Success Times: {success_times}] Current size: {}",
                                std::fs::metadata(cur_output_path).map(|m| m.len()).unwrap_or(0),
                            );
                        }
                    }
                    Err(e) => {
                        eprintln!("FuncLevelNodeReducer task error: {e:#}");
                        if self.debug {
                            return Err(e);
                        }
                        // Python's except branch rebuilds ast_state before breaking — the outer epoch top
                        // rebuilds it again, making that assignment dead code (not ported).
                        break;
                    }
                }
            }
            if early_return {
                println!("early_return is True, break");
                break 'outer;
            }
            if !last_epoch_has_reduced {
                break 'outer;
            }
        }
        Ok(cur_snapshot)
    }

    /// Python `NodeReducer.try_one_node` (task dispatch) + state adoption.
    fn try_one_node(
        &self,
        state: &mut PassState,
        task: &Task,
        cur_epoch_num: i64,
        rest_time: f64,
        cur_output_path: &Path,
    ) -> Result<(bool, Vec<NodeId>)> {
        // Python FuncLevelNodeReducer arguments: total_run_times=0,
        // enable_fast_mode=False (→ build_v6_cfg always takes the default all-on config).
        let core = TaskDispatchCore {
            oracle: self.oracle,
            dd_factory: &self.dd_factory,
            dir: &self.dir,
            debug: self.debug,
            only_one: self.only_one,
            v6_whole_dd: self.v6_whole_dd,
        };
        dispatch_task(
            &core,
            state,
            task,
            cur_output_path,
            cur_epoch_num,
            rest_time,
            0,
            false,
        )
    }
}
