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

# commit 718f0671e7e62eeab3b57899c43a9530e89aff63 (HEAD)
# Author: liang.he <liang.he@intel.com>
# Date:   Fri Dec 1 11:14:13 2023 +0800

#     Output warning and quit if import/export name contains '\00' (#2806)

#     Leave it as a limitation when import/export name contains '\00' in wasm file.
#     p.s. https://github.com/bytecodealliance/wasm-micro-runtime/issues/2789

# commit 873558c40edc60731fd9091722681e5e54bbe95c
# Author: Enrico Loparco <eloparco@amazon.com>
# Date:   Thu Nov 30 00:49:58 2023 +0000

#     Get rid of compilation warnings and minor doc fix (#2839)

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
    command = f'timeout 10s {RUNTIME_ROOT}/engines/wamrc-873558cZ -o {aot_path} {WASM}'
    result = subprocess.run(
        command,
        shell=True,
        capture_output=True,
        text=True,
    )
    if PRINT: print(result)
    _remove_temp_file(aot_path)
    expected_result = 'WASM module load failed: duplicate export name'
    if expected_result in result.stdout:
        return True
    else:
        return False

def correct_on_other():
    aot_path = _get_tempfile_path()
    command = f'timeout 10s {RUNTIME_ROOT}/engines/wamrc-718f067Z -o {aot_path} {WASM}'
    result = subprocess.run(
        command,
        shell=True,
        capture_output=True,
        text=True,
    )
    if PRINT: print(result)
    _remove_temp_file(aot_path)
    expected_result = 'WASM module load failed: duplicate export name'
    if expected_result not in result.stdout:
        return True
    else:
        return False

if not os.path.isfile(WASM):
    print(f"Error: The file {WASM} does not exist.")
    sys.exit(2)

if wrong_on_target() and correct_on_other():
    print("Interesting!")
    sys.exit(0)
else:
    print("Not interesting")
    sys.exit(1)
