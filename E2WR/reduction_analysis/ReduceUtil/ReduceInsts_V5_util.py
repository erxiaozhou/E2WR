
import collections
from extract_block_mutator.Context import Context
from extract_block_mutator.InstGeneration.InstFactory import InstFactory
from extract_block_mutator.InstUtil import Inst
from extract_block_mutator.InstUtil.InstReqUtil import get_inst_ty_req
from extract_block_mutator.funcType import funcType
from extract_block_mutator.funcTypeFactory import funcTypeFactory
from extract_block_mutator.typeReq import merge_req, typeReq
from reduction_analysis.ReduceUtil.NewInstUtil import get_inst_by_require_ty_const_n
from reduction_analysis.StackState import StackState, StackStatus, get_stack_state_from_type_req, get_stack_state_from_type_req
from reduction_analysis.ASTInfo.AST import ASTINode
from .util import get_stack_num_diff_from_inst_type
from typing import Any, Optional, Sequence, Union
import time


class DropOrType:
    def __init__(self, is_drop:bool, inst_type:Optional[funcType]=None):
        self.is_drop = is_drop
        self._inst_type = inst_type
        if not is_drop:
            assert inst_type is not None, 'inst_type should not be None when is_drop is False'

    @property
    def inst_type(self)->funcType:
        assert self._inst_type is not None, 'inst_type is None'
        return self._inst_type



class OneNodeType: 
    type_str:str
    # def __init__(self):
    #     pass

class _DeteriminedType(OneNodeType):
    def __init__(self, type_str: str):
        self.type_str = type_str

    def __str__(self) -> str:
        return f'{self.type_str}'
    def __repr__(self) -> str:
        return self.__str__()

class _AnyType(OneNodeType):
    def __init__(self):
        self.type_str = 'any'
    def __str__(self) -> str:
        return 'any'
    def __repr__(self) -> str:
        return self.__str__()

def gen_type_for_graph(type_str:Optional[str]) -> OneNodeType:
    if type_str is None or type_str == 'any':
        return _AnyType()
    else:
        return _DeteriminedType(type_str)


class ElemTypeInfo:
    def __init__(
        self,
        elem_type:Optional[DropOrType]=None
    ):
        self._elem_type= elem_type
        if elem_type is not None:
            if elem_type.is_drop:
                taken_op_num, gen_op_num = 1, 0
                gen_ops = []
                # taken_type
                taken_ops = [gen_type_for_graph('any')]
            else:
                taken_op_num, gen_op_num = get_stack_num_diff_from_inst_type(elem_type.inst_type)
                gen_ops = elem_type.inst_type.result_types
                taken_ops = [gen_type_for_graph(ty) for ty in elem_type.inst_type.param_types]
        else:
            taken_op_num =None
            gen_ops = None
            taken_ops = None
            gen_op_num = None
        self._taken_op_num = taken_op_num
        self._gen_op_num = gen_op_num
        self._gen_ops = gen_ops
        self._taken_ops = taken_ops

    @property
    def gen_ops(self)->list[str]:
        assert self._gen_ops is not None
        return self._gen_ops

    @property
    def taken_ops(self):
        assert self._taken_ops is not None
        return self._taken_ops

    @property
    def taken_op_num(self):
        assert self._taken_op_num is not None
        return self._taken_op_num

    @property
    def gen_op_num(self):
        assert self._gen_op_num is not None
        return self._gen_op_num

    @property
    def is_not_sure_type(self):
        return self._elem_type is None

    def __str__(self) -> str:
        return f'{self.__class__.__name__}(elem_type={self._elem_type})'

class ArbElemTypeInfo(ElemTypeInfo):
    def __init__(self, gen_ops:list[str], taken_ops:list[str]):
        self._taken_op_num = len(taken_ops)
        self._gen_op_num = len(gen_ops)
        self._gen_ops = gen_ops
        self._taken_ops = [gen_type_for_graph(s) for s in taken_ops]
        # [gen_type_for_graph('any')]

    @property
    def is_not_sure_type(self):
        # TODO to remove
        return False


def get_type_info(type_req:Optional[typeReq], inst:Optional[Inst])->ElemTypeInfo:
    if type_req is None:
        return ElemTypeInfo(None)
    if inst is not None and inst.opcode_text == 'drop':
        return ElemTypeInfo(DropOrType(is_drop=True))
    if len(type_req.tys) == 1 and type_req.req_type == 'eq':
        return ElemTypeInfo(DropOrType(is_drop=False, inst_type=type_req.tys[0]))
    if type_req.req_type == 'eq':
        types  = type_req.tys
        # params:list[list[str]] = []
        # results:list[list[str]] = []
        
        # ty_num = len(types)
        for ty in types:
            if ty.determined_return_ty:
                return ElemTypeInfo(None)
        if inst is not None and inst.opcode_text == 'select':
            return ArbElemTypeInfo(
                gen_ops = ['any'],
                taken_ops=['any', 'any', 'i32']
                # gen_ops = [],
                # taken_ops=[ 'any', 'i32']
            )
        if inst is not None and inst.opcode_text == 'ref.is_null':
            return ArbElemTypeInfo(
                gen_ops = ['i32'],
                taken_ops=['any']
            )

    return ElemTypeInfo(None)


