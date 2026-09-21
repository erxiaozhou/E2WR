from pathlib import Path
import time
from typing import Callable
from timeout_process import run_with_timeout
from reduction_analysis.ReducerCommonConfig import ORACLE_TIMEOUT
from util.debug_util import ValidateCheckType, validate_wasm
from enum import Enum

class CheckType(Enum):
    SELF_CHECK = 0
    TEST_CHECK = 1
    OTHER = 2
    UNKNOWN = 3

    def to_str(self):
        if self == CheckType.SELF_CHECK:
            return 'SelfCheck'
        elif self == CheckType.TEST_CHECK:
            return 'TestCheck'
        elif self == CheckType.OTHER:
            return 'Other'
        elif self == CheckType.UNKNOWN:
            return 'DebugUnknown'
        else:
            raise Exception(f'Unsupported CheckType: {self}')


class OracleOutputValidResult:
    def __init__(self) -> None:
        self.output_valid_info = False


OUTPUT_VALID_CFG = OracleOutputValidResult()


def call_oracle(oracle_func: Callable, target_path: str, tag: CheckType) -> bool:
    if isinstance(oracle_func, Oracle):
        return oracle_func(target_path, tag)
    return oracle_func(target_path)


def pass_script_oracle(cmd_or_oracle: str, target_path: str, timeout: int = ORACLE_TIMEOUT) -> bool:
    assert Path(target_path).exists(
    ), f"The target_path {target_path} does not exist"
    cmd = f'{cmd_or_oracle} {target_path}'
    start_time = time.time()
    result = run_with_timeout(cmd, timeout=timeout)
    end_time = time.time()
    if result['timeout_occurred']:
        print('failed oracle due to timeout')
        # print('executed cmd', cmd)
        # print(f'The oracle check of {target_path} is timeout, taking {end_time - start_time} seconds')
        # copy_file(target_path, f'tt/timeout_oracle_check_{Path(target_path).name}')
        # print(f'It is saved to tt/timeout_oracle_check_{Path(target_path).name}')
        # input('Press Enter to continue...')
        return False

    return result['returncode'] == 0


class Oracle:
    def __init__(self,
                 oracle_path: str,
                 default_timeout: int = ORACLE_TIMEOUT,
                 DEBUG: bool = False
                 ):
        self.oracle_path: str = oracle_path
        self.default_timeout: int = default_timeout
        self.DEBUG: bool = DEBUG

    def __call__(self, target_path,
                 tag=CheckType.UNKNOWN) -> bool:
        assert Path(target_path).exists(
        ), f"The target_path {target_path} does not exist"
        if OUTPUT_VALID_CFG.output_valid_info:
            # print(f'Oracle checking {target_path} ...')
            print(f'Is valid for oracle: {validate_wasm(target_path, tag=ValidateCheckType.OTHER)}')
        timeout = self.default_timeout
        start_time = time.time()
        result = pass_script_oracle(self.oracle_path, target_path, timeout)
        end_time = time.time()
        print(
            f'Time Cost {end_time - start_time}s, result: {result} ;; {tag}')
        if self.DEBUG:
            if result:
                print(
                    f'Pass the oracle, taking {end_time - start_time} seconds')
        return result
