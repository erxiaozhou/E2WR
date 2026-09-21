from extract_block_mutator.InstGeneration.InstFactory import InstFactory
from typing import Optional
from extract_block_mutator.WasmParzerUtil import append_func_core_base
from extract_block_mutator.funcTypeFactory import funcTypeFactory
from extract_block_mutator.parser_to_file_util import insert_func_core
from extract_block_mutator.wasmFunc import wasmFunc
from extract_block_mutator.WasmParser import WasmParser
from reduction_analysis.WasmSemanticsUtil import is_ref_type

from ..ASTInfo.AST import ASTINode, ASTNodeLoc, NodeList
from extract_block_mutator.InstUtil.Inst import Inst
from .ProbeUtilWasmFuncManager import ProbeUtilWasmFuncManager
from .ProbeType import ValueProbeId

from extract_block_mutator.InstGeneration.InstFactory import InstFactory
from .NonRefStackProbe import dump_local_probe_core, dump_stack_non_ref_probe, get_to_dump_local_idxs
from extract_block_mutator.InstUtil.Inst import Inst

from .ProbeUtilWasmFuncManager import ProbeUtilWasmFuncManager


def dump_stack_containing_ref(
    idx_manager: ProbeUtilWasmFuncManager,
    stack_types: list[str],
    probe: ValueProbeId
) -> list[Inst]:
    # assert 0
    call_idx = idx_manager.get_process_stack_contain_ref_func_idx(stack_types)
    assert call_idx is not None
    call_inst = InstFactory.gen_binary_info_inst_high_single_imm('call', call_idx)
    insts: list[Inst] = []
    insts.extend(probe.get_insts())
    insts.append(call_inst)
    return insts


def get_idx_pt(insts, inst_idx, rest_insts_after_last_layer):
    inst_idx_pt = inst_idx
    while True:
        last_idx = inst_idx_pt - 1
        if last_idx == -1:
            break
        last_inst = insts[last_idx]

        op_code = last_inst.opcode_text
        if op_code == 'block' \
                or op_code == 'loop' \
                or op_code == 'if' \
                or op_code == 'end' \
                or op_code == 'else':
            break
        else:
            rest_insts_after_last_layer.append(last_inst)
            inst_idx_pt = last_idx
    # if len(insts) == inst_idx_pt:
    #     inst_idx_pt -= 1
    return inst_idx_pt
    


def _is_node_list_tail(probe_loc:ASTNodeLoc, cur_node:ASTINode)->bool:
    if isinstance(cur_node, NodeList):
        if probe_loc.inst_idx == cur_node.loc.inst_idx+ cur_node.get_length():
            return True
    return False



def dump_executed_probe(
    save_global_base_idx:int,
    probe_memory_start_idx:int,
    idx_manager:ProbeUtilWasmFuncManager,
    probe: ValueProbeId,
    reserve_memory: bool = True,
    max_output_time: Optional[int] = None,
    probe_output_counter_start_idx: Optional[int] = None,
) -> list[Inst]:
    stack_types = []
    insts: list[Inst] = []
    insts.extend(
        dump_stack_non_ref_probe(
            save_global_base_idx,
            probe_memory_start_idx,
            stack_types,
            idx_manager,
            probe,
            reserve_memory=reserve_memory,
            max_output_time=max_output_time,
            probe_output_counter_start_idx=probe_output_counter_start_idx,
        )
    )
    return insts


def get_empty_print_core_probe_func() -> wasmFunc:
    return wasmFunc(
        insts=[],
        defined_local_types=[],
        func_ty=funcTypeFactory.generate_one_func_type_default(['i32'], [])
    )


def ensure_process_stack_contain_ref_func_idx_exist(
    save_global_base_idx:int,
    probe_memory_start_idx:int,
    probe_func_manager: ProbeUtilWasmFuncManager,
    parser: WasmParser,
    probe_id: ValueProbeId,
    stack_types: list[str]
) -> None:
    func_idx = probe_func_manager.get_process_stack_contain_ref_func_idx(stack_types)
    assert func_idx is None, print('func_idx', func_idx)
    if func_idx is None:
        new_func = gen_process_stack_contain_ref_func(
            save_global_base_idx,
            probe_memory_start_idx,
            probe_func_manager,
            stack_types,
            probe_id
        )
        append_func_core_base(parser, new_func)
        new_func_idx = parser.func_num - 1
        # print('================ debug func num related')
        # print('new_func_idx', new_func_idx)
        # print('parser.func_num', parser.func_num)
        # print('len(parser.defined_funcs)', len(parser.defined_funcs))
        # print('len(parser.func_type_idxs)', len(parser.func_type_idxs))
        # print('------------------------------------------')
        probe_func_manager.set_process_stack_contain_ref_func_idx(stack_types, new_func_idx)

