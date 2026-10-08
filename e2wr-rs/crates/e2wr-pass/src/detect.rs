//! Definition-reference detection (M4 first half): per index space plus types, the used/unused index sets.
//!
//! Mirrors the `detect_used_unused_*` family of `UnusedDefReducer.py` and
//! `detect_used_unused_type_idxs` of `UnusedDefReducerUtil.py`.
//! Differences from Python are all recorded corrections or user rulings:
//! - memory detection reads real indices, covering memory.size/grow/fill etc. (D-7 multi-memory correct semantics);
//! - the global candidate set = the defined range only (P-18 correction; Python mistakenly used defined+imported count);
//! - the tail-call family (return_call / return_call_indirect) is also marked (P-20 correction);
//! - function detection additionally scans global and table initializer expressions (P-21 correction);
//! - functions referenced by the start section are not marked used (same as Python: the start function itself is a deletion candidate,
//!   and the start section as a whole is in the candidate set).
//!
//! Python reduction strategy kept: type values dedup by first occurrence (only the first index of an equal type counts as used).
//!
//! Implementation structure (single-pass refactor): `scan_module_refs` collects the references of all spaces and block type values
//! in **one** pass with the `for_each_inst`/`for_each_ref` primitives (P-21 supplementary scan included); `detect_all` does pure
//! set operations on top to produce all eight results; the eight `detect_*` functions just read fields of `detect_all` —
//! one detection logic for the whole module, no per-detect scan loops.

use std::collections::{BTreeSet, HashSet};

use e2wr_ir::module::*;
use e2wr_ir::refs::{blocktype_func_type, for_each_inst, for_each_ref, IndexSpace};
use e2wr_ir::Inst;

/// The used/unused split of one index space.
#[derive(Debug, Clone, Default, PartialEq, Eq)]
pub struct UsedUnused {
    pub used: BTreeSet<u32>,
    pub unused: BTreeSet<u32>,
}

impl UsedUnused {
    fn from_full_used(full_used: &BTreeSet<u32>, total: u32) -> Self {
        let mut used = BTreeSet::new();
        let mut unused = BTreeSet::new();
        for i in 0..total {
            if full_used.contains(&i) {
                used.insert(i);
            } else {
                unused.insert(i);
            }
        }
        UsedUnused { used, unused }
    }
}

/// Per-space references (whole-space numbering) and block type values collected in one pass.
#[derive(Debug, Default)]
pub struct ModuleRefs {
    pub funcs: BTreeSet<u32>,
    pub globals: BTreeSet<u32>,
    pub mems: BTreeSet<u32>,
    pub tables: BTreeSet<u32>,
    pub elemsegs: BTreeSet<u32>,
    pub datas: BTreeSet<u32>,
    pub types: BTreeSet<u32>,
    /// Function type values resolved from block types (all three forms: empty/value/index).
    pub block_type_values: HashSet<FuncType>,
}

impl ModuleRefs {
    fn insert(&mut self, space: IndexSpace, idx: u32) {
        match space {
            IndexSpace::Func => &mut self.funcs,
            IndexSpace::Global => &mut self.globals,
            IndexSpace::Mem => &mut self.mems,
            IndexSpace::Table => &mut self.tables,
            IndexSpace::ElemSeg => &mut self.elemsegs,
            IndexSpace::DataSeg => &mut self.datas,
            IndexSpace::Type => &mut self.types,
        }
        .insert(idx);
    }
}

