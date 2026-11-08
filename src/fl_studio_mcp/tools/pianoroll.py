"""Piano-roll note tools -- write, quantize, and read back.

Write path: the pyscript sandbox can't take a job file, so the daemon
generates a .pyscript with the notes baked in, force-focuses FL and fires the
platform run-last-script shortcut. Read path: a generated read-only script
dumps the open piano roll to JSON next to itself, and when that is not
available the tool falls back to parsing FL's newest project file (including
the automatic backup). Requires the FL Piano roll open, and a one-time setup:
run 'MCP_Apply' once from the piano-roll Scripting menu so that 'Run last
script again' targets it.
"""

from __future__ import annotations

from typing import Annotated, List

from fastmcp import FastMCP
from pydantic import BaseModel, Field

from .. import safety
from ..connection import fetch_all_pages, get_bridge
from ..music import flp_reader, score_notes
from ..pyscript_gen import quantize_notes


class PianoRollNote(BaseModel):
    pitch: int = Field(ge=0, le=127,
                       description="MIDI note (60 = middle C; FL displays it as C5).")
    time_bars: float = Field(0.0, ge=0.0,
                             description="Start, in bars from the pattern start.")
    length_bars: float = Field(1.0, gt=0.0, description="Duration in bars.")
    velocity: float = Field(100 / 127.0, ge=0.0, le=1.0,
                            description="0.0-1.0 (0.787 ~= MIDI velocity 100).")
    porta: bool = Field(False,
                        description="Native FL instrument portamento flag; inverts the channel's global portamento state for this note. No effect on VST instruments.")


def _project_state(bridge) -> dict:
    """Current pattern number and tempo; empty dict when the bridge is down."""
    try:
        state = bridge.call("get_project_state", {})
    except Exception:
        return {}
    return state if isinstance(state, dict) else {}


def _list_names(bridge, command: str, key: str, id_key: str) -> dict:
    """Map id -> name from a paginated list command; {} when it fails."""
    try:
        data = fetch_all_pages(bridge, command, key)
    except Exception:
        return {}
    out = {}
    for item in data.get(key) or []:
        try:
            out[int(item[id_key])] = item.get("name")
        except (KeyError, TypeError, ValueError):
            continue
    return out


def _read_live(bridge, channel):
    try:
        return bridge.read_score(channel=channel)
    except Exception as e:
        return {"ok": False, "error": "live read failed",
                "detail": f"{type(e).__name__}: {e}"}


def _live_meta(bridge, channel, current_pattern):
    """Pattern/channel names + tempo for the live path.

    All optional: a missing list command leaves the field null instead of
    failing a read whose notes are already in hand.
    """
    pattern_names = _list_names(bridge, "pattern_list", "patterns", "pattern")
    channel_names = _list_names(bridge, "channel_list", "channels", "i")
    state = _project_state(bridge)
    return {
        "pattern_number": current_pattern,
        "pattern_name": pattern_names.get(int(current_pattern))
        if current_pattern is not None else None,
        "channel": channel,
        "channel_name": channel_names.get(int(channel)) if channel is not None else None,
        "tempo_bpm": state.get("tempo_bpm"),
    }


def _read_project(pattern, channel, max_notes, fallback_reason):
    found = flp_reader.find_project_file()
    if found is None:
        return {"ok": False, "error": "no project file found",
                "detail": "Looked in FL's Projects folder (including Backup/); set "
                          "FLSTUDIO_MCP_PROJECT to point at a specific .flp."}
    path, age = found
    try:
        project = flp_reader.parse_flp(path)
    except Exception as e:
        return {"ok": False, "error": "project parse failed",
                "detail": f"{type(e).__name__}: {e} ({path})"}

    try:
        out = score_notes.normalize_flp(project, pattern=pattern, channel=channel,
                                        max_notes=max_notes)
    except score_notes.PatternNotFound as e:
        return {"ok": False, "error": "pattern_not_found",
                "available_patterns": e.available, "detail": str(e)}

    out["source"] = "autosave"
    out["file"] = str(path)
    out["age_seconds"] = age
    out["fallback_reason"] = fallback_reason
    return out


