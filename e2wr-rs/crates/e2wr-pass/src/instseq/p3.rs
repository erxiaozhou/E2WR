//! U5: the P3 stage (local stack-state graph reduction of small node lists).
//! Mirrors Python `ReduceUtil/ReduceInsts_nodelist_P3_v7.py`
//! (`reduce_insts_p3_graph_based_v7` / `StackStateGraphReducer` /
//! `StackStateGraphHelper`) + `ReduceInsts_V5_util.py`'s
//! `get_type_reqs_from_elems` / `get_stack_state_after_each_elem_with_reqs`).
//!
//! - graph building: nodes = stack states (0 = the initial state), element edges i→i+1 (carrying the original index),
//!   balance edges j→i (i<j and support(i-state, j-state); i primary order, j secondary);
//! - cycle enumeration = an order-faithful replication of networkx simple_cycles (simple_cycles.rs);
//! - deletion certificates: consecutive state pairs on a cycle verified via `sstate1_support_sstate2`,
//!   what gets extracted is a consecutive element range with zero net stack effect (the D-12 validity guarantee);
//! - each batch of disjoint cycles delta-deleted via ProbDD (task_id is logging only, not ported).

use std::collections::{BTreeMap, BTreeSet, HashMap};
use std::time::SystemTime;

use anyhow::{bail, Result};
use e2wr_dd::factory::DdFactory;
use e2wr_dd::probdd::ProbDD;
use e2wr_ir::ast::{Ast, NodeKind};
use e2wr_ir::types::{
    get_inst_ty_req, merge, sstate1_support_sstate2, StackState, StackStatus, TR,
};

use crate::instseq::elem::OneElem;
use crate::instseq::reduce_ctx::NodeListReductionCtx;
use crate::instseq::simple_cycles::simple_cycles_directed;
use crate::instseq::trial::{deadline_after, deadline_passed, StageApplier};

/// Python `get_type_reqs_from_elems`: per-element type requirements (instructions via
/// get_inst_ty_req; block elements via the node's static type — Python asserts non-None → error).
pub fn get_type_reqs_from_elems(
    ast: &Ast,
    elems: &[OneElem],
    ctx: &NodeListReductionCtx,
) -> Result<Vec<TR>> {
    let mut type_reqs = Vec::with_capacity(elems.len());
    for elem in elems {
        let tr = match &elem.elem {
            crate::instseq::elem::ElemRef::Inst(inst) => {
                get_inst_ty_req(inst, Some(&ctx.context))
            }
            crate::instseq::elem::ElemRef::Node(node) => match &ast.node(*node).kind {
                // Python block.get_type_req(context) (block shapes don't depend on context).
                NodeKind::Insts { ty: Some(t), .. } => Some(TR::new([t.clone()])),
                NodeKind::Insts { ty: None, .. } => bail!("insts node has no static type"),
                _ => ast.get_type_req(*node),
            },
        };
        let Some(tr) = tr else {
            // Python asserts elem_type_req is not None.
            bail!("no type req for elem");
        };
        type_reqs.push(tr);
    }
    Ok(type_reqs)
}

/// Python `get_stack_state_after_each_elem_with_reqs`: advances the stack state per element;
/// elements whose tail looks like unreachable go straight to the ANY state (stack polymorphism after a terminator).
pub fn get_stack_state_after_each_elem_with_reqs(
    stack_init_status: &StackState,
    elems: &[OneElem],
    type_reqs: &[TR],
) -> Vec<StackState> {
    let mut out = Vec::with_capacity(elems.len());
    let mut base_req = stack_init_status.as_type_req();
    for (elem_type_req, elem) in type_reqs.iter().zip(elems.iter()) {
        let cur_stack;
        if elem.tail_likes_unreachable() {
            cur_stack = StackState {
                all_rest_types: vec![vec![]],
                status: StackStatus::Any,
            };
            base_req = cur_stack.as_type_req();
        } else {
            base_req = merge(&base_req, elem_type_req);
            cur_stack = StackState::from_tr(&base_req);
        }
        out.push(cur_stack);
    }
    out
}

