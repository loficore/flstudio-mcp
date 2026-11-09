import json

from fl_studio_mcp import pianoroll


def _trigger_writes(path, nonce, *, ok=True):
    def go():
        path.write_text(json.dumps({"ok": True, "nonce": nonce, "notes": []}))
        return {"sent_hotkey": True, "focused": True}
    return go


def test_dump_score_returns_matching_payload(tmp_path):
    dump = tmp_path / "MCP_SCORE_DUMP.json"
    res = pianoroll.dump_score(
        nonce="ab12cd34", timeout=1.0, scripts_dir=str(tmp_path),
        trigger=_trigger_writes(dump, "ab12cd34"))
    assert res["ok"] is True and res["payload"]["nonce"] == "ab12cd34"
    assert res["dump_path"] == str(dump)


def test_stale_dump_is_deleted_before_trigger(tmp_path):
    dump = tmp_path / "MCP_SCORE_DUMP.json"
    dump.write_text(json.dumps({"ok": True, "nonce": "ab12cd34"}))
    res = pianoroll.dump_score(
        nonce="ab12cd34", timeout=0.3, scripts_dir=str(tmp_path),
        trigger=lambda: {"sent_hotkey": True})
    assert res["ok"] is False and res["error"] == "dump timeout"
    assert not dump.exists()


def test_dump_with_old_nonce_is_not_accepted(tmp_path):
    dump = tmp_path / "MCP_SCORE_DUMP.json"

    def trigger():
        dump.write_text(json.dumps({"ok": True, "nonce": "deadbeef"}))
        return {"sent_hotkey": True}

    res = pianoroll.dump_score(nonce="ab12cd34", timeout=0.3,
                               scripts_dir=str(tmp_path), trigger=trigger)
    assert res["ok"] is False and res["error"] == "dump timeout"


def test_dump_with_ok_false_is_reported(tmp_path):
    dump = tmp_path / "MCP_SCORE_DUMP.json"

    def trigger():
        dump.write_text(json.dumps({"ok": False, "nonce": "ab12cd34",
                                    "error": "ValueError: boom"}))
        return {"sent_hotkey": True}

    res = pianoroll.dump_score(nonce="ab12cd34", timeout=1.0,
                               scripts_dir=str(tmp_path), trigger=trigger)
    assert res["ok"] is False and res["error"] == "dump failed"
    assert "boom" in res["detail"]


def test_trigger_failure_is_reported(tmp_path):
    res = pianoroll.dump_score(nonce="ab12cd34", timeout=0.3, scripts_dir=str(tmp_path),
                               trigger=lambda: {"sent_hotkey": False, "error": "no focus"})
    assert res["ok"] is False and res["error"] == "trigger failed"
    assert "no focus" in (res.get("detail") or "")


def test_unwritable_scripts_dir_is_reported(tmp_path):
    res = pianoroll.dump_score(nonce="ab12cd34", timeout=0.2,
                               scripts_dir=str(tmp_path / "missing"),
                               trigger=lambda: {"sent_hotkey": True})
    assert res["ok"] is False and res["error"] == "script write failed"


def test_generated_nonce_is_valid_when_omitted(tmp_path):
    dump = tmp_path / "MCP_SCORE_DUMP.json"
    seen = {}

    def trigger():
        text = (tmp_path / "MCP_Apply.pyscript").read_text()
        nonce = text.split("NONCE = ", 1)[1].splitlines()[0].strip().strip("'\"")
        seen["nonce"] = nonce
        dump.write_text(json.dumps({"ok": True, "nonce": nonce, "notes": []}))
        return {"sent_hotkey": True}

    res = pianoroll.dump_score(timeout=1.0, scripts_dir=str(tmp_path), trigger=trigger)
    assert res["ok"] is True
    assert seen["nonce"] == res["payload"]["nonce"]
    assert len(seen["nonce"]) == 8
