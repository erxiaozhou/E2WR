//! M10-U4 probe-encode-oracle chain acceptance:
//! 1. synthetic invalid mutations are filtered by validation before the oracle (both check_invalid semantics);
//! 2. identity mutations on corpus functions + a fake oracle: accept (/bin/true) / reject (/bin/false)
//!    behave correctly, with zero state change on rejection;
//! 3. element deletion: on acceptance the artifact passes wasm-tools validate, module instruction count and AST length
//!    shrink in step; the accumulated mutation dict = one-shot equivalent mutations in baseline coordinates (double-probe
//!    and single-shot artifacts byte-identical);
//! 4. best_path commit.

use std::collections::BTreeMap;
use std::path::{Path, PathBuf};
use std::process::Command;

use e2wr_dd::oracle::Oracle;
use e2wr_ir::ast::Ast;
use e2wr_ir::encode::encode_module;
use e2wr_ir::module::{Func, FuncType, Module};
use e2wr_ir::snapshot::Snapshot;
use e2wr_ir::Inst;

use e2wr_pass::instseq::{
    build_one_node_list_reduction_ctx, get_raw_elems_from_env, snapshot_insts_of,
    NodeListTrialApplier,
};

fn wasm_tools() -> PathBuf {
    std::env::var("E2WR_WASM_TOOLS").unwrap_or_else(|_| "wasm-tools".into()).into()
}

fn validate(path: &Path) -> bool {
    Command::new(wasm_tools()).arg("validate").arg(path).output()
        .map(|o| o.status.success())
        .unwrap_or(false)
}

fn temp_dir(name: &str) -> PathBuf {
    let d = std::env::temp_dir().join(format!("e2wr-pass-u4-{}-{name}", std::process::id()));
    std::fs::remove_dir_all(&d).ok();
    std::fs::create_dir_all(&d).unwrap();
    d
}

fn corpus_files() -> Vec<PathBuf> {
    let mut files = vec![];
    let tt_pat = format!("{}/CP9201/CP9201_MAIN/tt/*.wasm", std::env::var("HOME").unwrap_or_default());
    let bench_root = concat!(env!("CARGO_MANIFEST_DIR"), "/../../../benchmark");
    for pat in [
        tt_pat.as_str(),
        &format!("{bench_root}/RQ12/*.wasm"),
    ] {
        files.extend(glob::glob(pat).expect("valid pattern").filter_map(Result::ok));
    }
    files
}

fn true_oracle() -> Oracle {
    Oracle::new("/bin/true")
}

fn false_oracle() -> Oracle {
    Oracle::new("/bin/false")
}

fn synth_snapshot() -> Snapshot {
    let module = Module {
        types: vec![FuncType { params: vec![], results: vec![] }],
        defined_func_ty_ids: vec![0],
        defined_funcs: vec![Func {
            ty_idx: 0,
            locals: vec![],
            insts: vec![Inst::I32Const { value: 1 }, Inst::Drop, Inst::End],
        }],
        ..Default::default()
    };
    let bytes = encode_module(&module).unwrap();
    Snapshot::from_bytes(bytes).unwrap()
}

#[test]
fn synthetic_invalid_filtered_before_oracle() {
    let dir = temp_dir("synth-invalid");
    let tmp = dir.join("tmp_used.wasm");
    let snap = synth_snapshot();
    let ast = Ast::from_module(snap.module()).unwrap();
    let root = ast.root_of_func(0);
    let ctx = build_one_node_list_reduction_ctx(snap.module(), &ast, root).unwrap();
    let elems = get_raw_elems_from_env(snap.module(), &ast, &ctx).unwrap();
    assert_eq!(elems.len(), 2, "body = [i32.const 1, drop]");

    // Deleting the drop → stack residue → invalid; the oracle is always-true, so it would be adopted if it got past validation.
    let mut m = BTreeMap::new();
    m.insert(1usize, vec![]);
    let mut applier =
        NodeListTrialApplier::new(root, snap.clone(), true_oracle(), tmp.clone(), false).unwrap();
    assert!(!applier.try_mutation(&elems, &m, None).unwrap());
    // check_invalid=Some(false): Python's raise semantics.
    assert!(applier.try_mutation(&elems, &m, Some(false)).is_err());

    // Identity mutation: valid + always-true oracle → adopted.
    let mut identity = BTreeMap::new();
    identity.insert(0usize, vec![elems[0].clone()]);
    let mut applier =
        NodeListTrialApplier::new(root, snap, true_oracle(), tmp.clone(), false).unwrap();
    assert!(applier.try_mutation(&elems, &identity, None).unwrap());
    assert!(validate(&tmp));
}

