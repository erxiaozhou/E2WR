import logging
from pathlib import Path
from typing import Callable, Optional
from util.debug_util import validate_wasm, wasm2wat
from extract_block_mutator.WasmParser import get_parser_from_wasm_path
from extract_block_mutator.encode.new_defined_data_type import Blocktype
from file_util import copy_file
from ..util import get_node_scope
from ...ASTState import ASTState
from .NodeReplacement import NodeReplacement
from ...ASTInfo.AST import ASTINode, BlockNode, IfNode, LoopNode, RootNode, is_ancestor_of

from extract_block_mutator.InstUtil import Inst
from ...InstsScope import InstsScope
from .InstsReplacement import InstsReplacement
from reduction_analysis.ParserModification import MultiPhaseMutationApplier
from reduction_analysis.ReduceUtil.BaseReducer import BaseReducer


class NodeRewriter:
    def __init__(self,
                 logger: logging.Logger,
                 DEBUG: bool,
                 oracle_func:Callable,
                 tmp_used_path: str,
                 force_return_false_on_invalid_case:bool
                 ):
        self.logger = logger
        self.DEBUG = DEBUG
        self.oracle_func = oracle_func
        self.tmp_used_path = tmp_used_path
        self.force_return_false_on_invalid_case = force_return_false_on_invalid_case
        self.best_path:Optional[str] = None
        # Track pending mutations across successful rewrites
        self.MPMApplier = MultiPhaseMutationApplier()
        self._base = BaseReducer(tmp_used_path=self.tmp_used_path, oracle_func=self.oracle_func, DEBUG=self.DEBUG)

    def set_best_path(self, best_path:str):
        self.best_path = best_path

    def update_by_rewrite_a_node(
        self,  
        ast_state:ASTState,
        node:ASTINode, 
        new_insts_node:ASTINode, 
        onlyless_inst:bool,
        check_invalid_and_return_false=False
        )->bool:
        result = self.try_replace_nodes(
            ast_state, 
            [node],
            [new_insts_node],
            onlyless_inst,
            check_invalid_and_return_false
        )
        return result


    def try_replace_nodes(
        self, 
        ast_state:ASTState,     
        ori_nodes:list[ASTINode], 
        new_nodes:list[ASTINode], 
        onlyless_inst:bool,
        check_invalid_and_return_false=False,
        update_cf_insts:bool=True
    ):
        # 
        ori_large_scope = get_ori_node_list_scope(ori_nodes)
        new_node_insts = get_insts_in_node_lists(new_nodes)
        ori_len = ori_large_scope.get_length()
        new_len = len(new_node_insts)
        if onlyless_inst and new_len >= ori_len:
            return False
        func_idx = ori_nodes[0].loc.func_idx
        

        result = self.try_replace_insts_in_one_func(
            func_idx,
            ast_state,
            [ori_large_scope],
            [new_node_insts],
            check_invalid_and_return_false,
            update_cf_insts
        )
        
        if result:
            node_replacement = NodeReplacement(ori_nodes, new_nodes)
            node_replacement.apply()
            ast_state.ast_info.update_loc_info(func_idx)
        # tmp check
        if self.DEBUG:
            # print('tmp check ast update, last result is ', result)
            _tmp_check_ast_update(ast_state, {func_idx})
        if self.DEBUG:
            if result:
                # print('New nodes are: ', [nn.get_node_info() for nn in new_nodes])
                if len(new_nodes) == 1 and isinstance(new_nodes[0], RootNode):
                    to_check_nodes = new_nodes[0].sub_nodes
                else:
                    to_check_nodes = new_nodes
                for nn in to_check_nodes:
                    root = ast_state.ast_info.get_func_root_ast(nn.loc.func_idx)
                    assert is_ancestor_of(root, nn), f'{root.get_node_info()} is not a ancestor of {nn.get_node_info()}'
                    # print('----------------')
                    # print(nn.get_node_info())
                    # print('----------------')
                # for n in ori_nodes:
                #     print('----------------')
                #     print(n.get_node_info())
                #     print('----------------')
        return result

    def try_replace_insts_in_one_func(
        self, 
        func_idx:int,
        ast_state:ASTState,
        insts_scopes_to_replace:list[InstsScope],
        new_insts:list[list[Inst]],
        check_invalid_and_return_false=False,
        update_cf_insts:bool=True
    ):
       
        if not insts_scopes_to_replace:
            return False
        assert len(insts_scopes_to_replace) == len(new_insts), print(f'{len(insts_scopes_to_replace)} != {len(new_insts)}', insts_scopes_to_replace, new_insts)
        
        min_start_idx = min(scope.start_idx for scope in insts_scopes_to_replace)
        max_end_idx = max(scope.end_idx for scope in insts_scopes_to_replace)
        
        big_scope = InstsScope(func_idx, min_start_idx, max_end_idx)
        
        original_insts = ast_state.parser.defined_funcs[func_idx].insts
        original_is0 = original_insts[min_start_idx:max_end_idx].copy()
        
        modified_is0 = original_is0.copy()
        
        scope_insts_pairs = list(zip(insts_scopes_to_replace, new_insts))
        scope_insts_pairs.sort(key=lambda pair: pair[0].start_idx, reverse=True)
        
        for scope, new_inst_list in scope_insts_pairs:
            assert scope.func_idx == func_idx, f"Scope {scope} does not belong to function {func_idx}"
            
            relative_start = scope.start_idx - min_start_idx
            relative_end = scope.end_idx - min_start_idx
            
            modified_is0[relative_start:relative_end] = new_inst_list
        
        big_replacement = InstsReplacement(ast_state.parser, func_idx, big_scope, modified_is0, update_cf_insts)

        can_replace = self._try_apply_replacement(
            ast_state,
            big_replacement, 
            check_invalid_and_return_false
        )
        return can_replace

    def apply_insts_in_one_func_without_check(
        self,
        func_idx:int,
        ast_state:ASTState,
        insts_scopes_to_replace:list[InstsScope],
        new_insts:list[list[Inst]],
    ) -> None:
        if not insts_scopes_to_replace:
            return
        assert len(insts_scopes_to_replace) == len(new_insts), print(f'{len(insts_scopes_to_replace)} != {len(new_insts)}', insts_scopes_to_replace, new_insts)

        min_start_idx = min(scope.start_idx for scope in insts_scopes_to_replace)
        max_end_idx = max(scope.end_idx for scope in insts_scopes_to_replace)
        big_scope = InstsScope(func_idx, min_start_idx, max_end_idx)

        original_insts = ast_state.parser.defined_funcs[func_idx].insts
        original_is0 = original_insts[min_start_idx:max_end_idx].copy()
        modified_is0 = original_is0.copy()

        scope_insts_pairs = list(zip(insts_scopes_to_replace, new_insts))
        scope_insts_pairs.sort(key=lambda pair: pair[0].start_idx, reverse=True)

        for scope, new_inst_list in scope_insts_pairs:
            assert scope.func_idx == func_idx, f"Scope {scope} does not belong to function {func_idx}"
            relative_start = scope.start_idx - min_start_idx
            relative_end = scope.end_idx - min_start_idx
            modified_is0[relative_start:relative_end] = new_inst_list

        big_replacement = InstsReplacement(ast_state.parser, func_idx, big_scope, modified_is0)
        inst_mutation, type_mutations = big_replacement.as_func_inst_mutation()
        big_replacement.apply()
        self.MPMApplier.merge_to_snapshot(
            snapshot=ast_state.snapshot,
            inst_mutations=[inst_mutation],
            type_mutations=type_mutations,
        )
        self.MPMApplier.flush(ast_state.snapshot)

    def try_probe_insts_in_one_func(
        self,
        func_idx:int,
        ast_state:ASTState,
        insts_scopes_to_replace:list[InstsScope],
        new_insts:list[list[Inst]],
        check_invalid_and_return_false=False
    ) -> bool:
        if not insts_scopes_to_replace:
            return False
        assert len(insts_scopes_to_replace) == len(new_insts), print(f'{len(insts_scopes_to_replace)} != {len(new_insts)}', insts_scopes_to_replace, new_insts)

        min_start_idx = min(scope.start_idx for scope in insts_scopes_to_replace)
        max_end_idx = max(scope.end_idx for scope in insts_scopes_to_replace)

        big_scope = InstsScope(func_idx, min_start_idx, max_end_idx)

        original_insts = ast_state.parser.defined_funcs[func_idx].insts
        original_is0 = original_insts[min_start_idx:max_end_idx].copy()

        modified_is0 = original_is0.copy()

        scope_insts_pairs = list(zip(insts_scopes_to_replace, new_insts))
        scope_insts_pairs.sort(key=lambda pair: pair[0].start_idx, reverse=True)

        for scope, new_inst_list in scope_insts_pairs:
            assert scope.func_idx == func_idx, f"Scope {scope} does not belong to function {func_idx}"

            relative_start = scope.start_idx - min_start_idx
            relative_end = scope.end_idx - min_start_idx

            modified_is0[relative_start:relative_end] = new_inst_list

        big_replacement = InstsReplacement(ast_state.parser, func_idx, big_scope, modified_is0)

        return self._try_apply_replacement(
            ast_state,
            big_replacement,
            check_invalid_and_return_false
        )

    def _try_apply_replacement(
        self,
        ast_state:ASTState,
        replacement: InstsReplacement,
        check_invalid_and_return_false=False
    ) -> bool:
        snapshot = ast_state.snapshot
        inst_mutation, type_mutations = replacement.as_func_inst_mutation()
        tmp_snapshot = snapshot.copy_for_code_mutations({inst_mutation.func_idx})
        # For trial encoding, start with a fresh applier to avoid leaking persistent flags
        mutation_applier = MultiPhaseMutationApplier()
        mutation_applier.merge_to_snapshot(
            snapshot=tmp_snapshot,
            inst_mutations=[inst_mutation],
            type_mutations=type_mutations,
        )

        mutation_applier.flush_and_encode(
            snapshot=tmp_snapshot,
            output_file=self.tmp_used_path,
        )
        if self.DEBUG:
            print('Test file is writter to ', self.tmp_used_path)
        
        whether_valid = self._base.tmp_file_is_valid()
        if not whether_valid:
            if self.DEBUG:
                wat_path = Path(self.tmp_used_path).with_suffix('.wat')
                wasm2wat(self.tmp_used_path, wat_path)
            if (not check_invalid_and_return_false) and (not self.force_return_false_on_invalid_case):
                print(f'check_invalid_and_return_false is False, but the wasm is invalid, {wat_path} is saved')
                # cur_parser = get_parser_from_wasm_path('test_reduce/result/smith_p3_1973/NodeShrink/tmp_used.wasm')
                raise Exception(f'check_invalid_and_return_false is False, but the wasm is invalid, {wat_path} is saved')
        if whether_valid and self._base.oracle_passes():
            replacement.apply()
            # Merge into NodeRewriter's store and flush into ASTState snapshot
            self.MPMApplier.merge_to_snapshot(
                snapshot=snapshot,
                inst_mutations=[inst_mutation],
                type_mutations=type_mutations,
            )
            # Commit code definitions and updated section bytes to ASTState snapshot
            self.MPMApplier.flush(snapshot)
            if self.best_path:
                self._base.commit_tmp_to(self.best_path)
                # Path(self.tmp_used_path).rename(self.best_path)
            return True
        else:
            return False

    def _try_apply_replacement_probe(
        self,
        ast_state:ASTState,
        replacement: InstsReplacement,
        check_invalid_and_return_false=False
    ) -> bool:
        inst_mutation, type_mutations = replacement.as_func_inst_mutation()
        tmp_snapshot = ast_state.snapshot.copy_for_code_mutations({inst_mutation.func_idx})
        mutation_applier = MultiPhaseMutationApplier()
        mutation_applier.merge_to_snapshot(
            snapshot=tmp_snapshot,
            inst_mutations=[inst_mutation],
            type_mutations=type_mutations,
        )

        mutation_applier.flush_and_encode(
            snapshot=tmp_snapshot,
            output_file=self.tmp_used_path,
        )
        if self.DEBUG:
            print('Test file is writter to ', self.tmp_used_path)

        whether_valid = self._base.tmp_file_is_valid()
        if not whether_valid:
            if self.DEBUG:
                wat_path = Path(self.tmp_used_path).with_suffix('.wat')
                wasm2wat(self.tmp_used_path, wat_path)
            if (not check_invalid_and_return_false) and (not self.force_return_false_on_invalid_case):
                print(f'check_invalid_and_return_false is False, but the wasm is invalid, {wat_path} is saved')
                raise Exception(f'check_invalid_and_return_false is False, but the wasm is invalid, {wat_path} is saved')
        if whether_valid and self._base.oracle_passes():
            return True
        else:
            return False

    def try_replace_multi_pair_nodes(
        self, 
        ast_state:ASTState,     
        ori_node_lists:list[list[ASTINode]], 
        new_node_lists:list[list[ASTINode]], 
        onlyless_inst:bool,
        check_invalid_and_return_false=False
    ):
        # 
        ori_scopes = [get_ori_node_list_scope(node_list) for node_list in ori_node_lists]
        new_inst_lists = [get_insts_in_node_lists(new_node_list) for new_node_list in new_node_lists]
        ori_scope_len = sum([scope.get_length() for scope in ori_scopes])
        new_inst_num = sum([len(inst_list) for inst_list in new_inst_lists])
        if onlyless_inst and new_inst_num >= ori_scope_len:
            return False
        func_idx = ori_node_lists[0][0].loc.func_idx
        result = self.try_replace_insts_in_one_func(
            func_idx,
            ast_state,
            ori_scopes,
            new_inst_lists,
            check_invalid_and_return_false
        )
        if result:
            for ori_list, new_list in zip(ori_node_lists, new_node_lists):
                node_replacement = NodeReplacement(ori_list, new_list)
                node_replacement.apply()
            ast_state.ast_info.update_loc_info(func_idx)
        if self.DEBUG:
            if len(new_node_lists):
                if result:
                    node = new_node_lists[0][0].get_parent()
                else:
                    node = ori_node_lists[0][0].get_parent()
                _tmp_check_ast_update(ast_state, {func_idx})
                cur_input_parser = ast_state.parser
                node_insts = node.get_insts()
                parser_insts = ast_state.parser.defined_funcs[node.loc.func_idx].insts
                parser_scope_insts = parser_insts[node.loc.inst_idx:node.loc.inst_idx+node.get_length()]
                cur_input_parser_insts = cur_input_parser.defined_funcs[node.loc.func_idx].insts
                cur_input_parser_scope_insts = cur_input_parser_insts[node.loc.inst_idx:node.loc.inst_idx+node.get_length()]
                for node_inst, parser_inst, cur_input_parser_inst in zip(node_insts, parser_scope_insts, cur_input_parser_scope_insts):
                    assert node_inst.opcode_text == parser_inst.opcode_text
                    assert node_inst.opcode_text == cur_input_parser_inst.opcode_text
        return result

    def replace_nodes_without_check(
        self,
        ast_state:ASTState,
        ori_nodes:list[ASTINode],
        new_nodes:list[ASTINode],
        onlyless_inst:bool,
    ) -> bool:
        ori_scope = get_ori_node_list_scope(ori_nodes)
        new_node_insts = get_insts_in_node_lists(new_nodes)
        ori_len = ori_scope.get_length()
        new_len = len(new_node_insts)
        if onlyless_inst and new_len >= ori_len:
            return False
        func_idx = ori_nodes[0].loc.func_idx

        self.apply_insts_in_one_func_without_check(
            func_idx,
            ast_state,
            [ori_scope],
            [new_node_insts],
        )

        node_replacement = NodeReplacement(ori_nodes, new_nodes)
        node_replacement.apply()
        ast_state.ast_info.update_loc_info(func_idx)
        if self.DEBUG:
            _tmp_check_ast_update(ast_state, {func_idx})
        return True


