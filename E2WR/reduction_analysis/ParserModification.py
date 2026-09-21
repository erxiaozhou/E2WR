from pathlib import Path

from extract_block_mutator.WasmParser import WasmParser
from util.encoding_util import read_next_leb_num
from util.prepare_template import  seq_encode_seq, sec_name2_id
from typing import Optional, Protocol, Union
import leb128
from WasmInfoCfg import SectionType
from util.prepare_template import prepare_sec_name2all_ba
from extract_block_mutator.encode.byte_define.SectionPart import section_decoder
from singleton_decorator import singleton
from .ParserModificationUtil import MutationBatch, MutationPlan, _get_definition_decoder
from .ParserModificationUtil import  DefinitionMutation
from .ParserModificationUtil import RewriteLocalDesc
from .ParserModificationUtil import FuncInstMutation
from .ParserModificationUtil import SECTION_NAME_TO_TYPE
from extract_block_mutator.encode.byte_define.SectionPart import InstD, LocalsPart

class FuncBAParts:
    def __init__(
        self,
        local_part_ba: memoryview,
        inst_part_ba: list[memoryview],
    ):
        self.local_part_ba = local_part_ba
        self.inst_part_ba = inst_part_ba

    def copy(self):
        return FuncBAParts(
            local_part_ba=self.local_part_ba,
            inst_part_ba=self.inst_part_ba.copy()
        )

    @classmethod
    def from_ba(cls, ba: memoryview):
        size, offset = read_next_leb_num(ba, 0)
        max_offset = size + offset
        cur_offset = offset

        raw_locals, local_offset = LocalsPart.byte_decode_and_generate(
            ba[cur_offset:])
        local_part = ba[cur_offset:cur_offset+local_offset]
        cur_offset += local_offset
        inst_part_ba = []

        no_end_insts_max_size = max_offset - 1  # -1 for the inst `end`
        while cur_offset < no_end_insts_max_size:
            inst, offset = InstD.byte_decode_and_generate(ba[cur_offset:])
            inst_part_ba.append(ba[cur_offset:cur_offset+offset])
            cur_offset += offset
        return cls(local_part, inst_part_ba)

    def apply_one_mutation(self,
                           mutation: FuncInstMutation
                           ):
        self.inst_part_ba[mutation.start_offset:mutation.end_offset] = mutation.new_insts_ba

    def apply_mutations(self,
                        mutations: list[FuncInstMutation]
                        ):
        sorted_mutations = sorted(
            mutations, key=lambda m: m.start_offset, reverse=True)
        for mutation in sorted_mutations:
            self.apply_one_mutation(mutation)

    def encode(self):
        corecore_core_lt = bytearray()
        corecore_core_lt.extend(self.local_part_ba)
        for inst_ba in self.inst_part_ba:
            corecore_core_lt.extend(inst_ba)
        corecore_core_lt.append(0x0b)
        core_len = len(corecore_core_lt)
        size_ba = leb128.u.encode(core_len)
        return size_ba + corecore_core_lt


def _extract_original_definition_bytes(sec_ba: bytes, sec_name: str) -> list[memoryview]:
    vec_sections = {'type', 'import', 'table', 'memory', 'global', 'export', 'element', 'code', 'data', 'function'}
    read_num, offset = read_next_leb_num(sec_ba[1:], 0)
    sec_ba = sec_ba[1+offset:]
    if sec_name not in vec_sections:
        return [memoryview(sec_ba)]
    # skip the section id and length

    return _extract_vec_definition_bytes(sec_ba, sec_name)

