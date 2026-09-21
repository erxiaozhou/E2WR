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
    command = f'timeout 60s wat2wasm {WASM} -o {new_name}'
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

# os.system(f'file {WASM}')
# os.system(f'ls -sl {WASM}')
# print('WASM=', WASM)
# commit 6d2b05742997441fd4ac01d1cd18d71046cec703 (HEAD)
# Author: Ben L. Titzer <ben.titzer@gmail.com>
# Date:   Thu Jan 18 04:24:14 2024 -0500
#     [fast-int] Set four-byte sidetable entry by default

# commit 92a3330776928f0f1a39efe5bb83be67cb8cc0ba
# Merge: f8721590 83416e71
# Author: Ben L. Titzer <ben.titzer@gmail.com>
# Date:   Thu Jan 18 00:19:35 2024 +0000
#     [v3i/simd]: Minor update for the V3Interpreter (#150 from haoyu-zc/cleanup3)

# relevant code: setting fourByteSidetable off makes the control flow diverge

# // ELSE, (legal) CATCH, (legacy) CATCH_ALL: unconditional ctl xfer without stack copying
# bindHandlerNoAlign(Opcode.CATCH);
# bindHandlerNoAlign(Opcode.CATCH_ALL);
# bindHandlerNoAlign(Opcode.ELSE);
# asm.bind(ctl_xfer_nostack);
# if (FastIntTuning.fourByteSidetable) { // load and sign-extend a 4-byte pc delta
# 	asm.movd_r_m(r_tmp0, r_stp.plus(Sidetable_BrEntry.pc_delta.offset));
# 	asm.q.shl_r_i(r_tmp0, 32);
# 	asm.q.sar_r_i(r_tmp0, 32);
# } else {
# 	asm.movwsx_r_m(r_tmp0, r_stp.plus(Sidetable_BrEntry.pc_delta.offset));
# }
# asm.q.lea(r_ip, r_ip.plusR(r_tmp0, 1, -1)); // adjust ip
# if (FastIntTuning.fourByteSidetable) { // load and sign-extend a 4-byte STP delta
# 	asm.movwsx_r_m(r_tmp1, r_stp.plus(Sidetable_BrEntry.stp_delta.offset));
# 	asm.q.shl_r_i(r_tmp1, 32);
# 	asm.q.sar_r_i(r_tmp1, 32);
# } else {
# 	asm.movwsx_r_m(r_tmp1, r_stp.plus(Sidetable_BrEntry.stp_delta.offset));
# }
# asm.q.lea(r_stp, r_stp.plusR(r_tmp1, 4, 0)); // adjust stp XXX: preshift?
# endHandler();


def wrong_on_target():
    command = f'timeout 10s {RUNTIME_ROOT}/engines/wizard-92a3330 -mode=int {WASM}'
    result = subprocess.run(
        command,
        shell=True,
        capture_output=True,
        text=True,
    )
    if PRINT: print(result)
    # print(result)
    if result.returncode != 0 and result.returncode != 124: # error but not timeout
        return True
    else:
        return False

def correct_on_other():
    command = f'timeout 20s {RUNTIME_ROOT}/engines/wizard-6d2b057 -mode=int {WASM}'
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

# print(wrong_on_target())
# print(correct_on_other())

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

