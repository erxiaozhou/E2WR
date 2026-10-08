from time import time

from extract_block_mutator.Context import Context
from extract_block_mutator.tool import get_func_n_context_from_wasm_parser
from extract_block_mutator.wasmFunc import wasmFunc
from extract_block_mutator.WasmParser import WasmParser
from extract_block_mutator.InstUtil.Inst import Inst
from reduction_analysis.ASTInfo.ASTInfo import ASTInfo
from .StackState import StackState
from .StackState import get_stack_state_from_type_req
from .ASTInfo.AST import ASTINode, ASTNodeLoc, InstsNode, NodeList, RootNode, BlockNode, LoopNode, IfNode, traverse_ast
from extract_block_mutator.InstUtil.Inst import Inst
from extract_block_mutator.Context import generate_context_by_out_layers_reuse_data
from typing import Optional
from extract_block_mutator.Context import Context
from extract_block_mutator.InstUtil.InstReqUtil import get_inst_ty_req
from extract_block_mutator.WasmParser import WasmParser
from extract_block_mutator.funcTypeFactory import funcTypeFactory
from extract_block_mutator.typeReq import  merge_req, typeReq

def _get_cur_context_by_ast_info(
    parser: WasmParser,
    ast_info: ASTInfo,
    probe_loc: ASTNodeLoc,
    known_innermost_node:Optional[ASTINode]=None
):
    inst_idx = probe_loc.inst_idx
    # 
    # func_insts = parser.defined_funcs[probe_loc.func_idx].insts
    # func_insts_opcodes = [inst.opcode_text for inst in func_insts]
    # print('all insts in func:', func_insts_opcodes)
    # print('insts surrounding probe_loc:', func_insts_opcodes[max(0, inst_idx-5):inst_idx+5])
    # 

    def _covers_by_range(node: ASTINode) -> bool:
        start = node.loc.inst_idx
        if start > inst_idx:
            return False
        length = node.get_length()
        if length <= 0:
            return False
        end = start + length - 1
        if end < inst_idx:
            return False
        return True

    def locate_innermost() -> ASTINode:
        func_root = ast_info.get_func_root_ast(probe_loc.func_idx)
        candidates: list[tuple[int, ASTINode]] = []

        def visitor(n: ASTINode):
            if covers_node(n):
                depth = 0
                p = n
                while p is not None:
                    depth += 1
                    p = p.parent
                candidates.append((-depth, n))

        traverse_ast(func_root, visitor, pre_order=True, post_order=False)
        if not candidates:
            raise ValueError(f'No AST node covers inst_idx {inst_idx}')
        candidates.sort()
        return candidates[0][1]

    def inside_block_body(block: BlockNode) -> bool:
        body_len = block.sub_node_list.get_length()
        if body_len == 0:
            return False
        start = block.loc.inst_idx + 1
        end = start + body_len - 1
        return start <= inst_idx <= end

    def inside_loop_body(loop: LoopNode) -> bool:
        body_len = loop.sub_node_list.get_length()
        if body_len == 0:
            return False
        start = loop.loc.inst_idx + 1
        end = start + body_len - 1
        return start <= inst_idx <= end

    def inside_if_branch(if_node: IfNode) -> bool:
        if_len = if_node.if_sub_node_list.get_length()
        if if_len > 0:
            if_start = if_node.loc.inst_idx + 1
            if_end = if_start + if_len - 1
            if if_start <= inst_idx <= if_end:
                return True
        if if_node.has_else:
            else_len = if_node.else_sub_node_list.get_length()
            if else_len > 0:
                else_start = if_node.loc.inst_idx + if_len + 2
                else_end = else_start + else_len - 1
                if else_start <= inst_idx <= else_end:
                    return True
        return False

    def covers_node(node: ASTINode) -> bool:
        if isinstance(node, BlockNode):
            return inside_block_body(node)
        if isinstance(node, LoopNode):
            return inside_loop_body(node)
        if isinstance(node, IfNode):
            return inside_if_branch(node)
        return _covers_by_range(node)

    func_context = get_func_n_context_from_wasm_parser(parser, probe_loc.func_idx)
    # innermost = locate_innermost()
    if known_innermost_node is not None:
        innermost = known_innermost_node
    else:
        innermost = locate_innermost()

    outer_layers: list[list[str]] = []
    node_ptr = innermost
    while node_ptr is not None:
        if isinstance(node_ptr, BlockNode):
            outer_layers.append(node_ptr.get_block_type().result_types)
        elif isinstance(node_ptr, LoopNode):
            outer_layers.append(node_ptr.get_block_type().param_types)
        elif isinstance(node_ptr, IfNode):
            outer_layers.append(node_ptr.get_block_type().result_types)
        node_ptr = node_ptr.parent

    return generate_context_by_out_layers_reuse_data(func_context, outer_layers)


