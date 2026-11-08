# flstudio-mcp — Note bridge: how it works + the ONE setup step

**Version:** 1.1 · **Env:** FL Studio 24.2.2 and 25.2.5, Windows · **Date:** 2026-11-09

## Why a "bridge" at all
FL's Piano-roll pyscript sandbox blocks file I/O outside its own script folder
(reading and writing). Instead the daemon **generates** `MCP_Apply.pyscript`
with the notes baked in, then fires FL's **"Run last script again" (Ctrl+Alt+Y)**
— FL re-reads the file each time, applying the fresh notes.

> Files *inside* `Settings/Piano roll scripts/` are readable and writable from a
> pyscript (verified on FL 24.2.2). Paths anywhere else — including `%TEMP%` and
> the parent folder — fail with `SystemError`, and `os.makedirs` is refused.

## Hardening (what's automated)
- **Piano roll auto-opens.** Before every note-write the controller runs
  `ui.showWindow(widPianoRoll)` (proven to open it from a *closed* state). The
  response carries `piano_roll_ensured`. → no manual "open the piano roll".
- **Channel targeting.** `fl_arrange_select_channel(n)` selects the channel; the
  piano roll follows it, so notes go to the intended instrument.

## The ONE irreducible manual step (per FL session)
**Run `MCP Apply` once from the Piano roll's Scripting menu at session start.**

This *arms* `Ctrl+Alt+Y` to target our script. There is **no API to automate
it**: the FL `ui` module exposes only browser-menu navigation + window/focus
(no run-script function), and the only ways to run a piano-roll script are the
Scripting menu or `Ctrl+Alt+Y` (which needs a prior menu run). So this one click
is unavoidable.

After that single arm, **every** note-write this session is fully automatic
(auto-open + trigger) — verified live: post-arm, writing E5 replaced C5 with no
manual action.

## Reading notes back
Reading reuses the same `MCP_Apply` file and the same one-time arm. The tool
`fl_get_piano_roll_notes` writes a read-only variant of the script, which dumps
`flpianoroll.score` to `MCP_SCORE_DUMP.json` in the same folder the pyscript is
allowed to write — FL's pyscript sandbox refuses paths outside its own script
directory, so that is the only viable hand-off.

`source="auto"` tries the live dump first and falls back to parsing FL's newest
project file, including the automatic backup in `Projects/Backup/`. The fallback
needs no arm and no open window, but it can lag the screen by up to about a
minute. The response reports `source`, `file` and `age_seconds`, plus
`fallback_reason` when the live path was skipped or failed.

A live read that times out is almost always the un-armed state above, so run
`MCP Apply` once and retry before assuming a bug.

## Caveat
`triggered: true` only means the daemon sent the hotkey + focused FL — it can't
confirm the script ran. If notes don't appear, the cause is almost always the
un-armed state above; the `setup` field in every `apply_notes` response says so.

## Session start checklist (the whole setup)
1. loopMIDI ports up + FL open + controller loaded + daemon running (`fl_ping`).
2. **Run `MCP Apply` once** from the Piano roll Scripting menu.
That's it — arrangement / note-writes are automatic from there.
