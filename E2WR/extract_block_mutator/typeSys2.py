"""typeSys2 —— 四元组类型系统原型（M5 重建模，影子共存）。

类型 = (params, results, params_poly, results_poly)，记号 [t1*?]->[t2*?]：
  params_poly=True  表示真实参数在底部还可有任意内容（知识不完整/栈多态）；
  results_poly=True 表示真实结果在底部还可有任意内容。
列表方向从栈底到栈顶（与 wasm 惯例一致）。

统一映射（旧实现 → 四元组）：
  req_type='eq'                  → (P, R, F, F)
  req_type='eg_param_f'          → (P, R, T, F)     # br_if
  req_type='eg_param_and_result' → (P, R, T, T)     # br/return/br_table/ANY 态
  req_type='unreachable'         → ([], [], T, T)

与旧实现（funcType/typeReq/merge_req）的关系：影子共存、双实现对照，对照驱动见
e2wr-rs/tests/py_drivers/typesys2_driver.py；定稿后替换旧实现。
已知有意差异（对照中单独归类）：
  1. determined_return_ty 不再独立存在：其组合语义由 results_poly 覆盖，
     其"终结后本地栈视为空"的 StackState 投影差异逐条记录；
  2. 候选集合显式排序（旧 typeReq.tys 经 set 去重顺序不定，ty0 不确定）；
  3. select（无 immediate）按标准语义补建模为 [t t i32]->[t] 候选枚举
     （旧实现该路径为 None）；
  4. 'any' 通配不复存在（旧实现中疑似构造不出的死路径）。
"""
from dataclasses import dataclass
from enum import Enum
from typing import Optional


# 无 immediate 的 select 允许的数值/向量类型（引用类型须用 select t 形态）
SELECT_OPERAND_TYPES = ('f32', 'f64', 'i32', 'i64', 'v128')


@dataclass(frozen=True, order=True)
class FTy:
    """[params*?] -> [results*?]。列表从栈底到栈顶。

    terminal=True 表示该类型来自终结型指令（br/return/br_table，对应旧
    determined_return_ty=True）：其后的 compose 跳过消解（恒成功），
    参数取左侧、结果取右侧——语义是"右侧指令在不可达栈上执行"。
    """
    params: tuple
    results: tuple
    params_poly: bool = False
    results_poly: bool = False
    terminal: bool = False

    @classmethod
    def of(cls, params, results, params_poly=False, results_poly=False,
           terminal=False):
        return cls(tuple(params), tuple(results), params_poly, results_poly,
                   terminal)

    def is_unreachable(self) -> bool:
        return (not self.params and not self.results
                and self.params_poly and self.results_poly)


UNREACHABLE = FTy((), (), True, True, terminal=True)

# 旧 req_type → (params_poly, results_poly)
_REQ2POLY = {
    'eq': (False, False),
    'eg_param_f': (True, False),
    'eg_param_and_result': (True, True),
    'unreachable': (True, True),
}


def compose(a: FTy, b: FTy) -> Optional[FTy]:
    """a 之后执行 b 的复合类型；具体尾部失配且无多态覆盖时返回 None。

    规则（v2，与旧 _add_core1/_add_core2 及 merge_req special 分支对拍校准）：
    1. a.terminal=True（终结之后）：跳过消解恒成功，params=a.params、
       results=b.results（旧 special 分支 det 子路径的重建语义）；
    2. 消解：a.results 与 b.params 的重叠区（各自尾部 min(|R1|,|P2|) 个）
       逐位相等，失配 → None；
    3. b.params 有剩余前缀：a.results_poly=True 时被吞，否则进入复合参数
       （对应旧 special 分支 ANY 子路径的 final_param=val1.params）；
    4. a.results 有剩余前缀：b.terminal=True 时被吞（旧正常分支的
       val2.det / req2='unreachable' 重建），否则进入复合结果；
    5. 多态位按 or 传播；terminal 传播 = b.terminal（旧 det 传播 =
       val2.determined，由尾元素决定）。
    """
    if a.terminal:
        return FTy(a.params, b.results,
                   a.params_poly or b.params_poly,
                   a.results_poly or b.results_poly,
                   b.terminal)
    r1, p2 = a.results, b.params
    m = min(len(r1), len(p2))
    for i in range(1, m + 1):
        if r1[-i] != p2[-i]:
            return None
    if len(p2) > m:
        rest_p = () if a.results_poly else p2[:len(p2) - m]
    else:
        rest_p = ()
    if len(r1) > m:
        rest_r = () if b.terminal else r1[:len(r1) - m]
    else:
        rest_r = ()
    return FTy(rest_p + a.params, rest_r + b.results,
               a.params_poly or b.params_poly,
               a.results_poly or b.results_poly,
               b.terminal)


