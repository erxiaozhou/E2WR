//! M6.1 synthetic unit tests: tree shape, get_insts reconstruction, lengths, position recomputation, mutation primitives.
//!
//! Small hand-built modules (corpus-free), covering block/loop/if-else/nesting/terminal-instruction
//! splitting/post-build upgrade/position recomputation. Coordinate space = the Python convention (function-level end excluded).
//!
//! Note: Python's post-processing `convert_to_insts_node_with_type` keys on the child count
//! being 1 (not the instruction count); an else list with a single instruction-sequence child is upgraded too.

use std::sync::atomic::{AtomicUsize, Ordering};

use e2wr_ir::ast::{Ast, ListRole, NodeId, NodeKind};
use e2wr_ir::decode::decode_bytes;
use e2wr_ir::module::Module;
use e2wr_ir::types::{FTy, ValTy};
use e2wr_ir::Inst;

static SEQ: AtomicUsize = AtomicUsize::new(0);

/// Build a module from wat text and decode it (less error-prone than assembling a Module by hand).
fn parse(wat: &[u8]) -> Module {
    let n = SEQ.fetch_add(1, Ordering::SeqCst);
    let dir = std::env::temp_dir().join(format!("e2wr-ir-ast-syn-{}-{n}", std::process::id()));
    let _ = std::fs::remove_dir_all(&dir);
    std::fs::create_dir_all(&dir).unwrap();
    let wat_path = dir.join("a.wat");
    let wasm_path = dir.join("a.wasm");
    std::fs::write(&wat_path, wat).unwrap();
    let out = std::process::Command::new("wasm-tools")
        .arg("wat2wasm")
        .arg(&wat_path)
        .arg("-o")
        .arg(&wasm_path)
        .output()
        .expect("wasm-tools wat2wasm");
    assert!(
        out.status.success(),
        "wat2wasm failed: {}",
        String::from_utf8_lossy(&out.stderr)
    );
    decode_bytes(&std::fs::read(&wasm_path).unwrap()).unwrap()
}

/// Collect (NodeId, kind description, func_idx, inst_idx) via traversal.
fn dump(ast: &Ast, root: NodeId) -> Vec<(u32, String, u32, u32)> {
    ast.traverse_collect(root, &mut |id| {
        let n = ast.node(id);
        let kind = match &n.kind {
            NodeKind::Insts { insts, ty } => format!(
                "insts[{}]{}",
                insts.len(),
                if ty.is_some() { "+ty" } else { "" }
            ),
            NodeKind::List { role, children, ty } => format!(
                "list-{:?}[{}]{}",
                role,
                children.len(),
                if ty.is_some() { "+ty" } else { "" }
            ),
            NodeKind::Block { .. } => "block".into(),
            NodeKind::Loop { .. } => "loop".into(),
            NodeKind::If { has_else, .. } => format!("if{}", if *has_else { "+else" } else { "" }),
        };
        Some((id.0, kind, n.loc.func_idx, n.loc.inst_idx))
    })
}

fn strip_ids(v: &[(u32, String, u32, u32)]) -> Vec<(String, u32, u32)> {
    v.iter().map(|(_, k, f, p)| (k.clone(), *f, *p)).collect()
}

#[test]
fn flat_function_single_insts_node() {
    let m = parse(b"(module (func (export \"f\") (result i32) i32.const 1 i32.const 2 i32.add))");
    let ast = Ast::from_module(&m).unwrap();
    let root = ast.root_of_func(0);
    // Post-build processing: the root list has a single plain Insts child → upgraded to the typed flavor.
    assert_eq!(
        strip_ids(&dump(&ast, root)),
        vec![
            ("list-Root[1]+ty".into(), 0, 0),
            ("insts[3]+ty".into(), 0, 0),
        ]
    );
    // NodeId 1 = the plain Insts node from tree building; the upgraded node 2 is allocated in post-processing.
    assert_eq!(ast.node_count(), 3);
    let insts = ast.get_insts(root);
    assert_eq!(
        insts,
        vec![
            Inst::I32Const { value: 1 },
            Inst::I32Const { value: 2 },
            Inst::I32Add
        ]
    );
    assert_eq!(ast.get_length(root), 3);
    assert_eq!(ast.block_ty(root), Some(&FTy::of(&[], &[ValTy::I32])));
}

