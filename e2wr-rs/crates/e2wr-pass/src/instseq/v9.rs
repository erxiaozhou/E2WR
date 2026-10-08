//! U8: the V9 orchestration capstone (single node list, D-11).
//!
//! Mirrors Python (the post-round-4-cleanup baseline):
//! - `ReduceUtil/ReduceInsts_V9.py`: `reduce_node_list_by_insts_v9` /
//!   `OneNodeListReducerV9.reduce_multi` (stage order UR→CFN→CORE→CF_ELEM→REV→P3,
//!   the `for _ in range(1)` single straight pass);
//! - `ReduceUtil/ReduceInsts_cfg_util.py`: `ReduceNodeListV6CFG` / `build_v6_cfg`
//!   / `V7Cfg.from_reduce_cfg` (the FULL branch deleted, B-5);
//! - `ReduceUtil/RNOpParam.py`: `RNOpParam` / `OnlyOneInstTask`
//!   (a process-level global `ONLY_ONE_TASK_SETTING` in Python; an explicit parameter in Rust);
//! - `ReduceUtil/util.py get_try_time` (the three-tier budget);
//! - `ReduceInsts_V9_util.py finalize_process` +
//!   `RawElemsCache.get_raw_idxs_from_elems` (the final count: the number of elements
//!   with non-empty raw_index).
//!
//! The budget is required (P-27): with Python's `rest_time=None` the whole pipeline has no timeout protection, and
//! CF_ELEM's None path crashes outright at the timing print — the Rust entry takes a required f64 parameter,
//! eliminating the None branch; `expected_end_time` is always set.
//!
//! Python's entry identity assertion `ast_state is snapshot_rewriter.ast_state` is
//! guaranteed by construction in Rust (the applier and the context share one snapshot).

use std::path::PathBuf;
use std::time::SystemTime;

use anyhow::{bail, Result};
use e2wr_dd::factory::DdFactory;
use e2wr_dd::oracle::Oracle;
use e2wr_ir::ast::{Ast, NodeId};
use e2wr_ir::module::Module;
use e2wr_ir::snapshot::Snapshot;

use crate::instseq::cf_elem::remove_cf_elem;
use crate::instseq::cfn_stage::call_v9_cfn;
use crate::instseq::core_stage::call_v9_core;
use crate::instseq::elem::OneElem;
use crate::instseq::p3::reduce_insts_p3_graph_based_v7;
use crate::instseq::reduce_ctx::{build_one_node_list_reduction_ctx, get_raw_elems_from_env};
use crate::instseq::rev_stage::call_v9_rev;
use crate::instseq::trial::{deadline_after, deadline_passed, NodeListTrialApplier, StageApplier};
use crate::instseq::ur::reduce_by_unreachable_like_inst;

/// Python `OnlyOneInstTask` (the single-stage semantics of `--force_inst_mutation p3|core|rev`;
/// a process-level global in Python, an explicit parameter in Rust). Since R-11 the CLI/scheduler layer
/// also uses this enum directly for `--stage`/`--force_inst_mutation` (`Disable` =
/// all stages, the former InstMutationStage::Full).
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum OnlyOneInstTask {
    Core,
    P3,
    Rev,
    Disable,
}

/// The live field subset of Python `RNOpParam` (task/cur_input_path are U10-consumed fields,
/// added later; build_v6_cfg reads only the following three). `rest_time` is not in the struct — the production
/// chain passes it to `reduce_multi` as a separate parameter (R-10), as does the test entry.
#[derive(Clone, Debug)]
pub struct RNOpParam {
    pub cur_epoch_num: Option<i64>,
    pub total_run_times: Option<i64>,
    /// Python default True.
    pub enable_fast_mode: bool,
}

impl Default for RNOpParam {
    /// Python defaults: counter fields None, enable_fast_mode=True.
    fn default() -> Self {
        RNOpParam {
            cur_epoch_num: None,
            total_run_times: None,
            enable_fast_mode: true,
        }
    }
}

