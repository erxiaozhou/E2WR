//! Index-space reference accessors for instructions (M4).
//!
//! Reference detection (detect) and index remapping (remap) both access instruction immediates' indices
//! through this module, instead of scattering instruction-shape matching elsewhere. One index space corresponds to one kind of wasm numbering (functions, tables,
//! memories, globals, element segments, data segments, types).
//!
//! Note: type indices of GC (garbage-collection extension) instructions are also included in the Type space (e.g. the
//! array_type_index of array instructions); GC is outside the current corpus support, listed mechanically with correct semantics.

use crate::inst::Inst;
use crate::module::{BlockType, FuncType};

/// The seven wasm index spaces (numbering spaces instruction immediates may reference).
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash)]
pub enum IndexSpace {
    /// Function indices (immediates of call / ref.func etc.).
    Func,
    /// Table indices (table numbers of table.get / call_indirect etc.).
    Table,
    /// Memory indices (memarg numbers of load/store, the mem of memory.size, etc.).
    Mem,
    /// Global indices (global.get / global.set).
    Global,
    /// Element segment indices (elem.drop / table.init).
    ElemSeg,
    /// Data segment indices (data.drop / memory.init).
    DataSeg,
    /// Type section indices (the type of call_indirect, index-form block types, etc.).
    Type,
}

/// One reference of an instruction into one index space.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct Ref {
    pub space: IndexSpace,
    pub idx: u32,
}

