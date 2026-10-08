//! M11 FinalPolish function-level sub-step tests (synthetic modules + an always-true oracle).
//!
//! Covers: inline expansion/candidate judgment, wholesale unreachable replacement, return-type simplification
//! (including the padding and type-append paths), local simplification renumbering, and a full-orchestration smoke test.
//! Under an always-true oracle ProbDD converges to the empty keep set (deterministic, no random branching).

#![allow(clippy::field_reassign_with_default)]

use std::path::{Path, PathBuf};
use std::process::Command;

use e2wr_dd::factory::DdFactory;
use e2wr_dd::oracle::Oracle;
use e2wr_ir::module::*;
use wasmparser::ValType;
use e2wr_ir::snapshot::Snapshot;
use e2wr_ir::{Inst, SectionKind};
use e2wr_pass::final_polish::{
    get_called_ones_callee_and_caller, get_un_called_defined_func_idxs, FinalPolishConfig,
    FinalPolishPass,
};

fn ty(params: &[ValType], results: &[ValType]) -> FuncType {
    FuncType { params: params.to_vec(), results: results.to_vec() }
}

fn func(ty_idx: u32, locals: Vec<ValType>, insts: Vec<Inst>) -> Func {
    let mut insts = insts;
    insts.push(Inst::End);
    Func { ty_idx, locals, insts }
}

fn wasm_tools() -> PathBuf {
    std::env::var("E2WR_WASM_TOOLS")
        .map(PathBuf::from)
        .unwrap_or_else(|_| PathBuf::from("wasm-tools"))
}

fn validate(path: &Path) -> bool {
    Command::new(wasm_tools())
        .arg("validate")
        .arg(path)
        .output()
        .map(|o| o.status.success())
        .unwrap_or(false)
}

fn temp_dir(name: &str) -> PathBuf {
    let d = std::env::temp_dir().join(format!("e2wr-m11-{name}-{}", std::process::id()));
    std::fs::remove_dir_all(&d).ok();
    std::fs::create_dir_all(&d).unwrap();
    d
}

fn true_oracle() -> Oracle {
    Oracle::new("/bin/true")
}

fn run_pass(
    oracle: &Oracle,
    dir: &Path,
    module: &Module,
    cfg: FinalPolishConfig,
) -> (Snapshot, PathBuf) {
    let input = dir.join("in.wasm");
    let output = dir.join("out.wasm");
    Snapshot::from_module(module.clone()).encode_to_path(&input).unwrap();
    let pass = FinalPolishPass::new(oracle, DdFactory::default(), &dir.join("work"), cfg, false);
    let r = pass.reduce(&input, &output, 600.0).unwrap();
    assert_eq!(r.exec_status, e2wr_pass::common::ExecStatus::Success);
    (Snapshot::from_path(&output).unwrap(), output)
}

fn inline_only() -> FinalPolishConfig {
    FinalPolishConfig { enable_inline: true, polish_return_type: false, enable_size_polish: false }
}

// ---------------------------------------------------------------------------
// inline
// ---------------------------------------------------------------------------