def _tmp_check_ast_update(
    ast_state:ASTState,
    updated_func_idxs:set[int]
):
    func_num = len(ast_state.parser.defined_funcs)
    # print(f'parser is {id(ast_state.parser)}')
    for func_idx in updated_func_idxs:
        func = ast_state.parser.defined_funcs[func_idx]
        func_root = ast_state.ast_info.get_func_root_ast(func_idx)
        func_root_insts = func_root.get_insts()
        assert len(func.insts) == len(func_root_insts), f'{func.insts} != {func_root_insts}'
        for parser_inst, func_root_inst in zip(func.insts, func_root_insts):
            assert parser_inst.opcode_text == func_root_inst.opcode_text
        all_non_zero_nodes = ast_state.ast_info.get_all_nodes_in_a_func(func_idx)
        for n in all_non_zero_nodes:
            if n.get_length() == 0:
                continue
            start_idx = n.loc.inst_idx
            ast_start_inst_in_parser = func.insts[start_idx]
            ast_start_inst_in_ast = n.get_insts()[0]
            assert ast_start_inst_in_parser.opcode_text == ast_start_inst_in_ast.opcode_text
            
        insts = func.insts
        # print('ast inst num:', ast_state.ast_info.get_func_root_ast(func_idx).get_length())
        # print('func inst num:', len(insts))
        # print(f'insts are {insts}')
        for n in ast_state.ast_info.get_all_nodes_in_a_func(func_idx):
            if isinstance(n, BlockNode):
                assert insts[n.loc.inst_idx].opcode_text == 'block'
                assert insts[n.loc.inst_idx + n.get_length()-1].opcode_text == 'end'
            elif isinstance(n, IfNode):
                assert insts[n.loc.inst_idx].opcode_text == 'if'
                assert insts[n.loc.inst_idx + n.get_length()-1].opcode_text == 'end'
            elif isinstance(n, LoopNode):
                assert insts[n.loc.inst_idx].opcode_text == 'loop'
                assert insts[n.loc.inst_idx + n.get_length()-1].opcode_text == 'end'
            # ast_state.ast_info.get


def get_insts_in_node_lists(new_nodes:list[ASTINode])->list[Inst]:
    each_new_node_insts = [new_node.get_insts() for new_node in new_nodes]
    new_node_insts = []
    for each_new_node_insts in each_new_node_insts:
        new_node_insts.extend(each_new_node_insts)
    return new_node_insts


def get_ori_node_list_scope(ori_nodes:list[ASTINode])->InstsScope:
    ori_scopes = [get_node_scope(node) for node in ori_nodes]
    for i in range(len(ori_nodes)-1):
        cur_end = ori_scopes[i].end_idx
        next_start = ori_scopes[i+1].start_idx
        assert cur_end == next_start, f'{ori_nodes[i].get_node_info()} and {ori_nodes[i+1].get_node_info()} are not consecutive'
    ori_large_scope = InstsScope(
        ori_nodes[0].loc.func_idx, 
        ori_scopes[0].start_idx, 
        ori_scopes[-1].end_idx
    )
    return ori_large_scope
