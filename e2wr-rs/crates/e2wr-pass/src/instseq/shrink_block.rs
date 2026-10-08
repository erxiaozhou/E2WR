//! U9: block shrinking (Python `ReduceUtil/ShrinkBlock.py`: BlockShrink/IfShrink,
//! label depth rewriting, jump-back node truncation, param/result type trimming, empty-else cleanup).
//!
//! Quirks copied verbatim (see the migration notes):
//! - `get_first_jump_back_node`'s IfNode branch has no "stop on terminal" (the block/loop
//!   branches return early upon a terminal instruction when a result already exists; the if branch merges both arms' results and only
//!   bails on emptiness); br_if is not in the terminal set;
//! - `_process_block_inner_code_when_update_shorter_return_type`'s jump
//!   test uses depth 0 rather than the loop-maintained cur_depth (cur_depth only increments/decrements, never read,
//!   actual behavior copied);
//! - Python `MAX_DEPTH = None`: the depth cap is effectively off; that branch is not ported;
//! - an empty br_table label set: Python represents it as None and `inst_can_jump_to_here`
//!   crashes iterating None (never triggered by real data); Rust represents it as an empty table (equivalent
//!   judgment surface, no crash).
//!
//! Candidate generation: Python is a lazy generator (yielding candidates with timeout checks between probes);
//! Rust eagerly builds all candidates then probes one by one — generation itself has no side effects, and the timeout granularity
//! differs only at the timeout boundary (same return value).

use std::collections::BTreeMap;
use std::time::SystemTime;

use anyhow::{Context as _, Result};

use e2wr_ir::ast::{Ast, NodeId, NodeKind, NodeLoc};
use e2wr_ir::module::Module;
use wasmparser::ValType;
use e2wr_ir::stack_infer::infer_stack_types_on_probe_loc;
use e2wr_ir::types::{FTy, ValTy};
use e2wr_ir::Inst;

use crate::node_rewriter::{gen_specific_type_insts, NodeRewriter};

/// The candidate instruction sequence + the synthetic block type position table.
type CandidateInsts = Option<(
    Vec<Inst>,
    BTreeMap<usize, (Vec<ValType>, Vec<ValType>)>,
)>;
use crate::instseq::trial::{deadline_after, deadline_passed};

// R-28: the same-shaped private timeout check timed_out was deleted; uniformly use
// trial::deadline_passed.

/// Python `common_rewrite_insts`: resolves the removed block's label depths — a br
/// targeting this block → unreachable / br_if → drop; deeper labels get -1; a br_table hitting this block becomes
/// unreachable wholesale, otherwise its labels get -1 by depth.
pub fn common_rewrite_insts(inner_insts: &[Inst]) -> Vec<Inst> {
    let mut cur_depth: i64 = 0;
    let mut new_insts: Vec<Inst> = Vec::with_capacity(inner_insts.len());
    for inst in inner_insts {
        match inst {
            Inst::Block { .. } | Inst::Loop { .. } | Inst::If { .. } => cur_depth += 1,
            Inst::End => cur_depth -= 1,
            _ => {}
        }
        match inst {
            Inst::Br { relative_depth } => {
                if *relative_depth as i64 == cur_depth {
                    new_insts.push(Inst::Unreachable);
                } else if *relative_depth as i64 > cur_depth {
                    new_insts.push(Inst::Br {
                        relative_depth: relative_depth - 1,
                    });
                } else {
                    new_insts.push(inst.clone());
                }
            }
            Inst::BrIf { relative_depth } => {
                if *relative_depth as i64 == cur_depth {
                    new_insts.push(Inst::Drop);
                } else if *relative_depth as i64 > cur_depth {
                    new_insts.push(Inst::BrIf {
                        relative_depth: relative_depth - 1,
                    });
                } else {
                    new_insts.push(inst.clone());
                }
            }
            Inst::BrTable(data) => {
                let label_idxs = &data.targets;
                let default_label = data.default;
                let d = cur_depth as u32;
                if (!label_idxs.is_empty() && label_idxs.contains(&d)) || default_label == d
                {
                    new_insts.push(Inst::Unreachable);
                } else {
                    let new_label_idxs: Vec<u32> = label_idxs
                        .iter()
                        .map(|l| if *l > d { *l - 1 } else { *l })
                        .collect();
                    let new_default =
                        if default_label > d { default_label - 1 } else { default_label };
                    new_insts.push(Inst::BrTable(e2wr_ir::inst::BrTableData {
                        targets: new_label_idxs,
                        default: new_default,
                    }));
                }
            }
            _ => new_insts.push(inst.clone()),
        }
    }
    debug_assert_eq!(cur_depth, 0, "unbalanced block depth in rewrite");
    new_insts
}

