from typing import Union
from extract_block_mutator.WasmParser import WasmParser
from extract_block_mutator.parser_to_file_util import parser2wasm
from pathlib import Path




def get_insts_num_from_parser(parser:WasmParser):
    inst_num = 0
    for func in parser.defined_funcs:
        inst_num += len(func.insts)
    return inst_num
