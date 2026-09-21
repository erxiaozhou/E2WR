
from reduction_analysis.ProbDDUtil.ProbDDFactory import ProbDDFactory
from reduction_analysis.ReduceUtil.ElemGuidedNodeListReducerMF import transform_group_mutation_to_standard_param
from reduction_analysis.ASTInfo.AST import NodeList
from reduction_analysis.ProbDDUtil.adapt_util import get_test_cfg_func_for_dd
from typing import Optional
import time
from .ReduceInsts_V5_util import ArbitraryElemGroup, OneElem, StackChange, gen_type_for_graph
from reduction_analysis.ReduceUtil.OneNodeListReductionEnv import OneNodeListReducerApplier
from reduction_analysis.ReduceUtil.OneNodeListReductionEnv import OneNodeListReductionCtx
from .ReduceInsts_V5_util import MutElemGroup, ImmGroup, ElemGroupBase
from .ReduceInsts_V5_util import last_is_unreachable_like, next_group_is_unreachable
from .ReduceInsts_V9_util import NodeListElemInfo



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

def reduce_surrounding_groups(
    ctx: OneNodeListReductionCtx,
    elems: list[OneElem],
    reduce_applier: OneNodeListReducerApplier,
    rest_time: Optional[float] = None,
    DEBUG: bool = False
    ):
    elem_groups = split_surround_unreachable_groups(elems)
    reducer = SurroundUnreachableReducer(
        ctx=ctx,
        elem_groups=elem_groups,
        reduce_applier=reduce_applier,
        DEBUG=DEBUG,
    )
    result =  reducer.reduce(rest_time=rest_time)
    # 
    result_elems = []
    for group in result:
        result_elems.extend(group.elems)
    return result_elems



class SurroundUnreachableReducer:
    def __init__(self,
                 ctx: OneNodeListReductionCtx,
                 elem_groups: list[ElemGroupBase],
                 reduce_applier: OneNodeListReducerApplier,
                 DEBUG: bool = False
                 ):
        self.ctx = ctx
        self.elem_groups = elem_groups.copy()
        self.group_num = len(self.elem_groups)
        self.reduce_applier = reduce_applier
        self.ori_node_list = ctx.ori_node_list
        self.expected_end_time = None
        self.DEBUG = DEBUG
        self.raw_len = 0
        for group in elem_groups:
            # self.raw_len += group.
            for elem in group.elems:
                self.raw_len += elem.get_length()
        # 
        self.raw_groups =  elem_groups.copy()
        self.accepted_groups_mutation = {}
        #
        self.can_replace_idxs = get_surround_unreachable_replaceable_group_idxs(self.elem_groups)

    def gen_new_groups_and_try(self, to_save_group_idxs:list[int]) -> bool:
        if self.expected_end_time is not None and time.time() > self.expected_end_time:
            return False
        
        to_replaced_group_idxs = self.can_replace_idxs - set(to_save_group_idxs)
        # 
        new_mutation = {gid:MutElemGroup([]) for gid in to_replaced_group_idxs}
        cur_mutation = {}
        cur_mutation.update(self.accepted_groups_mutation)
        cur_mutation.update(new_mutation)
        # 
        raw_elems, mutation_elem_idx2new_elems = transform_group_mutation_to_standard_param(self.raw_groups, cur_mutation)
        # 
        result = self.reduce_applier.gen_replacement_by_elems_and_test_by_mutation(
            ctx=self.ctx,
            raw_elems=raw_elems,
            mutation_elem_idx2new_elems=mutation_elem_idx2new_elems,
            check_invalid_and_return_false=not self.DEBUG
        )
        if result:
            self.accepted_groups_mutation.update(new_mutation)
            new_elem_groups = []
            for idx in range(self.group_num):
                if idx in self.accepted_groups_mutation:
                    new_elem_groups.append(self.accepted_groups_mutation[idx])
                else:
                    new_elem_groups.append(self.raw_groups[idx])
            self.elem_groups = new_elem_groups
            self.has_success = True
        return result

    def reduce(self, rest_time: Optional[float] = None) -> list[ElemGroupBase]:
        self.has_success = False
        if rest_time is not None:
            self.expected_end_time = time.time() + rest_time
        # expected_end_time = None
        if not self.can_replace_idxs:
            return self.elem_groups
        # 
        test_config = get_test_cfg_func_for_dd(self.gen_new_groups_and_try)
        config = list(sorted(self.can_replace_idxs))
        dd = ProbDDFactory.get_default_probdd(test_config, task_id='V5NLU')
        minimal_config = dd(config, expected_end_time=self.expected_end_time)
        print(f"V5NLU minimal config: {minimal_config}")
        if self.has_success:
            # 
            cur_elems = []
            for group in self.elem_groups:
                cur_elems.extend(group.elems)
            self.reduce_applier.finalize(self.ori_node_list, self.raw_len, cur_elems)
        return self.elem_groups


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
