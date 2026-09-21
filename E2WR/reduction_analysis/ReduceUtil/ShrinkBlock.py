import time
from extract_block_mutator.InstGeneration.InstFactory import InstFactory
from extract_block_mutator.InstUtil import Inst
from extract_block_mutator.encode.new_defined_data_type import Blocktype
from extract_block_mutator.funcType import funcType
from extract_block_mutator.funcTypeFactory import funcTypeFactory
from reduction_analysis.ASTInfo.AST import NodeList
from .NonInstrumentationInstStrategy import GenSpecificType
from .NewInstUtil import get_inst_by_require_ty_const_n
from reduction_analysis.InferStackUtil import infer_stack_types_on_probe_loc
from reduction_analysis.StackState import StackState
from ..ASTInfo.AST import ASTINode, ASTNodeLoc, BlockNode, IfNode, InstsNode, LoopNode, func2AST, insts2AST, traverse_ast
from ..ASTState import ASTState
from reduction_analysis.ReduceUtil.RewritingUtil.NodeRewriter import NodeRewriter
from typing import Any, Generator, Optional, Union
from abc import ABC, abstractmethod
MAX_DEPTH = None

class ShrinkBase(ABC):
    def __init__(
        self,
        node_rewriter:NodeRewriter
    ):
        self.node_rewriter = node_rewriter

    @abstractmethod
    def shrink(
        self,
        target_node:Union[BlockNode, LoopNode],
        ast_state:ASTState
    ) -> bool:
        raise NotImplementedError

def common_rewrite_insts(
    inner_insts:list[Inst]
)  -> list[Inst]:
    cur_depth = 0
    # candi_sub_blocks = []
    new_insts:list[Inst] = []
    for inst_idx, inst in enumerate(inner_insts):
        if inst.opcode_text == 'loop':
            cur_depth += 1
        elif inst.opcode_text == 'block':
            cur_depth += 1
        elif inst.opcode_text == 'if':
            cur_depth += 1
        elif inst.opcode_text == 'end':
            cur_depth -= 1
        if inst.opcode_text == 'br' or inst.opcode_text == 'br_if':
            cur_imm = inst.imm_part.val
            if cur_imm == cur_depth:
                if inst.opcode_text == 'br':
                    new_insts.append(InstFactory.opcode_inst('unreachable'))
                else:
                    new_insts.append(InstFactory.opcode_inst('drop'))
                  
            elif cur_imm > cur_depth:
                new_target = InstFactory.gen_binary_info_inst_high_single_imm(inst.opcode_text, cur_imm - 1)
                new_insts.append(new_target)
            else:
                new_insts.append(inst)
        elif inst.opcode_text == 'br_table':
            imm_dict = inst.imm_part.data
            label_idxs = imm_dict['l']
            default_label = imm_dict['l_N']
            if (label_idxs is not None and cur_depth in label_idxs) or cur_depth == default_label:
                early_return_inst_idx = inst_idx
                new_insts.append(InstFactory.opcode_inst('unreachable'))
                 
            else:
                if label_idxs is not None:
                    new_lable_idxs = [d-1  if d > cur_depth else d for d in label_idxs]
                else:
                    new_lable_idxs = None
                if default_label > cur_depth:
                    new_default_label = default_label - 1
                else:
                    new_default_label = default_label
                imm_dict = {'l': new_lable_idxs, 'l_N': new_default_label}
                new_inst = InstFactory.gen_binary_info_inst_high(inst.opcode_text, imm_dict)
                new_insts.append(new_inst)
        else:
            new_insts.append(inst)
    assert cur_depth == 0
    return new_insts
def inst_can_jump_to_here(inst:Inst, cur_depth:int) -> bool:
    if inst.opcode_text == 'br' or inst.opcode_text == 'br_if':
        return inst.imm_part.val == cur_depth
    elif inst.opcode_text == 'br_table':
        imm_dict = inst.imm_part.data
        label_idxs = imm_dict['l']
        default_label = imm_dict['l_N']
        if default_label == cur_depth:
            return True
        if any(d == cur_depth for d in label_idxs):
            return True
    return False


