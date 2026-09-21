from __future__ import annotations

from typing import Optional

from extract_block_mutator.InstUtil.Inst import Inst
from extract_block_mutator.WasmParser import WasmParser
from reduction_analysis.Instrumentation.CallReturnProbeUtil import CallSiteInfo
from reduction_analysis.Instrumentation.DumpData import DumpDataList
from reduction_analysis.Instrumentation.ProbeType import ProbeType
from reduction_analysis.ReduceUtil.InstrumentationInstStrategy_not_used import (
    GenStackValueInsts,
    ProcessRefStrategy,
)
from reduction_analysis.WasmSemanticsUtil import is_ref_type


def try_gen_call_return_replacement_insts(
    parser: WasmParser,
    call_site: CallSiteInfo,
    dumped: DumpDataList,
    probe_idx: int,
) -> list[Inst]:

    stack_dump = dumped.get_the_last_dump_data(probe_idx, ProbeType.STACK)
    if stack_dump is None:
        return []

    dump_seq = stack_dump.dump_seq
    if len(dump_seq.all_types) == 0:
        return []

    # If any ref type is involved, there is no concrete dumped value -> do nothing.
    if any(is_ref_type(t) for t in dump_seq.all_types):
        return []

    # Get param count of callee for drop_num.
    if call_site.callee_type_idx < 0 or call_site.callee_type_idx >= len(parser.types):
        return []
    callee_type = parser.types[call_site.callee_type_idx]
    drop_param_num = len(getattr(callee_type, "param_types", []))

    # DumpSeq.values corresponds to the concrete values (non-ref). Here we already
    # ensured all types are non-ref, so it's aligned 1:1.
    if len(dump_seq.values) != len(dump_seq.all_types):
        return []

    strategy = GenStackValueInsts(
        stack_types=list(dump_seq.all_types),
        stack_vals=list(dump_seq.values),
        drop_num=drop_param_num,
        process_ref_strategy=ProcessRefStrategy.Skip,
    )

    if strategy.can_skip():
        return []

    return strategy.get_insts_for_replace()
