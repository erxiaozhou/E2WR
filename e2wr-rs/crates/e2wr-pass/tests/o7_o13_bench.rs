//! O-7/O-13 pre-refactoring baseline benchmark (before the refactor, 2026-09-27; O-8 added the same day).
//!
//! Purpose: isolate and measure the two clone costs in the NodeShrinkPass task loop —
//! - the (Snapshot, Ast) double clone injected per task (O-13: the new_with_* construction
//!   pattern of NodeListTrialApplier / NodeRewriter);
//! - the Ast queries' Vec clones (O-7: sub_nodes / get_insts).
//!
//! Also measures the decode/tree-build/pool-build breakdown, and the macro baseline of a whole
//! NodeShrinkPass stage under a real oracle (the to_test=None pure instruction-level path, avoiding instrumentation noise).
//!
//! O-8 addendum (second round, 2026-09-27): the per-task `Context::from_module` full rebuild
//! (via build_one_node_list_reduction_ctx →
//! cur_context_by_ast_info), and the per-task V9 prefix cost
//! (ctx building + get_raw_elems_from_env element materialization, O-12-related),
//! timing the first K NodeLists tasks chosen by the real task pool one by one.
//!
//! Corpus: the 20 oracle-bearing cases of benchmark/RQ12 by size order
//! (22KB–22MB). The oracle uses each case's own .py (shebang and executable bit in place).
//! Override the corpus root via E2WR_BENCH_RQ12 if needed.
//!
//! Run (release is the recommended form; see O-6):
//! ```
//! cargo test --release -p e2wr-pass --test o7_o13_bench -- --ignored --nocapture
//! ```
//! Environment variables:
//! - E2WR_BENCH_ORACLE: real (default, each case's own oracle) | true | false;
//! - E2WR_BENCH_MACRO_SECS: per-case macro timeout (default 90);
//! - E2WR_BENCH_SKIP_MACRO: =1 skips the macro part (micro only, for the O-8 addendum);
//! - E2WR_BENCH_CASES: comma-separated case-name filter;
//! - E2WR_BENCH_OUT: JSON output path (default target/bench-o7-o13/baseline.json).

use std::hint::black_box;
use std::path::{Path, PathBuf};
use std::time::{Duration, Instant};

use e2wr_dd::factory::DdFactory;
use e2wr_dd::oracle::Oracle;
use e2wr_ir::ast::{Ast, NodeId, NodeKind};
use e2wr_ir::snapshot::Snapshot;
use e2wr_ir::types::Context;
use e2wr_pass::instseq::{build_one_node_list_reduction_ctx, get_raw_elems_from_env};
use e2wr_pass::instseq::v9::OnlyOneInstTask;
use e2wr_pass::nodeshrink_pass::{AstNodePool, NodeShrinkPass, Task};

/// Corpus root: overridable via E2WR_BENCH_RQ12; defaults to the repo's benchmark/RQ12.
fn rq12_root() -> PathBuf {
    env_or(
        "E2WR_BENCH_RQ12",
        concat!(env!("CARGO_MANIFEST_DIR"), "/../../../benchmark/RQ12"),
    )
    .into()
}

/// The 20 oracle-bearing RQ12 cases by descending byte size (inventory of 2026-09-27).
const CASES: &[&str] = &[
    "boa",
    "ffmpeg",
    "commanderkeen",
    "figma-startpage",
    "hydro",
    "bullet",
    "pacalc",
    "wasmedge#3057",
    "mandelbrot",
    "pathfinding",
    "U2646",
    "C2557",
    "U2698",
    "U2931",
    "smith_p15_1675",
    "C2450",
    "wasmedge#3019",
    "wamr#2789",
    "wamr#2862",
    "U2641",
];

fn env_or(key: &str, default: &str) -> String {
    std::env::var(key).unwrap_or_else(|_| default.to_string())
}

/// Adaptive timing: one probe run first, then a repetition count (1..=200) chosen by budget; returns (count, total seconds).
fn time_clones<T, F: FnMut() -> T>(mut f: F, budget: Duration) -> (usize, f64) {
    let t = Instant::now();
    black_box(f());
    let one = t.elapsed().as_secs_f64().max(1e-9);
    let n = ((budget.as_secs_f64() / one) as usize).clamp(1, 200);
    let t = Instant::now();
    for _ in 0..n {
        black_box(f());
    }
    (n, t.elapsed().as_secs_f64())
}

