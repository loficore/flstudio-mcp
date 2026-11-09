"""Piano-roll note authoring -- the generate-script bridge.

FL's pyscript sandbox blocks file I/O (read AND write), so we can't pass note
data via a job file. Instead we GENERATE a .pyscript with the notes baked in
(pyscript_gen), write it into FL's Piano roll scripts folder, then force-focus
FL and fire the platform "Run last script again" shortcut
(Ctrl+Alt+Y on Windows, Cmd+Opt+Y on macOS). FL re-reads the file on that
hotkey, so the fresh notes apply with no manual click.

This runs in a process that can write files + send keystrokes (the daemon, or
a normally-launched MCP server). Under the Store/MSIX Claude Desktop the MCP
server can't, so it delegates here via the daemon's "apply_notes" op.

One-time setup the user must do: run MCP_Apply once from the piano-roll
Scripting menu so it becomes FL's "last script".
"""

from __future__ import annotations

import contextlib
import json
import os
import secrets
import sys
import threading
import time

from .pyscript_gen import (
    DUMP_FILE_NAME,
    PIANO_ROLL_SCRIPTS_DIR,
    write_apply_script,
    write_dump_script,
    write_quantize_script,
)

DEFAULT_DUMP_TIMEOUT = 8.0
_POLL_INTERVAL = 0.1
_LOCK_TIMEOUT = 15.0

# Reading and writing share one file (MCP_Apply.pyscript) and one hotkey, so a
# read that lands between a write's script-write and its hotkey would run the
# wrong script (notes silently not applied, or a read's "dump" script applying
# notes instead). Every write-trigger-wait sequence therefore holds a lock.
_SCRIPT_LOCK = threading.RLock()


@contextlib.contextmanager
def script_lock(scripts_dir=None, timeout=_LOCK_TIMEOUT):
    """Serialize the write-script -> hotkey -> wait sequence.

    The in-process lock covers the daemon's worker threads. A lock file next to
    the script extends it to a second process (direct-mode MCP server plus a
    daemon); if that file cannot be locked the in-process lock still holds.
    """
    deadline = time.monotonic() + float(timeout)
    if not _SCRIPT_LOCK.acquire(timeout=float(timeout)):
        raise TimeoutError("another piano-roll script is still running")
    handle = None
    try:
        handle = _lock_file(scripts_dir, deadline)
        yield
    finally:
        if handle is not None:
            _unlock_file(handle)
        _SCRIPT_LOCK.release()


def _lock_file(scripts_dir, deadline):
    if scripts_dir is None:
        return None
    path = os.path.join(scripts_dir, ".mcp_script.lock")
    try:
        handle = open(path, "a+b")
    except OSError:
        return None
    try:
        while True:
            try:
                if sys.platform == "win32":
                    import msvcrt
                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                return handle
            except OSError:
                if time.monotonic() >= deadline:
                    handle.close()
                    raise TimeoutError("another process is running a piano-roll script")
                time.sleep(_POLL_INTERVAL)
    except Exception:
        handle.close()
        raise


def _unlock_file(handle):
    try:
        if sys.platform == "win32":
            import msvcrt
            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    except OSError:
        pass
    finally:
        handle.close()


def _new_nonce() -> str:
    return secrets.token_hex(4)


def _read_dump(path: str, nonce: str) -> dict | None:
    """Return the dump payload if it is fresh and complete, else None."""
    try:
        with open(path, encoding="utf-8") as f:
            payload = json.load(f)
    except (OSError, ValueError):
        return None
    if not isinstance(payload, dict) or payload.get("nonce") != nonce:
        return None
    return payload