/// Python `inst_can_jump_to_here`.
fn inst_can_jump_to_here(inst: &Inst, cur_depth: u32) -> bool {
    match inst {
        Inst::Br { relative_depth } | Inst::BrIf { relative_depth } => {
            *relative_depth == cur_depth
        }
        Inst::BrTable(data) => {
            if data.default == cur_depth {
                return true;
            }
            data.targets.contains(&cur_depth)
        }
        _ => false,
    }
}

/// Python `get_first_jump_back_node` (the depth-cap branch not ported; MAX_DEPTH
/// always None). None means no jump-back node; Some is the pre-order-collected Insts node.
fn get_first_jump_back_node(
    ast: &Ast,
    start_node: NodeId,
    cur_depth: u32,
) -> Option<Vec<NodeId>> {
    match &ast.node(start_node).kind {
        NodeKind::Insts { .. } => None,
        NodeKind::List { children, .. } => {
            let mut results: Vec<NodeId> = Vec::new();
            for sub_node in children {
                match &ast.node(*sub_node).kind {
                    NodeKind::Insts { insts, .. } => {
                        if insts.is_empty() {
                            continue;
                        }
                        let last = &insts[insts.len() - 1];
                        if is_can_jump_insts(ast, *sub_node, cur_depth) {
                            results.push(*sub_node);
                        }
                        if is_terminating(last) && results.is_empty() {
                            return None;
                        }
                        if is_terminating(last) {
                            // Python: a result already present plus a terminal instruction → return the current result
                            // (the in-loop condition and the early return converge here).
                            return Some(results);
                        }
                    }
                    _ => {
                        if let Some(sub_result) =
                            get_first_jump_back_node(ast, *sub_node, cur_depth)
                        {
                            results.extend(sub_result);
                        }
                    }
                }
            }
            Some(results)
        }
        NodeKind::Block { body, .. } | NodeKind::Loop { body, .. } => {
            get_first_jump_back_node(ast, *body, cur_depth + 1)
        }
        NodeKind::If { then, els, has_else, .. } => {
            // Python IfNode branch: both arms recurse (depth +1) then merge non-empty results,
            // with no "stop on terminal" (asymmetric behavior copied verbatim).
            let r1 = get_first_jump_back_node(ast, *then, cur_depth + 1);
            let r2 = if *has_else {
                get_first_jump_back_node(ast, *els, cur_depth + 1)
            } else {
                None
            };
            let mut results = Vec::new();
            if let Some(r) = r1 {
                results.extend(r);
            }
            if let Some(r) = r2 {
                results.extend(r);
            }
            if results.is_empty() {
                None
            } else {
                Some(results)
            }
        }
    }
}

/// Python's terminal set {'br','unreachable','return','br_table'} (excluding
/// br_if, copied verbatim).
fn is_terminating(inst: &Inst) -> bool {
    matches!(
        inst,
        Inst::Br { .. } | Inst::Unreachable | Inst::Return | Inst::BrTable(_)
    )
}

/// Python `is_can_jump_insts_node`.
fn is_can_jump_insts(ast: &Ast, insts_node: NodeId, depth: u32) -> bool {
    if let NodeKind::Insts { insts, .. } = &ast.node(insts_node).kind {
        if let Some(last) = insts.last() {
            return inst_can_jump_to_here(last, depth);
        }
    }
    false
}