struct MicroResult {
    size: u64,
    funcs: usize,
    nodes: usize,
    inst_nodes: usize,
    list_nodes: usize,
    insts: u64,
    load_ms: f64,
    ast_ms: f64,
    snap_clone_ms: f64,
    ast_clone_ms: f64,
    snap_clone_n: usize,
    ast_clone_n: usize,
    query_sweep_ms: f64,
    pool_build_ms: f64,
    ctx_build_ms: f64,
    ctx_build_n: usize,
    task_pre_ms: f64,
    task_pre_n: usize,
    task_pre_elems: f64,
}

fn micro_bench(wasm: &Path) -> MicroResult {
    let size = std::fs::metadata(wasm).unwrap().len();
    let t = Instant::now();
    let snap = Snapshot::from_path(wasm).expect("decode");
    let load_ms = t.elapsed().as_secs_f64() * 1e3;

    let t = Instant::now();
    let ast = Ast::from_module(snap.module()).expect("build ast");
    let ast_ms = t.elapsed().as_secs_f64() * 1e3;

    let funcs = snap.module().defined_funcs.len();
    let nodes = ast.node_count();
    let mut inst_nodes = 0usize;
    let mut list_nodes = 0usize;
    let mut insts = 0u64;
    for i in 0..nodes {
        match ast.node(NodeId(i as u32)).kind {
            NodeKind::Insts { .. } => {
                inst_nodes += 1;
                insts += ast.get_length(NodeId(i as u32)) as u64;
            }
            NodeKind::List { .. } => list_nodes += 1,
            _ => {}
        }
    }

    // O-13: the equivalent of the per-task injected clone (the real chain = applier/rewriter constructor arguments).
    let (snap_clone_n, snap_clone_s) =
        time_clones(|| snap.clone(), Duration::from_secs(2));
    let (ast_clone_n, ast_clone_s) =
        time_clones(|| ast.clone(), Duration::from_secs(2));

    // O-7: query clones — all List nodes' sub_nodes + all Insts nodes' get_insts
    // (the query shapes of AstNodePool building and V9 element materialization); take the minimum of 3.
    let mut query_sweep_ms = f64::MAX;
    for _ in 0..3 {
        let t = Instant::now();
        let mut acc: u64 = 0;
        for i in 0..nodes {
            match ast.node(NodeId(i as u32)).kind {
                NodeKind::List { .. } => {
                    acc += black_box(ast.sub_nodes(NodeId(i as u32))).len() as u64;
                }
                NodeKind::Insts { .. } => {
                    acc += black_box(ast.get_insts(NodeId(i as u32))).len() as u64;
                }
                _ => {}
            }
        }
        black_box(acc);
        query_sweep_ms = query_sweep_ms.min(t.elapsed().as_secs_f64() * 1e3);
    }

    let t = Instant::now();
    let pool = AstNodePool::from_ast(&ast, Some(insts), true);
    let pool_build_ms = t.elapsed().as_secs_f64() * 1e3;
    black_box(pool.is_empty());

    // O-8: the per-task Context::from_module full rebuild (median function;
    // the cost is dominated by copying the whole-module type/index/global tables, largely independent of the target function).
    let module = snap.module();
    let mid_func = module.defined_funcs.len() / 2;
    let (ctx_build_n, ctx_build_s) = time_clones(
        || Context::from_module(module, mid_func).expect("context"),
        Duration::from_secs(2),
    );

    // The per-task V9 prefix (real shape: the task pool picks a NodeLists task →
    // build_one_node_list_reduction_ctx + get_raw_elems_from_env).
    // the first K NodeLists tasks timed one by one (the pool is ordered; the first K = the K largest
    // tasks, representing upper-side samples of the per-task cost).
    let mut pool2 = AstNodePool::from_ast(&ast, Some(insts), true);
    let mut pre_times: Vec<f64> = Vec::new();
    let mut pre_elems: Vec<usize> = Vec::new();
    while pre_times.len() < 16 {
        let Some(task) = pool2.practical_select(&ast, None) else {
            break;
        };
        if let Task::NodeLists(list) = task {
            let t = Instant::now();
            let ctx = build_one_node_list_reduction_ctx(module, &ast, list)
                .expect("reduction ctx");
            let elems = get_raw_elems_from_env(module, &ast, &ctx).expect("elems");
            pre_times.push(t.elapsed().as_secs_f64() * 1e3);
            pre_elems.push(elems.len());
        }
    }
    let task_pre_n = pre_times.len();
    let task_pre_ms = if task_pre_n > 0 {
        pre_times.iter().sum::<f64>() / task_pre_n as f64
    } else {
        0.0
    };
    let task_pre_elems = if task_pre_n > 0 {
        pre_elems.iter().sum::<usize>() as f64 / task_pre_n as f64
    } else {
        0.0
    };

    MicroResult {
        size,
        funcs,
        nodes,
        inst_nodes,
        list_nodes,
        insts,
        load_ms,
        ast_ms,
        snap_clone_ms: snap_clone_s * 1e3 / snap_clone_n as f64,
        ast_clone_ms: ast_clone_s * 1e3 / ast_clone_n as f64,
        snap_clone_n,
        ast_clone_n,
        query_sweep_ms,
        pool_build_ms,
        ctx_build_ms: ctx_build_s * 1e3 / ctx_build_n as f64,
        ctx_build_n,
        task_pre_ms,
        task_pre_n,
        task_pre_elems,
    }
}

