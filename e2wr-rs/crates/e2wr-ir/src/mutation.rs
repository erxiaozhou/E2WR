//! Definition-level mutations and the apply-and-encode channel (M3 subset; interface frozen per the design notes).
//!
//! Mirrors `DefinitionMutation`/`MutationBatch` from Python `ParserModificationUtil.py`,
//! `GeneralMutationApplier.apply_mutations_on_snapshot` from `ParserModification.py`
//! (including `sort_and_clear_section_mutations` and `_process_datacount_section`) and
//! `apply_mutation_and_encode_keep_snapshot`.
//!
//! Phase one lands definition-level mutations only (`Mutation::Definitions`); whole-function rewrite = replacing the Code section's
//! definition bytes (Python's `FuncInstMutation`/`RewriteLocalDesc` are also synthesized
//! into Code-section definition bytes inside the Encoder). Instruction-level byte slicing (the FuncBAParts equivalent) is left for phase two
//! (O-2).
//!
//! Main path after the O-6 optimization (approved 2026-09-22): **byte-layer interval replacement + verbatim 
//! pass-through of unmodified sections' original bytes** (following Python's cache pass-through idea, implemented with the Rust section readers' definition boundaries);
//! no more intermediate-representation cloning, no more whole-module re-encoding; the output is fully decoded to validate (invalid fails immediately,
//! not deferred to the oracle). The pre-optimization clone-and-reencode path is kept as
//! [`apply_mutations_reference`], the reference baseline for dual-path comparison (the design-notes gate).

use std::collections::BTreeMap;
use std::borrow::Cow;
use std::ops::Range;
use std::path::Path;

use anyhow::{anyhow, bail, Context, Result};

use crate::decode;
use crate::encode::encode_module;
use crate::inst::Inst;
use crate::module::*;
use crate::snapshot::{SectionKind, Snapshot};

/// Raw definition bytes of one section (elements of Python `DefinitionMutation.new_definitions` —
/// encoded to bytes at construction).
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct RawDef(pub Vec<u8>);

/// Interval replacement on one section's definition list (equivalent to Python's list slice assignment
/// `definitions[start:end] = new_definitions`).
#[derive(Debug, Clone)]
pub struct DefEdit {
    pub range: Range<u32>,
    pub repl: Vec<RawDef>,
}

impl DefEdit {
    pub fn delete(idx: u32) -> Self {
        DefEdit { range: idx..idx + 1, repl: vec![] }
    }

    pub fn replace_one(idx: u32, repl: RawDef) -> Self {
        DefEdit { range: idx..idx + 1, repl: vec![repl] }
    }

    fn is_delete(&self) -> bool {
        self.repl.is_empty()
    }

    fn is_delete_one(&self) -> bool {
        self.is_delete() && self.range.end - self.range.start == 1
    }

    fn is_one2one_replace(&self) -> bool {
        self.repl.len() == 1 && self.range.end - self.range.start == 1
    }
}

/// A mutation (phase one has the definition-level form only; extended as needed in phase two).
#[derive(Debug, Clone)]
pub enum Mutation {
    Definitions { section: SectionKind, edits: Vec<DefEdit> },
}

impl Mutation {
    pub fn definitions(section: SectionKind, edits: Vec<DefEdit>) -> Self {
        Mutation::Definitions { section, edits }
    }
}

/// Mirrors Python `sort_and_clear_section_mutations`: mutations within one section dedup by (start,end) —
/// an existing delete_one wins; an existing 1-to-1 replace can be overridden by a delete_one;
/// any other duplicate is dropped (the raise branch Python commented out). Then sorted by start, descending.
fn sort_and_clear(mutations: Vec<DefEdit>) -> Vec<DefEdit> {
    if mutations.len() <= 1 {
        return mutations;
    }
    let mut pos2m: BTreeMap<(u32, u32), DefEdit> = BTreeMap::new();
    for m in mutations {
        let pos = (m.range.start, m.range.end);
        match pos2m.get(&pos) {
            Some(ori) => {
                if ori.is_delete_one() {
                    // keep the original delete
                } else if ori.is_one2one_replace() && m.is_delete_one() {
                    pos2m.insert(pos, m);
                }
            }
            None => {
                pos2m.insert(pos, m);
            }
        }
    }
    let mut out: Vec<DefEdit> = pos2m.into_values().collect();
    out.sort_by(|a, b| b.range.start.cmp(&a.range.start));
    out
}

