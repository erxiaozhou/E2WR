"""funcType —— 兼容壳（实际实现见 typeSys2.FTy）。

2026-09-22 类型系统重建模（B-2）：funcType 退化为四元组+terminal 值类型的
包装，旧的 _add_core1/_add_core2 尾部消解特判由 typeSys2.compose 统一。
对外保持原属性面：param_types/result_types（list 视图）、determined_return_ty
（映射 terminal 位）、__add__（消解失败仍抛 FuncTypeCatException）。
'any' 通配为旧死路径（构造校验本就拒绝），不复刻。
"""
from .typeSys2 import FTy, compose


byte_val2type_str = {
    0x7F: 'i32',
    0x7E: 'i64',
    0x7D: 'f32',
    0x7C: 'f64',
    0x7B: 'v128',
    0x70: 'funcref',
    0x6F: 'externref'
}
type_str2byte_val = {
    'i32': 0x7F,
    'i64': 0x7E,
    'f32': 0x7D,
    'f64': 0x7C,
    'v128': 0x7B,
    'funcref': 0x70,
    'externref': 0x6F
}


class FuncTypeCatException(Exception):
    pass


import re
import leb128

func_type_pattern = re.compile(
    r'^(?:\(param\s*([^\)]*?)\))?\s*(?:\(result\s*([^\)]*?)\))?$')


class funcType:
    __slots__ = ('fty',)

    def __init__(self, param_types, result_types,
                 determined_return_ty: bool = False) -> None:
        if not isinstance(param_types, (list, tuple)):
            param_types = list(param_types)
        if not isinstance(result_types, (list, tuple)):
            result_types = list(result_types)
        from WasmInfoCfg import val_type_strs_list
        for ty in param_types:
            if ty not in val_type_strs_list:
                raise Exception(f'{ty} not in {val_type_strs_list}')
        for ty in result_types:
            if ty not in val_type_strs_list:
                raise Exception(f'{ty} not in {val_type_strs_list}')
        # funcType 恒非多态（多态位只属于 typeReq 需求侧）
        self.fty = FTy(tuple(param_types), tuple(result_types),
                       False, False, bool(determined_return_ty))

    @property
    def param_types(self):
        return list(self.fty.params)

    @property
    def result_types(self):
        return list(self.fty.results)

    @property
    def determined_return_ty(self):
        return self.fty.terminal

    @classmethod
    def from_strs(cls, param_types, result_types):
        return cls(param_types, result_types)

    def __add__(self, other):
        c = compose(self.fty, other.fty)
        if c is None:
            raise FuncTypeCatException('param2 does not match result1')
        return funcType(c.params, c.results, c.terminal)

    def __repr__(self):
        return (f'{self.__class__.__name__}'
                f'({self.param_types}, {self.result_types})')

    def __eq__(self, other):
        if self is other:
            return True
        if not isinstance(other, funcType):
            return NotImplemented
        return self.fty == other.fty

    def __hash__(self):
        return hash(self.fty)

    def copy(self):
        return funcType(self.fty.params, self.fty.results, self.fty.terminal)

    @property
    def as_bytes(self):
        r = bytearray([0x60])
        param_byte_vals = [type_str2byte_val[ty] for ty in self.fty.params]
        param_ba = leb128.u.encode(len(param_byte_vals)) + bytearray(param_byte_vals)
        result_byte_vals = [type_str2byte_val[ty] for ty in self.fty.results]
        result_ba = leb128.u.encode(len(result_byte_vals)) + bytearray(result_byte_vals)
        r.extend(param_ba)
        r.extend(result_ba)
        return r

    @classmethod
    def from_dict(cls, d):
        return cls(d['param'], d['result'])

    @classmethod
    def from_str(cls, s: str):
        s = str(s).split(';;')[0].strip()
        r = func_type_pattern.findall(s)
        if len(r) == 0:
            raise Exception(f'cannot parse funcType from {s}')
        param_types, result_types = r[0]
        param_types = [_ for _ in param_types.split(' ') if _ != '']
        result_types = [_ for _ in result_types.split(' ') if _ != '']
        return cls(param_types, result_types)


def match_func_type(fty1: funcType, fty2: funcType):
    """精确相等判定（旧实现去掉 'any' 通配后的语义；旧调用方仅为
    check_ftype_match_req 内部，已由 typeSys2.match 取代）。"""
    return fty1.fty.params == fty2.fty.params \
        and fty1.fty.results == fty2.fty.results
