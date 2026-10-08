//! Equivalence subgraphs: Python `find_eq_subg_util.py` + the `V6V1GraphHelper.py`
//! `EqSubGraphManager`/`get_eq_sgs`/`_build_remap_from_unify_pairs`.

use std::collections::{BTreeSet, HashMap};

use super::elem::OTy;
use super::graph_helper::{
    gen_subgraph_from_elem_idxs, GraphHelper, SgBuilder, SubGraphRepo, MAX_EQ_LINK,
};
use super::vop::{EdgeType, InstKey, VopId};

/// Python `find_subsequence_positions` (all subsequence match positions of needle in haystack,
/// emitted in DFS order; max_results truncates early).
pub fn find_subsequence_positions<T: PartialEq>(
    needle: &[T],
    haystack: &[T],
    max_results: Option<usize>,
) -> Vec<Vec<usize>> {
    let mut out: Vec<Vec<usize>> = Vec::new();
    if let Some(0) = max_results {
        return out;
    }
    let mut prefix: Vec<usize> = Vec::new();
    iter_positions(needle, haystack, 0, 0, &mut prefix, &mut out, max_results);
    out
}

fn iter_positions<T: PartialEq>(
    needle: &[T],
    haystack: &[T],
    start_idx: usize,
    needle_idx: usize,
    prefix: &mut Vec<usize>,
    out: &mut Vec<Vec<usize>>,
    max_results: Option<usize>,
) {
    if needle_idx >= needle.len() {
        out.push(prefix.clone());
        return;
    }
    let target = &needle[needle_idx];
    for idx in start_idx..haystack.len() {
        if &haystack[idx] != target {
            continue;
        }
        prefix.push(idx);
        iter_positions(needle, haystack, idx + 1, needle_idx + 1, prefix, out, max_results);
        prefix.pop();
        if let Some(m) = max_results {
            if out.len() >= m {
                return;
            }
        }
    }
}

/// Python `_build_remap_from_unify_pairs`: pairwise merging into shared (detached) operands.
pub fn build_remap_from_unify_pairs(
    helper: &mut GraphHelper,
    unify_pairs: &[(VopId, VopId)],
) -> Option<HashMap<VopId, VopId>> {
    if unify_pairs.is_empty() {
        return None;
    }
    let mut remap: HashMap<VopId, VopId> = HashMap::new();
    for (taken_op, matched_op) in unify_pairs {
        let ty = helper.vop_ty(*taken_op);
        let shared = helper.graph.alloc_detached(ty);
        remap.insert(*taken_op, shared);
        remap.insert(*matched_op, shared);
    }
    Some(remap)
}

/// Python `get_eq_sgs` (builds an EqSubGraphManager then takes its repo).
/// Traversal start: Python iterates a set (unstable order); Rust goes ascending.
pub fn get_eq_sgs(helper: &mut GraphHelper) -> SubGraphRepo {
    let builder = SgBuilder::new(helper);
    let mut repo = SubGraphRepo::new();
    let taking: Vec<usize> = (0..helper.all_elem_num)
        .filter(|i| !helper.get_ops_taken_by_elem(*i).is_empty())
        .collect();
    for elem_idx in taking {
        let inst = helper.graph.numidx2_instidx[&elem_idx];
        if inst.is_not_sure_type_inst() {
            continue;
        }
        init_all_eq_graph_idxs(helper, &builder, &mut repo, elem_idx);
    }
    repo
}

fn init_all_eq_graph_idxs(
    helper: &mut GraphHelper,
    builder: &SgBuilder,
    repo: &mut SubGraphRepo,
    taken_op_elem_idx: usize,
) {
    let forward = get_forward_eq_graphs(helper, taken_op_elem_idx);
    let backward = get_backward_eq_naive(helper, taken_op_elem_idx);
    for (idxs, borrowed_ops) in forward {
        let remap = build_forward_eq_remap(helper, builder, &idxs, &borrowed_ops);
        let idx_vec: Vec<usize> = idxs.into_iter().collect();
        let sg = gen_subgraph_from_elem_idxs(builder, helper, &idx_vec, remap.as_ref(), false);
        repo.insert_sg(sg);
    }
    for (idxs, unify_pairs) in backward {
        let remap = build_remap_from_unify_pairs(helper, &unify_pairs);
        let sg = gen_subgraph_from_elem_idxs(builder, helper, &idxs, remap.as_ref(), false);
        repo.insert_sg(sg);
    }
}

fn get_forward_eq_graphs(
    helper: &GraphHelper,
    taken_op_elem_idx: usize,
) -> Vec<(BTreeSet<usize>, Vec<VopId>)> {
    let taken_lists = get_eq_taken_ops(helper, taken_op_elem_idx);
    let mut result = Vec::new();
    for ops in taken_lists {
        let op_set: BTreeSet<VopId> = ops.iter().copied().collect();
        if let Some(idxs) = build_graph_with_insts_not_gen_other(helper, &op_set, taken_op_elem_idx)
        {
            // Skip the trivial case of "exactly the run ending at this element".
            let mn = *idxs.iter().next().expect("non-empty");
            if mn + idxs.len() - 1 == taken_op_elem_idx {
                continue;
            }
            result.push((idxs, ops));
        }
    }
    result
}