/// **Reference implementation** (the pre-O-6 path): clone the intermediate representation → apply structurally equivalent mutations →
/// re-encode the whole module. Kept for dual-path output comparison (the design-notes gate); production code should use
/// [`apply_mutations`].
pub fn apply_mutations_reference(snap: &Snapshot, batch: &[Mutation]) -> Result<Snapshot> {
    let mut module = snap.module().clone();

    // Group by section (Python sec_type2mutations).
    let mut by_section: BTreeMap<SectionKind, Vec<DefEdit>> = BTreeMap::new();
    for Mutation::Definitions { section, edits } in batch {
        by_section.entry(*section).or_default().extend(edits.iter().cloned());
    }

    let mut code_new_funcs: Vec<Func> = Vec::new();
    for (section, edits) in by_section {
        for edit in sort_and_clear(edits) {
            let s = edit.range.start as usize;
            let e = edit.range.end as usize;
            apply_def_edit(&mut module, section, s, e, &edit.repl, &mut code_new_funcs)
                .with_context(|| format!("applying {section:?} edit {edit:?}"))?;
        }
    }
    // Function/Code section consistency: sync Func.ty_idx from defined_func_ty_ids (D-6).
    if module.defined_funcs.len() == module.defined_func_ty_ids.len() {
        for (f, ty) in module.defined_funcs.iter_mut().zip(&module.defined_func_ty_ids) {
            f.ty_idx = *ty;
        }
    }

    // DataCount consistency (Python _process_datacount_section, the mutation-channel rule, P-16 latter half).
    let data_seg_num = module.data_sec_datas.len() as u32;
    let has_ndc_inst = code_new_funcs.iter().any(|f| body_uses_data_count(&f.insts));
    if data_seg_num > 0 && has_ndc_inst {
        module.data_count_sec_data = Some(data_seg_num);
    }
    if snap.module().data_count_sec_data.is_some() {
        if data_seg_num == 0 {
            module.data_count_sec_data = None;
        } else {
            module.data_count_sec_data = Some(data_seg_num);
        }
    }
    // Note: explicit DataCount edits (editing the DataCount section directly in the batch) already took effect
    // in the by_section loop above; this function does not override them (matching Python's order of applying definition
    // mutations first, then the DataCount rule — the Python rule runs after definition mutations and overrides unconditionally;
    // the explicit-DataCount-edit case does not occur in the corpus).

    Ok(Snapshot::from_module(module))
}

fn body_uses_data_count(insts: &[Inst]) -> bool {
    insts.iter().any(|i| matches!(i, Inst::DataDrop { .. } | Inst::MemoryInit { .. }))
}

