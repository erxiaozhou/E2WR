import hashlib
from util.debug_util import ValidateCheckType, validate_wasm, wasm2wat
from reduction_analysis.callsite_reduction import CallsiteRepStrategy,   replace_calls_interface_random_replacement
from reduction_analysis.Instrumentation.ValueProbeInstrument import ValueProbeManager
from reduction_analysis.ParserModification import Encoder, WMSnapshot
from reduction_analysis.ReduceUtil.RNOpParam import RNOpParam
from reduction_analysis.ReduceFrameWork.OneNodeReducer import NodeReducer
from reduction_analysis.ReductionDescUtil.OneReducerDirSystem import OneReducerDirSystem
from reduction_analysis.ReductionDescUtil.Oracle import CheckType, Oracle, call_oracle
from reduction_analysis.callsite_reduction_cal_weights_util import WeithtStrategy
from reduction_analysis.replace_indirect_call import replace_indirect_calls_interface
from reduction_analysis.util import get_insts_num_from_parser
from timeout_process import run_with_timeout
from .util import store_exception_case
from .FuncRemovalPass import TYRemoveFuncPass
from reduction_analysis.ReduceUtil.RewritingUtil.NodeRewriter import NodeRewriter, _tmp_check_ast_update
from ..ASTState import ASTState
from ..ReducerPassUtil.ReduceResult import ExecResult, ExecStatus
from reduction_analysis.ReducerCommonConfig import PASS_TIMEOUT
from ..ReducerPassUtil.ReducePass import reduce_checker
from typing import Optional
from file_util import copy_file
from pathlib import Path
import time
from ..ReducerPassUtil.ZReducerPass import ZReducerPass
from typing import Callable
import traceback
from .ASTNodePool import (
    ASTNodePool,
    NodeListsReduceTask,
    NodeScoreCalculator,
    OneCFNodeReduceTask,
    ScoreStrategy,
    get_node_size_score,
)
TIME_OUT_FOR_ONE_ATTEMPT = 3600
TIME_OUT_FOR_WHOLE_FUNC_LEVEL = 3600
REDUCE_CALLSITE_TIMEOUT = 1200
REDUCE_UNEXEC_FUNC_TIMEOUT = 300
# 
LARGE_TH_FOR_INSTRUMENTATION = 2
LARGE_TH_TO_FUNC_LEVEL_SIMPLIFY = 50
class UnStableCaaseException(Exception):
    pass


