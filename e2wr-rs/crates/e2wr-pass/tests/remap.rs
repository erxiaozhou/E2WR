//! remap.rs unit tests: index-remapping assertions on synthetic modules + corpus artifact validate +
//! the p10_0 type-deletion targeted regression (P-17) + combination scenarios.

#![allow(clippy::field_reassign_with_default)]

use std::collections::BTreeSet;
use std::path::PathBuf;

use e2wr_pass::detect;
use e2wr_pass::remap::{remap_all, DeleteSet};
use e2wr_ir::module::*;
use wasmparser::ValType;
use e2wr_ir::mutation::apply_mutations;
use e2wr_ir::snapshot::Snapshot;
use e2wr_ir::Inst;

fn ty(params: &[ValType], results: &[ValType]) -> FuncType {
    FuncType { params: params.to_vec(), results: results.to_vec() }
}

fn memarg(memory: u32) -> MemArg {
    MemArg { align: 2, max_align: 2, offset: 0, memory }
}

fn func(ty_idx: u32, insts: Vec<Inst>) -> Func {
    let mut insts = insts;
    insts.push(Inst::End);
    Func { ty_idx, locals: vec![], insts }
}

fn set(v: &[u32]) -> BTreeSet<u32> {
    v.iter().copied().collect()
}

fn apply(module: &Module, del: &DeleteSet) -> Module {
    let snap = Snapshot::from_module(module.clone());
    let mutations = remap_all(module, del).expect("remap_all");
    let new_snap = apply_mutations(&snap, &mutations).expect("apply_mutations");
    new_snap.module().clone()
}

fn import_func(module_name: &str, name: &str, ty_idx: u32) -> Import {
    Import { module: module_name.into(), name: name.into(), desc: ImportDesc::Func(ty_idx) }
}

fn import_global(module_name: &str, name: &str) -> Import {
    Import {
        module: module_name.into(),
        name: name.into(),
        desc: ImportDesc::Global(GlobalType { val_ty: ValType::I32, mutable: false }),
    }
}

fn g_init(v: i32) -> Global {
    Global {
        ty: GlobalType { val_ty: ValType::I32, mutable: false },
        init_expr: vec![Inst::I32Const { value: v }, Inst::End],
    }
}

fn table(min: u64) -> Table {
    Table {
        ty: TableType { elem_ty: RefType::FUNC, limits: Limits { min, max: None } },
        init_expr: None,
    }
}

fn memory(min: u64) -> Memory {
    Memory { ty: MemType { limits: Limits { min, max: None } } }
}

fn active_elem(table_idx: Option<u32>, idxs: Vec<u32>) -> ElemSeg {
    ElemSeg {
        mode: ElemMode::Active {
            table_idx,
            offset: vec![Inst::I32Const { value: 0 }, Inst::End],
        },
        payload: ElemPayload::FuncIdxs(idxs),
    }
}

/// Delete a global: global.get/set renumbered, exports deleted/renumbered (the P-19 whole-space judgment).
#[test]
fn remove_globals_remaps_insts_and_exports() {
    let mut m = Module::default();
    m.types = vec![ty(&[], &[])];
    m.imports = vec![import_global("e", "g")];
    // Whole space: 0=imported global, 1=G0, 2=G1 (deleted), 3=G2.
    m.defined_globals = vec![g_init(0), g_init(1), g_init(2)];
    m.defined_func_ty_ids = vec![0];
    m.defined_funcs = vec![func(0, vec![
        Inst::GlobalGet { global_index: 3 },
        Inst::GlobalSet { global_index: 1 },
        Inst::Drop,
    ])];
    m.exports = vec![
        Export { name: "g1".into(), desc: ExportDesc::Global(2) }, // points at the deleted one → dropped
        Export { name: "g2".into(), desc: ExportDesc::Global(3) }, // renumbered 3→2
    ];
    let del = DeleteSet { globals: set(&[1]), ..Default::default() };
    let new = apply(&m, &del);
    assert_eq!(new.defined_globals.len(), 2);
    assert_eq!(
        new.defined_funcs[0].insts,
        vec![
            Inst::GlobalGet { global_index: 2 },
            Inst::GlobalSet { global_index: 1 },
            Inst::Drop,
            Inst::End,
        ]
    );
    assert_eq!(new.exports.len(), 1);
    assert_eq!(new.exports[0].name, "g2");
    assert_eq!(new.exports[0].desc, ExportDesc::Global(2));
}

