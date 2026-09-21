
import logging
from typing import Optional
from extract_block_mutator.InstUtil.Inst import Inst
from extract_block_mutator.InstUtil.InstReqUtil import get_inst_ty_req
from extract_block_mutator.WasmParser import WasmParser
from extract_block_mutator.funcType import funcType
from extract_block_mutator.funcTypeFactory import funcTypeFactory
from extract_block_mutator.typeReq import merge_req, typeReq
from reduction_analysis.ASTInfo.AST import ASTINode, ASTNodeLoc, InstsNode, RootNode
from reduction_analysis.ASTInfo.ASTInfo import ASTInfo
from reduction_analysis.Instrumentation.DumpData import DumpDataList, OneDumpData
from reduction_analysis.Instrumentation.ProbeType import ProbeType
from reduction_analysis.Instrumentation.ValueProbeInstrument import ProbeDesc, ValueProbeManager
from reduction_analysis.ParserModification import WMSnapshot
from ..InstsScope import InstsScope


def dump_enter_and_exit_node_can_control_data(
    to_reduce_scope: InstsScope,
    inner_insts: list[Inst],
    snapshot: WMSnapshot,
    instrument_manager: ValueProbeManager,
    max_global_num: int,
    enabled_types:Optional[list[ProbeType]]=None
) -> tuple[dict[ProbeType, OneDumpData], dict[ProbeType, OneDumpData]]:
    parser = snapshot.parser
    if enabled_types is None:
        enabled_types = [ProbeType.STACK, ProbeType.LOCAL, ProbeType.GLOBAL]
    enabled_types = enabled_types
    
    probe0 = ProbeDesc(
        idx=0,
        loc=ASTNodeLoc(to_reduce_scope.func_idx, to_reduce_scope.start_idx),
        probe_types=set(
            enabled_types
        )
    )
    probe1 = ProbeDesc(
        idx=1,
        loc=ASTNodeLoc(to_reduce_scope.func_idx, to_reduce_scope.end_idx),
        probe_types=set(
            enabled_types
        )
    )
   
    considered_local_idxs = _detect_affected_local_idxs(inner_insts)
    dumped_can_control_data: DumpDataList = instrument_manager.instrument_multiple_places_and_get_result(
        snapshot=snapshot,
        probe_descs=[probe0, probe1],
        considered_local_idxs=considered_local_idxs,
        max_global_num=max_global_num
        )
    first_entry_dump_data: dict[ProbeType,
                                OneDumpData] = dumped_can_control_data.get_each_kind_first_dump_data(0)
    last_exit_dump_data: dict[ProbeType,
                              OneDumpData] = dumped_can_control_data.get_each_kind_last_dump_data(1)
    return first_entry_dump_data, last_exit_dump_data


def _detect_affected_local_idxs(
    inner_insts: list[Inst],
)->set[int]:
    local_idxs = set()
    for inst in inner_insts:
        if inst.opcode_text == 'local.set':
            local_idxs.add(inst.imm_part.val)
    return local_idxs
