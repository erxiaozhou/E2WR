//! Mutation generation: Python `ReduceInsts_V7_mutation_util.py` (the ng mutation path) +
//! `materialize_replacements`.
//!
//! Parity notes:
//! - Python `OneElem` has no `__eq__` (identity comparison) — in mutation-conflict detection, two non-empty
//!   replacement lists are never equal; only "both empty" key conflicts are tolerable; Rust implements those semantics.
//! - `gen_produced_operand_elem` values are random (same distribution, sequence not chased, D-2).
//! - The `[get_elem_mutation_for_sg_ng:no-plan]` print kept in the V7 file is not ported
//!   (pure logging).

use std::collections::{BTreeMap, BTreeSet, HashMap};

use e2wr_ir::types::{FTy, ValTy};
use e2wr_ir::Inst;

use super::elem::{ElemTypeInfo, OneElem, OTy};
use super::graph_helper::{GraphHelper, SubGraph};
use super::vop::VopId;
use crate::remap::const_inst;

/// Python `ConsumedOperandInfo` / `ProducedOperandInfo` (operand always has a value).
#[derive(Clone, Debug)]
pub struct OperandReplacement {
    pub operand: VopId,
    pub replacement_elem: OneElem,
}

/// Python `_ReplacementComponentPlan`.
#[derive(Clone, Debug)]
pub struct ReplacementComponentPlan {
    pub anchor_elem_idx: usize,
    pub removed_elem_idxs: Vec<usize>,
    pub consumed_operands: Vec<OperandReplacement>,
    pub produced_operands: Vec<OperandReplacement>,
}

/// Python `Replacement`.
#[derive(Clone, Debug)]
pub struct Replacement {
    pub sg_idx: u32,
    pub is_operand_aware: bool,
    pub covered_elem_idxs: BTreeSet<usize>,
    pub materialized_mutation: BTreeMap<usize, Vec<OneElem>>,
    pub elem_idx2operands: HashMap<usize, Vec<VopId>>,
    pub consumed_operands: BTreeSet<VopId>,
    pub produced_operands: BTreeSet<VopId>,
}

impl Replacement {
    /// Python `Replacement.__init__` (the component_plans path; concrete_mutation
    /// is always None — ng mutations take only the operand-aware path, the sole entry after B-1 deleted naive mutations).
    pub fn from_component_plans(sg_idx: u32, component_plans: Vec<ReplacementComponentPlan>) -> Replacement {        let mut covered = BTreeSet::new();
        let mut consumed_all = BTreeSet::new();
        let mut produced_all = BTreeSet::new();
        let mut materialized: BTreeMap<usize, Vec<OneElem>> = BTreeMap::new();
        let mut idx2operands: HashMap<usize, Vec<VopId>> = HashMap::new();
        for plan in &component_plans {
            covered.extend(plan.removed_elem_idxs.iter().copied());
            consumed_all.extend(plan.consumed_operands.iter().map(|o| o.operand));
            produced_all.extend(plan.produced_operands.iter().map(|o| o.operand));

            let mut new_elems: Vec<OneElem> = plan
                .consumed_operands
                .iter()
                .map(|o| o.replacement_elem.clone())
                .collect();
            new_elems.extend(plan.produced_operands.iter().map(|o| o.replacement_elem.clone()));
            let mut operands: Vec<VopId> =
                plan.consumed_operands.iter().map(|o| o.operand).collect();
            operands.extend(plan.produced_operands.iter().map(|o| o.operand));
            materialized.insert(plan.anchor_elem_idx, new_elems);
            idx2operands.insert(plan.anchor_elem_idx, operands);
            for idx in plan.removed_elem_idxs.iter().skip(1) {
                materialized.insert(*idx, Vec::new());
                idx2operands.insert(*idx, Vec::new());
            }
        }
        Replacement {
            sg_idx,
            is_operand_aware: true,
            covered_elem_idxs: covered,
            materialized_mutation: materialized,
            elem_idx2operands: idx2operands,
            consumed_operands: consumed_all,
            produced_operands: produced_all,
        }
    }

    /// Python `Replacement.__init__` (the concrete_mutation path — CFN's
    /// pre-materialized replacement: not operand-aware; covered/consumed/produced all empty).
    pub fn from_concrete_mutation(
        sg_idx: u32,
        concrete_mutation: BTreeMap<usize, Vec<OneElem>>,
    ) -> Replacement {
        Replacement {
            sg_idx,
            is_operand_aware: false,
            covered_elem_idxs: BTreeSet::new(),
            materialized_mutation: concrete_mutation,
            elem_idx2operands: HashMap::new(),
            consumed_operands: BTreeSet::new(),
            produced_operands: BTreeSet::new(),
        }
    }

    /// Python `gen_concrete_elems`: filters replacement elements by blocked operands.
    pub fn gen_concrete_elems(&self, blocked_operands: &BTreeSet<VopId>) -> BTreeMap<usize, Vec<OneElem>> {
        if !self.is_operand_aware || blocked_operands.is_empty() {
            return self.materialized_mutation.clone();
        }
        let mut out = BTreeMap::new();
        for (elem_idx, elems) in &self.materialized_mutation {
            let operands = self.elem_idx2operands.get(elem_idx).cloned().unwrap_or_default();
            if operands.is_empty() {
                debug_assert!(elems.is_empty());
                out.insert(*elem_idx, Vec::new());
                continue;
            }
            debug_assert_eq!(elems.len(), operands.len());
            let filtered: Vec<OneElem> = elems
                .iter()
                .cloned()
                .zip(operands.iter())
                .filter(|(_, op)| !blocked_operands.contains(op))
                .map(|(e, _)| e)
                .collect();
            out.insert(*elem_idx, filtered);
        }
        out
    }

