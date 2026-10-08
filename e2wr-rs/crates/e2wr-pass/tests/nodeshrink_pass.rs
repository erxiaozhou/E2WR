//! M10-U10 task pool and NodeShrinkPass body acceptance:
//! 1. AstNodePool synthetic unit tests (scoring order/triple filtering/CF dispatch/P3 heap fallback/success refill);
//! 2. NodeShrinkPass smoke test (to_test=None, no function level): always-true oracle full reduction,
//!    always-false oracle zero reduction;
//! 3. the P-5 odd-success spot check → exception recovery (branch 3: last_pass_case failing the oracle →
//!    early_return + trailing rollback): triggered with a counting oracle that passes the first K calls and rejects afterwards.

use std::path::{Path, PathBuf};

use e2wr_dd::factory::DdFactory;
use e2wr_dd::oracle::Oracle;
use e2wr_ir::ast::Ast;
use e2wr_ir::encode::encode_module;
use e2wr_ir::module::{Func, FuncType, Module};
use e2wr_ir::snapshot::Snapshot;
use e2wr_ir::Inst;

use e2wr_pass::nodeshrink_pass::{
    node_size_score, AstNodePool, NodeShrinkPass, Task,
};

fn temp_dir(name: &str) -> PathBuf {
    let d = std::env::temp_dir().join(format!("e2wr-pass-u10-{}-{name}", std::process::id()));
    std::fs::remove_dir_all(&d).ok();
    std::fs::create_dir_all(&d).unwrap();
    d
}

/// Two-function synthetic module: each body [i32.const N, drop, end] (P3 can delete the whole run).
fn two_func_snapshot() -> Snapshot {
    let module = Module {
        types: vec![FuncType { params: vec![], results: vec![] }],
        defined_func_ty_ids: vec![0, 0],
        defined_funcs: vec![
            Func {
                ty_idx: 0,
                locals: vec![],
                insts: vec![Inst::I32Const { value: 1 }, Inst::Drop, Inst::End],
            },
            Func {
                ty_idx: 0,
                locals: vec![],
                insts: vec![Inst::I32Const { value: 2 }, Inst::Drop, Inst::End],
            },
        ],
        ..Default::default()
    };
    let bytes = encode_module(&module).unwrap();
    Snapshot::from_bytes(bytes).unwrap()
}

/// Counting oracle script: the first K invocations exit 0, afterwards always 1.
fn counting_oracle(dir: &Path, k: u32) -> (Oracle, PathBuf) {
    let script = dir.join(format!("oracle_{k}.py"));
    let cnt = dir.join(format!("cnt_{k}.txt"));
    std::fs::write(&cnt, "0").unwrap();
    let script_body = format!(
        "#!/usr/bin/env python3\nimport sys\ncnt = '{}'\nc = int(open(cnt).read() or 0) + 1\nopen(cnt, 'w').write(str(c))\nsys.exit(0 if c <= {} else 1)\n",
        cnt.display(),
        k
    );
    std::fs::write(&script, script_body).unwrap();
    // The oracle runs directly via sh -c "<script> <path>"; the executable bit is required.
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        std::fs::set_permissions(&script, std::fs::Permissions::from_mode(0o755)).unwrap();
    }
    (Oracle::new(script.to_string_lossy().as_ref()), cnt)
}

fn body_inst_num_of(path: &Path) -> usize {
    Snapshot::from_path(path)
        .unwrap()
        .module()
        .defined_funcs
        .iter()
        .map(|f| f.insts.len() - 1)
        .sum()
}

