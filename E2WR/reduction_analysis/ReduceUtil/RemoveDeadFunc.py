from extract_block_mutator.WasmParser import WasmParser
from extract_block_mutator.encode.NGDataPayload import DataPayloadwithName
from ..ASTState import ASTState
from WasmInfoCfg import ExportType, ImportType, SectionType
from extract_block_mutator.InstGeneration.InstFactory import InstFactory
from extract_block_mutator.InstUtil.ByteInst import ByteImmInst
from extract_block_mutator.InstUtil.Inst import Inst
from extract_block_mutator.DefShell import gen_export_desc, gen_export_funcidx_part
from extract_block_mutator.get_data_shell import get_elemseg_attr, get_export_attr, get_impotr_attr, has_func_idx
from typing import Union, Optional
from reduction_analysis.ParserModification import FuncInstMutation, DefinitionMutation

from .NewInstUtil import  padding_input_type_naive


def remove_dead_funcs(
    parser: WasmParser,
    DEBUG: bool,
    to_remove_defined_func_idxs: Union[int, set[int]],
    to_remove_import_idxs:Optional[set[int]]=None,
    rewrite_callsites: bool=True,
    callsite_as_unreachable: bool=False
) -> tuple[list[FuncInstMutation], list[DefinitionMutation]]:
    if to_remove_import_idxs is None:
        to_remove_import_idxs = set()
    
    if isinstance(to_remove_defined_func_idxs, int):
        to_remove_defined_func_idxs = {to_remove_defined_func_idxs}
    
    func_inst_mutations = []
    section_mutations = []
    
    if len(to_remove_defined_func_idxs) == 0 and len(to_remove_import_idxs) == 0:
        return func_inst_mutations, section_mutations
    # 
    
    ifunc_idx2import_idx:dict[int, int] = {}
    func_idx = 0
    for import_idx, import_desc in enumerate(parser.imports):
        import_attr = get_impotr_attr(import_desc, 'type')
        if import_attr == ImportType.func:
            ifunc_idx2import_idx[func_idx] = import_idx
            func_idx += 1
    import_idx2ifunc_idx:dict[int, int] = {v: k for k, v in ifunc_idx2import_idx.items()}
    # 
    to_remove_import_func_idxs ={import_idx2ifunc_idx[idx] for idx in to_remove_import_idxs}
    ori_id2new_id: dict[int, int] = {}
    to_skip_num = 0
    import_func_num = parser.import_func_num
    
    for func_idx in range(import_func_num):
        if func_idx in to_remove_import_func_idxs:
            to_skip_num += 1
        else:
            ori_id2new_id[func_idx] = func_idx - to_skip_num

    
    total_func_num = parser.func_num
    for func_idx in range(import_func_num, total_func_num):
        if func_idx - import_func_num in to_remove_defined_func_idxs:
            to_skip_num += 1
        else:
            ori_id2new_id[func_idx] = func_idx - to_skip_num
    
    all_to_remove_func_idxs = {i + import_func_num for i in to_remove_defined_func_idxs} | to_remove_import_func_idxs
    removed_import_func_num = len(to_remove_import_func_idxs)
    detault_padding_func_idx = import_func_num - removed_import_func_num
    
    func_ty_idxs = parser.func_type_idxs.copy()
    func_mutations, sect_mutations = _rewrite_func_idxs_after_delete_a_func(parser, all_to_remove_func_idxs, ori_id2new_id, func_ty_idxs, rewrite_callsites, detault_padding_func_idx, callsite_as_unreachable)
    # print('VVZXXXX', 'func_mutations', func_mutations)
    func_inst_mutations.extend(func_mutations)
    section_mutations.extend(sect_mutations)
    
    func_mutations, sect_mutations = _remove_corresponding_export_func(parser, all_to_remove_func_idxs, ori_id2new_id)
    func_inst_mutations.extend(func_mutations)
    section_mutations.extend(sect_mutations)
    
    func_mutations, sect_mutations = _rewrite_start_idx_after_delete_a_func(parser, all_to_remove_func_idxs, ori_id2new_id)
    func_inst_mutations.extend(func_mutations)
    section_mutations.extend(sect_mutations)
    
    for remove_idx in sorted(to_remove_defined_func_idxs, reverse=True):
        section_mutations.append(DefinitionMutation(
            sec_type=SectionType.Function,
            start_offset=remove_idx,
            end_offset=remove_idx + 1,
            new_definitions=[]
        ))
        section_mutations.append(DefinitionMutation(
            sec_type=SectionType.Code,
            start_offset=remove_idx,
            end_offset=remove_idx + 1,
            new_definitions=[]
        ))
    for remove_idx in sorted(to_remove_import_idxs, reverse=True):
        section_mutations.append(DefinitionMutation(
            sec_type=SectionType.Import,
            start_offset=remove_idx,
            end_offset=remove_idx + 1,
            new_definitions=[]
        ))
    
    return func_inst_mutations, section_mutations
 

