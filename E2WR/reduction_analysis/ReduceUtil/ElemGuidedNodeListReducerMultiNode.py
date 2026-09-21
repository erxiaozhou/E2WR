from reduction_analysis.ReduceUtil.ReduceInsts_V5_util import OneElem
from reduction_analysis.ASTState import ASTState
from reduction_analysis.ReduceUtil.MutationInstsUtil import (
    FIMutationsInOneNodeList,
    InstsUpdateInfo,
    OneNodeListMutation,
    get_inst_mutation,
)
from reduction_analysis.ReduceUtil.MutationInstsUtil import OneReduceUnitInfo
from reduction_analysis.ReduceUtil.RewritingUtil.OnlyInstSnapshotRewriter import OnlyInstSnapshotRewriter
from reduction_analysis.ASTInfo.AST import NodeList
from reduction_analysis.ReduceUtil.OneNodeListReductionEnv import OneNodeListReductionCtx




class ElemGuidedNodeListReducerMultiNode:
    def __init__(self,
                 ori_node_lists: list[NodeList],
                 snapshot_rewriter: OnlyInstSnapshotRewriter,
                 ast_state: ASTState,
                 DEBUG: bool = False,
                 ):

        self.DEBUG = DEBUG

        self.ast_state = ast_state
        self.node_list2node_list_info:dict[NodeList, OneReduceUnitInfo] = {}
        for ori_node_list in ori_node_lists:
             self.node_list2node_list_info[ori_node_list] = OneReduceUnitInfo.from_ori_node_list(
                ori_node_list=ori_node_list,
                ast_state=ast_state,
            )
        self.snapshot_rewriter = snapshot_rewriter

    @property
    def actual_nodes(self):
        all_nodes = []
        for unit_info in self.node_list2node_list_info.values():
            all_nodes.extend(unit_info.actual_nodes)
        return all_nodes
   


    def gen_replacement_by_elems_and_test_by_mutation_v2(self, 
                                                      mutations:list[OneNodeListMutation],
                                                      check_invalid_and_return_false=None)->bool:
                                                 
        # * validate
        for mutation in mutations:
            assert mutation.mutation_elem_idx2new_elems
            # if self.DEBUG:
            #     assert self.node_list2node_list_info[mutation.ori_node_list].actual_nodes

        # 
        nodelist2mutations:dict[NodeList, FIMutationsInOneNodeList] = {}
        nodelist2mutation: dict[NodeList, OneNodeListMutation] = {}
        for mutation in mutations:
            one_nl_mutation: FIMutationsInOneNodeList = self._get_one_nl_mutation(
                mutation=mutation
            )
            # one_nl_mutation_list.append(one_nl_mutation)
            nodelist2mutations[mutation.ori_node_list] = one_nl_mutation
            nodelist2mutation[mutation.ori_node_list] = mutation
        can_apply = self.snapshot_rewriter.try_apply_tmp_snapshot_and_update_multi(
            one_nl_mutations_list=nodelist2mutations,
            node_list2node_list_info=self.node_list2node_list_info,
            node_list2mutation=nodelist2mutation,
            check_invalid_and_return_false=check_invalid_and_return_false,
        )
        return can_apply

    def gen_replacement_by_elems_and_test_by_mutation(
        self,
        ctx: "OneNodeListReductionCtx",
        raw_elems: list[OneElem],
        mutation_elem_idx2new_elems: dict[int, list[OneElem]],
        check_invalid_and_return_false=None,
    ) -> bool:
        ori_node_list = ctx.ori_node_list
        if ori_node_list not in self.node_list2node_list_info:
            raise ValueError('ori_node_list must be one of the ori_node_lists passed to __init__')
        return self.gen_replacement_by_elems_and_test_by_mutation_v2(
            mutations=[
                OneNodeListMutation(
                    ori_node_list=ori_node_list,
                    raw_elems=raw_elems,
                    mutation_elem_idx2new_elems=mutation_elem_idx2new_elems,
                )
            ],
            check_invalid_and_return_false=check_invalid_and_return_false,
        )

    def _get_one_nl_mutation(self, mutation: OneNodeListMutation) -> FIMutationsInOneNodeList:
        raw_elems = mutation.raw_elems
        mutation_elem_idx2new_elems = mutation.mutation_elem_idx2new_elems
        full_elem_list = []
        for elem_idx in range(len(raw_elems)):
            if elem_idx in mutation_elem_idx2new_elems:
                full_elem_list.extend(mutation_elem_idx2new_elems[elem_idx])
            else:
                full_elem_list.append(raw_elems[elem_idx])
        # 
        one_nl_mutations = get_inst_mutation(
            self.node_list2node_list_info[mutation.ori_node_list],
            raw_elems,
            mutation_elem_idx2new_elems,
        )
        return one_nl_mutations

    def finalize(self, ori_node_list:NodeList, raw_elems_length: int, new_elems: list[OneElem]) -> None:
        new_insts = []
        for elem in new_elems:
            new_insts.extend(elem.as_insts())
        # if self.DEBUG:
        #     parser_insts = self.ast_state.snapshot.parser.defined_funcs[ori_node_list.loc.func_idx].insts
        #     loc_end_idx = getattr(ori_node_list.loc, 'end_idx', None)
        #     print(
        #         "[Finalize-Debug][MultiNode.finalize] "
        #         f"func_idx={ori_node_list.loc.func_idx}, loc=({ori_node_list.loc.inst_idx},{loc_end_idx}), "
        #         f"raw_elems_length={raw_elems_length}, new_insts_len={len(new_insts)}, "
        #         f"parser_func_len_before={len(parser_insts)}"
        #     )
        self.snapshot_rewriter.finalize_replace_insts_range_v2(
            insts_update=InstsUpdateInfo(
                seq_loc=ori_node_list.loc,
                raw_length=raw_elems_length,
                new_insts=new_insts
            )
        )

    def cal_elems_length(self, elems:list[OneElem]):
        length = 0
        for elem in elems:
            length += elem.get_length()
        return length
