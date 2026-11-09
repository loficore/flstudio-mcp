import pytest
from fastmcp import Client
from flp_fixture import _note, build_flp

from fl_studio_mcp import connection
from fl_studio_mcp.server import build_server

_FIELDS = ["number", "time", "length", "velocity", "pan", "color", "slide", "porta",
           "pitchofs", "release", "fcut", "fres", "group", "repeats", "selected", "muted"]


class Daw:
    """Stand-in bridge: scripted read_score plus project metadata calls."""

    def __init__(self, result, *, pattern_number=2):
        self.result = result
        self.pattern_number = pattern_number
        self.calls = []

    def call(self, cmd, params=None, **kw):
        self.calls.append((cmd, params))
        if cmd == "get_project_state":
            return {"pattern_number": self.pattern_number, "tempo_bpm": 98.0}
        if cmd == "pattern_list":          # real shape: {"pattern": 1-based}
            return {"total": 2, "next_start": None,
                    "patterns": [{"pattern": 1, "name": "Chord"},
                                 {"pattern": 2, "name": "Melody"}]}
        if cmd == "channel_list":          # real shape: {"i": 0-based}
            return {"total": 2, "next_start": None,
                    "channels": [{"i": 0, "name": "piano"},
                                 {"i": 1, "name": "melody key"}]}
        return {}

    def read_score(self, channel=None, timeout=8.0):
        return self.result


def _live_payload():
    return {"ppq": 96, "tsnum": 4, "tsden": 4, "fields": _FIELDS,
            "notes": [[60, 0, 48, 0.78125, 0.5, 0, False, False, 0, 0.5, 0.5, 0.5,
                       0, 0, False, False]]}


@pytest.mark.asyncio
async def test_live_read(monkeypatch):
    monkeypatch.setattr(connection, "_bridge", Daw({"ok": True, "payload": _live_payload()}))
    async with Client(build_server()) as c:
        result = await c.call_tool("fl_get_piano_roll_notes", {"source": "live"})
    out = result.data
    assert out["ok"] is True and out["source"] == "live" and out["note_count"] == 1
    assert out["notes"][0]["velocity"] == 100
    assert out["pattern_number"] == 2 and out["pattern_name"] == "Melody"
    assert out["tempo_bpm"] == 98.0


@pytest.mark.asyncio
async def test_live_read_with_channel_reports_channel_name(monkeypatch):
    monkeypatch.setattr(connection, "_bridge", Daw({"ok": True, "payload": _live_payload()}))
    async with Client(build_server()) as c:
        result = await c.call_tool("fl_get_piano_roll_notes",
                                   {"source": "live", "channel": 1})
    out = result.data
    assert out["channel"] == 1 and out["channel_name"] == "melody key"


@pytest.mark.asyncio
async def test_auto_falls_back_to_autosave(monkeypatch, tmp_path):
    f = tmp_path / "x.flp"
    f.write_bytes(build_flp())
    monkeypatch.setenv("FLSTUDIO_MCP_PROJECT", str(f))
    monkeypatch.setattr(connection, "_bridge",
                        Daw({"ok": False, "error": "dump timeout"}))
    async with Client(build_server()) as c:
        result = await c.call_tool("fl_get_piano_roll_notes", {"source": "auto"})
    out = result.data
    assert out["source"] == "autosave" and out["fallback_reason"] == "dump timeout"
    assert out["note_count"] == 1
    assert out["file"] == str(f) and out["age_seconds"] >= 0


@pytest.mark.asyncio
async def test_auto_skips_live_for_a_different_pattern(monkeypatch, tmp_path):
    f = tmp_path / "x.flp"
    f.write_bytes(build_flp(patterns=(("Chord", 1, [_note(0, 384, 60, 100)]),
                                     ("Melody", 2, [_note(0, 96, 72, 90)]))))
    monkeypatch.setenv("FLSTUDIO_MCP_PROJECT", str(f))
    daw = Daw({"ok": True, "payload": _live_payload()})
    monkeypatch.setattr(connection, "_bridge", daw)
    async with Client(build_server()) as c:
        result = await c.call_tool("fl_get_piano_roll_notes",
                                   {"source": "auto", "pattern": 1})
    out = result.data
    assert out["source"] == "autosave" and out["pattern_number"] == 1
    assert out["note_count"] == 1
    assert not any(cmd == "read_score" for cmd, _ in daw.calls)


@pytest.mark.asyncio
async def test_live_pattern_mismatch_is_reported(monkeypatch):
    monkeypatch.setattr(connection, "_bridge", Daw({"ok": True, "payload": _live_payload()}))
    async with Client(build_server()) as c:
        result = await c.call_tool("fl_get_piano_roll_notes",
                                   {"source": "live", "pattern": 1})
    out = result.data
    assert out["ok"] is False and out["error"] == "pattern_mismatch"
    assert out["current_pattern"] == 2


@pytest.mark.asyncio
async def test_live_failure_without_fallback_is_an_error(monkeypatch):
    monkeypatch.setattr(connection, "_bridge", Daw({"ok": False, "error": "dump timeout"}))
    async with Client(build_server()) as c:
        result = await c.call_tool("fl_get_piano_roll_notes", {"source": "live"})
    out = result.data
    assert out["ok"] is False and out["error"] == "dump timeout"


