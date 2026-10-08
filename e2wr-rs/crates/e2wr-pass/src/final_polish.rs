//! FinalPolishPass (M4 definition level + M11 remaining sub-steps).
//!
//! Mirrors the full reduce() orchestration of Python `ReduceFrameWork/FinalPolishPass.py` and
//! its four function-level sub-steps (inline / unreachable_replacement / polish_return_type / polish_local), plus their dependencies:
//! function-level sub-steps, and their dependencies:
//! - inline: `ReduceUtil/InlineFunction.py` (InlineFunctionHelper.inline_a_func,
//!   _generate_inline_mutations, _generate_inline_instructions,
//!   _combine_type_mutations, _get_called_ones_callee_and_caller live in this file);
//!   the dead-function deletion part = the rewrite_callsites=False form of
//!   RemoveDeadFunc.remove_dead_funcs (the inline scenario's sole call site is already expanded;
//!   Python does no rewriting for calls to deleted functions, leaving the oracle's rejection as the fallback;
//!   return_call **renumbering** follows call isomorphically per the settled P-20 extension, likewise left untouched
//!   when pointing at a deleted function);
//! - unreachable_replacement: `FinalPolishPassUtil/ReplaceFuncWithUnreachable.py`
//!   FuncNodesReplaceUnreachableReducer (ProbDD, task 'FuncR');
//! - polish_return_type: `RelaxRetyrnTypeReducer` (ProbDD, task 'RTY',
//!   deriving the minimal return type from stack requirements, padding with constants when necessary; changed functions then run
//!   FuncLevelNodeReducer);
//! - polish_local: `remove_useless_locals` (unused-local deletion + renumbering).
//!
//! Behavior notes:
//! - sub-step order: inline → unreachable_replacement → polish_return_type →
//!   polish_local → update_types → memory → elem_def → data → elem_seg;
//! - each step's exceptions are caught by the orchestration layer, printed, and skipped (the snapshot keeps its pre-step state),
//!   re-raised in DEBUG (`_call_with_catch_exception`);
//! - polish_return_type's analysis data (elements/stack requirements) comes from the module **as of entering the step**
//!   (Python builds ast_state once at the step entry); mutation-membership decisions and placement go by the
//!   current snapshot (types only append, functions are not deleted; the two views stay consistent);
//! - DataReducer's `_reduce_one_data_seg` (the ProbDD byte-deletion path) is dead code,
//!   not replicated (see the not-replicated list in the plan notes);
//! - reduced_size_num = the byte-size delta (different from UUR's count semantics).
//!
//! Phasing note: the first half of this file (FinalPolishDefPass) is the M4 definition-level five sub-steps;
//! the full orchestration is FinalPolishPass (M11).

use std::collections::{BTreeMap, BTreeSet, HashMap};
use std::path::Path;
use std::time::{Duration, Instant, SystemTime};

use anyhow::{Context, Result};
use rand::Rng;

use e2wr_dd::factory::DdFactory;
use e2wr_dd::oracle::Oracle;
use e2wr_dd::probdd::ProbDD;
use e2wr_ir::ast::{Ast, NodeKind};
use e2wr_ir::module::*;
use wasmparser::ValType;
use e2wr_ir::mutation::{
    apply_mutation_and_encode, data_def, elem_def, export_def, func_def, memory_def,
    type_def, u32_def, DefEdit, Mutation,
};
use e2wr_ir::snapshot::{SectionKind, Snapshot};
use e2wr_ir::stack_infer::cur_context_by_ast_info;
use e2wr_ir::types::{get_inst_ty_req, merge, StackState, StackStatus, TR, ValTy};
use e2wr_ir::Inst;

use crate::common::{DirSystem, ExecResult, ExecStatus};
use crate::detect;
use crate::node_rewriter::{gen_specific_type_insts, process_insts_blocktypes, valty_to_wp};
use crate::nodeshrink_pass::FuncLevelNodeReducer;
use crate::remap::{remap_all, DeleteSet};

const FINE_REDUCTION_TIMEOUT: f64 = 120.0;

/// R-18: the shared `mutate_test_and_commit` implementation (two verbatim-identical methods of
/// `FinalPolishDefPass` and `FinalPolishPass` merged; mirrors Python
/// `BaseReducer.mutate_test_and_commit`). `oracle_err_as_reject` expresses the oracle-error
/// semantics difference between the two entries:
/// - true (the Pass flavor): log and treat as not passing (Python `call_oracle` does not raise);
/// - false (the DefPass flavor): propagate via `?` — a deviation introduced during the port; Z-2 ruled to keep it.
///
/// D-14: after encoding to disk and before the oracle, debug-validate per `validate` (Python
/// `BaseReducer.debug_validate_tmp`, no exemption; the exempted flavor lives in the `mutate_and_save`
/// wrapper — Python's `_mutate_and_save` and this method differ in exemption semantics).
fn mutate_test_and_commit_shared(
    oracle: &Oracle,
    oracle_err_as_reject: bool,
    validate: crate::common::DebugValidate,
    dir: &DirSystem,
    snap: &Snapshot,
    mutations: &[Mutation],
    output: &Path,
) -> Result<Option<Snapshot>> {
    let new_snapshot = apply_mutation_and_encode(snap, mutations, &dir.tmp_used_path)?;
    crate::common::debug_validate_wasm_file(validate, &dir.tmp_used_path)?;
    let debug = validate.is_debug();
    let passed = if oracle_err_as_reject {
        match oracle.check(&dir.tmp_used_path) {
            Ok(v) => v,
            Err(e) => {
                if debug {
                    eprintln!("oracle error: {e:#}");
                }
                false
            }
        }
    } else {
        oracle.check(&dir.tmp_used_path)?
    };
    if passed {
        std::fs::copy(&dir.tmp_used_path, output)?;
        Ok(Some(new_snapshot))
    } else {
        Ok(None)
    }
}

/// The in-step timeout check (mirrors BaseReducerWithTimeout.is_timeout_with_system_time;
/// Python uses strict greater-than; with duration=0 the first round does not count as timeout).
struct Deadline(Option<Instant>);

impl Deadline {
    fn from_duration(duration: Option<f64>) -> Self {
        Deadline(duration.map(|d| Instant::now() + Duration::from_secs_f64(d)))
    }
    fn is_timeout(&self) -> bool {
        self.0.map(|t| Instant::now() > t).unwrap_or(false)
    }
}

/// R-20c: the orchestration layer's time-budget helpers — the `remaining`/`not_too_long` closure pair (previously
/// duplicated across DefPass::reduce and Pass::reduce). `not_too_long_time` is the int()-truncated form
/// of Python `get_not_too_long_remaining_timeout`.
#[derive(Clone, Copy)]
struct StepBudget {
    should_stop: Instant,
}

impl StepBudget {
    fn remaining(&self) -> f64 {
        (self.should_stop - Instant::now()).as_secs_f64().max(0.0)
    }
    fn not_too_long(&self, max_time: f64) -> f64 {
        max_time.min(self.remaining())
    }
    fn not_too_long_time(&self, max_time: f64) -> f64 {
        self.not_too_long(max_time).trunc()
    }
}

/// R-20a: the FinalPolishPass orchestration step-executor type (for table-driven dispatch).
type PolishStep<'s> = Box<dyn FnOnce(Snapshot) -> Result<Snapshot> + 's>;

/// R-20a: a step-table row = (name, switch, pre-print, after-print, executor).
type PolishStepRow<'s> = (&'static str, bool, Option<&'static str>, Option<&'static str>, PolishStep<'s>);

pub struct FinalPolishDefPass<'a> {
    oracle: &'a Oracle,
    dir: DirSystem,
    /// D-14 restoration (once cleaned as unread): consumed by debug-mode artifact validity assertions.
    debug: bool,
}

impl<'a> FinalPolishDefPass<'a> {
    pub fn new(oracle: &'a Oracle, result_dir: &Path, debug: bool) -> Self {
        FinalPolishDefPass { oracle, dir: DirSystem::new(result_dir), debug }
    }

    /// Mirrors the definition-level subset of FinalPolishPass.reduce.
    pub fn reduce(&self, input: &Path, output: &Path, timeout: f64) -> Result<ExecResult> {
        let start_time = Instant::now();
        let original_size = std::fs::metadata(input)?.len();
        // Stop time = timeout + min(15, max(3, timeout×5%)).
        let margin = (timeout * 0.05).clamp(3.0, 15.0);
        let should_stop = start_time + Duration::from_secs_f64(timeout + margin);

        std::fs::copy(input, output)?;
        let mut snapshot = Snapshot::from_path(output)?;

        let budget = StepBudget { should_stop };

        // update_types: the orchestration layer only checks the stop time, no per-step duration (same as Python).
        if budget.remaining() > 0.0 {
            snapshot = self.update_types(snapshot, output)?;
        }
        if budget.remaining() > 0.0 {
            snapshot = self.reduce_memory_def(snapshot, output, Some(budget.not_too_long(30.0)))?;
        }
        if budget.remaining() > 0.0 {
            snapshot = self.reduce_elem_def(snapshot, output, Some(budget.not_too_long(FINE_REDUCTION_TIMEOUT)))?;
        }
        if budget.remaining() > 0.0 {
            snapshot = self.reduce_data_seg(snapshot, output, Some(budget.not_too_long(FINE_REDUCTION_TIMEOUT)))?;
        }
        if budget.remaining() > 0.0 {
            // The last sub-step's returned snapshot is never used afterwards (the artifact is already committed to output).
            let _ = self.reduce_elem_seg(snapshot, output, Some(budget.not_too_long(FINE_REDUCTION_TIMEOUT)))?;
        }

        let final_size = std::fs::metadata(output)?.len();
        let is_partial = budget.remaining() <= 0.0;
        Ok(ExecResult {
            exec_status: ExecStatus::Success,
            exec_taken_time: start_time.elapsed().as_secs_f64(),
            reduced_size_num: Some(original_size as i64 - final_size as i64),
            reduced_inst_num: None,
            is_partial_by_timeout: is_partial,
        })
    }

