//! detect.rs unit tests: used/unused judgments per index space on synthetic modules.

#![allow(clippy::field_reassign_with_default)]

use e2wr_pass::detect::*;
use e2wr_ir::module::*;
use wasmparser::ValType;
use e2wr_ir::Inst;

fn ty(params: &[ValType], results: &[ValType]) -> FuncType {
    FuncType { params: params.to_vec(), results: results.to_vec() }
}

fn memarg(memory: u32) -> e2wr_ir::module::MemArg {
    e2wr_ir::module::MemArg { align: 2, max_align: 2, offset: 0, memory }
}

fn func(ty_idx: u32, insts: Vec<Inst>) -> Func {
    let mut insts = insts;
    insts.push(Inst::End);
    Func { ty_idx, locals: vec![], insts }
}

fn set(v: &[u32]) -> std::collections::BTreeSet<u32> {
    v.iter().copied().collect()
}

/// An imported function + two defined functions + one unreferenced function.
fn basic_funcs_module() -> Module {
    let mut m = Module::default();
    m.types = vec![ty(&[], &[]), ty(&[ValType::I32], &[ValType::I32])];
    m.imports = vec![Import {
        module: "e".into(),
        name: "f".into(),
        desc: ImportDesc::Func(0),
    }];
    // Whole space: imported function 0; defined functions 1, 2; defined function 3 unreferenced.
    m.defined_func_ty_ids = vec![0, 0, 1];
    m.defined_funcs = vec![
        func(0, vec![Inst::Call { function_index: 0 }]),
        func(0, vec![Inst::Call { function_index: 1 }, Inst::RefFunc { function_index: 2 }]),
        func(1, vec![]),
    ];
    m
}

#[test]
fn funcs_and_import_funcs() {
    let m = basic_funcs_module();
    let r = detect_funcs(&m);
    assert_eq!(r.used, set(&[0, 1]));
    assert_eq!(r.unused, set(&[2]));

    let r = detect_import_funcs(&m);
    assert_eq!(r.used, set(&[0]));
    assert_eq!(r.unused, set(&[]));
}

/// An unreferenced imported function lands in unused (mapped back to the import entry index).
#[test]
fn import_func_unused() {
    let mut m = basic_funcs_module();
    m.defined_funcs[0] = func(0, vec![]);
    let r = detect_funcs(&m);
    assert_eq!(r.used, set(&[0, 1]));
    let r = detect_import_funcs(&m);
    assert_eq!(r.used, set(&[]));
    assert_eq!(r.unused, set(&[0]));
}

/// A function referenced by the start section is not marked used (same as Python: the start function itself is a deletion candidate).
#[test]
fn start_not_marked() {
    let mut m = basic_funcs_module();
    m.start_sec_data = Some(2); // defined function 2 (local 1)
    let r = detect_funcs(&m);
    assert_eq!(r.unused, set(&[2]));
}

/// Multi-memory: real memory numbers all marked (D-7), covering memory.size/grow/fill/copy/init.
#[test]
fn multi_memory_real_indices() {
    let mut m = Module::default();
    m.types = vec![ty(&[], &[])];
    m.defined_memory_datas = vec![Memory { ty: MemType { limits: Limits { min: 1, max: None } } }; 2];
    m.defined_func_ty_ids = vec![0];
    m.defined_funcs = vec![func(0, vec![
        Inst::I32Load { memarg: memarg(1) },
        Inst::I32Store { memarg: memarg(0) },
        Inst::MemorySize { mem: 0 },
        Inst::MemoryGrow { mem: 1 },
        Inst::MemoryFill { mem: 0 },
        Inst::MemoryCopy { dst_mem: 0, src_mem: 1 },
    ])];
    let r = detect_memories(&m);
    assert_eq!(r.used, set(&[0, 1]));
    assert_eq!(r.unused, set(&[]));

    // Remove all instruction references to memory 0, leaving only the export → still used.
    m.defined_funcs[0] = func(0, vec![
        Inst::I32Load { memarg: memarg(1) },
        Inst::MemorySize { mem: 1 },
    ]);
    m.exports = vec![Export { name: "m".into(), desc: ExportDesc::Memory(0) }];
    let r = detect_memories(&m);
    assert_eq!(r.used, set(&[0, 1]));

    // After dropping the export, memory 0 is unused.
    m.exports.clear();
    let r = detect_memories(&m);
    assert_eq!(r.used, set(&[1]));
    assert_eq!(r.unused, set(&[0]));
}

