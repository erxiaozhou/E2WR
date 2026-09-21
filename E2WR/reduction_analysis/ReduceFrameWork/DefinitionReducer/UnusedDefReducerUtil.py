from tkinter import FALSE

from extract_block_mutator.DefShell import gen_func_import_attr, gen_import_desc
from extract_block_mutator.InstGeneration.InstFactory import InstFactory
from extract_block_mutator.WasmParser import WasmParser
from extract_block_mutator.encode.new_defined_data_type import Blocktype
from WasmInfoCfg import ImportType, SectionType
from extract_block_mutator.funcType import funcType
from extract_block_mutator.get_data_shell import get_impotr_attr
from reduction_analysis.ParserModification import FuncInstMutation, DefinitionMutation


def detect_used_unused_type_idxs(
    parser: WasmParser
):
    used_types = set()
    direct_used_type_idxs = set()
    types = parser.types
    direct_used_type_idxs.update(parser.func_type_idxs)
    for defined_func in parser.defined_funcs:
        for inst_idx, inst in enumerate(defined_func.insts):
            op = inst.opcode_text
            if op == 'if' or op == 'block' or op == 'loop':
                imm = inst.imm_part.val
                assert isinstance(imm, Blocktype)
                block_type = imm.concrete_type(parser.types)
                used_types.add(block_type)
            if op == 'call_indirect':
                type_idx = inst.imm_part.data['y']
                direct_used_type_idxs.add(type_idx)
    for import_ in parser.imports:
        attr = get_impotr_attr(import_, 'type')
        if attr == ImportType.func:
            import_attr = get_impotr_attr(import_, 'import_attr')
            idx = import_attr.data['typeidx']  # type: ignore
            direct_used_type_idxs.add(idx)

    for type_idx in direct_used_type_idxs:
        ty = types[type_idx]
        used_types.add(ty)
    used_idxs = set()
    seen_types = set()
    for type_idx, ty in enumerate(types):
        if ty in used_types and ty not in seen_types:
            used_idxs.add(type_idx)
            seen_types.add(ty)
    return used_idxs, set(range(len(types))) - used_idxs


def update_parser_after_remove_type(
    parser: WasmParser,
    to_remove_idxs: set[int]
) -> tuple[list[FuncInstMutation], list[DefinitionMutation]]:
    func_inst_mutations = []
    section_mutations = []
    if len(to_remove_idxs) == 0:
        return func_inst_mutations, section_mutations

    new_types = [t for idx, t in enumerate(
        parser.types) if idx not in to_remove_idxs]
    ori_types = parser.types
    # _type2new_idx = {ty: idx for idx, ty in enumerate(new_types)}
    _type2new_idx = {}
    for idx, ty in enumerate(new_types):
        if ty in _type2new_idx:
            pass
        else:
            _type2new_idx[ty] = idx

    for func_idx, defined_func in enumerate(parser.defined_funcs):
        for inst_idx, inst in enumerate(defined_func.insts):
            op = inst.opcode_text
            if op == 'if' or op == 'block' or op == 'loop':
                imm = inst.imm_part.val
                assert isinstance(imm, Blocktype)
                if isinstance(imm.init_data, str) or isinstance(imm.init_data, bool) or isinstance(imm.init_data, int):
                    continue
                block_type = imm.concrete_type(parser.types)
                if block_type == funcType([], []):
                    new_bt = Blocktype(False)
                else:
                    
                    new_type_idx = _type2new_idx[block_type]
                # if new_type_idx == imm.init_data:
                #     continue
                    new_bt = Blocktype(new_type_idx)
                new_inst = InstFactory.gen_binary_info_inst_high_single_imm(
                    op, new_bt)
                func_inst_mutations.append(FuncInstMutation(
                    func_idx=func_idx,
                    start_offset=inst_idx,
                    end_offset=inst_idx + 1,
                    new_insts=[new_inst]
                ))
            elif op == 'call_indirect':
                ori_data = inst.imm_part.data
                type_idx = ori_data['y']
                new_type_idx = _type2new_idx[ori_types[type_idx]]
                if type_idx != new_type_idx:
                    new_data = ori_data.copy()
                    new_data['y'] = new_type_idx
                    new_inst = InstFactory.gen_binary_info_inst_high(
                        op='call_indirect',
                        imm_dict=new_data)
                    func_inst_mutations.append(FuncInstMutation(
                        func_idx=func_idx,
                        start_offset=inst_idx,
                        end_offset=inst_idx + 1,
                        new_insts=[new_inst]
                    ))

    for remove_idx in to_remove_idxs:
        section_mutations.append(DefinitionMutation(
            sec_type=SectionType.Type,
            start_offset=remove_idx,
            end_offset=remove_idx + 1,
            new_definitions=[]
        ))

    for start_idx, type_idx in enumerate(parser.defined_func_ty_ids):
        new_type_idx = _type2new_idx[ori_types[type_idx]]
        # new_func_ty_ids.append(new_type_idx)

    # if new_func_ty_ids != parser.defined_func_ty_ids:
    #     for start_idx in range(len(parser.defined_func_ty_ids)):
        if type_idx != new_type_idx:
            section_mutations.append(DefinitionMutation(
                sec_type=SectionType.Function,
                start_offset=start_idx,
                end_offset=start_idx + 1,
                new_definitions=[new_type_idx]  
            ))

    import_mutations_needed = []
    for import_idx, import_desc in enumerate(parser.imports):
        import_desc_type = get_impotr_attr(import_desc, 'type')
        if import_desc_type == ImportType.func:
            idx_ = get_impotr_attr(
                import_desc, 'import_attr').data['typeidx']  # type: ignore
            new_idx = _type2new_idx[ori_types[idx_]]
            if idx_ != new_idx:
                module_name = get_impotr_attr(import_desc, 'module_name')
                entity_name = get_impotr_attr(import_desc, 'entity_name')
                desc_ = gen_func_import_attr(new_idx)
                new_import_desc = gen_import_desc(
                    module_name, entity_name, desc_)
                import_mutations_needed.append((import_idx, new_import_desc))

    for import_idx, new_import_desc in import_mutations_needed:
        section_mutations.append(DefinitionMutation(
            sec_type=SectionType.Import,
            start_offset=import_idx,
            end_offset=import_idx + 1,
            new_definitions=[new_import_desc]
        ))

    return func_inst_mutations, section_mutations
