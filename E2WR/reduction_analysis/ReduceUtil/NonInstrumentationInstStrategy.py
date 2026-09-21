from abc import ABC, abstractmethod
from typing import Any, Optional
from extract_block_mutator.InstGeneration.InstFactory import InstFactory
from extract_block_mutator.funcType import funcType
from extract_block_mutator.InstUtil.Inst import Inst
from reduction_analysis.ASTInfo.AST import ASTNodeLoc, InstsNode, gen_new_insts_node
from enum import Enum
from .NewInstUtil import padding_input_type_naive


class ReduceStrategyType(Enum):
    UNREACHABLE = 1
    SPECIFIC_TYPE = 2
    STACK_VALUE = 3


class NewInstStrategy(ABC):
    can_preserve_type: bool

    def __init__(self) -> None:
        pass

    @abstractmethod
    def get_insts_for_replace(self) -> list[Inst]:
        raise NotImplementedError("subclass must implement this method")

    def gen_new_node(self, given_type: Optional[funcType], loc: ASTNodeLoc) -> InstsNode:
        insts: list[Inst] = self.get_insts_for_replace()
        # print('Cur strategy is ', self, 'insts', insts)
        return gen_new_insts_node(insts, loc, given_type)


class GenUnreachableInst(NewInstStrategy):
    def __init__(self, expected_type: Optional[funcType] = None) -> None:
        self.can_preserve_type = True
        super().__init__()
        self.expected_type = expected_type

    def get_insts_for_replace(self) -> list[Inst]:
        if self.expected_type is not None and self.expected_type.result_types == self.expected_type.param_types:
            return []
        else:
            return [InstFactory.opcode_inst('unreachable')]


class GenSpecificType(NewInstStrategy):
    def __init__(self, expected_type: funcType) -> None:
        self.can_preserve_type = True
        super().__init__()
        self.expected_type = expected_type

    def get_insts_for_replace(self) -> list[Inst]:
        return padding_input_type_naive(
            self.expected_type.param_types,
            self.expected_type.result_types
        )
