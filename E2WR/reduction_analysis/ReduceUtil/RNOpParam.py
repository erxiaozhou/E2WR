from dataclasses import dataclass
from typing import Optional
from reduction_analysis.ReduceFrameWork.ASTNodePool import ToReduceTask
from enum import Enum, auto

class OnlyOneInstTask(Enum):
    CORE = auto()
    P3 = auto()
    REV = auto()
    FULL = auto()
    DISABLE = auto()


ONLY_ONE_TASK_SETTING = [OnlyOneInstTask.DISABLE]
def get_only_one_task_setting():
    return ONLY_ONE_TASK_SETTING[0]

@dataclass(slots=True)
class RNOpParam:
    task: ToReduceTask
    cur_epoch_num: Optional[int] = None
    total_run_times: Optional[int] = None
    cur_input_path: Optional[str] = None
    rest_time: Optional[float] = None
    enable_fast_mode: bool = True
    enable_vp:bool=True