def idenfity_nodes_can_jump_to_here(
                 target_node:NodeList,
                 max_depth:Optional[int]=MAX_DEPTH) -> Generator[ASTINode, None, None]:
    def process_node(node, depth=0):
        if max_depth is not None and depth > max_depth:
            return
        
        if isinstance(node, InstsNode) and node.insts:
            last_inst = node.insts[-1]
            if inst_can_jump_to_here(last_inst, depth):
                yield node
        
        elif isinstance(node, BlockNode) or isinstance(node, LoopNode):
            new_depth = depth + 1
            for sub_node in node.sub_node_list.sub_nodes:
                yield from process_node(sub_node, new_depth)
        
        elif isinstance(node, IfNode):
            new_depth = depth + 1
            for sub_node in node.if_sub_node_list.sub_nodes:
                yield from process_node(sub_node, new_depth)
            
            if node.has_else:
                for sub_node in node.else_sub_node_list.sub_nodes:
                    yield from process_node(sub_node, new_depth)
        
        elif isinstance(node, NodeList):
            for sub_node in node.sub_nodes:
                yield from process_node(sub_node, depth)
    
    for node in target_node.sub_nodes:
        yield from process_node(node)



def _is_all_before_loc(node:ASTINode, loc:ASTNodeLoc):
    start_idx = node.loc.inst_idx
    target_pos = loc.inst_idx
    if start_idx > target_pos:
        return False
    end_pos = start_idx + node.get_length()
    if end_pos <= target_pos:
        return False
    return True

def collect_nodes_before_loc(root_node:ASTINode, loc:ASTNodeLoc):
    nodes = traverse_ast(root_node, lambda x: _is_all_before_loc(x, loc), collect_results=True)
    return nodes

def is_can_jump_insts_node(insts_node:InstsNode, depth:int):
    if isinstance(insts_node, InstsNode) and insts_node.insts:
        last_inst = insts_node.insts[-1]
        if inst_can_jump_to_here(last_inst, depth):
            return True
    return False


def get_first_jump_back_node(start_node:ASTINode, cur_depth:int=0, max_depth:Optional[int]=MAX_DEPTH)->Optional[list[InstsNode]]:
    if max_depth is not None and cur_depth > max_depth:
        return None
    
    if isinstance(start_node, InstsNode):
        return None
    elif isinstance(start_node, NodeList):
        # index = start_node.sub_nodes.index(end_node)
        # if end_node in start_node.sub_nodes:
        #     # index = start_node.sub_nodes.index(end_node)
        #     result = 
        results:list[InstsNode] = []
        for idx, sub_node in enumerate(start_node.sub_nodes):
            if isinstance(sub_node, InstsNode):
                insts = sub_node.get_insts()
                if insts:
                    last_inst = insts[-1]
                    
                    if is_can_jump_insts_node(sub_node, cur_depth):
                        # ast_inst = sub_node.insts[-1]
                        results.append(sub_node)
                        # if ast_inst.opcode_text != 'br_if':
                        #     return results
                    if last_inst.opcode_text in {'br', 'unreachable', 'return', 'br_table'}:
                        if len(results) == 0:
                            return None
                        return results
            else:
                sub_node_result = get_first_jump_back_node(sub_node, cur_depth, max_depth)
                if sub_node_result is not None:
                    # cur_node_result = [sub_node]
                    for cur_sub_list in sub_node_result:
                        # cur_r = cur_node_result + cur_sub_list
                        results.append(cur_sub_list)
        return results
    elif isinstance(start_node, BlockNode):
        node_list = start_node.sub_node_list
        return get_first_jump_back_node(node_list,  cur_depth+1, max_depth)
    elif isinstance(start_node, LoopNode):
        node_list = start_node.sub_node_list
        return get_first_jump_back_node(node_list, cur_depth+1, max_depth)
    elif isinstance(start_node, IfNode):
        r1 = get_first_jump_back_node(start_node.if_sub_node_list, cur_depth+1, max_depth)
        r2 = None
        if start_node.has_else:
            r2 = get_first_jump_back_node(start_node.else_sub_node_list, cur_depth+1, max_depth)
        results = []
        if r1 is not None:
            results.extend(r1)
        if r2 is not None:
            results.extend(r2)
        if len(results) == 0:
            return None
        return results


