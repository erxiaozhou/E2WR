from typing import Mapping, Optional
import time

from reduction_analysis.ReduceUtil.ElemGuidedNodeListReducerMultiNode import ElemGuidedNodeListReducerMultiNode
from reduction_analysis.ReduceUtil.V6V1GraphHelper import SubGraphRepo

from .ReduceInsts_V5_util import OneElem
from .ReduceInsts_cfg_util import V7Cfg
from .V6V1GraphHelper import  GraphHelper, SubGraphSplitter, SubGraph, get_init_subgraph_repo
from reduction_analysis.ReduceUtil.OneNodeListReductionEnv import OneNodeListReductionCtx
from reduction_analysis.ReduceUtil.MutationInstsUtil import OneNodeListMutation
from reduction_analysis.ReduceUtil.ReduceInsts_V9_util import (
    NodeListElemInfo,
    V7MutationPlannerBase,
    V7DDNodeListData,
    finalize_multi_node_list_v9,
    is_timeout,
    run_probdd_for_multi_candidates,
)
from reduction_analysis.ASTInfo.AST import NodeList

def _run_reduce_round_v7_multi(
    *,
    cfg: V7Cfg,
    reduce_applier: ElemGuidedNodeListReducerMultiNode,
    nl2mutation_gen: dict[NodeList, "_SubGraphReduceStateV7"],
    expected_end_time: Optional[float],
) -> None:
    if is_timeout(expected_end_time):
        return

    if cfg.enable_dd:
        nl2dd_data: dict[NodeList, V7DDNodeListData] = {
            node_list: st.as_dd_data() for node_list, st in nl2mutation_gen.items()
        }

        cand_id2info: dict[int, tuple[NodeList, int]] = {}
        all_cand_ids: list[int] = []
        next_id = 0
        for node_list, st in nl2mutation_gen.items():
            for sg_idx in st.unreplaced_candidate_sg_idxs():
                cand_id2info[next_id] = (node_list, sg_idx)
                all_cand_ids.append(next_id)
                next_id += 1

        if not all_cand_ids:
            return

        minimal_config, nl2run_state = run_probdd_for_multi_candidates(
            task_id='V9SG_MULTI',
            reduce_applier=reduce_applier,
            nl2dd_data=nl2dd_data,
            expected_end_time=expected_end_time,
            all_cand_ids=all_cand_ids,
            cand_id2info=cand_id2info,
        )

        # Apply DD results outside the DD runner.
        for node_list, run_state in nl2run_state.items():
            if not run_state.had_success:
                continue
            st = nl2mutation_gen[node_list]
            if run_state.replaced_sg_idxs:
                st.replaced_sg_idxs.update(run_state.replaced_sg_idxs)
                st.on_dd_test_success(replaced_sg_idxs=run_state.replaced_sg_idxs)
            st.has_any_success = True

        print(f"V7DD multi minimal config size={len(minimal_config)}")
        return
    else:
        for node_list, st in nl2mutation_gen.items():
            greedy_reduce_largest_subgraph_v7(
                st,
                reducer=reduce_applier,
                expected_end_time=expected_end_time,
            )


def call_V9_multi_basic(
    *,
    ctx_by_node_list: dict[NodeList, OneNodeListReductionCtx],
    reduce_applier: ElemGuidedNodeListReducerMultiNode,
    input_elems: NodeListElemInfo,
    cfg: V7Cfg,
    rest_time: Optional[float] = None,
) -> NodeListElemInfo:
    expected_end_time = None if rest_time is None else time.time() + rest_time

    # Build per-NodeList SG state.
    nl2mutation_gen: dict[NodeList, _SubGraphReduceStateV7] = {}
    for node_list, elems in input_elems.node_list2elems.items():
        ctx = ctx_by_node_list[node_list]
        st = _SubGraphReduceStateV7(
            input_elem_info=NodeListElemInfo.from_single(node_list=node_list, elems=elems),
            ctx=ctx,
            cfg=cfg,
        )
        nl2mutation_gen[node_list] = st

    # Initial reduction round (initial candidates are built only once).
    _run_reduce_round_v7_multi(
        cfg=cfg,
        reduce_applier=reduce_applier,
        nl2mutation_gen=nl2mutation_gen,
        expected_end_time=expected_end_time,
    )

    if cfg.enable_ddg_split:
        # Build initial split pool only when split is enabled.
        nl2pool: dict[NodeList, set[int]] = {}
        for node_list, st in nl2mutation_gen.items():
            nl2pool[node_list] = set(st.sg_replacements) - st.replaced_sg_idxs

        while True:
            if is_timeout(expected_end_time):
                break

            any_split = False
            for node_list, st in nl2mutation_gen.items():
                to_split_sg_idxs = nl2pool[node_list]
                new_pool = _V7SplitAndRefreshRound().run(
                    state=st,
                    to_split_sg_idxs=to_split_sg_idxs,
                    replaced_sg_idxs=st.replaced_sg_idxs,
                )
                if new_pool is not None:
                    any_split = True
                    nl2pool[node_list] = new_pool
                else:
                    # Keep pool semantics clean: if nothing is splittable, the frontier is empty.
                    nl2pool[node_list] = set()

            if not any_split:
                break

            _run_reduce_round_v7_multi(
                cfg=cfg,
                reduce_applier=reduce_applier,
                nl2mutation_gen=nl2mutation_gen,
                expected_end_time=expected_end_time,
            )
            # Update pools based on DD/greedy results.
            for node_list, to_split_sg_idxs in nl2pool.items():
                to_split_sg_idxs -= nl2mutation_gen[node_list].replaced_sg_idxs

    nl2failed_sg_idxs: dict[NodeList, set[int]] = {}
    for node_list, st in nl2mutation_gen.items():
        nl2failed_sg_idxs[node_list] = st.unreplaced_candidate_sg_idxs()

    return finalize_multi_node_list_v9(
        reduce_applier=reduce_applier,
        nl2planner=nl2mutation_gen,
        nl2failed_sg_idxs=nl2failed_sg_idxs,
    )

