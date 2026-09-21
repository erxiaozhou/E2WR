#!/usr/bin/env python3

from __future__ import annotations

from math import inf
import random
from typing import TYPE_CHECKING
from enum import Enum

from reduction_analysis.Instrumentation.ValueProbeInstrument import ProbeDesc
from reduction_analysis.ASTInfo.AST import ASTNodeLoc

if TYPE_CHECKING:
    from reduction_analysis.callsite_reduction import CallLikeSite


class WeithtStrategy(Enum):
    RANDOM = 1
    # 
    FIRST_APPEAR = 2
    LAST_APPEAR = 3
    # 
    CALLEE_INST_COUNT_MOST = 4
    CALLEE_INST_COUNT_LEAST = 5
    # 
    DUMP_OCCURRENCE_COUNT_MOST = 6
    DUMP_OCCURRENCE_COUNT_LEAST = 7
    #
    CALL_FRAMEWORK_MOST = 8
    CALL_FRAMEWORK_LEAST = 9


def calculate_weiths_by_strategy(
    strategy: WeithtStrategy,
    *,
    # Common parameters
    parser=None,
    probe_descs: list[ProbeDesc] | None = None,
    probe_idx2call_site: dict[int, "CallLikeSite"] | None = None,
    # Appeared/triggered probe idxs: idx not in this set should always be pushed to the end.
    appear_probe_idxs: set[int] | None = None,
    # Precomputed dump-derived views (so callers can avoid iterating DumpDataList repeatedly)
    probe_idx_exec_times: dict[int, int] | None = None,
    first_pos_by_probe: dict[int, int] | None = None,
    # For CALL_FRAMEWORK_* strategies
    callsite_probe_idx2loc: dict[int, ASTNodeLoc] | None = None,
    callsite_loc2_disappeared_count: dict[ASTNodeLoc, int] | None = None,
) -> dict[int, float]:
  

    def _need(name: str, val):
        if val is None:
            raise ValueError(f"calculate_weiths_by_strategy: `{name}` is required for strategy={strategy}")
        return val

    # A small helper to flip preference: higher weight wins tie-break.
    def _reverse_if_needed(weights: dict[int, float], reverse: bool) -> dict[int, float]:
        return get_reverse_weights(weights) if reverse else weights

    def _finalize(weights: dict[int, float]) -> dict[int, float]:
        # Make sure every probe_desc idx has a weight entry.
        if probe_descs is not None:
            for p in probe_descs:
                weights.setdefault(int(p.idx), 0.0)

        # Push non-appeared probes to the end.
        if appear_probe_idxs is not None:
            appeared = set(int(x) for x in appear_probe_idxs)
            for idx in list(weights.keys()):
                if int(idx) not in appeared:
                    weights[int(idx)] = -inf
        return weights

    if strategy == WeithtStrategy.RANDOM:
        # Deterministic: return all-zeros weights.
        if probe_descs is None:
            return {}
        return _finalize({int(p.idx): random.random() for p in probe_descs})

    if strategy in (WeithtStrategy.FIRST_APPEAR, WeithtStrategy.LAST_APPEAR):
        _probe_descs = _need("probe_descs", probe_descs)
        _first_pos_by_probe = _need("first_pos_by_probe", first_pos_by_probe)
        # FIRST_APPEAR: prefer earlier appearance => reverse=True
        # LAST_APPEAR: prefer later appearance => reverse=False
        reverse = (strategy == WeithtStrategy.FIRST_APPEAR)
        weights = _build_probdd_weights_by_first_appearance_position(
            probe_descs=_probe_descs,
            first_pos_by_probe=_first_pos_by_probe,
            reverse=bool(reverse),
        )
        return _finalize(weights)

    if strategy in (
        WeithtStrategy.CALLEE_INST_COUNT_MOST,
        WeithtStrategy.CALLEE_INST_COUNT_LEAST,
    ):
        _parser = _need("parser", parser)
        _probe_descs = _need("probe_descs", probe_descs)
        _probe_idx2call_site = _need("probe_idx2call_site", probe_idx2call_site)
        weights = _build_probdd_weights_by_callee_inst_counts(
            parser=_parser,
            probe_descs=_probe_descs,
            probe_idx2call_site=_probe_idx2call_site,
        )
        weights = _reverse_if_needed(weights, reverse=(strategy == WeithtStrategy.CALLEE_INST_COUNT_LEAST))
        return _finalize(weights)

    if strategy in (
        WeithtStrategy.DUMP_OCCURRENCE_COUNT_MOST,
        WeithtStrategy.DUMP_OCCURRENCE_COUNT_LEAST,
    ):
        _probe_descs = _need("probe_descs", probe_descs)
        _probe_idx_exec_times = _need("probe_idx_exec_times", probe_idx_exec_times)
        base = _build_probdd_weights_by_dump_occurrence_counts(
            probe_descs=_probe_descs,
            probe_idx_exec_times=_probe_idx_exec_times,
        )
        if strategy == WeithtStrategy.DUMP_OCCURRENCE_COUNT_LEAST:
            base = get_reverse_weights(base)
        return _finalize(base)

    if strategy in (
        WeithtStrategy.CALL_FRAMEWORK_MOST,
        WeithtStrategy.CALL_FRAMEWORK_LEAST,
    ):
        _probe_descs = _need("probe_descs", probe_descs)
        _callsite_probe_idx2loc = _need("callsite_probe_idx2loc", callsite_probe_idx2loc)
        _loc2cnt = _need("callsite_loc2_disappeared_count", callsite_loc2_disappeared_count)

        weights: dict[int, float] = {}
        for p in _probe_descs:
            idx = int(p.idx)
            loc = _callsite_probe_idx2loc[idx]
            weights[idx] = float(_loc2cnt.get(loc, 0))

        weights = _reverse_if_needed(weights, reverse=(strategy == WeithtStrategy.CALL_FRAMEWORK_LEAST))
        return _finalize(weights)
    raise ValueError(f"Unknown strategy: {strategy}")



