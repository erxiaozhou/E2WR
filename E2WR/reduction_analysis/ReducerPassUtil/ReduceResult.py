from typing import Optional, Union
from enum import Enum


class ReduceResultType(Enum):
    UNKNOWN = 0


class ExecStatus(Enum):
    SUCCESS = 0
    EXEC_FAILED = 1
    TIMEOUT = 2


def determine_exec_status_from_run_cmd_output(run_cmd_output: dict) -> ExecStatus:
    if run_cmd_output['timeout_occurred']:
        return ExecStatus.TIMEOUT
    elif run_cmd_output['returncode'] != 0:
        return ExecStatus.EXEC_FAILED
    else:
        return ExecStatus.SUCCESS


class ReduceProcessStatus(Enum):
    FAIL_ORACLE = 0
    PASS_ORACLE = 1
    IGNORE_BY_SIZE = 2

class EffectStatus(Enum):
    ACCEPT = 0
    REJECT = 1


class ReducedNum:
    def __init__(self, n: Optional[int]):
        self.n = n

    def __str__(self):
        return str(self.n)

    def __repr__(self):
        return f'{self.__class__.__name__}(n={self.n})'

    def __eq__(self, other):
        return self.n == other.n

    def cal_speed(self, taken_time: float) -> Optional[float]:
        if self.n is None:
            return None
        return self.n / max(0.0000001, taken_time)

    def gt0(self) -> bool:
        return self.n is not None and self.n > 0


class ExecResult:
    def __init__(self,
                 exec_status: ExecStatus,
                 exec_taken_time: float,
                 reduced_size_num: Optional[int] = None,
                 reduced_inst_num: Optional[int] = None,
                 is_partial_by_timeout: bool = False
                 ):
        self.exec_status = exec_status
        self.exec_taken_time = exec_taken_time
        self.reduced_size_num = ReducedNum(reduced_size_num)
        self.reduced_inst_num = ReducedNum(reduced_inst_num)
        self.is_partial_by_timeout = is_partial_by_timeout

    def is_size_effective(self) -> bool:
        result = False
        if self.reduced_inst_num.gt0() or self.reduced_size_num.gt0():
            result = True
       
        return result

    def is_successful_exec(self) -> bool:
        return self.exec_status == ExecStatus.SUCCESS
    
    def is_timeout(self) -> bool:
        return self.exec_status == ExecStatus.TIMEOUT
    

    def __str__(self):
        if not self.is_successful_exec():
            return f'{self.__class__.__name__}(exec_status={self.exec_status}, exec_taken_time={self.exec_taken_time})'
        else:
            return self.__repr__()

    def __repr__(self):
        return f'{self.__class__.__name__}(exec_status={self.exec_status}, exec_taken_time={self.exec_taken_time}, reduced_size={self.reduced_size_num}, reduced_inst_num={self.reduced_inst_num}, is_partial_by_timeout={self.is_partial_by_timeout})'

    def __eq__(self, other):
        return self.exec_status == other.exec_status and \
            self.exec_taken_time == other.exec_taken_time and \
            self.reduced_size_num == other.reduced_size_num and \
            self.reduced_inst_num == other.reduced_inst_num and \
            self.is_partial_by_timeout == other.is_partial_by_timeout


class ReduceResult:
    def __init__(
        self,
        exec_result: ExecResult,
        reduce_process_status: ReduceProcessStatus,
        effect_status: EffectStatus,
        total_time: float,
    ):
        self.exec_result = exec_result
        self.reduce_process_status = reduce_process_status
        self.effect_status = effect_status
        self.total_time = total_time
        self.size_reduce_speed = self.exec_result.reduced_size_num.cal_speed(
            self.total_time)
        self.inst_reduce_speed = self.exec_result.reduced_inst_num.cal_speed(
            self.total_time)

    def actual_reduced_size(self) -> int:
        if not self.can_accept():
            return 0
        assert self.pass_oracle()
        n = self.exec_result.reduced_size_num.n
        assert n is not None, f'reduced_size_num is None, exec_result: {self.exec_result}'
        return max(n, 0)

    def can_accept(self) -> bool:
        return self.effect_status == EffectStatus.ACCEPT

    def pass_oracle(self) -> bool:
        return self.reduce_process_status == ReduceProcessStatus.PASS_ORACLE

    def __eq__(self, other):
        return self.exec_result == other.exec_result and \
            self.reduce_process_status == other.reduce_process_status and \
            self.total_time == other.total_time

    def __str__(self):
        return f'{self.__class__.__name__}(exec_result={self.exec_result}, reduce_process_status={self.reduce_process_status}, total_time={self.total_time})'

    def __repr__(self):
        return f'{self.__class__.__name__}(exec_result={self.exec_result}, reduce_process_status={self.reduce_process_status}, total_time={self.total_time})'
