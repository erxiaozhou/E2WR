#!/usr/bin/env python3

import os
import sys
import subprocess
import tempfile
from pathlib import Path
import time
WAT2WASM_INFO = None

if len(sys.argv) == 2:
    WASM = sys.argv[1]
else:
    WASM = 'actual_input.wat'  
    assert Path(WASM).exists(), f'{WASM} not found'
    assert Path(WASM).suffix == '.wat', f'{WASM} is not a wat file, unexpected'
    new_name = Path(WASM).stem + '.wasm'
    command = f'timeout 10s wat2wasm {WASM} -o {new_name}'
    t0 = time.time()
    result = subprocess.run(
        command,
        shell=True,
        capture_output=True,
        text=True,
    )
    t1 = time.time()
    return_num = result.returncode
    WAT2WASM_INFO = f'{t1 - t0} {return_num}'
    WASM = new_name

if not (runtime_root := os.getenv("CP9201_RUNTIME_ROOT")):
    raise RuntimeError("CP9201_RUNTIME_ROOT is not set")

if not (valid_data_dir := os.getenv("VALID_DATA_DIR")):
    raise RuntimeError("VALID_DATA_DIR is not set")

RUNTIME_ROOT = Path(runtime_root)
VALID_DATA_DIR = Path(valid_data_dir)



PRINT = os.getenv('PRINT', 'False').lower() in ('true', '1', 't')

# commit 0ee5ffce8573a0e41f5a33dce562f541e98eb28a
# Author: Wenyong Huang <wenyong.huang@intel.com>
# Date:   Tue Mar 12 11:38:50 2024 +0800

#     Refactor APIs and data structures as preliminary work for Memory64 (#3209)

#     # Change the data type representing linear memory address from u32 to u64

#     ## APIs signature changes
#     - (Export)wasm_runtime_module_malloc
#       - wasm_module_malloc
#         - wasm_module_malloc_internal
#       - aot_module_malloc
#         - aot_module_malloc_internal
#     - wasm_runtime_module_realloc
#       - wasm_module_realloc
#         - wasm_module_realloc_internal
#       - aot_module_realloc
#         - aot_module_realloc_internal
#     - (Export)wasm_runtime_module_free
#       - wasm_module_free
#         - wasm_module_free_internal
#       - aot_module_malloc
#         - aot_module_free_internal
#     - (Export)wasm_runtime_module_dup_data
#       - wasm_module_dup_data
#       - aot_module_dup_data
#     - (Export)wasm_runtime_validate_app_addr
#     - (Export)wasm_runtime_validate_app_str_addr
#     - (Export)wasm_runtime_validate_native_addr
#     - (Export)wasm_runtime_addr_app_to_native
#     - (Export)wasm_runtime_addr_native_to_app
#     - (Export)wasm_runtime_get_app_addr_range
#     - aot_set_aux_stack
#     - aot_get_aux_stack
#     - wasm_set_aux_stack
#     - wasm_get_aux_stack
#     - aot_check_app_addr_and_convert, wasm_check_app_addr_and_convert
#       and jit_check_app_addr_and_convert
#     - wasm_exec_env_set_aux_stack
#     - wasm_exec_env_get_aux_stack
#     - wasm_cluster_create_thread
#     - wasm_cluster_allocate_aux_stack
#     - wasm_cluster_free_aux_stack

#     ## Data structure changes
#     - WASMModule and AOTModule
#       - field aux_data_end, aux_heap_base and aux_stack_bottom
#     - WASMExecEnv
#       - field aux_stack_boundary and aux_stack_bottom
#     - AOTCompData
#       - field aux_data_end, aux_heap_base and aux_stack_bottom
#     - WASMMemoryInstance(AOTMemoryInstance)
#       - field memory_data_size and change __padding to is_memory64
#     - WASMModuleInstMemConsumption
#       - field total_size and memories_size
#     - WASMDebugExecutionMemory
#       - field start_offset and current_pos
#     - WASMCluster
#       - field stack_tops

