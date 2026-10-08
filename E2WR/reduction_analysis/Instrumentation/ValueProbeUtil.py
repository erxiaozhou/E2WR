from extract_block_mutator.InstGeneration.InstFactory import InstFactory
from typing import Optional
from extract_block_mutator.funcTypeFactory import funcTypeFactory
from extract_block_mutator.wasmFunc import wasmFunc

from extract_block_mutator.InstUtil.Inst import Inst
from .ProbeUtilWasmFuncManager import ProbeUtilWasmFuncManager
from .ProbeType import ValueProbeId
from .NonRefStackProbe import dump_stack_non_ref_probe


def dump_executed_probe(
    save_global_base_idx:int,
    probe_memory_start_idx:int,
    idx_manager:ProbeUtilWasmFuncManager,
    probe: ValueProbeId,
    max_output_time: Optional[int] = None,
    probe_output_counter_start_idx: Optional[int] = None,
) -> list[Inst]:
    return dump_stack_non_ref_probe(
        save_global_base_idx,
        probe_memory_start_idx,
        idx_manager,
        probe,
        max_output_time=max_output_time,
        probe_output_counter_start_idx=probe_output_counter_start_idx,
    )


def get_empty_print_core_probe_func() -> wasmFunc:
    return wasmFunc(
        insts=[],
        defined_local_types=[],
        func_ty=funcTypeFactory.generate_one_func_type_default(['i32'], [])
    )







def get_store_func_by_type(
    type_str: str
) -> wasmFunc:
    if type_str == 'i32':
        return gen_store_i32_func()
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








