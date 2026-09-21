#!/usr/bin/env python3

import os
import sys
import subprocess
import tempfile
from pathlib import Path

if not (runtime_root := os.getenv("CP9201_RUNTIME_ROOT")):
    raise RuntimeError("CP9201_RUNTIME_ROOT is not set")

RUNTIME_ROOT = Path(runtime_root)


if len(sys.argv) == 2:
    WASM = sys.argv[1]
else:
    WASM = 'actual_input.wat'  
    assert Path(WASM).exists(), f'{WASM} not found'
    assert Path(WASM).suffix == '.wat', f'{WASM} is not a wat file, unexpected'
    new_name = Path(WASM).stem + '.wasm'
    command = f'timeout 10s wat2wasm {WASM} -o {new_name}'
    result = subprocess.run(
        command,
        shell=True,
        capture_output=True,
        text=True,
    )
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

if not os.path.isfile(WASM):
    print(f"Error: The file {WASM} does not exist.")
    sys.exit(2)
# print(f'wrong_on_target(): {wrong_on_target()}')
# print(f'correct_on_other(): {correct_on_other()}')
# assert 0
if wrong_on_target() and correct_on_other():
    print("Interesting!")
    sys.exit(0)
else:
    print("Not interesting")
    sys.exit(1)
