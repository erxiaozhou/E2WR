//! U3: the reduction context and element extraction of a single node list.
//!
//! Mirrors Python (the post-round-4-cleanup baseline):
//! - `ReduceUtil/OneNodeListReductionEnv.py`: `OneNodeListReductionCtx` /
//!   `build_one_node_list_reduction_ctx`;
//! - `ReduceUtil/ReduceInsts_V9_util.py get_raw_elems_from_env`.

use anyhow::{Context as _, Result};

use e2wr_ir::ast::{Ast, NodeId, NodeKind};
use e2wr_ir::module::Module;
use e2wr_ir::stack_infer::{cur_context_by_ast_info, get_node_type_req};
use e2wr_ir::types::{get_inst_ty_req, Context, FTy, StackState, StackStatus};

use crate::instseq::elem::{get_type_info, OneElem};

/// Python `OneNodeListReductionCtx` (the debug field stays out of the ctx — R-8: no consumer
/// inside the ctx; each stage receives debug via an explicit parameter).
pub struct NodeListReductionCtx {
    pub context: Context,
    pub node_type: FTy,
    pub ori_node_list: NodeId,
}

impl NodeListReductionCtx {
    /// Python `build_stack_init_status`.
    pub fn build_stack_init_status(&self) -> StackState {
        StackState {
            all_rest_types: vec![self.node_type.params.clone()],
            status: StackStatus::Normal,
        }
    }
}

/// Python `build_one_node_list_reduction_ctx`'s layered context at this call site **does not pass**
/// `known_innermost_node` (taking the `locate_innermost` full-tree scan). For a "list head"
/// probe, the deepest covering node can only be the list itself or its first Insts child (block-head instructions are excluded by the
/// covers predicate; both share the same parent chain and contribute nothing to the layered collection) ⟹ starting directly from the list
/// itself is equivalent (P-28). Empty list (a dead path: task filtering is always non-empty): Python's deepest
/// covering node is the parent block's outer list (the empty-bodied parent is excluded by the covers predicate), skipping one level.
fn innermost_node_for_ctx(ast: &Ast, list: NodeId) -> Result<NodeId> {
    if ast.get_length(list) > 0 {
        return Ok(list);
    }
    let parent = ast
        .node(list)
        .parent
        .context("node list has no parent")?;
    if ast.is_list(parent) {
        return Ok(parent);
    }
    Ok(ast.node(parent).parent.unwrap_or(parent))
}

/// Python `build_one_node_list_reduction_ctx`.
pub fn build_one_node_list_reduction_ctx(
    module: &Module,
    ast: &Ast,
    ori_node_list: NodeId,
) -> Result<NodeListReductionCtx> {
    let node_type = ast.get_node_list_type_in_ast_practical(ori_node_list)?;
    let context = cur_context_by_ast_info(
        module,
        ast,
        ast.node(ori_node_list).loc,
        innermost_node_for_ctx(ast, ori_node_list)?,
    )?;
    Ok(NodeListReductionCtx { context, node_type, ori_node_list })
}

/// Python `get_raw_elems_from_env`: walks the list's children — Insts children (the typed
/// flavor included; Python's isinstance covers subclasses) expand per instruction into instruction elements; block-shaped children
/// become one element per node, typed via `get_node_type_req` (static first, stack-inference fallback).
/// `raw_index` = the element's index in the original sequence.
pub fn get_raw_elems_from_env(
    module: &Module,
    ast: &Ast,
    ctx: &NodeListReductionCtx,
) -> Result<Vec<OneElem>> {
    let mut elems: Vec<OneElem> = Vec::new();
    for child in ast.sub_nodes(ctx.ori_node_list) {
        if let NodeKind::Insts { insts, .. } = &ast.node(child).kind {
            for inst in insts {
                let tr = get_inst_ty_req(inst, Some(&ctx.context));
                let ti = get_type_info(tr.as_ref(), Some(inst));
                let raw_index = Some(elems.len() as u32);
                elems.push(OneElem::inst(inst.clone(), ti, raw_index));
            }
        } else {
            let tr = get_node_type_req(module, ast, child)?;
            let ti = get_type_info(Some(&tr), None);
            let raw_index = Some(elems.len() as u32);
            elems.push(OneElem::node(child, ti, raw_index));
        }
    }
    Ok(elems)
}