/// Visits every index field an instruction references (a space may be referenced multiple times, e.g. both ends of
/// memory.copy, or both tables of table.copy).
pub fn for_each_index_field(inst: &mut Inst, f: &mut dyn FnMut(IndexSpace, &mut u32)) {
    macro_rules! one {
        // match ergonomics: the bound fields are already &mut u32, pass directly.
        ($field:expr, $sp:ident) => {
            f(IndexSpace::$sp, $field)
        };
    }
    macro_rules! memarg {
        ($field:expr) => {
            f(IndexSpace::Mem, &mut $field.memory)
        };
    }
    match inst {
        Inst::Call { function_index } => one!(function_index, Func),
        Inst::ReturnCall { function_index } => one!(function_index, Func),
        Inst::RefFunc { function_index } => one!(function_index, Func),
        Inst::CallIndirect { type_index, table_index } => {
            one!(type_index, Type);
            one!(table_index, Table);
        }
        Inst::ReturnCallIndirect { type_index, table_index } => {
            one!(type_index, Type);
            one!(table_index, Table);
        }
        Inst::GlobalGet { global_index } => one!(global_index, Global),
        Inst::GlobalSet { global_index } => one!(global_index, Global),
        Inst::MemorySize { mem } => one!(mem, Mem),
        Inst::MemoryGrow { mem } => one!(mem, Mem),
        Inst::MemoryFill { mem } => one!(mem, Mem),
        Inst::MemoryDiscard { mem } => one!(mem, Mem),
        Inst::MemoryInit { data_index, mem } => {
            one!(data_index, DataSeg);
            one!(mem, Mem);
        }
        Inst::MemoryCopy { dst_mem, src_mem } => {
            one!(dst_mem, Mem);
            one!(src_mem, Mem);
        }
        Inst::DataDrop { data_index } => one!(data_index, DataSeg),
        Inst::TableInit { elem_index, table } => {
            one!(elem_index, ElemSeg);
            one!(table, Table);
        }
        Inst::ElemDrop { elem_index } => one!(elem_index, ElemSeg),
        Inst::TableCopy { dst_table, src_table } => {
            one!(dst_table, Table);
            one!(src_table, Table);
        }
        Inst::TableFill { table } => one!(table, Table),
        Inst::TableGet { table } => one!(table, Table),
        Inst::TableSet { table } => one!(table, Table),
        Inst::TableGrow { table } => one!(table, Table),
        Inst::TableSize { table } => one!(table, Table),
        Inst::Block { blockty } | Inst::Loop { blockty } | Inst::If { blockty } => {
            // match ergonomics: idx is already &mut u32 here.
            if let BlockType::FuncType(idx) = blockty {
                f(IndexSpace::Type, idx);
            }
        }
        Inst::ArrayNewFixed { array_type_index, .. } => one!(array_type_index, Type),
        Inst::ArrayNewData { array_type_index, array_data_index } => {
            one!(array_type_index, Type);
            one!(array_data_index, DataSeg);
        }
        Inst::ArrayNewElem { array_type_index, array_elem_index } => {
            one!(array_type_index, Type);
            one!(array_elem_index, ElemSeg);
        }
        Inst::ArrayGet { array_type_index }
        | Inst::ArrayGetS { array_type_index }
        | Inst::ArrayGetU { array_type_index }
        | Inst::ArraySet { array_type_index }
        | Inst::ArrayFill { array_type_index } => one!(array_type_index, Type),
        Inst::ArrayCopy { array_type_index_dst, array_type_index_src } => {
            one!(array_type_index_dst, Type);
            one!(array_type_index_src, Type);
        }
        Inst::ArrayInitData { array_type_index, array_data_index } => {
            one!(array_type_index, Type);
            one!(array_data_index, DataSeg);
        }
        Inst::ArrayInitElem { array_type_index, array_elem_index } => {
            one!(array_type_index, Type);
            one!(array_elem_index, ElemSeg);
        }
        // The 111 memarg-shaped instructions (load/store/atomic/vector memory ops); the memory number lives in memarg.memory.
        Inst::I32Load { memarg, .. }
        | Inst::I64Load { memarg, .. }
        | Inst::F32Load { memarg, .. }
        | Inst::F64Load { memarg, .. }
        | Inst::I32Load8S { memarg, .. }
        | Inst::I32Load8U { memarg, .. }
        | Inst::I32Load16S { memarg, .. }
        | Inst::I32Load16U { memarg, .. }
        | Inst::I64Load8S { memarg, .. }
        | Inst::I64Load8U { memarg, .. }
        | Inst::I64Load16S { memarg, .. }
        | Inst::I64Load16U { memarg, .. }
        | Inst::I64Load32S { memarg, .. }
        | Inst::I64Load32U { memarg, .. }
        | Inst::I32Store { memarg, .. }
        | Inst::I64Store { memarg, .. }
        | Inst::F32Store { memarg, .. }
        | Inst::F64Store { memarg, .. }
        | Inst::I32Store8 { memarg, .. }
        | Inst::I32Store16 { memarg, .. }
        | Inst::I64Store8 { memarg, .. }
        | Inst::I64Store16 { memarg, .. }
        | Inst::I64Store32 { memarg, .. }
        | Inst::MemoryAtomicNotify { memarg, .. }
        | Inst::MemoryAtomicWait32 { memarg, .. }
        | Inst::MemoryAtomicWait64 { memarg, .. }
        | Inst::I32AtomicLoad { memarg, .. }
        | Inst::I64AtomicLoad { memarg, .. }
        | Inst::I32AtomicLoad8U { memarg, .. }
        | Inst::I32AtomicLoad16U { memarg, .. }
        | Inst::I64AtomicLoad8U { memarg, .. }
        | Inst::I64AtomicLoad16U { memarg, .. }
        | Inst::I64AtomicLoad32U { memarg, .. }
        | Inst::I32AtomicStore { memarg, .. }
        | Inst::I64AtomicStore { memarg, .. }
        | Inst::I32AtomicStore8 { memarg, .. }
        | Inst::I32AtomicStore16 { memarg, .. }
        | Inst::I64AtomicStore8 { memarg, .. }
        | Inst::I64AtomicStore16 { memarg, .. }
        | Inst::I64AtomicStore32 { memarg, .. }
        | Inst::I32AtomicRmwAdd { memarg, .. }
        | Inst::I64AtomicRmwAdd { memarg, .. }
        | Inst::I32AtomicRmw8AddU { memarg, .. }
        | Inst::I32AtomicRmw16AddU { memarg, .. }
        | Inst::I64AtomicRmw8AddU { memarg, .. }
        | Inst::I64AtomicRmw16AddU { memarg, .. }
        | Inst::I64AtomicRmw32AddU { memarg, .. }
        | Inst::I32AtomicRmwSub { memarg, .. }
        | Inst::I64AtomicRmwSub { memarg, .. }
        | Inst::I32AtomicRmw8SubU { memarg, .. }
        | Inst::I32AtomicRmw16SubU { memarg, .. }
        | Inst::I64AtomicRmw8SubU { memarg, .. }
        | Inst::I64AtomicRmw16SubU { memarg, .. }
        | Inst::I64AtomicRmw32SubU { memarg, .. }
        | Inst::I32AtomicRmwAnd { memarg, .. }
        | Inst::I64AtomicRmwAnd { memarg, .. }
        | Inst::I32AtomicRmw8AndU { memarg, .. }
        | Inst::I32AtomicRmw16AndU { memarg, .. }
        | Inst::I64AtomicRmw8AndU { memarg, .. }
        | Inst::I64AtomicRmw16AndU { memarg, .. }
        | Inst::I64AtomicRmw32AndU { memarg, .. }
        | Inst::I32AtomicRmwOr { memarg, .. }
        | Inst::I64AtomicRmwOr { memarg, .. }
        | Inst::I32AtomicRmw8OrU { memarg, .. }
        | Inst::I32AtomicRmw16OrU { memarg, .. }
        | Inst::I64AtomicRmw8OrU { memarg, .. }
        | Inst::I64AtomicRmw16OrU { memarg, .. }
        | Inst::I64AtomicRmw32OrU { memarg, .. }
        | Inst::I32AtomicRmwXor { memarg, .. }
        | Inst::I64AtomicRmwXor { memarg, .. }
        | Inst::I32AtomicRmw8XorU { memarg, .. }
        | Inst::I32AtomicRmw16XorU { memarg, .. }
        | Inst::I64AtomicRmw8XorU { memarg, .. }
        | Inst::I64AtomicRmw16XorU { memarg, .. }
        | Inst::I64AtomicRmw32XorU { memarg, .. }
        | Inst::I32AtomicRmwXchg { memarg, .. }
        | Inst::I64AtomicRmwXchg { memarg, .. }
        | Inst::I32AtomicRmw8XchgU { memarg, .. }
        | Inst::I32AtomicRmw16XchgU { memarg, .. }
        | Inst::I64AtomicRmw8XchgU { memarg, .. }
        | Inst::I64AtomicRmw16XchgU { memarg, .. }
        | Inst::I64AtomicRmw32XchgU { memarg, .. }
        | Inst::I32AtomicRmwCmpxchg { memarg, .. }
        | Inst::I64AtomicRmwCmpxchg { memarg, .. }
        | Inst::I32AtomicRmw8CmpxchgU { memarg, .. }
        | Inst::I32AtomicRmw16CmpxchgU { memarg, .. }
        | Inst::I64AtomicRmw8CmpxchgU { memarg, .. }
        | Inst::I64AtomicRmw16CmpxchgU { memarg, .. }
        | Inst::I64AtomicRmw32CmpxchgU { memarg, .. }
        | Inst::V128Load { memarg, .. }
        | Inst::V128Load8x8S { memarg, .. }
        | Inst::V128Load8x8U { memarg, .. }
        | Inst::V128Load16x4S { memarg, .. }
        | Inst::V128Load16x4U { memarg, .. }
        | Inst::V128Load32x2S { memarg, .. }
        | Inst::V128Load32x2U { memarg, .. }
        | Inst::V128Load8Splat { memarg, .. }
        | Inst::V128Load16Splat { memarg, .. }
        | Inst::V128Load32Splat { memarg, .. }
        | Inst::V128Load64Splat { memarg, .. }
        | Inst::V128Load32Zero { memarg, .. }
        | Inst::V128Load64Zero { memarg, .. }
        | Inst::V128Store { memarg, .. }
        | Inst::V128Load8Lane { memarg, .. }
        | Inst::V128Load16Lane { memarg, .. }
        | Inst::V128Load32Lane { memarg, .. }
        | Inst::V128Load64Lane { memarg, .. }
        | Inst::V128Store8Lane { memarg, .. }
        | Inst::V128Store16Lane { memarg, .. }
        | Inst::V128Store32Lane { memarg, .. }
        | Inst::V128Store64Lane { memarg, .. } => memarg!(memarg),
        // Remaining instructions reference no index space (local slot numbers, relative branch depths, constants,
        // and the like are outside this accessor's scope).
        _ => {}
    }
}

