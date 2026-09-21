import os
from pathlib import Path
import time
from typing import Callable, Optional, Union
from util.debug_util import ValidateCheckType, get_validation_info, validate_wasm
from extract_block_mutator.DefShell import gen_elem_seg2, gen_elem_seg6, gen_export_desc, gen_export_global_idx_part, gen_export_tableidx_part, gen_func_import_attr, gen_import_desc
from extract_block_mutator.WasmParser import WasmParser, get_parser_from_wasm_path
from extract_block_mutator.encode.NGDataPayload import DataPayloadwithName
from extract_block_mutator.encode.new_defined_data_type import Blocktype
from extract_block_mutator.parser_to_file_util import parser2wasm
from file_util import copy_file
from WasmInfoCfg import DataSegAttr, ElemSecAttr, ExportType, ImportType, SectionType, globalValMut
from extract_block_mutator.InstGeneration.InstFactory import InstFactory
from extract_block_mutator.InstUtil.Inst import Inst
from extract_block_mutator.get_data_shell import get_data_attr, get_elemseg_attr, get_export_attr, get_global_attr, get_impotr_attr, has_func_idx
from reduction_analysis.ReduceUtil.RemoveDeadFunc import remove_dead_funcs
from reduction_analysis.ReductionDescUtil.OneReducerDirSystem import OneReducerDirSystem
from reduction_analysis.ReductionDescUtil.Oracle import CheckType
from ...ParserModification import Encoder, WMSnapshot, apply_mutation_and_encode_keep_snapshot
from ...ParserModificationUtil import MutationBatch
from reduction_analysis.ProbDDUtil.ProbDDFactory import ProbDDFactory
from reduction_analysis.ProbDDUtil.adapt_util import get_test_cfg_func_for_dd
from reduction_analysis.ReducerPassUtil.ReduceResult import ExecResult, ExecStatus, ReduceResult
from reduction_analysis.ReducerPassUtil.ZReducerPass import ZReducerPass
from reduction_analysis.ParserModification import FuncInstMutation, DefinitionMutation
from .UnusedDefReducerUtil import detect_used_unused_type_idxs, update_parser_after_remove_type


def update_parser_after_remove_global(
    parser: WasmParser,
    to_remove_idxs: set[int]
) -> tuple[list[FuncInstMutation], list[DefinitionMutation]]:
    func_inst_mutations = []
    section_mutations = []

    if len(to_remove_idxs) == 0:
        return func_inst_mutations, section_mutations

    ori_idx2_new_idx = {}
    import_global_num = parser.import_global_num
    for idx in range(import_global_num):
        ori_idx2_new_idx[idx] = idx
    skip_num = 0
    for idx in range(import_global_num, import_global_num + len(parser.defined_globals)):
        if idx - import_global_num in to_remove_idxs:
            skip_num += 1
        else:
            ori_idx2_new_idx[idx] = idx - skip_num

    for func_idx, defined_func in enumerate(parser.defined_funcs):
        for inst_idx, inst in enumerate(defined_func.insts):
            op = inst.opcode_text
            if op == 'global.get' or op == 'global.set':
                old_global_idx = inst.imm_part.val
                new_global_idx = ori_idx2_new_idx[old_global_idx]
                if new_global_idx != old_global_idx:
                    new_inst = _update_single_imm_inst(ori_idx2_new_idx, inst)
                    mutation = FuncInstMutation(
                        func_idx=func_idx,
                        start_offset=inst_idx,
                        end_offset=inst_idx + 1,
                        new_insts=[new_inst]
                    )
                    func_inst_mutations.append(mutation)

    for export_idx, export_ in enumerate(parser.exports):
        attr = get_export_attr(export_, 'attr')
        if attr == ExportType.global_:
            ori_idx = get_export_attr(export_, 'idx')
            assert isinstance(ori_idx, int)
            if ori_idx in to_remove_idxs:
                section_mutations.append(DefinitionMutation(
                    sec_type=SectionType.Export,
                    start_offset=export_idx,
                    end_offset=export_idx + 1,
                    new_definitions=[]
                ))
            elif ori_idx in ori_idx2_new_idx:
                expected_new_func_idx = ori_idx2_new_idx[ori_idx]
                if expected_new_func_idx != ori_idx:
                    export_func_name = get_export_attr(export_, 'name')
                    new_idx_part = gen_export_global_idx_part(
                        global_idx=expected_new_func_idx)
                    new_export = gen_export_desc(
                        name=export_func_name, desc=new_idx_part)
                    section_mutations.append(DefinitionMutation(
                        sec_type=SectionType.Export,
                        start_offset=export_idx,
                        end_offset=export_idx + 1,
                        new_definitions=[new_export]
                    ))

    for remove_idx in to_remove_idxs:
        section_mutations.append(DefinitionMutation(
            sec_type=SectionType.Global,
            start_offset=remove_idx,
            end_offset=remove_idx + 1,
            new_definitions=[]
        ))

    return func_inst_mutations, section_mutations