/// Python `gen_new_insts_for_node_jump_to_target`.
fn gen_new_insts_for_node_jump_to_target(
    module: &Module,
    ast: &Ast,
    node: NodeId,
    rng: &mut impl rand::Rng,
) -> Result<Vec<Inst>> {
    let node_loc = ast.node(node).loc;
    let node_end_inst_idx = node_loc.inst_idx + ast.get_length(node) - 1;
    let new_loc = NodeLoc { func_idx: node_loc.func_idx, inst_idx: node_end_inst_idx };
    let stack_state = infer_stack_types_on_probe_loc(module, ast, new_loc, node)
        .context("infer stack types at jump-back node end")?;

    let insts_node_parent = ast.node(node).parent.context("jump node has no parent")?;
    let parent_node_list_inst_idx = ast.node(insts_node_parent).loc.inst_idx;
    let node_inst_offset = (node_loc.inst_idx - parent_node_list_inst_idx) as usize;
    let node_list_insts = ast.get_insts(insts_node_parent);
    let node_list_insts_before_node = node_list_insts[..node_inst_offset].to_vec();

    let parent_parent = ast
        .node(insts_node_parent)
        .parent
        .context("jump node's list has no parent block")?;
    let block_type = node_block_ty(ast, parent_parent)?;

    // Python get_insts_padding_pos_and_list_end:
    // rest = all_rest_types[0]; the expected type (rest → block results).
    let expected_rest_types: Vec<ValTy> = stack_state
        .all_rest_types
        .first()
        .context("empty all_rest_types")?
        .clone();
    let specific_type_gen_insts =
        gen_specific_type_insts(&expected_rest_types, &block_type.results, rng)?;

    let mut all_insts = node_list_insts_before_node;
    let mut node_insts = ast.get_insts(node);
    node_insts.pop();
    all_insts.extend(node_insts);
    all_insts.extend(specific_type_gen_insts);
    Ok(all_insts)
}

/// Python `get_block_type` (Block/Loop/If concrete types; None → error,
/// matching Python's NotImplementedError).
fn node_block_ty(ast: &Ast, node: NodeId) -> Result<FTy> {
    match &ast.node(node).kind {
        NodeKind::Block { ty, .. }
        | NodeKind::Loop { ty, .. }
        | NodeKind::If { ty, .. } => {
            ty.clone().context("block type is None")
        }
        NodeKind::List { ty, .. } => ty.clone().context("block type is None"),
        NodeKind::Insts { .. } => anyhow::bail!("insts node has no block type"),
    }
}

/// Python `BlockReplacementGenerator.rewrite_all_inner_insts` (including the trailing
/// padding candidates).
fn rewrite_all_inner_insts(
    module: &Module,
    ast: &Ast,
    target_list: NodeId,
    target_node: NodeId,
    rng: &mut impl rand::Rng,
) -> Result<Vec<Vec<Inst>>> {
    let target_list_loc = ast.node(target_list).loc;
    let target_node_start_idx = target_list_loc.inst_idx;
    let target_node_end_idx = target_node_start_idx + ast.get_length(target_list);

    let mut replacements: Vec<Vec<Inst>> = Vec::new();
    if let Some(meta_result) = get_first_jump_back_node(ast, target_list, 0) {
        for last_node in meta_result {
            let to_replace_node = ast.node(last_node).parent.context("no parent")?;
            let new_insts =
                gen_new_insts_for_node_jump_to_target(module, ast, last_node, rng)?;
            let ori_all_insts = ast.get_insts(target_list);
            let to_replace_node_offset =
                (ast.node(to_replace_node).loc.inst_idx - target_node_start_idx) as usize;
            let to_replace_node_length = ast.get_length(to_replace_node) as usize;
            let mut new_insts_before_rewrite: Vec<Inst> =
                ori_all_insts[..to_replace_node_offset].to_vec();
            new_insts_before_rewrite.extend(new_insts);
            new_insts_before_rewrite
                .extend(ori_all_insts[to_replace_node_offset + to_replace_node_length..].to_vec());
            replacements.push(common_rewrite_insts(&new_insts_before_rewrite));
        }
    }

    let to_detect_loc = NodeLoc {
        func_idx: target_list_loc.func_idx,
        inst_idx: target_node_end_idx,
    };
    let stack_state = infer_stack_types_on_probe_loc(module, ast, to_detect_loc, target_list)
        .context("infer stack types at block end")?;
    let block_type = node_block_ty(ast, target_node)?;
    let expected_rest_types: Vec<ValTy> = stack_state
        .all_rest_types
        .first()
        .context("empty all_rest_types")?
        .clone();
    let padding_insts =
        gen_specific_type_insts(&expected_rest_types, &block_type.results, rng)?;

    let mut ori_insts = ast.get_insts(target_list);
    ori_insts.extend(padding_insts);
    replacements.push(common_rewrite_insts(&ori_insts));
    Ok(replacements)
}

