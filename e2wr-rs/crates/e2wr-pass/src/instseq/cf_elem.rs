//! U5: the CF_ELEM stage (control-flow element-level deletion).
//! Mirrors Python `ReduceUtil/OneElemLevelReduce.py` `remove_cf_elem` +
//! `OneNonDtypedElemReducer` (dead-code prints not ported; `cal_rest_time_and_
//! reset_t0` is a timing+printing identity transform, not ported).
//!
//! Validity guarantee (D-12): a candidate element's actual stack effects are measured by the value-operand graph (precise dataflow);
//! elements whose outputs contain ANY are skipped outright; the replacement sequence = drop×consumed (type-agnostic)
//! plus constants per the measured output types, net effect exactly zero. A non-cf element accessing stack_change
//! is an assert crash in Python for not_sure elements (in the measured corpus, non-cf elements always have
//! determined/Arb types — a dead path); Rust's accessor panics with the same semantics.
//!
//! Known quirks copied verbatim: Python's None-budget path crashes at the timing print (P-27, hence
//! rest_time is required); the probe chain hardcodes check_invalid to True.

use std::collections::BTreeMap;
use std::collections::BTreeSet;
use std::time::SystemTime;

use anyhow::Result;
use e2wr_dd::factory::DdFactory;
use e2wr_dd::probdd::ProbDD;

use crate::instseq::core_stage::apply_elem_mutation;
use crate::instseq::elem::{
    gen_replacement_by_stack_change, OneElem, StackChange, OTy,
};
use crate::instseq::graph_helper::GraphHelper;
use crate::instseq::reduce_ctx::NodeListReductionCtx;
use crate::instseq::trial::{deadline_after, deadline_passed, StageApplier};

/// Python `remove_cf_elem`. `rest_time` is required (P-27: Python's None path
/// crashes outright at the timing print; real runs always have a budget); internally takes min(rest_time, 600).
pub fn remove_cf_elem(
    ctx: &NodeListReductionCtx,
    elems: &[OneElem],
    reduce_applier: &mut dyn StageApplier,
    dd_factory: &DdFactory,
    rest_time: f64,
) -> Result<Vec<OneElem>> {
    let rest_time = rest_time.min(600.0);
    if elems.is_empty() {
        return Ok(elems.to_vec());
    }
    let mut reducer = OneNonDtypedElemReducer::new(ctx, reduce_applier, elems)?;
    reducer.reduce(dd_factory, rest_time)
}

struct OneNonDtypedElemReducer<'a> {
    input_elems: Vec<OneElem>,
    raw_elem_num: usize,
    raw_elems_length: u32,
    applier: &'a mut dyn StageApplier,
    graph_helper: GraphHelper,
    expected_end_time: Option<SystemTime>,
    accepted_mutation: BTreeMap<usize, Vec<OneElem>>,
    raw_to_replace_idxs: BTreeSet<usize>,
    actual_stack_changes: BTreeMap<usize, StackChange>,
    raw_elems_before_elem_reduction: Vec<OneElem>,
    decompose_have_success: bool,
    /// Constant-value randomness source (same distribution, D-2).
    rng: rand::rngs::ThreadRng,
}

