from pathlib import Path
import time
from typing import Callable
from WasmInfoCfg import ExportType, ImportType
from util.debug_util import ValidateCheckType, validate_wasm
from extract_block_mutator.InstGeneration.InstFactory import InstFactory
from extract_block_mutator.InstUtil.ByteInst import ByteImmInst
from extract_block_mutator.InstUtil.Inst import Inst
from .util import generate_global_def
from extract_block_mutator.DefShell import gen_export_desc, gen_export_funcidx_part, gen_export_memidx_part, gen_import_func_desc, gen_limit1
from extract_block_mutator.encode.NGDataPayload import DataPayloadwithName
from extract_block_mutator.funcType import funcType
from extract_block_mutator.funcTypeFactory import funcTypeFactory
from extract_block_mutator.get_data_shell import get_elemseg_attr, get_export_attr, get_impotr_attr, has_func_idx
from extract_block_mutator.WasmParser import WasmParser
from timeout_process import run_with_timeout


def locate_imported_fd_write(parser:WasmParser):
    import_func_idx = 0
    for import_ in parser.imports:
        if get_impotr_attr(import_, 'type') == ImportType.func:
            if get_impotr_attr(import_, 'entity_name') == 'fd_write':
                return import_func_idx
            import_func_idx += 1
    return None


def insert_i32_globals(parser, num:int):
    for i in range(num):
        parser.defined_globals.append(generate_global_def(True, 'i32'))

def insert_i64_globals(parser, num:int):
    for i in range(num):
        parser.defined_globals.append(generate_global_def(True, 'i64'))


def rewrite_func_idxs_after_insert_import_func(
    parser:WasmParser, 
    original_import_func_num:int,
    new_import_func_num:int
):
    # 1. update ref.func  call
    # print('original_import_func_num', original_import_func_num, self.parser.func_num)
    for func in parser.defined_funcs:
        for inst_idx, inst in enumerate(func.insts):
            op = inst.opcode_text
            if op == 'call' or op == 'ref.func':
                assert isinstance(inst, ByteImmInst)
                imm0 = inst.imm_part.val
                assert isinstance(imm0, int)
                if imm0 >= original_import_func_num:
                    new_inst = InstFactory.gen_binary_info_inst_high_single_imm(op, imm0=imm0 + new_import_func_num)
                    func.insts[inst_idx] = new_inst
                # else:
    # 2. update element segment
    for seg_idx, seg in enumerate(parser.elem_sec_datas):
        seg_has_func_idx = has_func_idx(seg)
        if seg_has_func_idx:
            # if seg.data['name']
            # print('seg data', seg.data)
            # print('seg name', seg.inner_name)
            inner_name = seg.inner_name
            # assert isinstance(inner_name, DataPayloadName)
            inner_name_str = inner_name.name
            # assert isinstance(inner_name_str, str)
            # attr_ = get_elemseg_attr(seg, 'attr')
            
            if inner_name_str == 'passive_elem_seg0' \
                or inner_name_str == 'active_elem_seg0' \
                or inner_name_str == 'active_elem_seg1' \
                or inner_name_str == 'declarative_elem_seg0':
                func_idxs = seg.data['funcidxs']
                assert isinstance(func_idxs, list)
                for idx, func_idx in enumerate(func_idxs):
                    if func_idx >= original_import_func_num:
                        seg.data['funcidxs'][idx] = func_idx + new_import_func_num
            else:
                ref_type = get_elemseg_attr(seg, 'ref_type')
                if ref_type == 'externref':
                    pass
                for inst_idx, inst_expr in enumerate(seg.data['exprs']):
                    inst = inst_expr.val
                    assert isinstance(inst, Inst)
                    opcode = inst.opcode_text
                    if opcode == 'ref.func':
                        func_idx = inst.imm_part.val
                        if func_idx >= original_import_func_num:
                            seg.data['exprs'][inst_idx] = DataPayloadwithName(
                                name='Expr',
                                data={
                                    'expr':InstFactory.gen_binary_info_inst_high_single_imm(opcode, imm0=func_idx + new_import_func_num)
                                }
                            )
            # elif inner_name_str == 

            # raise NotImplementedError(f'not supported element segment : {seg}')
        
            