/// Python `ReduceNodeListV6CFG` (all-on default = the `ONE_V6_CFG` class-attribute initial values;
/// nobody writes that global — a read-only copy).
///
/// R-24 audit note: of the 64 combinations of the six switches, production can only reach the following
/// combinations constructible by `build_v6_cfg` (the rest are expressible but have no construction site) —
///
/// | # | source (only_one × fast/slow × whole_dd) | ddg_split | whole_dd | p3 | rev | cfg_reduce | cf |
/// |---|---|---|---|---|---|---|---|
/// | 1 | Core arm | T | T | F | F | F | T |
/// | 2 | P3 arm | T | F | T | F | F | T |
/// | 3 | Rev arm | T | F | F | T | F | T |
/// | 4 | Disable slow (whole_dd follows the `--disable_whole_dd` flag, default T) | T | flag | T | T | T | T |
/// | 5 | Disable fast × whole_dd=T | F | T | F | F | T | F |
/// | 6 | Disable fast × whole_dd=F | F | F | T | T | T | F |
///
/// In actual runs the default is `v6_whole_dd=True` and without `--force_inst_mutation`,
/// the main loop only takes #4 (#5 when fast mode triggers on the first task, total_run_times==0 and epoch<1).
/// The struct keeps all six fields' expressiveness — the m10_v9_parity truth-table
/// comparison asserts by field name, structure untouched (R-24 ruling).
#[derive(Clone, Copy, Debug)]
pub struct ReduceNodeListV6Cfg {
    pub enable_ddg_split: bool,
    pub enable_whole_dd: bool,
    pub enable_p3: bool,
    pub enable_rev: bool,
    pub enable_cfg_reduce: bool,
    pub use_cf_reduction: bool,
}

impl Default for ReduceNodeListV6Cfg {
    fn default() -> Self {
        ReduceNodeListV6Cfg {
            enable_ddg_split: true,
            enable_whole_dd: true,
            enable_p3: true,
            enable_rev: true,
            enable_cfg_reduce: true,
            use_cf_reduction: true,
        }
    }
}

impl ReduceNodeListV6Cfg {
    /// Python `disable_fine_ns`.
    pub fn disable_fine_ns(&mut self) {
        self.enable_p3 = false;
        self.enable_rev = false;
    }
}

/// Python `build_v6_cfg` (the FULL branch deleted; an uncovered OnlyOneInstTask value
/// reaches raise NotImplementedError → Rust eliminates that branch by enum exhaustiveness).
///
/// `v6_whole_dd` = the process-level global `ONE_V6_CFG.enable_whole_dd` made explicit
/// (Python's sole write site: script_run_reduce_v2.py's `--disable_whole_dd`;
/// in actual runs always True).
pub fn build_v6_cfg(
    op_param: &RNOpParam,
    only_one: OnlyOneInstTask,
    v6_whole_dd: bool,
) -> ReduceNodeListV6Cfg {
    match only_one {
        OnlyOneInstTask::Core => ReduceNodeListV6Cfg {
            enable_whole_dd: true,
            enable_p3: false,
            enable_rev: false,
            enable_cfg_reduce: false,
            ..ReduceNodeListV6Cfg::default()
        },
        OnlyOneInstTask::P3 => ReduceNodeListV6Cfg {
            enable_whole_dd: false,
            enable_p3: true,
            enable_rev: false,
            enable_cfg_reduce: false,
            ..ReduceNodeListV6Cfg::default()
        },
        OnlyOneInstTask::Rev => ReduceNodeListV6Cfg {
            enable_whole_dd: false,
            enable_p3: false,
            enable_rev: true,
            enable_cfg_reduce: false,
            ..ReduceNodeListV6Cfg::default()
        },
        OnlyOneInstTask::Disable => {
            // Python `_determine_fast_mode`: enable_fast_mode with both counter
            // fields given and total_run_times==0 and cur_epoch_num<1.
            let effective_fast_mode = op_param.enable_fast_mode
                && match (op_param.total_run_times, op_param.cur_epoch_num) {
                    (Some(0), Some(n)) => n < 1,
                    _ => false,
                };
            if !effective_fast_mode {
                ReduceNodeListV6Cfg {
                    enable_whole_dd: v6_whole_dd,
                    ..ReduceNodeListV6Cfg::default()
                }
            } else {
                let mut cfg = ReduceNodeListV6Cfg {
                    enable_whole_dd: v6_whole_dd,
                    ..ReduceNodeListV6Cfg::default()
                };
                if cfg.enable_whole_dd {
                    cfg.disable_fine_ns();
                    cfg.enable_p3 = false;
                }
                cfg.enable_ddg_split = false;
                cfg.use_cf_reduction = false;
                cfg
            }
        }
    }
}