/// Python `update_insts_in_block_v2`: candidates ascending by length (stable sort,
/// ties keep generation order).
fn update_insts_in_block_v2(
    module: &Module,
    ast: &Ast,
    target_list: NodeId,
    target_node: NodeId,
    rng: &mut impl rand::Rng,
) -> Result<Vec<Vec<Inst>>> {
    let mut replacements =
        rewrite_all_inner_insts(module, ast, target_list, target_node, rng)?;
    replacements.sort_by_key(|insts| insts.len());
    Ok(replacements)
}

/// Python `_get_new_node_list_from_insts_and_replace_old_node_list`
/// (ShrinkBlock call form: onlyless_inst always False,
/// check_invalid_and_return_false always True, update_cf_insts always True).
fn get_new_node_list_from_insts_and_replace_old_node_list(
    rewriter: &mut NodeRewriter,
    insts: &[Inst],
    expected_type: FTy,
    target_node: NodeId,
    synthetic: &BTreeMap<usize, (Vec<ValType>, Vec<ValType>)>,
) -> Result<(bool, Vec<NodeId>)> {
    rewriter.try_replace_block_with_insts(
        target_node,
        insts,
        expected_type,
        false,
        true,
        true,
        synthetic,
    )
}

/// FTy type string → module-level type (for synthetic block type bookkeeping; FTy has no multi-value shape,
/// always convertible).
fn wp_tys(tys: &[ValTy]) -> Vec<ValType> {
    tys.iter().map(wp_ty).collect()
}

fn wp_ty(t: &ValTy) -> ValType {
    use wasmparser::ValType as V;
    match t {
        ValTy::I32 => V::I32,
        ValTy::I64 => V::I64,
        ValTy::F32 => V::F32,
        ValTy::F64 => V::F64,
        ValTy::V128 => V::V128,
        ValTy::Funcref => V::FUNCREF,
        ValTy::Externref => V::EXTERNREF,
    }
}

/// Python `try_update_block_with_shorter_param_type`.
/// Returns (instruction sequence, synthetic block type position table).
fn try_update_block_with_shorter_param_type(
    ast: &Ast,
    rng: &mut impl rand::Rng,
    target_node: NodeId,
) -> Result<CandidateInsts> {
    assert_if_no_else(ast, target_node);
    let (params, results, op) = block_shape(ast, target_node)?;
    if params.is_empty() {
        return Ok(None);
    }
    let dropped_type = params[params.len() - 1];
    let new_params: Vec<ValType> = wp_tys(&params[..params.len() - 1]);

    let mut synthetic = BTreeMap::new();
    let mut new_insts: Vec<Inst> = vec![Inst::Drop];
    synthetic.insert(1usize, (new_params, wp_tys(&results)));
    new_insts.push(title_inst(op));
    new_insts.push(const_of(&dropped_type, rng)?);
    let mut ori = ast.get_insts(target_node);
    ori.drain(..1);
    new_insts.extend(ori);
    Ok(Some((new_insts, synthetic)))
}

