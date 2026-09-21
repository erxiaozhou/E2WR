from typing import Union
from extract_block_mutator.WasmParser import WasmParser, get_parser_from_wasm_path
from extract_block_mutator.parser_to_file_util import parser2wasm
from pathlib import Path


def safe_write_wasm(parser:WasmParser, path:Union[str, Path]):
    new_parser = WasmParser(
        types = parser.types.copy(),
        imports = parser.imports,
        defined_func_ty_ids = parser.defined_func_ty_ids    ,
        defined_table_datas = parser.defined_table_datas,
        defined_memory_datas = parser.defined_memory_datas,
        defined_globals = parser.defined_globals,
        exports = parser.exports,
        elem_sec_datas = parser.elem_sec_datas,
        defined_funcs = [_.copy() for _ in parser.defined_funcs],
        data_sec_datas = parser.data_sec_datas,
        start_sec_data = parser.start_sec_data,
        data_count_sec_data = parser.data_count_sec_data,
        customs = parser.customs,
    )
    parser2wasm(new_parser, path)

def get_insts_num(p:Union[Path, str]):
    parser = get_parser_from_wasm_path(p)
    return get_insts_num_from_parser(parser)

def get_insts_num_from_parser(parser:WasmParser):
    inst_num = 0
    for func in parser.defined_funcs:
        inst_num += len(func.insts)
    return inst_num