    /// Mirrors BaseReducer.mutate_test_and_commit: apply mutations to a temp file,
    /// commit to output on oracle pass and return the new snapshot; None on failure.
    /// R-18: the body goes through the shared implementation; the DefPass flavor keeps the oracle-error `?` propagation
    /// (Z-2 ruled to keep it; a known deviation from Python call_oracle not raising).
    /// D-14: Strict validation (BaseReducer.debug_validate_tmp, no exemption).
    fn mutate_test_and_commit(
        &self,
        snap: &Snapshot,
        mutations: &[Mutation],
        output: &Path,
    ) -> Result<Option<Snapshot>> {
        mutate_test_and_commit_shared(
            self.oracle,
            false,
            crate::common::DebugValidate::strict(self.debug),
            &self.dir,
            snap,
            mutations,
            output,
        )
    }

    /// Mirrors FinalPolishPass._mutate_and_save (the update_types decision entry):
    /// same shape as mutate_test_and_commit, except the debug validation carries the elem
    /// undeclared-reference exemption (Python :441-447).
    fn mutate_and_save(
        &self,
        snap: &Snapshot,
        mutations: &[Mutation],
        output: &Path,
    ) -> Result<Option<Snapshot>> {
        mutate_test_and_commit_shared(
            self.oracle,
            false,
            crate::common::DebugValidate::exempt_elem(self.debug),
            &self.dir,
            snap,
            mutations,
            output,
        )
    }

    /// Mirrors update_types: a one-shot try-delete of all unused types (no ProbDD, a single verdict).
    /// D-14: Python update_types goes through _mutate_and_save (elem exemption).
    pub fn update_types(&self, snapshot: Snapshot, output: &Path) -> Result<Snapshot> {
        let unused = detect::detect_types(snapshot.module()).unused;
        if unused.is_empty() {
            return Ok(snapshot);
        }
        let del = DeleteSet { types: unused, ..Default::default() };
        let mutations = remap_all(snapshot.module(), &del)?;
        Ok(self.mutate_and_save(&snapshot, &mutations, output)?.unwrap_or(snapshot))
    }

    /// Mirrors reduce_memory_def (MemoryDefReducer): greedy + two binary searches.
    pub fn reduce_memory_def(
        &self,
        snapshot: Snapshot,
        output: &Path,
        duration_sec: Option<f64>,
    ) -> Result<Snapshot> {
        let deadline = Deadline::from_duration(duration_sec);
        let mut snapshot = snapshot;
        let total = snapshot.module().defined_memory_datas.len();
        for idx in 0..total {
            if deadline.is_timeout() {
                break;
            }
            let m = snapshot.module().defined_memory_datas[idx].clone();
            let min_ = m.ty.limits.min;
            let max_ = m.ty.limits.max;

            // step 0: try min=1, no upper bound, directly.
            if let Some(s) = self.try_replace_memory(&snapshot, idx as u32, 1, None, output)? {
                snapshot = s;
                continue;
            }
            // step 1: drop the upper bound of a bounded definition.
            let mut cur_max = max_;
            if max_.is_some() {
                if let Some(s) = self.try_replace_memory(&snapshot, idx as u32, min_, None, output)? {
                    snapshot = s;
                    cur_max = None;
                }
            }
            // Binary-search the minimal min.
            let mut bs_min: u64 = 1;
            let mut bs_max: u64 = min_;
            while bs_min < bs_max {
                if deadline.is_timeout() {
                    return Ok(snapshot);
                }
                let mid = (bs_min + bs_max) / 2;
                if let Some(s) =
                    self.try_replace_memory(&snapshot, idx as u32, mid, cur_max, output)?
                {
                    bs_max = mid;
                    snapshot = s;
                } else {
                    bs_min = mid + 1;
                }
            }
            let cur_min = bs_min;
            // Binary-search the minimal max (when still bounded).
            if let Some(mx) = cur_max {
                let mut bs_min = cur_min;
                let mut bs_max = mx;
                while bs_min < bs_max {
                    if deadline.is_timeout() {
                        return Ok(snapshot);
                    }
                    let mid = (bs_min + bs_max) / 2;
                    if let Some(s) =
                        self.try_replace_memory(&snapshot, idx as u32, cur_min, Some(mid), output)?
                    {
                        bs_max = mid;
                        snapshot = s;
                    } else {
                        bs_min = mid + 1;
                    }
                }
            }
        }
        Ok(snapshot)
    }

    fn try_replace_memory(
        &self,
        snapshot: &Snapshot,
        idx: u32,
        min_: u64,
        max_: Option<u64>,
        output: &Path,
    ) -> Result<Option<Snapshot>> {
        let new_mem = Memory { ty: MemType { limits: Limits { min: min_, max: max_ } } };
        let raw = memory_def(&new_mem)?;
        let mutations = vec![Mutation::definitions(
            SectionKind::Memory,
            vec![DefEdit::replace_one(idx, raw)],
        )];
        self.mutate_test_and_commit(snapshot, &mutations, output)
    }

    /// Mirrors reduce_elem_def (ElemRewriteReducer): the expression form (each entry exactly one
    /// ref.func) is rewritten to the function-index shorthand form; others skipped. Traversed in original section order.
    pub fn reduce_elem_def(
        &self,
        snapshot: Snapshot,
        output: &Path,
        duration_sec: Option<f64>,
    ) -> Result<Snapshot> {
        let deadline = Deadline::from_duration(duration_sec);
        let mut snapshot = snapshot;
        let total = snapshot.module().elem_sec_datas.len();
        for idx in 0..total {
            if deadline.is_timeout() {
                break;
            }
            let seg = snapshot.module().elem_sec_datas[idx].clone();
            let new_seg = match &seg.payload {
                ElemPayload::FuncIdxs(_) => None,
                ElemPayload::Exprs { exprs, .. } => {
                    let mut idxs = Vec::with_capacity(exprs.len());
                    let mut all_ref_func = true;
                    for e in exprs {
                        // One ref.func + trailing end (Rust-decoded constant expressions keep the
                        // trailing end byte; on the Python side each expression is exactly one instruction).
                        match e.as_slice() {
                            [Inst::RefFunc { function_index }, Inst::End] => {
                                idxs.push(*function_index);
                            }
                            _ => {
                                all_ref_func = false;
                                break;
                            }
                        }
                    }
                    if all_ref_func {
                        Some(ElemSeg { mode: seg.mode.clone(), payload: ElemPayload::FuncIdxs(idxs) })
                    } else {
                        None
                    }
                }
            };
            if let Some(ns) = new_seg {
                let raw = elem_def(&ns)?;
                let mutations = vec![Mutation::definitions(
                    SectionKind::Element,
                    vec![DefEdit::replace_one(idx as u32, raw)],
                )];
                if let Some(s) = self.mutate_test_and_commit(&snapshot, &mutations, output)? {
                    snapshot = s;
                }
            }
        }
        Ok(snapshot)
    }

    /// R-20b: the "length-descending (stable) → empty-segment probe → prefix binary search" shared skeleton (previously
    /// duplicated across reduce_data_seg and reduce_elem_seg). `get_orig` materializes a segment's original
    /// material on entering it (later commits do not change the prefix source); `try_n` probes a segment
    /// with "keep the first n entries" (n=0 is the empty-segment probe).
    fn reduce_by_prefix_bisect<T>(
        &self,
        snapshot: Snapshot,
        output: &Path,
        duration_sec: Option<f64>,
        lens: Vec<usize>,
        get_orig: impl Fn(&Snapshot, usize) -> T,
        try_n: impl Fn(&Self, &T, &Snapshot, usize, usize, &Path) -> Result<Option<Snapshot>>,
    ) -> Result<Snapshot> {
        let deadline = Deadline::from_duration(duration_sec);
        let mut snapshot = snapshot;
        let mut order: Vec<usize> = (0..lens.len()).collect();
        // Python sorted(range, key=len, reverse=True): stable sort; equal lengths keep original order.
        order.sort_by_key(|&i| std::cmp::Reverse(lens[i]));
        for idx in order {
            if deadline.is_timeout() {
                break;
            }
            let orig = get_orig(&snapshot, idx);
            let total = lens[idx];

            // Empty-segment probe.
            if let Some(s) = try_n(self, &orig, &snapshot, idx, 0, output)? {
                snapshot = s;
                if deadline.is_timeout() {
                    return Ok(snapshot);
                }
                continue;
            }
            // Prefix binary search.
            let (mut left, mut right) = (1usize, total);
            while left <= right {
                if deadline.is_timeout() {
                    return Ok(snapshot);
                }
                let mid = (left + right) / 2;
                if let Some(s) = try_n(self, &orig, &snapshot, idx, mid, output)? {
                    snapshot = s;
                    right = mid - 1;
                } else {
                    left = mid + 1;
                }
            }
        }
        Ok(snapshot)
    }