/// Caller has no locals; callee has params/locals/return: the expansion = local.set padding params + a synthetic
/// block (result shorthand) + re-addressed local.get + br 0; the callee is deleted.
#[test]
fn inline_basic_with_params_locals_and_return() {
    let dir = temp_dir("inline-basic");
    let m = Module {
        types: vec![ty(&[], &[]), ty(&[ValType::I32], &[ValType::I32])],
        defined_func_ty_ids: vec![0, 1],
        defined_funcs: vec![
            // caller: i32.const 5; call 1; drop
            func(0, vec![], vec![
                Inst::I32Const { value: 5 },
                Inst::Call { function_index: 1 },
                Inst::Drop,
            ]),
            // callee: local.get 0; return (the local table [i64] exists just to trigger local extension)
            func(1, vec![ValType::I64], vec![
                Inst::LocalGet { local_index: 0 },
                Inst::Return,
            ]),
        ],
        ..Default::default()
    };
    assert_eq!(get_called_ones_callee_and_caller(&m), vec![(0, 1)]);
    let (snap, out) = run_pass(&true_oracle(), &dir, &m, inline_only());
    assert!(validate(&out), "inline output must validate");
    let mm = snap.module();
    assert_eq!(mm.defined_funcs.len(), 1, "callee removed");
    // caller locals = callee params [i32] + callee locals [i64].
    assert_eq!(mm.defined_funcs[0].locals, vec![ValType::I32, ValType::I64]);
    // Expansion sequence (same order as Python): local.set 0; block(result i32); local.get 0; br 0; end; drop.
    let expect = vec![
        Inst::I32Const { value: 5 },
        Inst::LocalSet { local_index: 0 },
        Inst::Block { blockty: wasmparser::BlockType::Type(ValType::I32) },
        Inst::LocalGet { local_index: 0 },
        Inst::Br { relative_depth: 0 },
        Inst::End,
        Inst::Drop,
        Inst::End,
    ];
    assert_eq!(mm.defined_funcs[0].insts, expect);
}

/// Callee without return and without locals: params dropped directly, no synthetic block.
#[test]
fn inline_without_return_and_locals() {
    let dir = temp_dir("inline-noret");
    let m = Module {
        types: vec![ty(&[], &[]), ty(&[ValType::I32], &[])],
        defined_func_ty_ids: vec![0, 1],
        defined_funcs: vec![
            func(0, vec![], vec![
                Inst::I32Const { value: 7 },
                Inst::Call { function_index: 1 },
            ]),
            // callee: nop (fine even without param-consuming instructions — params are dropped during expansion)
            func(1, vec![], vec![Inst::Nop]),
        ],
        ..Default::default()
    };
    let (snap, out) = run_pass(&true_oracle(), &dir, &m, inline_only());
    assert!(validate(&out));
    let mm = snap.module();
    assert_eq!(mm.defined_funcs.len(), 1);
    let expect = vec![
        Inst::I32Const { value: 7 },
        Inst::Drop,
        Inst::Nop,
        Inst::End,
    ];
    assert_eq!(mm.defined_funcs[0].insts, expect);
    assert!(mm.defined_funcs[0].locals.is_empty());
}

/// Inlining requires exactly one call: two calls (same caller) → no candidate, function count unchanged.
#[test]
fn inline_no_candidate_when_called_twice() {
    let m = Module {
        types: vec![ty(&[], &[]), ty(&[], &[])],
        defined_func_ty_ids: vec![0, 1],
        defined_funcs: vec![
            func(0, vec![], vec![
                Inst::Call { function_index: 1 },
                Inst::Call { function_index: 1 },
            ]),
            func(1, vec![], vec![Inst::Nop]),
        ],
        ..Default::default()
    };
    assert!(get_called_ones_callee_and_caller(&m).is_empty());
}

/// Calls in other functions pointing past a deleted function get renumbered down by one.
#[test]
fn inline_shifts_later_call_targets() {
    let dir = temp_dir("inline-shift");
    let m = Module {
        types: vec![ty(&[], &[])],
        defined_func_ty_ids: vec![0, 0, 0],
        defined_funcs: vec![
            func(0, vec![], vec![Inst::Call { function_index: 1 }]),
            func(0, vec![], vec![Inst::Nop]),
            // Nothing after func2 calling func1 (deleted); build the chain: 2 calls 0 (0<1 unchanged).
            func(0, vec![], vec![
                Inst::Call { function_index: 0 },
                Inst::Call { function_index: 0 },
            ]),
        ],
        ..Default::default()
    };
    // Candidate: callee=1 called once by func0 (func0 is called twice, not a candidate).
    assert_eq!(get_called_ones_callee_and_caller(&m), vec![(0, 1)]);
    let (snap, out) = run_pass(&true_oracle(), &dir, &m, inline_only());
    assert!(validate(&out));
    let mm = snap.module();
    assert_eq!(mm.defined_funcs.len(), 2);
    // func2 (becomes func1 after deleting 1) calls the original func0; target 0 < 1 → unchanged.
    assert!(mm.defined_funcs[1].insts.contains(&Inst::Call { function_index: 0 }));
}