def _build_probdd_weights_by_callee_inst_counts(
    *,
    parser,
    probe_descs: list[ProbeDesc],
    probe_idx2call_site: dict[int, "CallLikeSite"],
) -> dict[int, float]:

    import_func_num = int(getattr(parser, "import_func_num", 0))
    defined_func_num = len(getattr(parser, "defined_funcs", []))

    weights: dict[int, float] = {}
    for probe_desc in probe_descs:
        cs = probe_idx2call_site.get(probe_desc.idx)
        if cs is None:
            continue

        callee_func_idx = cs.callee_func_idx
        if callee_func_idx is None:
            weights[probe_desc.idx] = 0.0
            continue

        # Imported functions have no body in defined_funcs.
        defined_idx = int(callee_func_idx) - import_func_num
        if defined_idx < 0 or defined_idx >= defined_func_num:
            weights[probe_desc.idx] = 0.0
            continue

        callee_defined_func = parser.defined_funcs[defined_idx]
        inst_num = len(getattr(callee_defined_func, "insts", []) or [])
        weights[probe_desc.idx] = float(inst_num)

    return weights


def _build_probdd_weights_by_dump_occurrence_counts(
    *,
    probe_descs: list[ProbeDesc],
    probe_idx_exec_times: dict[int, int],
) -> dict[int, float]:

    weights: dict[int, float] = {}
    for probe_desc in probe_descs:
        idx = int(probe_desc.idx)
        weights[idx] = float(probe_idx_exec_times.get(idx, 0))
    return weights


def _build_probdd_weights_by_first_appearance_position(
    *,
    probe_descs: list[ProbeDesc],
    first_pos_by_probe: dict[int, int],
    reverse =False,
) -> dict[int, float]:
    weights: dict[int, float] = {}
    for probe_desc in probe_descs:
        idx = int(probe_desc.idx)
        pos = first_pos_by_probe.get(idx)
        if pos is None:
            weights[idx] = 0.0
        else:
            weights[idx] = float(pos + 1)
    if reverse:
        weights = get_reverse_weights(weights)
    return weights


def get_reverse_weights(
    weights: dict[int, float],
) -> dict[int, float]:
    return {int(k): -float(v) for k, v in weights.items()}
