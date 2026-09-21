from .prob_dd import ProbDD
from typing import Callable

def get_test_cfg_func_for_dd(
    reduce_func:Callable
)->Callable:
    def test_config(cfg:list[int], config_id=None):
        success_ = reduce_func(cfg)
        if success_:
            return ProbDD.PASS
        else:
            return ProbDD.FAIL
    return test_config
