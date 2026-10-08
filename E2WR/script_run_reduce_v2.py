#!/usr/bin/env python3

import argparse
import cProfile
import os
import time
from pathlib import Path
from typing import Callable
from reduction_analysis.ReduceFrameWork.DefaultZReducerFactory import get_default_z_reducer_pac
from reduction_analysis.ProbDDUtil.ProbDDFactory import ProbDDFactory
from reduction_analysis.ReduceUtil.RNOpParam import ONLY_ONE_TASK_SETTING, OnlyOneInstTask
from reduction_analysis.ReductionDescUtil.Oracle import Oracle
import pstats
from pstats import SortKey

from reduction_analysis.TopReducer.Reducer import FrameworkReducerDirSystem
from reduction_analysis.FrameWorkReducerCfg import FrameWorkReducerCfg
from reduction_analysis.TopReducer.MultiPassReducer import get_framework_reducer
from reduction_analysis.ReducerCommonConfig import FRAMEWORK_IN_DEBUG, PASS_TIMEOUT
from reduction_analysis.ReductionDescUtil.ReduceLimit import ReduceLimit
from reduction_analysis.ReducerPassUtil.ReducePass import ReduceAndCheckPass
from file_util import get_logger
from reduction_analysis.ReducerPassUtil.ZReducerPass import  ZReducerType
import random
import numpy as np
from reduction_analysis.ReduceUtil.ReduceInsts_cfg_util import ONE_V6_CFG
from reduction_analysis.callsite_reduction import CallsiteRepStrategy
# FinalPolishPass
# from 

def parse_args():
    parser = argparse.ArgumentParser(description='Run Wasm file Reducer')
    parser.add_argument('--input', '-i', required=True, help='Input Wasm file path')
    parser.add_argument('--output', '-o', required=True, help='Output Wasm file path')
    parser.add_argument('--init_p0', type=float, default=0.1, help='Initial p0, default 0.1')
    parser.add_argument('--to-test-func-name', '-tn', required=True, help='Function name to test')
    parser.add_argument('--oracle', required=True, help='Oracle script path')
    parser.add_argument('--work-dir', '-w', default='work_dir', help='Temporary file working directory')
    parser.add_argument('--time-limit', '-t', type=int, default=9000, help='Time limit(seconds), default 3600 seconds')
    parser.add_argument('--pass-timeout', type=int, default=PASS_TIMEOUT, help=f'Single pass execution timeout(seconds), default {PASS_TIMEOUT} seconds')
    parser.add_argument('--debug', '-d', action='store_true', default=False, help='Enable debug mode')
    # some parameters for option combinations
    parser.add_argument('--full_remove_uur', action='store_true', help='Disable the unused-def reducer (UUR) pass')
    parser.add_argument('--full_wo_update_probdd_p0', action='store_true', help='Use full std pass with probdd p0')
    
    parser.add_argument('--full_wo_final_polish', action='store_true', help='Use full std pass with final polish')
    parser.add_argument('--disable_fine_ns', action='store_true', help='disable fine grained in NS')
    parser.add_argument('--disable_whole_dd', action='store_true', help='disable the whole DD in NS')
    parser.add_argument('--disable_polish_return', action='store_true', help='disable polish return type in final polish')
    parser.add_argument('--disable_inline', action='store_true', help='disable inline in final polish')
    parser.add_argument('--disable_size_polish', action='store_true', help='disable size polish in final polish')
    # Add random seed parameter
    parser.add_argument('--seed', type=int, default=42, help='Random seed for controlling randomness, default is None (use system time)')
    parser.add_argument('--disable_callsite_reduction', action='store_true', help='Do not replace callsite')
    # parser.add_argument('--disable_vp_callsite_reduction', action='store_true', help='Do not replace callsite with dumped values')
# 
    parser.add_argument('--force_inst_mutation', default='disable', help='Temporary file working directory')
    
    return parser.parse_args()


def setup_passes(
    *,
    oracle_func: Callable[[str], bool],
    reducer_cfg: FrameWorkReducerCfg,
    args,
    to_test_func_name: str,
    polish_return_type: bool = True,
    enable_inline: bool = True,
    enable_size_polish: bool = True,
) -> list[ReduceAndCheckPass]:
    all_passes:list[ReduceAndCheckPass] = []
    work_dir = reducer_cfg.tmp_dir
    
    # 
    to_append_reducer_types = [ZReducerType.NODE_SHRINK]
    if reducer_cfg.use_remove_unused_elem:
        to_append_reducer_types.append(ZReducerType.UNUSED_DEF)
    if reducer_cfg.use_final_polish:
        to_append_reducer_types.append(ZReducerType.FINAL_POLISH)
    #
    if args.disable_callsite_reduction:
        cr_strategy = CallsiteRepStrategy.DISABLE
    else:
        cr_strategy = CallsiteRepStrategy.VP
    cr_strategy = CallsiteRepStrategy.TY_ONLY
    for reducer_type in to_append_reducer_types:
        cur_pass_and_check = get_default_z_reducer_pac(
            work_dir=work_dir,
            oracle_func=oracle_func,
            debug=reducer_cfg.debug,
            reducer_type=reducer_type,
            default_log_base_dir=reducer_cfg.log_dir,
            to_test_func_name=to_test_func_name,
            cr_strategy=cr_strategy,
            polish_return_type=polish_return_type,
            enable_inline=enable_inline,
            enable_size_polish=enable_size_polish,
        )
        all_passes.append(cur_pass_and_check)

    print(f"Loaded {len(all_passes)} passes in total")
    return all_passes