def get_per_exec_nodes(start_node:ASTINode, end_node:InstsNode, cur_depth=0)->Optional[list[tuple[NodeList, int, int]]]:  # parent list ; idx ; depth
    # nodes = []
    if isinstance(start_node, InstsNode):
        return None
    elif isinstance(start_node, NodeList):
        # index = start_node.sub_nodes.index(end_node)
        # if end_node in start_node.sub_nodes:
        #     # index = start_node.sub_nodes.index(end_node)
        #     result = 
        for idx, sub_node in enumerate(start_node.sub_nodes):
            if isinstance(sub_node, InstsNode) and sub_node == end_node:
                return [(start_node, idx, cur_depth)]
            else:
                sub_node_result = get_per_exec_nodes(sub_node, end_node, cur_depth)
                if sub_node_result is not None:
                    return [(start_node, idx, cur_depth)] + sub_node_result
    elif isinstance(start_node, BlockNode):
        node_list = start_node.sub_node_list
        return get_per_exec_nodes(node_list, end_node, cur_depth+1)
    elif isinstance(start_node, LoopNode):
        node_list = start_node.sub_node_list
        return get_per_exec_nodes(node_list, end_node, cur_depth+1)
    elif isinstance(start_node, IfNode):
        r1 = get_per_exec_nodes(start_node.if_sub_node_list, end_node, cur_depth+1)
        if start_node.has_else:
            if r1 is None:
                r2 = get_per_exec_nodes(start_node.else_sub_node_list, end_node, cur_depth+1)
                return r2
        return r1



def gen_new_insts_for_node_jump_to_target(node:InstsNode, ast_state:ASTState):
    node_end_inst_idx = node.loc.inst_idx + node.get_length() - 1
    new_loc = ASTNodeLoc(func_idx=node.loc.func_idx, inst_idx=node_end_inst_idx)
    stack_state: StackState = infer_stack_types_on_probe_loc(
        probe_loc=new_loc,
        parser=ast_state.parser, 
        ast_info=ast_state.ast_info,
        known_innermost_node=node
    )
    # block_type = node.parent.parent.get_block_type()
    insts_node_parent = node.parent
    assert isinstance(insts_node_parent, NodeList)
    parent_node_list_inst_idx = insts_node_parent.loc.inst_idx
    node_inst_offset = node.loc.inst_idx - parent_node_list_inst_idx
    node_list_insts = insts_node_parent.get_insts()
    node_list_insts_before_node = node_list_insts[:node_inst_offset]
    insts_node_parent_parent = insts_node_parent.parent
    assert insts_node_parent_parent is not None
    block_type = insts_node_parent_parent.get_block_type()
    specific_type_gen_insts = get_insts_padding_pos_and_list_end(stack_state, block_type)
    all_insts = node_list_insts_before_node + node.insts[:-1] + specific_type_gen_insts
    return all_insts

def get_insts_padding_pos_and_list_end(stack_state:StackState, block_type:funcType):
    expected_result_types = block_type.result_types
    expected_rest_types = stack_state.all_rest_types[0]
    expected_type = funcTypeFactory.generate_one_func_type_default(
        expected_rest_types,
        expected_result_types
    )
    specific_type_gen_insts = GenSpecificType(expected_type).get_insts_for_replace()
    return specific_type_gen_insts
    

def is_candi_target(ori_types:list[str], possible_types:list[str])->bool:
    if len(ori_types) < len(possible_types):
        return False
    idx = len(possible_types) - 1
    while idx >= 0:
        ori_type = ori_types[idx]
        possible_type = possible_types[idx]
        if ori_type != possible_type:
            return False
        idx -= 1
    return True


