//! Abstract syntax tree layer (M6.1): node hierarchy, tree building, read-only queries, mutation primitives, position recomputation.
//!
//! Mirrors Python `reduction_analysis/ASTInfo/AST.py`.
//!
//! Structural differences (D-4 implementation freedom + the M6 survey):
//! - Python uses inheritance polymorphism (ASTINode/InstsNode/InstsNodeWithType/NodeList/
//!   ElseNode/RootNode/BlockNode/LoopNode/IfNode) + object identity + a global monotonic
//!   `node_id` + parent pointers; Rust uses an arena (`Vec<AstNode>`) + `NodeId` indices.
//!   Allocation order matches Python exactly (list first then the block node; for if: the then list, then the else
//!   list, then the if node; instruction-sequence nodes allocated at flush), so NodeIds correspond
//!   one-to-one with Python's node_ids of a single tree build, usable by parity tests.
//! - Instruction-sequence nodes own a `Vec<Inst>` copy; block nodes additionally keep the raw
//!   `wasmparser::BlockType` (Python's `get_insts` rebuilds block instructions from funcType
//!   and loses the original encoding form, and its Blocktype even raises on re-encoding the
//!   funcType form; Rust keeps the original form so `get_insts` reconstruction matches instruction by instruction).
//! - Python `wasmFunc.insts` excludes the function-level trailing `end`, Rust `Func.insts` includes it;
//!   tree building consumes `&insts[..len-1]` (the tail must be `End`), so the coordinate space matches
//!   Python (no function-level end).
//!
//! Not ported (M6 redundancy survey):
//! `find_nodes_by_predicate`, `print_ast_structure`, `replace_sub_node`,
//! `replace_all_sub_nodes`, `is_leaf`, `InstsNode.__bool__`,
//! `NodeList.is_empty`, `_original_id`, the post-order branch of `traverse_ast` and its
//! `pre_order` parameter (every call site is pre-order), the dead `root_node_type`
//! parameter of `func2AST`/`insts2AST`, the defensive try/except of
//! `NodeList.get_length`/`BlockNode.get_length` (no raise point in the get_length chain), debug print, the empty base ASTNode.
//!
//! Semantics kept:
//! - The raise branch of `InstsNode.get_type_req` is not dead code — Python relies on the exception
//!   being caught by `get_node_type_req` to divert into the inference path; Rust's `get_type_req` returns
//!   `None` to mean "no static type, needs inference" (including the case where the list merge branch
//!   meets an untyped child and Python would raise upward — all collapsed into returning None).
//! - The child-requirement merge branch of `NodeList.get_type_req` (when block_type=None)
//!   is kept: every list of regular tree building is typed, but M10's NodeReplacement rebuilds
//!   lists untyped, so the branch is reachable.
//! - The side effect of the `IfNode.else_sub_node_list` property (accessing sets
//!   `has_else=True`, affecting get_insts/get_length/get_sub_nodes): expressed explicitly
//!   via `else_list_mark`; every call site that Python reaches through the property
//!   maps to that method.

use crate::inst_gen::Inst;
use crate::module::Module;
use crate::types::{merge, FTy, TR, ValTy};
use anyhow::{bail, Result};

/// Node location (Python `ASTNodeLoc`; the coordinate space excludes the function-level trailing end).
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, PartialOrd, Ord)]
pub struct NodeLoc {
    pub func_idx: u32,
    pub inst_idx: u32,
}

/// Node identity: arena index, allocated in creation order (matching Python's node_id of a single tree build).
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, PartialOrd, Ord)]
pub struct NodeId(pub u32);

/// List node role: plain list / else list / function root (Python NodeList/ElseNode/RootNode).
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum ListRole {
    Plain,
    Else,
    Root,
}

/// Node kinds.
#[derive(Debug, Clone)]
pub enum NodeKind {
    /// Instruction-sequence leaf. `ty=Some` maps to `InstsNodeWithType` (upgraded by
    /// post-build processing or built explicitly), `None` to plain `InstsNode` (no static type).
    Insts {
        insts: Vec<Inst>,
        ty: Option<FTy>,
    },
    /// A list node (NodeList/ElseNode/RootNode). `ty` is Python's `block_type`.
    List {
        role: ListRole,
        children: Vec<NodeId>,
        ty: Option<FTy>,
    },
    Block {
        /// The raw block type (guarantees get_insts reconstruction matches the original instructions).
        blockty: wasmparser::BlockType,
        /// The resolved concrete type (Python `block_type`, from concrete_type).
        ty: Option<FTy>,
        body: NodeId,
    },
    Loop {
        blockty: wasmparser::BlockType,
        ty: Option<FTy>,
        body: NodeId,
    },
    If {
        blockty: wasmparser::BlockType,
        ty: Option<FTy>,
        then: NodeId,
        els: NodeId,
        has_else: bool,
    },
}

