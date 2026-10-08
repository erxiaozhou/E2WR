//! FinalPolish definition-level sub-step tests: greedy and binary-search assertions under always-true/conditional oracles.

#![allow(clippy::field_reassign_with_default)]

use std::path::{Path, PathBuf};
use std::process::Command;

use e2wr_dd::oracle::Oracle;
use e2wr_ir::module::*;
use wasmparser::ValType;
use e2wr_ir::{Inst, Snapshot};
use e2wr_pass::final_polish::FinalPolishDefPass;

fn ty(params: &[ValType], results: &[ValType]) -> FuncType {
    FuncType { params: params.to_vec(), results: results.to_vec() }
}

fn func(ty_idx: u32, insts: Vec<Inst>) -> Func {
    let mut insts = insts;
    insts.push(Inst::End);
    Func { ty_idx, locals: vec![], insts }
}

fn memory(min: u64, max: Option<u64>) -> Memory {
    Memory { ty: MemType { limits: Limits { min, max } } }
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
    let d = std::env::temp_dir().join(format!("e2wr-pass-fp-{}-{name}", std::process::id()));
    std::fs::remove_dir_all(&d).ok();
    std::fs::create_dir_all(&d).unwrap();
    d
}

fn oracle_script(dir: &Path, name: &str, body: &str) -> Oracle {
    let p = dir.join(format!("{name}.sh"));
    std::fs::write(&p, format!("#!/bin/sh\n{body}\n")).unwrap();
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        std::fs::set_permissions(&p, std::fs::Permissions::from_mode(0o755)).unwrap();
    }
    Oracle::new(p.to_str().unwrap())
}

fn run_pass(oracle: &Oracle, dir: &Path, module: &Module) -> (Snapshot, PathBuf) {
    let input = dir.join("in.wasm");
    let output = dir.join("out.wasm");
    Snapshot::from_module(module.clone()).encode_to_path(&input).unwrap();
    let pass = FinalPolishDefPass::new(oracle, &dir.join("work"), false);
    let r = pass.reduce(&input, &output, 600.0).unwrap();
    assert_eq!(r.exec_status, e2wr_pass::common::ExecStatus::Success);
    let snap = Snapshot::from_path(&output).unwrap();
    assert!(validate(&output), "output must be valid");
    (snap, output)
}

/// update_types: orphan types deleted (always-true oracle).
#[test]
fn update_types_removes_orphans() {
    let dir = temp_dir("types");
    let oracle = Oracle::new("/bin/true");
    let mut m = Module::default();
    // types [X(orphan), A]: f0 uses A.
    m.types = vec![ty(&[ValType::F64, ValType::F64], &[]), ty(&[], &[])];
    m.defined_func_ty_ids = vec![1];
    m.defined_funcs = vec![func(1, vec![])];
    let (snap, _) = run_pass(&oracle, &dir, &m);
    assert_eq!(snap.module().types.len(), 1);
    assert_eq!(snap.module().defined_func_ty_ids, vec![0]);
}

/// reduce_memory_def: always-true → all (1, unbounded); conditional oracle (min>=3) →
/// step1 drops the max successfully + the min binary search converges to 3.
#[test]
fn memory_greedy_and_binary_search() {
    // Always-true: trying min=1 succeeds directly.
    let dir = temp_dir("mem-true");
    let oracle = Oracle::new("/bin/true");
    let mut m = Module::default();
    m.types = vec![ty(&[], &[])];
    m.defined_memory_datas = vec![memory(10, Some(20)), memory(7, None)];
    m.defined_func_ty_ids = vec![0];
    m.defined_funcs = vec![func(0, vec![])];
    let (snap, _) = run_pass(&oracle, &dir, &m);
    for mem in &snap.module().defined_memory_datas {
        assert_eq!(mem.ty.limits, Limits { min: 1, max: None });
    }

    // Conditional oracle: memory 0 needs min>=3.
    // (10,20): step0 (1) fails; step1 (10,None) succeeds (max dropped); min binary search 5→3 succeeds, 2 fails → (3,None).
    let dir = temp_dir("mem-bin");
    let oracle = oracle_script(
        &dir,
        "min3",
        &format!("{} print \"$1\" | grep -qE 'memory .;0;. ([3-9]|[1-9][0-9]+)'", wasm_tools().display()),
    );
    let mut m = Module::default();
    m.types = vec![ty(&[], &[])];
    m.defined_memory_datas = vec![memory(10, Some(20))];
    m.defined_func_ty_ids = vec![0];
    m.defined_funcs = vec![func(0, vec![])];
    let (snap, _) = run_pass(&oracle, &dir, &m);
    assert_eq!(snap.module().defined_memory_datas[0].ty.limits, Limits { min: 3, max: None });
}

