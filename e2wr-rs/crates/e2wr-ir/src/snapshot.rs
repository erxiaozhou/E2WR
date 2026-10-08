//! Snapshot: the sole owner of one wasm state (M2.4; interface frozen per the design notes).
//!
//! Internal structure after the O-6 optimization (approved 2026-09-22): **the byte layer is the source of truth** — the snapshot holds the full
//! module bytes plus the section table (each section's byte range + per-definition byte ranges); unmodified sections pass through
//! verbatim on encoding; `Module` (the intermediate representation) is an on-demand decode cache. Mutation application (mutation.rs)
//! does interval replacement at the byte layer, with no intermediate-representation rebuild.
//!
//! Unexpected conditions always error out (no silent fallback):
//! - byte-layer construction (`from_bytes`) fully decodes to validate; invalid artifacts fail immediately instead of being
//!   swallowed later at oracle-verdict time.
//!
//! Public interface frozen for the reducer layer: `from_path`/`from_bytes`/`module`/`encode_to_*`/
//! `full_copy`.

use std::cell::OnceCell;
use std::collections::BTreeMap;
use std::ops::Range;
use std::path::Path;

use anyhow::{bail, Context, Result};

use crate::decode::decode_bytes;
use crate::encode::encode_module;
use crate::module::Module;

/// Section kinds (aligned with Python `WasmInfoCfg.SectionType`).
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, PartialOrd, Ord)]
pub enum SectionKind {
    Custom,
    Type,
    Import,
    Function,
    Table,
    Memory,
    Global,
    Export,
    Start,
    Element,
    Code,
    Data,
    DataCount,
}

/// Section encoding order (Python `util/prepare_template.py` `seq_encode_seq`; custom first).
pub const SECTION_ENCODE_ORDER: [SectionKind; 13] = [
    SectionKind::Custom,
    SectionKind::Type,
    SectionKind::Import,
    SectionKind::Function,
    SectionKind::Table,
    SectionKind::Memory,
    SectionKind::Global,
    SectionKind::Export,
    SectionKind::Start,
    SectionKind::Element,
    SectionKind::DataCount,
    SectionKind::Code,
    SectionKind::Data,
];

/// Locating one section in the snapshot byte layer: the section's full byte range + per-definition byte ranges.
/// Start/DataCount sections carry no vector count (counted = false; defs are exactly the content);
/// Custom sections get no definition table (they cannot be edited by definition-level mutations).
#[derive(Debug, Clone)]
pub(crate) struct SectionEntry {
    /// The section's full byte range (section id and length prefix included), relative to the snapshot bytes.
    pub(crate) sec: Range<usize>,
    /// Per-definition byte ranges (vector count prefix excluded).
    pub(crate) defs: Vec<Range<usize>>,
    /// Whether the section content carries a vector count prefix.
    pub(crate) counted: bool,
}

/// A wasm state snapshot.
#[derive(Debug)]
pub struct Snapshot {
    /// Full module bytes (8-byte file header included).
    bytes: Vec<u8>,
    sections: BTreeMap<SectionKind, SectionEntry>,
    /// Intermediate-representation cache (decoded on first `module()`). Dropped when cloning the snapshot (the byte layer is the source of truth).
    module: OnceCell<Module>,
}

impl Clone for Snapshot {
    fn clone(&self) -> Self {
        Snapshot {
            bytes: self.bytes.clone(),
            sections: self.sections.clone(),
            module: OnceCell::new(),
        }
    }
}

impl Snapshot {
    /// Mirrors `WMSnapshot.from_path`: load from a wasm file and fully decode to validate.
    pub fn from_path(path: &Path) -> Result<Snapshot> {
        let bytes = decode_path_read(path)?;
        Snapshot::from_bytes(bytes)
    }

    /// Construct from bytes: build the section table + fully decode to validate (invalid bytes fail immediately).
    pub fn from_bytes(bytes: Vec<u8>) -> Result<Snapshot> {
        if bytes.len() < 8 || bytes[..4] != [0x00, 0x61, 0x73, 0x6d] {
            bail!("not a wasm module (bad magic/header): {} bytes", bytes.len());
        }
        let (bytes, sections) = normalize_empty_vec_sections(bytes)?;
        let module = decode_bytes(&bytes).context("decode for snapshot verification")?;
        Ok(Snapshot {
            module: OnceCell::from(module),
            bytes,
            sections,
        })
    }

