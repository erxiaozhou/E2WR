from typing import Protocol

# from extract_block_mutator.wasmFunc import wasmFunc
from .get_data_shell import get_global_attr
from extract_block_mutator.encode.NGDataPayload import DataPayloadwithName
from .funcType import funcType
from .get_data_shell import get_func_idxs
from util.util import AbstractMethodException

class InfoLoader(Protocol):
    types: list[funcType]
    imports: list[DataPayloadwithName]
    # defined_func_ty_ids:list[int]
    defined_table_datas: list[DataPayloadwithName]
    defined_memory_datas: list[DataPayloadwithName]
    defined_globals: list[DataPayloadwithName]
    exports: list[DataPayloadwithName]
    elem_sec_datas: list[DataPayloadwithName]
    # defined_funcs
    data_sec_datas: list[DataPayloadwithName]
    # defined_funcs:list[wasmFunc]
    # start_sec_data:Optional[int]
    # data_count_sec_data:Optional[int]
    # customs:list[DataPayloadwithName]

    # func_type_idxs: list[int]
    # import_func_ty_ids:list[int]
    import_func_num: int
    import_memory_num: int
    import_table_num: int
    import_global_num: int

    local_types: list[str]
    cur_func_ty: funcType

    @property
    def func_type_idxs(self):
        raise AbstractMethodException()
    @property
    def func_idxs_in_elem(self):
        func_idxs = []
        for elem_desc in self.elem_sec_datas:
            cur_func_idxs = get_func_idxs(elem_desc)
            func_idxs.extend(cur_func_idxs)
        # func_idxs = [x for x in func_idxs if x is not None]
        return list(set(func_idxs))
    # @property
    # def cur_func_ty(self)->funcType:
    #     ...

    @property
    def local_num(self):
        return len(self.local_types)

    @property
    def global_types(self):
        return [get_global_attr(d, 'global_val_type') for d in self.defined_globals]

    @property
    def global_muts(self):
        return [get_global_attr(d, 'mut') for d in self.defined_globals]

    @property
    def func_num(self) -> int:
        return len(self.func_type_idxs)

    @property
    def mem_num(self):
        return len(self.defined_memory_datas) + self.import_memory_num

    @property
    def global_num(self):
        return len(self.defined_globals) + self.import_global_num

    @property
    def table_num(self):
        return len(self.defined_table_datas) + self.import_table_num

    @property
    def type_num(self):
        return len(self.types)

    @property
    def defined_memory_num(self):
        return len(self.defined_memory_datas)

    @property
    def defined_table_num(self):
        return len(self.defined_table_datas)

    @property
    def defined_global_num(self):
        return len(self.defined_globals)