/// reduce_elem_def: the expression form (each entry exactly one ref.func) → the function-index shorthand;
/// segments mixed with ref.null are skipped.
#[test]
fn elem_def_rewrites_exprs_to_funcidxs() {
    let dir = temp_dir("elemdef");
    let oracle = Oracle::new("/bin/true");
    let mut m = Module::default();
    m.types = vec![ty(&[], &[])];
    m.defined_func_ty_ids = vec![0, 0];
    m.defined_funcs = vec![func(0, vec![]), func(0, vec![])];
    let ref_func = |i: u32| vec![Inst::RefFunc { function_index: i }];
    m.elem_sec_datas = vec![
        ElemSeg {
            mode: ElemMode::Active {
                table_idx: Some(0),
                offset: vec![Inst::I32Const { value: 0 }, Inst::End],
            },
            payload: ElemPayload::Exprs { elem_ty: RefType::FUNCREF, exprs: vec![ref_func(0), ref_func(1)] },
        },
        ElemSeg {
            mode: ElemMode::Passive,
            payload: ElemPayload::Exprs {
                elem_ty: RefType::FUNCREF,
                exprs: vec![vec![Inst::RefNull { hty: wasmparser::HeapType::Abstract { ty: wasmparser::AbstractHeapType::Func, shared: false } }]],
            },
        },
    ];
    m.defined_table_datas = vec![Table {
        ty: TableType { elem_ty: RefType::FUNCREF, limits: Limits { min: 4, max: None } },
        init_expr: None,
    }];
    // Direct sub-step invocation (the reduce_elem_seg that follows in the orchestration empties the element segments;
    // the conversion-effect assertions must avoid it).
    let output = dir.join("out.wasm");
    let input = dir.join("in.wasm");
    Snapshot::from_module(m.clone()).encode_to_path(&input).unwrap();
    std::fs::copy(&input, &output).unwrap();
    let pass = FinalPolishDefPass::new(&oracle, &dir.join("work"), false);
    let snap = pass
        .reduce_elem_def(Snapshot::from_path(&output).unwrap(), &output, None)
        .unwrap();
    // Segment 0: converted to shorthand (mode keeps the explicit table number).
    match &snap.module().elem_sec_datas[0] {
        ElemSeg { mode: ElemMode::Active { table_idx: Some(0), .. }, payload: ElemPayload::FuncIdxs(idxs) } => {
            assert_eq!(idxs, &vec![0, 1]);
        }
        other => panic!("seg0 not rewritten: {other:?}"),
    }
    // Segment 1: contains ref.null; stays in expression form.
    assert!(matches!(snap.module().elem_sec_datas[1].payload, ElemPayload::Exprs { .. }));
}

/// reduce_data_seg: always-true → empty data; conditional oracle (content must contain tail) → the prefix binary search keeps 4 bytes.
#[test]
fn data_seg_empty_and_prefix_binary() {
    // Always-true: the empty-segment probe succeeds directly.
    let dir = temp_dir("data-true");
    let oracle = Oracle::new("/bin/true");
    let mut m = base_data_module(b"tailAAAA");
    let (snap, _) = run_pass(&oracle, &dir, &m);
    assert!(snap.module().data_sec_datas[0].data.is_empty());

    // Conditional oracle: grep tail. Empty fails; binary search mid=4 "tail" succeeds, mid=2/3 fail → keep 4 bytes.
    let dir = temp_dir("data-bin");
    let oracle = oracle_script(&dir, "tail", "grep -q tail \"$1\"");
    m = base_data_module(b"tailAAAA");
    let (snap, _) = run_pass(&oracle, &dir, &m);
    assert_eq!(snap.module().data_sec_datas[0].data, b"tail");
}

fn base_data_module(bytes: &[u8]) -> Module {
    let mut m = Module::default();
    m.types = vec![ty(&[], &[])];
    m.defined_memory_datas = vec![memory(1, None)];
    m.defined_func_ty_ids = vec![0];
    m.defined_funcs = vec![func(0, vec![])];
    m.data_sec_datas = vec![DataSeg {
        mode: DataMode::Active { mem_idx: 0, offset: vec![Inst::I32Const { value: 0 }, Inst::End] },
        data: bytes.to_vec(),
    }];
    m
}

/// reduce_elem_seg: always-true → the element segment list emptied.
#[test]
fn elem_seg_emptied_with_true_oracle() {
    let dir = temp_dir("elemseg");
    let oracle = Oracle::new("/bin/true");
    let mut m = Module::default();
    m.types = vec![ty(&[], &[])];
    m.defined_func_ty_ids = vec![0, 0, 0];
    m.defined_funcs = vec![func(0, vec![]), func(0, vec![]), func(0, vec![])];
    m.elem_sec_datas = vec![ElemSeg {
        mode: ElemMode::Passive,
        payload: ElemPayload::FuncIdxs(vec![0, 1, 2]),
    }];
    let (snap, _) = run_pass(&oracle, &dir, &m);
    match &snap.module().elem_sec_datas[0].payload {
        ElemPayload::FuncIdxs(idxs) => assert!(idxs.is_empty()),
        other => panic!("{other:?}"),
    }
}

/// Orchestration entry + corpus + always-true oracle: the artifact is valid, size does not grow, each step decidable.
#[test]
fn def_only_pipeline_corpus_smoke() {
    let dir = temp_dir("corpus");
    let oracle = Oracle::new("/bin/true");
    // Small files (per-probe whole-module re-encoding on large files is a known performance trait of the initial
    // implementation's free zone, see O-6; the smoke test stays light with small files).
    let tt_pat = format!("{}/CP9201/CP9201_MAIN/tt/*.wasm", std::env::var("HOME").unwrap_or_default());
    let mut files: Vec<PathBuf> = glob::glob(&tt_pat)
        .unwrap()
        .filter_map(Result::ok)
        .filter(|f| f.metadata().map(|m| m.len() < 4096).unwrap_or(false))
        .collect();
    files.sort();
    let files: Vec<PathBuf> = files.into_iter().step_by(7).take(3).collect();
    assert!(!files.is_empty(), "no small corpus files");
    for f in files {
        let output = dir.join(f.file_name().unwrap());
        let pass = FinalPolishDefPass::new(&oracle, &dir.join("work"), false);
        let r = pass.reduce(&f, &output, 600.0)
            .unwrap_or_else(|e| panic!("{}: {e:#}", f.display()));
        assert!(validate(&output), "{} output invalid", f.display());
        let in_size = std::fs::metadata(&f).unwrap().len();
        let out_size = std::fs::metadata(&output).unwrap().len();
        assert!(out_size <= in_size, "{} grew: {in_size} -> {out_size}", f.display());
        assert_eq!(r.reduced_size_num, Some(in_size as i64 - out_size as i64));
    }
}
