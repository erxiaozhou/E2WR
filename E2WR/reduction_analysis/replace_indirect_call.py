#!/usr/bin/env python3

import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

from file_util import copy_file
from extract_block_mutator.InstGeneration.InstFactory import InstFactory
from reduction_analysis.ASTInfo.AST import ASTNodeLoc
from reduction_analysis.Instrumentation.DumpData import DumpDataList
from reduction_analysis.Instrumentation.ProbeType import ProbeType
from reduction_analysis.Instrumentation.ValueProbeInstrument import ProbeDesc, ValueProbeManager
from reduction_analysis.ParserModification import WMSnapshot
from reduction_analysis.ParserModificationUtil import FuncInstMutation
from reduction_analysis.callsite_reduction import dd_try_replace_callsites_interface


@dataclass(frozen=True)
class IndirectCallSite:
    defined_func_idx: int
    call_inst_idx: int
    callee_type_idx: int

    @property
    def probe_loc_before_call(self) -> ASTNodeLoc:
        return ASTNodeLoc(self.defined_func_idx, self.call_inst_idx)


@dataclass
class _CallFrame:
    func_idx: int
    pending_call_indirect_probe_idx: Optional[int] = None


def _try_get_imm_val(imm_part) -> Optional[int]:
    if imm_part is None:
        return None
    if isinstance(imm_part, int):
        return imm_part
    val = getattr(imm_part, "val", None)
    if isinstance(val, int):
        return val
    return None


def _try_get_call_indirect_type_idx(inst) -> Optional[int]:
    if getattr(inst, "opcode_text", None) != "call_indirect":
        return None
    imm = getattr(inst, "imm_part", None)
    if imm is None:
        return None

    y = getattr(imm, "y", None)
    if isinstance(y, int):
        return y

    for k in ("typeidx", "type_idx", "type"):
        v = getattr(imm, k, None)
        if isinstance(v, int):
            return v

    return _try_get_imm_val(imm)


def collect_indirect_call_sites_from_parser(parser) -> list[IndirectCallSite]:
    result: list[IndirectCallSite] = []
    for defined_func_idx, wasm_func in enumerate(parser.defined_funcs):
        for inst_idx, inst in enumerate(wasm_func.insts):
            if getattr(inst, "opcode_text", None) != "call_indirect":
                continue
            type_idx = _try_get_call_indirect_type_idx(inst)
            if type_idx is None:
                continue
            result.append(
                IndirectCallSite(
                    defined_func_idx=defined_func_idx,
                    call_inst_idx=inst_idx,
                    callee_type_idx=type_idx,
                )
            )
    return result


def infer_call_indirect_callee_map_from_executed_trace(
    dumped: DumpDataList,
    *,
    callsite_probe_idx2site: dict[int, IndirectCallSite],
    func_entry_probe_idx2func_idx: dict[int, int],
) -> dict[int, int]:

    call_stack: list[_CallFrame] = []
    callsite_probe_idx2callee_freq: dict[int, dict[int, int]] = {}

    for one in dumped:
        if one.probe_type != ProbeType.EXECUTED:
            continue
        probe_idx = one.probe_idx

        # Function-entry probe.
        if probe_idx in func_entry_probe_idx2func_idx:
            func_idx = func_entry_probe_idx2func_idx[probe_idx]
            if call_stack and call_stack[-1].pending_call_indirect_probe_idx is not None:
                pending_probe_idx = call_stack[-1].pending_call_indirect_probe_idx
                cur = callsite_probe_idx2callee_freq.setdefault(pending_probe_idx, {})
                cur[func_idx] = cur.get(func_idx, 0) + 1
                call_stack[-1].pending_call_indirect_probe_idx = None

            call_stack.append(_CallFrame(func_idx=func_idx, pending_call_indirect_probe_idx=None))
            continue

        # call_indirect-before probe.
        if probe_idx not in callsite_probe_idx2site:
            continue
        site = callsite_probe_idx2site[probe_idx]

        caller_func_idx = site.defined_func_idx
        while call_stack and call_stack[-1].func_idx != caller_func_idx:
            call_stack.pop()

        if not call_stack:
            call_stack.append(_CallFrame(func_idx=caller_func_idx, pending_call_indirect_probe_idx=None))

        # Overwrite stale pending mark (e.g., previous target is imported / no entry probe).
        call_stack[-1].pending_call_indirect_probe_idx = probe_idx

    result: dict[int, int] = {}
    for probe_idx, freq in callsite_probe_idx2callee_freq.items():
        # Deterministic tie-break: freq desc, func_idx asc.
        sorted_items = sorted(freq.items(), key=lambda x: (-x[1], x[0]))
        if len(sorted_items) == 0:
            continue
        result[probe_idx] = sorted_items[0][0]

    return result


