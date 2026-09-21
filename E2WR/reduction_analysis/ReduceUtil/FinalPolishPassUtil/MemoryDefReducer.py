from util.debug_util import validate_wasm
from extract_block_mutator.DefShell import gen_limit1, gen_limit2
from extract_block_mutator.encode.NGDataPayload import DataPayloadwithName
from WasmInfoCfg import SectionType
from reduction_analysis.ParserModification import DefinitionMutation, WMSnapshot
from reduction_analysis.ParserModificationUtil import MutationBatch
from typing import Optional
from typing import Callable
from reduction_analysis.ReduceUtil.BaseReducer import BaseReducerWithTimeout


class MemoryDefReducer(BaseReducerWithTimeout):
    def __init__(
        self,
        tmp_used_path:str,
        input_sp:WMSnapshot,
        output_path:str,
        oracle_func:Callable,
        DEBUG:bool
    ):
        super().__init__(tmp_used_path=tmp_used_path, oracle_func=oracle_func, DEBUG=DEBUG)
        self.input_sp = input_sp
        self.input_sp.set_mutable_sections({SectionType.Memory})
        self.output_path = output_path
        
    def reduce(self, duration_sec: Optional[float] = None) -> WMSnapshot:
        self.set_duration_sec_and_start_now(duration_sec)
        snapshot = self.input_sp
        defined_memory_num = snapshot.parser.defined_memory_num
        for dm_idx in range(defined_memory_num):
            if self.is_timeout_with_system_time():
                break
            ori_m = snapshot.parser.defined_memory_datas[dm_idx]
            payload_name = ori_m.inner_name.name
            # assert payload_name
            if payload_name == 'limits_without_max':
                hax_max = False
                max_ = None
                min_ = ori_m.data['min']
            elif payload_name == 'limits_with_max':
                hax_max = True
                max_ = ori_m.data['max']
                min_ = ori_m.data['min']
            else:
                raise Exception(f'{payload_name} is not supported')
            cur_max_ = max_
            cur_min_ = min_
            # step 0 : try the minimum value
            r = self.try_replace_one_def(snapshot, dm_idx, self._gen_memory_def(1))
            if r is not None:
                snapshot = r
                self.input_sp = snapshot
                continue
            # if hax_max:
            #     continue
            # step 1 : replace limits_with_max with limits_without_max
            if hax_max:
                new_m = self._gen_memory_def(min_)
                r = self.try_replace_one_def(snapshot, dm_idx, new_m)
                if r is not None:
                    snapshot = r
                    self.input_sp = snapshot
                    cur_max_ = None
            # use binary search to find the minimum cur_min_
            bs_min_ = 1
            bs_max_ = min_
            while bs_min_ < bs_max_:
                if self.is_timeout_with_system_time():
                    return self.input_sp
                bs_mid_ = (bs_min_ + bs_max_) // 2
                new_m = self._gen_memory_def(bs_mid_, cur_max_)
                r = self.try_replace_one_def(snapshot, dm_idx, new_m)
                if r is not None:
                    bs_max_ = bs_mid_
                    snapshot = r
                    self.input_sp = snapshot
                else:
                    bs_min_ = bs_mid_ + 1
            cur_min_ = bs_min_
            if cur_max_ is not None:
                bs_min_ = cur_min_ 
                bs_max_ = cur_max_
                while bs_min_ < bs_max_:
                    if self.is_timeout_with_system_time():
                        return self.input_sp
                    bs_mid_ = (bs_min_ + bs_max_) // 2
                    new_m = self._gen_memory_def(cur_min_, bs_mid_)
                    r = self.try_replace_one_def(snapshot, dm_idx, new_m)
                    if r is not None:
                        bs_max_ = bs_mid_
                        snapshot = r
                        self.input_sp = snapshot
                    else:
                        bs_min_ = bs_mid_ + 1
                cur_max_ = bs_max_
        return self.input_sp

    def _gen_memory_def(
        self,
        min_:int,
        max_:Optional[int]=None
    )->DataPayloadwithName:
        if max_ is None:
            return gen_limit1(min_)
        else:
            return gen_limit2(min_, max_)

    def try_replace_one_def(
        self, 
        snapshot:WMSnapshot,
        mem_idx:int,
        new_m:DataPayloadwithName
    )->Optional[WMSnapshot]:
        return self.mutate_test_and_commit(
            snapshot,
            MutationBatch(
                definition_mutations=[
                    DefinitionMutation(
                        sec_type=SectionType.Memory,
                        start_offset=mem_idx,
                        end_offset=mem_idx + 1,
                        new_definitions=[new_m]
                    )
                ]
            ),
            self.output_path,
            on_commit=lambda: _sync_memory_parser(snapshot, mem_idx, new_m)
        )


def _sync_memory_parser(snapshot: WMSnapshot, mem_idx: int, new_m: DataPayloadwithName):
    snapshot.parser.defined_memory_datas[mem_idx] = new_m

