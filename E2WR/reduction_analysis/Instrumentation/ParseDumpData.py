import struct
import traceback
from typing import Callable, TypeVar

from .NonRefStackProbe import infer_type_seq_required_word_num
from .ProbeType import ProbeType, decode_probe_type, PackedProbeHeaderCodec
from .DumpData import EmptyDumpSeq, OneDumpData, CanControlData
T = TypeVar('T')

class IllFormedDataException(Exception):
    pass

class TraceOOBException(Exception):
    pass


HEADER_WORD_NUM = 1  # packed_header(i32)


def _looks_like_header_word(raw_trace: bytearray, pos: int) -> bool:
    # header is i32 (4 bytes) whose lowest byte is marker 0xF0.
    return pos + 4 <= len(raw_trace) and raw_trace[pos] == PackedProbeHeaderCodec.MARKER_BYTE



def extract_head_info(data_block, pos) -> tuple[int, ProbeType, int]:
    check_not_OOB(pos, data_block)
    packed, pos = parse_i32(data_block, pos)
    assert isinstance(packed, int)
    try:
        probe_type_value, probe_idx = PackedProbeHeaderCodec.unpack(packed)
    except ValueError as e:
        raise IllFormedDataException(str(e))
    probe_type = decode_probe_type(probe_type_value)
    return pos, probe_type, probe_idx


def _get_types_by_probe_type(
    probe_type:ProbeType,
    can_control_data:CanControlData
)->list[str]:
    if probe_type == ProbeType.EXECUTED:
        return []
    raise Exception(f'unsupported probe type: {probe_type}')

def get_all_raw_bytes_and_then_parse(
    raw_trace:bytes,
    probe_idx2can_control_data:dict[int, CanControlData],
    debug:bool=False
)->list[OneDumpData]:
    raw_trace = bytearray(raw_trace)
    # print_ba(raw_trace)
    print('Raw bytes num', len(raw_trace))
    
    result = []
    pos = 0
    marker_byte = PackedProbeHeaderCodec.MARKER_BYTE

    # Cache computed event byte lengths to avoid repeatedly re-infering type sizes.
    # Keyed by (probe_type, probe_idx) because length depends on probe_type.
    event_len_cache: dict[tuple[ProbeType, int], int] = {}
    while True:
        pos = raw_trace.find(marker_byte, pos)
        if pos == -1 or pos > len(raw_trace) - 4:
            break
        try:
            if not _looks_like_header_word(raw_trace, pos):
                pos += 1
                continue

            start_idx = pos
            _, probe_type, probe_idx = extract_head_info(raw_trace, pos)

            # Reject false positives early.
            expected_control_data = probe_idx2can_control_data.get(probe_idx)
            if expected_control_data is None:
                pos += 1
                continue

            cache_key = (probe_type, probe_idx)
            data_length = event_len_cache.get(cache_key)
            if data_length is None:
                expected_types = _get_types_by_probe_type(probe_type, expected_control_data)
                expected_len = infer_type_seq_required_word_num(expected_types, ignore_ref=True)
                data_length = (expected_len + HEADER_WORD_NUM) * 4
                event_len_cache[cache_key] = data_length
            check_not_OOB(start_idx + data_length, raw_trace)

            # Do NOT assume events are contiguous in stdout.
            # The module may print other bytes between probe writes; treating that as a
            # false positive would drop real events and undercount execution.
            pos = start_idx + data_length
            prased_result = OneDumpData(EmptyDumpSeq(), ProbeType.EXECUTED, probe_idx)
            result.append(prased_result)
        except TraceOOBException as e:
            return result
        except Exception as e:
            if debug:
                raise e
            else:
                traceback.print_stack()
                return result
    return result


def get_all_raw_bytes_and_then_parse_executed_only(
    raw_trace: bytes,
    probe_idx2can_control_data: dict[int, CanControlData],
    debug: bool = False,
) -> list[OneDumpData]:


    # Keep behavior parity with the generic parser.
    print('Raw bytes num', len(raw_trace))

    marker_byte = PackedProbeHeaderCodec.MARKER_BYTE
    type_executed = 3  # encode_probe_type(ProbeType.EXECUTED)
    idx_mask = (1 << PackedProbeHeaderCodec.IDX_BITS) - 1

    # Membership test for false-positive suppression.
    expected_probe_idxs = probe_idx2can_control_data

    u32 = struct.Struct('<I')
    empty_seq = EmptyDumpSeq()
    result: list[OneDumpData] = []

    n = len(raw_trace)

    def _decode_if_valid(p: int) -> int | None:
        if p < 0 or p > n - 4:
            return None
        packed_u32 = u32.unpack_from(raw_trace, p)[0]
        if (packed_u32 & 0xFF) != marker_byte:
            return None
        probe_type_value = (packed_u32 >> PackedProbeHeaderCodec.MARKER_BITS) & PackedProbeHeaderCodec.TYPE_MASK
        if probe_type_value != type_executed:
            return None
        probe_idx = (packed_u32 >> (PackedProbeHeaderCodec.MARKER_BITS + PackedProbeHeaderCodec.TYPE_BITS)) & idx_mask
        if probe_idx not in expected_probe_idxs:
            return None
        return int(probe_idx)

    # NOTE: Do NOT assume probe headers appear at 4-byte aligned offsets in stdout.
    # While each event is 4 bytes, the iovec base address or host IO behavior may
    # result in arbitrary alignment in the captured stdout stream.
    pos = 0
    while True:
        pos = raw_trace.find(marker_byte, pos)
        if pos == -1 or pos > n - 4:
            break
        try:
            probe_idx = _decode_if_valid(pos)
            if probe_idx is None:
                pos += 1
                continue
            result.append(OneDumpData(empty_seq, ProbeType.EXECUTED, probe_idx))
            pos += 4
        except Exception as e:
            if debug:
                raise e
            traceback.print_stack()
            return result

    return result





    
def _parse_data_flow(data: bytearray, offset: int, type_str: str, parse_core: Callable[[bytearray, int], T], val_len: int) -> tuple[T, int]:
    # val_len 
    check_not_OOB(offset+val_len, data)
    val = parse_core(data, offset)
    offset += val_len
    return val, offset

def parse_i32(data: bytearray, offset: int) -> tuple[int, int]:
    val, offset = _parse_data_flow(data, offset, 'i32', parse_i32_core, 4)
    return val, offset






def parse_i32_core(data: bytearray, offset: int) -> int:
    return struct.unpack_from('<i', data, offset)[0]







def check_not_OOB(pos, data):
    if pos > len(data):
        traceback.print_exc()
        raise TraceOOBException(f'pos {pos} is out of range {len(data)}')