def _extract_vec_definition_bytes(sec_ba: bytes, sec_name: str) -> list[memoryview]:
    definition_bytes = []
    
    offset = 0
    def_count, offset = read_next_leb_num(sec_ba, offset)
    
    sub_decoder = _get_definition_decoder(sec_name)
    if sub_decoder is None:
        raise ValueError(f"Cannot get definition decoder for {sec_name}")
    
    for idx in range(def_count):
        start_offset = offset
        _, cur_offset = sub_decoder.byte_decode_and_generate(memoryview(sec_ba[offset:]))
        end_offset = offset + cur_offset
        def_bytes = sec_ba[start_offset:end_offset]
        definition_bytes.append(memoryview(def_bytes))
        offset = end_offset
    
    return definition_bytes


class WMSnapshot:
    def __init__(self,
                 section_original_bytes: dict[SectionType, memoryview],
                 definition_original_bytes: dict[SectionType, list[memoryview]],
                 func_bas:list[FuncBAParts],
                 parser: WasmParser,
                 mutable_sections:Optional[set[SectionType]]=None
                 ):
        self.section_original_bytes = section_original_bytes
        self.definition_original_bytes = definition_original_bytes
        self.func_bas = func_bas
        self.parser = parser
        if mutable_sections is None:
            mutable_sections = set(list(SectionType))
        self.mutable_sections = mutable_sections

    def set_mutable_sections(self, mutable_sections:set[SectionType]):
        self.mutable_sections = mutable_sections

    @classmethod
    def from_path(cls, wasm_path:Union[str, Path], mutable_sections:Optional[set[SectionType]]=None):
        section_original_bytes: dict[SectionType, memoryview] = {}
        definition_original_bytes: dict[SectionType, list[memoryview]] = {}
        func_bas:list[FuncBAParts]=[]
    
        types = None
        imports = None
        defined_func_ty_ids = None
        defined_table_datas = None
        defined_memory_datas = None
        defined_globals = None
        exports = None
        start_sec_data = None
        elem_sec_datas = None
        defined_funcs = None
        data_sec_datas = None
        data_count_sec_data = None
        customs = []
        
        sec_name2all_ba = prepare_sec_name2all_ba(wasm_path)
        
        for sec_name, sec_ba in sec_name2all_ba.items():
            if sec_name == 'pre':  
                continue
            sec_type = SECTION_NAME_TO_TYPE[sec_name]
            
            if sec_name == 'type':
                types = section_decoder.byte_decode_and_generate(memoryview(sec_ba))[0]
                section_original_bytes[sec_type] = memoryview(sec_ba)
                definition_original_bytes[sec_type] = _extract_original_definition_bytes(sec_ba, sec_name)
            elif sec_name == 'import':
                imports = section_decoder.byte_decode_and_generate(memoryview(sec_ba))[0]
                section_original_bytes[sec_type] = memoryview(sec_ba)
                definition_original_bytes[sec_type] = _extract_original_definition_bytes(sec_ba, sec_name)
            elif sec_name == 'function':
                defined_func_ty_ids = section_decoder.byte_decode_and_generate(memoryview(sec_ba))[0]
                section_original_bytes[sec_type] = memoryview(sec_ba)
                definition_original_bytes[sec_type] = _extract_original_definition_bytes(sec_ba, sec_name)
            elif sec_name == 'table':
                defined_table_datas = section_decoder.byte_decode_and_generate(memoryview(sec_ba))[0]
                section_original_bytes[sec_type] = memoryview(sec_ba)
                definition_original_bytes[sec_type] = _extract_original_definition_bytes(sec_ba, sec_name)
            elif sec_name == 'memory':
                defined_memory_datas = section_decoder.byte_decode_and_generate(memoryview(sec_ba))[0]
                section_original_bytes[sec_type] = memoryview(sec_ba)
                definition_original_bytes[sec_type] = _extract_original_definition_bytes(sec_ba, sec_name)
            elif sec_name == 'global':
                defined_globals = section_decoder.byte_decode_and_generate(memoryview(sec_ba))[0]
                section_original_bytes[sec_type] = memoryview(sec_ba)
                definition_original_bytes[sec_type] = _extract_original_definition_bytes(sec_ba, sec_name)
            elif sec_name == 'export':
                exports = section_decoder.byte_decode_and_generate(memoryview(sec_ba))[0]
                section_original_bytes[sec_type] = memoryview(sec_ba)
                definition_original_bytes[sec_type] = _extract_original_definition_bytes(sec_ba, sec_name)
            elif sec_name == 'start':
                start_sec_data = section_decoder.byte_decode_and_generate(memoryview(sec_ba))[0]
                section_original_bytes[sec_type] = memoryview(sec_ba)
                definition_original_bytes[sec_type] = _extract_original_definition_bytes(sec_ba, sec_name)
            elif sec_name == 'element':
                elem_sec_datas = section_decoder.byte_decode_and_generate(memoryview(sec_ba))[0]
                section_original_bytes[sec_type] = memoryview(sec_ba)
                definition_original_bytes[sec_type] = _extract_original_definition_bytes(sec_ba, sec_name)
            elif sec_name == 'code':
                defined_funcs = section_decoder.byte_decode_and_generate(memoryview(sec_ba))[0]
                section_original_bytes[sec_type] = memoryview(sec_ba)
                definition_original_bytes[sec_type] = _extract_original_definition_bytes(sec_ba, sec_name)
                for raw_ba in definition_original_bytes[SectionType.Code]:
                    func_bas.append(FuncBAParts.from_ba(raw_ba))
            elif sec_name == 'data':
                data_sec_datas = section_decoder.byte_decode_and_generate(memoryview(sec_ba))[0]
                section_original_bytes[sec_type] = memoryview(sec_ba)
                definition_original_bytes[sec_type] = _extract_original_definition_bytes(sec_ba, sec_name)
            elif sec_name == 'data_count':
                data_count_sec_data = section_decoder.byte_decode_and_generate(memoryview(sec_ba))[0]
                section_original_bytes[sec_type] = memoryview(sec_ba)
                definition_original_bytes[sec_type] = _extract_original_definition_bytes(sec_ba, sec_name)
            elif sec_name == 'custom':
                customs.append(section_decoder.byte_decode_and_generate(memoryview(sec_ba))[0])
                section_original_bytes[sec_type] = memoryview(sec_ba)
                definition_original_bytes[sec_type] = _extract_original_definition_bytes(sec_ba, sec_name)
        
        parser =  WasmParser(
            types=types,
            imports=imports,
            defined_func_ty_ids=defined_func_ty_ids,
            defined_table_datas=defined_table_datas,
            defined_memory_datas=defined_memory_datas,
            defined_globals=defined_globals,
            exports=exports,
            start_sec_data=start_sec_data,
            elem_sec_datas=elem_sec_datas,
            defined_funcs=defined_funcs,
            data_sec_datas=data_sec_datas,
            data_count_sec_data=data_count_sec_data,
            customs=customs
        )
        return cls(
            section_original_bytes=section_original_bytes,
            definition_original_bytes=definition_original_bytes,
            func_bas=func_bas,
            parser=parser,
            mutable_sections=mutable_sections
        )
        

    def copy(self):
        if SectionType.Code in self.mutable_sections:
            func_bas=[func_ba.copy() for func_ba in self.func_bas]
        else:
            func_bas = self.func_bas
        return self.__class__(
            section_original_bytes=self.section_original_bytes.copy(),
            definition_original_bytes={
                sec_type: [mv for mv in mv_list] 
                for sec_type, mv_list in self.definition_original_bytes.items()
            },
            func_bas=func_bas,
            parser=self.parser
        )

    def copy_and_reset_mutable_sections(self, mutable_sections):
        new_snapshot = self.copy()
        
        if mutable_sections is None:
            mutable_sections = set(list(SectionType))
        new_snapshot.mutable_sections = mutable_sections
        return new_snapshot

    def copy_for_code_mutations(self, func_idxs_to_mutate: set[int]):
        new_func_bas = list(self.func_bas)
        for idx in func_idxs_to_mutate:
            new_func_bas[idx] = new_func_bas[idx].copy()
        return self.__class__(
            section_original_bytes=self.section_original_bytes.copy(),
            definition_original_bytes={
                sec_type: [mv for mv in mv_list]
                for sec_type, mv_list in self.definition_original_bytes.items()
            },
            func_bas=new_func_bas,
            parser=self.parser
        )