def _update_single_imm_inst(
    ori_idx2_new_idx: dict[int, int],
    inst: Inst
):
    op = inst.opcode_text
    idx = inst.imm_part.val
    new_inst = InstFactory.gen_binary_info_inst_high_single_imm(
        op, imm0=ori_idx2_new_idx[idx])
    return new_inst


def update_parser_after_remove_memory(
    parser: WasmParser,
    to_remove_idxs: set[int]
) -> tuple[list[FuncInstMutation], list[DefinitionMutation]]:
    func_inst_mutations = []
    section_mutations = []
    for remove_idx in to_remove_idxs:
        section_mutations.append(DefinitionMutation(
            sec_type=SectionType.Memory,
            start_offset=remove_idx,
            end_offset=remove_idx + 1,
            new_definitions=[]
        ))
    return func_inst_mutations, section_mutations


def update_parser_after_remove_data(
    parser: WasmParser,
    to_remove_idxs: set[int]
) -> tuple[list[FuncInstMutation], list[DefinitionMutation]]:
    func_inst_mutations = []
    section_mutations = []

    if len(to_remove_idxs) == 0:
        return func_inst_mutations, section_mutations

    ori_idx2_new_idx = {}
    skip_num = 0
    for idx in range(len(parser.data_sec_datas)):
        if idx in to_remove_idxs:
            skip_num += 1
        else:
            ori_idx2_new_idx[idx] = idx - skip_num

    for func_idx, defined_func in enumerate(parser.defined_funcs):
        for inst_idx, inst in enumerate(defined_func.insts):
            op = inst.opcode_text
            if op == 'data.drop' or op == 'memory.init':
                old_data_idx = inst.imm_part.val
                new_data_idx = ori_idx2_new_idx[old_data_idx]
                if new_data_idx != old_data_idx:
                    new_inst = _update_single_imm_inst(ori_idx2_new_idx, inst)
                    func_inst_mutations.append(FuncInstMutation(
                        func_idx=func_idx,
                        start_offset=inst_idx,
                        end_offset=inst_idx + 1,
                        new_insts=[new_inst]
                    ))

    for remove_idx in to_remove_idxs:
        section_mutations.append(DefinitionMutation(
            sec_type=SectionType.Data,
            start_offset=remove_idx,
            end_offset=remove_idx + 1,
            new_definitions=[]
        ))

    return func_inst_mutations, section_mutations


def update_parser_after_remove_elem(
    parser: WasmParser,
    to_remove_idxs: set[int]
) -> tuple[list[FuncInstMutation], list[DefinitionMutation]]:
    func_inst_mutations = []
    section_mutations = []

    if len(to_remove_idxs) == 0:
        return func_inst_mutations, section_mutations

    ori_idx2_new_idx = {}
    skip_num = 0
    for idx in range(len(parser.elem_sec_datas)):
        if idx in to_remove_idxs:
            skip_num += 1
        else:
            ori_idx2_new_idx[idx] = idx - skip_num

    for func_idx, defined_func in enumerate(parser.defined_funcs):
        for inst_idx, inst in enumerate(defined_func.insts):
            op = inst.opcode_text
            if op == 'elem.drop':
                old_elem_idx = inst.imm_part.val
                new_elem_idx = ori_idx2_new_idx[old_elem_idx]
                if new_elem_idx != old_elem_idx:
                    new_inst = _update_single_imm_inst(ori_idx2_new_idx, inst)
                    func_inst_mutations.append(FuncInstMutation(
                        func_idx=func_idx,
                        start_offset=inst_idx,
                        end_offset=inst_idx + 1,
                        new_insts=[new_inst]
                    ))
            elif op == 'table.init':
                old_elem_idx = inst.imm_part.y
                new_elem_idx = ori_idx2_new_idx[old_elem_idx]
                if new_elem_idx != old_elem_idx:
                    new_inst = InstFactory.gen_binary_info_inst_high(
                        op='table.init',
                        imm_dict={
                            'x': inst.imm_part.x,
                            'y': new_elem_idx
                        }
                    )
                    func_inst_mutations.append(FuncInstMutation(
                        func_idx=func_idx,
                        start_offset=inst_idx,
                        end_offset=inst_idx + 1,
                        new_insts=[new_inst]
                    ))

    for remove_idx in to_remove_idxs:
        section_mutations.append(DefinitionMutation(
            sec_type=SectionType.Element,
            start_offset=remove_idx,
            end_offset=remove_idx + 1,
            new_definitions=[]
        ))

    return func_inst_mutations, section_mutations


