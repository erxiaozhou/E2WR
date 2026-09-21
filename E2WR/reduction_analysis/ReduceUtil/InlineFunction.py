from extract_block_mutator.InstGeneration.InstFactory import InstFactory
from extract_block_mutator.WasmParser import WasmParser
from extract_block_mutator.encode.new_defined_data_type import Blocktype
from extract_block_mutator.funcType import funcType
from extract_block_mutator.funcTypeFactory import funcTypeFactory
from extract_block_mutator.wasmFunc import wasmFunc
from extract_block_mutator.WasmParser import WasmParser
from WasmInfoCfg import SectionType
from extract_block_mutator.InstGeneration.InstFactory import InstFactory
from typing import Optional
from .RemoveDeadFunc import remove_dead_funcs
from reduction_analysis.ParserModification import DefinitionMutation, FuncInstMutation, RewriteLocalDesc
from .RewritingUtil.InstsReplacement import InstsReplacement
from ..InstsScope import InstsScope


class InlineFunctionHelper:
    @staticmethod
    def a_function_can_be_inlined_through_call(
        parser: WasmParser,
        target_defined_func_idx: int
    ) -> bool:
        if _detect_defined_func_idx_called_num(parser, target_defined_func_idx) != 1:
            return False
        return True

    @staticmethod
    def inline_a_func(
        parser: WasmParser,
        caller_func_idxs: set[int],
        inlined_func_idx: int
    )->tuple[list[FuncInstMutation], list[RewriteLocalDesc], list[DefinitionMutation]]:
     
        inlined_func = parser.defined_funcs[inlined_func_idx]
        inlined_func_type_idx = parser.defined_func_ty_ids[inlined_func_idx]
        inlined_func_type = parser.types[inlined_func_type_idx]

        actual_inlined_func_idx = parser.import_func_num + inlined_func_idx

        func_inst_mutations = []
        rewrite_local_descs = []
        definition_mutations = []
        raw_type_mutations = []
        for caller_defined_idx in caller_func_idxs:
            caller_func = parser.defined_funcs[caller_defined_idx]
            inst_mutations, local_descs, type_mutations = _generate_inline_mutations(
                caller_func,
                inlined_func,
                inlined_func_type,
                actual_inlined_func_idx,
                caller_defined_idx,
                parser
            )
            raw_type_mutations.extend(type_mutations)
            func_inst_mutations.extend(inst_mutations)
            rewrite_local_descs.extend(local_descs)
            # definition_mutations.extend(type_mutations)
        # assert 0, print(f'inst_mutations: {inst_mutations}')
        new_type_mutation = _combine_type_mutations(raw_type_mutations)
        if new_type_mutation is not None:
            definition_mutations.append(new_type_mutation)

        removal_func_mutations, removal_def_mutations = remove_dead_funcs(
            parser, DEBUG=False, to_remove_defined_func_idxs={inlined_func_idx}, rewrite_callsites=False
        )
        # assert 0, print(f'removal_func_mutations: {removal_func_mutations}')
        func_inst_mutations.extend(removal_func_mutations)
        definition_mutations.extend(removal_def_mutations)

        return func_inst_mutations, rewrite_local_descs, definition_mutations





def _combine_type_mutations(
    raw_type_mutations: list[DefinitionMutation]
)->Optional[DefinitionMutation]:
    introduced_types = []
    for m in raw_type_mutations:
        assert m.sec_type == SectionType.Type
        assert len(m.raw_new_definitions) == 1
        introduced_types.extend(m.raw_new_definitions)
    if len(introduced_types) == 0:
        return None
    assert len(set(introduced_types)) == 1, f"introduced_types: {introduced_types}"
    d0 = raw_type_mutations[0]
    return DefinitionMutation(
        sec_type=SectionType.Type,
        start_offset=d0.start_offset,
        end_offset=d0.end_offset,
        new_definitions=[introduced_types[0]]
    )

