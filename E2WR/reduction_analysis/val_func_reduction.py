#!/usr/bin/env python3

from __future__ import annotations

import time
from pathlib import Path
from typing import Any, Callable, Optional, Union

from file_util import copy_file
from reduction_analysis.Instrumentation.CallReturnProbeUtil import (
    CallSiteInfo,
    collect_direct_call_sites,
    gen_callsites_return_stack_probe_descs,
)
from reduction_analysis.Instrumentation.DumpData import DumpDataList
from reduction_analysis.Instrumentation.ProbeType import ProbeType
from reduction_analysis.Instrumentation.ValueProbeInstrument import ValueProbeManager
from reduction_analysis.ParserModification import Encoder, MultiPhaseMutationApplier, WMSnapshot
from reduction_analysis.ParserModificationUtil import FuncInstMutation
from reduction_analysis.ProbDDUtil.ProbDDFactory import ProbDDFactory
from reduction_analysis.ProbDDUtil.adapt_util import get_test_cfg_func_for_dd
from reduction_analysis.ReductionDescUtil.Oracle import CheckType, call_oracle
from reduction_analysis.ReduceUtil.InstrumentationInstStrategy_not_used import (
    GenStackValueInsts,
    ProcessRefStrategy,
)
from reduction_analysis.WasmSemanticsUtil import is_ref_type

from util.debug_util import ValidateCheckType, get_validation_info, validate_wasm


def _try_module_funcidx_to_defined(parser, module_func_idx: int) -> Optional[int]:
    if module_func_idx < int(parser.import_func_num):
        return None
    defined_idx = int(module_func_idx) - int(parser.import_func_num)
    if defined_idx < 0 or defined_idx >= len(parser.defined_funcs):
        return None
    return int(defined_idx)

def _gen_func_body_replacement_mutation(
    *,
    parser,
    defined_func_idx: int,
    result_types: list[str],
    dumped_vals: list[Any],
) -> Optional[FuncInstMutation]:
    if len(result_types) != len(dumped_vals):
        return None

    # Reuse existing inst generator to build: (drop 0) + const(return values)
    strategy = GenStackValueInsts(
        stack_types=list(result_types),
        stack_vals=list(dumped_vals),
        drop_num=0,
        process_ref_strategy=ProcessRefStrategy.Skip,
    )
    if strategy.can_skip():
        return None
    new_insts = strategy.get_insts_for_replace()

    ori_insts = parser.defined_funcs[int(defined_func_idx)].insts
    return FuncInstMutation(
        func_idx=int(defined_func_idx),
        start_offset=0,
        end_offset=len(ori_insts),
        new_insts=new_insts,
    )


def _instrument_callsites_and_collect_last_returns(
    *,
    base_snapshot: WMSnapshot,
    instrument_manager: ValueProbeManager,
) -> tuple[dict[int, tuple[list[str], list[Any]]], list[int]]:
  

    parser = base_snapshot.parser
    # Ensure the instrumented output directory exists.
    Path(instrument_manager.instrumented_path).parent.mkdir(parents=True, exist_ok=True)

    callsites: list[CallSiteInfo] = collect_direct_call_sites(parser)

    # Build callsite probes: dump callee results right after the call.
    eligible_callsites: list[CallSiteInfo] = []
    loc2callee_defined: dict[Any, int] = {}
    for cs in callsites:
        callee_defined = _try_module_funcidx_to_defined(parser, int(cs.callee_func_idx))
        if callee_defined is None:
            continue
        if len(cs.callee_result_types) == 0:
            continue
        if any(is_ref_type(t) for t in cs.callee_result_types):
            # Dump excludes ref types; we can't use such returns for replacement.
            continue
        eligible_callsites.append(cs)
        loc2callee_defined[cs.probe_loc_after_call] = int(callee_defined)

    probe_descs, probe_idx2cs = gen_callsites_return_stack_probe_descs(
        eligible_callsites,
        skip_void_calls=False,
    )

    if not probe_descs:
        return {}, []
    # The ValueProbeManager is provided by the caller, so we don't construct it here.

    dumped: DumpDataList = instrument_manager.instrument_multiple_places_and_get_result(
        snapshot=base_snapshot,
        probe_descs=probe_descs,
        max_global_num=200,
    )

    # Iterate in dynamic order; for each callee, keep only the last observed return.
    # Also maintain unique function order by FIRST appearance (append only on first hit).
    func_first_exec_seq: list[int] = []
    seen_funcs: set[int] = set()
    last_returns: dict[int, tuple[list[str], list[Any]]] = {}
    for one in dumped:
        if one.probe_type != ProbeType.STACK:
            continue
        probe_idx = int(one.probe_idx)
        cs = probe_idx2cs.get(probe_idx)
        if cs is None:
            continue
        callee_defined = loc2callee_defined[cs.probe_loc_after_call]
        dumped_vals = list(one.dump_seq.values)
        assert len(dumped_vals) == len(cs.callee_result_types)
        # if len(dumped_vals) != len(cs.callee_result_types):
        #     continue
        last_returns[int(callee_defined)] = (list(cs.callee_result_types), dumped_vals)

        caller_key = int(cs.defined_func_idx)
        if caller_key not in seen_funcs:
            seen_funcs.add(caller_key)
            func_first_exec_seq.append(caller_key)

        callee_key = int(callee_defined)
        if callee_key not in seen_funcs:
            seen_funcs.add(callee_key)
            func_first_exec_seq.append(callee_key)

    return last_returns, func_first_exec_seq


