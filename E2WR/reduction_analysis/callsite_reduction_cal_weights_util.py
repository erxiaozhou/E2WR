#!/usr/bin/env python3

from __future__ import annotations

from math import inf
from enum import Enum

from reduction_analysis.Instrumentation.ValueProbeInstrument import ProbeDesc


class WeithtStrategy(Enum):
    LAST_APPEAR = 3


def calculate_weiths_by_strategy(
    strategy: WeithtStrategy,
    *,
    probe_descs: list[ProbeDesc] | None = None,
    # Appeared/triggered probe idxs: idx not in this set should always be pushed to the end.
    appear_probe_idxs: set[int] | None = None,
    # Precomputed dump-derived views (so callers can avoid iterating DumpDataList repeatedly)
    first_pos_by_probe: dict[int, int] | None = None,
) -> dict[int, float]:

    def _need(name: str, val):
        if val is None:
            raise ValueError(f"calculate_weiths_by_strategy: `{name}` is required for strategy={strategy}")
        return val

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

    if strategy == WeithtStrategy.LAST_APPEAR:
        _probe_descs = _need("probe_descs", probe_descs)
        _first_pos_by_probe = _need("first_pos_by_probe", first_pos_by_probe)
        weights = _build_probdd_weights_by_first_appearance_position(
            probe_descs=_probe_descs,
            first_pos_by_probe=_first_pos_by_probe,
        )
        return _finalize(weights)
    raise ValueError(f"Unknown strategy: {strategy}")


def _build_probdd_weights_by_first_appearance_position(
    *,
    probe_descs: list[ProbeDesc],
    first_pos_by_probe: dict[int, int],
) -> dict[int, float]:
    weights: dict[int, float] = {}
    for probe_desc in probe_descs:
        idx = int(probe_desc.idx)
        pos = first_pos_by_probe.get(idx)
        if pos is None:
            weights[idx] = 0.0
        else:
            weights[idx] = float(pos + 1)
    return weights
