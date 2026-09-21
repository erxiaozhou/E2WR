
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
    def elem_type(self):
        assert self._elem_type is not None
        return self._elem_type

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
        self.have_take_rest_op =  (self.taken_op_num > 0)
        self.is_not_determined_type = 'any' in self.gen_types

    def __str__(self) -> str:
        return f'StackChange(taken_op_type_list={self.taken_op_type_list}, gen_types={self.gen_types})'

    def __repr__(self) -> str:
        return self.__str__()

    @property
    def gen_op_num(self)->int:
        return len(self.gen_types)

    # @property
    # def 

    @classmethod
    def empty(cls):
        return cls([], [])

    def merge_one(self, drop_or_type:Union[DropOrType, 'ElemTypeInfo']):
        if isinstance(drop_or_type, DropOrType):
            eq_sc = _drop_or_type_to_stack_change(drop_or_type)
            gen_vals = eq_sc.gen_types
            taken_op_type_list = eq_sc.taken_op_type_list
            takne_num = eq_sc.taken_op_num
        else:
            eq_sc = drop_or_type
            gen_vals = eq_sc.gen_ops
            taken_op_type_list = eq_sc.taken_ops
            takne_num = eq_sc.taken_op_num
        raw_gen_type_num = len(self.gen_types)

        # update taken_op_num
        rest_stack_op_num = raw_gen_type_num - takne_num
        if rest_stack_op_num < 0:
            extra_needed = -rest_stack_op_num
            # Consume more than currently generated: record the extra taken types
            self.taken_op_num += extra_needed
            self.taken_op_type_list.extend(taken_op_type_list[:extra_needed])
            self.have_take_rest_op = True
            self.gen_types = gen_vals
        else:
            self.gen_types = self.gen_types[:rest_stack_op_num] + gen_vals

    def not_take_any_op(self)->bool:
        return self.taken_op_num == 0


    @property
    def taken_op_type_strs(self)->list[str]:
        return [ty.type_str for ty in self.taken_op_type_list]

    @property
    def common_stack_size(self)->int:
        return get_node_type_common_stack_size(
                param_types=self.taken_op_type_strs,
                result_types=self.gen_types
        )

    @property
    def mini_replacement_length(self)->int:
        common_size = self.common_stack_size
        return (self.taken_op_num - common_size) + (self.gen_op_num - common_size)

class OneElem:
    def __init__(
        self,
        elem_idx,
        elem:Union[Inst, ASTINode],
        elem_type_info:ElemTypeInfo,
        raw_index:Optional[int]=None
    ):
        self.raw_index = raw_index
        self.elem_idx = elem_idx
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

    def to_vp_types(self)->Optional[list[str]]:
        if self.is_cf_related_inst():
            return None
        elif self.is_one_const_or_drop():
            return None
        gen_op_types = self.elem_type_info.gen_ops
        if len(gen_op_types) == 0:
            return None
        if 'any' in gen_op_types:
            return None
        if 'funcref' in gen_op_types or 'externref' in gen_op_types:
            return None
        return gen_op_types

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

    def is_unreachable_inst(self)->bool:
        opcode = self.inst_opcode
        if opcode is None:
            return False
        if opcode == 'unreachable':
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

    @property
    def gen_ops(self):
        return self.elem_type_info.gen_ops

    @property
    def taken_ops(self):
        return self.elem_type_info.taken_ops

    @property
    def taken_op_num(self):
        return self.elem_type_info.taken_op_num
    @property
    def gen_op_num(self):
        return self.elem_type_info.gen_op_num

    def as_insts(self)->list[Inst]:
        if isinstance(self.elem, Inst):
            return [self.elem]
        elif isinstance(self.elem, ASTINode):
            return self.elem.get_insts()
        else:
            raise ValueError('Elem is neither Inst nor ASTINode')



def _drop_or_type_to_stack_change(
    drop_or_type:DropOrType
):
    if drop_or_type.is_drop:
        takne_num = 1
        taken_ops = [gen_type_for_graph('any')]
        gen_vals = []
    else:
        # takne_num = len(drop_or_type.inst_type.param_types)
        taken_ops = [gen_type_for_graph(ty) for ty in drop_or_type.inst_type.param_types]
        gen_vals = drop_or_type.inst_type.result_types.copy()
    return StackChange(taken_ops, gen_vals)

