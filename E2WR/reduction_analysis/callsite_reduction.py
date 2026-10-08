#!/usr/bin/env python3

import os
import random
import time
from dataclasses import dataclass
from typing import Any, Callable, Optional
from file_util import copy_file
from reduction_analysis.Instrumentation.CallReturnProbeUtil import gen_callsites_return_stack_probe_descs
from reduction_analysis.Instrumentation.DumpData import DumpDataList
from reduction_analysis.Instrumentation.ValueProbeInstrument import ProbeDesc, ValueProbeManager
from reduction_analysis.Instrumentation.ProbeType import ProbeType
from reduction_analysis.ParserModification import MultiPhaseMutationApplier, WMSnapshot
from reduction_analysis.ParserModificationUtil import FuncInstMutation
from reduction_analysis.ProbDDUtil.ProbDDFactory import ProbDDFactory
from reduction_analysis.ProbDDUtil.adapt_util import get_test_cfg_func_for_dd
from reduction_analysis.ASTInfo.AST import ASTNodeLoc
from reduction_analysis.ReducerCommonConfig import CALLSITE_REDUCTION_INSTRUMENT_TIMEOUT
from reduction_analysis.ReductionDescUtil.Oracle import CheckType, Oracle, call_oracle

from reduction_analysis.callsite_reduction_cal_weights_util import (
    WeithtStrategy,
    calculate_weiths_by_strategy,
)
from reduction_analysis.ReduceUtil.NewInstUtil import get_inst_by_require_ty_const_n
from extract_block_mutator.InstGeneration.InstFactory import InstFactory

from util.debug_util import ValidateCheckType, validate_wasm
from enum import Enum
# class ReplaceValueStrategy

class CallsiteRepStrategy(Enum):
    DISABLE = 0
    TY_ONLY = 1
    VP = 2


class DumpDataListParser:

    def __init__(self, dumped: DumpDataList):
        self.probe_idx_exec_times: dict[int, int] = {}
        self.first_pos_by_probe: dict[int, int] = {}

        for i, one in enumerate(dumped):
            probe_idx = int(one.probe_idx)
            self.first_pos_by_probe.setdefault(probe_idx, int(i))
            self.probe_idx_exec_times[probe_idx] = self.probe_idx_exec_times.get(probe_idx, 0) + 1

    def prepare_calculate_weiths_kwargs(
        self,
        *,
        probe_descs: list[ProbeDesc],
        probe_idx2call_site: dict[int, "CallLikeSite"],
    ) -> dict[str, Any]:

        # Only compute weights for actual callsite probes (exclude EXECUTED probes).
        callsite_probe_descs: list[ProbeDesc] = [p for p in probe_descs if int(p.idx) in probe_idx2call_site]

        # Appeared/triggered probes: those with at least one dumped event.
        appear_probe_idxs: set[int] = {
            int(i) for i, c in self.probe_idx_exec_times.items() if int(c) > 0
        }

        return {
            "probe_descs": callsite_probe_descs,
            "appear_probe_idxs": appear_probe_idxs,
            "first_pos_by_probe": self.first_pos_by_probe,
        }


def prepare_calculate_weiths_kwargs_by_strategy(
    *,
    probe_descs: list[ProbeDesc],
    probe_idx2call_site: dict[int, "CallLikeSite"],
    dumped_parser: DumpDataListParser,
) -> dict[str, Any]:

    return dumped_parser.prepare_calculate_weiths_kwargs(
        probe_descs=probe_descs,
        probe_idx2call_site=probe_idx2call_site,
    )

class _DDEarlyStop(Exception):
    def __init__(self, replaced: set[int]):
        super().__init__("DD early stop")
        self.replaced = replaced



@dataclass(frozen=True)
class CallLikeSite:

    kind: str  # "call" | "call_indirect"
    defined_func_idx: int
    call_inst_idx: int
    callee_type_idx: int
    callee_result_types: list[str]
    callee_func_idx: Optional[int] = None  # only for direct call

    @property
    def probe_loc_after_call(self):
        

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


