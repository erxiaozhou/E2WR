import os
import json
import struct
import time
from pathlib import Path
import pickle
import chardet
import logging
import shutil
from timeout_process import run_with_timeout

def copy_dir(src_dir, tgt_dir):
    shutil.copytree(src_dir, tgt_dir)

def copy_file(src_file, tgt_file):
    shutil.copy(src_file, tgt_file)


def default_exec_cmd_without_return(cmd, timeout=2):
    run_with_timeout(cmd, timeout=timeout)

def pickle_dump(path, data):
    if isinstance(path, str):
        path = Path(path)
    with path.open("wb") as f:
        pickle.dump(data, f)


def pickle_load(path):
    if isinstance(path, str):
        path = Path(path)
    with path.open("rb") as f:
        data = pickle.load(f)
    return data


def check_dir(path):
    if not isinstance(path, Path):
        path = Path(path)
    parent_path = path.parent
    if not parent_path.exists():
        check_dir(parent_path)
    if not path.exists():
        path.mkdir()
    return path


def read_json(path):
    if isinstance(path, str):
        path = Path(path)
    f = path.open(encoding="utf8")
    data = json.load(f)
    f.close()
    return data


def save_json(path, data):
    if isinstance(path, str):
        path = Path(path)
    f = path.open("w", encoding="utf8")
    json.dump(data, f, ensure_ascii=False, indent=4)
    f.close()


def write_bytes(path, byte_seq):
    with open(path, 'wb') as fwriter:
        fwriter.write(byte_seq)


def read_bytes(path):
    with open(path, 'rb') as f:
        return bytearray(f.read())


def path_write(path, content):
    if isinstance(path, str):
        path = Path(path)
    with path.open('w', encoding='utf8') as f:
        f.write(content)


def path_read(path):
    if isinstance(path, str):
        path = Path(path)
    try:
        with path.open('r', encoding='utf8') as f:
            content = f.read()
    except UnicodeDecodeError:
        with path.open('rb') as f:
            rbs = f.read()
            result = chardet.detect(rbs)
            encoding = result['encoding']
        if encoding is not None:
            with path.open('r', encoding=encoding) as f:
                content = f.read()
        else:
            with path.open('rb') as f:
                content = f.read()
            content = str(content)
    return content


def rm_dir(dir):
    try:
        p = Path(dir)
        # For directories (not symlinks), remove recursively
        if p.exists():
            if p.is_dir() and not p.is_symlink():
                shutil.rmtree(p, ignore_errors=False)
            else:
                try:
                    p.unlink()
                except FileNotFoundError:
                    pass
    except FileNotFoundError:
        # Align with `rm -rf` behavior: ignore if not found
        pass
    return dir


def cp_file(src_path, tgt_path):
    src = Path(src_path)
    dst = Path(tgt_path)
    if dst.exists() and dst.is_dir():
        dst_final = dst / src.name
        shutil.copy2(src, dst_final)
    else:
        # Treat as file path; ensure parent exists
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)


def get_time_string():
    return time.strftime('%m-%d-%H-%M-%S', time.localtime())


def remove_file_without_exception(path):
    path = str(path)
    try:
        os.remove(path)
    except FileNotFoundError:
        pass


def print_ba(ba):
    ba = bytearray(ba)
    print([hex(x) for x in ba])


def bytes2uint(bs):
    int_val = int.from_bytes(bs, byteorder='little', signed=False)
    return int_val


def uint2bytes(val, int_byte_num):
    return bytearray(int.to_bytes(val, int_byte_num,
              byteorder='little', signed=False))


def f322bytes(val):
    return struct.pack('<f', val)


def f642bytes(val):
    return struct.pack('<d', val)


def bytes2f32(bs):
    return struct.unpack('=f', bs)


def byte2str(bs):
    if len(bs) == 0:
        return ''
    result = chardet.detect(bs)
    if result is None:
        encoding = None
    else:
        encoding = result.get('encoding', None)
    if encoding is not None:
        try:
            s = bs.decode(encoding)
        except UnicodeDecodeError as e:
            raise e
            s = str(bs)
    else:
        s = str(bs)
    return s


def get_logger(logger_name, log_file_name) -> logging.Logger:
    logger = logging.getLogger(logger_name)
    if is_uninitialized_logger(logger):
        logger.setLevel('DEBUG')
        file = logging.FileHandler(log_file_name, mode='w', encoding='utf8')
        fmt = logging.Formatter(
            fmt="%(asctime)s - %(levelname)-9s - %(filename)-8s : %(lineno)s line - %(message)s")
        file.setFormatter(fmt)
        logger.addHandler(file)
    return logger


def is_uninitialized_logger(logger):
    return len(logger.handlers) == 0

def get_json_fmt_str(data):
    return json.dumps(data, ensure_ascii=False, indent=4)