/// Delete a function: calls renumbered, ref.func repointed at the first post-deletion user function, element segment function indices rewritten,
/// exports deleted/renumbered, start renumbered, Function/Code/Import section deletions.
#[test]
fn remove_dead_funcs_full() {
    let mut m = Module::default();
    m.types = vec![ty(&[], &[]), ty(&[ValType::I32], &[])];
    m.imports = vec![import_func("e", "a", 0), import_func("e", "b", 0)];
    // Whole space: 0,1=imported functions; 2=f0, 3=f1 (deleted), 4=f2.
    m.defined_func_ty_ids = vec![0, 0, 1];
    m.defined_funcs = vec![
        func(0, vec![
            Inst::Call { function_index: 0 },
            Inst::Call { function_index: 4 },
            Inst::RefFunc { function_index: 3 },
            Inst::Drop,
        ]),
        func(0, vec![]),
        func(1, vec![]),
    ];
    m.exports = vec![
        Export { name: "f1".into(), desc: ExportDesc::Func(3) }, // deleted → dropped
        Export { name: "f2".into(), desc: ExportDesc::Func(4) }, // renumbered 4→3
    ];
    m.start_sec_data = Some(4); // renumbered 4→3
    m.elem_sec_datas = vec![active_elem(Some(0), vec![3, 4])];
    m.defined_table_datas = vec![table(1)];

    let del = DeleteSet { funcs: set(&[1]), ..Default::default() };
    let new = apply(&m, &del);

    assert_eq!(new.defined_funcs.len(), 2);
    assert_eq!(new.imports.len(), 2);
    // call 0 unchanged; call 4→3; ref.func 3→padding (2-0=2, the first post-deletion user function).
    assert_eq!(
        new.defined_funcs[0].insts,
        vec![
            Inst::Call { function_index: 0 },
            Inst::Call { function_index: 3 },
            Inst::RefFunc { function_index: 2 },
            Inst::Drop,
            Inst::End,
        ]
    );
    assert_eq!(new.exports.len(), 1);
    assert_eq!(new.exports[0].desc, ExportDesc::Func(3));
    assert_eq!(new.start_sec_data, Some(3));
    // Element segment function indices: 3 (deleted)→padding 2; 4→3.
    match &new.elem_sec_datas[0].payload {
        ElemPayload::FuncIdxs(idxs) => assert_eq!(idxs, &vec![2, 3]),
        _ => panic!(),
    }
}

/// A call pointing at a deleted function → padding (the common prefix untouched, extra params dropped, missing results padded with constants);
/// return_call handled the same way plus a return (P-20).
#[test]
fn call_to_dead_func_padded() {
    let mut m = Module::default();
    // Deleted function type: (i32,i32)->(f32): common prefix 0, drop 2 params, pad 1 f32.
    m.types = vec![ty(&[], &[]), ty(&[ValType::I32, ValType::I32], &[ValType::F32])];
    // f0 calls f1 (type (i32,i32)->(i32); two constants pushed before the call).
    m.defined_func_ty_ids = vec![0, 1];
    m.defined_funcs = vec![
        func(0, vec![
            Inst::I32Const { value: 1 },
            Inst::I32Const { value: 2 },
            Inst::Call { function_index: 1 },
            Inst::Drop,
        ]),
        func(1, vec![Inst::LocalGet { local_index: 0 }, Inst::LocalGet { local_index: 1 }, Inst::I32Add]),
    ];
    let del = DeleteSet { funcs: set(&[1]), ..Default::default() };
    let new = apply(&m, &del);
    assert_eq!(new.defined_funcs.len(), 1);
    let insts = &new.defined_funcs[0].insts;
    // Padding structure: the two original i32.const pushes + two drops + one f32.const + the original drop + end
    // (constant values random, D-2).
    assert!(matches!(insts[0], Inst::I32Const { .. }));
    assert!(matches!(insts[1], Inst::I32Const { .. }));
    assert_eq!(insts[2], Inst::Drop);
    assert_eq!(insts[3], Inst::Drop);
    assert!(matches!(insts[4], Inst::F32Const { .. }));
    assert_eq!(insts[5], Inst::Drop);
    assert_eq!(insts[6], Inst::End);
    assert_eq!(insts.len(), 7);
}