    /// Test constructor (R-15): the only production consumer is `apply_mutations_reference`
    /// (the reference path); everything else is tests; the normal production chain enters via `from_path`/
    /// `from_bytes`.
    pub fn from_module(module: Module) -> Snapshot {
        let bytes = encode_module(&module).expect("encode from module");
        let sections = build_section_table(&bytes).expect("table of encode output");
        Snapshot {
            module: OnceCell::from(module),
            bytes,
            sections,
        }
    }

    /// Read-only access to the module intermediate representation (the role of Python's snapshot.parser).
    /// A byte-layer snapshot has already been validated as decodable, so this cannot fail; if an internal invariant is broken,
    /// it aborts with context (a programming error; fail fast).
    pub fn module(&self) -> &Module {
        self.try_module().unwrap_or_else(|e| {
            panic!("snapshot module decode failed (internal invariant broken): {e:#}")
        })
    }

    /// Explicit-error version of `module()`.
    pub fn try_module(&self) -> Result<&Module> {
        if let Some(m) = self.module.get() {
            return Ok(m);
        }
        let m = decode_bytes(&self.bytes)?;
        let _ = self.module.set(m);
        Ok(self.module.get().unwrap())
    }

    /// Full module bytes (read-only).
    pub(crate) fn raw_bytes(&self) -> &[u8] {
        &self.bytes
    }

    pub(crate) fn section_entry(&self, kind: SectionKind) -> Option<&SectionEntry> {
        self.sections.get(&kind)
    }

    /// Full copy (unified semantics of the three Python copy flavors; the root of the Python `WMSnapshot.copy()`
    /// P-6 quirk, mutable_sections, was removed with the Z-1 dead-placeholder sweep;
    /// copy semantics converge to a plain clone — cloning drops the intermediate-representation cache; the byte layer is the source of truth).
    pub fn full_copy(&self) -> Snapshot {
        self.clone()
    }

    /// Encode to disk without mutations (mirrors `Encoder.encode_without_mutation`;
    /// byte-layer pass-through — unmodified content is the original bytes).
    pub fn encode_to_bytes(&self) -> Result<Vec<u8>> {
        Ok(self.bytes.clone())
    }

    /// Test-only convenience writer (R-15): production writes go through
    /// `apply_mutation_and_encode` (mutation.rs); only tests use this directly.
    pub fn encode_to_path(&self, out: &Path) -> Result<()> {
        let bytes = self.encode_to_bytes()?;
        Ok(std::fs::write(out, bytes)?)
    }
}

fn decode_path_read(path: &Path) -> Result<Vec<u8>> {
    std::fs::read(path).with_context(|| format!("read {}", path.display()))
}

/// Builds the section table; if a vector section has a zero count prefix (e.g. an input-borne empty import section `02 01 00`),
/// it is stripped from the byte stream first and the table is rebuilt.
///
/// Equivalence fix (2026-09-30, found by RQ12 batch testing): the Python
/// `Encoder.encode`/`encode_without_mutation` in `ParserModification.py` emits no section at all
/// when a vector section's definition list is empty
/// (vector sections = type/import/table/memory/global/export/element/code/data/function,
/// i.e. sections with `counted == true`), so an input-borne empty vector section is dropped
/// on the first Python re-encode, while the Rust snapshot byte pass-through kept it, causing a +3B size difference. Here we align
/// with Python semantics: empty vector sections are stripped at snapshot construction. Custom/Start/DataCount carry no vector
/// count and are not stripped (consistent with Python).
fn normalize_empty_vec_sections(
    bytes: Vec<u8>,
) -> Result<(Vec<u8>, BTreeMap<SectionKind, SectionEntry>)> {
    let sections = build_section_table(&bytes)?;
    let mut empty_secs: Vec<Range<usize>> = sections
        .values()
        .filter(|e| e.counted && e.defs.is_empty())
        .map(|e| e.sec.clone())
        .collect();
    if empty_secs.is_empty() {
        return Ok((bytes, sections));
    }
    // Section byte ranges never overlap; sort by start and concatenate the remaining bytes.
    empty_secs.sort_by_key(|r| r.start);
    let mut out = Vec::with_capacity(bytes.len());
    let mut prev = 0usize;
    for r in &empty_secs {
        out.extend_from_slice(&bytes[prev..r.start]);
        prev = r.end;
    }
    out.extend_from_slice(&bytes[prev..]);
    let sections = build_section_table(&out)?;
    Ok((out, sections))
}

