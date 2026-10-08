from reduction_analysis.ASTInfo.AST import ASTNodeLoc
from reduction_analysis.Instrumentation.ValueProbeInstrument import ValueProbeManager
from reduction_analysis.ParserModification import WMSnapshot


def get_un_exec_func_idxs(
    input_snapshot:WMSnapshot,
    instrument_manager: ValueProbeManager,
)->set[int]:
    return get_un_exec_func_idxs_by_freq(
        input_snapshot=input_snapshot,
        instrument_manager=instrument_manager
    )


def get_un_exec_func_idxs_by_freq(
    input_snapshot:WMSnapshot,
    instrument_manager: ValueProbeManager
)->set[int]:
    func_num = len(input_snapshot.parser.defined_funcs)
    locs = set(ASTNodeLoc(func_idx=i, inst_idx=0) for i in range(func_num))
    loc2freq = instrument_manager.instrument_and_get_exec_freq(input_snapshot, locs)
    un_exec_func_idxs = set()
    for loc, freq in loc2freq.items():
        if freq == 0:
            un_exec_func_idxs.add(loc.func_idx)
    return un_exec_func_idxs
