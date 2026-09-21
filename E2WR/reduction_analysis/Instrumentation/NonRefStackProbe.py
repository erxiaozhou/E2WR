import traceback
from typing import Optional
from extract_block_mutator.InstGeneration.InstFactory import InstFactory
from extract_block_mutator.InstUtil.ByteInst import ByteImmInst
from extract_block_mutator.InstUtil.Inst import Inst
from extract_block_mutator.encode.new_defined_data_type import Blocktype
from reduction_analysis.WasmSemanticsUtil import is_ref_type
from .ProbeType import ValueProbeId
from .ProbeUtilWasmFuncManager import ProbeUtilWasmFuncManager
from .InstSeqWithName import InstSeqWithName


class StackNonRefProbeGenerator:
    def __init__(
        self,
        idx_manager: ProbeUtilWasmFuncManager,
        new_global_base_idx: int,
        memory_start_idx: int,
        stack_types: list[str],
        probe: ValueProbeId,
        probe_output_counter_start_idx: Optional[int] = None,
        dynamic_probe_id_local_idx: Optional[int] = None,
    ):
        self.idx_manager = idx_manager
        self.new_global_base_idx = new_global_base_idx
        self.memory_start_idx = memory_start_idx
        self.stack_types = stack_types
        self.probe = probe
        self.probe_output_counter_start_idx = probe_output_counter_start_idx
        self.dynamic_probe_id_local_idx = dynamic_probe_id_local_idx
        #
        self.raw_operand_num = len(stack_types)
        self.raw_val_word_list = self._get_each_val_word_num()
        # self.raw_total
        self.to_save_total_word_num = infer_type_seq_required_word_num(self.to_save_stack_types)
        self.to_save_total_byte_num = self.to_save_total_word_num * 4
        # print('StackNonRefProbeGenerator __init__ stack_types', self.stack_types)

    def generate_probe(
        self,
        reserve_memory: bool = True,
        retrieve_stack: bool = True,
        max_output_time: Optional[int] = None,
    ) -> InstSeqWithName:
        reserve_memory = False  
        # assert not reserve_memory
        reserve_memory = True

        probe_insts = self._get_probe_insts(self.probe)
        head_insts = self._gen_head_insts(self.probe)
        assert self.expected_head_insts_len == len(head_insts) + len(probe_insts)

        data_insts = self._gen_store_insts()
        call_func_insts = self._gen_call_print_core_insts(max_output_time=max_output_time)

        retrieve_stack_insts = self._gen_retrieve_insts() if retrieve_stack else []
        save_memory_insts = self._gen_save_memory_insts() if reserve_memory else []
        retrieve_memory_insts = self._gen_retrieve_memory_insts() if reserve_memory else []

        expected_seq: list[str] = ['probe', 'head']
        if reserve_memory:
            expected_seq.append('save_memory')
        expected_seq.extend(['data', 'call_func'])
        if retrieve_stack:
            expected_seq.append('retrieve_stack')
        if reserve_memory:
            expected_seq.append('retrieve_memory')

        name2insts = {
            'probe': probe_insts,
            'head': head_insts,
            'save_memory': save_memory_insts,
            'data': data_insts,
            'call_func': call_func_insts,
            'retrieve_stack': retrieve_stack_insts,
            'retrieve_memory': retrieve_memory_insts,
        }
        # Only keep segments that appear in `expected_seq`.
        name2insts = {k: v for k, v in name2insts.items() if k in expected_seq}

        return InstSeqWithName(
            name2insts=name2insts,  # type: ignore
            expected_seq=expected_seq,
        )

    # ===== * ===== util functions ===== * =====

    @property
    def to_save_stack_types(self):
        return self.stack_types + self.expected_head_insts_types

    def _get_each_val_word_num(self):
        result = []
        for stack_ty in self.stack_types:
            result.append(decide_word_num(stack_ty))
        return result

    def _get_offsets_of_stack_types(self, stack_types: list[str], start_memory_offset: int):
        operand_num = len(stack_types)
        offsets = []
        memory_start_idx = start_memory_offset
        for i in range(operand_num):
            cur_type = stack_types[operand_num - i - 1]
            word_num = decide_word_num(cur_type)
            offsets.append(memory_start_idx)
            memory_start_idx += word_num * 4
        return offsets
    # ===== * ===== store related functions ===== * =====

    def _gen_store_insts(self):
        cur_stack_types = self.to_save_stack_types
        data_insts = []
        reversed_stack_types = cur_stack_types[::-1]
        start_memory_offset = self.memory_start_idx
        offsets = self._get_offsets_of_stack_types(
            cur_stack_types, start_memory_offset)
        for offset, stack_ty in zip(offsets, reversed_stack_types):
            store_insts = get_insts_store_stack_val(
                offset, stack_ty, self.idx_manager)
            data_insts.extend(store_insts)
        return data_insts

    # ===== * ===== retrieve related functions ===== * =====
    def _gen_retrieve_insts(self):
        # traceback.print_stack()
        to_retrieve_types = self.stack_types
        # print('VVVVV to_retrieve_types', len(to_retrieve_types), to_retrieve_types)
        start_offset = self.memory_start_idx + self.expected_head_byte_size
        offsets = self._get_offsets_of_stack_types(
            to_retrieve_types, start_offset)
        retrieve_insts = []
        for offset, stack_ty in zip(offsets, to_retrieve_types):
            retrieve_insts.append(get_mem_offset_inst(offset))
            retrieve_insts.append(get_load_inst(stack_ty))
        return retrieve_insts

    # ===== * ===== head related functions ===== * =====
    @property
    def expected_head_byte_size(self):
        return self.expected_head_insts_len * 4

    @property
    def expected_head_insts_len(self):
        return sum(self.expected_head_insts_word_nums)

    @property
    def expected_head_insts_types(self):
        # Compact header is a single i32 word (packed marker+type+idx),
        # emitted by `probe.get_insts()`.
        return ['i32']

    @property
    def expected_head_insts_word_nums(self):
        return [decide_word_num(stack_ty) for stack_ty in self.expected_head_insts_types]

    def _get_probe_insts(self, probe: ValueProbeId):
        return probe.get_insts()

    def _gen_head_insts(self, probe: ValueProbeId):
        # Header is fully contained in probe.get_insts() (one packed i32).
        return []

    # ===== * ===== the other functions ===== * =====

    def _gen_retrieve_memory_insts(self):
        insts = get_n_restore_memory_insts(
            self.new_global_base_idx, self.memory_start_idx, self.to_save_total_word_num)
        return insts

    def _gen_save_memory_insts(self):
        insts = get_n_save_memory_insts(
            self.new_global_base_idx, self.memory_start_idx, self.to_save_total_word_num)
        return insts

    def _gen_counter_addr_insts(self) -> list[Inst]:
        if self.probe_output_counter_start_idx is None:
            raise ValueError('probe_output_counter_start_idx is required when max_output_time is used')

        if self.dynamic_probe_id_local_idx is None:
            return [
                InstFactory.gen_binary_info_inst_high_single_imm(
                    'i32.const',
                    self.probe_output_counter_start_idx + int(self.probe.idx)
                )
            ]

        return [
            InstFactory.gen_binary_info_inst_high_single_imm('local.get', self.dynamic_probe_id_local_idx),
            InstFactory.gen_binary_info_inst_high_single_imm('i32.const', 10),
            InstFactory.opcode_inst('i32.shr_u'),
            InstFactory.gen_binary_info_inst_high_single_imm('i32.const', self.probe_output_counter_start_idx),
            InstFactory.opcode_inst('i32.add'),
        ]

    def _gen_call_print_core_insts(self, max_output_time: Optional[int] = None):
        word_num_inst = InstFactory.gen_binary_info_inst_high_single_imm(
            'i32.const', self.to_save_total_byte_num)
        probe_func_inst = InstFactory.gen_binary_info_inst_high_single_imm(
            'call', imm0=self.idx_manager.print_core_probe_func_idx)
        noop_probe_func_inst = InstFactory.gen_binary_info_inst_high_single_imm(
            'call', imm0=self.idx_manager.noop_print_core_probe_func_idx)

        if max_output_time is None:
            return [word_num_inst, probe_func_inst]

        if max_output_time <= 0:
            return [word_num_inst, noop_probe_func_inst]

        cond_if_type = Blocktype(self.idx_manager.print_core_probe_type_idx)
        insts: list[Inst] = [word_num_inst]
        insts.extend(self._gen_counter_addr_insts())
        insts.append(InstFactory.gen_binary_info_inst_high('i32.load8_u', {'offset': 0, 'align': 0}))
        insts.append(InstFactory.gen_binary_info_inst_high_single_imm('i32.const', int(max_output_time)))
        insts.append(InstFactory.opcode_inst('i32.lt_u'))
        insts.append(InstFactory.gen_binary_info_inst_high_single_imm('if', cond_if_type))
        insts.extend(self._gen_counter_addr_insts())
        insts.extend(self._gen_counter_addr_insts())
        insts.append(InstFactory.gen_binary_info_inst_high('i32.load8_u', {'offset': 0, 'align': 0}))
        insts.append(InstFactory.gen_binary_info_inst_high_single_imm('i32.const', 1))
        insts.append(InstFactory.opcode_inst('i32.add'))
        insts.append(InstFactory.gen_binary_info_inst_high('i32.store8', {'offset': 0, 'align': 0}))
        insts.append(probe_func_inst)
        insts.append(InstFactory.opcode_inst('else'))
        insts.append(noop_probe_func_inst)
        insts.append(InstFactory.opcode_inst('end'))
        return insts