class Encoder:
    def commit_modified_sections(self, snapshot: WMSnapshot, modified_sections:set[SectionType]):
        for sec_name in seq_encode_seq:
            sec_type = SECTION_NAME_TO_TYPE[sec_name]
            if sec_type in modified_sections:
                definitions = snapshot.definition_original_bytes.get(sec_type)
                if definitions is None:
                    continue
                if sec_name == 'custom':
                    combined = bytearray()
                    for definition in definitions:
                        section_bytes = self._encode_section(sec_name, [definition])
                        if section_bytes:
                            combined.extend(section_bytes)
                    snapshot.section_original_bytes[sec_type] = memoryview(bytes(combined))
                else:
                    section_bytes = self._encode_section(sec_name, definitions)
                    snapshot.section_original_bytes[sec_type] = memoryview(section_bytes)
                    
    def encode_without_mutation(self, snapshot: WMSnapshot, output_file:Union[str, Path]):
        output_file = str(output_file)
        with open(output_file, 'wb') as f:
            f.write(b'\0asm\1\0\0\0')
            
            for sec_name in seq_encode_seq:
                sec_type = SECTION_NAME_TO_TYPE[sec_name]
                
                definitions = snapshot.definition_original_bytes.get(sec_type)
                if definitions is None \
                    or( isinstance(definitions, list) and len(definitions) == 0):
                    continue
                original_section_bytes = snapshot.section_original_bytes.get(sec_type)
                if original_section_bytes:
                    f.write(original_section_bytes)

    def encode_v2(self, snapshot: WMSnapshot, modified_sections:set[SectionType],output_file:Union[str, Path]):
        self.commit_modified_sections(snapshot, modified_sections)
        self.encode_without_mutation(snapshot, output_file)
    
    def encode(self, snapshot, modified_sections:set[SectionType], output_file:Union[str, Path]):
        output_file = str(output_file)
        with open(output_file, 'wb') as f:
            f.write(b'\0asm\1\0\0\0')
            
            for sec_name in seq_encode_seq:
                sec_type = SECTION_NAME_TO_TYPE[sec_name]
                
                definitions = snapshot.definition_original_bytes.get(sec_type)
                if definitions is None \
                    or( isinstance(definitions, list) and len(definitions) == 0):
                    continue
                if sec_type not in modified_sections:
                    original_section_bytes = snapshot.section_original_bytes.get(sec_type)
                    if original_section_bytes:
                        f.write(original_section_bytes)
                else:
                    if sec_name == 'custom':
                        for definition in definitions:
                            section_bytes = self._encode_section(sec_name, [definition])
                            snapshot.section_original_bytes[sec_type] = memoryview(section_bytes)
                            if section_bytes:
                                f.write(section_bytes)
                    else:
                        section_bytes = self._encode_section(sec_name, definitions)
                        snapshot.section_original_bytes[sec_type] = memoryview(section_bytes)
                        if section_bytes:
                            f.write(section_bytes)

    def _encode_section(self, sec_name: str, definitions: list[memoryview]) -> bytes:
        if not definitions:
            return b''
        
        if len(definitions) == 1 and sec_name in ['start', 'data_count']:
            content = bytes(definitions[0])
        else:
            content = bytearray()
            if sec_name not in ['start', 'data_count']:
                content.extend(leb128.u.encode(len(definitions)))
            
            for definition in definitions:
                content.extend(definition)
            content = bytes(content)
        
        section_id = sec_name2_id[sec_name]
        result = bytearray()
        result.extend(leb128.u.encode(section_id))
        result.extend(leb128.u.encode(len(content)))
        result.extend(content)
        
        return bytes(result)




