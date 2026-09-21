from typing import Any, Generator, Optional
from ..ASTInfo.AST import ASTNodeLoc
from .ProbeType import ValueProbeId


class ValueProbeInfo:
    def __init__(self,
                 pos2probe: Optional[dict[ASTNodeLoc, ValueProbeId]] = None
                 ):
        if pos2probe is None:
            pos2probe = {}
        self._pos2probe = pos2probe
        self._probe_id2pos = None

    @property
    def probe_id2pos(self):
        if self._probe_id2pos is None:
            self._probe_id2pos = {v: k for k, v in self._pos2probe.items()}
        return self._probe_id2pos

    def get_probe_pos(self, probe_id: ValueProbeId):
        return self.probe_id2pos[probe_id]

    @property
    def all_probes(self):
        return set(self._pos2probe.values())

    @property
    def instrumented_func_idxs(self):
        idxs = set()
        for loc, probe_id in self._pos2probe.items():
            idxs.add(loc.func_idx)
        return idxs

    def get_pose_and_probe_id_as_iter(self) -> Generator[tuple[int, int, ValueProbeId], Any, None]:
        for loc, probe_id in self._pos2probe.items():
            yield loc.func_idx, loc.inst_idx, probe_id

    def register_probe_pos(self, loc: ASTNodeLoc, probe_id: ValueProbeId):
        self._pos2probe[loc] = probe_id
        self._probe_id2pos = None

    def get_reversed_probe_pos_in_defined_func(self,
                                               target_defined_func_idx: int) -> list[tuple[int, Any]]:
        result = []
        for func_idx, inst_idx, probe_id in self.get_pose_and_probe_id_as_iter():
            if func_idx == target_defined_func_idx:
                result.append((inst_idx, probe_id))
        result.sort(key=lambda x: x[0], reverse=True)
        return result