#[test]
fn pool_select_order_filter_and_p3() {
    let snap = two_func_snapshot();
    let ast = Ast::from_module(snap.module()).unwrap();
    let mut pool = AstNodePool::from_ast(&ast, Some(4), true);
    // Two function root lists (1.0+3=4 points each) → two list tasks; no CF nodes.
    let t1 = pool.practical_select(&ast, None).expect("task 1");
    let t2 = pool.practical_select(&ast, None).expect("task 2");
    assert!(matches!(t1, Task::NodeLists(_)) && matches!(t2, Task::NodeLists(_)));
    // Main heap exhausted → P3 heap empty → None.
    assert!(pool.practical_select(&ast, None).is_none());
    assert!(pool.is_empty());

    // Threshold: candidates scoring >= max_score are dropped (both root lists tie at 1.0+2=3).
    let mut pool = AstNodePool::from_ast(&ast, Some(4), true);
    assert!(pool.practical_select(&ast, Some(3.0)).is_none());
    // Relaxing the threshold → selectable.
    let mut pool = AstNodePool::from_ast(&ast, Some(4), true);
    assert!(pool.practical_select(&ast, Some(4.0)).is_some());

    // Removed nodes are skipped: after detaching all of func0's root list's whole-run children (length still >0's
    // parent-chain-break test: judging the root from outside the tree is infeasible — test the sub-list detach path instead).
    // Setup: root0's children all replaced with empty → root0 length 0 → the length filter kicks in.
    let mut ast2 = Ast::from_module(snap.module()).unwrap();
    let root0 = ast2.root_of_func(0);
    let children = ast2.sub_nodes(root0);
    ast2.replace_split_with_new_sub_nodes(root0, 0, children.len(), vec![]);
    let mut pool = AstNodePool::from_ast(&ast2, Some(4), true);
    // func0's root length 0 keeps it out of the pool; only func1's root remains.
    let t = pool.practical_select(&ast2, None).expect("only func1 root");
    let Task::NodeLists(n) = t else { panic!("list task") };
    assert_eq!(ast2.node(n).loc.func_idx, 1);

    // Success refill: the new node's subtree enters the main heap; under reprocess the parent list goes to the P3 heap; once the main heap
    // is exhausted, parent-list tasks come from the P3 heap; already-processed P3 nodes do not repeat.
    let mut ast3 = Ast::from_module(snap.module()).unwrap();
    let root0 = ast3.root_of_func(0);
    let root1 = ast3.root_of_func(1);
    let mut pool = AstNodePool::from_ast(&ast3, Some(4), true);
    // First take away both root tasks (score ties broken by NodeId order: root0, root1).
    let _ = pool.practical_select(&ast3, None).unwrap();
    let _ = pool.practical_select(&ast3, None).unwrap();
    // Simulate func1's root task succeeding: new nodes = empty (wholesale delete), parent = none (roots have none).
    pool.handle_task_success(&ast3, &Task::NodeLists(root1), &[]);
    assert!(pool.is_empty(), "no new nodes, root has no parent");
    let (new_nodes_snap, ast4) = {
        // Hand-build a 1-instruction child node attached to root0 (borrowing append_insts_tree).
        let module = snap.module();
        let (_r, new_nodes) = ast3
            .append_insts_tree(
                module,
                &[Inst::I32Const { value: 7 }, Inst::Drop],
                e2wr_ir::types::FTy::of(&[], &[]),
                0,
            )
            .unwrap();
        let children = ast3.sub_nodes(root0);
        ast3.replace_split_with_new_sub_nodes(
            root0,
            0,
            children.len(),
            new_nodes.clone(),
        );
        (new_nodes, ast3)
    };
    let mut pool = AstNodePool::from_ast(&ast4, Some(4), true);
    pool.handle_task_success(&ast4, &Task::NodeLists(root0), &new_nodes_snap);
    // New InstsNodes do not enter the heap; root0 (updated) and root1 are reachable via the P3 heap fallback.
    let mut got = Vec::new();
    while let Some(t) = pool.practical_select(&ast4, None) {
        got.push(t.first_node());
    }
    assert_eq!(got.len(), 2, "root0 and root1 each pass through the P3 heap once");
    // The P3 processed set: no repeats for the same node.
    let mut pool = AstNodePool::from_ast(&ast4, Some(4), true);
    pool.handle_task_success(&ast4, &Task::NodeLists(root0), &[]);
    assert!(pool.practical_select(&ast4, None).is_some());
    assert!(pool.practical_select(&ast4, None).is_some());
    assert!(pool.practical_select(&ast4, None).is_none());
}

#[test]
fn pool_score_and_root_is_list_task() {
    let snap = two_func_snapshot();
    let ast = Ast::from_module(snap.module()).unwrap();
    let root = ast.root_of_func(0);
    // NodeList score = 1 + length (root length = 2 body instructions, end excluded; unaffected
    // by max_length).
    assert_eq!(node_size_score(&ast, root, None), 1.0 + 2.0);
    assert_eq!(node_size_score(&ast, root, Some(100.0)), 1.0 + 2.0);
    // InstsNode score is always 0.
    let child = ast.sub_nodes(root)[0];
    if matches!(
        ast.node(child).kind,
        e2wr_ir::ast::NodeKind::Insts { .. }
    ) {
        assert_eq!(node_size_score(&ast, child, None), 0.0);
    }
}

