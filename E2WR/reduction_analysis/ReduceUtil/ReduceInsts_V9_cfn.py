from typing import Optional
import time

from reduction_analysis.ASTInfo.AST import NodeList
from reduction_analysis.ReduceUtil.ElemGuidedNodeListReducerMultiNode import ElemGuidedNodeListReducerMultiNode
from reduction_analysis.ReduceUtil.ElemGuidedNodeListReducerMF import transform_group_mutation_to_standard_param
from reduction_analysis.ReduceUtil.OneNodeListReductionEnv import OneNodeListReducerApplier, OneNodeListReductionCtx
from reduction_analysis.ReduceUtil.ReduceInsts_V9_util import (
    NodeListElemInfo,
    V7DDNodeListData,
    apply_elem_mutation,
    finalize_out_in_reverse_inst_order,
    is_timeout,
    run_probdd_for_multi_candidates,
)
from .ReduceInsts_V7_mutation_util import Replacement

from .ReduceInsts_V5_util import (
    MutElemGroup,
    OneElem,
)
from .ReduceInsts_V5_reduce_surround_unreachable import (
    get_surround_unreachable_replaceable_group_idxs,
    split_surround_unreachable_groups,
)

class _CFNPlanner:
    def __init__(
        self,
        *,
        ctx: OneNodeListReductionCtx,
        elems: list[OneElem],
    ):
        self.ctx = ctx
        self.input_elems = elems
        self.raw_elems_length = sum(elem.get_length() for elem in elems)
        self.has_any_success = False

        self.accepted_elem_mutation: dict[int, list[OneElem]] = {}
        self.sg_replacements: dict[int, Replacement] = {}
        self.candidate_sg_idxs: set[int] = set()
        self.replaced_sg_idxs: set[int] = set()

        self._build_candidates()

    def as_dd_data(self) -> V7DDNodeListData:
        return V7DDNodeListData(
            raw_elems=self.input_elems,
            accepted_elem_mutation=self.accepted_elem_mutation,
            sg_replacements=self.sg_replacements,
        )

    def _build_candidates(self) -> None:
        raw_groups = split_surround_unreachable_groups(self.input_elems)
        if not raw_groups:
            return

        can_replace_idxs = get_surround_unreachable_replaceable_group_idxs(raw_groups)

        for gid in sorted(can_replace_idxs):
            _, mutation = transform_group_mutation_to_standard_param(
                raw_groups,
                {gid: MutElemGroup([])},
            )
            if not mutation:
                continue
            self.candidate_sg_idxs.add(gid)
            self.sg_replacements[gid] = Replacement(
                sg_idx=gid,
                component_plans=(),
                concrete_mutation=mutation,
            )


def call_V9_cfn_multi_basic(
    *,
    ctx_by_node_list: dict[NodeList, OneNodeListReductionCtx],
    reduce_applier: OneNodeListReducerApplier,
    input_elems: NodeListElemInfo,
    rest_time: Optional[float] = None,
) -> NodeListElemInfo:
    assert isinstance(reduce_applier, ElemGuidedNodeListReducerMultiNode)
    expected_end_time = None if rest_time is None else time.time() + rest_time

    nl2planner: dict[NodeList, _CFNPlanner] = {
        node_list: _CFNPlanner(ctx=ctx_by_node_list[node_list], elems=elems)
        for node_list, elems in input_elems.node_list2elems.items()
    }
    nl2dd_data: dict[NodeList, V7DDNodeListData] = {
        node_list: planner.as_dd_data() for node_list, planner in nl2planner.items()
    }

    cand_id2info: dict[int, tuple[NodeList, int]] = {}
    all_cand_ids: list[int] = []
    next_id = 0
    for node_list, planner in nl2planner.items():
        for sg_idx in sorted(planner.candidate_sg_idxs):
            cand_id2info[next_id] = (node_list, sg_idx)
            all_cand_ids.append(next_id)
            next_id += 1

    if all_cand_ids and not is_timeout(expected_end_time):
        _, nl2run_state = run_probdd_for_multi_candidates(
            task_id='V9CFN_MULTI',
            reduce_applier=reduce_applier,
            nl2dd_data=nl2dd_data,
            expected_end_time=expected_end_time,
            all_cand_ids=all_cand_ids,
            cand_id2info=cand_id2info,
        )
        for node_list, run_state in nl2run_state.items():
            if not run_state.had_success:
                continue
            planner = nl2planner[node_list]
            if run_state.replaced_sg_idxs:
                planner.replaced_sg_idxs.update(run_state.replaced_sg_idxs)
            planner.has_any_success = True

    out: dict[NodeList, list[OneElem]] = {}
    node_list2raw_len: dict[NodeList, int] = {}
    node_list2has_success: dict[NodeList, bool] = {}
    for node_list, planner in nl2planner.items():
        out[node_list] = apply_elem_mutation(planner.input_elems, planner.accepted_elem_mutation)
        node_list2raw_len[node_list] = planner.raw_elems_length
        node_list2has_success[node_list] = planner.has_any_success

    finalize_out_in_reverse_inst_order(
        reduce_applier=reduce_applier,
        node_list2raw_len=node_list2raw_len,
        node_list2has_success=node_list2has_success,
        out=out,
    )
    return NodeListElemInfo(node_list2elems=out)