/// Python `try_update_block_with_shorter_result_type`.
fn try_update_block_with_shorter_result_type(
    ast: &Ast,
    rng: &mut impl rand::Rng,
    target_node: NodeId,
) -> Result<CandidateInsts> {
    assert_if_no_else(ast, target_node);
    let (params, results, op) = block_shape(ast, target_node)?;
    if results.is_empty() {
        return Ok(None);
    }
    let dropped_type = results[results.len() - 1];
    let new_results: Vec<ValType> = wp_tys(&results[..results.len() - 1]);

    let mut synthetic = BTreeMap::new();
    synthetic.insert(0usize, (wp_tys(&params), new_results));
    let mut new_insts: Vec<Inst> = vec![title_inst(op)];

    let inner_node = match &ast.node(target_node).kind {
        NodeKind::If { then, .. } => *then,
        NodeKind::Block { body, .. } | NodeKind::Loop { body, .. } => *body,
        NodeKind::List { .. } | NodeKind::Insts { .. } => {
            anyhow::bail!("not a block-like node")
        }
    };
    let inner_insts = ast.get_insts(inner_node);
    new_insts.extend(process_block_inner_code_when_update_shorter_return_type(
        &inner_insts,
    ));
    new_insts.push(Inst::Drop);
    new_insts.push(Inst::End);
    new_insts.push(const_of(&dropped_type, rng)?);
    Ok(Some((new_insts, synthetic)))
}

/// Python `_process_block_inner_code_when_update_shorter_return_type`
/// (jump test uses depth 0, cur_depth unconsumed — actual behavior copied; see the module comment).
fn process_block_inner_code_when_update_shorter_return_type(
    inner_insts: &[Inst],
) -> Vec<Inst> {
    // Python maintains a write-only cur_depth counter (never read); copying the behavior is
    // equivalent to not maintaining it; the jump test always uses depth 0 (see the module comment).
    let mut new_insts: Vec<Inst> = Vec::with_capacity(inner_insts.len() + 4);
    for inst in inner_insts {
        if inst_can_jump_to_here(inst, 0) {
            new_insts.push(Inst::Drop);
        }
        new_insts.push(inst.clone());
    }
    new_insts
}

/// Python `try_update_if_with_shorter_result_type`.
fn try_update_if_with_shorter_result_type(
    ast: &Ast,
    rng: &mut impl rand::Rng,
    target_node: NodeId,
) -> Result<CandidateInsts> {
    let (params, results, _op) = block_shape(ast, target_node)?;
    if results.is_empty() {
        return Ok(None);
    }
    let NodeKind::If { then, els, has_else, .. } = &ast.node(target_node).kind else {
        anyhow::bail!("not an if node");
    };
    if !*has_else {
        return try_update_block_with_shorter_result_type(ast, rng, target_node);
    }
    let dropped_type = results[results.len() - 1];
    let new_results: Vec<ValType> = wp_tys(&results[..results.len() - 1]);

    let mut synthetic = BTreeMap::new();
    synthetic.insert(0usize, (wp_tys(&params), new_results));
    let mut new_insts: Vec<Inst> = vec![Inst::If { blockty: wasmparser::BlockType::Empty }];
    new_insts.extend(ast.get_insts(*then));
    new_insts.push(Inst::Drop);
    new_insts.push(Inst::Else);
    new_insts.extend(ast.get_insts(*els));
    new_insts.push(Inst::Drop);
    new_insts.push(Inst::End);
    new_insts.push(const_of(&dropped_type, rng)?);
    Ok(Some((new_insts, synthetic)))
}

/// Block shape (param/result types + the head opcode). The has-else assertion is handled by each caller
/// per the corresponding Python function (both block-version functions assert no else; the if version's
/// shortened-result path allows an else).
fn block_shape(
    ast: &Ast,
    target_node: NodeId,
) -> Result<(Vec<ValTy>, Vec<ValTy>, BlockOp)> {
    let ty = node_block_ty(ast, target_node)?;
    let op = match &ast.node(target_node).kind {
        NodeKind::Block { .. } => BlockOp::Block,
        NodeKind::Loop { .. } => BlockOp::Loop,
        NodeKind::If { .. } => BlockOp::If,
        _ => anyhow::bail!("not a block-like node"),
    };
    Ok((ty.params, ty.results, op))
}