    /// Mirrors reduce_data_seg (DataReducer). The prefix takes the original bytes as of entering the segment.
    pub fn reduce_data_seg(
        &self,
        snapshot: Snapshot,
        output: &Path,
        duration_sec: Option<f64>,
    ) -> Result<Snapshot> {
        let lens: Vec<usize> = snapshot
            .module()
            .data_sec_datas
            .iter()
            .map(|d| d.data.len())
            .collect();
        self.reduce_by_prefix_bisect(
            snapshot,
            output,
            duration_sec,
            lens,
            |snap, idx| {
                let d = &snap.module().data_sec_datas[idx];
                (d.data.clone(), d.mode.clone())
            },
            |p, orig, snap, idx, n, out| {
                p.try_replace_data(snap, idx as u32, &orig.0[..n], &orig.1, out)
            },
        )
    }

    fn try_replace_data(
        &self,
        snapshot: &Snapshot,
        idx: u32,
        bytes: &[u8],
        mode: &DataMode,
        output: &Path,
    ) -> Result<Option<Snapshot>> {
        let new_data = DataSeg { mode: mode.clone(), data: bytes.to_vec() };
        let raw = data_def(&new_data)?;
        let mutations = vec![Mutation::definitions(
            SectionKind::Data,
            vec![DefEdit::replace_one(idx, raw)],
        )];
        self.mutate_test_and_commit(snapshot, &mutations, output)
    }

    /// Mirrors reduce_elem_seg (ElemReducer): length-descending (stable) → empty-segment probe →
    /// prefix binary search over the function-index/expression list.
    pub fn reduce_elem_seg(
        &self,
        snapshot: Snapshot,
        output: &Path,
        duration_sec: Option<f64>,
    ) -> Result<Snapshot> {
        let lens: Vec<usize> = snapshot
            .module()
            .elem_sec_datas
            .iter()
            .map(elemseg_len)
            .collect();
        self.reduce_by_prefix_bisect(
            snapshot,
            output,
            duration_sec,
            lens,
            |snap, idx| snap.module().elem_sec_datas[idx].clone(),
            |p, orig, snap, idx, n, out| {
                let payload = match &orig.payload {
                    ElemPayload::FuncIdxs(idxs) => ElemPayload::FuncIdxs(idxs[..n].to_vec()),
                    ElemPayload::Exprs { elem_ty, exprs } => ElemPayload::Exprs {
                        elem_ty: *elem_ty,
                        exprs: exprs[..n].to_vec(),
                    },
                };
                let raw =
                    elem_def(&ElemSeg { mode: orig.mode.clone(), payload }).expect("encode elem");
                let mutations = vec![Mutation::definitions(
                    SectionKind::Element,
                    vec![DefEdit::replace_one(idx as u32, raw)],
                )];
                p.mutate_test_and_commit(snap, &mutations, out)
            },
        )
    }
}

fn elemseg_len(seg: &ElemSeg) -> usize {
    match &seg.payload {
        ElemPayload::FuncIdxs(idxs) => idxs.len(),
        ElemPayload::Exprs { exprs, .. } => exprs.len(),
    }
}

// ===========================================================================
// M11: the remaining FinalPolish sub-steps (inline / unreachable replacement / return type / locals)
// ===========================================================================

/// Python `FinalPolishPass` construction parameters made explicit (defaults = the actual
/// script_run_reduce_v2 values: all three enabled; the `--disable_polish_return/--disable_inline/
/// --disable_size_polish` flags map to the negated fields).
#[derive(Debug, Clone, Copy)]
pub struct FinalPolishConfig {
    pub polish_return_type: bool,
    pub enable_inline: bool,
    pub enable_size_polish: bool,
}

impl Default for FinalPolishConfig {
    fn default() -> Self {
        FinalPolishConfig {
            polish_return_type: true,
            enable_inline: true,
            enable_size_polish: true,
        }
    }
}

/// The full FinalPolishPass (the whole orchestration of Python `FinalPolishPass.reduce`).
pub struct FinalPolishPass<'a> {
    oracle: &'a Oracle,
    dd_factory: DdFactory,
    dir: DirSystem,
    cfg: FinalPolishConfig,
    debug: bool,
    /// The process-level globals (--force_inst_mutation / --disable_whole_dd) made explicit,
    /// passed into FuncLevelNodeReducer via the polish_return_type step (M12 wiring).
    v6_only_one: crate::instseq::v9::OnlyOneInstTask,
    v6_whole_dd: bool,
}

