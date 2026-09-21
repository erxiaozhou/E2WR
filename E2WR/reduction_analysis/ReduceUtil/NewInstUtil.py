import random
from extract_block_mutator.InstGeneration.InstFactory import InstFactory
from extract_block_mutator.InstUtil import Inst


def infer_padding_inst_num(existing_layer, expected_layer):
    to_drop, inner_layer_to_pad = get_padding_meta_data(existing_layer, expected_layer)
    return len(to_drop) + len(inner_layer_to_pad)

def get_padding_meta_data(existing_layer, expected_layer):
    common_num = 0
    for ty1, ty2 in zip(existing_layer, expected_layer):
        if ty1 == ty2:
            common_num += 1
        else:
            break
    to_drop = existing_layer[::-1][:len(existing_layer)-common_num]
    inner_layer_to_pad = expected_layer[common_num:]
    return to_drop,inner_layer_to_pad


    

def padding_input_type_naive(existing_layer, expected_layer):
    to_drop, inner_layer_to_pad = get_padding_meta_data(existing_layer, expected_layer)

    insts = []
    for ty in to_drop:
        insts.extend(generate_n_drops(1))
    for ty in inner_layer_to_pad:
        insts.append(get_inst_by_require_ty_const_n(ty))
        
    return insts

def padding_core(to_remove_num:int, to_add_types:list[str])->list[Inst]:
    insts = []
    insts.extend(generate_n_drops(to_remove_num))
    for ty in to_add_types:
        insts.append(get_inst_by_require_ty_const_n(ty))
    return insts


def generate_n_drops(stack_len:int):
    new_insts = []
    for _ in range(stack_len):
        drop_inst = InstFactory.opcode_inst('drop')
        new_insts.append(drop_inst)
    return new_insts




def get_inst_by_require_ty_const_n(ty)->Inst:
    if ty == 'i32':
        val = random.choice([0,1])
        return InstFactory.gen_binary_info_inst_high_single_imm('i32.const', val)
    if ty == 'i64':
        val = random.choice([0,1])
        return InstFactory.gen_binary_info_inst_high_single_imm('i64.const', val)
    if ty == 'f32':
        val = random.choice([0.0, 1.0])
        return InstFactory.gen_binary_info_inst_high_single_imm('f32.const', val)
    if ty == 'f64':
        val = random.choice([0.0, 1.0])
        return InstFactory.gen_binary_info_inst_high_single_imm('f64.const', val)
    if ty == 'v128':
        vals = []
        for _ in range(16):
            vals.append(random.randint(0,255))
        return InstFactory.gen_binary_info_inst_high_single_imm('v128.const', vals)
    if ty == 'funcref':
        return InstFactory.gen_binary_info_inst_high_single_imm('ref.null', 'funcref')
    if ty == 'externref':
        return InstFactory.gen_binary_info_inst_high_single_imm('ref.null', 'externref')
    if ty == 'any':
        return get_inst_by_require_ty_const_n('i32')
    raise Exception(f'unexpected ty: {ty}')




