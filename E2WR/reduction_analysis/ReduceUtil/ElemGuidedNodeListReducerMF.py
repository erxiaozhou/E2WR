from reduction_analysis.ReduceUtil.ReduceInsts_V5_util import OneElem
from reduction_analysis.ReduceUtil.MutationInstsUtil import InstsUpdateInfo, get_inst_mutation, get_mutated_insts_sequence
from reduction_analysis.ASTState import ASTState
from reduction_analysis.ReduceUtil.MutationInstsUtil import OneReduceUnitInfo
from reduction_analysis.ReduceUtil.RewritingUtil.OnlyInstSnapshotRewriter import OnlyInstSnapshotRewriter
from reduction_analysis.ASTInfo.AST import NodeList
from reduction_analysis.ReduceUtil.OneNodeListReductionEnv import OneNodeListReductionCtx


def get_elem_guided_node_list_reducer(
    ori_node_list: NodeList,
    snapshot_rewriter: OnlyInstSnapshotRewriter,
    ast_state: ASTState,
    DEBUG: bool = False,
):
    unit_info = OneReduceUnitInfo.from_ori_node_list(
        ori_node_list=ori_node_list,
        ast_state=ast_state,
    )
    return ElemGuidedNodeListReducer(
        unit_info=unit_info,
        snapshot_rewriter=snapshot_rewriter,
        ast_state=ast_state,
        DEBUG=DEBUG,
    )


class ElemGuidedNodeListReducer:
    def __init__(self,
                 unit_info: OneReduceUnitInfo,
                 snapshot_rewriter: OnlyInstSnapshotRewriter,
                 ast_state: ASTState,
                 DEBUG: bool = False,
                 ):
        self.DEBUG = DEBUG

        self.ast_state = ast_state
        self.unit_info = unit_info
        self.snapshot_rewriter = snapshot_rewriter

    @property
    def actual_nodes(self):
        return self.unit_info.actual_nodes
   
    def gen_replacement_by_elems_and_test_by_mutation(
        self,
        ctx: "OneNodeListReductionCtx",
        raw_elems: list[OneElem],
        mutation_elem_idx2new_elems: dict[int, list[OneElem]],
        check_invalid_and_return_false=None,
    ) -> bool:
        # * validate
        assert mutation_elem_idx2new_elems
        assert self.unit_info.actual_nodes
        # * using cache
        # 
        full_elem_list = []
        for elem_idx in range(len(raw_elems)):
            if elem_idx in mutation_elem_idx2new_elems:
                full_elem_list.extend(mutation_elem_idx2new_elems[elem_idx])
            else:
                full_elem_list.append(raw_elems[elem_idx])
        # 
        mutations_in_one_node_list = get_inst_mutation(self.unit_info, raw_elems, mutation_elem_idx2new_elems)
        key, raw_elem_num = ctx.raw_elems_cache.get_raw_idxs_from_elems(full_elem_list)
        
        if ctx.raw_elems_cache.can_skip(key):
            if self.DEBUG:
                print('Already covered elems, skip testing')
            return False
        insts = get_mutated_insts_sequence(
            raw_elems, 
            mutation_elem_idx2new_elems
            )
        can_apply = self.snapshot_rewriter.try_apply_tmp_snapshot_and_update(
            mutations_in_one_node_list,
            check_invalid_and_return_false=check_invalid_and_return_false,
        )

        if not can_apply:
            if raw_elem_num:
                ctx.raw_elems_cache.add_covered(key)
        else:
            self.unit_info.update_ast_nodes(self.ast_state, insts)
        return can_apply



    def finalize(self, ori_node_list: NodeList, raw_elems_length: int, new_elems: list[OneElem]) -> None:
        new_insts = []
        for elem in new_elems:
            new_insts.extend(elem.as_insts())
        self.snapshot_rewriter.finalize_replace_insts_range_v2(
            insts_update=InstsUpdateInfo(
                seq_loc=ori_node_list.loc,
                raw_length=raw_elems_length,
                new_insts=new_insts,
            )
        )

    def cal_elems_length(self, elems:list[OneElem]):
        length = 0
        for elem in elems:
            length += elem.get_length()
        return length

# ==================================================================

def transform_group_mutation_to_standard_param(ori_groups, groups_mutation):
    raw_elems:list[OneElem] = []
    assert groups_mutation
    # * convert groups_mutation to mutation_elem_idx2new_elems
    mutation_elem_idx2new_elems:dict[int, list[OneElem]] = {}
    cur_elem_num = 0
    for group_idx, group in enumerate(ori_groups):
        if group_idx in groups_mutation:
            new_group = groups_mutation[group_idx]
            new_elems = new_group.elems
            ori_elem_num = len(group.elems)
            new_elem_num = len(new_elems)
            if ori_elem_num == 0:
                assert new_elem_num == 0
            # for i in range(ori_elem_num):
            mutation_elem_idx2new_elems[cur_elem_num] = new_elems
            for i in range(1, ori_elem_num):
                mutation_elem_idx2new_elems[cur_elem_num + i] = []
            # cur_elem_num += ori_elem_num
        # else:
        cur_elem_num += len(group.elems)
        raw_elems.extend(group.elems)
    return raw_elems,mutation_elem_idx2new_elems