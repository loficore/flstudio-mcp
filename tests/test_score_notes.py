import pytest
from flp_fixture import _note, build_flp

from fl_studio_mcp.music import flp_reader, score_notes


def _payload(rows, *, ppq=96, tsnum=4, tsden=4):
    return {"ppq": ppq, "tsnum": tsnum, "tsden": tsden,
            "fields": list(score_notes.NOTE_FIELDS), "notes": rows}


def _row(pitch=60, time=0, length=48, velocity=0.78125, pan=0.5,
         pitchofs=0, porta=False, slide=False):
    return [pitch, time, length, velocity, pan, 0, slide, porta,
            pitchofs, 0.5, 0.5, 0.5, 0, 0, False, False]


def test_beats_per_bar_handles_non_4_4():
    assert score_notes.beats_per_bar(4, 4) == 4.0
    assert score_notes.beats_per_bar(3, 4) == 3.0
    assert score_notes.beats_per_bar(6, 8) == 3.0


def test_normalize_live_converts_ticks_velocity_pan_cents():
    payload = _payload([_row(velocity=0.671875, pitchofs=-20)])
    out = score_notes.normalize_live(payload)
    n = out["notes"][0]
    assert (n["pitch"], n["start_ticks"], n["length_ticks"]) == (60, 0, 48)
    assert n["start_beats"] == 0.0 and n["length_bars"] == 0.125
    assert n["velocity"] == 86 and n["pan"] == 0.5 and n["cents"] == -20
    assert n["name"] == "C5"


def test_normalize_live_uses_non_4_4_bars():
    out = score_notes.normalize_live(_payload([_row(time=144, length=72)], tsnum=3, tsden=4))
    n = out["notes"][0]
    assert n["start_bars"] == 0.5 and n["length_bars"] == 0.25


def test_normalize_flp_filters_channel_and_pattern():
    proj = flp_reader.FlpProject(
        version="24.2.2", ppq=96, tempo_bpm=98.0, channel_count=2,
        selected_pattern=1,
        channels=[flp_reader.FlpChannel(0, "piano"), flp_reader.FlpChannel(1, "melody key")],
        patterns=[
            flp_reader.FlpPattern(1, "Chord", 384,
                                  [flp_reader.FlpNote(0, 384, 60, 100, 0, 0, 64, 120, False, 0)]),
            flp_reader.FlpPattern(2, "Melody", 768,
                                  [flp_reader.FlpNote(0, 96, 62, 90, 1, 0, 64, 120, False, 0)]),
        ],
        time_signature=(4, 4))
    out = score_notes.normalize_flp(proj, pattern=2, channel=1)
    assert out["pattern_name"] == "Melody" and out["channel_name"] == "melody key"
    assert out["note_count"] == 1
    assert out["notes"][0]["pitch"] == 62 and out["notes"][0]["velocity"] == 90


def test_normalize_flp_defaults_to_selected_and_converts_pan_cents():
    proj = flp_reader.FlpProject(
        version="", ppq=96, tempo_bpm=None, channel_count=1, selected_pattern=1,
        channels=[flp_reader.FlpChannel(0, "piano")],
        patterns=[flp_reader.FlpPattern(
            1, "P", 384,
            [flp_reader.FlpNote(0, 96, 72, 80, 0, 0, 96, 100, True, 0)])])
    out = score_notes.normalize_flp(proj)
    assert out["pattern_number"] == 1 and out["channel"] is None
    assert out["time_signature"] == {"numerator": 4, "denominator": 4}
    n = out["notes"][0]
    assert n["pan"] == 0.75 and n["cents"] == -20 and n["slide"] is True


def test_normalize_flp_missing_pattern_raises():
    proj = flp_reader.FlpProject(
        version="", ppq=96, tempo_bpm=None, channel_count=1, selected_pattern=1,
        channels=[], patterns=[flp_reader.FlpPattern(1, "P", 384, [])])
    with pytest.raises(score_notes.PatternNotFound) as err:
        score_notes.normalize_flp(proj, pattern=99)
    assert err.value.available == [1]


def test_normalize_truncates_and_summarizes():
    payload = _payload([_row(pitch=60 + i, time=i * 48) for i in range(3)])
    out = score_notes.normalize_live(payload, max_notes=2)
    assert out["truncated"] is True and len(out["notes"]) == 2
    assert out["note_count"] == 3
    assert out["summary"]["pitch_min"] == 60 and out["summary"]["pitch_max"] == 62


def test_summary_pitch_classes_and_span():
    payload = _payload([_row(pitch=60, time=0, length=384), _row(pitch=64, time=0),
                        _row(pitch=67, time=384, length=384)])
    out = score_notes.normalize_live(payload)
    hist = out["summary"]["pitch_class_histogram"]
    assert len(hist) == 12
    assert hist[0] == 1 and hist[4] == 1 and hist[7] == 1
    assert out["summary"]["span_bars"] == 2.0
    assert out["summary"]["notes_per_bar"] == 1.5


def test_empty_score_is_not_an_error():
    out = score_notes.normalize_live(_payload([]))
    assert out["ok"] is True and out["note_count"] == 0 and out["notes"] == []
    assert out["summary"]["pitch_min"] is None
    assert out["summary"]["pitch_class_histogram"] == [0] * 12


def test_fixture_round_trip_through_normalize_flp(tmp_path):
    p = tmp_path / "x.flp"
    p.write_bytes(build_flp(patterns=(("Two", 1, [_note(0, 48, 60, 90), _note(48, 48, 62, 91)]),)))
    out = score_notes.normalize_flp(flp_reader.parse_flp(p))
    assert [n["pitch"] for n in out["notes"]] == [60, 62]
    assert [n["velocity"] for n in out["notes"]] == [90, 91]
