import time
from extract_block_mutator.Context import Context
from extract_block_mutator.InstUtil.ByteInst import ByteImmInst
from extract_block_mutator.InstUtil.InstReqUtil import get_insts_ty_req
from extract_block_mutator.funcType import funcType
from extract_block_mutator.encode.new_defined_data_type import Blocktype
from extract_block_mutator.funcTypeFactory import funcTypeFactory
from extract_block_mutator.InstGeneration.InstFactory import InstFactory
from extract_block_mutator.InstUtil.Inst import Inst
from typing import Optional
from abc import ABC, abstractmethod
from extract_block_mutator.typeReq import merge_req, typeReq
from extract_block_mutator.wasmFunc import wasmFunc
from enum import Enum



class NodeType(Enum):
    INSTS = 'insts'
    BLOCK = 'block'
    LOOP = 'loop'
    IF = 'if'
    ELSE = 'else'
    ROOT = 'root'
    NODE_LIST = 'node_list'

class ASTNodeLoc:
    def __init__(self, func_idx:int, inst_idx:int):
        self.func_idx = func_idx
        self.inst_idx = inst_idx

    def __str__(self):
        return f"[{self.func_idx} - {self.inst_idx}]"

    def __repr__(self):
        return f"{self.__class__.__name__}(func_idx={self.func_idx}, inst_idx={self.inst_idx})"

    def __eq__(self, other):
        return self.func_idx == other.func_idx and self.inst_idx == other.inst_idx
    
    def __hash__(self):
        return hash((self.func_idx, self.inst_idx))

class ASTNode(ABC):pass

class FuncDescNode(ASTNode): pass

class AllFuncsNode(ASTNode): pass 
    


class ASTINode(ASTNode):
    _next_id = 0
    
    def __init__(self, loc:ASTNodeLoc):
        self.loc = loc
        self.parent:Optional['ASTINode'] = None
        self.node_id = ASTINode._next_id 
        ASTINode._next_id += 1
        self._original_id = None 

    def has_parent(self)->bool:
        return self.parent is not None
    
    def get_parent(self)->'ASTINode':
        if self.parent is None:
            raise ValueError(f'{self.get_node_info()} has no parent')
        return self.parent
    
    
    @property
    def inst_idx(self):
        return self.loc.inst_idx

    @inst_idx.setter
    def inst_idx(self, inst_idx:int):
        self.loc.inst_idx = inst_idx

    @property
    def func_idx(self):
        return self.loc.func_idx

    @func_idx.setter
    def func_idx(self, func_idx:int):
        self.loc.func_idx = func_idx
    @abstractmethod
    def get_type_req(self, context:Optional[Context]=None)->typeReq:
        pass

    @abstractmethod
    def get_block_type(self)->funcType:
        pass

    @abstractmethod
    def get_sub_nodes(self) -> list['ASTINode']:
        pass

    @abstractmethod
    def get_insts(self) -> list[Inst]:
        pass
    
    @abstractmethod
    def get_length(self) -> int:
        pass
    

    def is_leaf(self) -> bool:
        return len(self.get_sub_nodes()) == 0

    def get_inst_smy(self) -> str:
        insts = self.get_insts()
        if not insts:
            return 'No instructions'
        if len(insts) == 1:
            return insts[0].opcode_text
        else:
            op0 = insts[0].opcode_text
            op1 = insts[-1].opcode_text
            return f"[{op0} ... {op1}]"

    def get_node_info(self) -> str:
        node_type = self.__class__.__name__
        length = self.get_length()
        inst_info = self.get_inst_smy()
        return f"{node_type} {self.loc} len:{length} {inst_info} ID:{self.node_id}"

