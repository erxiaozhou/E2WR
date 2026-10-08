from util.debug_util import ValidateCheckType, get_validation_info, validate_wasm, wasm2wat
from extract_block_mutator.InstGeneration.InstFactory import InstFactory
from extract_block_mutator.InstUtil.ByteInst import ByteImmInst
from extract_block_mutator.InstUtil.Inst import Inst
from extract_block_mutator.InstUtil.InstReqUtil import get_inst_ty_req
from extract_block_mutator.WasmParser import WasmParser
from extract_block_mutator.encode.NGDataPayload import DataPayloadwithName
from extract_block_mutator.encode.new_defined_data_type import Blocktype
from extract_block_mutator.funcType import funcType
from extract_block_mutator.funcTypeFactory import funcTypeFactory
from extract_block_mutator.typeReq import merge_req, typeReq
from extract_block_mutator.wasmFunc import wasmFunc
from WasmInfoCfg import SectionType
from reduction_analysis.ASTInfo.AST import ASTINode, InstsNode
from reduction_analysis.ASTState import ASTState
from reduction_analysis.ReduceFrameWork.DefinitionReducer.UnusedDefReducerUtil import detect_used_unused_type_idxs, update_parser_after_remove_type
from reduction_analysis.ReduceUtil.InlineFunction import InlineFunctionHelper
from reduction_analysis.ReduceUtil.FinalPolishPassUtil.ReplaceFuncWithUnreachable import remove_funcs_by_replace_unreachable
from reduction_analysis.ReduceUtil.SpecificTypeInstsFactory import SpecificTypeInstsFactory
from reduction_analysis.ReductionDescUtil.Oracle import CheckType
from reduction_analysis.StackState import StackState, StackStatus, get_stack_state_from_type_req
from reduction_analysis.InferStackUtil import _get_cur_context_by_ast_info
from reduction_analysis.ParserModification import DefinitionMutation, FuncInstMutation, RewriteLocalDesc, WMSnapshot, apply_mutation_and_encode_keep_snapshot
from reduction_analysis.ParserModificationUtil import MutationBatch
from reduction_analysis.ProbDDUtil.ProbDDFactory import ProbDDFactory
from reduction_analysis.ProbDDUtil.adapt_util import get_test_cfg_func_for_dd
from reduction_analysis.ReduceFrameWork.FuncLevelNodeReducer import FuncLevelNodeReducer
from reduction_analysis.ReductionDescUtil.OneReducerDirSystem import OneReducerDirSystem
from reduction_analysis.ReduceUtil.RewritingUtil.NodeRewriter import NodeRewriter
from ..ReducerPassUtil.ReduceResult import ExecResult, ExecStatus
from reduction_analysis.ReducerCommonConfig import FINE_REDUCTION_TIMEOUT, PASS_TIMEOUT
from ..ReducerPassUtil.ReducePass import reduce_checker
from typing import Any, Optional, Union
from file_util import copy_file
from pathlib import Path
import time
from ..ReducerPassUtil.ZReducerPass import ZReducerPass
from typing import Callable
import traceback
from reduction_analysis.ReduceUtil.FinalPolishPassUtil.ElemRewriteReducer import ElemRewriteReducer
from reduction_analysis.ReduceUtil.FinalPolishPassUtil.ElemReducer import ElemReducer
from reduction_analysis.ReduceUtil.FinalPolishPassUtil.DataReducer import DataReducer
from reduction_analysis.ReduceUtil.FinalPolishPassUtil.MemoryDefReducer import MemoryDefReducer
from reduction_analysis.ReduceUtil.BaseReducer import BaseReducer

