from typing import Optional
import time

from reduction_analysis.ReduceUtil.ElemGuidedNodeListReducerMultiNode import ElemGuidedNodeListReducerMultiNode
from reduction_analysis.ReduceUtil.V6V1GraphHelper import SubGraphRepo
from .ReduceInsts_cfg_util import V7Cfg
from .V6V1GraphHelper import get_eq_sgs
from reduction_analysis.ReduceUtil.OneNodeListReductionEnv import OneNodeListReducerApplier, OneNodeListReductionCtx
from reduction_analysis.ReduceUtil.ReduceInsts_V9_util import (
    NodeListElemInfo,
    V7DDNodeListData,
    V7MutationPlannerBase,
    finalize_multi_node_list_v9,
    is_timeout,
    run_probdd_for_multi_candidates,
)
from reduction_analysis.ASTInfo.AST import NodeList
def _select_global_batch_v7rev(
    *,
    nl2mgr: dict[NodeList, "_V7RevCandidateManager"],
    nl2deferred_unreplaceable: dict[NodeList, set[int]],
) -> list[tuple[NodeList, int]]:
    batch_pairs: list[tuple[NodeList, int]] = []
    for node_list, mgr in nl2mgr.items():
        # Equivalent to ReduceNodeListV7REV: available := unreplaced candidates minus overlaps and deferred.
        candidate_sg_idxs = mgr.unreplaced_candidate_sg_idxs()
        candidate_sg_idxs -= nl2deferred_unreplaceable[node_list]
        if not candidate_sg_idxs:
            continue
        batch = mgr.select_non_overlapping_sg_batch(
            candidate_sg_idxs=candidate_sg_idxs,
        )
        batch_pairs.extend((node_list, sg_idx) for sg_idx in batch)
    return batch_pairs


def call_V9_rev_multi_basic(
    *,
    ctx_by_node_list: dict[NodeList, OneNodeListReductionCtx],
    reduce_applier: OneNodeListReducerApplier,
    input_elems: NodeListElemInfo,
    cfg: V7Cfg,
    rest_time: Optional[float] = None,
) -> NodeListElemInfo:
    assert isinstance(reduce_applier, ElemGuidedNodeListReducerMultiNode)
    expected_end_time = None if rest_time is None else time.time() + rest_time

    nl2planner: dict[NodeList, _V7RevMutationInit] = {}
    for node_list, elems in input_elems.node_list2elems.items():
        ctx = ctx_by_node_list[node_list]
        nl2planner[node_list] = _V7RevMutationInit(
            ctx=ctx,
            input_elem_info=NodeListElemInfo.from_single(node_list=node_list, elems=elems),
            cfg=cfg,
        )
    nl2mgr: dict[NodeList, _V7RevCandidateManager] = {
        node_list: _V7RevCandidateManager(planner=planner) for node_list, planner in nl2planner.items()
    }

    nl2dd_data: dict[NodeList, V7DDNodeListData] = {
        node_list: planner.as_dd_data() for node_list, planner in nl2planner.items()
    }

    nl2deferred_unreplaceable: dict[NodeList, set[int]] = {
        node_list: set() for node_list in nl2planner.keys()
    }

    while True:
        if is_timeout(expected_end_time):
            break

        batch_pairs = _select_global_batch_v7rev(
            nl2mgr=nl2mgr,
            nl2deferred_unreplaceable=nl2deferred_unreplaceable,
        )
        if not batch_pairs:
            break

        # Mark all SGs in this batch as tried.
        nl2batch_sg_idxs: dict[NodeList, set[int]] = {node_list: set() for node_list in nl2planner.keys()}
        for node_list, sg_idx in batch_pairs:
            nl2planner[node_list].tried_sg_idxs.add(sg_idx)
            nl2batch_sg_idxs[node_list].add(sg_idx)

        cand_id2info: dict[int, tuple[NodeList, int]] = {}
        all_cand_ids: list[int] = []
        for i, (node_list, sg_idx) in enumerate(batch_pairs):
            cand_id2info[i] = (node_list, sg_idx)
            all_cand_ids.append(i)

        _, nl2run_state = run_probdd_for_multi_candidates(
            task_id='V9REV_MULTI',
            reduce_applier=reduce_applier,
            nl2dd_data=nl2dd_data,
            expected_end_time=expected_end_time,
            all_cand_ids=all_cand_ids,
            cand_id2info=cand_id2info,
        )

        for node_list, run_state in nl2run_state.items():
            if run_state.had_success:
                planner = nl2planner[node_list]
                if run_state.replaced_sg_idxs:
                    planner.replaced_sg_idxs.update(run_state.replaced_sg_idxs)
                planner.has_any_success = True
            else:
                # No progress for this NodeList: defer this batch for that NodeList,
                # equivalent to ReduceNodeListV7REV's per-batch defer.
                nl2deferred_unreplaceable[node_list].update(nl2batch_sg_idxs.get(node_list, set()))

    return finalize_multi_node_list_v9(
        reduce_applier=reduce_applier,
        nl2planner=nl2planner,
    )