struct MacroResult {
    taken_s: f64,
    reduced_size: i64,
    reduced_inst: i64,
    is_partial: bool,
    success: bool,
}

fn macro_bench(case: &str, wasm: &Path, work_root: &Path, oracle_spec: &str, secs: f64) -> MacroResult {
    let dir = work_root.join(case);
    let _ = std::fs::remove_dir_all(&dir);
    std::fs::create_dir_all(&dir).unwrap();
    let oracle = Oracle::new(oracle_spec);
    let mut factory = DdFactory::default();
    factory.enable_p0_pred();
    factory.set_default_initial_p(0.1);
    let mut pass = NodeShrinkPass::with_stage(
        &oracle,
        &dir,
        None,
        factory,
        true,
        OnlyOneInstTask::Disable,
        false,
    );
    let out = dir.join("out.wasm");
    let r = pass.reduce(wasm, &out, secs).expect("nodeshrink reduce");
    MacroResult {
        taken_s: r.exec_taken_time,
        reduced_size: r.reduced_size_num.unwrap_or(0),
        reduced_inst: r.reduced_inst_num.map(|v| v as i64).unwrap_or(0),
        is_partial: r.is_partial_by_timeout,
        success: r.exec_status == e2wr_pass::common::ExecStatus::Success,
    }
}