/// Single-pass scan: references of all function-body instructions + block type values + the P-21 supplementary
/// scan (references inside global and table initializer expressions).
pub fn scan_module_refs(module: &Module) -> ModuleRefs {
    let mut out = ModuleRefs::default();
    for_each_inst(module, &mut |_pos, inst| {
        for_each_ref(inst, &mut |space, idx| out.insert(space, idx));
        let bt = match inst {
            Inst::Block { blockty } | Inst::Loop { blockty } | Inst::If { blockty } => {
                Some(blockty)
            }
            _ => None,
        };
        if let Some(bt) = bt {
            if let Some(ty) = blocktype_func_type(bt, &module.types) {
                out.block_type_values.insert(ty);
            }
        }
    });
    // P-21 supplementary scan: initializer expressions are section-level data (small); traversed separately.
    for g in &module.defined_globals {
        for inst in &g.init_expr {
            for_each_ref(inst, &mut |space, idx| out.insert(space, idx));
        }
    }
    for t in &module.defined_table_datas {
        if let Some(expr) = &t.init_expr {
            for inst in expr {
                for_each_ref(inst, &mut |space, idx| out.insert(space, idx));
            }
        }
    }
    out
}

/// Function indices referenced by element segments: all of them in the function-index-list form; the ref.func ones in expression form.
fn elemseg_func_idxs(seg: &ElemSeg) -> Vec<u32> {
    match &seg.payload {
        ElemPayload::FuncIdxs(idxs) => idxs.clone(),
        ElemPayload::Exprs { exprs, .. } => exprs
            .iter()
            .flatten()
            .filter(|inst| matches!(inst, Inst::RefFunc { .. }))
            .filter_map(|inst| {
                e2wr_ir::refs::refs(inst)
                    .into_iter()
                    .find(|r| r.space == IndexSpace::Func)
                    .map(|r| r.idx)
            })
            .collect(),
    }
}

/// The raw function-use set (whole-space numbering): function-body/initializer-expression references + element-segment function indices +
/// exported functions. The start section excluded (same as Python).
fn raw_used_func_idxs(module: &Module, refs: &ModuleRefs) -> BTreeSet<u32> {
    let mut used = refs.funcs.clone();
    for seg in &module.elem_sec_datas {
        used.extend(elemseg_func_idxs(seg));
    }
    for export in &module.exports {
        if let ExportDesc::Func(idx) = export.desc {
            used.insert(idx);
        }
    }
    used
}

/// Shifts whole-space numbering to defined-local numbering (import-range numbers dropped).
fn shift_to_defined(full: &BTreeSet<u32>, import_num: usize) -> BTreeSet<u32> {
    full.iter().filter(|i| **i as usize >= import_num).map(|i| i - import_num as u32).collect()
}

/// All eight detection results produced at once.
#[derive(Debug, Default)]
pub struct DetectAll {
    pub funcs: UsedUnused,
    pub globals: UsedUnused,
    pub memories: UsedUnused,
    pub tables: UsedUnused,
    pub elemsegs: UsedUnused,
    pub datas: UsedUnused,
    pub import_funcs: UsedUnused,
    pub types: UsedUnused,
}