class BlockReplacementGenerator:
    def __init__(self, 
                 target_node:NodeList,
                 to_replace_node:Union[BlockNode, LoopNode, IfNode],
                #  block_type:funcType,
                #  loc:ASTNodeLoc,
                 ast_state:ASTState,
                 max_depth:Optional[int]=MAX_DEPTH  
                 ):
        self.target_node = target_node
        self.block_type = to_replace_node.get_block_type()
        self.loc = to_replace_node.loc
        self.target_node_start_idx = target_node.loc.inst_idx
        self.ast_state = ast_state
        self.target_node_inst_idx = self.target_node.loc.inst_idx
        self.max_depth = max_depth
        # 
        jump_cur_nodes_gen = idenfity_nodes_can_jump_to_here(target_node, max_depth=max_depth)
        jump_cur_nodes = list(jump_cur_nodes_gen)
        self.jump_cur_node_head_locs = [i.loc for i in jump_cur_nodes]

    @property
    def target_node_insts(self):
        return self.target_node.get_insts()

    def rewrite_all_inner_insts(self)->list[list[Inst]]:
        target_node_end_idx = self.target_node_inst_idx + self.target_node.get_length() 
        meta_result = get_first_jump_back_node(self.target_node, 0, max_depth=self.max_depth)
        replacements = []
        if meta_result is not None:
            
            for meta_result in meta_result:
                # assert len(meta_result) > 0
                last_node = meta_result
                assert isinstance(last_node, InstsNode)
                to_replace_node = last_node.parent
                assert isinstance(to_replace_node, NodeList)
                new_insts = gen_new_insts_for_node_jump_to_target(last_node, self.ast_state)
                ori_all_insts = self.target_node_insts
                # to_replace_node_start_idx = to_replace_node.loc.inst_idx
                to_replace_node_offset = to_replace_node.loc.inst_idx - self.target_node_start_idx
                to_replace_node_length = to_replace_node.get_length()
                new_insts_before_rewrite = ori_all_insts[:to_replace_node_offset] + new_insts + ori_all_insts[to_replace_node_offset+to_replace_node_length:]
                rewrite_insts = common_rewrite_insts(new_insts_before_rewrite)
                replacements.append(rewrite_insts)
            
        
        to_detect_loc = ASTNodeLoc(func_idx=self.loc.func_idx, inst_idx=target_node_end_idx)
        stack_state = infer_stack_types_on_probe_loc(
            probe_loc=to_detect_loc,
            parser=self.ast_state.parser,
            ast_info=self.ast_state.ast_info,
            known_innermost_node=self.target_node
        )
        padding_insts = get_insts_padding_pos_and_list_end(stack_state, self.block_type)
        ori_insts = self.target_node_insts
        new_insts_before_rewrite = ori_insts + padding_insts
        rewrite_insts = common_rewrite_insts(new_insts_before_rewrite)
        replacements.append(rewrite_insts)
        return replacements
        # else:
                
    def replacement_generator_from_short_tolong(self):
        replacements = self.rewrite_all_inner_insts()
        replacements = sorted(replacements, key=lambda x: len(x))
        for replacement in replacements:
            yield replacement
        
            

def update_insts_in_block_v2(target_node:NodeList, to_replace_node:Union[BlockNode, LoopNode, IfNode], ast_state:ASTState, max_depth:Optional[int]=MAX_DEPTH):
    replacement_gen = BlockReplacementGenerator(target_node, to_replace_node, ast_state, max_depth=max_depth)
    for replacement in replacement_gen.replacement_generator_from_short_tolong():
        yield replacement