impl<'a> FinalPolishPass<'a> {
    pub fn new(
        oracle: &'a Oracle,
        dd_factory: DdFactory,
        result_dir: &Path,
        cfg: FinalPolishConfig,
        debug: bool,
    ) -> FinalPolishPass<'a> {
        FinalPolishPass {
            oracle,
            dd_factory,
            dir: DirSystem::new(result_dir),
            cfg,
            debug,
            v6_only_one: crate::instseq::v9::OnlyOneInstTask::Disable,
            v6_whole_dd: true,
        }
    }

    /// M12: the wiring entry for --force_inst_mutation / --disable_whole_dd; chain construction.
    pub fn with_v6(
        mut self,
        only_one: crate::instseq::v9::OnlyOneInstTask,
        v6_whole_dd: bool,
    ) -> Self {
        self.v6_only_one = only_one;
        self.v6_whole_dd = v6_whole_dd;
        self
    }

    /// Python `FinalPolishPass.reduce` (the @reduce_checker input-oracle check is borne
    /// by the caller/CLI).
    pub fn reduce(&self, input: &Path, output: &Path, timeout: f64) -> Result<ExecResult> {
        let start_time = Instant::now();
        let original_size = std::fs::metadata(input)?.len();
        // Stop time = timeout + min(15, max(3, timeout×5%)).
        let margin = (timeout * 0.05).clamp(3.0, 15.0);
        let should_stop = start_time + Duration::from_secs_f64(timeout + margin);

        std::fs::copy(input, output)?;
        let mut snapshot = Snapshot::from_path(output)?;

        let budget = StepBudget { should_stop };
        let skip = |step: &str| {
            if budget.remaining() <= 0.0 {
                println!("Skip {step} due to timeout guard");
                true
            } else {
                false
            }
        };

        // R-20a: table-driven straightening — the original 9 "skip gate + execute + print" if-chains
        // merged into one step table; each row = (name, switch, pre-print, after-print, executor).
        // Evaluation order verbatim-identical to the original chain: first skip(name) (prints the Skip line, short-circuits on the
        // switch) → pre-print → execute → after-print; the after-print is independent of whether the step actually
        // ran, and on execution error is skipped via `?` (as before).
        // The definition-level five sub-steps (M4, FinalPolishDefPass; sharing the same result_dir/tmp paths).
        let def = FinalPolishDefPass::new(self.oracle, &self.dir.tmp_dir, self.debug);
        let steps: Vec<PolishStepRow<'_>> = vec![
            (
                "inline",
                self.cfg.enable_inline,
                None,
                Some("inline replacement"),
                Box::new(|s| self.step("inline", output, s, |p, o, s| {
                    p.inline_step(o, s, should_stop)
                })),
            ),
            (
                "unreachable_replacement",
                self.cfg.enable_size_polish,
                None,
                None,
                Box::new(|s| self.step("unreachable_replacement", output, s, |p, o, s| {
                    p.unreachable_replacement_step(o, s)
                })),
            ),
            (
                "polish_return_type",
                self.cfg.polish_return_type,
                None,
                Some("polish_return_type"),
                Box::new(|s| self.step("polish_return_type", output, s, |p, o, s| {
                    p.polish_return_type_step(o, s, should_stop)
                })),
            ),
            (
                "polish_local",
                self.cfg.enable_size_polish,
                None,
                Some("polish_local"),
                Box::new(|s| self.step("polish_local", output, s, |p, o, s| {
                    p.polish_local_step(o, s, should_stop)
                })),
            ),
            (
                "update_types",
                true,
                None,
                Some("update_types"),
                Box::new(|s| def.update_types(s, output)),
            ),
            (
                "reduce_memory_def",
                self.cfg.enable_size_polish,
                Some("Will reduce memory def"),
                Some("reduce_memory_def"),
                Box::new(|s| def.reduce_memory_def(s, output, Some(budget.not_too_long(30.0)))),
            ),
            (
                "reduce_elem_def",
                self.cfg.enable_size_polish,
                Some("Will reduce elem def"),
                None,
                Box::new(|s| {
                    def.reduce_elem_def(s, output, Some(budget.not_too_long(FINE_REDUCTION_TIMEOUT)))
                }),
            ),
            (
                "reduce_data_seg",
                self.cfg.enable_size_polish,
                Some("Will reduce data seg"),
                None,
                Box::new(|s| {
                    def.reduce_data_seg(s, output, Some(budget.not_too_long(FINE_REDUCTION_TIMEOUT)))
                }),
            ),
            (
                "reduce_elem_seg",
                self.cfg.enable_size_polish,
                Some("Will reduce elem seg"),
                None,
                // The last sub-step's returned snapshot is never used afterwards (the artifact is already committed to output).
                Box::new(|s| {
                    def.reduce_elem_seg(s, output, Some(budget.not_too_long(FINE_REDUCTION_TIMEOUT)))
                }),
            ),
        ];
        for (name, enabled, pre, after, run) in steps {
            if !skip(name) && enabled {
                if let Some(pre) = pre {
                    println!("{pre}");
                }
                snapshot = run(snapshot)?;
            }
            if let Some(after) = after {
                println!(
                    "After {after}, current output size : {}",
                    std::fs::metadata(output)?.len()
                );
            }
        }

        let final_size = std::fs::metadata(output)?.len();
        Ok(ExecResult {
            exec_status: ExecStatus::Success,
            exec_taken_time: start_time.elapsed().as_secs_f64(),
            reduced_size_num: Some(original_size as i64 - final_size as i64),
            reduced_inst_num: None,
            is_partial_by_timeout: budget.remaining() <= 0.0,
        })
    }

    /// Python `_call_with_catch_exception`: a step's exception is printed and the pre-step snapshot is kept
    /// (full in-memory rollback; the output file may rest at an in-step intermediate commit, same as Python),
    /// re-raised in DEBUG.
    fn step(
        &self,
        name: &str,
        output: &Path,
        snapshot: Snapshot,
        f: impl FnOnce(&Self, &Path, Snapshot) -> Result<Snapshot>,
    ) -> Result<Snapshot> {
        let backup = snapshot.clone();
        match f(self, output, snapshot) {
            Ok(s) => Ok(s),
            Err(e) => {
                eprintln!("Error in {name}: {e:#}");
                if self.debug {
                    return Err(e);
                }
                Ok(backup)
            }
        }
    }

    /// Oracle verdict (exceptions treated as not passing; Python call_oracle does not raise).
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

    /// Python `BaseReducer.mutate_test_and_commit` (no on_commit — Rust snapshot application
    /// is the merge; Python's merge_mutations_to_parser synchronizes shared mutable state).
    /// R-18: the body goes through the shared implementation; oracle errors are logged and treated as not passing
    /// (Python call_oracle does not raise). D-14: Strict validation (no exemption).
    fn mutate_test_and_commit(
        &self,
        snap: &Snapshot,
        mutations: &[Mutation],
        output: &Path,
    ) -> Result<Option<Snapshot>> {
        mutate_test_and_commit_shared(
            self.oracle,
            true,
            crate::common::DebugValidate::strict(self.debug),
            &self.dir,
            snap,
            mutations,
            output,
        )
    }

    /// Python `FinalPolishPass._mutate_and_save` (the decision entry of inline / polish_local):
    /// same shape as mutate_test_and_commit, except the debug validation carries the elem
    /// undeclared-reference exemption (Python :437-443).
    fn mutate_and_save(
        &self,
        snap: &Snapshot,
        mutations: &[Mutation],
        output: &Path,
    ) -> Result<Option<Snapshot>> {
        mutate_test_and_commit_shared(
            self.oracle,
            true,
            crate::common::DebugValidate::exempt_elem(self.debug),
            &self.dir,
            snap,
            mutations,
            output,
        )
    }

    // ------------------------------------------------------------------
    // inline
    // ------------------------------------------------------------------

    /// Python `FinalPolishPass.inline`: candidates (defined functions called exactly once)
    /// inlined in a loop, candidates recomputed on success; breaks on timeout.
    pub fn inline_step(
        &self,
        output: &Path,
        input_sp: Snapshot,
        should_stop: Instant,
    ) -> Result<Snapshot> {
        let mut snapshot = input_sp;
        let mut candidates = get_called_ones_callee_and_caller(snapshot.module());
        while let Some((caller_func_idx, callee_func_idx)) = candidates.pop() {
            if (should_stop - Instant::now()).as_secs_f64() <= 0.0 {
                println!("Skip inline loop due to timeout guard");
                break;
            }
            println!(
                "inlined caller_func_idx: {caller_func_idx} ;;  callee_func_idx : {callee_func_idx} ;; rest num : {}",
                candidates.len()
            );
            let mutations =
                gen_inline_mutations(snapshot.module(), caller_func_idx, callee_func_idx)?;
            if let Some(new_snap) = self.mutate_and_save(&snapshot, &mutations, output)? {
                snapshot = new_snap;
                candidates = get_called_ones_callee_and_caller(snapshot.module());
            }
        }
        Ok(snapshot)
    }

    // ------------------------------------------------------------------
    // unreachable replacement
    // ------------------------------------------------------------------

    /// Python `remove_funcs_by_replace_unreachable` / FuncNodesReplaceUnreachableReducer:
    /// Candidates = defined functions with ≥2 instructions (body length excludes the trailing end); ProbDD converges on a must-keep set,
    /// deleted functions are replaced wholesale by an empty body (no-result form) or a single unreachable (result form),
    /// locals cleared; every pass commits (each success writes output); after convergence one final
    /// verification commit per the final deletion set.
    pub fn unreachable_replacement_step(&self, output: &Path, input_sp: Snapshot) -> Result<Snapshot> {
        let module = input_sp.module();
        let candidates: Vec<u32> = module
            .defined_funcs
            .iter()
            .enumerate()
            .filter(|(_, f)| f.insts.len().saturating_sub(1) >= 2)
            .map(|(i, _)| i as u32)
            .collect();
        let all: BTreeSet<u32> = candidates.iter().copied().collect();
        if all.is_empty() {
            return Ok(input_sp);
        }
        // First-error short-circuit flag (the error itself is recorded and re-raised by run_probdd_capturing).
        let mut build_failed = false;
        let mut try_remove = |keep: &[u32]| -> Result<bool> {
            if build_failed {
                return Ok(false);
            }
            let keep_set: BTreeSet<u32> = keep.iter().copied().collect();
            let to_delete: BTreeSet<u32> = all.difference(&keep_set).copied().collect();
            let mutations = match gen_unreachable_replacement_mutations(module, &to_delete) {
                Ok(m) => m,
                Err(e) => {
                    build_failed = true;
                    return Err(e);
                }
            };
            match apply_mutation_and_encode(&input_sp, &mutations, &self.dir.tmp_used_path) {
                Ok(_) => Ok(if self.oracle_check(&self.dir.tmp_used_path) {
                    let _ = std::fs::copy(&self.dir.tmp_used_path, output);
                    true
                } else {
                    false
                }),
                Err(e) => {
                    // Python: encode exceptions propagate through delta debugging and are caught by the pass-level catch.
                    build_failed = true;
                    Err(e)
                }
            }
        };
        let mut dd: ProbDD<u32> = self.dd_factory.create_probdd();
        let minimal_config =
            crate::common::run_probdd_capturing(&mut dd, &candidates, None, None, &mut try_remove)
                .map_err(|e| e.context("unreachable_replacement try_remove failed"))?;
        let minimal_set: BTreeSet<u32> = minimal_config.iter().copied().collect();
        let removed_idxs: BTreeSet<u32> = all.difference(&minimal_set).copied().collect();
        if removed_idxs.is_empty() {
            return Ok(input_sp);
        }
        let final_mutations = gen_unreachable_replacement_mutations(module, &removed_idxs)?;
        match self.mutate_test_and_commit(&input_sp, &final_mutations, output)? {
            Some(s) => Ok(s),
            None => Ok(input_sp),
        }
    }

    // ------------------------------------------------------------------
    // polish_return_type
    // ------------------------------------------------------------------

    /// Python `FinalPolishPass.polish_return_type`: uncalled functions each run
    /// RelaxRetyrnTypeReducer (ProbDD element deletion + stack-requirement-derived minimal return type +
    /// constant padding); changed functions then run FuncLevelNodeReducer.
    pub fn polish_return_type_step(
        &self,
        output: &Path,
        input_sp: Snapshot,
        should_stop: Instant,
    ) -> Result<Snapshot> {
        let to_stop_time = Some(should_stop);
        let mut ml = input_sp;
        // Analysis data source = the module as of entering this step (Python builds ast_state only once).
        let analysis_module = ml.module().clone();
        let analysis_ast = Ast::from_module(&analysis_module)?;
        let un_called_func_idxs = get_un_called_defined_func_idxs(&analysis_module);
        let mut modified_func_idxs: BTreeSet<u32> = BTreeSet::new();
        let mut rng = rand::thread_rng();
        for func_idx in un_called_func_idxs {
            if (should_stop - Instant::now()).as_secs_f64() <= 0.0 {
                println!("Skip polish_return_type loop due to timeout guard");
                break;
            }
            let mut analysis =
                build_rty_analysis(&analysis_module, &analysis_ast, func_idx)?;
            let new_ml = reduce_one_func_return_type(
                self,
                &mut analysis,
                &ml,
                func_idx,
                output,
                to_stop_time,
                &mut rng,
            )?;
            if let Some(s) = new_ml {
                modified_func_idxs.insert(func_idx);
                ml = s;
            }
        }
        println!(
            "[XX] Size After polish_return_type: {}",
            std::fs::metadata(output)?.len()
        );
        if !modified_func_idxs.is_empty()
            && (should_stop - Instant::now()).as_secs_f64() > 0.0
        {
            let reducer = FuncLevelNodeReducer::new(
                self.oracle,
                self.dd_factory.clone(),
                &self.dir.tmp_dir,
                self.debug,
            )
            .with_v6(self.v6_only_one, self.v6_whole_dd);
            let timeout = StepBudget { should_stop }.not_too_long_time(300.0);
            ml = reducer.reduce(output, &ml, &modified_func_idxs, timeout)?;
        }
        Ok(ml)
    }

    // ------------------------------------------------------------------
    // polish_local
    // ------------------------------------------------------------------

    /// Python `FinalPolishPass.polish_local`: `remove_useless_locals`
    /// Precomputes every function's (renumber map, local table) and probes-commit one by one in ascending function order.
    pub fn polish_local_step(
        &self,
        output: &Path,
        input_sp: Snapshot,
        should_stop: Instant,
    ) -> Result<Snapshot> {
        let mut snapshot = input_sp;
        // Precomputed (Python remove_useless_locals computes all of it once outside the loop).
        let per_func = remove_useless_locals(snapshot.module());
        for (func_idx, (renum, new_locals)) in per_func {
            if (should_stop - Instant::now()).as_secs_f64() <= 0.0 {
                println!("Skip polish_local loop due to timeout guard");
                break;
            }
            let Some(new_locals) = new_locals else {
                continue;
            };
            let module = snapshot.module();
            let param_num = module.types[module.defined_func_ty_ids[func_idx as usize] as usize]
                .params
                .len() as u32;
            let mut new_func = module.defined_funcs[func_idx as usize].clone();
            new_func.locals = new_locals;
            for inst in new_func.insts.iter_mut() {
                match inst {
                    Inst::LocalGet { local_index }
                    | Inst::LocalSet { local_index }
                    | Inst::LocalTee { local_index } => {
                        if *local_index >= param_num {
                            *local_index = *renum
                                .get(local_index)
                                .context("polish_local: used local missing in renumber map")?;
                        }
                    }
                    _ => {}
                }
            }
            let mutations = vec![Mutation::definitions(
                SectionKind::Code,
                vec![DefEdit::replace_one(func_idx, func_def(&new_func)?)],
            )];
            if let Some(s) = self.mutate_and_save(&snapshot, &mutations, output)? {
                snapshot = s;
            }
        }
        println!(
            "After local reduction, current output size : {}",
            std::fs::metadata(output)?.len()
        );
        Ok(snapshot)
    }
}

