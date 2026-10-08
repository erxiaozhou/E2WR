//! Stack type inference (M6.3, mirroring Python `reduction_analysis/InferStackUtil.py`).
//!
//! Not ported (M6 redundancy survey):
//! - `locate_innermost` and its 4 helper closures (~70 lines of dead path): every call chain passes a
//!   non-None `known_innermost_node` (ShrinkBlock twice, ReduceUtil/util once,
//!   FinalPolishPass once), so every entry point of this module requires the innermost node explicitly;
//! - the dead `known_innermost_node` parameter of `infer_stack_types_using_before_loc_structure`
//!   (unused in the body);
//! - the wholesale duplication (~95 lines) of the same-named functions in `ReduceUtil/util.py`: both copies behave identically,
//!   this is the single implementation, reused by M10's `infer_node_scope_type_req`;
//! - dead timing variables.
//!
//! Coordinate space: probe_loc.inst_idx follows the Python convention (function-level trailing end excluded);
//! Rust `Func.insts` includes the trailing end, so comparisons against the "function end" use len-1.

use anyhow::{bail, Context as _, Result};

use crate::ast::{Ast, NodeId, NodeLoc};
use crate::inst_gen::Inst;
use crate::module::Module;
use crate::types::{
    get_inst_ty_req, merge, Context, FTy, StackState, StackStatus, TR, ValTy,
};

/// The four-tuple returned by `get_structure_before_probe_loc`.
pub struct StructureBeforeProbeLoc {
    pub context: Context,
    pub nodes: Vec<NodeId>,
    pub last_block_param: Vec<ValTy>,
    pub rest_insts: Vec<Inst>,
}

/// Python `_get_cur_context_by_ast_info`: collect outer block types along the parent-pointer chain from the innermost node
/// (Block/If → result types, Loop → param types), prepended to the function-level label stack.
pub fn cur_context_by_ast_info(
    module: &Module,
    ast: &Ast,
    probe_loc: NodeLoc,
    known_innermost_node: NodeId,
) -> Result<Context> {
    let func_context = Context::from_module(module, probe_loc.func_idx as usize)
        .context("build func context")?;
    let mut outer_layers: Vec<Vec<ValTy>> = Vec::new();
    let mut cur = Some(known_innermost_node);
    while let Some(id) = cur {
        let node = ast.node(id);
        match &node.kind {
            crate::ast::NodeKind::Block { ty: Some(t), .. }
            | crate::ast::NodeKind::If { ty: Some(t), .. } => outer_layers.push(t.results.clone()),
            crate::ast::NodeKind::Loop { ty: Some(t), .. } => outer_layers.push(t.params.clone()),
            _ => {}
        }
        cur = node.parent;
    }
    Ok(func_context.with_out_layers(outer_layers))
}

/// Python `get_idx_pt`: skip ordinary instructions backwards from inst_idx-1, returning the first position after the nearest
/// block-level instruction (block/loop/if/end/else); instructions passed along are collected in reverse
/// into rest (the caller reverses it back to forward order).
fn get_idx_pt(insts: &[Inst], inst_idx: usize, rest: &mut Vec<Inst>) -> usize {
    let mut pt = inst_idx;
    while pt > 0 {
        let last = &insts[pt - 1];
        if matches!(
            last,
            Inst::Block { .. } | Inst::Loop { .. } | Inst::If { .. } | Inst::End | Inst::Else
        ) {
            break;
        }
        rest.push(last.clone());
        pt -= 1;
    }
    pt
}

/// Python `_is_node_list_tail`: the probe sits exactly one slot past a list node's covered end.
fn is_node_list_tail(ast: &Ast, probe_loc: NodeLoc, node: NodeId) -> bool {
    ast.is_list(node) && probe_loc.inst_idx == ast.node(node).loc.inst_idx + ast.get_length(node)
}

fn is_layer_op(inst: &Inst) -> bool {
    matches!(
        inst,
        Inst::Block { .. } | Inst::Loop { .. } | Inst::If { .. } | Inst::End | Inst::Else
    )
}

fn inst_req(ctx: &Context, inst: &Inst) -> Result<TR> {
    get_inst_ty_req(inst, Some(ctx))
        .ok_or_else(|| anyhow::anyhow!("no type req for inst {inst:?}"))
}

