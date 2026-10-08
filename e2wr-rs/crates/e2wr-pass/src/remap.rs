//! Index remapping (M4 first half): given deletion sets, produce an e2wr-ir mutation batch (the module is not edited directly).
//!
//! Mirrors the `update_parser_after_remove_global/_data/
//! _memory/_elem/_export/_table` and `update_parser_after_remove_start` of Python `UnusedDefReducer.py`,
//! `update_parser_after_remove_type` of `UnusedDefReducerUtil.py`,
//! `remove_dead_funcs` of `ReduceUtil/RemoveDeadFunc.py`,
//! and the stack-padding instruction sequences of `ReduceUtil/NewInstUtil.py`.
//!
//! Differences from Python are all recorded corrections or user rulings:
//! - deleting a memory performs a full index shift (D-7; Python only emits a section deletion);
//! - instruction rewrites merge per index-space field (D-8; later Python steps overwrote wholesale);
//! - index-form block types renumber with type deletions (P-17; a dead branch in Python);
//! - export deletion checks whole-space indices (P-19; Python conflated defined-local indices);
//! - the tail-call family is handled too (P-20; Python does not);
//! - an active element segment in implicit (MVP) form is upgraded to an explicit table number when table 0 moves (correct semantics;
//!   Python leaves the implicit form untouched).
//!
//! Python's single-instruction replacement is merged here into whole-function re-encoding: collect
//! (function index, instruction ordinal, new instruction sequence); within one function apply by ordinal, largest first, onto a body copy
//! (replacement lengths vary; back-to-front avoids misalignment), then produce the Code-section mutation via `func_def`.

use std::collections::{BTreeMap, BTreeSet, HashMap};

use anyhow::{bail, Context, Result};
use rand::Rng;

use e2wr_ir::module::*;
use wasmparser::ValType;
use e2wr_ir::mutation::{data_def, elem_def, export_def, func_def, import_def, u32_def, DefEdit, Mutation};
use e2wr_ir::refs::{for_each_index_field, for_each_inst, IndexSpace};
use e2wr_ir::snapshot::SectionKind;
use e2wr_ir::Inst;

/// Deletion description: the entry sets to delete per index space.
/// funcs/globals/mems/tables are defined-local indices; imports/exports are in-section entry indices;
/// types/elemsegs/datas are section indices; start means deleting the start section.
#[derive(Debug, Clone, Default, PartialEq, Eq)]
pub struct DeleteSet {
    pub funcs: BTreeSet<u32>,
    pub imports: BTreeSet<u32>,
    pub globals: BTreeSet<u32>,
    pub mems: BTreeSet<u32>,
    pub tables: BTreeSet<u32>,
    pub elemsegs: BTreeSet<u32>,
    pub datas: BTreeSet<u32>,
    pub exports: BTreeSet<u32>,
    pub types: BTreeSet<u32>,
    pub start: bool,
}

/// Instruction-level rewrite accumulator: position → new instruction sequence. Each position is written at most once per scan
/// (multi-space mappings overlay in one pass, the D-8 field-overlay semantics).
#[derive(Default)]
struct FuncRewrites {
    /// (function index, instruction ordinal) → new instruction sequence.
    map: BTreeMap<(u32, u32), Vec<Inst>>,
}

impl FuncRewrites {
    fn insert(&mut self, func_idx: u32, inst_idx: u32, new_insts: Vec<Inst>) {
        if self.map.insert((func_idx, inst_idx), new_insts).is_some() {
            panic!("the same instruction position was replaced twice (programming error)")
        }
    }

    /// Produces the Code-section mutation: each rewritten function replays all edits then re-encodes whole.
    fn build_code_edits(&self, module: &Module) -> Result<Vec<DefEdit>> {
        let mut by_func: BTreeMap<u32, Vec<(u32, Vec<Inst>)>> = BTreeMap::new();
        for ((func_idx, inst_idx), new_insts) in &self.map {
            by_func.entry(*func_idx).or_default().push((*inst_idx, new_insts.clone()));
        }
        let mut edits = Vec::new();
        for (func_idx, mut replacements) in by_func {
            let Some(func) = module.defined_funcs.get(func_idx as usize) else {
                bail!("inst rewrite targets deleted/out-of-range func {func_idx}");
            };
            let mut new_func = func.clone();
            // Applied back-to-front; variable-length replacements cannot misalign earlier ordinals.
            replacements.sort_by(|a, b| b.0.cmp(&a.0));
            for (inst_idx, new_insts) in replacements {
                let at = inst_idx as usize;
                if at >= new_func.insts.len() {
                    bail!("inst rewrite index {inst_idx} out of range in func {func_idx}");
                }
                new_func.insts.splice(at..at + 1, new_insts);
            }
            edits.push(DefEdit::replace_one(func_idx, func_def(&new_func)?));
        }
        Ok(edits)
    }
}

