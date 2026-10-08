//! NodeShrinkPass function-level preprocessing (M8, the round-0 skeleton).
//!
//! Mirrors Python (the round-0 slice of `ReduceFrameWork/NodeShrinkPass.py` reduce() +
//! `FuncRemovalPass.py` + `IdUnexecFuncUtil.py` + `FuncNodeRemover.py` +
//! `replace_indirect_call.py` + the delta-shared core of `callsite_reduction.py`).
//!
//! Execution order (matching NodeShrinkPass.reduce round 0):
//! 1. Copy input to output; wasm-strip produces a de-custom-sectioned file, adopted when the oracle passes
//!    (external tool failure/timeout ignored; the oracle failing on the missing file is the fallback);
//! 2. function-level orchestration `_run_function_level_reduction_to_file`:
//!    a. fewer than 2 defined functions → return directly;
//!    b. ≥ 500 functions (and first round) → run unexecuted-function delta deletion first (300 s budget);
//!    c. callsite replacement (the M9 body, see callsite.rs; delta budget 1200 s);
//!    d. unexecuted deletion not yet run and ≥ 2 functions → run it once more (300 s budget);
//! 3. indirect-to-direct call replacement (budget min(300, remaining); only when remaining > 0).
//!
//! Redundancy dispositions (per the M8/M9 redundancy analysis):
//! - R8-3/R8-4/R8-5/R8-6: write-only `last_try_is_proi`, single-iteration loop shell, dead constants,
//!   always-true conditions — straightened;
//! - R8-7: to_test_func_name is required (the InstrumentManager holds it directly, no None branch);
//! - R8-9: dead parameters (weights/un_exec_func_idxs) not ported;
//! - R8-10: the callsite_as_unreachable True branch kept (user ruling; default False);
//! - R8-11: the get_un_exec_func_idxs forwarding shell merged;
//! - R8-12: FuncNodeRemover's dead shuffle not ported; candidates always ascending;
//! - R8-13: imm extraction replaced by reading strongly-typed instruction fields.

use std::collections::{BTreeMap, BTreeSet, HashMap};
use std::path::{Path, PathBuf};
use std::process::{Command, Stdio};
use std::time::{Duration, Instant, SystemTime};

use anyhow::{Context, Result};

use e2wr_dd::factory::DdFactory;
use e2wr_dd::oracle::Oracle;
use e2wr_dd::probdd::{ProbDD, ReduceOutcome, TestOutcome};
use e2wr_ir::ast::NodeLoc;
use e2wr_ir::module::Module;
use e2wr_ir::mutation::{apply_mutation_and_encode, func_def, DefEdit, Mutation};
use e2wr_ir::snapshot::{SectionKind, Snapshot};
use e2wr_ir::Inst;

use crate::common::{DirSystem, ExecResult, ExecStatus};
use crate::instrumentation::{
    InstrumentManager, InstrumentParams, ProbeDesc, ProbeEvent,
};
use crate::remap::{remap_all_opts, DeleteSet};

/// The relevant Python `ReducerCommonConfig` constants (made an explicit config; defaults = actual Python values).
#[derive(Debug, Clone)]
pub struct FuncLevelConfig {
    /// R9-1: Python's three-value CallsiteRepStrategy (DISABLE/TY_ONLY/VP) was locked
    /// to TY_ONLY in real runs (P-1); the only live gate left is the disable boolean.
    pub disable_callsite: bool,
    /// R8-10: when True, deleting a function rewrites its call sites to a single unreachable
    /// (default False = the actual Python run value; the True branch kept, user ruling).
    pub callsite_as_unreachable: bool,
    /// Function-count threshold for running unexecuted deletion first (NodeShrinkPass `all_func_num >= 500`).
    pub proi_func_threshold: u32,
    /// Function-count threshold for running unexecuted deletion again after callsite replacement (LARGE_TH_FOR_INSTRUMENTATION=2).
    pub post_callsite_unexec_threshold: u32,
    /// Unexecuted-function deletion budget (REDUCE_UNEXEC_FUNC_TIMEOUT, seconds).
    pub unexec_timeout_s: f64,
    /// Callsite replacement delta budget (REDUCE_CALLSITE_TIMEOUT, seconds).
    pub callsite_dd_timeout_s: f64,
    /// Upper bound of the indirect-call replacement delta budget (the 300 of min(300, remaining), seconds).
    /// Consumed by both the FuncLevelPass and NodeShrinkPass entries after the Z-3 wiring.
    pub indirect_dd_timeout_cap_s: f64,
    /// wasm-strip subprocess timeout (run_with_timeout default 30 s).
    /// Consumed by both entries after the Z-3 wiring
    /// (the latter falls back to the default when fl is absent).
    pub strip_timeout_s: u64,
}

impl Default for FuncLevelConfig {
    fn default() -> Self {
        FuncLevelConfig {
            disable_callsite: false,
            callsite_as_unreachable: false,
            proi_func_threshold: 500,
            post_callsite_unexec_threshold: 2,
            unexec_timeout_s: 300.0,
            callsite_dd_timeout_s: 1200.0,
            indirect_dd_timeout_cap_s: 300.0,
            strip_timeout_s: 30,
        }
    }
}

/// Function-level preprocessing context: oracle, directories, instrumentation manager, delta-debugging factory.
/// Corresponds to the mutable state of a NodeShrinkPass instance (instrument_manager, ty_remove_func_pass).
pub struct FuncLevelCtx<'a> {
    pub oracle: &'a Oracle,
    pub dir: DirSystem,
    pub instr: InstrumentManager,
    pub dd_factory: DdFactory,
    pub cfg: FuncLevelConfig,
    pub debug: bool,
}

