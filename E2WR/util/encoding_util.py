import leb128


def read_next_leb_num(byte_seq, offset:int) -> tuple[int, int]:
    a = bytearray()
    while True:
        b = byte_seq[offset]
        offset += 1
        a.append(b)
        if (b & 0x80) == 0:
            break
    return leb128.u.decode(a), offset
def read_next_leb_int_num(byte_seq, offset:int) -> tuple[int, int]:
    a = bytearray()
    while True:
        b = byte_seq[offset]
        offset += 1
        a.append(b)
        if (b & 0x80) == 0:
            break
    return leb128.i.decode(a), offset