/// Rewrite information for the function space (produced by the remove_dead_funcs step).
struct FuncRewriteInfo {
    /// Whole-space deleted-function set (for the call/return_call padding special cases).
    all_deleted: BTreeSet<u32>,
    /// The general map over the whole function space (not deleted → new index; deleted → repoint at the first post-deletion user function),
    /// used by ref.func and calls to non-deleted functions.
    ref_func_map: BTreeMap<u32, u32>,
}

/// Instruction rewrite plan: per-space index maps (None = no deletion step for that space; skip the lookup while scanning).
/// Phase one fills it in Python step order; phase two applies everything in one scan.
#[derive(Default)]
struct RewritesPlan {
    func: Option<FuncRewriteInfo>,
    global: Option<BTreeMap<u32, u32>>,
    data: Option<BTreeMap<u32, u32>>,
    mem: Option<BTreeMap<u32, u32>>,
    elemseg: Option<BTreeMap<u32, u32>>,
    /// Type index → new index (the value map with first-occurrence-wins pre-expanded to all indices, deleted ones included —
    /// instructions referencing a deleted duplicate of a kept value repoint at the first kept index, P-17).
    type_idx_map: Option<BTreeMap<u32, u32>>,
    table: Option<BTreeMap<u32, u32>>,
    /// Defined-local indices of deleted functions (skipped while scanning).
    del_defined_funcs: BTreeSet<u32>,
}

impl RewritesPlan {
    fn map_for(&self, space: IndexSpace) -> Option<&BTreeMap<u32, u32>> {
        match space {
            IndexSpace::Func => self.func.as_ref().map(|f| &f.ref_func_map),
            IndexSpace::Global => self.global.as_ref(),
            IndexSpace::Mem => self.mem.as_ref(),
            IndexSpace::Table => self.table.as_ref(),
            IndexSpace::ElemSeg => self.elemseg.as_ref(),
            IndexSpace::DataSeg => self.data.as_ref(),
            IndexSpace::Type => self.type_idx_map.as_ref(),
        }
    }
}

/// Element-segment rewrite accumulator: function-index rewrites (remove_dead_funcs) and table-number rewrites (remove_tables)
/// may both touch the same segment; fields overlay and a single replacement is emitted,
/// avoiding same-position section mutations discarding each other in the mutation channel.
#[derive(Default)]
struct ElemRewrites {
    map: BTreeMap<u32, ElemSeg>,
}

impl ElemRewrites {
    fn entry(&mut self, idx: u32, orig: &ElemSeg) -> &mut ElemSeg {
        self.map.entry(idx).or_insert_with(|| orig.clone())
    }

    fn build_mutation(&self) -> Result<Option<Mutation>> {
        if self.map.is_empty() {
            return Ok(None);
        }
        let mut edits = Vec::new();
        for (idx, seg) in &self.map {
            edits.push(DefEdit::replace_one(*idx, elem_def(seg)?));
        }
        Ok(Some(Mutation::definitions(SectionKind::Element, edits)))
    }
}

/// Builds the whole-space index map: continuous whole-space numbering (imports first), skipping deleted entries shifts left
/// (like Python's ori_idx2_new_idx: whole-space index minus the accumulated skips).
fn build_shift_map(import_num: usize, defined_num: usize, del_defined: &BTreeSet<u32>) -> BTreeMap<u32, u32> {
    let mut map = BTreeMap::new();
    for i in 0..import_num as u32 {
        map.insert(i, i);
    }
    let mut skip = 0u32;
    for i in 0..defined_num as u32 {
        if del_defined.contains(&i) {
            skip += 1;
        } else {
            // The whole-space number of defined-local i is i + import_num; the shifted new number likewise
            // stays in the whole-space coordinate system.
            map.insert(i + import_num as u32, i + import_num as u32 - skip);
        }
    }
    map
}

fn lookup(map: &BTreeMap<u32, u32>, idx: u32) -> u32 {
    map.get(&idx).copied().unwrap_or_else(|| panic!("index {idx} missing in remap map"))
}

