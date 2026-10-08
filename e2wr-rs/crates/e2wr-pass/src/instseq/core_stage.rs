//! U6: the CORE stage (wholesale deletion/replacement of data-dependency-graph subgraphs).
//! Mirrors Python `ReduceUtil/ReduceInsts_V9_core.py` (`call_V9_multi_basic`
//! / `_SubGraphReduceStateV7` / `_V7SplitAndRefreshRound` /
//! `_run_reduce_round_v7_multi`) and the `ReduceInsts_V9_util.py`
//! `V7MutationPlannerBase` / `try_replace_candidates_v7_multi_dd_test` /
//! `run_probdd_for_multi_candidates` / `apply_elem_mutation` /
//! `finalize_multi_node_list_v9` (D-11 single-list degeneration: the nl2* dicts all vanish).
//!
//! State model: probes start from the **stage-baseline element sequence**, rebuilding the whole list per the dict of
//! "accepted mutations ∪ this round's candidate replacements" merged (same accumulation shape as P3); success advances
//! accepted/replaced; the final step finalizes the list once if any success happened.

use std::collections::{BTreeMap, BTreeSet};
use std::time::SystemTime;

use anyhow::Result;
use e2wr_dd::factory::DdFactory;
use e2wr_dd::probdd::ProbDD;
use e2wr_ir::ast::Ast;

use crate::instseq::elem::{cur_is_more_naive_v2, OneElem};
use crate::instseq::graph_helper::{
    get_init_subgraph_repo, sg_is_cf_only, GraphHelper, SubGraph, SubGraphRepo,
    SubGraphSplitter,
};
use crate::instseq::mutation::{
    get_elem_mutation_for_sg_ng, materialize_replacements, Replacement,
};
use crate::instseq::reduce_ctx::NodeListReductionCtx;
use crate::instseq::trial::{deadline_after, deadline_passed, StageApplier};

/// Python `apply_elem_mutation`: rebuilds the element sequence per the mutation dict.
pub fn apply_elem_mutation(
    raw_elems: &[OneElem],
    mutation_elem_idx2new_elems: &BTreeMap<usize, Vec<OneElem>>,
) -> Vec<OneElem> {
    if mutation_elem_idx2new_elems.is_empty() {
        return raw_elems.to_vec();
    }
    let mut full: Vec<OneElem> = Vec::with_capacity(raw_elems.len());
    for (elem_idx, elem) in raw_elems.iter().enumerate() {
        if let Some(new_elems) = mutation_elem_idx2new_elems.get(&elem_idx) {
            full.extend(new_elems.iter().cloned());
        } else {
            full.push(elem.clone());
        }
    }
    full
}

// R-28: the same-shaped private timeout check is_timeout was deleted; uniformly use
// trial::deadline_passed.

/// The live fields and methods of Python `V7MutationPlannerBase` (ctx is for graph building only;
/// element lengths and the simplifying test need the ast, passed as a parameter).
pub struct V7PlannerBase {
    pub graph_helper: GraphHelper,
    pub input_elems: Vec<OneElem>,
    /// Python raw_length = the instruction count sum (finalize's raw_elems_length).
    pub raw_length: u32,
    pub accepted_elem_mutation: BTreeMap<usize, Vec<OneElem>>,
    pub has_any_success: bool,
    pub sg_replacements: BTreeMap<u32, Replacement>,
    pub sg_repo: SubGraphRepo,
    rng: rand::rngs::ThreadRng,
}

impl V7PlannerBase {
    pub fn new(
        ctx: &NodeListReductionCtx,
        ast: &Ast,
        input_elems: &[OneElem],
    ) -> V7PlannerBase {
        let graph_helper = GraphHelper::new(
            input_elems,
            Some(&ctx.context),
            &ctx.node_type.params,
            Some(&ctx.node_type.results),
        );
        let raw_length: u32 = input_elems.iter().map(|e| e.get_length(ast)).sum();
        V7PlannerBase {
            graph_helper,
            input_elems: input_elems.to_vec(),
            raw_length,
            accepted_elem_mutation: BTreeMap::new(),
            has_any_success: false,
            sg_replacements: BTreeMap::new(),
            sg_repo: SubGraphRepo::new(),
            rng: rand::thread_rng(),
        }
    }

    /// Python's `cur_elems` property.
    pub fn cur_elems(&self) -> Vec<OneElem> {
        apply_elem_mutation(&self.input_elems, &self.accepted_elem_mutation)
    }