/// Python's two `assert not target_node.has_else` (block-version param/result trimming).
fn assert_if_no_else(ast: &Ast, target_node: NodeId) {
    if let NodeKind::If { has_else, .. } = &ast.node(target_node).kind {
        assert!(!*has_else, "if with else in block-shape path");
    }
}

enum BlockOp {
    Block,
    Loop,
    If,
}

/// Head-instruction placeholder (the synthetic block type resolves during normalization; the placeholder uses the empty shorthand).
fn title_inst(op: BlockOp) -> Inst {
    let blockty = wasmparser::BlockType::Empty;
    match op {
        BlockOp::Block => Inst::Block { blockty },
        BlockOp::Loop => Inst::Loop { blockty },
        BlockOp::If => Inst::If { blockty },
    }
}

/// Python `get_inst_by_require_ty_const_n`.
fn const_of(ty: &ValTy, rng: &mut impl rand::Rng) -> Result<Inst> {
    use wasmparser::ValType as V;
    let wp = match ty {
        ValTy::I32 => V::I32,
        ValTy::I64 => V::I64,
        ValTy::F32 => V::F32,
        ValTy::F64 => V::F64,
        ValTy::V128 => V::V128,
        ValTy::Funcref => V::FUNCREF,
        ValTy::Externref => V::EXTERNREF,
    };
    crate::remap::const_inst(&wp, rng)
}

/// Python `BlockShrink.shrink` (targets Block/Loop nodes).
pub fn block_shrink(
    rewriter: &mut NodeRewriter,
    target_node: NodeId,
    rest_time: Option<f64>,
    rng: &mut impl rand::Rng,
) -> Result<(bool, Vec<NodeId>)> {
    if rest_time.map(|t| t <= 0.0).unwrap_or(false) {
        return Ok((false, Vec::new()));
    }
    let expected_end_time = rest_time.map(deadline_after);

    let target_list = match &rewriter.ast().node(target_node).kind {
        NodeKind::Block { body, .. } | NodeKind::Loop { body, .. } => *body,
        _ => anyhow::bail!("block_shrink on non-block node"),
    };
    let module = rewriter.snapshot().module();
    let ast = rewriter.ast().clone();
    for insts in update_insts_in_block_v2(module, &ast, target_list, target_node, rng)? {
        if deadline_passed(expected_end_time) {
            return Ok((false, Vec::new()));
        }
        let ty = node_block_ty(&ast, target_node)?;
        let (result, new_nodes) = get_new_node_list_from_insts_and_replace_old_node_list(
            rewriter,
            &insts,
            ty,
            target_node,
            &BTreeMap::new(),
        )?;
        if result {
            return Ok((true, new_nodes));
        }
    }

    // update the block type: first try trimming one param, then one result.
    if deadline_passed(expected_end_time) {
        return Ok((false, Vec::new()));
    }
    if let Some((new_insts, synthetic)) =
        try_update_block_with_shorter_param_type(&ast, rng, target_node)?
    {
        let ty = node_block_ty(&ast, target_node)?;
        let (result, new_nodes) = get_new_node_list_from_insts_and_replace_old_node_list(
            rewriter,
            &new_insts,
            ty,
            target_node,
            &synthetic,
        )?;
        if result {
            return Ok((true, new_nodes));
        }
    }

    if deadline_passed(expected_end_time) {
        return Ok((false, Vec::new()));
    }
    if let Some((new_insts, synthetic)) =
        try_update_block_with_shorter_result_type(&ast, rng, target_node)?
    {
        let ty = node_block_ty(&ast, target_node)?;
        let (result, new_nodes) = get_new_node_list_from_insts_and_replace_old_node_list(
            rewriter,
            &new_insts,
            ty,
            target_node,
            &synthetic,
        )?;
        if result {
            return Ok((true, new_nodes));
        }
    }
    Ok((false, Vec::new()))
}