def dump_score(nonce=None, timeout=DEFAULT_DUMP_TIMEOUT, scripts_dir=None, trigger=None):
    """Run the dump script and return the JSON it wrote.

    Deletes any leftover dump first so a trigger that never lands cannot be
    mistaken for a fresh read; the nonce check covers the race where FL
    rewrites the file while we poll.
    """
    nonce = nonce or _new_nonce()
    scripts_dir = scripts_dir or PIANO_ROLL_SCRIPTS_DIR
    dump_path = os.path.join(scripts_dir, DUMP_FILE_NAME)

    try:
        with script_lock(scripts_dir):
            with contextlib.suppress(OSError):   # nothing stale to remove
                os.remove(dump_path)

            try:
                write_dump_script(nonce, dump_path, scripts_dir)
            except Exception as e:    # encoding errors are ValueError, not OSError
                return {"ok": False, "error": "script write failed",
                        "detail": f"{type(e).__name__}: {e}"}

            if trigger is None:
                from .pyscript_trigger import trigger_run_last_script
                trigger = trigger_run_last_script

            try:
                trig = trigger() or {}
            except Exception as e:    # trigger is best-effort; report, don't raise
                return {"ok": False, "error": "trigger failed",
                        "detail": f"{type(e).__name__}: {e}"}

            if trig.get("error") or trig.get("sent_hotkey") is False:
                return {"ok": False, "error": "trigger failed",
                        "detail": str(trig.get("error")
                                      or "the run-last-script shortcut was not sent")}

            deadline = time.monotonic() + float(timeout)
            while True:
                payload = _read_dump(dump_path, nonce)
                if payload is not None:
                    if not payload.get("ok", True):
                        return {"ok": False, "error": "dump failed",
                                "detail": str(payload.get("error")
                                              or "the script reported an error"),
                                "dump_path": dump_path}
                    return {"ok": True, "payload": payload, "dump_path": dump_path}
                if time.monotonic() >= deadline:
                    return {"ok": False, "error": "dump timeout", "dump_path": dump_path,
                            "detail": (f"No dump appeared within {float(timeout):.1f}s. Open "
                                       "the Piano roll and run 'MCP Apply' once from its "
                                       "Scripting menu to arm Run last script again.")}
                time.sleep(_POLL_INTERVAL)
    except TimeoutError as e:
        return {"ok": False, "error": "script busy", "detail": str(e)}


def apply_notes(notes, mode="replace", trigger=True, quantize=None, snap_ends=False):
    """Write a pyscript into MCP_Apply.pyscript and (optionally) trigger FL.

    Normally writes the given notes. If ``quantize`` (grid in bars) is set, writes
    a script that instead reads the score and snaps existing notes to that grid.
    Returns {ok, ..., script, triggered, focused, hint?}.
    """
    try:
        with script_lock(PIANO_ROLL_SCRIPTS_DIR):
            return _apply_notes_locked(notes, mode, trigger, quantize, snap_ends)
    except TimeoutError as e:
        return {"ok": False, "error": "script busy",
                "hint": f"Another piano-roll script is running ({e}); retry."}


def _apply_notes_locked(notes, mode, trigger, quantize, snap_ends):
    if quantize is not None:
        path = write_quantize_script(float(quantize), snap_ends)
        result = {"ok": True, "action": "quantize", "grid_bars": float(quantize),
                  "snap_ends": bool(snap_ends), "script": path}
    else:
        path = write_apply_script(notes, mode)
        result = {"ok": True, "count": len(notes), "script": path, "mode": mode}

    if not trigger:
        result["triggered"] = False
        return result

    try:
        from .pyscript_trigger import trigger_run_last_script
        trig = trigger_run_last_script()
        shortcut = trig.get("shortcut", "Ctrl+Alt+Y")
        result["triggered"] = bool(trig.get("sent_hotkey", True))
        result["focused"] = trig.get("focused", False)
        if trig.get("error"):
            result["error"] = trig["error"]
        # The trigger can't confirm the script actually ran. The Piano roll is
        # auto-opened by the caller, but the shortcut only fires OUR script if
        # MCP_Apply was run once this FL session (no API to arm it). So if notes
        # don't appear, that one-time arm is the cause.
        result["setup"] = ("If notes did not appear: run 'MCP Apply' ONCE from the "
                           "Piano roll Scripting menu this FL session (the only "
                           "manual step -- arms the shortcut; no FL API to automate it).")
        if not result["triggered"]:
            result["hint"] = (
                "Notes written but auto-trigger is unsupported here. Click "
                f"the FL Piano roll and press {shortcut} to apply."
            )
        elif not trig.get("focused"):
            result["hint"] = (
                "Could not focus FL automatically -- click the FL Piano roll "
                f"and press {shortcut}."
            )
    except Exception as e:
        # Script is written regardless; trigger is best-effort.
        result["triggered"] = False
        result["error"] = f"{type(e).__name__}: {e}"
        result["hint"] = (
            f"Notes written but auto-trigger failed ({e}). Click the FL Piano "
            "roll and press the run-last-script shortcut to apply."
        )
    return result