class _SubGraphReduceStateV7(V7MutationPlannerBase):
    def __init__(
        self,
        *,
        input_elem_info: NodeListElemInfo,
        ctx: OneNodeListReductionCtx,
        cfg: V7Cfg,
    ):
        super().__init__(ctx=ctx, input_elem_info=input_elem_info, cfg=cfg)
        self.sg_manager: SubGraphSplitter = SubGraphSplitter(
            graph_helper=self.graph_helper)
        self.replaced_sg_idxs: set[int] = set()
        self.build_mutation_core()

    def _build_sg_repo(self)->SubGraphRepo:
        return get_init_subgraph_repo(self.graph_helper)

    def unreplaced_candidate_sg_idxs(self) -> set[int]:
        return set(self.sg_replacements) - self.replaced_sg_idxs



class _V7SplitAndRefreshRound:
    def run(
        self,
        *,
        state: "_SubGraphReduceStateV7",
        to_split_sg_idxs: set[int],
        replaced_sg_idxs: set[int],
    ) -> Optional[set[int]]:
        sg_idxs_to_split = {
            sg_idx
            for sg_idx in to_split_sg_idxs
            if sg_idx not in replaced_sg_idxs and not state.get_sg(sg_idx).has_one_elem
        }
        if not sg_idxs_to_split:
            return None

        new_pool: set[int] = set()
        state.sg_replacements = {}
        for parent_sg_idx in sorted(sg_idxs_to_split):
            children: list[SubGraph] = state.sg_manager.replace_a_graph(
                parent_sg_idx, 
                state.sg_repo,
                replace_for_VP=not state.cfg.use_VP
                )
            if len(children) == 1:  # no splitting; skip
                continue

            for child_sg in children:
             

                ok = state._maybe_register_sg_mutation(child_sg)
                if not ok:
                    continue
                new_pool.add(child_sg.idx)

        return new_pool


# ===============================================================================

def greedy_reduce_largest_subgraph_v7(
    st: _SubGraphReduceStateV7,
    *,
    reducer: ElemGuidedNodeListReducerMultiNode,
    expected_end_time: Optional[float],
) -> set[int]:
    if not st.unreplaced_candidate_sg_idxs():
        return set()

    print('Reduce node list SG (greedy-largest)')
    to_try: set[int] = set(st.unreplaced_candidate_sg_idxs())

    while to_try:
        if is_timeout(expected_end_time):
            break

        best_sg_idx = max(to_try, key=lambda i: _estimate_sg_gain_v7(st, i))
        ok = _try_replace_core_for_state_v7_multi(
            reducer=reducer,
            st=st,
            to_replace_subgraph_idxs={best_sg_idx},
        )
        if ok:
            to_try &= st.unreplaced_candidate_sg_idxs()
        else:
            to_try.remove(best_sg_idx)

    return st.unreplaced_candidate_sg_idxs()


def _estimate_sg_gain_v7(st: _SubGraphReduceStateV7, sg_idx: int) -> int:
    sg = st.get_sg(sg_idx)
    elem_idxs_in_sg = sorted(sg.sg_elem_idxs)
    raw_elems_in_sg = [st.input_elems[idx] for idx in elem_idxs_in_sg]
    mutated_elems_in_sg = st._materialize_mutated_elems_for_elem_idxs(
        elem_idxs_in_sg,
            st.sg_replacements[sg_idx].materialize_mutation(),
    )
    raw_len = sum(e.get_length() for e in raw_elems_in_sg)
    mutated_len = sum(e.get_length() for e in mutated_elems_in_sg)
    return raw_len - mutated_len


def _apply_mutation_v7(
    *,
    reducer: ElemGuidedNodeListReducerMultiNode,
    ori_node_list: NodeList,
    raw_elems: list[OneElem],
    mutation_elem_idx2new_elems: dict[int, list[OneElem]],
) -> bool:
    if not mutation_elem_idx2new_elems:
        return False
    return reducer.gen_replacement_by_elems_and_test_by_mutation_v2(
            mutations=[
                OneNodeListMutation(
                    ori_node_list=ori_node_list,
                    raw_elems=raw_elems,
                    mutation_elem_idx2new_elems=mutation_elem_idx2new_elems,
                )
            ]
        )


def _try_replace_core_for_state_v7_multi(
    *,
    reducer: ElemGuidedNodeListReducerMultiNode,
    st: "_SubGraphReduceStateV7",
    to_replace_subgraph_idxs: set[int],
) -> bool:
    candidate_mutation: dict[int, list[OneElem]] = {}
    for subgraph_idx in to_replace_subgraph_idxs:
        per_sg_mutation = st.sg_replacements[subgraph_idx].materialize_mutation()
        for k, v in per_sg_mutation.items():
            assert k not in candidate_mutation, f"V7 SG mutation conflict at elem idx {k}"
            candidate_mutation[k] = v

    merged_mutation: dict[int, list[OneElem]] = {}
    merged_mutation.update(candidate_mutation)
    merged_mutation.update(st.accepted_elem_mutation)

    ok = _apply_mutation_v7(
        reducer=reducer,
        ori_node_list=st.ctx.ori_node_list,
        raw_elems=st.input_elems,
        mutation_elem_idx2new_elems=merged_mutation,
    )

    if ok:
        st.accepted_elem_mutation.update(candidate_mutation)
        st.replaced_sg_idxs.update(to_replace_subgraph_idxs)
        st.has_any_success = True
    return ok