def dump_stack_non_ref_probe(
    new_global_base_idx: int,
    memory_start_idx: int,
    stack_types: list[str], 
    idx_manager: ProbeUtilWasmFuncManager,
    probe: ValueProbeId,
    reserve_memory: bool = True,
    retrieve_stack: bool = True,
    max_output_time: Optional[int] = None,
    probe_output_counter_start_idx: Optional[int] = None,
    dynamic_probe_id_local_idx: Optional[int] = None,
):
    gen = StackNonRefProbeGenerator(
        idx_manager,
        new_global_base_idx,
        memory_start_idx,
        stack_types,
        probe,
        probe_output_counter_start_idx=probe_output_counter_start_idx,
        dynamic_probe_id_local_idx=dynamic_probe_id_local_idx,
    )
    return gen.generate_probe(
        reserve_memory=reserve_memory,
        retrieve_stack=retrieve_stack,
        max_output_time=max_output_time,
    ).get_insts()


def get_to_dump_local_idxs(
    all_local_types:list[str],
    considered_local_idxs:Optional[set[int]] = None
) -> list[int]:
    result_idxs = []
    local_num = len(all_local_types)
    for local_idx in range(local_num):
        type_ = all_local_types[local_idx]
        if is_ref_type(type_):
            continue
        if considered_local_idxs is not None and local_idx not in considered_local_idxs:
            continue
        result_idxs.append(local_idx)
    return result_idxs


