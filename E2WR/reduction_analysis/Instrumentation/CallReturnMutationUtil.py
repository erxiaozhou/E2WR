from __future__ import annotations

from typing import Any, Optional

from extract_block_mutator.funcType import funcType
from reduction_analysis.ParserModificationUtil import FuncInstMutation
from reduction_analysis.ReduceUtil.NewInstUtil import padding_input_type_naive
from reduction_analysis.ReduceUtil.InstrumentationInstStrategy_not_used import (
    GenStackValueInsts,
    ProcessRefStrategy,
)
from reduction_analysis.WasmSemanticsUtil import is_ref_type


def gen_call_replacement_mutation(
    *,
    func_idx: int,
    call_inst_idx: int,
    call_type: funcType,
    dumped_vals: Optional[list[Any]],
) -> FuncInstMutation:
    param_types = list(call_type.param_types)
    result_types = list(call_type.result_types)

    # Void call: optionally allow empty replacement only when it has no params.
    if len(result_types) == 0:
        if len(param_types) == 0:
            return FuncInstMutation(
                func_idx=func_idx,
                start_offset=call_inst_idx,
                end_offset=call_inst_idx + 1,
                new_insts=[],
            )
        # Otherwise: drop all params, no results to push.
        return FuncInstMutation(
            func_idx=func_idx,
            start_offset=call_inst_idx,
            end_offset=call_inst_idx + 1,
            new_insts=padding_input_type_naive(param_types, []),
        )

    new_insts = None

    if (
        dumped_vals is not None
        and len(result_types) > 0
        and not any(is_ref_type(t) for t in result_types)
        and len(dumped_vals) == len(result_types)
    ):
        strategy = GenStackValueInsts(
            stack_types=result_types,
            stack_vals=list(dumped_vals),
            drop_num=len(param_types),
            process_ref_strategy=ProcessRefStrategy.Skip,
        )
        if not strategy.can_skip():
            new_insts = strategy.get_insts_for_replace()

    if new_insts is None:
        # Always drop all params (call consumes them), then push random results.
        # We intentionally avoid padding_input_type_naive(param_types, result_types)
        # because it may keep a common prefix and thus fail to consume params.
        new_insts = []
        new_insts.extend(padding_input_type_naive(param_types, []))
        new_insts.extend(padding_input_type_naive([], result_types))

    return FuncInstMutation(
        func_idx=func_idx,
        start_offset=call_inst_idx,
        end_offset=call_inst_idx + 1,
        new_insts=new_insts,
    )
