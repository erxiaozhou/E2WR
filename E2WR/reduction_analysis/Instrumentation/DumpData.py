
from typing import Any, Optional
from .ProbeType import ProbeType

class NotInitializedException(Exception):
    pass

class CanControlData:
    def __init__(
        self,
        cur_stack:Optional[list[str]],
        all_local_types:Optional[list[str]],
        local_idxs:Optional[list[int]],
        all_global_types:Optional[list[str]],
        mutable_global_idxs:Optional[list[int]],
    ):
        self._cur_stack = cur_stack
        self._all_local_types = all_local_types
        self._local_idxs = local_idxs
        self._all_global_types = all_global_types
        self._mutable_global_idxs = mutable_global_idxs

    @property
    def cur_stack(self)->list[str]:
        if self._cur_stack is None:
            raise NotInitializedException('cur_stack')
        return self._cur_stack
    
    @property
    def all_local_types(self)->list[str]:
        if self._all_local_types is None:
            raise NotInitializedException('local_types')
        return self._all_local_types
    
    @property
    def local_idxs(self)->list[int]:
        if self._local_idxs is None:
            raise NotInitializedException('local_idxs')
        return self._local_idxs

    @property
    def all_global_types(self)->list[str]:
        if self._all_global_types is None:
            raise NotInitializedException('all_global_types')
        return self._all_global_types
    
    @property
    def mutable_global_idxs(self)->list[int]:
        if self._mutable_global_idxs is None:
            raise NotInitializedException('mutable_global_idxs')
        return self._mutable_global_idxs
    
    def __str__(self):
        return f'{self.__class__.__name__}(cur_stack={self._cur_stack}, all_local_types={self._all_local_types}, local_idxs={self._local_idxs}, all_global_types={self._all_global_types}, mutable_global_idxs={self._mutable_global_idxs})'

    def __repr__(self):
        return self.__str__()


class DumpSeq:
    def __init__(self, 
                 all_types: list[str], 
                 has_value_mask: list[bool],
                 values: list[Any]
                 ):
        self.all_types = all_types
        self.has_value_mask = has_value_mask
        self.values = values
        assert len(self.all_types) == len(self.has_value_mask)
        assert len([x for x in self.has_value_mask if x]) == len(self.values)

    def __str__(self):
        return f'{self.__class__.__name__}(all_types={self.all_types}, has_value_mask={self.has_value_mask}, values={self.values})'

    def __repr__(self):
        return self.__str__()

class EmptyDumpSeq(DumpSeq):
    _instance = None
    def __new__(cls, *args, **kwargs):
        if cls._instance is None:
            cls._instance = super().__new__(cls, *args, **kwargs)
        return cls._instance

    def __init__(self, *args, **kwargs):
        super().__init__([], [], [])













class OneDumpData:
    def __init__(
        self, 
        dump_seq: DumpSeq,
        probe_type: ProbeType,
        probe_idx: int
    ):
        self.dump_seq = dump_seq
        self.probe_type = probe_type
        self.probe_idx = probe_idx

    def __repr__(self):
        return f'{self.__class__.__name__}(dump_seq={self.dump_seq}, probe_type={self.probe_type}, probe_idx={self.probe_idx})'


class DumpDataList(list):
    def __init__(self, dump_data_list: list[OneDumpData]):
        super().__init__(dump_data_list)  

    def __str__(self):
        return f'{self.__class__.__name__}(dump_data_list={super().__str__()})'