class ElemGroupBase:
    def __init__(
        self,
        elems:list[OneElem]
    ):
        self.elems = elems
        self._index = None
    
    def get_length(self)->int:
        total_len = 0
        for elem in self.elems:
            total_len += elem.get_length()
        return total_len
    def is_one_const_or_drop(self)->bool:
        raise NotImplementedError

    def is_determined_type(self)->bool:
        raise NotImplementedError
        return True

    def as_insts(self)->list[Inst]:
        insts = []
        for elem in self.elems:
            insts.extend(elem.as_insts())
        return insts

    def set_index(self, index):
        self._index = index

    def __str__(self) -> str:
        return f'{self.__class__.__name__}({self.elems})'

    def __repr__(self) -> str:
        return self.__str__()

    @property
    def index(self):
        assert self._index is not None
        return self._index

    def is_empty(self)->bool:
        return len(self.elems) == 0


class ArbitraryElemGroup(ElemGroupBase):pass

class MutElemGroup(ElemGroupBase):
    def __init__(
        self,
        elems:list[OneElem]
    ):
        super().__init__(elems)
        self.stack_change = StackChange.empty()
        # self.have_take_rest_op
        for elem in elems:
            # print(f'Cur Elem is {elem.as_insts()}')
            # elem_type_info = elem.elem_type_info.elem_type
            self.stack_change.merge_one(elem.elem_type_info)

        self.remove_donot_affect_next_group_op_taken = self.stack_change.not_take_any_op()
        # self.start_elem_idx = self.elems[0].elem_idx
        

    def is_one_const_or_drop(self)->bool:
        if len(self.elems) != 1:
            return False
        inner_insts = self.elems[0].as_insts()
        if len(inner_insts) != 1:
            return False
        inst = inner_insts[0]
        if inst.opcode_text.endswith('.const'):
            return True
        if inst.opcode_text == 'drop':
            return True
        if inst.opcode_text == 'ref.null':
            return True
        return False

    @property
    def have_take_rest_op(self)->bool:
        return self.stack_change.have_take_rest_op


    @property
    def start_elem_idx(self):
        return self.elems[0].elem_idx

    @classmethod
    def from_one(cls,
        elem
    ):
        return cls([elem])


    def is_determined_type(self)->bool:
        return True



class MutElemGroupV6(MutElemGroup):
    def __init__(
        self,
        elems:list[OneElem],
        elem_idxs:list[int],
        taken_from_any_op_num:list[int],
        consumed_by_any_op_num:list[int]
    ):
        self.elems = elems
        self._index = None
        self.stack_change = StackChange.empty()
        # self.have_take_rest_op
        for elem, taken_from_any, taken_by_any in zip(elems, taken_from_any_op_num, consumed_by_any_op_num):
            # print(f'Cur Elem is {elem.as_insts()}')
            # elem_type_info = elem.elem_type_info.elem_type
            to_append_type_info = elem.elem_type_info
            # 
            gen_ops = to_append_type_info.gen_ops
            taken_ops = to_append_type_info.taken_ops
            actual_gen_ops = gen_ops[taken_from_any:]
            actual_taken_ops = taken_ops[:len(taken_ops)-taken_by_any]
            actual_elem_type_info = ArbElemTypeInfo(
                gen_ops=actual_gen_ops,
                taken_ops=[ty.type_str for ty in actual_taken_ops]
            )
            # 
            
            self.stack_change.merge_one(actual_elem_type_info)

        self.remove_donot_affect_next_group_op_taken = self.stack_change.not_take_any_op()
        # super().super().__init__(elems)
        # self.stack_change = StackChange.empty()

        self.taken_from_any_op_num = taken_from_any_op_num
        self.consumed_by_any_op_num = consumed_by_any_op_num
        self.elem_idxs = elem_idxs

    def is_one_const_or_drop(self)->bool:
        return super().is_one_const_or_drop()

    @property
    def start_elem_idx(self):
        return min(self.elem_idxs)

    def is_determined_type(self)->bool:
        # 
        if 'any' in self.stack_change.gen_types:
            return False
        return True
        cs = self.stack_change.common_stack_size
        gen_ops = self.stack_change.gen_types
        if 'any' in gen_ops[cs:]:
            return False
        return True
        # 
        return super().is_determined_type()