/// Python `IfShrink.shrink` (targets If nodes).
pub fn if_shrink(
    rewriter: &mut NodeRewriter,
    target_node: NodeId,
    rest_time: Option<f64>,
    rng: &mut impl rand::Rng,
) -> Result<(bool, Vec<NodeId>)> {
    if rest_time.map(|t| t <= 0.0).unwrap_or(false) {
        return Ok((false, Vec::new()));
    }
    let expected_end_time = rest_time.map(deadline_after);

    let ast = rewriter.ast().clone();
    let (if_branch, else_branch, has_else) = match &ast.node(target_node).kind {
        NodeKind::If { then, els, has_else, .. } => (*then, *els, *has_else),
        _ => anyhow::bail!("if_shrink on non-if node"),
    };
    let to_try_list: Vec<NodeId> = if has_else {
        if ast.get_length(if_branch) > ast.get_length(else_branch) {
            vec![else_branch, if_branch]
        } else {
            vec![if_branch, else_branch]
        }
    } else {
        vec![if_branch]
    };
    for one_branch in to_try_list {
        if deadline_passed(expected_end_time) {
            return Ok((false, Vec::new()));
        }
        let (try_result, new_nodes) =
            if_try_one_branch(rewriter, target_node, one_branch, expected_end_time, rng)?;
        if try_result {
            return Ok((true, new_nodes));
        }
    }
    Ok((false, Vec::new()))
}

/// Python `IfShrink.try_one_branch`.
fn if_try_one_branch(
    rewriter: &mut NodeRewriter,
    target_node: NodeId,
    one_branch: NodeId,
    expected_end_time: Option<SystemTime>,
    rng: &mut impl rand::Rng,
) -> Result<(bool, Vec<NodeId>)> {
    // Python expected_type = target_node.get_type_req().ty0 (block types are single-candidate).
    let ast = rewriter.ast().clone();
    let expected_type = node_block_ty(&ast, target_node)?;

    let module = rewriter.snapshot().module();
    for insts in update_insts_in_block_v2(module, &ast, one_branch, target_node, rng)? {
        if deadline_passed(expected_end_time) {
            return Ok((false, Vec::new()));
        }
        let mut insts_copy = Vec::with_capacity(insts.len() + 1);
        insts_copy.push(Inst::Drop);
        insts_copy.extend(insts);
        let (result, new_nodes) = get_new_node_list_from_insts_and_replace_old_node_list(
            rewriter,
            &insts_copy,
            expected_type.clone(),
            target_node,
            &BTreeMap::new(),
        )?;
        if result {
            return Ok((true, new_nodes));
        }
    }

    if deadline_passed(expected_end_time) {
        return Ok((false, Vec::new()));
    }

    if let Some((new_insts, synthetic)) =
        try_update_if_with_shorter_result_type(&ast, rng, target_node)?
    {
        let (result, new_nodes) = get_new_node_list_from_insts_and_replace_old_node_list(
            rewriter,
            &new_insts,
            expected_type,
            target_node,
            &synthetic,
        )?;
        if result {
            return Ok((true, new_nodes));
        }
    }

    // process empty else: has an else that is empty → rewrite to the no-else form.
    let (else_list, has_else) = match &rewriter.ast().node(target_node).kind {
        NodeKind::If { els, has_else, .. } => (*els, *has_else),
        _ => anyhow::bail!("not an if node"),
    };
    if has_else && ast.get_length(else_list) == 0 {
        let ty = node_block_ty(&ast, target_node)?;
        let mut synthetic = BTreeMap::new();
        synthetic.insert(0usize, (wp_tys(&ty.params), wp_tys(&ty.results)));
        let NodeKind::If { then, .. } = &ast.node(target_node).kind else {
            unreachable!()
        };
        let mut new_insts: Vec<Inst> = vec![title_inst(BlockOp::If)];
        new_insts.extend(ast.get_insts(*then));
        new_insts.push(Inst::End);
        let ty = node_block_ty(&ast, target_node)?;
        let (result, new_nodes) = get_new_node_list_from_insts_and_replace_old_node_list(
            rewriter,
            &new_insts,
            ty,
            target_node,
            &synthetic,
        )?;
        if result {
            return Ok((true, new_nodes));
        }
    }
    Ok((false, Vec::new()))
}

#[cfg(test)]
mod tests {
    use super::*;