def update_parser_after_remove_export(
    parser: WasmParser,
    to_remove_idxs: set[int]
) -> tuple[list[FuncInstMutation], list[DefinitionMutation]]:
    func_inst_mutations = []
    section_mutations = []

    if len(to_remove_idxs) == 0:
        return func_inst_mutations, section_mutations

    for remove_idx in to_remove_idxs:
        section_mutations.append(DefinitionMutation(
            sec_type=SectionType.Export,
            start_offset=remove_idx,
            end_offset=remove_idx + 1,
            new_definitions=[]
        ))

    return func_inst_mutations, section_mutations




def update_parser_after_remove_table(
    parser: WasmParser,
    to_remove_idxs: set[int]
) -> tuple[list[FuncInstMutation], list[DefinitionMutation]]:

    func_inst_mutations = []
    section_mutations = []

    if len(to_remove_idxs) == 0:
        return func_inst_mutations, section_mutations

    ori_idx2_new_idx = {}
    import_table_num = parser.import_table_num
    for idx in range(import_table_num):
        ori_idx2_new_idx[idx] = idx
    skip_num = 0
    for idx in range(import_table_num, import_table_num + parser.defined_table_num):
        if idx - import_table_num in to_remove_idxs:
            skip_num += 1
        else:
            ori_idx2_new_idx[idx] = idx - skip_num

    for func_idx, defined_func in enumerate(parser.defined_funcs):
        for inst_idx, inst in enumerate(defined_func.insts):
            op = inst.opcode_text
            if op == 'table.set' \
                    or op == 'table.size' \
                    or op == 'table.grow' \
                    or op == 'table.fill' \
                    or op == 'table.get':
                idx = inst.imm_part.val
                new_idx = ori_idx2_new_idx[idx]
                if idx != new_idx:
                    new_inst = _update_single_imm_inst(ori_idx2_new_idx, inst)
                    func_inst_mutations.append(FuncInstMutation(
                        func_idx=func_idx,
                        start_offset=inst_idx,
                        end_offset=inst_idx + 1,
                        new_insts=[new_inst]
                    ))

            elif op == 'table.init' \
                    or op == 'call_indirect':
                new_idx = ori_idx2_new_idx[inst.imm_part.x]
                if new_idx != inst.imm_part.x:

                    new_inst = InstFactory.gen_binary_info_inst_high(
                        op=op,
                        imm_dict={
                            'x': new_idx,
                            'y': inst.imm_part.y
                        })
                    func_inst_mutations.append(FuncInstMutation(
                        func_idx=func_idx,
                        start_offset=inst_idx,
                        end_offset=inst_idx + 1,
                        new_insts=[new_inst]
                    ))
            elif op == 'table.copy':
                ori_x = inst.imm_part.x
                ori_y = inst.imm_part.y
                new_x = ori_idx2_new_idx[ori_x]
                new_y = ori_idx2_new_idx[ori_y]
                if new_x != ori_x or new_y != ori_y:
                    new_inst = InstFactory.gen_binary_info_inst_high(
                        op=op,
                        imm_dict={
                            'x': new_x,
                            'y': new_y
                        })
                    func_inst_mutations.append(FuncInstMutation(
                        func_idx=func_idx,
                        start_offset=inst_idx,
                        end_offset=inst_idx + 1,
                        new_insts=[new_inst]
                    ))
    for export_idx, export_ in enumerate(parser.exports):
        attr = get_export_attr(export_, 'attr')
        if attr == ExportType.table:
            ori_idx = get_export_attr(export_, 'idx')
            assert isinstance(ori_idx, int)
            if ori_idx in to_remove_idxs:
                section_mutations.append(DefinitionMutation(
                    sec_type=SectionType.Export,
                    start_offset=export_idx,
                    end_offset=export_idx + 1,
                    new_definitions=[]
                ))
            elif ori_idx in ori_idx2_new_idx:
                expected_new_func_idx = ori_idx2_new_idx[ori_idx]
                if expected_new_func_idx != ori_idx:
                    export_func_name = get_export_attr(export_, 'name')
                    new_idx_part = gen_export_tableidx_part(
                        table_idx=expected_new_func_idx)
                    new_export = gen_export_desc(
                        name=export_func_name, desc=new_idx_part)
                    section_mutations.append(DefinitionMutation(
                        sec_type=SectionType.Export,
                        start_offset=export_idx,
                        end_offset=export_idx + 1,
                        new_definitions=[new_export]
                    ))
    # process elem section
    for elem_idx, elem_sec_data in enumerate(parser.elem_sec_datas):
        attr = get_elemseg_attr(elem_sec_data, 'attr')
        if attr == ElemSecAttr.active:
            name = elem_sec_data.inner_name.name
            if name == 'active_elem_seg1':
                ori_data = elem_sec_data.data
                ori_idx = ori_data['table_idx']
  
                new_idx = ori_idx2_new_idx[ori_idx]
                if new_idx != ori_idx:
                    
                    # new_data['table_idx'] = new_idx
                    new_data = ori_data.copy()
                    new_data['table_idx'] = new_idx
                    new_elem_sec_data = DataPayloadwithName(
                        new_data,
                        name
                    )
                    section_mutations.append(DefinitionMutation(
                        sec_type=SectionType.Element,
                        start_offset=elem_idx,
                        end_offset=elem_idx + 1,
                        new_definitions=[new_elem_sec_data]
                    ))
            elif name == 'active_elem_seg3':
                ori_data = elem_sec_data.data
                ori_idx = ori_data['tableidx']
  
                new_idx = ori_idx2_new_idx[ori_idx]
                if new_idx != ori_idx:
                    new_data = ori_data.copy()
                    new_data['tableidx'] = new_idx
                    new_elem_sec_data = DataPayloadwithName(
                        new_data,
                        name
                    )
                    section_mutations.append(DefinitionMutation(
                        sec_type=SectionType.Element,
                        start_offset=elem_idx,
                        end_offset=elem_idx + 1,
                        new_definitions=[new_elem_sec_data]
                    ))

    for remove_idx in to_remove_idxs:
        section_mutations.append(DefinitionMutation(
            sec_type=SectionType.Table,
            start_offset=remove_idx,
            end_offset=remove_idx + 1,
            new_definitions=[]
        ))

    return func_inst_mutations, section_mutations