/// An arena node (the field part of Python `ASTINode`).
#[derive(Debug, Clone)]
pub struct AstNode {
    pub kind: NodeKind,
    pub loc: NodeLoc,
    pub parent: Option<NodeId>,
}

/// A per-module forest of syntax trees: one root tree per defined function.
///
/// Mirrors `ASTInfo.from_parser` (`raw_ast: dict[func_idx, root]`);
/// the dead `func_idxs` parameter is not ported.
#[derive(Debug, Clone)]
pub struct Ast {
    pub(crate) nodes: Vec<AstNode>,
    pub func_roots: Vec<NodeId>,
}

/// Terminal instructions: trigger an immediate flush (the branch set of Python insts2AST).
fn is_terminal_op(inst: &Inst) -> bool {
    matches!(
        inst,
        Inst::Br { .. } | Inst::BrIf { .. } | Inst::BrTable(_) | Inst::Return | Inst::Unreachable
    )
}

fn blockty_to_fty(module: &Module, blockty: &wasmparser::BlockType) -> Result<FTy> {
    use wasmparser::BlockType;
    match blockty {
        BlockType::Empty => Ok(FTy::of(&[], &[])),
        BlockType::Type(v) => {
            let ty = ValTy::from_wasmparser(*v)
                .ok_or_else(|| anyhow::anyhow!("unsupported block result type {v:?}"))?;
            Ok(FTy::of(&[], &[ty]))
        }
        BlockType::FuncType(idx) => {
            let ft = module
                .types
                .get(*idx as usize)
                .ok_or_else(|| anyhow::anyhow!("block type index {idx} out of range"))?;
            let params: Option<Vec<ValTy>> = ft.params.iter().map(|t| ValTy::from_wasmparser(*t)).collect();
            let results: Option<Vec<ValTy>> =
                ft.results.iter().map(|t| ValTy::from_wasmparser(*t)).collect();
            match (params, results) {
                (Some(p), Some(r)) => Ok(FTy::of(&p, &r)),
                _ => bail!("unsupported value type in block type {idx}"),
            }
        }
    }
}

struct Builder<'a> {
    module: &'a Module,
    nodes: Vec<AstNode>,
}

/// Python `IfNode.else_sub_node_list` property: **accessing sets has_else=True**
/// (side-effect semantics); returns the else list.
fn else_list_mark(nodes: &mut [AstNode], if_node: NodeId) -> NodeId {
    match &mut nodes[if_node.0 as usize].kind {
        NodeKind::If { els, has_else, .. } => {
            *has_else = true;
            *els
        }
        _ => panic!("else_list_mark on non-if node"),
    }
}

/// Python `IfNode.set_else_start_idx`.
fn set_else_start_idx(nodes: &mut [AstNode], if_node: NodeId, inst_idx: u32) {
    let els = match &nodes[if_node.0 as usize].kind {
        NodeKind::If { els, .. } => *els,
        _ => panic!("set_else_start_idx on non-if node"),
    };
    nodes[els.0 as usize].loc.inst_idx = inst_idx;
}

impl<'a> Builder<'a> {
    fn alloc(&mut self, kind: NodeKind, loc: NodeLoc) -> NodeId {
        self.nodes.push(AstNode { kind, loc, parent: None });
        NodeId(self.nodes.len() as u32 - 1)
    }

    fn alloc_list(&mut self, role: ListRole, children: Vec<NodeId>, ty: Option<FTy>, loc: NodeLoc) -> NodeId {
        self.alloc(NodeKind::List { role, children, ty }, loc)
    }

    /// Python `NodeList.append`: append and set the parent pointer.
    fn append_child(&mut self, list: NodeId, child: NodeId) {
        if let NodeKind::List { children, .. } = &mut self.nodes[list.0 as usize].kind {
            children.push(child);
        }
        self.nodes[child.0 as usize].parent = Some(list);
    }