    /// Python `materialize_mutation`.
    pub fn materialize_mutation(&self) -> BTreeMap<usize, Vec<OneElem>> {
        self.materialized_mutation.clone()
    }
}

/// Python `materialize_replacements`: merges concrete mutations across replacements; on key conflicts only
/// "both empty" is tolerable (the equivalent semantics of Python OneElem identity comparison); otherwise a conflict error.
/// The empty error marker type: its only consumer (core_stage) drops it immediately; kept because it
/// is part of the U2 frozen signature (R-30 note; changing it requires revising the frozen interface).
#[derive(Debug)]
pub struct MutationConflict;

pub fn materialize_replacements(
    replacements: &[&Replacement],
) -> Result<BTreeMap<usize, Vec<OneElem>>, MutationConflict> {
    // Python: produced = the union of replacements' outputs, consumed = the union of inputs,
    // internal = their intersection (operands internalized across replacements).
    let mut produced_union = BTreeSet::new();
    let mut consumed_union = BTreeSet::new();
    for r in replacements.iter().filter(|r| r.is_operand_aware) {
        produced_union.extend(r.produced_operands.iter().copied());
        consumed_union.extend(r.consumed_operands.iter().copied());
    }
    let internal: BTreeSet<VopId> =
        produced_union.intersection(&consumed_union).copied().collect();
    let mut result: BTreeMap<usize, Vec<OneElem>> = BTreeMap::new();
    for repl in replacements {
        let cur = repl.gen_concrete_elems(&internal);
        for (k, v) in cur {
            if let Some(prev) = result.get(&k) {
                let both_empty = prev.is_empty() && v.is_empty();
                if !both_empty {
                    return Err(MutationConflict);
                }
            }
            result.insert(k, v);
        }
    }
    Ok(result)
}

/// Python `gen_produced_operand_elem`: produces one constant element by type.
pub fn gen_produced_operand_elem(operand_type: OTy, rng: &mut impl rand::Rng) -> OneElem {
    use wasmparser::ValType as V;
    let ty = operand_type.concrete().expect("produced operand must be concrete");
    let wp = match ty {
        ValTy::I32 => V::I32,
        ValTy::I64 => V::I64,
        ValTy::F32 => V::F32,
        ValTy::F64 => V::F64,
        ValTy::V128 => V::V128,
        ValTy::Funcref => V::FUNCREF,
        ValTy::Externref => V::EXTERNREF,
    };
    let inst = const_inst(&wp, rng).expect("const inst");
    OneElem::inst(inst, ElemTypeInfo::determined(&FTy::of(&[], &[ty])), None)
}

fn drop_replacement_elem() -> OneElem {
    OneElem::inst(Inst::Drop, ElemTypeInfo::drop_elem(), None)
}

/// Python `get_elem_mutation_for_sg_ng` (skip_num prefix alignment + component-plan building).
/// Returns None = an 'any' output operand or no plan.
pub fn get_elem_mutation_for_sg_ng(
    helper: &GraphHelper,
    sg: &SubGraph,
    rng: &mut impl rand::Rng,
) -> Option<Replacement> {
    assert!(!sg.components.is_empty());
    assert!(sg.components.iter().all(|c| !c.elem_idxs.is_empty()));

    let mut components = sg.components.clone();
    components.sort_by_key(|c| c.elem_idxs[0]);
    let bypass_cancel = !sg.enable_internal_cancel;

    let mut skip_num = 0usize;
    if !bypass_cancel {
        for (input_op, output_op) in sg
            .raw_external_input_ops
            .iter()
            .zip(sg.final_output_ops.iter())
        {
            if helper.vop_ty(*input_op) != helper.vop_ty(*output_op) {
                break;
            }
            skip_num += 1;
        }
    }
    let skipped_input_ops: BTreeSet<VopId> =
        sg.raw_external_input_ops[..skip_num].iter().copied().collect();
    let final_output_ops: &[VopId] = &sg.final_output_ops[skip_num..];

    let mut plans: Vec<ReplacementComponentPlan> = Vec::new();
    for (comp_i, comp) in components.iter().enumerate() {
        let (external_consumed, ops_to_rebuild): (Vec<VopId>, &[VopId]) = if bypass_cancel {
            (comp.taken_op_list.clone(), comp.gen_op_list.as_slice())
        } else {
            let consumed: Vec<VopId> = comp
                .taken_op_list
                .iter()
                .copied()
                .filter(|op| sg.ops_from_outside.contains(op) && !skipped_input_ops.contains(op))
                .collect();
            let rebuild: &[VopId] = if comp_i == components.len() - 1 {
                final_output_ops
            } else {
                &[]
            };
            (consumed, rebuild)
        };

        let mut produced: Vec<OperandReplacement> = Vec::new();
        for op in ops_to_rebuild {
            let ty = helper.vop_ty(*op);
            if ty == OTy::Any {
                return None;
            }
            produced.push(OperandReplacement {
                operand: *op,
                replacement_elem: gen_produced_operand_elem(ty, rng),
            });
        }
        let consumed: Vec<OperandReplacement> = external_consumed
            .iter()
            .map(|op| OperandReplacement { operand: *op, replacement_elem: drop_replacement_elem() })
            .collect();
        let mut removed = comp.elem_idxs.clone();
        removed.sort_unstable();
        let anchor = removed[0];
        plans.push(ReplacementComponentPlan {
            anchor_elem_idx: anchor,
            removed_elem_idxs: removed,
            consumed_operands: consumed,
            produced_operands: produced,
        });
    }

    if plans.is_empty() {
        return None;
    }
    Some(Replacement::from_component_plans(sg.idx, plans))
}
