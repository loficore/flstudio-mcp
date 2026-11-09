#!/usr/bin/env python3
"""Live test: read the notes FL currently has, through the real bridge.

Needs FL Studio running with the Piano roll open and 'MCP_Apply' armed once
(this session) from the Piano roll Scripting menu. Exits 77 (skip) when FL is
not reachable.

    python scripts/test_read_notes_live.py
    python scripts/test_read_notes_live.py 1        # channel 1
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from fl_studio_mcp.connection import get_bridge  # noqa: E402
from fl_studio_mcp.server import build_server    # noqa: E402

NO_FL_EXIT = 77
NO_MIDI_MARKERS = ("error creating OS-X MIDI client",       # macOS CoreMIDI
                   "error creating ALSA sequencer client",  # Linux without /dev/snd/seq
                   "MidiInAlsa")
_P = _F = 0


def check(label, cond, detail=""):
    global _P, _F
    _P += 1 if cond else 0
    _F += 0 if cond else 1
    print("  [%s] %s%s" % ("PASS" if cond else "FAIL", label, ("  -- " + detail) if detail else ""))


async def _read(**kwargs) -> dict:
    from fastmcp import Client
    async with Client(build_server()) as client:
        result = await client.call_tool("fl_get_piano_roll_notes", kwargs)
    return result.data


async def main() -> int:
    channel = int(sys.argv[1]) if len(sys.argv) > 1 else None

    try:
        bridge = get_bridge()
    except Exception as e:
        text = str(e)
        if any(m in text for m in NO_MIDI_MARKERS):
            print("No working MIDI system here: %s" % text.splitlines()[0])
            return NO_FL_EXIT
        print("FL is not reachable (%s: %s)." % (type(e).__name__, text))
        print("Start FL Studio, then run this script again.")
        return NO_FL_EXIT

    try:
        alive = bridge.is_alive()
    except Exception as e:
        print("FL is not reachable (%s: %s)." % (type(e).__name__, e))
        print("Start FL Studio, then run this script again.")
        return NO_FL_EXIT
    if not alive:
        print("FL Studio is not answering the heartbeat.")
        print("Start FL Studio, then run this script again.")
        return NO_FL_EXIT

    print("Open the Piano roll on the pattern you want to read, and make sure")
    print("'MCP Apply' has been run once this session (Piano roll Scripting menu).")
    print("Reading now...\n")

    out = await _read(source="live", channel=channel)

    if not out.get("ok"):
        print("Live read failed: %s -- %s" % (out.get("error"), out.get("detail")))
        print("\nIf the Piano roll is open and MCP_Apply was armed, this is a bug.")
        return 1

    check("source is live", out.get("source") == "live", str(out.get("source")))
    check("ppq read", isinstance(out.get("ppq"), int) and out["ppq"] > 0,
          str(out.get("ppq")))
    check("time signature read",
          isinstance(out.get("time_signature"), dict)
          and out["time_signature"].get("denominator") in (2, 4, 8, 16),
          str(out.get("time_signature")))
    check("notes came back", out.get("note_count", 0) > 0,
          "note_count=%s -- is the Piano roll empty?" % out.get("note_count"))

    notes = out.get("notes") or []
    if notes:
        first = notes[0]
        check("first note has a pitch and a start",
              isinstance(first.get("pitch"), int) and "start_ticks" in first,
              str(first))
        check("summary present", bool(out.get("summary")), str(out.get("summary")))

    print("\n%d passed, %d failed" % (_P, _F))
    print("Now change a note in FL (without saving) and run this again -- the")
    print("new value should show up, because the live path reads the open roll.")
    return 0 if _F == 0 else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