def _rewrite_start_idx_after_delete_a_func(
    parser: WasmParser, 
    target_func_idxs: set[int],
    ori_id2new_id: dict[int, int]
) -> tuple[list[FuncInstMutation], list[DefinitionMutation]]:
    func_inst_mutations = []
    section_mutations = []
    
    cur_start = parser.start_sec_data
    if cur_start is None:
        return func_inst_mutations, section_mutations
    elif cur_start in target_func_idxs:
        section_mutations.append(DefinitionMutation(
            sec_type=SectionType.Start,
            start_offset=0,
            end_offset=1,
            new_definitions=[]
        ))
    else:
        expected_new_start = ori_id2new_id[cur_start]
        if expected_new_start != cur_start:
            section_mutations.append(DefinitionMutation(
                sec_type=SectionType.Start,
                start_offset=0,
                end_offset=1,
                new_definitions=[expected_new_start]
            ))
    
    return func_inst_mutations, section_mutations


def _remove_corresponding_export_func(
    parser: WasmParser  , 
    target_func_idxs: set[int],
    ori_id2new_id: dict[int, int]
) -> tuple[list[FuncInstMutation], list[DefinitionMutation]]:
    func_inst_mutations = []
    section_mutations = []
    
    for export_idx, export_ in enumerate(parser.exports):
        attr = get_export_attr(export_, 'attr')
        if attr == ExportType.func:
            func_idx = get_export_attr(export_, 'idx')
            assert isinstance(func_idx, int)
            if func_idx in target_func_idxs:
               
                section_mutations.append(DefinitionMutation(
                        sec_type=SectionType.Export,
                        start_offset=export_idx,
                        end_offset=export_idx + 1,
                        new_definitions=[]
                    ))
                continue
            expected_new_func_idx = ori_id2new_id[func_idx]
            if expected_new_func_idx != func_idx:
                export_func_name = get_export_attr(export_, 'name')
                new_idx_part = gen_export_funcidx_part(func_idx=expected_new_func_idx)
                new_export = gen_export_desc(name=export_func_name, desc=new_idx_part)
                section_mutations.append(DefinitionMutation(
                    sec_type=SectionType.Export,
                    start_offset=export_idx,
                    end_offset=export_idx + 1,
                    new_definitions=[new_export]
                ))
    
    return func_inst_mutations, section_mutations



