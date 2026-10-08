from __future__ import annotations

from typing import Any, Iterable

from reduction_analysis.Instrumentation.ProbeType import ProbeType
from reduction_analysis.Instrumentation.ValueProbeInstrument import ProbeDesc


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
            )
        )

    return probe_descs, probe_idx2call_site
