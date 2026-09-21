from reduction_analysis.ReduceFrameWork.ASTNodePool import ToReduceTask

from ..ASTInfo.AST import ASTINode
from ..ASTState import ASTState
from typing import Union
from file_util import copy_file, get_time_string
from pathlib import Path
import traceback
import signal

def store_exception_case(tester, cur_input_path:str, tmp_dir:Union[str, Path], task:ToReduceTask, ast_state:ASTState, e:Exception):
    traceback.print_exc()
    tmp_dir = Path(tmp_dir)
    time_ttr = get_time_string()
    target_path = tmp_dir / f'{time_ttr}_ori.wasm'
    copy_file(cur_input_path, target_path)
    print(f'Error In {tester.__class__.__name__}, during processing node: {task.get_nodes_info()}, saved to {target_path}: {e}')
    try:
        ast_state.to_file(target_path)
    except Exception as e:
        print(f'Error In {tester.__class__.__name__} during processing node: {task.get_nodes_info()}: {e}')
        traceback.print_exc()


class TimeoutHandler:
    def __init__(self, timeout_seconds):
        self.timeout_seconds = timeout_seconds
        
    def __enter__(self):
        if self.timeout_seconds is not None:
            signal.signal(signal.SIGALRM, self._timeout_handler)
            signal.alarm(int(self.timeout_seconds))
        return self
        
    def __exit__(self, exc_type, exc_val, exc_tb):
        if self.timeout_seconds is not None:
            signal.alarm(0) 
            
    def _timeout_handler(self, signum, frame):
        raise TimeoutError(f"Operation timed out after {self.timeout_seconds} seconds")