class BlockShrink(ShrinkBase): 
    def shrink(
        self,
        target_node:Union[BlockNode, LoopNode],
        ast_state:ASTState,
        rest_time:Optional[float]=None
    ) -> tuple[bool, list[ASTINode]]:
        if rest_time is not None and rest_time <= 0:
            return False, []
        
        start_time = time.time()
        if rest_time is not None:
            self.expected_end_time = start_time + rest_time
        else:
            self.expected_end_time = None
        
        for insts in update_insts_in_block_v2(target_node.sub_node_list, target_node, ast_state):
            if self.expected_end_time is not None and time.time() > self.expected_end_time:
                return False, []
            
            result, new_nodes =  _get_new_node_list_from_insts_and_replace_old_node_list(
                insts=insts,
                expected_type=target_node.get_block_type(),
                node_rewriter=self.node_rewriter,
                target_node=target_node,
                ast_state=ast_state,
                onlyless_inst=False
            )
            if result:
                return True, new_nodes
        
        # update the block type
        # ## try param
        if self.expected_end_time is not None and time.time() > self.expected_end_time:
            return False, []
        
        new_insts = try_update_block_with_shorter_param_type(target_node)
        if new_insts is not None:
            result, new_nodes =  _get_new_node_list_from_insts_and_replace_old_node_list(
                insts=new_insts,
                expected_type=target_node.get_block_type(),
                node_rewriter=self.node_rewriter,
                target_node=target_node,
                ast_state=ast_state,
                onlyless_inst=False
            )
            if result:
                return True, new_nodes
        
        if self.expected_end_time is not None and time.time() > self.expected_end_time:
            return False, []
        
        new_insts = try_update_block_with_shorter_result_type(target_node)
        if new_insts is not None:
            result, new_nodes =  _get_new_node_list_from_insts_and_replace_old_node_list(
                insts=new_insts,
                expected_type=target_node.get_block_type(),
                node_rewriter=self.node_rewriter,
                target_node=target_node,
                ast_state=ast_state,
                onlyless_inst=False
            )
            if result:
                return True, new_nodes
        # 
        return False, []

def try_update_block_with_shorter_param_type(
    target_node:Union[BlockNode, LoopNode, IfNode]
) -> Optional[list[Inst]]:
    if isinstance(target_node, IfNode):
        assert not target_node.has_else
    block_type = target_node.get_block_type()
    params = block_type.param_types
    if len(params) == 0:
        return None
    # ori_insts = target_node.get_insts()
    new_type = funcTypeFactory.generate_one_func_type_default(
        params[:-1],
        block_type.result_types
    )
    new_insts = []
    ori_insts = target_node.get_insts()
    op = ori_insts[0].opcode_text
    new_insts.append(InstFactory.opcode_inst('drop'))
    new_block_title_inst = InstFactory.gen_binary_info_inst_high_single_imm(op, Blocktype(new_type))
    new_insts.append(new_block_title_inst)
    droped_type = params[-1]
    new_insts.append(get_inst_by_require_ty_const_n(droped_type))
    new_insts.extend(target_node.get_insts()[1:])
    return new_insts

def try_update_block_with_shorter_result_type(
    target_node:Union[BlockNode, LoopNode, IfNode]
) -> Optional[list[Inst]]:
    if isinstance(target_node, IfNode):
        assert not target_node.has_else
    block_type = target_node.get_block_type()
    result_types = block_type.result_types
    if len(result_types) == 0:
        return None
    # ori_insts = target_node.get_insts()
    new_type = funcTypeFactory.generate_one_func_type_default(
        block_type.param_types,
        result_types[:-1]
    )
    new_insts = []
    ori_insts = target_node.get_insts()
    op = ori_insts[0].opcode_text
    new_block_title_inst = InstFactory.gen_binary_info_inst_high_single_imm(op, Blocktype(new_type))
    new_insts.append(new_block_title_inst)
    if not isinstance(target_node, IfNode):
        inner_insts = target_node.sub_node_list.get_insts()
    else:
        inner_insts = target_node.if_sub_node_list.get_insts()
    inner_insts = _process_block_inner_code_when_update_shorter_return_type(inner_insts)
    new_insts.extend(inner_insts)
    new_insts.append(InstFactory.opcode_inst('drop'))
    new_insts.append(InstFactory.opcode_inst('end'))
    new_insts.append(get_inst_by_require_ty_const_n(result_types[-1]))
    return new_insts

def _process_block_inner_code_when_update_shorter_return_type(inner_insts:list[Inst])->list[Inst]:
    new_insts = []
    cur_depth = 0
    
    for inst in inner_insts:
        if inst.opcode_text == 'loop':
            cur_depth += 1
        elif inst.opcode_text == 'block':
            cur_depth += 1
        elif inst.opcode_text == 'if':
            cur_depth += 1
        elif inst.opcode_text == 'end':
            cur_depth -= 1
        if inst_can_jump_to_here(inst, 0):
            new_insts.append(InstFactory.opcode_inst('drop'))
        
        new_insts.append(inst)
    
    return new_insts