class NodeShrinkPass(ZReducerPass):
    def __init__(
        self,
        dir_system: OneReducerDirSystem,
        oracle_func:Callable,
        cr_strategy: CallsiteRepStrategy = CallsiteRepStrategy.VP,
        DEBUG: bool = False,
        ignore_exceptions: bool = False,
        name='NodeShrink',
        use_function_level_removal: bool = True,
        to_test_func_name: Optional[str] = None
    ):
        super().__init__(
            dir_system=dir_system,
            oracle_func=oracle_func,
            name=name,
            DEBUG=DEBUG
        )
        self.node_scorer = NodeScoreCalculator(ScoreStrategy.SIZE)
        self.ignore_exceptions = ignore_exceptions
        self.use_function_level_removal = use_function_level_removal
        self.can_prio_unused = (to_test_func_name is not None)
        self.to_test_func_name = to_test_func_name
        self.instrumented_path = str(dir_system.default_instrumented_path)
        # 
        self.cr_strategy = cr_strategy
        print('Current cr_strategy: ', self.cr_strategy)

        # Reused instrumentation manager for callsite probing/reduction.
        # Eagerly initialized (as requested) so multiple methods can reuse it.
        self.instrument_manager: Optional[ValueProbeManager] = None
        if self.to_test_func_name is not None:
            instrumented_path = str(Path(self.tmp_dir) / 'instrumented.wasm')
            self.instrument_manager = ValueProbeManager(
                instrumented_path=instrumented_path,
                to_test_func_name=str(self.to_test_func_name),
                DEBUG=bool(self.DEBUG),
                check_func=self.oracle_func,
            )
        self.node_rewriter = NodeRewriter(
            logger=self.logger,
            DEBUG=self.DEBUG,
            oracle_func=self.oracle_func,
            tmp_used_path=self.tmp_used_path,
            force_return_false_on_invalid_case=not self.DEBUG
        )
        self.last_output_hash: Optional[str] = None
        self.last_run_stop_size_score: Optional[float] = None
        self.input_parser_inst_num: int = -1
        
        self.one_node_reducer = NodeReducer(
            node_rewriter=self.node_rewriter,
            DEBUG=self.DEBUG,
            logger=self.logger,
            instrument_manager=self.instrument_manager,
        )
        self.total_run_times = -1
        self.ty_remove_func_pass = TYRemoveFuncPass(
            oracle=self.oracle_func,
            tmp_used_path=self.tmp_used_path,
            DEBUG=self.DEBUG,
        )

    def remove_proi_unexec_funcs(
        self,
        cur_input_path: str,
        cur_output_path: str,
        input_snapshot: WMSnapshot,
        *,
        timeout: Optional[float] = None,
    ) -> tuple[WMSnapshot, set[int]]:
        t0 = time.time()
        if self.instrument_manager is None:
            return input_snapshot, set()
        result = self.ty_remove_func_pass.remove_proi_unexec_funcs(
            cur_input_path=cur_input_path,
            cur_output_path=cur_output_path,
            input_snapshot=input_snapshot,
            instrument_manager=self.instrument_manager,
            timeout=timeout,
            callsite_as_unreachable=False
        )
        print(f'[remove_proi_unexec_funcs] takes {time.time() - t0}s')
        return result

    def _run_function_level_reduction_to_file(
        self,
        *,
        cur_input_path: str,
        cur_output_path: str,
    ) -> tuple[bool, WMSnapshot]:
        # if 
        has_reduced = False
        input_snapshot = WMSnapshot.from_path(cur_input_path)
        if self.input_parser_inst_num == -1:
            self.input_parser_inst_num = get_insts_num_from_parser(input_snapshot.parser)

        all_func_num = len(input_snapshot.parser.defined_funcs)
        if all_func_num < 2:
            print(f'Function num {all_func_num} is less than 2, skip function level reduction')
            return has_reduced, input_snapshot
        print(
            f'Raw setting:  {self.can_prio_unused} ;; total_run_times: {self.total_run_times} ;; all_func_num: {all_func_num}'
        )
        print('Will remove functions')
        # self.can_prio_unused = self.can_prio_unused and (all_func_num >= 100) and self.total_run_times == 0


        time_before_func_level = time.time()
        all_removed_func_idxs: set[int] = set()

        # Ensure the output path exists even when the proi pass removes nothing.
        if str(cur_input_path) != str(cur_output_path):
            copy_file(cur_input_path, cur_output_path)

        assert self.to_test_func_name is not None
        last_try_is_proi = False
        has_reduce_prio = False
        if self.can_prio_unused and (all_func_num >= 500) and self.total_run_times == 0:
            input_snapshot, _removed_func_idxs = self.remove_proi_unexec_funcs(
                cur_input_path=cur_input_path,
                cur_output_path=cur_output_path,
                input_snapshot=input_snapshot,
                timeout=REDUCE_UNEXEC_FUNC_TIMEOUT,
            )
            all_removed_func_idxs.update(_removed_func_idxs)
            cur_input_path = cur_output_path
            last_try_is_proi = True
            has_reduce_prio = True

        round_num = 0
        t0: float = time.time()
        
        for _ in range(1):
            if (time.time() - t0) > 3600:
                print('Function level removal time out break')
                break

            use_callsite_removal = len(input_snapshot.parser.defined_funcs) >= 2
            print(f'use_callsite_removal: {use_callsite_removal}')
            if use_callsite_removal:
                print('Start callsite replacement pre-pass =============================')
                
                if  self.cr_strategy != CallsiteRepStrategy.DISABLE:
                    input_snapshot, _replaced_callsite_idxs = self.replace_callsites(
                        cur_input_path=cur_input_path,
                        cur_output_path=cur_output_path,
                        input_snapshot=input_snapshot.copy(),
                        dd_timeout_s=REDUCE_CALLSITE_TIMEOUT,
                    )
                    if _replaced_callsite_idxs:
                        last_try_is_proi = False
                    cur_input_path = cur_output_path
                    if self.DEBUG:
                        print(f'After callsite replacement, cur_input_path: {cur_input_path}')
                        Encoder().encode_without_mutation(
                            input_snapshot, Path(self.tmp_dir) / 'tmp_after_callsite.wasm'
                        )
                        if not validate_wasm(cur_input_path, tag=ValidateCheckType.SELF_CHECK):
                            raise ValueError(
                                f'KKK After callsite replacement, {cur_input_path} is invalid wasm'
                            )
                        if not validate_wasm(Path(self.tmp_dir) / 'tmp_after_callsite.wasm', tag=ValidateCheckType.SELF_CHECK):
                            raise ValueError(
                                f'KKK After callsite replacement, {Path(self.tmp_dir) / "tmp_after_callsite.wasm"}  snapshot is invalid wasm'
                            )
            if False:
                input_snapshot, removed_func_idxs = self.ty_remove_func_pass.ensure_pass_run(
                    input_snapshot=input_snapshot,
                    cur_output_path=cur_output_path,
                    proi_func_idxs=None,
                    timeout=min(
                        TIME_OUT_FOR_ONE_ATTEMPT,
                        TIME_OUT_FOR_WHOLE_FUNC_LEVEL - (time.time() - t0),
                    ),
                    weights=None,
                )
                if removed_func_idxs:
                    last_try_is_proi = False

                if not removed_func_idxs:
                    print(f'No more functions to remove after {round_num} rounds')
                    print(f'There are {len(input_snapshot.parser.defined_funcs)} functions left')
                    break

                all_removed_func_idxs.update(removed_func_idxs)
            print(f'There are {len(input_snapshot.parser.defined_funcs)} functions left')

            cur_input_path = cur_output_path
            round_num += 1

        if not has_reduce_prio and (len(input_snapshot.parser.defined_funcs) >= LARGE_TH_FOR_INSTRUMENTATION):
        # if False:
            input_snapshot, _all_removed_func_idxs = self.remove_proi_unexec_funcs(
                cur_input_path=cur_input_path,
                cur_output_path=cur_output_path,
                input_snapshot=input_snapshot,
                timeout=REDUCE_UNEXEC_FUNC_TIMEOUT,
            )
            all_removed_func_idxs.update(_all_removed_func_idxs)

        if all_removed_func_idxs:
            has_reduced = True
            print(f"Removed {len(all_removed_func_idxs)} functions")
            cur_input_path = cur_output_path


        if self.DEBUG:
            print('Will start inst level removal, cur_input_path: ', cur_input_path)
            if not validate_wasm(cur_input_path, tag=ValidateCheckType.SELF_CHECK):
                raise ValueError(
                    f'After function level removal, {cur_input_path} is invalid wasm'
                )

        # Canonicalize: ensure the latest binary is at cur_output_path.
        if str(cur_input_path) != str(cur_output_path):
            copy_file(cur_input_path, cur_output_path)
        print(f'Function level removal time cost: {time.time() - time_before_func_level}s')
        return has_reduced, input_snapshot

    @reduce_checker
    def reduce(self,
               cur_input_path: str,
               cur_output_path: str,
               timeout: int = PASS_TIMEOUT) -> ExecResult:
        # 
        print('Is debug mode: ', self.DEBUG)
        self.node_rewriter.set_best_path(cur_output_path)
        start_time = time.time()
        original_size = Path(cur_input_path).stat().st_size
        success_times = 0
        copy_file(cur_input_path, cur_output_path)
        cur_input_path = cur_output_path
        # 
        stripped_path = str(Path(self.tmp_dir) / 'stripped_input.wasm')
        run_with_timeout(f'wasm-strip {cur_input_path} -o {stripped_path}')
        if self.oracle_func(stripped_path, CheckType.OTHER):
            copy_file(stripped_path, cur_input_path)
        self.input_parser_inst_num = -1
        # 
        should_stop_time = start_time + timeout
        # assert 0, considered_timeout
        self.total_run_times += 1
        total_trys = 0
        cur_epoch_num = -1
        last_exception_score: Optional[float] = None
        last_pass_case = Path(self.tmp_dir) / 'last_pass_case.wasm'
        if last_pass_case.exists():
            last_pass_case.unlink()

        cur_input_hash = hashlib.sha256(
            open(cur_input_path, 'rb').read()).hexdigest()
        has_start_info = cur_input_hash == self.last_output_hash

        if not has_start_info:
            self.last_run_stop_size_score = None

        try:
            early_return = False
            success_times_at_last_epoch = 0
            success_tasks_in_this_epoch = []
            while time.time() < should_stop_time:
                last_epoch_has_reduced = False
                cur_epoch_num += 1
                # if self.DEBUG:
                #     print('============================================')
                #     for success_task in success_tasks_in_this_epoch:
                #         print(f'\t\t[DEBUG] Success task in epoch {cur_epoch_num-1}: {success_task.get_nodes_info()}')
                success_tasks_in_this_epoch = []
                # 
                # 
                if cur_epoch_num >= 1:
                    self.last_run_stop_size_score = None
                    # 
                    cur_success_times = success_times
                    print(f'Success times at last epoch [{cur_epoch_num-1}]: ', cur_success_times - success_times_at_last_epoch)
                    success_times_at_last_epoch = cur_success_times
                    # 
                #
                if self.use_function_level_removal and (cur_epoch_num == 0):
                    did_reduce, input_snapshot = self._run_function_level_reduction_to_file(
                        cur_input_path=cur_input_path,
                        cur_output_path=cur_output_path,
                    )
                    cur_input_path = cur_output_path
                    if did_reduce:
                        last_epoch_has_reduced = True
                    ast_state = ASTState.from_snapshot(input_snapshot)
                else:
                    ast_state = ASTState.from_path(cur_input_path)
                
                if self.input_parser_inst_num == -1:
                    self.input_parser_inst_num = get_insts_num_from_parser(ast_state.parser)
                assert self.input_parser_inst_num >= 0
                if str(cur_input_path) != str(last_pass_case):
                    copy_file(cur_input_path, last_pass_case)
                if (cur_epoch_num == 0) and (self.instrument_manager is not None):
                    rest_time = should_stop_time - time.time()
                    if rest_time > 0:
                        _new_snapshot, _replaced_indirect_idxs = self.replace_call_indirects(
                            cur_input_path=cur_input_path,
                            cur_output_path=cur_output_path,
                            input_snapshot=ast_state.snapshot.copy(),
                            dd_timeout_s=min(300.0, rest_time),
                        )
                        cur_input_path = cur_output_path
                        if _replaced_indirect_idxs:
                            ast_state = ASTState.from_snapshot(_new_snapshot)
                            self.input_parser_inst_num = get_insts_num_from_parser(ast_state.parser)
                            if str(cur_input_path) != str(last_pass_case):
                                copy_file(cur_input_path, last_pass_case)
                pool = ASTNodePool.from_ast_state(
                    ast_state=ast_state,
                    considered_func_idxs=None,
                    strategy=self.node_scorer.strategy,
                    all_inst_num=self.input_parser_inst_num,
                    prioritize_node_lists=False,
                    # prioritize_node_lists=False,
                    # prioritize_node_lists=(cur_epoch_num == 0) and (self.total_run_times==0),
                    # prioritize_node_lists=((cur_epoch_num == 0)  and self.input_parser_inst_num > 10000),
                    # prioritize_node_lists=((cur_epoch_num == 0) and (self.total_run_times==0) and self.input_parser_inst_num > 1000000),
                    reprocess_parents=True,
                )
                # if not pool:
                #     break
                # success_times
                while True:
                    last_pass_size = Path(cur_input_path).stat().st_size
                    max_score = _get_min_of_two_optional_float(self.last_run_stop_size_score, last_exception_score)
                    task = pool.practical_select(max_=1, max_score=max_score)
                    
                    if task is None:
                        break
                    if len(task.get_nodes()) == 0:
                        break
                    node = task.get_first_node()
                    print(f'Current process task: {task.get_nodes_info()}; Node num: {len(task.get_nodes())}')
                    rest_time = should_stop_time - time.time()
                    if rest_time < 0:
                        early_return = True
                        if cur_epoch_num == 0:
                            assert self.input_parser_inst_num > 0
                            _max_length = self.input_parser_inst_num
                            self.last_run_stop_size_score = get_node_size_score(node, max_length=_max_length)
                        break
                    try:
                        reduce_start_time = time.time()
                        total_trys += 1
                        task_inst_num_before = task.get_total_inst_num()
                        success_, new_nodes, skip = self.one_node_reducer.try_one_task(
                            ast_state=ast_state,
                            task=task,
                            op_param=RNOpParam(
                                cur_epoch_num=cur_epoch_num,
                                total_run_times=self.total_run_times,
                                cur_input_path=cur_input_path,
                                rest_time=rest_time,
                                task=task,
                            )
                        )
                        if isinstance(task, NodeListsReduceTask):
                            _ri = (task_inst_num_before - sum(n.get_length() for n in new_nodes)) if success_ else 0
                            _counts_as_reduce = success_ and _ri > 0
                        else:
                            _ri = None
                            _counts_as_reduce = success_
                        print(f'[DEBUG] Current input size: {Path(cur_input_path).stat().st_size}')
                        print(f'[DEBUG] Current output size: {Path(cur_output_path).stat().st_size}')
                        if success_ and (not call_oracle(self.oracle_func, cur_output_path, CheckType.SELF_CHECK)) and (success_times %2==1):
                        # if (not self.oracle_func(cur_output_path)) and success_:
                            print(f'Current process task: {task.get_nodes_info()}')
                            if isinstance(self.oracle_func, Oracle):
                                print('Oracle path : ', self.oracle_func.oracle_path)
                            print(f'After one_node_reducer, the cur_output_path {cur_output_path} does not pass the oracle anymore')
                            raise UnStableCaaseException(f'After one_node_reducer, the cur_output_path {cur_output_path} does not pass the oracle anymore')
                        # 
                        if success_:
                            success_tasks_in_this_epoch.append(task)
                            if _counts_as_reduce:
                                last_epoch_has_reduced = True
                            pool.handle_task_success(task=task, new_nodes=new_nodes)
                            success_times += 1
                            copy_file(cur_output_path, last_pass_case)
                            cur_input_path = str(last_pass_case)
                            if self.DEBUG:
                                wat_path = f'{self.tmp_dir}/tmp.wat'
                                wasm2wat(cur_input_path, wat_path)
                                print(f'{wat_path} is saved')
                            if self.DEBUG:
                                _tmp_check_ast_update(
                                    ast_state, task.get_func_idxs())
                        reduce_cur_node_time = time.time() - reduce_start_time
                        cur_pass_size = Path(cur_input_path).stat().st_size
                        ri_str = f'RI: {_ri}' if _ri is not None else 'RI: N/A'
                        print(f'[Success Times: {success_times}/{total_trys}][{cur_epoch_num}]', f'CurSize: {cur_pass_size}',
                              f'Last task : {task.get_nodes_info()} success: {success_} RS: {last_pass_size-cur_pass_size} {ri_str} TC: {reduce_cur_node_time:.2f} RT: {should_stop_time - time.time():.2f}')
                        last_pass_size = cur_pass_size
                    except Exception as e:
                        if self.DEBUG and (not isinstance(e, UnStableCaaseException)):
                            traceback.print_exc()
                            raise e
                        else:
                            assert self.input_parser_inst_num > 0
                            _max_length = self.input_parser_inst_num
                            last_exception_score = (get_node_size_score(node, max_length=_max_length) - 0.00001)
                            print('Process UnStableCaaseException ..., next last_exception_score is: ', last_exception_score)
                            try:
                                store_exception_case(
                                    tester=self,
                                    cur_input_path=cur_input_path,
                                    tmp_dir=self.tmp_dir,
                                    task=task,
                                    ast_state=ast_state,
                                    e=e,
                                )
                            except Exception:
                                pass
                            if not Path(last_pass_case).exists():
                                print('No last passing case found, stop reducing')
                                early_return = True
                            elif Path(last_pass_case).exists() and call_oracle(self.oracle_func, str(last_pass_case), CheckType.SELF_CHECK):
                                copy_file(last_pass_case, cur_output_path)
                                cur_input_path = str(last_pass_case)
                                ast_state = ASTState.from_path(cur_input_path)
                            else:
                                # print('last_pass_case not exists, will stop reducing')
                                print('last_pass_case not exists or does not pass oracle, will stop reducing')
                                early_return = True  # no known-good input; stop reducing
                            traceback.print_exc()
                            break
                if early_return:
                    print('early_return is True, break')
                    break
                if not pool:
                    last_exception_score = None
                    if (not last_epoch_has_reduced) and (cur_epoch_num > 0 or self.total_run_times > 0):
                        print(f'Pool is empty and last_epoch_has_reduced? {last_epoch_has_reduced}, break')
                        break
                   

        except Exception as e:
            traceback.print_exc()
            if self.DEBUG:
                raise e
        if Path(last_pass_case).exists():
            if not call_oracle(self.oracle_func, cur_output_path, CheckType.SELF_CHECK):
                print('Store last passing case to output path')
                copy_file(last_pass_case, cur_output_path)
        time_len = time.time() - start_time

        try:
            with open(cur_input_path, 'rb') as f:
                self.last_output_hash = hashlib.sha256(f.read()).hexdigest()
        except (IOError, OSError) as e:
            print(f"failed: {e}")
            self.last_output_hash = None
        assert ast_state is not None
        cur_inst_num = get_insts_num_from_parser(ast_state.parser)

        current_size = Path(cur_output_path).stat().st_size
        reduced_size = original_size - current_size
        assert ast_state is not None

        result = ExecResult(
            exec_status=ExecStatus.SUCCESS,
            exec_taken_time=time_len,
            reduced_size_num=reduced_size,
            reduced_inst_num=self.input_parser_inst_num - cur_inst_num,
            is_partial_by_timeout=(time.time() >= should_stop_time)
        )
        if self.DEBUG:
            wat_path = f'{self.tmp_dir}/tmp.wat'
            wasm2wat(cur_input_path, wat_path)
            print(f'The final result {wat_path} is saved')

        return result
    def replace_call_indirects(
        self,
        cur_input_path: str,
        cur_output_path: str,
        input_snapshot: WMSnapshot,
        dd_timeout_s: Optional[float] = None,
    ) -> tuple[WMSnapshot, set[int]]:
        print('Start reduce call_indirects ==========================================')
        replace_back = str(Path(self.tmp_dir) / 'replace_call_indirect_bak.wasm')
        copy_file(cur_input_path, replace_back)

        if self.instrument_manager is None:
            copy_file(cur_input_path, cur_output_path)
            return input_snapshot, set()
        try:
            t0 = time.time()
            input_snapshot, replaced_callsite_idxs = replace_indirect_calls_interface(
                cur_input_path=cur_input_path,
                cur_output_path=cur_output_path,
                tmp_dir=str(self.tmp_dir),
                oracle_func=self.oracle_func,
                input_snapshot=input_snapshot,
                instrument_manager=self.instrument_manager,
                dd_timeout_s=dd_timeout_s,
                DEBUG=bool(self.DEBUG),
            )
            print(f'Replaced {len(replaced_callsite_idxs)} call_indirects, takes {time.time() - t0}s')
        except Exception as e:
            print('Exception when replacing call_indirects: ', e)
            traceback.print_exc()
            copy_file(replace_back, cur_output_path)
            if not validate_wasm(cur_output_path, tag=ValidateCheckType.SELF_CHECK):
                raise ValueError(f'After failed call_indirect replacement, {cur_output_path} is invalid wasm')
            input_snapshot = WMSnapshot.from_path(cur_output_path)
            return input_snapshot, set()
        return input_snapshot, replaced_callsite_idxs

    def replace_callsites(
        self,
        cur_input_path: str,
        cur_output_path: str,
        input_snapshot: WMSnapshot,
        dd_timeout_s: Optional[float] = None,
    ):
        print('Start reduce callsites ==========================================')
        replace_back = str( Path(self.tmp_dir) / 'replace_call_bak.wasm')
        copy_file(cur_input_path, replace_back)

        try:
            if self.instrument_manager is None:
                copy_file(cur_input_path, cur_output_path)
                return input_snapshot, set()
            t0 = time.time()
            input_snapshot, replaced_callsite_idxs = replace_calls_interface_random_replacement(
                cur_input_path=cur_input_path,
                cur_output_path=cur_output_path,
                tmp_dir=str(self.tmp_dir),
                oracle_func=self.oracle_func,
                input_snapshot=input_snapshot,
                instrument_manager=self.instrument_manager,
                dd_timeout_s=dd_timeout_s,
                DEBUG=bool(self.DEBUG),
                save_ratio=0.95,
                weight_strategy=WeithtStrategy.LAST_APPEAR,
                unexecuted_as_unreachable=False
            )
            print(f'Replaced {len(replaced_callsite_idxs)} callsites, takes {time.time() - t0}s')
        except Exception as e:
            print('Exception when replacing callsites: ', e)
            traceback.print_exc()
            copy_file(replace_back, cur_output_path)
            if not validate_wasm(cur_output_path, tag=ValidateCheckType.SELF_CHECK):
                raise ValueError(f'After failed callsite replacement, {cur_output_path} is invalid wasm')
            input_snapshot = WMSnapshot.from_path(cur_output_path)
            return input_snapshot, set()
        return input_snapshot, replaced_callsite_idxs

def _get_min_of_two_optional_float(a: Optional[float], b: Optional[float]) -> Optional[float]:
    if a is None:
        return b
    if b is None:
        return a
    return min(a, b)
