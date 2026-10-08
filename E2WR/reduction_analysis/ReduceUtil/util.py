from enum import Enum
from reduction_analysis.InferStackUtil import infer_stack_req_before_insts
from typing import Optional

from extract_block_mutator.Context import Context
from extract_block_mutator.InstUtil.InstReqUtil import get_inst_ty_req
from extract_block_mutator.WasmParser import WasmParser, get_parser_from_wasm_path
from extract_block_mutator.funcType import funcType
from extract_block_mutator.funcTypeFactory import funcTypeFactory
from extract_block_mutator.typeReq import merge_req, typeReq
from ..ASTState import ASTState
from reduction_analysis.InferStackUtil import get_structure_before_probe_loc
from ..ASTInfo.AST import ASTINode, InstsNode, NodeList, RootNode
from ..InstsScope import InstsScope
from extract_block_mutator.InstUtil import Inst
from ..ASTInfo.ASTInfo import ASTInfo
from ..ASTInfo.AST import ASTNodeLoc

from ..StackState import StackState, StackStatus, get_stack_state_from_type_req

def get_node_type_req(ast_state, node:ASTINode):
    try:
        expected_type_req = node.get_type_req()
    except:
        assert isinstance(node, InstsNode)
        expected_type_req = infer_node_scope_type_req(
            ast_state.parser,
            ast_state.ast_info,
            get_node_scope(node),
            node
        )
    return expected_type_req

def get_node_scope(node:ASTINode):
    func_idx = node.func_idx
    start_idx = node.inst_idx
    end_idx = start_idx + node.get_length()
    return InstsScope(func_idx, start_idx, end_idx)

def check_parser_match_wasm_file(parser:WasmParser, wasm_file:str)->bool:
    input_parser = get_parser_from_wasm_path(wasm_file)
    for cur_parser_func, input_parser_func in zip(parser.defined_funcs, input_parser.defined_funcs):
        assert len(cur_parser_func.insts) == len(input_parser_func.insts)
        for cur_inst, input_inst in zip(cur_parser_func.insts, input_parser_func.insts):
            assert cur_inst.opcode_text == input_inst.opcode_text
    return True



def remove_empty_node_in_nodelist(node_list:NodeList)->None:
    to_remove = []
    for n in node_list.sub_nodes:
        if n.get_length() == 0:
            to_remove.append(n)
    
    for n in to_remove:
        node_list.sub_nodes.remove(n)


def infer_stack_types_using_before_loc_structure(
    context:Context,
    nodes:list[ASTINode],
    last_block_param:list[str],
    rest_insts:list[Inst],
    parent_node_list:Optional[NodeList] = None,
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
                parent_node_list = nodes[0].get_parent()
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
        
def infer_node_scope_type_req(
    parser: WasmParser,
    ast_info: ASTInfo,
    scope: InstsScope,
    node: ASTINode
) -> typeReq:
    if node.get_length() == 0:
        return typeReq(tys=[funcTypeFactory.generate_one_func_type_default([], [])], req_type='eq')
    # if isinstance(node, Rtt):
    # print('infer_node_scope_type_req', node.get_node_info())
    try:
        block_type_req = node.get_type_req()
        block_type = block_type_req.ty0
        print('get type immediately', block_type)
        return node.get_type_req()
    except Exception as e:
        pass
        
    assert not isinstance(node, RootNode)
    assert isinstance(node, InstsNode)
    parent_node = node.parent
    assert isinstance(parent_node, NodeList)
    loc1 = ASTNodeLoc(scope.func_idx, scope.start_idx)
    # loc2 = ASTNodeLoc(scope.func_idx, scope.end_idx)
    # 
    context, nodes, last_block_param, rest_insts = get_structure_before_probe_loc(
        loc1, parser, ast_info, node)
    type_req = infer_stack_types_using_before_loc_structure(
        context=context, nodes=nodes,
        rest_insts=rest_insts,
        last_block_param=last_block_param,
        parent_node_list=parent_node
        )
    # 
    stack_state_at_loc1 = get_stack_state_from_type_req(type_req)
    base_req = stack_state_at_loc1.as_type_req()
    all_candis = [
        typeReq(
            [ty], base_req.req_type
        )
        for ty in base_req.tys
    ]
    # candi_idx2results = {}
    all_types = []
    ref_type = 'eq'
    last_op_is_deterministic_type = False
    for candi_idx, candi in enumerate(all_candis):
        base_req = candi
        mini_depth = 0
        cur_depth = 0
        try_next_candi = False
        insts = node.get_insts()
        introduce_poly_type = False
        for inst in insts:
            inst_req = get_inst_ty_req(inst, context)
            assert inst_req is not None 
            if inst_req.req_type == 'eg_param_and_result' or inst_req.req_type == 'unreachable':
                introduce_poly_type = True
            new_req = merge_req(base_req, inst_req)
            base_req = new_req
            if base_req.impossible():
                try_next_candi = True
                break
            repr_type = inst_req.ty0
            taken_op_num = len(repr_type.param_types)
            cur_depth -= taken_op_num
            if cur_depth < mini_depth:
                mini_depth = cur_depth
            cur_depth += len(repr_type.result_types)
            last_op_is_deterministic_type = inst_req.ty0.determined_return_ty
        if try_next_candi:
            continue
        # candi_idx2results[candi_idx].append(base_req)
        # cur_result_type = base_req.ty0
        cur_state_at_loc1 = get_stack_state_from_type_req(candi)
        cur_state_at_loc2 = get_stack_state_from_type_req(base_req)
        if introduce_poly_type or (cur_state_at_loc2.status == StackStatus.ANY and cur_state_at_loc1.status != StackStatus.ANY):
            ref_type = 'eg_param_and_result'
        else:
            ref_type = 'eq'
        stack_types_at_loc1_list = cur_state_at_loc1.all_rest_types
        stack_types_at_loc2_list = cur_state_at_loc2.all_rest_types
        common_stack_num = 0
        for stack_types_at_loc1 in stack_types_at_loc1_list:
            for stack_types_at_loc2 in stack_types_at_loc2_list:
                for ty1, ty2 in zip(stack_types_at_loc1, stack_types_at_loc2):
                    if ty1 == ty2:
                        common_stack_num += 1
                    else:
                        break
                if stack_state_at_loc1.status == StackStatus.ANY :
                    actual_common_stack_num = 0
                else:
                    actual_common_stack_num = min(common_stack_num,  len(stack_types_at_loc1)+mini_depth)
                # actual_common_stack_num = min(common_stack_num,  len(stack_types_at_loc1)+mini_depth)
                
                to_consume_ops = stack_types_at_loc1[actual_common_stack_num:]
                to_product_ops = stack_types_at_loc2[actual_common_stack_num:]
                if stack_state_at_loc1.status == StackStatus.ANY :
                    if len(to_consume_ops) == 0:
                        ty = funcTypeFactory.generate_one_func_type_default([], to_product_ops, last_op_is_deterministic_type)
                    else:
                        ty = funcTypeFactory.generate_one_func_type_default(to_consume_ops, to_product_ops, last_op_is_deterministic_type)
                else:
                    ty = funcTypeFactory.generate_one_func_type_default(to_consume_ops, to_product_ops, last_op_is_deterministic_type)
                all_types.append(ty)
    return typeReq(all_types, ref_type)


def get_stack_num_diff_from_inst_type(
    inst_type:funcType
):
    taken = len(inst_type.param_types)
    stored = len(inst_type.result_types)
    return taken, stored



def get_try_time(inst_num:int)->int:
    if inst_num <= 3000:
        return 300
    elif inst_num <= 20000:
        return 900
    else:
        return 1800
