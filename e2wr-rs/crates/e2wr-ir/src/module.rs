//! Module-level intermediate representation (M2.1).
//!
//! Mirrors the field set of `WasmParser` in Python `extract_block_mutator/WasmParser.py`
//! (types/imports/defined_func_ty_ids/defined_table_datas/defined_memory_datas/
//! defined_globals/exports/start_sec_data/elem_sec_datas/defined_funcs/
//! data_sec_datas/data_count_sec_data/customs). Field naming matches Python.
//!
//! Structural differences (D-1/D-6): the dynamic-dict DataPayloadwithName is not ported;
//! strongly-typed structs are used instead; `Func` stores a type index instead of a type-object reference, and the reducer maintains index consistency.

use crate::inst_gen::Inst;

// R-26: wasmparser type re-exports keep only names with actual users (MemArg/RefType
// are used by tests; BlockType is used by the encoding side). ValType/HeapType had no users
// of this path (downstream writes the full wasmparser:: path), so they were dropped from the re-exports;
// this file itself still uses ValType via a private import.

pub use wasmparser::{BlockType, MemArg, RefType};
use wasmparser::ValType;

/// Function type (Python `funcType`).
#[derive(Debug, Clone, PartialEq, Eq, Hash)]
pub struct FuncType {
    pub params: Vec<ValType>,
    pub results: Vec<ValType>,
}

/// Limits (min/max; Python table/memory limits).
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Limits {
    pub min: u64,
    pub max: Option<u64>,
}

/// Table type.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct TableType {
    pub elem_ty: RefType,
    pub limits: Limits,
}

/// Memory type.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct MemType {
    pub limits: Limits,
}

/// Global variable type.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct GlobalType {
    pub val_ty: ValType,
    pub mutable: bool,
}

/// An import entry (the import flavor of Python `DataPayloadwithName`).
#[derive(Debug, Clone, PartialEq)]
pub struct Import {
    pub module: String,
    pub name: String,
    pub desc: ImportDesc,
}

#[derive(Debug, Clone, PartialEq)]
pub enum ImportDesc {
    Func(u32),
    Table(TableType),
    Memory(MemType),
    Global(GlobalType),
}

/// A table defined in the module (reference-types allow an initializer expression).
#[derive(Debug, Clone, PartialEq)]
pub struct Table {
    pub ty: TableType,
    /// None = no explicit initializer expression (defaults to the table type's null reference).
    pub init_expr: Option<Vec<Inst>>,
}

#[derive(Debug, Clone, PartialEq)]
pub struct Memory {
    pub ty: MemType,
}

#[derive(Debug, Clone, PartialEq)]
pub struct Global {
    pub ty: GlobalType,
    pub init_expr: Vec<Inst>,
}

#[derive(Debug, Clone, PartialEq)]
pub struct Export {
    pub name: String,
    pub desc: ExportDesc,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum ExportDesc {
    Func(u32),
    Table(u32),
    Memory(u32),
    Global(u32),
}

/// Element segment.
#[derive(Debug, Clone, PartialEq)]
pub struct ElemSeg {
    pub mode: ElemMode,
    pub payload: ElemPayload,
}

#[derive(Debug, Clone, PartialEq)]
pub enum ElemMode {
    Passive,
    Declarative,
    Active {
        /// None = MVP encoding form (flags 0x00, implicitly table 0);
        /// Some(0) is the explicit table index form (flags 0x02). The original form is kept so that re-encoding
        /// reproduces the original bytes (equivalent to Python copying unmodified definition bytes).
        table_idx: Option<u32>,
        offset: Vec<Inst>,
    },
}

#[derive(Debug, Clone, PartialEq)]
pub enum ElemPayload {
    /// Expressed as a function index list (encoded in the shorthand form).
    FuncIdxs(Vec<u32>),
    /// Expressed as a constant expression list, with the element reference type.
    Exprs { elem_ty: RefType, exprs: Vec<Vec<Inst>> },
}

/// Data segment.
#[derive(Debug, Clone, PartialEq)]
pub struct DataSeg {
    pub mode: DataMode,
    pub data: Vec<u8>,
}

#[derive(Debug, Clone, PartialEq)]
pub enum DataMode {
    Passive,
    Active { mem_idx: u32, offset: Vec<Inst> },
}

/// Custom section.
#[derive(Debug, Clone, PartialEq)]
pub struct Custom {
    pub name: String,
    pub data: Vec<u8>,
}

/// A function defined in the module (Python `wasmFunc`).
///
/// `locals` is the expanded local variable sequence (Python `defined_local_types`); on encoding it is
/// compressed back into (type, count) declarations; `insts` includes the trailing `end` instruction.
#[derive(Debug, Clone, PartialEq)]
pub struct Func {
    pub ty_idx: u32,
    pub locals: Vec<ValType>,
    pub insts: Vec<Inst>,
}

/// The module (the intermediate-representation part of Python `WasmParser`).
#[derive(Debug, Clone, Default, PartialEq)]
pub struct Module {
    pub types: Vec<FuncType>,
    pub imports: Vec<Import>,
    /// Type indices of the function section (module-defined functions only).
    pub defined_func_ty_ids: Vec<u32>,
    pub defined_table_datas: Vec<Table>,
    pub defined_memory_datas: Vec<Memory>,
    pub defined_globals: Vec<Global>,
    pub exports: Vec<Export>,
    pub start_sec_data: Option<u32>,
    pub elem_sec_datas: Vec<ElemSeg>,
    pub defined_funcs: Vec<Func>,
    pub data_sec_datas: Vec<DataSeg>,
    pub data_count_sec_data: Option<u32>,
    pub customs: Vec<Custom>,
}

impl Module {
    /// Import counts per kind (Python `import_*_num`).
    pub fn import_func_num(&self) -> usize {
        self.imports.iter().filter(|i| matches!(i.desc, ImportDesc::Func(_))).count()
    }
    pub fn import_table_num(&self) -> usize {
        self.imports.iter().filter(|i| matches!(i.desc, ImportDesc::Table(_))).count()
    }
    pub fn import_memory_num(&self) -> usize {
        self.imports.iter().filter(|i| matches!(i.desc, ImportDesc::Memory(_))).count()
    }
    pub fn import_global_num(&self) -> usize {
        self.imports.iter().filter(|i| matches!(i.desc, ImportDesc::Global(_))).count()
    }

    /// Type indices of imported functions (Python `import_func_ty_ids`).
    /// R-26: no users outside the crate (only the same file's `func_type_idxs` uses it); made private.
    fn import_func_ty_ids(&self) -> Vec<u32> {
        self.imports
            .iter()
            .filter_map(|i| match &i.desc {
                ImportDesc::Func(ty) => Some(*ty),
                _ => None,
            })
            .collect()
    }

    /// Type indices over the whole function index space (imports first, then definitions; Python `func_type_idxs`).
    pub fn func_type_idxs(&self) -> Vec<u32> {
        let mut v = self.import_func_ty_ids();
        v.extend(self.defined_func_ty_ids.iter().copied());
        v
    }
}
