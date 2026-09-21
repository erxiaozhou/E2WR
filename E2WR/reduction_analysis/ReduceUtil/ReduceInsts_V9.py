
from reduction_analysis.Instrumentation.DumpData import DumpSeq
from reduction_analysis.ReduceUtil.ElemGuidedNodeListReducerMultiNode import ElemGuidedNodeListReducerMultiNode
from reduction_analysis.ReduceUtil.OneNodeListReductionEnv import OneNodeListReductionCtx, build_one_node_list_reduction_ctx
from .ReduceInsts_nodelist_P3_v7 import reduce_insts_p3_graph_based_v7
from reduction_analysis.ASTInfo.AST import ASTINode, NodeList
from ..ASTState import ASTState
from .util import get_try_time
from typing import Optional
import time
from .ReduceInsts_V5_util import OneElem
from .ReduceInsts_V5_reduce_surround_unreachable import reduce_by_unreachable_like_inst
from .ReduceInsts_cfg_util import V7Cfg, build_v6_cfg
from .ReduceInsts_V9_cfn import call_V9_cfn_multi_basic
from .ReduceInsts_V9_core import call_V9_multi_basic
from .ReduceInsts_V9_rev import call_V9_rev_multi_basic
from .OneElemLevelReduce import remove_cf_elem
from reduction_analysis.ReduceUtil.RNOpParam import RNOpParam
from reduction_analysis.ReduceUtil.RewritingUtil.OnlyInstSnapshotRewriter import OnlyInstSnapshotRewriter
from reduction_analysis.Instrumentation.ValueProbeInstrument import ValueProbeManager
from reduction_analysis.ReduceUtil.VPUtil import get_vp_results_for_each_elem_in_multi_node_list
from reduction_analysis.ReduceUtil.ReduceInsts_V9_util import NodeListElemInfo, RawElemsEnv, finalize_process, get_raw_elems_from_env

# SMALL_NODE_TH = 1000
# SMALL_NODE_TH = 500
SMALL_NODE_TH = 50
class InstCounter:
    def __init__(self):
        self.t0 = 0
        self.inst_num = 0
    def init(self, new_node_list2elems: dict[NodeList, list[OneElem]]):
        self.t0 = time.time()
        self.inst_num = get_total_inst_num(new_node_list2elems)

    def update_state_and_print(self,
            new_node_list2elems: dict[NodeList, list[OneElem]],
            epoch_num,
            tag=''
        ):
        cur_time = time.time()
        cur_inst_num = get_total_inst_num(new_node_list2elems)
        print(f'ST[{epoch_num}][{tag}] {cur_time-self.t0} {self.inst_num-cur_inst_num}')
        self.t0 = cur_time
        self.inst_num = cur_inst_num


def get_total_inst_num(new_node_list2elems: dict[NodeList, list[OneElem]]):
    total = 0
    for node_list, elems in new_node_list2elems.items():
        for elem in elems:
            total += elem.get_length()
    return total


def reduce_node_list_by_insts_v9(
    ori_node_lists:list[NodeList],
    ast_state:ASTState,
    DEBUG:bool,
    op_param: RNOpParam,
    snapshot_rewriter: OnlyInstSnapshotRewriter,
    vp_manager: Optional[ValueProbeManager] = None,
    rest_time:Optional[float]=None,

) -> tuple[bool, list[ASTINode]]:
    if ast_state is not snapshot_rewriter.ast_state:
        raise ValueError(
            'reduce_node_list_by_insts_v9 requires ast_state is snapshot_rewriter.ast_state'
        )
    reducer = OneNodeListReducerV9(
        DEBUG=DEBUG,
        snapshot_rewriter=snapshot_rewriter,
        op_param=op_param,
        vp_manager=vp_manager,
    )
    return reducer.reduce_multi(ori_node_lists, rest_time=rest_time)