def dump_local_probe_core(
    new_global_base_idx:int,
    memory_start_idx:int,
    all_local_types:list[str],
    local_idxs:list[int],
    idx_manager:ProbeUtilWasmFuncManager,
    probe: ValueProbeId,
    reserve_memory: bool = True,
    max_output_time: Optional[int] = None,
    probe_output_counter_start_idx: Optional[int] = None,
    dynamic_probe_id_local_idx: Optional[int] = None,
) -> InstSeqWithName:
    stack_types = []
    insts = []
    for local_idx in local_idxs:
        type_ = all_local_types[local_idx]
        
        stack_types.append(type_)
        local_get_inst = InstFactory.gen_binary_info_inst_high_single_imm('local.get', local_idx)
        insts.append(local_get_inst)
    # * 2. dump stack
    
    gen = StackNonRefProbeGenerator(
        idx_manager,
        new_global_base_idx,
        memory_start_idx,
        stack_types,
        probe,
        probe_output_counter_start_idx=probe_output_counter_start_idx,
        dynamic_probe_id_local_idx=dynamic_probe_id_local_idx,
    )
    inst_seq = gen.generate_probe(
        reserve_memory=reserve_memory,
        retrieve_stack=False,
        max_output_time=max_output_time,
    )
    inst_seq.insert_insts(insts, 'local.set', 0)
    return inst_seq


