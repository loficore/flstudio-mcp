#!/usr/bin/env python3
"""Test reading notes back from a project file: build a tiny .flp, read it
through fl_get_piano_roll_notes, and check the notes come back with the right
pitch, velocity, PPQ and time signature. No FL Studio needed.

    python scripts/test_read_notes.py
"""
from __future__ import annotations

import asyncio
import os
import struct
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from fl_studio_mcp.server import build_server  # noqa: E402

_P = _F = 0


def check(label, cond, detail=""):
    global _P, _F
    _P += 1 if cond else 0
    _F += 0 if cond else 1
    print("  [%s] %s%s" % ("PASS" if cond else "FAIL", label, ("  -- " + detail) if detail else ""))


def _varint(n: int) -> bytes:
    groups = []
    while True:
        groups.append(n & 0x7F)
        n >>= 7
        if not n:
            break
    groups.reverse()
    return bytes(g | (0x80 if i < len(groups) - 1 else 0)
                 for i, g in enumerate(groups))


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


def build_flp(*, ppq=96, tempo=98000, tsnum=4, tsden=4) -> bytes:
    """One pattern ('Melody'), one channel ('melody key'), two notes."""
    notes = _note(0, 48, 60, 90, 0) + _note(48, 96, 64, 100, 0)
    ev = bytearray()
    ev += _event(17, tsnum) + _event(18, tsden)
    ev += _event(64, 0) + _text(203, "melody key")
    ev += _event(65, 1) + _text(193, "Melody") + _event(164, 192)
    ev += _event(224, notes)
    ev += _event(65, 1)
    ev += _event(67, 1) + _event(156, tempo)
    header = b"FLhd" + struct.pack("<I", 6) + struct.pack("<hHH", 0, 1, ppq)
    return header + b"FLdt" + struct.pack("<I", len(ev)) + bytes(ev)


async def _read(**kwargs) -> dict:
    from fastmcp import Client
    async with Client(build_server()) as client:
        result = await client.call_tool("fl_get_piano_roll_notes", kwargs)
    return result.data


async def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="flmcp-read-"))
    project = tmp / "read_notes_test.flp"
    project.write_bytes(build_flp())
    os.environ["FLSTUDIO_MCP_PROJECT"] = str(project)

    out = await _read(source="autosave")
    check("read succeeded", out.get("ok") is True, str(out.get("error")))
    check("source is autosave", out.get("source") == "autosave", str(out.get("source")))
    check("read the project file", out.get("file") == str(project), str(out.get("file")))
    check("age reported", isinstance(out.get("age_seconds"), (int, float)))
    check("pattern name", out.get("pattern_name") == "Melody", str(out.get("pattern_name")))
    check("pattern number", out.get("pattern_number") == 1, str(out.get("pattern_number")))
    check("ppq from the file", out.get("ppq") == 96, str(out.get("ppq")))
    check("time signature from the file",
          out.get("time_signature") == {"numerator": 4, "denominator": 4},
          str(out.get("time_signature")))
    check("tempo from the file", out.get("tempo_bpm") == 98.0, str(out.get("tempo_bpm")))
    check("note count", out.get("note_count") == 2, str(out.get("note_count")))

    notes = out.get("notes") or []
    pitches = [n["pitch"] for n in notes]
    check("pitches", pitches == [60, 64], str(pitches))
    check("note names", [n["name"] for n in notes] == ["C5", "E5"],
          str([n["name"] for n in notes]))
    velocities = [n["velocity"] for n in notes]
    check("velocities", velocities == [90, 100], str(velocities))
    starts = [n["start_ticks"] for n in notes]
    check("start ticks", starts == [0, 48], str(starts))
    check("length in bars", abs(notes[1]["length_bars"] - 0.25) < 1e-9,
          str(notes[1]["length_bars"]))
    check("summary pitch range",
          out.get("summary", {}).get("pitch_min") == 60
          and out.get("summary", {}).get("pitch_max") == 64,
          str(out.get("summary")))

    truncated = await _read(source="autosave", max_notes=1)
    check("max_notes truncates", truncated.get("truncated") is True
          and len(truncated.get("notes") or []) == 1, str(truncated.get("truncated")))
    check("truncated keeps the true count", truncated.get("note_count") == 2,
          str(truncated.get("note_count")))

    missing = await _read(source="autosave", pattern=99)
    check("missing pattern reports an error", missing.get("ok") is False
          and missing.get("error") == "pattern_not_found", str(missing.get("error")))

    os.environ.pop("FLSTUDIO_MCP_PROJECT", None)
    print("\n%d passed, %d failed" % (_P, _F))
    return 0 if _F == 0 else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