fn apply_def_edit(
    module: &mut Module,
    section: SectionKind,
    s: usize,
    e: usize,
    repl: &[RawDef],
    code_new_funcs: &mut Vec<Func>,
) -> Result<()> {
    macro_rules! splice {
        ($list:expr, $conv:expr) => {{
            let decoded: Vec<_> = repl
                .iter()
                .map(|r| $conv(&r.0))
                .collect::<Result<Vec<_>>>()?;
            let list = &mut $list;
            if e > list.len() || s > e {
                bail!("edit range {s}..{e} out of bounds (len {})", list.len());
            }
            list.splice(s..e, decoded);
        }};
    }
    match section {
        SectionKind::Type => {
            splice!(module.types, |b: &[u8]| decode_single_type(b));
        }
        SectionKind::Import => {
            splice!(module.imports, |b: &[u8]| decode_single_import(b));
        }
        SectionKind::Function => {
            splice!(module.defined_func_ty_ids, |b: &[u8]| decode_single_u32(b));
        }
        SectionKind::Table => {
            splice!(module.defined_table_datas, |b: &[u8]| decode_single_table(b));
        }
        SectionKind::Memory => {
            splice!(module.defined_memory_datas, |b: &[u8]| decode_single_memory(b));
        }
        SectionKind::Global => {
            splice!(module.defined_globals, |b: &[u8]| decode_single_global(b));
        }
        SectionKind::Export => {
            splice!(module.exports, |b: &[u8]| decode_single_export(b));
        }
        SectionKind::Element => {
            splice!(module.elem_sec_datas, |b: &[u8]| decode_single_elem(b));
        }
        SectionKind::Data => {
            splice!(module.data_sec_datas, |b: &[u8]| decode_single_data(b));
        }
        SectionKind::Code => {
            let decoded: Vec<Func> = repl
                .iter()
                .map(|r| decode_single_func(&r.0))
                .collect::<Result<Vec<_>>>()?;
            code_new_funcs.extend(decoded.iter().cloned());
            if e > module.defined_funcs.len() || s > e {
                bail!("edit range {s}..{e} out of bounds (len {})", module.defined_funcs.len());
            }
            module.defined_funcs.splice(s..e, decoded);
        }
        SectionKind::Start => {
            let decoded: Vec<_> = repl
                .iter()
                .map(|r| decode_single_u32(&r.0))
                .collect::<Result<Vec<_>>>()?;
            match decoded.len() {
                0 => module.start_sec_data = None,
                1 => module.start_sec_data = Some(decoded[0]),
                n => bail!("start section cannot hold {n} definitions"),
            }
        }
        SectionKind::DataCount => {
            let decoded: Vec<_> = repl
                .iter()
                .map(|r| decode_single_u32(&r.0))
                .collect::<Result<Vec<_>>>()?;
            match decoded.len() {
                0 => module.data_count_sec_data = None,
                1 => module.data_count_sec_data = Some(decoded[0]),
                n => bail!("data count section cannot hold {n} definitions"),
            }
        }
        SectionKind::Custom => bail!("custom section edits are not supported"),
    }
    Ok(())
}

/// Mirrors `apply_mutation_and_encode_keep_snapshot`: apply mutations on a copy of the snapshot, write out the
/// wasm, and return the new snapshot after application.
pub fn apply_mutation_and_encode(
    snap: &Snapshot,
    batch: &[Mutation],
    out: &Path,
) -> Result<Snapshot> {
    let new_snap = apply_mutations(snap, batch)?;
    let bytes = new_snap.encode_to_bytes()?;
    std::fs::write(out, bytes).with_context(|| format!("write {}", out.display()))?;
    Ok(new_snap)
}

// The deferred-batch channel shell (Python `MultiPhaseMutationApplier`) was deleted: zero callers repo-wide
// and no real deferred semantics (R-2).

// ---------------------------------------------------------------------------
// Single-definition encode/decode helpers (for building RawDef; the encode of Python's per-section sub-decoders).
//---------------------------------------------------------------------------

fn uleb(mut v: u64, out: &mut Vec<u8>) {
    loop {
        let mut b = (v & 0x7f) as u8;
        v >>= 7;
        if v != 0 {
            b |= 0x80;
        }
        out.push(b);
        if v == 0 {
            break;
        }
    }
}

pub fn u32_def(v: u32) -> RawDef {
    let mut b = Vec::new();
    uleb(v as u64, &mut b);
    RawDef(b)
}

/// Extract single-definition bytes from the full bytes of a "single-entry section": drop the section id, length, and vector count.
fn strip_single_entry_section(section_bytes: &[u8]) -> Result<&[u8]> {
    let mut pos = 1; // skip the section id (1 byte)
    let mut shift = 0u32;
    let mut len: u64 = 0;
    loop {
        let b = *section_bytes.get(pos).ok_or_else(|| anyhow!("section too short"))?;
        pos += 1;
        len |= ((b & 0x7f) as u64) << shift;
        if b & 0x80 == 0 {
            break;
        }
        shift += 7;
    }
    let content = &section_bytes[pos..pos + len as usize];
    // Drop the vector count (single entry = 0x01, 1 byte)
    if content.first() != Some(&1) {
        bail!("expected single-entry section, got count {:?}", content.first());
    }
    Ok(&content[1..])
}