def dump_global_probe_core(
    save_global_base_idx:int,
    probe_memory_start_idx:int,
    all_global_types:list[str],
    mutable_global_idxs:list[int],
    idx_manager:ProbeUtilWasmFuncManager,
    probe: ValueProbeId,
    reserve_memory: bool = True,
    max_output_time: Optional[int] = None,
    probe_output_counter_start_idx: Optional[int] = None,
    dynamic_probe_id_local_idx: Optional[int] = None,
) -> InstSeqWithName:
    stack_types = []
    insts = []
    for value_idx, global_pos in enumerate(mutable_global_idxs):
        type_ = all_global_types[global_pos]
        assert not is_ref_type(type_)
        
        stack_types.append(type_)
        global_get_inst = InstFactory.gen_binary_info_inst_high_single_imm('global.get', global_pos)
        insts.append(global_get_inst)
    # * 2. dump stack
    
    gen = StackNonRefProbeGenerator(
        idx_manager,
        save_global_base_idx,
        probe_memory_start_idx,
        stack_types,
        probe,
        probe_output_counter_start_idx=probe_output_counter_start_idx,
        dynamic_probe_id_local_idx=dynamic_probe_id_local_idx,
    )
    inst_seq = gen.generate_probe(
        reserve_memory=reserve_memory,
        retrieve_stack=False,
        max_output_time=max_output_time,
    )
    inst_seq.insert_insts(insts, 'global.set', 0)
    return inst_seq




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


def infer_type_seq_required_word_num(type_seq:list[str], ignore_ref=False)->int:
    result = []
    for stack_ty in type_seq:
        if ignore_ref and is_ref_type(stack_ty):
            continue
        result.append(decide_word_num(stack_ty))
    result = sum(result)
    return result

def get_insts_store_stack_val(
    memory_offset: int,
    stack_ty: str,
    idx_manager: ProbeUtilWasmFuncManager
) -> list[Inst]:
    insts = [
        get_mem_offset_inst(memory_offset),
        get_call_store_func_inst(stack_ty, idx_manager)
    ]
    return insts


def get_call_store_func_inst(stack_ty: str, idx_manager: ProbeUtilWasmFuncManager) -> Inst:
    if stack_ty == 'i32':
        idx = idx_manager.store_i32_func_idx
    elif stack_ty == 'i64':
        idx = idx_manager.store_i64_func_idx
    elif stack_ty == 'f32':
        idx = idx_manager.store_f32_func_idx
    elif stack_ty == 'f64':
        idx = idx_manager.store_f64_func_idx
    elif stack_ty == 'v128':
        idx = idx_manager.store_v128_func_idx
    else:
        raise ValueError(f'unknown stack type: {stack_ty}')
    return InstFactory.gen_binary_info_inst_high_single_imm('call', imm0=idx)


def get_store_inst(stack_ty) -> ByteImmInst:
    if stack_ty == 'i32':
        store_inst = InstFactory.gen_binary_info_inst_high(
            'i32.store', {'offset': 0, 'align': 2})
    elif stack_ty == 'i64':
        store_inst = InstFactory.gen_binary_info_inst_high(
            'i64.store', {'offset': 0, 'align': 3})
    elif stack_ty == 'f32':
        store_inst = InstFactory.gen_binary_info_inst_high(
            'f32.store', {'offset': 0, 'align': 2})
    elif stack_ty == 'f64':
        store_inst = InstFactory.gen_binary_info_inst_high(
            'f64.store', {'offset': 0, 'align': 3})
    elif stack_ty == 'v128':
        store_inst = InstFactory.gen_binary_info_inst_high(
            'v128.store', {'offset': 0, 'align': 4})
    else:
        raise ValueError(f'unknown stack type: {stack_ty}')
    return store_inst


def get_mem_offset_inst(offset: int) -> ByteImmInst:
    return InstFactory.gen_binary_info_inst_high_single_imm('i32.const', offset)


def get_load_inst(stack_ty: str) -> ByteImmInst:
    if stack_ty == 'i32':
        load_inst = InstFactory.gen_binary_info_inst_high(
            'i32.load', {'offset': 0, 'align': 2})
    elif stack_ty == 'i64':
        load_inst = InstFactory.gen_binary_info_inst_high(
            'i64.load', {'offset': 0, 'align': 3})
    elif stack_ty == 'f32':
        load_inst = InstFactory.gen_binary_info_inst_high(
            'f32.load', {'offset': 0, 'align': 2})
    elif stack_ty == 'f64':
        load_inst = InstFactory.gen_binary_info_inst_high(
            'f64.load', {'offset': 0, 'align': 3})
    elif stack_ty == 'v128':
        load_inst = InstFactory.gen_binary_info_inst_high(
            'v128.load', {'offset': 0, 'align': 4})
    else:
        raise ValueError(f'unknown stack type: {stack_ty}')
    return load_inst


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