/// Common-prefix semantics (vs Python get_padding_meta_data): params and results compared position by position toward the stack bottom,
/// equal ones cancel — (i32,i32)->(i32) drops only 1, no constant padding.
#[test]
fn padding_common_prefix_semantics() {
    let mut m = Module::default();
    m.types = vec![ty(&[], &[]), ty(&[ValType::I32, ValType::I32], &[ValType::I32])];
    m.defined_func_ty_ids = vec![0, 1];
    m.defined_funcs = vec![
        func(0, vec![
            Inst::I32Const { value: 1 },
            Inst::I32Const { value: 2 },
            Inst::Call { function_index: 1 },
            Inst::Drop,
        ]),
        func(1, vec![Inst::LocalGet { local_index: 0 }, Inst::LocalGet { local_index: 1 }, Inst::I32Add]),
    ];
    let del = DeleteSet { funcs: set(&[1]), ..Default::default() };
    let new = apply(&m, &del);
    assert_eq!(
        new.defined_funcs[0].insts,
        vec![
            Inst::I32Const { value: 1 },
            Inst::I32Const { value: 2 },
            Inst::Drop,
            Inst::Drop,
            Inst::End,
        ]
    );
}

/// return_call pointing at a deleted function → padding + return (P-20); return_call pointing at a kept function → renumbering.
#[test]
fn return_call_handled() {
    let mut m = Module::default();
    m.types = vec![ty(&[], &[]), ty(&[ValType::I32], &[ValType::F32])];
    // f0: return_call f1 (deleted) and return_call f2 (kept).
    m.defined_func_ty_ids = vec![1, 1, 1];
    m.defined_funcs = vec![
        func(1, vec![Inst::I32Const { value: 3 }, Inst::ReturnCall { function_index: 1 }]),
        func(1, vec![Inst::I32Const { value: 0 }]),
        func(1, vec![Inst::I32Const { value: 0 }]),
    ];
    let del = DeleteSet { funcs: set(&[1]), ..Default::default() };
    let new = apply(&m, &del);
    // f1's return_call → drop + f32.const + return.
    assert_eq!(
        new.defined_funcs[0].insts,
        vec![
            Inst::I32Const { value: 3 },
            Inst::Drop,
            new.defined_funcs[0].insts[2].clone(),
            Inst::Return,
            Inst::End,
        ]
    );
    assert!(matches!(new.defined_funcs[0].insts[2], Inst::F32Const { .. }));
}

/// Delete a type: call_indirect type numbers renumbered, index-form block types renumbered (P-17),
/// function-section type indices renumbered, imported-function types renumbered.
#[test]
fn remove_types_rewrites_blocktypes_and_calls() {
    // Type section [X(orphan), A, B, B2(same value as B)]; delete X(0) and B2(3).
    // f0's type A(1), block type referencing B(2), call_indirect referencing B2(3), imported function type B2(3).
    let mut m = Module::default();
    m.types = vec![
        ty(&[ValType::F64, ValType::F64], &[]), // X: orphan
        ty(&[], &[]),                           // A
        ty(&[ValType::I32], &[]),               // B
        ty(&[ValType::I32], &[]),               // B2, same value as B
    ];
    m.imports = vec![import_func("e", "a", 3)];
    m.defined_func_ty_ids = vec![1];
    m.defined_funcs = vec![func(1, vec![
        Inst::Block { blockty: BlockType::FuncType(2) },
        Inst::CallIndirect { type_index: 3, table_index: 0 },
    ])];
    m.defined_table_datas = vec![table(1)];
    let del = DeleteSet { types: set(&[0, 3]), ..Default::default() };
    let new = apply(&m, &del);
    assert_eq!(new.types.len(), 2);
    // After deletion [A, B]: block type B(2)→1; call_indirect B2(3)→B's value→1.
    assert_eq!(new.defined_funcs[0].insts[0], Inst::Block { blockty: BlockType::FuncType(1) });
    assert_eq!(
        new.defined_funcs[0].insts[1],
        Inst::CallIndirect { type_index: 1, table_index: 0 }
    );
    // f0's function-section type A(1)→0 (X deleted before it).
    assert_eq!(new.defined_func_ty_ids, vec![0]);
    // Imported function type B2(3)→1.
    assert_eq!(new.imports[0].desc, ImportDesc::Func(1));
}