def infer_stack_types_on_probe_loc(
    probe_loc: ASTNodeLoc,
    parser: WasmParser,
    ast_info: ASTInfo,
    known_innermost_node:Optional[ASTINode]=None
) -> StackState:
    context, nodes, last_block_param, rest_insts = get_structure_before_probe_loc(
        probe_loc, parser, ast_info, known_innermost_node)
    type_req = infer_stack_types_using_before_loc_structure(
        context=context, nodes=nodes,
        rest_insts=rest_insts,
        last_block_param=last_block_param,
        
        )
    return get_stack_state_from_type_req(type_req)

def infer_stack_types_using_before_loc_structure(
    context:Context,
    nodes:list[ASTINode],
    last_block_param:list[str],
    rest_insts:list[Inst],
    parent_node_list:Optional[NodeList] = None,
    known_innermost_node:Optional[ASTINode]=None
)->typeReq:
    base_req = infer_stack_req_before_insts(context, nodes, last_block_param)
    ori_base_req = base_req
    insts_reqs = []
    for inst in rest_insts:
        type_req = get_inst_ty_req(inst, context)
        insts_reqs.append(type_req)
        new_req = merge_req(base_req, type_req)
        base_req = new_req
    
   
    assert not base_req.impossible(), print('type_rbase_req, rest_type_reqeqs_to_merge', base_req, '\nbase_req', ori_base_req, '\n')
    if len(base_req.tys) == 1:
        return base_req
    else:
        # return base_req
        cur_node_idx = len(nodes)
        insts_node_idx = cur_node_idx
        # 
        if parent_node_list is None:
            if len(nodes) > 0:
                parent = nodes[0].get_parent()
                assert isinstance(parent, NodeList)
                parent_node_list = parent
            else:
                raise ValueError(f'parent_node_list is None: {nodes}')
        assert isinstance(parent_node_list, NodeList)
        # 
        rest_covered_insts_node_num = 0
        rest_inst_num_in_uncovered_node = 0
        rr_num = len(rest_insts)
        for n in parent_node_list.sub_nodes[cur_node_idx:]:
            cur_node_len = n.get_length()
            next_rr_num = rr_num - cur_node_len
            if next_rr_num >= 0:
                rest_covered_insts_node_num += 1
                rr_num = next_rr_num
            else:
                rest_inst_num_in_uncovered_node = rr_num
                break
        # 
        covered_insts_node = parent_node_list.sub_nodes[insts_node_idx+ rest_covered_insts_node_num]
        
        # parent_node_list
        assert covered_insts_node.get_parent() == parent_node_list
        pass_check_type_candis = []
        parent_node_type = parent_node_list.get_block_type()
        for type_candi in base_req.tys:
            has_failed = False
            base_under_check = typeReq(tys=[type_candi], req_type=base_req.req_type)
            for inst in covered_insts_node.get_insts()[rest_inst_num_in_uncovered_node:]:
                inst_type_req = get_inst_ty_req(inst, context)
                base_under_check = merge_req(base_under_check, inst_type_req)
                if len(base_under_check.tys) == 0:
                    has_failed = True
                    break
            if has_failed:
                continue
            for rest_node in parent_node_list.sub_nodes[insts_node_idx+rest_covered_insts_node_num+1:]:
                if isinstance(rest_node, InstsNode):
                    for inst in rest_node.get_insts():
                        inst_type_req = get_inst_ty_req(inst, context)
                        base_under_check = merge_req(base_under_check, inst_type_req)
                        if len(base_under_check.tys) == 0:
                            has_failed = True
                            break
                    if  has_failed:
                        break
                else:
                    type_req = rest_node.get_type_req(context)
                    base_under_check = merge_req(base_under_check, type_req)
                    if len(base_under_check.tys) == 0:
                        has_failed = True
                        break
            if has_failed:
                continue
            final_base_req = base_under_check
            if not has_failed:
                all_possible_result_types = [ty.result_types for ty in final_base_req.tys]
                min_len = min([len(ty) for ty in all_possible_result_types])
                min_len = min(min_len, len(parent_node_type.result_types))
                all_matched = True
                for possible_result_type in all_possible_result_types:
                    if possible_result_type[len(possible_result_type)-min_len:] != parent_node_type.result_types[len(parent_node_type.result_types)-min_len:]:
                        all_matched = False
                        break
                if all_matched:
                    pass_check_type_candis.append(type_candi)
        assert len(pass_check_type_candis) > 0, f'pass_check_type_candis is empty: {base_req}'
        base_req = typeReq(tys=pass_check_type_candis, req_type=base_req.req_type)
        return base_req

