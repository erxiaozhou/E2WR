from typing import Union
from .ReduceResult import ExecResult, ReduceResult
from abc import ABC, abstractmethod
from pathlib import Path
from reduction_analysis.ReducerCommonConfig import PASS_TIMEOUT
import functools
from util.util import AbstractMethodException
from typing import Callable

def reduce_checker(func: Callable) -> Callable:
    @functools.wraps(func)
    def wrapper(self, input_path, output_path, timeout=PASS_TIMEOUT) -> ReduceResult:
        assert Path(input_path).exists(), f"input_path {input_path} does not exist"
        
        result = func(self, input_path, output_path, timeout)
        
        
        return result
    return wrapper

class ReducePass(ABC):
    def __init__(self,
                 name:str,
                 tmp_dir:Union[str, Path],
                 ):
        self.name = name
        self.tmp_dir = str(tmp_dir)
        
    @abstractmethod
    def reduce(self, 
               input_path, 
               output_path, 
               timeout:int=PASS_TIMEOUT
               )->ExecResult:
        raise AbstractMethodException


class ReduceAndCheckPass(ReducePass):
    def __init__(self,
                 name:str,
                 reduce_pass:ReducePass,
                 pass_oracle_func:Callable,
                 ):
        super().__init__(name, reduce_pass.tmp_dir)
        self.reduce_pass = reduce_pass
        self.pass_oracle_func = pass_oracle_func

    @reduce_checker
    def reduce(self, 
               input_path, 
               output_path, 
               timeout:int=PASS_TIMEOUT
               )->ExecResult:
        return self.reduce_pass.reduce(input_path, output_path, timeout)


    @abstractmethod
    def reduce_and_check(self, 
                         input_path, 
                         output_path, 
                         timeout:int=PASS_TIMEOUT
                         )->ReduceResult:
        raise AbstractMethodException
