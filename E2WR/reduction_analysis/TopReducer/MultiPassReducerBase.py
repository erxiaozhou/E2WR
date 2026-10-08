from util.debug_util import wasm2wat
from file_util import copy_file, get_logger, check_dir
from reduction_analysis.ReducerCommonConfig import PASS_TIMEOUT
from pathlib import Path
from typing import Optional, Union, Callable
import time
from abc import abstractmethod
from ..ReducerPassUtil.ReduceResult import ReduceProcessStatus, ReduceResult
from ..ReducerPassUtil.ReducePass import ReduceAndCheckPass
from ..ReductionDescUtil.ReduceLimit import ReduceLimit
from .PassStatistics import PassStatistics
from .Reducer import FrameworkReducerDirSystem, FrameworkReducer


class MPFrameworkReducerBase(FrameworkReducer):
    def __init__(
        self, 
        input_path: Union[str, Path],
        output_path: Union[str, Path],  
        oracle_func:Callable,
        work_dir_system: FrameworkReducerDirSystem,
        reduce_limit: ReduceLimit,
        passes: list[ReduceAndCheckPass],
        save_input_snapshot: bool = True
    ):
        super().__init__(input_path, output_path, oracle_func, work_dir_system, save_input_snapshot)
        self.reduce_limit = reduce_limit
        self.passes = passes
        self.logger = get_logger("MultiPassReducer", self.reducer_logger_path)
        print('Log is at ', self.reducer_logger_path)
        copy_file(self.input_path, self.output_path)
        all_pass_names = [pass_.name for pass_ in self.passes]
        assert len(all_pass_names) > 0, "no passes available for reduce"
        assert len(set(all_pass_names)) == len(all_pass_names), "pass names should be unique"
        self.stats = PassStatistics(all_pass_names)
        self.select_times = 0
        self.tried_since_last_update: set[str] = set()
        # if Path

    def cur_result_file_size(self)->int:
        return Path(self.output_path).stat().st_size


    @abstractmethod
    def select_pass(self, candi_names:list[str], last_result: Optional[ReduceResult]=None)->ReduceAndCheckPass:
        pass
    
    def run_one_pass(self,
                     candi_names:list[str], 
                     last_result: Optional[ReduceResult]=None
                     )->ReduceResult:
        selected_pass = self.select_pass(candi_names, last_result)
        cur_testing_time = self.get_cur_testing_time()
        if cur_testing_time is None:
            cur_pass_timeout = PASS_TIMEOUT
        else:
            max_timeout = max(int(cur_testing_time/10), PASS_TIMEOUT)
            sum_timeout = self.reduce_limit.timeout
            if sum_timeout is not None:
                cur_pass_timeout = int(min(max_timeout, sum_timeout))
            else:
                cur_pass_timeout = max_timeout
        cur_time = self.get_cur_testing_time()
        print(f"Run pass: {selected_pass.name}, cur pass timeout: {cur_pass_timeout:0.2f}, cur_testing_time: {cur_time:0.2f} selection times: {self.select_times}")
        cur_result = selected_pass.reduce_and_check(input_path=self.output_path, output_path=self.tmp_output_path, timeout=cur_pass_timeout)
        cause_update = self.post_process(self.tmp_output_path, cur_result)
        if cause_update:
            self.tried_since_last_update.clear()
        else:
            self.tried_since_last_update.add(selected_pass.name)
        self.stats.update_stats(selected_pass.name, cur_result, cause_update)
        self.logger.info(f"Run pass: {selected_pass.name}, result: {cur_result}, cur_testing_time: {cur_time:0.2f}")
        
        return cur_result
    
    def post_process(self, may_reduced_case:str, pass_reduce_result:ReduceResult)->bool:
        print(f'pass_reduce_result: {pass_reduce_result}')
        # input('press enter to continue...')
        if not pass_reduce_result.exec_result.is_successful_exec():  
            return False
        else:
            assert Path(may_reduced_case).exists(), f"may_reduced_case {may_reduced_case} does not exist"
        is_expected = pass_reduce_result.can_accept()
        if is_expected:
            assert pass_reduce_result.reduce_process_status == ReduceProcessStatus.PASS_ORACLE, f'pass_reduce_result.reduce_process_status: {pass_reduce_result.reduce_process_status}'
        # print(f'is_expected: {is_expected}')
        if is_expected:
            last_optimal_size = self.cur_result_file_size()
            # assert last_optimal_size >= Path(may_reduced_case).stat().st_size
            copy_file(may_reduced_case, self.output_path)
            cur_size = self.cur_result_file_size()
            # print(f'Last optimal  case size is {last_optimal_size}, Current case size is {cur_size}, reduce size is {last_optimal_size - cur_size}')
            self.logger.info(f'Last optimal  case size is {last_optimal_size}, Current case size is {cur_size}, reduce size is {last_optimal_size - cur_size}')
            return True
        else:
            return False

    
    def run(self):
        self.check_before_run()
        start_time = time.time()
        self.set_start_time(start_time)
        cur_times = 0
        last_result = None
        copy_file(self.input_path, self.output_path)

        candi_names = [pass_.name for pass_ in self.passes]
        while not self.reduce_limit.limit_is_reached(start_time, time.time(), cur_times):
            cur_times += 1
            last_result = self.run_one_pass(candi_names, last_result)
            untried = [pass_.name for pass_ in self.passes if pass_.name not in self.tried_since_last_update]
            print(f'Untried passes since last update: {len(untried)} passes ')
            self.logger.info(f'Untried passes since last update: {len(untried)} passes ')
            if len(untried) == 0:
                break
            candi_names = untried
        
        stats_dir = Path(self.output_path).parent
        check_dir(stats_dir)
        stats_file = str(stats_dir / f"{self.__class__.__name__}_stats.json")
        json_file, summary_file = self.stats.save_to_file(stats_file)
        self.logger.info(f"stats data saved to: {json_file}")
        self.logger.info(f"stats summary saved to: {summary_file}")
        print(f"stats data saved to: {json_file}")
        wat_path = Path(self.output_path).with_suffix('.wat')
        wasm2wat(self.output_path, wat_path)
        print(f"wasm saved to: {self.output_path}")
        print(f"wat saved to: {wat_path}")
        print('Log is at ', self.reducer_logger_path)
        
        # reduce_report = self.get_reduce_report()
        # print(reduce_report)

    def get_reduce_report(self)->str:
        base_report = f"Reduce done, original size: {Path(self.input_path).stat().st_size}, current size: {self.cur_result_file_size()}, reduced: {Path(self.input_path).stat().st_size - self.cur_result_file_size()}\n\n"
        
        base_report += self.stats.get_summary()
        return base_report