/// Python `get_try_time`: ≤3000 → 300 s; ≤20000 → 900 s; otherwise 1800 s.
pub fn get_try_time(inst_num: u32) -> u32 {
    if inst_num <= 3000 {
        300
    } else if inst_num <= 20000 {
        900
    } else {
        1800
    }
}

/// Python `SMALL_NODE_TH = 50`: lists with more than 50 elements skip P3.
pub const SMALL_NODE_TH: usize = 50;

/// The result of reduce_multi (Python's final_result/cur_nodes as return values; the final-state
/// elements observable via `finalize_process`'s call parameters — returned explicitly here for comparison).
pub struct V9Outcome {
    pub final_result: bool,
    pub cur_nodes: Vec<NodeId>,
    pub final_elems: Vec<OneElem>,
}

/// Python `OneNodeListReducerV9`.
pub struct OneNodeListReducerV9 {
    debug: bool,
    reduction_cfg: ReduceNodeListV6Cfg,
    expected_end_time: Option<SystemTime>,
    has_reduced: bool,
}

impl OneNodeListReducerV9 {
    pub fn new(debug: bool, reduction_cfg: ReduceNodeListV6Cfg) -> OneNodeListReducerV9 {
        OneNodeListReducerV9 {
            debug,
            reduction_cfg,
            expected_end_time: None,
            has_reduced: false,
        }
    }

    // R-28: the same-shaped method-version timeout check time_exhausted was deleted; call sites uniformly use
    // trial::deadline_passed(self.expected_end_time).

    /// Python `_get_rest_time` / `_rest_time_now` (budget required → always Some).
    fn rest_time_now(&self) -> f64 {
        let deadline = self.expected_end_time.expect("deadline always set (P-27)");
        deadline
            .duration_since(SystemTime::now())
            .map(|d| d.as_secs_f64())
            // Already past: Python's expected_end - now is negative seconds; same semantics (an immediately-due
            // deadline; each stage's timeout check fires right after).
            .unwrap_or(0.0)
    }

