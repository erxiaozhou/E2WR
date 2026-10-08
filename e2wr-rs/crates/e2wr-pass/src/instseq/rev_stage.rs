//! U7: the REV stage (reverse reduction by equivalence-subgraph replacement).
//! Mirrors Python `ReduceUtil/ReduceInsts_V9_rev.py` (`_V7RevMutationInit` /
//! `_V7RevCandidateManager` / `_select_global_batch_v7rev` /
//! `call_V9_rev_multi_basic`, D-11 single-list degeneration: the nl2* dicts all vanish).
//!
//! Difference from CORE: the subgraph repo comes from `get_eq_sgs` (equivalence subgraphs; their
//! enable_internal_cancel=False → mutation generation takes the bypass_cancel=True path,
//! internalized in `get_elem_mutation_for_sg_ng`); candidates greedily pick disjoint batches sorted by "mutation cost";
//! a wholly failed batch defers (not selected next round); on success, candidates are filtered by the coverage of accepted
//! mutations' elements.
//!
//! Write-only fields copied verbatim: `tried_sg_idxs` (written by the REV main loop, no readers anywhere)
//! and `replaced_sg_idxs` (merged on success, no readers on the REV path).

use std::collections::{BTreeMap, BTreeSet};

use anyhow::Result;
use e2wr_dd::factory::DdFactory;
use e2wr_ir::ast::Ast;

use super::core_stage::{fresh_cand_ids, run_probdd_candidates_on, V7PlannerBase};
use super::elem::OneElem;
use super::eq_subgraph::get_eq_sgs;
use super::reduce_ctx::NodeListReductionCtx;
use super::trial::{deadline_after, deadline_passed, StageApplier};

// R-28: the same-shaped private timeout check is_timeout was deleted; uniformly use
// trial::deadline_passed.

/// Python `_V7RevMutationInit`.
pub struct RevPlanner {
    pub base: V7PlannerBase,
    /// Python replaced_sg_idxs: merged into the running replacement set on successful rounds (no readers on the REV path).
    pub replaced_sg_idxs: BTreeSet<u32>,
    /// Python tried_sg_idxs: marked per batch by the main loop (no readers anywhere; behaviorally neutral).
    pub tried_sg_idxs: BTreeSet<u32>,
}

impl RevPlanner {
    pub fn new(ctx: &NodeListReductionCtx, ast: &Ast, elems: &[OneElem]) -> RevPlanner {
        let mut base = V7PlannerBase::new(ctx, ast, elems);
        // Python _build_sg_repo = get_eq_sgs (the equivalence-subgraph repo).
        base.sg_repo = get_eq_sgs(&mut base.graph_helper);
        let mut planner = RevPlanner {
            base,
            replaced_sg_idxs: BTreeSet::new(),
            tried_sg_idxs: BTreeSet::new(),
        };
        planner.base.build_mutation_core(ast);
        planner
    }
}

/// Python `_estimate_sg_mutation_cost` (probe/test-visible): the total length of sg elements
/// after materializing replacements. Python asserts every sg element exists in the mutation dict
/// (normally always true; anchors + deleted elements cover all sg elements); treated loosely here as in U6
/// (missing counts as empty).
pub fn estimate_sg_mutation_cost(planner: &RevPlanner, ast: &Ast, sg_idx: u32) -> u64 {
    let sg = planner.base.get_sg(sg_idx);
    let mut elem_idxs_in_sg: Vec<usize> = sg.sg_elem_idxs.to_vec();
    elem_idxs_in_sg.sort_unstable();
    let mutation = planner.base.sg_replacements[&sg_idx].materialize_mutation();
    elem_idxs_in_sg
        .iter()
        .filter_map(|idx| mutation.get(idx))
        .flat_map(|elems| elems.iter())
        .map(|e| u64::from(e.get_length(ast)))
        .sum()
}

/// Python `_V7RevCandidateManager` (the cost table computed once at construction).
/// R-30 note: the single-field struct and the planner-passing method shape copy Python's
/// `_V7RevCandidateManager` result (keeping same-shape comparison across stages), not simplified.
pub struct RevCandidateManager {
    sg_idx2cost: BTreeMap<u32, u64>,
}

impl RevCandidateManager {
    pub fn new(planner: &RevPlanner, ast: &Ast) -> RevCandidateManager {
        let mut sg_idx2cost = BTreeMap::new();
        for &sg_idx in planner.base.sg_replacements.keys() {
            sg_idx2cost.insert(sg_idx, estimate_sg_mutation_cost(planner, ast, sg_idx));
        }
        RevCandidateManager { sg_idx2cost }
    }

    /// Python `_sg_mutation_elem_idxs`.
    fn sg_mutation_elem_idxs(planner: &RevPlanner, sg_idx: u32) -> BTreeSet<usize> {
        planner.base.sg_replacements[&sg_idx].covered_elem_idxs.clone()
    }

