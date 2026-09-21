from typing import Optional
from extract_block_mutator.encode.NGDataPayload import DataPayloadwithName

from .InstUtil.Inst import Inst
from .InstGeneration.InstFactory import InstFactory
from .funcType import funcType


start_block_ops = set([
    'block', 'loop', 'if'
])

class wasmFunc:
    def __init__(self, insts, defined_local_types, func_ty:Optional[funcType]=None):
        if _need_append_end(insts):
            insts.append(InstFactory.opcode_inst('end'))
        self.insts:list[Inst] = insts
        self.defined_local_types:list[str] = defined_local_types
        self._func_ty: Optional[funcType] = func_ty
        
    @property
    def locals_with_def_repr(self):
        local_type_num = []
        cur_local = None
        for local_type in self.defined_local_types:
            if local_type == cur_local:
                local_type_num[-1][1] += 1
            else:
                cur_local = local_type
                local_type_num.append([local_type, 1])
        local_defs = []
        for local_type, count in local_type_num:
            local_def = DataPayloadwithName(
                data = {
                    'value_type': local_type,
                    'count': count
                },
                name = 'locals'
            )
            local_defs.append(local_def)
        return local_defs
    

    def copy(self):
        return wasmFunc(
            insts=[inst.copy() for inst in self.insts],
            defined_local_types=self.defined_local_types.copy(),
            func_ty=self.func_ty.copy()
        )

    @property
    def func_ty(self) -> funcType:  
        assert self._func_ty is not None
        return self._func_ty
    @func_ty.setter
    def func_ty(self, func_ty:funcType):
        self._func_ty = func_ty
    @property
    def param_types(self):
        return self.func_ty.param_types
    
    @property
    def result_types(self):
        return self.func_ty.result_types

    @property
    def local_types(self)->list[str]:
        return self.param_types + self.defined_local_types



def _need_append_end(insts:list[Inst]):
    

    start_line_num = 0
    end_line_num = 0
    for inst in insts:
        if inst.opcode_text in start_block_ops:
            start_line_num += 1
        if inst.opcode_text == 'end':
            end_line_num += 1
    if start_line_num == end_line_num + 1:
        return True
    if start_line_num == end_line_num:
        return False
    raise Exception(f'start_line_num {start_line_num} != end_line_num {end_line_num}: insts:{insts}')