def gen_process_stack_contain_ref_func(
    save_global_base_idx:int,
    probe_memory_start_idx:int,
    probe_func_manager: ProbeUtilWasmFuncManager,
    stack_types: list[str],
    probe_id: ValueProbeId,
    probe_output_counter_start_idx: Optional[int] = None,
    max_output_time: Optional[int] = None,
) -> wasmFunc:
    func_type = funcTypeFactory.generate_one_func_type_default(
        stack_types + ['i32'],
        stack_types
    )
    raw_stack_size = len(stack_types)
    local_idxs  = get_to_dump_local_idxs(stack_types)

    inst_seq = dump_local_probe_core(
        save_global_base_idx,
        probe_memory_start_idx,
        stack_types,
        local_idxs,
        probe_func_manager,
        probe_id,
        reserve_memory=True,
        max_output_time=max_output_time,
        probe_output_counter_start_idx=probe_output_counter_start_idx,
        dynamic_probe_id_local_idx=raw_stack_size,
    )
    actual_probe_insts = [
        InstFactory.gen_binary_info_inst_high_single_imm('local.get', raw_stack_size),
    ]
    inst_seq.replace_insts(actual_probe_insts, 'probe') # type: ignore
    # 
    local_get_all_insts = []
    for i in range(raw_stack_size):
        local_get_inst = InstFactory.gen_binary_info_inst_high_single_imm('local.get', i)
        local_get_all_insts.append(local_get_inst)
    inst_seq.insert_insts(local_get_all_insts, 'local_get_all', 0)
    new_func_insts = inst_seq.get_insts()
    new_func = wasmFunc(
        insts=new_func_insts,
        defined_local_types=[],
        func_ty=func_type
    )
    return new_func


def ensure_store_func_idx_exist(
    probe_func_manager: ProbeUtilWasmFuncManager,
    parser: WasmParser,
    expected_type: str
) -> None:
    if probe_func_manager.not_need_update():
        return
    store_func_idx = probe_func_manager.get_store_func_by_type(expected_type)
    if store_func_idx is None:
        # cur_defined_func_num = len(parser.defined_funcs)
        new_func_idx = parser.func_num
        # print('|||| new_func_idx', new_func_idx, 'len(parser.defined_funcs)', len(parser.defined_funcs))
        new_func = get_store_func_by_type(expected_type)
        insert_func_core(parser, new_func)
        probe_func_manager.set_store_func_idx(expected_type, new_func_idx)
        return

def ensure_store_func_idx_exist_for_types(
    probe_func_manager: ProbeUtilWasmFuncManager,
    parser: WasmParser,
    types: list[str]
) -> None:
    # print('ensure_store_func_idx_exist_for_types', types)
    for ty in types:
        if is_ref_type(ty):
            continue
        ensure_store_func_idx_exist(probe_func_manager, parser, ty)

def get_store_func_by_type(
    type_str: str
) -> wasmFunc:
    if type_str == 'i32':
        return gen_store_i32_func()
    elif type_str == 'f32':
        return gen_store_f32_func()
    elif type_str == 'i64':
        return gen_store_i64_func()
    elif type_str == 'f64':
        return gen_store_f64_func()
    elif type_str == 'v128':
        return gen_store_v128_func()
    else:
        raise ValueError(f'unknown type: {type_str}')


