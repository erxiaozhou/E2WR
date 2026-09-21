from typing import Optional


class ProbeUtilWasmFuncManager:
    def __init__(
        self,
        print_core_probe_func_idx: Optional[int] = None,
        print_core_probe_type_idx: Optional[int] = None,
        noop_print_core_probe_func_idx: Optional[int] = None,
        store_i32_func_idx: Optional[int] = None,
        store_f32_func_idx: Optional[int] = None,
        store_i64_func_idx: Optional[int] = None,
        store_f64_func_idx: Optional[int] = None,
        store_v128_func_idx: Optional[int] = None
    ):
        self._print_core_probe_func_idx = print_core_probe_func_idx
        self._print_core_probe_type_idx = print_core_probe_type_idx
        self._noop_print_core_probe_func_idx = noop_print_core_probe_func_idx
        self._store_i32_func_idx = store_i32_func_idx
        self._store_f32_func_idx = store_f32_func_idx
        self._store_i64_func_idx = store_i64_func_idx
        self._store_f64_func_idx = store_f64_func_idx
        self._store_v128_func_idx = store_v128_func_idx
        self.process_stack_contain_ref_func_idxs:dict[tuple, int] = {}

    def get_process_stack_contain_ref_func_idx(self, stack_types: list[str]) -> Optional[int]:
        # assert 0
        key = tuple(stack_types)
        return self.process_stack_contain_ref_func_idxs.get(key)

    def set_process_stack_contain_ref_func_idx(self, stack_types: list[str], func_idx: int):
        key = tuple(stack_types)
        self.process_stack_contain_ref_func_idxs[key] = func_idx

    def not_need_update(self) -> bool:
        if self._print_core_probe_func_idx is None \
                and self._print_core_probe_type_idx is None \
                and self._noop_print_core_probe_func_idx is None \
                and self._store_i32_func_idx is None \
                and self._store_f32_func_idx is None \
                and self._store_i64_func_idx is None \
                and self._store_f64_func_idx is None \
                and self._store_v128_func_idx is None:
            return True
        return False

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

    @property
    def store_f32_func_idx(self) -> int:
        idx = self._store_f32_func_idx
        assert idx is not None, 'store_f32_func_idx is not set'
        return idx

    @property
    def store_i64_func_idx(self) -> int:
        idx = self._store_i64_func_idx
        assert idx is not None, 'store_i64_func_idx is not set'
        return idx

    @property
    def store_f64_func_idx(self) -> int:
        idx = self._store_f64_func_idx
        assert idx is not None, 'store_f64_func_idx is not set'
        return idx

    @property
    def store_v128_func_idx(self) -> int:
        idx = self._store_v128_func_idx
        assert idx is not None, 'store_v128_func_idx is not set'
        return idx

    def get_store_func_by_type(self, expected_type: str) -> Optional[int]:
        if expected_type == 'i32':
            return self._store_i32_func_idx
        elif expected_type == 'f32':
            return self._store_f32_func_idx
        elif expected_type == 'i64':
            return self._store_i64_func_idx
        elif expected_type == 'f64':
            return self._store_f64_func_idx
        elif expected_type == 'v128':
            return self._store_v128_func_idx
        else:
            raise ValueError(f'unknown type: {expected_type}')

    def set_store_func_idx(self, expected_type: str, func_idx: int):
        if expected_type == 'i32':
            assert self._store_i32_func_idx is None
            self._store_i32_func_idx = func_idx
        elif expected_type == 'f32':
            assert self._store_f32_func_idx is None
            self._store_f32_func_idx = func_idx
        elif expected_type == 'i64':
            assert self._store_i64_func_idx is None
            self._store_i64_func_idx = func_idx
        elif expected_type == 'f64':
            assert self._store_f64_func_idx is None
            self._store_f64_func_idx = func_idx
        elif expected_type == 'v128':
            assert self._store_v128_func_idx is None
            self._store_v128_func_idx = func_idx
        else:
            raise ValueError(f'unknown type: {expected_type}')