#     ## Components that are affected by the APIs and data structure changes
#     - libc-builtin
#     - libc-emcc
#     - libc-uvwasi
#     - libc-wasi
#     - Python and Go Language Embedding
#     - Interpreter Debug engine
#     - Multi-thread: lib-pthread, wasi-threads and thread manager

# commit b6216a5f8a5a6a6b7c516a5ad414c43ce904f400
# Author: Wenyong Huang <wenyong.huang@intel.com>
# Date:   Mon Mar 11 18:11:43 2024 +0800

#     Fix ip (bytecode offset) not committed into the latest aot frame (#3213)


def _get_tempfile_path():
    fd, temp_path = tempfile.mkstemp(suffix='.aot')
    os.close(fd)
    os.unlink(temp_path)
    return temp_path

def _remove_temp_file(temp_path):
    try:
        if os.path.exists(temp_path):
            os.unlink(temp_path)
    except OSError as e:
        # Log warning but don't fail - cleanup is best effort
        print(f"Warning: Failed to remove {temp_path}: {e}", file=sys.stderr)
def wrong_on_target():
    aot_path = _get_tempfile_path()
    command = f'{RUNTIME_ROOT}/CAO/wamr_compiler_install_before_3209/bin/wamrc -o {aot_path} {WASM} && {RUNTIME_ROOT}/CAO/install_aot_before_3209/bin/iwasm --heap-size=o -f entry {aot_path}'
    result = subprocess.run(
        command,
        shell=True,
        capture_output=True,
        text=True,
    )
    if PRINT: print(result)
    expected_output = '0x0:i32,0x0:i32,0x0:i32,0x0:i32'
    _remove_temp_file(aot_path)
    if expected_output in result.stdout:
        return True
    else:
        return False

def correct_on_other():
    aot_path = _get_tempfile_path()
    command = f'{RUNTIME_ROOT}/CAO/wamr_compiler_install_after_3209/bin/wamrc -o {aot_path} {WASM} && {RUNTIME_ROOT}/CAO/install_aot_after_3209/bin/iwasm --heap-size=o -f entry {aot_path}'
    result = subprocess.run(
        command,
        shell=True,
        capture_output=True,
        text=True,
    )
    if PRINT: print(result)
    expected_output = '''0x200d:i32,0x200d:i32,0x200d:i32,0x200d:i32'''
    _remove_temp_file(aot_path)
    if expected_output in result.stdout:
        return True
    else:
        return False

def write_oracle(info_):
    trigger = get_trigger_info()
    log_file_name = f'{VALID_DATA_DIR}/{trigger}.log'
    with open(log_file_name, 'a+') as f:
        to_rwite_content = f"{info_}\n"
        f.write(to_rwite_content)

def get_trigger_info():
    import psutil
    ppid = os.getppid()
    cur_cmd = ' '.join(sys.argv)
    if cur_cmd.endswith('test.wasm'):
        return  Path(cur_cmd.split()[-1]).parent.name
    parent = psutil.Process(ppid)
    grandparent = parent.parent()
    if grandparent:
        gp_cmd = ' '.join(grandparent.cmdline())
    else:
        gp_cmd = None
    if gp_cmd is not None:
        gp_cmd = gp_cmd.strip("'")
        if 'jar' in gp_cmd:
            result_dir = Path(gp_cmd.split()[-1]).name
            return result_dir
        elif 'shrink' in gp_cmd:
            result_idr = Path(gp_cmd.split()[-3]).parent.name
            return result_idr
    return None

def main():

    if not os.path.isfile(WASM):
        print(f"Error: The file {WASM} does not exist.")
        return 2
    if wrong_on_target() and correct_on_other():
        print("Interesting!")
        return 0
    else:
        print("Not interesting")
        return 1


if __name__ == "__main__":
    t0 = time.time()
    return_num = main()
    t1 = time.time()
    wat2wasm_part = WAT2WASM_INFO if WAT2WASM_INFO is not None else 'None None'
    info_ = f'{wat2wasm_part} {t1 - t0} {return_num}'
    write_oracle(info_)
    sys.exit(return_num)