/// Python `get_structure_before_probe_loc`: decompose the structure before the probe position
/// (context, preceding nodes of the covering list, innermost block params, uncovered stray instructions).
/// Production consumers are module-internal only (R-15: kept pub for the parity test).
pub fn get_structure_before_probe_loc(
    module: &Module,
    ast: &Ast,
    probe_loc: NodeLoc,
    known_innermost_node: NodeId,
) -> Result<StructureBeforeProbeLoc> {
    let func = module
        .defined_funcs
        .get(probe_loc.func_idx as usize)
        .context("func idx out of range")?;
    let insts = &func.insts;
    let inst_idx = probe_loc.inst_idx as usize;

    let context = cur_context_by_ast_info(module, ast, probe_loc, known_innermost_node)?;
    let func_root = ast.root_of_func(probe_loc.func_idx as usize);

    let mut rest_rev: Vec<Inst> = Vec::new();
    let inst_idx_pt = get_idx_pt(insts, inst_idx, &mut rest_rev);

    // Python: probe at the function end (total insts length, function-level end excluded).
    if inst_idx_pt == insts.len() - 1 {
        return Ok(StructureBeforeProbeLoc {
            context,
            nodes: vec![func_root],
            last_block_param: vec![],
            rest_insts: vec![],
        });
    }
    let last_inst = &insts[inst_idx_pt];
    let mut rest_insts: Vec<Inst> = rest_rev;
    rest_insts.reverse();

    let cur_node_start_loc = NodeLoc { func_idx: probe_loc.func_idx, inst_idx: inst_idx_pt as u32 };
    let cur_nodes: Vec<NodeId> = ast.get_not_empty_nodes_by_pos(cur_node_start_loc);

    let last_block_param: Vec<ValTy>;
    let cur_node: NodeId;
    match cur_nodes.len() {
        0 => {
            // probe right after a block-level instruction (end/else): find the covering node from the tail position.
            if !matches!(last_inst, Inst::End | Inst::Else) {
                bail!("no node at pos but last inst is not end/else: {last_inst:?}");
            }
            let tail_idx = if matches!(last_inst, Inst::Else) { inst_idx_pt - 1 } else { inst_idx_pt };
            let mut ns = ast.get_node_by_tail_loc(NodeLoc { func_idx: probe_loc.func_idx, inst_idx: tail_idx as u32 });
            ns.retain(|n| !ast.is_list(*n));
            ns.retain(|n| ast.get_length(*n) > 0);
            let Some(&found) = ns.first() else {
                return Ok(StructureBeforeProbeLoc {
                    context,
                    nodes: vec![],
                    last_block_param: vec![],
                    rest_insts: vec![],
                });
            };
            if ns.len() != 1 {
                bail!("multiple tail nodes at {tail_idx}");
            }
            let Some(ty) = ast.block_ty(found) else {
                bail!("tail node has no block type");
            };
            return Ok(StructureBeforeProbeLoc {
                context,
                nodes: ast.sub_nodes(found),
                last_block_param: ty.params.clone(),
                rest_insts: vec![],
            });
        }
        1 => {
            cur_node = cur_nodes[0];
            let parent = ast
                .get_parent_node(cur_node)
                .context("single node has no parent")?;
            // The root's parent-level params are empty (the Python isinstance(parent, RootNode) branch).
            last_block_param = match &ast.node(parent).kind {
                crate::ast::NodeKind::List { role: crate::ast::ListRole::Root, .. } => vec![],
                _ => ast
                    .block_ty(parent)
                    .context("parent has no block type")?
                    .params
                    .clone(),
            };
            if is_node_list_tail(ast, probe_loc, parent) {
                return Ok(StructureBeforeProbeLoc {
                    context,
                    nodes: vec![parent],
                    last_block_param: vec![],
                    rest_insts: vec![],
                });
            }
        }
        _ => {
            // Two nodes at one position: the block body list + its first non-list child.
            let (node_list_node, non_list) = if ast.is_list(cur_nodes[0]) {
                if ast.is_list(cur_nodes[1]) {
                    bail!("two list nodes at same pos");
                }
                (cur_nodes[0], cur_nodes[1])
            } else {
                if !ast.is_list(cur_nodes[1]) {
                    bail!("no list node at pos");
                }
                (cur_nodes[1], cur_nodes[0])
            };
            cur_node = non_list;
            if is_node_list_tail(ast, probe_loc, node_list_node) {
                let ty = ast.block_ty(node_list_node).context("list has no block type")?;
                return Ok(StructureBeforeProbeLoc {
                    context,
                    nodes: vec![node_list_node],
                    last_block_param: ty.params.clone(),
                    rest_insts: vec![],
                });
            }
            let most_inner = match ast.get_parent_node(node_list_node) {
                None => {
                    // The list is the root.
                    node_list_node
                }
                Some(p) => p,
            };
            last_block_param = ast
                .block_ty(most_inner)
                .context("most inner node has no block type")?
                .params
                .clone();
        }
    }

    // Common tail: find the list covering cur_node, take its children positioned before cur_node.
    let cur_node_loc = ast.node(cur_node).loc;
    let nodes_before: Vec<NodeId> = match ast.get_parent_node_list_by_pos(cur_node_loc) {
        None => {
            if !is_layer_op(last_inst) {
                bail!("no covering list and last inst is not layer op: {last_inst:?}");
            }
            if !rest_insts.is_empty() {
                bail!("rest insts not empty without covering list");
            }
            vec![]
        }
        Some(list) => ast
            .sub_nodes(list)
            .into_iter()
            .filter(|n| {
                if ast.node(*n).loc.inst_idx >= cur_node_loc.inst_idx {
                    return false;
                }
                if ast.is_list(*n) {
                    // Python: asserts here that the predecessor node is not a list (structural invariant).
                    panic!("node before cur_node is a NodeList");
                }
                true
            })
            .collect(),
    };
    Ok(StructureBeforeProbeLoc { context, nodes: nodes_before, last_block_param, rest_insts })
}