fn build_graph_with_insts_not_gen_other(
    helper: &GraphHelper,
    borrowed_ops: &BTreeSet<VopId>,
    specified_end_idx: usize,
) -> Option<BTreeSet<usize>> {
    let direct = get_concrete_elems_gen_ops(helper, borrowed_ops)?;
    for inst in &direct {
        if !inst_not_gen_other_ops_taken_by_concrete_ops(helper, *inst, borrowed_ops) {
            return None;
        }
    }
    let mut result = BTreeSet::new();
    result.insert(specified_end_idx);
    for inst in direct {
        if let InstKey::Concrete { idx, .. } = inst {
            result.insert(idx);
        }
    }
    Some(result)
}

fn inst_not_gen_other_ops_taken_by_concrete_ops(
    helper: &GraphHelper,
    inst: InstKey,
    borrowed_ops: &BTreeSet<VopId>,
) -> bool {
    let produced = helper
        .graph
        .inst_idx_to_produced_ops
        .get(&inst)
        .map(Vec::as_slice)
        .unwrap_or(&[]);
    for op in produced {
        if borrowed_ops.contains(op) {
            continue;
        }
        if helper.graph.consumer_relation[*op as usize] == Some(EdgeType::Consume) {
            return false;
        }
    }
    true
}

/// Python `_get_concrete_elems_gen_ops`: CF outputs are skipped; producers outside the graph → None.
fn get_concrete_elems_gen_ops(
    helper: &GraphHelper,
    target_ops: &BTreeSet<VopId>,
) -> Option<BTreeSet<InstKey>> {
    let mut result = BTreeSet::new();
    for op in target_ops {
        if helper.op_is_v_produce(*op) {
            continue;
        }
        let producer = helper.graph.producer[*op as usize].expect("producer set");
        if !producer.is_inside_concrete_inst() {
            return None;
        }
        result.insert(producer);
    }
    Some(result)
}

/// Python `_get_eq_taken_ops`.
fn get_eq_taken_ops(helper: &GraphHelper, elem_idx: usize) -> Vec<Vec<VopId>> {
    let taken_ops: Vec<VopId> = helper
        .get_ops_taken_by_elem(elem_idx)
        .iter()
        .copied()
        .filter(|op| !helper.op_is_v_produce(*op))
        .collect();
    let stack_ops: Vec<VopId> = helper.stack_ops_before(elem_idx).to_vec();
    let rest_types: Vec<OTy> = helper
        .stack_ops_before(elem_idx + 1)
        .iter()
        .map(|op| helper.vop_ty(*op))
        .collect();
    let stack_op_types: Vec<OTy> = stack_ops.iter().map(|op| helper.vop_ty(*op)).collect();
    let n = stack_ops.len();
    let can_keep = find_subsequence_positions(&rest_types, &stack_op_types, Some(MAX_EQ_LINK));
    let taken_set: BTreeSet<VopId> = taken_ops.iter().copied().collect();
    let mut result = Vec::new();
    for keep in can_keep {
        let keep_set: BTreeSet<usize> = keep.into_iter().collect();
        let taken_idxs: Vec<usize> = (0..n).filter(|i| !keep_set.contains(i)).collect();
        let matched_ops: Vec<VopId> = taken_idxs.iter().map(|i| stack_ops[*i]).collect();
        let matched_set: BTreeSet<VopId> = matched_ops.iter().copied().collect();
        if matched_set == taken_set {
            continue;
        }
        result.push(matched_ops);
    }
    result
}

fn infer_mini_stack_change(ori: &[OTy], cur: &[OTy]) -> (Vec<OTy>, Vec<OTy>) {
    let cp = ori.iter().zip(cur).take_while(|(a, b)| a == b).count();
    (ori[cp..].to_vec(), cur[cp..].to_vec())
}

/// The return type of `_get_backward_eq_taken_ops_on_future_stack_elem_idxs_naive`:
/// a list of (future stack positions, (pre-equivalence operand, post-equivalence operand) pairs).
type BackwardEqNaive = Vec<(Vec<usize>, Vec<(VopId, VopId)>)>;

