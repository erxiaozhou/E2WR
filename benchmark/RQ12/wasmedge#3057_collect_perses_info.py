#!/usr/bin/env python3

import os
import sys
import subprocess
import tempfile
from pathlib import Path
import time


if not (runtime_root := os.getenv("CP9201_RUNTIME_ROOT")):
    raise RuntimeError("CP9201_RUNTIME_ROOT is not set")

if not (valid_data_dir := os.getenv("VALID_DATA_DIR")):
    raise RuntimeError("VALID_DATA_DIR is not set")

RUNTIME_ROOT = Path(runtime_root)
VALID_DATA_DIR = Path(valid_data_dir)



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
PRINT = os.getenv('PRINT', 'False').lower() in ('true', '1', 't')

# commit 93fd4aeb6fb8b03cad3ffdb539642c52a645acc7
# Author: Shen-Ta Hsieh <beststeve@secondstate.io>
# Date:   Thu Feb 1 14:57:13 2024 +0800

#     [JIT] Create interface class `Executable` to hold shared library, AOT section and JIT library in `Symbol`

#     * Move `Wrapper` and `IntrinsticsTable` into `Executable
#     * Split `SharedLibrary` into two class
#     * Remove LDMgr
#     * Add `loadExecutable` in `Loader`

#     Signed-off-by: Shen-Ta Hsieh <beststeve@secondstate.io>

# commit 4cbb3767a01c31d8756081290e56a7dbf4d8b1ec  --> CAN'T BUILD
# commit 58f4a624f6afa3cbd70661b30a2e54f6fe13e846 --> CAN'T BUILD
# commit 6ad56a4638c4cabeffd05c8d47a1905f0c24ebeb --> CAN'T BUILD

# commit q
# Author: hydai <z54981220@gmail.com>
# Date:   Tue Feb 13 16:09:13 2024 +0800

#     [OpenWrt] Bump the WasmEdge version and API version

#     Signed-off-by: hydai <z54981220@gmail.com>

def _get_tempfile_path():
    fd, temp_path = tempfile.mkstemp(suffix='.so')
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
    so_path = _get_tempfile_path()
    lib_cfg = 'LD_LIBRARY_PATH={RUNTIME_ROOT}/wasmedge-862fffd/lib'
    wasmedge_862fffd_path = f'{RUNTIME_ROOT}/wasmedge-862fffd/bin/wasmedge'
    command = f'{lib_cfg} timeout 10s {wasmedge_862fffd_path} compile {WASM} {so_path} && {lib_cfg} timeout 10s {wasmedge_862fffd_path} {so_path} main'
    # print(f'command1: {command}')
    result = subprocess.run(
        command,
        shell=True,
        capture_output=True,
        text=True,
        errors='replace',            # Better handling of binary artifacts
    )
    if PRINT: print(result.stdout)
    expected_output = 'out of bounds memory access'
    result_value = expected_output not in (result.stdout + result.stderr)
    _remove_temp_file(so_path)
    return result_value

def correct_on_other():
    so_path = _get_tempfile_path()
    lib_cfg = 'LD_LIBRARY_PATH={RUNTIME_ROOT}/wasmedge-93fd4ae/lib'
    wasmedge_93fd4ae_path = f'{RUNTIME_ROOT}/wasmedge-93fd4ae/bin/wasmedge'
    command = f'{lib_cfg} timeout 10s {wasmedge_93fd4ae_path} compile {WASM} {so_path} && {lib_cfg} timeout 10s {wasmedge_93fd4ae_path} {so_path} main'
    # print(f'command2: {command}')
    result = subprocess.run(
        command,
        shell=True,
        capture_output=True,
        text=True,
        errors='replace',            # Better handling of binary artifacts
    )
    if PRINT: print(result.stdout)
    expected_output = 'out of bounds memory access'
    result_value = expected_output in result.stdout + result.stderr
    _remove_temp_file(so_path)
    return result_value


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