class InstsNode(ASTINode):
    def __init__(self, insts: list[Inst], func_idx:int, inst_idx:int):
        self.insts = insts
        super().__init__(loc=ASTNodeLoc(func_idx=func_idx, inst_idx=inst_idx))

    def get_type_req(self, context:Optional[Context]=None) -> typeReq:
        raise ValueError('Though it is possible, it should not be called')
        if context is None:
            raise ValueError('context is None')
        ty_req = get_insts_ty_req(self.insts, context)
        # if len(self.insts) <= 1:
        #     print(f'The insts to infer type requirement is :{self.insts}. The node : {self.get_node_info()} ty_req is {ty_req}')
        return ty_req

    def get_block_type(self)->funcType:
        raise NotImplementedError()

    def get_sub_nodes(self):
        return []

    def get_insts(self) -> list[Inst]:
        return self.insts
    
    def get_length(self) -> int:
        return len(self.insts)

    def __str__(self):
        return f'{self.__class__.__name__}(loc={self.loc}, length={self.get_length()})'
        
    def __bool__(self):
        return bool(self.insts)

class InstsNodeWithType(InstsNode):
    def __init__(self, insts: list[Inst], func_idx:int, inst_idx:int, block_type:Optional[funcType]=None):
        self.block_type = block_type
        super().__init__(insts=insts, func_idx=func_idx, inst_idx=inst_idx)
        
    def get_block_type(self)->funcType:
        if self.block_type is None:
            raise NotImplementedError('block_type is None')
        return self.block_type

    def get_type_req(self, context:Optional[Context]=None):
        return typeReq.from_one_ty(self.get_block_type())

def gen_new_insts_node(
    insts:list[Inst],
    loc:ASTNodeLoc,
    block_type:Optional[funcType]=None
)->InstsNode:
    if block_type is None:
        return InstsNode(insts, loc.func_idx, loc.inst_idx)
    else:
        return InstsNodeWithType(insts, loc.func_idx, loc.inst_idx, block_type)

       
class NodeList(ASTINode):
    def __init__(self, sub_nodes: list[ASTINode], func_idx:int, inst_idx:int, block_type:Optional[funcType]=None):
        # assert block_type is not None
        self.sub_nodes = sub_nodes
        self.block_type = block_type
        super().__init__(loc=ASTNodeLoc(func_idx=func_idx, inst_idx=inst_idx))

    def get_sub_node_lengths(self)->list[int]:
        return [node.get_length() for node in self.sub_nodes]

    def get_type_req(self, context:Optional[Context]=None):
        if self.block_type is not None:
            return typeReq.from_one_ty(self.block_type)
        type_reqs = [node.get_type_req(context) for node in self.sub_nodes]
        # print('&&&&&&&&&&&&&&&&&&&&&&&&&&')
        for node, type_req in zip(self.sub_nodes, type_reqs):
            print(node.get_node_info(), type_req)
        # print(f'type_reqs in NodeList.get_type_req: {type_reqs}')
        if len(type_reqs) == 0:
            return typeReq.from_one_ty(funcTypeFactory.generate_one_func_type_default([], []))
        else:
            base_type_req = type_reqs[0]
            for next_type_req in type_reqs[1:]:
                print(f'base_type_req : {next_type_req}\nnext_type_req:{next_type_req}\nmerged:{merge_req(base_type_req, next_type_req)}')
                base_type_req = merge_req(base_type_req, next_type_req)
            return base_type_req

    def get_block_type(self)->funcType:
        if self.block_type is None:
            raise NotImplementedError('block_type is None')
        return self.block_type

    def get_sub_nodes(self):
        return self.sub_nodes

    def append(self, node: ASTINode):
        self.sub_nodes.append(node)
        node.parent = self

    def replace_split_with_new_sub_nodes(self, start_index, end_index, new_nodes):
     
        if start_index < 0:
            start_index = 0
        if end_index > len(self.sub_nodes):
            end_index = len(self.sub_nodes)
            
        for node in new_nodes:
            if node == self:
                raise ValueError(f"{node} should not be NodeList itself")
                
        new_sub_nodes = self.sub_nodes[:start_index] + new_nodes + self.sub_nodes[end_index:]
        
        for removed_node in self.sub_nodes[start_index:end_index]:
            if removed_node.parent == self:
                removed_node.parent = None
                
        for node in new_nodes:
            # print('!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!! has updated parent')
            node.parent = self
            
        self.sub_nodes = new_sub_nodes

    def replace_sub_node(self, old_node: ASTINode, new_node: ASTINode):
        try:
            index = self.sub_nodes.index(old_node)
            self.replace_split_with_new_sub_nodes(index, index + 1, [new_node])
        except ValueError:
            raise ValueError(f"{old_node} is not in the sub_nodes list")

    def get_node_info(self) -> str:
        node_type = self.__class__.__name__
        length = self.get_length()
        inst_info = self.get_inst_smy()
        return f"{node_type} {self.loc} length:{length} {inst_info} IDX: {self.node_id} SubNodes:{len(self.sub_nodes)}"


    def replace_all_sub_nodes(self, new_sub_nodes: list[ASTINode]):
        self.replace_split_with_new_sub_nodes(0, len(self.sub_nodes), new_sub_nodes)

    def __len__(self):
        return len(self.sub_nodes)
    @property
    def is_empty(self):
        return len(self.sub_nodes) == 0

    def get_insts(self):
        insts = []
        for node in self.sub_nodes:
            insts.extend(node.get_insts())
        return insts

    def get_length(self) -> int:
        sum_ = 0
        node_num = len(self.sub_nodes)
        for node_idx, node in enumerate(self.sub_nodes):
            try:
                node_length = node.get_length()
                
            
            except Exception as e:
                print('Error getting length of node: ', node.get_node_info(), e)
                if node_idx == node_num - 1:
                    node_length = 1
                else:
                    node_length = max(
                        0,
                        self.sub_nodes[node_idx + 1].inst_idx - node.inst_idx
                    )
                # print(f"Error getting length of node {node.get_node_info()}: {e}")
                # raise e
            sum_ += node_length
        return sum_
        try:
            return sum(node.get_length() for node in self.sub_nodes)
        except Exception:
            if len(self.sub_nodes) == 0:
                return 0
            try:
                start_node = self.sub_nodes[0]
                start_idx = start_node.loc.inst_idx
                last_node = self.sub_nodes[-1]
                end_idx = last_node.loc.inst_idx + last_node.get_length()
                return end_idx - start_idx
            except Exception:
                return len(self.sub_nodes)