    /// Python `reduce_multi` (D-11 single-list degeneration: nodelist2ctx /
    /// node_list2elems / NodeListElemInfo all degenerate to single values).
    ///
    /// `module`+`ast` are the **baseline view** (the equivalent of Python's elements referencing old AST node
    /// objects — the applier's internal tree updates after successful probes; element reads are unaffected);
    /// the applier is given by the caller (the real chain = [`NodeListTrialApplier`]; the test stub
    /// implements StageApplier separately).
    pub fn reduce_multi(
        &mut self,
        module: &Module,
        ast: &Ast,
        ori_node_list: NodeId,
        applier: &mut dyn StageApplier,
        dd_factory: &DdFactory,
        rest_time: f64,
    ) -> Result<V9Outcome> {
        if self.has_reduced {
            // Python raise RuntimeError('can only be called once').
            bail!("OneNodeListReducerV9.reduce_multi can only be called once");
        }
        // Python: filters lists with get_length()==0; all empty → early return (False, []).
        if ast.get_length(ori_node_list) == 0 {
            return Ok(V9Outcome {
                final_result: false,
                cur_nodes: Vec::new(),
                final_elems: Vec::new(),
            });
        }
        let total_inst_num = ast.get_length(ori_node_list);
        let rest_time = rest_time.min(get_try_time(total_inst_num) as f64);
        self.expected_end_time = Some(deadline_after(rest_time));

        // prepare infos (Python: build_one_node_list_reduction_ctx +
        // get_raw_elems_from_env).
        let ctx = build_one_node_list_reduction_ctx(module, ast, ori_node_list)?;
        let mut elems: Vec<OneElem> = get_raw_elems_from_env(module, ast, &ctx)?;
        let raw_elem_num = elems.len();

        // Python's `for _ in range(1)`: a single straight pass (a labeled block catches the break).
        'single_pass: {
            // Control flow guided ==========================================
            if self.reduction_cfg.enable_cfg_reduce {
                elems = reduce_by_unreachable_like_inst(&elems, applier, self.debug)?;
                elems = call_v9_cfn(
                    ast,
                    applier,
                    dd_factory,
                    &elems,
                    Some(self.rest_time_now()),
                )?;
                if deadline_passed(self.expected_end_time) {
                    break 'single_pass;
                }
            }
            let enable_ddg_split = self.reduction_cfg.enable_ddg_split;
            // V7 (CORE) ====================================================
            if self.reduction_cfg.enable_whole_dd {
                elems = call_v9_core(
                    &ctx,
                    ast,
                    applier,
                    dd_factory,
                    &elems,
                    enable_ddg_split,
                    Some(self.rest_time_now()),
                )?;
                if deadline_passed(self.expected_end_time) {
                    break 'single_pass;
                }
            }
            // CF_ELEM ======================================================
            // Python: use_cf_reduction and enable_whole_dd; the budget takes
            // _rest_time_now() (the None path crashes → required, P-27).
            if self.reduction_cfg.use_cf_reduction && self.reduction_cfg.enable_whole_dd {
                elems = remove_cf_elem(&ctx, &elems, applier, dd_factory, self.rest_time_now())?;
            }
            if deadline_passed(self.expected_end_time) {
                break 'single_pass;
            }
            // V7 REV =======================================================
            if self.reduction_cfg.enable_rev {
                elems = call_v9_rev(
                    &ctx,
                    ast,
                    applier,
                    dd_factory,
                    &elems,
                    Some(self.rest_time_now()),
                )?;
                if deadline_passed(self.expected_end_time) {
                    break 'single_pass;
                }
            }
            // P3 ============================================================
            // Python: len(elems) > SMALL_NODE_TH skips; the budget
            // min(_rest_time_now(), 30).
            if self.reduction_cfg.enable_p3 {
                if elems.len() <= SMALL_NODE_TH {
                    let p3_rest_time = self.rest_time_now().min(30.0);
                    elems = reduce_insts_p3_graph_based_v7(
                        &ctx,
                        ast,
                        applier,
                        dd_factory,
                        Some(p3_rest_time),
                        &elems,
                        self.debug,
                    )?;
                }
                if deadline_passed(self.expected_end_time) {
                    break 'single_pass;
                }
            }
            // final_check (a no-op besides the timeout check).
        }

        // Python finalize_process: the final count (elements with non-empty raw_index)
        // compared against the initial total; cur_nodes takes applier.actual_nodes.
        let cur_raw_elem_num = elems.iter().filter(|e| e.raw_index.is_some()).count();
        let final_result = cur_raw_elem_num < raw_elem_num;
        let cur_nodes = applier.actual_nodes();

        self.has_reduced = true;
        Ok(V9Outcome { final_result, cur_nodes, final_elems: elems })
    }
}

/// Python `reduce_node_list_by_insts_v9`: the real probe-chain entry (builds
/// a NodeListTrialApplier then runs [`OneNodeListReducerV9::reduce_multi`]).
/// Currently no production consumer (NodeShrinkPass assembles applier + reducer directly);
/// positioned as a **test entry** (the real-chain smoke test of its sole consumer, m10_v9_parity).
///
/// The baseline ast/module are built separately from the snapshot (the applier holds another evolving tree internally; element
/// reads go through the baseline view — equivalent to Python's elements referencing old node objects).
#[allow(clippy::too_many_arguments)]
pub fn reduce_node_list_by_insts_v9(
    ori_node_list: NodeId,
    snapshot: Snapshot,
    oracle: Oracle,
    tmp_used_path: PathBuf,
    op_param: &RNOpParam,
    only_one: OnlyOneInstTask,
    v6_whole_dd: bool,
    dd_factory: &DdFactory,
    rest_time: f64,
    debug: bool,
) -> Result<(bool, Vec<NodeId>)> {
    let mut applier =
        NodeListTrialApplier::new(ori_node_list, snapshot.clone(), oracle, tmp_used_path, debug)?;
    let module = snapshot.module();
    let ast = Ast::from_module(module)?;
    let mut reducer =
        OneNodeListReducerV9::new(debug, build_v6_cfg(op_param, only_one, v6_whole_dd));
    let V9Outcome { final_result, cur_nodes, .. } =
        reducer.reduce_multi(module, &ast, ori_node_list, &mut applier, dd_factory, rest_time)?;
    Ok((final_result, cur_nodes))
}
