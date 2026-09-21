from WasmInfoCfg import ExportType, ImportType
from .get_data_shell import get_export_attr, get_impotr_attr
from extract_block_mutator.encode.NGDataPayload import  DataPayloadwithName
from .encode.byte_define.SectionPart import  section_decoder

from .funcType import funcType
from .wasmFunc import wasmFunc
from typing import Optional
from util.prepare_template import prepare_sec_name2all_ba
from .InfoLoader import InfoLoader


class WasmParser(InfoLoader):
    def __init__(self, 
                types=None,
                 imports=None,
                defined_func_ty_ids=None, 
                defined_table_datas=None,
                defined_memory_datas=None,
                defined_globals=None,
                 exports=None,
                elem_sec_datas=None,
                defined_funcs=None,
                data_sec_datas=None,
                start_sec_data=None, 
                data_count_sec_data=None,
                customs=None
                 ) -> None:
        if types is None:
            types = []
        if imports is None:
            imports = []
        if defined_func_ty_ids is None:
            defined_func_ty_ids = []
        if defined_table_datas is None:
            defined_table_datas = []
        if defined_memory_datas is None:
            defined_memory_datas = []
        if defined_globals is None:
            defined_globals = []
        if exports is None:
            exports = []
        if elem_sec_datas is None:
            elem_sec_datas = []
        if defined_funcs is None:
            defined_funcs = []
        if data_sec_datas is None:
            data_sec_datas = []
        if customs is None:
            customs = []
        self.types:list[funcType] = types
        self.imports:list[DataPayloadwithName] = imports
        self.defined_func_ty_ids:list[int] = defined_func_ty_ids
        self.defined_table_datas:list[DataPayloadwithName] = defined_table_datas
        self.defined_memory_datas:list[DataPayloadwithName] = defined_memory_datas
        self.defined_globals:list[DataPayloadwithName] = defined_globals
        self.exports:list[DataPayloadwithName] = exports
        self.start_sec_data:Optional[int] = start_sec_data
        self.elem_sec_datas: list[DataPayloadwithName] = elem_sec_datas
        self.defined_funcs:list[wasmFunc] = defined_funcs
        self.data_sec_datas:list[DataPayloadwithName] = data_sec_datas
        self.data_count_sec_data:Optional[int] = data_count_sec_data
        self.customs:list[DataPayloadwithName] = customs
        
        
        self.import_func_num = 0
        self.import_memory_num = 0
        self.import_table_num = 0
        self.import_global_num = 0
        # 
        self.import_func_ty_ids:list[int] = []
        for import_desc in self.imports:
            import_desc_type = get_impotr_attr(import_desc, 'type')
            if import_desc_type == ImportType.func:
                import_attr = get_impotr_attr(import_desc, 'import_attr')
                idx = import_attr.data['typeidx'] # type: ignore
                assert isinstance(idx, int)
                self.import_func_ty_ids.append(idx)
                self.import_func_num += 1
            elif import_desc_type == ImportType.mem:
                self.import_memory_num += 1
            elif import_desc_type == ImportType.table:
                self.import_table_num += 1
            elif import_desc_type == ImportType.global_:
                self.import_global_num += 1

        assert len(self.func_type_idxs) == len(self.defined_funcs) + self.import_func_num
        for defined_func_idx, defined_func in zip(self.defined_func_ty_ids, self.defined_funcs):
            if defined_func_idx >= len(self.types):
                raise ValueError(f'defined_func_idx {defined_func_idx} is greater than the number of types {len(self.types)},, self.defined_func_ty_ids : {self.defined_func_ty_ids}, defined func num : {len(self.defined_funcs)}')
            cur_func_type = self.types[defined_func_idx]
            if defined_func is not None:
                defined_func.func_ty = cur_func_type
        # defined func 0 location
        if len(self.defined_funcs):
            self.func0 = self.defined_funcs[0]
        else:
            self.func0 = None
        self._func0_block = None
        self._insts0_early_return_pos = None
        self._sop_pos_candis = None
        assert len(self.defined_funcs) + self.import_func_num == self.func_num, f'{len(self.defined_funcs)} + {self.import_func_num} != {self.func_num}'
    @property
    def func_type_idxs(self):
        return self.import_func_ty_ids + self.defined_func_ty_ids

    @property
    def local_types(self)->list[str]:
        raise AttributeError

    @property
    def cur_func_ty(self):
        raise AttributeError

    def copy(self):
        parser = WasmParser(
            types = [_.copy() for _ in self.types],
            imports= [_.copy() for _ in self.imports],
            defined_func_ty_ids = self.defined_func_ty_ids.copy(),
            defined_table_datas = [_.copy() for _ in self.defined_table_datas],
            defined_memory_datas = [_.copy() for _ in self.defined_memory_datas],
            defined_globals = [_.copy() for _ in self.defined_globals],
            exports= [_.copy() for _ in self.exports],
            elem_sec_datas = [_.copy() for _ in self.elem_sec_datas],
            defined_funcs = [_.copy() for _ in self.defined_funcs],
            data_sec_datas = [_.copy() for _ in self.data_sec_datas],
            start_sec_data = None if self.start_sec_data is None else self.start_sec_data,
            data_count_sec_data = None if self.data_count_sec_data is None else self.data_count_sec_data,
            customs = [_.copy() for _ in self.customs],
        )
        if self._func0_block is not None:
            parser._func0_block = self._func0_block.copy()
        if self._sop_pos_candis is not None:
            parser._sop_pos_candis = self._sop_pos_candis.copy()
        return parser

    def light_copy(self):
        parser = WasmParser(
            types = [_.copy() for _ in self.types],
            imports= self.imports,
            defined_func_ty_ids = self.defined_func_ty_ids,
            defined_table_datas = self.defined_table_datas,
            defined_memory_datas = self.defined_memory_datas,
            defined_globals = self.defined_globals,
            exports= self.exports,
            elem_sec_datas = self.elem_sec_datas,
            defined_funcs = [_.copy() for _ in self.defined_funcs],
            data_sec_datas = self.data_sec_datas,
            start_sec_data = None if self.start_sec_data is None else self.start_sec_data,
            data_count_sec_data = None if self.data_count_sec_data is None else self.data_count_sec_data,
            customs = self.customs,
        )
        return parser


    def get_to_test_func_idx(self) -> Optional[int]:
        for _export in self.exports:
            if get_export_attr(_export, 'attr') == ExportType.func:
                if get_export_attr(_export, 'name') == 'to_test':
                    return get_export_attr(_export, 'idx')  # type: ignore
        return None

    @classmethod
    def from_wasm_path(cls, wasm_path):
        return get_parser_from_wasm_path(wasm_path)


