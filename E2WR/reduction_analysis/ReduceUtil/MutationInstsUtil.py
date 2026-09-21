from __future__ import annotations
from dataclasses import dataclass

from reduction_analysis.ASTInfo.AST import ASTNodeLoc, NodeList
from reduction_analysis.ASTInfo.AST import get_node_list_type_in_ast_practical, insts2AST
from reduction_analysis.ParserModificationUtil import FuncInstMutation
from reduction_analysis.ReduceUtil.ReduceInsts_V5_util import OneElem

from extract_block_mutator.InstUtil.Inst import Inst
from extract_block_mutator.funcType import funcType
from reduction_analysis.ASTState import ASTState
from reduction_analysis.ReduceUtil.RewritingUtil.NodeReplacement import NodeReplacement

@dataclass
class InstsUpdateInfo:
    seq_loc: ASTNodeLoc
    raw_length: int
    new_insts: list[Inst]


@dataclass
class FIMutationsInOneNodeList:
    func_idx: int
    inst_mutations: list[FuncInstMutation]


@dataclass(frozen=True)
class OneNodeListMutation:
    ori_node_list: NodeList
    raw_elems: list[OneElem]
    mutation_elem_idx2new_elems: dict[int, list[OneElem]]


def get_mutated_insts_sequence(
    raw_elems: list[OneElem],
    mutation_elem_idx2new_elems: dict[int, list[OneElem]],
) -> list[Inst]:
    mutation_idxs = sorted(mutation_elem_idx2new_elems.keys())
    start_idx = mutation_idxs[0]
    end_idx = mutation_idxs[-1] + 1

    raw_pre_insts: list[Inst] = []
    for idx in range(start_idx):
        raw_pre_insts.extend(raw_elems[idx].as_insts())

    raw_post_insts: list[Inst] = []
    for idx in range(end_idx, len(raw_elems)):
        raw_post_insts.extend(raw_elems[idx].as_insts())

    new_insts_for_replacement: list[Inst] = []
    for raw_elem_idx in range(start_idx, end_idx):
        if raw_elem_idx in mutation_elem_idx2new_elems:
            new_elems = mutation_elem_idx2new_elems[raw_elem_idx]
            for elem in new_elems:
                new_insts_for_replacement.extend(elem.as_insts())
        else:
            ori_elem = raw_elems[raw_elem_idx]
            new_insts_for_replacement.extend(ori_elem.as_insts())

    return raw_pre_insts + new_insts_for_replacement + raw_post_insts



@dataclass(slots=True)
class OneReduceUnitInfo:
    ori_node_list: NodeList
    func_idx: int
    inst_idx: int
    node_type: funcType
    types: list[funcType]
    _last_nodes: list

    @classmethod
    def from_ori_node_list(cls, ori_node_list: NodeList, ast_state: ASTState) -> OneReduceUnitInfo:
        return cls(
            ori_node_list=ori_node_list,
            func_idx=ori_node_list.loc.func_idx,
            inst_idx=ori_node_list.loc.inst_idx,
            node_type=get_node_list_type_in_ast_practical(ori_node_list),
            types=ast_state.snapshot.parser.types,
            _last_nodes=list(ori_node_list.sub_nodes),
        )

    @property
    def actual_nodes(self):
        return self._last_nodes

    def update_ast_nodes(self, ast_state: ASTState, insts: list[Inst]) -> None:
        new_nodes = insts2AST(
            insts=insts,
            given_type=self.node_type,
            func_idx=self.func_idx,
            types=self.types,
            root_node_type=NodeList,
        ).sub_nodes
        if len(new_nodes) == len(self.actual_nodes) == 0:
            return

        node_replacement = NodeReplacement(
            original_nodes=self.actual_nodes,
            new_nodes=new_nodes,
        )
        for n in new_nodes:
            n.parent = self.ori_node_list
        node_replacement.apply()

        ast_state.ast_info.update_loc_info(func_idx=self.func_idx)
        self._last_nodes = new_nodes


def get_inst_mutation(unit_info: OneReduceUnitInfo, raw_elems: list[OneElem], mutation_elem_idx2new_elems:dict[int, list[OneElem]])->FIMutationsInOneNodeList:
    elem_idx2start_idx = {}
    elem_idx2end_idx = {}
    cur_inst_idx = unit_info.inst_idx
    for elem_idx, elem in enumerate(raw_elems):
        elem_idx2start_idx[elem_idx] = cur_inst_idx
        cur_inst_idx += elem.get_length()
        elem_idx2end_idx[elem_idx] = cur_inst_idx
    each_inst_mutations:list[FuncInstMutation] = []
    for mutation_elem_idx, new_elems in mutation_elem_idx2new_elems.items():
        ori_start_inst_idx = elem_idx2start_idx[mutation_elem_idx]
        ori_end_inst_idx = elem_idx2end_idx[mutation_elem_idx]
        new_insts = []
        for _elem in new_elems:
            new_insts.extend(_elem.as_insts())
        each_inst_mutations.append(
            FuncInstMutation(
                func_idx=unit_info.func_idx, 
                start_offset=ori_start_inst_idx, 
                end_offset=ori_end_inst_idx,
                new_insts=new_insts
            )
        )
    return FIMutationsInOneNodeList(
        func_idx=unit_info.func_idx, 
        inst_mutations=each_inst_mutations,
    )