def main():
    args = parse_args()
    # Apply V6 reducer toggles (global config used by NodeShrink/V6 pipeline)
    if args.disable_fine_ns:
        ONE_V6_CFG.disable_fine_ns()
    ONE_V6_CFG.enable_whole_dd = not args.disable_whole_dd
    #
    force_inst_mutation = args.force_inst_mutation.lower()
    if force_inst_mutation != 'disable':
        if force_inst_mutation == 'p3':
            ONLY_ONE_TASK_SETTING[0] = OnlyOneInstTask.P3
        elif force_inst_mutation == 'core':
            ONLY_ONE_TASK_SETTING[0] = OnlyOneInstTask.CORE
        elif force_inst_mutation == 'rev':
            ONLY_ONE_TASK_SETTING[0] = OnlyOneInstTask.REV
        else:
            raise ValueError(f"Invalid value for --force_inst_mutation: {args.force_inst_mutation}. Valid options are 'disable', 'p3', 'core', 'rev'.")
    
    #
    print(
        "V6 cfg applied: "
        f"enable_whole_dd={ONE_V6_CFG.enable_whole_dd}"
    )
    # Set random seed
    random.seed(args.seed)
    np.random.seed(args.seed)
    print(f"Random seed set: {args.seed}")
    # 
    FRAMEWORK_IN_DEBUG[0] = args.debug
    # 
    
    # Check if input file and Oracle script exist
    if not os.path.exists(args.input):
        print(f"Error: Input file {args.input} does not exist")
        return 1
    
    if not os.path.exists(args.oracle):
        print(f"Error: Oracle script {args.oracle} does not exist")
        return 1
    
    oracle_func = Oracle(args.oracle)
    
    # Prepare log directory
    work_dir_system = FrameworkReducerDirSystem(work_dir=Path(args.work_dir))
    
    
    # Setup passes
    framework_reducer_cfg: FrameWorkReducerCfg = get_framework_cfg(args, work_dir_system)
    ProbDDFactory.set_probdd_factory(
        use_p0_pred=not args.full_wo_update_probdd_p0,
        initialP=args.init_p0,
        logger=get_logger('dd_log', log_file_name=str(work_dir_system.log_base_dir / "dd_log.log"))
    )
    
    #
    passes = setup_passes(
        oracle_func=oracle_func,
        reducer_cfg=framework_reducer_cfg,
        args=args,
        to_test_func_name=args.to_test_func_name,
        polish_return_type=not args.disable_polish_return,
        enable_inline=not args.disable_inline,
        enable_size_polish=not args.disable_size_polish,
    )
    # Print configuration information
    print("\n==== Wasm Reducer Configuration ====")
    print(f"Input file: {args.input}")
    print(f"Output file: {args.output}")
    print(f"Oracle script: {args.oracle}")
    print(f"Working directory: {work_dir_system}")
    print(f"Time limit: {args.time_limit} seconds")
    print(f'Reducer Framework Configuration: {framework_reducer_cfg}')
   
    print(f"Random seed: {args.seed if args.seed is not None else 'not set (use system time)'}")
    print("==========================\n")
    # assert args.reducer_type == 'random'
    # Initialize reducer
    reducer = get_framework_reducer(
            input_path=args.input,
            output_path=args.output,
            oracle_func=oracle_func,
            work_dir_system=work_dir_system,
            reduce_limit=ReduceLimit(
                timeout=args.time_limit,
            ),
            passes=passes,
    )
    
    print(f"\nStarting Wasm file reduction ...")
    start_time = time.time()
    if not args.debug:
        reducer.run()
    else:
        try:
            # reducer.run()
            profiler = cProfile.Profile()
            profiler.enable()
            reducer.run()
                
            # Stop profiling
            profiler.disable()
            
            # Save results to file
            profiler.dump_stats('reduce_performance.prof')
            
            # Output sorted statistics
            with open('reduce_stats.txt', 'w') as f:
                stats = pstats.Stats(profiler, stream=f).sort_stats(SortKey.CUMULATIVE)
                stats.print_stats(100)  # Only print top 50 lines
            # 
            end_time = time.time()
            elapsed = end_time - start_time
            
            # Print results
            print("\n==== Reduction Completed ====")
            print(f"Total time: {elapsed:.2f} seconds")
            # Print Reduction report
            print("\n==== Reduction Report ====")
            print(reducer.get_reduce_report())
            
            return 0
        except Exception as e:
            print(f"\nError: Exception occurred during reduction process: {e}")
            import traceback
            traceback.print_exc()
            return 1

def get_framework_cfg(args, work_dir_system:FrameworkReducerDirSystem):
    framework_reducer_cfg: FrameWorkReducerCfg = FrameWorkReducerCfg.default_cfg(
        tmp_dir=work_dir_system.tmp_dir,
        log_dir=work_dir_system.log_base_dir,
        )
    if args.full_remove_uur:
        framework_reducer_cfg.use_remove_unused_elem = False
    elif args.full_wo_final_polish:
        framework_reducer_cfg.use_final_polish = False
    return framework_reducer_cfg

if __name__ == "__main__":
    exit(main())
