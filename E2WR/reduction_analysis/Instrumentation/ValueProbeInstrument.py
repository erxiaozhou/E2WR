import time
from typing import Callable, Optional, Iterable
from WasmInfoCfg import ExportType
from reduction_analysis.ReducerCommonConfig import VP_INSTRUMENTATION_TIMEOUT
from util.debug_util import ValidateCheckType, validate_wasm
from extract_block_mutator.DefShell import gen_export_desc, gen_export_funcidx_part
from extract_block_mutator.InstGeneration.InstFactory import InstFactory
from extract_block_mutator.InstUtil.ByteInst import ByteImmInst
from extract_block_mutator.WasmParzerUtil import append_func_core_base
from extract_block_mutator.get_data_shell import get_export_attr, get_global_attr
from ..StackState import StackStatus
from reduction_analysis.ParserModification import WMSnapshot, Encoder
from reduction_analysis.ParserModification import GeneralMutationApplier
from reduction_analysis.ParserModificationUtil import DefinitionMutation, FuncInstMutation, MutationBatch
from WasmInfoCfg import SectionType
from extract_block_mutator.wasmFunc import wasmFunc
from extract_block_mutator.WasmParser import WasmParser
from .DumpData import OneDumpData
from extract_block_mutator.encode.NGDataPayload import DataPayloadwithName
from extract_block_mutator.encode.new_defined_data_type import Blocktype

from .NonRefStackProbe import dump_global_probe_core, dump_local_probe_core, dump_stack_non_ref_probe, get_to_dump_local_idxs
from .ParseDumpData import get_all_raw_bytes_and_then_parse, get_all_raw_bytes_and_then_parse_executed_only, parse_collect_data_core
from ..ASTInfo.AST import ASTINode, ASTNodeLoc
from extract_block_mutator.InstUtil.Inst import Inst
from extract_block_mutator.DefShell import (
    gen_export_memidx_part,
    gen_import_func_desc,
    gen_limit,
    gen_limit1,
)
from .util import generate_global_def
from extract_block_mutator.funcTypeFactory import funcTypeFactory
from extract_block_mutator.get_data_shell import get_elemseg_attr, get_impotr_attr, has_func_idx, get_memory_attr
from WasmInfoCfg import ImportType
from .CodeProbeUtil import exec_and_get_trace
from ..ASTInfo.ASTInfo import ASTInfo
from .CodeProbeUtil import locate_imported_fd_write
from reduction_analysis.InferStackUtil import infer_stack_types_on_probe_loc
from reduction_analysis.WasmSemanticsUtil import stack_has_no_ref, is_ref_type
from .ValueProbeUtil import get_print_core_probe_func, get_empty_print_core_probe_func, get_store_func_by_type, gen_process_stack_contain_ref_func
from .ProbeType import ValueProbeId, ProbeType
from .ProbeUtilWasmFuncManager import ProbeUtilWasmFuncManager
from .DumpData import CanControlData, DumpDataList
from .ValueProbeUtil import dump_stack_containing_ref,  dump_executed_probe
from reduction_analysis.ReductionDescUtil.Oracle import CheckType, call_oracle

class FailedOracleAfterProbeException(Exception):
    pass

class ToTestFuncNotInBinaryException(Exception):
    pass


class ToTestFuncRequireParamsException(Exception):
    pass


def ensure_snapshot_exports_to_test_func(
    snapshot: WMSnapshot,
    value_probe_manager: 'ValueProbeManager',
    require_zero_arity: bool = False,
) -> None:
    parser = snapshot.parser
    to_test_func_name = value_probe_manager.to_test_func_name

    for export in parser.exports:
        if (
            get_export_attr(export, 'attr') == ExportType.func
            and get_export_attr(export, 'name') == to_test_func_name
        ):
            if require_zero_arity:
                func_idx = get_export_attr(export, 'idx')
                assert isinstance(func_idx, int)
                func_type_idx = parser.func_type_idxs[func_idx]
                func_type = parser.types[func_type_idx]
                param_types = list(func_type.param_types)
                if len(param_types) > 0:
                    raise ToTestFuncRequireParamsException(
                        f'to_test_func_name "{to_test_func_name}" requires params: {param_types}. '
                        'Current instrumentation redirects `_start` to that export, so probing is skipped.'
                    )
            return None

    exported_func_names = [
        get_export_attr(export, 'name')
        for export in parser.exports
        if get_export_attr(export, 'attr') == ExportType.func
    ]
    raise ToTestFuncNotInBinaryException(
        f'to_test_func_name "{to_test_func_name}" not found in exports. '
        f'Available exported functions: {exported_func_names}'
    )

class ProbeDesc:
    def __init__(
        self,
        idx: int,
        loc: ASTNodeLoc,
        probe_types: set[ProbeType],
        specified_stack_types:Optional[list[str]]=None
    ):
        self.idx = idx
        self.loc = loc
        self.probe_types = probe_types
        self.specified_stack_types = specified_stack_types
    def __str__(self):
        return f'{self.__class__.__name__}(idx={self.idx}, loc={self.loc}, probe_types={self.probe_types})'

    def __repr__(self):
        return self.__str__()

    def __eq__(self, other):
        return self.idx == other.idx and self.loc == other.loc and self.probe_types == other.probe_types

    def __hash__(self):
        return hash((self.idx, self.loc, frozenset(self.probe_types)))

# def gen_instrument_insts