class OneNodeListReducerV9:
    def __init__(
        self,
    DEBUG:bool,
    snapshot_rewriter: OnlyInstSnapshotRewriter,
    op_param: RNOpParam,
    vp_manager: Optional[ValueProbeManager] = None
    ):
        self.DEBUG = DEBUG
        self.snapshot_rewriter = snapshot_rewriter
        self.vp_manager = vp_manager
        self.expected_end_time: Optional[float] = None
        reduction_cfg = build_v6_cfg(
            op_param=op_param,
        )
        self.reduction_cfg = reduction_cfg
        self.has_reduced = False
        self.op_param = op_param
        

    def _get_rest_time(self, after_what: str = '') -> Optional[float]:
        if self.expected_end_time is None:
            rest_time = None
        else:
            rest_time = self.expected_end_time - time.time()
        prompt = f'after {after_what}' if after_what else ''
        print(f'Rest time {prompt}:', rest_time)
        return rest_time
    

    def reduce_multi(self, ori_node_lists:list[NodeList], rest_time:Optional[float]=None):
        if self.has_reduced:
            raise RuntimeError('OneNodeListReducerV9.reduce_once can only be called once')
        ori_node_lists = [node_list for node_list in ori_node_lists if node_list.get_length() > 0]
        if len(ori_node_lists) == 0:
            return False, []
        if rest_time is not None:
            total_inst_num = sum(node_list.get_length() for node_list in ori_node_lists)
            rest_time = min(rest_time, get_try_time(total_inst_num))
        print('Rest time at start of V9:', rest_time)
        self.expected_end_time = time.time() + rest_time if rest_time is not None else None
        # prepare infos
        nodelist2ctx: dict[NodeList, OneNodeListReductionCtx] = {}
        node_list2elems: dict[NodeList, list[OneElem]] = {}
        for ori_node_list in ori_node_lists:
            ctx = build_one_node_list_reduction_ctx(
                ori_node_list=ori_node_list,
                ast_state=self.snapshot_rewriter.ast_state,
                DEBUG=self.DEBUG,
            )
            nodelist2ctx[ori_node_list] = ctx
            node_list2elems[ori_node_list] = get_raw_elems_from_env(ast_state=self.snapshot_rewriter.ast_state, ctx=ctx)
        cur_elems: NodeListElemInfo = NodeListElemInfo(node_list2elems=node_list2elems)
        raw_elem_num = cur_elems.get_elem_num()
        # 
        reduce_applier = ElemGuidedNodeListReducerMultiNode(
            ori_node_lists=ori_node_lists,
            ast_state=self.snapshot_rewriter.ast_state,
            snapshot_rewriter=self.snapshot_rewriter,
            DEBUG=self.DEBUG,
        )
        inst_counter = InstCounter()
        inst_counter.init(cur_elems.node_list2elems)

        for _ in range(1):

            # Control flow guided ===========================================================
            if self.reduction_cfg.enable_cfg_reduce:
                new_node_list2elems: dict[NodeList, list[OneElem]] = {}
                for node_list, elems in cur_elems.node_list2elems.items():
                    reduced = reduce_by_unreachable_like_inst(
                        ctx=nodelist2ctx[node_list],
                        elems=elems,
                        reduce_applier=reduce_applier,
                        DEBUG=self.DEBUG,
                    )
                    new_node_list2elems[node_list] = reduced
                # 
                cur_elems = NodeListElemInfo(node_list2elems=new_node_list2elems)
                if True:
                    inst_counter.update_state_and_print(cur_elems.node_list2elems, self.op_param.cur_epoch_num, f'UR')
                cur_elems = call_V9_cfn_multi_basic(
                    ctx_by_node_list=nodelist2ctx,
                    reduce_applier=reduce_applier,
                    input_elems=cur_elems,
                    DEBUG=self.DEBUG,
                    rest_time=self._rest_time_now(),
                )
                if True:
                    inst_counter.update_state_and_print(cur_elems.node_list2elems, self.op_param.cur_epoch_num, f'CF')
                # 
                rest_time = self._get_rest_time('reduce surround unreachable')
                if self._time_exhausted():
                    break
                
            # VP ===========================================================
            # init VP values
            elem_ref_values:dict[OneElem, DumpSeq]={}
            if self.reduction_cfg.use_VP:
                print('Start VP phase')
                # assert 0
                t_start_vp = time.time()
                if self.vp_manager is None:
                    print('[VP] Skip: vp_manager is None')
                else:
                    elem_ref_values = get_vp_results_for_each_elem_in_multi_node_list(
                        node_list2elems=cur_elems.node_list2elems,
                        only_enable_idxs_by_node_list=None,
                        snapshot=self.snapshot_rewriter.ast_state.snapshot,
                        instrument_manager=self.vp_manager,
                    )
                    print(f'[VP] Collected dump for {len(elem_ref_values)} elems, taking {time.time() - t_start_vp:.2f} seconds')
            v7_cfg = V7Cfg.from_reduce_cfg(
                self.reduction_cfg,
                elem_ref_values=elem_ref_values,
            )
            # V7 ===========================================================
            if self.reduction_cfg.enable_whole_dd:
                print('Start V9 phase')
                cur_elems = call_V9_multi_basic(
                    ctx_by_node_list=nodelist2ctx,
                    reduce_applier=reduce_applier,
                    input_elems=cur_elems,
                    rest_time=rest_time,
                    cfg=v7_cfg,
                )
                if True:
                    inst_counter.update_state_and_print(cur_elems.node_list2elems, self.op_param.cur_epoch_num, f'CORE')
                rest_time = self._get_rest_time('V7')
                if self._time_exhausted():
                    break
            if self.reduction_cfg.use_cf_reduction and self.reduction_cfg.enable_whole_dd:
            # if False:
                print('Start CF reduction phase')
                new_node_list2elems: dict[NodeList, list[OneElem]] = {}
                for node_list, elems in cur_elems.node_list2elems.items():
                    new_node_list2elems[node_list] = remove_cf_elem(
                        ctx=nodelist2ctx[node_list],
                        elems=elems,
                        reduce_applier=reduce_applier,
                        rest_time=self._rest_time_now(),
                        DEBUG=self.DEBUG,
                    )
                cur_elems = NodeListElemInfo(node_list2elems=new_node_list2elems)
                if True:
                    inst_counter.update_state_and_print(cur_elems.node_list2elems, self.op_param.cur_epoch_num, f'CF_ELEM')
                rest_time = self._get_rest_time('remove_cf_elem')
            if self._time_exhausted():
                break
            # V7 REV ===========================================================
            if self.reduction_cfg.enable_rev:
                print('Start V9 REV')
                cur_elems = call_V9_rev_multi_basic(
                    ctx_by_node_list=nodelist2ctx,
                    reduce_applier=reduce_applier,
                    input_elems=cur_elems,
                    rest_time=rest_time,
                    cfg=v7_cfg,
                )
                if True:
                    inst_counter.update_state_and_print(cur_elems.node_list2elems, self.op_param.cur_epoch_num, f'REV')
                rest_time = self._get_rest_time('V7_REV')
                
                if self._time_exhausted():
                    break
                # P3 ============================================================
            if self.reduction_cfg.enable_p3:
                print('Start P3 phase')
                new_node_list2elems: dict[NodeList, list[OneElem]] = {}
                for node_list, elems in cur_elems.node_list2elems.items():
                    print(f'[P3GATE] epoch={self.op_param.cur_epoch_num} '
                        f'len_elems={len(elems)} '
                        # f'inst_total={sum(e.get_length() for e in elems)} '
                        f'TH={SMALL_NODE_TH} skip={len(elems) > SMALL_NODE_TH}')

                    if len(elems) > SMALL_NODE_TH:
                        new_node_list2elems[node_list] = elems
                        continue
                    p3_rest_time = self._rest_time_now()
                    p3_rest_time = min(p3_rest_time, 30) if p3_rest_time is not None else None
                    new_node_list2elems[node_list] = reduce_insts_p3_graph_based_v7(
                        ctx=nodelist2ctx[node_list],
                        reduce_applier=reduce_applier,
                        rest_time=p3_rest_time,
                        elems=elems,
                        must_contain_cf=False,
                    )
                cur_elems = NodeListElemInfo(node_list2elems=new_node_list2elems)
                if True:
                    inst_counter.update_state_and_print(cur_elems.node_list2elems, self.op_param.cur_epoch_num, f'P3')
                rest_time = self._get_rest_time('P3')
                if self._time_exhausted():
                    break
           
            rest_time = self._get_rest_time('final_check')
            if self._time_exhausted():
                break
        # ============================================================
        final_result, cur_nodes = finalize_process(reduce_applier, raw_elem_num, cur_elems)

        self.has_reduced = True
        return final_result, cur_nodes

    def _time_exhausted(self) -> bool:
        return self.expected_end_time is not None and time.time() > self.expected_end_time

    def _rest_time_now(self) -> Optional[float]:
        if self.expected_end_time is None:
            return None
        return self.expected_end_time - time.time()


# end new logic code =============================================================================================================
