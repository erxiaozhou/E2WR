from pathlib import Path
from typing import Callable, Optional, Union
from WasmInfoCfg import SectionType
from extract_block_mutator.InstGeneration.InstFactory import InstFactory
from extract_block_mutator.WasmParser import WasmParser
from extract_block_mutator.funcType import funcType
from extract_block_mutator.wasmFunc import wasmFunc
from ..BaseReducer import BaseReducer
from ...ParserModification import WMSnapshot
from ...ParserModificationUtil import MutationBatch
from ...ParserModification import DefinitionMutation
from reduction_analysis.ProbDDUtil.ProbDDFactory import ProbDDFactory
from reduction_analysis.ProbDDUtil.adapt_util import get_test_cfg_func_for_dd
import time


def remove_funcs_by_replace_unreachable(
    tmp_used_path: Union[str, Path],
    oracle_func:Callable,
    input_sp: WMSnapshot,
    output_path: str,
    DEBUG: bool = False,
    timeout: Optional[float] = None
) -> WMSnapshot:
    reducer = FuncNodesReplaceUnreachableReducer(
        tmp_used_path=tmp_used_path,
        input_sp=input_sp,
        output_path=output_path,
        oracle_func=oracle_func,
        DEBUG=DEBUG
    )
    return reducer.reduce(timeout)


class FuncNodesReplaceUnreachableReducer(BaseReducer):
    def __init__(
        self,
        tmp_used_path: Union[str, Path],
        input_sp: WMSnapshot,
        output_path: str,
        oracle_func:Callable,
        DEBUG: bool = False
    ):
        super().__init__(tmp_used_path=tmp_used_path, oracle_func=oracle_func, DEBUG=DEBUG)
        self.input_sp = input_sp
        self.input_sp.set_mutable_sections({SectionType.Code})
        self.output_path = output_path

        self.to_stop_time = None
        self.snapshot = self.input_sp

    def reduce(self,
               timeout: Optional[float] = None
               ) -> WMSnapshot:
        #
        start_time = time.time()
        snapshot = self.snapshot
        # all_func_idxs = set(range(len(ml.parser.defined_funcs)))
        self.candidate_func_idxs = set()
        for func_idx, func in enumerate(snapshot.parser.defined_funcs):
            if not _a_func_only_have_one_or_less_inst(func):
                self.candidate_func_idxs.add(func_idx)
        

        if timeout is not None:
            self.to_stop_time = start_time + timeout

        self.all_candidate_func_idxs = set(self.candidate_func_idxs)
        #
        test_config = get_test_cfg_func_for_dd(self._try_remove)
        dd = ProbDDFactory.get_default_probdd(test_config, task_id='FuncR')
        minimal_config = dd(self.candidate_func_idxs)
        removed_idxs = self.all_candidate_func_idxs - set(minimal_config)
        if len(removed_idxs) == 0:
            return self.input_sp

        final_mutations = gen_unreachable_replacement_mutations(
            self.snapshot.parser,
            set(removed_idxs)
        )
        final_snapshot = self.mutate_test_and_commit(
            self.snapshot,
            MutationBatch(definition_mutations=final_mutations),
            self.output_path,
            on_commit=lambda: _merge_code_mutations_to_parser(self.snapshot.parser, final_mutations)
        )
        if final_snapshot is not None:
            self.input_sp = final_snapshot
            return final_snapshot

        return self.input_sp

    def _try_remove(
        self,
        to_save_descs: list[int],
    ):
        if self.to_stop_time is not None and time.time() > self.to_stop_time:
            return False

        to_delete_descs = list(
            self.all_candidate_func_idxs - set(to_save_descs))
        
        snapshot = self.snapshot
        section_mutations = gen_unreachable_replacement_mutations(
            snapshot.parser,
            set(to_delete_descs)
        )

        new_snapshot = self.mutate_test_and_commit(
            snapshot, 
            MutationBatch(
                definition_mutations=section_mutations
            ),
            self.output_path
        )
        if new_snapshot is not None:
            return True
        return False


def _a_func_only_have_one_or_less_inst(
    func:wasmFunc
):
    if len(func.insts) < 2:
        return True
    return False

def gen_unreachable_replacement_mutations(
    parser:WasmParser,
    to_remove_func_idxs:set[int]
):
    section_mutations = []
    for func_idx in to_remove_func_idxs:
        func = parser.defined_funcs[func_idx]
        new_func = _gen_a_func_with_one_unreachable_inst(func.func_ty)
        section_mutations.append(DefinitionMutation(
            sec_type=SectionType.Code,
            start_offset=func_idx,
            end_offset=func_idx + 1,
            new_definitions=[new_func]
        ))
    return section_mutations

def _gen_a_func_with_one_unreachable_inst(raw_func_type:funcType):
    if len(raw_func_type.result_types) == 0:
        func = wasmFunc(
            func_ty=raw_func_type,
            insts=[],
            defined_local_types=[]
        )
    else: 
        func = wasmFunc(
        func_ty=raw_func_type,
        insts=[InstFactory.opcode_inst('unreachable')],
        defined_local_types=[]
    )
    return func


def _merge_code_mutations_to_parser(
    parser: WasmParser,
    definition_mutations: list[DefinitionMutation]
):
    code_mutations = [m for m in definition_mutations if m.sec_type == SectionType.Code]
    sorted_mutations = sorted(code_mutations, key=lambda m: m.start_offset, reverse=True)
    for mutation in sorted_mutations:
        parser.defined_funcs[mutation.start_offset:mutation.end_offset] = mutation.raw_new_definitions