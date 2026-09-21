from typing import Optional
from ..ASTInfo.AST import ASTNodeLoc


class SkipProbeScope:
    def __init__(
        self,
        func_idx: int,
        start_idx: Optional[int] = None,
        end_idx: Optional[int] = None
    ):
        self.func_idx = func_idx
        self.start_idx = start_idx
        self.end_idx = end_idx

    def need_skip_pos(self, pos: ASTNodeLoc) -> bool:
        if pos.func_idx != self.func_idx:
            return False
        if self.start_idx is not None and pos.inst_idx < self.start_idx:
            return False
        if self.end_idx is not None and pos.inst_idx > self.end_idx:
            return False
        return True

    def __eq__(self, other):
        return self.func_idx == other.func_idx \
            and self.start_idx == other.start_idx \
            and self.end_idx == other.end_idx

    def __hash__(self):
        return hash((self.func_idx, self.start_idx, self.end_idx))