def register(mcp: FastMCP) -> None:
    @mcp.tool(annotations={
        "title": "Write piano-roll notes",
        "readOnlyHint": False,
        "destructiveHint": True,   # mode='replace' clears existing notes
        "idempotentHint": False,
        "openWorldHint": True,
    })
    def fl_write_piano_roll_notes(
        notes: List[PianoRollNote],
        mode: Annotated[str, Field(
            description="'replace' clears the pattern first; 'append' adds to it.",
        )] = "replace",
        quantize: Annotated[float, Field(
            description="Optional grid (bars) to snap note starts to before writing: 0.0625=1/16, 0.125=1/8, 0=off.",
        )] = 0.0,
    ) -> dict:
        """Write notes into the currently-open FL Piano roll.

        Each note: {pitch (MIDI), time_bars, length_bars, velocity 0-1,
        porta (optional, slide into the note)}. Set
        quantize (grid in bars) to snap note starts to a grid before writing.
        Setup: open the Piano roll and run 'MCP_Apply' once from its Scripting
        menu (so Run last script again targets it). mode='replace' clears first.
        """
        arr = [n.model_dump() for n in notes]
        if quantize and quantize > 0:
            arr = quantize_notes(arr, float(quantize))
        preview = safety.preview_write(tool="write_piano_roll_notes",
                                       params={"notes": arr, "mode": mode, "quantize": quantize})
        if preview is not None:
            return preview
        return get_bridge().apply_notes(arr, mode)

    @mcp.tool(annotations={
        "title": "Quantize piano-roll notes",
        "readOnlyHint": False, "destructiveHint": False,
        "idempotentHint": False, "openWorldHint": True,
    })
    def fl_quantize_pattern(
        grid_bars: Annotated[float, Field(gt=0, description="Snap grid in bars: 0.0625=1/16, 0.125=1/8, 0.25=1/4.")] = 0.0625,
        snap_ends: Annotated[bool, Field(description="Also snap note lengths to the grid.")] = False,
    ) -> dict:
        """Quantize the notes ALREADY in the open Piano roll: reads the score,
        snaps note starts (and optionally lengths) to the grid, rewrites -- via
        the pyscript bridge, no dialog. Needs the Piano roll open + MCP_Apply
        armed once this session (same setup as note writing)."""
        preview = safety.preview_write(tool="quantize_pattern",
                                       params={"grid_bars": grid_bars, "snap_ends": snap_ends})
        if preview is not None:
            return preview
        return get_bridge().apply_notes([], trigger=True, quantize=float(grid_bars), snap_ends=snap_ends)

    @mcp.tool(annotations={
        "title": "Read piano-roll notes",
        # Not read-only in the strict sense: the live path selects a channel
        # (which retargets later note writes) and focuses FL. It never edits
        # project data and never enters the write safety layer.
        "readOnlyHint": False, "destructiveHint": False,
        "idempotentHint": True, "openWorldHint": True,
    })
    def fl_get_piano_roll_notes(
        channel: Annotated[int | None, Field(
            description="Channel rack position (0 = first channel, the same index "
                        "FL's channel selection uses). Defaults to the channel the "
                        "Piano roll already shows.",
        )] = None,
        pattern: Annotated[int | None, Field(
            description="Pattern number to read. Defaults to the current pattern. "
                        "Only the project-file path can read a pattern other than "
                        "the current one.",
        )] = None,
        source: Annotated[str, Field(
            description="'auto' (live first, project file as fallback), 'live' "
                        "(open Piano roll only), 'autosave' (project file only).",
        )] = "auto",
        max_notes: Annotated[int, Field(
            ge=1, le=20000,
            description="Maximum notes to return; extra notes are counted but not listed.",
        )] = 2000,
    ) -> dict:
        """Read the notes FL currently holds, so you can analyse or edit them.

        Returns the notes with pitch, name, start/length in ticks, beats and
        bars, velocity, pan and cents, plus a short summary (pitch range,
        pitch-class histogram, span). 'auto' reads the open Piano roll first
        and falls back to FL's newest project file (the automatic backup, up
        to about a minute old) when the Piano roll is not reachable.

        Passing channel selects that channel on the live path (FL's selection
        persists, so later note writes go there); with source='autosave' it
        just filters. The project-file path never changes anything.

        Setup for the live path: open the Piano roll and run 'MCP_Apply' once
        from its Scripting menu.
        """
        if source not in ("auto", "live", "autosave"):
            return {"ok": False, "error": "invalid source",
                    "detail": "source must be 'auto', 'live' or 'autosave'"}

        fallback_reason = None
        live = None
        current_pattern = None

        if source in ("auto", "live"):
            try:
                bridge = get_bridge()
            except Exception as e:
                bridge = None
                if source == "live":
                    return {"ok": False, "error": "live read failed",
                            "detail": f"{type(e).__name__}: {e}"}
                fallback_reason = "live read failed"

            if bridge is not None:
                state = _project_state(bridge)
                current_pattern = state.get("pattern_number")
                if isinstance(current_pattern, int) and current_pattern < 0:
                    current_pattern = None
                mismatch = (pattern is not None and current_pattern is not None
                            and int(pattern) != int(current_pattern))
                unknown = pattern is not None and current_pattern is None
                if mismatch or unknown:
                    if source == "live":
                        if unknown:
                            return {"ok": False, "error": "pattern_unverified",
                                    "detail": "Could not read the current pattern number, so "
                                              "the open Piano roll cannot be verified against "
                                              f"pattern={pattern}. Use source='autosave'."}
                        return {"ok": False, "error": "pattern_mismatch",
                                "current_pattern": current_pattern,
                                "detail": "the live path only reads the current pattern; "
                                          "use source='autosave' for another one."}
                else:
                    res = _read_live(bridge, channel)
                    if res.get("ok"):
                        live = res
                    else:
                        if source == "live":
                            return {"ok": False,
                                    "error": res.get("error", "live read failed"),
                                    "detail": res.get("detail")}
                        fallback_reason = res.get("error", "live read failed")

        if live is not None:
            try:
                out = score_notes.normalize_live(live["payload"], max_notes=max_notes)
            except Exception as e:      # malformed script output
                return {"ok": False, "error": "dump malformed",
                        "detail": f"{type(e).__name__}: {e}"}
            out.update(_live_meta(bridge, channel, current_pattern))
            out["source"] = "live"
            out["file"] = None
            out["age_seconds"] = None
            out["fallback_reason"] = None
            return out

        return _read_project(pattern, channel, max_notes, fallback_reason)