def _gen_replace_call_indirect_with_drop_and_call(
    *,
    func_idx: int,
    call_inst_idx: int,
    callee_func_idx: int,
) -> FuncInstMutation:
    new_insts = [
        InstFactory.opcode_inst("drop"),
        InstFactory.gen_binary_info_inst_high_single_imm("call", imm0=callee_func_idx),
    ]
    return FuncInstMutation(
        func_idx=func_idx,
        start_offset=call_inst_idx,
        end_offset=call_inst_idx + 1,
        new_insts=new_insts,
    )


def replace_indirect_calls_interface(
    *,
    cur_input_path: str,
    cur_output_path: str,
    tmp_dir: str,
    oracle_func: Callable,
    input_snapshot: WMSnapshot,
    instrument_manager: ValueProbeManager,
    dd_timeout_s: Optional[float] = None,
    DEBUG: bool = False,
) -> tuple[WMSnapshot, set[int]]:
  

    t0 = time.time()
    tmp_dir_path = Path(tmp_dir)
    tmp_dir_path.mkdir(parents=True, exist_ok=True)

    Path(instrument_manager.instrumented_path).parent.mkdir(parents=True, exist_ok=True)

    base_snapshot = input_snapshot
    parser = input_snapshot.parser

    call_sites = collect_indirect_call_sites_from_parser(parser)
    if cur_input_path != cur_output_path:
        copy_file(cur_input_path, cur_output_path)

    if len(call_sites) == 0:
        return input_snapshot, set()

    probe_descs: list[ProbeDesc] = []
    callsite_probe_idx2site: dict[int, IndirectCallSite] = {}

    for i, site in enumerate(call_sites):
        probe_idx = i
        callsite_probe_idx2site[probe_idx] = site
        probe_descs.append(
            ProbeDesc(
                idx=probe_idx,
                loc=site.probe_loc_before_call,
                probe_types={ProbeType.EXECUTED},
            )
        )

    func_entry_probe_idx2func_idx: dict[int, int] = {}
    entry_probe_start = len(probe_descs)
    for defined_func_idx in range(len(parser.defined_funcs)):
        probe_idx = entry_probe_start + defined_func_idx
        func_entry_probe_idx2func_idx[probe_idx] = defined_func_idx
        probe_descs.append(
            ProbeDesc(
                idx=probe_idx,
                loc=ASTNodeLoc(defined_func_idx, 0),
                probe_types={ProbeType.EXECUTED},
            )
        )

    dumped = instrument_manager.instrument_multiple_places_and_get_result(
        snapshot=base_snapshot,
        probe_descs=probe_descs,
        max_global_num=200,
        only_executed_probe=True,
        allocated_time=30,
        max_output_time=255,
    )

    observed_defined_callee_by_probe_idx = infer_call_indirect_callee_map_from_executed_trace(
        dumped,
        callsite_probe_idx2site=callsite_probe_idx2site,
        func_entry_probe_idx2func_idx=func_entry_probe_idx2func_idx,
    )

    cand_idx2mutation: dict[int, FuncInstMutation] = {}
    for probe_idx, site in callsite_probe_idx2site.items():
        if probe_idx not in observed_defined_callee_by_probe_idx:
            continue
        observed_defined_callee_idx = observed_defined_callee_by_probe_idx[probe_idx]
        callee_func_idx = parser.import_func_num + observed_defined_callee_idx

        # Safety check: function-entry probes use defined-function indices,
        # while `call` immediates and `parser.func_type_idxs` use module-level
        # function indices (imports + defined funcs).
        callee_type_idx = parser.func_type_idxs[callee_func_idx]
        # assert callee_type_idx == site.callee_type_idx

        cand_idx2mutation[probe_idx] = _gen_replace_call_indirect_with_drop_and_call(
            func_idx=site.defined_func_idx,
            call_inst_idx=site.call_inst_idx,
            callee_func_idx=callee_func_idx,
        )

    universe = sorted(list(cand_idx2mutation.keys()))
    if len(universe) == 0:
        return input_snapshot, set()

    dd_tmp_path = str(tmp_dir_path / "dd_tmp_replace_indirect_calls.wasm")
    replaced = dd_try_replace_callsites_interface(
        base_snapshot=base_snapshot,
        cand_idx2mutation=cand_idx2mutation,
        universe=universe,
        tmp_out_path=dd_tmp_path,
        base_wasm_path_for_oracle=cur_input_path,
        oracle_func=oracle_func,
        DEBUG=DEBUG,
        dd_timeout_s=dd_timeout_s,
        cur_output_path=cur_output_path,
        save_ratio=1,
        weights=None,
    )

    print(
        "Replacing call_indirect done, "
        f"taking {time.time() - t0:.1f}s, "
        f"replaced {len(replaced)}/{len(universe)} callsites."
    )

    return WMSnapshot.from_path(cur_output_path), set(replaced)
