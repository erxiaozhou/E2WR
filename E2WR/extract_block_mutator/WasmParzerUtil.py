
from .WasmParser import WasmParser



def append_func_core_base(wasm_parser: WasmParser, generated_func):
    func_type = generated_func.func_ty
    type_idx = _identify_func_type_idx(wasm_parser, func_type)
    if type_idx is None:
        wasm_parser.types.append(func_type)
        type_idx = len(wasm_parser.types) - 1
    wasm_parser.defined_func_ty_ids.append(type_idx)
    wasm_parser.defined_funcs.append(generated_func)
    # traceback.print_stack()


def _identify_func_type_idx(wasm_parser, func_type):
    for type_idx, cur_type in enumerate(wasm_parser.types):
        if cur_type == func_type:
            return type_idx
    return None