/// Export-section handling (shared by the global/table/memory/func steps):
/// pointing at a deleted entry (whole-space numbering, P-19 correction) → drop the export; number changed → rewrite.
#[allow(clippy::too_many_arguments)]
fn rewrite_exports_of_kind(
    module: &Module,
    deleted_full: &BTreeSet<u32>,
    shift: &BTreeMap<u32, u32>,
    kind: ExportDesc,
    mutations: &mut Vec<Mutation>,
) -> Result<()> {
    // `kind` is passed as a tag (each variant's number serves as the match key).
    let kind_of = |d: &ExportDesc| std::mem::discriminant(d);
    let want = kind_of(&kind);
    for (i, e) in module.exports.iter().enumerate() {
        if kind_of(&e.desc) != want {
            continue;
        }
        let idx = match &e.desc {
            ExportDesc::Func(v) | ExportDesc::Table(v) | ExportDesc::Memory(v) | ExportDesc::Global(v) => *v,
        };
        if deleted_full.contains(&idx) {
            mutations.push(Mutation::definitions(
                SectionKind::Export,
                vec![DefEdit::delete(i as u32)],
            ));
        } else {
            let new = lookup(shift, idx);
            if new != idx {
                let mut ne = e.clone();
                ne.desc = match &e.desc {
                    ExportDesc::Func(_) => ExportDesc::Func(new),
                    ExportDesc::Table(_) => ExportDesc::Table(new),
                    ExportDesc::Memory(_) => ExportDesc::Memory(new),
                    ExportDesc::Global(_) => ExportDesc::Global(new),
                };
                mutations.push(Mutation::definitions(
                    SectionKind::Export,
                    vec![DefEdit::replace_one(i as u32, export_def(&ne)?)],
                ));
            }
        }
    }
    Ok(())
}

/// Defined-local deletion set → whole-space deletion set.
fn full_space_del(import_num: usize, del_defined: &BTreeSet<u32>) -> BTreeSet<u32> {
    del_defined.iter().map(|i| i + import_num as u32).collect()
}

/// Per-entry deletion within a section.
fn delete_each(mutations: &mut Vec<Mutation>, section: SectionKind, del: &BTreeSet<u32>) {
    if del.is_empty() {
        return;
    }
    mutations.push(Mutation::definitions(
        section,
        del.iter().map(|i| DefEdit::delete(*i)).collect(),
    ));
}

// ---------------------------------------------------------------------------
// Stack padding (Python NewInstUtil.padding_input_type_naive; constant values chosen at random,
// per D-2: no attempt to match Python's random sequence).
// ---------------------------------------------------------------------------

fn padding_insts(params: &[ValType], results: &[ValType], rng: &mut impl Rng) -> Result<Vec<Inst>> {
    // The common prefix toward the stack bottom stays untouched.
    let common = params.iter().zip(results).take_while(|(a, b)| a == b).count();
    // Extra params are dropped one by one, top-of-stack first.
    let drops = params.len() - common;
    // Missing results are padded with constants.
    let to_pad = &results[common..];
    let mut insts = vec![Inst::Drop; drops];
    for ty in to_pad {
        insts.push(const_inst(ty, rng)?);
    }
    Ok(insts)
}

/// Generates a constant instruction by type (Python `NewInstUtil.get_inst_by_require_ty_const_n`;
/// same value set and randomness semantics, not chasing Python's sequence, D-2). Shared by M8/M9 and this file.
/// R-31: was a private const_inst plus a cross-module forwarding shell const_inst_for_type;
/// the shell was deleted and this became pub(crate) for 5 modules.
pub(crate) fn const_inst(ty: &ValType, rng: &mut impl Rng) -> Result<Inst> {
    Ok(match ty {
        ValType::I32 => Inst::I32Const { value: if rng.gen_bool(0.5) { 1 } else { 0 } },
        ValType::I64 => Inst::I64Const { value: if rng.gen_bool(0.5) { 1 } else { 0 } },
        ValType::F32 => Inst::F32Const {
            value: wasmparser::Ieee32::from(if rng.gen_bool(0.5) { 1.0 } else { 0.0 }),
        },
        ValType::F64 => Inst::F64Const {
            value: wasmparser::Ieee64::from(if rng.gen_bool(0.5) { 1.0 } else { 0.0 }),
        },
        ValType::V128 => {
            // wasmparser::V128 has no public constructor; built from bytes via the instruction decoder.
            let mut bytes = [0u8; 16];
            rng.fill(&mut bytes);
            let mut buf = vec![0xfd, 0x0c];
            buf.extend_from_slice(&bytes);
            let mut reader =
                wasmparser::OperatorsReader::new(wasmparser::BinaryReader::new(&buf, 0));
            match reader.read()? {
                wasmparser::Operator::V128Const { value } => Inst::V128Const { value },
                _ => bail!("v128 decode produced unexpected operator"),
            }
        }
        ValType::Ref(rt) => Inst::RefNull { hty: rt.heap_type() },
    })
}