// ---------------------------------------------------------------------------
// Section-table construction: one light scan establishing each section's and definition's byte ranges.
// Definition boundaries come from wasmparser's section readers (SectionLimited::into_iter_with_offsets,
// which yield absolute offsets in the module byte stream).
// ---------------------------------------------------------------------------

fn read_leb_u32(bytes: &[u8], pos: usize) -> Result<(u32, usize)> {
    let mut result: u64 = 0;
    let mut shift = 0u32;
    let mut i = 0usize;
    loop {
        let b = *bytes
            .get(pos + i)
            .with_context(|| format!("truncated LEB at {pos}"))?;
        i += 1;
        result |= ((b & 0x7f) as u64) << shift;
        if b & 0x80 == 0 {
            break;
        }
        shift += 7;
        if i > 5 {
            bail!("invalid LEB128 at {pos}");
        }
    }
    Ok((result as u32, i))
}

fn kind_from_id(id: u8) -> Option<SectionKind> {
    use SectionKind::*;
    Some(match id {
        0 => Custom,
        1 => Type,
        2 => Import,
        3 => Function,
        4 => Table,
        5 => Memory,
        6 => Global,
        7 => Export,
        8 => Start,
        9 => Element,
        10 => Code,
        11 => Data,
        12 => DataCount,
        _ => return None,
    })
}

/// Shared section-header scan (R-14 unification): skip the 8-byte file header and yield per section
/// (section id, the section's full byte range — id and length prefix included). Out-of-range/truncated/invalid LEB
/// always errors. The hand-written LEB loop of mutation.rs `find_section_bytes` was merged into this
/// (its inputs are encoder output, always valid LEB within 5 bytes; adopting this implementation's
/// 5-byte limit does not change behavior).
pub(crate) fn scan_section_headers(bytes: &[u8]) -> Result<Vec<(u8, Range<usize>)>> {
    let mut out = Vec::new();
    let mut pos = 8usize;
    while pos < bytes.len() {
        let sec_start = pos;
        let id = bytes[pos];
        pos += 1;
        let (len, len_width) = read_leb_u32(bytes, pos)?;
        pos += len_width;
        let content_start = pos;
        let sec_end = content_start
            .checked_add(len as usize)
            .context("section length overflow")?;
        if sec_end > bytes.len() {
            bail!("truncated section id {id}: claims {len} bytes at {content_start}");
        }
        out.push((id, sec_start..sec_end));
        pos = sec_end;
    }
    Ok(out)
}

/// For vector-counted sections, build per-definition boundaries via SectionLimited's offset iteration.
/// Definition i's range = its start .. the next definition's start (the last one extends to the section content end);
/// the first definition starts after the count prefix (SectionLimited consumes the count at construction).
fn def_ranges_via_limited<'a, T: wasmparser::FromReader<'a>>(
    bytes: &'a [u8],
    content: Range<usize>,
    kind: SectionKind,
) -> Result<Vec<Range<usize>>> {
    let reader = wasmparser::BinaryReader::new(&bytes[content.clone()], content.start);
    let limited = wasmparser::SectionLimited::<T>::new(reader)
        .with_context(|| format!("parse {kind:?} section header"))?;
    let count = limited.count() as usize;
    let mut starts: Vec<usize> = Vec::with_capacity(count);
    for entry in limited.into_iter_with_offsets() {
        let (pos, _item) = entry.with_context(|| format!("parse {kind:?} section definitions"))?;
        starts.push(pos);
    }
    if starts.len() != count {
        bail!(
            "{kind:?} section: count prefix says {count} definitions, parsed {}",
            starts.len()
        );
    }
    let mut ranges = Vec::with_capacity(count);
    for (i, start) in starts.iter().enumerate() {
        let end = if i + 1 < starts.len() { starts[i + 1] } else { content.end };
        if *start >= end {
            bail!("{kind:?} section: non-increasing definition boundary at {start}");
        }
        ranges.push(*start..end);
    }
    Ok(ranges)
}

