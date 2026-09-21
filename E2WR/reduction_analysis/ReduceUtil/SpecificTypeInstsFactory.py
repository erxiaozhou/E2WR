from extract_block_mutator.InstUtil import Inst
from extract_block_mutator.funcType import funcType
from .NonInstrumentationInstStrategy import GenSpecificType


class SpecificTypeInstsFactory:
    def __init__(self) -> None:
        self._type2code_snippet = {}
        # self.stable_types:set[funcType] = set()
        self.not_sure_types: set[funcType] = set()

    def update(self, success_: bool):
        if success_:
            # self.stable_types.update(self.not_sure_types)
            self.not_sure_types.clear()
        else:
            for k in self.not_sure_types:
                self._type2code_snippet.pop(k, None)
            # self.not_sure_types.update(self.stable_types)
            # self.stable_types.clear()

    def get_code_snippet(self, expected_type: funcType) -> list[Inst]:
        if expected_type not in self._type2code_snippet:
            self.not_sure_types.add(expected_type)
            specific_type_gen_insts = GenSpecificType(
                expected_type).get_insts_for_replace()
            self._type2code_snippet[expected_type] = specific_type_gen_insts
        return self._type2code_snippet[expected_type].copy()
