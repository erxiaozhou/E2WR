from extract_block_mutator.InstUtil.Inst import Inst


class InstSeqWithName:
    def __init__(self, name2insts: dict[str, list[Inst]], expected_seq: list[str]):
        self.name2insts = name2insts
        self.expected_seq = expected_seq
    
    def get_insts(self) -> list[Inst]:
        insts = []
        for name in self.expected_seq:
            insts.extend(self.name2insts[name])
        return insts
    
    def insert_insts(self, insts: list[Inst], name:str, pos:int)->None:
        self.expected_seq.insert(pos, name)
        self.name2insts[name] = insts
        
    def replace_insts(self, insts: list[Inst], name:str)->None:
        self.name2insts[name] = insts