/// Python `infer_stack_req_before_insts`: starting from the last_block_param stack, merge requirements node by node
/// (instruction-sequence nodes merge instruction by instruction — including the typed flavor; Python's isinstance
/// includes subclasses). Module-internal consumers only (R-15 privatization).
fn infer_stack_req_before_insts(
    ctx: &Context,
    ast: &Ast,
    nodes: &[NodeId],
    last_block_param: &[ValTy],
) -> Result<TR> {
    let mut base = TR::new([FTy::of(&[], last_block_param)]);
    for &n in nodes {
        if let crate::ast::NodeKind::Insts { insts, .. } = &ast.node(n).kind {
            for inst in insts {
                base = merge(&base, &inst_req(ctx, inst)?);
            }
        } else {
            let r = ast
                .get_type_req(n)
                .ok_or_else(|| anyhow::anyhow!("node {n:?} has no static type req"))?;
            base = merge(&base, &r);
        }
        if base.impossible() {
            bail!("impossible req after node {n:?}");
        }
    }
    Ok(base)
}

/// Python `infer_stack_types_using_before_loc_structure` (the single version after
/// merging the duplicate implementation in ReduceUtil/util.py). Module-internal consumers only (R-15
/// privatization).
fn infer_stack_types_using_before_loc_structure(
    ctx: &Context,
    ast: &Ast,
    nodes: &[NodeId],
    last_block_param: &[ValTy],
    rest_insts: &[Inst],
    parent_node_list: Option<NodeId>,
) -> Result<TR> {
    let mut base = infer_stack_req_before_insts(ctx, ast, nodes, last_block_param)?;
    for inst in rest_insts {
        base = merge(&base, &inst_req(ctx, inst)?);
    }
    if base.impossible() {
        bail!("impossible req after rest insts");
    }
    if base.cands().len() == 1 {
        return Ok(base);
    }

    // Candidate filtering: validate each candidate against the covering list's subsequent content.
    let cur_node_idx = nodes.len();
    let insts_node_idx = cur_node_idx;
    let parent_list = match parent_node_list {
        Some(l) => l,
        None => {
            let Some(&first) = nodes.first() else {
                bail!("parent_node_list is None: nodes empty");
            };
            let p = ast
                .get_parent_node(first)
                .context("first node has no parent")?;
            if !ast.is_list(p) {
                bail!("parent of first node is not a list");
            }
            p
        }
    };
    let children = ast.sub_nodes(parent_list);
    let Some(parent_ty) = ast.block_ty(parent_list) else {
        bail!("parent list has no block type");
    };

    // Count how many complete child nodes cover each stray instruction.
    let mut rr_num = rest_insts.len() as i64;
    let mut rest_covered = 0usize;
    let mut rest_in_uncovered = 0usize;
    if cur_node_idx <= children.len() {
        for n in &children[cur_node_idx..] {
            let l = ast.get_length(*n) as i64;
            let next_rr = rr_num - l;
            if next_rr >= 0 {
                rest_covered += 1;
                rr_num = next_rr;
            } else {
                rest_in_uncovered = rr_num as usize;
                break;
            }
        }
    }
    let covered_idx = insts_node_idx + rest_covered;
    let Some(&covered_insts_node) = children.get(covered_idx) else {
        bail!("covered node index {covered_idx} out of range (children {})", children.len());
    };

    let mut pass: Vec<FTy> = Vec::new();
    for candi in base.cands().to_vec() {
        let mut under = TR::new([candi.clone()]);
        let mut failed = false;
        // Instructions of the covering node (stray part skipped; Python out-of-range slice semantics = empty).
        let cov_insts = ast.get_insts(covered_insts_node);
        for inst in &cov_insts[rest_in_uncovered.min(cov_insts.len())..] {
            under = merge(&under, &inst_req(ctx, inst)?);
            if under.cands().is_empty() {
                failed = true;
                break;
            }
        }
        if failed {
            continue;
        }
        for &rn in &children[(covered_idx + 1).min(children.len())..] {
            if matches!(ast.node(rn).kind, crate::ast::NodeKind::Insts { .. }) {
                for inst in ast.get_insts(rn) {
                    under = merge(&under, &inst_req(ctx, &inst)?);
                    if under.cands().is_empty() {
                        failed = true;
                        break;
                    }
                }
            } else {
                let Some(r) = ast.get_type_req(rn) else {
                    failed = true;
                    break;
                };
                under = merge(&under, &r);
            }
            if failed {
                break;
            }
        }
        if failed {
            continue;
        }
        // The result suffix must match the parent list type's result suffix (compare the shorter of the two lengths).
        let all_results: Vec<&Vec<ValTy>> = under.cands().iter().map(|c| &c.results).collect();
        let mut min_len = all_results.iter().map(|r| r.len()).min().unwrap_or(0);
        min_len = min_len.min(parent_ty.results.len());
        let all_matched = all_results.iter().all(|r| {
            r[r.len() - min_len..] == parent_ty.results[parent_ty.results.len() - min_len..]
        });
        if all_matched {
            pass.push(candi);
        }
    }
    if pass.is_empty() {
        bail!("pass_check_type_candis is empty: {base:?}");
    }
    Ok(TR::new(pass))
}

