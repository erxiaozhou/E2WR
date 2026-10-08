//! The UnusedDefReducer main loop (M4 latter half).
//!
//! Mirrors Python `ReduceFrameWork/DefinitionReducer/UnusedDefReducer.py`'s
//! `UnusedDefReducer.reduce` and `try_remove`.
//!
//! Behavior notes (surveyed and confirmed 2026-09-22):
//! - Each round reloads the snapshot from the current input, runs the eight detections, and builds the candidate deletion list in a fixed order:
//!   types → functions → data segments → element segments → globals → memories → tables → start (index -1) →
//!   all exports → unused imported functions. The order shapes ProbDD's exploration path and must be preserved.
//! - ProbDD converges on a must-keep set with try_remove as the test function; after each round the current input points at the output,
//!   until the candidates are empty or a round deletes nothing.
//! - The baseline entry enables online p0 inference (initialP=0.1); this implementation uses the same config.
//! - reduced_size_num = the number of definitions deleted (P-7).

use std::collections::BTreeSet;
use std::path::{Path, PathBuf};
use std::time::{Duration, Instant};

use anyhow::{Context, Result};

use e2wr_dd::factory::DdFactory;
use e2wr_dd::oracle::Oracle;
use e2wr_dd::probdd::ProbDD;
use e2wr_ir::mutation::apply_mutation_and_encode;
use e2wr_ir::snapshot::{SectionKind, Snapshot};
use crate::detect;

use crate::common::{DirSystem, ExecResult, ExecStatus};
use crate::remap::{remap_all, DeleteSet};

/// Baseline config: online p0 inference + initialP=0.1 (a fresh instance per round, same as Python).
fn default_uur_factory() -> DdFactory {
    let mut factory = DdFactory::default();
    factory.enable_p0_pred();
    factory.set_default_initial_p(0.1);
    factory
}

/// A candidate deletion item: section kind + index within the section (start uses index -1, matching Python
/// `_DefDesc(SectionType.Start, -1)`).
pub type DefDesc = (SectionKind, i32);

pub struct UurPass<'a> {
    oracle: &'a Oracle,
    dir: DirSystem,
    debug: bool,
    /// Delta-debugging factory config (online p0 inference + initial probability). The Python side's
    /// `ProbDDFactory.get_default_probdd` reads a process-level global (the full-pipeline entry's
    /// --init_p0/--full_wo_update_probdd_p0 thus affects UUR); M12 wires the entry through
    /// [`UurPass::with_dd_factory`], defaulting to the same as the standalone baseline.
    dd_factory: DdFactory,
}

impl<'a> UurPass<'a> {
    pub fn new(oracle: &'a Oracle, result_dir: &Path, debug: bool) -> Self {
        UurPass {
            oracle,
            dir: DirSystem::new(result_dir),
            debug,
            dd_factory: default_uur_factory(),
        }
    }

    /// M12: injected by the full-pipeline entry per CLI config (the explicit equivalent
    /// of Python's process-level ProbDDFactory).
    pub fn with_dd_factory(
        oracle: &'a Oracle,
        result_dir: &Path,
        dd_factory: DdFactory,
        debug: bool,
    ) -> Self {
        UurPass {
            oracle,
            dir: DirSystem::new(result_dir),
            debug,
            dd_factory,
        }
    }

