import time
from typing import Optional

from reduction_analysis.ParserModification import WMSnapshot
from reduction_analysis.ASTInfo.AST import ASTNodeLoc, NodeList
from reduction_analysis.Instrumentation.DumpData import DumpSeq, OneDumpData
from reduction_analysis.Instrumentation.ProbeType import ProbeType
from reduction_analysis.Instrumentation.ValueProbeInstrument import ProbeDesc, ValueProbeManager
from reduction_analysis.ReduceUtil.ReduceInsts_V5_util import OneElem


class ToProbeLocInfo:
    def __init__(self, loc:ASTNodeLoc, stack_types_to_dump:list[str]):
        self.loc = loc
        self.stack_types_to_dump = stack_types_to_dump

class VPResults:
    def __init__(self, loc2dump_seq:dict[ASTNodeLoc, Optional[DumpSeq]]):
        self.loc2dump_seq = loc2dump_seq

class VPLocDesc:
    def __init__(self, nodelist:NodeList, elem_idx:int):
        # * the locate is after the elem_idx-th
        self.nodelist = nodelist
        self.elem_idx = elem_idx


def _get_stack_dump_seq_by_probe_idx(
    to_probe_infos: list[ToProbeLocInfo],
    snapshot: WMSnapshot,
    instrument_manager: ValueProbeManager,
) -> dict[int, Optional[DumpSeq]]:
    probs:list[ProbeDesc] = []
    for idx, to_probe_info in enumerate(to_probe_infos):
        prob = ProbeDesc(
            idx=idx,
            probe_types={ProbeType.STACK},
            loc=to_probe_info.loc,
            specified_stack_types=to_probe_info.stack_types_to_dump,
        )
        probs.append(prob)
    # 
    allocated_time=2
    t0 = time.time()
    dumped = instrument_manager.instrument_multiple_places_and_get_result(
        snapshot=snapshot,
        probe_descs=probs,
        max_global_num=200,
        allocated_time=allocated_time,
        max_output_time=None,
    )
    if time.time() - t0 > allocated_time:  ## If timeout, ignore the results and return empty.
        return {}

    probdesc2stack_dump: dict[int, OneDumpData] = {}
    for one_dump_data in dumped:
        probe_idx = one_dump_data.probe_idx
        probdesc2stack_dump[probe_idx] = one_dump_data

    result: dict[int, Optional[DumpSeq]] = {}
    for prob in probs:
        stack_dump = probdesc2stack_dump.get(prob.idx)
        result[prob.idx] = stack_dump.dump_seq if stack_dump is not None else None
    return result

def get_actual_loc_by_vp_loc_desc(
    vp_loc_desc:VPLocDesc,
    elems:list[OneElem]
):
    if vp_loc_desc.elem_idx >= len(elems):
        raise ValueError(f'elem_idx {vp_loc_desc.elem_idx} out of range for elems with length {len(elems)}')
    inst_num = 0
    for idx in range(vp_loc_desc.elem_idx+1):
        inst_num += elems[idx].get_length()
    # 
    func_idx = vp_loc_desc.nodelist.func_idx
    inst_idx = vp_loc_desc.nodelist.inst_idx + inst_num
    return ASTNodeLoc(func_idx=func_idx, inst_idx=inst_idx)
        

def get_stack_value_on_specific_loc(
    to_probe_infos: list[ToProbeLocInfo],
    # locs:list[ASTNodeLoc],
    # stack_types_to_dump: list[list[str]],
    snapshot: WMSnapshot,
    instrument_manager: ValueProbeManager,
)->VPResults:
    if len(to_probe_infos) == 0:
        return VPResults({})

    dump_seq_by_probe_idx = _get_stack_dump_seq_by_probe_idx(
        to_probe_infos=to_probe_infos,
        snapshot=snapshot,
        instrument_manager=instrument_manager,
    )
    if len(dump_seq_by_probe_idx) == 0:
        return VPResults({})

    result_raw_dict = {}
    for idx, to_probe_info in enumerate(to_probe_infos):
        loc = to_probe_info.loc
        cur_dump_seq = dump_seq_by_probe_idx.get(idx)
        result_raw_dict[loc] = cur_dump_seq
    return VPResults(result_raw_dict)
    # 
        # prob = f"stack_value_at_func{loc.func_idx}_inst{loc.inst_idx}_depth{idx}_type{stack_type}"
        # probs.append(prob)