def get_print_core_probe_func(fd_write_id: int, g_base: int, memory_start_idx: int = 20012):
    assert memory_start_idx >= 12
    result_offset = memory_start_idx - 12
    para_start_offset = memory_start_idx - 8
    para_len_offset = memory_start_idx - 4
    print_probe_wasm_func = wasmFunc(
        insts=[
            # InstFactory.opcode_inst('return'),
            InstFactory.gen_binary_info_inst_high_single_imm( 'i32.const', result_offset),
            InstFactory.gen_binary_info_inst_high( 'i32.load', {'offset': 0, 'align': 2}),
            InstFactory.gen_binary_info_inst_high_single_imm( 'global.set', imm0=g_base+0),
            InstFactory.gen_binary_info_inst_high_single_imm( 'i32.const', para_start_offset),
            InstFactory.gen_binary_info_inst_high( 'i32.load', {'offset': 0, 'align': 2}),
            InstFactory.gen_binary_info_inst_high_single_imm( 'global.set', imm0=g_base+1),
            InstFactory.gen_binary_info_inst_high_single_imm(
                'i32.const', para_len_offset),
            InstFactory.gen_binary_info_inst_high(
                'i32.load', {'offset': 0, 'align': 2}),
            InstFactory.gen_binary_info_inst_high_single_imm(
                'global.set', imm0=g_base+2),
            #
            InstFactory.gen_binary_info_inst_high_single_imm(
                'i32.const', memory_start_idx),
            InstFactory.gen_binary_info_inst_high_single_imm('local.set', 1),
            InstFactory.gen_binary_info_inst_high_single_imm(
                'i32.const', para_start_offset),
            InstFactory.gen_binary_info_inst_high_single_imm('local.get', 1),
            InstFactory.gen_binary_info_inst_high(
                'i32.store', {'offset': 0, 'align': 2}),
            InstFactory.gen_binary_info_inst_high_single_imm(
                'i32.const', para_len_offset),
            InstFactory.gen_binary_info_inst_high_single_imm('local.get', 0),
            InstFactory.gen_binary_info_inst_high(
                'i32.store', {'offset': 0, 'align': 2}),

            InstFactory.gen_binary_info_inst_high_single_imm('i32.const', 1),
            InstFactory.gen_binary_info_inst_high_single_imm(
                'i32.const', para_start_offset),
            InstFactory.gen_binary_info_inst_high_single_imm('i32.const', 1),
            InstFactory.gen_binary_info_inst_high_single_imm(
                'i32.const', result_offset),
            InstFactory.gen_binary_info_inst_high_single_imm(
                'call', imm0=fd_write_id),
            InstFactory.opcode_inst('drop'),

            InstFactory.gen_binary_info_inst_high_single_imm(
                'i32.const', result_offset),
            InstFactory.gen_binary_info_inst_high_single_imm(
                'global.get', imm0=g_base+0),
            InstFactory.gen_binary_info_inst_high(
                'i32.store', {'offset': 0, 'align': 2}),
            InstFactory.gen_binary_info_inst_high_single_imm(
                'i32.const', para_start_offset),
            InstFactory.gen_binary_info_inst_high_single_imm(
                'global.get', imm0=g_base+1),
            InstFactory.gen_binary_info_inst_high(
                'i32.store', {'offset': 0, 'align': 2}),
            InstFactory.gen_binary_info_inst_high_single_imm(
                'i32.const', para_len_offset),
            InstFactory.gen_binary_info_inst_high_single_imm(
                'global.get', imm0=g_base+2),
            InstFactory.gen_binary_info_inst_high(
                'i32.store', {'offset': 0, 'align': 2})
        ],
        defined_local_types=['i32'],
        func_ty=funcTypeFactory.generate_one_func_type_default(['i32'], [])
    )

    return print_probe_wasm_func


def gen_store_i32_func():
    func = wasmFunc(
        insts=[
            InstFactory.gen_binary_info_inst_high_single_imm(
                'local.get', imm0=1),
            InstFactory.gen_binary_info_inst_high_single_imm(
                'local.get', imm0=0),
            InstFactory.gen_binary_info_inst_high(
                'i32.store', {'offset': 0, 'align': 2}),
        ],
        defined_local_types=[],
        func_ty=funcTypeFactory.generate_one_func_type_default(
            ['i32', 'i32'], [])
    )
    return func


def gen_store_f32_func():
    func = wasmFunc(
        insts=[
            InstFactory.gen_binary_info_inst_high_single_imm(
                'local.get', imm0=1),
            InstFactory.gen_binary_info_inst_high_single_imm(
                'local.get', imm0=0),
            InstFactory.gen_binary_info_inst_high(
                'f32.store', {'offset': 0, 'align': 2}),
        ],
        defined_local_types=[],
        func_ty=funcTypeFactory.generate_one_func_type_default(
            ['f32', 'i32'], [])
    )
    return func


def gen_store_i64_func():
    func = wasmFunc(
        insts=[
            InstFactory.gen_binary_info_inst_high_single_imm(
                'local.get', imm0=1),
            InstFactory.gen_binary_info_inst_high_single_imm(
                'local.get', imm0=0),
            InstFactory.gen_binary_info_inst_high(
                'i64.store', {'offset': 0, 'align': 3}),
        ],
        defined_local_types=[],
        func_ty=funcTypeFactory.generate_one_func_type_default(
            ['i64', 'i32'], [])
    )
    return func


def gen_store_f64_func():
    func = wasmFunc(
        insts=[
            InstFactory.gen_binary_info_inst_high_single_imm(
                'local.get', imm0=1),
            InstFactory.gen_binary_info_inst_high_single_imm(
                'local.get', imm0=0),
            InstFactory.gen_binary_info_inst_high(
                'f64.store', {'offset': 0, 'align': 3}),
        ],
        defined_local_types=[],
        func_ty=funcTypeFactory.generate_one_func_type_default(
            ['f64', 'i32'], [])
    )
    return func


def gen_store_v128_func():
    func = wasmFunc(
        insts=[
            InstFactory.gen_binary_info_inst_high_single_imm(
                'local.get', imm0=1),
            InstFactory.gen_binary_info_inst_high_single_imm(
                'local.get', imm0=0),
            InstFactory.gen_binary_info_inst_high(
                'v128.store', {'offset': 0, 'align': 4}),
        ],
        defined_local_types=[],
        func_ty=funcTypeFactory.generate_one_func_type_default(
            ['v128', 'i32'], [])
    )
    return func