    pub fn get_sg(&self, sg_idx: u32) -> &SubGraph {
        self.sg_repo.get_sg_by_idx(sg_idx)
    }

    /// Python `build_mutation_core`.
    pub fn build_mutation_core(&mut self, ast: &e2wr_ir::ast::Ast) {
        let sgs: Vec<SubGraph> =
            self.sg_repo.items().map(|(_, sg)| sg.clone()).collect();
        for sg in &sgs {
            if sg_is_cf_only(sg, &self.input_elems) {
                continue;
            }
            self.maybe_register_sg_mutation(ast, sg);
        }
    }

    /// Python `_is_sg_mutation_simplifying`.
    fn is_sg_mutation_simplifying(
        &self,
        ast: &Ast,
        sg: &SubGraph,
        mutation: &BTreeMap<usize, Vec<OneElem>>,
    ) -> bool {
        let mut elem_idxs_in_sg = sg.sg_elem_idxs.clone();
        elem_idxs_in_sg.sort_unstable();
        let raw_elems_in_sg: Vec<OneElem> = elem_idxs_in_sg
            .iter()
            .map(|i| self.input_elems[*i].clone())
            .collect();
        let mutated: Vec<OneElem> = elem_idxs_in_sg
            .iter()
            .filter_map(|i| mutation.get(i).cloned())
            .flatten()
            .collect();
        cur_is_more_naive_v2(&raw_elems_in_sg, &mutated, ast)
    }

    /// Python `_maybe_register_sg_mutation`.
    pub fn maybe_register_sg_mutation(
        &mut self,
        ast: &Ast,
        sg: &SubGraph,
    ) -> bool {
        let Some(mutation) =
            get_elem_mutation_for_sg_ng(&self.graph_helper, sg, &mut self.rng)
        else {
            return false;
        };
        let concrete: BTreeMap<usize, Vec<OneElem>> = if mutation.is_operand_aware {
            // Python: internal = consumed ∩ produced; blocked on both sides of the same set.
            let internal: BTreeSet<_> = mutation
                .consumed_operands
                .intersection(&mutation.produced_operands)
                .copied()
                .collect();
            mutation.gen_concrete_elems(&internal)
        } else {
            mutation.materialize_mutation()
        };
        if !self.is_sg_mutation_simplifying(ast, sg, &concrete) {
            return false;
        }
        if self.sg_replacements.contains_key(&sg.idx) {
            // Python asserts sg.idx not in sg_replacements (unique initialization +
            // the split round clearing first; normally unreachable).
            panic!("sg {} already registered", sg.idx);
        }
        self.sg_replacements.insert(sg.idx, mutation);
        true
    }
}

/// Python `_SubGraphReduceStateV7`.
pub struct CoreReduceState {
    pub base: V7PlannerBase,
    pub sg_manager: SubGraphSplitter,
    pub replaced_sg_idxs: BTreeSet<u32>,
}

impl CoreReduceState {
    pub fn new(
        ctx: &NodeListReductionCtx,
        ast: &e2wr_ir::ast::Ast,
        elems: &[OneElem],
    ) -> CoreReduceState {
        let mut base = V7PlannerBase::new(ctx, ast, elems);
        // Python _build_sg_repo = get_init_subgraph_repo.
        base.sg_repo = get_init_subgraph_repo(&base.graph_helper);
        let sg_manager = SubGraphSplitter::new(&base.graph_helper);
        let mut st = CoreReduceState { base, sg_manager, replaced_sg_idxs: BTreeSet::new() };
        st.base.build_mutation_core(ast);
        st
    }

    /// Python `unreplaced_candidate_sg_idxs`.
    pub fn unreplaced_candidate_sg_idxs(&self) -> BTreeSet<u32> {
        self.base
            .sg_replacements
            .keys()
            .copied()
            .filter(|idx| !self.replaced_sg_idxs.contains(idx))
            .collect()
    }
}

/// Python `_V7SplitAndRefreshRound` (unit struct + one method; R-25 degenerated
/// to a free function).
fn split_and_refresh_round(
    ast: &e2wr_ir::ast::Ast,
    state: &mut CoreReduceState,
    to_split_sg_idxs: &BTreeSet<u32>,
    replaced_sg_idxs: &BTreeSet<u32>,
) -> Option<BTreeSet<u32>> {
    let sg_idxs_to_split: Vec<u32> = to_split_sg_idxs
        .iter()
        .copied()
        .filter(|idx| {
            !replaced_sg_idxs.contains(idx) && !state.base.get_sg(*idx).has_one_elem
        })
        .collect();
    if sg_idxs_to_split.is_empty() {
        return None;
    }
    let mut new_pool: BTreeSet<u32> = BTreeSet::new();
    state.base.sg_replacements.clear();
    for parent_sg_idx in sg_idxs_to_split {
        let children =
            state.sg_manager.replace_a_graph(&state.base.graph_helper, parent_sg_idx, &mut state.base.sg_repo);
        if children.len() == 1 {
            // Python: skip if not split.
            continue;
        }
        for child_sg in &children {
            if !state.base.maybe_register_sg_mutation(ast, child_sg) {
                continue;
            }
            new_pool.insert(child_sg.idx);
        }
    }
    Some(new_pool)
}

