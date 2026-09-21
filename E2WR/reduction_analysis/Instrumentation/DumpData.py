
from typing import Any, Optional
from .ProbeType import ProbeType
from reduction_analysis.WasmSemanticsUtil import is_ref_type

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

    @classmethod
    def from_dump_data(cls, all_types: list[str], dump_vals: list, considered_idxs: Optional[list[int]]=None):
        has_value_mask = [not is_ref_type(ty) for ty in all_types]
        if considered_idxs is not None:
            for idx in range(len(all_types)):
                if idx not in considered_idxs:
                    has_value_mask[idx] = False
        # values = [dump_vals[i] for i in range(len(all_types)) if has_value_mask[i]]
        values = dump_vals
        # assert len(values) == sum([1 for x in has_value_mask if x]), print(f'len(values)={len(values)}, sum([1 for x in has_value_mask if x])={sum([1 for x in has_value_mask if x])}   all_types ={all_types}  dump_vals={dump_vals}   considered_idxs={considered_idxs}')
        return cls(all_types, has_value_mask, values)

    def __str__(self):
        return f'{self.__class__.__name__}(all_types={self.all_types}, has_value_mask={self.has_value_mask}, values={self.values})'

    def __repr__(self):
        return self.__str__()

    def get_seq_on_idxs(self, idxs: list[int])->'DumpSeq':
        cur_types = [self.all_types[i] for i in idxs]
        cur_has_value_mask = [self.has_value_mask[i] for i in idxs]
        cur_values = [self.values[i] for i in idxs]
        return self.__class__(cur_types, cur_has_value_mask, cur_values)



class EmptyDumpSeq(DumpSeq):
    _instance = None
    def __new__(cls, *args, **kwargs):
        if cls._instance is None:
            cls._instance = super().__new__(cls, *args, **kwargs)
        return cls._instance

    def __init__(self, *args, **kwargs):
        super().__init__([], [], [])



class DrmpSeqWithIdxs(DumpSeq):
    def __init__(self, all_types: list[str], has_value_mask: list[bool], values: list[Any], idxs: list[int]):
        super().__init__(all_types, has_value_mask, values)
        self.idxs = idxs

    @classmethod
    def from_dump_data(cls, all_types: list[str], dump_vals: list, global_idxs: list[int]):
        has_value_mask = [idx in global_idxs for idx in range(len(all_types))]
        assert len([x for x in has_value_mask if x]) == len(dump_vals)
        return cls(all_types, has_value_mask, dump_vals, global_idxs)

    def get_seq_on_idxs(self, idxs: list[int])->'DrmpSeqWithIdxs':
        cur_types = [self.all_types[i] for i in idxs]
        cur_has_value_mask = [self.has_value_mask[i] for i in idxs]
        cur_values = [self.values[i] for i in idxs]
        cur_idxs = [self.idxs[i] for i in idxs]
        return self.__class__(cur_types, cur_has_value_mask, cur_values, cur_idxs)



class DumpStack(DumpSeq): pass

class DumpLocal(DrmpSeqWithIdxs): pass

class DumpGlobal(DrmpSeqWithIdxs):  pass



def get_idxs_has_diff_vals(dump_seq1:DumpSeq, dump_seq2:DumpSeq)->list[int]:
    result = []
    val_offset1 = -1
    val_offset2 = -1
    # print('dump_seq1.all_types', dump_seq1.all_types, len(dump_seq1.all_types))
    # print('dump_seq1.values', dump_seq1.values, len(dump_seq1.values))
    # print('dump_seq2.all_types', dump_seq2.all_types, len(dump_seq2.all_types))
    # print('dump_seq2.values', dump_seq2.values, len(dump_seq2.values))
    # print('dump_seq1.has_value_mask', dump_seq1.has_value_mask, len(dump_seq1.has_value_mask), sum(dump_seq1.has_value_mask))
    # print('dump_seq2.has_value_mask', dump_seq2.has_value_mask, len(dump_seq2.has_value_mask), sum(dump_seq2.has_value_mask))
    # 
    print('len(dump_seq1.all_types), len(dump_seq1.has_value_mask), len(dump_seq1.values), sum(dump_seq1.has_value_mask)', len(dump_seq1.all_types), len(dump_seq1.has_value_mask), len(dump_seq1.values), sum(dump_seq1.has_value_mask))
    assert len(dump_seq1.all_types) == len(dump_seq1.has_value_mask)
    assert len(dump_seq1.values) == sum(dump_seq1.has_value_mask)
    # 
    for i in range(len(dump_seq1.all_types)):
        assert dump_seq1.has_value_mask[i] == dump_seq2.has_value_mask[i] 
        if dump_seq1.has_value_mask[i]:
            val_offset1 += 1
        if dump_seq2.has_value_mask[i]:
            val_offset2 += 1
        if dump_seq1.has_value_mask[i] and dump_seq2.has_value_mask[i]:
            if dump_seq1.values[val_offset1] != dump_seq2.values[val_offset2]:
                result.append(i)
                print('result', result, 'i', i, 'val_offset1', val_offset1, 'val_offset2', val_offset2)
    return result


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

def gen_one_dump_data(
    can_control_data: CanControlData,
    vals,
    probe_type: ProbeType,
    probe_idx: int
)->OneDumpData:
    if probe_type == ProbeType.STACK:
        dump_seq = DumpStack.from_dump_data(can_control_data.cur_stack, vals)
    elif probe_type == ProbeType.LOCAL:
        dump_seq = DumpLocal.from_dump_data(can_control_data.all_local_types, vals, can_control_data.local_idxs)
    elif probe_type == ProbeType.GLOBAL:
        dump_seq = DumpGlobal.from_dump_data(can_control_data.all_global_types, vals, can_control_data.mutable_global_idxs)
    elif probe_type == ProbeType.EXECUTED:
        dump_seq = EmptyDumpSeq()
    return OneDumpData(dump_seq, probe_type, probe_idx)

class DumpDataList(list):
    def __init__(self, dump_data_list: list[OneDumpData]):
        super().__init__(dump_data_list)  

    def __str__(self):
        return f'{self.__class__.__name__}(dump_data_list={super().__str__()})'

    def get_dump_data_with_probe_idx(self, probe_idx: int)->'DumpDataList':
        return DumpDataList([x for x in self if x.probe_idx == probe_idx])
    
    def get_dump_data_with_probe_type(self, probe_type: ProbeType)->'DumpDataList':
        raise NotImplementedError("Not implemented")

    def get_the_last_dump_data(self, probe_idx: int, probe_type: ProbeType)->Optional[OneDumpData]:
        for x in reversed(self):
            if x.probe_idx == probe_idx and x.probe_type == probe_type:
                return x
        return None

    def get_each_kind_last_dump_data(self, probe_idx: int)->dict[ProbeType, OneDumpData]:
        result = {}
        for x in reversed(self):
            if x.probe_idx == probe_idx:
                if x.probe_type not in result:
                    result[x.probe_type] = x
        return result

    def get_each_kind_first_dump_data(self, probe_idx: int)->dict[ProbeType, OneDumpData]:
        result = {}
        for x in self:
            if x.probe_idx == probe_idx:
                if x.probe_type not in result:
                    result[x.probe_type] = x
        return result
