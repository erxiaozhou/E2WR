from reduction_analysis.ProbDDUtil.ProbDDFactory import ProbDDFactory
from reduction_analysis.ProbDDUtil.adapt_util import get_test_cfg_func_for_dd
from .ReduceInsts_V5_util import OneElem, StackChange, cal_rest_time_and_reset_t0, gen_replacement_by_stack_change, gen_type_for_graph
from .V6V1GraphHelper import GraphHelper
import time
from typing import Optional
from .OneNodeListReductionEnv import OneNodeListReductionCtx, OneNodeListReducerApplier




def remove_cf_elem(
    ctx: OneNodeListReductionCtx,
    elems: list[OneElem],
    reduce_applier: OneNodeListReducerApplier,
    rest_time: Optional[float] = None,
    DEBUG: Optional[bool] = None,
) -> list[OneElem]:
    if DEBUG is None:
        DEBUG = ctx.DEBUG
    if rest_time is not None:
        rest_time = min(rest_time, 600)
    if len(elems) ==0:
        return elems
    rest_time, t0 = cal_rest_time_and_reset_t0(rest_time, time.time(), after_what='init  elem-level removal')
    # * step 1 : remove single non-cf elem
    final_result = OneNonDtypedElemReducer(
        ctx=ctx,
        reduce_applier=reduce_applier,
        input_elems=elems,
        DEBUG=DEBUG,
        ignore_idxs=set()
    ).reduce(
        rest_time=rest_time
    )
    return final_result
    # * step 2 : remove cf elem
    
    # for _ in range(1):
    # return final_result, cur_nodes
    # raise NotImplementedError

 


class OneElemReducerBase:
    def __init__(self,
        ctx: OneNodeListReductionCtx,
        reduce_applier: OneNodeListReducerApplier,
        input_elems:list[OneElem],
        DEBUG:bool,
        ignore_idxs:set[int]
    ):
        self.input_elems = input_elems
        self.raw_elem_num = len(input_elems)
        self.raw_elems_length = sum(elem.get_length() for elem in input_elems)
        self.DEBUG = DEBUG
        self.reduce_applier = reduce_applier
        self.ctx = ctx
        self.graph_helper = GraphHelper(
            elems=input_elems, 
            context=ctx.context, 
            init_stack=ctx.node_type.param_types, 
            end_stack=ctx.node_type.result_types
            )
        self.expected_end_time = None
        self.ignore_idxs = ignore_idxs
        self.accepted_mutation:dict[int, list[OneElem]] = {}
    @property
    def cur_elems(self):
        elems = []
        for idx in range(self.raw_elem_num):
            if idx in self.accepted_mutation:
                elems.extend(self.accepted_mutation[idx])
            else:
                elems.append(self.input_elems[idx])
        return elems



    def _is_timeout(self)->bool:
        if self.expected_end_time is None:
            return False
        return time.time() > self.expected_end_time


 
class OneNonDtypedElemReducer(OneElemReducerBase):
    def __init__(self,
        ctx: OneNodeListReductionCtx,
        reduce_applier: OneNodeListReducerApplier,
        input_elems:list[OneElem],
        DEBUG:bool,
        ignore_idxs:set[int]
    ):
        super().__init__(
            ctx=ctx,
            reduce_applier=reduce_applier,
            input_elems=input_elems,
            DEBUG=DEBUG,
            ignore_idxs=ignore_idxs
        )
        # 
        self.raw_to_replace_idxs = set()
        self.actual_stack_changes:dict[int, StackChange] = {}
        for elem_idx, elem in enumerate(input_elems):
            if elem_idx in self.ignore_idxs:
                continue
            if elem.is_cf_related_inst() or elem.stack_change.is_not_determined_type:
                taken_num, gen_strs = self.graph_helper.count_dependency_on_cf(elem_idx)
                if 'any' in gen_strs:
                    continue
                stack_change = StackChange(
                    taken_op_type_list=[gen_type_for_graph('any') for _ in range(taken_num)],
                    gen_types=gen_strs
                )
                self.actual_stack_changes[elem_idx] = stack_change
                self.raw_to_replace_idxs.add(elem_idx)
        
        # 


    def reduce(self,
               rest_time: Optional[float] = None
               ) -> list[OneElem]:
        print('One Elem Level CF Reduction')
        if rest_time is not None:
            self.expected_end_time = time.time() + rest_time
        self.decompose_have_success = False
        if not self.raw_to_replace_idxs:
            return  self.cur_elems
        self.raw_elems_before_elem_reduction = self.cur_elems.copy()
        test_config = get_test_cfg_func_for_dd(self.try_replace_one_elem)
        # 

        config = sorted(list(self.raw_to_replace_idxs))
        dd = ProbDDFactory.get_default_probdd(test_config, task_id='V6OneElem Remove CF')
        minimal_config = dd(config, expected_end_time=self.expected_end_time)
        print(f"Remove CF minimal config: {minimal_config}")
        
        if self.decompose_have_success:
            self.reduce_applier.finalize(
                self.ctx.ori_node_list,
                self.raw_elems_length,
                self.cur_elems,
            )
        return  self.cur_elems



    def try_replace_one_elem(
        self,
        to_save_elem_group_idxs:list[int],
    )->bool:
        if self._is_timeout():
            return False
        # 
        new_to_replace_elem_idxs = self.raw_to_replace_idxs - set(to_save_elem_group_idxs)
        if not new_to_replace_elem_idxs:
            return False
        idx2new_elems:dict[int, list[OneElem]] = {}
        

        for elem_idx in new_to_replace_elem_idxs:
            considered_stack_change = self.actual_stack_changes[elem_idx]
            new_elems = gen_replacement_by_stack_change(considered_stack_change)
            idx2new_elems[elem_idx] = new_elems
        # 
        cur_mutation = {}
        cur_mutation.update(idx2new_elems)
        cur_mutation.update(self.accepted_mutation)
        assert cur_mutation
        # 
        result = self.reduce_applier.gen_replacement_by_elems_and_test_by_mutation(
            ctx=self.ctx,
            raw_elems=self.raw_elems_before_elem_reduction,
            mutation_elem_idx2new_elems=cur_mutation,
            check_invalid_and_return_false=True
        )
        if result:
            self.raw_to_replace_idxs -= new_to_replace_elem_idxs
            self.decompose_have_success = True
            self.accepted_mutation.update(idx2new_elems)

        return result