class BufferConfig:
    def __init__(
        self,
        ori_global_num: int,
        max_new_global_num: int,
        memory_start_idx: int,
        counter_mem_start_idx:int
    ):
        self.ori_global_num = ori_global_num
        self.max_new_global_num = max_new_global_num
        self.memory_start_idx = memory_start_idx
        self.use_new_memory = False
        #
        self.probe_memory_start_idx = memory_start_idx + 12
        self.save_data_global_num = max_new_global_num - 4
        self.save_memory_global_start_idx = ori_global_num + 4
        # Per-function executed bitmap area.
        # One byte per function, 0 means not seen, 0xF0 means seen.
        self.func_exec_bitmap_start_idx = memory_start_idx + counter_mem_start_idx
        self.func_exec_bitmap_seen_value = 0xF0
        # Generic per-probe output counter area (1 byte per probe idx).
        self.probe_output_counter_start_idx = memory_start_idx + counter_mem_start_idx
        # print('[BufferConfig] Counter Start Address', self.func_exec_bitmap_start_idx)

    def set_use_new_memory(self, use_new_memory: bool):
        self.use_new_memory = use_new_memory

    def debug_dump(self):
        print('[BufferConfig] ori_global_num=', self.ori_global_num,
              ' max_new_global_num=', self.max_new_global_num,
              ' memory_start_idx=', self.memory_start_idx,
              ' probe_memory_start_idx=', self.probe_memory_start_idx,
              ' func_exec_bitmap_start_idx=', hex(self.func_exec_bitmap_start_idx),
              ' probe_output_counter_start_idx=', hex(self.probe_output_counter_start_idx),
              ' save_memory_global_start_idx=', hex(self.save_memory_global_start_idx),
              ' use_new_memory=', self.use_new_memory)

def gen_same_type_prob_descs(  # generate the probes for the same type
    locs: list[ASTNodeLoc],
    probe_types: set[ProbeType],
    start_idx: int = 0
) -> list[ProbeDesc]:
    result = []
    for i, loc in enumerate(locs):
        result.append(ProbeDesc(idx=start_idx+i, loc=loc, probe_types=probe_types))
    return result