class FinalPolishPass(ZReducerPass):
    def __init__(
        self,
        dir_system:OneReducerDirSystem,
        oracle_func:Callable,
        to_test_func_name:Optional[str]=None,
        DEBUG:bool=False,
        name = 'FinalPolishPass',
        polish_return_type: bool = True,
        enable_inline: bool = True,
        enable_size_polish: bool = True,
    ):
        # assert 0
        # self.tmp_stage_path = str(Path(tmp_dir) / 'tmp_final_polish_stage.wasm')
        super().__init__(
            oracle_func=oracle_func,
            dir_system=dir_system,
            name=name,
            DEBUG=DEBUG
        )
        self.polish_return_type_enabled = polish_return_type
        self.enable_inline = enable_inline
        self.enable_size_polish = enable_size_polish
        self.node_rewriter = NodeRewriter(
            logger=self.logger,
            DEBUG=self.DEBUG,
            oracle_func=self.oracle_func,
            tmp_used_path=self.tmp_used_path,
            force_return_false_on_invalid_case=not self.DEBUG
        )
        self.to_test_func_name = to_test_func_name
        self.should_stop_time: Optional[float] = None

    def _get_remaining_timeout(self) -> float:
        if self.should_stop_time is None:
            return 0.0
        return max(self.should_stop_time - time.time(), 0.0)

    def get_not_too_long_remaining_timeout(self, max_time: float = FINE_REDUCTION_TIMEOUT) -> float:
        return min(max_time, self._get_remaining_timeout())

    def _is_timeout(self) -> bool:
        return self._get_remaining_timeout() <= 0

    def _should_skip_due_to_timeout(self, step_name: str) -> bool:
        if not self._is_timeout():
            return False
        print(f'Skip {step_name} due to timeout guard')
        return True

    @reduce_checker
    def reduce(self, 
               cur_input_path:str, 
               cur_output_path:str, 
               timeout:int=PASS_TIMEOUT)->ExecResult:
        # assert 0
        start_time = time.time()
        original_size = Path(cur_input_path).stat().st_size
        have_success = False
        self.should_stop_time = start_time + timeout + min(15.0, max(3.0, float(timeout) * 0.05))
        copy_file(cur_input_path, cur_output_path)
        cur_input_path = cur_output_path
        cur_snapshot = WMSnapshot.from_path(cur_output_path)

        if not self._should_skip_due_to_timeout('inline') and self.enable_inline:
            next_snapshot = self._call_with_catch_exception(self.inline, cur_output_path, cur_snapshot)
            if isinstance(next_snapshot, WMSnapshot):
                cur_snapshot = next_snapshot
        print(f'After inline replacement, current output size : {Path(cur_output_path).stat().st_size}')

        # unreachable replacement 
        
        if not self._should_skip_due_to_timeout('unreachable_replacement') and self.enable_size_polish:
            next_snapshot = self._call_with_catch_exception(self.unreachable_replacement, cur_output_path, cur_snapshot)
            if isinstance(next_snapshot, WMSnapshot):
                cur_snapshot = next_snapshot

        if not self._should_skip_due_to_timeout('polish_return_type') and self.polish_return_type_enabled:
            next_snapshot = self._call_with_catch_exception(self.polish_return_type, cur_output_path, cur_snapshot)
            if isinstance(next_snapshot, WMSnapshot):
                cur_snapshot = next_snapshot
        # assert 0, f'cur_output_path: {cur_output_path} '
        print(f'After polish_return_type, current output size : {Path(cur_output_path).stat().st_size}')

        # print(f'After inline and return type reduction, modified_func_idxs: {modified_func_idxs}, current output size : {Path(cur_output_path).stat().st_size}')
        if not self._should_skip_due_to_timeout('polish_local') and self.enable_size_polish:
            next_snapshot = self._call_with_catch_exception(self.polish_local, cur_output_path, cur_snapshot)
            if isinstance(next_snapshot, WMSnapshot):
                cur_snapshot = next_snapshot
        print(f'After polish_local, current output size : {Path(cur_output_path).stat().st_size}')
        if not self._should_skip_due_to_timeout('update_types'):
            next_snapshot = self._call_with_catch_exception(self.update_types, cur_output_path, cur_snapshot)
            if isinstance(next_snapshot, WMSnapshot):
                cur_snapshot = next_snapshot
        print(f'After update_types, current output size : {Path(cur_output_path).stat().st_size}')
        if not self._should_skip_due_to_timeout('reduce_memory_def') and self.enable_size_polish:
            print('Will reduce memory def')
            next_snapshot = self._call_with_catch_exception(
                self.reduce_memory_def,
                cur_output_path,
                cur_snapshot
            )
            if isinstance(next_snapshot, WMSnapshot):
                cur_snapshot = next_snapshot
        print(f'After reduce_memory_def, current output size : {Path(cur_output_path).stat().st_size}')
        if not self._should_skip_due_to_timeout('reduce_elem_def') and self.enable_size_polish:
            print('Will reduce elem def')
            next_snapshot = self._call_with_catch_exception(
                self.reduce_elem_def,
                cur_output_path,
                cur_snapshot
            )
            if isinstance(next_snapshot, WMSnapshot):
                cur_snapshot = next_snapshot
        if not self._should_skip_due_to_timeout('reduce_data_seg') and self.enable_size_polish:
            print('Will reduce data seg')
            next_snapshot = self._call_with_catch_exception(
                self.reduce_data_seg,
                cur_output_path,
                cur_snapshot
            )
            if isinstance(next_snapshot, WMSnapshot):
                cur_snapshot = next_snapshot
        if not self._should_skip_due_to_timeout('reduce_elem_seg') and self.enable_size_polish:
            print('Will reduce elem seg')
            next_snapshot = self._call_with_catch_exception(
                self.reduce_elem_seg,
                cur_output_path,
                cur_snapshot
            )
            if isinstance(next_snapshot, WMSnapshot):
                cur_snapshot = next_snapshot
        result = ExecResult(
            exec_status=ExecStatus.SUCCESS,
            exec_taken_time=time.time() - start_time,
            reduced_size_num=original_size - Path(cur_output_path).stat().st_size,
            reduced_inst_num=None,
            is_partial_by_timeout=self._is_timeout()
        )
        if self.DEBUG:
            wat_path = f'{self.tmp_dir}/tmp.wat'    
            wasm2wat(cur_output_path, wat_path)
            print(f'The final result {wat_path} is saved')
        
        return result


    def _call_with_catch_exception(
        self,
        func:Callable,
        *args,
        **kwargs
    ):
        try:
            return func(*args, **kwargs)
        except Exception as e:
            print(f'Error in {func.__name__}: {e}')
            traceback.print_exc()
            if  self.DEBUG:
                raise e
            return None

    def inline(
        self,
        cur_output_path,
        input_sp: WMSnapshot
    ) -> WMSnapshot:
        input_sp.set_mutable_sections(set(list(SectionType)))
        snapshot = input_sp
        
        can_inline_func_idxs = _get_called_ones_callee_and_caller(snapshot.parser)
            # to_ignore_func_idxs = set()
        # removed_func_idxs = set()
        while can_inline_func_idxs:
            if self._should_skip_due_to_timeout('inline loop'):
                break
            caller_func_idx, callee_func_idx = can_inline_func_idxs.pop()
            print(f'inlined caller_func_idx: {caller_func_idx} ;;  callee_func_idx : {callee_func_idx} ;; rest num : {len(can_inline_func_idxs)}')
           
                
            func_inst_mutations, rewrite_local_descs, definition_mutations = InlineFunctionHelper.inline_a_func(
                    snapshot.parser,
                    {caller_func_idx},
                    callee_func_idx
                )
            new_ml = self._mutate_and_save(
                    func_inst_mutations,
                    rewrite_local_descs,
                    definition_mutations,
                    snapshot,
                    cur_output_path
                )
            if new_ml is not None:
                snapshot = new_ml
                can_inline_func_idxs = _get_called_ones_callee_and_caller(snapshot.parser)
        return snapshot

    def unreachable_replacement(
        self,
        cur_output_path,
        input_sp: WMSnapshot
    ) -> WMSnapshot:
        return remove_funcs_by_replace_unreachable(
            tmp_used_path=self.tmp_used_path,
            oracle_func=self.oracle_func,
            input_sp=input_sp,
            output_path=cur_output_path,
            DEBUG=self.DEBUG,
        )


    def polish_return_type(
        self,
        cur_output_path,
        input_sp: WMSnapshot
    ) -> WMSnapshot:
        input_sp.set_mutable_sections({SectionType.Type, SectionType.Function, SectionType.Code})
        ml = input_sp
        un_called_func_idxs = _get_un_called_defined_func_idxs(ml.parser)
        ast_state = ASTState.from_path(cur_output_path)
        modified_func_idxs = set()
        for func_idx in un_called_func_idxs:
            if self._should_skip_due_to_timeout('polish_return_type loop'):
                break
                # input(f'Will reduce return type of func {func_idx}')
            reduce_type_mutator = RelaxRetyrnTypeReducer(
                    ml=ml,
                    func_idx=func_idx,
                    rewriter=self.node_rewriter,
                    ast_state=ast_state,
                    tmp_write_path=self.tmp_used_path,
                    output_path=cur_output_path,
                    oracle_func=self.oracle_func,
                    DEBUG=self.DEBUG,
                    expected_end_time=self.should_stop_time
                )
            new_ml = reduce_type_mutator.reduce_types()
            if new_ml is not None:
                modified_func_idxs.add(func_idx)
                ml = new_ml
                have_success = True
        print('[XX] Size After polish_return_type: ', Path(cur_output_path).stat().st_size)
        if modified_func_idxs and not self._should_skip_due_to_timeout('func level node reducer'):
                # assert False
                # if there is modified funcs, the result must be write to output_path
            inner_func_reducer = FuncLevelNodeReducer(
                    node_rewriter=self.node_rewriter,
                    DEBUG=self.DEBUG
                )
            ml = inner_func_reducer.reduce(
                    cur_input_path=cur_output_path,
                    cur_output_path=cur_output_path,
                    considered_func_idxs=modified_func_idxs,
                    timeout=int(self.get_not_too_long_remaining_timeout(300)),
                    input_snapshot=ml,
                )
            
        return ml

    def reduce_memory_def(
        self,
        cur_output_path,
        input_sp: WMSnapshot
    ):
        reducer = MemoryDefReducer(
            tmp_used_path=self.tmp_used_path,
            input_sp=input_sp,
            output_path=cur_output_path,
            oracle_func=self.oracle_func,
            DEBUG=self.DEBUG
        )
        return reducer.reduce(duration_sec=self.get_not_too_long_remaining_timeout(30))

    def reduce_elem_def(
        self,
        cur_output_path,
        input_sp: WMSnapshot
    ):
        reducer = ElemRewriteReducer(
            tmp_used_path=self.tmp_used_path,
            input_sp=input_sp,
            output_path=cur_output_path,
            oracle_func=self.oracle_func,
            DEBUG=self.DEBUG
        )
        return reducer.reduce(duration_sec=self.get_not_too_long_remaining_timeout(120))

    def reduce_data_seg(
        self,
        cur_output_path,
        input_sp: WMSnapshot
    ):
        reducer = DataReducer(
            tmp_used_path=self.tmp_used_path,
            input_sp=input_sp,
            output_path=cur_output_path,
            oracle_func=self.oracle_func,
            DEBUG=self.DEBUG
        )
        return reducer.reduce(duration_sec=self.get_not_too_long_remaining_timeout(120))

    def reduce_elem_seg(
        self,
        cur_output_path,
        input_sp: WMSnapshot
    ):
        reducer = ElemReducer(
            tmp_used_path=self.tmp_used_path,
            input_sp=input_sp,
            output_path=cur_output_path,
            oracle_func=self.oracle_func,
            DEBUG=self.DEBUG
        )
        return reducer.reduce(duration_sec=self.get_not_too_long_remaining_timeout(120))

    def update_types(
        self,
        cur_output_path,
        input_sp: WMSnapshot
    ) -> WMSnapshot:
        input_sp.set_mutable_sections(set(list(SectionType)))
        ml = input_sp
        used_type_idxs, unused_type_idxs = detect_used_unused_type_idxs(
                    ml.parser
                )
        new_func_inst_mutations, new_section_mutations = update_parser_after_remove_type(
                        ml.parser, unused_type_idxs)
        new_snapshot = self._mutate_and_save(
            new_func_inst_mutations,
            [],
            new_section_mutations,
            ml,
            cur_output_path
        )
        if new_snapshot is not None:
            return new_snapshot
        return ml

    def polish_local(
        self,
        cur_output_path,
        input_sp: WMSnapshot
    ) -> WMSnapshot:
        input_sp.set_mutable_sections({SectionType.Code})
        snapshot = input_sp
        func_idx2local_mutations = remove_useless_locals(snapshot.parser)
        for func_idx, (ims, wlm) in func_idx2local_mutations.items():
            if self._should_skip_due_to_timeout('polish_local loop'):
                break
            if wlm is None:
                continue
            
            new_snapshot = self._mutate_and_save(
                    ims,
                    [wlm],
                    [],
                    snapshot,
                    cur_output_path
                )
            if new_snapshot is not None:
                snapshot = new_snapshot
        print(f'After local reduction, current output size : {Path(cur_output_path).stat().st_size}')
        return snapshot

    def _relocate_func_idxs(self, 
                            raw_func_idx:int,
                            removed_func_idxs:set[int]
    )->int:
        minus = 0
        for removed_func_idx in removed_func_idxs:
            if raw_func_idx > removed_func_idx:
                minus += 1
        return raw_func_idx - minus

    def _mutate_and_save(self, 
                         func_inst_mutations:list[FuncInstMutation], 
                         local_rewrite_descs:list[RewriteLocalDesc], 
                         definition_mutations:list[DefinitionMutation],
                         snapshot:WMSnapshot, 
                         output_file
                         )->Optional[WMSnapshot]:
        new_snapshot = apply_mutation_and_encode_keep_snapshot(
            snapshot,
            MutationBatch(
                func_inst_mutations=func_inst_mutations,
                rewrite_local_descs=local_rewrite_descs,
                definition_mutations=definition_mutations
            ),
            self.tmp_used_path
        )
        if self.DEBUG:
            is_valid = validate_wasm(self.tmp_used_path, tag=ValidateCheckType.TEST_CHECK)
            if not is_valid:
                info_ = get_validation_info(self.tmp_used_path)
                assert info_ is not None
                if 'is not declared in any elem sections' in info_:
                    pass
                else:
                    raise Exception(f'{self.tmp_used_path} is invalid, info: ')
        if self.oracle_func(self.tmp_used_path, CheckType.TEST_CHECK):
            copy_file(self.tmp_used_path, output_file)
            merge_mutations_to_parser(
                snapshot.parser,
                func_inst_mutations,
                local_rewrite_descs,
                definition_mutations
            )
            return new_snapshot
        return None


