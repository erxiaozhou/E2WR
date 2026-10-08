//! UUR main-loop tests: convergence assertions on synthetic modules + a semantic fake oracle, always-true/always-false oracles,
//! timeout behavior, corpus smoke test.

#![allow(clippy::field_reassign_with_default)]

use std::path::{Path, PathBuf};
use std::process::Command;

use e2wr_dd::oracle::Oracle;
use e2wr_ir::module::*;
use wasmparser::ValType;
use e2wr_ir::{Inst, Snapshot};
use e2wr_pass::uur::UurPass;

fn ty(params: &[ValType], results: &[ValType]) -> FuncType {
    FuncType { params: params.to_vec(), results: results.to_vec() }
}

fn func(ty_idx: u32, insts: Vec<Inst>) -> Func {
    let mut insts = insts;
    insts.push(Inst::End);
    Func { ty_idx, locals: vec![], insts }
}

fn wasm_tools() -> PathBuf {
    std::env::var("E2WR_WASM_TOOLS")
        .map(PathBuf::from)
        .unwrap_or_else(|_| PathBuf::from("wasm-tools"))
}

fn validate(path: &Path) -> bool {
    Command::new(wasm_tools()).arg("validate").arg(path).output().map(|o| o.status.success()).unwrap_or(false)
}

fn temp_dir(name: &str) -> PathBuf {
    let d = std::env::temp_dir().join(format!("e2wr-pass-uur-{}-{name}", std::process::id()));
    std::fs::remove_dir_all(&d).ok();
    d
}

/// Three-function module: f0 called by the exported f2 (must keep); f1 unreferenced (should delete).
/// Function types: f0/f1 share t0; f2 uses t1 (t1 still used by f2 after f1's deletion).
fn module_with_unused_func() -> Module {
    let mut m = Module::default();
    m.types = vec![ty(&[], &[]), ty(&[ValType::I32], &[ValType::I32])];
    m.defined_func_ty_ids = vec![0, 0, 1];
    m.defined_funcs = vec![
        func(0, vec![Inst::Nop]),
        func(0, vec![Inst::Nop]), // f1: unreferenced
        func(1, vec![Inst::I32Const { value: 7 }, Inst::Call { function_index: 0 }]),
    ];
    m.exports = vec![Export { name: "must_keep".into(), desc: ExportDesc::Func(2) }];
    m
}

fn write_module(m: &Module, path: &Path) {
    let snap = Snapshot::from_module(m.clone());
    snap.encode_to_path(path).unwrap();
}

/// Semantic oracle: the wasm's wat text must contain "must_keep" (an export name).
fn must_keep_oracle_sh() -> PathBuf {
    let p = temp_dir("oracle");
    std::fs::create_dir_all(&p).unwrap();
    let sh = p.join("must_keep.sh");
    std::fs::write(
        &sh,
        format!("#!/bin/sh\n{} print \"$1\" | grep -q must_keep\n", wasm_tools().display()),
    )
    .unwrap();
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        std::fs::set_permissions(&sh, std::fs::Permissions::from_mode(0o755)).unwrap();
    }
    sh
}

#[test]
fn uur_removes_unused_and_keeps_exported() {
    let dir = temp_dir("sem");
    std::fs::create_dir_all(&dir).unwrap();
    let input = dir.join("in.wasm");
    let output = dir.join("out.wasm");
    write_module(&module_with_unused_func(), &input);

    let oracle = Oracle::new(must_keep_oracle_sh().to_str().unwrap());
    let pass = UurPass::new(&oracle, &dir.join("work"), false);
    let r = pass.reduce(&input, &output, None).unwrap();

    assert_eq!(r.exec_status, e2wr_pass::common::ExecStatus::Success);
    assert!(r.reduced_size_num.unwrap() > 0);
    // Artifact valid, f1 deleted (function count 3 → 2), exports still point at valid functions.
    assert!(validate(&output), "output must be valid");
    let out = Snapshot::from_path(&output).unwrap();
    assert_eq!(out.module().defined_funcs.len(), 2);
    assert_eq!(out.module().exports.len(), 1);
    assert!(matches!(out.module().exports[0].desc, ExportDesc::Func(_)));
    // The oracle still passes on the final artifact.
    assert!(oracle.check(&output).unwrap());
}

/// Always-false oracle: the first round's ProbDD fails wholesale → zero deletion → input copied verbatim, ExecFailed.
#[test]
fn uur_all_fail_copies_input() {
    let dir = temp_dir("allfail");
    std::fs::create_dir_all(&dir).unwrap();
    let input = dir.join("in.wasm");
    let output = dir.join("out.wasm");
    write_module(&module_with_unused_func(), &input);

    let oracle = Oracle::new("/bin/false");
    let pass = UurPass::new(&oracle, &dir.join("work"), false);
    let r = pass.reduce(&input, &output, None).unwrap();
    assert_eq!(r.exec_status, e2wr_pass::common::ExecStatus::ExecFailed);
    assert_eq!(r.reduced_size_num, Some(0));
    let (a, b) = (std::fs::read(&input).unwrap(), std::fs::read(&output).unwrap());
    assert_eq!(a, b);
}

/// Timeout 0: the verdict function always fails → zero deletion → ExecFailed with is_partial_by_timeout.
#[test]
fn uur_zero_timeout_is_partial() {
    let dir = temp_dir("timeout");
    std::fs::create_dir_all(&dir).unwrap();
    let input = dir.join("in.wasm");
    let output = dir.join("out.wasm");
    write_module(&module_with_unused_func(), &input);

    let oracle = Oracle::new("/bin/true");
    let pass = UurPass::new(&oracle, &dir.join("work"), false);
    let r = pass.reduce(&input, &output, Some(0.0)).unwrap();
    assert_eq!(r.exec_status, e2wr_pass::common::ExecStatus::ExecFailed);
    assert!(r.is_partial_by_timeout);
}

/// Always-true oracle + corpus: all multi-round iterations adopted; artifact valid, oracle passes.
#[test]
fn uur_corpus_smoke_with_true_oracle() {
    let dir = temp_dir("corpus");
    std::fs::create_dir_all(&dir).unwrap();
    let oracle = Oracle::new("/bin/true");
    let tt_pat = format!("{}/CP9201/CP9201_MAIN/tt/*.wasm", std::env::var("HOME").unwrap_or_default());
    let files: Vec<PathBuf> = glob::glob(&tt_pat)
        .unwrap()
        .filter_map(Result::ok)
        .collect::<Vec<_>>()
        .into_iter()
        .step_by(83)
        .take(4)
        .collect();
    assert!(!files.is_empty());
    for f in files {
        let output = dir.join(f.file_name().unwrap());
        let pass = UurPass::new(&oracle, &dir.join("work"), false);
        let r = pass.reduce(&f, &output, None)
            .unwrap_or_else(|e| panic!("{}: {e:#}", f.display()));
        assert!(validate(&output), "{} output invalid", f.display());
        let in_size = std::fs::metadata(&f).unwrap().len();
        let out_size = std::fs::metadata(&output).unwrap().len();
        assert!(out_size <= in_size, "{} grew: {in_size} -> {out_size}", f.display());
        let _ = r;
    }
}
