from traceback import print_exc
import networkx as nx
from typing import Optional
from reduction_analysis.StackState import StackState, StackStatus, sstate1_support_sstate2
import time
from .ReduceInsts_V5_util import  OneElem
from .ReduceInsts_V5_util import get_stack_state_after_each_elem_with_reqs, get_type_reqs_from_elems
from reduction_analysis.ProbDDUtil.ProbDDFactory import ProbDDFactory
from reduction_analysis.ProbDDUtil.adapt_util import get_test_cfg_func_for_dd

from reduction_analysis.ReduceUtil.OneNodeListReductionEnv import OneNodeListReducerApplier, OneNodeListReductionCtx
def reduce_insts_p3_graph_based_v7(
    ctx: OneNodeListReductionCtx,
    reduce_applier: OneNodeListReducerApplier,
    rest_time:Optional[float],
    elems: list[OneElem],
    must_contain_cf: bool = False
) ->list[OneElem]:
    reducer = StackStateGraphReducer(
        ctx=ctx,
        mutation_applier=reduce_applier,
        elems=elems,
        rest_time=rest_time,
        must_contain_cf=must_contain_cf
    )
    reducer.reduce_graph_based()
    return reducer.current_elems


class StackStateGraphReducer:
    def __init__(
        self,
        *,
        ctx: OneNodeListReductionCtx,
        mutation_applier: OneNodeListReducerApplier,
        elems: list[OneElem],
        rest_time: Optional[float],
        must_contain_cf: bool,
    ):
        self.ctx = ctx
        node_list_type = ctx.node_type
        stack_init_status = ctx.build_stack_init_status()
        
        self.type_reqs = get_type_reqs_from_elems(
            elems,
            ctx.context
        )
        # self.node_list_type = node_list_type
        # 
        self.last_state = StackState(
                all_rest_types=[node_list_type.result_types],
                status=StackStatus.NORMAL
            )
        # 
        self.stack_init_status = stack_init_status
        self.mutation_applier = mutation_applier
        self.DEBUG: bool = ctx.DEBUG
        self.raw_elems = elems
        self.must_contain_cf = must_contain_cf
        if rest_time is not None:
            self.expected_end_time = time.time() + rest_time
        else:
            self.expected_end_time = None
        self.reduced_elem_idxs: set[int] = set()
        self.has_any_success = False
        self.raw_elem_num = len(elems)
        self.raw_elems_length = sum(elem.get_length() for elem in elems)
        self.cf_elem_idxs: set[int] = {
            idx for idx, elem in enumerate(elems)
            if elem.is_cf_related_inst()
        }
        self.cf_non_unreachable_elem_idxs: set[int] = {
            idx for idx, elem in enumerate(elems)
            if elem.is_target_one_inst({'return',   'br_if'})
        }
        self.current_cycle_batch: Optional[list[list[int]]] = None
        self.unreplaced_cycle_positions: Optional[set[int]] = None
        self.graph_helper = StackStateGraphHelper(ctx.DEBUG)

    @property
    def current_elems(self) -> list[OneElem]:
        idxs=  self._get_kept_indices()
        return [self.raw_elems[i] for i in idxs]
    
    def _get_kept_indices(self, exclude_idxs: Optional[set[int]] = None) -> list[int]:
        if exclude_idxs is None:
            exclude_idxs = set()
        all_excluded = self.reduced_elem_idxs | exclude_idxs
        return [i for i in range(self.raw_elem_num) if i not in all_excluded]
    
    
    def get_current_stack_states(self) -> list[StackState]:
        current_states = [self.stack_init_status]
        to_append_req_idxs = self._get_kept_indices()
        type_reqs = [self.type_reqs[i] for i in to_append_req_idxs]
        _cur_elems = self.current_elems
        state_after_each_elem = get_stack_state_after_each_elem_with_reqs(
            self.stack_init_status,
            _cur_elems,
            type_reqs
        )
        current_states.extend(state_after_each_elem)
        if len(current_states) > 1:
            current_states[-1] = self.last_state
        return current_states

    def _is_timeout(self) -> bool:
        if self.expected_end_time is None:
            return False
        return time.time() > self.expected_end_time

    def _normalize_sequences(self, sequences: list[list[int]]) -> list[list[int]]:
        normalized: list[list[int]] = []
        seen: set[tuple[int, ...]] = set()
        for seq in sequences:
            if not seq:
                continue
            seq_tuple = tuple(sorted(seq))
            if seq_tuple in seen:
                continue
            seen.add(seq_tuple)
            normalized.append(list(seq_tuple))
        normalized.sort(key=lambda seq: (len(seq), seq[0]))
        return normalized

    def _seq_contains_cf(self, seq: list[int]) -> bool:
        return bool(set(seq) & self.cf_elem_idxs)

    def _seq_contains_non_unreachable_cf(self, seq: list[int]) -> bool:
        return bool(set(seq) & self.cf_non_unreachable_elem_idxs)

    def _seq_starts_with_any_status(
        self,
        seq: list[int],
        current_states: list[StackState],
        kept_orig_indices: list[int],
    ) -> bool:
        if not seq:
            return False
        first_orig_idx = min(seq)
        try:
            pos = kept_orig_indices.index(first_orig_idx)
        except ValueError:
            return False
        return pos < len(current_states) and current_states[pos].status == StackStatus.ANY

    def _ignore_idxs_contain_any_start_seq(
        self,
        to_ignore_orig_idxs: set[int],
        current_states: list[StackState],
        kept_orig_indices: list[int],
    ) -> bool:
        sorted_idxs = sorted(to_ignore_orig_idxs)
        runs: list[list[int]] = []
        for x in sorted_idxs:
            if runs and x == runs[-1][-1] + 1:
                runs[-1].append(x)
            else:
                runs.append([x])
        return any(
            self._seq_starts_with_any_status(run, current_states, kept_orig_indices)
            for run in runs
        )

    def _split_local_search_ranges(self, kept_orig_indices: list[int]) -> list[tuple[int, int]]:
        ranges: list[tuple[int, int]] = []
        start: Optional[int] = None
        for elem_idx, raw_elem_idx in enumerate(kept_orig_indices):
            if raw_elem_idx in self.cf_elem_idxs:
                if start is not None and start < elem_idx:
                    ranges.append((start, elem_idx))
                start = None
                continue
            if start is None:
                start = elem_idx
        if start is not None and start < len(kept_orig_indices):
            ranges.append((start, len(kept_orig_indices)))
        return ranges

    def _collect_local_reducible_sequences(
        self,
        current_states: list[StackState],
        kept_orig_indices: list[int],
    ) -> list[list[int]]:
        reducible_sequences: list[list[int]] = []
        for start_idx, end_idx in self._split_local_search_ranges(kept_orig_indices):
            local_states = current_states[start_idx:end_idx + 1]
            local_kept_orig_indices = kept_orig_indices[start_idx:end_idx]
            if not local_kept_orig_indices:
                continue
            G = self.graph_helper.build_graph(local_states, local_kept_orig_indices)
            cycles = self.graph_helper.find_cycles(G)
            if not cycles:
                continue
            reducible_sequences.extend(
                self.graph_helper.extract_reducible_sequences(G, cycles)
            )
        if True:
            reducible_sequences = [
                seq for seq in reducible_sequences
                if self._seq_starts_with_any_status(seq, current_states, kept_orig_indices)
            ]
        return self._normalize_sequences(reducible_sequences)

    def _collect_global_reducible_sequences_with_cf(
        self,
        current_states: list[StackState],
        kept_orig_indices: list[int],
    ) -> list[list[int]]:
        if not kept_orig_indices:
            return []
        G = self.graph_helper.build_graph(current_states, kept_orig_indices)
        cycles = self.graph_helper.find_cycles(G)
        if not cycles:
            return []
        reducible_sequences = self.graph_helper.extract_reducible_sequences(G, cycles)
        # does not consider the seq that only contain `unreachable` as CF, since the 
        # unreachable it contains should be handled by the `CORE` phase ; since the `unreachable` is
        # left, it may be  un-removable
        reducible_sequences = [
            seq for seq in reducible_sequences
            if self._seq_contains_non_unreachable_cf(seq) 
            # if self._seq_contains_cf(seq)
        ]
        return self._normalize_sequences(reducible_sequences)

    def _select_non_overlapping_cycle_batch(
        self,
        candidate_cycles: list[list[int]],
    ) -> list[list[int]]:
        batch: list[list[int]] = []
        used_idxs: set[int] = set(self.reduced_elem_idxs)
        for seq in sorted(candidate_cycles, key=lambda s: (len(s), s[0])):
            seq_set = set(seq)
            if seq_set & used_idxs:
                continue
            batch.append(seq)
            used_idxs.update(seq_set)
        return batch

    def _try_replace_cycle_batch(self, to_save_cycle_positions: list[int]) -> bool:
        if self._is_timeout():
            return False

        batch = self.current_cycle_batch
        unreplaced_cycle_positions = self.unreplaced_cycle_positions
        assert batch is not None
        assert unreplaced_cycle_positions is not None

        to_replace_cycle_positions = (
            unreplaced_cycle_positions - set(to_save_cycle_positions)
        )
        to_ignore_orig_idxs: set[int] = set()
        for cycle_pos in to_replace_cycle_positions:
            to_ignore_orig_idxs.update(batch[cycle_pos])

        if len(to_ignore_orig_idxs) == 0:
            return True

        result = self.try_replace_elems(to_ignore_orig_idxs, source_tag='LOCAL')
        if result:
            unreplaced_cycle_positions.difference_update(to_replace_cycle_positions)
            self.has_any_success = True
        return result

    def _run_dd_on_cycle_batch(self, batch: list[list[int]]) -> set[int]:
        if not batch or self._is_timeout():
            return set()

        self.current_cycle_batch = batch
        self.unreplaced_cycle_positions = set(range(len(batch)))

        try:
            test_config = get_test_cfg_func_for_dd(self._try_replace_cycle_batch)
            dd = ProbDDFactory.get_default_probdd(test_config, task_id='IP3LV7')
            minimal_config = dd(
                list(range(len(batch))),
                expected_end_time=self.expected_end_time,
            )
            if self.DEBUG:
                print(f"P3 local batch minimal config: {minimal_config}")

            return set(range(len(batch))) - self.unreplaced_cycle_positions
        finally:
            self.current_cycle_batch = None
            self.unreplaced_cycle_positions = None

    def _run_local_graph_reduction(self) -> None:
        current_states = self.get_current_stack_states()
        kept_orig_indices = self._get_kept_indices()
        # print('[P3DBG] current stack states:')
        # for i, state in enumerate(current_states):
        #     if i == 0:
        #         print(f"[P3DBG]  [{i}] <init> {state}")
        #     else:
        #         orig_idx = kept_orig_indices[i - 1]
        #         print(f"[P3DBG]  [{i}] {self.raw_elems[orig_idx]} {state}")
        # print('===============================================')

        current_elems = self.current_elems
        batch_all = self._collect_local_reducible_sequences(
            current_states,
            kept_orig_indices,
        )
        collect_times_in_func = 0

        while batch_all and not self._is_timeout():
            collect_times_in_func += 1
            batch = self._select_non_overlapping_cycle_batch(batch_all)
            if not batch:
                break
            # print(f"[P3DBG] local reducible seqs collected in {collect_times_in_func} times: {len(batch)} seq(s) in this batch")

            reduced_before = set(self.reduced_elem_idxs)
            self._run_dd_on_cycle_batch(batch)
            removed_in_this_batch = self.reduced_elem_idxs - reduced_before

            batch_seq_set = {tuple(seq) for seq in batch}
            next_batch_all: list[list[int]] = []
            for seq in batch_all:
                seq_tuple = tuple(seq)
                if seq_tuple in batch_seq_set:
                    continue
                if removed_in_this_batch and (set(seq) & removed_in_this_batch):
                    continue
                next_batch_all.append(seq)
            batch_all = next_batch_all

    def _run_global_graph_reduction(self) -> None:
        collect_times_in_func = 0
        while not self._is_timeout():
            current_states = self.get_current_stack_states()

            kept_orig_indices = self._get_kept_indices()
            # t0_ = time.time()
            reducible_sequences = self._collect_global_reducible_sequences_with_cf(
                current_states,
                kept_orig_indices,
            )
            collect_times_in_func += 1
            # print(f"[P3DBG] global reducible seqs collected in {time.time() - t0_:.2f}s: [{collect_times_in_func}] {len((reducible_sequences))} seq(s)")
            if not reducible_sequences:
                if self.DEBUG:
                    print(f"No global control-flow cycles found, reduce end, try {collect_times_in_func} times, success {collect_times_in_func-1} times")
                return

            all_failed = True
            for seq in reducible_sequences:
                if self._is_timeout():
                    return
                if self.try_replace_elems(set(seq), source_tag='GLOBAL'):
                    self.has_any_success = True
                    all_failed = False
                    break
            if all_failed:
                return
    
    def try_replace_elems(self, to_ignore_orig_idxs: set[int], source_tag='N') -> bool:
        all_removed_elem_idxs = to_ignore_orig_idxs | self.reduced_elem_idxs
        mutation = {idx:[] for idx in all_removed_elem_idxs}
        # 
        to_mutate_elems = [self.raw_elems[idx] for idx in to_ignore_orig_idxs]
        covered_by_cache = self.ctx.raw_elems_cache.covers_failed(to_mutate_elems)
        if covered_by_cache:
            if self.DEBUG:
                print(f"Skip trying removal of elems at idxs {to_ignore_orig_idxs} due to known failure")
            return False
        # 
        result = self.mutation_applier.gen_replacement_by_elems_and_test_by_mutation(
            ctx=self.ctx,
            raw_elems=self.raw_elems,
            mutation_elem_idx2new_elems=mutation,
            check_invalid_and_return_false=not self.DEBUG,
        )
        # current_states = self.get_current_stack_states()
        # kept_orig_indices = self._get_kept_indices()
        # contain_any = self._ignore_idxs_contain_any_start_seq(
        #     to_ignore_orig_idxs, current_states, kept_orig_indices
        # )
        # print(f"[P3DBG] source_tag={source_tag} oracle={'PASS' if result else 'FAIL'} "
        #       f"CONTAIN_ANY={contain_any} "
        #       f"tried {len(to_ignore_orig_idxs)} elem(s): "
        #       f"raw_idxs={sorted(to_ignore_orig_idxs)} elems={[self.raw_elems[idx] for idx in to_ignore_orig_idxs]}")

        if result:
            self.reduced_elem_idxs.update(to_ignore_orig_idxs)
        else:
            if not covered_by_cache:
                self.ctx.raw_elems_cache.add_failed_by_raw_elems(to_mutate_elems)
        return result

    def reduce_graph_based(self) -> set[int]:
        # print('Before Local P3 =====================================')
        self._run_local_graph_reduction()
        # print('After Local P3 =====================================')
        if not self._is_timeout():
            self._run_global_graph_reduction()

        if self.has_any_success:
            self.mutation_applier.finalize(
                self.ctx.ori_node_list,
                self.raw_elems_length,
                self.current_elems,
            )
        return self.reduced_elem_idxs



