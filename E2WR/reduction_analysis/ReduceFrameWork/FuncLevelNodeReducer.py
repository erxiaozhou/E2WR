from reduction_analysis.ReduceFrameWork.OneNodeReducer import NodeReducer
from ..ASTInfo.AST import ASTINode, NodeList, traverse_ast
from reduction_analysis.ReduceUtil.RewritingUtil.NodeRewriter import _tmp_check_ast_update
from ..ASTState import ASTState
from reduction_analysis.ReduceUtil.RewritingUtil.NodeRewriter import NodeRewriter
from ..ASTInfo.ASTInfo import ASTInfo
from ..ReduceUtil.ReduceStrategy import SpecificTypeReplacementGen
from reduction_analysis.ReducerCommonConfig import PASS_TIMEOUT
from typing import Optional
from file_util import copy_file
from pathlib import Path
import time
import traceback
from .ASTNodePool import ASTNodePool, ScoreStrategy
from .ASTNodePool import OneCFNodeReduceTask, NodeListsReduceTask
from reduction_analysis.ReduceUtil.RNOpParam import RNOpParam
from reduction_analysis.ParserModification import WMSnapshot


class FuncLevelNodeReducer:
    def __init__(
        self,
        node_rewriter: NodeRewriter,
        DEBUG: bool = False

    ):
        self.DEBUG = DEBUG
        self.node_rewriter = node_rewriter
        self.one_node_reducer = NodeReducer(
            node_rewriter=self.node_rewriter,
            DEBUG=self.DEBUG,
            logger=None,
        )

    def _is_to_reduce_node(self, node: ASTINode, ast_info: ASTInfo):
        if node.get_length() == 0:
            return False
        if ast_info.node_is_removed(node=node):
            return False
        return True

    def reduce(self,
               cur_input_path: str,
               cur_output_path: str,
               input_snapshot: WMSnapshot,
               considered_func_idxs:Optional[set[int]]=None,
               timeout: int = PASS_TIMEOUT,
               ) -> WMSnapshot:
        print('Start FuncLevelNodeReducer, timeout: ', timeout)
        print('Size before FuncLevelNodeReducer: ', Path(cur_input_path).stat().st_size)
        start_time = time.time()
        success_times = 0
        if cur_input_path != cur_output_path:
            copy_file(cur_input_path, cur_output_path)
            cur_input_path = cur_output_path
        should_stop_time = start_time + timeout
        total_trys = 0
        self.cur_epoch_num = -1
        cur_snapshot = input_snapshot

        try:
            early_return = False
            while time.time() < should_stop_time:
                self.cur_epoch_num += 1
                #
                ast_state = ASTState.from_snapshot(cur_snapshot)
                ast_info = ast_state.ast_info
                pool = ASTNodePool.from_ast_state(
                    ast_state=ast_state,
                    considered_func_idxs=considered_func_idxs,
                    strategy=ScoreStrategy.SIZE,
                )
                last_epoch_has_reduced = False
                while pool:
                    task= pool.practical_select(max_=1)
                    if task is None:
                        break
                    node = task.get_first_node()
                    rest_time = should_stop_time - time.time()
                    if rest_time < 0:
                        early_return = True
                        break
                    try:
                        if not self._is_to_reduce_node(node=node, ast_info=ast_info):
                            continue
                        total_trys += 1
                        success_, new_nodes ,_= self.one_node_reducer.try_one_node(
                            ast_state=ast_state,
                            node=node,
                            op_param=RNOpParam(
                                task=task,
                                total_run_times=0,
                            enable_fast_mode=False,
                                cur_epoch_num=self.cur_epoch_num,
                                rest_time=rest_time,
                            )
                        )
                        if success_:
                            last_epoch_has_reduced = True
                            pool.push_non_empty_subtree_nodes(new_nodes=new_nodes)
                            success_times += 1
                            ast_state.to_file(cur_output_path)
                            cur_input_path = cur_output_path
                            cur_snapshot = ast_state.snapshot

                            if self.DEBUG:
                                _tmp_check_ast_update(
                                    ast_state, {node.loc.func_idx})
                        print(f'[FL][Success Times: {success_times}/{total_trys}] ', f'Current size: {Path(cur_input_path).stat().st_size}',
                              'Last reduced node : ', node.get_node_info(), 'success: ', success_)
                    except Exception as e:
                        traceback.print_exc()
                        if self.DEBUG:
                            raise e
                        else:
                            ast_state = ASTState.from_snapshot(cur_snapshot)
                            break
                if early_return:
                    print('early_return is True, break')
                    break
                if not last_epoch_has_reduced:
                    break

        except Exception as e:
            traceback.print_exc()
            if self.DEBUG:
                raise e
        return cur_snapshot
