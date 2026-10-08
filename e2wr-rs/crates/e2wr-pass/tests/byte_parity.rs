//! O-6 dual-path comparison (production path): on corpus files, detect unused definitions → remap_all produces mutations
//! → applied via the byte pass-through (apply_mutations) and the reference baseline (apply_mutations_reference)
//! respectively; the artifacts must be byte-identical.

use std::collections::BTreeSet;
use std::path::{Path, PathBuf};

use e2wr_ir::mutation::{apply_mutations, apply_mutations_reference};
use e2wr_ir::Snapshot;
use e2wr_pass::detect;
use e2wr_pass::remap::{remap_all, DeleteSet};

#[test]
fn product_path_parity_corpus() {
    let tt_pat = format!("{}/CP9201/CP9201_MAIN/tt/*.wasm", std::env::var("HOME").unwrap_or_default());
    let mut files: Vec<PathBuf> = glob::glob(&tt_pat)
        .unwrap()
        .filter_map(Result::ok)
        .filter(|f| f.metadata().map(|m| m.len() < 60_000).unwrap_or(false))
        .collect();
    files.sort();
    let files: Vec<PathBuf> = files.into_iter().step_by(11).take(12).collect();
    assert!(files.len() >= 6);
    let mut compared = 0;
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
            exports: (0..module.exports.len() as u32).collect::<BTreeSet<u32>>(),
            start: module.start_sec_data.is_some(),
        };
        if del == DeleteSet::default() {
            continue;
        }
        let batch = remap_all(module, &del).unwrap_or_else(|e| panic!("{}: {e:#}", f.display()));
        let a = apply_mutations(&snap, &batch)
            .unwrap_or_else(|e| panic!("{} direct: {e:#}", f.display()));
        let b = apply_mutations_reference(&snap, &batch)
            .unwrap_or_else(|e| panic!("{} reference: {e:#}", f.display()));
        let (ba, bb) = (a.encode_to_bytes().unwrap(), b.encode_to_bytes().unwrap());
        // The known and only difference classes (one-to-one with Python's two encoding paths; see
        // the P-16 records): the byte pass-through (mutation channel) keeps the original file's
        // empty vector sections and existing DataCount; the reference baseline (clone then the parser2wasm rules)
        // drops empty sections and may revive DataCount with value 0. After stripping those sections the artifacts must be byte-identical.
        let (na, nb) = (strip_empty_sections(&ba), strip_empty_sections(&bb));
        assert_eq!(
            na, nb,
            "{}: product-path products differ beyond empty-section normalization ({} vs {} bytes)",
            f.display(),
            na.len(),
            nb.len()
        );
        // The pass-through artifact itself must be valid (full correctness under mutation-channel semantics).
        let out = std::env::temp_dir().join(format!("byte-parity-{}.wasm", std::process::id()));
        std::fs::write(&out, &ba).unwrap();
        assert!(
            std::process::Command::new(
                std::env::var("E2WR_WASM_TOOLS").unwrap_or_else(|_| "wasm-tools".into())
            )
            .arg("validate")
            .arg(&out)
            .output()
            .map(|o| o.status.success())
            .unwrap_or(false),
            "{}: direct-path product invalid",
            f.display()
        );
        compared += 1;
    }
    assert!(compared >= 5, "too few compared: {compared}");
}

/// Strips "empty vector sections (count 0)" and "DataCount sections with value 0": the two encoding paths' differences on these
/// sections stem from the pre-existing rule differences between Python's mutation channel and parser2wasm (same origin as P-16)
/// and are not defects of the byte pass-through implementation.
fn strip_empty_sections(bytes: &[u8]) -> Vec<u8> {
    let mut out = bytes[..8].to_vec();
    let mut pos = 8usize;
    while pos < bytes.len() {
        let sec_start = pos;
        let id = bytes[pos];
        pos += 1;
        let mut shift = 0u32;
        let mut len: u64 = 0;
        loop {
            let b = bytes[pos];
            pos += 1;
            len |= ((b & 0x7f) as u64) << shift;
            if b & 0x80 == 0 {
                break;
            }
            shift += 7;
        }
        let content_start = pos;
        let sec_end = content_start + len as usize;
        let is_empty_vec = matches!(id, 1..=11) && bytes.get(content_start) == Some(&0);
        let is_zero_datacount = id == 12 && bytes.get(content_start) == Some(&0);
        if !is_empty_vec && !is_zero_datacount {
            out.extend_from_slice(&bytes[sec_start..sec_end]);
        }
        pos = sec_end;
    }
    out
}

#[allow(dead_code)]
fn unused(_: &Path) {}
