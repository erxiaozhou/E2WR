from typing import Optional, Callable

from reduction_analysis.Instrumentation.ValueProbeInstrument import ValueProbeManager
from reduction_analysis.ParserModification import WMSnapshot
from reduction_analysis.ProbDDUtil.ProbDDFactory import ProbDDFactory
from reduction_analysis.ReduceFrameWork.IdUnexecFuncUtil import get_un_exec_func_idxs
from reduction_analysis.ReduceUtil.FuncNodeRemover import remove_funcs_by_dd


class TYRemoveFuncPass:
    def __init__(
        self,
        oracle: Callable,
        tmp_used_path: str,
        DEBUG: bool,
    ):
        self.oracle = oracle
        self.tmp_used_path = tmp_used_path
        self.DEBUG = DEBUG

    def ensure_pass_run(
        self,
        input_snapshot: WMSnapshot,
        cur_output_path: str,
        *,
        timeout: Optional[float] = None,
        weights: Optional[dict[int, float]] = None,
        proi_func_idxs: Optional[set[int]] = None,
        callsite_as_unreachable: bool=False,
    ) -> tuple[WMSnapshot, set[int]]:
        
        weights = {}
        for defined_func_idx in range(len(input_snapshot.parser.defined_funcs)):
            # weights[defined_func_idx] = len([i for i in input_snapshot.parser.defined_funcs[defined_func_idx].insts if 'call' in i.opcode_text])
            weights[defined_func_idx] = len(input_snapshot.parser.defined_funcs[defined_func_idx].insts) 
        new_snapshot, removed_func_idxs = remove_funcs_by_dd(
            tmp_used_path=self.tmp_used_path,
            oracle_func=self.oracle,
            DEBUG=self.DEBUG,
            input_snapshot=input_snapshot.copy(),
            cur_output_path=cur_output_path,
            weights=weights,
            proi_func_idxs=proi_func_idxs,
            timeout=timeout,
            callsite_as_unreachable=callsite_as_unreachable,
        )
        return new_snapshot, removed_func_idxs

    def remove_proi_unexec_funcs(
        self,
        *,
        cur_input_path: str,
        cur_output_path: str,
        input_snapshot: WMSnapshot,
        instrument_manager: ValueProbeManager,
        un_exec_func_idxs:Optional[set[int]] = None,
        timeout: Optional[float] = None,
        callsite_as_unreachable=False
    ) -> tuple[WMSnapshot, set[int]]:
        all_removed_func_idxs: set[int] = set()
        if un_exec_func_idxs is None:
            try:
                un_exec_func_idxs = get_un_exec_func_idxs(
                    input_snapshot=input_snapshot,
                    instrument_manager=instrument_manager,
                )
            except Exception as e:
                print('Get unexec func idxs exception: ', e)
                un_exec_func_idxs = set()

        # print('un_exec_func_idxs is ', un_exec_func_idxs)
        print(f'There are {len(un_exec_func_idxs)} unexec func idxs')
        ProbDDFactory.use_a_initp_temp(0.0001)
        if un_exec_func_idxs:
            input_snapshot, removed_func_idxs = self.ensure_pass_run(
                input_snapshot=input_snapshot,
                cur_output_path=cur_output_path,
                proi_func_idxs=set(un_exec_func_idxs),
                timeout=timeout,
                callsite_as_unreachable=callsite_as_unreachable,
            )
            all_removed_func_idxs.update(removed_func_idxs)
        ProbDDFactory.reset_initp()
        print(f'After proi, there are {len(all_removed_func_idxs)} functions removeed ')
        return input_snapshot, all_removed_func_idxs