/// call_indirect's type and table numbers touched by both the type step and the table step → field overlay (D-8).
#[test]
fn call_indirect_type_and_table_both_rewritten() {
    let mut m = Module::default();
    m.types = vec![ty(&[], &[]), ty(&[ValType::I32], &[]), ty(&[], &[ValType::I32])];
    m.defined_func_ty_ids = vec![0];
    m.defined_table_datas = vec![table(1), table(1), table(1)];
    m.defined_funcs = vec![func(0, vec![
        Inst::CallIndirect { type_index: 2, table_index: 2 },
        Inst::CallIndirect { type_index: 0, table_index: 0 },
    ])];
    // Delete type 1 and table 1: type 2→1, table 2→1.
    let del = DeleteSet { types: set(&[1]), tables: set(&[1]), ..Default::default() };
    let new = apply(&m, &del);
    assert_eq!(
        new.defined_funcs[0].insts[0],
        Inst::CallIndirect { type_index: 1, table_index: 1 }
    );
    // Untouched instructions are unaffected.
    assert_eq!(
        new.defined_funcs[0].insts[1],
        Inst::CallIndirect { type_index: 0, table_index: 0 }
    );
}

/// Delete a memory: the full shift (D-7) — instruction memory references, active data segments' memory numbers, exports deleted/renumbered.
#[test]
fn remove_memory_shifts_everything() {
    let mut m = Module::default();
    m.types = vec![ty(&[], &[])];
    m.defined_memory_datas = vec![memory(1), memory(2), memory(3)]; // delete memory 1.
    m.defined_func_ty_ids = vec![0];
    m.defined_funcs = vec![func(0, vec![
        Inst::I32Load { memarg: memarg(2) },     // 2→1
        Inst::MemorySize { mem: 2 },             // 2→1
        Inst::MemoryCopy { dst_mem: 2, src_mem: 0 }, // (1,0)
        Inst::MemoryInit { data_index: 0, mem: 2 },  // mem 2→1
        Inst::Drop,
    ])];
    m.data_sec_datas = vec![
        DataSeg { mode: DataMode::Active { mem_idx: 2, offset: vec![Inst::I32Const { value: 0 }, Inst::End] }, data: vec![9] },
        DataSeg { mode: DataMode::Passive, data: vec![8] },
    ];
    m.exports = vec![
        Export { name: "m1".into(), desc: ExportDesc::Memory(1) }, // deleted → dropped
        Export { name: "m2".into(), desc: ExportDesc::Memory(2) }, // 2→1
    ];
    let del = DeleteSet { mems: set(&[1]), ..Default::default() };
    let new = apply(&m, &del);
    assert_eq!(new.defined_memory_datas.len(), 2);
    assert_eq!(new.defined_funcs[0].insts[0], Inst::I32Load { memarg: memarg(1) });
    assert_eq!(new.defined_funcs[0].insts[1], Inst::MemorySize { mem: 1 });
    assert_eq!(new.defined_funcs[0].insts[2], Inst::MemoryCopy { dst_mem: 1, src_mem: 0 });
    assert_eq!(new.defined_funcs[0].insts[3], Inst::MemoryInit { data_index: 0, mem: 1 });
    // Active data segment mem_idx 2→1.
    match &new.data_sec_datas[0].mode {
        DataMode::Active { mem_idx, .. } => assert_eq!(*mem_idx, 1),
        _ => panic!(),
    }
    assert_eq!(new.exports.len(), 1);
    assert_eq!(new.exports[0].desc, ExportDesc::Memory(1));
}

