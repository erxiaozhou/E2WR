from ..ReducerPassUtil.ReduceResult import ExecResult, ReduceResult, ReduceProcessStatus, EffectStatus
from typing import Callable
from reduction_analysis.ReductionDescUtil.Oracle import CheckType, call_oracle


def get_reduce_result(
    output_path:str, 
    exec_result:ExecResult, 
    pass_oracle_func:Callable,
    time_cost:float,
    require_smaller_size:bool=True
)->ReduceResult:
    pass_oracle = None
    if require_smaller_size:
        if not exec_result.is_size_effective():
            pass_oracle = ReduceProcessStatus.IGNORE_BY_SIZE
    if pass_oracle is None:
        if not call_oracle(pass_oracle_func, output_path, CheckType.SELF_CHECK):
            pass_oracle = ReduceProcessStatus.FAIL_ORACLE
        else:
            pass_oracle = ReduceProcessStatus.PASS_ORACLE
    result =  ReduceResult(
        exec_result=exec_result,
        reduce_process_status=pass_oracle,
        total_time=time_cost,
        effect_status=EffectStatus.ACCEPT if pass_oracle == ReduceProcessStatus.PASS_ORACLE else EffectStatus.REJECT
    )
    return result