def _generate_inline_mutations(
    caller_func,
    inlined_func,
    inlined_func_type: funcType,
    actual_inlined_func_idx: int,
    caller_defined_idx: int,
    parser: WasmParser
) -> tuple[list[FuncInstMutation], list[RewriteLocalDesc], list[DefinitionMutation]]:
    original_local_count = len(caller_func.local_types)
    
    if any(t.opcode_text.startswith('local') for t in inlined_func.insts):
        
        new_local_types = caller_func.defined_local_types.copy()
        new_local_types.extend(inlined_func.local_types)
        local_descs =[ RewriteLocalDesc(
            func_idx=caller_defined_idx,
            new_defined_locals=new_local_types
        )]
        need_init_inlined_locals = True
    else:
        local_descs = []
        need_init_inlined_locals = False
    func_inst_mutations = []
    type_mutations = []
    
    for inst_idx, inst in enumerate(caller_func.insts):
        if inst.opcode_text == 'call' and inst.imm_part.val == actual_inlined_func_idx:
            inline_instructions = _generate_inline_instructions(
                inlined_func,
                inlined_func_type,
                original_local_count,
                actual_inlined_func_idx=actual_inlined_func_idx,
                inline_func_has_return=_func_has_return(inlined_func),
                need_init_inlined_locals=need_init_inlined_locals,
            )
            replacement = InstsReplacement(
                parser=parser,
                func_idx=caller_defined_idx,
                original_scope=InstsScope(
                    func_idx=caller_defined_idx,
                    start_idx=inst_idx,
                    end_idx=inst_idx + 1
                ),
                new_insts=inline_instructions
            )
            
            inst_mutation, type_mutations = replacement.as_func_inst_mutation()
            func_inst_mutations.append(inst_mutation)
            type_mutations.extend(type_mutations)
                

    return func_inst_mutations, local_descs, type_mutations


def _func_has_return(func: wasmFunc) -> bool:
    for inst in func.insts:
        if inst.opcode_text == 'return':
            return True
    return False


def _generate_inline_instructions(
    inlined_func,
    inlined_func_type: funcType,
    original_local_count: int,
    actual_inlined_func_idx: int,
    inline_func_has_return: bool,
    need_init_inlined_locals: bool
) -> list:
    
    instructions = []
    param_count = len(inlined_func_type.param_types)
    for i in range(param_count - 1, -1, -1):
        param_local_idx = original_local_count + i
        if need_init_inlined_locals:
            new_inst = InstFactory.gen_binary_info_inst_high_single_imm(
                'local.set', param_local_idx
            )
        else:
            new_inst = InstFactory.opcode_inst('drop')
        instructions.append(new_inst)

    if inline_func_has_return:
        block_type = Blocktype(funcTypeFactory.generate_one_func_type_default(
            [],
            inlined_func_type.result_types
        ))
        block_inst = InstFactory.gen_binary_info_inst_high_single_imm(
            'block', block_type)
        instructions.append(block_inst)

    cur_depth = 0

    for inst in inlined_func.insts:
        if inst.opcode_text in ['loop', 'block', 'if']:
            cur_depth += 1
        elif inst.opcode_text == 'end':
            cur_depth -= 1

        if inst.opcode_text == 'return':
            br_target = cur_depth
            br_inst = InstFactory.gen_binary_info_inst_high_single_imm(
                'br', br_target)
            instructions.append(br_inst)
        elif inst.opcode_text in ['local.get', 'local.set', 'local.tee']:
            original_local_idx = inst.imm_part.val
            new_local_idx = original_local_idx + original_local_count
            new_inst = InstFactory.gen_binary_info_inst_high_single_imm(
                inst.opcode_text, new_local_idx
            )
            instructions.append(new_inst)
        elif inst.opcode_text == 'call':
            called = inst.imm_part.val
            if called > actual_inlined_func_idx:
                new_inst = InstFactory.gen_binary_info_inst_high_single_imm(
                    inst.opcode_text, called-1
                )
                instructions.append(new_inst)
            else:
                instructions.append(inst.copy())
        else:
            instructions.append(inst.copy())

    if inline_func_has_return:
        end_inst = InstFactory.opcode_inst('end')
        instructions.append(end_inst)

    return instructions


def _detect_defined_func_idx_called_num(
    parser: WasmParser,
    target_defined_func_idx: int
) -> int:
    num = 0
    actual_func_idx = parser.import_func_num + target_defined_func_idx
    for func in parser.defined_funcs:
        for inst in func.insts:
            op = inst.opcode_text
            if op == 'call':
                func_idx = inst.imm_part.val
                if func_idx == actual_func_idx:
                    num += 1
    return num
