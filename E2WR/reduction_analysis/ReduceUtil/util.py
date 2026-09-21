from enum import Enum
from reduction_analysis.InferStackUtil import infer_stack_req_before_insts
import time
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
class FailToAnalyzeTypeReq(Exception):
    pass

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

def is_const_or_drop_inst(inst:Inst)->bool:
    return is_const_inst(inst) or is_drop_inst(inst)

def is_drop_inst(inst:Inst)->bool:
    opcode = inst.opcode_text
    if opcode == 'drop':
        return True
    else:
        return False

def is_const_inst(inst:Inst)->bool:
    opcode = inst.opcode_text
    if opcode == 'i32.const' \
        or opcode == 'i64.const' \
        or opcode == 'f32.const' \
        or opcode == 'f64.const' \
        or opcode == 'ref.null' \
        or opcode == 'v128.const':
        return True
    else:
        return False



def is_all_const_or_all_drop_insts_node(node:ASTINode):
    if isinstance(node,InstsNode):
        insts = node.insts
        is_all_const = True
        is_all_drop = True
        for inst in insts:
            if not is_const_inst(inst):
                is_all_const = False
            if not is_drop_inst(inst):
                is_all_drop = False
        result =  is_all_const or is_all_drop
    else:
        result = False
    return result

def is_all_const_or_drop_insts_node(node:ASTINode):
    if isinstance(node,InstsNode):
        insts = node.insts
        for inst in insts:
            if not is_const_or_drop_inst(inst):
                return False
        return True
    else:
        return False



def get_each_loc_type(
    insts:list[Inst],
    cur_status:StackState,
    context:Context,
    node_type:Optional[funcType] = None
)->tuple[list[funcType], funcType, int, list[typeReq]]:
    inst_types:list[funcType] = []
    input_req = cur_status.as_type_req()
    base_req = input_req
    min_stack_depth = 0
    can_drop_inst_num = 0
    type_reqs = []
    type_req_before_each_inst = []
    type_req_before_each_inst.append(input_req)
    # 
    # 
    for inst_idx, inst in enumerate(insts):
        type_req = get_inst_ty_req(inst, context)
        if type_req is None:
            raise FailToAnalyzeTypeReq(f"Fail to analyze type req for inst: {inst}")

        cur_base_req = merge_req(base_req, type_req)
        assert len(cur_base_req.tys) > 0
        # if len(cur_base_req.tys) == 1:
        #     actual_type = cur_base_req.tys[0]
        type_reqs.append(type_req)
        type_req_before_each_inst.append(cur_base_req)
        if type_req.req_type == 'eq':
            if len(type_req.tys) == 1:
                cur_inst_type = type_req.tys[0]
            else:
                type1 = _get_stack_type_from_type_req(base_req)
                type2 = _get_stack_type_from_type_req(cur_base_req)
                cur_inst_type =_get_inst_type_by_diff_type_diff(type1, type2, 0)
                
        else:
            cur_inst_type = type_req.ty0
            type1 = _get_stack_type_from_type_req(base_req)
            mini_required_type_num = len(cur_inst_type.param_types)
            assert inst_idx == len(insts) - 1, f'{inst_idx} != {len(insts) - 1}'
            
        inst_types.append(cur_inst_type)
        base_req = cur_base_req

    if len(type_req_before_each_inst[-1].tys)> 1:
        if node_type is not None:
            expected_last_state = merge_req(input_req, typeReq(tys=[node_type], req_type='eq'))
            result_types = expected_last_state.ty0.result_types
            matched_candi = None
            for candi in type_req_before_each_inst[-1].tys:
                candi_result_types = candi.result_types
                min_len = min(len(candi_result_types), len(result_types))
                if candi_result_types[len(candi_result_types)-min_len:] == result_types[len(result_types)-min_len:]:
                    matched_candi = candi
                    break
            # assert matched_candi is not None, f'matched_candi is None: {type_req_before_each_inst[-1]}'
            if matched_candi is not None:
                type_req_before_each_inst[-1] = typeReq(tys=[matched_candi], req_type=type_req_before_each_inst[-1].req_type)
            else:
                pass
        
    # 
    if (any(len(req.tys) > 1 for req in type_reqs)):
        updated_type_idxs = refine_type_reqs(
        node_type_reqs=type_reqs,  
        state_type_reqs=type_req_before_each_inst 
        )
        for idx in updated_type_idxs:
            inst_types[idx] = type_reqs[idx].ty0
    

    # 
    cur_inst_stack_diff = 0
    for cur_inst_type in inst_types:
        cur_inst_stack_diff -= len(cur_inst_type.param_types)
        min_stack_depth = min(min_stack_depth, cur_inst_stack_diff)
        cur_inst_stack_diff += len(cur_inst_type.result_types)

    begin_stack_types = get_stack_state_from_type_req(input_req).rest_types
    end_stack_types = get_stack_state_from_type_req(base_req).rest_types
    inst_seq_type = _get_inst_type_by_diff_type_diff(begin_stack_types, end_stack_types, -min_stack_depth)
    return inst_types, inst_seq_type, can_drop_inst_num, type_reqs