impl<'a> OneNonDtypedElemReducer<'a> {
    fn new(
        ctx: &'a NodeListReductionCtx,
        applier: &'a mut dyn StageApplier,
        input_elems: &[OneElem],
    ) -> Result<OneNonDtypedElemReducer<'a>> {
        let graph_helper = GraphHelper::new(
            input_elems,
            Some(&ctx.context),
            &ctx.node_type.params,
            Some(&ctx.node_type.results),
        );
        let raw_elems_length = applier.cal_elems_length(input_elems);
        Ok(OneNonDtypedElemReducer {
            input_elems: input_elems.to_vec(),
            raw_elem_num: input_elems.len(),
            raw_elems_length,
            applier,
            graph_helper,
            expected_end_time: None,
            accepted_mutation: BTreeMap::new(),
            raw_to_replace_idxs: BTreeSet::new(),
            actual_stack_changes: BTreeMap::new(),
            raw_elems_before_elem_reduction: Vec::new(),
            decompose_have_success: false,
            rng: rand::thread_rng(),
        })
    }

    /// Python `__init__`'s candidate scan (ignore_idxs always empty; no parameter).
    fn scan_candidates(&mut self) {
        for elem_idx in 0..self.raw_elem_num {
            let elem = &self.input_elems[elem_idx];
            // Python's `or` short-circuit: cf elements never ask for stack_change (not_sure cf
            // elements therefore never crash); non-cf elements access stack_change (not_sure means
            // panic, a dead path).
            let is_candidate = elem.is_cf_related_inst()
                || elem.stack_change().is_not_determined_type;
            if !is_candidate {
                continue;
            }
            let (taken_num, gen_strs) = self.graph_helper.count_dependency_on_cf(elem_idx);
            if gen_strs.contains(&OTy::Any) {
                continue;
            }
            let stack_change = StackChange::new(
                std::iter::repeat_n(OTy::Any, taken_num).collect(),
                gen_strs,
            );
            self.actual_stack_changes.insert(elem_idx, stack_change);
            self.raw_to_replace_idxs.insert(elem_idx);
        }
    }

    // R-30: the old cur_elems method was a verbatim copy of core_stage::apply_elem_mutation
    // (minus only the empty-dict fast return; behaviorally equivalent); deleted, the call site uses the shared version.

    // R-28: the same-shaped method-version timeout check is_timeout was deleted; call sites uniformly use
    // trial::deadline_passed(self.expected_end_time).

    fn reduce(&mut self, dd_factory: &DdFactory, rest_time: f64) -> Result<Vec<OneElem>> {
        self.expected_end_time = Some(deadline_after(rest_time));
        self.scan_candidates();
        if self.raw_to_replace_idxs.is_empty() {
            return Ok(apply_elem_mutation(&self.input_elems, &self.accepted_mutation));
        }
        self.raw_elems_before_elem_reduction = apply_elem_mutation(&self.input_elems, &self.accepted_mutation);
        let config: Vec<u32> =
            self.raw_to_replace_idxs.iter().map(|i| *i as u32).collect();
        let mut dd: ProbDD<u32> = dd_factory.create_probdd();
        let expected_end_time = self.expected_end_time;
        // Python's minimal_config only prints; never consumed.
        let _keep = crate::common::run_probdd_capturing(
            &mut dd,
            &config,
            None,
            expected_end_time,
            |to_save: &[u32]| self.try_replace_one_elem(to_save),
        )?;
        if self.decompose_have_success {
            self.applier
                .finalize(self.raw_elems_length, &apply_elem_mutation(&self.input_elems, &self.accepted_mutation))?;
        }
        Ok(apply_elem_mutation(&self.input_elems, &self.accepted_mutation))
    }

    fn try_replace_one_elem(&mut self, to_save_elem_group_idxs: &[u32]) -> Result<bool> {
        if deadline_passed(self.expected_end_time) {
            return Ok(false);
        }
        let saved: BTreeSet<usize> =
            to_save_elem_group_idxs.iter().map(|i| *i as usize).collect();
        let new_to_replace: BTreeSet<usize> =
            self.raw_to_replace_idxs.difference(&saved).copied().collect();
        if new_to_replace.is_empty() {
            return Ok(false);
        }
        let mut idx2new_elems: BTreeMap<usize, Vec<OneElem>> = BTreeMap::new();
        for elem_idx in &new_to_replace {
            let sc = self
                .actual_stack_changes
                .get(elem_idx)
                .expect("candidate has stack change");
            let new_elems = gen_replacement_by_stack_change(sc, &mut self.rng);
            idx2new_elems.insert(*elem_idx, new_elems);
        }
        let mut cur_mutation: BTreeMap<usize, Vec<OneElem>> = BTreeMap::new();
        cur_mutation.extend(idx2new_elems.iter().map(|(k, v)| (*k, v.clone())));
        for (k, v) in &self.accepted_mutation {
            cur_mutation.insert(*k, v.clone());
        }
        // Python: assert cur_mutation (always non-empty: new_to_replace is non-empty).
        // Python hardcodes check_invalid_and_return_false=True.
        let result = self.applier.try_mutation(
            &self.raw_elems_before_elem_reduction,
            &cur_mutation,
            Some(true),
        )?;
        if result {
            for idx in &new_to_replace {
                self.raw_to_replace_idxs.remove(idx);
            }
            self.decompose_have_success = true;
            self.accepted_mutation.extend(idx2new_elems);
        }
        Ok(result)
    }
}