def _try_get_call_indirect_type_idx(inst) -> Optional[int]:
    if getattr(inst, "opcode_text", None) != "call_indirect":
        return None
    imm = getattr(inst, "imm_part", None)
    if imm is None:
        return None
    # Common encoding: y = typeidx
    y = getattr(imm, "y", None)
    if isinstance(y, int):
        return y
    # Fallback keys seen in other encodings
    for k in ("typeidx", "type_idx", "type"):
        v = getattr(imm, k, None)
        if isinstance(v, int):
            return v
    # Last resort: if it's a single-int payload
    return _try_get_imm_val(imm)


def collect_call_like_sites_from_parser(parser) -> list[CallLikeSite]:
    results: list[CallLikeSite] = []
    for defined_func_idx, wasm_func in enumerate(parser.defined_funcs):
        for inst_idx, inst in enumerate(wasm_func.insts):
            op = inst.opcode_text
            if op == "call":
                callee_func_idx = _try_get_imm_val(getattr(inst, "imm_part", None))
                if callee_func_idx is None:
                    continue
                # if callee_func_idx < 0 or callee_func_idx >= len(parser.func_type_idxs):
                #     continue
                callee_type_idx = parser.func_type_idxs[callee_func_idx]
                # if callee_type_idx < 0 or callee_type_idx >= len(parser.types):
                #     continue
                callee_result_types = list(parser.types[callee_type_idx].result_types)
                results.append(
                    CallLikeSite(
                        kind="call",
                        defined_func_idx=defined_func_idx,
                        call_inst_idx=inst_idx,
                        callee_func_idx=callee_func_idx,
                        callee_type_idx=callee_type_idx,
                        callee_result_types=callee_result_types,
                    )
                )
            elif op == "call_indirect":
                type_idx = _try_get_call_indirect_type_idx(inst)
                if type_idx is None:
                    continue
                if type_idx < 0 or type_idx >= len(parser.types):
                    continue
                callee_result_types = list(parser.types[type_idx].result_types)
                results.append(
                    CallLikeSite(
                        kind="call_indirect",
                        defined_func_idx=defined_func_idx,
                        call_inst_idx=inst_idx,
                        callee_type_idx=type_idx,
                        callee_result_types=callee_result_types,
                        callee_func_idx=None,
                    )
                )
    return results




def _apply_inst_mutations_to_tmp_and_test(
    base_snapshot: WMSnapshot,
    inst_mutations: list[FuncInstMutation],
    tmp_out_path: str,
    base_wasm_path_for_oracle: str,
    oracle_func: Callable,
    DEBUG: bool = False,
    cur_output_path: Optional[str] = None,
) -> bool:
    if not inst_mutations:
        # Important: this helper is used by multiple callers; do not rely on global WASM_PATH.
        # If no mutations are applied, test the provided base wasm path.
        result = bool(call_oracle(oracle_func, base_wasm_path_for_oracle, CheckType.TEST_CHECK))
        if result and cur_output_path is not None:
            if base_wasm_path_for_oracle != cur_output_path:
                copy_file(base_wasm_path_for_oracle, cur_output_path)
        return result

    func_idxs_to_copy = {m.func_idx for m in inst_mutations}
    tmp_snapshot = base_snapshot.copy_for_code_mutations(func_idxs_to_copy)
    applier = MultiPhaseMutationApplier()
    applier.merge_to_snapshot(snapshot=tmp_snapshot, inst_mutations=inst_mutations, type_mutations=[])
    applier.flush_and_encode(snapshot=tmp_snapshot, output_file=tmp_out_path)

    if DEBUG and (not validate_wasm(tmp_out_path, print_detail_reason=True, tag=ValidateCheckType.TEST_CHECK)):
        if  True:
            print(f"[invalid] total_inst_mutations={len(inst_mutations)}")
            # Directly dump the mutations we applied (first N is usually enough).
            # Each FuncInstMutation repr includes func_idx/start_offset/end_offset/new_insts.
            preview_n = min(10, len(inst_mutations))
            print(f"[invalid] inst_mutations[:{preview_n}] =")
            for m in inst_mutations[:preview_n]:
                print(m)
        # invalid must hard-fail (as requested)
        raise Exception("Generated wasm is invalid after applying mutations.")

    result = bool(call_oracle(oracle_func, tmp_out_path, CheckType.TEST_CHECK))
    if result and cur_output_path is not None:
        # Always keep the latest oracle-PASS case at cur_output_path.
        copy_file(tmp_out_path, cur_output_path)
    return result