def detect_used_unused_defined_import_func_idxs(  # return the idx of import
    parser: WasmParser
):
    # 
    ifunc_idx2import_idx:dict[int, int] = {}
    func_idx = 0
    for import_idx, import_desc in enumerate(parser.imports):
        import_attr = get_impotr_attr(import_desc, 'type')
        if import_attr == ImportType.func:
            ifunc_idx2import_idx[func_idx] = import_idx
            func_idx += 1
    # 
    import_func_num = func_idx
    if import_func_num == 0:
        return set(), set()
    candis = set(range(import_func_num))
    #
    used_idxs = _get_raw_used_idx_core(parser)
    used_idxs = used_idxs.intersection(candis)
    unused_idxs = candis - used_idxs
    unused_idxs = set(ifunc_idx2import_idx[idx] for idx in unused_idxs)
    used_idxs = set(ifunc_idx2import_idx[idx] for idx in used_idxs)
    return used_idxs, unused_idxs

def detect_used_unused_defined_global_idxs(
    parser: WasmParser
):
    defined_global_num = parser.defined_global_num
    import_global_num = parser.import_global_num
    candis = set(range(defined_global_num+import_global_num))
    #
    used_idxs = set()
    for func_idx, defined_func in enumerate(parser.defined_funcs):
        for inst_idx, inst in enumerate(defined_func.insts):
            op = inst.opcode_text
            if op == 'global.get' or op == 'global.set':
                idx = inst.imm_part.val
                used_idxs.add(idx)
    used_idxs.update(_get_global_idxs_in_export_seg(parser))
    used_idxs = set(
        idx - import_global_num for idx in used_idxs if idx >= import_global_num)
    return used_idxs, candis - used_idxs


