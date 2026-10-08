//! Callsite replacement (M9, the TY_ONLY strategy path).
//!
//! Mirrors Python `callsite_reduction.py`'s `replace_calls_interface_random_
//! replacement` ("random" in the name is a historical leftover; the actual strategy replaces by type match) and
//! `collect_call_like_sites_from_parser`, `_gen_random_call_replacement_
//! mutation_by_type`; weight computation mirrors `callsite_reduction_cal_weights_util.py`.
//!
//! Redundancy dispositions (per the M9 redundancy analysis):
//! - R9-1: the three-value strategy enum collapses to a boolean switch (func_level::FuncLevelConfig::
//!   disable_callsite);
//! - R9-2: the dead skip_void_calls parameter is not ported; semantics kept — void callsites are enumerated too,
//!   their replacement = drop params only, no constant padding;
//! - R9-3: the four-layer weight wrapper flattens into one function, last_appearance_weights
//!   (appeared = first-appearance position + 1; never appeared = negative infinity, sorted last);
//! - R9-6: Python's detour of generating a STACK probe description then retrofitting it into EXECUTED
//!   is not replicated (D-10); an EXECUTED probe is built directly at the position after the call;
//! - constant values are random in {0,1} (D-2: Python's random sequence is not chased).

use std::collections::{BTreeMap, BTreeSet, HashMap};
use std::path::Path;
use std::time::Instant;

use anyhow::{Context, Result};
use rand::Rng;

use e2wr_ir::ast::NodeLoc;
use e2wr_ir::module::Module;
use wasmparser::ValType;
use e2wr_ir::snapshot::Snapshot;
use e2wr_ir::Inst;

use crate::func_level::{
    dd_try_replace_callsites, FuncLevelCtx, InstEdit,
};
use crate::instrumentation::{InstrumentParams, ProbeDesc, ProbeEvent};
use crate::remap::const_inst;

