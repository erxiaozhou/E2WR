from pathlib import Path

from reduction_analysis.ReduceUtil.NonInstrumentationInstStrategy import ReduceStrategyType
from reduction_analysis.ReduceFrameWork.FinalPolishPass import FinalPolishPass
from reduction_analysis.ReduceFrameWork.NodeShrinkPass import NodeShrinkPass
from reduction_analysis.ReduceFrameWork.PersesReducePass import PersesReducePass
from reduction_analysis.ReduceUtil.ReduceStrategy import SpecificTypeReplacementGen
from reduction_analysis.ReductionDescUtil.OneReducerDirSystem import OneReducerDirSystem
from reduction_analysis.callsite_reduction import CallsiteRepStrategy
from ..ReducerPassUtil.ZReducerPass import ZReducerPassAndCheck, ZReducerType, ZReducerPass
from reduction_analysis.ReduceFrameWork.DefinitionReducer.UnusedDefReducer import UnusedDefReducer
from typing import Callable, Optional


def get_default_z_reducer(
    work_dir: Path,
    oracle_func: Callable,
    debug: bool,
    reducer_type: ZReducerType,
    default_log_base_dir: Optional[Path] = None,
    to_test_func_name: Optional[str] = None,
    cr_strategy: CallsiteRepStrategy = CallsiteRepStrategy.VP,
    polish_return_type: bool = True,
    enable_inline: bool = True,
    enable_size_polish: bool = True,
) -> ZReducerPass:
    log_path = None if default_log_base_dir is None else str(
        default_log_base_dir / f"{reducer_type.value}.log")
    #
    if reducer_type == ZReducerType.UNUSED_DEF:
        return UnusedDefReducer(
            dir_system=OneReducerDirSystem.from_framework_dir(
                work_dir=work_dir,
                name=reducer_type.value,
                log_path=log_path
            ),
            oracle_func=oracle_func,
            DEBUG=debug
        )
    elif reducer_type == ZReducerType.FINAL_POLISH:
        return FinalPolishPass(
            dir_system=OneReducerDirSystem.from_framework_dir(
                work_dir=work_dir,
                name=reducer_type.value,
                log_path=log_path
            ),
                to_test_func_name=to_test_func_name,
            oracle_func=oracle_func,
            DEBUG=debug,
            polish_return_type=polish_return_type,
            enable_inline=enable_inline,
            enable_size_polish=enable_size_polish,
        )
    elif reducer_type == ZReducerType.NODE_SHRINK:
        #
        vp_pass = NodeShrinkPass(
            dir_system=OneReducerDirSystem.from_framework_dir(
                work_dir=work_dir,
                name=reducer_type.value,
                log_path=log_path
            ),
            oracle_func=oracle_func,
            DEBUG=debug,
            ignore_exceptions=True,
            name=reducer_type.value,
            to_test_func_name=to_test_func_name,
            cr_strategy=cr_strategy,
        )
        return vp_pass
    raise NotImplementedError


def get_default_z_reducer_pac(
    work_dir: Path,
    oracle_func: Callable,
    debug: bool,
    reducer_type: ZReducerType,
    default_log_base_dir: Optional[Path] = None,
    to_test_func_name: Optional[str] = None,
    cr_strategy: CallsiteRepStrategy = CallsiteRepStrategy.DISABLE,
    polish_return_type: bool = True,
    enable_inline: bool = True,
    enable_size_polish: bool = True,
) -> ZReducerPassAndCheck:
    # raise NotImplementedError
    return ZReducerPassAndCheck(
        z_probe_pass=get_default_z_reducer(
            work_dir=work_dir,
            oracle_func=oracle_func,
            debug=debug,
            reducer_type=reducer_type,
            default_log_base_dir=default_log_base_dir,
            to_test_func_name=to_test_func_name,
            cr_strategy=cr_strategy,
            polish_return_type=polish_return_type,
            enable_inline=enable_inline,
            enable_size_polish=enable_size_polish,
        )
    )