def detect_used_unused_defined_memory_idxs(
    parser: WasmParser
):
    # defined_memory_num = parser.defined_memory_num
    # import_global_num = parser.import_global_num
    import_memory_num = parser.import_memory_num
    candis = set(range(parser.defined_memory_num))
    #
    used_idxs = set()
    for mem_idx, defined_func in enumerate(parser.defined_funcs):
        for inst_idx, inst in enumerate(defined_func.insts):
            op = inst.opcode_text
            if ('store' in op) or ('load' in op):
                used_idxs.add(import_memory_num)
    import_memory_num = parser.import_memory_num
    for data_seg in parser.data_sec_datas:
        name = data_seg.inner_name.name
        # print(name)
        # assert 0
        if name == 'active_memory_zero':
            used_idxs.add(import_memory_num)
        elif name == 'active_memory_index':
            # assert 0
            mem_idx = data_seg.data['memory_index']
            used_idxs.add(mem_idx)

    for export_ in parser.exports:
        attr = get_export_attr(export_, 'attr')
        if attr == ExportType.mem:
            mem_idx = get_export_attr(export_, 'idx')
            assert isinstance(mem_idx, int)
            used_idxs.add(mem_idx)
            # continue
        # attr = get_data_attr(
        #     data_seg,
        #     'attr'
        # )
        # if attr == DataSegAttr.active:
        #     assert 0
        #     used_idxs.add(0)
    used_idxs = set(
        idx - import_memory_num for idx in used_idxs if idx >= import_memory_num)
    return used_idxs, candis - used_idxs


def detect_used_unused_elemseg_idxs(
    parser: WasmParser
):
    candis = set(range(len(parser.elem_sec_datas)))
    used_idxs = set()
    for func_idx, defined_func in enumerate(parser.defined_funcs):
        for inst_idx, inst in enumerate(defined_func.insts):
            op = inst.opcode_text
            if op == 'elem.drop':
                idx = inst.imm_part.val
                used_idxs.add(idx)
            elif op == 'table.init':
                idx = inst.imm_part.y
                used_idxs.add(idx)
                # table.init
    return used_idxs, candis - used_idxs


def detect_used_unused_table_idxs(
    parser: WasmParser
):
    candis = set(range(parser.defined_table_num))
    used_idxs = set()
    for func_idx, defined_func in enumerate(parser.defined_funcs):
        for inst_idx, inst in enumerate(defined_func.insts):
            op = inst.opcode_text
            if op == 'table.set' \
                    or op == 'table.size' \
                    or op == 'table.grow' \
                    or op == 'table.fill' \
                    or op == 'table.get':
                idx = inst.imm_part.val
                used_idxs.add(idx)
            elif op == 'table.init' \
                    or op == 'call_indirect':
                used_idxs.add(inst.imm_part.x)
            elif op == 'table.copy':
                used_idxs.add(inst.imm_part.x)
                used_idxs.add(inst.imm_part.y)
                # table.init
    used_idxs.update(_get_table_idxs_in_export_seg(parser))
    for elem_seg in parser.elem_sec_datas:
        attr = get_elemseg_attr(elem_seg, 'attr')
        if attr != ElemSecAttr.active:
            continue
        name = elem_seg.inner_name.name
        data = elem_seg.data
        if name == 'active_elem_seg0' or name == 'active_elem_seg2':
            used_idxs.add(0)
        elif name == 'active_elem_seg1':
            used_idxs.add(data['table_idx'])
        elif name == 'active_elem_seg3':
            used_idxs.add(data['tableidx'])
    import_table_num = parser.import_table_num
    used_idxs = set(
        idx - import_table_num for idx in used_idxs if idx >= import_table_num)
    return used_idxs, candis - used_idxs


def detect_used_unused_data_idxs(
    parser: WasmParser
):
    candis = set(range(len(parser.data_sec_datas)))
    used_idxs = set()
    for func_idx, defined_func in enumerate(parser.defined_funcs):
        for inst_idx, inst in enumerate(defined_func.insts):
            op = inst.opcode_text
            if op == 'data.drop' or op == 'memory.init':
                idx = inst.imm_part.val
                used_idxs.add(idx)
    return used_idxs, candis - used_idxs


# memory ?

# table ?

# function !
def detect_used_unused_defined_func_idxs(
    parser: WasmParser
):
    import_func_num = parser.import_func_num
    # total = parser.func_num
    candis = set(range(len(parser.defined_funcs)))

    used_idxs = _get_raw_used_idx_core(parser)
    used_idxs = set(
        idx - import_func_num for idx in used_idxs if idx >= import_func_num)
    return used_idxs, candis - used_idxs

def _get_raw_used_idx_core(parser):
    used_idxs = set()
    for func_idx, defined_func in enumerate(parser.defined_funcs):
        for inst_idx, inst in enumerate(defined_func.insts):
            op = inst.opcode_text
            if op == 'call' or op == 'ref.func':
                idx = inst.imm_part.val
                used_idxs.add(idx)
    for seg in parser.elem_sec_datas:
        used_idxs.update(_get_func_idxs_in_elem_seg(seg))
    used_idxs.update(_get_func_idxs_in_export_seg(parser))
    return used_idxs