fn section_id(kind: SectionKind) -> u8 {
    use SectionKind::*;
    match kind {
        Custom => 0,
        Type => 1,
        Import => 2,
        Function => 3,
        Table => 4,
        Memory => 5,
        Global => 6,
        Export => 7,
        Start => 8,
        Element => 9,
        Code => 10,
        Data => 11,
        DataCount => 12,
    }
}

/// Locates the full bytes of the given section in the module bytes (8-byte file header excluded).
/// Section-header traversal reuses the shared scan in snapshot.rs (R-14 unification).
fn find_section_bytes(module_bytes: &[u8], want: u8) -> Result<&[u8]> {
    for (id, sec) in crate::snapshot::scan_section_headers(module_bytes)? {
        if id == want {
            return Ok(&module_bytes[sec]);
        }
    }
    bail!("section id {want} not found")
}

/// Obtain single-definition bytes by "building a mini module containing only that definition, then encoding it whole"
/// (reusing every encoder from encode.rs, guaranteeing consistency with whole-module encoding).
/// Locates by section id (the mini module may carry a DataCount section; the target section cannot be assumed first).
fn encode_via_mini_module(section: SectionKind, mini: Module) -> Result<RawDef> {
    let bytes = encode_module(&mini)?;
    let sec = find_section_bytes(&bytes, section_id(section))?;
    strip_single_entry_section(sec).map(|b| RawDef(b.to_vec()))
}

pub fn type_def(ty: &FuncType) -> Result<RawDef> {
    encode_via_mini_module(SectionKind::Type,Module { types: vec![ty.clone()], ..Default::default() },
    )
}

pub fn import_def(imp: &Import) -> Result<RawDef> {
    encode_via_mini_module(SectionKind::Import,Module { imports: vec![imp.clone()], ..Default::default() },
    )
}

pub fn memory_def(m: &Memory) -> Result<RawDef> {
    encode_via_mini_module(SectionKind::Memory,Module { defined_memory_datas: vec![m.clone()], ..Default::default() },
    )
}

pub fn global_def(g: &Global) -> Result<RawDef> {
    encode_via_mini_module(SectionKind::Global,Module { defined_globals: vec![g.clone()], ..Default::default() },
    )
}

pub fn export_def(e: &Export) -> Result<RawDef> {
    encode_via_mini_module(SectionKind::Export,Module { exports: vec![e.clone()], ..Default::default() },
    )
}

pub fn elem_def(e: &ElemSeg) -> Result<RawDef> {
    encode_via_mini_module(SectionKind::Element,Module { elem_sec_datas: vec![e.clone()], ..Default::default() },
    )
}

pub fn data_def(d: &DataSeg) -> Result<RawDef> {
    encode_via_mini_module(SectionKind::Data,Module { data_sec_datas: vec![d.clone()], ..Default::default() },
    )
}

/// Whole-function re-encode path: the function body's (length prefix, local declarations, instructions, trailing end)
/// definition bytes. ty_idx is not part of the Code-section definition (the function section carries it).
pub fn func_def(f: &Func) -> Result<RawDef> {
    encode_via_mini_module(SectionKind::Code,Module { defined_funcs: vec![f.clone()], ..Default::default() },
    )
}

fn decode_single_u32(b: &[u8]) -> Result<u32> {
    let mut r = wasmparser::BinaryReader::new(b, 0);
    Ok(r.read_var_u32()?)
}

// Single-definition decode: prepend a single-entry count and reuse the decoders.

fn with_count_prefix(def: &[u8]) -> Vec<u8> {
    let mut v = vec![1u8];
    v.extend_from_slice(def);
    v
}