    /// Flush accumulated plain instructions into one Insts node attached to the current list
    /// (Python `process_insts_in_frame`; an empty sequence creates no node).
    fn flush_insts(&mut self, cur_insts: &mut Vec<Inst>, cur_list: NodeId, start_idx: u32) {
        if cur_insts.is_empty() {
            return;
        }
        let insts = std::mem::take(cur_insts);
        let node = self.alloc(
            NodeKind::Insts { insts, ty: None },
            NodeLoc { func_idx: self.nodes[cur_list.0 as usize].loc.func_idx, inst_idx: start_idx },
        );
        self.append_child(cur_list, node);
    }
}

impl Ast {
    /// Mirrors Python `ASTInfo.from_parser`: build the tree per function (func2AST).
    pub fn from_module(module: &Module) -> Result<Ast> {
        let mut builder = Builder { module, nodes: Vec::new() };
        let mut func_roots = Vec::with_capacity(module.defined_funcs.len());
        for (func_idx, func) in module.defined_funcs.iter().enumerate() {
            let func_idx = func_idx as u32;
            // Rust `Func.insts` includes the function-level trailing End (Python's does not); tree building drops it.
            if func.insts.last() != Some(&Inst::End) {
                bail!("func {func_idx} body does not end with `end`");
            }
            let body = &func.insts[..func.insts.len() - 1];
            let ft = module
                .types
                .get(func.ty_idx as usize)
                .ok_or_else(|| anyhow::anyhow!("func type index {} out of range", func.ty_idx))?;
            let results: Option<Vec<ValTy>> =
                ft.results.iter().map(|t| ValTy::from_wasmparser(*t)).collect();
            let results = results.ok_or_else(|| anyhow::anyhow!("unsupported func result type"))?;
            // Root type = [] → the function's result types (given_type in Python func2AST).
            let given_ty = FTy::of(&[], &results);
            let root = build_insts(&mut builder, body, given_ty, func_idx)?;
            // Post-processing runs right after this function's build (Python calls it per function at the
            // end of insts2AST, so upgraded-node ids interleave with later functions' build ids; the order must match).
            upgrade_single_insts_children(&mut builder.nodes, root);
            func_roots.push(root);
        }
        Ok(Ast { nodes: builder.nodes, func_roots })
    }

    pub fn node(&self, id: NodeId) -> &AstNode {
        &self.nodes[id.0 as usize]
    }

    /// Python `insts2AST` (with the trailing single-child type-upgrade traversal) appends onto
    /// the existing arena: returns (temporary root, the root's children). Used by `update_ast_nodes` to
    /// rebuild list children — new node ids continue after existing nodes, all original ids stay stable.
    /// The temporary root has role Root and type given_ty (Python callers pass root_node_type=
    /// NodeList; the role takes no part in child extraction or upgrade decisions).
    pub fn append_insts_tree(
        &mut self,
        module: &Module,
        insts: &[Inst],
        given_ty: FTy,
        func_idx: u32,
    ) -> Result<(NodeId, Vec<NodeId>)> {
        let mut b =
            Builder { module, nodes: std::mem::take(&mut self.nodes) };
        let root = build_insts(&mut b, insts, given_ty, func_idx)?;
        upgrade_single_insts_children(&mut b.nodes, root);
        let children = match &b.nodes[root.0 as usize].kind {
            NodeKind::List { children, .. } => children.clone(),
            _ => unreachable!("build_insts root is a list"),
        };
        self.nodes = b.nodes;
        Ok((root, children))
    }

    pub fn node_mut(&mut self, id: NodeId) -> &mut AstNode {
        &mut self.nodes[id.0 as usize]
    }

    /// Function root (Python `ASTInfo.get_func_root_ast`; out-of-range index panics, matching
    /// Python's KeyError on dict access).
    pub fn root_of_func(&self, func_idx: usize) -> NodeId {
        self.func_roots[func_idx]
    }

    /// Test-only (R-15): zero production callers; used by tests to count nodes.
    pub fn node_count(&self) -> usize {
        self.nodes.len()
    }

    /// Children of a list-role node (Python `get_sub_nodes`):
    /// Insts→[], List→children, Block/Loop→[body], If→[then] (with else when present).
    pub fn sub_nodes(&self, id: NodeId) -> Vec<NodeId> {
        match &self.node(id).kind {
            NodeKind::Insts { .. } => vec![],
            NodeKind::List { children, .. } => children.clone(),
            NodeKind::Block { body, .. } | NodeKind::Loop { body, .. } => vec![*body],
            NodeKind::If { then, els, has_else, .. } => {
                if *has_else {
                    vec![*then, *els]
                } else {
                    vec![*then]
                }
            }
        }
    }

