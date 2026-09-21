from __future__ import annotations
from pathlib import Path
import time
from typing import Callable, Optional

from file_util import copy_file
from util.debug_util import ValidateCheckType, validate_wasm
from reduction_analysis.ParserModification import WMSnapshot, apply_mutation_and_encode_keep_snapshot
from reduction_analysis.ParserModificationUtil import MutationBatch
from reduction_analysis.ReductionDescUtil.Oracle import CheckType, call_oracle


class BaseReducer:
    def __init__(
        self,
        tmp_used_path: str | Path,
        oracle_func: Callable,
        DEBUG: bool = False,
    ) -> None:
        self.tmp_used_path = str(tmp_used_path)
        self.oracle_func = oracle_func
        self.DEBUG = DEBUG

    def debug_validate_tmp(self) -> None:
        if self.DEBUG:
            assert self.tmp_file_is_valid(ValidateCheckType.TEST_CHECK)

    def tmp_file_is_valid(self, tag: ValidateCheckType = ValidateCheckType.TEST_CHECK) -> bool:
        return validate_wasm(self.tmp_used_path, print_detail_reason=self.DEBUG, tag=tag)

    def oracle_passes(self, tag: CheckType = CheckType.TEST_CHECK) -> bool:
        return call_oracle(self.oracle_func, self.tmp_used_path, tag)

    def commit_tmp_to(self, output_path: str | Path) -> None:
        # print(f'Committing {self.tmp_used_path} to {output_path}')
        copy_file(self.tmp_used_path, str(output_path))

    def mutate_test_and_commit(
        self,
        snapshot: WMSnapshot,
        mutation_batch: MutationBatch,
        output_path: str | Path,
        on_commit: Optional[Callable[[], None]] = None,
    ) -> Optional[WMSnapshot]:
        new_snapshot = apply_mutation_and_encode_keep_snapshot(
            snapshot,
            mutation_batch,
            self.tmp_used_path,
        )
        self.debug_validate_tmp()
        if self.oracle_passes():
            self.commit_tmp_to(output_path)
            if on_commit is not None:
                on_commit()
            return new_snapshot
        return None


class BaseReducerWithTimeout(BaseReducer):
    def __init__(
        self,
        tmp_used_path: str | Path,
        oracle_func: Callable,
        DEBUG: bool = False,
    ) -> None:
        super().__init__(tmp_used_path, oracle_func, DEBUG)
        self._deadline: Optional[float] = None
        self.start_time: Optional[float] = None

    def set_duration_sec_and_start_now(self, duration_sec: Optional[float]) -> None:
        if duration_sec is not None:
            self.start_time = time.time()
            self._deadline = self.start_time + duration_sec

    def is_timeout_with_system_time(self) -> bool:
        now_ts = time.time()
        return self._is_timeout(now_ts)

    def _is_timeout(self, now_ts: float) -> bool:
        return self._deadline is not None and now_ts > self._deadline
