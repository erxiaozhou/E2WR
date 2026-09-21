#!/usr/bin/python3
import subprocess
import sys
import re
import os
from pathlib import Path

import threading
import signal
import time
from typing import List, Tuple, Dict, Any, Union, Optional
if not (runtime_root := os.getenv("CP9201_RUNTIME_ROOT")):
    raise RuntimeError("CP9201_RUNTIME_ROOT is not set")

RUNTIME_ROOT = Path(runtime_root)
runtime0 = f'{RUNTIME_ROOT}/CAO/before_2459_2450_wamr_jit/bin/iwasm'
assert Path(runtime0).exists(), f'{runtime0} not found'
runtime1 = f'{RUNTIME_ROOT}/CAO/after_2550_2459_wamr_jit/bin/iwasm'
assert Path(runtime1).exists(), f'{runtime1} not found'

def run_with_timeout(
    cmd: Union[List[str], str],
    timeout: int = 30,
    shell: bool = True,
    cwd: Optional[str] = None,
    env: Optional[Dict[str, str]] = None,
    input_data: Optional[str] = None,
    kill_process_group: bool = True,
    text: bool = True
) -> Dict[str, Any]:
    
    
   
    popen_kwargs = {
        'stdout': subprocess.PIPE,
        'stderr': subprocess.PIPE,
        'shell': shell,
        'text': text,
    }
    if cwd:
        popen_kwargs['cwd'] = cwd
    if env:
        popen_kwargs['env'] = env
    if input_data is not None:
        popen_kwargs['stdin'] = subprocess.PIPE
    if kill_process_group:
        popen_kwargs['preexec_fn'] = os.setsid
    start_time = time.time()
    process = subprocess.Popen(cmd, **popen_kwargs)
    timeout_occurred = False
    def terminate_on_timeout():
        nonlocal timeout_occurred
        timeout_occurred = True

        try:
            if kill_process_group:
                os.killpg(os.getpgid(process.pid), signal.SIGTERM)
            else:
                process.terminate()
            try:
                process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                if kill_process_group:
                    os.killpg(os.getpgid(process.pid), signal.SIGKILL)
                else:
                    process.kill()
        except Exception as e:
            pass
    timer = threading.Timer(timeout, terminate_on_timeout)
    timer.daemon = True
    timer.start()
    try:
        stdout, stderr = process.communicate(input=input_data)
        timer.cancel()
        execution_time = time.time() - start_time
        return {
            'stdout': stdout,
            'stderr': stderr,
            'returncode': process.returncode,
            'timeout_occurred': timeout_occurred,
            'execution_time': execution_time
        }
    except Exception as e:
        timer.cancel()
        try:
            if kill_process_group:
                os.killpg(os.getpgid(process.pid), signal.SIGKILL)
            else:
                process.kill()
        except:
            pass
        raise e


def main()->int:
    if len(sys.argv) == 2:
        wasm_file = sys.argv[1]
    else:
        wasm_file = 'actual_input.wat'  
        assert Path(wasm_file).exists(), f'{wasm_file} not found'
        assert Path(wasm_file).suffix == '.wat', f'{wasm_file} is not a wat file, unexpected'
        new_name = Path(wasm_file).stem + '.wasm'
        run_with_timeout(f'wat2wasm {wasm_file} -o {new_name}', timeout=30)
        wasm_file = new_name
    command1 = f'{runtime0}  --heap-size=0 --llvm-jit  -f main {wasm_file}'
    command2 = f'{runtime1}  --heap-size=0 --llvm-jit  -f main {wasm_file}'
    try:
        wasm_tools_output = subprocess.check_output(
            f"wasm-tools print {wasm_file}", 
            shell=True, 
            stderr=subprocess.STDOUT
        ).decode()
        if not re.search(r'\(export "main"', wasm_tools_output):
            return 1
    except subprocess.CalledProcessError:
        return 1
    try:
        result1 = run_with_timeout(
            cmd=command1,
            timeout=10,
            shell=True,
            text=True
        )
        result2 = run_with_timeout(
            cmd=command2,
            timeout=10,
            shell=True,
            text=True
        )
        if len(result1['stderr']) !=0 or len(result2['stderr']) !=0:
            return 1
        if result1['timeout_occurred'] or result2['timeout_occurred']:
            return 1
        if result1['stdout'] == result2['stdout'] and result1['stderr'] == result2['stderr'] and result1['returncode'] == result2['returncode']:
            
            return 1
        else:
            return 0
    except subprocess.CalledProcessError:
        return 1
    except Exception as e:
        return 1
if __name__ == "__main__":
    return_num = main()
    sys.exit(return_num)