fn decode_single_type(b: &[u8]) -> Result<FuncType> {
    let content = with_count_prefix(b);
    let r = wasmparser::TypeSectionReader::new(wasmparser::BinaryReader::new(&content, 0)).unwrap();
    let rg = r.into_iter().next().ok_or_else(|| anyhow!("empty type def"))??;
    let sub = rg.types().next().ok_or_else(|| anyhow!("empty rec group"))?;
    if !sub.is_final || sub.supertype_idx.is_some() {
        bail!("unsupported non-final/subtype entry");
    }
    match &sub.composite_type.inner {
        wasmparser::CompositeInnerType::Func(ft) => Ok(FuncType {
            params: ft.params().to_vec(),
            results: ft.results().to_vec(),
        }),
        _ => bail!("non-func type def"),
    }
}

fn decode_single_import(b: &[u8]) -> Result<Import> {
    let content = with_count_prefix(b);
    let r = wasmparser::ImportSectionReader::new(wasmparser::BinaryReader::new(&content, 0)).unwrap();
    let imp = r.into_iter().next().ok_or_else(|| anyhow!("empty def"))??;
    decode::conv_import(imp)
}

fn decode_single_table(b: &[u8]) -> Result<Table> {
    let content = with_count_prefix(b);
    let r = wasmparser::TableSectionReader::new(wasmparser::BinaryReader::new(&content, 0)).unwrap();
    let t = r.into_iter().next().ok_or_else(|| anyhow!("empty def"))??;
    decode::conv_table(t)
}

fn decode_single_memory(b: &[u8]) -> Result<Memory> {
    let content = with_count_prefix(b);
    let r = wasmparser::MemorySectionReader::new(wasmparser::BinaryReader::new(&content, 0)).unwrap();
    let m = r.into_iter().next().ok_or_else(|| anyhow!("empty def"))??;
    Ok(Memory { ty: decode::conv_mem_type(m) })
}

fn decode_single_global(b: &[u8]) -> Result<Global> {
    let content = with_count_prefix(b);
    let r = wasmparser::GlobalSectionReader::new(wasmparser::BinaryReader::new(&content, 0)).unwrap();
    let g = r.into_iter().next().ok_or_else(|| anyhow!("empty def"))??;
    decode::conv_global(g)
}

fn decode_single_export(b: &[u8]) -> Result<Export> {
    let content = with_count_prefix(b);
    let r = wasmparser::ExportSectionReader::new(wasmparser::BinaryReader::new(&content, 0)).unwrap();
    let e = r.into_iter().next().ok_or_else(|| anyhow!("empty def"))??;
    decode::conv_export(e)
}

fn decode_single_elem(b: &[u8]) -> Result<ElemSeg> {
    let content = with_count_prefix(b);
    let r = wasmparser::ElementSectionReader::new(wasmparser::BinaryReader::new(&content, 0)).unwrap();
    let e = r.into_iter().next().ok_or_else(|| anyhow!("empty def"))??;
    decode::conv_element(e)
}

fn decode_single_data(b: &[u8]) -> Result<DataSeg> {
    let content = with_count_prefix(b);
    let r = wasmparser::DataSectionReader::new(wasmparser::BinaryReader::new(&content, 0)).unwrap();
    let d = r.into_iter().next().ok_or_else(|| anyhow!("empty def"))??;
    decode::conv_data(d)
}

fn decode_single_func(b: &[u8]) -> Result<Func> {
    let content = with_count_prefix(b);
    let r = wasmparser::CodeSectionReader::new(wasmparser::BinaryReader::new(&content, 0)).unwrap();
    let body = r.into_iter().next().ok_or_else(|| anyhow!("empty def"))??;
    decode::conv_func_body(body)
}

// ---------------------------------------------------------------------------
// Byte pass-through main path (O-6), following Python's "unmodified sections pass through, modified sections are located and replaced" idea;
// definition boundaries come from the snapshot section table (snapshot.rs), and the replacement content (RawDef) is already encoded bytes.
// ---------------------------------------------------------------------------