/// A call / call_indirect callsite (Python `CallLikeSite`).
#[derive(Debug, Clone)]
pub struct CallLikeSite {
    /// "call" | "call_indirect" (call_indirect has no callee function index).
    pub kind: CallLikeKind,
    pub defined_func_idx: u32,
    pub call_inst_idx: u32,
    pub callee_type_idx: u32,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum CallLikeKind {
    Call,
    CallIndirect,
}

/// Mirrors `collect_call_like_sites_from_parser`: scans all defined function bodies
/// (coordinate space = Python wasmFunc.insts, trailing end excluded, D-9).
pub fn collect_call_like_sites(module: &Module) -> Vec<CallLikeSite> {
    let func_ty_idxs = module.func_type_idxs();
    let mut results = Vec::new();
    for (defined_func_idx, func) in module.defined_funcs.iter().enumerate() {
        let body_len = func.insts.len().saturating_sub(1);
        for inst_idx in 0..body_len {
            match &func.insts[inst_idx] {
                Inst::Call { function_index } => {
                    // In valid wasm func_type_idxs must contain the index (Python's out-of-range
                    // continue is a defensive branch, unreachable under strong typing).
                    let Some(callee_type_idx) = func_ty_idxs.get(*function_index as usize)
                    else {
                        continue;
                    };
                    results.push(CallLikeSite {
                        kind: CallLikeKind::Call,
                        defined_func_idx: defined_func_idx as u32,
                        call_inst_idx: inst_idx as u32,
                        callee_type_idx: *callee_type_idx,
                    });
                }
                Inst::CallIndirect { type_index, .. } => {
                    if (*type_index as usize) >= module.types.len() {
                        continue;
                    }
                    results.push(CallLikeSite {
                        kind: CallLikeKind::CallIndirect,
                        defined_func_idx: defined_func_idx as u32,
                        call_inst_idx: inst_idx as u32,
                        callee_type_idx: *type_index,
                    });
                }
                _ => {}
            }
        }
    }
    results
}

/// Weights (the flattened Python LAST_APPEARANCE strategy, R9-3):
/// a probe that appeared = first-appearance position in the event stream + 1; never appeared = negative infinity (sinks
/// in sampling order); mirrors `_build_probdd_weights_by_first_appearance_position` +
/// `_finalize` (its default 0.0 only occurs on paths overridden by -inf, not expressed separately).
pub fn last_appearance_weights(
    dumped: &[ProbeEvent],
    probe_descs: &[ProbeDesc],
) -> HashMap<u32, f64> {
    let mut first_pos_by_probe: BTreeMap<u32, usize> = BTreeMap::new();
    for (i, one) in dumped.iter().enumerate() {
        first_pos_by_probe.entry(one.probe_idx).or_insert(i);
    }
    let mut weights = HashMap::new();
    for p in probe_descs {
        weights.insert(
            p.idx,
            match first_pos_by_probe.get(&p.idx) {
                Some(pos) => (*pos + 1) as f64,
                None => f64::NEG_INFINITY,
            },
        );
    }
    weights
}

/// The replacement mutation (mirrors `_gen_random_call_replacement_mutation_by_type`):
/// drop × all params + one constant per result type (no common-prefix optimization — that is
/// remove_dead_funcs' stack-padding path padding_input_type_naive's behavior; the two differ).
/// call_indirect drops one extra i32 (the table element index).
pub fn call_replacement_edit(
    site: &CallLikeSite,
    module: &Module,
    rng: &mut impl Rng,
) -> Result<InstEdit> {
    let ty = &module.types[site.callee_type_idx as usize];
    let mut param_types: Vec<ValType> = ty.params.clone();
    if site.kind == CallLikeKind::CallIndirect {
        param_types.push(ValType::I32);
    }
    let mut new_insts = Vec::with_capacity(param_types.len() + ty.results.len());
    for _ in param_types {
        new_insts.push(Inst::Drop);
    }
    for r in &ty.results {
        new_insts.push(const_inst(r, rng)?);
    }
    Ok(InstEdit {
        func_idx: site.defined_func_idx,
        inst_idx: site.call_inst_idx,
        new_insts,
    })
}

/// Mirrors `replace_calls_interface_random_replacement` (the callsite replacement body;
/// NodeShrinkPass.replace_callsites calls it with save_ratio=0.95).
/// The probe goes right after the call (call_inst_idx + 1); instrumentation uses the general parser
/// (only_executed_probe=False, the P-25 dual-parser semantics), no output cap.
pub fn replace_callsites(
    ctx: &FuncLevelCtx,
    cur_input_path: &Path,
    cur_output_path: &Path,
    input_snapshot: &Snapshot,
    dd_timeout_s: Option<f64>,
) -> Result<(Snapshot, BTreeSet<u32>)> {
    let t0 = Instant::now();
    let module = input_snapshot.module();
    let call_sites = collect_call_like_sites(module);
    if call_sites.is_empty() {
        if cur_input_path != cur_output_path {
            std::fs::copy(cur_input_path, cur_output_path)?;
        }
        return Ok((input_snapshot.full_copy(), BTreeSet::new()));
    }

    // D-10/R9-6: build the EXECUTED probe directly (Python routes through a STACK description).
    // Void callsites are not skipped (Python passes skip_void_calls=False, R9-2).
    let mut probe_descs: Vec<ProbeDesc> = Vec::new();
    let mut probe_idx2call_site: BTreeMap<u32, CallLikeSite> = BTreeMap::new();
    for (i, cs) in call_sites.iter().enumerate() {
        let probe_idx = i as u32;
        probe_idx2call_site.insert(probe_idx, cs.clone());
        probe_descs.push(ProbeDesc {
            idx: probe_idx,
            loc: NodeLoc {
                func_idx: cs.defined_func_idx,
                inst_idx: cs.call_inst_idx + 1,
            },
        });
    }
    if cur_input_path != cur_output_path {
        std::fs::copy(cur_input_path, cur_output_path).with_context(|| {
            format!("copy {} -> {}", cur_input_path.display(), cur_output_path.display())
        })?;
    }

    let params = InstrumentParams {
        allocated_time: crate::instrumentation::INSTRUMENTATION_TIMEOUT,
        only_executed_probe: false,
        max_output_time: None,
    };
    let dumped =
        ctx.instr.instrument_multiple_places_and_get_result(input_snapshot, &probe_descs, &params)?;
    let weights = last_appearance_weights(&dumped, &probe_descs);
    let mut rng = rand::thread_rng();
    let mut cand_idx2edit: BTreeMap<u32, InstEdit> = BTreeMap::new();
    for probe_idx in probe_idx2call_site.keys() {
        let cs = &probe_idx2call_site[probe_idx];
        cand_idx2edit.insert(*probe_idx, call_replacement_edit(cs, module, &mut rng)?);
    }

    let universe: Vec<u32> = cand_idx2edit.keys().copied().collect();
    if universe.is_empty() {
        return Ok((input_snapshot.full_copy(), BTreeSet::new()));
    }

    let dd_tmp_path = ctx.dir.tmp_dir.join("dd_tmp_replace_calls_random.wasm");
    let replaced = dd_try_replace_callsites(
        ctx,
        input_snapshot,
        &cand_idx2edit,
        &universe,
        &dd_tmp_path,
        cur_input_path,
        Some(cur_output_path),
        0.95,
        dd_timeout_s,
        Some(&weights),
    )?;
    println!(
        "Reducing CallSites(RandomReplacement) done, taking {:.1}s, replaced {}/{} callsites.",
        t0.elapsed().as_secs_f64(),
        replaced.len(),
        universe.len()
    );
    Ok((Snapshot::from_path(cur_output_path)?, replaced))
}

