from enum import Enum
from extract_block_mutator.InstGeneration.InstFactory import InstFactory


class PackedProbeHeaderCodec:
    """Codec for compact probe header packed into a single i32.

    Layout (little-endian bits in the u32 value):
    - bits[0:8)   : marker byte = 0xF0
    - bits[8:10)  : probe_type (2 bits, 4 kinds)
    - bits[10:32) : probe_idx (22 bits)
    """

    MARKER_BYTE = 0xF0
    MARKER_BITS = 8
    TYPE_BITS = 2
    IDX_BITS = 32 - MARKER_BITS - TYPE_BITS  # 22

    IDX_MASK = (1 << IDX_BITS) - 1
    TYPE_MASK = (1 << TYPE_BITS) - 1

    @classmethod
    def pack(cls, probe_type_value: int, probe_idx: int) -> int:
        assert 0 <= probe_type_value <= cls.TYPE_MASK
        assert 0 <= probe_idx <= cls.IDX_MASK
        packed_u32 = (
            (cls.MARKER_BYTE)
            | (probe_type_value << cls.MARKER_BITS)
            | (probe_idx << (cls.MARKER_BITS + cls.TYPE_BITS))
        )
        return packed_u32

    @classmethod
    def unpack(cls, packed: int) -> tuple[int, int]:
        packed_u32 = packed & 0xFFFFFFFF
        marker = packed_u32 & 0xFF
        if marker != cls.MARKER_BYTE:
            raise ValueError(f'bad marker: {hex(marker)} != {hex(cls.MARKER_BYTE)}')
        probe_type_value = (packed_u32 >> cls.MARKER_BITS) & cls.TYPE_MASK
        probe_idx = (packed_u32 >> (cls.MARKER_BITS + cls.TYPE_BITS)) & cls.IDX_MASK
        return probe_type_value, probe_idx


class ProbeType(Enum):
    STACK = 'stack'
    EXECUTED = 'executed'


def encode_probe_type(probe_type: ProbeType) -> int:
    if probe_type == ProbeType.STACK:
        return 0
    elif probe_type == ProbeType.EXECUTED:
        return 3
    else:
        raise ValueError(f'unknown probe type: {probe_type}')

def decode_probe_type(probe_type_value: int) -> ProbeType:
    if probe_type_value == 0:
        return ProbeType.STACK
    elif probe_type_value == 3:
        return ProbeType.EXECUTED
    else:
        raise ValueError(f'unknown probe type value: {probe_type_value}')


class ValueProbeId:
    def __init__(self,
                 idx: int,
                 probe_type: ProbeType
                 ):
        self.idx = idx
        self.probe_type = probe_type

    def get_insts(self):
        # Compact header in one i32: marker(0xF0,8b) + type(2b) + idx(22b).
        probe_type_value = encode_probe_type(self.probe_type)
        assert self.idx <= PackedProbeHeaderCodec.IDX_MASK
        packed = PackedProbeHeaderCodec.pack(probe_type_value, self.idx)
        return [
            InstFactory.gen_binary_info_inst_high_single_imm('i32.const', imm0=packed),
        ]

    def __str__(self):
        return f'ProbeId(idx={self.idx}, probe_type={self.probe_type})'