    /// Type of a block/list node (Python `get_block_type`; None corresponds to Python's
    /// raise NotImplementedError('block_type is None').
    pub fn block_ty(&self, id: NodeId) -> Option<&FTy> {
        match &self.node(id).kind {
            NodeKind::Insts { ty, .. } => ty.as_ref(),
            NodeKind::List { ty, .. } => ty.as_ref(),
            NodeKind::Block { ty, .. } | NodeKind::Loop { ty, .. } | NodeKind::If { ty, .. } => {
                ty.as_ref()
            }
        }
    }

    /// Python `AST.get_node_list_type_in_ast_practical`: the root list takes its own block type
    /// (`([], function results)`), other lists take the parent node's block type (parent being Block/Loop/If
    /// or the root list). Python's `block_type is None` raise maps to None → error here.
    pub fn get_node_list_type_in_ast_practical(&self, node_list: NodeId) -> Result<FTy> {
        let src = match &self.node(node_list).kind {
            NodeKind::List { role: ListRole::Root, .. } => node_list,
            _ => self
                .node(node_list)
                .parent
                .ok_or_else(|| anyhow::anyhow!("node list {node_list:?} has no parent"))?,
        };
        self.block_ty(src)
            .cloned()
            .ok_or_else(|| anyhow::anyhow!("node list {node_list:?} type source has no block type"))
    }

    /// Python `get_length`: the number of instructions a node covers (block head/tail included).
    pub fn get_length(&self, id: NodeId) -> u32 {
        match &self.node(id).kind {
            NodeKind::Insts { insts, .. } => insts.len() as u32,
            NodeKind::List { children, .. } => children.iter().map(|c| self.get_length(*c)).sum(),
            NodeKind::Block { body, .. } | NodeKind::Loop { body, .. } => {
                2 + self.get_length(*body)
            }
            NodeKind::If { then, els, has_else, .. } => {
                if *has_else {
                    3 + self.get_length(*then) + self.get_length(*els)
                } else {
                    2 + self.get_length(*then)
                }
            }
        }
    }

    /// Python `get_insts`: reconstructs the linear instruction sequence a node covers.
    /// Block head/tail instructions are rebuilt from the saved raw BlockType (more faithful than
    /// Python, which rebuilds from funcType, losing the original encoding form, and the funcType form cannot be re-encoded).
    pub fn get_insts(&self, id: NodeId) -> Vec<Inst> {
        match &self.node(id).kind {
            NodeKind::Insts { insts, .. } => insts.clone(),
            NodeKind::List { children, .. } => {
                let mut out = Vec::new();
                for c in children {
                    out.extend(self.get_insts(*c));
                }
                out
            }
            NodeKind::Block { blockty, body, .. } => {
                let mut out = vec![Inst::Block { blockty: *blockty }];
                out.extend(self.get_insts(*body));
                out.push(Inst::End);
                out
            }
            NodeKind::Loop { blockty, body, .. } => {
                let mut out = vec![Inst::Loop { blockty: *blockty }];
                out.extend(self.get_insts(*body));
                out.push(Inst::End);
                out
            }
            NodeKind::If { blockty, then, els, has_else, .. } => {
                let mut out = vec![Inst::If { blockty: *blockty }];
                out.extend(self.get_insts(*then));
                if *has_else {
                    out.push(Inst::Else);
                    out.extend(self.get_insts(*els));
                }
                out.push(Inst::End);
                out
            }
        }
    }

    /// The node's static stack requirement (Python `get_type_req`).
    /// `None` = no static type (a plain Insts node, or the merge branch of an untyped list meeting
    /// an untyped child) — matching the semantics of Python's raise being caught by `get_node_type_req`
    /// and diverted to the inference path. The context parameter of the Python signature has no
    /// effect at any live call site (removed per R-9).
    pub fn get_type_req(&self, id: NodeId) -> Option<TR> {
        match &self.node(id).kind {
            NodeKind::Insts { ty, .. } => ty.as_ref().map(|t| TR::new([t.clone()])),
            NodeKind::List { children, ty, .. } => match ty {
                Some(t) => Some(TR::new([t.clone()])),
                None => {
                    // Python merge branch: empty child list → empty type.
                    let mut acc = TR::new([FTy::of(&[], &[])]);
                    for c in children {
                        let r = self.get_type_req(*c)?;
                        acc = merge(&acc, &r);
                    }
                    Some(acc)
                }
            },
            NodeKind::Block { ty, .. } | NodeKind::Loop { ty, .. } => {
                ty.as_ref().map(|t| TR::new([t.clone()]))
            }
            NodeKind::If { ty, .. } => ty.as_ref().map(|t| {
                // The i32 condition operand sits atop the params (Python IfNode.get_type_req).
                let mut params = t.params.clone();
                params.push(ValTy::I32);
                TR::new([FTy::of(&params, &t.results)])
            }),
        }
    }