/// Single-pass scan + pure set operations, producing all eight detection results.
pub fn detect_all(module: &Module) -> DetectAll {
    let refs = scan_module_refs(module);

    // Functions.
    let raw_funcs = raw_used_func_idxs(module, &refs);
    let funcs_used = shift_to_defined(&raw_funcs, module.import_func_num());
    let funcs = UsedUnused::from_full_used(&funcs_used, module.defined_funcs.len() as u32);

    // Globals (candidates = defined range only, P-18 correction).
    let mut gfull = refs.globals.clone();
    for export in &module.exports {
        if let ExportDesc::Global(idx) = export.desc {
            gfull.insert(idx);
        }
    }
    let globals_used = shift_to_defined(&gfull, module.import_global_num());
    let globals = UsedUnused::from_full_used(&globals_used, module.defined_globals.len() as u32);

    // Memories (real numbering, D-7).
    let mut mfull = refs.mems.clone();
    for data in &module.data_sec_datas {
        if let DataMode::Active { mem_idx, .. } = &data.mode {
            mfull.insert(*mem_idx);
        }
    }
    for export in &module.exports {
        if let ExportDesc::Memory(idx) = export.desc {
            mfull.insert(idx);
        }
    }
    let memories_used = shift_to_defined(&mfull, module.import_memory_num());
    let memories =
        UsedUnused::from_full_used(&memories_used, module.defined_memory_datas.len() as u32);

    // Tables.
    let mut tfull = refs.tables.clone();
    for export in &module.exports {
        if let ExportDesc::Table(idx) = export.desc {
            tfull.insert(idx);
        }
    }
    for seg in &module.elem_sec_datas {
        if let ElemMode::Active { table_idx, .. } = &seg.mode {
            tfull.insert(table_idx.unwrap_or(0));
        }
    }
    let tables_used = shift_to_defined(&tfull, module.import_table_num());
    let tables = UsedUnused::from_full_used(&tables_used, module.defined_table_datas.len() as u32);

    // Element / data segments (no imported form).
    let elemsegs = UsedUnused::from_full_used(&refs.elemsegs, module.elem_sec_datas.len() as u32);
    let datas = UsedUnused::from_full_used(&refs.datas, module.data_sec_datas.len() as u32);

    // Imported functions (mapped back to import-entry numbering).
    let mut import_funcs = UsedUnused::default();
    let mut ifunc_idx = 0u32;
    for (import_idx, imp) in module.imports.iter().enumerate() {
        if matches!(imp.desc, ImportDesc::Func(_)) {
            if raw_funcs.contains(&ifunc_idx) {
                import_funcs.used.insert(import_idx as u32);
            } else {
                import_funcs.unused.insert(import_idx as u32);
            }
            ifunc_idx += 1;
        }
    }

    // Types: directly-indexed references (function signatures + instructions + imported functions) + block type values;
    // dedup strategy kept from Python (first occurrence wins for equal values).
    let mut direct: BTreeSet<u32> = module.func_type_idxs().into_iter().collect();
    for imp in &module.imports {
        if let ImportDesc::Func(ty_idx) = imp.desc {
            direct.insert(ty_idx);
        }
    }
    direct.extend(refs.types.iter().copied());
    let mut used_values: HashSet<FuncType> = refs.block_type_values;
    for idx in &direct {
        if let Some(ty) = module.types.get(*idx as usize) {
            used_values.insert(ty.clone());
        }
    }
    let mut types = UsedUnused::default();
    let mut seen: HashSet<FuncType> = HashSet::new();
    for (idx, ty) in module.types.iter().enumerate() {
        if used_values.contains(ty) && !seen.contains(ty) {
            types.used.insert(idx as u32);
            seen.insert(ty.clone());
        } else {
            types.unused.insert(idx as u32);
        }
    }

    DetectAll { funcs, globals, memories, tables, elemsegs, datas, import_funcs, types }
}

// ---------------------------------------------------------------------------
// Single-function entries (all read fields of detect_all; one logic, no separate scan loops).
// ---------------------------------------------------------------------------

/// Used/unused defined functions (local numbering; candidates = all defined functions).
pub fn detect_funcs(module: &Module) -> UsedUnused {
    detect_all(module).funcs
}

/// Used/unused defined globals (local numbering). Candidates = defined range only (P-18 correction).
pub fn detect_globals(module: &Module) -> UsedUnused {
    detect_all(module).globals
}

/// Used/unused defined memories (local numbering). Reads real memory numbers (D-7 user ruling).
pub fn detect_memories(module: &Module) -> UsedUnused {
    detect_all(module).memories
}

/// Used/unused defined tables (local numbering).
pub fn detect_tables(module: &Module) -> UsedUnused {
    detect_all(module).tables
}

/// Used/unused element segments (segment indices).
pub fn detect_elemsegs(module: &Module) -> UsedUnused {
    detect_all(module).elemsegs
}

/// Used/unused data segments (segment indices).
pub fn detect_datas(module: &Module) -> UsedUnused {
    detect_all(module).datas
}

/// Used/unused imported functions (import-entry numbering).
pub fn detect_import_funcs(module: &Module) -> UsedUnused {
    detect_all(module).import_funcs
}

/// Used/unused type-section entries (equal values dedup by first occurrence, the Python reduction strategy kept).
pub fn detect_types(module: &Module) -> UsedUnused {
    detect_all(module).types
}