def infer_stack_req_before_insts(
    context:Context,
    nodes:list[ASTINode],
    last_block_param:list[str]
)->typeReq:
    inner_block_stack_types = last_block_param
    base_req =typeReq.from_one_ty(
        funcTypeFactory.generate_one_func_type_default([], inner_block_stack_types))
    for n in nodes:
        if isinstance(n, InstsNode):
            for inst in n.get_insts():
                cur_type_req = get_inst_ty_req(inst, context)
                new_req = merge_req(base_req, cur_type_req)
                base_req = new_req
        else:
            cur_type_req = n.get_type_req(context)
            new_req = merge_req(base_req, cur_type_req)
            base_req = new_req
        assert not base_req.impossible(), print('XXXXX base_req', base_req, n.get_node_info())
    return base_req


def get_structure_before_probe_loc(
    probe_loc: ASTNodeLoc,
    parser: WasmParser,
    ast_info: ASTInfo,
    known_innermost_node:Optional[ASTINode]=None
) -> tuple[Context, list[ASTINode], list[str], list[Inst]]:
    func: wasmFunc = parser.defined_funcs[probe_loc.func_idx]
    insts = func.insts
    inst_idx = probe_loc.inst_idx
    rest_insts_after_last_layer = []
    t0 = time()
    cur_context =  _get_cur_context_by_ast_info(
        parser, ast_info, probe_loc, known_innermost_node)
    t1 = time()
   
    # 
    inst_idx_pt = get_idx_pt(insts, inst_idx, rest_insts_after_last_layer)
    
    if inst_idx_pt == len(parser.defined_funcs[probe_loc.func_idx].insts):
        return cur_context, [ast_info.get_func_root_ast(probe_loc.func_idx)], [], []
    last_inst = insts[inst_idx_pt]
    rest_insts_after_last_layer = rest_insts_after_last_layer[::-1]
    cur_node_start_loc = ASTNodeLoc(probe_loc.func_idx, inst_idx_pt)
    #
    cur_nodes = ast_info.get_not_empty_nodes_by_pos(cur_node_start_loc)
    cur_nodes = [n for n in cur_nodes if n.get_length()]
    if len(cur_nodes) == 0:
        assert last_inst.opcode_text in {'end', 'else'} #and insts[inst_idx_pt-1].opcode_text in {'end', 'else'}
        tail_loc = ASTNodeLoc(probe_loc.func_idx, inst_idx_pt)
        if last_inst.opcode_text == 'else':
            tail_loc = ASTNodeLoc(probe_loc.func_idx, inst_idx_pt-1)
        cur_nodes = ast_info.get_node_by_tail_loc(tail_loc)
        cur_nodes = [n for n in cur_nodes if not isinstance(n, NodeList)]
        # assert len(cur_nodes) == 1, print('cur_nodes', cur_nodes, [x.get_node_info() for x in cur_nodes])
        cur_nodes = [n for n in cur_nodes if n.get_length() > 0]
        if len(cur_nodes) == 0:
            return cur_context, [], [], []
        else:
            assert len(cur_nodes) == 1, print('cur_nodes', cur_nodes, [x.get_node_info() for x in cur_nodes])
            cur_node = cur_nodes[0]
            last_block_param = cur_node.get_block_type().param_types
            nodes_before_probe_loc = cur_node.get_sub_nodes()
            rest_insts_after_last_layer = []
            return cur_context, nodes_before_probe_loc, last_block_param, rest_insts_after_last_layer
        
    elif len(cur_nodes) == 1:
        cur_node = cur_nodes[0]
        parent_nodes = ast_info.get_parent_node(cur_node)
        assert len(parent_nodes) == 1, print('parent_nodes', parent_nodes)
        parent_node = parent_nodes[0]
        if isinstance(parent_node, RootNode):
            last_block_param = []
        else:
            last_block_param = parent_node.get_block_type().param_types
        # print('probe_loc', probe_loc, 'parent_node', parent_node.get_node_info())
        if _is_node_list_tail(probe_loc, parent_node):
            return cur_context, [parent_node], [], []
    else:
        # print('COVERED BY TWO')
        # remove the NodeList node
        if isinstance(cur_nodes[0], NodeList):
            cur_node = cur_nodes[1]
            node_list_node = cur_nodes[0]
            assert not isinstance(cur_node, NodeList)
        else:
            assert isinstance(cur_nodes[1], NodeList)
            cur_node = cur_nodes[0]
            node_list_node = cur_nodes[1]
        if _is_node_list_tail(probe_loc, node_list_node):
            return cur_context, [node_list_node], node_list_node.get_block_type().param_types, []
        # print('ast_info.get_parent_node(node_list_node)', ast_info.get_parent_node(
            # node_list_node), 'node_list_node', node_list_node, 'cur inst', insts[inst_idx])
        most_inner_nodes = ast_info.get_parent_node(node_list_node)
        if len(most_inner_nodes) == 0:
            assert isinstance(node_list_node, RootNode) 
            most_inner_node = node_list_node
            # if probe_loc.inst_idx == most_inner_node
        else:
            assert len(most_inner_nodes) == 1, print(
                'most_inner_nodes', most_inner_nodes)
            most_inner_node = most_inner_nodes[0]
        last_block_param = most_inner_node.get_block_type().param_types
    #
    cur_node_loc = cur_node.loc
    node_list_covering_probe_loc = ast_info.get_parent_node_list_by_pos(
        cur_node_loc)
    if node_list_covering_probe_loc is None:
        last_opcode = last_inst.opcode_text
        assert last_opcode == 'block' \
            or last_opcode == 'loop' \
            or last_opcode == 'if' \
            or last_opcode == 'else'
        #
        nodes_before_probe_loc = []
        assert not rest_insts_after_last_layer
    else:
        # assert node_list_covering_probe_loc is not None,print(probe_loc, cur_node_loc)
        nodes_before_probe_loc = []
        for n in node_list_covering_probe_loc.get_sub_nodes():
            n_inst_idx = n.loc.inst_idx
            cur_node_start_idx = cur_node_loc.inst_idx
            if n_inst_idx < cur_node_start_idx:
                assert not isinstance(n, NodeList)
                nodes_before_probe_loc.append(n)
    return cur_context, nodes_before_probe_loc, last_block_param, rest_insts_after_last_layer

def get_idx_pt(insts, inst_idx, rest_insts_after_last_layer):
    inst_idx_pt = inst_idx
    while True:
        last_idx = inst_idx_pt - 1
        if last_idx == -1:
            break
        last_inst = insts[last_idx]

        op_code = last_inst.opcode_text
        if op_code == 'block' \
                or op_code == 'loop' \
                or op_code == 'if' \
                or op_code == 'end' \
                or op_code == 'else':
            break
        else:
            rest_insts_after_last_layer.append(last_inst)
            inst_idx_pt = last_idx
    # if len(insts) == inst_idx_pt:
    #     inst_idx_pt -= 1
    return inst_idx_pt
    


def _is_node_list_tail(probe_loc:ASTNodeLoc, cur_node:ASTINode)->bool:
    if isinstance(cur_node, NodeList):
        if probe_loc.inst_idx == cur_node.loc.inst_idx+ cur_node.get_length():
            return True
    return False