class _V7RevMutationInit(V7MutationPlannerBase):
    def __init__(
        self,
        *,
        ctx: OneNodeListReductionCtx,
        input_elem_info: NodeListElemInfo,
        cfg: V7Cfg,
    ):
        super().__init__(ctx=ctx, input_elem_info=input_elem_info, cfg=cfg)
        self.replaced_sg_idxs: set[int] = set()
        self.tried_sg_idxs: set[int] = set()
        self.build_mutation_core()

    def _build_sg_repo(self)->SubGraphRepo:
        return get_eq_sgs(self.graph_helper)


class _V7RevCandidateManager:
    def __init__(self, *, planner: _V7RevMutationInit):
        self._planner = planner
        self._sg_idx2cost: dict[int, int] = {}
        self._calculate_cost()

    def _calculate_cost(self) -> None:
        for sg_idx in self._planner.sg_replacements.keys():
            self._sg_idx2cost[sg_idx] = self._estimate_sg_mutation_cost(sg_idx)

    def _sg_mutation_elem_idxs(self, sg_idx: int) -> set[int]:
        return set(self._planner.sg_replacements[sg_idx].covered_elem_idxs)

    def unreplaced_candidate_sg_idxs(self) -> set[int]:
        used_elem_idxs = set(self._planner.accepted_elem_mutation.keys())
        return {
            sg_idx
            for sg_idx, replacement in self._planner.sg_replacements.items()
            if not (replacement.covered_elem_idxs & used_elem_idxs)
        }

    def _estimate_sg_mutation_cost(self, sg_idx: int) -> int:
        sg = self._planner.get_sg(sg_idx)
        elem_idxs_in_sg = sorted(sg.sg_elem_idxs)
        mutated_elems_in_sg = self._planner._materialize_mutated_elems_for_elem_idxs(
            elem_idxs_in_sg,
            self._planner.sg_replacements[sg_idx].materialize_mutation(),
        )
        return sum(e.get_length() for e in mutated_elems_in_sg)

    def select_non_overlapping_sg_batch(
        self,
        *,
        candidate_sg_idxs: set[int],
    ) -> list[int]:
        used_elem_idxs = set(self._planner.accepted_elem_mutation.keys())
        remaining = [
            sg_idx
            for sg_idx in candidate_sg_idxs
            if not (self._sg_mutation_elem_idxs(sg_idx) & used_elem_idxs)
        ]
        if not remaining:
            return []

        remaining.sort(key=lambda i: self._sg_idx2cost.get(i, 10**12))

        batch: list[int] = []
        batch_used: set[int] = set()
        for sg_idx in remaining:
            cur_idxs = self._sg_mutation_elem_idxs(sg_idx)
            if cur_idxs & batch_used:
                continue
            batch.append(sg_idx)
            batch_used |= cur_idxs
        return batch