def _get_func_idxs_in_elem_seg(
    seg
):
    seg_has_func_idx = has_func_idx(seg)
    if not seg_has_func_idx:
        return set()
    inner_name = seg.inner_name
    inner_name_str = inner_name.name
    # for func_idx in seg.data['func_idxs']:
    #     func_idxs.add(func_idx)
    func_idxs = set()
    if inner_name_str == 'passive_elem_seg0' \
            or inner_name_str == 'active_elem_seg0' \
            or inner_name_str == 'active_elem_seg1' \
            or inner_name_str == 'declarative_elem_seg0':
        func_idxs = set(seg.data['funcidxs'])
    else:
        ref_type = get_elemseg_attr(seg, 'ref_type')
        if ref_type == 'externref':
            pass
        for inst_idx, inst_expr in enumerate(seg.data['exprs']):
            inst = inst_expr.val
            assert isinstance(inst, Inst)
            opcode = inst.opcode_text
            if opcode == 'ref.func':
                func_idx = inst.imm_part.val
                func_idxs.add(func_idx)
    return func_idxs


def _get_func_idxs_in_export_seg(parser: WasmParser):
    idxs = set()
    for idx, export_ in enumerate(parser.exports):
        attr = get_export_attr(export_, 'attr')
        if attr == ExportType.func:
            func_idx = get_export_attr(export_, 'idx')
            assert isinstance(func_idx, int)
            idxs.add(func_idx)
    return idxs


def _get_global_idxs_in_export_seg(parser: WasmParser):
    idxs = set()
    for idx, export_ in enumerate(parser.exports):
        attr = get_export_attr(export_, 'attr')
        if attr == ExportType.global_:
            func_idx = get_export_attr(export_, 'idx')
            assert isinstance(func_idx, int)
            idxs.add(func_idx)
    return idxs


def _get_table_idxs_in_export_seg(parser: WasmParser):
    idxs = set()
    for idx, export_ in enumerate(parser.exports):
        attr = get_export_attr(export_, 'attr')
        if attr == ExportType.table:
            idx = get_export_attr(export_, 'idx')
            assert isinstance(idx, int)
            idxs.add(idx)
    return idxs


class _DefDesc:
    def __init__(
        self,
        section_type: SectionType,
        idx: int
    ):
        self.section_type = section_type
        self.idx = idx

    def __str__(self):
        return(f'{self.section_type.name}({self.idx})')
        # return f'{self.__class__.__name__}({self.section_type},{self.idx})'

    def __repr__(self):
        return f'{self.__class__.__name__}({self.section_type},{self.idx})'

    def __eq__(self, other):
        if not isinstance(other, _DefDesc):
            return False
        return self.section_type == other.section_type and self.idx == other.idx

    def __hash__(self):
        return hash((self.section_type, self.idx))


class _DefDescs:
    def __init__(
        self,
        descs: list[_DefDesc]
    ):
        self.descs = descs

    def as_dict(self):
        d = {}
        for desc in self.descs:
            d.setdefault(desc.section_type, []).append(desc.idx)
        return d