/// Combination: the same data segment touched both by deletion (the data step) and by memory-number rewriting (the memory step)
/// → the mutation channel's same-position conflict rule: deletion wins.
#[test]
fn combined_memory_and_data_delete_wins() {
    let mut m = Module::default();
    m.types = vec![ty(&[], &[])];
    m.defined_memory_datas = vec![memory(1), memory(2)]; // delete memory 1.
    m.defined_func_ty_ids = vec![0];
    m.defined_funcs = vec![func(0, vec![
        Inst::I32Load { memarg: memarg(0) },
        Inst::Drop,
    ])];
    m.data_sec_datas = vec![
        DataSeg { mode: DataMode::Active { mem_idx: 0, offset: vec![Inst::I32Const { value: 0 }, Inst::End] }, data: vec![1] },
        // data1 is active in memory 0 (kept) while being deleted by the data step: mem_idx 0 needs no rewriting,
        // and the deletion takes effect as usual.
        DataSeg { mode: DataMode::Active { mem_idx: 0, offset: vec![Inst::I32Const { value: 0 }, Inst::End] }, data: vec![2] },
    ];
    let del = DeleteSet {
        mems: set(&[1]),
        datas: set(&[1]),
        ..Default::default()
    };
    let new = apply(&m, &del);
    assert_eq!(new.data_sec_datas.len(), 1);
    assert_eq!(new.data_sec_datas[0].data, vec![1]);
    assert_eq!(new.defined_memory_datas.len(), 1);
}

/// Delete a data segment: the data segment numbers of data.drop / memory.init renumbered.
#[test]
fn remove_datas_rewrites_indices() {
    let mut m = Module::default();
    m.types = vec![ty(&[], &[])];
    m.defined_memory_datas = vec![memory(1)];
    m.defined_func_ty_ids = vec![0];
    m.defined_funcs = vec![func(0, vec![
        Inst::DataDrop { data_index: 2 },        // 2→1
        Inst::MemoryInit { data_index: 2, mem: 0 }, // data 2→1
        Inst::Drop,
        Inst::Drop,
        Inst::Drop,
    ])];
    m.data_sec_datas = vec![
        DataSeg { mode: DataMode::Passive, data: vec![1] },
        DataSeg { mode: DataMode::Passive, data: vec![2] }, // delete segment 1.
        DataSeg { mode: DataMode::Passive, data: vec![3] },
    ];
    let del = DeleteSet { datas: set(&[1]), ..Default::default() };
    let new = apply(&m, &del);
    assert_eq!(new.data_sec_datas.len(), 2);
    assert_eq!(new.defined_funcs[0].insts[0], Inst::DataDrop { data_index: 1 });
    assert_eq!(new.defined_funcs[0].insts[1], Inst::MemoryInit { data_index: 1, mem: 0 });
}

/// Delete an element segment: the element segment numbers of elem.drop / table.init renumbered.
#[test]
fn remove_elemsegs_rewrites_indices() {
    let mut m = Module::default();
    m.types = vec![ty(&[], &[])];
    m.defined_table_datas = vec![table(1)];
    m.defined_func_ty_ids = vec![0];
    m.defined_funcs = vec![func(0, vec![
        Inst::ElemDrop { elem_index: 2 },
        Inst::TableInit { elem_index: 2, table: 0 },
        Inst::Drop,
        Inst::Drop,
        Inst::Drop,
    ])];
    m.elem_sec_datas = vec![
        ElemSeg { mode: ElemMode::Passive, payload: ElemPayload::FuncIdxs(vec![0]) },
        ElemSeg { mode: ElemMode::Passive, payload: ElemPayload::FuncIdxs(vec![0]) }, // delete segment 1.
        ElemSeg { mode: ElemMode::Passive, payload: ElemPayload::FuncIdxs(vec![0]) },
    ];
    let del = DeleteSet { elemsegs: set(&[1]), ..Default::default() };
    let new = apply(&m, &del);
    assert_eq!(new.elem_sec_datas.len(), 2);
    assert_eq!(new.defined_funcs[0].insts[0], Inst::ElemDrop { elem_index: 1 });
    assert_eq!(new.defined_funcs[0].insts[1], Inst::TableInit { elem_index: 1, table: 0 });
}