class BlockNode(ASTINode):
    def __init__(self, sub_nodes: list[ASTINode], block_type:Optional[funcType]=None, func_idx:int=0, inst_idx:int=0):
        self.sub_node_list = NodeList(sub_nodes, func_idx, inst_idx+1, block_type)
        self.sub_node_list.parent = self
        self.block_type = block_type
        super().__init__(loc=ASTNodeLoc(func_idx=func_idx, inst_idx=inst_idx))

    def get_type_req(self, context:Optional[Context]=None):
        block_type = self.get_block_type()
        return typeReq.from_one_ty(block_type)

    def get_block_type(self)->funcType:
        if self.block_type is None:
            raise NotImplementedError('block_type is None')
        return self.block_type

    def get_sub_nodes(self):
        return [self.sub_node_list]

    def get_insts(self) -> list[Inst]:
        block_type = self.block_type
        if block_type is None:
            raise NotImplementedError('block_type is None')
        return [
            InstFactory.gen_binary_info_inst_high_single_imm(
                'block', Blocktype(block_type)),
            *self.sub_node_list.get_insts(),
            InstFactory.opcode_inst('end')
        ]
    
    def set_sub_node_list(self, new_sub_node_list: NodeList):
        self.sub_node_list = new_sub_node_list
        self.sub_node_list.parent = self

    def get_length(self) -> int:
        try:
            return 2 + self.sub_node_list.get_length()
        except Exception:
            return 2
    

