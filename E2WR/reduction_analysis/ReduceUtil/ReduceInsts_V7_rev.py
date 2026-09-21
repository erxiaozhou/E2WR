from extract_block_mutator.Context import Context
from extract_block_mutator.funcType import funcType
from reduction_analysis.ProbDDUtil.ProbDDFactory import ProbDDFactory

from reduction_analysis.ReduceUtil.ReduceInsts_V5_util import cur_is_more_naive
from reduction_analysis.ProbDDUtil.adapt_util import get_test_cfg_func_for_dd
from typing import Optional
import time
from .ReduceInsts_V5_util import OneElem
from .ReduceInsts_cfg_util import V7Cfg
from .V6V1GraphHelper import EqSubGraphManager, GraphHelper,  SubGraph, get_eq_sgs
from reduction_analysis.ReduceUtil.ReduceInsts_V7_mutation_util import get_elem_mutation_for_sg_ng, get_elem_mutation_for_sg_naive
from reduction_analysis.ReduceUtil.OneNodeListReductionEnv import OneNodeListReducerApplier, OneNodeListReductionCtx
from reduction_analysis.ReduceUtil.ReduceInsts_V9_util import NodeListElemInfo
from .V6V1GraphHelper import sg_is_cf_only


def call_V7_rev(
        ctx: OneNodeListReductionCtx,
        reduce_applier: OneNodeListReducerApplier,
        input_elems: NodeListElemInfo,
        cfg: V7Cfg,
        rest_time:Optional[float]=None,
        ) -> NodeListElemInfo:
    reducer = ReduceNodeListV7REV(
        ctx=ctx,
        reduce_applier=reduce_applier,
        input_elem_info=input_elems,
        cfg=cfg,
    )
    result = reducer.reduce(rest_time=rest_time)
    return NodeListElemInfo.from_single(node_list=reducer.ori_node_list, elems=result)
