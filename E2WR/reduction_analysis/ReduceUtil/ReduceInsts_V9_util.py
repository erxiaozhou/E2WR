from __future__ import annotations

from dataclasses import dataclass
import time
from typing import Callable, Mapping, Optional, Protocol

from reduction_analysis.ProbDDUtil.ProbDDFactory import ProbDDFactory
from reduction_analysis.ProbDDUtil.adapt_util import get_test_cfg_func_for_dd

from extract_block_mutator.Context import Context
from extract_block_mutator.InstUtil.InstReqUtil import get_inst_ty_req
from reduction_analysis.ASTInfo.AST import InstsNode, NodeList
from reduction_analysis.ASTState import ASTState
from reduction_analysis.ReduceUtil.ElemGuidedNodeListReducerMultiNode import ElemGuidedNodeListReducerMultiNode
from reduction_analysis.ReduceUtil.MutationInstsUtil import OneNodeListMutation
from reduction_analysis.ReduceUtil.OneNodeListReductionEnv import OneNodeListReductionCtx
from .ReduceInsts_V7_mutation_util import (
    Replacement,
    get_elem_mutation_for_sg_ng,
    materialize_replacements,
)
from .ReduceInsts_cfg_util import V7Cfg
from .ReduceInsts_V5_util import cur_is_more_naive_v2, get_type_info, ElemTypeInfo, OneElem
from .ReduceInsts_V5_util import RawElemsCache
from .V6V1GraphHelper import GraphHelper, SubGraph, SubGraphRepo, sg_is_cf_only
from .util import get_node_type_req


def is_timeout(expected_end_time: Optional[float]) -> bool:
    return expected_end_time is not None and time.time() > expected_end_time


def apply_elem_mutation(
    raw_elems: list[OneElem],
    mutation_elem_idx2new_elems: dict[int, list[OneElem]],
) -> list[OneElem]:
    if not mutation_elem_idx2new_elems:
        return raw_elems
    full_elem_list: list[OneElem] = []
    for elem_idx in range(len(raw_elems)):
        if elem_idx in mutation_elem_idx2new_elems:
            full_elem_list.extend(mutation_elem_idx2new_elems[elem_idx])
        else:
            full_elem_list.append(raw_elems[elem_idx])
    return full_elem_list


class NodeListPlannerState(Protocol):
    ctx: OneNodeListReductionCtx
    input_elems: list[OneElem]
    raw_length: int
    has_any_success: bool

    @property
    def cur_elems(self) -> list[OneElem]:
        ...

    def get_sg(self, sg_idx: int) -> SubGraph:
        ...


def finalize_out_in_reverse_inst_order(
    *,
    reduce_applier: ElemGuidedNodeListReducerMultiNode,
    node_list2raw_len: dict[NodeList, int],
    node_list2has_success: dict[NodeList, bool],
    out: dict[NodeList, list[OneElem]],
) -> None:
    # * If multiple NodeLists belong to the same function, replacing inst ranges
    # * must be applied in descending inst_idx order to keep later offsets stable.
    node_lists = [
        node_list
        for node_list in node_list2raw_len.keys()
        if node_list2has_success.get(node_list, False)
    ]
    node_lists.sort(key=lambda nl: (nl.loc.func_idx, nl.loc.inst_idx), reverse=True)

    for node_list in node_lists:
        reduce_applier.finalize(node_list, node_list2raw_len[node_list], out[node_list])


def finalize_multi_node_list_v9(
    *,
    reduce_applier: ElemGuidedNodeListReducerMultiNode,
    nl2planner: Mapping[NodeList, NodeListPlannerState],
) -> NodeListElemInfo:
    out: dict[NodeList, list[OneElem]] = {}
    node_list2raw_len: dict[NodeList, int] = {}
    node_list2has_success: dict[NodeList, bool] = {}

    for node_list, planner in nl2planner.items():
        out[node_list] = planner.cur_elems
        node_list2raw_len[node_list] = planner.raw_length
        node_list2has_success[node_list] = planner.has_any_success

    finalize_out_in_reverse_inst_order(
        reduce_applier=reduce_applier,
        node_list2raw_len=node_list2raw_len,
        node_list2has_success=node_list2has_success,
        out=out,
    )

    return NodeListElemInfo(node_list2elems=out)


@dataclass(slots=True)
class NodeListElemInfo:
    node_list2elems: dict[NodeList, list[OneElem]]

    def get_elem_num(self):
        return sum(len(elems) for elems in self.node_list2elems.values())

    def get_single_node_list_and_elems(self) -> tuple[NodeList, list[OneElem]]:
        if len(self.node_list2elems) != 1:
            raise ValueError(
                f'Expected exactly one NodeList in node_list2elems, got {len(self.node_list2elems)}'
            )
        return next(iter(self.node_list2elems.items()))

    @classmethod
    def from_single(cls, *, node_list: NodeList, elems: list[OneElem]) -> 'NodeListElemInfo':
        return cls(node_list2elems={node_list: elems})


