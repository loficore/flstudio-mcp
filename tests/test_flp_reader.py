import pytest
from flp_fixture import _note, build_flp

from fl_studio_mcp.music import flp_reader


def test_parses_channels_patterns_notes_and_tempo(tmp_path):
    p = tmp_path / "x.flp"
    p.write_bytes(build_flp())
    proj = flp_reader.parse_flp(p)
    assert proj.ppq == 96 and proj.tempo_bpm == 98.0
    assert proj.time_signature == (4, 4)
    assert [(c.iid, c.name) for c in proj.channels] == [(0, "piano"), (1, "melody key")]
    assert proj.selected_pattern == 1
    assert len(proj.patterns) == 1
    pat = proj.patterns[0]
    assert (pat.iid, pat.name, pat.length) == (1, "Chord", 384)
    n = pat.notes[0]
    assert (n.key, n.position, n.length, n.velocity, n.rack_channel) == (60, 0, 384, 100, 0)


def test_rejects_a_file_shorter_than_its_declared_chunk(tmp_path):
    """A torn autosave must fail, not parse 'ok' with missing tail metadata."""
    data = build_flp()
    p = tmp_path / "torn.flp"
    p.write_bytes(data[:len(data) - 5])      # cut inside the last (tempo) event
    with pytest.raises(flp_reader.FlpError):
        flp_reader.parse_flp(p)


def test_rejects_bad_header(tmp_path):
    p = tmp_path / "x.flp"
    p.write_bytes(b"nope")
    with pytest.raises(flp_reader.FlpError):
        flp_reader.parse_flp(p)


def test_pattern_new_twice_is_merged_by_iid(tmp_path):
    p = tmp_path / "x.flp"
    p.write_bytes(build_flp(patterns=(
        ("Chord", 1, [_note(0, 384, 60, 100), _note(384, 384, 64, 100)]),
        ("Melody", 2, [_note(0, 96, 72, 90)]),
    )))
    proj = flp_reader.parse_flp(p)
    assert [(x.iid, x.name, len(x.notes)) for x in proj.patterns] == [
        (1, "Chord", 2), (2, "Melody", 1)]


def test_large_note_blob_uses_multibyte_length(tmp_path):
    """A note blob above 127 bytes needs the multi-byte varint length."""
    many = [_note(i * 24, 24, 60 + (i % 12), 100) for i in range(200)]
    p = tmp_path / "big.flp"
    p.write_bytes(build_flp(patterns=(("Big", 1, many),)))
    proj = flp_reader.parse_flp(p)
    assert len(proj.patterns[0].notes) == 200
    assert proj.patterns[0].notes[-1].key == 60 + (199 % 12)


def test_non_four_four_time_signature(tmp_path):
    p = tmp_path / "x.flp"
    p.write_bytes(build_flp(tsnum=6, tsden=8))
    assert flp_reader.parse_flp(p).time_signature == (6, 8)


def test_find_project_file_newest_and_env_override(tmp_path, monkeypatch):
    a, b = tmp_path / "a.flp", tmp_path / "b.flp"
    a.write_bytes(build_flp())
    b.write_bytes(build_flp())
    import os
    os.utime(a, (1, 1))
    os.utime(b, (2, 2))
    path, age = flp_reader.find_project_file(tmp_path)
    assert path == b and age >= 0
    monkeypatch.setenv("FLSTUDIO_MCP_PROJECT", str(a))
    assert flp_reader.find_project_file(tmp_path)[0] == a