#[test]
fn corpus_identity_accept_and_reject() {
    let dir = temp_dir("identity");
    for f in corpus_files().iter().step_by(11) {
        let snap = Snapshot::from_path(f).unwrap();
        let module = snap.module();
        if module.defined_funcs.is_empty() {
            continue;
        }
        let ast = Ast::from_module(module).unwrap();
        let root = ast.root_of_func(0);
        let ctx = build_one_node_list_reduction_ctx(module, &ast, root).unwrap();
        let elems = get_raw_elems_from_env(module, &ast, &ctx).unwrap();
        if elems.len() < 2 {
            continue;
        }
        let mid = elems.len() / 2;
        let mut identity = BTreeMap::new();
        identity.insert(mid, vec![elems[mid].clone()]);
        let base_bytes = snap.encode_to_bytes().unwrap();
        let orig_nodes: Vec<_> = ast.sub_nodes(root).to_vec();

        // Always-true oracle: adopted; list length unchanged.
        let tmp_accept = dir.join(format!("accept-{}.wasm", f.file_name().unwrap().to_string_lossy()));
        let mut applier = NodeListTrialApplier::new(
            root, snap.clone(), true_oracle(), tmp_accept.clone(), false).unwrap();
        assert!(
            applier.try_mutation(&elems, &identity, None).unwrap(),
            "{}: identity mutation should be accepted by /bin/true",
            f.display()
        );
        assert!(validate(&tmp_accept), "{}: identity product invalid", f.display());
        let (final_snap, final_ast) = applier.into_state();
        let new_len = final_ast.get_length(root);
        let old_len = ast.get_length(root);
        assert_eq!(new_len, old_len, "{}: identity changed list length", f.display());
        let final_insts = snapshot_insts_of(&final_snap, 0).unwrap();
        let base_insts = snapshot_insts_of(&snap, 0).unwrap();
        assert_eq!(final_insts.len(), base_insts.len());

        // Always-false oracle: rejected; snapshot and AST unchanged.
        let tmp_reject = dir.join("reject.wasm");
        let mut applier = NodeListTrialApplier::new(
            root, snap.clone(), false_oracle(), tmp_reject, false).unwrap();
        assert!(!applier.try_mutation(&elems, &identity, None).unwrap());
        let (final_snap, final_ast) = applier.into_state();
        assert_eq!(final_snap.encode_to_bytes().unwrap(), base_bytes);
        assert_eq!(final_ast.sub_nodes(root), orig_nodes);
    }
}