/// Always-true oracle: both bodies fully deleted (P3 finds cycles); result bit true, artifact valid.
#[test]
fn nodeshrink_pass_true_oracle_reduces() {
    let dir = temp_dir("true");
    let input = dir.join("in.wasm");
    let output = dir.join("out.wasm");
    let snap = two_func_snapshot();
    std::fs::write(&input, snap.encode_to_bytes().unwrap()).unwrap();
    let oracle = Oracle::new("/bin/true");
    let mut pass = NodeShrinkPass::new(
        &oracle,
        &dir.join("work"),
        None,
        DdFactory::default(),
        false,
        false,
    );
    let result = pass
        .reduce(&input, &output, 20.0)
        .expect("reduce ok");
    assert!(e2wr_pass::instseq::validate_wasm_bytes(&std::fs::read(&output).unwrap()));
    assert!(result.reduced_size_num.unwrap() > 0, "bytes shrunk");
    assert_eq!(body_inst_num_of(&output), 0, "both bodies fully reduced");
}

/// Always-false oracle: zero reduction (strip not adopted, all probes rejected).
#[test]
fn nodeshrink_pass_false_oracle_zero() {
    let dir = temp_dir("false");
    let input = dir.join("in.wasm");
    let output = dir.join("out.wasm");
    let snap = two_func_snapshot();
    std::fs::write(&input, snap.encode_to_bytes().unwrap()).unwrap();
    let oracle = Oracle::new("/bin/false");
    let mut pass = NodeShrinkPass::new(
        &oracle,
        &dir.join("work"),
        None,
        DdFactory::default(),
        false,
        false,
    );
    let result = pass.reduce(&input, &output, 5.0).expect("reduce ok");
    assert_eq!(result.reduced_size_num, Some(0));
    assert_eq!(body_inst_num_of(&output), 4, "both bodies intact");
    assert_eq!(
        std::fs::read(&output).unwrap(),
        std::fs::read(&input).unwrap()
    );
}

/// P-5 spot check → exception recovery branch 3. Call order (probe-measured): strip(1), task1 probe(2),
/// task1 spot check(3, an even count — not triggered), task2 probe(4), task2 spot check(5). K=4: the first four
/// pass, task2's spot check fails (success_times=1, odd) → recovery; the last_pass_case re-verification
/// (6) also fails → early_return → the tail copies last_pass_case as the output.
#[test]
fn nodeshrink_pass_audit_recovery_branch3() {
    let dir = temp_dir("audit");
    let input = dir.join("in.wasm");
    let output = dir.join("out.wasm");
    let snap = two_func_snapshot();
    std::fs::write(&input, snap.encode_to_bytes().unwrap()).unwrap();
    let (oracle, _cnt) = counting_oracle(&dir, 4);
    let mut pass = NodeShrinkPass::new(
        &oracle,
        &dir.join("work"),
        None,
        DdFactory::default(),
        false,
        false,
    );
    let result = pass.reduce(&input, &output, 30.0).expect("reduce ok");
    // The spot check fires before success processing (Python's order): recovery rolls **the second commit** back to
    // last_pass_case (holding only the first commit) — the recovery path's key behavior.
    assert!(e2wr_pass::instseq::validate_wasm_bytes(&std::fs::read(&output).unwrap()));
    assert_eq!(body_inst_num_of(&output), 2, "second commit rolled back");
    assert!(result.reduced_size_num.unwrap() > 0);
    // Branch-3 early termination: the 30-second budget should not be exhausted.
    assert!(!result.is_partial_by_timeout, "early_return ends before timeout");
}

/// The first 2 calls pass (strip + the first task): the second task's probe is rejected, no second success →
/// no spot check triggered; the output keeps the first commit.
#[test]
fn nodeshrink_pass_partial_accept() {
    let dir = temp_dir("partial");
    let input = dir.join("in.wasm");
    let output = dir.join("out.wasm");
    let snap = two_func_snapshot();
    std::fs::write(&input, snap.encode_to_bytes().unwrap()).unwrap();
    let (oracle, _cnt) = counting_oracle(&dir, 2);
    let mut pass = NodeShrinkPass::new(
        &oracle,
        &dir.join("work"),
        None,
        DdFactory::default(),
        false,
        false,
    );
    let result = pass.reduce(&input, &output, 6.0).expect("reduce ok");
    assert!(e2wr_pass::instseq::validate_wasm_bytes(&std::fs::read(&output).unwrap()));
    assert_eq!(body_inst_num_of(&output), 2, "one body reduced, one intact");
    assert!(result.reduced_size_num.unwrap() > 0);
}