/// An element segment touched by both the function step (function index rewriting) and the table step (table number rewriting) → fields overlaid into
/// one replacement (instead of two same-position replacements discarding each other).
#[test]
fn elemseg_funcidx_and_table_both_rewritten() {
    let mut m = Module::default();
    m.types = vec![ty(&[], &[])];
    // 3 tables [t0,t1,t2], delete t1; 3 functions [f0,f1,f2], delete f1.
    m.defined_table_datas = vec![table(1), table(1), table(1)];
    m.defined_func_ty_ids = vec![0, 0, 0];
    m.defined_funcs = vec![func(0, vec![]), func(0, vec![]), func(0, vec![])];
    // Element segment: explicit table 2, function indices [f2] (whole-space 2).
    m.elem_sec_datas = vec![active_elem(Some(2), vec![2])];
    let del = DeleteSet { funcs: set(&[1]), tables: set(&[1]), ..Default::default() };
    let new = apply(&m, &del);
    assert_eq!(new.elem_sec_datas.len(), 1);
    match &new.elem_sec_datas[0] {
        ElemSeg { mode: ElemMode::Active { table_idx: Some(1), .. }, payload: ElemPayload::FuncIdxs(idxs) } => {
            assert_eq!(idxs, &vec![1]); // f2 whole-space 2 → 1 after deleting f1
        }
        other => panic!("unexpected elem {other:?}"),
    }
}

/// An implicit-form (MVP encoding) active element segment pointing at table 0: stays implicitly encoded when table 0 is not deleted.
/// (When table 0 is deleted, the segment's target table no longer exists — contradictory input, which detect guarantees cannot happen;
/// remap fails fast on it.)
#[test]
fn implicit_elemseg_kept_when_table0_stays() {
    let mut m = Module::default();
    m.types = vec![ty(&[], &[])];
    m.defined_table_datas = vec![table(1), table(1)]; // delete table 1; table 0 kept.
    m.defined_func_ty_ids = vec![0];
    m.defined_funcs = vec![func(0, vec![])];
    m.elem_sec_datas = vec![active_elem(None, vec![])];
    let del = DeleteSet { tables: set(&[1]), ..Default::default() };
    let new = apply(&m, &del);
    assert_eq!(new.defined_table_datas.len(), 1);
    match &new.elem_sec_datas[0].mode {
        ElemMode::Active { table_idx: None, .. } => {}
        other => panic!("unexpected mode {other:?}"),
    }
}

/// start section deletion and renumbering.
#[test]
fn start_removed_or_shifted() {
    let base = || {
        let mut m = Module::default();
        m.types = vec![ty(&[], &[])];
        m.defined_func_ty_ids = vec![0, 0, 0];
        m.defined_funcs = vec![func(0, vec![]), func(0, vec![]), func(0, vec![])];
        m
    };
    // Deletion: del.start (precondition: the snapshot has a start section — the mutation channel errors on editing
    // an absent section; see mutation.rs's fail-fast convention).
    let mut m = base();
    m.start_sec_data = Some(0);
    let del = DeleteSet { start: true, ..Default::default() };
    let new = apply(&m, &del);
    assert_eq!(new.start_sec_data, None);
    // The start function deleted → the start section deleted with it (inside remove_dead_funcs).
    let mut m = base();
    m.start_sec_data = Some(1);
    let del = DeleteSet { funcs: set(&[1]), ..Default::default() };
    let new = apply(&m, &del);
    assert_eq!(new.start_sec_data, None);
    // The start function not deleted but with earlier deletions → renumbered.
    let mut m = base();
    m.start_sec_data = Some(2);
    let del = DeleteSet { funcs: set(&[1]), ..Default::default() };
    let new = apply(&m, &del);
    assert_eq!(new.start_sec_data, Some(1));
}

/// Out-of-range deletion set → error (fail fast).
#[test]
fn out_of_range_delete_rejected() {
    let m = Module::default();
    let del = DeleteSet { types: set(&[0]), ..Default::default() };
    assert!(remap_all(&m, &del).is_err());
}

// ---------------------------------------------------------------------------
// Corpus test: all unused detected + all exports + start deleted (simulating one full-candidate deletion);
// the artifact must pass wasm-tools validate.
// ---------------------------------------------------------------------------