class LoopNode(ASTINode):
    def __init__(self, sub_nodes: list[ASTINode], block_type:Optional[funcType]=None, func_idx:int=0, inst_idx:int=0):
        self.sub_node_list = NodeList(sub_nodes, func_idx=func_idx, inst_idx=inst_idx+1, block_type=block_type)
        self.sub_node_list.parent = self
        self.block_type = block_type
        super().__init__(loc=ASTNodeLoc(func_idx=func_idx, inst_idx=inst_idx))

    def set_sub_node_list(self, new_sub_node_list: NodeList):
        self.sub_node_list = new_sub_node_list
        self.sub_node_list.parent = self

    def get_type_req(self, context:Optional[Context]=None):
        block_type = self.get_block_type()
        return typeReq.from_one_ty(block_type)

    def get_block_type(self)->funcType:
        if self.block_type is None:
            raise NotImplementedError('block_type is None')
        return self.block_type
    def get_sub_nodes(self):
        return [self.sub_node_list]

    def get_insts(self) -> list[Inst]:
        block_type = self.block_type
        if block_type is None:
            raise NotImplementedError('block_type is None')
        return [
            InstFactory.gen_binary_info_inst_high_single_imm(
                'loop', Blocktype(block_type)),
            *self.sub_node_list.get_insts(),
            InstFactory.opcode_inst('end')
        ]
    
    
    def get_length(self) -> int:
        return 2 + self.sub_node_list.get_length()
    


class ElseNode(NodeList): pass
class RootNode(NodeList): pass


class IfNode(ASTINode):
    def __init__(self, if_sub_nodes: list[ASTINode], else_sub_nodes: list[ASTINode], block_type:Optional[funcType]=None, func_idx:int=0, inst_idx:int=0):
        self.if_sub_node_list = NodeList(if_sub_nodes, func_idx=func_idx, inst_idx=inst_idx+1, block_type=block_type)
        self._else_sub_node_list = ElseNode(else_sub_nodes, func_idx=func_idx, inst_idx=inst_idx, block_type=block_type)
        self.if_sub_node_list.parent = self
        self._else_sub_node_list.parent = self
        self.block_type = block_type
        self.has_else = False
        super().__init__(loc=ASTNodeLoc(func_idx=func_idx, inst_idx=inst_idx))

    @property
    def else_sub_node_list(self):
        self.has_else = True
        return self._else_sub_node_list

    def set_else_sub_node_list(self, new_else_sub_node_list: ElseNode):
        self._else_sub_node_list = new_else_sub_node_list
        self._else_sub_node_list.parent = self

    def set_if_sub_node_list(self, new_if_sub_node_list: NodeList):
        self.if_sub_node_list = new_if_sub_node_list
        self.if_sub_node_list.parent = self


    def set_else_start_idx(self, inst_idx:int):
        self._else_sub_node_list.inst_idx = inst_idx

    def get_type_req(self, context:Optional[Context]=None):
        if self.block_type is None:
            raise NotImplementedError('block_type is None')
        param_types = self.block_type.param_types + ['i32']
        result_types = self.block_type.result_types
        func_type = funcTypeFactory.generate_one_func_type_default(param_types, result_types)
        return typeReq.from_one_ty(func_type)

    def get_block_type(self)->funcType:
        if self.block_type is None:
            raise NotImplementedError('block_type is None')
        return self.block_type

    def get_sub_nodes(self):
        if self.has_else:
            return [self.if_sub_node_list, self._else_sub_node_list]
        else:
            return [self.if_sub_node_list]

    def get_insts(self) -> list[Inst]:
        block_type = self.block_type
        if block_type is None:
            raise NotImplementedError('block_type is None')
        if not self.has_else:
            return [
            InstFactory.gen_binary_info_inst_high_single_imm(
                'if', Blocktype(block_type)),
            *self.if_sub_node_list.get_insts(),
            InstFactory.opcode_inst('end')
        ]
        else:
            result = [
            InstFactory.gen_binary_info_inst_high_single_imm(
                'if', Blocktype(block_type)),
            *self.if_sub_node_list.get_insts(),
            InstFactory.opcode_inst('else'),
            *self.else_sub_node_list.get_insts(),
            InstFactory.opcode_inst('end')
            ]
            
            return result
    
    
    def get_length(self) -> int:
        if not self.has_else:
            return 2 + self.if_sub_node_list.get_length()
        else:
            return 3 + self.if_sub_node_list.get_length() + self.else_sub_node_list.get_length()