/// Python `reduce_insts_p3_graph_based_v7`. No timeout when `rest_time` is None
/// (P3 handles None explicitly; the upstream V9 passes min(rest, 30)).
pub fn reduce_insts_p3_graph_based_v7(
    ctx: &NodeListReductionCtx,
    ast: &Ast,
    reduce_applier: &mut dyn StageApplier,
    dd_factory: &DdFactory,
    rest_time: Option<f64>,
    elems: &[OneElem],
    debug: bool,
) -> Result<Vec<OneElem>> {
    let mut reducer =
        StackStateGraphReducer::new(ctx, ast, reduce_applier, elems, rest_time, debug)?;
    reducer.reduce_graph_based(dd_factory)?;
    Ok(reducer.current_elems())
}

struct StackStateGraphReducer<'a> {
    mutation_applier: &'a mut dyn StageApplier,
    type_reqs: Vec<TR>,
    last_state: StackState,
    stack_init_status: StackState,
    expected_end_time: Option<SystemTime>,
    reduced_elem_idxs: BTreeSet<usize>,
    has_any_success: bool,
    raw_elems: Vec<OneElem>,
    raw_elem_num: usize,
    raw_elems_length: u32,
    cf_elem_idxs: BTreeSet<usize>,
    cf_non_unreachable_elem_idxs: BTreeSet<usize>,
    current_cycle_batch: Option<Vec<Vec<usize>>>,
    unreplaced_cycle_positions: Option<BTreeSet<usize>>,
    /// The probe chain's check_invalid default (Python `!DEBUG`; an explicit parameter since R-8).
    debug: bool,
}