def get_to_dump_locs(
):
    pass


def _get_vp_probe_info_for_each_elem_in_node_list(
    node_list:NodeList,
    only_enable_idxs:Optional[set[int]],
    elems:list[OneElem]
) -> dict[OneElem, ToProbeLocInfo]:
    # 
    result: dict[OneElem, ToProbeLocInfo] = {}
    func_idx = node_list.func_idx
    start_idx = node_list.inst_idx
    # offset = 
    elem_start_idx = start_idx
    for elem_idx, elem in enumerate(elems):
        if only_enable_idxs is not None and elem_idx not in only_enable_idxs:
            elem_start_idx += elem.get_length()
            continue
        after_elem_offset = elem_start_idx + elem.get_length()

        to_vp_types = elem.to_vp_types()
        if to_vp_types is not None:
            to_probe_info = ToProbeLocInfo(
                loc=ASTNodeLoc(func_idx=func_idx, inst_idx=after_elem_offset),
                stack_types_to_dump=to_vp_types,
            )
            result[elem] = to_probe_info

        elem_start_idx = after_elem_offset
    return result


def get_vp_results_for_each_elem_in_node_list(
    node_list:NodeList,
    elems:list[OneElem],
    only_enable_idxs:Optional[set[int]],
    snapshot: WMSnapshot,
    instrument_manager: ValueProbeManager,
)->dict[OneElem, DumpSeq]:
    probe_info = _get_vp_probe_info_for_each_elem_in_node_list(
        node_list=node_list,
        only_enable_idxs=only_enable_idxs,
        elems=elems,
    )

    if len(probe_info) == 0:
        return {}
    # print('probe_info is ', probe_info)

    # Dict preserves insertion order; probe order follows `elems` iteration order.
    to_probe_infos = list(probe_info.values())
    vp_results = get_stack_value_on_specific_loc(
        to_probe_infos=to_probe_infos,
        snapshot=snapshot,
        instrument_manager=instrument_manager,
    )

    result: dict[OneElem, DumpSeq] = {}
    for elem, to_probe_info in probe_info.items():
        dumped_seq = vp_results.loc2dump_seq.get(to_probe_info.loc)
        if dumped_seq is None:
            continue
        result[elem] = dumped_seq
    return result


def get_vp_results_for_each_elem_in_multi_node_list(
    node_list2elems: dict[NodeList, list[OneElem]],
    only_enable_idxs_by_node_list: Optional[dict[NodeList, set[int]]],
    snapshot: WMSnapshot,
    instrument_manager: ValueProbeManager,
) -> dict[OneElem, DumpSeq]:
    probe_items: list[tuple[OneElem, ToProbeLocInfo]] = []
    for node_list, elems in node_list2elems.items():
        only_enable_idxs = None
        if only_enable_idxs_by_node_list is not None:
            only_enable_idxs = only_enable_idxs_by_node_list.get(node_list)
        probe_info = _get_vp_probe_info_for_each_elem_in_node_list(
            node_list=node_list,
            only_enable_idxs=only_enable_idxs,
            elems=elems,
        )
        probe_items.extend(probe_info.items())

    print('There are totally ', len(probe_items), ' elems to probe.')

    if len(probe_items) == 0:
        return {}

    dump_seq_by_probe_idx = _get_stack_dump_seq_by_probe_idx(
        to_probe_infos=[to_probe_info for _, to_probe_info in probe_items],
        snapshot=snapshot,
        instrument_manager=instrument_manager,
    )

    result: dict[OneElem, DumpSeq] = {}
    for idx, (elem, _) in enumerate(probe_items):
        dumped_seq = dump_seq_by_probe_idx.get(idx)
        if dumped_seq is None:
            continue
        result[elem] = dumped_seq
    return result