def process_insts_in_frame(
    cur_insts:list[Inst],
    cur_list_node:NodeList,
    cur_inst_start_idx:int,
    func_idx:int,
):
    if len(cur_insts) == 0:
        return
    else:
        insts_node = InstsNode(insts=cur_insts, func_idx=func_idx, inst_idx=cur_inst_start_idx)
        cur_list_node.append(insts_node)




def func2AST(
    func: wasmFunc,
    func_idx:int,
    types: list[funcType],
    root_node_type:type[NodeList]=RootNode
) -> NodeList:
    insts = func.insts
    given_type = funcTypeFactory.generate_one_func_type_default([], func.func_ty.result_types)
    return insts2AST(insts, given_type, func_idx, types, root_node_type)

def insts2AST(
    insts:list[Inst],
    given_type:funcType,
    func_idx:int,
    types: list[funcType],
    root_node_type:type[NodeList]=NodeList
) -> NodeList:
    cur_depth = 0
    block_frame = []
    list_frame = []
    root_block = root_node_type(sub_nodes=[], func_idx=func_idx, inst_idx=0, block_type=given_type)
    cur_list_node = root_block
    cur_insts = []
    cur_inst_start_idx = 0
    
    t0 = time.time()
    for inst_idx, inst in enumerate(insts):
        inst_op = inst.opcode_text

        if inst_op == 'block' or inst_op == 'loop' or inst_op == 'if':
            cur_depth += 1
            process_insts_in_frame(cur_insts, cur_list_node, cur_inst_start_idx, func_idx)
            cur_insts = []
            assert isinstance(inst, ByteImmInst)
            block_type = inst.imm_part.val.concrete_type(types)
            if inst_op == 'block':
                new_block = BlockNode(sub_nodes=[], block_type=block_type, func_idx=func_idx, inst_idx=inst_idx)
                cur_list_node.append(new_block)
                cur_list_node = new_block.sub_node_list
            elif inst_op == 'loop':
                new_block = LoopNode(sub_nodes=[], block_type=block_type, func_idx=func_idx, inst_idx=inst_idx)
                cur_list_node.append(new_block)
                cur_list_node = new_block.sub_node_list
            elif inst_op == 'if':
                new_block = IfNode(if_sub_nodes=[], else_sub_nodes=[], block_type=block_type, func_idx=func_idx, inst_idx=inst_idx)
                cur_list_node.append(new_block)
                cur_list_node = new_block.if_sub_node_list
            block_frame.append(new_block)
            list_frame.append(cur_list_node)
        elif inst_op == 'else':
            process_insts_in_frame(cur_insts, cur_list_node, cur_inst_start_idx, func_idx)
            cur_insts = []
           
            block_ = None
            for i in range(len(block_frame)-1, -1, -1):
                if isinstance(block_frame[i], IfNode) and not block_frame[i].has_else:
                    block_ = block_frame[i]
                    break
                    
            assert block_ is not None, f"inst_idx:{inst_idx}"

            
            assert isinstance(block_, IfNode)
            assert not block_.has_else
            cur_list_node = block_.else_sub_node_list
            block_.set_else_start_idx(inst_idx+1)
            
            for i in range(len(list_frame)-1, -1, -1):
                if list_frame[i] == block_.if_sub_node_list:
                    list_frame[i] = cur_list_node
                    break
        elif inst_op == 'end':
            process_insts_in_frame(cur_insts, cur_list_node, cur_inst_start_idx, func_idx)
            cur_insts = []
            block_frame.pop()
            list_frame.pop()
            cur_depth -= 1
            
            if list_frame:
                cur_list_node = list_frame[-1]
            else:
                cur_list_node = root_block
        elif inst_op in {'br', 'br_if', 'br_table', 'return', 'unreachable'}:
            if not cur_insts:
                cur_inst_start_idx = inst_idx
            cur_insts.append(inst)
            process_insts_in_frame(cur_insts, cur_list_node, cur_inst_start_idx, func_idx)
            cur_insts = []
           
        else:
            if not cur_insts:
                cur_inst_start_idx = inst_idx
            cur_insts.append(inst)
            
    process_insts_in_frame(cur_insts, cur_list_node, cur_inst_start_idx, func_idx)
    

    def convert_to_insts_node_with_type(node):
        if isinstance(node, NodeList) and len(node.sub_nodes) == 1:
            if isinstance(node.sub_nodes[0], InstsNode) and not isinstance(node.sub_nodes[0], InstsNodeWithType):
                insts_node = node.sub_nodes[0]
                new_node = InstsNodeWithType(
                    insts=insts_node.insts,
                    func_idx=insts_node.func_idx,
                    inst_idx=insts_node.inst_idx,
                    block_type=node.block_type
                )
                node.sub_nodes[0] = new_node
                new_node.parent = node
    traverse_ast(root_block, convert_to_insts_node_with_type)
    return root_block