def _get_stack_type_from_type_req(type_req:typeReq)->list[str]:
    return type_req.tys[0].result_types

def _get_inst_type_by_diff_type_diff(type1:list[str], type2:list[str],requqired_num:int)->funcType:
    common_num = 0
    for ty1, ty2 in zip(type1, type2):
        if ty1 == ty2:
            common_num += 1
        else:
            break
    max_to_reduce_common_num = len(type1) - requqired_num
    common_num = min(common_num, max_to_reduce_common_num)
    to_drop = type1[::-1][:len(type1)-common_num]
    assert common_num <= len(type1)
    to_drop: list[str] = type1[:len(type1)-common_num]
    to_drop = type1[common_num:]
    inner_layer_to_pad = type2[common_num:]
    return funcTypeFactory.generate_one_func_type_default(to_drop, inner_layer_to_pad)


def check_parser_match_wasm_file(parser:WasmParser, wasm_file:str)->bool:
    input_parser = get_parser_from_wasm_path(wasm_file)
    for cur_parser_func, input_parser_func in zip(parser.defined_funcs, input_parser.defined_funcs):
        assert len(cur_parser_func.insts) == len(input_parser_func.insts)
        for cur_inst, input_inst in zip(cur_parser_func.insts, input_parser_func.insts):
            assert cur_inst.opcode_text == input_inst.opcode_text
    return True


def is_clean_stack_inst(inst:Inst)->bool:
    op = inst.opcode_text
    if op == 'unreachable':
        return True
    elif op == 'br':
        return True
    elif op == 'return':
        return True
    elif op == 'br_table':
        return True
    else:
        return False

def remove_empty_node_in_nodelist(node_list:NodeList)->None:
    to_remove = []
    for n in node_list.sub_nodes:
        if n.get_length() == 0:
            to_remove.append(n)
    
    for n in to_remove:
        node_list.sub_nodes.remove(n)


def refine_type_reqs(
    node_type_reqs:list[typeReq], 
    state_type_reqs:list[typeReq]
    )->list[int]:
    t0 = time.time()
    to_reinfer_inst_idxs = []
    updated_type_idxs = []
    # 
    for idx, req in enumerate(node_type_reqs):
        # if req.req_type == 'eq':
        if len(req.tys) != 1:
            to_reinfer_inst_idxs.append(idx)
    # if len(to_reinfer_inst_idxs) == 0:
    #     return [] 
    assert len(state_type_reqs) == len(node_type_reqs) + 1
    try_times = 0
    while True:
        try_times += 1
        has_remove_ = False
        for inst_idx in range(len(node_type_reqs)-1, -1, -1):
            before_req = state_type_reqs[inst_idx]
            after_req = state_type_reqs[inst_idx+1]

            if (len(node_type_reqs[inst_idx].tys) != 1 or len(before_req.tys) != 1):
                cur_inst_candi_types = node_type_reqs[inst_idx].tys
                before_req_candi_types = before_req.tys
                possible_before_type_candis:list[funcType] = []
                possible_cur_type_candis:list[funcType] = []
                for candi_cur_type in cur_inst_candi_types:
                    for candi_before_type in before_req_candi_types:
                        _cur_inst_req = typeReq(tys=[candi_cur_type], req_type=node_type_reqs[inst_idx].req_type)
                        _before_req = typeReq(tys=[candi_before_type], req_type=before_req.req_type)
                        combined_req = merge_req(_before_req, _cur_inst_req)
                        if len(combined_req.tys) == 0:
                            continue
                        if len(after_req.tys) == 1:
                            min_result_len = min(
                                len(combined_req.ty0.result_types),
                                len(after_req.ty0.result_types),
                                )
                            if combined_req.ty0.result_types[len(combined_req.ty0.result_types)-min_result_len:] != after_req.ty0.result_types[len(after_req.ty0.result_types)-min_result_len:]:
                                continue
                        possible_before_type_candis.append(candi_before_type)
                        possible_cur_type_candis.append(candi_cur_type)
                node_type_reqs[inst_idx] = typeReq(tys=possible_cur_type_candis, req_type=node_type_reqs[inst_idx].req_type)
                state_type_reqs[inst_idx] = typeReq(tys=possible_before_type_candis, req_type=before_req.req_type)
                if len(possible_cur_type_candis) == 1 and inst_idx in to_reinfer_inst_idxs:

                    updated_type_idxs.append(inst_idx)
                    to_reinfer_inst_idxs.remove(inst_idx)
                    has_remove_ = True
        if not has_remove_:
            break
        print('cur refine times', try_times)
        if try_times > 50:
            raise ValueError(f'try_times > 50: {try_times}')
    t1 = time.time()
    # if t1 - t0 > 20:
    #     raise ValueError(f'refine_type_reqs time cost: {t1 - t0}')
    return updated_type_idxs


