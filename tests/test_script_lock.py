"""Reads and writes share MCP_Apply.pyscript and one hotkey -- they must not interleave."""

import threading
import time

from fl_studio_mcp import pianoroll, pyscript_trigger


def test_read_cannot_run_while_a_write_is_triggering(tmp_path, monkeypatch):
    events = []
    apply_trigger_started = threading.Event()
    release_apply = threading.Event()

    def fake_write_apply(notes, mode="replace", scripts_dir=None):
        events.append("apply-write")
        return str(tmp_path / "MCP_Apply.pyscript")

    def fake_write_dump(nonce, dump_path, scripts_dir=None):
        events.append("dump-write")
        return str(tmp_path / "MCP_Apply.pyscript")

    def fake_trigger():
        events.append("apply-trigger")
        apply_trigger_started.set()
        release_apply.wait(5)
        events.append("apply-trigger-done")
        return {"sent_hotkey": True}

    monkeypatch.setattr(pianoroll, "write_apply_script", fake_write_apply)
    monkeypatch.setattr(pianoroll, "write_dump_script", fake_write_dump)
    monkeypatch.setattr(pyscript_trigger, "trigger_run_last_script", fake_trigger)

    def run_apply():
        pianoroll.apply_notes([{"pitch": 60, "time_bars": 0, "length_bars": 1,
                                "velocity": 0.8}])

    worker = threading.Thread(target=run_apply)
    worker.start()
    assert apply_trigger_started.wait(5), "apply never reached its trigger"

    dump = tmp_path / "MCP_SCORE_DUMP.json"

    def dump_trigger():
        events.append("dump-trigger")
        dump.write_text('{"ok": true, "nonce": "ab12cd34", "notes": []}')
        return {"sent_hotkey": True}

    result = {}

    def run_read():
        result.update(pianoroll.dump_score(nonce="ab12cd34", timeout=3.0,
                                           scripts_dir=str(tmp_path),
                                           trigger=dump_trigger))

    reader = threading.Thread(target=run_read)
    reader.start()
    time.sleep(0.2)
    release_apply.set()
    worker.join(5)
    reader.join(5)

    assert result.get("ok") is True, result
    # The read must not touch the script until the write's trigger is done.
    assert events.index("dump-write") > events.index("apply-trigger-done"), events
