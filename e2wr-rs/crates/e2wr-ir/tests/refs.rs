//! refs.rs unit tests: enumerating references of each index space (rewrite tests live in src/refs.rs unit tests, R-15).

use e2wr_ir::inst::BrTableData;
use e2wr_ir::module::{BlockType, FuncType};
use wasmparser::ValType;
use e2wr_ir::refs::{blocktype_func_type, refs, IndexSpace, Ref};
use e2wr_ir::Inst;

fn m() -> wasmparser::MemArg {
    wasmparser::MemArg { align: 2, max_align: 2, offset: 0, memory: 0 }
}

fn r(space: IndexSpace, idx: u32) -> Ref {
    Ref { space, idx }
}

#[test]
fn refs_by_space() {
    let cases: Vec<(Inst, Vec<Ref>)> = vec![
        (Inst::Call { function_index: 3 }, vec![r(IndexSpace::Func, 3)]),
        (Inst::ReturnCall { function_index: 4 }, vec![r(IndexSpace::Func, 4)]),
        (Inst::RefFunc { function_index: 5 }, vec![r(IndexSpace::Func, 5)]),
        (
            Inst::CallIndirect { type_index: 6, table_index: 7 },
            vec![r(IndexSpace::Type, 6), r(IndexSpace::Table, 7)],
        ),
        (
            Inst::ReturnCallIndirect { type_index: 8, table_index: 9 },
            vec![r(IndexSpace::Type, 8), r(IndexSpace::Table, 9)],
        ),
        (Inst::GlobalGet { global_index: 2 }, vec![r(IndexSpace::Global, 2)]),
        (Inst::GlobalSet { global_index: 3 }, vec![r(IndexSpace::Global, 3)]),
        (Inst::MemorySize { mem: 1 }, vec![r(IndexSpace::Mem, 1)]),
        (Inst::MemoryGrow { mem: 1 }, vec![r(IndexSpace::Mem, 1)]),
        (Inst::MemoryFill { mem: 2 }, vec![r(IndexSpace::Mem, 2)]),
        (
            Inst::MemoryInit { data_index: 4, mem: 2 },
            vec![r(IndexSpace::DataSeg, 4), r(IndexSpace::Mem, 2)],
        ),
        (
            Inst::MemoryCopy { dst_mem: 2, src_mem: 0 },
            vec![r(IndexSpace::Mem, 2), r(IndexSpace::Mem, 0)],
        ),
        (Inst::DataDrop { data_index: 1 }, vec![r(IndexSpace::DataSeg, 1)]),
        (
            Inst::TableInit { elem_index: 2, table: 1 },
            vec![r(IndexSpace::ElemSeg, 2), r(IndexSpace::Table, 1)],
        ),
        (Inst::ElemDrop { elem_index: 3 }, vec![r(IndexSpace::ElemSeg, 3)]),
        (
            Inst::TableCopy { dst_table: 1, src_table: 2 },
            vec![r(IndexSpace::Table, 1), r(IndexSpace::Table, 2)],
        ),
        (Inst::TableGet { table: 1 }, vec![r(IndexSpace::Table, 1)]),
        (Inst::TableSet { table: 1 }, vec![r(IndexSpace::Table, 1)]),
        (Inst::TableSize { table: 1 }, vec![r(IndexSpace::Table, 1)]),
        (Inst::TableGrow { table: 1 }, vec![r(IndexSpace::Table, 1)]),
        (Inst::TableFill { table: 1 }, vec![r(IndexSpace::Table, 1)]),
        // memarg-shaped: real memory numbers (multi-memory support).
        (
            Inst::I32Load { memarg: wasmparser::MemArg { memory: 3, ..m() } },
            vec![r(IndexSpace::Mem, 3)],
        ),
        (
            Inst::F64Store { memarg: wasmparser::MemArg { memory: 2, ..m() } },
            vec![r(IndexSpace::Mem, 2)],
        ),
        (
            Inst::I32AtomicRmwAdd { memarg: wasmparser::MemArg { memory: 1, ..m() } },
            vec![r(IndexSpace::Mem, 1)],
        ),
        (
            Inst::V128Load { memarg: wasmparser::MemArg { memory: 4, ..m() } },
            vec![r(IndexSpace::Mem, 4)],
        ),
        // Index-form block types reference the type section; empty/value-type forms do not.
        (
            Inst::Block { blockty: BlockType::FuncType(9) },
            vec![r(IndexSpace::Type, 9)],
        ),
        (Inst::Loop { blockty: BlockType::Empty }, vec![]),
        (Inst::If { blockty: BlockType::Type(ValType::I32) }, vec![]),
        // Instructions referencing no index space.
        (Inst::Nop, vec![]),
        (Inst::Drop, vec![]),
        (Inst::I32Const { value: 1 }, vec![]),
        (Inst::LocalGet { local_index: 0 }, vec![]),
        (Inst::Br { relative_depth: 0 }, vec![]),
        (Inst::BrTable(BrTableData { targets: vec![0, 1], default: 2 }), vec![]),
        (Inst::RefNull { hty: wasmparser::HeapType::Abstract { ty: wasmparser::AbstractHeapType::Func, shared: false } }, vec![]),
    ];
    for (inst, want) in cases {
        assert_eq!(refs(&inst), want, "inst {inst:?}");
    }
}

#[test]
fn blocktype_to_func_type() {
    let types = vec![
        FuncType { params: vec![], results: vec![] },
        FuncType { params: vec![ValType::I32], results: vec![ValType::I32, ValType::F64] },
    ];
    assert_eq!(
        blocktype_func_type(&BlockType::Empty, &types),
        Some(FuncType { params: vec![], results: vec![] })
    );
    assert_eq!(
        blocktype_func_type(&BlockType::Type(ValType::I64), &types),
        Some(FuncType { params: vec![], results: vec![ValType::I64] })
    );
    assert_eq!(
        blocktype_func_type(&BlockType::FuncType(1), &types),
        Some(types[1].clone())
    );
    assert_eq!(blocktype_func_type(&BlockType::FuncType(9), &types), None);
}