// ---------------------------------------------------------------------------
// inline helpers (Python InlineFunction.py + the two collection functions of FinalPolishPass)
// ---------------------------------------------------------------------------


/// Python `_get_called_ones_callee_and_caller`: the caller list per defined function
/// (duplicates included — one caller calling multiple times counts per call); the exactly-once-called ones are collected as
/// (caller, callee). Key order = defined function index ascending (the Python dict pre-injects 0..n-1;
/// imported callees' negative keys always have callers and never enter the result). Callers pop from the tail.
pub fn get_called_ones_callee_and_caller(module: &Module) -> Vec<(u32, u32)> {
    let import_num = module.import_func_num();
    let n = module.defined_funcs.len();
    let mut callers: Vec<Vec<u32>> = vec![Vec::new(); n];
    for (fi, f) in module.defined_funcs.iter().enumerate() {
        let body_end = f.insts.len().saturating_sub(1);
        for inst in &f.insts[..body_end] {
            if let Inst::Call { function_index } = inst {
                let defined = *function_index as i64 - import_num as i64;
                if defined < 0 {
                    continue;
                }
                let du = defined as usize;
                if du < n {
                    callers[du].push(fi as u32);
                }
            }
        }
    }
    callers
        .into_iter()
        .enumerate()
        .filter(|(_, cs)| cs.len() == 1)
        .map(|(callee, cs)| (cs[0], callee as u32))
        .collect()
}

/// Python `_get_un_called_defined_func_idxs`: defined functions no defined function calls
/// (functions reachable only via export/start count as uncalled too).
pub fn get_un_called_defined_func_idxs(module: &Module) -> Vec<u32> {
    let import_num = module.import_func_num();
    let n = module.defined_funcs.len();
    let mut called = vec![false; n];
    for f in &module.defined_funcs {
        let body_end = f.insts.len().saturating_sub(1);
        for inst in &f.insts[..body_end] {
            if let Inst::Call { function_index } = inst {
                let defined = *function_index as i64 - import_num as i64;
                if (0..n as i64).contains(&defined) {
                    called[defined as usize] = true;
                }
            }
        }
    }
    (0..n as u32).filter(|i| !called[*i as usize]).collect()
}

/// The synthetic block type table (sequence position → params/results); see the same-named concept in node_rewriter.
type SyntheticBt = BTreeMap<usize, (Vec<ValType>, Vec<ValType>)>;

/// Python `_generate_inline_instructions`: builds the expansion sequence of the inlined function body.
/// Returns (instruction sequence, the synthetic blocks' position table) — a synthetic block corresponds to Python's
/// `Blocktype(funcType object)` (the unresolved form), handed to process_insts_blocktypes to go through
/// the same normalization chain as InstsReplacement.
fn generate_inline_instructions(
    inlined_body: &[Inst],
    inlined_ty: &FuncType,
    original_local_count: u32,
    actual_inlined_func_idx: u32,
    inline_func_has_return: bool,
    need_init_inlined_locals: bool,
) -> (Vec<Inst>, SyntheticBt) {
    let mut instructions: Vec<Inst> = Vec::new();
    let mut synthetic = BTreeMap::new();
    // Parameter popping: from the last parameter first (the top of the stack is the last parameter).
    for i in (0..inlined_ty.params.len() as u32).rev() {
        let param_local_idx = original_local_count + i;
        if need_init_inlined_locals {
            instructions.push(Inst::LocalSet { local_index: param_local_idx });
        } else {
            instructions.push(Inst::Drop);
        }
    }
    if inline_func_has_return {
        // Blocktype(funcType([], result_types)) — a synthetic block type.
        let results: Vec<ValType> =
            inlined_ty.results.iter().map(|t| valty_to_wp(&ValTy::from_wasmparser(*t).expect("unsupported valtype"))).collect();
        synthetic.insert(instructions.len(), (Vec::new(), results));
        instructions.push(Inst::Block { blockty: wasmparser::BlockType::Empty });
    }
    let mut cur_depth: u32 = 0;
    for inst in inlined_body {
        match inst {
            Inst::Block { .. } | Inst::Loop { .. } | Inst::If { .. } => cur_depth += 1,
            Inst::End => cur_depth = cur_depth.saturating_sub(1),
            _ => {}
        }
        match inst {
            Inst::Return => instructions.push(Inst::Br { relative_depth: cur_depth }),
            Inst::LocalGet { local_index } => instructions.push(Inst::LocalGet {
                local_index: *local_index + original_local_count,
            }),
            Inst::LocalSet { local_index } => instructions.push(Inst::LocalSet {
                local_index: *local_index + original_local_count,
            }),
            Inst::LocalTee { local_index } => instructions.push(Inst::LocalTee {
                local_index: *local_index + original_local_count,
            }),
            Inst::Call { function_index } => {
                if *function_index > actual_inlined_func_idx {
                    instructions.push(Inst::Call { function_index: function_index - 1 });
                } else {
                    instructions.push(inst.clone());
                }
            }
            _ => instructions.push(inst.clone()),
        }
    }
    if inline_func_has_return {
        instructions.push(Inst::End);
    }
    (instructions, synthetic)
}