class ValueProbeManager:
    def __init__(
        self,
        instrumented_path: str,
        to_test_func_name:str,
        DEBUG:bool=False,
        check_func:Optional[Callable]=None
    ):

        self._has_instrumented = False
        self.instrumented_path = instrumented_path
        self.DEBUG = DEBUG
        self.to_test_func_name = to_test_func_name
        self.check_func = check_func

    def instrument_and_get_exec_freq(
        self,
        snapshot: WMSnapshot,
        locs:set[ASTNodeLoc],
        max_global_num:int = 200,
    ) -> dict[ASTNodeLoc, int]:
        probe_descs = []
        idx2loc = {}
        for idx, loc in enumerate(locs):
            probe_descs.append(ProbeDesc(idx=idx, loc=loc, probe_types=set([ProbeType.EXECUTED])))
            idx2loc[idx] = loc
        print('len(probe_descs)', len(probe_descs))
        dumped_result =  self.instrument_multiple_places_and_get_result(
            snapshot=snapshot,
            probe_descs=probe_descs,
            max_global_num=max_global_num,
            only_executed_probe=True
        )
        if len(dumped_result) == 0:
            return {}
        # identify executed probes
        loc2freq:dict[ASTNodeLoc, int] = {}
        for loc in locs:
            loc2freq[loc] = 0
        for one_dump_data in dumped_result:
            assert one_dump_data.probe_type == ProbeType.EXECUTED
            probe_idx = one_dump_data.probe_idx
            loc = idx2loc[probe_idx]
            loc2freq[loc] = loc2freq.get(loc, 0) + 1
        
        return loc2freq

    
    # def instrument_and_get_function_exec_times(self, 
    #     snapshot: WMSnapshot,
    #     max_new_global_num=200,
    # )->set[int]:
    #     # get
    #     raise Exception


    def instrument_and_get_function_level_whether_exec(self, 
        snapshot: WMSnapshot,
        max_new_global_num=200,
        start_offset=0x1000
    )->set[int]:
        func_num = len(snapshot.parser.defined_funcs)
        if func_num == 0:
            return set()
        parser = snapshot.parser
        _, all_memory_start_idx = self._calc_probe_memory_start_idx(parser)
        # Bitmap uses one byte per defined function at all_memory_start_idx + start_offset.
        # Keep it inside the selected 64KiB page to avoid OOB traps.
        if start_offset + func_num >= 65536:
            raise ValueError(
                f'func_num too large for one-page bitmap window: {func_num}, '
                f'need <= {65536 - start_offset - 1}'
            )

        # probe_idx == func_idx for direct mapping.
        probe_descs: list[ProbeDesc] = []
        for func_idx in range(func_num):
            probe_descs.append(
                ProbeDesc(
                    idx=func_idx,
                    loc=ASTNodeLoc(func_idx=func_idx, inst_idx=0),
                    probe_types={ProbeType.EXECUTED},
                )
            )

        dumped_result = self.instrument_multiple_places_and_get_result(
            snapshot=snapshot,
            probe_descs=probe_descs,
            max_global_num=max_new_global_num,
            only_executed_probe=True,
            function_level_exec_once=True,
            counter_mem_start_idx=start_offset,
            require_pass_check=True
        )
        if len(dumped_result) == 0:
            return set()

        executed_func_idxs: set[int] = set()
        for one_dump_data in dumped_result:
            assert one_dump_data.probe_type == ProbeType.EXECUTED
            executed_func_idxs.add(int(one_dump_data.probe_idx))

        all_func_idxs = set(range(func_num))
        unexecuted_func_idxs = all_func_idxs - executed_func_idxs
        return unexecuted_func_idxs

    def _collect_and_parse_trace(self,
                           probe_idx2can_control_data: dict[int, CanControlData],
                           allocated_time=60,
                           only_executed_probe: bool = False
                           ) -> DumpDataList:
        raw_trace = exec_and_get_trace(self.instrumented_path, allocated_time=allocated_time, DEBUG=self.DEBUG)
        if only_executed_probe:
            parsed_results = get_all_raw_bytes_and_then_parse_executed_only(raw_trace, probe_idx2can_control_data)
        else:

            parsed_results: list[OneDumpData] = get_all_raw_bytes_and_then_parse(
                raw_trace, 
                probe_idx2can_control_data,
                debug=self.DEBUG
            )

        return DumpDataList(parsed_results)

    def get_can_control_data_before_loc(
        self,
        parser: WasmParser,
        ast_info: ASTInfo,
        original_global_num: int,
        probe_desc: ProbeDesc,
        considered_local_idxs:Optional[set[int]] = None
    ) -> CanControlData:
        loc = probe_desc.loc
        probe_types = probe_desc.probe_types
        cur_stacks = None
        local_types = None
        all_global_types = None
        mutable_global_idxs = None
        local_idxs = None
        for probe_type in probe_types:
            if probe_type == ProbeType.EXECUTED:
                continue
            if probe_type == ProbeType.STACK:
                if probe_desc.specified_stack_types is not None:
                    cur_stacks = probe_desc.specified_stack_types
                else:
                    cur_stack_state = infer_stack_types_on_probe_loc(loc, parser, ast_info)
                    if cur_stack_state.status == StackStatus.ANY:
                        cur_stacks = []
                    else:
                        cur_stacks = cur_stack_state.rest_types
            elif probe_type == ProbeType.LOCAL:
                local_types = detect_local_loc_types(parser, loc.func_idx)
                local_idxs = get_to_dump_local_idxs(local_types, considered_local_idxs)
                
            elif probe_type == ProbeType.GLOBAL:
                # no const ; no ref
                all_global_types, mutable_global_idxs = detect_mut_global_types(
                    original_global_num, parser)
        return CanControlData(
            cur_stacks, 
            local_types, 
            local_idxs,
            all_global_types, 
            mutable_global_idxs
            )

    def _calc_probe_memory_start_idx(self, parser: WasmParser) -> tuple[bool, int]:
    
        page_size = 65536
        max_pages_noop = 65535

        if parser.import_memory_num > 0:
            raise ValueError('Imported memory is not supported by this instrumentation path')

        old_min_pages: int = 0
        if len(parser.defined_memory_datas) > 0:
            mem0 = parser.defined_memory_datas[0]
            min_ = get_memory_attr(mem0, 'min')
            assert isinstance(min_, int)
            old_min_pages = min_
        need_memory = old_min_pages < max_pages_noop
        need_memory = False
        if need_memory:
            new_base = old_min_pages * page_size
        else:
            # new_base = max(old_min_pages - 1, 0) * page_size
            if old_min_pages <= 1:
                page_base = 0
            else:
                page_base = 1
            new_base = page_base * page_size + 0x1000
        # print(f'New base is : {new_base}, 0x : {new_base:x}')
        # print('old_min_pages:', old_min_pages, 'max_pages_noop:', max_pages_noop)

        return need_memory, new_base

    def instrument_multiple_places_and_get_result(self,
                                                  *,
                                                snapshot: WMSnapshot,
                                                probe_descs: list[ProbeDesc],
                                                max_global_num: int = 200,
                                                considered_local_idxs=None,
                                                allocated_time=VP_INSTRUMENTATION_TIMEOUT,
                                                only_executed_probe=False,
                                                function_level_exec_once: bool = False,
                                                max_output_time: Optional[int] = 255,
                                                counter_mem_start_idx:int = 0x1000,
                                                require_pass_check=False
                                                ) -> DumpDataList:
        if len(probe_descs) == 0:
            return DumpDataList([])

        if max_output_time is not None:
            if max_output_time < 0:
                raise ValueError(f'max_output_time must be >= 0, got {max_output_time}')
            # Counter is one byte per probe.
            if max_output_time > 255:
                raise ValueError(f'max_output_time must be <= 255 when byte-counter is used, got {max_output_time}')

        parser = snapshot.parser
        raw_defined_func_num = len(parser.defined_funcs)
        
        for probe_desc in probe_descs:
            assert raw_defined_func_num > probe_desc.loc.func_idx
        
        # ===== Decide probe memory start idx (byte offset) =====
        # Per requirement: `all_memory_start_idx` is never provided externally.
        need_memory, all_memory_start_idx = self._calc_probe_memory_start_idx(parser)
        # print('all_memory_start_idx:', all_memory_start_idx, hex(all_memory_start_idx))


        # ===== Build CanControlData first (pure analysis, no mutations) =====
        buffer_config = BufferConfig(
            ori_global_num=parser.global_num,
            max_new_global_num=max_global_num,
            memory_start_idx=all_memory_start_idx,
            counter_mem_start_idx=counter_mem_start_idx
        )
        # buffer_config.debug_dump()
        # if self.DEBUG:
        #     buffer_config.debug_dump()
        if max_output_time is not None:
            max_probe_idx = max(int(p.idx) for p in probe_descs)
            # Counter bytes live at all_memory_start_idx + 0x1000.
            # Keep within one 64KiB page window.
            if counter_mem_start_idx + max_probe_idx >= 65536:
                raise ValueError(
                    f'probe idx too large for one-page counter window: max_probe_idx={max_probe_idx}, '
                    f'need <= {65536 - counter_mem_start_idx - 1}'
                )


        parse_executed_only = bool(only_executed_probe)
        ast_info = ASTInfo.from_parser(parser)
        probe_idx2can_control_data = {}
        for probe_desc in probe_descs:
            can_control_data: CanControlData = self.get_can_control_data_before_loc(
                parser,
                ast_info,
                buffer_config.ori_global_num,
                probe_desc,
                considered_local_idxs
            )
            probe_idx2can_control_data[probe_desc.idx] = can_control_data

        # ===== Build mutations (append-only for defs, insert-only for insts) =====
        try:
            ensure_snapshot_exports_to_test_func(
                snapshot,
                self,
                require_zero_arity=True,
            )
            mutation_batch, probe_func_manager = self._build_snapshot_instrumentation_mutations(
                snapshot=snapshot,
                need_update_memory=need_memory,
                probe_descs=probe_descs,
                buffer_config=buffer_config,
                probe_idx2can_control_data=probe_idx2can_control_data,
                considered_local_idxs=considered_local_idxs,
                function_level_exec_once=function_level_exec_once,
                max_output_time=max_output_time,
            )

            
            # Apply mutations on a snapshot copy, then encode.
            func_idxs_to_mutate = {m.func_idx for m in mutation_batch.func_inst_mutations}
            instrumented_snapshot = snapshot.copy_for_code_mutations(func_idxs_to_mutate)
            applier = GeneralMutationApplier()
            modified_sections = applier.apply_mutations_on_snapshot(instrumented_snapshot, mutation_batch)
            encoder = Encoder()
            encoder.encode_v2(instrumented_snapshot, modified_sections, self.instrumented_path)

            if self.DEBUG:
                assert validate_wasm(self.instrumented_path, tag=ValidateCheckType.OTHER), f'self.instrumented_path is {self.instrumented_path}'
            if require_pass_check and self.check_func is not None:
                if not call_oracle(self.check_func, self.instrumented_path, CheckType.SELF_CHECK):
                    raise FailedOracleAfterProbeException(f'Check failed for instrumented module at {self.instrumented_path}')
            # parse collected data
            result = self._collect_and_parse_trace(
                probe_idx2can_control_data, 
                only_executed_probe=parse_executed_only,
                allocated_time=allocated_time
                )
            return result
        except (ToTestFuncNotInBinaryException, ToTestFuncRequireParamsException) as e:
            print(f'Trigger skip probe instrumentation: {e}')
            return DumpDataList([])

    # =========================
    # Snapshot/mutation pipeline
    # =========================

    def _build_snapshot_instrumentation_mutations(
        self,
        *,
        snapshot: WMSnapshot,
        need_update_memory: bool,
        probe_descs: list[ProbeDesc],
        buffer_config: BufferConfig,
        probe_idx2can_control_data: dict[int, CanControlData],
        considered_local_idxs=None,
        function_level_exec_once: bool = False,
        max_output_time: Optional[int] = None,
    ) -> tuple[MutationBatch, ProbeUtilWasmFuncManager]:

        parser = snapshot.parser
        definition_mutations: list[DefinitionMutation] = []
        func_inst_mutations: list[FuncInstMutation] = []

        # ---- 1) import fd_write if needed (append import), and shift indices ----
        old_import_func_num = parser.import_func_num
        fd_write_func_idx = locate_imported_fd_write(parser)
        need_insert_fd_write = fd_write_func_idx is None
        delta_import_func = 1 if need_insert_fd_write else 0

        # if self.DEBUG:
        #     print('[ValueProbe] need_insert_fd_write:', need_insert_fd_write, 'old_import_func_num:', old_import_func_num)

        if need_insert_fd_write:
            fd_write_type = funcTypeFactory.generate_one_func_type_default(['i32', 'i32', 'i32', 'i32'], ['i32'])
            fd_write_typeidx = self._get_or_append_typeidx(parser, fd_write_type, definition_mutations)
            fd_write_func_idx = old_import_func_num
            definition_mutations.append(
                DefinitionMutation(
                    SectionType.Import,
                    len(parser.imports),
                    len(parser.imports),
                    [
                        gen_import_func_desc(
                            module_name='wasi_unstable',
                            entity_name='fd_write',
                            func_type_idx=fd_write_typeidx,
                        )
                    ],
                )
            )
        assert fd_write_func_idx is not None

        new_import_func_num = old_import_func_num + delta_import_func

        if delta_import_func:
            # Shift call/ref.func immediates
            func_inst_mutations.extend(
                self._gen_shift_code_funcidx_mutations(
                    parser.defined_funcs,
                    insertion_point=old_import_func_num,
                    delta=delta_import_func,
                )
            )
            # if self.DEBUG:
            #     print('[ValueProbe] shifted call/ref.func immediates; mutations:', len(func_inst_mutations))
            # Shift exports
            for i, e in enumerate(parser.exports):
                if get_export_attr(e, 'attr') != ExportType.func:
                    continue
                # If we will repoint `_start` later, avoid creating a duplicate mutation
                # for the same export slot by skipping `_start` here.
                if get_export_attr(e, 'name') == '_start' and self.to_test_func_name != '_start':
                    continue
                old_idx = get_export_attr(e, 'idx')
                assert isinstance(old_idx, int)
                if old_idx < old_import_func_num:
                    continue
                new_idx = old_idx + delta_import_func
                definition_mutations.append(
                    DefinitionMutation(
                        SectionType.Export,
                        i,
                        i + 1,
                        [gen_export_desc(get_export_attr(e, 'name'), gen_export_funcidx_part(new_idx))],
                    )
                )
            # Shift start
            if parser.start_sec_data is not None and parser.start_sec_data >= old_import_func_num:
                definition_mutations.append(
                    DefinitionMutation(SectionType.Start, 0, 1, [parser.start_sec_data + delta_import_func])
                )
            # Shift element segments
            for i, seg in enumerate(parser.elem_sec_datas):
                if not has_func_idx(seg):
                    continue
                seg2 = self._shift_one_elem_seg(seg, old_import_func_num, delta_import_func)
                if seg2 is not None:
                    definition_mutations.append(DefinitionMutation(SectionType.Element, i, i + 1, [seg2]))
                    # if self.DEBUG:
                    #     try:
                    #         print('[ValueProbe] shifted elem seg', i, 'inner_name', seg.inner_name.name)
                    #     except Exception:
                    #         print('[ValueProbe] shifted elem seg', i)

        # ---- 2) memory present + exported as "memory" ----
        max_pages_noop = 65535
        inserted_or_extended_memory = False

        if parser.import_memory_num == 0 and len(parser.defined_memory_datas) == 0:
            # No memory at all: insert one page.
            # assert False # ZZZ
            inserted_or_extended_memory = True
            definition_mutations.append(DefinitionMutation(SectionType.Memory, 0, 0, [gen_limit1(1)]))
        else:
            if parser.import_memory_num > 0:
                raise ValueError('Imported memory is not supported by this instrumentation path')
            # Defined memory: bump its limits (+1 min/max) if possible.
            mem0 = parser.defined_memory_datas[0]
            old_min = get_memory_attr(mem0, 'min')
            old_max = get_memory_attr(mem0, 'max')
            assert isinstance(old_min, int)
            # if old_min < max_pages_noop:
            if need_update_memory:
                new_min = old_min + 1
                new_max = (old_max + 1) if isinstance(old_max, int) else None
                inserted_or_extended_memory = True
                # ZZZ
                definition_mutations.append(
                    DefinitionMutation(SectionType.Memory, 0, 1, [gen_limit(new_min, new_max)])
                )
                print('Have memory, extended it by 1 page for probe dumping. old_min:', old_min, 'new_min:', new_min, 'old_max:', old_max, 'new_max:', new_max)

        buffer_config.set_use_new_memory(inserted_or_extended_memory)

        # ---- 3) exports: ensure `memory` and `_start` exist when needed ----
        # NOTE: ParserModification.sort_and_clear_section_mutations de-duplicates by (start,end) scope,
        # so we must append all new exports in a *single* insertion mutation.
        exports_to_append: list = []

        if not self._has_exported_memory(parser.exports):
            exports_to_append.append(gen_export_desc(name='memory', desc=gen_export_memidx_part(0)))

        has_start_export = any(
            get_export_attr(e, 'attr') == ExportType.func and get_export_attr(e, 'name') == '_start'
            for e in parser.exports
        )

        if not has_start_export:
            # If the input wasm has no `_start` export, `wasmtime run` will fail/produce empty output.
            if self.to_test_func_name != '_start':
                to_exec_func_idx = self._get_exported_func_idx_by_name(parser, self.to_test_func_name)
                assert to_exec_func_idx is not None
                to_exec_func_idx = self._shift_idx_if_needed(to_exec_func_idx, old_import_func_num, delta_import_func)
                exports_to_append.append(gen_export_desc('_start', gen_export_funcidx_part(to_exec_func_idx)))
            else:
                raise ValueError('to_test_func_name is _start, but no _start export found; this instrumentation path requires a _start export to work. That indicates the `to_test_func_name` provided is incorrect.')
                # Original behavior: add `_start` as an alias for common entry exports.
                alias_idx = self._get_exported_func_idx_by_name(parser, 'to_test')
                if alias_idx is None:
                    alias_idx = self._get_exported_func_idx_by_name(parser, 'main')
                if alias_idx is None and parser.start_sec_data is not None:
                    alias_idx = parser.start_sec_data
                if alias_idx is not None:
                    alias_idx = self._shift_idx_if_needed(alias_idx, old_import_func_num, delta_import_func)
                    exports_to_append.append(gen_export_desc('_start', gen_export_funcidx_part(alias_idx)))

        if exports_to_append:
            definition_mutations.append(
                DefinitionMutation(
                    SectionType.Export,
                    len(parser.exports),
                    len(parser.exports),
                    exports_to_append,
                )
            )

        # ---- 3) repoint _start if needed ----
        if self.to_test_func_name != '_start':
            to_exec_func_idx = self._get_exported_func_idx_by_name(parser, self.to_test_func_name)
            assert to_exec_func_idx is not None
            to_exec_func_idx = self._shift_idx_if_needed(to_exec_func_idx, old_import_func_num, delta_import_func)
            replaced = False
            for i, e in enumerate(parser.exports):
                if get_export_attr(e, 'attr') == ExportType.func and get_export_attr(e, 'name') == '_start':
                    definition_mutations.append(
                        DefinitionMutation(
                            SectionType.Export,
                            i,
                            i + 1,
                            [gen_export_desc('_start', gen_export_funcidx_part(to_exec_func_idx))],
                        )
                    )
                    replaced = True
                    break

        # ---- 4) append globals for dumping ----
        # Keep the same layout as the original pipeline:
        #   - 3 i32 globals for print_core_probe_func
        #   - (max_global_num - 3) i64 globals, with `save_memory_global_start_idx = ori + 4`
        new_globals: list = []
        new_globals.extend([generate_global_def(True, 'i32') for _ in range(3)])
        new_globals.extend([generate_global_def(True, 'i64') for _ in range(max(0, buffer_config.max_new_global_num - 3))])
        definition_mutations.append(
            DefinitionMutation(
                SectionType.Global,
                len(parser.defined_globals),
                len(parser.defined_globals),
                new_globals,
            )
        )

        # ---- 5) append helper functions (print_core + store funcs + optional ref-stack processors) ----
        helper_funcs: list[wasmFunc] = []
        helper_typeidxs: list[int] = []

        # Print core probe func first (index stable)
        print_core_func = get_print_core_probe_func(
            fd_write_id=fd_write_func_idx,
            g_base=buffer_config.ori_global_num,
            memory_start_idx=buffer_config.probe_memory_start_idx,
        )
        print_core_typeidx = self._get_or_append_typeidx(parser, print_core_func.func_ty, definition_mutations)
        helper_funcs.append(print_core_func)
        helper_typeidxs.append(print_core_typeidx)
        print_core_func_idx = new_import_func_num + len(parser.defined_funcs) + 0

        noop_print_core_func = get_empty_print_core_probe_func()
        helper_funcs.append(noop_print_core_func)
        helper_typeidxs.append(print_core_typeidx)
        noop_print_core_func_idx = new_import_func_num + len(parser.defined_funcs) + 1

        probe_func_manager = ProbeUtilWasmFuncManager(
            print_core_probe_func_idx=print_core_func_idx,
            print_core_probe_type_idx=print_core_typeidx,
            noop_print_core_probe_func_idx=noop_print_core_func_idx,
        )

        # Decide which store funcs are needed across all probes (skip ref types)
        required_store_types: set[str] = {'i32'}
        required_ref_stack_types: set[tuple[str, ...]] = set()
        for probe_desc in probe_descs:
            can_control_data = probe_idx2can_control_data[probe_desc.idx]
            for pt in probe_desc.probe_types:
                if pt == ProbeType.EXECUTED:
                    continue
                if pt == ProbeType.STACK:
                    considered = can_control_data.cur_stack
                    for ty in considered:
                        if is_ref_type(ty):
                            continue
                        required_store_types.add(ty)
                    if not stack_has_no_ref(considered):
                        required_ref_stack_types.add(tuple(considered))
                elif pt == ProbeType.LOCAL:
                    all_local_types = can_control_data.all_local_types
                    for ty in all_local_types:
                        if is_ref_type(ty):
                            continue
                        required_store_types.add(ty)
                elif pt == ProbeType.GLOBAL:
                    all_global_types = can_control_data.all_global_types
                    mutable_idxs = can_control_data.mutable_global_idxs
                    for idx in mutable_idxs:
                        ty = all_global_types[idx]
                        if is_ref_type(ty):
                            continue
                        required_store_types.add(ty)

        store_order = ['i32', 'i64', 'f32', 'f64', 'v128']
        store_type2funcidx: dict[str, int] = {}
        for ty in store_order:
            if ty not in required_store_types:
                continue
            f = get_store_func_by_type(ty)
            helper_funcs.append(f)
            helper_typeidxs.append(self._get_or_append_typeidx(parser, f.func_ty, definition_mutations))
            func_idx = new_import_func_num + len(parser.defined_funcs) + (len(helper_funcs) - 1)
            store_type2funcidx[ty] = func_idx

        for ty, idx in store_type2funcidx.items():
            probe_func_manager.set_store_func_idx(ty, idx)

        # Ref-stack processing helper funcs (only when needed)
        if required_ref_stack_types:
            dummy_probe_id = ValueProbeId(0, ProbeType.STACK)
            for stack_types in sorted(required_ref_stack_types, key=lambda x: repr(x)):
                func_idx = new_import_func_num + len(parser.defined_funcs) + len(helper_funcs)
                probe_func_manager.set_process_stack_contain_ref_func_idx(list(stack_types), func_idx)
                new_func = gen_process_stack_contain_ref_func(
                    buffer_config.save_memory_global_start_idx,
                    buffer_config.probe_memory_start_idx,
                    probe_func_manager,
                    list(stack_types),
                    dummy_probe_id,
                    probe_output_counter_start_idx=buffer_config.probe_output_counter_start_idx,
                    max_output_time=max_output_time,
                )
                helper_funcs.append(new_func)
                helper_typeidxs.append(self._get_or_append_typeidx(parser, new_func.func_ty, definition_mutations))

        # Append to Function/Code sections
        # ZZZ DUMP
        definition_mutations.append(
            DefinitionMutation(
                SectionType.Function,
                len(parser.defined_func_ty_ids),
                len(parser.defined_func_ty_ids),
                helper_typeidxs,
            )
        )
        definition_mutations.append(
            DefinitionMutation(
                SectionType.Code,
                len(parser.defined_funcs),
                len(parser.defined_funcs),
                helper_funcs,
            )
        )

        # ---- 6) generate probe insts (pure) and insert via FuncInstMutation ----
        probe_desc_to_insts = self.generate_probe_insts_for_all_descs(
            buffer_config,
            probe_descs,
            probe_func_manager,
            parser,
            probe_idx2can_control_data,
            function_level_exec_once=function_level_exec_once,
            max_output_time=max_output_time,
        )

        # ZZZ DUMP
        # # IMPORTANT: keep shift mutations before probe insertions at the same index.
        for probe_desc, probe_insts in probe_desc_to_insts.items():
            func_inst_mutations.append(
                FuncInstMutation(
                    func_idx=int(probe_desc.loc.func_idx),
                    start_offset=int(probe_desc.loc.inst_idx),
                    end_offset=int(probe_desc.loc.inst_idx),
                    new_insts=probe_insts,
                )
            )
        # print(func_inst_mutations)
        # func_inst_mutations = []
        

        return MutationBatch(definition_mutations=definition_mutations, func_inst_mutations=func_inst_mutations), probe_func_manager

    def _get_or_append_typeidx(self, parser: WasmParser, target_type, def_ms: list[DefinitionMutation]) -> int:
        for idx, ty in enumerate(parser.types):
            if ty == target_type:
                return idx

        insert_at = len(parser.types)
        pending: list = []
        for m in def_ms:
            if m.sec_type == SectionType.Type and m.start_offset == insert_at and m.end_offset == insert_at:
                pending = list(m.raw_new_definitions)
                break
        for i, ty in enumerate(pending):
            if ty == target_type:
                return insert_at + i
        pending.append(target_type)

        def_ms[:] = [m for m in def_ms if not (m.sec_type == SectionType.Type and m.start_offset == insert_at and m.end_offset == insert_at)]
        def_ms.append(DefinitionMutation(SectionType.Type, insert_at, insert_at, pending))
        return insert_at + len(pending) - 1

    def _has_exported_memory(self, exports: Iterable) -> bool:
        for e in exports:
            if get_export_attr(e, 'attr') == ExportType.mem and get_export_attr(e, 'name') == 'memory':
                return True
        return False

    def _shift_idx_if_needed(self, idx: int, insertion_point: int, delta: int) -> int:
        return idx + delta if idx >= insertion_point else idx

    def _get_exported_func_idx_by_name(self, parser: WasmParser, func_name: str) -> Optional[int]:
        for export in parser.exports:
            if get_export_attr(export, 'attr') == ExportType.func and get_export_attr(export, 'name') == func_name:
                idx = get_export_attr(export, 'idx')
                assert isinstance(idx, int)
                return idx
        return None

    def _gen_shift_code_funcidx_mutations(self, funcs: list[wasmFunc], insertion_point: int, delta: int) -> list[FuncInstMutation]:
        ms: list[FuncInstMutation] = []
        for func_idx, func in enumerate(funcs):
            for inst_idx, inst in enumerate(func.insts):
                if inst.opcode_text in {'call', 'ref.func'}:
                    imm = inst.imm_part.val
                    if isinstance(imm, int) and imm >= insertion_point:
                        new_inst = InstFactory.gen_binary_info_inst_high_single_imm(inst.opcode_text, imm0=imm + delta)
                        ms.append(FuncInstMutation(func_idx, inst_idx, inst_idx + 1, [new_inst]))
        return ms

    def _shift_one_elem_seg(self, seg: DataPayloadwithName, insertion_point: int, delta: int) -> Optional[DataPayloadwithName]:
        seg2 = seg.copy()
        changed = False
        inner_name = seg2.inner_name.name
        if inner_name in {'passive_elem_seg0', 'active_elem_seg0', 'active_elem_seg1', 'declarative_elem_seg0'}:
            funcidxs = seg2.data.get('funcidxs')
            if isinstance(funcidxs, list):
                new_funcidxs = []
                for x in funcidxs:
                    if isinstance(x, int) and x >= insertion_point:
                        new_funcidxs.append(x + delta)
                        changed = True
                    else:
                        new_funcidxs.append(x)
                if changed:
                    seg2.data['funcidxs'] = new_funcidxs
        else:
            exprs = seg2.data.get('exprs')
            if isinstance(exprs, list):
                new_exprs = []
                for expr in exprs:
                    expr2 = expr.copy()
                    inst = getattr(expr2, 'val', None)
                    if isinstance(inst, Inst) and inst.opcode_text == 'ref.func':
                        imm = inst.imm_part.val
                        if isinstance(imm, int) and imm >= insertion_point:
                            new_inst = InstFactory.gen_binary_info_inst_high_single_imm('ref.func', imm0=imm + delta)
                            if 'expr' in expr2.data:
                                expr2.data['expr'] = new_inst
                            elif 'val' in expr2.data:
                                expr2.data['val'] = new_inst
                            changed = True
                    new_exprs.append(expr2)
                if changed:
                    seg2.data['exprs'] = new_exprs
        return seg2 if changed else None
    
    def generate_probe_insts_for_all_descs(self,
                                          buffer_config: BufferConfig,
                                          probe_descs: list[ProbeDesc],
                                          probe_func_manager: ProbeUtilWasmFuncManager,
                                          parser: WasmParser,
                                          probe_idx2can_control_data: dict[int, CanControlData],
                                          function_level_exec_once: bool = False,
                                          max_output_time: Optional[int] = None,
                                          ) -> dict[ProbeDesc, list[Inst]]:
        probe_desc_to_insts = {}
        
        for probe_desc in probe_descs:
            can_control_data = probe_idx2can_control_data[probe_desc.idx]
            
            probe_insts = self.get_probe_insts(
                buffer_config,
                probe_desc,
                probe_func_manager,
                parser,
                can_control_data,
                function_level_exec_once=function_level_exec_once,
                max_output_time=max_output_time,
            )
            # print(f'||||||| probe_desc: {probe_desc} probe_insts', probe_insts)
            probe_desc_to_insts[probe_desc] = probe_insts
            
        return probe_desc_to_insts
    
    def insert_probe_insts(self,
                          parser: WasmParser,
                          probe_desc_to_insts: dict[ProbeDesc, list[Inst]]
                          ) -> None:
       
        func_idx_to_probe_descs = {}
        for probe_desc in probe_desc_to_insts.keys():
            func_idx = probe_desc.loc.func_idx
            if func_idx not in func_idx_to_probe_descs:
                func_idx_to_probe_descs[func_idx] = []
            func_idx_to_probe_descs[func_idx].append(probe_desc)
        
        for func_idx in sorted(func_idx_to_probe_descs.keys(), reverse=True):
            cur_func_probe_descs = sorted(
                func_idx_to_probe_descs[func_idx], 
                key=lambda x: x.loc.inst_idx, 
                reverse=True
            )
            
            for probe_desc in cur_func_probe_descs:
                probe_insts = probe_desc_to_insts[probe_desc]
                parser.defined_funcs[probe_desc.loc.func_idx].insts[probe_desc.loc.inst_idx:probe_desc.loc.inst_idx] = probe_insts

    def get_probe_insts(self,
                     buffer_config: BufferConfig,
                     probe_desc: ProbeDesc,
                     probe_func_manager: ProbeUtilWasmFuncManager,
                     parser: WasmParser,
                     can_control_data: CanControlData,
                     function_level_exec_once: bool = False,
                     max_output_time: Optional[int] = None,
                     ) -> list[Inst]:  # type: ignore
        
        probe_types = probe_desc.probe_types
        probe_idx = probe_desc.idx
        new_insts = []
        for probe_type in probe_types:
            if probe_type == ProbeType.EXECUTED:
                if function_level_exec_once:
                    cur_dump_insts = get_can_execute_insts_once_per_func(
                        buffer_config,
                        probe_func_manager,
                        probe_idx=probe_idx,
                        func_idx=probe_desc.loc.func_idx,
                        max_output_time=max_output_time,
                    )
                else:
                    cur_dump_insts = get_can_execute_insts(
                        buffer_config,
                        probe_func_manager,
                        probe_idx,
                        max_output_time=max_output_time,
                    )
            if probe_type == ProbeType.STACK:
                considered_types = can_control_data.cur_stack
                cur_dump_insts = get_dump_stack_insts(
                    buffer_config,
                    probe_func_manager,
                    considered_types,
                    probe_idx,
                    max_output_time=max_output_time,
                )
            elif probe_type == ProbeType.LOCAL:
                considered_types = can_control_data.all_local_types
                local_idxs = can_control_data.local_idxs
                local_probe_id = ValueProbeId(probe_idx, ProbeType.LOCAL)
                cur_dump_insts = dump_local_probe_core(
                    buffer_config.save_memory_global_start_idx,
                    buffer_config.probe_memory_start_idx,
                    considered_types,
                    local_idxs,
                    probe_func_manager,
                    local_probe_id,
                    reserve_memory=not buffer_config.use_new_memory,
                    max_output_time=max_output_time,
                    probe_output_counter_start_idx=buffer_config.probe_output_counter_start_idx,
                ).get_insts()
            elif probe_type == ProbeType.GLOBAL:
                cur_dump_insts = get_dump_global_insts(
                    buffer_config,
                    probe_func_manager,
                    can_control_data.all_global_types,
                    can_control_data.mutable_global_idxs,
                    probe_idx,
                    max_output_time=max_output_time,
                )
            new_insts.extend(cur_dump_insts)
        return new_insts



