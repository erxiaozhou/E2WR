from extract_block_mutator.InstUtil.Inst import Inst
from extract_block_mutator.encode.NGDataPayload import DataPayloadwithName
from extract_block_mutator.encode.byte_define.SectionPart import InstD, LocalsPart
from util.encoding_util import read_next_leb_num
from typing import Any, Optional, Protocol
import leb128
from WasmInfoCfg import SectionType
from extract_block_mutator.encode.byte_define.DecoderID import DecoderMemory


SECTION_NAME_TO_TYPE = {
    'type': SectionType.Type,
    'function': SectionType.Function,
    'import': SectionType.Import,
    'table': SectionType.Table,
    'memory': SectionType.Memory,
    'global': SectionType.Global,
    'export': SectionType.Export,
    'start': SectionType.Start,
    'element': SectionType.Element,
    'code': SectionType.Code,
    'data': SectionType.Data,
    'data_count': SectionType.DataCount,
    'custom': SectionType.Custom
}
TYPE_TO_SECTION_NAME = {v: k for k, v in SECTION_NAME_TO_TYPE.items()}

definition_decoder_map = {
    'type': 'functype',
    'import': 'import',
    'table': 'table',
    'memory': 'memory',
    'global': 'global',
    'function': 'u32',
    'export': 'export',
    'element': 'element_segment',
    'data': 'data',
    'code': 'Func',
    'data_count': 'u32',
    'start': 'u32',
}


def _get_definition_decoder(sec_name: str):
    decoder_name = definition_decoder_map.get(sec_name)
    if not decoder_name:
        raise ValueError(f"Unsupported section type: {sec_name}")

    return DecoderMemory.get_decoder(decoder_name)

class MutationPlan(Protocol): pass

class FuncInstMutation(MutationPlan):
    def __init__(
        self,
        func_idx: int,
        start_offset: int,
        end_offset: int,
        new_insts: list[Inst]
    ):
        self.func_idx = func_idx
        self.start_offset = start_offset
        self.end_offset = end_offset
        inst_bas = []
        for inst in new_insts:
            inst_bas.append(InstD.encode(inst))
        self.new_insts: list[Inst] = new_insts
        self.new_insts_ba = inst_bas

    def __str__(self):
        return f'{self.__class__.__name__}(func_idx={self.func_idx}, start_offset={self.start_offset}, end_offset={self.end_offset}, new_insts={self.new_insts})'

    def __repr__(self):
        return self.__str__()


class RewriteLocalDesc(MutationPlan):
    def __init__(
        self,
        func_idx: int,
        new_defined_locals: list[str]
    ):
        self.func_idx = func_idx
        self.new_defined_locals = new_defined_locals

    def __str__(self):
        return f'{self.__class__.__name__}(func_idx={self.func_idx}, new_defined_locals={self.new_defined_locals})'

    def __repr__(self):
        return self.__str__()

    def encode(self) -> bytearray:
        return LocalsPart.encode(self._get_locals_with_def_repr()) # type: ignore

    def _get_locals_with_def_repr(self):
        local_type_num = []
        cur_local = None
        for local_type in self.new_defined_locals:
            if local_type == cur_local:
                local_type_num[-1][1] += 1
            else:
                cur_local = local_type
                local_type_num.append([local_type, 1])
        local_defs = []
        for local_type, count in local_type_num:
            local_def = DataPayloadwithName(
                data={
                    'value_type': local_type,
                    'count': count
                },
                name='locals'
            )
            local_defs.append(local_def)
        return local_defs


class DefinitionMutation(MutationPlan):
    def __init__(
        self,
        sec_type: SectionType,
        start_offset: int,
        end_offset: int,
        new_definitions: list[Any]
    ):
        self.sec_type = sec_type
        self.start_offset = start_offset
        self.end_offset = end_offset
        new_ba = []
        # decoder = _get_definition_decoder(definition_decoder_map[TYPE_TO_SECTION_NAME[sec_type]])
        decoder = _get_definition_decoder(TYPE_TO_SECTION_NAME[sec_type])
        for new_definition in new_definitions:
            if new_definition is None:
                continue
            # print('new_definition', new_definition)
            # print('decoder', decoder)
            # print('new_definition.name', new_definition.inner_name)
            # print('new_definition.data', new_definition.data)
            new_ba.append(decoder.encode(new_definition))
        self.new_definitions = new_ba
        self.raw_new_definitions = new_definitions

    def __str__(self):
        return f'DefinitionMutation(sec_type={self.sec_type}, start_offset={self.start_offset}, end_offset={self.end_offset}, new_definitions={self.new_definitions})'

    def __repr__(self):
        return self.__str__()

    def get_scope(self):
        return self.start_offset, self.end_offset

    @property
    def is_delete(self):
        return len(self.new_definitions) == 0

    @property
    def is_delete_one(self):
        return self.is_delete and self.end_offset - self.start_offset == 1

    @property
    def is_one2one_replace(self):
        return len(self.new_definitions) == 1 and self.end_offset - self.start_offset == 1


class MutationBatch(MutationPlan):
    def __init__(
        self,
        definition_mutations: Optional[list[DefinitionMutation]] = None,
        func_inst_mutations: Optional[list[FuncInstMutation]] = None,
        rewrite_local_descs: Optional[list[RewriteLocalDesc]] = None,
    ):
        if definition_mutations is None:
            definition_mutations = []
        if func_inst_mutations is None:
            func_inst_mutations = []
        if rewrite_local_descs is None:
            rewrite_local_descs = []
        self.definition_mutations = definition_mutations
        self.func_inst_mutations = func_inst_mutations
        self.rewrite_local_descs = rewrite_local_descs


# def merge_code_insts_removal(
#     definition_mutations: list[DefinitionMutation],
#     func_inst_mutations:list[FuncInstMutation]
# ):
    
