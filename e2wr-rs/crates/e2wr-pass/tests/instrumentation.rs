//! M7 instrumentation probe synthetic unit tests and corpus validation.
//!
//! - Event header codec: pack/unpack round trip, invalid marker;
//! - dual parsers: normal events, 0xF0 mixed into program output, non-alignment, unknown ids, invalid type bits
//!   (the general one aborts / the executed-only one skips — the P-25 semantic difference);
//! - corpus end-to-end (expected values taken from actual runs of the Python baseline `instrumentation_driver.py`):
//!   event sequences and frequencies agree, the instrumented artifact decodes, the general parser yields the same results on the same trace;
//! - skip and error paths: a parameter-taking/unexported to_test → empty result; imported memory → error.

use std::collections::{BTreeSet, HashSet};
use std::path::{Path, PathBuf};

use e2wr_ir::ast::NodeLoc;
use e2wr_ir::Snapshot;
use e2wr_pass::instrumentation::*;

fn fixtures() -> PathBuf {
    PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("../../tests/fixtures")
}

fn temp_instr_path(tag: &str) -> PathBuf {
    std::env::temp_dir().join(format!("e2wr_instr_test_{tag}_{}.wasm", std::process::id()))
}

fn func_entry_descs(snap: &Snapshot) -> Vec<ProbeDesc> {
    (0..snap.module().defined_funcs.len() as u32)
        .map(|i| ProbeDesc { idx: i, loc: NodeLoc { func_idx: i, inst_idx: 0 } })
        .collect()
}

fn run_case(tag: &str, wasm: &Path, to_test: &str) -> Vec<ProbeEvent> {
    let snap = Snapshot::from_path(wasm).expect("decode");
    let mgr = InstrumentManager::new(&temp_instr_path(tag), to_test, false);
    let descs = func_entry_descs(&snap);
    let params = InstrumentParams { only_executed_probe: true, ..Default::default() };
    mgr.instrument_multiple_places_and_get_result(&snap, &descs, &params)
        .expect("instrument ok")
}

/// Same as run_case, but additionally validates that the instrumented artifact fully decodes (skip paths have no artifact and cannot use it).
fn run_case_with_output(tag: &str, wasm: &Path, to_test: &str) -> Vec<ProbeEvent> {
    let events = run_case(tag, wasm, to_test);
    let out = temp_instr_path(tag);
    let bytes = std::fs::read(&out).expect("instrumented file");
    Snapshot::from_bytes(bytes).expect("instrumented wasm decodes");
    events
}

// ---------------------------------------------------------------------------
// Event header codec.
// ---------------------------------------------------------------------------

#[test]
fn header_codec_roundtrip() {
    for (ty, idx) in [(0u32, 0u32), (3, 1), (0, (1 << 22) - 1), (3, 1 << 20)] {
        let packed = pack_probe_header(ty, idx);
        assert_eq!(packed & 0xFF, MARKER_BYTE as u32);
        let (ty2, idx2) = unpack_probe_header(packed).expect("valid header");
        assert_eq!((ty2, idx2), (ty, idx));
    }
    assert!(unpack_probe_header(0xEF).is_none());
    assert!(unpack_probe_header(0x1234).is_none());
}

#[test]
fn probe_type_decode_fidelity() {
    assert_eq!(decode_probe_type(0), Some(ProbeType::Stack));
    assert_eq!(decode_probe_type(3), Some(ProbeType::Executed));
    // LOCAL(1)/GLOBAL(2) were deleted: decoding them is invalid (Python raises ValueError).
    assert!(decode_probe_type(1).is_none());
    assert!(decode_probe_type(2).is_none());
    assert!(decode_probe_type(4).is_none());
}

// ---------------------------------------------------------------------------
// Dual parsers.
// ---------------------------------------------------------------------------

fn ev(bytes: &[u8]) -> Vec<u8> {
    bytes.to_vec()
}

fn known_set(idxs: &[u32]) -> HashSet<u32> {
    idxs.iter().copied().collect()
}

/// Builds an event word (marker + type=EXECUTED + idx).
fn event_word(idx: u32) -> [u8; 4] {
    pack_probe_header(encode_probe_type(ProbeType::Executed), idx).to_le_bytes()
}

fn event_word_typed(ty: u32, idx: u32) -> [u8; 4] {
    pack_probe_header(ty, idx).to_le_bytes()
}