def _gen_random_call_replacement_mutation_by_type(
    *,
    func_idx: int,
    call_inst_idx: int,
    param_types: list[str],
    result_types: list[str],
) -> FuncInstMutation:


    new_insts = []
    for _ in param_types:
        new_insts.append(InstFactory.opcode_inst("drop"))
    for ty in result_types:
        new_insts.append(get_inst_by_require_ty_const_n(ty))

    return FuncInstMutation(
        func_idx=func_idx,
        start_offset=call_inst_idx,
        end_offset=call_inst_idx + 1,
        new_insts=new_insts,
    )



def dd_try_replace_callsites_interface(
    *,
    base_snapshot: WMSnapshot,
    cand_idx2mutation: dict[int, FuncInstMutation],
    universe: list[int],
    tmp_out_path: str,
    base_wasm_path_for_oracle: str,
    oracle_func: Callable,
    DEBUG: bool = False,
    save_ratio=0.7,
    dd_timeout_s: Optional[float] = None,
    cur_output_path: Optional[str] = None,
    weights: Optional[dict[int, float]] = None,
) -> set[int]:

    accepted_replaced: set[int] = set()
    remaining_to_consider: set[int] = set(universe)

    total_candidates = len(set(universe) | accepted_replaced)
    early_stop_target = int(total_candidates * save_ratio)

    expected_end_time = None
    if dd_timeout_s is not None:
        expected_end_time = time.time() + float(dd_timeout_s)

    best_replaced: set[int] = set()
    if DEBUG:

        baseline_mutations = [cand_idx2mutation[i] for i in accepted_replaced]
        baseline_ok = _apply_inst_mutations_to_tmp_and_test(
            base_snapshot=base_snapshot,
            inst_mutations=baseline_mutations,
            tmp_out_path=tmp_out_path,
            base_wasm_path_for_oracle=base_wasm_path_for_oracle,
            oracle_func=oracle_func,
            DEBUG=DEBUG,
            cur_output_path=cur_output_path,
        )
        if baseline_ok:
            best_replaced = set(accepted_replaced)

        if not remaining_to_consider:
            raise ValueError("No mutation but fails the oracle. There is something wrong")
        # return set(best_replaced)

    cur_universe = list(remaining_to_consider)
    random.shuffle(cur_universe)

    def _reduce_func(to_save_cfg: list[int]) -> bool:
        nonlocal best_replaced
        to_save_set = set(to_save_cfg)
        newly_replaced_this_try = remaining_to_consider - to_save_set
        to_replace = newly_replaced_this_try | accepted_replaced
        inst_mutations = [cand_idx2mutation[i] for i in to_replace]
        print(
                    "Try replace callsites: "
                    f"new={len(newly_replaced_this_try)} accepted={len(accepted_replaced)} total={len(to_replace)} "
                    f"(remaining_pool={len(remaining_to_consider)})"
                )
            
        result = _apply_inst_mutations_to_tmp_and_test(
            base_snapshot=base_snapshot,
            inst_mutations=inst_mutations,
            tmp_out_path=tmp_out_path,
            base_wasm_path_for_oracle=base_wasm_path_for_oracle,
            oracle_func=oracle_func,
            DEBUG=DEBUG,
            cur_output_path=cur_output_path,
        )

        if result:
            best_replaced = set(to_replace)
        # Early-stop (single-round): once we have any PASS case that replaces
        # at least `early_stop_target` candidates, stop further DD search.
        if result and len(to_replace) >= early_stop_target:
            print(
                "DD early stop (single-round): "
                f"replaced={len(to_replace)}/{total_candidates} target={early_stop_target}"
            )
            raise _DDEarlyStop(set(to_replace))
        return result

    test_cfg = get_test_cfg_func_for_dd(_reduce_func)
    dd = ProbDDFactory.get_default_probdd(test_cfg, task_id=f"CallSiteDD")

    # Some DD implementations accept expected_end_time and/or weights.
    try:
        minimal_to_save = dd(cur_universe, weights=weights, expected_end_time=expected_end_time)
    except _DDEarlyStop as e:
        return set(e.replaced)

    newly_replaced = remaining_to_consider - set(minimal_to_save)
    print(
        "DD round summary: "
        f"remaining_in={len(cur_universe)} "
        f"minimal_to_save={len(minimal_to_save)} newly_replaced={len(newly_replaced)} "
        f"accepted_before={len(accepted_replaced)} accepted_after={len(accepted_replaced | newly_replaced)}"
    )
    accepted_replaced |= newly_replaced

    return set(best_replaced)