// ---------------------------------------------------------------------------
// The remap steps (order matching the call order of Python UnusedDefReducer.try_remove).
// ---------------------------------------------------------------------------

/// Deletes dead functions (imported ones included). Mirrors RemoveDeadFunc.remove_dead_funcs
/// (invoked with rewrite_callsites=true; callsite_as_unreachable is passed by `remap_all_opts`
/// and takes effect in phase two's padding special case while scanning).
fn step_remove_dead_funcs(
    module: &Module,
    del: &DeleteSet,
    plan: &mut RewritesPlan,
    elem_rewrites: &mut ElemRewrites,
    mutations: &mut Vec<Mutation>,
) -> Result<()> {
    if del.funcs.is_empty() && del.imports.is_empty() {
        return Ok(());
    }

    // Imported function index ↔ import entry index.
    let mut ifunc2import: BTreeMap<u32, u32> = BTreeMap::new();
    for (import_idx, imp) in module.imports.iter().enumerate() {
        if let ImportDesc::Func(_) = imp.desc {
            let n = ifunc2import.len() as u32;
            ifunc2import.insert(n, import_idx as u32);
        }
    }
    // Import entry index → imported function index (function imports only; the candidate set contains only function imports anyway).
    let import2ifunc: BTreeMap<u32, u32> =
        ifunc2import.iter().map(|(k, v)| (*v, *k)).collect();
    let del_import_funcs: BTreeSet<u32> = del
        .imports
        .iter()
        .filter_map(|i| import2ifunc.get(i).copied())
        .collect();

    let import_func_num = ifunc2import.len();
    // Whole-function-space map (imports first).
    let shift = {
        let mut map = BTreeMap::new();
        let mut skip = 0u32;
        for i in 0..import_func_num as u32 {
            if del_import_funcs.contains(&i) {
                skip += 1;
            } else {
                map.insert(i, i - skip);
            }
        }
        for i in 0..module.defined_funcs.len() as u32 {
            if del.funcs.contains(&i) {
                skip += 1;
            } else {
                map.insert(i + import_func_num as u32, i + import_func_num as u32 - skip);
            }
        }
        map
    };
    // Whole-space deleted-function set.
    let all_deleted: BTreeSet<u32> = {
        let mut s = del_import_funcs.clone();
        s.extend(del.funcs.iter().map(|i| i + import_func_num as u32));
        s
    };
    // ref.func pointing at a deleted function repoints at the first post-deletion user function.
    let padding_func_idx = import_func_num as u32 - del_import_funcs.len() as u32;

    // Element-segment function index rewrite (folded into elem_rewrites).
    for (seg_idx, seg) in module.elem_sec_datas.iter().enumerate() {
        let seg_changed = match &seg.payload {
            ElemPayload::FuncIdxs(_) => {
                let cur = elem_rewrites.entry(seg_idx as u32, seg);
                match &mut cur.payload {
                    ElemPayload::FuncIdxs(cur_idxs) => {
                        let before = cur_idxs.clone();
                        for idx in cur_idxs.iter_mut() {
                            *idx = if all_deleted.contains(idx) {
                                padding_func_idx
                            } else {
                                lookup(&shift, *idx)
                            };
                        }
                        *cur_idxs != before
                    }
                    ElemPayload::Exprs { .. } => unreachable!(),
                }
            }
            ElemPayload::Exprs { .. } => {
                let cur = elem_rewrites.entry(seg_idx as u32, seg);
                match &mut cur.payload {
                    ElemPayload::Exprs { exprs, .. } => {
                        let mut changed = false;
                        for expr in exprs.iter_mut() {
                            for inst in expr.iter_mut() {
                                if let Inst::RefFunc { function_index } = inst {
                                    let before = *function_index;
                                    *function_index =
                                        if all_deleted.contains(&before) {
                                            padding_func_idx
                                        } else {
                                            lookup(&shift, before)
                                        };
                                    if *function_index != before {
                                        changed = true;
                                    }
                                }
                            }
                        }
                        changed
                    }
                    ElemPayload::FuncIdxs(_) => unreachable!(),
                }
            }
        };
        if !seg_changed {
            // Unchanged segments stay out of the rewrite table (avoiding redundant equal-value replacements).
            elem_rewrites.map.remove(&(seg_idx as u32));
        }
    }

    // Exports: delete or renumber.
    rewrite_exports_of_kind(module, &all_deleted, &shift, ExportDesc::Func(0), mutations)?;

    // start section: delete if it points at a deleted function, otherwise renumber.
    if let Some(start) = module.start_sec_data {
        if all_deleted.contains(&start) {
            mutations.push(Mutation::definitions(
                SectionKind::Start,
                vec![DefEdit::delete(0)],
            ));
        } else {
            let new = lookup(&shift, start);
            if new != start {
                mutations.push(Mutation::definitions(
                    SectionKind::Start,
                    vec![DefEdit::replace_one(0, u32_def(new))],
                ));
            }
        }
    }

    // Function + Code sections delete by defined-local index; the Import section by import-entry index.
    delete_each(mutations, SectionKind::Function, &del.funcs);
    delete_each(mutations, SectionKind::Code, &del.funcs);
    delete_each(mutations, SectionKind::Import, &del.imports);

    // The function-space instruction rewrites are registered into the plan (padding/renumbering applied uniformly in the single scan):
    // ref.func goes through the general map (not deleted → new index; deleted → padding); the padding special case for
    // call/return_call is decided by all_deleted in the scan callback (see scan_and_rewrite).
    let mut ref_func_map = shift.clone();
    for i in &all_deleted {
        ref_func_map.insert(*i, padding_func_idx);
    }
    plan.func = Some(FuncRewriteInfo { all_deleted, ref_func_map });
    plan.del_defined_funcs = del.funcs.clone();
    Ok(())
}