// ---------------------------------------------------------------------------
// unreachable_replacement
// ---------------------------------------------------------------------------

/// Always-true oracle: functions with ≥2 instructions all become empty bodies (no-result form) or a single unreachable
/// (result form); single-instruction functions untouched.
#[test]
fn unreachable_replacement_true_oracle() {
    let dir = temp_dir("unreach");
    let m = Module {
        types: vec![ty(&[], &[]), ty(&[], &[ValType::I32])],
        defined_func_ty_ids: vec![0, 1, 0],
        defined_funcs: vec![
            // 2 instructions, no result → empty body.
            func(0, vec![ValType::I32], vec![Inst::Nop, Inst::Nop]),
            // 2 instructions, with result → single unreachable.
            func(1, vec![], vec![
                Inst::I32Const { value: 1 },
                Inst::I32Const { value: 2 },
                Inst::I32Add,
            ]),
            // 1 instruction → not a candidate.
            func(0, vec![], vec![Inst::Nop]),
        ],
        exports: vec![Export {
            name: "a".into(),
            desc: ExportDesc::Func(0),
        }],
        ..Default::default()
    };
    let cfg = FinalPolishConfig {
        enable_inline: false,
        polish_return_type: false,
        enable_size_polish: true,
    };
    let (snap, out) = run_pass(&true_oracle(), &dir, &m, cfg);
    assert!(validate(&out));
    let mm = snap.module();
    assert_eq!(mm.defined_funcs.len(), 3, "funcs not removed, only bodies replaced");
    assert_eq!(mm.defined_funcs[0].insts, vec![Inst::End]);
    assert_eq!(mm.defined_funcs[0].locals, Vec::<ValType>::new());
    assert_eq!(
        mm.defined_funcs[1].insts,
        vec![Inst::Unreachable, Inst::End]
    );
    assert_eq!(mm.defined_funcs[2].insts, vec![Inst::Nop, Inst::End]);
}

// ---------------------------------------------------------------------------
// polish_return_type
// ---------------------------------------------------------------------------

/// Uncalled (exported) function: under an always-true oracle ProbDD deletes all elements → empty body, type
/// becomes []→[] (reused by update_types after the type append). Called functions untouched.
#[test]
fn polish_return_type_true_oracle() {
    let dir = temp_dir("rty");
    let m = Module {
        types: vec![ty(&[], &[ValType::I32, ValType::I32]), ty(&[], &[])],
        defined_func_ty_ids: vec![0, 0],
        defined_funcs: vec![
            // Uncalled (exported only): two i32.const.
            func(0, vec![], vec![
                Inst::I32Const { value: 1 },
                Inst::I32Const { value: 2 },
            ]),
            // Called by itself → not in the uncalled set; stays untouched.
            func(0, vec![], vec![
                Inst::Call { function_index: 1 },
            ]),
        ],
        exports: vec![Export { name: "u".into(), desc: ExportDesc::Func(0) }],
        ..Default::default()
    };
    assert_eq!(get_un_called_defined_func_idxs(&m), vec![0]);
    let cfg = FinalPolishConfig {
        enable_inline: false,
        polish_return_type: true,
        enable_size_polish: false,
    };
    let (snap, out) = run_pass(&true_oracle(), &dir, &m, cfg);
    assert!(validate(&out));
    let mm = snap.module();
    // func0 simplified to an empty body + the []→[] type.
    assert_eq!(mm.defined_funcs[0].insts, vec![Inst::End]);
    let new_ty_idx = mm.defined_func_ty_ids[0] as usize;
    assert_eq!(mm.types[new_ty_idx], ty(&[], &[]));
    // func1 unchanged (the self-call target 1 did not move).
    assert_eq!(mm.defined_funcs[1].insts, vec![
        Inst::Call { function_index: 1 },
        Inst::End
    ]);
}

