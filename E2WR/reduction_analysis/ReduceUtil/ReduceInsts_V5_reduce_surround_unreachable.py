
from .ReduceInsts_V5_util import ArbitraryElemGroup, OneElem
from reduction_analysis.ReduceUtil.OneNodeListReductionEnv import OneNodeListReducerApplier
from reduction_analysis.ReduceUtil.OneNodeListReductionEnv import OneNodeListReductionCtx
from .ReduceInsts_V5_util import ImmGroup, ElemGroupBase
from .ReduceInsts_V5_util import last_is_unreachable_like, next_group_is_unreachable



def reduce_by_unreachable_like_inst(
    ctx: OneNodeListReductionCtx,
    elems: list[OneElem],
    reduce_applier: OneNodeListReducerApplier,
    DEBUG: bool = False,
) -> list[OneElem]:
    raw_elem_num = len(elems)
    reduced_elems:list[OneElem] = []
    raw_elems_length = sum(elem.get_length() for elem in elems)
    for elem in elems:
        reduced_elems.append(elem)
        if elem.tail_likes_unreachable():
            # 
            visited_elem_num = len(reduced_elems)
            if visited_elem_num == raw_elem_num:
                break
            mutation = {idx:[]for idx in range(visited_elem_num, raw_elem_num)}
            result = reduce_applier.gen_replacement_by_elems_and_test_by_mutation(
                ctx=ctx,
                raw_elems=elems,
                mutation_elem_idx2new_elems=mutation,
                check_invalid_and_return_false=not DEBUG
            )
            if result:
                reduce_applier.finalize(ctx.ori_node_list, raw_elems_length, reduced_elems)
                break
    return reduced_elems


def split_surround_unreachable_groups(elems: list[OneElem]) -> list[ElemGroupBase]:
    unprocessed_elems: list[OneElem] = []
    elem_groups: list[ElemGroupBase] = []
    for elem in elems:
        if elem.tail_likes_unreachable():
            if unprocessed_elems:
                elem_groups.append(ArbitraryElemGroup(unprocessed_elems))
                unprocessed_elems = []
            elem_groups.append(ImmGroup([elem]))
        else:
            unprocessed_elems.append(elem)
    if unprocessed_elems:
        elem_groups.append(ArbitraryElemGroup(unprocessed_elems))
    return elem_groups


def get_surround_unreachable_replaceable_group_idxs(
    elem_groups: list[ElemGroupBase],
) -> set[int]:
    can_replace_idxs: set[int] = set()
    for group_idx, group in enumerate(elem_groups):
        if len(group.elems) == 0:
            continue
        if last_is_unreachable_like(elem_groups, group_idx) or next_group_is_unreachable(elem_groups, group_idx):
            can_replace_idxs.add(group_idx)
    can_replace_idxs -= get_seq_unreachable_group_idx_starts(elem_groups)
    return can_replace_idxs
            

def get_seq_unreachable_group_idx_starts(
    elem_groups: list[ElemGroupBase],
):
    if not elem_groups:
        return set()
    len_ = len(elem_groups)
    result = set()
    for g_idx, group in enumerate(elem_groups):
        if g_idx < len_ - 1:
            if isinstance(group, ImmGroup):
                if group.is_unreachable_inst():
                    next_group = elem_groups[g_idx + 1]
                    if isinstance(next_group, ImmGroup):
                        if next_group.is_unreachable_inst():
                            result.add(g_idx)
    return result