/// Python `InlineFunctionHelper.inline_a_func` + `remove_dead_funcs(
/// rewrite_callsites=False)` as one integrated mutation generator.
///
/// Structure notes (vs Python's mutation division):
/// - callsite replacement (FuncInstMutation) + function-number shifts elsewhere
///   (remove_dead_funcs' renumbering mutations) merge into whole-function re-encoding Code-section edits;
/// - Python does **no rewriting** for a call pointing at a deleted function when rewrite_callsites=False
///   (dangling calls remain, rejected by the oracle as the fallback; here the sole call site is already expanded,
///   leftovers can only come from uncounted return_calls); return_call renumbering follows call isomorphically
///   per the settled P-20 extension, likewise left untouched when pointing at a deleted function;
/// - ref.func pointing at a deleted function → repoint at the first kept function post-deletion (= the imported-function count,
///   no imports deleted); other references shift by whole space (above the deleted number get -1);
/// - appended types (synthetic block types not expressible in short encodings during expansion) concatenate at the type section's end.
#[allow(clippy::too_many_arguments)]
fn gen_inline_mutations(
    module: &Module,
    caller_idx: u32,
    callee_idx: u32,
) -> Result<Vec<Mutation>> {
    let import_num = module.import_func_num() as u32;
    let actual = import_num + callee_idx;
    let inlined = &module.defined_funcs[callee_idx as usize];
    let inlined_ty = &module.types[module.defined_func_ty_ids[callee_idx as usize] as usize];
    let body_end = inlined.insts.len().saturating_sub(1);
    let inlined_body = &inlined.insts[..body_end];

    let caller = &module.defined_funcs[caller_idx as usize];
    let caller_param_num =
        module.types[module.defined_func_ty_ids[caller_idx as usize] as usize].params.len() as u32;
    let original_local_count = caller_param_num + caller.locals.len() as u32;

    // Python: `any(t.opcode_text.startswith('local'))` (only local.get/set/tee).
    let uses_locals = inlined_body
        .iter()
        .any(|i| matches!(i, Inst::LocalGet { .. } | Inst::LocalSet { .. } | Inst::LocalTee { .. }));
    let has_return = inlined_body.iter().any(|i| matches!(i, Inst::Return));

    // The new local table = caller's defined locals + the inlined function's params (as locals) + its defined locals.
    let new_locals: Option<Vec<ValType>> = uses_locals.then(|| {
        let mut v = caller.locals.clone();
        v.extend(inlined_ty.params.iter().copied());
        v.extend(inlined.locals.iter().copied());
        v
    });

    let (expansion, synthetic) = generate_inline_instructions(
        inlined_body,
        inlined_ty,
        original_local_count,
        actual,
        has_return,
        uses_locals,
    );
    let (processed_expansion, new_types) =
        process_insts_blocktypes(module, &expansion, true, &synthetic)?;

    // Whole-space shift: above the deleted number gets -1; references to the deleted number go through the special case.
    let shift = |full_idx: u32| -> u32 {
        if full_idx > actual {
            full_idx - 1
        } else {
            full_idx
        }
    };
    // Reference rewriting for a single instruction (call/return_call renumbering; ref.func pointing at the deleted one repoints at the
    // first kept function post-deletion). None = no rewrite needed.
    let rewrite_inst = |inst: &Inst| -> Option<Inst> {
        match inst {
            Inst::Call { function_index } | Inst::ReturnCall { function_index } => {
                let new = shift(*function_index);
                if new == *function_index {
                    return None;
                }
                Some(match inst {
                    Inst::ReturnCall { .. } => Inst::ReturnCall { function_index: new },
                    _ => Inst::Call { function_index: new },
                })
            }
            Inst::RefFunc { function_index } => {
                let new = if *function_index == actual {
                    import_num
                } else {
                    shift(*function_index)
                };
                (new != *function_index).then_some(Inst::RefFunc { function_index: new })
            }
            _ => None,
        }
    };

    let mut code_edits: Vec<DefEdit> = Vec::new();
    let mut type_edits: Vec<DefEdit> = Vec::new();

    // The caller: whole-function re-encoding (call site → expansion; other positions → shift rewriting).
    {
        let mut new_insts: Vec<Inst> = Vec::with_capacity(caller.insts.len());
        for inst in &caller.insts {
            let is_site = matches!(inst, Inst::Call { function_index } if *function_index == actual);
            if is_site {
                new_insts.extend(processed_expansion.iter().cloned());
            } else {
                match rewrite_inst(inst) {
                    Some(new) => new_insts.push(new),
                    None => new_insts.push(inst.clone()),
                }
            }
        }
        let mut new_func = caller.clone();
        new_func.insts = new_insts;
        if let Some(nl) = new_locals {
            new_func.locals = nl;
        }
        code_edits.push(DefEdit::replace_one(caller_idx, func_def(&new_func)?));
    }

    // Other functions: those touched by shift rewriting get whole-function re-encoding.
    for (f_idx, f) in module.defined_funcs.iter().enumerate() {
        let f_idx = f_idx as u32;
        if f_idx == callee_idx || f_idx == caller_idx {
            continue;
        }
        let mut changed = false;
        let mut new_insts = f.insts.clone();
        for inst in new_insts.iter_mut() {
            if let Some(new) = rewrite_inst(inst) {
                *inst = new;
                changed = true;
            }
        }
        if changed {
            let mut new_func = f.clone();
            new_func.insts = new_insts;
            code_edits.push(DefEdit::replace_one(f_idx, func_def(&new_func)?));
        }
    }

    // Type appends (Python _combine_type_mutations: merged into one mutation at the type section's end).
    if !new_types.is_empty() {
        let base = module.types.len() as u32;
        let mut defs = Vec::with_capacity(new_types.len());
        for ty in &new_types {
            defs.push(type_def(ty)?);
        }
        type_edits.push(DefEdit { range: base..base, repl: defs });
    }

    let mut mutations: Vec<Mutation> = Vec::new();
    if !type_edits.is_empty() {
        mutations.push(Mutation::definitions(SectionKind::Type, type_edits));
    }
    // Element segments: function-number rewriting (funcidxs form / ref.func expression form; externref segments skipped).
    let elem_mutation = rewrite_elem_segs_for_func_removal(module, actual, import_num, &shift)?;
    if let Some(m) = elem_mutation {
        mutations.push(m);
    }
    // Exports: pointing at a deleted function → dropped; number shifted → renumbered.
    for (i, e) in module.exports.iter().enumerate() {
        if let ExportDesc::Func(v) = e.desc {
            if v == actual {
                mutations.push(Mutation::definitions(
                    SectionKind::Export,
                    vec![DefEdit::delete(i as u32)],
                ));
            } else {
                let new = shift(v);
                if new != v {
                    let mut ne = e.clone();
                    ne.desc = ExportDesc::Func(new);
                    mutations.push(Mutation::definitions(
                        SectionKind::Export,
                        vec![DefEdit::replace_one(i as u32, export_def(&ne)?)],
                    ));
                }
            }
        }
    }
    // start section: deleted → drop the section; shifted → renumber.
    if let Some(start) = module.start_sec_data {
        if start == actual {
            mutations.push(Mutation::definitions(
                SectionKind::Start,
                vec![DefEdit { range: 0..1, repl: vec![] }],
            ));
        } else {
            let new = shift(start);
            if new != start {
                mutations.push(Mutation::definitions(
                    SectionKind::Start,
                    vec![DefEdit { range: 0..1, repl: vec![u32_def(new)] }],
                ));
            }
        }
    }
    // Function + code sections: delete the inlined (already uniquely referenced and eliminated) callee.
    mutations.push(Mutation::definitions(
        SectionKind::Function,
        vec![DefEdit::delete(callee_idx)],
    ));
    if !code_edits.is_empty() {
        mutations.push(Mutation::definitions(SectionKind::Code, code_edits));
    }
    mutations.push(Mutation::definitions(
        SectionKind::Code,
        vec![DefEdit::delete(callee_idx)],
    ));
    Ok(mutations)
}

/// Element-segment function-number rewriting (the element-segment part of Python `_rewrite_func_idxs_after_delete_a_func`):
/// the funcidxs form per entry, the expression form per ref.func;
/// pointing at the deleted function → repoint at the first kept function post-deletion; shifted → renumber. externref segments skipped.
fn rewrite_elem_segs_for_func_removal(
    module: &Module,
    actual: u32,
    padding_idx: u32,
    shift: &impl Fn(u32) -> u32,
) -> Result<Option<Mutation>> {
    let mut edits: Vec<DefEdit> = Vec::new();
    for (seg_idx, seg) in module.elem_sec_datas.iter().enumerate() {
        let mut new_seg = seg.clone();
        let mut changed = false;
        match &seg.payload {
            ElemPayload::FuncIdxs(idxs) => {
                let new_idxs: Vec<u32> = idxs
                    .iter()
                    .map(|&i| {
                        if i == actual {
                            changed = true;
                            padding_idx
                        } else {
                            let n = shift(i);
                            if n != i {
                                changed = true;
                            }
                            n
                        }
                    })
                    .collect();
                if changed {
                    new_seg.payload = ElemPayload::FuncIdxs(new_idxs);
                }
            }
            ElemPayload::Exprs { elem_ty, exprs } => {
                if *elem_ty == RefType::EXTERNREF {
                    continue;
                }
                let mut new_exprs = exprs.clone();
                for expr in new_exprs.iter_mut() {
                    if let [Inst::RefFunc { function_index }, rest @ ..] = expr.as_mut_slice() {
                        let new = if *function_index == actual {
                            changed = true;
                            padding_idx
                        } else {
                            let n = shift(*function_index);
                            if n != *function_index {
                                changed = true;
                            }
                            n
                        };
                        *function_index = new;
                        let _ = rest;
                    }
                }
                if changed {
                    new_seg.payload = ElemPayload::Exprs { elem_ty: *elem_ty, exprs: new_exprs };
                }
            }
        }
        if changed {
            edits.push(DefEdit::replace_one(seg_idx as u32, elem_def(&new_seg)?));
        }
    }
    if edits.is_empty() {
        return Ok(None);
    }
    Ok(Some(Mutation::definitions(SectionKind::Element, edits)))
}

