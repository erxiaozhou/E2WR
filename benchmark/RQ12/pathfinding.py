#!/usr/bin/env python3

import os
import shutil
import sys
import subprocess
from pathlib import Path
import tempfile
if not (runtime_root := os.getenv("CP9201_RUNTIME_ROOT")):
    raise RuntimeError("CP9201_RUNTIME_ROOT is not set")

RUNTIME_ROOT = Path(runtime_root)

def _get_tempfile_path():
    fd, temp_path = tempfile.mkstemp(suffix='.wasm')
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

WASM_IS_TMP = False
if Path(WASM).suffix != '.wasm':
    wasm_path = _get_tempfile_path()
    shutil.copy(WASM, wasm_path)
    WASM = wasm_path
    WASM_IS_TMP = True
PRINT = os.getenv('PRINT', 'False').lower() in ('true', '1', 't')
# commit ccf0c5622ed3acba83fd2c8b8612edc072a3438c
# Author: Ben L. Titzer <ben.titzer@gmail.com>
# Date:   Wed Feb 8 10:06:03 2023 -0500

#     [jit] Fix register constraint in call sequence

# commit 25abe41f3d6e3db22e22584dbd645a51f9978b14 (HEAD)
# Author: Ben L. Titzer <ben.titzer@gmail.com>
# Date:   Tue Feb 7 21:12:48 2023 -0500

#     [simd] Fix CodeValidator for load/store signatures

def wrong_on_target():
    command = f'timeout 10s {RUNTIME_ROOT}/engines/wizard-25abe41 -mode=jit {WASM}'
    result = subprocess.run(
        command,
        shell=True,
        capture_output=True,
        text=True,
    )
    if PRINT: print(result)
    if result.returncode != 0 and result.returncode != 124: # error but not timeout
        return True
    else:
        return False

def correct_on_other():
    command = f'timeout 20s {RUNTIME_ROOT}/engines/wizard-ccf0c56 -mode=jit {WASM}'
    result = subprocess.run(
        command,
        shell=True,
        capture_output=True,
        text=True,
    )
    if PRINT: print(result)
    if result.returncode == 0:
        return True
    else:
        return False

if not os.path.isfile(WASM):
    print(f"Error: The file {WASM} does not exist.")
    sys.exit(2)

if wrong_on_target() and correct_on_other():
    print("Interesting!")
    if WASM_IS_TMP:
        _remove_temp_file(WASM)
    sys.exit(0)
else:
    print("Not interesting")
    if WASM_IS_TMP:
        _remove_temp_file(WASM)
    sys.exit(1)