impl<'a> StackStateGraphReducer<'a> {
    fn new(
        ctx: &NodeListReductionCtx,
        ast: &'a Ast,
        mutation_applier: &'a mut dyn StageApplier,
        elems: &[OneElem],
        rest_time: Option<f64>,
        debug: bool,
    ) -> Result<StackStateGraphReducer<'a>> {
        let node_type = &ctx.node_type;
        let stack_init_status = ctx.build_stack_init_status();
        let type_reqs = get_type_reqs_from_elems(ast, elems, ctx)?;
        let last_state = StackState {
            all_rest_types: vec![node_type.results.clone()],
            status: StackStatus::Normal,
        };
        let raw_elems_length = mutation_applier.cal_elems_length(elems);
        let mut cf_elem_idxs = BTreeSet::new();
        let mut cf_non_unreachable_elem_idxs = BTreeSet::new();
        for (idx, elem) in elems.iter().enumerate() {
            if elem.is_cf_related_inst() {
                cf_elem_idxs.insert(idx);
            }
            // P-4: the set {'return','br_if'}.
            if elem.is_return_or_br_if() {
                cf_non_unreachable_elem_idxs.insert(idx);
            }
        }
        Ok(StackStateGraphReducer {
            mutation_applier,
            type_reqs,
            last_state,
            stack_init_status,
            expected_end_time: rest_time.map(deadline_after),
            reduced_elem_idxs: BTreeSet::new(),
            has_any_success: false,
            raw_elems: elems.to_vec(),
            raw_elem_num: elems.len(),
            raw_elems_length,
            cf_elem_idxs,
            cf_non_unreachable_elem_idxs,
            current_cycle_batch: None,
            unreplaced_cycle_positions: None,
            debug,
        })
    }

    fn current_elems(&self) -> Vec<OneElem> {
        let idxs = self.get_kept_indices();
        idxs.iter().map(|i| self.raw_elems[*i].clone()).collect()
    }

    /// R-29: the exclude_idxs parameter was deleted — all 4 call sites always pass the empty set
    /// (Python's 5 call sites also all use the default empty set).
    fn get_kept_indices(&self) -> Vec<usize> {
        let all_excluded = self.reduced_elem_idxs.clone();
        (0..self.raw_elem_num).filter(|i| !all_excluded.contains(i)).collect()
    }

    fn get_current_stack_states(&self) -> Vec<StackState> {
        let mut current_states = vec![self.stack_init_status.clone()];
        let to_append_req_idxs = self.get_kept_indices();
        let type_reqs: Vec<TR> =
            to_append_req_idxs.iter().map(|i| self.type_reqs[*i].clone()).collect();
        let _cur_elems = self.current_elems();
        let state_after_each_elem = get_stack_state_after_each_elem_with_reqs(
            &self.stack_init_status,
            &_cur_elems,
            &type_reqs,
        );
        current_states.extend(state_after_each_elem);
        if current_states.len() > 1 {
            let n = current_states.len();
            current_states[n - 1] = self.last_state.clone();
        }
        current_states
    }

    // R-28: the method-version same-shaped timeout check is_timeout was deleted; call sites uniformly use
    // trial::deadline_passed(self.expected_end_time).

    /// Python `_normalize_sequences`: sort and dedup (ascending within the key), then stable
    /// sort by (length, first element) — ties keep enumeration order.
    fn normalize_sequences(&self, sequences: &[Vec<usize>]) -> Vec<Vec<usize>> {
        let mut normalized: Vec<Vec<usize>> = Vec::new();
        let mut seen: BTreeSet<Vec<usize>> = BTreeSet::new();
        for seq in sequences {
            if seq.is_empty() {
                continue;
            }
            let mut s = seq.clone();
            s.sort_unstable();
            if seen.contains(&s) {
                continue;
            }
            seen.insert(s.clone());
            normalized.push(s);
        }
        normalized.sort_by(|a, b| a.len().cmp(&b.len()).then(a[0].cmp(&b[0])));
        normalized
    }

    fn seq_contains_non_unreachable_cf(&self, seq: &[usize]) -> bool {
        seq.iter().any(|i| self.cf_non_unreachable_elem_idxs.contains(i))
    }

    fn seq_starts_with_any_status(
        &self,
        seq: &[usize],
        current_states: &[StackState],
        kept_orig_indices: &[usize],
    ) -> bool {
        let Some(&first_orig_idx) = seq.iter().min() else { return false };
        let Some(pos) = kept_orig_indices.iter().position(|i| *i == first_orig_idx) else {
            return false;
        };
        pos < current_states.len() && current_states[pos].status == StackStatus::Any
    }

    fn split_local_search_ranges(&self, kept_orig_indices: &[usize]) -> Vec<(usize, usize)> {
        let mut ranges: Vec<(usize, usize)> = Vec::new();
        let mut start: Option<usize> = None;
        for (elem_idx, raw_elem_idx) in kept_orig_indices.iter().enumerate() {
            if self.cf_elem_idxs.contains(raw_elem_idx) {
                if let Some(s) = start {
                    if s < elem_idx {
                        ranges.push((s, elem_idx));
                    }
                }
                start = None;
                continue;
            }
            if start.is_none() {
                start = Some(elem_idx);
            }
        }
        if let Some(s) = start {
            if s < kept_orig_indices.len() {
                ranges.push((s, kept_orig_indices.len()));
            }
        }
        ranges
    }

    fn collect_local_reducible_sequences(
        &self,
        current_states: &[StackState],
        kept_orig_indices: &[usize],
    ) -> Vec<Vec<usize>> {
        let mut reducible_sequences: Vec<Vec<usize>> = Vec::new();
        for (start_idx, end_idx) in self.split_local_search_ranges(kept_orig_indices) {
            let local_states = &current_states[start_idx..(end_idx + 1).min(current_states.len())];
            let local_kept: Vec<usize> =
                kept_orig_indices[start_idx..end_idx.min(kept_orig_indices.len())].to_vec();
            if local_kept.is_empty() {
                continue;
            }
            let graph = StackStateGraphHelper::build_graph(local_states, &local_kept);
            let cycles = StackStateGraphHelper::find_cycles(&graph);
            if cycles.is_empty() {
                continue;
            }
            reducible_sequences
                .extend(StackStateGraphHelper::extract_reducible_sequences(&graph, &cycles));
        }
        reducible_sequences.retain(|seq| {
            self.seq_starts_with_any_status(seq, current_states, kept_orig_indices)
        });
        self.normalize_sequences(&reducible_sequences)
    }

    fn collect_global_reducible_sequences_with_cf(
        &self,
        current_states: &[StackState],
        kept_orig_indices: &[usize],
    ) -> Vec<Vec<usize>> {
        if kept_orig_indices.is_empty() {
            return vec![];
        }
        let graph = StackStateGraphHelper::build_graph(current_states, kept_orig_indices);
        let cycles = StackStateGraphHelper::find_cycles(&graph);
        if cycles.is_empty() {
            return vec![];
        }
        let mut reducible_sequences =
            StackStateGraphHelper::extract_reducible_sequences(&graph, &cycles);
        // CF sequences of only unreachable are left for the CORE stage (Python comment semantics copied).
        reducible_sequences.retain(|seq| self.seq_contains_non_unreachable_cf(seq));
        self.normalize_sequences(&reducible_sequences)
    }

    fn select_non_overlapping_cycle_batch(
        &self,
        candidate_cycles: &[Vec<usize>],
    ) -> Vec<Vec<usize>> {
        let mut batch: Vec<Vec<usize>> = Vec::new();
        let mut used_idxs: BTreeSet<usize> = self.reduced_elem_idxs.clone();
        let mut sorted: Vec<&Vec<usize>> = candidate_cycles.iter().collect();
        sorted.sort_by(|a, b| a.len().cmp(&b.len()).then(a[0].cmp(&b[0])));
        for seq in sorted {
            let seq_set: BTreeSet<usize> = seq.iter().copied().collect();
            if seq_set.intersection(&used_idxs).next().is_some() {
                continue;
            }
            batch.push(seq.clone());
            used_idxs.extend(seq_set);
        }
        batch
    }

    fn try_replace_cycle_batch(&mut self, to_save_cycle_positions: &[u32]) -> Result<bool> {
        if deadline_passed(self.expected_end_time) {
            return Ok(false);
        }
        let batch = self
            .current_cycle_batch
            .as_ref()
            .expect("cycle batch set");
        let unreplaced = self
            .unreplaced_cycle_positions
            .as_ref()
            .expect("unreplaced set");
        let to_save: BTreeSet<usize> =
            to_save_cycle_positions.iter().map(|i| *i as usize).collect();
        let to_replace: BTreeSet<usize> = unreplaced.difference(&to_save).copied().collect();
        let mut to_ignore_orig_idxs: BTreeSet<usize> = BTreeSet::new();
        for cycle_pos in &to_replace {
            to_ignore_orig_idxs.extend(batch[*cycle_pos].iter().copied());
        }
        if to_ignore_orig_idxs.is_empty() {
            return Ok(true);
        }
        let result = self.try_replace_elems(to_ignore_orig_idxs)?;
        if result {
            let unreplaced_mut = self
                .unreplaced_cycle_positions
                .as_mut()
                .expect("unreplaced set");
            for pos in to_replace {
                unreplaced_mut.remove(&pos);
            }
            self.has_any_success = true;
        }
        Ok(result)
    }

    fn run_dd_on_cycle_batch(&mut self, dd_factory: &DdFactory, batch: Vec<Vec<usize>>) -> Result<BTreeSet<usize>> {
        if batch.is_empty() || deadline_passed(self.expected_end_time) {
            return Ok(BTreeSet::new());
        }
        self.current_cycle_batch = Some(batch.clone());
        self.unreplaced_cycle_positions =
            Some((0..batch.len()).collect());
        let mut dd: ProbDD<u32> = dd_factory.create_probdd();
        let config: Vec<u32> = (0..batch.len() as u32).collect();
        let expected_end_time = self.expected_end_time;
        // R-19: the error first lands in the return value, preserving the original order of "kept computation/batch clearing before the re-raise"
        // (Python evaluates the return before the finally).
        let dd_outcome = crate::common::run_probdd_capturing(
            &mut dd,
            &config,
            None,
            expected_end_time,
            |to_save: &[u32]| self.try_replace_cycle_batch(to_save),
        );
        // Python: the return expression evaluates before finally — use the pre-clearing unreplaced set.
        let kept: BTreeSet<usize> = (0..batch.len())
            .filter(|i| !self.unreplaced_cycle_positions.as_ref().expect("unreplaced set").contains(i))
            .collect();
        // Python finally: clear the batch regardless of success.
        self.current_cycle_batch = None;
        self.unreplaced_cycle_positions = None;
        // minimal_config is unconsumed (same as cf_elem; Python only prints).
        dd_outcome?;
        Ok(kept)
    }

    fn run_local_graph_reduction(&mut self, dd_factory: &DdFactory) -> Result<()> {
        let current_states = self.get_current_stack_states();
        let kept_orig_indices = self.get_kept_indices();
        let mut batch_all = self.collect_local_reducible_sequences(&current_states, &kept_orig_indices);
        while !batch_all.is_empty() && !deadline_passed(self.expected_end_time) {
            let batch = self.select_non_overlapping_cycle_batch(&batch_all);
            if batch.is_empty() {
                break;
            }
            let reduced_before = self.reduced_elem_idxs.clone();
            let batch_for_later = batch.clone();
            self.run_dd_on_cycle_batch(dd_factory, batch)?;
            let removed_in_this_batch: BTreeSet<usize> =
                self.reduced_elem_idxs.difference(&reduced_before).copied().collect();

            let batch_seq_set: BTreeSet<Vec<usize>> =
                batch_for_later.iter().cloned().collect();
            let mut next_batch_all: Vec<Vec<usize>> = Vec::new();
            for seq in &batch_all {
                if batch_seq_set.contains(seq) {
                    continue;
                }
                if !removed_in_this_batch.is_empty()
                    && seq.iter().any(|i| removed_in_this_batch.contains(i))
                {
                    continue;
                }
                next_batch_all.push(seq.clone());
            }
            batch_all = next_batch_all;
        }
        Ok(())
    }

    fn run_global_graph_reduction(&mut self, _dd_factory: &DdFactory) -> Result<()> {
        while !deadline_passed(self.expected_end_time) {
            let current_states = self.get_current_stack_states();
            let kept_orig_indices = self.get_kept_indices();
            let reducible_sequences =
                self.collect_global_reducible_sequences_with_cf(&current_states, &kept_orig_indices);
            if reducible_sequences.is_empty() {
                return Ok(());
            }
            let mut all_failed = true;
            for seq in &reducible_sequences {
                if deadline_passed(self.expected_end_time) {
                    return Ok(());
                }
                let set: BTreeSet<usize> = seq.iter().copied().collect();
                if self.try_replace_elems(set)? {
                    self.has_any_success = true;
                    all_failed = false;
                    break;
                }
            }
            if all_failed {
                return Ok(());
            }
        }
        Ok(())
    }

    fn try_replace_elems(&mut self, to_ignore_orig_idxs: BTreeSet<usize>) -> Result<bool> {
        // Python: the mutation dict is cumulative — this round's deletions ∪ already-deleted (the real probe chain
        // rebuilds whole functions in baseline coordinates; missing the union would resurrect deleted elements).
        let mut all_removed = to_ignore_orig_idxs.clone();
        all_removed.extend(self.reduced_elem_idxs.iter().copied());
        let mutation: BTreeMap<usize, Vec<OneElem>> =
            all_removed.into_iter().map(|idx| (idx, vec![])).collect();
        let result = self.mutation_applier.try_mutation(
            &self.raw_elems,
            &mutation,
            Some(!self.debug),
        )?;
        if result {
            self.reduced_elem_idxs.extend(to_ignore_orig_idxs);
        }
        Ok(result)
    }

    fn reduce_graph_based(&mut self, dd_factory: &DdFactory) -> Result<()> {
        self.run_local_graph_reduction(dd_factory)?;
        if !deadline_passed(self.expected_end_time) {
            self.run_global_graph_reduction(dd_factory)?;
        }
        if self.has_any_success {
            let cur = self.current_elems();
            self.mutation_applier.finalize(self.raw_elems_length, &cur)?;
        }
        Ok(())
    }
}