// ---------------------------------------------------------------------------
// unreachable replacement helpers
// ---------------------------------------------------------------------------

/// Python `gen_unreachable_replacement_mutations` + `_gen_a_func_with_one_
/// unreachable_inst`: no-result form → empty body; result form → a single unreachable;
/// locals cleared, type unchanged.
fn gen_unreachable_replacement_mutations(
    module: &Module,
    to_remove_func_idxs: &BTreeSet<u32>,
) -> Result<Vec<Mutation>> {
    let mut edits = Vec::with_capacity(to_remove_func_idxs.len());
    for &func_idx in to_remove_func_idxs {
        let func = module
            .defined_funcs
            .get(func_idx as usize)
            .with_context(|| format!("unreachable replacement on missing func {func_idx}"))?;
        let results_is_empty = module.types[module.defined_func_ty_ids[func_idx as usize] as usize]
            .results
            .is_empty();
        let mut insts = Vec::new();
        if !results_is_empty {
            insts.push(Inst::Unreachable);
        }
        insts.push(Inst::End);
        let new_func = Func { ty_idx: func.ty_idx, locals: Vec::new(), insts };
        edits.push(DefEdit::replace_one(func_idx, func_def(&new_func)?));
    }
    Ok(vec![Mutation::definitions(SectionKind::Code, edits)])
}

// ---------------------------------------------------------------------------
// polish_return_type helpers (Python RelaxRetyrnTypeReducer)
// ---------------------------------------------------------------------------

/// The stack-requirement analysis data of one top-level element (instruction or control-flow node).
struct RtyElem {
    /// Python DropOrParams.drop (whether the instruction is a drop).
    is_drop: bool,
    /// Python DropOrParams.params = ty0.param_types (the consumed stack types).
    params: Vec<ValTy>,
    /// The element's full stack requirement (Python elem_reqs).
    req: TR,
    /// The instruction sequence the element contributes (Inst or node.get_insts()).
    insts: Vec<Inst>,
}

/// One function's RelaxRetyrnTypeReducer static data (the derivations inside Python's constructor).
struct RtyAnalysis {
    can_skip: bool,
    elems: Vec<RtyElem>,
    /// The original local table (kept by the new function body).
    ori_locals: Vec<ValType>,
    /// Initial stack state: the root list's block_type = []→results, whose param_types are empty —
    /// rest_types=[[]] (function params are not pre-seeded on the stack; an uncalled function's input is meaningless anyway,
    /// padded by constants).
    stack_init: StackState,
    /// The padding-instruction cache (Python SpecificTypeInstsFactory._type2code_snippet;
    /// key = result type list, value = the constant padding sequence, generated randomly on first sight of a new type then reused).
    snippet_cache: HashMap<Vec<ValTy>, Vec<Inst>>,
}

impl RtyAnalysis {
    /// Python `SpecificTypeInstsFactory.get_code_snippet(funcType([], params)).
    fn get_snippet(&mut self, params: &[ValTy], rng: &mut impl Rng) -> Result<Vec<Inst>> {
        if let Some(v) = self.snippet_cache.get(params) {
            return Ok(v.clone());
        }
        let insts = gen_specific_type_insts(&[], params, rng)?;
        self.snippet_cache.insert(params.to_vec(), insts.clone());
        Ok(insts)
    }
}

/// Python RelaxRetyrnTypeReducer construction: derives one function's element sequence and stack requirements
/// from the module and syntax tree (as of entering the step).
fn build_rty_analysis(module: &Module, ast: &Ast, func_idx: u32) -> Result<RtyAnalysis> {
    let ty_idx = module.defined_func_ty_ids[func_idx as usize] as usize;
    let func_ty = &module.types[ty_idx];
    let can_skip = func_ty.results.is_empty();
    let ori_locals = module.defined_funcs[func_idx as usize].locals.clone();

    let root = ast.root_of_func(func_idx as usize);
    let stack_init = StackState {
        all_rest_types: vec![Vec::new()],
        status: StackStatus::Normal,
    };

    let context = cur_context_by_ast_info(module, ast, ast.node(root).loc, root)?;
    let mut elems: Vec<RtyElem> = Vec::new();
    for child in ast.sub_nodes(root) {
        match &ast.node(child).kind {
            NodeKind::Insts { insts, .. } => {
                for inst in insts {
                    let req = get_inst_ty_req(inst, Some(&context))
                        .ok_or_else(|| anyhow::anyhow!("no type req for inst {inst:?}"))?;
                    let is_drop = matches!(inst, Inst::Drop);
                    elems.push(RtyElem {
                        is_drop,
                        params: req.ty0().params.clone(),
                        req,
                        insts: vec![inst.clone()],
                    });
                }
            }
            _ => {
                let req = ast
                    .get_type_req(child)
                    .ok_or_else(|| anyhow::anyhow!("node {child:?} has no static type req"))?;
                elems.push(RtyElem {
                    is_drop: false,
                    params: req.ty0().params.clone(),
                    req,
                    insts: ast.get_insts(child),
                });
            }
        }
    }
    Ok(RtyAnalysis { can_skip, elems, ori_locals, stack_init, snippet_cache: HashMap::new() })
}

