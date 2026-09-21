#!/usr/bin/python3
import subprocess
import sys
import re
import os


import threading
import signal
from pathlib import Path
import time
from typing import List, Tuple, Dict, Any, Union, Optional
if not (runtime_root := os.getenv("CP9201_RUNTIME_ROOT")):
    raise RuntimeError("CP9201_RUNTIME_ROOT is not set")

if not (oracle_data_dir := os.getenv("ORACLE_DATA_DIR")):
    raise RuntimeError("ORACLE_DATA_DIR is not set")

RUNTIME_ROOT = Path(runtime_root)
ORACLE_DATA_DIR = Path(oracle_data_dir)

runtime0 = f'{RUNTIME_ROOT}/useable_runtimes/pr10_fjit/bin/iwasm'
runtime1 = f'{RUNTIME_ROOT}/useable_runtimes/wamr_before_3031_bb053e3a2de24daaabdbbea7dfa7a3dd1ab8cf87/install_fast_jit/bin/iwasm'

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
    command1 = f'{runtime0} --heap-size=0 --fast-jit -f to_test {wasm_file}'
    command2 = f'{runtime1} --heap-size=0 --fast-jit -f to_test {wasm_file}'
    try:
        wasm_tools_output = subprocess.check_output(
            f"wasm-tools print {wasm_file}", 
            shell=True, 
            stderr=subprocess.STDOUT
        ).decode()
        if not re.search(r'\(export "to_test"', wasm_tools_output):
            return 1
    except subprocess.CalledProcessError:
        return 1
    try:
        result1 = run_with_timeout(
            cmd=command1,
            timeout=30,
            shell=True,
            text=True
        )
        result2 = run_with_timeout(
            cmd=command2,
            timeout=30,
            shell=True,
            text=True
        )
        # print(result1)
        # print(result2)
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


if __name__ == "__main__":
    t0 = time.time()
    return_num = main()
    t1 = time.time()
    info_ = f'{t1 - t0} {return_num}'
    write_oracle(info_)
    sys.exit(return_num)