def get_dump_stack_insts(
        buffer_config: BufferConfig,
        probe_func_manager: ProbeUtilWasmFuncManager,
        considered_types: list[str],
    probe_idx: int,
    max_output_time: Optional[int] = None,
):
    # print('========IN get_dump_stack_insts======== ')
    # print('considered_types', considered_types, 'probe_idx', probe_idx)
    probe_memory_start_idx = buffer_config.probe_memory_start_idx
    stack_probe_id = ValueProbeId(probe_idx, ProbeType.STACK)
    if not stack_has_no_ref(considered_types):
        dump_stack_insts = dump_stack_containing_ref(
            probe_func_manager,
            considered_types,
            stack_probe_id
        )
        # print(f'Use dump_stack_containing_ref generate dump_stack_insts : {dump_stack_insts}')
    else:
        dump_stack_insts = dump_stack_non_ref_probe(
            buffer_config.save_memory_global_start_idx,
            probe_memory_start_idx,
            considered_types,
            probe_func_manager,
            stack_probe_id,
            reserve_memory=not buffer_config.use_new_memory,
            retrieve_stack=True,
            max_output_time=max_output_time,
            probe_output_counter_start_idx=buffer_config.probe_output_counter_start_idx,
        )
    # print('========OUT get_dump_stack_insts========')
    return dump_stack_insts