/// Mirrors update_parser_after_remove_global.
fn step_remove_globals(
    module: &Module,
    del: &DeleteSet,
    plan: &mut RewritesPlan,
    mutations: &mut Vec<Mutation>,
) -> Result<()> {
    if del.globals.is_empty() {
        return Ok(());
    }
    let import_num = module.import_global_num();
    let shift = build_shift_map(import_num, module.defined_globals.len(), &del.globals);
    let deleted_full = full_space_del(import_num, &del.globals);

    plan.global = Some(shift.clone());
    rewrite_exports_of_kind(module, &deleted_full, &shift, ExportDesc::Global(0), mutations)?;
    delete_each(mutations, SectionKind::Global, &del.globals);
    Ok(())
}

/// Mirrors update_parser_after_remove_data.
fn step_remove_datas(
    module: &Module,
    del: &DeleteSet,
    plan: &mut RewritesPlan,
    mutations: &mut Vec<Mutation>,
) -> Result<()> {
    if del.datas.is_empty() {
        return Ok(());
    }
    let shift = build_shift_map(0, module.data_sec_datas.len(), &del.datas);
    plan.data = Some(shift);
    delete_each(mutations, SectionKind::Data, &del.datas);
    Ok(())
}

/// Mirrors update_parser_after_remove_memory plus the D-7 completion:
/// Python only emits a memory-section deletion; this implementation also shifts all memory indices —
/// instruction memory references, active data segments' memory numbers, memory exports (delete or renumber).
fn step_remove_memories(
    module: &Module,
    del: &DeleteSet,
    plan: &mut RewritesPlan,
    mutations: &mut Vec<Mutation>,
) -> Result<()> {
    if del.mems.is_empty() {
        return Ok(());
    }
    let import_num = module.import_memory_num();
    let shift = build_shift_map(import_num, module.defined_memory_datas.len(), &del.mems);
    let deleted_full = full_space_del(import_num, &del.mems);

    plan.mem = Some(shift.clone());
    // Active data segments' memory numbers are rewritten (same-position conflicts with data-segment deletion follow the M3 mutation-channel
    // rule: deletion wins).
    for (i, data) in module.data_sec_datas.iter().enumerate() {
        if del.datas.contains(&(i as u32)) {
            continue; // the data segment itself is deleted: rewriting mem_idx is pointless; the data step performs the deletion.
        }
        if let DataMode::Active { mem_idx, .. } = &data.mode {
            let new = lookup(&shift, *mem_idx);
            if new != *mem_idx {
                let mut nd = data.clone();
                if let DataMode::Active { mem_idx, .. } = &mut nd.mode {
                    *mem_idx = new;
                }
                mutations.push(Mutation::definitions(
                    SectionKind::Data,
                    vec![DefEdit::replace_one(i as u32, data_def(&nd)?)],
                ));
            }
        }
    }
    rewrite_exports_of_kind(module, &deleted_full, &shift, ExportDesc::Memory(0), mutations)?;
    delete_each(mutations, SectionKind::Memory, &del.mems);
    Ok(())
}