/// Python `StackStateGraphHelper`. The P3 graph has at most one element edge per direction per node pair +
/// at most one balance edge (and the two directions are mutually exclusive); multiplicity is never needed.
struct StackStateGraphHelper;

/// The graph's edge-table shape: element edges (from, to, original index) and balance edges (from, to) in
/// Python's addition order.
struct StackGraph {
    n: usize,
    elem_edges: Vec<(usize, usize, usize)>,
    edges: Vec<(usize, usize)>,
}

impl StackStateGraphHelper {
    fn build_graph(current_states: &[StackState], kept_orig_indices: &[usize]) -> StackGraph {
        let n = current_states.len();
        let mut edges: Vec<(usize, usize)> = Vec::new();
        let mut elem_edges: Vec<(usize, usize, usize)> = Vec::new();
        for (i, orig_idx) in kept_orig_indices.iter().enumerate() {
            elem_edges.push((i, i + 1, *orig_idx));
            edges.push((i, i + 1));
        }
        for i in 0..n {
            for j in (i + 1)..n {
                if sstate1_support_sstate2(&current_states[i], &current_states[j]) {
                    edges.push((j, i));
                }
            }
        }
        StackGraph { n, elem_edges, edges }
    }

    fn find_cycles(graph: &StackGraph) -> Vec<Vec<usize>> {
        simple_cycles_directed(graph.n, &graph.edges)
    }