    /// Mirrors `UnusedDefReducer.reduce`. Detection/mutation errors propagate upward
    /// (Python does not catch them either); a failed oracle verdict merely means "deletion combination rejected", not an error.
    pub fn reduce(
        &self,
        cur_input_path: &Path,
        cur_output_path: &Path,
        timeout: Option<f64>,
    ) -> Result<ExecResult> {
        let start_time = Instant::now();
        let to_stop_time = timeout.map(|t| start_time + Duration::from_secs_f64(t));
        let original_input = PathBuf::from(cur_input_path);
        let mut cur_input_path = original_input.clone();
        let mut total_removed_num: u64 = 0;
        let mut cannot_remove_num: Option<u64> = None;

        loop {
            let snap = Snapshot::from_path(&cur_input_path)
                .with_context(|| format!("load {}", cur_input_path.display()))?;
            let module = snap.module();
            let candidates = build_candidates(module);

            let n = candidates.len() as u64;
            if n == 0 || Some(n) == cannot_remove_num {
                break;
            }
            let all: BTreeSet<DefDesc> = candidates.iter().copied().collect();

            let tmp_used = self.dir.tmp_used_path.clone();
            let output = PathBuf::from(cur_output_path);
            // First-error short-circuit flag (the error itself is recorded and re-raised by run_probdd_capturing).
            let mut remap_failed = false;
            let oracle = self.oracle;
            let debug = self.debug;

            let mut try_remove = |keep: &[DefDesc]| -> Result<bool> {
                // After a remap error, make subsequent verdicts fail immediately and re-raise after the main loop converges
                // (in Python this path aborts by raising; behaviorally equal without losing context;
                //   the remap_failed flag preserves the first error — the helper records the last Err).
                if remap_failed {
                    return Ok(false);
                }
                if let Some(t) = to_stop_time {
                    if Instant::now() > t {
                        return Ok(false);
                    }
                }
                let keep_set: BTreeSet<DefDesc> = keep.iter().copied().collect();
                let del = build_delete_set(&all, &keep_set);
                let mutations = match remap_all(module, &del) {
                    Ok(m) => m,
                    Err(e) => {
                        // In Python this path aborts by raising; record it here and treat as "deletion failed".
                        remap_failed = true;
                        return Err(e);
                    }
                };
                if let Err(e) = apply_mutation_and_encode(&snap, &mutations, &tmp_used) {
                    if debug {
                        eprintln!("UUR encode failed: {e:#}");
                    }
                    return Ok(false);
                }
                // D-14: the debug validation of UnusedDefReducer's batch probing (Python
                // :919-928, with the undeclared-reference exemption for elem).
                crate::common::debug_validate_wasm_file(
                    crate::common::DebugValidate::exempt_elem(debug),
                    &tmp_used,
                )?;
                Ok(match oracle.check(&tmp_used) {
                    Ok(true) => {
                        if std::fs::copy(&tmp_used, &output).is_err() {
                            return Ok(false);
                        }
                        true
                    }
                    Ok(false) => false,
                    Err(e) => {
                        if debug {
                            eprintln!("oracle error: {e:#}");
                        }
                        false
                    }
                })
            };

            // A fresh instance per round, same as Python; config comes from the factory config at construction.
            let factory = self.dd_factory.clone();
            let mut dd: ProbDD<DefDesc> = factory.create_probdd();
            let minimal_config =
                crate::common::run_probdd_capturing(&mut dd, &candidates, None, None, &mut try_remove)
                    .map_err(|e| e.context("try_remove remap failed"))?;

            cannot_remove_num = Some(minimal_config.len() as u64);
            let removed_num = n - cannot_remove_num.unwrap();
            if removed_num == 0 {
                break;
            }
            total_removed_num += removed_num;
            cur_input_path = cur_output_path.to_path_buf();
        }

        let result = total_removed_num > 0;
        if !result {
            std::fs::copy(&original_input, cur_output_path).with_context(|| {
                format!("copy {} -> {}", original_input.display(), cur_output_path.display())
            })?;
        }
        Ok(ExecResult {
            exec_status: if result { ExecStatus::Success } else { ExecStatus::ExecFailed },
            exec_taken_time: start_time.elapsed().as_secs_f64(),
            reduced_size_num: Some(total_removed_num as i64),
            reduced_inst_num: Some(0),
            is_partial_by_timeout: to_stop_time
                .map(|t| Instant::now() >= t)
                .unwrap_or(false),
        })
    }
}

/// Builds the candidate deletion list in the fixed order (the order = the construction order of Python `to_remove_descs`).
fn build_candidates(module: &e2wr_ir::Module) -> Vec<DefDesc> {
    // One pass produces all eight results (previously each of the eight detects scanned the instructions).
    let d = detect::detect_all(module);
    let mut out: Vec<DefDesc> = Vec::new();
    let push_section = |sec: SectionKind, idxs: &BTreeSet<u32>, out: &mut Vec<DefDesc>| {
        for i in idxs {
            out.push((sec, *i as i32));
        }
    };
    push_section(SectionKind::Type, &d.types.unused, &mut out);
    push_section(SectionKind::Function, &d.funcs.unused, &mut out);
    push_section(SectionKind::Data, &d.datas.unused, &mut out);
    push_section(SectionKind::Element, &d.elemsegs.unused, &mut out);
    push_section(SectionKind::Global, &d.globals.unused, &mut out);
    push_section(SectionKind::Memory, &d.memories.unused, &mut out);
    push_section(SectionKind::Table, &d.tables.unused, &mut out);
    if module.start_sec_data.is_some() {
        out.push((SectionKind::Start, -1));
    }
    for i in 0..module.exports.len() as i32 {
        out.push((SectionKind::Export, i));
    }
    push_section(SectionKind::Import, &d.import_funcs.unused, &mut out);
    out
}

/// Candidate universe minus the must-keep set → per-index-space deletion sets.
fn build_delete_set(all: &BTreeSet<DefDesc>, keep: &BTreeSet<DefDesc>) -> DeleteSet {
    let mut del = DeleteSet::default();
    for &(sec, idx) in all {
        if keep.contains(&(sec, idx)) {
            continue;
        }
        match sec {
            SectionKind::Type => {
                del.types.insert(idx as u32);
            }
            SectionKind::Function => {
                del.funcs.insert(idx as u32);
            }
            SectionKind::Data => {
                del.datas.insert(idx as u32);
            }
            SectionKind::Element => {
                del.elemsegs.insert(idx as u32);
            }
            SectionKind::Global => {
                del.globals.insert(idx as u32);
            }
            SectionKind::Memory => {
                del.mems.insert(idx as u32);
            }
            SectionKind::Table => {
                del.tables.insert(idx as u32);
            }
            SectionKind::Start => {
                del.start = true;
            }
            SectionKind::Export => {
                del.exports.insert(idx as u32);
            }
            SectionKind::Import => {
                del.imports.insert(idx as u32);
            }
            _ => {}
        }
    }
    del
}