    /// Pre-order traversal (Python `traverse_ast` specialized to pre-order: visit the node first, then
    /// recurse into children in `sub_nodes` order; replacing children while visiting is kept consistent
    /// by `upgrade_single_insts_children`'s call order).
    /// Test-only (R-15): production traversals use `traverse_collect` or dedicated recursion.
    pub fn traverse_pre<F: FnMut(NodeId)>(&self, root: NodeId, f: &mut F) {
        f(root);
        for c in self.sub_nodes(root) {
            self.traverse_pre(c, f);
        }
    }

    /// Collecting pre-order traversal (Python `traverse_ast(..., collect_results=True)`;
    /// yields the entry result when the visit returns Some). Test-only (R-15):
    /// zero external production callers (nodeshrink collecting uses the dedicated recursion collect_non_empty_preorder).
    pub fn traverse_collect<T, F: FnMut(NodeId) -> Option<T>>(&self, root: NodeId, f: &mut F) -> Vec<T> {
        let mut out = Vec::new();
        self.traverse_pre(root, &mut |id| {
            if let Some(v) = f(id) {
                out.push(v);
            }
        });
        out
    }

    /// Whether `ancestor` is an ancestor of `descendant` (inclusive; Python `is_ancestor_of`
    /// returns True for `ancestor == descendant` on the first line; walks the parent-pointer chain,
    /// equivalent to the Python recursive version minus its cycle-guard dead branch).
    /// Test-only (R-15): production reachability uses nodeshrink_pass's
    /// `node_is_removed` (a separate implementation, not depending on this method).
    pub fn is_ancestor_of(&self, ancestor: NodeId, descendant: NodeId) -> bool {
        let mut cur = Some(descendant);
        while let Some(c) = cur {
            if c == ancestor {
                return true;
            }
            cur = self.node(c).parent;
        }
        false
    }

    // ------------------------------------------------------------------
    // The ASTInfo query set (the live methods of Python `ASTInfo/ASTInfo.py`;
    // dead methods not ported: update_call_insts_in_ast / get_non_empty_ast_head_pos /
    // update_node_loc_after_replace_one_node (raises DeprecationWarning at construction),
    // get_non_empty_nodes_from_longest_to_shortest, the whole remove_a_func chain
    // (its sole caller ASTState.remove_a_func has no callers), the dead func_idxs
    // parameter of from_parser. In Rust the queries hang directly on Ast, no ASTInfo wrapper.)
    // ------------------------------------------------------------------

    /// Python `get_not_empty_nodes_by_pos`: nodes whose loc equals pos with length > 0
    /// (exact loc equality, not interval covering). R-26: the only production consumer is
    /// crate-internal stack_infer; pub(crate).
    pub(crate) fn get_not_empty_nodes_by_pos(&self, pos: NodeLoc) -> Vec<NodeId> {
        let root = self.root_of_func(pos.func_idx as usize);
        self.traverse_collect(root, &mut |id| {
            (self.node(id).loc == pos && self.get_length(id) > 0).then_some(id)
        })
    }

    /// Python `get_parent_node_list_by_pos`: find the node such that "some non-list child
    /// sits exactly at pos" (first hit in pre-order). Hits whose child is a list are skipped and scanning
    /// continues; a list whose parent is a list is structurally illegal (Python raises), panic here.
    /// R-26: the only production consumer is crate-internal stack_infer; pub(crate).
    pub(crate) fn get_parent_node_list_by_pos(&self, pos: NodeLoc) -> Option<NodeId> {
        let root = self.root_of_func(pos.func_idx as usize);
        let hits = self.traverse_collect(root, &mut |id| {
            for child in self.sub_nodes(id) {
                if self.node(child).loc != pos {
                    continue;
                }
                if self.is_list(child) {
                    if self.is_list(id) {
                        panic!("NodeList's parent should not be NodeList");
                    }
                    continue;
                }
                return Some(id);
            }
            None
        });
        hits.into_iter().next()
    }

    /// Python `get_parent_node` (the odd list-returning + assert len<=1 API collapsed to
    /// Option; the result = the stored parent pointer, equivalent to Python's whole-tree search).
    /// R-26: the only production consumer is crate-internal stack_infer; pub(crate).
    pub(crate) fn get_parent_node(&self, id: NodeId) -> Option<NodeId> {
        self.node(id).parent
    }

