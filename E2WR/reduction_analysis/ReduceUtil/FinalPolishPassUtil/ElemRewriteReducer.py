from extract_block_mutator.DefShell import gen_elem_decl0, gen_elem_passive0, gen_elem_seg0, gen_elem_seg2
from extract_block_mutator.encode.NGDataPayload import DataPayloadwithName
from extract_block_mutator.get_data_shell import get_func_idxs_or_null_list_from_exprs
from WasmInfoCfg import SectionType
from reduction_analysis.ParserModification import DefinitionMutation, WMSnapshot
from reduction_analysis.ParserModificationUtil import MutationBatch
from typing import Optional
from typing import Callable
from reduction_analysis.ReduceUtil.BaseReducer import BaseReducerWithTimeout
from .util import get_int_from_expr_or_int


class ElemRewriteReducer(BaseReducerWithTimeout):
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
        self.input_sp.set_mutable_sections({SectionType.Element})
        self.output_path = output_path

    def reduce(self, duration_sec: Optional[float] = None) -> WMSnapshot:
        self.set_duration_sec_and_start_now(duration_sec)
        sp = self.input_sp
        
        defined_elem_num = len(sp.parser.elem_sec_datas)
        for elem_idx in range(defined_elem_num):
            if self.is_timeout_with_system_time():
                break
            ori_e = sp.parser.elem_sec_datas[elem_idx]
            new_data = self._rewrite_one_elem(ori_e)
            if new_data is None:
                continue
            r = self.try_replace_one_def(sp, elem_idx, new_data)
            if r is not None:
                sp = r
                self.input_sp = sp
        return self.input_sp

    def _rewrite_one_elem(self, ori_elem:DataPayloadwithName)-> Optional[DataPayloadwithName]:
        name  = ori_elem.inner_name.name
        if name in ['active_elem_seg0', 'passive_elem_seg0', 'active_elem_seg1', 'declarative_elem_seg0']:
            return None
        func_idxs = get_func_idxs_or_null_list_from_exprs(ori_elem.data['exprs'])
        if any(func_idx is None for func_idx in func_idxs):
            return None
        if name == 'active_elem_seg2':
            return gen_elem_seg0(
                funcidxs=func_idxs,
                offset=get_int_from_expr_or_int(ori_elem.data['offset'])
            )
        elif name == 'passive_elem_seg1':
            return gen_elem_passive0(func_idxs)
        elif name == 'active_elem_seg3':
            # func_idxs = get_func_idxs(ori_elem)
            data = ori_elem.data
            tableidx = data['tableidx']
            offset = get_int_from_expr_or_int(data['offset'])
            # func_idxs = data['exprs']
            return gen_elem_seg2(tableidx, offset, func_idxs)
        elif name == 'declarative_elem_seg1':
            return gen_elem_decl0(func_idxs)


    def try_replace_one_def(
        self, 
        sp:WMSnapshot,
        elem_idx:int,
        new_m:DataPayloadwithName
    )->Optional[WMSnapshot]:
        return self.mutate_test_and_commit(
            sp,
            MutationBatch(
                definition_mutations=[
                    DefinitionMutation(
                        sec_type=SectionType.Element,
                        start_offset=elem_idx,
                        end_offset=elem_idx + 1,
                        new_definitions=[new_m]
                    )
                ]
            ),
            self.output_path,
            on_commit=lambda: _sync_elem_parser(sp, elem_idx, new_m)
        )


def _sync_elem_parser(sp: WMSnapshot, elem_idx: int, new_m: DataPayloadwithName):
    sp.parser.elem_sec_datas[elem_idx] = new_m
