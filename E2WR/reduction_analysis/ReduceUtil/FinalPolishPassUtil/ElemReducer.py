from extract_block_mutator.encode.NGDataPayload import DataPayloadwithName
from WasmInfoCfg import SectionType
from extract_block_mutator.get_data_shell import get_elemseg_attr
from reduction_analysis.ParserModification import DefinitionMutation, WMSnapshot
from reduction_analysis.ParserModificationUtil import MutationBatch
from typing import Callable, Optional
from reduction_analysis.ReduceUtil.BaseReducer import BaseReducerWithTimeout
from .util import get_idxs_by_strategy, ListDataScheduleStrategy


class ElemReducer(BaseReducerWithTimeout):
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
        self.input_sp.set_mutable_sections({SectionType.Element})
        self.output_path = output_path
        self.schedule_strategy = schedule_strategy
        self.max_try_num = max_try_num

    def _gen_one_data_def_and_test(
        self,
        raw_bytes:list
    ):
        new_def = self.encoder_func(raw_bytes)
        mutation = DefinitionMutation(
                    sec_type=SectionType.Element,
                    start_offset=self.elem_idx,
                    end_offset=self.elem_idx + 1,
                    new_definitions=[new_def]
                )
        return self.mutate_test_and_commit(
            self.input_sp,
            MutationBatch(
                func_inst_mutations=[],
                definition_mutations=[mutation]
            ),
            self.output_path,
            on_commit=lambda: _sync_elem_parser(self.input_sp, self.elem_idx, new_def)
        )

    def reduce(self, duration_sec: Optional[float] = None) -> WMSnapshot:
        self.set_duration_sec_and_start_now(duration_sec)
        elem_segs = self.input_sp.parser.elem_sec_datas
        get_lengths_func: Callable[[], list[int]] = lambda : [get_elemseg_attr(adata, 'elem_len') for adata in elem_segs] # type: ignore
        elem_idx_order = get_idxs_by_strategy(
            len(elem_segs),
            strategy=self.schedule_strategy,
            get_elem_lengths_func=get_lengths_func
        )
        for elem_idx in elem_idx_order:
            if self.is_timeout_with_system_time():
                break
            self.elem_idx = elem_idx
            ori_data = elem_segs[elem_idx]
            data = ori_data.data
            name = ori_data.inner_name.name
            if name == 'active_elem_seg0':
                self.encoder_func = lambda x : DataPayloadwithName(
                    name=name,
                    data={
                        'funcidxs': x,
                        'offset': data['offset']
                    }
                )
                elem_key = 'funcidxs'
            elif name == 'passive_elem_seg0':
                self.encoder_func = lambda x : DataPayloadwithName(
                    name=name,
                    data={
                        'funcidxs': x,
                        'elemkind': data['elemkind']
                    }
                )
                elem_key = 'funcidxs'
            elif name == 'active_elem_seg1':
                self.encoder_func = lambda x : DataPayloadwithName(
                    name=name,
                    data={
                        'funcidxs': x,
                        'elemkind': data['elemkind'],
                        'table_idx': data['table_idx'],
                        'offset': data['offset']
                    }
                )
                elem_key = 'funcidxs'
            elif name == 'declarative_elem_seg0':
                self.encoder_func = lambda x : DataPayloadwithName(
                    name=name,
                    data={
                        'funcidxs': x,
                        'elemkind': data['elemkind']
                    }
                )
                elem_key = 'funcidxs'
            elif name == 'active_elem_seg2':
                self.encoder_func = lambda x : DataPayloadwithName(
                    name=name,
                    data={
                        'exprs': x,
                        'offset': data['offset']
                    }
                )
                elem_key = 'exprs'
            elif name == 'passive_elem_seg1':
                self.encoder_func = lambda x : DataPayloadwithName(
                    name=name,
                    data={
                        'exprs': x,
                        'elemkind': data['elemkind']
                      
                    }
                )
                elem_key = 'exprs'
            elif name == 'active_elem_seg3':
                self.encoder_func = lambda x : DataPayloadwithName(
                    name=name,
                    data={
                        'exprs': x,
                        'elemkind': data['elemkind'],
                        'tableidx': data['tableidx'],
                        'offset': data['offset']
                    }
                )
                elem_key = 'exprs'
            elif name == 'declarative_elem_seg1':
                self.encoder_func = lambda x : DataPayloadwithName(
                    name=name,
                    data={
                        'exprs': x,
                        'elemkind': data['elemkind']
                    }
                )
                elem_key = 'exprs'
            # start from 0
            elems = data[elem_key]
            r = self._gen_one_data_def_and_test([])
            if r is not None:
                self.input_sp = r
                if self.is_timeout_with_system_time():
                    return self.input_sp
                continue
            left = 1
            right = len(elems)
            cur_def_attempt = 0
            while left <= right:
                if self.is_timeout_with_system_time():
                    return self.input_sp
                mid = (left + right) // 2
                if self.DEBUG:
                    print(f'elem_idx: {elem_idx}, left: {left}, right: {right}, mid: {mid}')
                r = self._gen_one_data_def_and_test(elems[:mid])
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


def _sync_elem_parser(snapshot: WMSnapshot, elem_idx: int, new_def: DataPayloadwithName):
    snapshot.parser.elem_sec_datas[elem_idx] = new_def
