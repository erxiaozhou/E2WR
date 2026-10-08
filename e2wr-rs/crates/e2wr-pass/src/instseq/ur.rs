//! U5: the UR stage (dead-code deletion after terminal instructions).
//! Mirrors Python `ReduceUtil/ReduceInsts_V5_reduce_surround_unreachable.py`
//! `reduce_by_unreachable_like_inst` (the grouping functions like split_surround_unreachable_groups
//! belong to the CFN stage (U6) and are not in this module).

use std::collections::BTreeMap;

use anyhow::Result;

use crate::instseq::elem::OneElem;
use crate::instseq::trial::StageApplier;

/// Python `reduce_by_unreachable_like_inst`: on an element whose tail looks like
/// unreachable (return/br/br_table/unreachable), probe "delete everything after it"; on success
/// finalize wraps up; the probe's check_invalid = !DEBUG.
pub fn reduce_by_unreachable_like_inst(
    elems: &[OneElem],
    reduce_applier: &mut dyn StageApplier,
    debug: bool,
) -> Result<Vec<OneElem>> {
    let raw_elem_num = elems.len();
    let mut reduced_elems: Vec<OneElem> = Vec::new();
    let raw_elems_length = reduce_applier.cal_elems_length(elems);
    for elem in elems {
        reduced_elems.push(elem.clone());
        if elem.tail_likes_unreachable() {
            let visited_elem_num = reduced_elems.len();
            if visited_elem_num == raw_elem_num {
                break;
            }
            let mutation: BTreeMap<usize, Vec<OneElem>> =
                (visited_elem_num..raw_elem_num).map(|idx| (idx, vec![])).collect();
            let result = reduce_applier.try_mutation(elems, &mutation, Some(!debug))?;
            if result {
                reduce_applier.finalize(raw_elems_length, &reduced_elems)?;
                break;
            }
        }
    }
    Ok(reduced_elems)
}