/// Mirrors update_parser_after_remove_elem.
fn step_remove_elemsegs(
    module: &Module,
    del: &DeleteSet,
    plan: &mut RewritesPlan,
    mutations: &mut Vec<Mutation>,
) -> Result<()> {
    if del.elemsegs.is_empty() {
        return Ok(());
    }
    let shift = build_shift_map(0, module.elem_sec_datas.len(), &del.elemsegs);
    plan.elemseg = Some(shift);
    delete_each(mutations, SectionKind::Element, &del.elemsegs);
    Ok(())
}

/// Mirrors update_parser_after_remove_start.
fn step_remove_start(mutations: &mut Vec<Mutation>) {
    mutations.push(Mutation::definitions(SectionKind::Start, vec![DefEdit::delete(0)]));
}

/// Mirrors update_parser_after_remove_export.
fn step_remove_exports(del: &DeleteSet, mutations: &mut Vec<Mutation>) {
    delete_each(mutations, SectionKind::Export, &del.exports);
}

/// Mirrors update_parser_after_remove_type plus the P-17 correction (block type renumbering).
fn step_remove_types(
    module: &Module,
    del: &DeleteSet,
    plan: &mut RewritesPlan,
    mutations: &mut Vec<Mutation>,
) -> Result<()> {
    if del.types.is_empty() {
        return Ok(());
    }
    // Post-deletion type table; type value → new index (first occurrence wins for equal values).
    let new_types: Vec<&FuncType> = module
        .types
        .iter()
        .enumerate()
        .filter(|(i, _)| !del.types.contains(&(*i as u32)))
        .map(|(_, t)| t)
        .collect();
    let mut type2new: HashMap<&FuncType, u32> = HashMap::new();
    for (i, t) in new_types.iter().enumerate() {
        type2new.entry(t).or_insert(i as u32);
    }
    let value_new_idx = |t: &FuncType| -> Result<u32> {
        type2new
            .get(&t)
            .copied()
            .ok_or_else(|| anyhow::anyhow!("type value missing after removal (input inconsistent?)"))
    };

    // Instruction type references renumber: the value map pre-expanded into an index map. Indices whose value survives
    // (deleted duplicates of kept values included — instructions referencing them repoint at the first kept index, P-17)
    // all have a mapping; indices of dropped values (orphan types) are not mapped, and instructions cannot reference their values
    // (guaranteed by detection semantics); a lookup miss fails fast (same as the original step-by-step implementation).
    // call_indirect and index-form block types are applied uniformly in the single scan.
    let mut idx_map = BTreeMap::new();
    for (i, ty) in module.types.iter().enumerate() {
        if let Some(new) = type2new.get(ty) {
            idx_map.insert(i as u32, *new);
        }
    }
    plan.type_idx_map = Some(idx_map);

    // Function-section type index renumbering.
    for (i, ty_idx) in module.defined_func_ty_ids.iter().enumerate() {
        let ty = module
            .types
            .get(*ty_idx as usize)
            .with_context(|| format!("func {i} type {ty_idx} missing"))?;
        let new = value_new_idx(ty)?;
        if new != *ty_idx {
            mutations.push(Mutation::definitions(
                SectionKind::Function,
                vec![DefEdit::replace_one(i as u32, u32_def(new))],
            ));
        }
    }

    // Imported-function type index renumbering.
    for (i, imp) in module.imports.iter().enumerate() {
        if let ImportDesc::Func(ty_idx) = &imp.desc {
            let ty = module
                .types
                .get(*ty_idx as usize)
                .with_context(|| format!("import {i} type {ty_idx} missing"))?;
            let new = value_new_idx(ty)?;
            if new != *ty_idx {
                let mut ni = imp.clone();
                ni.desc = ImportDesc::Func(new);
                mutations.push(Mutation::definitions(
                    SectionKind::Import,
                    vec![DefEdit::replace_one(i as u32, import_def(&ni)?)],
                ));
            }
        }
    }

    delete_each(mutations, SectionKind::Type, &del.types);
    Ok(())
}