class MutationApplierProtocol(Protocol):
    def apply(self, snapshot: WMSnapshot, plan: MutationPlan) -> WMSnapshot:
        ...


class GeneralMutationApplier():
    def apply_mutations_on_copy(self, 
                                    snapshot: WMSnapshot,
                                    mutation_batch: MutationBatch
                        ) -> tuple[WMSnapshot, set[SectionType]]:
        new_snapshot = snapshot.copy()
        modified_sections = self.apply_mutations_on_snapshot(
            new_snapshot,
            mutation_batch
        )
        return new_snapshot, modified_sections

    def apply_mutations_on_snapshot(self, 
                                    snapshot: WMSnapshot,
                                    mutation_batch: MutationBatch
                        ) -> set[SectionType]:
        self._update_code_definitions_using_func_ba(snapshot, mutation_batch)

        sec_type2mutations = {}

        for m in mutation_batch.definition_mutations:
            sec_type2mutations.setdefault(m.sec_type, []).append(m)
        # 
        for sec_type, mutations in sec_type2mutations.items():
            original_definitions = snapshot.definition_original_bytes.get(sec_type)
            
            new_definitions = original_definitions if original_definitions is not None else []
            
            for mutation in sort_and_clear_section_mutations(mutations):
                new_definitions[mutation.start_offset:mutation.end_offset] = mutation.new_definitions
                if sec_type == SectionType.Code:
                    self._update_func_bas_from_definitions(snapshot, mutation)
                
            if isinstance(new_definitions, list) and len(new_definitions) == 0 and original_definitions is None:
                continue
            snapshot.definition_original_bytes[sec_type] = new_definitions
      
        modified_sections = self._get_modified_sections(mutation_batch)
        has_modified_datacount = self._process_datacount_section(
            snapshot,
            mutation_batch,
        )
        if has_modified_datacount:
            modified_sections.add(SectionType.DataCount)
        return modified_sections

    def _update_func_bas_from_definitions(self, snapshot, mutation):
        func_bas = []
        for func_bytes  in mutation.new_definitions:
            func_bas.append(FuncBAParts.from_ba(func_bytes))
        snapshot.func_bas[mutation.start_offset:mutation.end_offset] = func_bas

    def _update_code_definitions_using_func_ba(self, snapshot, mutation_batch):
        if SectionType.Code in snapshot.definition_original_bytes:
            code_definitions = snapshot.definition_original_bytes[SectionType.Code]
            func_idx2inst_mutations: dict[int, list[FuncInstMutation]]= {}
            func_idx2local_descs: dict[int, RewriteLocalDesc] = {}
            for local_mutation in mutation_batch.rewrite_local_descs:
                func_idx2local_descs[local_mutation.func_idx] = local_mutation
            for m in mutation_batch.func_inst_mutations:
                func_idx2inst_mutations.setdefault(m.func_idx, []).append(m)
            mutated_func_idxs = set(func_idx2inst_mutations.keys()) | set(func_idx2local_descs.keys())
            for func_idx in mutated_func_idxs:
                func_parts:FuncBAParts = snapshot.func_bas[func_idx]
                if func_idx in func_idx2inst_mutations:
                    # 
                    # _func_inst_mutations: list[FuncInstMutation] = func_idx2inst_mutations[func_idx]
                    # # start_inst_idx = 
                    # for m in _func_inst_mutations:
                    #     # assert m.func_idx == func_idx
                    #     start_idx = m.start_offset
                    #     end_idx = m.end_offset
                    #     print('==============================')
                    #     print((snapshot.parser.defined_funcs[func_idx].insts[start_idx:end_idx]))
                    # 
                    func_parts.apply_mutations(func_idx2inst_mutations[func_idx])
                if func_idx in func_idx2local_descs:
                    new_local_bytes = func_idx2local_descs[func_idx].encode()
                    func_parts.local_part_ba = memoryview(new_local_bytes) # type: ignore
                code_definitions[func_idx] = memoryview(func_parts.encode())
    def _process_datacount_section(
        self,
        snapshot: WMSnapshot,
        mutation_batch: MutationBatch,
    ):
        modified_data_count_section = False
        data_seg_num = len(snapshot.definition_original_bytes.get(SectionType.Data, []))
        has_introduce_ndc_inst = False
        for fim in mutation_batch.func_inst_mutations:
            for inst in fim.new_insts:
                op = inst.opcode_text
                if op == 'data.drop' or op == 'memory.init':
                    has_introduce_ndc_inst = True
                    break
        for def_mutation in mutation_batch.definition_mutations:
            if def_mutation.sec_type == SectionType.Code:
                new_funcs = def_mutation.raw_new_definitions
                for wasm_func in new_funcs:
                    if has_introduce_ndc_inst:
                        break
                    for inst in wasm_func.insts:
                        op = inst.opcode_text
                        if op == 'data.drop' or op == 'memory.init':
                            has_introduce_ndc_inst = True
                            break
        if data_seg_num > 0 and has_introduce_ndc_inst:
            snapshot.definition_original_bytes[SectionType.DataCount] = [memoryview(leb128.u.encode(data_seg_num))]
            modified_data_count_section = True
        if snapshot.section_original_bytes.get(SectionType.DataCount):
            if len(snapshot.definition_original_bytes.get(SectionType.Data, [])) == 0:
                snapshot.definition_original_bytes[SectionType.DataCount] = []
                modified_data_count_section = True
            else:
                # cur_data_count =
                snapshot.definition_original_bytes[SectionType.DataCount] = [memoryview(leb128.u.encode(data_seg_num))]
                modified_data_count_section = True
        return modified_data_count_section
    def _get_modified_sections(self,
                               mutation_batch: MutationBatch) -> set[SectionType]:
        modified_sections: set[SectionType] = set()
        if mutation_batch.func_inst_mutations:
            modified_sections.add(SectionType.Code)
        if mutation_batch.rewrite_local_descs:
            modified_sections.add(SectionType.Code)
        
        # modified_sections.update(section_mutations.keys())
        for m in mutation_batch.definition_mutations:
            modified_sections.add(m.sec_type)
        if SectionType.Data in modified_sections:
            modified_sections.add(SectionType.DataCount)
   
        return modified_sections