/// Python `RelaxRetyrnTypeReducer.reduce_types` + `try_remove` + 
/// `_get_new_insts_and_new_func_type` + `_get_processed_insts_and_new_types`.
/// Returns Some(new snapshot) = this function had an accepted return-type simplification.
#[allow(clippy::too_many_arguments)]
fn reduce_one_func_return_type(
    pass: &FinalPolishPass<'_>,
    analysis: &mut RtyAnalysis,
    snapshot: &Snapshot,
    func_idx: u32,
    output: &Path,
    to_stop_time: Option<Instant>,
    rng: &mut impl Rng,
) -> Result<Option<Snapshot>> {
    if analysis.can_skip {
        return Ok(None);
    }
    let is_timeout = || to_stop_time.is_some_and(|t| Instant::now() >= t);
    if is_timeout() {
        return Ok(None);
    }
    let to_reduce_elems: Vec<u32> = (0..analysis.elems.len() as u32).collect();
    let ori_cfg_len = to_reduce_elems.len();
    let raw_types: Vec<FuncType> = snapshot.module().types.clone();
    // The last passing mutation set (Python self.def_mutations).
    let mut def_mutations: Option<Vec<Mutation>> = None;
    // First-error short-circuit flag (the error itself is recorded and re-raised by run_probdd_capturing).
    let mut try_failed = false;

    {
        let mut try_remove = |keep: &[u32]| -> Result<bool> {
            if try_failed {
                return Ok(false);
            }
            if is_timeout() {
                return Ok(false);
            }
            // _get_new_insts_and_new_func_type.
            let mut new_insts: Vec<Inst> = Vec::new();
            let mut cur_tr = analysis.stack_init.as_type_req();
            let mut cur_state = analysis.stack_init.clone();
            let mut keep_sorted = keep.to_vec();
            keep_sorted.sort_unstable();
            for idx in keep_sorted {
                let exp_len = analysis.elems[idx as usize].params.len();
                let is_drop = analysis.elems[idx as usize].is_drop;
                let rest_len = cur_state.all_rest_types[0].len();
                let satisfied = rest_len >= exp_len
                    && cur_state.all_rest_types[0][rest_len - exp_len..]
                        == analysis.elems[idx as usize].params[..];
                if satisfied || (rest_len >= exp_len && is_drop) {
                    // The stack already holds the needed input, or a drop can consume anything.
                } else {
                    // Padding: the constant sequence for []→params (the cached mutable borrow is separate from the element borrow).
                    let params = analysis.elems[idx as usize].params.clone();
                    let pad = match analysis.get_snippet(&params, rng) {
                        Ok(p) => p,
                        Err(e) => {
                            try_failed = true;
                            return Err(e);
                        }
                    };
                    new_insts.extend(pad.iter().cloned());
                    let pad_ty = e2wr_ir::types::FTy::of(&[], &params);
                    cur_tr = merge(&cur_tr, &TR::new([pad_ty]));
                }
                new_insts.extend(analysis.elems[idx as usize].insts.iter().cloned());
                cur_tr = merge(&cur_tr, &analysis.elems[idx as usize].req);
                let Some(next) = rest_types_first(&cur_tr) else {
                    try_failed = true;
                    return Err(anyhow::anyhow!(
                        "return type inference produced empty candidates"
                    ));
                };
                cur_state = next;
            }
            let results: Vec<ValType> =
                cur_state.all_rest_types[0].iter().map(valty_to_wp).collect();
            let new_func_ty = FuncType { params: Vec::new(), results };

            // _get_processed_insts_and_new_types (block type normalization, short encoding preferred).
            let (processed_insts, new_types) =
                match process_return_type_blocktypes(&raw_types, new_insts) {
                    Ok(v) => v,
                    Err(e) => {
                        try_failed = true;
                        return Err(e);
                    }
                };
            if !new_types.is_empty() {
                // Python: raise Exception (caught by the pass-level catch; the whole step is skipped).
                try_failed = true;
                return Err(anyhow::anyhow!(
                    "polish_return_type: block type not expressible ({})",
                    new_types.len()
                ));
            }

            // Assemble the mutations (membership decisions and placement by the current snapshot).
            let module = snapshot.module();
            let mut mutations: Vec<Mutation> = Vec::new();
            let mut new_func = module.defined_funcs[func_idx as usize].clone();
            // The element concatenation sequence excludes the function-level trailing end (the Rust convention includes it in bodies).
            new_func.insts = processed_insts;
            new_func.insts.push(Inst::End);
            new_func.locals = analysis.ori_locals.clone();
            let code_def = match func_def(&new_func) {
                Ok(d) => d,
                Err(e) => {
                    try_failed = true;
                    return Err(e);
                }
            };
            mutations.push(Mutation::definitions(
                SectionKind::Code,
                vec![DefEdit::replace_one(func_idx, code_def)],
            ));
            let func_ty_idx = match module.types.iter().position(|t| *t == new_func_ty) {
                Some(i) => i as u32,
                None => {
                    let at = module.types.len() as u32;
                    let ty_def = match type_def(&new_func_ty) {
                        Ok(d) => d,
                        Err(e) => {
                            try_failed = true;
                            return Err(e);
                        }
                    };
                    mutations.push(Mutation::definitions(
                        SectionKind::Type,
                        vec![DefEdit { range: at..at, repl: vec![ty_def] }],
                    ));
                    at
                }
            };
            mutations.push(Mutation::definitions(
                SectionKind::Function,
                vec![DefEdit::replace_one(func_idx, u32_def(func_ty_idx))],
            ));

            match apply_mutation_and_encode(snapshot, &mutations, &pass.dir.tmp_used_path) {
                Ok(_) => {}
                Err(e) => {
                    try_failed = true;
                    return Err(e);
                }
            }
            // D-14: the debug validation of RelaxRetyrnTypeReducer probes (Python
            // :734-739, no exemption).
            crate::common::debug_validate_wasm_file(
                crate::common::DebugValidate::strict(pass.debug),
                &pass.dir.tmp_used_path,
            )?;
            if pass.oracle_check(&pass.dir.tmp_used_path) {
                if std::fs::copy(&pass.dir.tmp_used_path, output).is_err() {
                    return Ok(false);
                }
                def_mutations = Some(mutations);
                return Ok(true);
            }
            Ok(false)
        };
        let expected_end_time = to_stop_time.map(|t| {
            SystemTime::now()
                + Duration::from_secs_f64((t - Instant::now()).as_secs_f64().max(0.0))
        });
        let mut dd: ProbDD<u32> = pass.dd_factory.create_probdd();
        let minimal_config =
            crate::common::run_probdd_capturing(&mut dd, &to_reduce_elems, None, expected_end_time, &mut try_remove)
                .map_err(|e| e.context("polish_return_type try_remove failed"))?;
        if is_timeout() {
            return Ok(None);
        }
        if minimal_config.len() == ori_cfg_len {
            return Ok(None);
        }
    }
    // After convergence, a final verification commit per the last passing mutation set (the tail of Python reduce_types).
    let mutations = def_mutations
        .context("polish_return_type: minimal config shrank but no passing trial")?;
    match pass.mutate_test_and_commit(snapshot, &mutations, output)? {
        Some(s) => Ok(Some(s)),
        None => Ok(None),
    }
}

/// The candidate projection after merge (Python get_stack_state_from_type_req;
/// empty candidates = the IndexError on Python's rest_types[0], collapsed to an error here).
fn rest_types_first(tr: &TR) -> Option<StackState> {
    let all = e2wr_ir::types::rest_types_view(tr);
    if all.is_empty() {
        return None;
    }
    Some(StackState { all_rest_types: all, status: e2wr_ir::types::status_view(tr) })
}

/// The RelaxRetyrnTypeReducer flavor of `_get_processed_insts_and_new_types` + `_get_short_encoding_functype_as_blocktype_param`:
/// priorities differ from the InstsReplacement flavor (node_rewriter::process_insts_blocktypes) — the short encoding
/// wins **over** an existing same-shape index (no-param-no-result → the empty shorthand; no-param-one-result → the value-type shorthand),
/// otherwise the first same-shape index in types is taken; new types are collected only when nothing can express it (the caller requires emptiness).
/// otherwise the first same-shape index in types is taken; new types are collected only when nothing can express it (the caller requires emptiness).
fn process_return_type_blocktypes(
    types: &[FuncType],
    insts: Vec<Inst>,
) -> Result<(Vec<Inst>, Vec<FuncType>)> {
    let mut new_insts: Vec<Inst> = Vec::with_capacity(insts.len());
    let mut new_types: Vec<FuncType> = Vec::new();
    for inst in insts {
        match inst {
            Inst::Block { blockty } => {
                let bt = normalize_return_type_bt(blockty, types, &mut new_types)?;
                new_insts.push(Inst::Block { blockty: bt });
            }
            Inst::Loop { blockty } => {
                let bt = normalize_return_type_bt(blockty, types, &mut new_types)?;
                new_insts.push(Inst::Loop { blockty: bt });
            }
            Inst::If { blockty } => {
                let bt = normalize_return_type_bt(blockty, types, &mut new_types)?;
                new_insts.push(Inst::If { blockty: bt });
            }
            other => new_insts.push(other),
        }
    }
    Ok((new_insts, new_types))
}

fn normalize_return_type_bt(
    blockty: wasmparser::BlockType,
    types: &[FuncType],
    new_types: &mut Vec<FuncType>,
) -> Result<wasmparser::BlockType> {
    let wasmparser::BlockType::FuncType(idx) = blockty else {
        // The empty shorthand (Python init_data=True) and the value-type shorthand (str) are kept verbatim.
        return Ok(blockty);
    };
    let ty = types
        .get(idx as usize)
        .with_context(|| format!("blocktype index {idx} out of range"))?;
    if ty.params.is_empty() {
        if ty.results.is_empty() {
            return Ok(wasmparser::BlockType::Empty);
        }
        if ty.results.len() == 1 {
            return Ok(wasmparser::BlockType::Type(ty.results[0]));
        }
    }
    if let Some(j) = types.iter().position(|t| t == ty) {
        return Ok(wasmparser::BlockType::FuncType(j as u32));
    }
    // Python: appending new types — the caller then asserts new_types is empty (otherwise the whole step errors).
    new_types.push(ty.clone());
    Ok(wasmparser::BlockType::FuncType(
        (types.len() + new_types.len() - 1) as u32,
    ))
}

// ---------------------------------------------------------------------------
// polish_local helpers
// ---------------------------------------------------------------------------

/// One function's local-simplification result: (old→new number map, new local table).
type LocalRewrite = (BTreeMap<u32, u32>, Option<Vec<ValType>>);

/// Python `remove_useless_locals` + `_gen_reduce_local_mutations_for_one_func`:
/// Per function: (old→new number map, new local table — None means nothing deletable).
/// New number = position in the kept table + param_num (Python ori_local_idx2new).
///
/// Coordinate space: Python insts excludes the trailing end (Rust includes it); local.get/set/tee can never
/// collide with end, so instruction ordinals agree.
fn remove_useless_locals(module: &Module) -> Vec<(u32, LocalRewrite)> {
    let mut out = Vec::with_capacity(module.defined_funcs.len());
    for (func_idx, func) in module.defined_funcs.iter().enumerate() {
        let param_num = module.types[module.defined_func_ty_ids[func_idx] as usize]
            .params
            .len() as u32;
        // Used defined locals: old number → type (Python used_locals, recorded on first occurrence;
        // repeated same-number records carry the same type — the Python dict overwrites with equal values, no behavioral difference).
        let mut used_locals: BTreeMap<u32, ValType> = BTreeMap::new();
        let body_end = func.insts.len().saturating_sub(1);
        for inst in &func.insts[..body_end] {
            let local_index = match inst {
                Inst::LocalGet { local_index }
                | Inst::LocalSet { local_index }
                | Inst::LocalTee { local_index } => *local_index,
                _ => continue,
            };
            if local_index < param_num {
                continue;
            }
            used_locals.insert(local_index, func.locals[(local_index - param_num) as usize]);
        }
        if used_locals.len() == func.locals.len() {
            out.push((func_idx as u32, (BTreeMap::new(), None)));
            continue;
        }
        // Renumber in ascending old order: new number = position in the table + param_num.
        let mut renum: BTreeMap<u32, u32> = BTreeMap::new();
        let new_locals: Vec<ValType> = used_locals
            .iter()
            .enumerate()
            .map(|(pos, (old, ty))| {
                renum.insert(*old, pos as u32 + param_num);
                *ty
            })
            .collect();
        out.push((func_idx as u32, (renum, Some(new_locals))));
    }
    out
}
