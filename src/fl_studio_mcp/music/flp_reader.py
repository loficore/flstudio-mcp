"""Minimal, dependency-free reader for FL Studio project files (.flp).

An FLP is a chunked binary file: an ``FLhd`` header (format, channel count,
PPQ), then an ``FLdt`` chunk holding a flat event stream. Each event is an
id byte plus a value whose width is implied by the id range -- one byte below
64, two below 128, four below 192, and a length-prefixed blob above that.

This module keeps only what note reading needs: channel names, patterns (name,
length, notes), the selected pattern and the tempo. Unknown events are skipped
by width, so a project written by a newer FL version still parses; fields it
does not know simply come back empty instead of raising.

No third-party dependency: PyFLP is GPL-3.0 and does not run on Python 3.12+.
"""

from __future__ import annotations

import os
import struct
import time
from dataclasses import dataclass, field
from pathlib import Path

# Event ids we consume. The rest are skipped by their encoded width.
_EID_CHANNEL_NEW = 64          # ChannelID.New
_EID_CHANNEL_NAME = 192        # ChannelID._Name (TEXT); older projects
_EID_PLUGIN_NAME = 203         # PluginID.Name (TEXT + 11); FL 12+ channel name
_EID_PATTERN_NEW = 65          # PatternID.New
_EID_PATTERN_NAME = 193        # PatternID.Name (TEXT + 1)
_EID_PATTERN_LENGTH = 164      # PatternID.Length (DWORD + 36)
_EID_PATTERN_NOTES = 224       # PatternID.Notes (DATA + 16)
_EID_SELECTED_PATTERN = 67     # PatternsID.CurrentlySelected (WORD + 3)
_EID_TEMPO = 156               # ProjectID.Tempo (DWORD + 28), BPM * 1000
_EID_TIMESIG_NUM = 17          # ArrangementsID.TimeSigNum
_EID_TIMESIG_DEN = 18          # ArrangementsID.TimeSigBeat
_EID_FL_VERSION = 199          # ProjectID.FLVersion (TEXT + 7)

_NOTE = struct.Struct("<IHHIHHBBBBBBBB")     # 24 bytes
_NOTE_FLAG_SLIDE = 8

_HEADER = struct.Struct("<4sIhHH")           # magic, size, format, channels, ppq


class FlpError(ValueError):
    """Raised when a file is not a readable FL Studio project."""


@dataclass
class FlpNote:
    position: int
    length: int
    key: int
    velocity: int
    rack_channel: int
    midi_channel: int
    pan: int
    fine_pitch: int
    slide: bool
    group: int


@dataclass
class FlpChannel:
    iid: int
    name: str


@dataclass
class FlpPattern:
    iid: int
    name: str
    length: int
    notes: list[FlpNote] = field(default_factory=list)


@dataclass
class FlpProject:
    version: str
    ppq: int
    tempo_bpm: float | None
    channel_count: int
    selected_pattern: int | None
    channels: list[FlpChannel] = field(default_factory=list)
    patterns: list[FlpPattern] = field(default_factory=list)
    time_signature: tuple[int, int] | None = None


def _varint(buf: bytes, i: int, end: int) -> tuple[int, int]:
    """Decode the big-endian, 7-bits-per-group varint used for blob lengths."""
    groups = []
    while True:
        if i >= end:
            raise FlpError("truncated event length")
        b = buf[i]
        i += 1
        groups.append(b & 0x7F)
        if not b & 0x80:
            break
    value = 0
    for g in reversed(groups):
        value = (value << 7) | g
    return value, i


