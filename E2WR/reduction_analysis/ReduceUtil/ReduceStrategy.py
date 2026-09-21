from abc import ABC, abstractmethod
from typing import List, Optional, Set
from dataclasses import dataclass, field
from extract_block_mutator.WasmParser import WasmParser
from extract_block_mutator.InstUtil.Inst import Inst
from extract_block_mutator.funcType import funcType
from reduction_analysis.ASTInfo.AST import InstsNode
from reduction_analysis.Instrumentation.ValueProbeInstrument import  ValueProbeManager
from ..ASTInfo.AST import ASTINode
from .NonInstrumentationInstStrategy import GenSpecificType
from .NewInstUtil import generate_n_drops, get_inst_by_require_ty_const_n, padding_input_type_naive
from .NonInstrumentationInstStrategy import ReduceStrategyType


class ReduceWithNewNode(ABC):
    @abstractmethod
    def gen_new_node(self, ori_node:ASTINode)->ASTINode:
        pass


@dataclass
class RpInstParam: pass

@dataclass
class TypeAwareRpInstParam(RpInstParam):
    expected_types: List[funcType]
    # simpilify_indirect_call: bool

@dataclass
class SVRpInstParam(TypeAwareRpInstParam):
    func_idx: int
    inst_idxs: List[int]
    clean_stack_idxs: List[int]
    parser: WasmParser


@dataclass
class TypeAwareRpNodeParam(RpInstParam):
    nodes_to_be_reduced:List[ASTINode]
    node_types:List[funcType]
    ignore_node_idxs:Set[int]


@dataclass
class SVRpNodeParam(TypeAwareRpNodeParam):
    to_reduce_path:str
    clean_stack_idxs:List[int]


class ReplacementGen(ABC):
    supported_strategies:set[ReduceStrategyType]
    instrument_manager:Optional[ValueProbeManager]
    @abstractmethod
    def gen_inst_replacement(
        self,
        param:RpInstParam
    )->list[list[Inst]]:
        raise NotImplementedError
    @abstractmethod
    def gen_node_replacements(
        self,
        param:RpInstParam
    ):
        raise NotImplementedError


class SpecificTypeReplacementGen(ReplacementGen):
    def __init__(self) -> None:
        self.supported_strategies = {ReduceStrategyType.SPECIFIC_TYPE}
        self.instrument_manager = None
        
    def gen_inst_replacement(
        self,
        param:TypeAwareRpInstParam
    )->list[list[Inst]]:
        result:list[list[Inst]] = []
        for inst_type in param.expected_types:
            insts:list[Inst] = get_random_insts(inst_type, True)
            result.append(insts)
        return result

    def gen_node_replacements(
        self,
        param:TypeAwareRpNodeParam
    ):
        result:dict[int, InstsNode] = {}
        for node_idx, node in enumerate(param.nodes_to_be_reduced):
            if node_idx in param.ignore_node_idxs:
                continue
            new_node_type = param.node_types[node_idx]
            specific_type_gen_insts = GenSpecificType(new_node_type)
            new_insts_node = specific_type_gen_insts.gen_new_node(
                given_type=new_node_type,
                loc=node.loc
            )
            result[node_idx] = new_insts_node
        return result


def get_random_insts(inst_type:funcType, most_naive:bool=True)->list[Inst]:
    if not most_naive:
        return padding_input_type_naive(
            inst_type.param_types,
            inst_type.result_types
        )
    else:
        insts = []
        insts.extend(generate_n_drops(len(inst_type.param_types)))
        for ty in inst_type.result_types:
            insts.append(get_inst_by_require_ty_const_n(ty))
        return insts