/// Mirrors update_parser_after_remove_table.
fn step_remove_tables(
    module: &Module,
    del: &DeleteSet,
    plan: &mut RewritesPlan,
    elem_rewrites: &mut ElemRewrites,
    mutations: &mut Vec<Mutation>,
) -> Result<()> {
    if del.tables.is_empty() {
        return Ok(());
    }
    let import_num = module.import_table_num();
    let shift = build_shift_map(import_num, module.defined_table_datas.len(), &del.tables);
    let deleted_full = full_space_del(import_num, &del.tables);

    plan.table = Some(shift.clone());

    // Active element segments' target table numbers are rewritten. The implicit form (MVP encoding, table 0) is upgraded
    // to an explicit table number when table 0 moves; otherwise it stays implicit (byte-faithful).
    for (seg_idx, seg) in module.elem_sec_datas.iter().enumerate() {
        if let ElemMode::Active { table_idx, .. } = &seg.mode {
            let old = table_idx.unwrap_or(0);
            let new = lookup(&shift, old);
            if new != old {
                let cur = elem_rewrites.entry(seg_idx as u32, seg);
                if let ElemMode::Active { table_idx, .. } = &mut cur.mode {
                    *table_idx = Some(new);
                }
            }
        }
    }

    rewrite_exports_of_kind(module, &deleted_full, &shift, ExportDesc::Table(0), mutations)?;
    delete_each(mutations, SectionKind::Table, &del.tables);
    Ok(())
}

/// Main entry: given deletion sets, produce a mutation batch (via the M3 mutation channel; the module is not edited directly).
/// Step order matches Python UnusedDefReducer.try_remove.
/// Phase two: scan function bodies once, applying all spaces' maps to produce instruction edits.
/// - call / return_call pointing at a deleted function → padding replacement (tail calls get return, P-20);
/// - other references (ref.func, block types) look up the plan per space, cloning once, fields overlaid (D-8);
/// - deleted defined functions are skipped.
fn scan_and_rewrite(
    module: &Module,
    plan: &RewritesPlan,
    rewrites: &mut FuncRewrites,
    callsite_as_unreachable: bool,
    rng: &mut impl Rng,
) -> Result<()> {
    let func_ty_idxs = module.func_type_idxs();
    let mut last_err: Option<anyhow::Error> = None;
    for_each_inst(module, &mut |pos, inst| {
        if last_err.is_some() || plan.del_defined_funcs.contains(&pos.func_idx) {
            return;
        }
        // The padding special case for call / return_call (a deleted callee must not go through the renumbering map).
        if let Some(fri) = &plan.func {
            if matches!(inst, Inst::Call { .. } | Inst::ReturnCall { .. }) {
                let target = match inst {
                    Inst::Call { function_index } | Inst::ReturnCall { function_index } => {
                        *function_index
                    }
                    _ => unreachable!(),
                };
                if fri.all_deleted.contains(&target) {
                    if callsite_as_unreachable {
                        // The True branch at line 204 of Python RemoveDeadFunc.py: a call to a deleted function becomes
                        // a single unreachable wholesale (stack polymorphism; params/results need no padding).
                        // Python does not handle return_call (P-20); Rust likewise swaps return_call for
                        // unreachable (unreachable is also legal at a tail-call position).
                        rewrites.insert(pos.func_idx, pos.inst_idx, vec![Inst::Unreachable]);
                        return;
                    }
                    let ty_idx = match func_ty_idxs.get(target as usize) {
                        Some(t) => *t,
                        None => {
                            last_err = Some(anyhow::anyhow!("call target {target} has no type"));
                            return;
                        }
                    };
                    let ft = match module.types.get(ty_idx as usize) {
                        Some(t) => t,
                        None => {
                            last_err = Some(anyhow::anyhow!("type {ty_idx} missing"));
                            return;
                        }
                    };
                    match padding_insts(&ft.params, &ft.results, rng) {
                        Ok(mut seq) => {
                            if matches!(inst, Inst::ReturnCall { .. }) {
                                seq.push(Inst::Return);
                            }
                            rewrites.insert(pos.func_idx, pos.inst_idx, seq);
                        }
                        Err(e) => last_err = Some(e),
                    }
                    return;
                }
            }
        }
        // General path: per-space map lookups (zero-cost skip when no space is configured).
        let mut new_inst = inst.clone();
        let mut changed = false;
        for_each_index_field(&mut new_inst, &mut |space, idx| {
            if let Some(map) = plan.map_for(space) {
                let new = match map.get(idx) {
                    Some(v) => *v,
                    None => {
                        // Referencing an unmapped index = the caller built a contradictory deletion set (e.g. deleting a
                        // table referenced by an element segment); fail fast (same panic as the original step-by-step implementation).
                        panic!("index {idx} missing in {space:?} remap map");
                    }
                };
                if new != *idx {
                    *idx = new;
                    changed = true;
                }
            }
        });
        if changed {
            rewrites.insert(pos.func_idx, pos.inst_idx, vec![new_inst]);
        }
    });
    match last_err {
        Some(e) => Err(e),
        None => Ok(()),
    }
}