class UnusedDefReducer(ZReducerPass):
    def __init__(
        self,
        dir_system: OneReducerDirSystem,
        oracle_func:Callable,
        DEBUG: bool = False,
        name: str = 'UnusedDefReducer',
        # tmp_
    ):
        self.to_stop_time = None
        super().__init__(
            dir_system=dir_system,
            oracle_func=oracle_func,
            name=name,
            DEBUG=DEBUG
        )

    def reduce(self,
               cur_input_path: str,
               cur_output_path: str,
               timeout: Optional[float] = None
               ) -> ExecResult:
        #
        self.cur_input_path = cur_input_path
        self.output_path = cur_output_path
        total_removed_num = 0
        # ori_size
        cannot_remove_num: Optional[int] = None
        start_time = time.time()

        if timeout is not None:
            self.to_stop_time = start_time + timeout
        while True:
            self.snapshot = WMSnapshot.from_path(self.cur_input_path)
            used_func_idxs, unused_func_idxs = detect_used_unused_defined_func_idxs(
                self.snapshot.parser)
            used_data_idxs, unused_data_idxs = detect_used_unused_data_idxs(
                self.snapshot.parser)
            used_elemseg_idxs, unused_elemseg_idxs = detect_used_unused_elemseg_idxs(
                self.snapshot.parser)
            used_global_idxs, unused_global_idxs = detect_used_unused_defined_global_idxs(
                self.snapshot.parser)
            used_type_idxs, unused_type_idxs = detect_used_unused_type_idxs(
                self.snapshot.parser)
            used_memory_idxs, unused_memory_idxs = detect_used_unused_defined_memory_idxs(
                self.snapshot.parser
            )
            used_table_idxs, unused_table_idxs = detect_used_unused_table_idxs(
                self.snapshot.parser
            )
            used_import_func_idxs, unused_import_func_idxs = detect_used_unused_defined_import_func_idxs(
                self.snapshot.parser
            )
            # print('unused_type_idxs', unused_type_idxs)
            # unsed_exports = detect_used_unused_defined_export_idxs(ori_parser, self.to_test_func)
            if self.DEBUG:
                assert len(self.snapshot.parser.defined_funcs) == len(
                    used_func_idxs) + len(unused_func_idxs), print(
                        f'used_func_idxs: {used_func_idxs}, unused_func_idxs: {unused_func_idxs}',
                        f'len(self.ml.parser.defined_funcs): {len(self.snapshot.parser.defined_funcs)}',
                        f'len(used_func_idxs) : {len(used_func_idxs) }',
                        f'len(unused_func_idxs) : {len(unused_func_idxs)}',
                )
                assert len(self.snapshot.parser.data_sec_datas) == len(
                    used_data_idxs) + len(unused_data_idxs)
                assert len(self.snapshot.parser.elem_sec_datas) == len(
                    used_elemseg_idxs) + len(unused_elemseg_idxs)
                assert len(self.snapshot.parser.defined_globals) == len(
                    used_global_idxs) + len(unused_global_idxs)
                assert len(self.snapshot.parser.types) == len(
                    used_type_idxs) + len(unused_type_idxs)
            #
            to_remove_descs = []
            print('unused_func_idxs', unused_func_idxs)
            for idx in unused_type_idxs:
                to_remove_descs.append(_DefDesc(SectionType.Type, idx))
            for idx in unused_func_idxs:
                to_remove_descs.append(_DefDesc(SectionType.Function, idx))
            for idx in unused_data_idxs:
                to_remove_descs.append(_DefDesc(SectionType.Data, idx))
            for idx in unused_elemseg_idxs:
                to_remove_descs.append(_DefDesc(SectionType.Element, idx))
            for idx in unused_global_idxs:
                to_remove_descs.append(_DefDesc(SectionType.Global, idx))
            for idx in unused_memory_idxs:
                to_remove_descs.append(_DefDesc(SectionType.Memory, idx))
            for idx in unused_table_idxs:
                to_remove_descs.append(_DefDesc(SectionType.Table, idx))
            if self.snapshot.parser.start_sec_data is not None:
                to_remove_descs.append(_DefDesc(SectionType.Start, -1))
            for idx in range(len(self.snapshot.parser.exports)):
                to_remove_descs.append(_DefDesc(SectionType.Export, idx))
            for idx in unused_import_func_idxs:
                to_remove_descs.append(_DefDesc(SectionType.Import, idx))
            if len(to_remove_descs) == 0 or len(to_remove_descs) == cannot_remove_num:
                break
            # TODO import
            self.to_remove_descs = to_remove_descs
            self.all_to_remove_descs = set(to_remove_descs)
            #
            test_config = get_test_cfg_func_for_dd(self.try_remove)
            dd = ProbDDFactory.get_default_probdd(test_config, task_id='UUR')
            minimal_config = dd(self.to_remove_descs)
            cannot_remove_num = len(minimal_config)
            removed_num = len(self.to_remove_descs) - cannot_remove_num
         
            if removed_num == 0:
                break
            total_removed_num += removed_num
            self.cur_input_path = self.output_path
            # assert 0
        if self.DEBUG:
            print(f'total_removed_num: {total_removed_num}')
            print(f'cannot_remove_num: {cannot_remove_num}')
        result = total_removed_num > 0
        if not result:
            copy_file(cur_input_path, self.output_path)
        es = ExecStatus.SUCCESS if result else ExecStatus.EXEC_FAILED
        to_stop_time = getattr(self, 'to_stop_time', None)
        exec_result = ExecResult(
            exec_status=es,
            exec_taken_time=time.time() - start_time,
            reduced_size_num=total_removed_num,
            reduced_inst_num=0,
            is_partial_by_timeout=(to_stop_time is not None and time.time() >= to_stop_time)
        )
        return exec_result

    def try_remove(
        self,
        to_save_descs: list[_DefDesc],
    ):
        if self.to_stop_time is not None and time.time() > self.to_stop_time:
            return False
        to_delete_descs = list(self.all_to_remove_descs - set(to_save_descs))
        ml = self.snapshot.copy()

        to_delete = _DefDescs(to_delete_descs).as_dict()
        #
        full_func_inst_mutations = []
        full_section_mutations = []
        parser = ml.parser

        new_func_inst_mutations, new_section_mutations = remove_dead_funcs(
            parser,
            self.DEBUG,
            set(to_delete.get(SectionType.Function, [])),
            set(to_delete.get(SectionType.Import, []))
        )

        full_func_inst_mutations.extend(new_func_inst_mutations)
        full_section_mutations.extend(new_section_mutations)
        #
        new_func_inst_mutations, new_section_mutations = update_parser_after_remove_global(
            parser, set(to_delete.get(SectionType.Global, [])))
        full_func_inst_mutations.extend(new_func_inst_mutations)
        full_section_mutations.extend(new_section_mutations)

        new_func_inst_mutations, new_section_mutations = update_parser_after_remove_data(
            parser,
            set(to_delete.get(SectionType.Data, []))
        )
        full_func_inst_mutations.extend(new_func_inst_mutations)
        full_section_mutations.extend(new_section_mutations)
        new_func_inst_mutations, new_section_mutations = update_parser_after_remove_memory(
            parser,
            set(to_delete.get(SectionType.Memory, []))
        )
        full_func_inst_mutations.extend(new_func_inst_mutations)
        full_section_mutations.extend(new_section_mutations)
        new_func_inst_mutations, new_section_mutations = update_parser_after_remove_elem(
            parser,
            set(to_delete.get(SectionType.Element, []))
        )
        full_func_inst_mutations.extend(new_func_inst_mutations)
        full_section_mutations.extend(new_section_mutations)
        if SectionType.Start in to_delete:
            new_func_inst_mutations, new_section_mutations = update_parser_after_remove_start()
            full_func_inst_mutations.extend(new_func_inst_mutations)
            full_section_mutations.extend(new_section_mutations)

        new_func_inst_mutations, new_section_mutations = update_parser_after_remove_export(
            parser, set(to_delete.get(SectionType.Export, [])))

        full_func_inst_mutations.extend(new_func_inst_mutations)
        full_section_mutations.extend(new_section_mutations)
        new_func_inst_mutations, new_section_mutations = update_parser_after_remove_type(
            parser, set(to_delete.get(SectionType.Type, [])))
        full_func_inst_mutations.extend(new_func_inst_mutations)
        full_section_mutations.extend(new_section_mutations)
        #
        new_func_inst_mutations, new_section_mutations = update_parser_after_remove_table(
            parser, set(to_delete.get(SectionType.Table, [])))
        full_func_inst_mutations.extend(new_func_inst_mutations)
        full_section_mutations.extend(new_section_mutations)
        # 
        # new_func_inst_mutations, new_section_mutations = remove_import_funcs(
        #     parser,
            
        # )
        # full_func_inst_mutations.extend(new_func_inst_mutations)
        # full_section_mutations.extend(new_section_mutations)
        

        apply_mutation_and_encode_keep_snapshot(
            self.snapshot, 
            MutationBatch(
                func_inst_mutations=full_func_inst_mutations,
                definition_mutations=full_section_mutations
            ),
            self.tmp_used_path)
        if self.DEBUG:
            _see_snapshot_size_for_debug(self.snapshot)
            is_valid = validate_wasm(self.tmp_used_path, tag=ValidateCheckType.TEST_CHECK)
            if not is_valid:
                info_ = get_validation_info(self.tmp_used_path)
                assert info_ is not None
                if 'is not declared in any elem sections' in info_:
                    pass
                else:
                    raise Exception(f'{self.tmp_used_path} is invalid')
        if self.oracle_func(self.tmp_used_path, CheckType.TEST_CHECK):
            copy_file(self.tmp_used_path, self.output_path)
            return True
        return False


def update_parser_after_remove_start(
) -> tuple[list[FuncInstMutation], list[DefinitionMutation]]:
    func_inst_mutations = []
    section_mutations = [
        DefinitionMutation(
            sec_type=SectionType.Start,
            start_offset=0,
            end_offset=1,
            new_definitions=[]
        )
    ]
    return func_inst_mutations, section_mutations


def _see_snapshot_size_for_debug(
    snapshot: WMSnapshot
):
    tmp_path = 'tt/debug_snapshot.wasm'
    Encoder().encode_without_mutation(snapshot, tmp_path)
    file_size = os.path.getsize(tmp_path)
    print(f'debug snapshot size: {file_size} bytes')
    # snapshot.encode_to_path(tmp_path)