class ReduceNodeListV7REV:
    def __init__(
        self,
        *,
        ctx: OneNodeListReductionCtx,
        reduce_applier: OneNodeListReducerApplier,
        input_elem_info: NodeListElemInfo,
        cfg: V7Cfg,
    ):
        self.ctx = ctx
        self.ori_node_list, self.input_elems = input_elem_info.get_single_node_list_and_elems()
        self.DEBUG = ctx.DEBUG
        self.reduce_applier = reduce_applier
        self.expected_end_time: Optional[float] = None
        self.cfg = cfg

        graph_helper = GraphHelper(
            elems=self.input_elems,
            context=ctx.context,
            init_stack=ctx.node_type.param_types,
            end_stack=ctx.node_type.result_types,
        )
        self.graph_helper = graph_helper
        self.subgraph_repo_eq = get_eq_sgs(graph_helper)
        

        self.raw_length = sum(elem.get_length() for elem in self.input_elems)

        # 

        # Accepted mutations across all passes (elem_idx -> new elems).
        self.accepted_elem_mutation: dict[int, list[OneElem]] = {}

        # Single-pass SG-level DD candidates.
        # sg_idx -> elem-level mutation (elem_idx -> new elems)
        self.sg_mutations: dict[int, dict[int, list[OneElem]]] = {}

        # SG idxs that have been successfully replaced/removed so far.
        # Internal flags for finalization.
        self.has_any_success = False

        # Track SGs that were actually attempted by DD. We only record failures for
        # subgraphs that have been tried at least once.
        self._tried_sg_idxs: set[int] = set()

        # Precompute candidates for the initial (original) subgraphs.
        for sg_idx, sg in self.subgraph_repo_eq.items():
            if sg_is_cf_only(sg, self.input_elems):
                continue
            self._maybe_add_sg_candidate(sg)

        # Cache simple cost heuristics for greedy batching.
        # Lower is preferred.
        self._sg_idx2cost: dict[int, int] = {}
        for sg_idx in self.sg_mutations.keys():
            self._sg_idx2cost[sg_idx] = self._estimate_sg_mutation_cost(sg_idx)

    @property
    def unreplaced_candidate_sg_idxs(self) -> set[int]:
        # An SG is effectively "replaced" once its mutation elem indices are part of the
        # accepted mutation. This also naturally excludes any SG that overlaps with
        # already-accepted mutations.
        used_elem_idxs = set(self.accepted_elem_mutation.keys())
        return {
            sg_idx
            for sg_idx, mutation in self.sg_mutations.items()
            if not (set(mutation.keys()) & used_elem_idxs)
        }

    def _maybe_add_sg_candidate(self, sg: SubGraph) -> None:
        if self.cfg.enable_minimal_replacement:
            mutation = get_elem_mutation_for_sg_ng(sg, self.cfg)
        else:
            mutation = get_elem_mutation_for_sg_naive(sg, self.cfg)
        if mutation is None:
            return
        if not self._is_sg_mutation_simplifying(sg, mutation):
            return
        assert sg.idx not in self.sg_mutations
        self.sg_mutations[sg.idx] = mutation

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

    def get_sg(self, sg_idx: int) -> SubGraph:
        return self.subgraph_repo_eq.get_sg_by_idx(sg_idx)

    def _sg_mutation_elem_idxs(self, sg_idx: int) -> set[int]:
        return set(self.sg_mutations[sg_idx].keys())

    def _estimate_sg_mutation_cost(self, sg_idx: int) -> int:
        sg = self.get_sg(sg_idx)
        elem_idxs_in_sg = sorted(sg.sg_elem_idxs)
        mutated_elems_in_sg = self._materialize_mutated_elems_for_elem_idxs(
            elem_idxs_in_sg,
            self.sg_mutations[sg_idx],
        )
        mutated_len = sum(e.get_length() for e in mutated_elems_in_sg)
        # Prefer smaller mutated output.
        return mutated_len

    def _select_non_overlapping_sg_batch(self, candidate_sg_idxs: set[int]) -> list[int]:
        used_elem_idxs = set(self.accepted_elem_mutation.keys())
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

    def _run_dd_on_sg_batch(self, batch_sg_idxs: list[int]) -> set[int]:
        if not batch_sg_idxs:
            return set()
        if self._is_timeout():
            return set()

        # Mark these SGs as actually tried in this reduction.
        self._tried_sg_idxs.update(batch_sg_idxs)

        batch_set = set(batch_sg_idxs)
        unreplaced_subgraph_idxs: set[int] = set(batch_set)

        def _try_replace_one_sub_graph_level(to_save_subgraph_idxs: list[int]) -> bool:
            if self._is_timeout():
                return False

            to_replace_subgraph_idxs = unreplaced_subgraph_idxs - set(to_save_subgraph_idxs)
            candidate_mutation: dict[int, list[OneElem]] = {}
            for subgraph_idx in to_replace_subgraph_idxs:
                per_sg_mutation = self.sg_mutations[subgraph_idx]
                for k, v in per_sg_mutation.items():
                    assert k not in candidate_mutation, (
                        f"V7 SG mutation conflict at elem idx {k}"
                    )
                    candidate_mutation[k] = v

            overlap = set(candidate_mutation.keys()) & set(self.accepted_elem_mutation.keys())
            assert not overlap, (
                f"V7 SG mutation overlaps accepted mutation at elem idxs {sorted(overlap)[:10]}"
            )

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
                unreplaced_subgraph_idxs.difference_update(to_replace_subgraph_idxs)
                self.has_any_success = True
            return result

        test_config = get_test_cfg_func_for_dd(_try_replace_one_sub_graph_level)
        dd = ProbDDFactory.get_default_probdd(test_config, task_id='V7SG')
        minimal_config = dd(sorted(batch_sg_idxs), expected_end_time=self.expected_end_time)
        print(f"V7DD batch minimal config: {minimal_config}")

        return batch_set - unreplaced_subgraph_idxs

    def run_dd_subgraph_batches(self) -> set[int]:
        replaced_total: set[int] = set()
        deferred_unreplaceable: set[int] = set()

        available: set[int] = set(self.unreplaced_candidate_sg_idxs)
        while available and not self._is_timeout():
            # Exclude anything that now overlaps already-accepted mutations.
            used_elem_idxs = set(self.accepted_elem_mutation.keys())
            available = {
                sg_idx
                for sg_idx in available
                if not (self._sg_mutation_elem_idxs(sg_idx) & used_elem_idxs)
            }
            if not available:
                break

            batch = self._select_non_overlapping_sg_batch(available)
            if not batch:
                break

            print(f"V7DD batch size={len(batch)} remaining={len(available)}")
            replaced_in_batch = self._run_dd_on_sg_batch(batch)
            if replaced_in_batch:
                replaced_total |= replaced_in_batch
                available -= replaced_in_batch
                # Loop continues; overlap filtering happens at top.
            else:
                # Make progress: if DD can't delete anything from this batch now,
                # skip these SGs for the remainder of this reduction.
                deferred_unreplaceable.update(batch)
                available -= set(batch)

        if deferred_unreplaceable and self.DEBUG:
            print(f"V7DD skipped unreplaceable SGs: {len(deferred_unreplaceable)}")
        return replaced_total



    def reduce(self, rest_time: Optional[float] = None) -> list[OneElem]:
        if rest_time is not None:
            self.expected_end_time = time.time() + rest_time

        if not self.unreplaced_candidate_sg_idxs:
            return self.cur_elems

        replaced_sg_idxs = self.run_dd_subgraph_batches()

        # Record SG-level failures (tried but not replaced), similar to V7_core.
        failed_sg_idxs = self._tried_sg_idxs - set(replaced_sg_idxs)
        for sg_idx in sorted(failed_sg_idxs):
            sg = self.get_sg(sg_idx)
            elem_idxs = sg.sg_elem_idxs
            failed_elems = [self.input_elems[i] for i in sorted(elem_idxs)]
            self.ctx.raw_elems_cache.add_failed_by_raw_elems(failed_elems)

        # Finalize once at the end so all offsets remain relative to the original snapshot.
        if self.has_any_success:
            self.reduce_applier.finalize(
                self.ctx.ori_node_list,
                self.raw_length,
                self.cur_elems,
            )
        return self.cur_elems
# raw code =============================================================================================================
