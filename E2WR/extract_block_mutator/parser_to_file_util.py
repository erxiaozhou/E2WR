from extract_block_mutator.InstUtil.Inst import Inst, imm_is_blocktpye
from extract_block_mutator.encode.new_defined_data_type import Blocktype
from extract_block_mutator.funcType import funcType
from extract_block_mutator.WasmParzerUtil import append_func_core_base
from .InstGeneration.InstFactory import InstFactory
from .WasmParser import WasmParser
from util.prepare_template import seq_encode_seq
from .encode.byte_define.SectionPart import custom_sec_decoder, type_sec_decoder, function_sec_decoder, import_sec_decoer,  table_sec_decoder,  memory_sec_decoder,  global_sec_decoder,  export_sec_decoder,  start_sec_decoder, elem_sec_decoder,  code_sec_decoder,  data_sec_decoder, data_count_sec_decoder

str2decoder = {
    'custom': custom_sec_decoder,
    'type': type_sec_decoder,
    'function': function_sec_decoder,
    'import': import_sec_decoer,
    'table': table_sec_decoder,
    'memory': memory_sec_decoder,
    'global': global_sec_decoder,
    'export': export_sec_decoder,
    'start': start_sec_decoder,
    'element': elem_sec_decoder,
    'code': code_sec_decoder,
    'data': data_sec_decoder,
    'data_count': data_count_sec_decoder
}


def parser2wasm_core(parser: WasmParser, out_path):
    # type_sec =
    sec_name2ba = {
        'type': parser.types,
        'function': parser.defined_func_ty_ids,
        'import': parser.imports,
        'table': parser.defined_table_datas,
        'memory': parser.defined_memory_datas,
        'global': parser.defined_globals,
        'export': parser.exports,
        'start': parser.start_sec_data,
        'element': parser.elem_sec_datas,
        'code': parser.defined_funcs,
        'data': parser.data_sec_datas,
        'data_count': parser.data_count_sec_data,
        'custom': parser.customs
    }
    sec_name2ba = {k: v for k, v in sec_name2ba.items() if v is not None}
    bas: list = []
    for encoder_name in seq_encode_seq:
        if encoder_name not in sec_name2ba:
            continue
        decoder = str2decoder[encoder_name]
        if encoder_name == 'custom':
            for custom_sec in sec_name2ba['custom']:
                ba = decoder.encode(custom_sec)
                bas.append(ba)
        else:
            if isinstance(sec_name2ba[encoder_name], list) and len(sec_name2ba[encoder_name]) == 0:
                continue
            ba = decoder.encode(sec_name2ba[encoder_name])
            bas.append(ba)
    with open(out_path, 'wb') as f:
        f.write(b'\0asm\1\0\0\0')
        for ba in bas:
            f.write(ba)


# def _light_copy_parser(parser: WasmParser):
    


def parser2wasm(parser: WasmParser, wasm_path):

    type_to_idx = {}
    for idx, func_type in enumerate(parser.types):
        type_to_idx[func_type] = idx
    
    replaced_num = 0
    func_type_idxs = []
    need_insert_data_count = False
    
    for func in parser.defined_funcs:
        func_type = func.func_ty
        func_type_key =func.func_ty
        
        if func_type_key in type_to_idx:
            idx = type_to_idx[func_type_key]
        else:
            parser.types.append(func_type)
            idx = len(parser.types) - 1
            type_to_idx[func_type_key] = idx
        func_type_idxs.append(idx)
        
        for inst_idx, inst in enumerate(func.insts):
            if not need_insert_data_count:
                op = inst.opcode_text
                if op == 'data.drop' or op == 'memory.init':
                    need_insert_data_count = True
            
            if imm_is_blocktpye(inst):
                imm = inst.imm_part.val
                if imm.init_with_type():
                    indicated_type: funcType = imm.init_data
                    indicated_type_key =indicated_type
                    
                    if indicated_type_key in type_to_idx:
                        idx = type_to_idx[indicated_type_key]
                    else:
                        parser.types.append(indicated_type)
                        idx = len(parser.types) - 1
                        type_to_idx[indicated_type_key] = idx

                    block_type = Blocktype(idx)
                    func.insts[inst_idx] = InstFactory.gen_binary_info_inst_high_single_imm(
                        inst.opcode_text, block_type)
                    replaced_num += 1
    
    parser.defined_func_ty_ids = func_type_idxs
    
    # process data count
    data_seg_num = len(parser.data_sec_datas)
    if need_insert_data_count:
        parser.data_count_sec_data = data_seg_num
    elif data_seg_num > 0 and (parser.data_count_sec_data is not None):
        parser.data_count_sec_data = data_seg_num

    parser2wasm_core(parser, wasm_path)




def insert_func_core(wasm_parser: WasmParser, generated_func):
    # wasm_parser.defined_funcs.append(generated_func)
    append_func_core_base(wasm_parser, generated_func)