def _rewrite_func_idxs_after_delete_a_func(
    parser: WasmParser, 
    deleted_func_idxs: set[int],
    ori_id2new_id: dict[int, int],
    func_ty_idxs: list[int],
    rewrite_callsites: bool,
    detault_padding_func_idx:int,
    callsite_as_unreachable: bool=False
) -> tuple[list[FuncInstMutation], list[DefinitionMutation]]:
    func_inst_mutations = []
    section_mutations = []
    deleted_defined_func_idxs = set(idx -parser.import_func_num for idx in deleted_func_idxs if idx >= parser.import_func_num)
    for func_idx, func in enumerate(parser.defined_funcs):
        if func_idx in deleted_defined_func_idxs:
            continue
        mutations_for_func = []
        for inst_idx, inst in enumerate(func.insts):
            op = inst.opcode_text
            if  op == 'ref.func' or  op == 'call':
                assert isinstance(inst, ByteImmInst)
                imm0 = inst.imm_part.val
                assert isinstance(imm0, int)
                cur_im = None
                if imm0 in deleted_func_idxs:
                    new_insts = []
                    if rewrite_callsites and (op == 'call'):
                        # print('MAY REPLACE `CALL` CALLSITE')
                        func_type = parser.types[func_ty_idxs[imm0]]
                        if callsite_as_unreachable:
                            new_insts.append(InstFactory.opcode_inst('unreachable'))
                            # print('PR FUNC UNREACHABLE REPLACEMENT')
                        else:
                            new_inst_seq = padding_input_type_naive(
                                func_type.param_types,
                                func_type.result_types
                            )
                            new_insts.extend(new_inst_seq)
                            # print('PR FUNC TRIVIAL REPLACEMENT')
                        cur_im = FuncInstMutation(
                            func_idx=func_idx,
                            start_offset=inst_idx,
                            end_offset=inst_idx + 1,
                            new_insts=new_insts
                        )
                    elif op == 'ref.func':
                        new_insts.append(InstFactory.gen_binary_info_inst_high_single_imm(op, imm0=detault_padding_func_idx))
                        cur_im = FuncInstMutation(
                            func_idx=func_idx,
                            start_offset=inst_idx,
                            end_offset=inst_idx + 1,
                            new_insts=new_insts
                        )
                else:
                    expected_new_imm0 = ori_id2new_id[imm0]
                    if expected_new_imm0 != imm0:
                        cur_im = FuncInstMutation(
                        func_idx=func_idx,
                        start_offset=inst_idx,
                        end_offset=inst_idx + 1,
                        new_insts=[InstFactory.gen_binary_info_inst_high_single_imm(op, imm0=expected_new_imm0)]
                        )
                
                if cur_im is not None:
                    mutations_for_func.append(cur_im)
        
        func_inst_mutations.extend(mutations_for_func)
    
    for seg_idx, seg in enumerate(parser.elem_sec_datas):
        seg_has_func_idx = has_func_idx(seg)
        if seg_has_func_idx:
            inner_name = seg.inner_name
            inner_name_str = inner_name.name
            seg_changed = False
            new_seg = None
            
            if inner_name_str in ['passive_elem_seg0', 'active_elem_seg0', 'active_elem_seg1', 'declarative_elem_seg0']:
                func_idxs = seg.data['funcidxs']
                assert isinstance(func_idxs, list)
                new_func_idxs = []
                for func_idx in func_idxs:
                    if func_idx in deleted_func_idxs:
                        new_func_idxs.append(detault_padding_func_idx)
                        seg_changed = True
                    else:
                        expected_new_func_idx = ori_id2new_id[func_idx]
                        if expected_new_func_idx != func_idx:
                            new_func_idxs.append(expected_new_func_idx)
                            seg_changed = True
                        else:
                            new_func_idxs.append(func_idx)
                
                if seg_changed:
                    new_seg_data = seg.data.copy()
                    new_seg_data['funcidxs'] = new_func_idxs
                    new_seg = DataPayloadwithName(
                        data=new_seg_data,
                        name=seg.inner_name.name
                    )
            else:
                ref_type = get_elemseg_attr(seg, 'ref_type')
                if ref_type != 'externref':
                    new_exprs = []
                    for inst_expr in seg.data['exprs']:
                        inst = inst_expr.val
                        assert isinstance(inst, Inst)
                        opcode = inst.opcode_text
                        if opcode == 'ref.func':
                            func_idx = inst.imm_part.val
                            if func_idx in deleted_func_idxs:
                                _new_inst = InstFactory.gen_binary_info_inst_high_single_imm(opcode, imm0=detault_padding_func_idx)
                                new_exprs.append(DataPayloadwithName(
                                    data={'expr': _new_inst},
                                    name='Expr'
                                ))
                                seg_changed = True
                            else:
                                expected_new_func_idx = ori_id2new_id[func_idx]
                                if expected_new_func_idx != func_idx:
                                    _new_inst = InstFactory.gen_binary_info_inst_high_single_imm(opcode, imm0=expected_new_func_idx)
                                    new_exprs.append(DataPayloadwithName(
                                        data={'expr': _new_inst},
                                        name='Expr'
                                    ))
                                    seg_changed = True
                                else:
                                    new_exprs.append(inst_expr)
                        else:
                            new_exprs.append(inst_expr)
                    
                    if seg_changed:
                        new_seg_data = seg.data.copy()
                        new_seg_data['exprs'] = new_exprs
                        new_seg = DataPayloadwithName(
                            data=new_seg_data,
                            name=seg.inner_name.name
                        )
            
            if seg_changed and new_seg is not None:
                section_mutations.append(DefinitionMutation(
                    sec_type=SectionType.Element,
                    start_offset=seg_idx,
                    end_offset=seg_idx + 1,
                    new_definitions=[new_seg]
                ))
    
    return func_inst_mutations, section_mutations