class V7MutationPlannerBase:
    def __init__(
        self,
        *,
        ctx: OneNodeListReductionCtx,
        input_elem_info: NodeListElemInfo,
        cfg: V7Cfg,
    ) -> None:
        self.ctx = ctx
        self.cfg = cfg
        self.ori_node_list, self.input_elems = input_elem_info.get_single_node_list_and_elems()

        self.graph_helper = GraphHelper(
            elems=self.input_elems,
            context=ctx.context,
            init_stack=ctx.node_type.param_types,
            end_stack=ctx.node_type.result_types,
        )
        self.raw_length = sum(elem.get_length() for elem in self.input_elems)

        self.accepted_elem_mutation: dict[int, list[OneElem]] = {}

        # Shared flag used by finalization (reverse inst-order finalize needs per-NodeList success).
        self.has_any_success: bool = False
        self.sg_replacements: dict[int, Replacement] = {}
        self.sg_repo: SubGraphRepo = self._build_sg_repo()

    def _build_sg_repo(self) -> SubGraphRepo:
        raise NotImplementedError

    @property
    def cur_elems(self) -> list[OneElem]:
        return apply_elem_mutation(self.input_elems, self.accepted_elem_mutation)

    def build_mutation_core(self):
        for sg_idx, sg in self.sg_repo.items():
            if sg_is_cf_only(sg, self.input_elems):
                continue
            self._maybe_register_sg_mutation(sg)
    def _materialize_mutated_elems_for_elem_idxs(
        self,
        elem_idxs: list[int],
        mutation_elem_idx2new_elems: dict[int, list[OneElem]],
    ) -> list[OneElem]:
        out: list[OneElem] = []
        for idx in elem_idxs:
            assert idx in mutation_elem_idx2new_elems, f"Unexpected elem idx {idx} not in mutation"
            out.extend(mutation_elem_idx2new_elems[idx])
        return out

    def _is_sg_mutation_simplifying(
        self,
        sg: SubGraph,
        mutation: dict[int, list[OneElem]],
    ) -> bool:
        elem_idxs_in_sg = sorted(sg.sg_elem_idxs)
        raw_elems_in_sg = [self.input_elems[idx] for idx in elem_idxs_in_sg]
        mutated_elems_in_sg = self._materialize_mutated_elems_for_elem_idxs(
            elem_idxs_in_sg,
            mutation,
        )

        return cur_is_more_naive_v2(raw_elems_in_sg, mutated_elems_in_sg)

    def _maybe_register_sg_mutation(self, sg: SubGraph) -> bool:
        mutation = get_elem_mutation_for_sg_ng(sg, self.cfg)

        if mutation is None:
            return False

        if mutation.is_operand_aware:
            internal = mutation.consumed_operands & mutation.produced_operands
            concrete_mutation = mutation.gen_concrete_elems(
                skip_consumed_operands=internal,
                skip_produced_operands=internal,
            )
        else:
            concrete_mutation = mutation.materialize_mutation()

        if not self._is_sg_mutation_simplifying(sg, concrete_mutation):
            return False

        assert sg.idx not in self.sg_replacements
        self.sg_replacements[sg.idx] = mutation
        return True

    def get_sg(self, sg_idx: int) -> SubGraph:
        return self.sg_repo.get_sg_by_idx(sg_idx)

    def as_dd_data(self) -> "V7DDNodeListData":
        return V7DDNodeListData(
            raw_elems=self.input_elems,
            accepted_elem_mutation=self.accepted_elem_mutation,
            sg_replacements=self.sg_replacements,
        )


def try_replace_candidates_v7_multi_dd_test(
    *,
    reduce_applier: ElemGuidedNodeListReducerMultiNode,
    nl2dd_data: Mapping[NodeList, "V7DDNodeListData"],
    nl2run_state: Mapping[NodeList, "V7DDNodeListRunState"],
    expected_end_time: Optional[float],
    all_cand_ids: list[int],
    cand_id2info: dict[int, tuple[NodeList, int]],
    to_save_cand_ids: list[int],
) -> bool:
    if is_timeout(expected_end_time):
        return False

    to_replace = set(all_cand_ids)
    to_replace -= set(to_save_cand_ids)

    nl2candidate_replacements: dict[NodeList, list[Replacement]] = {
        node_list: [] for node_list in nl2dd_data.keys()
    }
    nl2candidate_mutation: dict[NodeList, dict[int, list[OneElem]]] = {
        node_list: {} for node_list in nl2dd_data.keys()
    }
    nl2replaced_sg_idxs: dict[NodeList, set[int]] = {
        node_list: set() for node_list in nl2dd_data.keys()
    }
    # print(f'Current To replace: {to_replace} {set(nl2dd_data[node_list].sg_replacements.keys())}')
    # assert len(set(dd_data.sg_replacements.keys())).issubset(to_replace)
    for cand_id in to_replace:
        node_list, sg_idx = cand_id2info[cand_id]
        dd_data = nl2dd_data[node_list]
        replacement = dd_data.sg_replacements[sg_idx]
        nl2candidate_replacements[node_list].append(replacement)
        nl2replaced_sg_idxs[node_list].add(sg_idx)

    mutations: list[OneNodeListMutation] = []
    for node_list, dd_data in nl2dd_data.items():
        cand = nl2candidate_mutation[node_list]
        cand.update(materialize_replacements(
            nl2candidate_replacements[node_list]
        ))

        merged_mutation = dict(dd_data.accepted_elem_mutation)
        merged_mutation.update(cand)
        if not merged_mutation:
            continue
        mutations.append(
            OneNodeListMutation(
                ori_node_list=node_list,
                raw_elems=dd_data.raw_elems,
                mutation_elem_idx2new_elems=merged_mutation,
            )
        )

    if not mutations:
        return False

    ok = reduce_applier.gen_replacement_by_elems_and_test_by_mutation_v2(mutations=mutations)
    if ok:
        for node_list in nl2dd_data.keys():
            dd_data = nl2dd_data[node_list]
            run_state = nl2run_state[node_list]
            cand = nl2candidate_mutation[node_list]
            did_mutate = bool(cand)
            if did_mutate:
                dd_data.accepted_elem_mutation.update(cand)

            replaced = nl2replaced_sg_idxs[node_list]
            if replaced:
                run_state.replaced_sg_idxs |= replaced

            if did_mutate or replaced:
                run_state.had_success = True
    return ok


