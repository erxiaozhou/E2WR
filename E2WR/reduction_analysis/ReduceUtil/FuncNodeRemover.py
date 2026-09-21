from pathlib import Path
from random import shuffle
import time
from typing import Callable, Optional, Union
from extract_block_mutator.WasmParser import WasmParser
from util.debug_util import ValidateCheckType, get_validation_info, validate_wasm
from file_util import copy_file
from reduction_analysis.ProbDDUtil.prob_dd import ProbDD
from reduction_analysis.ReductionDescUtil.Oracle import CheckType
from .RemoveDeadFunc import remove_dead_funcs
from ..ParserModification import Encoder, WMSnapshot, apply_mutation_and_encode_keep_snapshot
from ..ParserModificationUtil import MutationBatch
from reduction_analysis.ProbDDUtil.ProbDDFactory import ProbDDFactory
from reduction_analysis.ProbDDUtil.adapt_util import get_test_cfg_func_for_dd


def remove_funcs_by_dd(
    tmp_used_path: Union[str, Path],
    oracle_func: Callable,
    input_snapshot: WMSnapshot,
    cur_output_path: str,
    DEBUG: bool = False,
    timeout: Optional[float] = None,
    weights: Optional[dict[int, float]] = None,
    proi_func_idxs: Optional[set[int]] = None,
    callsite_as_unreachable: bool=False
) -> tuple[WMSnapshot, set[int]]:
    t0 = time.time()
    reducer = FuncNodesRemoveReducer(
        tmp_used_path=tmp_used_path,
        oracle_func=oracle_func,
        DEBUG=DEBUG,
        callsite_as_unreachable=callsite_as_unreachable
    )
    if DEBUG:
        Encoder().encode_without_mutation(input_snapshot, tmp_used_path)
        is_valid = validate_wasm(tmp_used_path, tag=ValidateCheckType.SELF_CHECK)
        assert is_valid, f'{tmp_used_path} is invalid before reduction'
    reduced_snapshot, result = reducer.reduce_by_remove(
        input_snapshot, cur_output_path, timeout,
        weights=weights, 
        all_func_idxs=proi_func_idxs)
    if proi_func_idxs is not None:
        # result = result.intersection(proi_func_idxs)
        print(f'There are {len(proi_func_idxs)} candidate functions.')
    print(f'Removed {len(result)} functions by DD.')
    print(f'Function removal time: {time.time() - t0:.2f} seconds.')
    return reduced_snapshot, result


class FuncNodesRemoveReducer:
    def __init__(
        self,
        tmp_used_path: Union[str, Path],
        oracle_func: Callable,
        DEBUG: bool = False,
        callsite_as_unreachable: bool=False
    ):
        self.tmp_write_path = str(tmp_used_path)
        self.DEBUG = DEBUG
        self.oracle_func = oracle_func
        self.callsite_as_unreachable = callsite_as_unreachable

        self.to_stop_time = None

    def reduce_by_remove(self,
               input_snapshot: WMSnapshot,
               cur_output_path: str,
               timeout: Optional[float] = None,
                all_func_idxs: Optional[set[int]] = None,
                weights: Optional[dict[int, float]] = None,
               ) -> tuple[WMSnapshot, set[int]]:
        #
        print('Start removing functions by DD...')
        self.output_path = cur_output_path
        start_time = time.time()
        snapshot = input_snapshot
        # Keep an immutable base snapshot for DD test generation.
        # Each `try_remove` test must be evaluated from the same base.
        self.base_snapshot = snapshot
        # Keep latest accepted snapshot for returning to callers.
        self.snapshot = snapshot
        self.last_committed_save_descs: set[int] = set(range(len(snapshot.parser.defined_funcs)))
        if all_func_idxs is None:
            all_func_idxs = set(range(len(snapshot.parser.defined_funcs)))
        else:
            all_func_idxs = set(all_func_idxs).intersection(set(range(len(snapshot.parser.defined_funcs))))
        print('There are {} candidate functions to remove.'.format(len(all_func_idxs)))
        if timeout is not None:
            self.to_stop_time = start_time + timeout

        self.candidate_func_idxs = all_func_idxs
        self.all_candidate_func_idxs = set(all_func_idxs)
        #
        test_config = get_test_cfg_func_for_dd(self.try_remove)
        dd: ProbDD = ProbDDFactory.get_default_probdd(test_config, task_id='FuncR')
        # 
        config = sorted(list(self.candidate_func_idxs))
        candidates = self.candidate_func_idxs
        if weights is None:
            shuffle(config)
        # else:
            
            
        #  
        minimal_config = dd(sorted(list(self.candidate_func_idxs)), weights, self.to_stop_time)
        minimal_config_set = set(minimal_config)
        removed_idxs = self.all_candidate_func_idxs - minimal_config_set
        # Ensure returned snapshot corresponds to final DD result.
        if self.last_committed_save_descs != minimal_config_set:
            final_snapshot = self._build_snapshot_from_save_descs(minimal_config)
            copy_file(self.tmp_write_path, self.output_path)
            self.snapshot = final_snapshot
            self.last_committed_save_descs = minimal_config_set

        # Refresh only parser to avoid rebuilding full WMSnapshot structures.
        self.snapshot.parser = WasmParser.from_wasm_path(self.output_path)

        return self.snapshot, removed_idxs

    def _build_snapshot_from_save_descs(
        self,
        to_save_descs: list[int],
    ) -> WMSnapshot:
        to_delete_descs = list(
            self.all_candidate_func_idxs - set(to_save_descs))

        func_inst_mutations, section_mutations = remove_dead_funcs(
            self.base_snapshot.parser,
            self.DEBUG,
            set(to_delete_descs),
            callsite_as_unreachable=self.callsite_as_unreachable
        )
        mutation_batch = MutationBatch(
            definition_mutations=section_mutations,
            func_inst_mutations=func_inst_mutations,
        )

        return apply_mutation_and_encode_keep_snapshot(
            self.base_snapshot,
            mutation_batch,
            self.tmp_write_path,
        )

    def try_remove(
        self,
        to_save_descs: list[int],
    ):
        if self.to_stop_time is not None and time.time() > self.to_stop_time:
            return False

        to_delete_descs = list(
            self.all_candidate_func_idxs - set(to_save_descs))
        t11 = time.time()
        t0 = time.time()
      
        
        t1 = time.time()
        new_snapshot = self._build_snapshot_from_save_descs(to_save_descs)
        t2 = time.time()
        t3 = time.time()
        if self.DEBUG:
            is_valid = validate_wasm(self.tmp_write_path, tag=ValidateCheckType.TEST_CHECK)
            if not is_valid:
                info_ = get_validation_info(self.tmp_write_path)
                assert info_ is not None
                if 'type mismatch in initializer expression' in info_:
                    pass
                else:
                    raise Exception(f'{self.tmp_write_path} is invalid: {info_}')
        print(f'parser time: {t1 - t0:.2e} remove time: {t2 - t1:.2e} rewrite time: {t3 - t2:.2e}')
        if self.oracle_func(self.tmp_write_path, CheckType.TEST_CHECK):
            copy_file(self.tmp_write_path, self.output_path)
            self.snapshot = new_snapshot
            self.last_committed_save_descs = set(to_save_descs)
            return True
        return False
