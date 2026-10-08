from reduction_analysis.ReduceUtil.RNOpParam import OnlyOneInstTask, RNOpParam, get_only_one_task_setting
from dataclasses import dataclass, replace

@dataclass
class ReduceNodeListV6CFG:
    enable_ddg_split: bool = True
    enable_whole_dd: bool = True

    enable_p3: bool = True
    enable_rev: bool = True
    enable_cfg_reduce: bool = True
    use_cf_reduction: bool = True

    def disable_fine_ns(self) -> None:
        self.enable_p3 = False
        self.enable_rev = False

    def copy(self) -> 'ReduceNodeListV6CFG':
        return replace(self)

ONE_V6_CFG = ReduceNodeListV6CFG()



def build_v6_cfg(
    op_param: RNOpParam,
) -> ReduceNodeListV6CFG:
    if get_only_one_task_setting() != OnlyOneInstTask.DISABLE:
        if get_only_one_task_setting() == OnlyOneInstTask.CORE:
            return ReduceNodeListV6CFG(
                enable_whole_dd=True,
                enable_p3=False,
                enable_rev=False,
                enable_cfg_reduce=False,
            )
        elif get_only_one_task_setting() == OnlyOneInstTask.P3:
            return ReduceNodeListV6CFG(
                enable_whole_dd=False,
                enable_p3=True,
                enable_rev=False,
                enable_cfg_reduce=False,
            )
        elif get_only_one_task_setting() == OnlyOneInstTask.REV:
            return ReduceNodeListV6CFG(
                enable_whole_dd=False,
                enable_p3=False,
                enable_rev=True,
                enable_cfg_reduce=False,
            )
        raise NotImplementedError('OnlyOneInstTask is not supported in v6 cfg builder yet')
    effective_fast_mode = _determine_fast_mode(op_param)
    if not effective_fast_mode:
        result_cfg =  ONE_V6_CFG.copy()
    else:
        new_cfg = ONE_V6_CFG.copy()
        if ONE_V6_CFG.enable_whole_dd:
            new_cfg.disable_fine_ns()
            new_cfg.enable_p3 = False
        new_cfg.enable_ddg_split = False
        new_cfg.use_cf_reduction = False
        result_cfg =  new_cfg
    #
    return result_cfg

def _determine_fast_mode(op_param: RNOpParam):
    effective_fast_mode = False
    # Centralized policy (moved from RNOpParam.should_use_fast_mode).
    if op_param.enable_fast_mode:
        if op_param.total_run_times is not None and op_param.cur_epoch_num is not None:
            if op_param.total_run_times == 0 and op_param.cur_epoch_num < 1:
                effective_fast_mode = True
    return effective_fast_mode


@dataclass
class V7Cfg:
    enable_ddg_split: bool = True

    @classmethod
    def from_reduce_cfg(
        cls,
        reduce_cfg,
    ) -> 'V7Cfg':
        return cls(
            enable_ddg_split=reduce_cfg.enable_ddg_split,
        )
