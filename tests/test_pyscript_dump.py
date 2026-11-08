from pathlib import Path

import pytest

from fl_studio_mcp import pianoroll, pyscript_gen


def test_dump_script_embeds_nonce_path_and_reads_score():
    src = pyscript_gen.render_dump_script(
        "ab12cd34", r"C:\x\Piano roll scripts\MCP_SCORE_DUMP.json")
    assert "ab12cd34" in src
    assert "MCP_SCORE_DUMP.json" in src
    assert "flpianoroll" in src and "score" in src and "getNote" in src
    for forbidden in ("addNote", "clearNotes", "deleteNote"):
        assert forbidden not in src


def test_dump_script_lists_every_note_field_once():
    from fl_studio_mcp.music.score_notes import NOTE_FIELDS

    src = pyscript_gen.render_dump_script("ab12cd34", "d.json")
    fields_line = next(line for line in src.splitlines() if line.startswith("FIELDS = "))
    for name in NOTE_FIELDS:
        assert fields_line.count(f"'{name}'") == 1


def test_dump_script_reports_errors_instead_of_silently_failing():
    src = pyscript_gen.render_dump_script("ab12cd34", "d.json")
    assert '"ok": False' in src
    assert "except Exception" in src


def test_dump_script_rejects_bad_nonce():
    with pytest.raises(ValueError):
        pyscript_gen.render_dump_script("bad nonce!", "x.json")
    with pytest.raises(ValueError):
        pyscript_gen.render_dump_script("abc", "x.json")


def test_write_dump_script_handles_non_ascii_paths(tmp_path):
    """FL's script folder lives under the user profile; CJK/accented paths are normal."""
    scripts = tmp_path / "练习" / "Piano roll scripts"
    scripts.mkdir(parents=True)
    path = pyscript_gen.write_dump_script(
        "ab12cd34", str(scripts / "MCP_SCORE_DUMP.json"), str(scripts))
    text = Path(path).read_text(encoding="utf-8")
    assert "ab12cd34" in text and "练习" in text


def test_dump_score_reports_unwritable_script_as_such(tmp_path, monkeypatch):
    """A write failure must not escape as a generic live-read crash."""
    def boom(*args, **kwargs):
        raise UnicodeEncodeError("ascii", "练习", 0, 1, "ordinal not in range(128)")

    monkeypatch.setattr(pianoroll, "write_dump_script", boom)
    res = pianoroll.dump_score(nonce="ab12cd34", timeout=0.2,
                               scripts_dir=str(tmp_path), trigger=lambda: {"sent_hotkey": True})
    assert res["ok"] is False and res["error"] == "script write failed"


def test_write_dump_script_overwrites_mcp_apply(tmp_path):
    path = pyscript_gen.write_dump_script(
        "ab12cd34", str(tmp_path / "d.json"), str(tmp_path))
    assert path == str(tmp_path / "MCP_Apply.pyscript")
    assert "ab12cd34" in (tmp_path / "MCP_Apply.pyscript").read_text(encoding="ascii")