fn build_section_table(bytes: &[u8]) -> Result<BTreeMap<SectionKind, SectionEntry>> {
    let mut map: BTreeMap<SectionKind, SectionEntry> = BTreeMap::new();
    for (id, sec) in scan_section_headers(bytes)? {
        let sec_start = sec.start;
        let sec_end = sec.end;
        // Section content range = drop the section id byte and the length prefix (the prefix width can be derived back).
        let len_width = {
            let (len, w) = read_leb_u32(bytes, sec_start + 1)?;
            let _ = len;
            w
        };
        let content = sec_start + 1 + len_width..sec_end;
        let Some(kind) = kind_from_id(id) else {
            bail!("unknown section id {id} at {sec_start}");
        };
        let entry = match kind {
            SectionKind::Custom => {
                // P-15: only the last of multiple custom sections survives (same or different names, later write wins).
                SectionEntry { sec: sec_start..sec_end, defs: vec![], counted: false }
            }
            SectionKind::Start | SectionKind::DataCount => {
                // No vector count: the section content itself is the single definition.
                if content.is_empty() {
                    bail!("{kind:?} section with empty content at {sec_start}");
                }
                SectionEntry { sec: sec_start..sec_end, defs: vec![content], counted: false }
            }
            SectionKind::Type => {
                let defs = def_ranges_via_limited::<wasmparser::RecGroup>(bytes, content.clone(), kind)?;
                // This intermediate representation's definition granularity is one function type; only implicit recursion groups
                // (i.e. plain funcType entries) are accepted. Explicit recursion groups or multi-type groups would misalign definition indices
                // with type indices — error out directly (fail fast; the corpus has no such form).
                for r in &defs {
                    // Explicit recursion groups start with 0x4E (implicit = bare funcType 0x60). The explicit form
                    // would misalign definition and type indices; error out directly (fail fast).
                    if bytes[r.start] == 0x4E {
                        bail!(
                            "explicit rec group at {} is not supported (definition/type index would diverge)",
                            r.start
                        );
                    }
                }
                SectionEntry { sec: sec_start..sec_end, defs, counted: true }
            }
            SectionKind::Import => SectionEntry {
                sec: sec_start..sec_end,
                defs: def_ranges_via_limited::<wasmparser::Import>(bytes, content.clone(), kind)?,
                counted: true,
            },
            SectionKind::Function => SectionEntry {
                sec: sec_start..sec_end,
                defs: def_ranges_via_limited::<u32>(bytes, content.clone(), kind)?,
                counted: true,
            },
            SectionKind::Table => SectionEntry {
                sec: sec_start..sec_end,
                defs: def_ranges_via_limited::<wasmparser::Table>(bytes, content.clone(), kind)?,
                counted: true,
            },
            SectionKind::Memory => SectionEntry {
                sec: sec_start..sec_end,
                defs: def_ranges_via_limited::<wasmparser::MemoryType>(bytes, content.clone(), kind)?,
                counted: true,
            },
            SectionKind::Global => SectionEntry {
                sec: sec_start..sec_end,
                defs: def_ranges_via_limited::<wasmparser::Global>(bytes, content.clone(), kind)?,
                counted: true,
            },
            SectionKind::Export => SectionEntry {
                sec: sec_start..sec_end,
                defs: def_ranges_via_limited::<wasmparser::Export>(bytes, content.clone(), kind)?,
                counted: true,
            },
            SectionKind::Element => SectionEntry {
                sec: sec_start..sec_end,
                defs: def_ranges_via_limited::<wasmparser::Element>(bytes, content.clone(), kind)?,
                counted: true,
            },
            SectionKind::Code => SectionEntry {
                sec: sec_start..sec_end,
                defs: def_ranges_via_limited::<wasmparser::FunctionBody>(
                    bytes, content.clone(), kind,
                )?,
                counted: true,
            },
            SectionKind::Data => SectionEntry {
                sec: sec_start..sec_end,
                defs: def_ranges_via_limited::<wasmparser::Data>(bytes, content.clone(), kind)?,
                counted: true,
            },
        };
        if map.insert(kind, entry).is_some() && kind != SectionKind::Custom {
            bail!("duplicate section {kind:?} at {sec_start}");
        }
    }
    Ok(map)
}