#[test]
fn nested_block_loop_if() {
    let wat = br#"
(module (func (export "f") (param i32) (result i32)
  block (result i32)
    loop
      local.get 0
      if (result i32)
        i32.const 10
      else
        i32.const 20
        return
      end
      br 1
    end
    unreachable
  end))"#;
    let m = parse(wat);
    let ast = Ast::from_module(&m).unwrap();
    let root = ast.root_of_func(0);
    let d = dump(&ast, root);

    // Instruction ordinals (Python coordinates, function-level end dropped):
    // block(0) loop(1) local.get(2) if(3) const10(4) else(5)
    // const20(6) return(7) end(8) br(9) end(10) unreachable(11) [end(12)]
    // Allocation order (aligned with Python node_id): body lists before block nodes; if: then before else.
    assert_eq!(
        strip_ids(&d),
        vec![
            ("list-Root[1]+ty".into(), 0, 0),   // 0 root
            ("block".into(), 0, 0),             // 2
            ("list-Plain[2]+ty".into(), 0, 1),  // 1 block body
            ("loop".into(), 0, 1),              // 4
            ("list-Plain[3]+ty".into(), 0, 2),  // 3 loop body (local.get/if/br as three children)
            ("insts[1]".into(), 0, 2),          // 5 local.get (multi-child list, no upgrade)
            ("if+else".into(), 0, 3),           // 8
            ("list-Plain[1]+ty".into(), 0, 4),  // 6 then
            ("insts[1]+ty".into(), 0, 4),       // 13 const10 (upgraded)
            ("list-Else[1]+ty".into(), 0, 6),   // 7 else
            ("insts[2]+ty".into(), 0, 6),       // 14 const20+return (single child, upgraded)
            ("insts[1]".into(), 0, 9),          // 11 br 1 (terminal instruction stands alone)
            ("insts[1]".into(), 0, 11),         // 12 unreachable
        ],
        "tree dump: {d:?}"
    );

    // get_insts reconstruction matches the original sequence (minus the function-level end) (M6 acceptance line 1).
    let rebuilt = ast.get_insts(root);
    let orig = &m.defined_funcs[0].insts[..m.defined_funcs[0].insts.len() - 1];
    assert_eq!(&rebuilt, orig);
    assert_eq!(ast.get_length(root) as usize, orig.len());
    assert_eq!(ast.block_ty(root), Some(&FTy::of(&[], &[ValTy::I32])));
}

#[test]
fn if_without_else() {
    let wat = br#"
(module (func (export "f")
  i32.const 1
  if
    nop
  end
  drop))"#;
    let m = parse(wat);
    let ast = Ast::from_module(&m).unwrap();
    let root = ast.root_of_func(0);
    assert_eq!(
        strip_ids(&dump(&ast, root)),
        vec![
            ("list-Root[3]+ty".into(), 0, 0),
            ("insts[1]".into(), 0, 0),
            ("if".into(), 0, 1),
            ("list-Plain[1]+ty".into(), 0, 2),
            ("insts[1]+ty".into(), 0, 2),
            ("insts[1]".into(), 0, 4),
        ]
    );
    let rebuilt = ast.get_insts(root);
    let orig = &m.defined_funcs[0].insts[..m.defined_funcs[0].insts.len() - 1];
    assert_eq!(&rebuilt, orig);
}

#[test]
fn terminal_insts_merge_with_preceding() {
    // return right after plain instructions → the same Insts node (Python's aggregation rule).
    let m = parse(b"(module (func (export \"f\") local.get 0 return))");
    let ast = Ast::from_module(&m).unwrap();
    let root = ast.root_of_func(0);
    assert_eq!(
        strip_ids(&dump(&ast, root)),
        vec![
            ("list-Root[1]+ty".into(), 0, 0),
            ("insts[2]+ty".into(), 0, 0),
        ]
    );
}

#[test]
fn update_loc_info_identity_and_recompute() {
    let wat = br#"
(module (func (export "f") (param i32) (result i32)
  block (result i32)
    loop
      local.get 0
      if (result i32)
        i32.const 10
      else
        i32.const 20
        return
      end
      br 1
    end
    unreachable
  end))"#;
    let m = parse(wat);
    let mut ast = Ast::from_module(&m).unwrap();
    let root = ast.root_of_func(0);
    let before = dump(&ast, root);

    // Unmodified tree → position recomputation is the identity (M6 acceptance line 2).
    ast.update_loc_info(0);
    assert_eq!(before, dump(&ast, root));

    // Recompute after modification: empty the if's then body; positions shift by length and match an independent rebuild.
    let d0 = dump(&ast, root);
    let then_list = d0
        .iter()
        .find(|(id, kind, _, _)| {
            kind == "list-Plain[1]+ty"
                && matches!(&ast.node(NodeId(*id)).kind, NodeKind::List { role: ListRole::Plain, children, .. } if children.len() == 1)
        })
        .map(|(id, _, _, _)| NodeId(*id))
        .expect("then list");
    ast.replace_split_with_new_sub_nodes(then_list, 0, 1, vec![]);
    let rebuilt = ast.get_insts(root);
    assert_eq!(rebuilt.len(), 12);

    ast.update_loc_info(0);
    let d1 = dump(&ast, root);
    let else_pos = d1
        .iter()
        .find(|(_, k, _, _)| k == "list-Else[1]+ty")
        .map(|x| x.3)
        .unwrap();
    assert_eq!(else_pos, 5, "else body must shift left by 1 after removing one then inst");

    // Matches an independent rebuild (shape and positions; NodeId allocation order naturally differs, excluded from comparison).
    let mut m2 = m.clone();
    let mut new_insts = rebuilt.clone();
    new_insts.push(Inst::End);
    m2.defined_funcs[0].insts = new_insts;
    let ast2 = Ast::from_module(&m2).unwrap();
    let root2 = ast2.root_of_func(0);
    assert_eq!(strip_ids(&d1), strip_ids(&dump(&ast2, root2)));
}

