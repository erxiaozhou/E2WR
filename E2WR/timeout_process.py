#!/usr/bin/env python3
import subprocess
import threading
import signal
import os
import time
from typing import Any, Union, Optional

def run_with_timeout(
    cmd,
    timeout: int = 30,
    shell: bool = True,
    cwd: Optional[str] = None,
    env: Optional[dict[str, str]] = None,
    input_data: Optional[str] = None,
    kill_process_group: bool = True,
    text: bool = True
) -> dict[str, Any]:
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
        print(f"Process timeout, execution time exceeds {timeout} seconds, terminating...")
        
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