/// Python `_get_backward_eq_taken_ops_on_future_stack_elem_idxs_naive`.
fn get_backward_eq_naive(
    helper: &GraphHelper,
    elem_idx: usize,
) -> BackwardEqNaive {
    let all = helper.all_elem_num;
    if elem_idx + 1 >= all {
        return vec![];
    }
    let taken_ops: Vec<VopId> = helper.get_ops_taken_by_elem(elem_idx).to_vec();
    let stack_before = helper.stack_ops_before(elem_idx).to_vec();
    let stack_after = helper.stack_ops_before(elem_idx + 1).to_vec();
    if stack_before.len() < stack_after.len() {
        return vec![];
    }
    let types = |v: &[VopId]| -> Vec<OTy> { v.iter().map(|op| helper.vop_ty(*op)).collect() };
    let (to_drop_types, to_gen_types) =
        infer_mini_stack_change(&types(&stack_before), &types(&stack_after));
    if !to_gen_types.is_empty() {
        return vec![];
    }
    if to_drop_types.is_empty() {
        return vec![];
    }
    let mut unique_drop: Vec<OTy> = Vec::new();
    for t in &to_drop_types {
        if !unique_drop.contains(t) {
            unique_drop.push(*t);
        }
    }
    if unique_drop.len() != 1 {
        return vec![];
    }
    let drop_type = to_drop_types[0];

    let longest = longest_no_taken_seq(helper, elem_idx + 1);
    if longest == 0 {
        return vec![];
    }
    let reference_stack_end = elem_idx + 1 + longest;
    if reference_stack_end >= all {
        return vec![];
    }
    let reference_stack = helper.stack_ops_before(reference_stack_end).to_vec();
    if reference_stack.len() < stack_before.len() {
        return vec![];
    }
    for idx in 0..stack_before.len() {
        if helper.vop_ty(reference_stack[idx]) != helper.vop_ty(stack_before[idx]) {
            return vec![];
        }
    }
    let stack_after_taken = &stack_before[..stack_before.len() - to_drop_types.len()];
    let actual_reference_stack = &reference_stack[stack_after_taken.len()..];
    let mut len_of_drop_num = 0usize;
    for v in actual_reference_stack {
        if helper.vop_ty(*v) == drop_type {
            len_of_drop_num += 1;
        } else {
            break;
        }
    }
    let considered_types: Vec<OTy> = actual_reference_stack[..len_of_drop_num]
        .iter()
        .map(|op| helper.vop_ty(*op))
        .collect();
    let can_taken_idxs =
        find_subsequence_positions(&to_drop_types, &considered_types, Some(MAX_EQ_LINK));
    let mut result = Vec::new();
    for taken_idxs in can_taken_idxs {
        let matched_ops: Vec<VopId> = taken_idxs.iter().map(|i| actual_reference_stack[*i]).collect();
        let mut cur_inst_idxs: Vec<usize> = matched_ops
            .iter()
            .map(|op| match helper.graph.producer[*op as usize] {
                Some(InstKey::Concrete { idx, .. }) => idx,
                _ => panic!("matched op must have concrete producer"),
            })
            .collect();
        cur_inst_idxs.push(elem_idx);
        let unify_pairs: Vec<(VopId, VopId)> =
            taken_ops.iter().copied().zip(matched_ops.iter().copied()).collect();
        result.push((cur_inst_idxs, unify_pairs));
    }
    result
}

/// Python `_longest_no_taken_seq`.
fn longest_no_taken_seq(helper: &GraphHelper, start_elem_idx: usize) -> usize {
    let all = helper.all_elem_num;
    if start_elem_idx + 1 >= all {
        return 0;
    }
    let mut cur = helper.stack_ops_before(start_elem_idx).to_vec();
    let mut next = helper.stack_ops_before(start_elem_idx + 1).to_vec();
    let mut cur_len = 0usize;
    let mut start = start_elem_idx;
    while no_op_consumed(&cur, &next) {
        cur_len += 1;
        start += 1;
        if start + 1 >= all {
            break;
        }
        cur = next;
        next = helper.stack_ops_before(start + 1).to_vec();
    }
    cur_len
}

/// Python `_no_op_consumed` (identity prefix comparison).
fn no_op_consumed(ori: &[VopId], new: &[VopId]) -> bool {
    new.len() >= ori.len() && ori.iter().zip(new).all(|(a, b)| a == b)
}

/// Python `_build_forward_eq_remap`: outflow/inflow same-type pairs merged into shared operands.
fn build_forward_eq_remap(
    helper: &mut GraphHelper,
    builder: &SgBuilder,
    idxs: &BTreeSet<usize>,
    borrowed_ops: &[VopId],
) -> Option<HashMap<VopId, VopId>> {
    let idx_vec: Vec<usize> = idxs.iter().copied().collect();
    let tmp_sg = gen_subgraph_from_elem_idxs(builder, helper, &idx_vec, None, true);
    let outflow: BTreeSet<VopId> =
        tmp_sg.raw_gen_ops.difference(&tmp_sg.raw_taken_ops).copied().collect();
    let inflow: BTreeSet<VopId> =
        tmp_sg.raw_taken_ops.difference(&tmp_sg.raw_gen_ops).copied().collect();
    let mut remap: HashMap<VopId, VopId> = HashMap::new();
    let mut used_inflow: BTreeSet<VopId> = BTreeSet::new();
    for bop in borrowed_ops {
        if !outflow.contains(bop) {
            continue;
        }
        for iop in &inflow {
            if used_inflow.contains(iop) {
                continue;
            }
            if helper.vop_ty(*iop) == helper.vop_ty(*bop) {
                let ty = helper.vop_ty(*bop);
                let shared = helper.graph.alloc_detached(ty);
                remap.insert(*bop, shared);
                remap.insert(*iop, shared);
                used_inflow.insert(*iop);
                break;
            }
        }
    }
    if remap.is_empty() {
        None
    } else {
        Some(remap)
    }
}
