from typing import Any, Optional
from extract_block_mutator.InstGeneration.InstFactory import InstFactory
from extract_block_mutator.funcType import funcType
from extract_block_mutator.funcTypeFactory import funcTypeFactory
from extract_block_mutator.wasmFunc import wasmFunc
from extract_block_mutator.WasmParser import WasmParser
from extract_block_mutator.InstUtil.Inst import Inst
from reduction_analysis.WasmSemanticsUtil import is_ref_type
from reduction_analysis.InstsScope import InstsScope
from enum import Enum
from reduction_analysis.ReduceUtil.NonInstrumentationInstStrategy import NewInstStrategy
from reduction_analysis.ReduceUtil.NewInstUtil import padding_input_type_naive, generate_n_drops, get_inst_by_require_ty_const_n


class ProcessRefStrategy(Enum):
    Skip = 0
    OnlyPreserveType = 1

class GenStackValueInsts(NewInstStrategy):
    def __init__(self, 
                 stack_types: list[str], 
                 stack_vals: list[Any], 
                 drop_num:Optional[int]=None, 
                 process_ref_strategy:ProcessRefStrategy=ProcessRefStrategy.Skip
                 ) -> None:
        super().__init__() 
        self.can_preserve_type = True
        # ! may not correct for NAN
        self.stack_types = stack_types  
        self.stack_vals = stack_vals
        if drop_num is None:
            self.drop_num = len(stack_types)
        else:
            self.drop_num = drop_num
        self.process_ref_strategy = process_ref_strategy
    def get_insts_for_replace(self) -> list[Inst]:
        allow_rand_ref = self.process_ref_strategy == ProcessRefStrategy.OnlyPreserveType
        return _ReplaceInstsFactory.get_stack_value_insts(
            self.stack_types,
            self.stack_vals,
            self.drop_num,
            allow_rand_ref
        )

    def can_skip(self)->bool:
        if len(self.stack_types) == 0:
            return True
        if self.process_ref_strategy == ProcessRefStrategy.Skip:
            if any([is_ref_type(ty) for ty in self.stack_types]):
                return True
        return False


    def __repr__(self):
        return f'{self.__class__.__name__}(stack_types={self.stack_types}, stack_vals={self.stack_vals}, drop_num={self.drop_num})'

class GenLocalInsts(NewInstStrategy):
    def __init__(self,
                 local_idxs: list[int],
        local_types: list[str], 
        local_vals: list[Any]
    ):
        super().__init__()
        self.can_preserve_type = False
        self.local_types = local_types
        self.local_vals = local_vals
        self.local_idxs = local_idxs
        assert len(local_idxs) == len(local_vals)
    
    def get_insts_for_replace(self) -> list[Inst]:
        return _ReplaceInstsFactory.get_local_insts(
            self.local_idxs,
            self.local_types,
            self.local_vals
        )

    def can_skip(self)->bool:
        result = len(self.local_vals) == 0
        return result

    def __repr__(self):
        return f'ReplaceWithLocal(local_idxs={self.local_idxs}, local_types={self.local_types}, local_vals={self.local_vals})'

class GenGlobalInsts(NewInstStrategy):
    def __init__(self,
                 global_idxs: list[int],
                 global_types: list[str],
                 global_vals: list[Any]
                 ) -> None:
        super().__init__()
        self.can_preserve_type = False
        self.global_idxs = global_idxs
        self.global_types = global_types
        self.global_vals = global_vals
        assert not any([is_ref_type(self.global_types[idx]) for idx in global_idxs])
    
    def get_insts_for_replace(self) -> list[Inst]:
        return _ReplaceInstsFactory.get_global_insts(
            self.global_idxs,
            self.global_types,
            self.global_vals
        )
    
    def can_skip(self)->bool:
        result = len(self.global_vals) == 0
        return result

    def __repr__(self):
        return f'ReplaceWithGlobal(global_idxs={self.global_idxs}, global_types={self.global_types}, global_vals={self.global_vals})'

class ReplaceWithSaveStatesInput(NewInstStrategy):
    def __init__(self,
                 to_save_states_insts_scope:list[InstsScope],
                 to_save_part_types:list[funcType],
                 stack_snapshot_vals:list[list[str]],
                 expected_type:funcType,
                 expected_stack_values:list[str],
                 ori_insts:list[Inst]
                 ) -> None:
        super().__init__()
        self.can_preserve_type = False
        self.to_save_states_insts_scope = to_save_states_insts_scope
        self.stack_snapshot_vals = stack_snapshot_vals
        self.expected_type = expected_type
        self.ori_insts = ori_insts
        self.expected_stack_values = expected_stack_values
        self.to_save_part_types = to_save_part_types
        assert len(expected_stack_values) == len(expected_type.result_types)
        assert len(to_save_states_insts_scope) == len(stack_snapshot_vals)
        assert len(to_save_part_types) == len(to_save_states_insts_scope)
    def get_insts_for_replace(self) -> list[Inst]:
        insts = []
        # part 2: add each *stack snapshot* and *to_save_states_insts_scope*
        last_result = self.expected_type.param_types
        for stack_snapshot_val, stack_snapshot_type, to_save_states_insts_scope in zip(self.stack_snapshot_vals, self.to_save_part_types, self.to_save_states_insts_scope):
            param_types = stack_snapshot_type.param_types
            result_types = stack_snapshot_type.result_types
            # 
            expected_type = funcTypeFactory.generate_one_func_type_default(last_result, param_types)
            padding_insts = padding_input_type_naive(param_types, expected_type.param_types)
            insts.extend(padding_insts)
            # 
            set_val_insts = _ReplaceInstsFactory.get_stack_value_insts(param_types, stack_snapshot_val, drop_num=len(param_types))
            to_save_insts = self.ori_insts[to_save_states_insts_scope.start_idx:to_save_states_insts_scope.end_idx]
            insts.extend(set_val_insts)
            insts.extend(to_save_insts)
            last_result = result_types
            
        # part 3: add expected_type insts
        final_expected_val_insts = _gen_const_insts_with_expected_val(self.expected_type.result_types, self.expected_stack_values)
        insts.extend(final_expected_val_insts)
        return insts

    