def rewrite_export_func_idxs(
    parser:WasmParser,
    original_import_func_num:int,
    new_import_func_num:int
):
    for idx, export_ in enumerate(parser.exports):
        # print('export_', export_)
        # print('self.parser.get_to_test_func_idx()', self.parser.get_to_test_func_idx())
        attr = get_export_attr(export_, 'attr')
        if attr == ExportType.func:
            func_idx = get_export_attr(export_, 'idx')
            assert isinstance(func_idx, int)
            if func_idx >= original_import_func_num:
                func_idx += new_import_func_num
                export_func_name = get_export_attr(export_, 'name')
                new_idx_part = gen_export_funcidx_part(func_idx=func_idx)
                new_export = gen_export_desc(name=export_func_name, desc=new_idx_part)
                parser.exports[idx] = new_export


def rewrite_start_idx_after_insert_probe_func(parser:WasmParser, 
    original_import_func_num:int,
    new_import_func_num:int
):
    cur_start = parser.start_sec_data
    if cur_start is None:
        return
    if cur_start >= original_import_func_num:
        cur_start += new_import_func_num
        parser.start_sec_data = cur_start

def get_target_func_type_idx_by_prepare(parser:WasmParser, target_type:funcType)->int:
    for idx, func_type in enumerate(parser.types):
        if func_type == target_type:
            return idx
    parser.types.append(target_type)
    return len(parser.types) - 1



def export_memory_for_wasi(parser:WasmParser):
    # identify whether there is a exported memory
    for export_ in parser.exports:
        if get_export_attr(export_, 'attr') == ExportType.mem and get_export_attr(export_, 'name') == 'memory':
            return
    parser.exports.append(gen_export_desc(name='memory', desc=gen_export_memidx_part(0)))


def prepare_memory_for_wasi(parser:WasmParser):
    if parser.mem_num == 0:
        parser.defined_memory_datas.append(gen_limit1(1))


def exec_and_get_trace(path:str,allocated_time=60, DEBUG=False):
    # cmd = self.cmd.format(path)
    cmd = f'timeout {allocated_time} wasmtime run {path}'
    print('USED TRACE COLLECTOR CMD', cmd)
    result = run_with_timeout(cmd, timeout=allocated_time, text=False)
    print(f'[VP] Take {result["execution_time"]} seconds ')
    if DEBUG and len(result['stdout']) == 0:
        # check whether the case,is invalid
        if not validate_wasm(path, tag=ValidateCheckType.OTHER):
            raise Exception(f'The instrumented program is invalid : {path}')
        if result['returncode'] != 0:
            
            if 'Cannot allocate memory' in str(result["stderr"]):
                print(f'Warning: There may be something wrong: StdOut: {result["stdout"]}, StdErr: {result["stderr"]}, ReturnCode: {result["returncode"]}')
                # raise Exception(f'Memory allocation failed for {path}')
    # if result['timeout_occurred']:
    #     print(f'The case {path} is timeout')
        # input('Press Enter to continue...')
    return result['stdout']
def import_fd_write_and_export_memory(parser:WasmParser):
    fd_write_func_idx = prepare_fd_write_import(parser)
    prepare_memory_for_wasi(parser)
    export_memory_for_wasi(parser)
    return fd_write_func_idx


def prepare_fd_write_import(parser:WasmParser):
    import_func_idx = locate_imported_fd_write(parser)
    if import_func_idx is None:
        import_func_idx = parser.import_func_num
        fd_write_type = funcTypeFactory.generate_one_func_type_default(['i32', 'i32', 'i32', 'i32'], ['i32'])
        fd_write_type_idx = get_target_func_type_idx_by_prepare(parser, fd_write_type)
        new_import = gen_import_func_desc(
            module_name='wasi_unstable',
            entity_name='fd_write',
            func_type_idx=fd_write_type_idx
        )
        parser.import_func_ty_ids.append(fd_write_type_idx)
        parser.imports.append(new_import)
        parser.import_func_num += 1
        rewrite_func_idxs_after_insert_import_func(parser, import_func_idx, 1)
        rewrite_export_func_idxs(parser, import_func_idx, 1)
        rewrite_start_idx_after_insert_probe_func(parser, import_func_idx, 1)
    return import_func_idx
