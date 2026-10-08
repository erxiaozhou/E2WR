//! M2 corpus regression: tt/ (139 wasm) + benchmark RQ12 (75) + RQ3 (29):
//! decode→encode→wasm-tools validate passes; encoding is idempotent (second-encode bytes identical).

use std::path::PathBuf;
use std::process::Command;

use e2wr_ir::decode::decode_bytes;
use e2wr_ir::encode::encode_module;
use e2wr_ir::snapshot::Snapshot;

fn wasm_tools() -> PathBuf {
    std::env::var("E2WR_WASM_TOOLS")
        .map(PathBuf::from)
        .unwrap_or_else(|_| PathBuf::from("wasm-tools"))
}

fn validate(path: &std::path::Path) -> bool {
    Command::new(wasm_tools())
        .arg("validate")
        .arg(path)
        .output()
        .map(|o| o.status.success())
        .unwrap_or(false)
}

fn corpus() -> Vec<PathBuf> {
    let mut files = vec![];
    let tt_pat = format!("{}/CP9201/CP9201_MAIN/tt/*.wasm", std::env::var("HOME").unwrap_or_default());
    let bench_root = concat!(env!("CARGO_MANIFEST_DIR"), "/../../../benchmark");
    for pat in [
        tt_pat.as_str(),
        &format!("{bench_root}/RQ12/*.wasm"),
        &format!("{bench_root}/RQ3/*.wasm"),
    ] {
        files.extend(glob::glob(pat).expect("valid pattern").filter_map(Result::ok));
    }
    assert!(!files.is_empty(), "corpus not found");
    files
}

fn temp_out(name: &str) -> PathBuf {
    let dir = std::env::temp_dir().join(format!("e2wr-ir-roundtrip-{}", std::process::id()));
    std::fs::create_dir_all(&dir).unwrap();
    dir.join(name)
}

#[test]
fn corpus_roundtrip_decode_encode_validate() {
    let files = corpus();
    let mut skipped_invalid_source = 0usize;
    let mut ok = 0usize;
    for f in &files {
        let bytes = std::fs::read(f).unwrap();
        // Skip sources that themselves fail validate (historical artifacts); only valid sources must round-trip.
        let src_valid = {
            let tmp = temp_out("src.wasm");
            std::fs::write(&tmp, &bytes).unwrap();
            validate(&tmp)
        };
        let module = match decode_bytes(&bytes) {
            Ok(m) => m,
            Err(e) => {
                panic!("decode failed for {}: {e:#}", f.display());
            }
        };
        let encoded = encode_module(&module).expect("encode");
        // Encoding idempotence
        let module2 = decode_bytes(&encoded).expect("re-decode");
        let encoded2 = encode_module(&module2).expect("re-encode");
        assert_eq!(
            encoded, encoded2,
            "encode not idempotent for {}",
            f.display()
        );
        let out = temp_out("rt.wasm");
        std::fs::write(&out, &encoded).unwrap();
        if !src_valid {
            skipped_invalid_source += 1;
            continue;
        }
        assert!(
            validate(&out),
            "roundtrip invalid for {}: stderr:\n{}",
            f.display(),
            String::from_utf8_lossy(
                &Command::new(wasm_tools())
                    .arg("validate")
                    .arg(&out)
                    .output()
                    .unwrap()
                    .stderr
            )
        );
        ok += 1;
    }
    eprintln!(
        "corpus roundtrip: {} ok, {} skipped (invalid source), {} total",
        ok,
        skipped_invalid_source,
        files.len()
    );
}

#[test]
fn snapshot_roundtrip_bytes_equal_encode_module() {
    // The snapshot channel (from_path → encode_to_bytes) matches encode_module output (same path originally).
    let files = corpus();
    let f = &files[0];
    let snap = Snapshot::from_path(f).unwrap();
    let a = snap.encode_to_bytes().unwrap();
    let b = encode_module(snap.module()).unwrap();
    assert_eq!(a, b);
}

#[test]
fn snapshot_full_copy_semantics() {
    // The old P-6 calibration point (Python copy() not copying mutable_sections): that field was
    // removed with the Z-1 dead-placeholder sweep and copy semantics converged to a plain clone.
    // Kept here as a behavior guard of full_copy: copied bytes identical.
    let files = corpus();
    let snap = Snapshot::from_path(&files[0]).unwrap();
    let copy = snap.full_copy();
    assert_eq!(
        snap.encode_to_bytes().unwrap(),
        copy.encode_to_bytes().unwrap(),
        "full_copy yields identical bytes"
    );
}

#[test]
fn snapshot_drops_empty_vec_sections() {
    // Equivalence fix (2026-09-30): input-borne empty vector sections (zero count prefix, e.g. an empty
    // import section `02 01 00` or an empty table section `04 01 00`) are stripped at snapshot construction —
    // Python Encoder.encode/encode_without_mutation emits no section at all for vector sections with empty
    // definition lists (root cause of +3B on 5 RQ12 batch-testing cases).
    let mk = |extra: &[u8]| -> Vec<u8> {
        let mut wasm: Vec<u8> = vec![0x00, 0x61, 0x73, 0x6d, 0x01, 0x00, 0x00, 0x00];
        // type section: 1 × () -> ()
        wasm.extend_from_slice(&[0x01, 0x04, 0x01, 0x60, 0x00, 0x00]);
        wasm.extend_from_slice(extra);
        // function section: 1 function, type 0; code section: empty body
        wasm.extend_from_slice(&[0x03, 0x02, 0x01, 0x00]);
        wasm.extend_from_slice(&[0x0a, 0x04, 0x01, 0x02, 0x00, 0x0b]);
        wasm
    };
    for (name, extra) in [("import", [0x02u8, 0x01, 0x00]), ("table", [0x04u8, 0x01, 0x00])] {
        let wasm = mk(&extra);
        let snap = Snapshot::from_bytes(wasm.clone()).expect("snapshot with empty section");
        let out = snap.encode_to_bytes().expect("encode");
        assert_eq!(out.len(), wasm.len() - 3, "empty {name} section must be dropped");
        assert_eq!(out, mk(&[]), "normalized bytes = module without the empty {name} section");
    }
    // Inputs without empty sections are unaffected (bytes verbatim).
    let plain = mk(&[]);
    let snap = Snapshot::from_bytes(plain.clone()).unwrap();
    assert_eq!(snap.encode_to_bytes().unwrap(), plain);
}