class MultiStrategy(NewInstStrategy):
    def __init__(self, strategies: list[NewInstStrategy]) -> None:
        self.strategies = strategies
        if not any([strategy.can_preserve_type for strategy in strategies]):
            raise Exception('MultiStrategy must have at least one strategy that can preserve type')
    
    def get_insts_for_replace(self) -> list[Inst]:
        insts = []
        for strategy in self.strategies:
            insts.extend(strategy.get_insts_for_replace())
        return insts

    def __repr__(self):
        return f'MultiStrategy({", ".join([str(strategy) for strategy in self.strategies])})'


class _ReplaceInstsFactory:

    @staticmethod
    def get_local_insts(local_idxs: list[int], local_types: list[str], local_vals: list[Any]):
        insts = []
        for local_idx, local_val in zip(local_idxs,  local_vals):
            local_type = local_types[local_idx]
            print('local_idx', local_idx, 'local_type', local_type, 'local_val', local_val)
            const_val_inst = _get_const_inst(local_type, local_val)
            insts.append(const_val_inst)
            local_set_inst = InstFactory.gen_binary_info_inst_high_single_imm('local.set', local_idx)
            insts.append(local_set_inst)
        return insts

    @staticmethod
    def get_global_insts(global_idxs: list[int], global_types: list[str], global_vals: list[Any]):
        insts = []
        
        for global_idx, global_val in zip(global_idxs, global_vals):
            global_type = global_types[global_idx]
            const_val_inst = _get_const_inst(global_type, global_val)
            insts.append(const_val_inst)
            global_set_inst = InstFactory.gen_binary_info_inst_high_single_imm('global.set', global_idx)
            insts.append(global_set_inst)
        return insts

    @staticmethod
    def get_stack_value_insts(stack_types: list[str], stack_vals: list[Any], drop_num:int, gen_rand_ref:bool=False):
        if not gen_rand_ref:
            assert len(stack_types) == len(stack_vals)
            assert not any([is_ref_type(ty) for ty in stack_types])
        new_insts = []
        new_insts.extend(generate_n_drops(drop_num))
        set_val_insts = []
        val_idx = 0
        for ty in stack_types:
            if is_ref_type(ty):
                inst = get_inst_by_require_ty_const_n(ty)
            else:
                inst = _get_const_inst(ty, stack_vals[val_idx])
                val_idx += 1
            set_val_insts.append(inst)
        new_insts.extend(set_val_insts)
        return new_insts



def apply_strategy(
    parser: WasmParser, 
    scope: InstsScope, 
    strategy: NewInstStrategy,
    only_apply_when_shorter:bool=True
    ) -> Optional[int]:
    func: wasmFunc = parser.defined_funcs[scope.func_idx]
    insts = strategy.get_insts_for_replace()
    if insts == parser.defined_funcs[scope.func_idx].insts[scope.start_idx:scope.end_idx]:
        return None
    ori_length = scope.end_idx - scope.start_idx
    new_length = len(insts)
    if only_apply_when_shorter and new_length >= ori_length:
        return None
    _replace_insts_in_func(func, scope, insts)
    return new_length - ori_length




def _gen_const_insts_with_expected_val(stack_types:list[str], stack_vals:list[Any]):
    new_insts = []
    for stack_type, stack_val in zip(stack_types, stack_vals):
        inst = _get_const_inst(stack_type, stack_val)
        new_insts.append(inst)
    return new_insts

def _get_const_inst(stack_type, stack_val):
    if stack_type == 'i32':
        opcode = 'i32.const'
    elif stack_type == 'i64':
        opcode = 'i64.const'
    elif stack_type == 'f32':
        opcode = 'f32.const'
    elif stack_type == 'f64':
        opcode = 'f64.const'
    elif stack_type == 'v128':
        opcode = 'v128.const'
    else:
        raise NotImplementedError(f'Unsupported stack type: {stack_type}')
    inst = InstFactory.gen_binary_info_inst_high_single_imm(opcode, stack_val)
    return inst


def _replace_insts_in_func(
    func: wasmFunc,
    reduce_scope: InstsScope,
    new_insts: list[Inst]
):
    # assert 0
    # print('|||||||===|||',reduce_scope, new_insts)
    func.insts[reduce_scope.start_idx:reduce_scope.end_idx] = new_insts