def _apply_func_replacement_mutations_and_test(
    *,
    base_snapshot: WMSnapshot,
    func_idx2mutation: dict[int, FuncInstMutation],
    to_replace_defined_idxs: set[int],
    tmp_out_path: Union[str, Path],
    oracle_func: Callable,
    DEBUG: bool,
) -> bool:
    if not to_replace_defined_idxs:
        return False

    inst_mutations = [func_idx2mutation[i] for i in to_replace_defined_idxs]
    func_idxs_to_copy = {m.func_idx for m in inst_mutations}
    tmp_snapshot = base_snapshot.copy_for_code_mutations(set(func_idxs_to_copy))

    applier = MultiPhaseMutationApplier()
    applier.merge_to_snapshot(snapshot=tmp_snapshot, inst_mutations=inst_mutations, type_mutations=[])
    applier.flush_and_encode(snapshot=tmp_snapshot, output_file=tmp_out_path)

    if DEBUG:
        is_valid = validate_wasm(str(tmp_out_path), tag=ValidateCheckType.TEST_CHECK)
        if not is_valid:
            info_ = get_validation_info(str(tmp_out_path))
            raise Exception(f"{tmp_out_path} is invalid: {info_}")

    ok = bool(call_oracle(oracle_func, str(tmp_out_path), CheckType.TEST_CHECK))
    return ok


def replace_funcs_by_dd_vp(
    tmp_used_path: Union[str, Path],
    oracle_func: Callable,
    input_snapshot: WMSnapshot,
    cur_output_path: str,
    *,
    instrument_manager: ValueProbeManager,
    DEBUG: bool = False,
    timeout: Optional[float] = None,
    # weights: Optional[dict[int, float]] = None,
    proi_func_idxs: Optional[set[int]] = None,
) -> set[int]:

    t0 = time.time()
    tmp_used_path = Path(tmp_used_path)
    # 1) Instrument callsites and collect last returns per callee.
    last_returns, func_exec_seq = _instrument_callsites_and_collect_last_returns(
        base_snapshot=input_snapshot,
        instrument_manager=instrument_manager,
    )
    weights = {}
    for appear_idx, func_idx in enumerate(func_exec_seq):
        # ProbDD tie-break prefers higher weight; later appearance should win.
        weights[int(func_idx)] = float(appear_idx)

    if not last_returns:
        return set()

    parser = input_snapshot.parser

    # 2) Build replacement mutations per function.
    func_idx2mutation: dict[int, FuncInstMutation] = {}
    for defined_func_idx, (result_types, dumped_vals) in last_returns.items():
        if proi_func_idxs is not None and int(defined_func_idx) not in set(proi_func_idxs):
            continue
        func_ty = parser.defined_funcs[int(defined_func_idx)].func_ty
        if DEBUG:
            assert list(func_ty.result_types) == list(result_types)
           
        mutation = _gen_func_body_replacement_mutation(
            parser=parser,
            defined_func_idx=int(defined_func_idx),
            result_types=list(result_types),
            dumped_vals=list(dumped_vals),
        )
        # assert mutation is not None 
        if mutation is None:
            continue
        func_idx2mutation[int(defined_func_idx)] = mutation

    candidates: set[int] = set(func_idx2mutation.keys())
    if not candidates:
        return set()

    print(f"[val_func_reduction] candidates: {len(candidates)}")

    start_time = time.time()
    to_stop_time = None
    if timeout is not None:
        to_stop_time = start_time + float(timeout)

    # Reuse tmp_used_path as the only temporary output during DD.
    all_candidate_idxs = set(candidates)

    def _try_keep(to_save_cfg: list[int]) -> bool:
        if to_stop_time is not None and time.time() > to_stop_time:
            return False
        to_replace = all_candidate_idxs - set(to_save_cfg)
        if not to_replace:
            # No replacement: baseline case.
            return False

        ok = _apply_func_replacement_mutations_and_test(
            base_snapshot=input_snapshot,
            func_idx2mutation=func_idx2mutation,
            to_replace_defined_idxs=set(to_replace),
            tmp_out_path=tmp_used_path,
            oracle_func=oracle_func,
            DEBUG=DEBUG,
        )
        if ok:
            copy_file(tmp_used_path, cur_output_path)
        return ok

    test_config = get_test_cfg_func_for_dd(_try_keep)
    dd = ProbDDFactory.get_default_probdd(test_config, task_id="ValFuncR")
    minimal_to_save = dd(sorted(list(all_candidate_idxs)), weights, to_stop_time)
    replaced = all_candidate_idxs - set(minimal_to_save)

    # cur_output_path is updated on each oracle-PASS attempt; keep it as final output.

    print(f"[val_func_reduction] replaced {len(replaced)}/{len(all_candidate_idxs)} funcs")
    print(f"[val_func_reduction] time: {time.time() - t0:.2f}s")

    return set(replaced)

