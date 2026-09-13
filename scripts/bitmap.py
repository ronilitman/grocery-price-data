"""Little-endian bitmap encoding for a deal's branch set.

Bit *i* is branch *i* of a chain, per the bit assignment build_app_db.py
writes to ``store_bits`` (KAN-7). Both the writer here and every reader -
the API subtasks that filter deals by branch - import this module so the two
ends never disagree about bit order.
"""


def pack(bits, n):
    """Pack the branch indices in ``bits`` into an ``n``-bit bitmap.

    One byte per 8 branches, little-endian within each byte: bit *i* lives at
    byte ``i >> 3``, position ``i & 7``.
    """
    buf = bytearray((n + 7) // 8)
    for i in bits:
        buf[i >> 3] |= 1 << (i & 7)
    return bytes(buf)


def has_branch(blob, i):
    """Whether branch ``i`` is set in a bitmap packed by ``pack``."""
    return bool(blob[i >> 3] & (1 << (i & 7)))