class ImmGroup(ElemGroupBase):
    def __init__(
        self,
        elems:list[OneElem],
        idxs:Optional[list[int]]=None
    ):
        super().__init__(elems)
        self.idxs = idxs

    @property
    def start_elem_idx(self):
        if self.idxs is not None:
            return min(self.idxs)
        else:
            raise ValueError('idxs must be provided for ImmGroup')

    def is_one_const_or_drop(self)->bool:
        return False

    def is_unreachable_inst(self)->bool:
        only_inst = self.get_the_only_one_inst()
        if only_inst is None:
            return False
        if only_inst.opcode_text == 'unreachable':
            return True
        return False
    def tail_likes_unreachable(self)->bool:
        only_inst = self.get_the_only_one_inst()
        if only_inst is None:
            return False
        if only_inst.opcode_text in ['return', 'br',  'br_table', 'unreachable']:
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
    @classmethod
    def from_one(cls,
        elem
    ):
        return cls([elem])

    def is_determined_type(self)->bool:
        return False

class RawElemProbModel:
    def __init__(
        self,
        init_elems:list[int]
        ):
        self.p:dict[int, float] = collections.OrderedDict()
        assert init_elems
        init0 = 1 / len(init_elems) 
        for idx in init_elems:
            self.p[idx] = init0
        # 
    def update_once(
        self,
        selected_elem_idxs:set[int],
        deleteconfig:set[int],
        success:bool
    ):
        if success:
            for idx in deleteconfig:
                self.p[idx] = 0
        else:
            self._update_probabilities_on_fail(deleteconfig, selected_elem_idxs, self.p)

    def _update_probabilities_on_fail(self, deleteconfig, _config2test, d):
        if not deleteconfig:
            return
        ratio = self.computRatio(deleteconfig, d)
        if ratio is None:
            return
        for key in deleteconfig:
            if 0 < d.get(key, 0) < 1:
                d[key] = min(1.0, d[key] * ratio)


    def computRatio(self, deleteconfig, p):
        tmplog = 1
        for delc in deleteconfig:
            if p[delc] > 0 and p[delc] < 1:
                tmplog *= (1 - p[delc])
        denom = 1 - tmplog

        return 1 / denom


RawElemCacheKey = int

ENABLE_COVERED_HASH_CACHE: bool = True   # covered_hashs exact-sequence dedup (applier)
ENABLE_FAILED_IDXS_CACHE: bool = False    # failed_idxs cross-phase removal-set memory