/// Mirrors Python `GeneralMutationApplier.apply_mutations_on_snapshot` +
/// `Encoder.encode` at the byte layer: interval replacement on the snapshot bytes, unmodified sections pass through verbatim,
/// and the output is returned as a new snapshot after full decode validation.
pub fn apply_mutations(snap: &Snapshot, batch: &[Mutation]) -> Result<Snapshot> {
    let bytes = snap.raw_bytes();

    // Group by section (Python sec_type2mutations).
    let mut by_section: BTreeMap<SectionKind, Vec<DefEdit>> = BTreeMap::new();
    for Mutation::Definitions { section, edits } in batch {
        by_section.entry(*section).or_default().extend(edits.iter().cloned());
    }
    if let Some(edits) = by_section.get(&SectionKind::Custom) {
        if !edits.is_empty() {
            anyhow::bail!("custom section edits are not supported ({} edits)", edits.len());
        }
    }

    // Pre-computation of the DataCount rule (see datacount_outcome below; the rule matches
    // apply_mutations_reference, including the Python _process_datacount_section semantics).
    let final_data_defs = splice_defs(
        bytes,
        snap.section_entry(SectionKind::Data).map(|e| &e.defs[..]).unwrap_or(&[]),
        &sort_and_clear(by_section.get(&SectionKind::Data).cloned().unwrap_or_default()),
        SectionKind::Data,
    )?
    .len();
    let mut has_ndc = false;
    for edit in sort_and_clear(by_section.get(&SectionKind::Code).cloned().unwrap_or_default()) {
        for repl in &edit.repl {
            if body_uses_data_count(&decode_single_func(&repl.0)?.insts) {
                has_ndc = true;
            }
        }
    }
    let explicit_dc: Option<u32> = {
        // Explicit DataCount edits (absent from the corpus; semantics copied from the reference path: the rule result overrides).
        let edits = sort_and_clear(by_section.get(&SectionKind::DataCount).cloned().unwrap_or_default());
        let defs = splice_defs(
            bytes,
            snap.section_entry(SectionKind::DataCount).map(|e| &e.defs[..]).unwrap_or(&[]),
            &edits,
            SectionKind::DataCount,
        )?;
        match defs.len() {
            0 => Some(u32::MAX), // sentinel for explicit deletion (see datacount_outcome)
            1 => Some(decode_single_u32(&defs[0])?),
            n => anyhow::bail!("data count section cannot hold {n} definitions"),
        }
    };

    // Assembly: header + sections in SECTION_ENCODE_ORDER order (unmodified pass through, modified are rebuilt).
    let mut out: Vec<u8> = Vec::with_capacity(bytes.len());
    out.extend_from_slice(&bytes[..8]);
    let mut new_sections: BTreeMap<SectionKind, crate::snapshot::SectionEntry> = BTreeMap::new();
    for kind in crate::snapshot::SECTION_ENCODE_ORDER {
        if kind == SectionKind::Custom && snap.section_entry(kind).is_some() {
            // Custom sections are not editable (intercepted above); pass through verbatim (P-15: the section table keeps only the last one).
            let entry = snap.section_entry(kind).unwrap();
            let sec_start = out.len();
            out.extend_from_slice(&bytes[entry.sec.clone()]);
            new_sections.insert(
                kind,
                crate::snapshot::SectionEntry {
                    sec: sec_start..out.len(),
                    defs: vec![],
                    counted: false,
                },
            );
            continue;
        }
        let edits = by_section
            .get(&kind)
            .map(|v| sort_and_clear(v.clone()))
            .unwrap_or_default();
        if kind == SectionKind::DataCount {
            if let Some(value) = datacount_outcome(snap, final_data_defs as u32, has_ndc, explicit_dc)? {
                write_section(&mut out, kind, &[Cow::Owned(u32_def(value).0)], false, &mut new_sections)?;
            } // None: the section disappears
            continue;
        }
        let Some(entry) = snap.section_entry(kind) else {
            if !edits.is_empty() {
                // Python semantics: editing a missing section = splicing onto an empty definition list (the applier works on
                // the parsed model's lists; the encoder emits a section only when non-empty). Instrumentation relies on this to append
                // definitions to modules with no imports/globals/memories. Only vector-counted sections are supported; Start/
                // DataCount/Custom still error out when missing.
                if matches!(
                    kind,
                    SectionKind::Start | SectionKind::DataCount | SectionKind::Custom
                ) {
                    anyhow::bail!("{kind:?} edits on a snapshot without that section");
                }
                let defs = splice_defs(bytes, &[], &edits, kind)?;
                write_section(&mut out, kind, &defs, true, &mut new_sections)?;
            }
            continue;
        };
        if edits.is_empty() {
            // Unmodified section: original bytes pass through; section-table ranges shift.
            let sec_start = out.len();
            out.extend_from_slice(&bytes[entry.sec.clone()]);
            let shift = sec_start as i64 - entry.sec.start as i64;
            let defs = entry
                .defs
                .iter()
                .map(|r| (r.start as i64 + shift) as usize..(r.end as i64 + shift) as usize)
                .collect();
            new_sections.insert(
                kind,
                crate::snapshot::SectionEntry { sec: sec_start..out.len(), defs, counted: entry.counted },
            );
        } else {
            let defs = splice_defs(bytes, &entry.defs, &edits, kind)?;
            write_section(&mut out, kind, &defs, entry.counted, &mut new_sections)?;
        }
    }

    Snapshot::from_bytes(out)
}