    // The following six Python ASTInfo methods (get_all_root_trees /
    // get_all_non_empty_ast_nodes / get_root_inode_of_a_node / node_is_removed /
    // get_all_nodes_in_a_func / pub set_else_start_idx) were deleted:
    // zero callers repo-wide (nodeshrink_pass uses its own free functions; R-4).
    // The free-function set_else_start_idx is kept.

    /// Python `get_node_by_tail_loc`: nodes where loc.inst_idx + length - 1 equals
    /// tail_loc.inst_idx exactly (no length filter, root included).
    /// R-26: the only production consumer is crate-internal stack_infer; pub(crate).
    pub(crate) fn get_node_by_tail_loc(&self, tail_loc: NodeLoc) -> Vec<NodeId> {
        let root = self.root_of_func(tail_loc.func_idx as usize);
        self.traverse_collect(root, &mut |id| {
            (self.node(id).loc.inst_idx + self.get_length(id) - 1 == tail_loc.inst_idx)
                .then_some(id)
        })
    }

    /// Whether the node is a list (Python `isinstance(node, NodeList)`, including Else/Root).
    pub fn is_list(&self, id: NodeId) -> bool {
        matches!(self.node(id).kind, NodeKind::List { .. })
    }

    // ------------------------------------------------------------------
    // Mutation primitives (child-structure operations of Python NodeList/Block/Loop/If; consumed by M8/M10)
    // ------------------------------------------------------------------

    /// Python `NodeList.replace_split_with_new_sub_nodes`:
    /// Replaces children[start..end] with new_nodes (out-of-range indices clamp to the bounds;
    /// a list must not be placed inside its own new_nodes; removed children still attached to
    /// this list get their parent pointer detached; new nodes get parent pointers attached).
    pub fn replace_split_with_new_sub_nodes(
        &mut self,
        list: NodeId,
        start: usize,
        end: usize,
        new_nodes: Vec<NodeId>,
    ) {
        let len = match &self.nodes[list.0 as usize].kind {
            NodeKind::List { children, .. } => children.len(),
            _ => panic!("replace_split_with_new_sub_nodes on non-list node"),
        };
        let start = start.min(len);
        let end = end.min(len);
        for n in &new_nodes {
            if *n == list {
                panic!("node list cannot contain itself");
            }
        }
        let old: Vec<NodeId> = match &mut self.nodes[list.0 as usize].kind {
            NodeKind::List { children, .. } => children.drain(start..end).collect(),
            _ => unreachable!(),
        };
        let insert_at = start;
        if let NodeKind::List { children, .. } = &mut self.nodes[list.0 as usize].kind {
            for (i, n) in new_nodes.iter().enumerate() {
                children.insert(insert_at + i, *n);
            }
        }
        for removed in old {
            if self.nodes[removed.0 as usize].parent == Some(list) {
                self.nodes[removed.0 as usize].parent = None;
            }
        }
        for n in new_nodes {
            self.nodes[n.0 as usize].parent = Some(list);
        }
    }

    /// Python `BlockNode.set_sub_node_list` / `LoopNode.set_sub_node_list`.
    pub fn set_block_body(&mut self, block: NodeId, new_body: NodeId) {
        match &mut self.nodes[block.0 as usize].kind {
            NodeKind::Block { body, .. } | NodeKind::Loop { body, .. } => *body = new_body,
            _ => panic!("set_block_body on non-block node"),
        }
        self.nodes[new_body.0 as usize].parent = Some(block);
    }

    /// Python `IfNode.set_if_sub_node_list`.
    pub fn set_if_then(&mut self, if_node: NodeId, new_then: NodeId) {
        match &mut self.nodes[if_node.0 as usize].kind {
            NodeKind::If { then, .. } => *then = new_then,
            _ => panic!("set_if_then on non-if node"),
        }
        self.nodes[new_then.0 as usize].parent = Some(if_node);
    }

    /// Python `IfNode.set_else_sub_node_list` (does not set has_else, same as Python).
    pub fn set_if_else(&mut self, if_node: NodeId, new_else: NodeId) {
        match &mut self.nodes[if_node.0 as usize].kind {
            NodeKind::If { els, .. } => *els = new_else,
            _ => panic!("set_if_else on non-if node"),
        }
        self.nodes[new_else.0 as usize].parent = Some(if_node);
    }

