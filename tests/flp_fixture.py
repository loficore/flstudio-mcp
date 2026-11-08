"""Build synthetic FLP byte streams for tests.

The reader only needs the event subset below: channel names, patterns (name,
length, notes), the selected pattern, and the tempo. Everything else in a real
project is skipped by the parser, so a minimal stream is enough to exercise it
without shipping a multi-megabyte project file.
"""

from __future__ import annotations

import struct


def _varint(n: int) -> bytes:
    """Little-endian, 7 bits per group -- the FLP/protobuf encoding."""
    out = bytearray()
    while True:
        group = n & 0x7F
        n >>= 7
        if n:
            out.append(group | 0x80)
        else:
            out.append(group)
            return bytes(out)


def _event(eid: int, value) -> bytes:
    if eid < 64:
        return bytes([eid, value & 0xFF])
    if eid < 128:
        return bytes([eid]) + struct.pack("<H", value)
    if eid < 192:
        return bytes([eid]) + struct.pack("<I", value)
    data = bytes(value)
    return bytes([eid]) + _varint(len(data)) + data


def _text(eid: int, s: str) -> bytes:
    return _event(eid, s.encode("utf-16-le") + b"\x00\x00")


def _note(pos: int, length: int, key: int, vel: int, rack: int = 0) -> bytes:
    return struct.pack("<IHHIHHBBBBBBBB", pos, 0, rack, length, key, 0,
                       120, 0, 64, 0, 64, vel, 128, 128)


def build_flp(*, ppq=96, tempo=98000, patterns=None, channels=None, selected=1,
              tsnum=4, tsden=4) -> bytes:
    """Return a minimal FLP byte stream.

    Each pattern's ``PatternID.New`` appears twice (before notes and after the
    name), matching real files where the reader must merge by iid instead of
    appending a second pattern. Channel names use ``PluginID.Name`` (203), the
    event FL 12+ writes; the reader also accepts the older ``ChannelID._Name``.
    """
    if patterns is None:
        patterns = (("Chord", 1, [_note(0, 384, 60, 100)]),)
    if channels is None:
        channels = (("piano", 0), ("melody key", 1))
    ev = bytearray()
    ev += _event(17, tsnum) + _event(18, tsden)
    for name, iid in channels:
        ev += _event(64, iid) + _text(203, name)
    for name, iid, notes in patterns:
        ev += _event(65, iid) + _text(193, name) + _event(164, 384)
        ev += _event(224, b"".join(notes))
        ev += _event(65, iid)
    ev += _event(67, selected) + _event(156, tempo)
    header = b"FLhd" + struct.pack("<I", 6) + struct.pack("<hHH", 0, len(channels), ppq)
    return header + b"FLdt" + struct.pack("<I", len(ev)) + bytes(ev)
