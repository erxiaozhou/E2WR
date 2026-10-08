from typing import Optional


class ProbeUtilWasmFuncManager:
    def __init__(
        self,
        print_core_probe_func_idx: Optional[int] = None,
        print_core_probe_type_idx: Optional[int] = None,
        noop_print_core_probe_func_idx: Optional[int] = None,
        store_i32_func_idx: Optional[int] = None,
    ):
        self._print_core_probe_func_idx = print_core_probe_func_idx
        self._print_core_probe_type_idx = print_core_probe_type_idx
        self._noop_print_core_probe_func_idx = noop_print_core_probe_func_idx
        self._store_i32_func_idx = store_i32_func_idx

    @property
    def print_core_probe_func_idx(self) -> int:
        idx = self._print_core_probe_func_idx
        assert idx is not None, 'print_core_probe_func_idx is not set'
        return idx

    @property
    def print_core_probe_type_idx(self) -> int:
        idx = self._print_core_probe_type_idx
        assert idx is not None, 'print_core_probe_type_idx is not set'
        return idx

    @property
    def noop_print_core_probe_func_idx(self) -> int:
        idx = self._noop_print_core_probe_func_idx
        assert idx is not None, 'noop_print_core_probe_func_idx is not set'
        return idx

    @property
    def store_i32_func_idx(self) -> int:
        idx = self._store_i32_func_idx
        assert idx is not None, 'store_i32_func_idx is not set'
        return idx

    def set_store_func_idx(self, expected_type: str, func_idx: int):
        # 插桩只创建 i32 存储函数（required_store_types 恒为 {'i32'}）
        if expected_type == 'i32':
            assert self._store_i32_func_idx is None
            self._store_i32_func_idx = func_idx
        else:
            raise ValueError(f'unknown type: {expected_type}')
