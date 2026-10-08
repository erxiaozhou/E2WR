//! U6: the CFN stage (control-flow-guided: deletion of groups adjacent to unreachable).
//! Mirrors Python `ReduceUtil/ReduceInsts_V9_cfn.py` (`_CFNPlanner` /
//! `call_V9_cfn_multi_basic`, D-11 single-list degeneration) plus the depended-upon
//! `ReduceInsts_V5_reduce_surround_unreachable.py` grouping functions and
//! `ElemGuidedNodeListReducerMF.transform_group_mutation_to_standard_param`
//! (the MF file's sole surviving function). The group model is B-6-simplified to "element list + Imm marker".

use std::collections::{BTreeMap, BTreeSet};

use anyhow::Result;
use e2wr_dd::factory::DdFactory;
use e2wr_ir::ast::Ast;
use e2wr_ir::Inst;

use crate::instseq::core_stage::apply_elem_mutation;
use crate::instseq::elem::OneElem;
use crate::instseq::mutation::Replacement;
use crate::instseq::trial::{deadline_after, StageApplier};

/// Python's `ElemGroupBase` family (B-6: only the Imm marker and the element list kept).
pub struct ElemGroup {
    /// Imm = a group of one single terminal-instruction element (Python ImmGroup).
    pub is_imm: bool,
    pub elems: Vec<OneElem>,
}

impl ElemGroup {
    fn is_empty(&self) -> bool {
        self.elems.is_empty()
    }

    /// Python `ImmGroup.get_the_only_one_inst`.
    fn the_only_one_inst(&self, ast: &Ast) -> Option<Inst> {
        if self.elems.len() != 1 {
            return None;
        }
        let insts = self.elems[0].as_insts(ast);
        if insts.len() != 1 {
            return None;
        }
        Some(insts[0].clone())
    }

    /// Python `ImmGroup.is_unreachable_inst`.
    fn is_unreachable_inst(&self, ast: &Ast) -> bool {
        matches!(self.the_only_one_inst(ast), Some(Inst::Unreachable))
    }
}

/// Python `split_surround_unreachable_groups`.
pub fn split_surround_unreachable_groups(
    _ast: &Ast,
    elems: &[OneElem],
) -> Vec<ElemGroup> {
    let mut unprocessed: Vec<OneElem> = Vec::new();
    let mut groups: Vec<ElemGroup> = Vec::new();
    for elem in elems {
        if elem.tail_likes_unreachable() {
            if !unprocessed.is_empty() {
                groups.push(ElemGroup { is_imm: false, elems: std::mem::take(&mut unprocessed) });
            }
            groups.push(ElemGroup { is_imm: true, elems: vec![elem.clone()] });
        } else {
            unprocessed.push(elem.clone());
        }
    }
    if !unprocessed.is_empty() {
        groups.push(ElemGroup { is_imm: false, elems: unprocessed });
    }
    groups
}

/// Python `last_is_unreachable_like` (opcode ∈ unreachable/br/br_table/return).
fn last_is_unreachable_like(groups: &[ElemGroup], cur_group_idx: usize, ast: &Ast) -> bool {
    let mut last_idx = cur_group_idx as isize - 1;
    while last_idx >= 0 && groups[last_idx as usize].is_empty() {
        last_idx -= 1;
    }
    if last_idx < 0 {
        return false;
    }
    let g = &groups[last_idx as usize];
    if !g.is_imm {
        return false;
    }
    matches!(
        g.the_only_one_inst(ast),
        Some(Inst::Unreachable)
            | Some(Inst::Br { .. })
            | Some(Inst::BrTable(_))
            | Some(Inst::Return)
    )
}

/// Python `next_group_is_unreachable` (unreachable only).
fn next_group_is_unreachable(groups: &[ElemGroup], cur_group_idx: usize, ast: &Ast) -> bool {
    let mut next_idx = cur_group_idx + 1;
    while next_idx < groups.len() && groups[next_idx].is_empty() {
        next_idx += 1;
    }
    if next_idx >= groups.len() {
        return false;
    }
    let g = &groups[next_idx];
    g.is_imm && g.is_unreachable_inst(ast)
}

/// Python `get_seq_unreachable_group_idx_starts`.
fn seq_unreachable_group_idx_starts(groups: &[ElemGroup], ast: &Ast) -> BTreeSet<usize> {
    let mut result = BTreeSet::new();
    if groups.is_empty() {
        return result;
    }
    for g_idx in 0..groups.len() - 1 {
        if groups[g_idx].is_imm
            && groups[g_idx].is_unreachable_inst(ast)
            && groups[g_idx + 1].is_imm
            && groups[g_idx + 1].is_unreachable_inst(ast)
        {
            result.insert(g_idx);
        }
    }
    result
}

/// Python `get_surround_unreachable_replaceable_group_idxs`.
fn get_surround_unreachable_replaceable_group_idxs(
    groups: &[ElemGroup],
    ast: &Ast,
) -> BTreeSet<usize> {
    let mut can_replace = BTreeSet::new();
    for (group_idx, group) in groups.iter().enumerate() {
        if group.is_empty() {
            continue;
        }
        if last_is_unreachable_like(groups, group_idx, ast)
            || next_group_is_unreachable(groups, group_idx, ast)
        {
            can_replace.insert(group_idx);
        }
    }
    for idx in seq_unreachable_group_idx_starts(groups, ast) {
        can_replace.remove(&idx);
    }
    can_replace
}

