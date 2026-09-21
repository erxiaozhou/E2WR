from reduction_analysis.ReductionDescUtil.ReduceLimit import ReduceLimit
from reduction_analysis.ReducerPassUtil.ReducePass import ReduceAndCheckPass
from .Reducer import FrameworkReducerDirSystem
from .MultiPassReducerBase import MPFrameworkReducerBase
from typing import Optional, Union, Callable
from ..ReducerPassUtil.ReduceResult import ReduceResult
from pathlib import Path


STATE_TRANSITIONS: dict[str, str] = {
    'NodeShrink': 'UnusedDefReducer',
    'UnusedDefReducer': 'FinalPolishPass',
    'FinalPolishPass': 'NodeShrink',
}
RERUN_NO_FURTHER_GAIN_PASSES = ['UnusedDefReducer', 'NodeShrink', 'FinalPolishPass']

# RERUN_NO_FURTHER_GAIN_PASSES = ['UnusedDefReducer']


class RandomReducer(MPFrameworkReducerBase):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # Seed the "previous" pass to FinalPolishPass so the first transition
        # (STATE_TRANSITIONS['FinalPolishPass'] == 'NodeShrink') picks NodeShrink.
        self.last_selected_pass_name: str = 'FinalPolishPass'
        self.pass_name_to_idx = {pass_.name: idx for idx, pass_ in enumerate(self.passes)}
        self.saturated_passes: set[str] = set()

    def _update_saturation(self, pass_name: str, cause_update: bool, is_partial_by_timeout: bool) -> None:
        if not cause_update:
            return
        self.saturated_passes.clear()
        if pass_name in RERUN_NO_FURTHER_GAIN_PASSES and not is_partial_by_timeout:
            self.saturated_passes.add(pass_name)

    def post_process(self, may_reduced_case: str, pass_reduce_result: ReduceResult) -> bool:
        cause_update = super().post_process(may_reduced_case, pass_reduce_result)
        self._update_saturation(
            self.last_selected_pass_name,
            cause_update,
            pass_reduce_result.exec_result.is_partial_by_timeout,
        )
        return cause_update

    def _find_next_pass(self, candi_names: list[str]) -> Optional[int]:
        assert len(candi_names) > 0
        visited = set()
        current_state = self.last_selected_pass_name
        candi_set = set(candi_names)
        while current_state not in visited:
            visited.add(current_state)
            next_name = STATE_TRANSITIONS.get(current_state)
            if next_name is None:
                break
            if next_name in candi_set:
                return self.pass_name_to_idx.get(next_name)
            current_state = next_name
        raise ValueError("Cannot find a valid pass")

    def select_pass(self, candi_names: list[str], last_result: Optional[ReduceResult] = None) -> ReduceAndCheckPass:
        for skip_name in list(self.saturated_passes):
            if skip_name in candi_names:
                self.tried_since_last_update.add(skip_name)
        effective_candi = [n for n in candi_names if n not in self.tried_since_last_update]
        assert effective_candi, (
            "no runnable pass: every candidate is already in tried_since_last_update; "
            "this should be unreachable since saturated passes are folded into tried and "
            "the run loop breaks when untried becomes empty"
        )
        selected_idx = self._find_next_pass(effective_candi)
        assert selected_idx is not None
        selected = self.passes[selected_idx]
        self.last_selected_pass_name = selected.name
        self.select_times += 1
        return selected
    def check_before_run(self):
        if not self.passes:
            raise ValueError("No passes available for reduce")
        super().check_before_run()


def get_framework_reducer(
        input_path: Union[str, Path],
        output_path: Union[str, Path],
        oracle_func: Callable,
        work_dir_system: FrameworkReducerDirSystem,
        reduce_limit: ReduceLimit,
        passes: list[ReduceAndCheckPass],
) -> MPFrameworkReducerBase:
    return RandomReducer(
        input_path=input_path,
        output_path=output_path,
        oracle_func=oracle_func,
        work_dir_system=work_dir_system,
        reduce_limit=reduce_limit,
        passes=passes,
    )