def get_parser_from_wasm_path(wasm_path):
    types = None
    imports = None
    defined_func_ty_ids = None
    defined_table_datas = None
    defined_memory_datas = None
    defined_globals = None
    exports= None
    start_sec_data = None
    elem_sec_datas = None
    defined_funcs = None
    data_sec_datas = None
    data_count_sec_data = None
    customs = []
    
    sec_name2all_ba = prepare_sec_name2all_ba(wasm_path)
    for sec_name, sec_ba in sec_name2all_ba.items():
        # print(f'Processing {sec_name}')
        if sec_name == 'type':
            # print_ba(sec_ba)
            types = section_decoder.byte_decode_and_generate(memoryview(sec_ba))[0]
        elif sec_name == 'import':
            imports = section_decoder.byte_decode_and_generate(memoryview(sec_ba))[0]
        elif sec_name == 'function':
            defined_func_ty_ids = section_decoder.byte_decode_and_generate(memoryview(sec_ba))[0]
        elif sec_name == 'table':
            defined_table_datas = section_decoder.byte_decode_and_generate(memoryview(sec_ba))[0]
        elif sec_name == 'memory':
            defined_memory_datas = section_decoder.byte_decode_and_generate(memoryview(sec_ba))[0]
        elif sec_name == 'global':
            defined_globals = section_decoder.byte_decode_and_generate(memoryview(sec_ba))[0]
        elif sec_name == 'export':
            exports = section_decoder.byte_decode_and_generate(memoryview(sec_ba))[0]
        elif sec_name == 'start':
            start_sec_data = section_decoder.byte_decode_and_generate(memoryview(sec_ba))[0]
        elif sec_name == 'element':
            elem_sec_datas = section_decoder.byte_decode_and_generate(memoryview(sec_ba))[0]
        elif sec_name == 'code':
            defined_funcs = section_decoder.byte_decode_and_generate(memoryview(sec_ba))[0]
        elif sec_name == 'data':
            data_sec_datas = section_decoder.byte_decode_and_generate(memoryview(sec_ba))[0]
        elif sec_name == 'data_count':
            data_count_sec_data = section_decoder.byte_decode_and_generate(memoryview(sec_ba))[0]
        elif sec_name == 'custom':
            customs.append(section_decoder.byte_decode_and_generate(memoryview(sec_ba))[0])
    return WasmParser(
        types = types,
        imports= imports,
        defined_func_ty_ids = defined_func_ty_ids,
        defined_table_datas = defined_table_datas,
        defined_memory_datas = defined_memory_datas,
        defined_globals = defined_globals,
        exports= exports,
        start_sec_data = start_sec_data,
        elem_sec_datas = elem_sec_datas,
        defined_funcs = defined_funcs,
        data_sec_datas = data_sec_datas,
        data_count_sec_data = data_count_sec_data,
        customs=customs
    )