def merge_mutations_to_parser(
    parser:WasmParser,
    func_inst_mutations:list[FuncInstMutation],
    local_rewrite_descs:list[RewriteLocalDesc],
    definition_mutations:list[DefinitionMutation]
):
    func_idx2inst_mutations: dict[int, list[FuncInstMutation]] = {}
    for rewrite_local_desc in local_rewrite_descs:
        parser.defined_funcs[rewrite_local_desc.func_idx].defined_local_types = rewrite_local_desc.new_defined_locals
    
    for m in func_inst_mutations:
        func_idx2inst_mutations.setdefault(m.func_idx, []).append(m)
    
    for func_idx, inst_mutations in func_idx2inst_mutations.items():
        wasm_func = parser.defined_funcs[func_idx]
        
        sorted_mutations = sorted(inst_mutations, key=lambda m: m.start_offset, reverse=True)
        for mutation in sorted_mutations:
            wasm_func.insts[mutation.start_offset:mutation.end_offset] = mutation.new_insts
        
    supported_sections = {SectionType.Type, SectionType.Import, SectionType.Function, SectionType.Code, SectionType.Export, SectionType.Element}
    sec_type2mutations = {}
    
    for m in definition_mutations:
        assert m.sec_type in supported_sections, f'{m.sec_type} is not in {supported_sections}'
        sec_type2mutations.setdefault(m.sec_type, []).append(m)
    
    sec_type2target_list = {
        SectionType.Type: parser.types,
        SectionType.Import: parser.imports,
        SectionType.Function: parser.defined_func_ty_ids,
        SectionType.Code: parser.defined_funcs,
        SectionType.Export: parser.exports,
        SectionType.Element: parser.elem_sec_datas
    }
    for sec_type, mutations in sec_type2mutations.items():
        sorted_mutations = sorted(mutations, key=lambda m: m.start_offset, reverse=True)
        
        for mutation in sorted_mutations:
            sec_type2target_list[sec_type][mutation.start_offset:mutation.end_offset] = mutation.raw_new_definitions