def get_can_execute_insts(
        buffer_config: BufferConfig,
        probe_func_manager: ProbeUtilWasmFuncManager,
    probe_idx: int,
    max_output_time: Optional[int] = None,
) -> list[Inst]:
    insts = dump_executed_probe(
        buffer_config.save_memory_global_start_idx,
        buffer_config.probe_memory_start_idx,
        probe_func_manager,
        ValueProbeId(probe_idx, ProbeType.EXECUTED),
        reserve_memory=not buffer_config.use_new_memory,
        max_output_time=max_output_time,
        probe_output_counter_start_idx=buffer_config.probe_output_counter_start_idx,
    )
    return insts


def get_can_execute_insts_once_per_func(
        buffer_config: BufferConfig,
        probe_func_manager: ProbeUtilWasmFuncManager,
        probe_idx: int,
        func_idx: int,
    max_output_time: Optional[int] = None,
) -> list[Inst]:
    reserve_memory = not buffer_config.use_new_memory
    flag_addr = buffer_config.func_exec_bitmap_start_idx + int(func_idx)
    seen_value = int(buffer_config.func_exec_bitmap_seen_value)

    cond_if_type = Blocktype(True)  # empty blocktype

    first_seen_path = dump_executed_probe(
        buffer_config.save_memory_global_start_idx,
        buffer_config.probe_memory_start_idx,
        probe_func_manager,
        ValueProbeId(probe_idx, ProbeType.EXECUTED),
        reserve_memory=reserve_memory,
        max_output_time=max_output_time,
        probe_output_counter_start_idx=buffer_config.probe_output_counter_start_idx,
    )

    insts: list[Inst] = [
        InstFactory.gen_binary_info_inst_high_single_imm('i32.const', flag_addr),
        InstFactory.gen_binary_info_inst_high('i32.load8_u', {'offset': 0, 'align': 0}),
        InstFactory.opcode_inst('i32.eqz'),
        InstFactory.gen_binary_info_inst_high_single_imm('if', cond_if_type),
        InstFactory.gen_binary_info_inst_high_single_imm('i32.const', flag_addr),
        InstFactory.gen_binary_info_inst_high_single_imm('i32.const', seen_value),
        InstFactory.gen_binary_info_inst_high('i32.store8', {'offset': 0, 'align': 0}),
    ]
    insts.extend(first_seen_path)
    insts.append(InstFactory.opcode_inst('end'))
    return insts