    /// Python `IfNode.else_sub_node_list` property: **accessing sets has_else=True**
    /// (side-effect semantics); returns the else list.
    /// Test-only (R-15): production tree building uses the free-function else_list_mark.
    pub fn else_list_mark(&mut self, if_node: NodeId) -> NodeId {
        else_list_mark(&mut self.nodes, if_node)
    }

    /// Python `ASTInfo.update_loc_info`: recompute inst_idx for all nodes of one
    /// function from the tree shape (func_idx untouched; root is 0).
    pub fn update_loc_info(&mut self, func_idx: usize) {
        let root = self.func_roots[func_idx];
        self.update_node_positions(root, 0);
    }

    fn update_node_positions(&mut self, id: NodeId, position: u32) {
        self.nodes[id.0 as usize].loc.inst_idx = position;
        match &self.node(id).kind {
            NodeKind::Block { body, .. } | NodeKind::Loop { body, .. } => {
                let body = *body;
                self.update_node_positions(body, position + 1);
            }
            NodeKind::If { then, els, has_else, .. } => {
                let (then, els, has_else) = (*then, *els, *has_else);
                self.update_node_positions(then, position + 1);
                if has_else {
                    // The else opcode sits one slot after the then body, the else body one further.
                    let else_position = position + 1 + self.get_length(then);
                    self.update_node_positions(els, else_position + 1);
                }
            }
            NodeKind::List { children, .. } => {
                let children = children.clone();
                let mut pos = position;
                for c in children {
                    self.update_node_positions(c, pos);
                    pos += self.get_length(c);
                }
            }
            NodeKind::Insts { .. } => {}
        }
    }

    // ------------------------------------------------------------------
    // Tree building (Python insts2AST)
    // ------------------------------------------------------------------
}

/// Pre-order node id sequence (depends only on the arena slice; used by post-build processing before Ast assembly).
fn traverse_pre_ids(nodes: &[AstNode], id: NodeId, out: &mut Vec<NodeId>) {
    out.push(id);
    let children: Vec<NodeId> = match &nodes[id.0 as usize].kind {
        NodeKind::Insts { .. } => vec![],
        NodeKind::List { children, .. } => children.clone(),
        NodeKind::Block { body, .. } | NodeKind::Loop { body, .. } => vec![*body],
        NodeKind::If { then, els, has_else, .. } => {
            if *has_else {
                vec![*then, *els]
            } else {
                vec![*then]
            }
        }
    };
    for c in children {
        traverse_pre_ids(nodes, c, out);
    }
}

/// Python post-build processing `convert_to_insts_node_with_type`: pre-order traversal;
    /// a list with exactly one child which is a plain Insts node has that child upgraded to the
    /// typed flavor (a newly allocated node; allocation order = Python's node_id order of new
    /// InstsNodeWithType objects). Called right after each function's build.
fn upgrade_single_insts_children(nodes: &mut Vec<AstNode>, root: NodeId) {
    let mut ids = Vec::new();
    traverse_pre_ids(nodes, root, &mut ids);
    for id in ids {
        let (child, ty, loc) = match &nodes[id.0 as usize].kind {
            NodeKind::List { children, ty, .. } if children.len() == 1 => {
                let c = children[0];
                match &nodes[c.0 as usize].kind {
                    NodeKind::Insts { ty: None, .. } => (c, ty.clone(), nodes[c.0 as usize].loc),
                    _ => continue,
                }
            }
            _ => continue,
        };
        let insts = match &nodes[child.0 as usize].kind {
            NodeKind::Insts { insts, .. } => insts.clone(),
            _ => unreachable!(),
        };
        let new = NodeId(nodes.len() as u32);
        nodes.push(AstNode {
            kind: NodeKind::Insts { insts, ty },
            loc,
            parent: Some(id),
        });
        if let NodeKind::List { children, .. } = &mut nodes[id.0 as usize].kind {
            children[0] = new;
        }
    }
}

