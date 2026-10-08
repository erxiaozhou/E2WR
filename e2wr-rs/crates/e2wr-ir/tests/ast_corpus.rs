//! M6.2 corpus regression: over all decodable wasm in tt/ + benchmark RQ12/RQ3:
//! 1. per-function tree building succeeds (block/loop/if/else balancing checked during the build);
//! 2. `get_insts()` reconstruction equals the original instruction sequence (function-level trailing end excluded);
//! 3. `update_loc_info` identity: unmodified tree recomputes to the same positions.

use std::path::PathBuf;

use e2wr_ir::ast::{Ast, NodeId};
use e2wr_ir::decode::decode_bytes;

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

fn locs_snapshot(ast: &Ast) -> Vec<(NodeId, (u32, u32))> {
    let mut out = Vec::new();
    for fi in 0..ast.func_roots.len() {
        let root = ast.root_of_func(fi);
        ast.traverse_pre(root, &mut |id| {
            let n = ast.node(id);
            out.push((id, (n.loc.func_idx, n.loc.inst_idx)));
        });
    }
    out
}

#[test]
fn corpus_build_roundtrip_and_loc_identity() {
    let files = corpus();
    let mut total_funcs = 0usize;
    let mut total_nodes = 0usize;
    for f in &files {
        let bytes = std::fs::read(f).unwrap();
        let module = match decode_bytes(&bytes) {
            Ok(m) => m,
            Err(_) => continue, // decode-failure baseline (covered by other M2 tests); skip
        };
        let mut ast = match Ast::from_module(&module) {
            Ok(a) => a,
            Err(e) => panic!("build ast failed on {}: {e}", f.display()),
        };
        for fi in 0..module.defined_funcs.len() {
            let root = ast.root_of_func(fi);
            // Acceptance line 1: reconstruction equals the original (function-level end excluded).
            let func = &module.defined_funcs[fi];
            let expect = &func.insts[..func.insts.len() - 1];
            let rebuilt = ast.get_insts(root);
            assert_eq!(
                &rebuilt, expect,
                "get_insts mismatch: {} func {fi}",
                f.display()
            );
            assert_eq!(ast.get_length(root) as usize, expect.len());
            total_funcs += 1;
        }
        // Acceptance line 2: position recomputation is the identity.
        let before = locs_snapshot(&ast);
        for fi in 0..module.defined_funcs.len() {
            ast.update_loc_info(fi);
        }
        assert_eq!(before, locs_snapshot(&ast), "loc identity: {}", f.display());
        total_nodes += ast.node_count();
    }
    eprintln!("corpus ast: {} files, {total_funcs} funcs, {total_nodes} nodes", files.len());
    assert!(total_funcs > 100, "corpus unexpectedly small");
}
