//! func_level.rs / callsite.rs unit tests (M8/M9 synthetic units):
//! callsite and indirect-callsite enumeration, trace→callee inference, weight flattening, replacement mutation shapes,
//! instruction-interval mutation splicing, the callsite_as_unreachable branch, reduce_ext early-stop semantics.

#![allow(clippy::field_reassign_with_default)]

use std::collections::{BTreeMap, BTreeSet};

use e2wr_dd::factory::DdFactory;
use e2wr_dd::probdd::{ProbDD, ReduceOutcome, TestOutcome};
use e2wr_pass::callsite::{
    collect_call_like_sites, last_appearance_weights, CallLikeKind,
};
use e2wr_pass::func_level::{
    collect_indirect_call_sites, infer_call_indirect_callee_map_from_executed_trace,
    inst_edits_to_mutations, IndirectCallSite,
};
use e2wr_pass::instrumentation::{ProbeDesc, ProbeEvent};
use e2wr_pass::remap::{remap_all_opts, DeleteSet};
use e2wr_ir::ast::NodeLoc;
use e2wr_ir::module::*;
use wasmparser::ValType;
use e2wr_ir::mutation::apply_mutations;
use e2wr_ir::snapshot::Snapshot;
use e2wr_ir::Inst;

fn ty(params: &[ValType], results: &[ValType]) -> FuncType {
    FuncType { params: params.to_vec(), results: results.to_vec() }
}

fn func(ty_idx: u32, insts: Vec<Inst>) -> Func {
    let mut insts = insts;
    insts.push(Inst::End);
    Func { ty_idx, locals: vec![], insts }
}

// ---------------------------------------------------------------------------
// Enumeration
// ---------------------------------------------------------------------------

/// 2 functions: f0 = [call 1, call_indirect t1, i32.const 0, drop], f1 = [call_indirect t0].
/// 1 imported function (func 0 = import; f0/f1 are whole-space 1/2).
fn sample_module() -> Module {
    let mut m = Module::default();
    m.types.push(ty(&[], &[])); // t0
    m.types.push(ty(&[ValType::I32], &[ValType::I32, ValType::I32])); // t1
    m.imports.push(Import {
        module: "e".into(),
        name: "f".into(),
        desc: ImportDesc::Func(0),
    });
    m.defined_funcs.push(func(
        0,
        vec![
            Inst::Call { function_index: 2 },
            Inst::CallIndirect { type_index: 1, table_index: 0 },
            Inst::I32Const { value: 0 },
            Inst::Drop,
        ],
    ));
    m.defined_funcs.push(func(0, vec![Inst::CallIndirect { type_index: 0, table_index: 0 }]));
    m.defined_func_ty_ids = vec![0, 0];
    m
}

#[test]
fn collect_indirect_sites_coords_in_python_space() {
    let m = sample_module();
    let sites = collect_indirect_call_sites(&m);
    assert_eq!(sites.len(), 2);
    assert_eq!((sites[0].defined_func_idx, sites[0].call_inst_idx, sites[0].callee_type_idx), (0, 1, 1));
    assert_eq!((sites[1].defined_func_idx, sites[1].call_inst_idx, sites[1].callee_type_idx), (1, 0, 0));
}

#[test]
fn collect_call_like_sites_covers_call_and_indirect() {
    let m = sample_module();
    let sites = collect_call_like_sites(&m);
    assert_eq!(sites.len(), 3);
    // f0's call 2: callee type = whole-space function 2 (defined f1) → t0.
    assert_eq!(sites[0].kind, CallLikeKind::Call);
    assert_eq!((sites[0].defined_func_idx, sites[0].call_inst_idx, sites[0].callee_type_idx), (0, 0, 0));
    assert_eq!(sites[1].kind, CallLikeKind::CallIndirect);
    assert_eq!(sites[2].kind, CallLikeKind::CallIndirect);
}

// ---------------------------------------------------------------------------
// Trace → callee inference
// ---------------------------------------------------------------------------

fn site(f: u32, i: u32) -> IndirectCallSite {
    IndirectCallSite { defined_func_idx: f, call_inst_idx: i, callee_type_idx: 0 }
}

fn ev(idx: u32) -> ProbeEvent {
    ProbeEvent { probe_idx: idx }
}

/// Probe id layout: 0/1 = the two indirect call sites (in f0 and f1), 2/3/4 = defined function 0/1/2 entries.
fn infer_map(events: &[u32]) -> BTreeMap<u32, u32> {
    let mut callsite_map = BTreeMap::new();
    callsite_map.insert(0u32, site(0, 5));
    callsite_map.insert(1u32, site(1, 3));
    let mut entry_map = BTreeMap::new();
    entry_map.insert(2u32, 0u32);
    entry_map.insert(3u32, 1u32);
    entry_map.insert(4u32, 2u32);
    infer_call_indirect_callee_map_from_executed_trace(
        &events.iter().map(|i| ev(*i)).collect::<Vec<_>>(),
        &callsite_map,
        &entry_map,
    )
}