/// Lists every index an instruction references.
pub fn refs(inst: &Inst) -> Vec<Ref> {
    let mut out = Vec::new();
    for_each_ref(inst, &mut |space, idx| out.push(Ref { space, idx }));
    out
}

/// Visits every index an instruction references (immutable, zero-allocation version; used by hot scanning paths;
/// `refs` is its collecting wrapper). Internally it reuses the mutable matching on a stack copy (cloning one instruction
/// copies a few dozen bytes; most variants need no heap allocation).
pub fn for_each_ref(inst: &Inst, f: &mut dyn FnMut(IndexSpace, u32)) {
    let mut tmp = inst.clone();
    for_each_index_field(&mut tmp, &mut |space, idx| f(space, *idx));
}

/// Position of a function-body instruction (defined function index + instruction ordinal).
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct InstPos {
    pub func_idx: u32,
    pub inst_idx: u32,
}

/// The single function-body traversal loop: visits the instructions of all defined function bodies, in function order then instruction order.
/// Every scan (detect / remap / ...) goes through this primitive, no separate loops; a "scan strategy" is simply the callback
/// passed to it (combined with `for_each_ref` to build various collect/rewrite behaviors).
/// Note: global and table initializer expressions are outside this primitive (they belong to the detection's
/// P-21 supplementary-scan semantics; callers traverse section-level data themselves).
pub fn for_each_inst(module: &crate::module::Module, f: &mut dyn FnMut(InstPos, &Inst)) {
    for (func_idx, func) in module.defined_funcs.iter().enumerate() {
        for (inst_idx, inst) in func.insts.iter().enumerate() {
            f(
                InstPos { func_idx: func_idx as u32, inst_idx: inst_idx as u32 },
                inst,
            );
        }
    }
}