fn wasm_tools() -> PathBuf {
    std::env::var("E2WR_WASM_TOOLS")
        .map(PathBuf::from)
        .unwrap_or_else(|_| PathBuf::from("wasm-tools"))
}

fn validate(path: &std::path::Path) -> bool {
    std::process::Command::new(wasm_tools())
        .arg("validate")
        .arg(path)
        .output()
        .map(|o| o.status.success())
        .unwrap_or(false)
}

fn corpus_files() -> Vec<PathBuf> {
    let mut files = vec![];
    let tt_pat = format!("{}/CP9201/CP9201_MAIN/tt/*.wasm", std::env::var("HOME").unwrap_or_default());
    let bench_root = concat!(env!("CARGO_MANIFEST_DIR"), "/../../../benchmark");
    for pat in [
        tt_pat.as_str(),
        &format!("{bench_root}/RQ12/*.wasm"),
    ] {
        files.extend(glob::glob(pat).expect("valid pattern").filter_map(Result::ok));
    }
    files.sort();
    files
}

#[test]
fn corpus_delete_all_unused_validates() {
    let files: Vec<PathBuf> = corpus_files().into_iter().step_by(13).collect();
    let out_dir = std::env::temp_dir().join(format!("e2wr-pass-remap-{}", std::process::id()));
    std::fs::create_dir_all(&out_dir).unwrap();
    let mut checked = 0;
    for f in files {
        let snap = match Snapshot::from_path(&f) {
            Ok(s) => s,
            Err(_) => continue,
        };
        let module = snap.module();
        let del = DeleteSet {
            funcs: detect::detect_funcs(module).unused,
            imports: detect::detect_import_funcs(module).unused,
            globals: detect::detect_globals(module).unused,
            mems: detect::detect_memories(module).unused,
            tables: detect::detect_tables(module).unused,
            elemsegs: detect::detect_elemsegs(module).unused,
            datas: detect::detect_datas(module).unused,
            types: detect::detect_types(module).unused,
            exports: (0..module.exports.len() as u32).collect(),
            start: module.start_sec_data.is_some(),
        };
        if del == DeleteSet::default() {
            continue;
        }
        let mutations = remap_all(module, &del).unwrap_or_else(|e| panic!("{}: {e}", f.display()));
        let new_snap = apply_mutations(&snap, &mutations)
            .unwrap_or_else(|e| panic!("{}: {e}", f.display()));
        let out = out_dir.join(f.file_name().unwrap());
        new_snap.encode_to_path(&out).unwrap();
        assert!(validate(&out), "{} produced invalid wasm", f.display());
        checked += 1;
    }
    assert!(checked > 10, "corpus sample too small: {checked}");
}

/// The p10_0 targeted regression (P-17): after deleting the orphan types {82,83,84} the artifact must be valid
/// (the same path's Python output was out of range as actually measured; valid after correct renumbering).
#[test]
fn p10_0_type_deletion_regression() {
    let f = PathBuf::from(concat!(env!("CARGO_MANIFEST_DIR"), "/../../../benchmark/RQ12/p10_0.wasm"));
    if !f.exists() {
        return;
    }
    let snap = Snapshot::from_path(&f).unwrap();
    let module = snap.module();
    let unused = detect::detect_types(module).unused;
    assert_eq!(unused, set(&[82, 83, 84]), "p10_0 unused types changed?");
    let del = DeleteSet { types: unused, ..Default::default() };
    let mutations = remap_all(module, &del).unwrap();
    let new_snap = apply_mutations(&snap, &mutations).unwrap();
    assert_eq!(new_snap.module().types.len(), 84);
    let out = std::env::temp_dir().join(format!("p10_0_typedel_rs_{}.wasm", std::process::id()));
    new_snap.encode_to_path(&out).unwrap();
    assert!(validate(&out), "p10_0 type deletion must stay valid (P-17)");
    // The correctly renumbered Python path's artifact is 10714 bytes (reference; encoder differences permit inequality,
    // but the gap should stay within a byte or two).
    let size = out.metadata().unwrap().len();
    assert!((size as i64 - 10714).abs() < 8, "unexpected size {size} vs 10714");
}

