"""typeReq —— 兼容壳（实际实现见 typeSys2.TR / compose / match）。

2026-09-22 类型系统重建模（B-2）：
- typeReq = 候选集合 TR 的包装；req_type 四值不再是独立维度，而是候选
  poly 位的视图（构造时按 req_type 展开 poly，读取时按 poly 反推）；
- merge_req = typeSys2.merge（笛卡尔积 compose，失败淘汰），
  旧 special 分支的对象重建逻辑全部消失；
- check_ftype_match_req = typeSys2.match（req 的 poly 位 = 底部截尾语义；
  unreachable 型需求 ((),(),T,T) 匹配一切 —— 修复旧实现未定义分支的
  意外严格行为，与 CP9201 tests/test_check_ftype_match_req.py 的期望一致）；
- 候选集合显式按值定序（旧 list(set(...)) 顺序不定，ty0 不确定）；
- _add_core1 的 val1.determined 分支（'eq'+det=True 组合）为旧死路径，
  不复刻。
"""
from enum import Enum

from .funcType import funcType
from .typeSys2 import FTy, TR, UNREACHABLE, match as _match, merge as _merge


class REQRESULT(Enum):
    UNMATCH = 1
    MATCH = 2
    UNKNOWN = 3

    def matched(self):
        return self == REQRESULT.MATCH


_REQ2POLY = {
    'eq': (False, False),
    'eg_param_f': (True, False),
    'eg_param_and_result': (True, True),
    'unreachable': (True, True),
}


def _poly2req(tr: TR) -> str:
    """poly 位反推 req_type 视图（仅展示/兼容；判定面等价已验证）。"""
    if not tr.cands:
        return 'eq'
    c = tr.cands[0]
    if not c.params_poly:
        return 'eq'
    if not c.results_poly:
        return 'eg_param_f'
    if not c.params and not c.results and c.terminal:
        return 'unreachable'
    return 'eg_param_and_result'


class typeReq:
    __slots__ = ('_tr',)

    def __init__(self, tys, req_type='eq'):
        assert req_type in _REQ2POLY, req_type
        if req_type == 'unreachable':
            self._tr = TR((UNREACHABLE,))
            return
        pp, rp = _REQ2POLY[req_type]
        cands = {FTy(tuple(t.param_types), tuple(t.result_types), pp, rp,
                     t.determined_return_ty) for t in tys}
        self._tr = TR(tuple(sorted(cands)))

    @classmethod
    def _from_tr(cls, tr: TR):
        obj = cls.__new__(cls)
        obj._tr = tr
        return obj

    @classmethod
    def from_one_ty(cls, ty: funcType, req_type='eq'):
        return cls([ty], req_type)

    @property
    def tr(self) -> TR:
        return self._tr

    @property
    def tys(self):
        return [funcType(c.params, c.results, c.terminal) for c in self._tr.cands]

    @property
    def ty0(self) -> funcType:
        c = self._tr.cands[0]
        return funcType(c.params, c.results, c.terminal)

    @property
    def req_type(self) -> str:
        return _poly2req(self._tr)

    def impossible(self):
        return self._tr.impossible()

    def __eq__(self, other):
        if not isinstance(other, typeReq):
            return False
        return self._tr == other._tr

    def __hash__(self):
        return hash(self._tr)

    def __repr__(self):
        return f'typeReq(tys={self.tys}, req_type={self.req_type})'


def merge_req(req1: typeReq, req2: typeReq) -> typeReq:
    return typeReq._from_tr(_merge(req1.tr, req2.tr))


def check_ftype_match_req(fty: funcType, req: typeReq):
    if req is None:
        return REQRESULT.UNKNOWN
    for cand in req.tr.cands:
        if _match(fty.fty, cand) is not None and _match(fty.fty, cand).name == 'MATCH':
            return REQRESULT.MATCH
    return REQRESULT.UNMATCH