@dataclass(frozen=True)
class TR:
    """候选集合（对应旧 typeReq；req_type 维度并入候选的 poly 位）。"""
    cands: tuple  # 排序去重的 FTy 元组（显式定序，替代旧 set 无序 + 驻留工厂）

    def impossible(self) -> bool:
        return len(self.cands) == 0

    @property
    def ty0(self) -> FTy:
        return self.cands[0]

    def __repr__(self):
        return f'TR({list(self.cands)})'


def tr_of(*ftys: FTy) -> TR:
    return TR(tuple(sorted(set(ftys))))


def merge(tr1: TR, tr2: TR) -> TR:
    """候选笛卡尔积逐一 compose，失败淘汰（对应旧 merge_req）。"""
    out = set()
    for x in tr1.cands:
        for y in tr2.cands:
            c = compose(x, y)
            if c is not None:
                out.add(c)
    return TR(tuple(sorted(out)))


class REQRESULT2(Enum):
    UNMATCH = 1
    MATCH = 2


def match(fty: FTy, req: FTy) -> REQRESULT2:
    """fty（实际/候选类型）是否满足 req（需求）；req 的 poly 位 = 底部截尾语义。

    对应旧 check_ftype_match_req：eg_param_f = params 尾部匹配 + results 全等；
    eg_param_and_result = 双尾部匹配；unreachable 型需求 ((),(),T,T) 匹配一切。
    """
    if req.params_poly:
        if len(fty.params) < len(req.params):
            return REQRESULT2.UNMATCH
        if fty.params[len(fty.params) - len(req.params):] != req.params:
            return REQRESULT2.UNMATCH
    elif fty.params != req.params:
        return REQRESULT2.UNMATCH
    if req.results_poly:
        if len(fty.results) < len(req.results):
            return REQRESULT2.UNMATCH
        if fty.results[len(fty.results) - len(req.results):] != req.results:
            return REQRESULT2.UNMATCH
    elif fty.results != req.results:
        return REQRESULT2.UNMATCH
    return REQRESULT2.MATCH


def tr_from_old(old_req) -> TR:
    """旧 typeReq → TR（对拍桥；延迟 import 以保持本模块零依赖）。"""
    from .typeReq import typeReq  # noqa: F401  (类型标注用)
    pp, rp = _REQ2POLY[old_req.req_type]
    if old_req.req_type == 'unreachable':
        return TR((UNREACHABLE,))
    cands = {FTy(tuple(t.param_types), tuple(t.result_types), pp, rp,
                 t.determined_return_ty)
             for t in old_req.tys}
    return TR(tuple(sorted(cands)))


def get_inst_ty_req2(inst, context_info=None, cur_params=None) -> Optional[TR]:
    """指令的栈需求（新版入口；内部复用旧指令类型表，select 补标准建模）。"""
    from .InstUtil.InstReqUtil import get_inst_ty_req as _old_get_inst_ty_req
    if inst.opcode_text == 'select':
        # 旧路径：'any' 在 generate_spec_type_repr 处 ValueError → 描述缺失 → None。
        # 新版按标准语义：select : [t t i32] -> [t]，t ∈ 数值/向量类型。
        cands = tuple(sorted(FTy((t, t, 'i32'), (t,)) for t in SELECT_OPERAND_TYPES))
        if cur_params is None:
            return TR(cands)
        passed = tuple(
            c for c in cands
            if compose(FTy((), tuple(cur_params)), c) is not None)
        return TR(passed)
    old = _old_get_inst_ty_req(inst, context_info, cur_params)
    if old is None:
        return None
    return tr_from_old(old)


# ---- StackState 投影（对应旧 get_stack_state_from_type_req）----

def rest_types_view(tr: TR) -> list:
    """各候选的结果列表；全部 terminal（旧 tys[0].determined_return_ty）时 [[]]。"""
    if tr.cands and all(c.terminal for c in tr.cands):
        return [[]]
    return [list(c.results) for c in tr.cands]


def status_view(tr: TR) -> str:
    """'ANY'/'NORMAL'。旧映射 (F,F)/(T,F)→NORMAL、(T,T)/unreachable→ANY，
    即 status 仅由 results_poly 决定。"""
    return 'ANY' if any(c.results_poly for c in tr.cands) else 'NORMAL'