#[test]
fn ancestor_and_parent_queries() {
    let wat = br#"
(module (func (export "f")
  i32.const 1
  if
    nop
  end
  drop))"#;
    let m = parse(wat);
    let ast = Ast::from_module(&m).unwrap();
    let root = ast.root_of_func(0);
    assert!(ast.is_ancestor_of(root, root));
    let if_id = ast
        .traverse_collect(root, &mut |id| {
            matches!(ast.node(id).kind, NodeKind::If { .. }).then_some(id)
        })
        .pop()
        .unwrap();
    assert!(ast.is_ancestor_of(root, if_id));
    assert!(!ast.is_ancestor_of(if_id, root));
    assert_eq!(ast.node(if_id).parent, Some(root));

    // else_list_mark side effect: after access has_else is set and the length counts the else body.
    let before_len = ast.get_length(if_id);
    let mut ast_mut = ast.clone();
    let els = ast_mut.else_list_mark(if_id);
    assert!(matches!(
        ast_mut.node(els).kind,
        NodeKind::List { role: ListRole::Else, .. }
    ));
    assert!(ast_mut.get_length(if_id) > before_len);
}

#[test]
fn empty_function() {
    let m = parse(b"(module (func (export \"f\")))");
    let ast = Ast::from_module(&m).unwrap();
    let root = ast.root_of_func(0);
    assert_eq!(ast.get_length(root), 0);
    assert_eq!(ast.get_insts(root), vec![]);
}

#[test]
fn block_type_variants() {
    // Index-form block type (multiple results).
    let wat = br#"
(module (type $t (func (result i32 i32)))
 (func (export "f") (result i32)
  block (type $t)
    i32.const 1
    i32.const 2
  end
  i32.add))"#;
    let m = parse(wat);
    let ast = Ast::from_module(&m).unwrap();
    let root = ast.root_of_func(0);
    let blk = ast
        .traverse_collect(root, &mut |id| {
            matches!(ast.node(id).kind, NodeKind::Block { .. }).then_some(id)
        })
        .pop()
        .unwrap();
    match &ast.node(blk).kind {
        NodeKind::Block { blockty: wasmparser::BlockType::FuncType(_), ty, .. } => {
            assert_eq!(ty.as_ref().unwrap(), &FTy::of(&[], &[ValTy::I32, ValTy::I32]));
        }
        k => panic!("expected FuncType block: {k:?}"),
    }
    let rebuilt = ast.get_insts(root);
    let orig = &m.defined_funcs[0].insts[..m.defined_funcs[0].insts.len() - 1];
    assert_eq!(&rebuilt, orig);
}

#[test]
fn get_type_req_variants() {
    let wat = br#"
(module (func (export "f") (param i32) (result i32)
  local.get 0
  if (result i32)
    i32.const 10
  else
    i32.const 20
  end
  drop
  i32.const 0))"#;
    let m = parse(wat);
    let ast = Ast::from_module(&m).unwrap();
    let root = ast.root_of_func(0);
    // The if's type requirement = params + i32 condition → results.
    let if_id = ast
        .traverse_collect(root, &mut |id| {
            matches!(ast.node(id).kind, NodeKind::If { .. }).then_some(id)
        })
        .pop()
        .unwrap();
    let tr = ast.get_type_req(if_id).unwrap();
    // Python: block-type params + ['i32'] (the condition operand); this example's block type has no params.
    assert_eq!(tr.ty0(), &FTy::of(&[ValTy::I32], &[ValTy::I32]));
    // Plain Insts nodes have no static type (None = Python's raise signal).
    let plain = ast
        .traverse_collect(root, &mut |id| {
            matches!(ast.node(id).kind, NodeKind::Insts { ty: None, .. }).then_some(id)
        })
        .pop()
        .expect("multi-child list keeps a plain insts node");
    assert!(ast.get_type_req(plain).is_none());
}

#[test]
fn unbalanced_bodies_rejected() {
    // Hand-built invalid function bodies (missing end / extra end) — from_module must error.
    let mut m = Module::default();
    m.types.push(e2wr_ir::module::FuncType { params: vec![], results: vec![] });
    m.defined_func_ty_ids.push(0);
    m.defined_funcs.push(e2wr_ir::module::Func {
        ty_idx: 0,
        locals: vec![],
        insts: vec![Inst::I32Const { value: 1 }],
    });
    assert!(Ast::from_module(&m).is_err(), "body without trailing end must fail");
}