/// Python `infer_stack_types_on_probe_loc`: infer the stack state at the probe position.
pub fn infer_stack_types_on_probe_loc(
    module: &Module,
    ast: &Ast,
    probe_loc: NodeLoc,
    known_innermost_node: NodeId,
) -> Result<StackState> {
    let s = get_structure_before_probe_loc(module, ast, probe_loc, known_innermost_node)?;
    let tr = infer_stack_types_using_before_loc_structure(
        &s.context,
        ast,
        &s.nodes,
        &s.last_block_param,
        &s.rest_insts,
        None,
    )?;
    Ok(StackState::from_tr(&tr))
}

/// Python `ReduceUtil/util.py get_node_type_req`: static types first; without a static type
/// (plain Insts nodes, the merge branch of untyped lists) fall back to [`infer_node_scope_type_req`]
/// stack inference. Python's bare try/except and the infer-internal same-argument retry of `get_type_req`
/// (which must fail again — a dead retry) collapse into the None branch of this match.
pub fn get_node_type_req(module: &Module, ast: &Ast, node: NodeId) -> Result<TR> {
    if let Some(tr) = ast.get_type_req(node) {
        return Ok(tr);
    }
    infer_node_scope_type_req(module, ast, node)
}

/// Python negative-index slice semantics: `s[neg:]` = `s[len+neg:]`, then clamped up to 0;
/// out-of-range positive indices give an empty slice.
fn clamp_slice_start(start: i64, len: usize) -> usize {
    if start < 0 {
        (len as i64 + start).max(0) as usize
    } else {
        (start as usize).min(len)
    }
}