def try_update_if_with_shorter_result_type(
    target_node:IfNode
) -> Optional[list[Inst]]:
    block_type = target_node.get_block_type()
    result_types = block_type.result_types
    if len(result_types) == 0:
        return None
    if not target_node.has_else:
        return try_update_block_with_shorter_result_type(target_node)
    else:
        new_type = funcTypeFactory.generate_one_func_type_default(
            block_type.param_types,
            result_types[:-1]
        )
        new_insts = []
        new_insts.append(InstFactory.gen_binary_info_inst_high_single_imm('if', Blocktype(new_type)))
        new_insts.extend(target_node.if_sub_node_list.get_insts())
        new_insts.append(InstFactory.opcode_inst('drop'))
        new_insts.append(InstFactory.opcode_inst('else'))
        new_insts.extend(target_node.else_sub_node_list.get_insts())
        new_insts.append(InstFactory.opcode_inst('drop'))
        new_insts.append(InstFactory.opcode_inst('end'))
        new_insts.append(get_inst_by_require_ty_const_n(result_types[-1]))
        return new_insts
    # new_insts = []
    # new_insts.append(InstFactory.opcode_inst('drop'))
    # new_insts.append(InstFactory.opcode_inst('end'))
    # new_type = funcTypeFactory.generate_one_func_type_default(
    #     block_type.param_types,



class IfShrinkBase(ShrinkBase):
    def shrink(
        self,
        target_node:IfNode,
        ast_state:ASTState,
        rest_time:Optional[float]=None
    ) -> tuple[bool, list[ASTINode]]:
        if rest_time is not None and rest_time <= 0:
            return False, []
        
        start_time = time.time()
        if rest_time is not None:
            self.expected_end_time = start_time + rest_time
        else:
            self.expected_end_time = None
        
        if_branch = target_node.if_sub_node_list
        if target_node.has_else:
            else_branch = target_node.else_sub_node_list
            if if_branch.get_length() > else_branch.get_length():
                to_try_list = [else_branch, if_branch]
            else:
                to_try_list = [if_branch, else_branch]
        else:
            to_try_list = [if_branch]
        for one_branch in to_try_list:
            if self.expected_end_time is not None and time.time() > self.expected_end_time:
                return False, []
            try_result, new_nodes = self.try_one_branch(target_node, ast_state, one_branch)
            if try_result:
                return True, new_nodes
        return False, []

    @abstractmethod
    def try_one_branch(
        self,
        target_node:IfNode,
        ast_state:ASTState,
        one_branch:NodeList
    ) -> tuple[bool, list[ASTINode]]:
        raise NotImplementedError
    
class IfShrink(IfShrinkBase):
    def try_one_branch(self, target_node, ast_state, one_branch)->tuple[bool, list[ASTINode]]:
        for insts in update_insts_in_block_v2(one_branch, target_node, ast_state):
            if self.expected_end_time is not None and time.time() > self.expected_end_time:
                return False, []
            
            insts_copy = insts.copy()
            insts_copy.insert(0, InstFactory.opcode_inst('drop'))
            result, new_nodes = _get_new_node_list_from_insts_and_replace_old_node_list(
                insts=insts_copy,
                expected_type=target_node.get_type_req().ty0,
                node_rewriter=self.node_rewriter,
                target_node=target_node,
                ast_state=ast_state,
                onlyless_inst=False
            )
            if result:
                return True, new_nodes
        
        if self.expected_end_time is not None and time.time() > self.expected_end_time:
            return False, []
        
        insts = try_update_if_with_shorter_result_type(target_node)
        # assert 0, print('new insts are {}'.format(insts))
        if insts is not None:
            result, new_nodes = _get_new_node_list_from_insts_and_replace_old_node_list(
                insts=insts,
                expected_type=target_node.get_type_req().ty0,
                node_rewriter=self.node_rewriter,
                target_node=target_node,
                ast_state=ast_state,
                onlyless_inst=False,
                # check_invalid_and_return_false=True
            )
            if result:
                # assert 0, print('new insts are {}'.format(insts))
                return True, new_nodes
        # process empty else
        if target_node.has_else and target_node.else_sub_node_list.get_length() == 0:
            # target_node.has_else = False
            # insts = target_node.get_insts()
            block_type = target_node.get_block_type()
            insts = [
                InstFactory.gen_binary_info_inst_high_single_imm(
                'if', Blocktype(block_type)),
                *target_node.if_sub_node_list.get_insts(),
                InstFactory.opcode_inst('end')
            ]
            result, new_nodes = _get_new_node_list_from_insts_and_replace_old_node_list(
                insts=insts,
                expected_type=target_node.get_type_req().ty0,
                node_rewriter=self.node_rewriter,
                target_node=target_node,
                ast_state=ast_state,
                onlyless_inst=False,
            )
            if result:
                return True, new_nodes
        return False, []