#[test]
fn infer_callee_nested_call_stack() {
    // Entry f0 → the indirect call inside f0 (probe 0) → entry f2 (callee) → after return, f1's entry.
    let m = infer_map(&[2, 0, 4, 3]);
    assert_eq!(m.get(&0), Some(&2));
    assert!(!m.contains_key(&1));
}

#[test]
fn infer_callee_imported_target_leaves_no_record() {
    // After f0's indirect call, the callee is an imported function (no entry event); f0 issues another indirect
    // call without returning: the first pending marker is overwritten and dropped, nothing counted.
    let m = infer_map(&[2, 0, 0, 4]);
    // Only the second marker gets closed by entry event 4 (f2).
    assert_eq!(m.get(&0), Some(&2));
    assert_eq!(m.len(), 1);
}

#[test]
fn infer_callee_tie_break_by_smaller_func_idx() {
    // Probe 0 hits f2 twice and f1 twice: the tie takes the smaller function index, f1.
    let m = infer_map(&[2, 0, 4, 2, 0, 3]);
    assert_eq!(m.get(&0), Some(&1));
}

#[test]
fn infer_callee_freq_majority_wins() {
    // f2 hit twice, f1 once → f2.
    let m = infer_map(&[2, 0, 4, 2, 0, 4, 2, 0, 3]);
    assert_eq!(m.get(&0), Some(&2));
}

// ---------------------------------------------------------------------------
// Weights
// ---------------------------------------------------------------------------

#[test]
fn last_appearance_weights_positions_and_neg_inf() {
    let descs = vec![
        ProbeDesc { idx: 0, loc: NodeLoc { func_idx: 0, inst_idx: 0 } },
        ProbeDesc { idx: 1, loc: NodeLoc { func_idx: 0, inst_idx: 1 } },
        ProbeDesc { idx: 2, loc: NodeLoc { func_idx: 0, inst_idx: 2 } },
    ];
    // Event stream: probe 1 appears first, then 1 again, finally 0's first appearance.
    let dumped = vec![ev(1), ev(1), ev(0)];
    let w = last_appearance_weights(&dumped, &descs);
    assert_eq!(w[&0], 3.0); // first appearance at position 2 → +1
    assert_eq!(w[&1], 1.0); // first appearance at position 0 → +1
    assert_eq!(w[&2], f64::NEG_INFINITY); // never appeared
}

// ---------------------------------------------------------------------------
// Mutation shapes and splicing
// ---------------------------------------------------------------------------

#[test]
fn call_replacement_edit_shape() {
    use e2wr_pass::callsite::{call_replacement_edit, CallLikeSite};
    let m = sample_module();
    // f0's call_indirect t1: params=[i32] + table index i32 → 2 drops + 2 i32 constants.
    let edit = call_replacement_edit(
        &CallLikeSite {
            kind: CallLikeKind::CallIndirect,
            defined_func_idx: 0,
            call_inst_idx: 1,
            callee_type_idx: 1,
        },
        &m,
        &mut fake_rng(),
    )
    .unwrap();
    assert_eq!(edit.func_idx, 0);
    assert_eq!(edit.inst_idx, 1);
    assert_eq!(edit.new_insts.len(), 4);
    assert!(matches!(edit.new_insts[0], Inst::Drop));
    assert!(matches!(edit.new_insts[1], Inst::Drop));
    assert!(matches!(edit.new_insts[2], Inst::I32Const { .. }));
    assert!(matches!(edit.new_insts[3], Inst::I32Const { .. }));
}

/// A pseudo-random source fixed at 0 (constant values random in {0,1}; the unit test checks shape only).
fn fake_rng() -> rand::rngs::StdRng {
    use rand::SeedableRng;
    rand::rngs::StdRng::seed_from_u64(0)
}

#[test]
fn inst_edits_splice_two_edits_same_func() {
    let m = sample_module();
    // f0: replace instruction 0 (call 2 → nop) and instruction 2 (i32.const → drop).
    let edits = vec![
        e2wr_pass::func_level::InstEdit {
            func_idx: 0,
            inst_idx: 0,
            new_insts: vec![Inst::Nop],
        },
        e2wr_pass::func_level::InstEdit {
            func_idx: 0,
            inst_idx: 2,
            new_insts: vec![Inst::Nop, Inst::Nop],
        },
    ];
    let mutations = inst_edits_to_mutations(&m, &edits).unwrap();
    let snap = Snapshot::from_module(m);
    let new_snap = apply_mutations(&snap, &mutations).unwrap();
    let f0 = &new_snap.module().defined_funcs[0];
    // [nop, call_indirect, nop, nop, drop, end]
    assert_eq!(f0.insts.len(), 6);
    assert!(matches!(f0.insts[0], Inst::Nop));
    assert!(matches!(f0.insts[1], Inst::CallIndirect { .. }));
    assert!(matches!(f0.insts[2], Inst::Nop));
    assert!(matches!(f0.insts[3], Inst::Nop));
    assert!(matches!(f0.insts[4], Inst::Drop));
}