    fn br(d: u32) -> Inst {
        Inst::Br { relative_depth: d }
    }
    fn br_if(d: u32) -> Inst {
        Inst::BrIf { relative_depth: d }
    }
    fn block() -> Inst {
        Inst::Block { blockty: wasmparser::BlockType::Empty }
    }

    /// opcode short name (test assertions only).
    fn op_name(i: &Inst) -> &'static str {
        match i {
            Inst::Unreachable => "unreachable",
            Inst::Drop => "drop",
            Inst::Br { .. } => "br",
            Inst::BrIf { .. } => "br_if",
            Inst::BrTable(_) => "br_table",
            Inst::Block { .. } => "block",
            Inst::Loop { .. } => "loop",
            Inst::If { .. } => "if",
            Inst::Else => "else",
            Inst::End => "end",
            Inst::Nop => "nop",
            _ => "other",
        }
    }

    #[test]
    fn common_rewrite_labels() {
        // The sequence sits in the removed block's layer: nested blocks each add +1 depth. A br_if hitting depth 2
        // → drop; shallower br's stay; deeper ones get -1.
        let full = vec![
            block(),
            block(),
            br(0),
            br(1),
            br_if(2),
            br_if(3),
            Inst::End,
            Inst::End,
        ];
        let out = common_rewrite_insts(&full);
        let names: Vec<&str> = out.iter().map(op_name).collect();
        assert_eq!(
            names,
            vec!["block", "block", "br", "br", "drop", "br_if", "end", "end"]
        );
        assert!(matches!(out[5], Inst::BrIf { relative_depth: 2 }));

        // br_table: hitting this block → unreachable wholesale; deeper labels get -1.
        let table_hit = Inst::BrTable(e2wr_ir::inst::BrTableData {
            targets: vec![0, 2],
            default: 1,
        });
        let out = common_rewrite_insts(&[table_hit]);
        assert!(matches!(out[0], Inst::Unreachable));
        let table_pass = Inst::BrTable(e2wr_ir::inst::BrTableData {
            targets: vec![1, 3],
            default: 2,
        });
        let out = common_rewrite_insts(&[table_pass]);
        match &out[0] {
            Inst::BrTable(d) => {
                assert_eq!(d.targets, vec![0, 2]);
                assert_eq!(d.default, 1);
            }
            other => panic!("expected br_table, got {other:?}"),
        }

        // br_if hitting this block → drop.
        let out = common_rewrite_insts(&[br_if(0)]);
        assert!(matches!(out[0], Inst::Drop));
    }

    #[test]
    fn first_jump_back_finds_nodes() {
        // Construction: root list [insts(br 0, nop), block[insts(br 1), insts(unreachable)]].
        let module = Module::default();
        let mut ast = Ast::from_module(&module).unwrap();
        let root = ast
            .append_insts_tree(
                &module,
                &[
                    br_if(0),
                    Inst::Nop,
                    block(),
                    br(1),
                    Inst::End,
                    Inst::Unreachable,
                ],
                FTy::of(&[], &[]),
                0,
            )
            .unwrap()
            .0;
        let found = get_first_jump_back_node(&ast, root, 0).unwrap();
        // The first segment's trailing br_if jumps to the root but is not in the terminal set → collection continues (tree building
        // splits jump-class instructions into their own segments — existing M6 behavior); the in-block br 1 hits after relative depth +1;
        // the trailing unreachable stops on sight (a result already exists → return).
        assert_eq!(found.len(), 2);
        let kinds: Vec<String> = found
            .iter()
            .map(|n| match &ast.node(*n).kind {
                NodeKind::Insts { insts, .. } => format!("insts:{}", insts.len()),
                _ => "other".to_string(),
            })
            .collect();
        assert_eq!(kinds, vec!["insts:1", "insts:1"]);

        // No jump-back → Some([]) (Python returns an empty list).
        let mut ast2 = Ast::from_module(&module).unwrap();
        let root2 = ast2
            .append_insts_tree(&module, &[Inst::Nop], FTy::of(&[], &[]), 0)
            .unwrap()
            .0;
        assert!(get_first_jump_back_node(&ast2, root2, 0).unwrap().is_empty());
    }
}
