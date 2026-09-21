#!/usr/bin/env python3

import os
import shutil
import sys
import subprocess
from pathlib import Path
import tempfile
if not (runtime_root := os.getenv("CP9201_RUNTIME_ROOT")):
    raise RuntimeError("CP9201_RUNTIME_ROOT is not set")

if not (oracle_data_dir := os.getenv("ORACLE_DATA_DIR")):
    raise RuntimeError("ORACLE_DATA_DIR is not set")

RUNTIME_ROOT = Path(runtime_root)
ORACLE_DATA_DIR = Path(oracle_data_dir)

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

# commit 33ec201952ca2d8cef4fa2f93d9265395872c907
# Author: Ben L. Titzer <ben.titzer@gmail.com>
# Date:   Tue Feb 7 13:59:28 2023 -0500

#     [memory64] Parse initial and max memory sizes properly

# commit c0f4ac3763069d6d5f12fbcad7ffbcd935901c5a
# Author: Ben L. Titzer <ben.titzer@gmail.com>
# Date:   Tue Feb 7 12:24:48 2023 -0500

#     [memory64] Parse is64 bit on memory declarations

def wrong_on_target():
    command = f'timeout 10s {RUNTIME_ROOT}/engines/wizard-c0f4ac3 -mode=int {WASM}'
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
    command = f'timeout 20s {RUNTIME_ROOT}/engines/wizard-33ec201 -mode=int {WASM}'
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

def write_oracle(info_):
    trigger = get_trigger_info()
    log_file_name = f'{ORACLE_DATA_DIR}/{trigger}.log'
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
        if WASM_IS_TMP:
            _remove_temp_file(WASM)
        return 0
    else:
        print("Not interesting")
        if WASM_IS_TMP:
            _remove_temp_file(WASM)
        return 1


if __name__ == "__main__":
    import time
    t0 = time.time()
    return_num = main()
    t1 = time.time()
    info_ = f'{t1 - t0} {return_num}'
    write_oracle(info_)
    sys.exit(return_num)