/// Python `transform_group_mutation_to_standard_param` (the empty-group replacement shape:
/// group head index → empty table, other in-group indices → empty table).
fn transform_group_mutation_to_standard_param(
    groups: &[ElemGroup],
    groups_mutation: &BTreeSet<usize>,
) -> (Vec<OneElem>, BTreeMap<usize, Vec<OneElem>>) {
    let mut raw_elems: Vec<OneElem> = Vec::new();
    let mut mutation: BTreeMap<usize, Vec<OneElem>> = BTreeMap::new();
    let mut cur_elem_num = 0usize;
    for (group_idx, group) in groups.iter().enumerate() {
        if groups_mutation.contains(&group_idx) {
            let ori_elem_num = group.elems.len();
            if ori_elem_num == 0 {
                // Python: the empty group asserts the new group is also empty (the caller already filtered empty groups).
            }
            mutation.insert(cur_elem_num, vec![]);
            for i in 1..ori_elem_num {
                mutation.insert(cur_elem_num + i, vec![]);
            }
        }
        cur_elem_num += group.elems.len();
        raw_elems.extend(group.elems.iter().cloned());
    }
    (raw_elems, mutation)
}

/// Python `_CFNPlanner`.
pub struct CfnPlanner {
    pub input_elems: Vec<OneElem>,
    pub raw_elems_length: u32,
    pub has_any_success: bool,
    pub accepted_elem_mutation: BTreeMap<usize, Vec<OneElem>>,
    pub sg_replacements: BTreeMap<u32, Replacement>,
    pub candidate_sg_idxs: BTreeSet<u32>,
    /// Write-only: no readers after construction (Python `_CFNPlanner.replaced_sg_idxs`
    /// is likewise write-only, kept verbatim; see rev_stage.rs's comment on the same-shaped field — R-29 note).
    pub replaced_sg_idxs: BTreeSet<u32>,
}

impl CfnPlanner {
    fn new(ast: &Ast, elems: &[OneElem]) -> CfnPlanner {
        let mut planner = CfnPlanner {
            input_elems: elems.to_vec(),
            raw_elems_length: elems.iter().map(|e| e.get_length(ast)).sum(),
            has_any_success: false,
            accepted_elem_mutation: BTreeMap::new(),
            sg_replacements: BTreeMap::new(),
            candidate_sg_idxs: BTreeSet::new(),
            replaced_sg_idxs: BTreeSet::new(),
        };
        planner.build_candidates(ast);
        planner
    }

    fn build_candidates(&mut self, ast: &Ast) {
        let raw_groups = split_surround_unreachable_groups(ast, &self.input_elems);
        if raw_groups.is_empty() {
            return;
        }
        let can_replace_idxs =
            get_surround_unreachable_replaceable_group_idxs(&raw_groups, ast);
        for gid in can_replace_idxs {
            let (_raw, mutation) =
                transform_group_mutation_to_standard_param(&raw_groups, &BTreeSet::from([gid]));
            if mutation.is_empty() {
                continue;
            }
            self.candidate_sg_idxs.insert(gid as u32);
            self.sg_replacements
                .insert(gid as u32, Replacement::from_concrete_mutation(gid as u32, mutation));
        }
    }

    fn cur_elems(&self) -> Vec<OneElem> {
        apply_elem_mutation(&self.input_elems, &self.accepted_elem_mutation)
    }
}

/// Python `call_V9_cfn_multi_basic` (single-list degeneration; task_id is logging only).
/// No timeout when `rest_time` is None.
pub fn call_v9_cfn(
    ast: &Ast,
    applier: &mut dyn StageApplier,
    dd_factory: &DdFactory,
    elems: &[OneElem],
    rest_time: Option<f64>,
) -> Result<Vec<OneElem>> {
    let expected_end_time = rest_time.map(deadline_after);
    let mut planner = CfnPlanner::new(ast, elems);

    let candidate: Vec<u32> = planner.candidate_sg_idxs.iter().copied().collect();
    // R-28: the previously inlined three-line timeout check now uses trial::deadline_passed.
    if !candidate.is_empty() && !super::trial::deadline_passed(expected_end_time) {
        let (all_cand_ids, cand_id2sg_idx) =
            crate::instseq::core_stage::fresh_cand_ids(candidate.iter().copied());
        crate::instseq::core_stage::run_probdd_candidates_on(
            applier,
            &planner.input_elems,
            &mut planner.accepted_elem_mutation,
            &planner.sg_replacements,
            &mut planner.replaced_sg_idxs,
            &mut planner.has_any_success,
            dd_factory,
            expected_end_time,
            &all_cand_ids,
            &cand_id2sg_idx,
        )?;
    }

    // Python finalize_out_in_reverse_inst_order (single list: finalize only on success).
    if planner.has_any_success {
        let cur = planner.cur_elems();
        applier.finalize(planner.raw_elems_length, &cur)?;
    }
    Ok(planner.cur_elems())
}
