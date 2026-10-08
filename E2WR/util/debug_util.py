from pathlib import Path
from file_util import check_dir, path_read
from timeout_process import run_with_timeout
import tempfile
import re
from enum import Enum



_case_p = re.compile(r'^.*?\.wasm:.*?:')


class ValidateCheckType(Enum):
    SELF_CHECK = 0
    TEST_CHECK = 1
    OTHER = 2
    UNKNOWN = 3

    def to_str(self):
        if self == ValidateCheckType.SELF_CHECK:
            return 'SelfCheck'
        elif self == ValidateCheckType.TEST_CHECK:
            return 'TestCheck'
        elif self == ValidateCheckType.OTHER:
            return 'Other'
        elif self == ValidateCheckType.UNKNOWN:
            return 'DebugUnknown'
        else:
            raise Exception(f'Unsupported ValidateCheckType: {self}')

# ========================= validate wasm ==============================


def validate_wasm(wasm_path, print_detail_reason=False, timeout=10, tag=ValidateCheckType.UNKNOWN):
    cmd = 'wasm-validate {}'.format(wasm_path)
    run_result = run_with_timeout(cmd, timeout=timeout)
    if print_detail_reason:
        reason = run_result['stderr'].strip(' \t\n')
        if reason:
            print(reason)
    result =  not bool(run_result['returncode'])
    print('Valid wasm?:', result, ';;', tag)
    return result

def get_validation_info(wasm_path):
    cmd = 'wasm-validate {}'.format(wasm_path)
    run_result = run_with_timeout(cmd, timeout=10)
    reason = run_result['stderr'].strip(' \t\n')
    if len(reason) == 0:
        return ''
    else:
        return reason

# ======================================================================


def wasm2wat(wasm_path, wat_path):
    cmd = 'wasm2wat --no-check {} -o {}'.format(wasm_path, wat_path)
    run_with_timeout(cmd, timeout=5)


def wat_content2wasm(wat_content, wasm_path):
    with tempfile.NamedTemporaryFile(delete=False) as f:
        f.write(wat_content.encode())
        # f.flush()
    cmd = 'wat2wasm  --no-check {}  -o {}'.format(f.name, wasm_path)
    run_with_timeout(cmd, timeout=5)
    Path(f.name).unlink()


def wat2wasm(wat_path, wasm_path):
    content = path_read(wat_path)
    wat_content2wasm(content, wasm_path)