def get_dump_global_insts(buffer_config: BufferConfig,
                          probe_func_manager: ProbeUtilWasmFuncManager,
                          all_global_types: list[str],
                          mutable_global_idxs: list[int],
                          probe_idx: int,
                          max_output_time: Optional[int] = None,
                          ):
    probe_memory_start_idx = buffer_config.probe_memory_start_idx
    global_probe_id = ValueProbeId(probe_idx, ProbeType.GLOBAL)
    # 
    to_dump_global_types = [all_global_types[i] for i in mutable_global_idxs]

    dump_global_insts = dump_global_probe_core(
        buffer_config.save_memory_global_start_idx,
        probe_memory_start_idx,
        all_global_types,
        mutable_global_idxs,
        probe_func_manager,
        global_probe_id,
        reserve_memory=not buffer_config.use_new_memory,
        max_output_time=max_output_time,
        probe_output_counter_start_idx=buffer_config.probe_output_counter_start_idx,
    ).get_insts()
    return dump_global_insts


def detect_local_loc_types(parser, func_idx, ulimit: Optional[int] = None):
    cur_func: wasmFunc = parser.defined_funcs[func_idx]
    local_types = [_ for _ in cur_func.local_types]
    if ulimit is not None and len(local_types) > ulimit:
        local_types = local_types[:ulimit]
    return local_types


def detect_mut_global_types(ori_defined_global_num, parser:WasmParser, ulimit: Optional[int] = None) -> tuple[list[str], list[int]]:
    mutable_global_idxs: list[int] = []
    saved_num = 0
    all_global_types = []
    for defined_global_idx, global_def in enumerate(parser.defined_globals):
        if ulimit is not None and saved_num >= ulimit:
            break
        if defined_global_idx >= ori_defined_global_num:
            break
        val_type = get_global_attr(global_def, 'global_val_type')
        all_global_types.append(val_type)
        mutable = get_global_attr(global_def, 'mut')
        if mutable:
            if not is_ref_type(val_type):
                saved_num += 1
                mutable_global_idxs.append(defined_global_idx)
    return all_global_types, mutable_global_idxs