pub fn remap_all(module: &Module, del: &DeleteSet) -> Result<Vec<Mutation>> {
    remap_all_opts(module, del, false)
}

/// The full form of `remap_all`: `callsite_as_unreachable` corresponds to the same-named parameter of
/// `RemoveDeadFunc.remove_dead_funcs` (when True, calls to deleted functions are rewritten from padding to a single unreachable).
/// Since M8 the caller (the unexecuted-function deletion chain of function-level preprocessing) keeps the ability to switch
/// to True (user ruling; not degenerated to constant False).
pub fn remap_all_opts(
    module: &Module,
    del: &DeleteSet,
    callsite_as_unreachable: bool,
) -> Result<Vec<Mutation>> {
    validate_delete_set(module, del)?;

    let mut rewrites = FuncRewrites::default();
    let mut elem_rewrites = ElemRewrites::default();
    let mut mutations: Vec<Mutation> = Vec::new();
    let mut plan = RewritesPlan::default();

    // Phase one: build maps and section-level edits in Python step order (no instruction scan).
    // The generation order of section-level edits is part of the mutation channel's first-wins/deletion-overrides-replacement semantics; do not reorder.
    step_remove_dead_funcs(module, del, &mut plan, &mut elem_rewrites, &mut mutations)?;
    step_remove_globals(module, del, &mut plan, &mut mutations)?;
    step_remove_datas(module, del, &mut plan, &mut mutations)?;
    step_remove_memories(module, del, &mut plan, &mut mutations)?;
    step_remove_elemsegs(module, del, &mut plan, &mut mutations)?;
    if del.start {
        step_remove_start(&mut mutations);
    }
    step_remove_exports(del, &mut mutations);
    step_remove_types(module, del, &mut plan, &mut mutations)?;
    step_remove_tables(module, del, &mut plan, &mut elem_rewrites, &mut mutations)?;

    // Phase two: the single scan producing instruction edits.
    let mut rng = rand::thread_rng();
    scan_and_rewrite(module, &plan, &mut rewrites, callsite_as_unreachable, &mut rng)?;

    if let Some(m) = elem_rewrites.build_mutation()? {
        mutations.push(m);
    }
    let code_edits = rewrites.build_code_edits(module)?;
    if !code_edits.is_empty() {
        mutations.push(Mutation::definitions(SectionKind::Code, code_edits));
    }
    Ok(mutations)
}

/// Deletion-set range validation: everything within the corresponding section lengths (fail fast, no silence).
fn validate_delete_set(module: &Module, del: &DeleteSet) -> Result<()> {
    let check = |name: &str, len: usize, set: &BTreeSet<u32>| -> Result<()> {
        if let Some(i) = set.iter().find(|i| **i as usize >= len) {
            bail!("{name} delete index {i} out of range (len {len})");
        }
        Ok(())
    };
    check("funcs", module.defined_funcs.len(), &del.funcs)?;
    check("imports", module.imports.len(), &del.imports)?;
    check("globals", module.defined_globals.len(), &del.globals)?;
    check("mems", module.defined_memory_datas.len(), &del.mems)?;
    check("tables", module.defined_table_datas.len(), &del.tables)?;
    check("elemsegs", module.elem_sec_datas.len(), &del.elemsegs)?;
    check("datas", module.data_sec_datas.len(), &del.datas)?;
    check("exports", module.exports.len(), &del.exports)?;
    check("types", module.types.len(), &del.types)?;
    Ok(())
}
