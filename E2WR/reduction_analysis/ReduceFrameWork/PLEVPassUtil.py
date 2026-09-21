import hashlib
from reduction_analysis.ASTInfo.AST import ASTINode
from ..ASTState import ASTState
from typing import Optional, Union
from pathlib import Path


class LastExitASTState:
    def __init__(
        self,
        ast_state:Optional[ASTState],
        process_failed_nodes:list[ASTINode],
        
    ):
        self._ast_state = ast_state
        self.process_failed_nodes = process_failed_nodes
        self.cur_hash = None

    @property
    def ast_state(self):
        assert self._ast_state is not None, 'ast_state is None'
        return self._ast_state

    def get_ast_state(self):
        return self.ast_state

    def set_ast_state(self, ast_state:ASTState):
        self._ast_state = ast_state

    @staticmethod
    def cal_file_hash(cur_path:Union[str, Path]):
        return hashlib.md5(Path(cur_path).read_bytes()).hexdigest()

    def update(self, cur_path:Union[str, Path], force=False ):
        cur_hash = self.cal_file_hash(cur_path)
        if cur_hash != self.cur_hash or force:
            self.cur_hash = cur_hash
            self._ast_state = ASTState.from_path(cur_path)
            self.process_failed_nodes.clear()

    def clean_node_lists(self):
        self.process_failed_nodes.clear()

    def is_to_skip_node(self, node:ASTINode):
        if node in self.process_failed_nodes:
            return True
        return False