def _get_called_ones_callee_and_caller(
    parser:WasmParser
):
    # 
    called2caller:dict[int, list[int]] = {}
    for func_idx, func in enumerate(parser.defined_funcs):
        called2caller.setdefault(func_idx, [])
        for inst in func.insts:
            if inst.opcode_text == 'call':
                defined_idx = inst.imm_part.val-parser.import_func_num
                if defined_idx < 0:
                    continue
                called2caller.setdefault(defined_idx, []).append(func_idx)
    # 
    results:list[tuple[int, int]] = []
    for callee_func_idx, caller_func_idxs in called2caller.items():
        if len(caller_func_idxs) == 1:
            caller_func_idx = list(caller_func_idxs)[0]
            results.append((caller_func_idx, callee_func_idx))
    return results

def _get_un_called_defined_func_idxs(
    parser:WasmParser
):
    called2caller:dict[int, set[int]] = _get_defined_func_call_times_relation(parser)
    results:list[int] = []
    for func_idx, caller_func_idxs in called2caller.items():
        if len(caller_func_idxs) == 0:
            results.append(func_idx)
    return results

def _get_defined_func_call_times_relation(
    parser:WasmParser
):
    defined_called2defined_caller:dict[int, set[int]] = {}
    for func_idx, func in enumerate(parser.defined_funcs):
        defined_called2defined_caller.setdefault(func_idx, set())
        for inst in func.insts:
            if inst.opcode_text == 'call':
                defined_called2defined_caller.setdefault(inst.imm_part.val-parser.import_func_num, set()).add(func_idx)
                # called2caller.setdefault(inst.imm_part.val, set()).add(func_idx)
    return defined_called2defined_caller