def replace_calls_interface_random_replacement(
    *,
    cur_input_path: str,
    cur_output_path: str,
    tmp_dir: str,
    oracle_func: Callable,
    input_snapshot: WMSnapshot,
    instrument_manager: ValueProbeManager,
    dd_timeout_s: Optional[float] = None,
    DEBUG: bool = False,
    skip_void_calls: bool = True,
    save_ratio: float = 0.7,
):
    t0 = time.time()
    os.makedirs(tmp_dir, exist_ok=True)

    # NOTE: If there are no callsites/candidates, this function is a no-op.
    base_snapshot = input_snapshot
    parser = input_snapshot.parser

    call_sites = collect_call_like_sites_from_parser(parser)
    if not call_sites:
        if cur_input_path != cur_output_path:
            copy_file(cur_input_path, cur_output_path)
        return input_snapshot, set()

    probe_descs, probe_idx2call_site = gen_callsites_return_stack_probe_descs(
        call_sites,
        # Void-return callsites must still be considered: their random
        # replacement is just parameter drops with no produced values.
        skip_void_calls=False,
    )
    if cur_input_path != cur_output_path:
        copy_file(cur_input_path, cur_output_path)

    if not probe_descs:
        return input_snapshot, set()

    callsite_probe_descs: list[ProbeDesc] = [p for p in probe_descs if int(p.idx) in probe_idx2call_site]
    callsite_exec_probe_descs: list[ProbeDesc] = [
        ProbeDesc(
            idx=int(probe_desc.idx),
            loc=probe_desc.loc,
            probe_types={ProbeType.EXECUTED},
        )
        for probe_desc in callsite_probe_descs
    ]

    dumped = instrument_manager.instrument_multiple_places_and_get_result(
        snapshot=base_snapshot,
        probe_descs=callsite_exec_probe_descs,
        max_global_num=200,
        max_output_time=None,
        allocated_time=CALLSITE_REDUCTION_INSTRUMENT_TIMEOUT,
    )
    dumped_parser = DumpDataListParser(dumped)

    weight_kwargs = prepare_calculate_weiths_kwargs_by_strategy(
        probe_descs=callsite_exec_probe_descs,
        probe_idx2call_site=probe_idx2call_site,
        dumped_parser=dumped_parser,
    )
    probdd_weights = calculate_weiths_by_strategy(WeithtStrategy.LAST_APPEAR, **weight_kwargs)

    cand_idx2mutation: dict[int, FuncInstMutation] = {}
    for probe_desc in callsite_probe_descs:
        cs = probe_idx2call_site.get(probe_desc.idx)
        if cs is None:
            continue

        call_type = parser.types[cs.callee_type_idx]
        param_types = list(call_type.param_types)
        result_types = list(call_type.result_types)

        # call_indirect additionally consumes the table element index (i32).
        if cs.kind == "call_indirect":
            param_types = list(param_types) + ["i32"]

        mutation = _gen_random_call_replacement_mutation_by_type(
            func_idx=cs.defined_func_idx,
            call_inst_idx=cs.call_inst_idx,
            param_types=param_types,
            result_types=result_types,
        )
        cand_idx2mutation[probe_desc.idx] = mutation

    universe = sorted(list(cand_idx2mutation.keys()))
    if not universe:
        return input_snapshot, set()


    dd_tmp_path = os.path.join(tmp_dir, "dd_tmp_replace_calls_random.wasm")
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
        save_ratio=save_ratio,
        weights=probdd_weights,
    )
    print(
        f"Reducing CallSites(RandomReplacement) done, taking {time.time() - t0:.1f}s, "
        f"replaced {len(replaced)}/{len(universe)} callsites."
    )
    return WMSnapshot.from_path(cur_output_path), set(replaced)
