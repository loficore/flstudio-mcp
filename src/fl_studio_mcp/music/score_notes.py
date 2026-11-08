"""Normalize exposed note data into one shape for both read backends.

The live path gets notes from the piano-roll pyscript dump (ticks, velocity
0..1, pan 0..1); the autosave path gets them from :mod:`flp_reader` (ticks,
velocity 0..127, pan 0..128). This module converts both into the same dicts
and computes the summary the tool returns.

Ticks are the source of truth: PPQ varies per project (96 in the probe
project, not 480) and the time signature is read from the data, never assumed
to be 4/4.
"""

from __future__ import annotations

from .flp_reader import FlpProject

NOTE_NAMES = ("C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B")

# Field order of a dump row. The generated pyscript writes exactly this order;
# keep the two in sync (pyscript_gen imports this tuple to build its literal).
NOTE_FIELDS = ("number", "time", "length", "velocity", "pan", "color",
               "slide", "porta", "pitchofs", "release", "fcut", "fres",
               "group", "repeats", "selected", "muted")

DEFAULT_MAX_NOTES = 2000


class PatternNotFound(LookupError):
    """The project has no pattern with the requested number."""

    def __init__(self, pattern: int, available: list[int]) -> None:
        super().__init__(f"pattern {pattern} not found; available: {available}")
        self.pattern = pattern
        self.available = available


def beats_per_bar(tsnum: int, tsden: int) -> float:
    """Length of one bar in beats for a time signature.

    A beat is a quarter note: 4/4 -> 4, 3/4 -> 3, 6/8 -> 3.
    """
    if not tsnum or not tsden:
        return 4.0
    return tsnum * 4.0 / tsden


def note_name(pitch: int) -> str:
    """FL's display name: MIDI 60 is C5, not C4."""
    return f"{NOTE_NAMES[pitch % 12]}{pitch // 12}"


def make_note(*, pitch: int, start_ticks: int, length_ticks: int, velocity: int,
              pan: float, cents: int, slide: bool, porta: bool,
              ppq: int, beats_per_bar: float) -> dict:
    start_beats = start_ticks / ppq
    length_beats = length_ticks / ppq
    return {
        "pitch": int(pitch),
        "name": note_name(int(pitch)),
        "start_ticks": int(start_ticks),
        "length_ticks": int(length_ticks),
        "start_beats": start_beats,
        "length_beats": length_beats,
        "start_bars": start_beats / beats_per_bar,
        "length_bars": length_beats / beats_per_bar,
        "velocity": int(velocity),
        "pan": float(pan),
        "cents": int(cents),
        "slide": bool(slide),
        "porta": bool(porta),
    }


def summarize(notes: list[dict], *, ppq: int, beats_per_bar: float) -> dict:
    """Pitch range, pitch-class histogram and span for a note list."""
    histogram = [0] * 12
    if not notes:
        return {"pitch_min": None, "pitch_max": None,
                "pitch_class_histogram": histogram,
                "span_bars": 0.0, "notes_per_bar": None}

    pitches = [n["pitch"] for n in notes]
    for pitch in pitches:
        histogram[pitch % 12] += 1
    start = min(n["start_ticks"] for n in notes)
    end = max(n["start_ticks"] + n["length_ticks"] for n in notes)
    span_bars = (end - start) / ppq / beats_per_bar
    return {
        "pitch_min": min(pitches),
        "pitch_max": max(pitches),
        "pitch_class_histogram": histogram,
        "span_bars": span_bars,
        "notes_per_bar": (len(notes) / span_bars) if span_bars > 0 else None,
    }


def _truncate(notes: list[dict], max_notes: int) -> tuple[list[dict], bool]:
    if max_notes < 0:
        max_notes = 0
    return notes[:max_notes], len(notes) > max_notes


def normalize_live(payload: dict, *, max_notes: int = DEFAULT_MAX_NOTES) -> dict:
    """Convert a pyscript dump payload into the tool's note shape."""
    ppq = int(payload.get("ppq") or 96)
    tsnum = int(payload.get("tsnum") or 4)
    tsden = int(payload.get("tsden") or 4)
    per_bar = beats_per_bar(tsnum, tsden)
    fields = payload.get("fields") or list(NOTE_FIELDS)
    index = {name: i for i, name in enumerate(fields)}

    def get(row, name, default=None):
        i = index.get(name)
        if i is None or i >= len(row):
            return default
        return row[i]

    notes = []
    for row in payload.get("notes") or []:
        velocity = get(row, "velocity", 0.0) or 0.0
        notes.append(make_note(
            pitch=int(get(row, "number", 0)),
            start_ticks=int(get(row, "time", 0)),
            length_ticks=int(get(row, "length", 0)),
            velocity=round(float(velocity) * 128),
            pan=float(get(row, "pan", 0.5) or 0.0),
            cents=int(get(row, "pitchofs", 0) or 0),
            slide=bool(get(row, "slide", False)),
            porta=bool(get(row, "porta", False)),
            ppq=ppq,
            beats_per_bar=per_bar,
        ))

    visible, truncated = _truncate(notes, max_notes)
    return {
        "ok": True,
        "ppq": ppq,
        "time_signature": {"numerator": tsnum, "denominator": tsden},
        "note_count": len(notes),
        "notes": visible,
        "truncated": truncated,
        "summary": summarize(notes, ppq=ppq, beats_per_bar=per_bar),
    }


def normalize_flp(project: FlpProject, *, pattern: int | None = None,
                  channel: int | None = None,
                  max_notes: int = DEFAULT_MAX_NOTES) -> dict:
    """Convert notes from a parsed project into the tool's note shape."""
    available = [p.iid for p in project.patterns]
    number = project.selected_pattern if pattern is None else pattern

    if not project.patterns:
        pattern_obj = None
    else:
        pattern_obj = next((p for p in project.patterns if p.iid == number), None)
        if pattern_obj is None:
            raise PatternNotFound(number, available)

    tsnum, tsden = project.time_signature or (4, 4)
    per_bar = beats_per_bar(tsnum, tsden)
    channel_name = None
    if channel is not None:
        match = next((c for c in project.channels if c.iid == channel), None)
        channel_name = match.name if match else None

    notes = []
    for n in (pattern_obj.notes if pattern_obj else []):
        if channel is not None and n.rack_channel != channel:
            continue
        notes.append(make_note(
            pitch=n.key,
            start_ticks=n.position,
            length_ticks=n.length,
            velocity=n.velocity,
            pan=n.pan / 128.0,
            cents=n.fine_pitch - 120,
            slide=n.slide,
            porta=False,
            ppq=project.ppq,
            beats_per_bar=per_bar,
        ))

    visible, truncated = _truncate(notes, max_notes)
    return {
        "ok": True,
        "pattern_number": pattern_obj.iid if pattern_obj else None,
        "pattern_name": pattern_obj.name if pattern_obj else None,
        "channel": channel,
        "channel_name": channel_name,
        "ppq": project.ppq,
        "time_signature": {"numerator": tsnum, "denominator": tsden},
        "tempo_bpm": project.tempo_bpm,
        "note_count": len(notes),
        "notes": visible,
        "truncated": truncated,
        "summary": summarize(notes, ppq=project.ppq, beats_per_bar=per_bar),
    }