# rewrite function return type ========================================================================================

class DropOrParams:
    def __init__(
        self,
        drop:bool,
        params:list[int]
    ):
        self.drop = drop
        self.params = params


class RelaxRetyrnTypeReducer(BaseReducer):
    def __init__(
        self,
        ml:WMSnapshot,
        func_idx:int,
        rewriter:NodeRewriter,
        ast_state:ASTState,
        tmp_write_path:str,
        output_path:str,
        oracle_func:Callable,
        DEBUG:bool,
        expected_end_time: Optional[float] = None
    ):
        super().__init__(
            tmp_used_path=tmp_write_path,
            oracle_func=oracle_func,
            DEBUG=DEBUG
        )
        # 
        # 
        self.tmp_write_path = tmp_write_path
        self.output_path = output_path
        self.oracle_func = oracle_func
        self.snapshot = ml
        self.rewriter = rewriter
        self.ast_state = ast_state
        self.DEBUG = DEBUG
        ori_node_list = ast_state.ast_info.get_func_root_ast(func_idx)
        self.ori_func = ast_state.parser.defined_funcs[func_idx]
        self.func_idx = func_idx
        self.ori_locals = self.ori_func.defined_local_types
        self.loc = ori_node_list.loc
        self.raw_func_param_types = ast_state.parser.defined_funcs[func_idx].func_ty.param_types
        # 
        self.can_skip = len(ast_state.parser.defined_funcs[func_idx].func_ty.result_types) == 0
        self.elems:list[Union[Inst, ASTINode]]=[]
        # infer each node type and req
        self.node_param_types:list[DropOrParams] = []
        self.elem_reqs = []
        self.raw_types = ast_state.parser.types
        # self.to_append_types = []
        self.insts_factory = SpecificTypeInstsFactory()
        # context, nodes, last_block_param, _ = get_structure_before_probe_loc(
        #     ori_node_list.loc,
        #     ast_state.parser,
        #     ast_state.ast_info
        # )
        context = _get_cur_context_by_ast_info(
            ast_state.parser,
            ast_state.ast_info,
            ori_node_list.loc,
            ori_node_list
        )
       
        self.context = context
        for node in ori_node_list.sub_nodes: # type: ignore
            if isinstance(node, InstsNode):
                for inst in node.insts:
                    type_req = get_inst_ty_req(inst, context_info=context)
                    assert type_req is not None
                    if inst.opcode_text == 'drop':
                        self.node_param_types.append(DropOrParams(drop=True, params=type_req.ty0.param_types))
                    else:
                        self.node_param_types.append(DropOrParams(drop=False, params=type_req.ty0.param_types))
                    self.elem_reqs.append(type_req)
                    self.elems.append(inst)
            else:
                node_type_req = node.get_type_req(context=context)
                self.node_param_types.append(DropOrParams(drop=False, params=node_type_req.ty0.param_types))
                self.elem_reqs.append(node_type_req)
                self.elems.append(node)
        self.list_node_type = ori_node_list.get_block_type()
        self.stack_init_state = StackState(
            all_rest_types=[self.list_node_type.param_types],
            status=StackStatus.NORMAL
        )
        self.to_stop_time = expected_end_time
        
        # 
        must_save_ones = []
        # for i, elem in enumerate(self.elems):
        #     if isinstance(elem, Inst):
        #         must_save_ones.append(i)
        self.must_save_ones = []
        self.cfg = [i for i in range(len(self.elems)) if i not in must_save_ones]
        # 

        self.def_mutations = []
        # 
        self.func_only_have_unreachable_inst = len(self.ori_func.insts) == 1 and self.ori_func.insts[0].opcode_text == 'unreachable'
        self.insts_factory = SpecificTypeInstsFactory()
        
    def reduce_types(
        self
    )->Optional[WMSnapshot]:
        if self.can_skip:
            return None
        if self.to_stop_time is not None and time.time() >= self.to_stop_time:
            return None
        to_reduce_elems = self.cfg
        ori_cfg_len = len(to_reduce_elems)
        # 
        
        test_config = get_test_cfg_func_for_dd(self.try_remove)
        dd = ProbDDFactory.get_default_probdd(test_config, task_id='RTY')
        minimal_config = dd(to_reduce_elems, expected_end_time=self.to_stop_time)
        if self.to_stop_time is not None and time.time() >= self.to_stop_time:
            return None
        if len(minimal_config) == ori_cfg_len:
            return None
        # self.cur_return_type_len = len(minimal_config)
        new_snapshot = self.mutate_test_and_commit(
            self.snapshot,
            MutationBatch(
                definition_mutations=self.def_mutations
            ),
            self.output_path,
            on_commit=lambda: merge_mutations_to_parser(
                self.snapshot.parser,
                [],
                [],
                self.def_mutations
            )
        )
        if new_snapshot is not None:
            return new_snapshot
        return None
      
    # def _get_mutations
        
    def try_remove(
        self,
        to_save_return_idxs:list[int]
    )->bool:
        if self.to_stop_time is not None and time.time() >= self.to_stop_time:
            return False
      
        new_func_ty, new_insts = self._get_new_insts_and_new_func_type(to_save_return_idxs)
        new_insts, new_types = self._get_processed_insts_and_new_types(new_insts, self.raw_types)
        # assert len(new_types) == 0
        if len(new_types) != 0:
            raise Exception(f'new_types: {new_types}, self.raw_types: {self.raw_types}, new_insts: {new_insts}')
        
        new_func = wasmFunc(
            func_ty=new_func_ty,
            insts=new_insts,
            defined_local_types=self.ori_locals
        )
        def_mutations = []
        func_mutation = DefinitionMutation(
            sec_type=SectionType.Code,
            start_offset=self.func_idx,
            end_offset=self.func_idx + 1,
            new_definitions=[new_func]
        )
        def_mutations.append(func_mutation)
        if new_func_ty not in self.snapshot.parser.types:
            type_mutation = DefinitionMutation(
                sec_type=SectionType.Type,
                start_offset=len(self.snapshot.parser.types),
                end_offset=len(self.snapshot.parser.types) + 1,
                new_definitions=[new_func_ty]
            )
            def_mutations.append(type_mutation)
            func_ty_idx = len(self.snapshot.parser.types)
        else:
            func_ty_idx = self.snapshot.parser.types.index(new_func_ty)
            # 
       
        def_mutations.append(DefinitionMutation(
            sec_type=SectionType.Function,
            start_offset=self.func_idx,
            end_offset=self.func_idx + 1,
            new_definitions=[func_ty_idx]
        ))
        new_snapshot = apply_mutation_and_encode_keep_snapshot(
            self.snapshot,
            MutationBatch(
                definition_mutations=def_mutations
            ),
            self.tmp_write_path
        )
        if self.DEBUG:
            is_valid = validate_wasm(self.tmp_write_path, tag=ValidateCheckType.TEST_CHECK)
            if not is_valid:
                info_ = get_validation_info(self.tmp_write_path)
                assert info_ is not None
                raise Exception(f'{self.tmp_write_path} is invalid')

        if self.oracle_func(self.tmp_write_path, CheckType.TEST_CHECK):
            # print(f'self.tmp_write_path: {self.tmp_write_path}')
            # print(f'self.output_path: {self.output_path}')
            
            copy_file(self.tmp_write_path, self.output_path)
            self.def_mutations = def_mutations
            return True
        return False


    def _get_code_snippet(self, expected_type:funcType)->list[Inst]:
        return self.insts_factory.get_code_snippet(expected_type)
    
    def _update_insts(self, ori_list, elem:Union[Inst, ASTINode]):
        if isinstance(elem, Inst):
            ori_list.append(elem)
        else:
            ori_list.extend(elem.get_insts())
            
    def _get_new_insts_and_new_func_type(self, to_save_return_idxs):
        new_insts = []
        cur_stack_state_req = self.stack_init_state.as_type_req()
        cur_stack_state = self.stack_init_state
        for node_idx in sorted(to_save_return_idxs+self.must_save_ones):
            expect_result_type = self.node_param_types[node_idx]
            # 
            exp_len = len(expect_result_type.params)
            rest_type_length = len(cur_stack_state.rest_types)
            if rest_type_length >= exp_len and cur_stack_state.rest_types[rest_type_length-exp_len:] == expect_result_type.params:
                pass
            elif rest_type_length >= exp_len and expect_result_type.drop:
                pass
            else:
            # 
                expected_padding_type = funcTypeFactory.generate_one_func_type_default(
                    param_type=[],
                    result_type=expect_result_type.params
                )
                new_padding = self._get_code_snippet(expected_padding_type)
                new_insts.extend(new_padding)
                # 
                new_cur_stack_state_req = merge_req(
                    cur_stack_state_req, 
                    typeReq(
                        tys=[expected_padding_type],
                        req_type='eq'
                    )
                )
                cur_stack_state_req = new_cur_stack_state_req
                assert len(new_cur_stack_state_req.tys) > 0
                # 
            self._update_insts(new_insts, self.elems[node_idx])
           
            
            
            new_cur_stack_state_req = merge_req(
                cur_stack_state_req,
                self.elem_reqs[node_idx]
            )
            assert len(new_cur_stack_state_req.tys) > 0
            cur_stack_state_req = new_cur_stack_state_req
            cur_stack_state = get_stack_state_from_type_req(cur_stack_state_req)

        new_func_ty = funcTypeFactory.generate_one_func_type_default(
            param_type=[],
            result_type=cur_stack_state.rest_types
        )
        
        return new_func_ty,new_insts

    def _get_processed_insts_and_new_types(
        self,
        insts:list[Inst], 
        types:list[funcType]
    ):
        new_insts = []
        new_types = []
        for inst in insts:
            op = inst.opcode_text
            ori_types_num = len(types)
            
            if op == 'block' \
                or op == 'loop' \
                or op == 'if':
                imm = inst.imm_part.val
                assert isinstance(imm, Blocktype)
                if isinstance(imm.init_data, str) or isinstance(imm.init_data, bool):
                    continue
                actual_func = imm.concrete_type(types)
                short_encoding = _get_short_encoding_functype_as_blocktype_param(actual_func)
                if short_encoding is not None:
                    inst = InstFactory.gen_binary_info_inst_high_single_imm(
                        op,
                        Blocktype(short_encoding)
                    )
                else:
                    
                    # print(f"actual_func: {actual_func}")
                    # print(f'block type: {imm}')
                    # print(f'block type init_data: {imm.init_data}')
                    # print(f'types: {types}')
                    if actual_func in types:
                        new_idx = types.index(actual_func)
                    elif actual_func in new_types:
                        new_idx = new_types.index(actual_func) + ori_types_num
                    else:
                        new_types.append(actual_func)
                        new_idx = len(new_types)-1  + ori_types_num
                    
                    inst = InstFactory.gen_binary_info_inst_high_single_imm(
                        op,
                        Blocktype(new_idx)
                    )
            new_insts.append(inst)
        return new_insts, new_types

