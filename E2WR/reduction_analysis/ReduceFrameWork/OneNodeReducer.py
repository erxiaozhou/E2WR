from extract_block_mutator.WasmParser import get_parser_from_wasm_path
from ..ASTInfo.AST import ASTINode, BlockNode, IfNode, InstsNode, LoopNode, NodeList
from ..ASTState import ASTState
from typing import Optional
from logging import Logger
from reduction_analysis.ReduceUtil.RNOpParam import RNOpParam
from enum import Enum
from reduction_analysis.ReduceUtil.RewritingUtil.NodeRewriter import NodeRewriter, _tmp_check_ast_update

from .util import TimeoutHandler
from ..ReduceUtil.ShrinkBlock import BlockShrink, IfShrink
from ..ReduceUtil.util import check_parser_match_wasm_file, remove_empty_node_in_nodelist

from reduction_analysis.ReduceUtil.RewritingUtil.OnlyInstSnapshotRewriter import OnlyInstSnapshotRewriter
from ..ReduceUtil.ReduceInsts_V9 import reduce_node_list_by_insts_v9
from .ASTNodePool import ToReduceTask, OneCFNodeReduceTask, NodeListsReduceTask


class NodeReducer:
    def __init__(
        self,
        *,
        node_rewriter: NodeRewriter,
        DEBUG: bool,
        logger: Optional[Logger],
    ):
        self.DEBUG = DEBUG
        self.node_rewriter = node_rewriter
        self.logger = logger

    def try_one_node(
        self,
        *,
        ast_state: ASTState,
        node: ASTINode,
        op_param: RNOpParam,
    ) -> tuple[bool, list[ASTINode], bool]:
        if isinstance(node, InstsNode):
            return False, [], False
        if isinstance(node, NodeList):
            task: ToReduceTask = NodeListsReduceTask(nodes=[node])
        else:
            task = OneCFNodeReduceTask(node=node)
        return self.try_one_task(
            ast_state=ast_state,
            task=task,
            op_param=op_param,
        )

    def try_one_task(
        self,
        *,
        ast_state: ASTState,
        task: ToReduceTask,
        op_param: RNOpParam,
    ) -> tuple[bool, list[ASTINode], bool]:
        if isinstance(task, OneCFNodeReduceTask):
            node = task.node
            assert node.parent is not None
            success, new_nodes = self._try_non_list_node(
                ast_state=ast_state,
                node=node,
                op_param=op_param,
            )
            # new_nodes = []
            return success, new_nodes, False

        if isinstance(task, NodeListsReduceTask):
            success, new_nodes = self._try_node_lists(
                ast_state=ast_state,
                node_lists=task.nodes,
                op_param=op_param,
            )
            return success, new_nodes, False

        raise ValueError(f'Unsupported task type: {type(task)}')

    def _try_node_lists(
        self,
        *,
        ast_state: ASTState,
        node_lists: list[NodeList],
        op_param: RNOpParam,
    ) -> tuple[bool, list[ASTINode]]:
        rest_time = op_param.rest_time
        cur_epoch_num = op_param.cur_epoch_num
        assert cur_epoch_num is not None

        node_lists = [nl for nl in node_lists if nl.get_length() > 0]
        if len(node_lists) == 0:
            return False, []

        for nl in node_lists:
            remove_empty_node_in_nodelist(nl)

        snapshot_rewriter = OnlyInstSnapshotRewriter(
            ast_state=ast_state,
            node_rewriter=self.node_rewriter,
            DEBUG=self.DEBUG,
        )
        result, gen_nodes = reduce_node_list_by_insts_v9(
            ori_node_lists=node_lists,
            ast_state=ast_state,
            DEBUG=self.DEBUG,
            op_param=op_param,
            snapshot_rewriter=snapshot_rewriter,
            rest_time=rest_time,
        )
        return result, gen_nodes

    def _try_non_list_node(
        self,
        *,
        ast_state: ASTState,
        node: ASTINode,
        op_param: RNOpParam,
    ) -> tuple[bool, list[ASTINode]]:
        rest_time = op_param.rest_time
        if rest_time is None:
            rest_time = 900
        cur_input_path = op_param.cur_input_path

        allocate_rest_time = min(rest_time, 900)
        timeout = allocate_rest_time + 60
        rest_time = allocate_rest_time

        print('Rest time for reducing node is ', op_param.rest_time, node)
        print('Allocate rest time is ', rest_time)
        if self.logger is not None:
            self.logger.info(f'Trying to reduce node: {node.get_node_info()}')
        else:
            print('Current reduced node is ', node.get_node_info())

        if isinstance(node, NodeList):
            raise ValueError('NodeList is not supported ')

        with TimeoutHandler(timeout):
            if self.DEBUG:
                _tmp_check_ast_update(ast_state, {node.loc.func_idx})
                _debug_insts(ast_state, node, cur_input_path)
                assert cur_input_path is None or isinstance(cur_input_path, str)
                if cur_input_path is not None:
                    check_parser_match_wasm_file(ast_state.parser, cur_input_path)

            if isinstance(node, (BlockNode, LoopNode)):
                node_reducer = BlockShrink(self.node_rewriter)
                result, new_nodes = node_reducer.shrink(
                    target_node=node,
                    ast_state=ast_state,
                    rest_time=rest_time,
                )
                return result, new_nodes
            if isinstance(node, IfNode):
                node_reducer = IfShrink(self.node_rewriter)
                result, new_nodes = node_reducer.shrink(
                    target_node=node,
                    ast_state=ast_state,
                    rest_time=rest_time,
                )
                return result, new_nodes
            raise ValueError(f'{type(node)} is not supported')
    
def _debug_insts( ast_state, node, cur_input_path):
    parser_insts = ast_state.parser.defined_funcs[node.loc.func_idx].insts
    parser_scope_insts = parser_insts[node.loc.inst_idx:node.loc.inst_idx+node.get_length()]
    
    for node_inst, parser_inst in zip(node.get_insts(), parser_scope_insts):
        assert node_inst.opcode_text == parser_inst.opcode_text
    if cur_input_path is None:
        return
    cur_input_parser = get_parser_from_wasm_path(cur_input_path)
    cur_input_parser_insts = cur_input_parser.defined_funcs[node.loc.func_idx].insts
    cur_input_parser_scope_insts = cur_input_parser_insts[node.loc.inst_idx:node.loc.inst_idx+node.get_length()]
    for node_inst,  cur_input_parser_inst in zip(node.get_insts(),  cur_input_parser_scope_insts):
        assert node_inst.opcode_text == cur_input_parser_inst.opcode_text