class StackChange:
    def __init__(
        self,
        # taken_op_num:int,
        taken_op_type_list:list[OneNodeType],
        gen_types:list[str]
    ):
        self.taken_op_type_list = taken_op_type_list.copy()
        self.taken_op_num = len(taken_op_type_list)
        self.gen_types = gen_types.copy()
        self.is_not_determined_type = 'any' in self.gen_types

    def __str__(self) -> str:
        return f'StackChange(taken_op_type_list={self.taken_op_type_list}, gen_types={self.gen_types})'

    def __repr__(self) -> str:
        return self.__str__()

class OneElem:
    def __init__(
        self,
        elem_idx,
        elem:Union[Inst, ASTINode],
        elem_type_info:ElemTypeInfo,
        raw_index:Optional[int]=None
    ):
        self.raw_index = raw_index
        self.elem = elem
        self.elem_type_info = elem_type_info
        self._stack_change = None
        self.is_one_inst = isinstance(elem, Inst)
        self.inst_opcode:Optional[str] = elem.opcode_text if self.is_one_inst else None  # type: ignore
        # self.stack_change = StackChange

    def get_length(self)->int:
        if self.is_one_inst:
            return 1
        elif isinstance(self.elem, ASTINode):
            return self.elem.get_length()
        else:
            raise ValueError('Elem is neither Inst nor ASTINode')

    @property
    def stack_change(self)->StackChange:
        if self._stack_change is None:
            self._stack_change = StackChange(
            taken_op_type_list=self.elem_type_info.taken_ops,
            gen_types=self.elem_type_info.gen_ops
            )
        return self._stack_change

    def tail_likes_unreachable(self)->bool:
        # only_inst = self.get_the_only_one_inst()
        opcode = self.inst_opcode
        if opcode is None:
            return False
        if opcode in ['return', 'br',  'br_table', 'unreachable']:
            return True
        return False

    def is_cf_related_inst(self)->bool:
        opcode = self.inst_opcode
        if opcode is None:
            return False
        if opcode in ['return', 'br',  'br_table', 'unreachable', 'br_if']:
            return True
        return False

    def is_target_one_inst(self, target_insts:set[str])->bool:
        opcode = self.inst_opcode
        if opcode is None:
            return False
        if opcode in target_insts:
            return True
        return False

    def is_one_const_or_drop(self)->bool:
        opcode = self.inst_opcode
        if opcode is None:
            return False
        if opcode.endswith('.const'):
            return True
        if opcode == 'drop':
            return True
        if opcode == 'ref.null':
            return True
        return False

    
    def __str__(self) -> str:
        return f'OneElem({self.elem})'

    def __repr__(self) -> str:
        return self.__str__()
    def is_determined_type(self)->bool:
        return not self.elem_type_info.is_not_sure_type

    def as_insts(self)->list[Inst]:
        if isinstance(self.elem, Inst):
            return [self.elem]
        elif isinstance(self.elem, ASTINode):
            return self.elem.get_insts()
        else:
            raise ValueError('Elem is neither Inst nor ASTINode')



class ElemGroupBase:
    def __init__(
        self,
        elems:list[OneElem]
    ):
        self.elems = elems

    def is_empty(self)->bool:
        return len(self.elems) == 0

    def __str__(self) -> str:
        return f'{self.__class__.__name__}({self.elems})'

    def __repr__(self) -> str:
        return self.__str__()


class ArbitraryElemGroup(ElemGroupBase):pass

class MutElemGroup(ElemGroupBase):pass


class ImmGroup(ElemGroupBase):
    def is_unreachable_inst(self)->bool:
        only_inst = self.get_the_only_one_inst()
        if only_inst is None:
            return False
        if only_inst.opcode_text == 'unreachable':
            return True
        return False
    def get_the_only_one_inst(self)->Optional[Inst]:
        if len(self.elems) != 1:
            return None
        inner_insts = self.elems[0].as_insts()
        if len(inner_insts) != 1:
            return None
        inst = inner_insts[0]
        return inst



RawElemCacheKey = int

class RawElemsCache:
    @staticmethod
    def get_raw_idxs_from_elems(elems:list[OneElem])->tuple[RawElemCacheKey, int]:
        idxs = []
        for elem in elems:
            raw_idx = elem.raw_index
            if raw_idx is not None:
                idxs.append(raw_idx)
        idx_tuple = tuple(idxs)
        return hash(idx_tuple), len(idxs)



def get_stack_state_after_each_elem_with_reqs(
    stack_init_status:StackState,
    elems: list[OneElem],
    type_reqs:list[typeReq]
    )->list[StackState]:
    stack_state_after_each_inst = []
    base_req = stack_init_status.as_type_req()
    for elem_type_req, elem in zip(type_reqs, elems):
        if elem.tail_likes_unreachable():
            cur_stack =  StackState([[]], StackStatus.ANY)
            base_req = cur_stack.as_type_req()
        else:
            new_req = merge_req(base_req, elem_type_req)
            base_req = new_req
            cur_stack = get_stack_state_from_type_req(new_req)
        stack_state_after_each_inst.append(cur_stack)
    return stack_state_after_each_inst

