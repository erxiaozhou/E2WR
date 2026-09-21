
from WasmInfoCfg import globalValMut
from extract_block_mutator.DefShell import gen_global_data, gen_global_type
from extract_block_mutator.InstGeneration.InstFactory import InstFactory


def generate_global_def(mut, val_type):
    if isinstance(mut,  bool):
        mut = globalValMut.from_bool(mut)
    global_type = gen_global_type(val_type, mut)
    # init_part = 
    if global_type.data['val_type'] == 'i32':
        init_part = InstFactory.gen_binary_info_inst_high_single_imm('i32.const', imm0=0)
    elif global_type.data['val_type'] == 'i64':
        init_part = InstFactory.gen_binary_info_inst_high_single_imm('i64.const', imm0=0)
    else:
        raise NotImplementedError('global type not supported')
    # global_type_data = gen_global_type(global_type, mut)
    return gen_global_data(global_type, init_part)
