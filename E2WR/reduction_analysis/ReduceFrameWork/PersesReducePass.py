
from pathlib import Path
import os
import time
from typing import Union, Callable, Optional
from file_util import copy_file
from reduction_analysis.ReductionDescUtil.Oracle import Oracle
from reduction_analysis.ReducerPassUtil.ReduceResult import ExecResult, ExecStatus
from reduction_analysis.ReductionDescUtil.OneReducerDirSystem import OneReducerDirSystem
from reduction_analysis.ReducerPassUtil.ZReducerPass import ZReducerPass
from timeout_process import run_with_timeout


class PersesReducePass(ZReducerPass):
    def __init__(
        self,
        dir_system: OneReducerDirSystem,
        oracle_func:Callable,
        DEBUG: bool = False,
        name: str = 'PersesReducePass',
        # tmp_
    ):
        assert isinstance(oracle_func, Oracle)
        self.oracle_path = oracle_func.oracle_path
        self.cmd = f'python {os.environ.get("PERSES_REDUCE_SCRIPT", "perses_reduce.py")}  --wasm_file   {{input_path}}  --oracle_script {{oracle_script}} --output_path {{output_path}} --timeout {{timeout}}'

        self.to_stop_time = None
        super().__init__(
            dir_system=dir_system,
            oracle_func=oracle_func,
            name=name,
            DEBUG=DEBUG,
            instrument_manager=None
        )

    def reduce(self,
               cur_input_path: str,
               cur_output_path: str,
               timeout:float
               ) -> ExecResult:
        #
        start_time = time.time()
        self.logger.info(f'timeout: {timeout}')
        self.logger.info('start run perses reduce')
        cmd = self.cmd.format(
            input_path=cur_input_path,
            oracle_script=self.oracle_path,
            output_path=cur_output_path,
            timeout=timeout-5
        )
        print(f'cmd: {cmd}')
        self.logger.info(f'cmd: {cmd}')
        run_with_timeout(cmd, timeout=int(timeout-1))
        self.logger.info(' perses reduce done')

        if timeout is not None:
            self.to_stop_time = start_time + timeout
        input_size = Path(cur_input_path).stat().st_size
        if not Path(cur_output_path).exists():
            copy_file(cur_input_path, cur_output_path)
        output_size = Path(cur_output_path).stat().st_size
        print(f'input_size: {input_size}, output_size: {output_size}')
        self.logger.info(f'input_size: {input_size}, output_size: {output_size}')
        total_removed_num = input_size - output_size
        es = ExecStatus.SUCCESS if total_removed_num > 0 else ExecStatus.EXEC_FAILED
        exec_result = ExecResult(
            exec_status=es,
            exec_taken_time=time.time() - start_time,
            reduced_size_num=total_removed_num,
            reduced_inst_num=0
        )
        return exec_result
