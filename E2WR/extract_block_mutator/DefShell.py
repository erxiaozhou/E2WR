from typing import Optional, Union
from extract_block_mutator.InstGeneration.InstFactory import InstFactory
from extract_block_mutator.InstUtil.ByteInst import ByteImmInst
from extract_block_mutator.InstUtil.Inst import Inst
from extract_block_mutator.encode.NGDataPayload import DataPayloadwithName



def gen_global_type(val_type, mut) -> DataPayloadwithName:
    return DataPayloadwithName({'val_type': val_type, 'mut': mut}, 'globaltype')


def get_offset_inst_from_int(i)->ByteImmInst:
    assert isinstance(i, int), f'Expect int, but got {type(i)}'
    inst = InstFactory.gen_binary_info_inst_high_single_imm('i32.const', i)
    return inst

def gen_offset_expr_from_int(i) -> DataPayloadwithName:
    assert isinstance(i, int), f'Expect int XX, but got {type(i)}'
    inst = get_offset_inst_from_int(i)
    return DataPayloadwithName({'expr': inst}, 'Expr')


def gen_global_data(global_type, init: Inst) -> DataPayloadwithName:
    expr_init = DataPayloadwithName({'expr': init}, 'Expr')
    return DataPayloadwithName(
        {'gt': global_type, 'e': expr_init},
        'global'
        # 'GlobalData'
    )




def gen_limit1(min_: int) -> DataPayloadwithName:
    return DataPayloadwithName({'min': min_}, 'limits_without_max')

def gen_limit2(min_: int, max_: int) -> DataPayloadwithName:
    return DataPayloadwithName({'min': min_, 'max': max_}, 'limits_with_max')

def gen_limit(min_, max_: Optional[int] = None):
    # if max_v is not None and min_v > max_v:
    #     min_v, max_v = max_v, min_v
    # return DataPayloadwithName
    if max_ is None:
        limit_ = gen_limit1(min_)
    else:
        limit_ = gen_limit2(min_, max_)
    return limit_




def  gen_export_funcidx_part(func_idx):
    return DataPayloadwithName({'desc': func_idx}, 'func_exportdesc')


def gen_export_tableidx_part(table_idx):
    return DataPayloadwithName({'desc': table_idx}, 'table_exportdesc')


def gen_export_memidx_part(memory_idx):
    return DataPayloadwithName({'desc': memory_idx}, 'mem_exportdesc')


def gen_export_desc(name, desc):
    return DataPayloadwithName({'name': name, 'desc': desc}, 'export')



def gen_export_global_idx_part(global_idx):
    return DataPayloadwithName({'desc': global_idx}, 'global_exportdesc')




def gen_func_import_attr(type_idx):
    return DataPayloadwithName({'typeidx': type_idx}, 'func_importdesc')




def gen_import_desc(module_name, entity_name, import_attr) -> DataPayloadwithName:
    return DataPayloadwithName(
        data={
            'module': module_name,
            'name': entity_name,
            'desc': import_attr
        },
        name='import'
    )

def gen_import_func_desc(module_name, entity_name, func_type_idx) -> DataPayloadwithName:
    return DataPayloadwithName(
        data={
            'module': module_name,
            'name': entity_name,
            'desc': DataPayloadwithName(
                data={
                    'typeidx': func_type_idx
                },
                name='func_importdesc'
            )
        },
        name='import'
    )


def gen_passive_data_seg(data) -> DataPayloadwithName:
    return DataPayloadwithName({'bytes': data}, 'passive_def')

def gen_active_data_seg0(data, init) -> DataPayloadwithName:
    return DataPayloadwithName(
        {
            'bytes': data,
            'expression': gen_offset_expr_from_int(init)
            }, 
        'active_memory_zero')

def gen_active_data_seg1(data, init, mem_idx) -> DataPayloadwithName:
    return DataPayloadwithName(
        {
            'bytes': data,
            'memory_index': mem_idx,
            'expression': gen_offset_expr_from_int(init)
            }, 
        'active_memory_index')


def gen_elem_seg0(funcidxs, offset) -> DataPayloadwithName:
    return DataPayloadwithName(
        {
            'funcidxs': funcidxs,
            'offset': gen_offset_expr_from_int(offset)
        },
        'active_elem_seg0'
    )
# passive
def gen_elem_passive0(funcidxs) -> DataPayloadwithName:
    return DataPayloadwithName(
        {
            'funcidxs': funcidxs,
            'elemkind': 'funcref'
        },
        'passive_elem_seg0'
    )

def gen_elem_seg2(table_idx, offset, funcidxs) -> DataPayloadwithName:
    return DataPayloadwithName(
        {
            'table_idx': table_idx,
            'offset': gen_offset_expr_from_int(offset),
            'funcidxs': funcidxs,
            'elemkind': 'funcref'
        },
        'active_elem_seg1'
    )

# decl
def gen_elem_decl0(funcidxs) -> DataPayloadwithName:
    return DataPayloadwithName(
        {
            'funcidxs': funcidxs,
            'elemkind': 'funcref'
        },
        'declarative_elem_seg0'
    )

# passive

def gen_elem_seg6(table_idx, offset, exprs, valtype) -> DataPayloadwithName:
    return DataPayloadwithName(
        {
            'tableidx': table_idx,
            'offset': gen_offset_expr_from_int(offset),
            'exprs': exprs,
            'elemkind': valtype
        },
        'active_elem_seg3'
    )

# decl