def apply_mutation_and_encode(
    snapshot: WMSnapshot,
    mutation_batch: MutationBatch,
    output_file: Union[str, Path]
):
    applier = GeneralMutationApplier()
    modified_sections = applier.apply_mutations_on_snapshot(
        snapshot,
        mutation_batch
    )
    encoder: Encoder = Encoder()
    encoder.encode_v2(snapshot, modified_sections, output_file)
# class 


def apply_mutation_and_encode_keep_snapshot(
    snapshot: WMSnapshot,
    mutation_batch: MutationBatch,
    output_file: Union[str, Path]
):
    applier = GeneralMutationApplier()
    snapshot = snapshot.copy()
    modified_sections = applier.apply_mutations_on_snapshot(
        snapshot,
        mutation_batch
    )
    encoder: Encoder = Encoder()
    encoder.encode_v2(snapshot, modified_sections, output_file)
    return snapshot

class MultiPhaseMutationApplier:
    def __init__(self) -> None:
        self.have_mutate_func_idxs:set[int] = set()
        self.have_mutate_code:bool=False
        self.have_add_new_types:bool = False


    def get_modified_sections(self)->set[SectionType]:
        modified_sections = set()
        if self.have_mutate_func_idxs or self.have_mutate_code:
            modified_sections.add(SectionType.Code)
        if self.have_add_new_types:
            modified_sections.add(SectionType.Type)
        return modified_sections

    # Grouped API: apply pending mutations to snapshot, then optionally flush to file
    def merge_to_snapshot(
        self,
        snapshot: 'WMSnapshot',
        inst_mutations: list[FuncInstMutation],
        type_mutations: list[DefinitionMutation],
    ) -> None:
        # IMPORTANT:
        # Mutations use instruction-index offsets into the *original* function body.
        # If we apply multiple mutations to the same function, earlier mutations may
        # change the instruction list length and shift later offsets.
        # Therefore we must apply mutations per-function in descending start_offset.
        if inst_mutations:
            func_idx2inst_mutations: dict[int, list[FuncInstMutation]] = {}
            for m in inst_mutations:
                func_idx2inst_mutations.setdefault(m.func_idx, []).append(m)
            for func_idx, ms in func_idx2inst_mutations.items():
                self.have_mutate_func_idxs.add(func_idx)
                snapshot.func_bas[func_idx].apply_mutations(ms)
        for type_mutation in type_mutations:
            assert type_mutation.sec_type == SectionType.Type
            snapshot.definition_original_bytes[SectionType.Type][
                type_mutation.start_offset:type_mutation.end_offset
            ] = type_mutation.new_definitions
        if not self.have_add_new_types:
            self.have_add_new_types = len(type_mutations) > 0

    def flush_and_encode(
        self,
        snapshot: 'WMSnapshot',
        output_file: Union[str, Path]
    ) -> tuple[set[SectionType], WMSnapshot]:
        # Flush mutated function bodies into Code definitions
        modified_sections = self.flush(snapshot)
        encoder = Encoder()
        encoder.encode_without_mutation(snapshot, output_file)
        
        # encoder.encode_v2(snapshot, modified_sections, output_file)
        return modified_sections, snapshot

    def flush(self, snapshot):
        if self.have_mutate_func_idxs:
            code_defs = snapshot.definition_original_bytes.get(SectionType.Code)
            assert code_defs is not None
            for func_idx in self.have_mutate_func_idxs:
                code_defs[func_idx] = memoryview(snapshot.func_bas[func_idx].encode())

        modified_sections = self.get_modified_sections()
        
        encoder = Encoder()
        encoder.commit_modified_sections(snapshot, modified_sections)
        # After committing, clear pending flags to avoid repeated re-encoding
        # on subsequent flushes when no new mutations were merged.
        self.have_mutate_func_idxs.clear()
        self.have_mutate_code = False
        self.have_add_new_types = False
        return modified_sections



