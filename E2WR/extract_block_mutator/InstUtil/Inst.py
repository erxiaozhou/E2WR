from typing import Any
from util.util import AbstractMethodException

class Inst:
    opcode_text:str
    imm_part:Any
    def __init__(self) -> None:
        raise AbstractMethodException

    def get_imm_by_ph(self, imm_ph_repr:str):
        raise AbstractMethodException

    def __eq__(self, o: object) -> bool:
        raise AbstractMethodException

    def __hash__(self) -> int:
        raise AbstractMethodException

    def copy(self):
        raise DeprecationWarning('To remove')


class NoImmInst(Inst):
    def __init__(self, opcode_text:str) -> None:
        self.opcode_text = opcode_text

    def copy(self):
        return self

    

    def __eq__(self, o: object) -> bool:
        if not isinstance(o, Inst):
            return False
        return self.opcode_text == o.opcode_text
    
    def __repr__(self) -> str:
        return f'NoImmInst({self.opcode_text})'

    def __hash__(self) -> int:
        return hash(self.opcode_text)



blocktype_ops = {'block', 'loop', 'if'}
def imm_is_blocktpye(inst:Inst):
    if inst.opcode_text not in blocktype_ops:
        return False
    return True
