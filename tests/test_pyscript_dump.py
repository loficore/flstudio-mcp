import pytest

from fl_studio_mcp import pyscript_gen


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


def test_write_dump_script_overwrites_mcp_apply(tmp_path):
    path = pyscript_gen.write_dump_script(
        "ab12cd34", str(tmp_path / "d.json"), str(tmp_path))
    assert path == str(tmp_path / "MCP_Apply.pyscript")
    assert "ab12cd34" in (tmp_path / "MCP_Apply.pyscript").read_text(encoding="ascii")