class RawElemsCache:
    def __init__(self):
        # self.covered_idxs:set[RawElemCacheKey] = set()
        self.covered_hashs:set[int] = set()
        self.failed_idxs:set[frozenset[int]] = set()
        # self.hash_length_map:dict[int, int] = dict()

    def can_skip(self, key:RawElemCacheKey)->bool:
        if ENABLE_COVERED_HASH_CACHE and key in self.covered_hashs:
            print('[cache-hit] covered')
            return True
        return False

    def add_covered(self, key:RawElemCacheKey):
        if ENABLE_COVERED_HASH_CACHE:
            self.covered_hashs.add(key)

    def add_failed_by_raw_elems(self, elems:list[OneElem]):
        if not ENABLE_FAILED_IDXS_CACHE:
            return
        idxs = self.get_contained_raw_elems(elems)
        if len(idxs) == 0:
            return

        # Maintain minimal failed sets:
        # - If an existing failed set is already a subset of `idxs`, `idxs` is redundant.
        # - If some existing failed sets are supersets of `idxs`, replace them with `idxs`.
        to_remove: list[frozenset[int]] = []
        for failed_idx_set in self.failed_idxs:
            if failed_idx_set.issubset(idxs):
                return
            if idxs.issubset(failed_idx_set):
                to_remove.append(failed_idx_set)

        for s in to_remove:
            self.failed_idxs.discard(s)
        # print('add failed idxs: ', idxs)
        self.failed_idxs.add(idxs)

    def covers_failed(self, elems:list[OneElem])->bool:
        if not ENABLE_FAILED_IDXS_CACHE:
            return False
        idxs = self.get_contained_raw_elems(elems)
        # return idxs in self.failed_idxs
        if len(idxs) == 0:
            return False
        for failed_idx_set in self.failed_idxs:
            if failed_idx_set == idxs:
                print('[cache-hit] failed')
                return True
        return False
           
    def covers_failed_ori(self, elems:list[OneElem])->bool:
        idxs = self.get_contained_raw_elems(elems)
        # return idxs in self.failed_idxs
        if len(idxs) == 0:
            return False
        for failed_idx_set in self.failed_idxs:
            if failed_idx_set.issubset(idxs):
                return True
        return False
             

    def get_contained_raw_elems(self, elems:list[OneElem])->frozenset[int]:
        idxs = set()
        for elem in elems:
            raw_idx = elem.raw_index
            if raw_idx is not None:
                idxs.add(raw_idx)
        return frozenset(idxs)

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

def get_node_type_common_stack_size(
    param_types:list[str],
    result_types:list[str]
):
    common_stack_size = 0
    for ty1, ty2 in zip(param_types, result_types):
        if ty1 == ty2:
            common_stack_size += 1
        else:
            break
    return common_stack_size


def reset_stack_change(
    raw_gen_types:list[str],
    raw_taken_types:list[str],
    # raw_taken_num:int,
    taken_from_any_cnt:int=0,
    taken_by_any_cnt:int=0,
    force_common_size:Optional[int]=None
):  
    actual_taken_types = raw_taken_types[taken_from_any_cnt:]
    actual_gen_types = raw_gen_types[taken_by_any_cnt:]
    # if 'any' in actual_gen_types
        # common_size = 0
    if force_common_size is not None:
        common_size = force_common_size
    else:
        common_size = get_node_type_common_stack_size(
            param_types=actual_taken_types,
            result_types=actual_gen_types
        )
    
    return StackChange(
        [gen_type_for_graph(ty) for ty in actual_taken_types[common_size:]],
        actual_gen_types[common_size:]
    )

def gen_new_group_by_stack_change( stack_change:StackChange, cur_elem_groups, group_idx:int) -> MutElemGroup:
    
    if last_is_unreachable_like(cur_elem_groups, group_idx) \
        or next_group_is_unreachable(cur_elem_groups, group_idx):
        return MutElemGroup([])
    else:
        new_elems = gen_replacement_by_stack_change(stack_change)
        new_group = MutElemGroup(new_elems)
        return new_group

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

def change_cost(
    ori_stack:Sequence[str],
    cur_stack:Sequence[str]
):
    common_length = get_seq_common_prefix_len(ori_stack, cur_stack)
    drop_num = len(ori_stack) - common_length
    gen_num = len(cur_stack) - common_length
    return drop_num + gen_num

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



def cur_is_more_naive(
    raw_elems: list[OneElem],
    cur_elems: list[OneElem]
):
    cur_elem_length = sum(elem.get_length() for elem in cur_elems)
    raw_elem_length = sum(elem.get_length() for elem in raw_elems)
    if cur_elem_length < raw_elem_length:
        return True
    cur_trival_inst_num = sum(1 for elem in cur_elems if elem.is_one_const_or_drop())
    raw_trival_inst_num = sum(1 for elem in raw_elems if elem.is_one_const_or_drop())
    if cur_trival_inst_num > raw_trival_inst_num:
        return True
    return False



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
    cur_trival_inst_num = sum(1 for elem in cur_elems if elem.is_one_const_or_drop())
    raw_trival_inst_num = sum(1 for elem in raw_elems if elem.is_one_const_or_drop())
    if cur_trival_inst_num > raw_trival_inst_num:
        return True
    return False