@dataclass(slots=True)
class V7DDNodeListData:
    raw_elems: list[OneElem]
    accepted_elem_mutation: dict[int, list[OneElem]]
    sg_replacements: Mapping[int, Replacement]


@dataclass(slots=True)
class V7DDNodeListRunState:
    replaced_sg_idxs: set[int]
    had_success: bool


@dataclass(slots=True)
class _V7DDReduceFunc:
    reduce_applier: ElemGuidedNodeListReducerMultiNode
    nl2dd_data: Mapping[NodeList, V7DDNodeListData]
    nl2run_state: Mapping[NodeList, V7DDNodeListRunState]
    expected_end_time: Optional[float]
    all_cand_ids: list[int]
    cand_id2info: dict[int, tuple[NodeList, int]]

    def __call__(self, to_save_cand_ids: list[int]) -> bool:
        return try_replace_candidates_v7_multi_dd_test(
            reduce_applier=self.reduce_applier,
            nl2dd_data=self.nl2dd_data,
            nl2run_state=self.nl2run_state,
            expected_end_time=self.expected_end_time,
            all_cand_ids=self.all_cand_ids,
            cand_id2info=self.cand_id2info,
            to_save_cand_ids=to_save_cand_ids,
        )
class RawElemsEnv(Protocol):
    context: Context
    ori_node_list: NodeList


def get_raw_elems_from_env(*, ast_state: ASTState, ctx: RawElemsEnv) -> list[OneElem]:
    elems = []
    elem_types: list[ElemTypeInfo] = []

    for node in ctx.ori_node_list.sub_nodes:
        if isinstance(node, InstsNode):
            for inst in node.get_insts():
                elems.append(inst)
                inst_type_req = get_inst_ty_req(inst, ctx.context)
                type_info = get_type_info(inst_type_req, inst)
                elem_types.append(type_info)
        else:
            elems.append(node)
            node_type_req = get_node_type_req(ast_state, node)
            type_info = get_type_info(node_type_req, None)
            elem_types.append(type_info)

    return [
        OneElem(elem_idx, elem, elem_type, raw_index=elem_idx)
        for elem_idx, (elem, elem_type) in enumerate(zip(elems, elem_types))
    ]


def finalize_process(reduce_applier, raw_elem_num: int, cur_elems: NodeListElemInfo):
    cur_raw_elem_num = 0
    for elems in cur_elems.node_list2elems.values():
        cur_raw_elem_num += RawElemsCache.get_raw_idxs_from_elems(elems)[1]
    final_result = cur_raw_elem_num < raw_elem_num
    cur_nodes = reduce_applier.actual_nodes
    return final_result, cur_nodes


def run_probdd_for_multi_candidates(
    *,
    task_id: str,
    reduce_applier: ElemGuidedNodeListReducerMultiNode,
    nl2dd_data: Mapping[NodeList, V7DDNodeListData],
    expected_end_time: Optional[float],
    all_cand_ids: list[int],
    cand_id2info: dict[int, tuple[NodeList, int]],
) -> tuple[list[int], dict[NodeList, V7DDNodeListRunState]]:
    nl2run_state: dict[NodeList, V7DDNodeListRunState] = {
        node_list: V7DDNodeListRunState(replaced_sg_idxs=set(), had_success=False)
        for node_list in nl2dd_data.keys()
    }

    reduce_func = _V7DDReduceFunc(
        reduce_applier=reduce_applier,
        nl2dd_data=nl2dd_data,
        nl2run_state=nl2run_state,
        expected_end_time=expected_end_time,
        all_cand_ids=all_cand_ids,
        cand_id2info=cand_id2info,
    )
    test_config = get_test_cfg_func_for_dd(reduce_func)
    dd = ProbDDFactory.get_default_probdd(test_config, task_id=task_id)
    minimal_config = dd(list(all_cand_ids), expected_end_time=expected_end_time)
    return minimal_config, nl2run_state