/// Rewrites instruction immediates by index space: applies the mapping to each referenced index in the space, leaving other fields untouched.
/// Returns Some(new instruction) iff at least one index changed (None otherwise, so callers can skip the replacement).
/// Zero production callers (R-15: kept for unit tests only, bound to tests).
#[cfg(test)]
fn rewrite_refs(
    inst: &Inst,
    space: IndexSpace,
    map: &mut dyn FnMut(u32) -> u32,
) -> Option<Inst> {
    let mut new_inst = inst.clone();
    let mut changed = false;
    for_each_index_field(&mut new_inst, &mut |sp, idx| {
        if sp == space {
            let old = *idx;
            let new = map(old);
            if new != old {
                *idx = new;
                changed = true;
            }
        }
    });
    if changed {
        Some(new_inst)
    } else {
        None
    }
}

/// Resolves a block type into a function type value (for type-usage detection; equivalent to Python `Blocktype.concrete_type`):
/// - empty form → no params, no results;
/// - value type form → no params, one result of that type;
/// - index form → look up the type section (out of bounds returns None, treated as an error).
pub fn blocktype_func_type(bt: &BlockType, types: &[FuncType]) -> Option<FuncType> {
    match bt {
        BlockType::Empty => Some(FuncType { params: vec![], results: vec![] }),
        BlockType::Type(t) => Some(FuncType { params: vec![], results: vec![*t] }),
        BlockType::FuncType(idx) => types.get(*idx as usize).cloned(),
    }
}

#[cfg(test)]
mod tests {
    //! Behavior tests of rewrite_refs (R-15, moved in from tests/refs.rs — the function has zero
    //! production callers and became test-bound when privatized).
    use super::*;

    #[test]
    fn rewrite_by_space() {
        // Only the given space is rewritten; fields of other spaces stay untouched.
        let ci = Inst::CallIndirect { type_index: 6, table_index: 7 };
        let out = rewrite_refs(&ci, IndexSpace::Type, &mut |i| if i == 6 { 5 } else { i }).unwrap();
        assert_eq!(out, Inst::CallIndirect { type_index: 5, table_index: 7 });
        let out = rewrite_refs(&ci, IndexSpace::Table, &mut |i| if i == 7 { 2 } else { i }).unwrap();
        assert_eq!(out, Inst::CallIndirect { type_index: 6, table_index: 2 });

        // Both references in one space are rewritten (both ends of memory.copy, both tables of table.copy).
        let mc = Inst::MemoryCopy { dst_mem: 3, src_mem: 1 };
        let out = rewrite_refs(&mc, IndexSpace::Mem, &mut |i| i - 1).unwrap();
        assert_eq!(out, Inst::MemoryCopy { dst_mem: 2, src_mem: 0 });

        // memory.init: DataSeg and Mem belong to two different spaces, each rewritten separately.
        let mi = Inst::MemoryInit { data_index: 4, mem: 2 };
        let out = rewrite_refs(&mi, IndexSpace::DataSeg, &mut |i| if i == 4 { 3 } else { i }).unwrap();
        assert_eq!(out, Inst::MemoryInit { data_index: 3, mem: 2 });
        let out = rewrite_refs(&mi, IndexSpace::Mem, &mut |i| if i == 2 { 1 } else { i }).unwrap();
        assert_eq!(out, Inst::MemoryInit { data_index: 4, mem: 1 });

        // The block type's Type reference can be rewritten; the empty form is unaffected.
        let b = Inst::Block { blockty: BlockType::FuncType(9) };
        let out = rewrite_refs(&b, IndexSpace::Type, &mut |i| if i == 9 { 8 } else { i }).unwrap();
        assert_eq!(out, Inst::Block { blockty: BlockType::FuncType(8) });
        assert!(rewrite_refs(&Inst::Block { blockty: BlockType::Empty }, IndexSpace::Type, &mut |i| i + 1).is_none());

        // No change after mapping → None.
        assert!(rewrite_refs(&ci, IndexSpace::Type, &mut |i| i).is_none());
        // The instruction has no reference in that space → None.
        assert!(rewrite_refs(&Inst::Nop, IndexSpace::Func, &mut |i| i + 1).is_none());
    }
}