#[test]
#[ignore = "O-7/O-13 baseline benchmark: with a real oracle, macro defaults to 90s per case, ~35 min for the full set (E2WR_BENCH_SKIP_MACRO=1 runs micro only)"]
fn o7_o13_baseline() {
    let oracle_mode = env_or("E2WR_BENCH_ORACLE", "real");
    let macro_secs: f64 = env_or("E2WR_BENCH_MACRO_SECS", "90").parse().unwrap();
    let skip_macro = env_or("E2WR_BENCH_SKIP_MACRO", "") == "1";
    let filter: Vec<String> = env_or("E2WR_BENCH_CASES", "")
        .split(',')
        .filter(|s| !s.is_empty())
        .map(str::to_string)
        .collect();
    let out_path = PathBuf::from(env_or(
        "E2WR_BENCH_OUT",
        concat!(env!("CARGO_MANIFEST_DIR"), "/../../target/bench-o7-o13/baseline.json"),
    ));

    let work_root = out_path.parent().unwrap().join("work");
    std::fs::create_dir_all(&work_root).unwrap();

    println!(
        "baseline: oracle={oracle_mode} macro_secs={macro_secs} skip_macro={skip_macro} cases={}",
        if filter.is_empty() { "all".to_string() } else { filter.join(",") }
    );
    println!(
        "{:<20} {:>9} {:>6} {:>8} {:>9} {:>10} {:>10} {:>10} {:>11} {:>9} {:>9} {:>9} {:>10} | {:>7} {:>8} {:>8} {:>5}",
        "case", "size", "funcs", "nodes", "insts",
        "load_ms", "ast_ms", "snapCl_ms", "astCl_ms", "query_ms", "ctxB_ms", "taskPre", "taskPreEls",
        "mac_s", "redSize", "redInst", "part"
    );

    let mut rows: Vec<serde_json::Value> = Vec::new();
    for case in CASES {
        if !filter.is_empty() && !filter.iter().any(|f| f == case) {
            continue;
        }
        let wasm = rq12_root().join(format!("{case}.wasm"));
        if !wasm.exists() {
            eprintln!("{case}: missing {}, skipped", wasm.display());
            continue;
        }
        let m = micro_bench(&wasm);
        let oracle_spec = match oracle_mode.as_str() {
            "true" => "/bin/true".to_string(),
            "false" => "/bin/false".to_string(),
            _ => rq12_root().join(format!("{case}.py")).display().to_string(),
        };
        let g = if skip_macro {
            None
        } else {
            Some(macro_bench(case, &wasm, &work_root, &oracle_spec, macro_secs))
        };
        println!(
            "{:<20} {:>9} {:>6} {:>8} {:>9} {:>10.1} {:>10.1} {:>10.2} {:>11.2} {:>9.1} {:>9.2} {:>9.2} {:>10.1} | {:>7} {:>8} {:>8} {:>5}",
            case, m.size, m.funcs, m.nodes, m.insts,
            m.load_ms, m.ast_ms, m.snap_clone_ms, m.ast_clone_ms, m.query_sweep_ms,
            m.ctx_build_ms, m.task_pre_ms, m.task_pre_elems,
            g.as_ref().map(|x| format!("{:.1}", x.taken_s)).unwrap_or_else(|| "-".into()),
            g.as_ref().map(|x| x.reduced_size.to_string()).unwrap_or_else(|| "-".into()),
            g.as_ref().map(|x| x.reduced_inst.to_string()).unwrap_or_else(|| "-".into()),
            g.as_ref().map(|x| x.is_partial.to_string()).unwrap_or_else(|| "-".into()),
        );
        rows.push(serde_json::json!({
            "case": case,
            "size": m.size,
            "funcs": m.funcs,
            "nodes": m.nodes,
            "inst_nodes": m.inst_nodes,
            "list_nodes": m.list_nodes,
            "insts": m.insts,
            "load_ms": m.load_ms,
            "ast_ms": m.ast_ms,
            "snap_clone_ms": m.snap_clone_ms,
            "ast_clone_ms": m.ast_clone_ms,
            "snap_clone_n": m.snap_clone_n,
            "ast_clone_n": m.ast_clone_n,
            "query_sweep_ms": m.query_sweep_ms,
            "pool_build_ms": m.pool_build_ms,
            "ctx_build_ms": m.ctx_build_ms,
            "ctx_build_n": m.ctx_build_n,
            "task_pre_ms": m.task_pre_ms,
            "task_pre_n": m.task_pre_n,
            "task_pre_elems": m.task_pre_elems,
            "macro_oracle": if skip_macro { None } else { Some(oracle_spec) },
            "macro_secs": if skip_macro { None } else { Some(macro_secs) },
            "macro_taken_s": g.as_ref().map(|x| x.taken_s),
            "macro_reduced_size": g.as_ref().map(|x| x.reduced_size),
            "macro_reduced_inst": g.as_ref().map(|x| x.reduced_inst),
            "macro_is_partial": g.as_ref().map(|x| x.is_partial),
            "macro_success": g.as_ref().map(|x| x.success),
        }));
    }

    let doc = serde_json::json!({
        "meta": {
            "mode": "baseline-pre-O7-O13-O8",
            "oracle_mode": oracle_mode,
            "macro_secs": macro_secs,
            "skip_macro": skip_macro,
            "unix_ts": std::time::SystemTime::now()
                .duration_since(std::time::UNIX_EPOCH)
                .map(|d| d.as_secs())
                .unwrap_or(0),
        },
        "cases": rows,
    });
    std::fs::write(&out_path, serde_json::to_string_pretty(&doc).unwrap()).unwrap();
    println!("baseline json written to {}", out_path.display());
}