def _decode_text(raw: bytes) -> str:
    """Decode a text event.

    Modern projects store UTF-16LE; older ones store single-byte text. Detect
    by looking for NUL bytes in the high half rather than by FL version.
    """
    if len(raw) >= 2 and len(raw) % 2 == 0 and raw[1::2].count(0) >= max(1, len(raw) // 4):
        try:
            return raw.decode("utf-16-le").rstrip("\x00").strip()
        except UnicodeDecodeError:
            pass
    return raw.decode("utf-8", "replace").rstrip("\x00").strip()


def _parse_notes(blob: bytes) -> list[FlpNote]:
    notes = []
    for off in range(0, len(blob) - _NOTE.size + 1, _NOTE.size):
        (position, flags, rack, length, key, group, fine,
         _reserved, _release, midi_channel, pan, velocity, _mx, _my) = _NOTE.unpack_from(blob, off)
        notes.append(FlpNote(
            position=position, length=length, key=key, velocity=velocity,
            rack_channel=rack, midi_channel=midi_channel, pan=pan,
            fine_pitch=fine, slide=bool(flags & _NOTE_FLAG_SLIDE), group=group,
        ))
    return notes


def parse_flp(path: str | os.PathLike) -> FlpProject:
    """Parse ``path`` and return the channels, patterns and notes it holds."""
    with open(path, "rb") as f:
        data = f.read()

    if len(data) < _HEADER.size or data[:4] != b"FLhd":
        raise FlpError("not an FL Studio project (bad FLhd header)")
    _magic, header_size, _fmt, channel_count, ppq = _HEADER.unpack_from(data, 0)

    offset = 8 + header_size
    if data[offset:offset + 4] != b"FLdt":
        raise FlpError("missing FLdt chunk")
    if len(data) < offset + 8:
        raise FlpError("truncated FLdt chunk header")
    body_size = struct.unpack_from("<I", data, offset + 4)[0]
    i = offset + 8
    end = i + body_size
    # FL autosaves about once a minute; reading one mid-write yields a file
    # shorter than its declared chunk. Reject it rather than silently parsing
    # a project missing its tail (tempo, selected pattern, time signature).
    if end > len(data):
        raise FlpError("truncated FLdt chunk")

    pattern_by_iid: dict[int, FlpPattern] = {}
    channels: list[FlpChannel] = []
    current_pattern: FlpPattern | None = None
    selected: int | None = None
    tempo: float | None = None
    version = ""
    tsnum: int | None = None
    tsden: int | None = None

    while i < end:
        eid = data[i]
        i += 1
        if eid < 64:
            start, i = i, i + 1
        elif eid < 128:
            start, i = i, i + 2
        elif eid < 192:
            start, i = i, i + 4
        else:
            length, i = _varint(data, i, end)
            start, i = i, i + length
        if i > end:
            raise FlpError(f"truncated event {eid}")
        raw = data[start:i]

        if eid == _EID_CHANNEL_NEW:
            channels.append(FlpChannel(iid=struct.unpack_from("<H", raw)[0], name=""))
        elif eid in (_EID_CHANNEL_NAME, _EID_PLUGIN_NAME) and channels:
            # FL 12+ writes the user-visible channel name as PluginID.Name;
            # older projects used ChannelID._Name. Later events win.
            name = _decode_text(raw)
            if name:
                channels[-1].name = name
        elif eid == _EID_PATTERN_NEW:
            iid = struct.unpack_from("<H", raw)[0]
            current_pattern = pattern_by_iid.get(iid)
            if current_pattern is None:
                # New iid: create once. Real files repeat New for the same
                # pattern (notes block, then name block) -- merge, don't append.
                current_pattern = FlpPattern(iid=iid, name="", length=0)
                pattern_by_iid[iid] = current_pattern
        elif eid == _EID_PATTERN_NAME and current_pattern is not None:
            current_pattern.name = _decode_text(raw)
        elif eid == _EID_PATTERN_LENGTH and current_pattern is not None:
            current_pattern.length = struct.unpack_from("<I", raw)[0]
        elif eid == _EID_PATTERN_NOTES and current_pattern is not None:
            current_pattern.notes.extend(_parse_notes(raw))
        elif eid == _EID_SELECTED_PATTERN:
            selected = struct.unpack_from("<H", raw)[0]
        elif eid == _EID_TEMPO:
            tempo = struct.unpack_from("<I", raw)[0] / 1000.0
        elif eid == _EID_TIMESIG_NUM:
            tsnum = raw[0]
        elif eid == _EID_TIMESIG_DEN:
            tsden = raw[0]
        elif eid == _EID_FL_VERSION:
            version = _decode_text(raw)

    return FlpProject(
        version=version,
        ppq=ppq,
        tempo_bpm=tempo,
        channel_count=channel_count,
        selected_pattern=selected,
        channels=channels,
        patterns=list(pattern_by_iid.values()),
        time_signature=(tsnum, tsden) if tsnum and tsden else None,
    )


def projects_dir() -> Path:
    """Where FL Studio keeps projects, including the ``Backup`` subfolder."""
    from ..paths import fl_settings_dir
    return fl_settings_dir().parent / "Projects"


def find_project_file(root: str | os.PathLike | None = None) -> tuple[Path, float] | None:
    """Return the newest ``.flp`` under ``root`` and its age in seconds.

    ``FLSTUDIO_MCP_PROJECT`` pins a specific file. FL rewrites its automatic
    backup about once a minute, so the newest file -- usually that backup -- is
    the freshest view of unsaved edits.
    """
    override = os.environ.get("FLSTUDIO_MCP_PROJECT")
    if override:
        path = Path(override)
        if not path.is_file():
            return None
        return path, max(0.0, time.time() - path.stat().st_mtime)

    base = Path(root) if root is not None else projects_dir()
    try:
        candidates = list(base.rglob("*.flp"))
    except OSError:
        return None

    newest: Path | None = None
    newest_mtime = float("-inf")
    for path in candidates:
        try:
            mtime = path.stat().st_mtime
        except OSError:          # vanished or unreadable mid-scan
            continue
        if mtime > newest_mtime:
            newest, newest_mtime = path, mtime
    if newest is None:
        return None
    return newest, max(0.0, time.time() - newest_mtime)
