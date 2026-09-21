
from typing import Union, Iterable, Callable, Optional
from enum import Enum
from extract_block_mutator.InstUtil.ByteInst import ByteImmInst
from extract_block_mutator.encode.NGDataPayload import DataPayloadwithName
import random


def get_int_from_expr_or_int(expr_or_int:Union[DataPayloadwithName, ByteImmInst])->int:
    if isinstance(expr_or_int, DataPayloadwithName):
        inst_ = expr_or_int.val
    else:
        inst_ = expr_or_int
    return inst_.imm_part.val

class ListDataScheduleStrategy(Enum):
    DECREASING_LEN = 1
    RANDOM = 2
    ORIGINAL_ORDER = 3
    DECREASING_WITH_RANDOM = 4


def get_idxs_by_strategy(
    full_length:int,
    strategy:ListDataScheduleStrategy,
    get_elem_lengths_func:Optional[Callable[[],list[int]]]=None
)->Iterable[int]:
    if strategy == ListDataScheduleStrategy.DECREASING_LEN:
        assert get_elem_lengths_func is not None
        elem_lengths:list[int] = get_elem_lengths_func()
        for len_ in elem_lengths:
            # assert isinstance(len_, int)
            if not isinstance(len_, int):
                raise ValueError(f'Element lengths must be integers. get_elem_lengths_func : {get_elem_lengths_func} ;; elem_lengths: {elem_lengths}')
        elem_idx_order = sorted(range(full_length), key=lambda i: elem_lengths[i], reverse=True)
        return elem_idx_order
    elif strategy == ListDataScheduleStrategy.ORIGINAL_ORDER:
        return list(range(full_length))
    elif strategy == ListDataScheduleStrategy.RANDOM:
        elem_idxs = list(range(full_length))
        random.shuffle(elem_idxs)
        return elem_idxs
    elif strategy == ListDataScheduleStrategy.DECREASING_WITH_RANDOM:
        def elem_idx_generator():
            visited = set()
            all_idxs = set(range(full_length))
            idxs_by_order = get_idxs_by_strategy(full_length, ListDataScheduleStrategy.DECREASING_LEN)
            for idx in idxs_by_order:
                if idx in visited:
                    continue
                if random.random() < 0.8:
                    to_generate =  idx
                else:
                    to_generate = random.choice(list(all_idxs - visited))
                visited.add(to_generate)
                yield to_generate
            assert visited == all_idxs
        return elem_idx_generator()
    else:
        raise ValueError('Not supported strategy')