def apply_inst_mutation_store_and_encode(
    snapshot: WMSnapshot,
    mutation_store: MultiPhaseMutationApplier,
    output_file: Union[str, Path]
)    -> tuple[set[SectionType], WMSnapshot]:
    # Back-compat wrapper
    return mutation_store.flush_and_encode(snapshot, output_file)



def sort_and_clear_section_mutations(section_mutations: list[DefinitionMutation])->list[DefinitionMutation]:
    if len(section_mutations) == 1:
        return section_mutations
    pos2mutations:dict[tuple[int, int], DefinitionMutation] = {}
    # print('section_mutations is ', section_mutations)
    for m in section_mutations:
        # assert m.is_one2one_replace or m.is_delete_one, f'm is {m}'
        pos = m.get_scope()
        if pos in pos2mutations:
            ori_one = pos2mutations[pos]
            if ori_one.is_delete_one:
                pass
            elif ori_one.is_one2one_replace and m.is_delete_one:
                pos2mutations[pos] = m
            # else:
            #     raise ValueError(f'On pos {pos} there are two mutations: {ori_one} and {m}')
        else:
            pos2mutations[pos] = m
    all_mutations = []
    for pos in sorted(pos2mutations.keys(), key=lambda x: x[0], reverse=True):
        all_mutations.append(pos2mutations[pos])
    return all_mutations
    
    