def get_node_type_of_each_node(
    node_list:NodeList,
    ast_state:ASTState
) -> tuple[list[funcType], list[typeReq], list[typeReq]]:
    
    node0_loc = node_list.loc
    node_types = []
    context, nodes, last_block_param, _ = get_structure_before_probe_loc(
    node0_loc,
    ast_state.parser,
    ast_state.ast_info
    )
    base_req = infer_stack_req_before_insts(context, nodes, last_block_param)
    node_type_reqs:list[typeReq] = []
    node_types:list[funcType] = []
    all_has_one_type = True
    type_req_before_each_node = [base_req]
    # init_stack
    for node in node_list.sub_nodes:
        node_type_req = get_node_type_req(ast_state, node)
        if len(node_type_req.tys) != 1:
            all_has_one_type = False
        node_types.append(node_type_req.ty0)
        node_type_reqs.append(node_type_req)
        base_req = merge_req(base_req, node_type_req)
        type_req_before_each_node.append(base_req)
    # 
    if all_has_one_type:
        return node_types, node_type_reqs, type_req_before_each_node
    ref_type = 'eq'
    if len(type_req_before_each_node[-1].tys) != 1:
        type_req_before_each_node[-1] = typeReq(tys=[
                node_list.get_block_type()
                ], req_type=ref_type)
        
    need_refine = False
    for idx, req in enumerate(node_type_reqs):
        if len(req.tys) != 1:
            need_refine = True
    if need_refine:
        updated_type_idxs = refine_type_reqs(
            node_type_reqs, 
            type_req_before_each_node
        )
        for idx in updated_type_idxs:
            node_types[idx] = node_type_reqs[idx].ty0
    return node_types, node_type_reqs, type_req_before_each_node

def get_node_type_reqs_of_each_node(
    node_list:NodeList,
    after_node_idx:int,
    ast_state:ASTState
) :
    node0_loc = node_list.loc
    node_types = []
    context, nodes, last_block_param, _ = get_structure_before_probe_loc(
    node0_loc,
    ast_state.parser,
    ast_state.ast_info
    )
    base_req = infer_stack_req_before_insts(context, nodes, last_block_param)
    node_type_reqs:list[typeReq] = []
    node_types:list[funcType] = []
    type_req_before_each_node = [base_req]
    for node in node_list.sub_nodes:
        node_type_req = node.get_type_req(context)
        node_types.append(node_type_req.ty0)
        node_type_reqs.append(node_type_req)
        base_req = merge_req(base_req, node_type_req)
        type_req_before_each_node.append(base_req)
    # 
    type_req_before_each_node[-1] = typeReq(tys=[
            node_list.get_block_type()
            ], req_type='eq')
    # 
    
    updated_type_idxs = refine_type_reqs(
        node_type_reqs, 
        type_req_before_each_node
    )
    for idx in updated_type_idxs:
        node_types[idx] = node_type_reqs[idx].ty0
    return type_req_before_each_node[after_node_idx+1]



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

def get_context_and_cur_stack_type_req(
    ori_node_list:NodeList,
    ast_state:ASTState,
) -> tuple[Context, typeReq]:
    context, nodes, last_block_param, rest_insts= get_structure_before_probe_loc(
        probe_loc=ori_node_list.loc,
        parser=ast_state.parser, 
        ast_info=ast_state.ast_info,
        known_innermost_node=ori_node_list
    )
    parent_node_list = ori_node_list
    cur_stack_type_req = infer_stack_types_using_before_loc_structure(
        context=context,
        nodes=nodes, 
        last_block_param=last_block_param, 
        rest_insts=rest_insts,
        parent_node_list=parent_node_list
    )
    return context, cur_stack_type_req


def get_try_time(inst_num:int)->int:
    if inst_num <= 3000:
        return 300
    elif inst_num <= 20000:
        return 900
    else:
        return 1800