class StackStateGraphHelper:
    def __init__(self, DEBUG: bool):
        self.DEBUG = DEBUG

    def build_graph(self, current_states: list[StackState], kept_orig_indices: list[int]) -> nx.MultiDiGraph:
        G = nx.MultiDiGraph()
        for i, state in enumerate(current_states):
            G.add_node(i, state=state)
        for i, orig_idx in enumerate(kept_orig_indices):
            G.add_edge(i, i + 1, inst_idx=orig_idx)
        for i in range(len(current_states)):
            for j in range(i + 1, len(current_states)):
                if sstate1_support_sstate2(current_states[i], current_states[j]):
                    G.add_edge(j, i, is_balance_edge=True)
        return G

    def find_cycles(self, G: nx.MultiDiGraph) -> list[list[int]]:
        return list(nx.simple_cycles(G))

    def extract_instruction_indices_from_cycle(self, G: nx.MultiDiGraph, cycle: list[int]) -> list[int]:
        inst_indices = []
        for i in range(len(cycle)):
            cur_node = cycle[i]
            next_node = cycle[(i + 1) % len(cycle)]
            if G.has_edge(cur_node, next_node):
                edge_data_dict = G[cur_node][next_node]
                for edge_data in edge_data_dict.values():
                    if 'inst_idx' in edge_data:
                        inst_indices.append(edge_data['inst_idx'])
                        break
        return inst_indices

    @staticmethod
    def is_continuous_sequence(inst_idxs: list[int]) -> bool:
        if len(inst_idxs) <= 1:
            return True
        sorted_idxs = sorted(inst_idxs)
        for i in range(1, len(sorted_idxs)):
            if sorted_idxs[i] != sorted_idxs[i - 1] + 1:
                return False
        return True

    def extract_reducible_sequences(self, G: nx.MultiDiGraph, cycles: list[list[int]]) -> list[list[int]]:
        reducible_sequences = []
        if self.DEBUG:
            print(f"Processing {len(cycles)} detected cycles")
        for cycle in cycles:
            if not cycle:
                continue
            inst_indices = self.extract_instruction_indices_from_cycle(G, cycle)
            if inst_indices and self.is_continuous_sequence(inst_indices):
                reducible_sequences.append(inst_indices)
        if self.DEBUG:
            print(f"Final reducible sequences: {reducible_sequences}")
        return reducible_sequences