/// R-25: the nine environment parameters of `try_replace_candidates_dd_test` bundled (previously nine
/// positional parameter groups; ProbDD's per-round `to_save_cand_ids` stays a separate parameter).
struct CandidateDdEnv<'a> {
    applier: &'a mut dyn StageApplier,
    raw_elems: &'a [OneElem],
    accepted_elem_mutation: &'a mut BTreeMap<usize, Vec<OneElem>>,
    sg_replacements: &'a BTreeMap<u32, Replacement>,
    replaced_out: &'a mut BTreeSet<u32>,
    had_success_out: &'a mut bool,
    expected_end_time: Option<SystemTime>,
    all_cand_ids: &'a [u32],
    cand_id2sg_idx: &'a BTreeMap<u32, u32>,
}

/// Python `try_replace_candidates_v7_multi_dd_test` (single-list degeneration).
fn try_replace_candidates_dd_test(
    env: &mut CandidateDdEnv<'_>,
    to_save_cand_ids: &[u32],
) -> Result<bool> {
    if deadline_passed(env.expected_end_time) {
        return Ok(false);
    }
    let to_save: BTreeSet<u32> = to_save_cand_ids.iter().copied().collect();
    let to_replace_ids: Vec<u32> =
        env.all_cand_ids.iter().copied().filter(|id| !to_save.contains(id)).collect();

    let candidate_replacements: Vec<&Replacement> = to_replace_ids
        .iter()
        .filter_map(|id| env.sg_replacements.get(&env.cand_id2sg_idx[id]))
        .collect();
    let cand_mutation = materialize_replacements(&candidate_replacements)
        .map_err(|_| anyhow::anyhow!("conflicting concrete mutations"))?;

    let mut merged: BTreeMap<usize, Vec<OneElem>> = env.accepted_elem_mutation.clone();
    for (k, v) in cand_mutation {
        merged.insert(k, v);
    }
    if merged.is_empty() {
        return Ok(false);
    }
    let ok = env.applier.try_mutation(env.raw_elems, &merged, None)?;
    if ok {
        for (k, v) in merged {
            env.accepted_elem_mutation.insert(k, v);
        }
        let replaced: BTreeSet<u32> =
            to_replace_ids.iter().map(|id| env.cand_id2sg_idx[id]).collect();
        if !replaced.is_empty() {
            env.replaced_out.extend(replaced);
        }
        *env.had_success_out = true;
    }
    Ok(ok)
}

/// Python `run_probdd_for_multi_candidates` (single-list degeneration; task_id is logging only).
/// Shared by CORE and CFN: operates the planner state via field references.
#[allow(clippy::too_many_arguments)]
pub(crate) fn run_probdd_candidates_on(
    applier: &mut dyn StageApplier,
    raw_elems: &[OneElem],
    accepted_elem_mutation: &mut BTreeMap<usize, Vec<OneElem>>,
    sg_replacements: &BTreeMap<u32, Replacement>,
    replaced_out: &mut BTreeSet<u32>,
    had_success_out: &mut bool,
    dd_factory: &DdFactory,
    expected_end_time: Option<SystemTime>,
    all_cand_ids: &[u32],
    cand_id2sg_idx: &BTreeMap<u32, u32>,
) -> Result<()> {
    let mut run_replaced: BTreeSet<u32> = BTreeSet::new();
    let mut had_success = false;
    let mut dd: ProbDD<u32> = dd_factory.create_probdd();
    let mut env = CandidateDdEnv {
        applier,
        raw_elems,
        accepted_elem_mutation,
        sg_replacements,
        replaced_out: &mut run_replaced,
        had_success_out: &mut had_success,
        expected_end_time,
        all_cand_ids,
        cand_id2sg_idx,
    };
    let _keep = crate::common::run_probdd_capturing(
        &mut dd,
        all_cand_ids,
        None,
        expected_end_time,
        |to_save: &[u32]| try_replace_candidates_dd_test(&mut env, to_save),
    )
    .map_err(|e| e.context("dd test failed"))?;
    // Python: apply the DD result outside the round.
    if had_success {
        if !run_replaced.is_empty() {
            replaced_out.extend(run_replaced);
        }
        *had_success_out = true;
    }
    Ok(())
}