/// The memory number designated by an active data segment participates in memory use marking.
#[test]
fn active_data_marks_memory() {
    let mut m = Module::default();
    m.types = vec![ty(&[], &[])];
    m.defined_memory_datas = vec![Memory { ty: MemType { limits: Limits { min: 1, max: None } } }; 2];
    m.data_sec_datas = vec![DataSeg {
        mode: DataMode::Active { mem_idx: 1, offset: vec![Inst::I32Const { value: 0 }] },
        data: vec![1, 2],
    }];
    let r = detect_memories(&m);
    assert_eq!(r.used, set(&[1]));
    assert_eq!(r.unused, set(&[0]));
}

/// Imported globals: the candidate set = the defined range only; unused contains no out-of-range index (P-18 correction).
#[test]
fn imported_global_candidate_fix() {
    let mut m = Module::default();
    m.types = vec![ty(&[], &[])];
    m.imports = vec![Import {
        module: "e".into(),
        name: "g".into(),
        desc: ImportDesc::Global(GlobalType { val_ty: ValType::I32, mutable: false }),
    }];
    m.defined_globals = vec![
        Global { ty: GlobalType { val_ty: ValType::I32, mutable: false }, init_expr: vec![Inst::I32Const { value: 0 }, Inst::End] },
        Global { ty: GlobalType { val_ty: ValType::I32, mutable: false }, init_expr: vec![Inst::I32Const { value: 1 }, Inst::End] },
    ];
    m.defined_func_ty_ids = vec![0];
    // Whole space: imported global 0; defined globals 1, 2. References: whole-space 1 (the first defined global).
    m.defined_funcs = vec![func(0, vec![Inst::GlobalGet { global_index: 1 }])];
    let r = detect_globals(&m);
    assert_eq!(r.used, set(&[0]));
    assert_eq!(r.unused, set(&[1]));
    assert!(r.unused.iter().all(|i| (*i as usize) < m.defined_globals.len()));
}

/// Table use: table-family instructions, call_indirect/return_call_indirect table numbers, exports, active element segment table numbers
/// (the implicit form pointing at table 0).
#[test]
fn tables() {
    let mut m = Module::default();
    m.types = vec![ty(&[], &[])];
    let tt = TableType {
        elem_ty: e2wr_ir::module::RefType::FUNC,
        limits: Limits { min: 1, max: None },
    };
    m.defined_table_datas = vec![Table { ty: tt.clone(), init_expr: None }; 3];
    m.elem_sec_datas = vec![
        // Explicit table 2.
        ElemSeg {
            mode: ElemMode::Active { table_idx: Some(2), offset: vec![Inst::I32Const { value: 0 }, Inst::End] },
            payload: ElemPayload::FuncIdxs(vec![0]),
        },
        // Implicit form (MVP encoding) pointing at table 0.
        ElemSeg {
            mode: ElemMode::Active { table_idx: None, offset: vec![Inst::I32Const { value: 0 }, Inst::End] },
            payload: ElemPayload::FuncIdxs(vec![0]),
        },
    ];
    m.defined_func_ty_ids = vec![0];
    m.defined_funcs = vec![func(0, vec![
        Inst::CallIndirect { type_index: 0, table_index: 1 },
        Inst::ReturnCallIndirect { type_index: 0, table_index: 1 },
        Inst::TableGet { table: 1 },
    ])];
    let r = detect_tables(&m);
    assert_eq!(r.used, set(&[0, 1, 2]));
    assert_eq!(r.unused, set(&[]));

    // Tail-call table numbers are marked too (P-20 correction): only return_call_indirect referencing table 2 remains.
    m.defined_funcs[0] = func(0, vec![Inst::ReturnCallIndirect { type_index: 0, table_index: 2 }]);
    m.elem_sec_datas.clear();
    let r = detect_tables(&m);
    assert_eq!(r.used, set(&[2]));
    assert_eq!(r.unused, set(&[0, 1]));
}