def get_type_reqs_from_elems(
    elems: list[OneElem],
    context:Context
)->list[typeReq]:
    type_reqs = []
    for elem in elems:
        if isinstance(elem.elem, Inst):
            inst = elem.elem
            elem_type_req = get_inst_ty_req(inst, context)
        else:
            block = elem.elem
            elem_type_req = block.get_type_req(context)
        assert elem_type_req is not None
        type_reqs.append(elem_type_req)
    return type_reqs


def sort_and_get_continuous_groups(nums:set[int]) -> list[list[int]]:
    num_list = sorted(list(nums))
    continuous_groups: list[list[int]] = []
    cur_group:list[int] = []
    last_num = None
    for num in num_list:
        if last_num is None or num == last_num + 1:
            cur_group.append(num)
        else:
            continuous_groups.append(cur_group)
            cur_group = [num]
        last_num = num
    if cur_group:
        continuous_groups.append(cur_group)
    return continuous_groups



def gen_replacement_by_stack_change(stack_change:StackChange):
    new_elems:list[OneElem] = []
    for drop_idx in range(stack_change.taken_op_num):
        new_elem = OneElem(
                    elem_idx=-1,
                    elem=InstFactory.opcode_inst('drop'),
                    elem_type_info=ElemTypeInfo(DropOrType(is_drop=True))
                )
        new_elems.append(new_elem)
    for ty in stack_change.gen_types:
        new_inst = get_inst_by_require_ty_const_n(ty)
        assert isinstance(new_inst, Inst)
        new_elem = OneElem(
                    elem_idx=-1,
                    elem=new_inst,
                    elem_type_info=ElemTypeInfo(DropOrType(is_drop=False, inst_type=funcTypeFactory.generate_one_func_type_default([], [ty])))
                )
        new_elems.append(new_elem)
    return new_elems


def last_is_unreachable_like(cur_elem_groups, cur_group_idx: int) -> bool:
    last_idx = cur_group_idx - 1
    while last_idx >= 0 and cur_elem_groups[last_idx].is_empty():
        last_idx -= 1
    if last_idx < 0:
        return False
    next_group = cur_elem_groups[last_idx]
    if not isinstance(next_group, ImmGroup):
        return False
    only_inst = next_group.get_the_only_one_inst()
    if only_inst is None:
        return False
    inst_opcode = only_inst.opcode_text
    if inst_opcode in {'unreachable', 'br', 'br_table', 'return'}:
        return True
    return False


def next_group_is_unreachable(cur_elem_groups, cur_group_idx: int) -> bool:
    next_idx = cur_group_idx + 1
    # skip empty groups safely; stop if we run off the end
    while next_idx < len(cur_elem_groups) and cur_elem_groups[next_idx].is_empty():
        next_idx += 1
    if next_idx >= len(cur_elem_groups):
        return False
    next_group = cur_elem_groups[next_idx]
    if not isinstance(next_group, ImmGroup):
        return False
    return next_group.is_unreachable_inst()


def cal_rest_time_and_reset_t0(rest_time:Optional[float], t0:float, after_what=''):
    if rest_time is not None:
        rest_time = rest_time - (time.time() - t0)
    if after_what:
        prompt = f'after {after_what}'
    else:
        prompt = ''
    print(f'Rest time {prompt}: {rest_time:.6f}')
    t0 = time.time()
    return rest_time, t0

def infer_mini_stack_change(
    ori_stack:Sequence[str],
    cur_stack:Sequence[str]
)->tuple[list[str], list[str]]:
    max_prefix_len = get_seq_common_prefix_len(ori_stack, cur_stack)
    # to_drop_num = len(ori_stack) - max_prefix_len
    to_gen_types = list(cur_stack[max_prefix_len:])
    to_drop_types = list(ori_stack[max_prefix_len:])
    return to_drop_types, to_gen_types


def get_seq_common_prefix_len(
    seq1:Sequence,
    seq2:Sequence
)->int:
    common_len = 0
    for ty1, ty2 in zip(seq1, seq2):
        if ty1 == ty2:
            common_len += 1
        else:
            break
    return common_len



def cur_is_more_naive_v2(
    raw_elems: list[OneElem],
    cur_elems: list[OneElem]
):
    cur_elem_length = sum(elem.get_length() for elem in cur_elems)
    raw_elem_length = sum(elem.get_length() for elem in raw_elems)
    if cur_elem_length < raw_elem_length:
        return True
    cur_non_trival_inst_num = sum(1 for elem in cur_elems if not elem.is_one_const_or_drop())
    raw_non_trival_inst_num = sum(1 for elem in raw_elems if not elem.is_one_const_or_drop())
    if cur_non_trival_inst_num < raw_non_trival_inst_num:
        return True
    return False