/// Python `ReduceUtil/util.py infer_node_scope_type_req` (the scope argument is derived from node
/// positions, not passed separately). Quirks copied verbatim:
/// - `last_op_is_deterministic_type` and `ref_type` are never reset per candidate (an early-broken
///   candidate keeps the previous candidate's tail value; the final value is decided by the last surviving candidate);
/// - `common_stack_num` accumulates across (loc1, loc2) candidate pairs within one candidate, never reset;
/// - the stack-depth trimming status uses the outer `stack_state_at_loc1` while the slice uses the current candidate's
///   stack table;
/// - the ANY branch's if/else special case for empty taken yields an empty slice on both sides, equivalent to taking
///   `(consume, produce)` directly;
/// - `mini_depth` lets actual_common go negative, wrapping per Python's negative-index slice.
///
/// Production consumers are module-internal only (the fallback path of get_node_type_req; R-15 privatization).
fn infer_node_scope_type_req(module: &Module, ast: &Ast, node: NodeId) -> Result<TR> {
    if ast.get_length(node) == 0 {
        return Ok(TR::new([FTy::of(&[], &[])]));
    }
    match &ast.node(node).kind {
        crate::ast::NodeKind::Insts { .. } => {}
        crate::ast::NodeKind::List { role: crate::ast::ListRole::Root, .. } => {
            bail!("infer_node_scope_type_req on root list {node:?}")
        }
        _ => bail!("infer_node_scope_type_req on non-insts node {node:?}"),
    }
    let parent = ast
        .node(node)
        .parent
        .context("insts node has no parent")?;
    if !ast.is_list(parent) {
        bail!("parent of insts node {node:?} is not a list");
    }
    let loc1 = ast.node(node).loc;
    let s = get_structure_before_probe_loc(module, ast, loc1, node)?;
    let type_req = infer_stack_types_using_before_loc_structure(
        &s.context,
        ast,
        &s.nodes,
        &s.last_block_param,
        &s.rest_insts,
        Some(parent),
    )?;

    let stack_state_at_loc1 = StackState::from_tr(&type_req);
    let base_req = stack_state_at_loc1.as_type_req();
    let insts = ast.get_insts(node);

    let mut all_types: Vec<FTy> = Vec::new();
    let mut ref_is_eg = false;
    let mut last_op_det = false;
    for candi in base_req.cands().to_vec() {
        let candi_tr = TR::new([candi.clone()]);
        let mut base = candi_tr.clone();
        let mut mini_depth: i64 = 0;
        let mut cur_depth: i64 = 0;
        let mut try_next = false;
        let mut introduce_poly = false;
        for inst in &insts {
            let inst_req = inst_req(&s.context, inst)?;
            // Python: req_type ∈ {'eg_param_and_result','unreachable'} ⟺
            // First candidate with both poly bits (the typeReq._poly2req view).
            if inst_req
                .cands()
                .first()
                .map(|c| c.params_poly && c.results_poly)
                .unwrap_or(false)
            {
                introduce_poly = true;
            }
            base = merge(&base, &inst_req);
            if base.impossible() {
                try_next = true;
                break;
            }
            let repr = inst_req.ty0();
            cur_depth -= repr.params.len() as i64;
            if cur_depth < mini_depth {
                mini_depth = cur_depth;
            }
            cur_depth += repr.results.len() as i64;
            last_op_det = repr.terminal;
        }
        if try_next {
            continue;
        }
        let cur_state_loc1 = StackState::from_tr(&candi_tr);
        let cur_state_loc2 = StackState::from_tr(&base);
        ref_is_eg = introduce_poly
            || (cur_state_loc2.status == StackStatus::Any
                && cur_state_loc1.status != StackStatus::Any);
        let mut common_stack_num: i64 = 0;
        for st1 in &cur_state_loc1.all_rest_types {
            for st2 in &cur_state_loc2.all_rest_types {
                for (t1, t2) in st1.iter().zip(st2.iter()) {
                    if t1 == t2 {
                        common_stack_num += 1;
                    } else {
                        break;
                    }
                }
                let actual: i64 = if stack_state_at_loc1.status == StackStatus::Any {
                    0
                } else {
                    common_stack_num.min(st1.len() as i64 + mini_depth)
                };
                let c1 = clamp_slice_start(actual, st1.len());
                let c2 = clamp_slice_start(actual, st2.len());
                all_types.push(FTy {
                    params: st1[c1..].to_vec(),
                    results: st2[c2..].to_vec(),
                    params_poly: false,
                    results_poly: false,
                    terminal: last_op_det,
                });
            }
        }
    }
    // Python's trailing typeReq(all_types, ref_type): the poly bits of req_type are baked into
    // all candidates at construction ('eg_param_and_result' → both poly); terminal keeps
    // each candidate's own last_op_is_deterministic_type.
    let pp = ref_is_eg;
    Ok(TR::new(
        all_types
            .into_iter()
            .map(|mut f| {
                f.params_poly = pp;
                f.results_poly = pp;
                f
            }),
    ))
}
