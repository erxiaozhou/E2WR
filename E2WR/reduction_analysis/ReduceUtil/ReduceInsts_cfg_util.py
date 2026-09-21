from extract_block_mutator.InstUtil import Inst
from reduction_analysis.Instrumentation.DumpData import DumpSeq
from reduction_analysis.ASTInfo.AST import NodeList

from typing import Optional
from .ReduceInsts_V5_util import OneElem
from reduction_analysis.ReduceUtil.RNOpParam import OnlyOneInstTask, RNOpParam, get_only_one_task_setting
from reduction_analysis.ReduceUtil.InstrumentationInstStrategy_not_used import GenStackValueInsts, ProcessRefStrategy
from dataclasses import dataclass, field, replace

ZDEBUG = True

@dataclass
class ReduceNodeListV6CFG:
    enable_ddg_split: bool = True
    enable_dd: bool = True
    enable_whole_dd: bool = True
    enable_minimal_replacement: bool = True

    enable_fine_ns: bool = True
    enable_p3: bool = True
    enable_rev: bool = True
    enable_cfg_reduce: bool = True
    use_cf_reduction: bool = True

    use_VP: bool = True

    def disable_fine_ns(self) -> None:
        self.enable_fine_ns = False
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
                enable_dd=True,
                enable_ddg_split=True,
                enable_whole_dd=True,
                enable_minimal_replacement=True,
                enable_fine_ns=False,
                enable_p3=False,
                enable_rev=False,
                enable_cfg_reduce=False,
                use_VP=True,
            )
        elif get_only_one_task_setting() == OnlyOneInstTask.P3:
            return ReduceNodeListV6CFG(
                enable_dd=False,
                enable_ddg_split=False,
                enable_whole_dd=False,
                enable_minimal_replacement=False,
                enable_fine_ns=False,
                enable_p3=True,
                enable_rev=False,
                enable_cfg_reduce=False,
                use_VP=False,
            )
        elif get_only_one_task_setting() == OnlyOneInstTask.REV:
            return ReduceNodeListV6CFG(
                enable_dd=False,
                enable_ddg_split=False,
                enable_whole_dd=False,
                enable_minimal_replacement=False,
                enable_fine_ns=False,
                enable_p3=False,
                enable_rev=True,
                enable_cfg_reduce=False,
                use_VP=False,
            )
        elif get_only_one_task_setting() == OnlyOneInstTask.FULL:
             return ReduceNodeListV6CFG(
                enable_dd=True,
                enable_ddg_split=True,
                enable_whole_dd=True,
                enable_minimal_replacement=True,
                enable_fine_ns=True,
                enable_p3=True,
                enable_rev=True,
                enable_cfg_reduce=True,
                use_VP=True,
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
    # if ONE_V6_CFG.use_cf_reduction:
    # use_VP = cur_epoch_num is None or cur_epoch_num 
    if ONE_V6_CFG.use_VP:
        result_cfg.use_VP = _determine_use_VP(op_param)

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
    

def _determine_use_VP(op_param: RNOpParam):
    # return False
    # return True
    use_VP = False
    
    if op_param.cur_epoch_num is None:
        use_VP = True
    elif op_param.total_run_times is None:
        use_VP = True
    elif  op_param.total_run_times != 0:
        use_VP = True
    elif op_param.cur_epoch_num > 2:
        use_VP = True
    #     if op_param.cur_epoch_num %2==1:
    #         use_VP = True
    
    # else:
    #     if ZDEBUG:
    #         if op_param.task.get_total_inst_num() < 2000:
    #             use_VP = True
        # elif op_param.task.get_total_inst_num() < 200:
        #     use_VP = True
    return use_VP


@dataclass
class V7Cfg:
    enable_dd: bool = True
    enable_ddg_split: bool = True
    enable_minimal_replacement: bool = True
    use_VP: bool = True
    elem_ref_values: Optional[dict[OneElem, DumpSeq]] = None

    # Internal memoization cache.
    # Keep it out of __init__ to preserve the old public constructor signature.
    elem_idx2val: dict[tuple[OneElem, int], Inst] = field(
        default_factory=dict,
        init=False,
        repr=False,
    )

    @classmethod
    def from_reduce_cfg(
        cls,
        reduce_cfg,
        *,
        elem_ref_values: Optional[dict[OneElem, DumpSeq]] = None,
    ) -> 'V7Cfg':
        return cls(
            enable_dd=reduce_cfg.enable_dd,
            enable_ddg_split=reduce_cfg.enable_ddg_split,
            enable_minimal_replacement=reduce_cfg.enable_minimal_replacement,
            use_VP=reduce_cfg.use_VP,
            elem_ref_values=elem_ref_values,
        )

    def get_replacement_with_val(self, elem:OneElem, val_idx:int)->Optional[Inst]:
        if self.elem_ref_values is None:
            return None
        key = (elem, val_idx)
        if key in self.elem_idx2val:
            return self.elem_idx2val[key]

        dump_seq = self.elem_ref_values.get(elem)
        if dump_seq is None:
            return None
        if val_idx < 0 or val_idx >= len(dump_seq.all_types):
            return None
        if not dump_seq.has_value_mask[val_idx]:
            return None

        # DumpSeq.values is compacted to only entries with has_value_mask == True.
        value_offset = -1
        for i in range(val_idx + 1):
            if dump_seq.has_value_mask[i]:
                value_offset += 1
        if value_offset < 0 or value_offset >= len(dump_seq.values):
            return None

        stack_type = dump_seq.all_types[val_idx]
        stack_val = dump_seq.values[value_offset]

        # Reuse the same value->const instruction generation logic as
        # `reduction_analysis/Instrumentation/CallReturnMutationUtil.py`.
        strategy = GenStackValueInsts(
            stack_types=[stack_type],
            stack_vals=[stack_val],
            drop_num=0,
            process_ref_strategy=ProcessRefStrategy.Skip,
        )
        if strategy.can_skip():
            return None
        insts = strategy.get_insts_for_replace()
        if len(insts) != 1:
            return None
        inst = insts[0]
        self.elem_idx2val[key] = inst
        return inst
