from WasmInfoCfg import SectionType
from extract_block_mutator.InstGeneration.InstFactory import InstFactory
from extract_block_mutator.InstUtil import Inst
from extract_block_mutator.WasmParser import WasmParser
from extract_block_mutator.encode.new_defined_data_type import Blocktype
from extract_block_mutator.funcType import funcType
from extract_block_mutator.funcTypeFactory import funcTypeFactory
from ...InstsScope import InstsScope
from reduction_analysis.ParserModification import DefinitionMutation, FuncInstMutation


class InstsReplacement:
    def __init__(self, 
                 parser: WasmParser,
                 func_idx: int,
                 original_scope: InstsScope,
                 new_insts: list[Inst],
                 update_cf_insts: bool = True
                 ):
        self.parser = parser
        self.func_idx = func_idx
        self.original_scope = original_scope
        self.new_insts, self.new_types = self._get_processed_insts_and_new_types(
            new_insts,
            parser.types,
            update_cf_insts
        )
        
        assert original_scope.func_idx == func_idx, f"Scope {original_scope} does not belong to function {func_idx}"
        
        self.original_insts = parser.defined_funcs[func_idx].insts[original_scope.start_idx:original_scope.end_idx].copy()
        
        self.new_end_idx = original_scope.start_idx + len(new_insts)


    def _get_processed_insts_and_new_types(
        self,
        insts:list[Inst], 
        types:list[funcType],
        update_cf_insts
    ):
        new_insts = []
        new_types = []
        # update_cf_insts=True
        for inst in insts:
            op = inst.opcode_text
            ori_types_num = len(types)
            
            if op == 'block' \
                or op == 'loop' \
                or op == 'if':
                imm = inst.imm_part.val
                assert isinstance(imm, Blocktype)
                
                if isinstance(imm.init_data, str) or isinstance(imm.init_data, bool):
                    pass
                else:
                    actual_func = imm.concrete_type(types)
                    if actual_func == funcTypeFactory.generate_one_func_type_default([], []):
                        inst = InstFactory.gen_binary_info_inst_high_single_imm(
                            op,
                            Blocktype(True)
                        )
                    elif actual_func in types:
                        new_idx = types.index(actual_func)
                        inst = InstFactory.gen_binary_info_inst_high_single_imm(
                            op,
                            Blocktype(new_idx)
                        )
                    elif len(actual_func.param_types) == 0 and len(actual_func.result_types) == 1:
                        result_str = actual_func.result_types[0]
                        inst = InstFactory.gen_binary_info_inst_high_single_imm(
                            op,
                            Blocktype(result_str)
                        )
                    else:
                        if update_cf_insts:
                            actual_func = imm.concrete_type(types)
                            # print(f"actual_func: {actual_func}")
                            # print(f'block type: {imm}')
                            # print(f'block type init_data: {imm.init_data}')
                            # print(f'types: {types}')
                            if actual_func in types:
                                new_idx = types.index(actual_func)
                            elif actual_func in new_types:
                                new_idx = new_types.index(actual_func) + ori_types_num
                            else:
                                new_types.append(actual_func)
                                new_idx = len(new_types)-1  + ori_types_num
                            
                            inst = InstFactory.gen_binary_info_inst_high_single_imm(
                                op,
                                Blocktype(new_idx)
                            )
                        else:
                            raise Exception(f'Cannot handle blocktype {actual_func} when not updating cf insts')
            new_insts.append(inst)
        return new_insts, new_types


    def as_func_inst_mutation(self) -> tuple[FuncInstMutation, list[DefinitionMutation]]:
        type_mutations = []
        for offset, new_type in enumerate(self.new_types):
            type_mutations.append(DefinitionMutation(
                SectionType.Type,
                self.parser.type_num + offset,
                self.parser.type_num + offset + 1,
                [new_type]
            ))
        return FuncInstMutation(
            func_idx=self.func_idx, 
            start_offset=self.original_scope.start_idx, 
            end_offset=self.original_scope.end_idx,
            new_insts=self.new_insts
        ), type_mutations
    
    @classmethod
    def from_multiple_scopes(cls, 
                            parser: WasmParser, 
                            func_idx: int, 
                            scopes: list[InstsScope], 
                            new_insts_list: list[list[Inst]]):
        assert len(scopes) == len(new_insts_list)
        replacements = []
        for scope, new_insts in zip(scopes, new_insts_list):
            replacements.append(cls(parser, func_idx, scope, new_insts))
        return replacements
    
    def apply(self):
        insts = self.parser.defined_funcs[self.func_idx].insts
        insts[self.original_scope.start_idx:self.original_scope.end_idx] = self.new_insts
        self.parser.types.extend(self.new_types)
        return True
    
    def revert(self):
        insts = self.parser.defined_funcs[self.func_idx].insts
        insts[self.original_scope.start_idx:self.new_end_idx] = self.original_insts
        return True
    
    def get_scope_info(self):
        return {
            'func_idx': self.func_idx,
            'start_idx': self.original_scope.start_idx,
            'end_idx': self.original_scope.end_idx,
            'original_length': len(self.original_insts),
            'new_length': len(self.new_insts)
        } 

    