@pytest.mark.asyncio
async def test_live_cannot_verify_pattern_without_project_state(monkeypatch):
    """If the pattern number is unknown, live must fail closed, not read anything."""
    class NoState(Daw):
        def call(self, cmd, params=None, **kw):
            return {}
    monkeypatch.setattr(connection, "_bridge",
                        NoState({"ok": True, "payload": _live_payload()}))
    async with Client(build_server()) as c:
        result = await c.call_tool("fl_get_piano_roll_notes",
                                   {"source": "live", "pattern": 3})
    out = result.data
    assert out["ok"] is False and out["error"] == "pattern_unverified"


@pytest.mark.asyncio
async def test_live_sentinel_pattern_is_unknown(monkeypatch):
    """The controller returns -1 when it cannot tell; do not report it."""
    monkeypatch.setattr(connection, "_bridge",
                        Daw({"ok": True, "payload": _live_payload()}, pattern_number=-1))
    async with Client(build_server()) as c:
        result = await c.call_tool("fl_get_piano_roll_notes", {"source": "live"})
    out = result.data
    assert out["ok"] is True and out["pattern_number"] is None


@pytest.mark.asyncio
async def test_auto_falls_back_when_the_bridge_cannot_open(monkeypatch, tmp_path):
    """A missing MIDI port must degrade auto to the project file, not raise."""
    f = tmp_path / "x.flp"
    f.write_bytes(build_flp())
    monkeypatch.setenv("FLSTUDIO_MCP_PROJECT", str(f))
    monkeypatch.setattr(connection, "_bridge", None)

    def no_bridge():
        raise connection.FLPortMissing("No OUTPUT MIDI port matching 'FLStudioMCP'")

    monkeypatch.setattr("fl_studio_mcp.tools.pianoroll.get_bridge", no_bridge)
    async with Client(build_server()) as c:
        auto = await c.call_tool("fl_get_piano_roll_notes", {"source": "auto"})
        live = await c.call_tool("fl_get_piano_roll_notes", {"source": "live"})
    assert auto.data["source"] == "autosave"
    assert auto.data["fallback_reason"] == "live read failed"
    assert live.data["ok"] is False and live.data["error"] == "live read failed"
    assert "FLPortMissing" in live.data["detail"]


@pytest.mark.asyncio
async def test_malformed_dump_is_reported_not_crashed(monkeypatch):
    payload = {"ppq": 0, "tsnum": 4, "tsden": 4, "fields": _FIELDS,
               "notes": [[None, 0, 48, 0.5, 0.5, 0, False, False, 0,
                          0.5, 0.5, 0.5, 0, 0, False, False]]}
    monkeypatch.setattr(connection, "_bridge", Daw({"ok": True, "payload": payload}))
    async with Client(build_server()) as c:
        result = await c.call_tool("fl_get_piano_roll_notes", {"source": "live"})
    out = result.data
    assert out["ok"] is False and out["error"] == "dump malformed"


@pytest.mark.asyncio
async def test_read_tool_is_not_advertised_as_read_only():
    """The live path selects a channel and focuses FL, so the hint must say so."""
    async with Client(build_server()) as c:
        tools = await c.list_tools()
    tool = next(t for t in tools if t.name == "fl_get_piano_roll_notes")
    annotations = tool.annotations
    assert getattr(annotations, "read_only_hint", None) is False
    assert getattr(annotations, "destructive_hint", None) is False


@pytest.mark.asyncio
async def test_empty_pattern_truncation_and_missing_pattern(monkeypatch, tmp_path):
    f = tmp_path / "x.flp"
    f.write_bytes(build_flp(patterns=(
        ("Empty", 1, []),
        ("Two", 2, [_note(0, 48, 60, 90), _note(48, 48, 62, 91)]),
    )))
    monkeypatch.setenv("FLSTUDIO_MCP_PROJECT", str(f))
    async with Client(build_server()) as c:
        empty = await c.call_tool("fl_get_piano_roll_notes",
                                  {"source": "autosave", "pattern": 1})
        cut = await c.call_tool("fl_get_piano_roll_notes",
                                {"source": "autosave", "pattern": 2, "max_notes": 1})
        missing = await c.call_tool("fl_get_piano_roll_notes",
                                    {"source": "autosave", "pattern": 99})
    assert empty.data["ok"] is True and empty.data["note_count"] == 0
    assert empty.data["notes"] == []
    assert cut.data["truncated"] is True and len(cut.data["notes"]) == 1
    assert cut.data["note_count"] == 2
    assert missing.data["ok"] is False
    assert missing.data["error"] == "pattern_not_found"
    assert missing.data["available_patterns"] == [1, 2]


@pytest.mark.asyncio
async def test_missing_project_file_is_an_error(monkeypatch, tmp_path):
    monkeypatch.setenv("FLSTUDIO_MCP_PROJECT", str(tmp_path / "nope.flp"))
    async with Client(build_server()) as c:
        result = await c.call_tool("fl_get_piano_roll_notes", {"source": "autosave"})
    out = result.data
    assert out["ok"] is False and out["error"] == "no project file found"


@pytest.mark.asyncio
async def test_unparseable_project_is_an_error(monkeypatch, tmp_path):
    bad = tmp_path / "bad.flp"
    bad.write_bytes(b"not a project")
    monkeypatch.setenv("FLSTUDIO_MCP_PROJECT", str(bad))
    async with Client(build_server()) as c:
        result = await c.call_tool("fl_get_piano_roll_notes", {"source": "autosave"})
    out = result.data
    assert out["ok"] is False and out["error"] == "project parse failed"
    assert str(bad) in out["detail"]