/// The DataCount section's final value (rule order and override relations aligned with the reference implementation).
/// Returning None means the section disappears. Explicit edits take effect only when no rule fired (sentinel u32::MAX = explicit deletion).
fn datacount_outcome(
    snap: &Snapshot,
    final_data_defs: u32,
    has_ndc: bool,
    explicit: Option<u32>,
) -> Result<Option<u32>> {
    if snap.section_entry(SectionKind::DataCount).is_some() {
        return Ok(if final_data_defs == 0 { None } else { Some(final_data_defs) });
    }
    if final_data_defs > 0 && has_ndc {
        return Ok(Some(final_data_defs));
    }
    Ok(match explicit {
        None => None,
        Some(u32::MAX) => None,
        Some(v) => Some(v),
    })
}

/// Interval replacement on a definition sequence: untouched definitions reference original byte slices; touched ones are replaced by repl bytes.
/// The input edits must already be sort_and_clear'ed (deduped, start descending).
fn splice_defs<'b>(
    bytes: &'b [u8],
    defs: &[std::ops::Range<usize>],
    edits: &[DefEdit],
    kind: SectionKind,
) -> Result<Vec<Cow<'b, [u8]>>> {
    let mut out: Vec<Cow<[u8]>> = defs.iter().map(|r| Cow::Borrowed(&bytes[r.clone()])).collect();
    for edit in edits {
        let (s, e) = (edit.range.start as usize, edit.range.end as usize);
        if e > out.len() || s > e {
            anyhow::bail!("{kind:?} edit range {}..{} out of bounds (len {})", s, e, out.len());
        }
        out.splice(s..e, edit.repl.iter().map(|r| Cow::Owned(r.0.clone())));
    }
    Ok(out)
}

/// Reassembles one section: section id + content length + (count) + per-definition bytes; zero definitions means the section disappears.
fn write_section<'b>(
    out: &mut Vec<u8>,
    kind: SectionKind,
    defs: &[Cow<'b, [u8]>],
    counted: bool,
    new_sections: &mut BTreeMap<SectionKind, crate::snapshot::SectionEntry>,
) -> Result<()> {
    if defs.is_empty() {
        return Ok(()); // the section disappears
    }
    let defs_total: usize = defs.iter().map(|d| d.len()).sum();
    // Writing a content-length placeholder first is impossible (the LEB width depends on the length) — the length is known, encode directly.
    let mut count_leb = Vec::new();
    if counted {
        uleb(defs.len() as u64, &mut count_leb);
    }
    let content_len = count_leb.len() + defs_total;
    let sec_start = out.len();
    out.push(section_id(kind));
    uleb(content_len as u64, out);
    out.extend_from_slice(&count_leb);
    let mut new_defs = Vec::with_capacity(defs.len());
    for d in defs {
        let s = out.len();
        out.extend_from_slice(d);
        new_defs.push(s..out.len());
    }
    new_sections.insert(
        kind,
        crate::snapshot::SectionEntry { sec: sec_start..out.len(), defs: new_defs, counted },
    );
    Ok(())
}