def get_all_nodes_as_a_list(root_node: ASTINode)->list[ASTINode]:
    nodes = []
    def collect_nodes(node):
        nodes.append(node)
    traverse_ast(root_node, collect_nodes, pre_order=True, post_order=False, collect_results=True)
    assert nodes is not None
    return nodes

def traverse_ast(root_node: ASTINode, visitor_func, pre_order=True, post_order=False, collect_results=False):
    results = [] if collect_results else None
    
    def _traverse(node):
        # Pre-order traversal: process node before children
        if pre_order:
            result = visitor_func(node)
            if collect_results and result is not None and results is not None:
                results.append(result)
        
        # Process children
        for child in node.get_sub_nodes():
            _traverse(child)
        
        # Post-order traversal: process node after children
        if post_order:
            result = visitor_func(node)
            if collect_results and result is not None and results is not None:
                results.append(result)
    
    _traverse(root_node)
    return results if collect_results else None

def find_nodes_by_type(root_node: ASTINode, node_type):
    nodes = []
    
    def collect_matching_nodes(node):
        if isinstance(node, node_type):
            nodes.append(node)
    
    traverse_ast(root_node, collect_matching_nodes)
    return nodes

def find_nodes_by_predicate(root_node: ASTINode, predicate_func):
    nodes = []
    
    def collect_matching_nodes(node):
        if predicate_func(node):
            nodes.append(node)
    
    traverse_ast(root_node, collect_matching_nodes)
    return nodes

def count_instructions(root_node: ASTINode):
   
    total_count = 0
    
    def count_node_insts(node):
        nonlocal total_count
        insts = node.get_insts()
        total_count += len(insts)
    
    traverse_ast(root_node, count_node_insts, pre_order=True, post_order=False)
    return total_count

def print_ast_structure(root_node: ASTINode, indent=""):
 
    print(f"{indent}{root_node.__class__.__name__}")
    
    for child in root_node.get_sub_nodes():
        print_ast_structure(child, indent + "  ")

def locate_node_contain_inst_pos(root_node: ASTINode, inst_pos: int) -> ASTINode:
   
    matching_nodes = []
    
    def find_matching_nodes(node, depth=0):
        start_idx = node.inst_idx
        length = node.get_length()
        end_idx = start_idx + length - 1
        
        if start_idx <= inst_pos <= end_idx:
            matching_nodes.append((node, depth, length))
            
            for child in node.get_sub_nodes():
                find_matching_nodes(child, depth + 1)
    
    find_matching_nodes(root_node)
    
    if matching_nodes:
        matching_nodes.sort(key=lambda x: (-x[1], x[2]))
        return matching_nodes[0][0]
    
    return root_node


def is_ancestor_of(ancestor:ASTINode, descendant:ASTINode)->bool:

    if ancestor == descendant:
        return True
    has_found = False
    for child in ancestor.get_sub_nodes():
        if child == ancestor:
            raise ValueError(f'{ancestor} is ancestor of itself :{ancestor} {[n for n in ancestor.get_sub_nodes()]}')
        if is_ancestor_of(child, descendant):
            has_found = True
            break
    return has_found

def get_node_list_type_in_ast_practical(node_list:NodeList)->funcType:
    if isinstance(node_list, RootNode):
        node_type = node_list.get_block_type()
    else:
        assert node_list.parent is not None
        node_type = node_list.parent.get_block_type()
    return node_type