#[test]
fn parse_executed_only_basic() {
    let mut trace = vec![];
    trace.extend_from_slice(&event_word(0));
    trace.extend_from_slice(b"hello");
    trace.extend_from_slice(&event_word(2));
    trace.extend_from_slice(&event_word(0));
    let known = known_set(&[0, 1, 2]);
    let out = parse_executed_only(&trace, &known);
    let idxs: Vec<u32> = out.iter().map(|e| e.probe_idx).collect();
    assert_eq!(idxs, vec![0, 2, 0]);
}

#[test]
fn parse_executed_only_ignores_bad_headers() {
    // A 0xF0 in the program output: unknown id, invalid type bits → always skipped.
    let mut trace = vec![];
    trace.extend_from_slice(&event_word(1));
    trace.extend_from_slice(&event_word_typed(1, 1)); // LOCAL value → invalid type
    trace.extend_from_slice(&event_word_typed(0, 1)); // STACK type
    trace.extend_from_slice(&event_word_typed(3, 9)); // unknown id
    trace.push(0xF0); // fewer than 4 trailing bytes
    trace.extend_from_slice(&event_word(2));
    let known = known_set(&[1, 2]);
    let idxs: Vec<u32> = parse_executed_only(&trace, &known).iter().map(|e| e.probe_idx).collect();
    assert_eq!(idxs, vec![1, 2]);
}

#[test]
fn parse_executed_only_unaligned_and_split() {
    // Events need neither 4-byte alignment (odd offsets) nor contiguity.
    let mut trace = ev(b"ab");
    trace.extend_from_slice(&event_word(0));
    trace.push(b'c');
    trace.extend_from_slice(&event_word(0));
    let known = known_set(&[0]);
    let idxs: Vec<u32> = parse_executed_only(&trace, &known).iter().map(|e| e.probe_idx).collect();
    assert_eq!(idxs, vec![0, 0]);
}

#[test]
fn parse_generic_aborts_on_stack_header() {
    // P-25: the general parser aborts and returns partial results on a "known-id non-EXECUTED header".
    let mut trace = vec![];
    trace.extend_from_slice(&event_word(1));
    trace.extend_from_slice(&event_word_typed(0, 1)); // STACK + known id → abort
    trace.extend_from_slice(&event_word(2));
    let known = known_set(&[1, 2]);
    let idxs: Vec<u32> = parse_generic(&trace, &known).iter().map(|e| e.probe_idx).collect();
    assert_eq!(idxs, vec![1]);
}

#[test]
fn parse_generic_aborts_on_invalid_type_even_if_unknown_idx() {
    // Python semantics: an undecodable type value raises before the id check → partial results.
    let mut trace = vec![];
    trace.extend_from_slice(&event_word(1));
    trace.extend_from_slice(&event_word_typed(1, 99)); // LOCAL value + unknown id
    trace.extend_from_slice(&event_word(2));
    let known = known_set(&[1, 2]);
    let idxs: Vec<u32> = parse_generic(&trace, &known).iter().map(|e| e.probe_idx).collect();
    assert_eq!(idxs, vec![1]);
}

#[test]
fn parse_generic_skips_unknown_idx() {
    let mut trace = vec![];
    trace.extend_from_slice(&event_word(1));
    trace.extend_from_slice(&event_word_typed(3, 77)); // unknown id → skip and continue
    trace.extend_from_slice(&event_word(2));
    let known = known_set(&[1, 2]);
    let idxs: Vec<u32> = parse_generic(&trace, &known).iter().map(|e| e.probe_idx).collect();
    assert_eq!(idxs, vec![1, 2]);
}

#[test]
fn parse_empty_and_short_traces() {
    assert!(parse_executed_only(&[], &known_set(&[0])).is_empty());
    assert!(parse_generic(&[], &known_set(&[0])).is_empty());
    assert!(parse_executed_only(&[0xF0, 0x00], &known_set(&[0])).is_empty());
    assert!(parse_generic(&[0xF0, 0x00], &known_set(&[0])).is_empty());
}

// ---------------------------------------------------------------------------
// Corpus end-to-end (expected values = actual Python-baseline results).
// ---------------------------------------------------------------------------

fn idxs(events: &[ProbeEvent]) -> Vec<u32> {
    events.iter().map(|e| e.probe_idx).collect()
}

#[test]
fn fixture_probe_basic_matches_python() {
    let events = run_case_with_output("basic", &fixtures().join("probe_basic.wasm"), "_start");
    assert_eq!(idxs(&events), vec![6, 3, 0, 1, 2, 3, 0, 1, 2, 1, 2]);
}

