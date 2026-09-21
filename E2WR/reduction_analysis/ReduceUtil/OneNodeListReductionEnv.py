
from dataclasses import dataclass
from typing import Protocol, Any

from extract_block_mutator.Context import Context
from extract_block_mutator.funcType import funcType
from reduction_analysis.ASTState import ASTState
from reduction_analysis.ASTInfo.AST import NodeList

from reduction_analysis.InferStackUtil import _get_cur_context_by_ast_info
from reduction_analysis.ASTInfo.AST import get_node_list_type_in_ast_practical
from reduction_analysis.ReduceUtil.ReduceInsts_V5_util import OneElem
from reduction_analysis.ReduceUtil.ReduceInsts_V5_util import RawElemsCache

from reduction_analysis.StackState import StackState, StackStatus


@dataclass(slots=True)
class OneNodeListReductionCtx:
    context: Context
    node_type: funcType
    DEBUG: bool
    ori_node_list: NodeList
    raw_elems_cache: RawElemsCache

    def build_stack_init_status(self) -> StackState:
        return StackState(
            all_rest_types=[self.node_type.param_types],
            status=StackStatus.NORMAL,
        )

class OneNodeListReducerApplier(Protocol):
    @property
    def actual_nodes(self) -> list[Any]:
        ...

    def gen_replacement_by_elems_and_test_by_mutation(
        self,
        ctx: OneNodeListReductionCtx,
        raw_elems: list[OneElem],
        mutation_elem_idx2new_elems: dict[int, list[OneElem]],
        check_invalid_and_return_false=None,
    ) -> bool:
        ...

    def finalize(
        self,
        ori_node_list: NodeList,
        raw_elems_length: int,
        new_elems: list[OneElem],
    ) -> None:
        ...

    def cal_elems_length(self, elems: list[OneElem]) -> int:
        ...


def build_one_node_list_reduction_ctx(
    *,
    ori_node_list: NodeList,
    ast_state: ASTState,
    DEBUG: bool,
) -> OneNodeListReductionCtx:
    node_type = get_node_list_type_in_ast_practical(node_list=ori_node_list)
    context = _get_cur_context_by_ast_info(
        ast_state.parser,
        ast_state.ast_info,
        ori_node_list.loc,
    )

    return OneNodeListReductionCtx(
        context=context,
        node_type=node_type,
        DEBUG=DEBUG,
        ori_node_list=ori_node_list,
        raw_elems_cache=RawElemsCache(),
    )
