from WasmInfoCfg import ImportType
from util.debug_util import ValidateCheckType, validate_wasm
from extract_block_mutator.get_data_shell import get_impotr_attr
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