#[test]
fn fixture_probe_basic_freq_matches_python() {
    let snap = Snapshot::from_path(&fixtures().join("probe_basic.wasm")).unwrap();
    let mgr = InstrumentManager::new(&temp_instr_path("basicfreq"), "_start", false);
    let locs: BTreeSet<NodeLoc> = (0..7u32)
        .map(|i| NodeLoc { func_idx: i, inst_idx: 0 })
        .collect();
    let freq = mgr.instrument_and_get_exec_freq(&snap, &locs).unwrap();
    // Python baseline: {0:2, 1:3, 2:3, 3:2, 4:0, 5:0, 6:1}.
    let want = [(0u32, 2u32), (1, 3), (2, 3), (3, 2), (4, 0), (5, 0), (6, 1)];
    assert_eq!(freq.len(), want.len());
    for (f, n) in want {
        assert_eq!(freq[&NodeLoc { func_idx: f, inst_idx: 0 }], n, "func {f}");
    }
}

#[test]
fn fixture_probe_fdimport_matches_python() {
    let events = run_case_with_output("fdimport", &fixtures().join("probe_fdimport.wasm"), "_start");
    assert_eq!(idxs(&events), vec![2, 0, 1]);
}

#[test]
fn fixture_probe_nomem_matches_python() {
    let events = run_case_with_output("nomem", &fixtures().join("probe_nomem.wasm"), "_start");
    assert_eq!(idxs(&events), vec![2, 0, 0, 1]);
}

#[test]
fn fixture_generic_parser_same_events() {
    // The callsite path uses the general parser: the same batch of events (identical to the executed-only one absent program-output false matches).
    let snap = Snapshot::from_path(&fixtures().join("probe_basic.wasm")).unwrap();
    let mgr = InstrumentManager::new(&temp_instr_path("generic"), "_start", false);
    let descs = func_entry_descs(&snap);
    let params = InstrumentParams::default(); // only_executed_probe = false
    let events = mgr
        .instrument_multiple_places_and_get_result(&snap, &descs, &params)
        .unwrap();
    assert_eq!(idxs(&events), vec![6, 3, 0, 1, 2, 3, 0, 1, 2, 1, 2]);
}

#[test]
fn fixture_max_output_time_none_path() {
    // The old VPUtil path's max_output_time=None: no counter guard, direct output.
    let snap = Snapshot::from_path(&fixtures().join("probe_basic.wasm")).unwrap();
    let mgr = InstrumentManager::new(&temp_instr_path("nolimit"), "_start", false);
    let descs = func_entry_descs(&snap);
    let params = InstrumentParams { max_output_time: None, ..Default::default() };
    let events = mgr
        .instrument_multiple_places_and_get_result(&snap, &descs, &params)
        .unwrap();
    assert_eq!(idxs(&events), vec![6, 3, 0, 1, 2, 3, 0, 1, 2, 1, 2]);
}

#[test]
fn fixture_skip_when_to_test_requires_params() {
    let events = run_case("param", &fixtures().join("probe_paramstart.wasm"), "_start");
    assert!(events.is_empty());
}

#[test]
fn fixture_skip_when_to_test_not_exported() {
    let events = run_case("notest", &fixtures().join("probe_notest.wasm"), "_start");
    assert!(events.is_empty());
}

#[test]
fn fixture_imported_memory_errors() {
    let snap = Snapshot::from_path(&fixtures().join("probe_impmem.wasm")).unwrap();
    let mgr = InstrumentManager::new(&temp_instr_path("impmem"), "_start", false);
    let descs = func_entry_descs(&snap);
    let err = mgr
        .instrument_multiple_places_and_get_result(&snap, &descs, &InstrumentParams::default())
        .unwrap_err();
    assert!(err.to_string().contains("Imported memory is not supported"));
}

#[test]
fn fixture_instrumented_output_validates_via_wasm_tools() {
    let snap = Snapshot::from_path(&fixtures().join("probe_basic.wasm")).unwrap();
    let out = temp_instr_path("wtval");
    let mgr = InstrumentManager::new(&out, "_start", false);
    let descs = func_entry_descs(&snap);
    let _ = mgr
        .instrument_multiple_places_and_get_result(&snap, &descs, &InstrumentParams::default())
        .unwrap();
    let status = std::process::Command::new(unwrap_tools())
        .arg("validate")
        .arg(&out)
        .status()
        .expect("spawn wasm-tools");
    assert!(status.success(), "wasm-tools validate failed");
}

fn unwrap_tools() -> String {
std::env::var("WASM_TOOLS_PATH").unwrap_or_else(|_| "wasm-tools".to_string())
}