    /// Python `unreplaced_candidate_sg_idxs`: filtered by "disjoint from the elements covered by accepted
    /// mutations" (note: different from CORE's replaced-flag filtering).
    pub fn unreplaced_candidate_sg_idxs(&self, planner: &RevPlanner) -> BTreeSet<u32> {
        let used_elem_idxs: BTreeSet<usize> =
            planner.base.accepted_elem_mutation.keys().copied().collect();
        planner
            .base
            .sg_replacements
            .keys()
            .copied()
            .filter(|sg_idx| {
                Self::sg_mutation_elem_idxs(planner, *sg_idx).is_disjoint(&used_elem_idxs)
            })
            .collect()
    }

    /// Python `select_non_overlapping_sg_batch`: greedily picks mutually disjoint batches in ascending cost
    /// (missing counts as 10^12). Python breaks ties by set-iteration order (P-29-class
    /// indeterminacy); Rust breaks ties by sg_idx ascending.
    pub fn select_non_overlapping_sg_batch(
        &self,
        planner: &RevPlanner,
        candidate_sg_idxs: &BTreeSet<u32>,
    ) -> Vec<u32> {
        let used_elem_idxs: BTreeSet<usize> =
            planner.base.accepted_elem_mutation.keys().copied().collect();
        let mut remaining: Vec<u32> = candidate_sg_idxs
            .iter()
            .copied()
            .filter(|sg_idx| {
                Self::sg_mutation_elem_idxs(planner, *sg_idx).is_disjoint(&used_elem_idxs)
            })
            .collect();
        if remaining.is_empty() {
            return Vec::new();
        }
        remaining.sort_by_key(|sg_idx| {
            (self.sg_idx2cost.get(sg_idx).copied().unwrap_or(10_u64.pow(12)), *sg_idx)
        });

        let mut batch: Vec<u32> = Vec::new();
        let mut batch_used: BTreeSet<usize> = BTreeSet::new();
        for sg_idx in remaining {
            let cur_idxs = Self::sg_mutation_elem_idxs(planner, sg_idx);
            if !cur_idxs.is_disjoint(&batch_used) {
                continue;
            }
            batch.push(sg_idx);
            batch_used.extend(cur_idxs);
        }
        batch
    }
}

/// Python `call_V9_rev_multi_basic` (single-list degeneration; the task_id 'V9REV_MULTI'
/// is logging only). No timeout when `rest_time` is None.
pub fn call_v9_rev(
    ctx: &NodeListReductionCtx,
    ast: &Ast,
    applier: &mut dyn StageApplier,
    dd_factory: &DdFactory,
    elems: &[OneElem],
    rest_time: Option<f64>,
) -> Result<Vec<OneElem>> {
    let expected_end_time = rest_time.map(deadline_after);

    let mut planner = RevPlanner::new(ctx, ast, elems);
    let mgr = RevCandidateManager::new(&planner, ast);
    let mut deferred_unreplaceable: BTreeSet<u32> = BTreeSet::new();

    loop {
        if deadline_passed(expected_end_time) {
            break;
        }

        // Python _select_global_batch_v7rev (single list):
        // candidates = unreplaced candidates - the defer set; an empty batch terminates.
        let mut candidate_sg_idxs = mgr.unreplaced_candidate_sg_idxs(&planner);
        candidate_sg_idxs.retain(|sg_idx| !deferred_unreplaceable.contains(sg_idx));
        if candidate_sg_idxs.is_empty() {
            break;
        }
        let batch = mgr.select_non_overlapping_sg_batch(&planner, &candidate_sg_idxs);
        if batch.is_empty() {
            break;
        }

        for sg_idx in &batch {
            planner.tried_sg_idxs.insert(*sg_idx);
        }
        let batch_sg_idxs: BTreeSet<u32> = batch.iter().copied().collect();

        let (all_cand_ids, cand_id2sg_idx) = fresh_cand_ids(batch.iter().copied());

        // Python run_probdd_for_multi_candidates (single-list degeneration; on success
        // replaced_sg_idxs / has_any_success merged by the runner's semantics).
        let mut had_success = false;
        run_probdd_candidates_on(
            applier,
            &planner.base.input_elems,
            &mut planner.base.accepted_elem_mutation,
            &planner.base.sg_replacements,
            &mut planner.replaced_sg_idxs,
            &mut had_success,
            dd_factory,
            expected_end_time,
            &all_cand_ids,
            &cand_id2sg_idx,
        )?;

        if had_success {
            planner.base.has_any_success = true;
        } else {
            // Python: no progress for this list this round → defer this batch's candidates.
            deferred_unreplaceable.extend(batch_sg_idxs);
        }
    }

    // Python finalize_multi_node_list_v9 (single list: finalize only on success).
    if planner.base.has_any_success {
        let cur = planner.base.cur_elems();
        applier.finalize(planner.base.raw_length, &cur)?;
    }
    Ok(planner.base.cur_elems())
}