/// R-25: the candidate-id allocation boilerplate (Python: the `enumerate` position is the candidate id) — returns
/// (candidate id list, id→sg_idx map). The `cand_id2sg_idx` indirection carries ProbDD's
/// candidate-numbering semantics and is kept. The three same-shaped call sites of CORE/CFN/REV unified here.
pub(crate) fn fresh_cand_ids(
    sg_idxs: impl ExactSizeIterator<Item = u32>,
) -> (Vec<u32>, BTreeMap<u32, u32>) {
    let all_cand_ids: Vec<u32> = (0..sg_idxs.len() as u32).collect();
    let cand_id2sg_idx: BTreeMap<u32, u32> =
        all_cand_ids.iter().copied().zip(sg_idxs).collect();
    (all_cand_ids, cand_id2sg_idx)
}

/// Python `_run_reduce_round_v7_multi` (single-list degeneration).
fn run_reduce_round(
    applier: &mut dyn StageApplier,
    state: &mut CoreReduceState,
    dd_factory: &DdFactory,
    expected_end_time: Option<SystemTime>,
) -> Result<()> {
    if deadline_passed(expected_end_time) {
        return Ok(());
    }
    let unreplaced = state.unreplaced_candidate_sg_idxs();
    if unreplaced.is_empty() {
        return Ok(());
    }
    // Python: candidate ids allocated by the unreplaced set (small-int sets iterated ascending).
    let (all_cand_ids, cand_id2sg_idx) = fresh_cand_ids(unreplaced.iter().copied());
    let CoreReduceState { base, replaced_sg_idxs, .. } = state;
    // R-25: the old "local had copy → set afterwards" dance degenerated to borrowing the field directly (the runner's
    // semantics = set true only on success, consistent with this round's merge semantics).
    run_probdd_candidates_on(
        applier,
        &base.input_elems,
        &mut base.accepted_elem_mutation,
        &base.sg_replacements,
        replaced_sg_idxs,
        &mut base.has_any_success,
        dd_factory,
        expected_end_time,
        &all_cand_ids,
        &cand_id2sg_idx,
    )
}

/// Python `call_V9_multi_basic` (the CORE stage entry, single-list degeneration).
/// `enable_ddg_split` = Python `V7Cfg.enable_ddg_split` (R-10: the single-field
/// struct degenerated to a direct parameter). No timeout when `rest_time` is None.
pub fn call_v9_core(
    ctx: &NodeListReductionCtx,
    ast: &e2wr_ir::ast::Ast,
    applier: &mut dyn StageApplier,
    dd_factory: &DdFactory,
    elems: &[OneElem],
    enable_ddg_split: bool,
    rest_time: Option<f64>,
) -> Result<Vec<OneElem>> {
    let expected_end_time = rest_time.map(deadline_after);
    let mut state = CoreReduceState::new(ctx, ast, elems);

    // The initial reduction round (initial candidates built only once).
    run_reduce_round(applier, &mut state, dd_factory, expected_end_time)?;

    if enable_ddg_split {
        // Python: the initial split pool is built only when split is on.
        let mut pool: BTreeSet<u32> = state
            .base
            .sg_replacements
            .keys()
            .copied()
            .filter(|idx| !state.replaced_sg_idxs.contains(idx))
            .collect();
        loop {
            if deadline_passed(expected_end_time) {
                break;
            }
            let replaced_snapshot = state.replaced_sg_idxs.clone();
            let new_pool =
                split_and_refresh_round(ast, &mut state, &pool, &replaced_snapshot);
            let any_split = new_pool.is_some();
            pool = new_pool.unwrap_or_default();
            if !any_split {
                break;
            }
            run_reduce_round(applier, &mut state, dd_factory, expected_end_time)?;
            // Python: shrink the pool per the DD result.
            pool.retain(|idx| !state.replaced_sg_idxs.contains(idx));
        }
    }

    // Python finalize_multi_node_list_v9 → finalize_out_in_reverse_inst_order
    // (single list: finalize only on success).
    if state.base.has_any_success {
        let cur = state.base.cur_elems();
        applier.finalize(state.base.raw_length, &cur)?;
    }
    Ok(state.base.cur_elems())
}
