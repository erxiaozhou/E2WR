from extract_block_mutator.DefShell import gen_active_data_seg0, gen_active_data_seg1, gen_passive_data_seg
from WasmInfoCfg import SectionType
from extract_block_mutator.get_data_shell import get_data_attr
from reduction_analysis.ParserModification import DefinitionMutation, WMSnapshot
from reduction_analysis.ParserModificationUtil import MutationBatch
from reduction_analysis.ProbDDUtil.ProbDDFactory import ProbDDFactory
from reduction_analysis.ProbDDUtil.adapt_util import get_test_cfg_func_for_dd
from .util import get_idxs_by_strategy, ListDataScheduleStrategy
from typing import Callable, Optional
from reduction_analysis.ReduceUtil.BaseReducer import BaseReducerWithTimeout
from .util import get_int_from_expr_or_int


class DataReducer(BaseReducerWithTimeout):
    def __init__(
        self,
        tmp_used_path:str,
        input_sp:WMSnapshot,
        output_path:str,
        oracle_func:Callable,
        DEBUG:bool,
        max_try_num:Optional[int] = None,
        schedule_strategy: ListDataScheduleStrategy = ListDataScheduleStrategy.DECREASING_LEN
    ):
        super().__init__(tmp_used_path=tmp_used_path, oracle_func=oracle_func, DEBUG=DEBUG)
        self.input_sp = input_sp
        self.input_sp.set_mutable_sections({SectionType.Data})
        self.output_path = output_path
        self.schedule_strategy = schedule_strategy
        self.max_try_num = max_try_num

    def _reduce_one_data_seg(
        self,
        raw_bytes:bytearray,
        task_name='Reduce_Data'
        
    ):
        
        test_config = get_test_cfg_func_for_dd(lambda x: self._gen_one_data_def_and_test(x) is not None)
        config = raw_bytes.copy()
        if len(config) == 0: 
            return None
        ori_cfg_len = len(config)
        dd = ProbDDFactory.get_default_probdd(test_config, task_id=task_name)
        rest_inst_idxs = dd(config)
        return bytearray([b for b in config if b  in rest_inst_idxs])

    def _gen_one_data_def_and_test(
        self,
        raw_bytes:bytearray
    ):
        new_def = self.encoder_func(raw_bytes)
        mutation = DefinitionMutation(
                    sec_type=SectionType.Data,
                    start_offset=self.data_idx,
                    end_offset=self.data_idx + 1,
                    new_definitions=[new_def]
                )
        return self.mutate_test_and_commit(
            self.input_sp,
            MutationBatch(
                func_inst_mutations=[],
                definition_mutations=[mutation]
            ),
            self.output_path,
            on_commit=lambda: _sync_data_parser(self.input_sp, self.data_idx, new_def)
        )

    def reduce(self, duration_sec: Optional[float] = None) -> WMSnapshot:
        self.set_duration_sec_and_start_now(duration_sec)
        get_lengths_func: Callable[[], list[int]] = lambda : [get_data_attr(adata, 'data_len') for adata in self.input_sp.parser.data_sec_datas] # type: ignore
        data_idx_order = get_idxs_by_strategy(
            len(self.input_sp.parser.data_sec_datas),
            strategy=self.schedule_strategy,
            get_elem_lengths_func=get_lengths_func
        )
        for data_idx in data_idx_order:
            if self.is_timeout_with_system_time():
                break
            self.data_idx = data_idx
            ori_data = self.input_sp.parser.data_sec_datas[data_idx]
            data = ori_data.data
            name = ori_data.inner_name.name
            if name == 'passive_def':
                self.encoder_func = gen_passive_data_seg
            elif name == 'active_memory_zero':
                self.encoder_func = lambda x: gen_active_data_seg0(x, get_int_from_expr_or_int(data['expression']))
            elif name == 'active_memory_index':
                self.encoder_func = lambda x: gen_active_data_seg1(x, get_int_from_expr_or_int(data['expression']), data['memory_index'])
            # start from 0
            r = self._gen_one_data_def_and_test( bytearray([]))
            if r is not None:
                self.input_sp = r
                if self.is_timeout_with_system_time():
                    return self.input_sp
                continue
            
            left = 1
            right = len(data['bytes'])
            cur_def_attempt = 0
            while left <= right:
                if self.is_timeout_with_system_time():
                    return self.input_sp
                mid = (left + right) // 2
                if self.DEBUG:
                    print(f'data_idx: {data_idx}, left: {left}, right: {right}, mid: {mid}')
                r = self._gen_one_data_def_and_test(data['bytes'][:mid])
                if r is not None:
                    self.input_sp = r
                    if self.is_timeout_with_system_time():
                        return self.input_sp
                    right = mid - 1
                else:
                    left = mid + 1
                cur_def_attempt += 1
                if self.max_try_num is not None and cur_def_attempt >= self.max_try_num:
                    break
        return self.input_sp


def _sync_data_parser(snapshot: WMSnapshot, data_idx: int, new_def):
    snapshot.parser.data_sec_datas[data_idx] = new_def