/// Python `insts2AST`: builds the tree from a linear instruction sequence (function-level end excluded).
fn build_insts(
    b: &mut Builder,
    insts: &[Inst],
    given_ty: FTy,
    func_idx: u32,
) -> Result<NodeId> {
    let root = b.alloc_list(ListRole::Root, vec![], Some(given_ty), NodeLoc { func_idx, inst_idx: 0 });
    let mut cur_list = root;
    let mut cur_insts: Vec<Inst> = Vec::new();
    let mut cur_inst_start_idx: u32 = 0;
    let mut block_frame: Vec<NodeId> = Vec::new();
    let mut list_frame: Vec<NodeId> = Vec::new();

    for (inst_idx, inst) in insts.iter().enumerate() {
        let inst_idx = inst_idx as u32;
        match inst {
            Inst::Block { blockty } | Inst::Loop { blockty } | Inst::If { blockty } => {
                b.flush_insts(&mut cur_insts, cur_list, cur_inst_start_idx);
                let ty = blockty_to_fty(b.module, blockty)?;
                let is_if = matches!(inst, Inst::If { .. });
                // Allocation order aligned with Python: the block body list before the block node; for if,
                // the then list, then the else list, then the if node. Body-list parent pointers are
                // attached here (Python sets sub_node_list.parent inside each block node's __init__).
                let block = if is_if {
                    let then = b.alloc_list(
                        ListRole::Plain,
                        vec![],
                        Some(ty.clone()),
                        NodeLoc { func_idx, inst_idx: inst_idx + 1 },
                    );
                    let els = b.alloc_list(
                        ListRole::Else,
                        vec![],
                        Some(ty.clone()),
                        NodeLoc { func_idx, inst_idx },
                    );
                    let node = b.alloc(
                        NodeKind::If { blockty: *blockty, ty: Some(ty), then, els, has_else: false },
                        NodeLoc { func_idx, inst_idx },
                    );
                    b.nodes[then.0 as usize].parent = Some(node);
                    b.nodes[els.0 as usize].parent = Some(node);
                    node
                } else {
                    let body = b.alloc_list(
                        ListRole::Plain,
                        vec![],
                        Some(ty.clone()),
                        NodeLoc { func_idx, inst_idx: inst_idx + 1 },
                    );
                    let kind = if matches!(inst, Inst::Block { .. }) {
                        NodeKind::Block { blockty: *blockty, ty: Some(ty), body }
                    } else {
                        NodeKind::Loop { blockty: *blockty, ty: Some(ty), body }
                    };
                    let node = b.alloc(kind, NodeLoc { func_idx, inst_idx });
                    b.nodes[body.0 as usize].parent = Some(node);
                    node
                };
                b.append_child(cur_list, block);
                cur_list = match &b.nodes[block.0 as usize].kind {
                    NodeKind::Block { body, .. } | NodeKind::Loop { body, .. } => *body,
                    NodeKind::If { then, .. } => *then,
                    _ => unreachable!(),
                };
                block_frame.push(block);
                list_frame.push(cur_list);
            }
            Inst::Else => {
                b.flush_insts(&mut cur_insts, cur_list, cur_inst_start_idx);
                // Find the innermost if on the stack that still lacks an else (must exist in well-formed wasm).
                let mut found = None;
                for i in (0..block_frame.len()).rev() {
                    let is_if_no_else = matches!(
                        &b.nodes[block_frame[i].0 as usize].kind,
                        NodeKind::If { has_else: false, .. }
                    );
                    if is_if_no_else {
                        found = Some(block_frame[i]);
                        break;
                    }
                }
                let block_ = found.ok_or_else(|| {
                    anyhow::anyhow!("`else` without open if-less-else at inst {inst_idx}")
                })?;
                cur_list = else_list_mark(&mut b.nodes, block_);
                set_else_start_idx(&mut b.nodes, block_, inst_idx + 1);
                // Replace the then-list entry in list_frame with the else list.
                let then = match &b.nodes[block_.0 as usize].kind {
                    NodeKind::If { then, .. } => *then,
                    _ => unreachable!(),
                };
                for i in (0..list_frame.len()).rev() {
                    if list_frame[i] == then {
                        list_frame[i] = cur_list;
                        break;
                    }
                }
            }
            Inst::End => {
                b.flush_insts(&mut cur_insts, cur_list, cur_inst_start_idx);
                if block_frame.pop().is_none() {
                    bail!("unbalanced `end` at inst {inst_idx}");
                }
                list_frame.pop();
                cur_list = list_frame.last().copied().unwrap_or(root);
            }
            _ => {
                if is_terminal_op(inst) {
                    if cur_insts.is_empty() {
                        cur_inst_start_idx = inst_idx;
                    }
                    cur_insts.push(inst.clone());
                    b.flush_insts(&mut cur_insts, cur_list, cur_inst_start_idx);
                } else {
                    if cur_insts.is_empty() {
                        cur_inst_start_idx = inst_idx;
                    }
                    cur_insts.push(inst.clone());
                }
            }
        }
    }
    b.flush_insts(&mut cur_insts, cur_list, cur_inst_start_idx);
    if !block_frame.is_empty() {
        bail!("unclosed block(s) at end of function");
    }
    Ok(root)
}
