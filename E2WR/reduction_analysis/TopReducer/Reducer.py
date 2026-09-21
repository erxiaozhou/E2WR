import shutil
from file_util import check_dir
from pathlib import Path
from typing import Callable, Union, Optional
from abc import ABC, abstractmethod
import time
from ..ReducerPassUtil.ReduceResult import ReduceResult
from util.util import AbstractMethodException


class FrameworkReducerDirSystem:
    def __init__(self, 
                 work_dir:Path,
                 log_base_dir:Optional[Path]=None,
                 reducer_logger_path:Optional[str]=None,
                 tmp_output_path:Optional[str]=None,
                 tmp_dir:Optional[Path]=None,
                 init_work_dir:bool = True
                 ) -> None:
        self.work_dir = check_dir(work_dir) if init_work_dir else work_dir
        # 
        self.log_base_dir = check_dir(self._set_default(
            log_base_dir, 
            self.work_dir, 
            "logs")
        )
        self.reducer_logger_path = self._set_default(
            reducer_logger_path, 
            self.log_base_dir, 
            "reducer.log"
        )
        self.tmp_output_path = self._set_default(
            tmp_output_path, 
            self.work_dir, 
            "tmp_phase_output.wasm"
        )
        self.tmp_dir = check_dir(self._set_default(
            tmp_dir,
            self.work_dir,
            "tmp"
        ))

    def _set_default(self, argument_val, base_dir:Path, default_name:str)->str:
        if argument_val is None:
            argument_val = str(base_dir / default_name)
        return argument_val
        

    @property
    def input_snapshot_path(self)->Path:
        return self.work_dir / "input.wasm"


class FrameworkReducer(ABC):
    def __init__(
        self,
        input_path: Union[str, Path],
        output_path: Union[str, Path],
        oracle_func:Callable,
        work_dir_system: FrameworkReducerDirSystem,
        save_input_snapshot: bool
    ):
        self.input_path = str(input_path)
        self.output_path = str(output_path)
        self.work_dir_system = work_dir_system
        self.save_input_snapshot = save_input_snapshot

        self.oracle_func = oracle_func
        self.original_size = Path(self.input_path).stat().st_size
        self.start_time: Optional[float] = None
        
        self.tmp_output_path = self.work_dir_system.tmp_output_path
        self.reducer_logger_path = self.work_dir_system.reducer_logger_path
        self.work_dir = self.work_dir_system.work_dir


    def set_start_time(self, start_time: float):
        self.start_time = start_time

    def get_cur_testing_time(self)->Optional[float]:
        if self.start_time is None:
            return None
        return time.time() - self.start_time

    @abstractmethod
    def run(self)->None:
        raise AbstractMethodException

    @abstractmethod
    def run_one_pass(self)->ReduceResult:
        raise AbstractMethodException

    @abstractmethod
    def get_reduce_report(self)->str:
        raise AbstractMethodException

    def check_before_run(self)->None:   
        if self.save_input_snapshot:
            shutil.copy(self.input_path, self.work_dir_system.input_snapshot_path)
        check_dir(Path(self.output_path).parent)