impl<'a> FuncLevelCtx<'a> {
    pub fn new(
        oracle: &'a Oracle,
        result_dir: &Path,
        to_test_func_name: &str,
        dd_factory: DdFactory,
        cfg: FuncLevelConfig,
        debug: bool,
    ) -> Self {
        let dir = DirSystem::new(result_dir);
        let instrumented_path = dir.tmp_dir.join("instrumented.wasm");
        FuncLevelCtx {
            oracle,
            instr: InstrumentManager::new(&instrumented_path, to_test_func_name, debug),
            dd_factory,
            dir,
            cfg,
            debug,
        }
    }

    /// Oracle verdict (Python `call_oracle`: exceptions treated as not passing).
    pub(crate) fn oracle_check(&self, path: &Path) -> bool {
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
}

// ---------------------------------------------------------------------------
// Instruction-interval mutations (Python FuncInstMutation) and the shared probing base
// ---------------------------------------------------------------------------

/// One in-function instruction-interval mutation: replaces func's instruction at inst_idx with new_insts
/// (Python `ParserModificationUtil.FuncInstMutation`; start/end always adjacent single entries).
#[derive(Debug, Clone)]
pub struct InstEdit {
    pub func_idx: u32,
    pub inst_idx: u32,
    pub new_insts: Vec<Inst>,
}

/// Turns a group of instruction-interval mutations into a Code-section mutation batch (whole-function re-encode path:
/// within a function apply by ordinal, largest first; variable-length replacements stay aligned). Mirrors the FuncBAParts semantics of the M3 mutation channel.
pub fn inst_edits_to_mutations(module: &Module, edits: &[InstEdit]) -> Result<Vec<Mutation>> {
    let mut by_func: BTreeMap<u32, Vec<(u32, Vec<Inst>)>> = BTreeMap::new();
    for e in edits {
        by_func.entry(e.func_idx).or_default().push((e.inst_idx, e.new_insts.clone()));
    }
    let mut code_edits = Vec::new();
    for (func_idx, mut replacements) in by_func {
        let func = module
            .defined_funcs
            .get(func_idx as usize)
            .with_context(|| format!("inst edit targets missing func {func_idx}"))?;
        let mut new_func = func.clone();
        replacements.sort_by(|a, b| b.0.cmp(&a.0));
        for (inst_idx, new_insts) in replacements {
            let at = inst_idx as usize;
            if at >= new_func.insts.len() {
                anyhow::bail!("inst edit index {inst_idx} out of range in func {func_idx}");
            }
            new_func.insts.splice(at..at + 1, new_insts);
        }
        code_edits.push(DefEdit::replace_one(func_idx, func_def(&new_func)?));
    }
    if code_edits.is_empty() {
        return Ok(vec![]);
    }
    Ok(vec![Mutation::definitions(SectionKind::Code, code_edits)])
}

/// Mirrors `callsite_reduction._apply_inst_mutations_to_tmp_and_test`:
/// apply mutations on the baseline snapshot, encode to tmp, ask the oracle; when passing and cur_output is given,
/// always copy the latest passing artifact to cur_output. An empty mutation set tests the baseline file directly.
#[allow(clippy::too_many_arguments)]
pub(crate) fn apply_inst_edits_and_test(
    ctx: &FuncLevelCtx,
    base_snapshot: &Snapshot,
    edits: &[InstEdit],
    tmp_out_path: &Path,
    base_wasm_path: &Path,
    cur_output_path: Option<&Path>,
) -> Result<bool> {
    if edits.is_empty() {
        let result = ctx.oracle_check(base_wasm_path);
        if let Some(out) = cur_output_path {
            if result && base_wasm_path != out {
                std::fs::copy(base_wasm_path, out)
                    .with_context(|| format!("copy {} -> {}", base_wasm_path.display(), out.display()))?;
            }
        }
        return Ok(result);
    }
    let mutations = inst_edits_to_mutations(base_snapshot.module(), edits)?;
    apply_mutation_and_encode(base_snapshot, &mutations, tmp_out_path)?;
    // D-14: callsite_reduction's DEBUG validation (Python :207-219: on invalid it
    // prints mutation details and raises; here it errors upward; no exemption).
    crate::common::debug_validate_wasm_file(
        crate::common::DebugValidate::strict(ctx.debug),
        tmp_out_path,
    )?;
    let result = ctx.oracle_check(tmp_out_path);
    if result {
        if let Some(out) = cur_output_path {
            std::fs::copy(tmp_out_path, out)
                .with_context(|| format!("copy {} -> {}", tmp_out_path.display(), out.display()))?;
        }
    }
    Ok(result)
}

/// The shared delta-replacement core (shared by M8 indirect calls / M9 callsites).
/// Mirrors `callsite_reduction.dd_try_replace_callsites_interface`:
/// - accepted_replaced is always the empty set (Python's same-named local has a single dead assignment after initialization);
/// - a passing verdict records best_replaced; passing with replacements reaching total×save_ratio
///   aborts delta debugging immediately (Python's `_DDEarlyStop` exception; here via reduce_ext's Stop);
/// - candidates are shuffled before delta debugging (D-2: not chasing Python's random sequence);
/// - the DEBUG block (empty-mutation baseline probe) is not ported (R8-2).
#[allow(clippy::too_many_arguments)]
pub(crate) fn dd_try_replace_callsites(
    ctx: &FuncLevelCtx,
    base_snapshot: &Snapshot,
    cand_idx2edit: &BTreeMap<u32, InstEdit>,
    universe: &[u32],
    tmp_out_path: &Path,
    base_wasm_path: &Path,
    cur_output_path: Option<&Path>,
    save_ratio: f64,
    dd_timeout_s: Option<f64>,
    weights: Option<&HashMap<u32, f64>>,
) -> Result<BTreeSet<u32>> {
    let remaining: BTreeSet<u32> = universe.iter().copied().collect();
    // Python's int(total*save_ratio): truncates toward zero.
    let early_stop_target = (universe.len() as f64 * save_ratio) as usize;
    let expected_end_time =
        dd_timeout_s.map(|s| SystemTime::now() + Duration::from_secs_f64(s));
    let mut best_replaced: BTreeSet<u32> = BTreeSet::new();
    let mut early_stop: Option<BTreeSet<u32>> = None;
    let mut test_err: Option<anyhow::Error> = None;

    let mut cur_universe: Vec<u32> = universe.to_vec();
    {
        use rand::seq::SliceRandom;
        cur_universe.shuffle(&mut rand::thread_rng());
    }

    let mut test = |keep: &[u32]| -> TestOutcome {
        let keep_set: BTreeSet<u32> = keep.iter().copied().collect();
        let to_replace: BTreeSet<u32> =
            remaining.difference(&keep_set).copied().collect();
        let edits: Vec<InstEdit> = to_replace
            .iter()
            .filter_map(|i| cand_idx2edit.get(i).cloned())
            .collect();
        println!(
            "Try replace callsites: new={} total={} (remaining_pool={})",
            to_replace.len(),
            to_replace.len(),
            remaining.len()
        );
        let result = match apply_inst_edits_and_test(
            ctx,
            base_snapshot,
            &edits,
            tmp_out_path,
            base_wasm_path,
            cur_output_path,
        ) {
            Ok(v) => v,
            Err(e) => {
                // Python: encode exceptions propagate through delta debugging, are caught by the outer wrapper and rolled back;
                // here recorded and treated as failure, re-raised at the end (the outer layer likewise rolls back).
                test_err = Some(e);
                return TestOutcome::Fail;
            }
        };
        if result {
            best_replaced = to_replace.clone();
        }
        if result && to_replace.len() >= early_stop_target {
            println!(
                "DD early stop (single-round): replaced={}/{} target={}",
                to_replace.len(),
                universe.len(),
                early_stop_target
            );
            early_stop = Some(to_replace);
            return TestOutcome::Stop;
        }
        if result {
            TestOutcome::Pass
        } else {
            TestOutcome::Fail
        }
    };

    let mut dd: ProbDD<u32> = ctx.dd_factory.create_probdd();
    let outcome = dd.reduce_ext(&cur_universe, weights, expected_end_time, &mut test);
    if let Some(e) = test_err {
        return Err(e);
    }
    match outcome {
        ReduceOutcome::Stopped => Ok(early_stop.unwrap_or_default()),
        ReduceOutcome::Config(minimal_to_save) => {
            let newly_replaced: BTreeSet<u32> = {
                let keep: BTreeSet<u32> = minimal_to_save.iter().copied().collect();
                remaining.difference(&keep).copied().collect()
            };
            println!(
                "DD round summary: remaining_in={} minimal_to_save={} newly_replaced={}",
                universe.len(),
                minimal_to_save.len(),
                newly_replaced.len()
            );
            Ok(best_replaced)
        }
    }
}

// ---------------------------------------------------------------------------
// The unexecuted-function delta-deletion chain (IdUnexecFuncUtil → FuncRemovalPass → FuncNodeRemover)
// ---------------------------------------------------------------------------

/// Mirrors `IdUnexecFuncUtil.get_un_exec_func_idxs_by_freq`: a probe at each defined function entry
/// (position (func,0)) collects execution frequencies; zero means unexecuted. Instrumentation failure yields the empty set
/// (Python catches, prints, and empties; kin to P-25's empty-trace = all-executed behavior).
fn get_un_exec_func_idxs(instr: &InstrumentManager, snapshot: &Snapshot) -> Result<BTreeSet<u32>> {
    let func_num = snapshot.module().defined_funcs.len() as u32;
    let locs: BTreeSet<NodeLoc> = (0..func_num)
        .map(|i| NodeLoc { func_idx: i, inst_idx: 0 })
        .collect();
    let loc2freq = instr.instrument_and_get_exec_freq(snapshot, &locs)?;
    Ok(loc2freq
        .iter()
        .filter(|(_, freq)| **freq == 0)
        .map(|(loc, _)| loc.func_idx)
        .collect())
}

/// Mirrors `FuncRemovalPass.remove_proi_unexec_funcs`: probe unexecuted functions →
/// delta debugging with the initial probability temporarily switched to 0.0001 for deletion → restore. The candidates parameter
/// (Python un_exec_func_idxs) is never passed by any caller, not ported (R8-9); Python's
/// cur_input_path parameter is likewise dead, not ported (R-5).
pub fn remove_proi_unexec_funcs(
    ctx: &mut FuncLevelCtx,
    cur_output_path: &Path,
    input_snapshot: &Snapshot,
    timeout: Option<f64>,
) -> Result<(Snapshot, BTreeSet<u32>)> {
    let unexec = match get_un_exec_func_idxs(&ctx.instr, input_snapshot) {
        Ok(v) => v,
        Err(e) => {
            println!("Get unexec func idxs exception: {e:#}");
            BTreeSet::new()
        }
    };
    println!("There are {} unexec func idxs", unexec.len());
    let mut all_removed: BTreeSet<u32> = BTreeSet::new();
    let mut snapshot_out = None;
    ctx.dd_factory.use_a_initp_temp(0.0001);
    if !unexec.is_empty() {
        let r = ensure_pass_run(ctx, input_snapshot, cur_output_path, &unexec, timeout);
        match r {
            Ok((snap, removed)) => {
                all_removed.extend(removed);
                snapshot_out = Some(snap);
            }
            Err(e) => {
                ctx.dd_factory.reset_initp();
                return Err(e);
            }
        }
    }
    ctx.dd_factory.reset_initp();
    println!("After proi, there are {} functions removeed ", all_removed.len());
    Ok((snapshot_out.unwrap_or_else(|| input_snapshot.full_copy()), all_removed))
}

/// Mirrors `FuncRemovalPass.ensure_pass_run`: the weights parameter is not ported (always self-built in the body:
/// per function = instruction count; Python insts excludes the trailing end, Rust includes, so body length -1).
fn ensure_pass_run(
    ctx: &mut FuncLevelCtx,
    input_snapshot: &Snapshot,
    cur_output_path: &Path,
    proi_func_idxs: &BTreeSet<u32>,
    timeout: Option<f64>,
) -> Result<(Snapshot, BTreeSet<u32>)> {
    let module = input_snapshot.module();
    let mut weights: HashMap<u32, f64> = HashMap::new();
    for (idx, f) in module.defined_funcs.iter().enumerate() {
        weights.insert(idx as u32, f.insts.len().saturating_sub(1) as f64);
    }
    remove_funcs_by_dd(
        ctx,
        input_snapshot,
        cur_output_path,
        Some(&weights),
        proi_func_idxs,
        timeout,
    )
}

/// Mirrors `FuncNodeRemover.remove_funcs_by_dd` + `FuncNodesRemoveReducer`.
/// Every probe rebuilds from the immutable baseline snapshot by keep set (no accumulation); candidates always ascending
/// (R8-12: Python's dead shuffle not ported); at the end, if the final keep set differs from the last passing
/// keep set, rebuild once more so the artifact is consistent.
#[allow(clippy::too_many_arguments)]
fn remove_funcs_by_dd(
    ctx: &mut FuncLevelCtx,
    input_snapshot: &Snapshot,
    cur_output_path: &Path,
    weights: Option<&HashMap<u32, f64>>,
    proi_func_idxs: &BTreeSet<u32>,
    timeout: Option<f64>,
) -> Result<(Snapshot, BTreeSet<u32>)> {
    let t0 = Instant::now();
    println!("Start removing functions by DD...");
    // D-14: the baseline self-check at Python FuncNodeRemover:35-38 — in debug, first re-encode the input
    // snapshot to disk and validate (the "invalid before reduction" assertion; no exemption).
    if ctx.debug {
        let bytes = input_snapshot.encode_to_bytes()?;
        std::fs::write(&ctx.dir.tmp_used_path, &bytes)?;
        crate::common::debug_validate_wasm_file(
            crate::common::DebugValidate::Strict,
            &ctx.dir.tmp_used_path,
        )?;
    }
    let module = input_snapshot.module();
    let func_num = module.defined_funcs.len() as u32;
    let all_func_idxs: BTreeSet<u32> =
        proi_func_idxs.iter().filter(|i| **i < func_num).copied().collect();
    println!("There are {} candidate functions to remove.", all_func_idxs.len());
    let to_stop_time = timeout.map(|t| Instant::now() + Duration::from_secs_f64(t));
    let tmp_write_path = ctx.dir.tmp_used_path.clone();
    let callsite_as_unreachable = ctx.cfg.callsite_as_unreachable;

    // The keep set at the last oracle pass; initial = the full set (meaning current output = baseline).
    let mut last_committed: BTreeSet<u32> = (0..func_num).collect();
    // First-error short-circuit flag (the error itself is recorded and re-raised by run_probdd_capturing).
    let mut build_failed = false;

    let build = |to_save: &BTreeSet<u32>| -> Result<Snapshot> {
        let mut del = DeleteSet::default();
        for i in &all_func_idxs {
            if !to_save.contains(i) {
                del.funcs.insert(*i);
            }
        }
        let mutations = remap_all_opts(module, &del, callsite_as_unreachable)?;
        apply_mutation_and_encode(input_snapshot, &mutations, &tmp_write_path)
    };

    let mut try_remove = |to_save: &[u32]| -> Result<bool> {
        if build_failed {
            return Ok(false);
        }
        if let Some(t) = to_stop_time {
            if Instant::now() > t {
                return Ok(false);
            }
        }
        let keep: BTreeSet<u32> = to_save.iter().copied().collect();
        let new_snapshot = match build(&keep) {
            Ok(s) => s,
            Err(e) => {
                build_failed = true;
                return Err(e);
            }
        };
        // D-14: the debug validation of FuncNodesRemoveReducer probes (Python
        // :162-170, initializer type-mismatch exemption).
        crate::common::debug_validate_wasm_file(
            crate::common::DebugValidate::exempt_init_expr(ctx.debug),
            &tmp_write_path,
        )?;
        if ctx.oracle_check(&tmp_write_path) {
            if std::fs::copy(&tmp_write_path, cur_output_path).is_err() {
                return Ok(false);
            }
            let _ = new_snapshot;
            last_committed = keep;
            return Ok(true);
        }
        Ok(false)
    };

    let config: Vec<u32> = all_func_idxs.iter().copied().collect();
    let expected_end_time = to_stop_time.map(|t| {
        SystemTime::now()
            + Duration::from_secs_f64((t - Instant::now()).as_secs_f64().max(0.0))
    });
    let mut dd: ProbDD<u32> = ctx.dd_factory.create_probdd();
    let minimal_config =
        crate::common::run_probdd_capturing(&mut dd, &config, weights, expected_end_time, &mut try_remove)
            .map_err(|e| e.context("try_remove build failed"))?;
    let minimal_set: BTreeSet<u32> = minimal_config.iter().copied().collect();
    let removed_idxs: BTreeSet<u32> =
        all_func_idxs.difference(&minimal_set).copied().collect();
    if last_committed != minimal_set {
        build(&minimal_set)?;
        std::fs::copy(&tmp_write_path, cur_output_path).with_context(|| {
            format!("copy {} -> {}", tmp_write_path.display(), cur_output_path.display())
        })?;
    }
    println!("Removed {} functions by DD.", removed_idxs.len());
    println!("Function removal time: {:.2} seconds.", t0.elapsed().as_secs_f64());
    Ok((Snapshot::from_path(cur_output_path)?, removed_idxs))
}

// ---------------------------------------------------------------------------
// Indirect-to-direct call replacement (replace_indirect_call.py)
// ---------------------------------------------------------------------------

/// A call_indirect callsite (Python `IndirectCallSite`).
#[derive(Debug, Clone)]
pub struct IndirectCallSite {
    pub defined_func_idx: u32,
    pub call_inst_idx: u32,
    pub callee_type_idx: u32,
}

/// Mirrors `collect_indirect_call_sites_from_parser`: scans all defined function bodies.
/// Coordinate space = Python wasmFunc.insts (trailing end excluded; D-9).
pub fn collect_indirect_call_sites(module: &Module) -> Vec<IndirectCallSite> {
    let mut result = Vec::new();
    for (defined_func_idx, func) in module.defined_funcs.iter().enumerate() {
        let body_len = func.insts.len().saturating_sub(1);
        for inst_idx in 0..body_len {
            if let Inst::CallIndirect { type_index, .. } = &func.insts[inst_idx] {
                result.push(IndirectCallSite {
                    defined_func_idx: defined_func_idx as u32,
                    call_inst_idx: inst_idx as u32,
                    callee_type_idx: *type_index,
                });
            }
        }
    }
    result
}

/// Mirrors `infer_call_indirect_callee_map_from_executed_trace`: reconstruct the call stacks from the execution trace
/// and infer each indirect call site's actually-hit callee (defined-function entry events only;
/// imported callees have no entry event, and pending markers are overwritten and dropped). Per site take the most frequent,
/// ties broken by the smaller function index.
pub fn infer_call_indirect_callee_map_from_executed_trace(
    dumped: &[ProbeEvent],
    callsite_probe_idx2site: &BTreeMap<u32, IndirectCallSite>,
    func_entry_probe_idx2func_idx: &BTreeMap<u32, u32>,
) -> BTreeMap<u32, u32> {
    struct CallFrame {
        func_idx: u32,
        pending_call_indirect_probe_idx: Option<u32>,
    }
    let mut call_stack: Vec<CallFrame> = Vec::new();
    let mut callsite_probe_idx2callee_freq: BTreeMap<u32, BTreeMap<u32, u32>> = BTreeMap::new();

    for one in dumped {
        let probe_idx = one.probe_idx;
        if let Some(func_idx) = func_entry_probe_idx2func_idx.get(&probe_idx) {
            let func_idx = *func_idx;
            if let Some(top) = call_stack.last_mut() {
                if let Some(pending) = top.pending_call_indirect_probe_idx.take() {
                    let cur = callsite_probe_idx2callee_freq.entry(pending).or_default();
                    *cur.entry(func_idx).or_insert(0) += 1;
                }
            }
            call_stack.push(CallFrame {
                func_idx,
                pending_call_indirect_probe_idx: None,
            });
            continue;
        }
        let Some(site) = callsite_probe_idx2site.get(&probe_idx) else {
            continue;
        };
        let caller_func_idx = site.defined_func_idx;
        while call_stack
            .last()
            .is_some_and(|f| f.func_idx != caller_func_idx)
        {
            call_stack.pop();
        }
        if call_stack.is_empty() {
            call_stack.push(CallFrame {
                func_idx: caller_func_idx,
                pending_call_indirect_probe_idx: None,
            });
        }
        // Overwrite a stale pending marker (e.g. the previous target was an imported function, no entry event).
        call_stack.last_mut().unwrap().pending_call_indirect_probe_idx = Some(probe_idx);
    }

    let mut result = BTreeMap::new();
    for (probe_idx, freq) in callsite_probe_idx2callee_freq {
        if freq.is_empty() {
            continue;
        }
        // Frequency descending, function index ascending; take the first.
        let best = freq
            .into_iter()
            .max_by(|a, b| a.1.cmp(&b.1).then(b.0.cmp(&a.0)))
            .unwrap();
        result.insert(probe_idx, best.0);
    }
    result
}

/// Mirrors `replace_indirect_calls_interface`.
/// Returns (output snapshot, the set of replaced callsites). The observed callee's type index may differ from the site's declared
/// type index (the funcref-table mechanism); the oracle rejects such candidates as the fallback
/// (Python's assert there is commented out; R8-14).
pub fn replace_indirect_calls(
    ctx: &FuncLevelCtx,
    cur_input_path: &Path,
    cur_output_path: &Path,
    input_snapshot: &Snapshot,
    dd_timeout_s: Option<f64>,
) -> Result<(Snapshot, BTreeSet<u32>)> {
    let t0 = Instant::now();
    println!("Start reduce call_indirects ==========================================");
    let module = input_snapshot.module();
    let call_sites = collect_indirect_call_sites(module);
    if cur_input_path != cur_output_path {
        std::fs::copy(cur_input_path, cur_output_path).with_context(|| {
            format!("copy {} -> {}", cur_input_path.display(), cur_output_path.display())
        })?;
    }
    if call_sites.is_empty() {
        return Ok((input_snapshot.full_copy(), BTreeSet::new()));
    }

    // Probes: the position before each callsite (call_inst_idx) + each defined function entry (f, 0).
    let mut probe_descs: Vec<ProbeDesc> = Vec::new();
    let mut callsite_probe_idx2site: BTreeMap<u32, IndirectCallSite> = BTreeMap::new();
    for (i, site) in call_sites.iter().enumerate() {
        let probe_idx = i as u32;
        callsite_probe_idx2site.insert(probe_idx, site.clone());
        probe_descs.push(ProbeDesc {
            idx: probe_idx,
            loc: NodeLoc { func_idx: site.defined_func_idx, inst_idx: site.call_inst_idx },
        });
    }
    let mut func_entry_probe_idx2func_idx: BTreeMap<u32, u32> = BTreeMap::new();
    let entry_probe_start = probe_descs.len() as u32;
    for defined_func_idx in 0..module.defined_funcs.len() as u32 {
        let probe_idx = entry_probe_start + defined_func_idx;
        func_entry_probe_idx2func_idx.insert(probe_idx, defined_func_idx);
        probe_descs.push(ProbeDesc {
            idx: probe_idx,
            loc: NodeLoc { func_idx: defined_func_idx, inst_idx: 0 },
        });
    }

    let params = InstrumentParams {
        only_executed_probe: true,
        allocated_time: 30,
        max_output_time: Some(255),
    };
    let dumped =
        ctx.instr.instrument_multiple_places_and_get_result(input_snapshot, &probe_descs, &params)?;

    let observed_defined_callee_by_probe_idx = infer_call_indirect_callee_map_from_executed_trace(
        &dumped,
        &callsite_probe_idx2site,
        &func_entry_probe_idx2func_idx,
    );

    // Mutation: drop (consumes the table element index i32) + call the observed callee.
    // Entry events carry defined-local numbers; the call immediate needs the whole-space number (add the imported-function count).
    let import_func_num = module.import_func_num() as u32;
    let mut cand_idx2edit: BTreeMap<u32, InstEdit> = BTreeMap::new();
    for (probe_idx, site) in &callsite_probe_idx2site {
        let Some(observed_defined_callee_idx) = observed_defined_callee_by_probe_idx.get(probe_idx)
        else {
            continue;
        };
        let callee_func_idx = import_func_num + observed_defined_callee_idx;
        cand_idx2edit.insert(
            *probe_idx,
            InstEdit {
                func_idx: site.defined_func_idx,
                inst_idx: site.call_inst_idx,
                new_insts: vec![
                    Inst::Drop,
                    Inst::Call { function_index: callee_func_idx },
                ],
            },
        );
    }

    let universe: Vec<u32> = cand_idx2edit.keys().copied().collect();
    if universe.is_empty() {
        return Ok((input_snapshot.full_copy(), BTreeSet::new()));
    }

    let dd_tmp_path = ctx.dir.tmp_dir.join("dd_tmp_replace_indirect_calls.wasm");
    // save_ratio=1: the early-stop target = the full amount, triggered only when all replacements succeed (effectively almost never early).
    let replaced = dd_try_replace_callsites(
        ctx,
        input_snapshot,
        &cand_idx2edit,
        &universe,
        &dd_tmp_path,
        cur_input_path,
        Some(cur_output_path),
        1.0,
        dd_timeout_s,
        None,
    )?;
    println!(
        "Replacing call_indirect done, taking {:.1}s, replaced {}/{} callsites.",
        t0.elapsed().as_secs_f64(),
        replaced.len(),
        universe.len()
    );
    Ok((Snapshot::from_path(cur_output_path)?, replaced))
}

// ---------------------------------------------------------------------------
// Orchestration (the NodeShrinkPass.reduce round-0 skeleton)
// ---------------------------------------------------------------------------

/// Runs the external wasm-strip (Python shells out to `wasm-strip in -o out`, default 30 s
/// timeout, failures ignored). Timeout/spawn failure only logs: the later oracle fails on the missing file
/// and the original input is kept automatically (same as Python).
pub(crate) fn run_wasm_strip(input: &Path, output: &Path, timeout_s: u64) {
    let child = Command::new("wasm-strip")
        .arg(input)
        .arg("-o")
        .arg(output)
        .stdout(Stdio::null())
        .stderr(Stdio::null())
        .spawn();
    let mut child = match child {
        Ok(c) => c,
        Err(e) => {
            eprintln!("wasm-strip spawn failed: {e}");
            return;
        }
    };
    let deadline = Instant::now() + Duration::from_secs(timeout_s);
    loop {
        match child.try_wait() {
            Ok(Some(_)) => return,
            Ok(None) => {
                if Instant::now() > deadline {
                    let _ = child.kill();
                    eprintln!("wasm-strip timed out after {timeout_s}s");
                    return;
                }
                std::thread::sleep(Duration::from_millis(10));
            }
            Err(e) => {
                eprintln!("wasm-strip wait failed: {e}");
                return;
            }
        }
    }
}

/// Mirrors the exception wrapping of `NodeShrinkPass.replace_call_indirects`/`replace_callsites`:
/// back up first; on failure roll back to the backup and return an empty result (Python validates once more after rollback —
/// the rolled-back artifact is the original input, necessarily valid; no repeat validation).
pub(crate) fn with_backup_on_err(
    ctx: &FuncLevelCtx,
    backup_name: &str,
    cur_input_path: &Path,
    cur_output_path: &Path,
    f: impl FnOnce() -> Result<(Snapshot, BTreeSet<u32>)>,
) -> Result<(Snapshot, BTreeSet<u32>)> {
    let backup = ctx.dir.tmp_dir.join(backup_name);
    std::fs::copy(cur_input_path, &backup).with_context(|| {
        format!("copy {} -> {}", cur_input_path.display(), backup.display())
    })?;
    match f() {
        Ok(r) => Ok(r),
        Err(e) => {
            println!("Exception when replacing callsites: {e:#}");
            std::fs::copy(&backup, cur_output_path).with_context(|| {
                format!("rollback copy {} -> {}", backup.display(), cur_output_path.display())
            })?;
            // D-14: Python's replace_callsites/replace_indirect_calls except
            // branches validate the rolled-back artifact **unconditionally** after rollback (:514/:552, executed on the production
            // path too; not debug-gated), raising when invalid. Aligned here as constant Strict.
            crate::common::debug_validate_wasm_file(
                crate::common::DebugValidate::Strict,
                cur_output_path,
            )?;
            Ok((Snapshot::from_path(cur_output_path)?, BTreeSet::new()))
        }
    }
}

/// R-17: the `run_function_level` shared base (mirroring Python's
/// `_run_function_level_reduction_to_file`, straightened per R8-4/R8-6) — merges two ~100-line copies:
/// nodeshrink_pass.rs (the NodeShrinkPass main-entry round 0) and this file
/// (the FuncLevelPass single-stage entry); behavioral differences are expressed as parameters:
/// - `fl: None` = no function-level facilities (to_test function name absent); all function-level steps skipped;
/// - `input_snapshot` is loaded by the caller (the two entries' error texts differ, and the main entry collects
///   the input_parser_inst_num baseline after loading);
/// - `is_first_round`: Python's `total_run_times == 0` first-round test (constant true for the single-stage entry);
/// - `raw_setting`: the "Raw setting" print values (to_test presence / total_run_times);
/// - `print_time_cost`: the trailing time-cost print (the single-stage entry has it, the main entry not);
/// - `debug`: the D-14 debug-validation switch (double validation after callsite replacement + the tail validation).
#[allow(clippy::too_many_arguments)]
pub(crate) fn run_function_level_shared(
    mut fl: Option<&mut FuncLevelCtx<'_>>,
    input_snapshot: Snapshot,
    cur_input_path: &Path,
    cur_output_path: &Path,
    is_first_round: bool,
    raw_setting: (bool, i64),
    print_time_cost: bool,
    debug: bool,
) -> Result<(bool, Snapshot)> {
    let mut has_reduced = false;
    let mut input_snapshot = input_snapshot;
    let all_func_num = input_snapshot.module().defined_funcs.len();
    if all_func_num < 2 {
        println!("Function num {all_func_num} is less than 2, skip function level reduction");
        return Ok((has_reduced, input_snapshot));
    }
    println!(
        "Raw setting:  {} ;; total_run_times: {} ;; all_func_num: {all_func_num}",
        raw_setting.0, raw_setting.1
    );
    println!("Will remove functions");
    let time_before_func_level = Instant::now();
    let mut all_removed_func_idxs: BTreeSet<u32> = BTreeSet::new();

    if cur_input_path != cur_output_path {
        std::fs::copy(cur_input_path, cur_output_path).with_context(|| {
            format!("copy {} -> {}", cur_input_path.display(), cur_output_path.display())
        })?;
    }
    let mut cur_input_path = cur_input_path.to_path_buf();

    let mut has_reduce_prio = false;
    if let Some(fl) = fl.as_deref_mut() {
        if all_func_num >= fl.cfg.proi_func_threshold as usize && is_first_round {
            let (snap, removed) = remove_proi_unexec_funcs(
                fl,
                cur_output_path,
                &input_snapshot,
                Some(fl.cfg.unexec_timeout_s),
            )?;
            input_snapshot = snap;
            all_removed_func_idxs.extend(removed);
            cur_input_path = cur_output_path.to_path_buf();
            has_reduce_prio = true;
        }
    }

    // Callsite replacement (the M9 body; Python gates on cr_strategy != DISABLE → the negated boolean).
    let use_callsite_removal = input_snapshot.module().defined_funcs.len() >= 2;
    println!("use_callsite_removal: {use_callsite_removal}");
    if use_callsite_removal {
        if let Some(fl) = fl.as_deref_mut() {
            if !fl.cfg.disable_callsite {
                println!("Start callsite replacement pre-pass =============================");
                let snapshot_ref = &input_snapshot;
                let in_path = cur_input_path.clone();
                let (snap, _replaced) = with_backup_on_err(
                    fl,
                    "replace_call_bak.wasm",
                    &in_path,
                    cur_output_path,
                    || {
                        crate::callsite::replace_callsites(
                            fl,
                            &in_path,
                            cur_output_path,
                            snapshot_ref,
                            Some(fl.cfg.callsite_dd_timeout_s),
                        )
                    },
                )?;
                input_snapshot = snap;
                cur_input_path = cur_output_path.to_path_buf();
            }
        }
        // D-14: the double validation at Python NodeShrinkPass:193-203 (after callsite replacement,
        // inside the use_callsite_removal block, no exemption): validate the current artifact + re-encode the
        // replaced snapshot to tmp_after_callsite.wasm and validate again (snapshot↔disk consistency).
        // With fl absent (no to_test function name) the directory facilities are absent too; the re-encoded artifact
        // goes to the system temp dir (Python's tmp_dir always exists; an approximation here).
        if debug {
            crate::common::debug_validate_wasm_file(
                crate::common::DebugValidate::Strict,
                &cur_input_path,
            )?;
            let reencoded = input_snapshot.encode_to_bytes()?;
            let tmp_after = fl
                .as_deref()
                .map(|f| f.dir.tmp_dir.join("tmp_after_callsite.wasm"))
                .unwrap_or_else(|| {
                    std::env::temp_dir().join("e2wr_tmp_after_callsite.wasm")
                });
            std::fs::write(&tmp_after, &reencoded)?;
            crate::common::debug_validate_wasm_file(
                crate::common::DebugValidate::Strict,
                &tmp_after,
            )?;
        }
    }
    println!(
        "There are {} functions left",
        input_snapshot.module().defined_funcs.len()
    );

    if !has_reduce_prio {
        // Last use; consume the Option directly (clippy needless_option_as_deref).
        if let Some(fl) = fl {
            if input_snapshot.module().defined_funcs.len()
                >= fl.cfg.post_callsite_unexec_threshold as usize
            {
                let (snap, removed) = remove_proi_unexec_funcs(
                    fl,
                    cur_output_path,
                    &input_snapshot,
                    Some(fl.cfg.unexec_timeout_s),
                )?;
                input_snapshot = snap;
                all_removed_func_idxs.extend(removed);
            }
        }
    }

    if !all_removed_func_idxs.is_empty() {
        has_reduced = true;
        println!("Removed {} functions", all_removed_func_idxs.len());
    }

    // D-14: the tail validation at Python NodeShrinkPass:224-228 (the "Will start inst level
    // removal" print + validate, after the function-level deletion block; no exemption).
    if debug {
        println!("Will start inst level removal, cur_input_path: {}", cur_input_path.display());
        crate::common::debug_validate_wasm_file(
            crate::common::DebugValidate::Strict,
            &cur_input_path,
        )?;
    }

    if cur_input_path != cur_output_path {
        std::fs::copy(&cur_input_path, cur_output_path)?;
    }
    if print_time_cost {
        println!(
            "Function level removal time cost: {}s",
            time_before_func_level.elapsed().as_secs_f64()
        );
    }
    Ok((has_reduced, input_snapshot))
}

/// The single-stage entry: mirrors the round-0 skeleton of NodeShrinkPass.reduce()
/// (strip → function-level orchestration → indirect-call replacement).
pub struct FuncLevelPass;

impl FuncLevelPass {
    pub fn reduce(
        ctx: &mut FuncLevelCtx,
        cur_input_path: &Path,
        cur_output_path: &Path,
        timeout: Option<f64>,
    ) -> Result<ExecResult> {
        let start_time = Instant::now();
        let original_size = std::fs::metadata(cur_input_path)?.len();
        std::fs::copy(cur_input_path, cur_output_path).with_context(|| {
            format!("copy {} -> {}", cur_input_path.display(), cur_output_path.display())
        })?;
        let mut cur_input_path: PathBuf = cur_output_path.to_path_buf();

        let stripped_path = ctx.dir.tmp_dir.join("stripped_input.wasm");
        run_wasm_strip(&cur_input_path, &stripped_path, ctx.cfg.strip_timeout_s);
        if ctx.oracle_check(&stripped_path) {
            std::fs::copy(&stripped_path, &cur_input_path)?;
        }

        // Instruction-count baseline: the first snapshot after strip adoption (Python's input_parser_inst_num
        // is first assigned at the _run_function_level_reduction_to_file entry).
        let input_inst_num = body_inst_num_from_path(&cur_input_path)?;

        let should_stop_time = timeout.map(|t| start_time + Duration::from_secs_f64(t));

        // R-17: the single-stage entry goes through the shared base (fl always present, first round, prints time cost;
        // the snapshot is loaded here to preserve the original error text).
        let input_snapshot = Snapshot::from_path(&cur_input_path)
            .with_context(|| format!("load {}", cur_input_path.display()))?;
        let debug = ctx.debug;
        let (_has_reduced, input_snapshot) = run_function_level_shared(
            Some(&mut *ctx),
            input_snapshot,
            &cur_input_path,
            cur_output_path,
            true,
            (true, 0),
            true,
            debug,
        )?;
        cur_input_path = cur_output_path.to_path_buf();

        // Indirect-call replacement: only in round 0 and when remaining time > 0; budget min(300, remaining).
        if let Some(stop) = should_stop_time {
            let rest_time = (stop - Instant::now()).as_secs_f64();
            if rest_time > 0.0 {
                let dd_timeout = rest_time.min(ctx.cfg.indirect_dd_timeout_cap_s);
                let snapshot_ref = &input_snapshot;
                let (_snap, _replaced) = with_backup_on_err(
                    ctx,
                    "replace_call_indirect_bak.wasm",
                    &cur_input_path,
                    cur_output_path,
                    || {
                        replace_indirect_calls(
                            ctx,
                            &cur_input_path,
                            cur_output_path,
                            snapshot_ref,
                            Some(dd_timeout),
                        )
                    },
                )?;
                cur_input_path = cur_output_path.to_path_buf();
            }
        }

        let cur_inst_num = body_inst_num_from_path(&cur_input_path)?;
        let current_size = std::fs::metadata(cur_output_path)?.len();
        // The byte delta can be negative: padding replacements can grow the file (same formula in Python, no saturation).
        let reduced_size = original_size as i64 - current_size as i64;
        Ok(ExecResult {
            exec_status: ExecStatus::Success,
            exec_taken_time: start_time.elapsed().as_secs_f64(),
            reduced_size_num: Some(reduced_size),
            reduced_inst_num: Some(input_inst_num.saturating_sub(cur_inst_num)),
            is_partial_by_timeout: should_stop_time
                .is_some_and(|t| Instant::now() >= t),
        })
    }
}

/// Total instructions of a function body (Python get_insts_num_from_parser: body instructions exclude the trailing end).
pub(crate) fn body_inst_num(snap: &Snapshot) -> u64 {
    snap.module()
        .defined_funcs
        .iter()
        .map(|f| f.insts.len().saturating_sub(1) as u64)
        .sum()
}

pub(crate) fn body_inst_num_from_path(path: &Path) -> Result<u64> {
    Ok(body_inst_num(&Snapshot::from_path(path)?))
}
