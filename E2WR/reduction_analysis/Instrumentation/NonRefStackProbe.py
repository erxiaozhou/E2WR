from typing import Optional
from extract_block_mutator.InstGeneration.InstFactory import InstFactory
from extract_block_mutator.InstUtil.Inst import Inst
from extract_block_mutator.encode.new_defined_data_type import Blocktype
from reduction_analysis.WasmSemanticsUtil import is_ref_type
from .ProbeType import ValueProbeId
from .ProbeUtilWasmFuncManager import ProbeUtilWasmFuncManager


def decide_word_num(stack_ty):
    if stack_ty == 'i32' or stack_ty == 'f32':
        word_num = 1
    elif stack_ty == 'i64' or stack_ty == 'f64':
        word_num = 2
    elif stack_ty == 'v128':
        word_num = 4
    else:
        raise ValueError(f'unknown stack type: {stack_ty}')
    return word_num


def infer_type_seq_required_word_num(type_seq: list[str], ignore_ref=False) -> int:
    result = []
    for stack_ty in type_seq:
        if ignore_ref and is_ref_type(stack_ty):
            continue
        result.append(decide_word_num(stack_ty))
    result = sum(result)
    return result


def dump_stack_non_ref_probe(
    new_global_base_idx: int,
    memory_start_idx: int,
    idx_manager: ProbeUtilWasmFuncManager,
    probe: ValueProbeId,
    max_output_time: Optional[int] = None,
    probe_output_counter_start_idx: Optional[int] = None,
) -> list[Inst]:
    # EXECUTED 探针固定只输出 1 个 i32 事件头（4 字节）。
    # 原实现的 reserve_memory/retrieve_stack 参数被恒定覆盖/恒为空栈，已删除。
    insts: list[Inst] = list(probe.get_insts())
    insts.extend(get_n_save_memory_insts(new_global_base_idx, memory_start_idx, 1))
    insts.append(get_mem_offset_inst(memory_start_idx))
    insts.append(
        InstFactory.gen_binary_info_inst_high_single_imm(
            'call', imm0=idx_manager.store_i32_func_idx))
    insts.extend(_gen_call_print_core_insts(
        idx_manager, probe, max_output_time, probe_output_counter_start_idx))
    insts.extend(get_n_restore_memory_insts(new_global_base_idx, memory_start_idx, 1))
    return insts


def _gen_counter_addr_insts(
    probe_output_counter_start_idx: Optional[int],
    probe: ValueProbeId,
) -> list[Inst]:
    if probe_output_counter_start_idx is None:
        raise ValueError('probe_output_counter_start_idx is required when max_output_time is used')

    return [
        InstFactory.gen_binary_info_inst_high_single_imm(
            'i32.const',
            probe_output_counter_start_idx + int(probe.idx)
        )
    ]


def _gen_call_print_core_insts(
    idx_manager: ProbeUtilWasmFuncManager,
    probe: ValueProbeId,
    max_output_time: Optional[int],
    probe_output_counter_start_idx: Optional[int],
):
    word_num_inst = InstFactory.gen_binary_info_inst_high_single_imm(
        'i32.const', 4)
    probe_func_inst = InstFactory.gen_binary_info_inst_high_single_imm(
        'call', imm0=idx_manager.print_core_probe_func_idx)
    noop_probe_func_inst = InstFactory.gen_binary_info_inst_high_single_imm(
        'call', imm0=idx_manager.noop_print_core_probe_func_idx)

    if max_output_time is None:
        return [word_num_inst, probe_func_inst]

    if max_output_time <= 0:
        return [word_num_inst, noop_probe_func_inst]

    cond_if_type = Blocktype(idx_manager.print_core_probe_type_idx)
    insts: list[Inst] = [word_num_inst]
    insts.extend(_gen_counter_addr_insts(probe_output_counter_start_idx, probe))
    insts.append(InstFactory.gen_binary_info_inst_high('i32.load8_u', {'offset': 0, 'align': 0}))
    insts.append(InstFactory.gen_binary_info_inst_high_single_imm('i32.const', int(max_output_time)))
    insts.append(InstFactory.opcode_inst('i32.lt_u'))
    insts.append(InstFactory.gen_binary_info_inst_high_single_imm('if', cond_if_type))
    insts.extend(_gen_counter_addr_insts(probe_output_counter_start_idx, probe))
    insts.extend(_gen_counter_addr_insts(probe_output_counter_start_idx, probe))
    insts.append(InstFactory.gen_binary_info_inst_high('i32.load8_u', {'offset': 0, 'align': 0}))
    insts.append(InstFactory.gen_binary_info_inst_high_single_imm('i32.const', 1))
    insts.append(InstFactory.opcode_inst('i32.add'))
    insts.append(InstFactory.gen_binary_info_inst_high('i32.store8', {'offset': 0, 'align': 0}))
    insts.append(probe_func_inst)
    insts.append(InstFactory.opcode_inst('else'))
    insts.append(noop_probe_func_inst)
    insts.append(InstFactory.opcode_inst('end'))
    return insts


def get_mem_offset_inst(offset: int):
    return InstFactory.gen_binary_info_inst_high_single_imm('i32.const', offset)


def get_mini_save_memory_insts(
    new_global_base_idx: int,
    memory_start_idx: int
) -> list[Inst]:
    insts = [
        get_mem_offset_inst(memory_start_idx),
        InstFactory.gen_binary_info_inst_high(
            'i64.load', {'offset': 0, 'align': 3}),
        InstFactory.gen_binary_info_inst_high_single_imm(
            'global.set', new_global_base_idx)
    ]
    return insts


def get_n_save_memory_insts(
    new_global_base_idx: int,
    memory_start_idx: int,
    num_memory: int
) -> list[Inst]:
    insts = []
    # need_inst_num =
    if num_memory % 2 == 0:
        need_inst_num = num_memory // 2
    else:
        need_inst_num = num_memory // 2 + 1
    for i in range(need_inst_num):
        insts.extend(get_mini_save_memory_insts(
            new_global_base_idx + i, memory_start_idx + i * 8))
    return insts


def get_mini_restore_memory_insts(
    new_global_base_idx: int,
    memory_start_idx: int
) -> list[Inst]:
    insts = [
        get_mem_offset_inst(memory_start_idx),
        InstFactory.gen_binary_info_inst_high_single_imm(
            'global.get', new_global_base_idx),
        InstFactory.gen_binary_info_inst_high(
            'i64.store', {'offset': 0, 'align': 3}),
    ]
    return insts


def get_n_restore_memory_insts(
    new_global_base_idx: int,
    memory_start_idx: int,
    num_memory: int
) -> list[Inst]:
    insts = []

    if num_memory % 2 == 0:
        need_inst_num = num_memory // 2
    else:
        need_inst_num = num_memory // 2 + 1
    for i in range(need_inst_num):
        insts.extend(get_mini_restore_memory_insts(
            new_global_base_idx + i, memory_start_idx + i * 8))
    return insts