    /// Python `extract_instruction_indices_from_cycle`: the element edges of consecutive node pairs
    /// on the cycle (at most one element edge per pair in the P3 graph; mutual exclusion of directions keeps the adjacency multitable unique).
    fn extract_instruction_indices_from_cycle(
        graph: &StackGraph,
        cycle: &[usize],
    ) -> Vec<usize> {
        let mut inst_indices = Vec::new();
        let elem_edge_by_pair: HashMap<(usize, usize), usize> = graph
            .elem_edges
            .iter()
            .map(|(f, t, idx)| ((*f, *t), *idx))
            .collect();
        for i in 0..cycle.len() {
            let cur = cycle[i];
            let next = cycle[(i + 1) % cycle.len()];
            if let Some(idx) = elem_edge_by_pair.get(&(cur, next)) {
                inst_indices.push(*idx);
            }
        }
        inst_indices
    }

    fn is_continuous_sequence(inst_idxs: &[usize]) -> bool {
        if inst_idxs.len() <= 1 {
            return true;
        }
        let mut sorted = inst_idxs.to_vec();
        sorted.sort_unstable();
        sorted.windows(2).all(|w| w[1] == w[0] + 1)
    }

    fn extract_reducible_sequences(graph: &StackGraph, cycles: &[Vec<usize>]) -> Vec<Vec<usize>> {
        let mut out = Vec::new();
        for cycle in cycles {
            if cycle.is_empty() {
                continue;
            }
            let inst_indices = Self::extract_instruction_indices_from_cycle(graph, cycle);
            if !inst_indices.is_empty() && Self::is_continuous_sequence(&inst_indices) {
                out.push(inst_indices);
            }
        }
        out
    }
}