/// Element/data segment use: the element segment numbers of elem.drop/table.init; the data segment numbers of data.drop/memory.init;
/// ref.func inside element segment expression payloads participates in function use marking.
#[test]
fn elemseg_and_data() {
    let mut m = Module::default();
    m.types = vec![ty(&[], &[])];
    m.defined_func_ty_ids = vec![0];
    m.defined_funcs = vec![func(0, vec![
        Inst::ElemDrop { elem_index: 0 },
        Inst::TableInit { elem_index: 1, table: 0 },
        Inst::DataDrop { data_index: 0 },
        Inst::MemoryInit { data_index: 1, mem: 0 },
    ])];
    m.elem_sec_datas = vec![ElemSeg { mode: ElemMode::Passive, payload: ElemPayload::FuncIdxs(vec![1]) }; 3];
    m.data_sec_datas = vec![DataSeg { mode: DataMode::Passive, data: vec![] }; 3];
    let r = detect_elemsegs(&m);
    assert_eq!(r.used, set(&[0, 1]));
    assert_eq!(r.unused, set(&[2]));
    let r = detect_datas(&m);
    assert_eq!(r.used, set(&[0, 1]));
    assert_eq!(r.unused, set(&[2]));
}

/// Type use: only the first index of an equal-valued type counts as used (the Python dedup strategy kept);
/// index-form block type values participate; empty/value-type form resolution values participate.
#[test]
fn types_dedup_first_wins() {
    let mut m = Module::default();
    m.types = vec![ty(&[], &[]), ty(&[ValType::I32], &[ValType::I32]), ty(&[], &[])];
    m.defined_func_ty_ids = vec![0, 1];
    m.defined_funcs = vec![
        // Block type index 2 references an equal-valued type (same value as 0) → 0 used, 2 a candidate.
        func(0, vec![Inst::Block { blockty: BlockType::FuncType(2) }]),
        func(1, vec![]),
    ];
    let r = detect_types(&m);
    assert_eq!(r.used, set(&[0, 1]));
    assert_eq!(r.unused, set(&[2]));
}

/// The function type value resolved from a value-type-form block type matches an equal-valued type-section entry.
#[test]
fn blocktype_value_form_matches_type_entry() {
    let mut m = Module::default();
    // Type 0 = ()->(i32), exactly equal to the value-type form of a block producing i32.
    m.types = vec![ty(&[], &[ValType::I32])];
    m.defined_func_ty_ids = vec![0];
    m.defined_funcs = vec![func(0, vec![Inst::Block { blockty: BlockType::Type(ValType::I32) }])];
    let r = detect_types(&m);
    assert_eq!(r.used, set(&[0]));
    assert!(r.unused.is_empty());
}

/// P-21 supplementary scan: ref.func inside table initializer expressions participates in function use marking.
#[test]
fn table_init_expr_ref_func_scanned() {
    let mut m = Module::default();
    m.types = vec![ty(&[], &[])];
    m.defined_func_ty_ids = vec![0];
    m.defined_funcs = vec![func(0, vec![])]; // defined function 0 (whole-space 0), no body references.
    m.defined_table_datas = vec![Table {
        ty: TableType { elem_ty: e2wr_ir::module::RefType::FUNC, limits: Limits { min: 1, max: None } },
        init_expr: Some(vec![Inst::RefFunc { function_index: 0 }, Inst::End]),
    }];
    let r = detect_funcs(&m);
    assert_eq!(r.used, set(&[0]));
    assert!(r.unused.is_empty());
}

/// ref.func inside global initializer expressions participates in function use marking (P-21 supplementary scan).
#[test]
fn global_init_expr_ref_func_scanned() {
    let mut m = Module::default();
    m.types = vec![ty(&[], &[])];
    m.defined_func_ty_ids = vec![0];
    m.defined_funcs = vec![func(0, vec![])];
    m.defined_globals = vec![Global {
        ty: GlobalType { val_ty: ValType::Ref(e2wr_ir::module::RefType::FUNC), mutable: false },
        init_expr: vec![Inst::RefFunc { function_index: 0 }, Inst::End],
    }];
    let r = detect_funcs(&m);
    assert_eq!(r.used, set(&[0]));
}
