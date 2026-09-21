from typing import Optional

from reduction_analysis.ProbDDUtil.ProbDDFactory import ProbDDFactory

from reduction_analysis.ProbDDUtil.adapt_util import get_test_cfg_func_for_dd
import time

from reduction_analysis.ReduceUtil.ReduceInsts_V5_util import cur_is_more_naive

from .ReduceInsts_V5_util import OneElem
from .ReduceInsts_cfg_util import V7Cfg
from .V6V1GraphHelper import  GraphHelper, SubGraphSplitter, SubGraph, get_init_subgraph_repo
from .V6V1GraphHelper import sg_is_cf_only
from .ReduceInsts_V7_mutation_util import get_elem_mutation_for_sg_naive, get_elem_mutation_for_sg_ng
from reduction_analysis.ReduceUtil.OneNodeListReductionEnv import OneNodeListReducerApplier, OneNodeListReductionCtx
from reduction_analysis.ReduceUtil.ReduceInsts_V9_util import NodeListElemInfo
NO_SKIP_INSTS_TH = 500
SG_GROUP_TH = 10000



def call_V7(
    ctx: OneNodeListReductionCtx,
    reduce_applier: OneNodeListReducerApplier,
        input_elems: NodeListElemInfo,
        cfg: V7Cfg,
        rest_time: Optional[float] = None,
        ) -> NodeListElemInfo:
    reducer = ReduceNodeListV7(
        ctx=ctx,
        reduce_applier=reduce_applier,
        input_elem_info=input_elems,
        cfg=cfg,
        rest_time=rest_time,
    )
    result = reducer.reduce()
    return NodeListElemInfo.from_single(node_list=reducer.ori_node_list, elems=result)


class _SubGraphDDPoolV7:

    def __init__(
        self,
        *,
        cur_sg_idxs: Optional[set[int]] = None,
        replaced_sg_idxs: Optional[set[int]] = None,
        no_more_split_sg_idxs: Optional[set[int]] = None,
        fail_to_replace_sg_idxs: Optional[set[int]] = None,
    ) -> None:
        # Current universe of SG idxs that belong to the *current* round.
        # In multi-round mode, this is rebuilt per round from split children.
        # IMPORTANT: this universe is for splitting/progressing; it is NOT the same as
        # `sg_mutations` (which is the subset worth attempting to replace in DD).
        self.cur_sg_idxs: set[int] = set(cur_sg_idxs or set())

        # SG idxs that have been successfully replaced/removed so far.
        self.replaced_sg_idxs: set[int] = set(replaced_sg_idxs or set())
        self.fail_to_replace_sg_idxs_in_previous_pass: set[int] = set(fail_to_replace_sg_idxs or set())

        # sg_idx -> elem-level mutation (elem_idx -> new elems)
        self.sg_mutations: dict[int, dict[int, list[OneElem]]] = {}

        # sg idxs that should NOT be split/decomposed again in future rounds.
        # This is persistent across rebuilds for multi-round split-and-reduce.
        self.no_more_split_sg_idxs: set[int] = set(no_more_split_sg_idxs or set())

    @property
    def unreplaced_cur_sg_idxs(self) -> set[int]:
        return self.cur_sg_idxs - self.replaced_sg_idxs

    @property
    def unreplaced_candidate_sg_idxs(self) -> set[int]:
        return set(self.sg_mutations.keys()) - self.replaced_sg_idxs

    


