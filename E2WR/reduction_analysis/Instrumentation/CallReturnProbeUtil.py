from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable, Optional

from extract_block_mutator.WasmParser import WasmParser
from extract_block_mutator.InstUtil.Inst import Inst
from reduction_analysis.ASTInfo.AST import ASTNodeLoc
from reduction_analysis.Instrumentation.ProbeType import ProbeType
from reduction_analysis.Instrumentation.ValueProbeInstrument import ProbeDesc


@dataclass(frozen=True)
class CallSiteInfo:
    defined_func_idx: int  # index into parser.defined_funcs
    call_inst_idx: int  # 0-based index into wasmFunc.insts
    callee_func_idx: int  # module-level function index (includes imports)
    callee_type_idx: int  # index into parser.types
    callee_result_types: list[str]  # result types (bottom->top)

    @property
    def probe_loc_after_call(self) -> ASTNodeLoc:
        # ValueProbeManager inserts probe insts at insts[loc.inst_idx:loc.inst_idx]
        # so inst_idx=call_inst_idx+1 means "right after the call".
        return ASTNodeLoc(self.defined_func_idx, self.call_inst_idx + 1)


def _try_get_imm_val(imm_part) -> Optional[int]:
    if imm_part is None:
        return None
    if isinstance(imm_part, int):
        return imm_part
    val = getattr(imm_part, "val", None)
    if isinstance(val, int):
        return val
    return None


def try_get_direct_call_target_func_idx(inst: Inst) -> Optional[int]:
    if inst.opcode_text != "call":
        return None
    return _try_get_imm_val(inst.imm_part)


def infer_callee_result_types(parser: WasmParser, callee_func_idx: int) -> Optional[list[str]]:

    if callee_func_idx < 0 or callee_func_idx >= len(parser.func_type_idxs):
        return None
    type_idx = parser.func_type_idxs[callee_func_idx]
    if type_idx < 0 or type_idx >= len(parser.types):
        return None
    return list(parser.types[type_idx].result_types)


def collect_direct_call_sites(parser: WasmParser) -> list[CallSiteInfo]:
    results: list[CallSiteInfo] = []
    for defined_func_idx, wasm_func in enumerate(parser.defined_funcs):
        insts = wasm_func.insts
        for call_inst_idx, inst in enumerate(insts):
            callee_func_idx = try_get_direct_call_target_func_idx(inst)
            if callee_func_idx is None:
                continue
            if callee_func_idx < 0 or callee_func_idx >= len(parser.func_type_idxs):
                continue
            callee_type_idx = parser.func_type_idxs[callee_func_idx]
            if callee_type_idx < 0 or callee_type_idx >= len(parser.types):
                continue
            callee_result_types = list(parser.types[callee_type_idx].result_types)

            results.append(
                CallSiteInfo(
                    defined_func_idx=defined_func_idx,
                    call_inst_idx=call_inst_idx,
                    callee_func_idx=callee_func_idx,
                    callee_type_idx=callee_type_idx,
                    callee_result_types=callee_result_types,
                )
            )
    return results
def gen_callsites_return_stack_probe_descs(
    call_sites: Iterable[Any],
    *,
    skip_void_calls: bool = True,
) -> tuple[list[ProbeDesc], dict[int, Any]]:
    start_idx: int = 0
    probe_descs: list[ProbeDesc] = []
    probe_idx2call_site: dict[int, Any] = {}
    for cs in call_sites:
        result_types = list(cs.callee_result_types)
        if skip_void_calls and len(result_types) == 0:
            continue
        loc = cs.probe_loc_after_call
        probe_idx = int(start_idx) + len(probe_descs)
        probe_idx2call_site[int(probe_idx)] = cs
        probe_descs.append(
            ProbeDesc(
                idx=int(probe_idx),
				loc=loc,
                probe_types={ProbeType.STACK},
                specified_stack_types=result_types,
            )
        )

    return probe_descs, probe_idx2call_site