# def get_parent_nod

def update_insts_in_block(
    target_node:NodeList
)  -> Generator[list[Inst], Any, None]:
    inner_insts = target_node.get_insts()
    cur_depth = 0
    # candi_sub_blocks = []
    new_insts:list[Inst] = []
    for inst_idx, inst in enumerate(inner_insts):
        if inst.opcode_text == 'loop':
            cur_depth += 1
        elif inst.opcode_text == 'block':
            cur_depth += 1
        elif inst.opcode_text == 'if':
            cur_depth += 1
        elif inst.opcode_text == 'end':
            cur_depth -= 1
        if inst.opcode_text == 'br' or inst.opcode_text == 'br_if':
            cur_imm = inst.imm_part.val
            if cur_imm == cur_depth:
                if inst.opcode_text == 'br':
                    yield new_insts
                else:
                    new_insts.append(InstFactory.opcode_inst('drop'))
                    yield new_insts
            elif cur_imm > cur_depth:
                new_target = InstFactory.gen_binary_info_inst_high_single_imm(inst.opcode_text, cur_imm - 1)
                new_insts.append(new_target)
            else:
                new_insts.append(inst)
        elif inst.opcode_text == 'br_table':
            imm_dict = inst.imm_part.data
            label_idxs = imm_dict['l']
            default_label = imm_dict['l_N']
            if (label_idxs is not None and cur_depth in label_idxs) or cur_depth == default_label:
                early_return_inst_idx = inst_idx
                new_insts.append(InstFactory.opcode_inst('drop'))
                yield new_insts
            else:
                if label_idxs is not None:
                    new_lable_idxs = [d-1  if d > cur_depth else d for d in label_idxs]
                else:
                    new_lable_idxs = None
                if default_label > cur_depth:
                    new_default_label = default_label - 1
                else:
                    new_default_label = default_label
                imm_dict = {'l': new_lable_idxs, 'l_N': new_default_label}
                new_inst = InstFactory.gen_binary_info_inst_high(inst.opcode_text, imm_dict)
                new_insts.append(new_inst)
        else:
            new_insts.append(inst)
    for _ in range(cur_depth):
        new_insts.append(InstFactory.opcode_inst('end'))
    yield new_insts

def _get_new_node_list_from_insts_and_replace_old_node_list(
    insts:list[Inst],
    expected_type:funcType,
    node_rewriter:NodeRewriter,
    target_node:ASTINode,
    ast_state:ASTState,
    onlyless_inst:bool
) -> tuple[bool, list[ASTINode]]:
    new_node: NodeList = insts2AST(insts, 
                             expected_type,
                             target_node.loc.func_idx,
                             ast_state.parser.types
                             )

    try_result = node_rewriter.try_replace_nodes(
        ast_state,
        [target_node],
        new_node.sub_nodes,
        onlyless_inst=onlyless_inst,
        check_invalid_and_return_false=True
    )
    if try_result:
        new_nodes = new_node.sub_nodes
    else:
        new_nodes = []
    return try_result, new_nodes

