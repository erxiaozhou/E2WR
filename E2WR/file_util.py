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


def copy_file(src_file, tgt_file):
    shutil.copy(src_file, tgt_file)







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






def get_time_string():
    return time.strftime('%m-%d-%H-%M-%S', time.localtime())




def print_ba(ba):
    ba = bytearray(ba)
    print([hex(x) for x in ba])










def bytes2f32(bs):
    return struct.unpack('=f', bs)




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

