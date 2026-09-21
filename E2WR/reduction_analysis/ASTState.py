from pathlib import Path
from typing import Union
from reduction_analysis.ParserModification import WMSnapshot,Encoder
from .ASTInfo.ASTInfo import ASTInfo


class ASTState:
    def __init__(self,
                 snapshot: WMSnapshot,
                 ast_info: ASTInfo,
                 ):
        self.snapshot: WMSnapshot = snapshot
        self.ast_info = ast_info

    @property
    def parser(self):
        return self.snapshot.parser

    def remove_a_func(self, func_idx:int):
        self.ast_info.remove_a_func(func_idx)


    def to_file(self, path:Union[str, Path]):
        # Assumes mutations (if any) have been flushed into snapshot by the caller
        Encoder().encode_without_mutation(self.snapshot, str(path))

    def copy(self):
        snapshot = self.snapshot.copy()
        ast_info = ASTInfo.from_parser(self.parser)
        return ASTState(snapshot, ast_info)

    @classmethod
    def from_path(cls, path:Union[str, Path]):
        snapshot = WMSnapshot.from_path(str(path))
        ast_info = ASTInfo.from_parser(snapshot.parser)
        return cls(snapshot, ast_info)
    @classmethod
    def from_snapshot(cls, snapshot:WMSnapshot):
        ast_info = ASTInfo.from_parser(snapshot.parser)
        return cls(snapshot, ast_info)