class _SubGraphReduceStateV7:
    def __init__(
        self,
        *,
        input_elems: list[OneElem],
        ctx: OneNodeListReductionCtx,
        reduce_applier: OneNodeListReducerApplier,
        expected_end_time: Optional[float],
        cfg: V7Cfg,
    ):
        self.ctx = ctx
        self.input_elems = input_elems
        self.reduce_applier = reduce_applier
        self.expected_end_time = expected_end_time
        self.cfg = cfg
        # 
        
        graph_helper = GraphHelper(
            elems=input_elems,
            context=ctx.context,
            init_stack=ctx.node_type.param_types,
            end_stack=ctx.node_type.result_types,
        )
        self.graph_helper = graph_helper
        self.sg_repo = get_init_subgraph_repo(graph_helper)
        # 
        self.sg_manager: SubGraphSplitter = SubGraphSplitter(
            graph_helper=graph_helper)
        # 
        self.raw_subgraph_num = self.sg_repo.sg_num
   
        # 

        # Accepted mutations across all passes (elem_idx -> new elems).
        self.accepted_elem_mutation: dict[int, list[OneElem]] = {}

        # DD-round-related state (candidates + split tracking).
        # Initialize the universe for the first round.
        self.dd_pool = _SubGraphDDPoolV7()

        # Internal flags for finalization.
        self.has_any_success = False
        self.unreplaced_subgraph_idxs: set[int] = set()

        # Precompute candidates for the initial (original) subgraphs.
        for sg_idx in self.sg_repo.get_all_keys():
            assert sg_idx in self.sg_repo, (
                f"missing sg_idx {sg_idx} in sg_repo, all keys: {list(self.sg_repo._sg_idx2sg.keys())}"
            )
            sg = self.get_sg(sg_idx)
            if sg_is_cf_only(sg, self.input_elems):
                continue
            self.update_mutation(self.dd_pool, sg)
            # Only keep SGs that may still be split in future rounds.
            if sg.idx not in self.dd_pool.no_more_split_sg_idxs:
                self.dd_pool.cur_sg_idxs.add(sg.idx)
        pass

    def update_mutation(self, pool: _SubGraphDDPoolV7, sg: SubGraph) -> None:
   
        if self.cfg.enable_minimal_replacement:
            mutation = get_elem_mutation_for_sg_ng(sg, self.cfg)
        else:
            mutation = get_elem_mutation_for_sg_naive(sg, self.cfg)
        if mutation is not None:
            if self._is_sg_mutation_simplifying(sg, mutation):
            # if True:
                assert sg.idx not in pool.sg_mutations
                pool.sg_mutations[sg.idx] = mutation
            else:
                pool.no_more_split_sg_idxs.add(sg.idx)

    def get_elems_in_sg(self, sg: SubGraph) -> list[OneElem]:
        return [self.input_elems[idx] for idx in sorted(sg.sg_elem_idxs)]

    def _materialize_mutated_elems_for_elem_idxs(
        self,
        elem_idxs: list[int],
        mutation_elem_idx2new_elems: dict[int, list[OneElem]],
    ) -> list[OneElem]:
        out: list[OneElem] = []
        for idx in elem_idxs:
            assert idx in mutation_elem_idx2new_elems, (
                f"Unexpected elem idx {idx} not in mutation"
            )
            out.extend(mutation_elem_idx2new_elems[idx])
        return out

    def _is_sg_mutation_simplifying(
        self,
        sg: SubGraph,
        mutation_elem_idx2new_elems: dict[int, list[OneElem]],
    ) -> bool:
        elem_idxs_in_sg = sorted(sg.sg_elem_idxs)
        raw_elems_in_sg = [self.input_elems[idx] for idx in elem_idxs_in_sg]
        mutated_elems_in_sg = self._materialize_mutated_elems_for_elem_idxs(
            elem_idxs_in_sg,
            mutation_elem_idx2new_elems,
        )
        return cur_is_more_naive(raw_elems_in_sg, mutated_elems_in_sg)

    @staticmethod
    def _apply_elem_mutation(
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

    @property
    def cur_elems(self) -> list[OneElem]:
        return self._apply_elem_mutation(self.input_elems, self.accepted_elem_mutation)

    def _is_timeout(self) -> bool:
        if self.expected_end_time is None:
            return False
        return time.time() > self.expected_end_time
# 

    def get_sg(self, sg_idx: int) -> SubGraph:
        return self.sg_repo.get_sg_by_idx(sg_idx)


    def _try_replace_one_sub_graph_level(self, to_save_subgraph_idxs: list[int]) -> bool:
        if self._is_timeout():
            return False

        to_replace_subgraph_idxs = self.unreplaced_subgraph_idxs - set(to_save_subgraph_idxs)
        return self._try_replace_core(to_replace_subgraph_idxs)

    def _try_replace_core(self, to_replace_subgraph_idxs: set[int]) -> bool:
        candidate_mutation: dict[int, list[OneElem]] = {}
        for subgraph_idx in to_replace_subgraph_idxs:
            per_sg_mutation = self.dd_pool.sg_mutations[subgraph_idx]
            for k, v in per_sg_mutation.items():
                assert k not in candidate_mutation, f"V7 SG mutation conflict at elem idx {k}"
                candidate_mutation[k] = v


        merged_mutation: dict[int, list[OneElem]] = {}
        merged_mutation.update(candidate_mutation)
        merged_mutation.update(self.accepted_elem_mutation)

        result = self.reduce_applier.gen_replacement_by_elems_and_test_by_mutation(
            ctx=self.ctx,
            raw_elems=self.input_elems,
            mutation_elem_idx2new_elems=merged_mutation,
        )

        if result:
            self.accepted_elem_mutation.update(candidate_mutation)
            self.unreplaced_subgraph_idxs -= to_replace_subgraph_idxs

            # Update pool-level tracking for multi-round.
            self.dd_pool.replaced_sg_idxs.update(to_replace_subgraph_idxs)

            self.reduce_sg_has_success = True
            self.has_any_success = True
        return result

    def run_dd_subgraph_pass(self) -> set[int]:
        if not self.dd_pool.unreplaced_candidate_sg_idxs:
            self.unreplaced_subgraph_idxs = set()
            return set()

        print('Reduce node list SG')
        self.reduce_sg_has_success = False

        # Candidates for this pass.
        self.unreplaced_subgraph_idxs = set(self.dd_pool.unreplaced_candidate_sg_idxs)
        sorted_idxs = sorted(list(self.unreplaced_subgraph_idxs))
        self.replaceable_sg_num = len(self.unreplaced_subgraph_idxs)


        # Main SG-level DD.
        test_config = get_test_cfg_func_for_dd(self._try_replace_one_sub_graph_level)
        dd = ProbDDFactory.get_default_probdd(test_config, task_id='V7SG')
        candidates = sorted(list(self.unreplaced_subgraph_idxs))
        minimal_config = dd(candidates, expected_end_time=self.expected_end_time)
        print(f"V7DD minimal config: {minimal_config}")

        replaced_in_this_pass = set(candidates) - set(minimal_config)
        return replaced_in_this_pass

    def split_and_refresh_candidates(self, sg_idxs_to_split: set[int]) -> None:
        # DD-related variables are re-initialized here by rebuilding candidate pool
        # from scratch. No parent SG (whether succeeded or failed in previous round)
        # will appear in the new pool.
        new_pool = _SubGraphDDPoolV7(
            cur_sg_idxs=set(),
            no_more_split_sg_idxs=self.dd_pool.no_more_split_sg_idxs,
            replaced_sg_idxs=self.dd_pool.replaced_sg_idxs,
        )

        for parent_sg_idx in sorted(sg_idxs_to_split):
            # Parent might have been replaced already.
            if parent_sg_idx in new_pool.replaced_sg_idxs:
                continue
            if parent_sg_idx in new_pool.no_more_split_sg_idxs:
                continue
            assert parent_sg_idx in self.sg_repo

            # Once an SG is used as a split parent, it should never be revisited as a parent.
            # (It has already failed replacement in this round by definition of sg_idxs_to_split.)
            new_pool.no_more_split_sg_idxs.add(parent_sg_idx)
            cur_sg = self.get_sg(parent_sg_idx)
            if cur_sg.has_one_elem:
                continue

            children = self.sg_manager.replace_a_graph(parent_sg_idx, self.sg_repo)
            if len(children) == 1:
                continue

            for child_sg in children:
                self.update_mutation(new_pool, child_sg)
                if child_sg.idx not in new_pool.no_more_split_sg_idxs:
                    new_pool.cur_sg_idxs.add(child_sg.idx)

        self.dd_pool = new_pool

    @property
    def cur_replace_candidates(self) -> set[int]:
        return set(self.dd_pool.sg_mutations.keys())


class GreedyLargestSubGraphReducerV7:
    def __init__(self, st: _SubGraphReduceStateV7):
        self.st = st

    def _candidate_sg_idxs(self) -> set[int]:
        return set(self.st.dd_pool.unreplaced_candidate_sg_idxs)

    def _estimate_sg_gain(self, sg_idx: int) -> int:
        sg = self.st.get_sg(sg_idx)
        elem_idxs_in_sg = sorted(sg.sg_elem_idxs)
        raw_elems_in_sg = [self.st.input_elems[idx] for idx in elem_idxs_in_sg]
        mutated_elems_in_sg = self.st._materialize_mutated_elems_for_elem_idxs(
            elem_idxs_in_sg,
            self.st.dd_pool.sg_mutations[sg_idx],
        )
        raw_len = sum(e.get_length() for e in raw_elems_in_sg)
        mutated_len = sum(e.get_length() for e in mutated_elems_in_sg)
        return raw_len - mutated_len

    def run(self) -> set[int]:
        candidate_sg_idxs = self._candidate_sg_idxs()
        if not candidate_sg_idxs:
            self.st.unreplaced_subgraph_idxs = set()
            return set()

        print('Reduce node list SG (greedy-largest)')
        self.st.reduce_sg_has_success = False

        self.st.unreplaced_subgraph_idxs = set(candidate_sg_idxs)
        to_try: set[int] = set(self.st.unreplaced_subgraph_idxs)

        while to_try:
            if self.st._is_timeout():
                break

            best_sg_idx = max(to_try, key=self._estimate_sg_gain)
            ok = self.st._try_replace_core({best_sg_idx})
            if ok:
                to_try = to_try & self.st.unreplaced_subgraph_idxs
            else:
                to_try.remove(best_sg_idx)

        return self.st.unreplaced_subgraph_idxs

class ReduceNodeListV7:
    def __init__(
        self,
        *,
        ctx: OneNodeListReductionCtx,
        reduce_applier: OneNodeListReducerApplier,
        input_elem_info: NodeListElemInfo,
        cfg: V7Cfg,
        rest_time: Optional[float] = None,
    ):
        self.ctx = ctx
        self.reduce_applier = reduce_applier
        self.cfg: V7Cfg = cfg
        self.expected_end_time = None
        self.ori_node_list, input_elems = input_elem_info.get_single_node_list_and_elems()
        self.raw_elems_length = sum(elem.get_length() for elem in input_elems)
        # 
        if rest_time is not None:
            self.expected_end_time = time.time() + rest_time
        # 
        self._sg_state:_SubGraphReduceStateV7 = _SubGraphReduceStateV7(
            input_elems=input_elems,
            ctx=ctx,
            reduce_applier=reduce_applier,
            expected_end_time=self.expected_end_time,
            cfg=self.cfg,
        )


    def reduce(self)->list[OneElem]:
        st = self._sg_state
        if st.raw_subgraph_num == 0:
            return st.cur_elems

        # Multi-round split-and-reduce:
        while True:
            # 1) Try DD on current candidates (may be empty).

            if self.cfg.enable_dd:
                st.run_dd_subgraph_pass()
            else:
                GreedyLargestSubGraphReducerV7(st).run()

            if not self.cfg.enable_ddg_split:
                break

            # 2) Split all *splittable* unreplaced SGs in the current universe.
            # In V7's intended design, we only split SGs that were candidates in the
            # previous round but were not replaced.
            sg_idxs_to_split: set[int] = set()
            for sg_idx in st.dd_pool.unreplaced_candidate_sg_idxs:
                if sg_idx in st.dd_pool.no_more_split_sg_idxs:
                    continue
                if st.get_sg(sg_idx).has_one_elem:
                    continue
                sg_idxs_to_split.add(sg_idx)

            print('V7 SG replaced idxs:', sorted(list(st.dd_pool.replaced_sg_idxs)))
            print('V7 SG to split idxs:', sorted(list(sg_idxs_to_split)))

            # No more progress possible: DD already ran for this round and nothing is splittable.
            if not sg_idxs_to_split:
                break

            st.split_and_refresh_candidates(sg_idxs_to_split)

            # If nothing survives into the next round, stop.
            if not st.dd_pool.cur_sg_idxs:
                break
        for sg_idx in st.unreplaced_subgraph_idxs:
            # idxs 
            sg = st.get_sg(sg_idx)
            elem_idxs = sg.sg_elem_idxs
            _failed_elems = [st.input_elems[i] for i in sorted(elem_idxs)]
            # print('V7 SG unreplaced idxs:', idxs)
            self.ctx.raw_elems_cache.add_failed_by_raw_elems(_failed_elems)

        # Finalize once at the end so all offsets remain relative to the original snapshot.
        if st.has_any_success:
            self.reduce_applier.finalize(
                self.ctx.ori_node_list,
                self.raw_elems_length,
                st.cur_elems,
            )
        return st.cur_elems
# raw code =============================================================================================================

    @property
    def cur_elems(self):
        return self._sg_state.cur_elems