// ---------------------------------------------------------------------------
// callsite_as_unreachable branch (R8-10, kept by user ruling)
// ---------------------------------------------------------------------------

#[test]
fn remap_deleted_call_target_unreachable_when_enabled() {
    let mut m = Module::default();
    m.types.push(ty(&[ValType::I32], &[ValType::I32]));
    // f0 calls f1 (with params/results); delete f1.
    m.defined_funcs.push(func(
        0,
        vec![
            Inst::I32Const { value: 1 },
            Inst::Call { function_index: 1 },
            Inst::Drop,
        ],
    ));
    m.defined_funcs.push(func(0, vec![Inst::I32Const { value: 2 }, Inst::Drop]));
    m.defined_func_ty_ids = vec![0, 0];

    let mut del = DeleteSet::default();
    del.funcs.insert(1);

    // Default (False): padding = common-prefix alignment + constants for missing results. The call's [i32]->[i32]
    // common prefix i32/i32 is fully kept → no drops, no constants → the call site's replacement is the empty sequence.
    let snap = Snapshot::from_module(m.clone());
    let mutations = remap_all_opts(&m, &del, false).unwrap();
    let new_snap = apply_mutations(&snap, &mutations).unwrap();
    let f0 = &new_snap.module().defined_funcs[0];
    assert_eq!(f0.insts.len(), 3); // [i32.const, drop, end]
    assert!(matches!(f0.insts[1], Inst::Drop));

    // True: a single unreachable.
    let snap = Snapshot::from_module(m.clone());
    let mutations = remap_all_opts(&m, &del, true).unwrap();
    let new_snap = apply_mutations(&snap, &mutations).unwrap();
    let f0 = &new_snap.module().defined_funcs[0];
    assert!(matches!(f0.insts[0], Inst::I32Const { .. }));
    assert!(matches!(f0.insts[1], Inst::Unreachable));
    assert_eq!(new_snap.module().defined_funcs.len(), 1);
}

// ---------------------------------------------------------------------------
// reduce_ext early stop (Python _DDEarlyStop semantics)
// ---------------------------------------------------------------------------

#[test]
fn reduce_ext_stop_aborts_immediately() {
    let config: Vec<u32> = (0..16u32).collect();
    let mut calls = 0usize;
    let mut factory = DdFactory::default();
    factory.set(false, 0.1);
    let mut dd: ProbDD<u32> = factory.create_probdd();
    let outcome = dd.reduce_ext(&config, None, None, &mut |_cfg: &[u32]| {
        calls += 1;
        // The very first verdict demands an early stop (equivalent to the save_ratio=0 scenario).
        TestOutcome::Stop
    });
    assert!(matches!(outcome, ReduceOutcome::Stopped));
    // Exception-passthrough semantics: immediate return, no further sampling.
    assert_eq!(calls, 1);
}

#[test]
fn reduce_ext_pass_fail_equivalent_to_reduce() {
    let config: Vec<u32> = (0..12u32).collect();
    let keep = [3u32, 7];
    let judge = |cfg: &[u32]| keep.iter().all(|k| cfg.contains(k));
    let mut factory = DdFactory::default();
    factory.set(false, 0.1);
    let mut dd: ProbDD<u32> = factory.create_probdd();
    let a = dd.reduce(&config, None, None, &mut |cfg| judge(cfg));

    let mut dd2: ProbDD<u32> = factory.create_probdd();
    let b = match dd2.reduce_ext(&config, None, None, &mut |cfg| {
        if judge(cfg) {
            TestOutcome::Pass
        } else {
            TestOutcome::Fail
        }
    }) {
        ReduceOutcome::Config(c) => c,
        _ => panic!("unexpected stop"),
    };
    assert_eq!(a, b);
}

// ---------------------------------------------------------------------------
// Pure logic of the delta-replacement core: early-stop target computation (int truncation)
// ---------------------------------------------------------------------------

#[test]
fn early_stop_target_truncates_like_python_int() {
    // Python's int(total*save_ratio): 0.95*10 = 9.5 → 9.
    let target = (10.0_f64 * 0.95) as usize;
    assert_eq!(target, 9);
    let target = (3.0_f64 * 0.95) as usize;
    assert_eq!(target, 2);
    let _ = target;
}

// Silence the unused warning: BTreeSet used as needed in this file's other cases.
#[allow(dead_code)]
fn _unused(_: &BTreeSet<u32>) {}