def _get_short_encoding_functype_as_blocktype_param(
    func_type:funcType
)->Union[None, str, bool]:
    if len(func_type.param_types) != 0:
        return None
    if len(func_type.result_types) == 0:
        return True
    if len(func_type.result_types) == 1:
        return func_type.result_types[0]
    return None

# rewrite locals ========================================================================================
def remove_useless_locals(
    parser:WasmParser
)->dict[int, tuple[list[FuncInstMutation], Optional[RewriteLocalDesc]]]:
    # inst_ms = []
    # local_mutations = []
    result = {}
    for func_idx, func in enumerate(parser.defined_funcs):
        ims, wlm = _gen_reduce_local_mutations_for_one_func(func, func_idx)
        # inst_ms.extend(ims)
        # if wlm is not None:
        #     local_mutations.append(wlm)
        result[func_idx] = (ims, wlm)
    return result


def _gen_reduce_local_mutations_for_one_func(
    func:wasmFunc,
    func_idx:int
)->tuple[list[FuncInstMutation], Optional[RewriteLocalDesc]]:
    raw_locals = func.local_types
    param_num = len(func.param_types)
    raw_defined_locals = func.defined_local_types
    raw_defined_local_num = len(raw_defined_locals)
    used_locals: dict[int,str] = {}
    use_local_insts: dict[int, tuple[str, int]] = {}
    inst_ops = {'local.get', 'local.set', 'local.tee'}
    for inst_idx, inst in enumerate(func.insts):
        op = inst.opcode_text
        if op in inst_ops:
            imm = inst.imm_part.val
            if imm < param_num:
                continue
            use_local_insts[inst_idx] = (op, imm)
            ty = raw_locals[imm]
            used_locals[imm] = ty
    if len(used_locals) == raw_defined_local_num:
        return [], None
    idx_and_ty = list(used_locals.items())
    idx_and_ty.sort(key=lambda x: x[0])
    new_locals = []
    ori_local_idx2new:dict[int, int] = {}
    for idx, ty in idx_and_ty:
        new_locals.append(ty)
        ori_local_idx2new[idx] = len(new_locals) - 1 + param_num
    wlm = RewriteLocalDesc(
        func_idx=func_idx,
        new_defined_locals=new_locals
    )
    ims = []
    for inst_idx, (op, imm) in use_local_insts.items():
        new_imm = ori_local_idx2new[imm]
        ims.append(FuncInstMutation(
            func_idx=func_idx,
            start_offset=inst_idx,
            end_offset=inst_idx + 1,
            new_insts=[InstFactory.gen_binary_info_inst_high_single_imm(op, new_imm)]
        ))
    return ims, wlm