#[test]
fn corpus_deletion_cumulative_equals_oneshot() {
    let dir = temp_dir("cumulative");
    let mut cases_checked = 0usize;
    for f in corpus_files().iter().step_by(13) {
        let snap = Snapshot::from_path(f).unwrap();
        let module = snap.module();
        if module.defined_funcs.is_empty() {
            continue;
        }
        let ast = Ast::from_module(module).unwrap();
        let root = ast.root_of_func(0);
        let ctx = build_one_node_list_reduction_ctx(module, &ast, root).unwrap();
        let elems = get_raw_elems_from_env(module, &ast, &ctx).unwrap();
        if elems.len() < 4 {
            continue;
        }
        // Find a single-element deletion accepted by (the always-true oracle + validation).
        let mut first_ok: Option<usize> = None;
        for idx in 1..elems.len().saturating_sub(1) {
            let mut m = BTreeMap::new();
            m.insert(idx, vec![]);
            let tmp = dir.join("scan.wasm");
            let mut applier = NodeListTrialApplier::new(
                root, snap.clone(), true_oracle(), tmp.clone(), false).unwrap();
            if applier.try_mutation(&elems, &m, None).unwrap() {
                assert!(validate(&tmp), "{}: accepted deletion product invalid", f.display());
                let (fs, fa) = applier.into_state();
                let expect_len = elems
                    .iter()
                    .enumerate()
                    .map(|(i, e)| if i == idx { 0 } else { e.get_length(&fa) as u64 })
                    .sum::<u64>();
                assert_eq!(
                    fa.get_length(root) as u64, expect_len,
                    "{}: AST length mismatch after deleting elem {idx}",
                    f.display()
                );
                let insts = snapshot_insts_of(&fs, 0).unwrap();
                let base_len =
                    snap.module().defined_funcs[0].insts.len() as u64;
                assert_eq!(
                    insts.len() as u64,
                    base_len - elems[idx].get_length(&fa) as u64,
                    "{}: module inst count mismatch",
                    f.display()
                );
                first_ok = Some(idx);
                break;
            }
        }
        let Some(a) = first_ok else { continue };
        // A second index (different from a) for the accumulated vs one-shot comparison.
        let b = (a + 2).min(elems.len() - 1);
        if b == a {
            continue;
        }

        let tmp_seq = dir.join("seq.wasm");
        let mut seq = NodeListTrialApplier::new(
            root, snap.clone(), true_oracle(), tmp_seq.clone(), false).unwrap();
        let mut m1 = BTreeMap::new();
        m1.insert(a, vec![]);
        let r1 = seq.try_mutation(&elems, &m1, None).unwrap();
        let mut m2 = BTreeMap::new();
        m2.insert(a, vec![]);
        m2.insert(b, vec![]);
        let r2 = seq.try_mutation(&elems, &m2, None).unwrap();

        let tmp_one = dir.join("one.wasm");
        let mut one = NodeListTrialApplier::new(
            root, snap.clone(), true_oracle(), tmp_one.clone(), false).unwrap();
        let r3 = one.try_mutation(&elems, &m2, None).unwrap();

        assert_eq!(r1, true, "{}: first deletion previously accepted", f.display());
        assert_eq!(
            r2, r3,
            "{}: cumulative-second-trial vs one-shot divergence (a={a} b={b})",
            f.display()
        );
        if r2 {
            assert_eq!(
                std::fs::read(&tmp_seq).unwrap(),
                std::fs::read(&tmp_one).unwrap(),
                "{}: cumulative product bytes differ from one-shot",
                f.display()
            );
        }
        cases_checked += 1;
        if cases_checked >= 6 {
            break;
        }
    }
    assert!(cases_checked >= 1, "no corpus case exercised cumulative semantics");
}

#[test]
fn best_path_commit_on_success() {
    let dir = temp_dir("best-path");
    let tmp = dir.join("tmp_used.wasm");
    let best = dir.join("best.wasm");
    let snap = synth_snapshot();
    let ast = Ast::from_module(snap.module()).unwrap();
    let root = ast.root_of_func(0);
    let ctx = build_one_node_list_reduction_ctx(snap.module(), &ast, root).unwrap();
    let elems = get_raw_elems_from_env(snap.module(), &ast, &ctx).unwrap();

    let mut identity = BTreeMap::new();
    identity.insert(0usize, vec![elems[0].clone()]);
    let mut applier =
        NodeListTrialApplier::new(root, snap, true_oracle(), tmp.clone(), false).unwrap();
    applier.set_best_path(best.clone());
    assert!(applier.try_mutation(&elems, &identity, None).unwrap());
    assert!(best.exists());
    assert_eq!(std::fs::read(&best).unwrap(), std::fs::read(&tmp).unwrap());
}