// ---------------------------------------------------------------------------
// polish_local
// ---------------------------------------------------------------------------

/// Local simplification: unreferenced locals deleted, used ones renumbered; params untouched.
#[test]
fn polish_local_renumbers_and_drops() {
    let dir = temp_dir("plocal");
    let m = Module {
        types: vec![ty(&[ValType::I32], &[])],
        defined_func_ty_ids: vec![0],
        defined_funcs: vec![func(
            0,
            // Defined locals [f32, i64, i32] (local numbers 1/2/3); only 1 (i64) and 3 (i32) are used.
            vec![ValType::F32, ValType::I64, ValType::I32],
            vec![
                Inst::I64Const { value: 0 },
                Inst::LocalSet { local_index: 2 },
                Inst::LocalGet { local_index: 2 },
                Inst::Drop,
                Inst::I32Const { value: 0 },
                Inst::LocalSet { local_index: 3 },
                Inst::LocalGet { local_index: 3 },
                Inst::Drop,
            ],
        )],
        ..Default::default()
    };
    // Direct sub-step invocation (in the full orchestration, unreachable_replacement empties the body first;
    // local simplification is verified in isolation here).
    let input = dir.join("in.wasm");
    let output = dir.join("out.wasm");
    Snapshot::from_module(m.clone()).encode_to_path(&input).unwrap();
    std::fs::copy(&input, &output).unwrap();
    let oracle = true_oracle();
    let pass = FinalPolishPass::new(
        &oracle,
        DdFactory::default(),
        &dir.join("work"),
        FinalPolishConfig::default(),
        false,
    );
    let snap = pass
        .polish_local_step(
            &output,
            Snapshot::from_path(&output).unwrap(),
            std::time::Instant::now() + std::time::Duration::from_secs(60),
        )
        .unwrap();
    assert!(validate(&output));
    let mm = snap.module();
    let f = &mm.defined_funcs[0];
    // Used locals: 2 (i64) and 3 (i32) → new table [i64, i32], new numbers 1/2.
    assert_eq!(f.locals, vec![ValType::I64, ValType::I32]);
    assert_eq!(
        f.insts,
        vec![
            Inst::I64Const { value: 0 },
            Inst::LocalSet { local_index: 1 },
            Inst::LocalGet { local_index: 1 },
            Inst::Drop,
            Inst::I32Const { value: 0 },
            Inst::LocalSet { local_index: 2 },
            Inst::LocalGet { local_index: 2 },
            Inst::Drop,
            Inst::End,
        ]
    );
}

// ---------------------------------------------------------------------------
// Full-orchestration smoke test
// ---------------------------------------------------------------------------

#[test]
fn full_pass_smoke_validates() {
    let dir = temp_dir("full");
    let m = Module {
        types: vec![
            ty(&[], &[]),
            ty(&[ValType::I32], &[ValType::I32]),
            ty(&[], &[ValType::I32, ValType::I32]),
        ],
        defined_func_ty_ids: vec![0, 1, 2],
        defined_funcs: vec![
            // func0 calls func1 (once, inlinable).
            func(0, vec![], vec![
                Inst::I32Const { value: 3 },
                Inst::Call { function_index: 1 },
                Inst::Drop,
            ]),
            func(1, vec![], vec![Inst::LocalGet { local_index: 0 }, Inst::Return]),
            // func2 uncalled (exported); extra return values simplifiable.
            func(2, vec![], vec![
                Inst::I32Const { value: 1 },
                Inst::I32Const { value: 2 },
            ]),
        ],
        exports: vec![Export { name: "u".into(), desc: ExportDesc::Func(2) }],
        ..Default::default()
    };
    let (snap, out) = run_pass(&true_oracle(), &dir, &m, FinalPolishConfig::default());
    assert!(validate(&out), "full pass output must validate");
    let mm = snap.module();
    // After inlining func1 is deleted → 2 functions remain.
    assert_eq!(mm.defined_funcs.len(), 2);
    let _ = SectionKind::Code;
}
