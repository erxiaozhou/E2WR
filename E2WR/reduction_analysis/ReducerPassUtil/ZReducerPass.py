import time
from reduction_analysis.ReductionDescUtil.OneReducerDirSystem import OneReducerDirSystem
from .get_reduce_result_util import get_reduce_result
from .ReduceResult import ReduceResult
from reduction_analysis.ReducerCommonConfig import PASS_TIMEOUT
from .ReducePass import ReduceAndCheckPass, ReducePass, reduce_checker
from typing import  Callable, Optional
from file_util import get_logger
from pathlib import Path
from util.util import AbstractMethodException
from enum import Enum


class ZReducerType(Enum):
    NODE_SHRINK = 'NodeShrink'
    PLEV = 'PLEV'
    FINAL_POLISH = 'FinalPolishPass'
    UNUSED_DEF = 'UnusedDefReducer'


class ZReducerPass(ReducePass):
    def __init__(
        self,
        dir_system:OneReducerDirSystem,
        oracle_func: Callable,
        name: str,
        DEBUG: bool
    ):
        self.tmp_dir = dir_system.tmp_dir
        self.tmp_used_path = dir_system.tmp_used_path
        self.log_path = dir_system.get_default_log_path(name)
        
        super().__init__(
            name,
            self.tmp_dir
        )
        self.oracle_func = oracle_func
        self.DEBUG = DEBUG
        
        self.logger = get_logger(self.name, self.log_path)

    @reduce_checker
    def reduce(self,
               cur_input_path: str,
               cur_output_path: str,
               timeout: int = PASS_TIMEOUT) -> ReduceResult:
        raise AbstractMethodException


class ZReducerPassAndCheck(ReduceAndCheckPass):
    def __init__(
        self,
        z_probe_pass:ZReducerPass,
    ):
        super().__init__(
            name=z_probe_pass.name,
            reduce_pass=z_probe_pass,
            pass_oracle_func=z_probe_pass.oracle_func,
        )

    def reduce_and_check(self, 
                         input_path:str, 
                         output_path:str, 
                         timeout:int=PASS_TIMEOUT
                         )->ReduceResult:
        start_time = time.time()
        exec_result = self.reduce_pass.reduce(input_path, output_path, timeout)
        can_accept = exec_result.is_successful_exec()
        end_time = time.time()
        result = get_reduce_result(
            output_path, 
            exec_result,
            self.pass_oracle_func,
            end_time - start_time, 
            require_smaller_size=True
            )
        can_accept = result.can_accept()
        print('input_path:', input_path)
        print('output_path:', output_path)
        print(f'ZPASS {self.name} can accept: {can_accept} pass_oracle: {result.pass_oracle()}')
        return result
