"""funcTypeFactory —— 薄壳。

2026-09-22 类型系统重建模（B-2）：FTy 为 frozen 值类型（自带 hash/eq），
预生成 ≤4 元组合的驻留字典不再必要，整体删除。
"""


class funcTypeFactory:
    @staticmethod
    def generate_one_func_type_default(param_type, result_type,
                                       determined_return_ty=False):
        from .funcType import funcType
        return funcType(param_type, result_type, determined_return_ty)
