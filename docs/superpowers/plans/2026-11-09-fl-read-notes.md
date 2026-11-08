# 读取 FL 工程音符 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 新增 `fl_get_piano_roll_notes`，读取当前钢琴卷帘（live）或工程文件（autosave）的音符，供 AI 分析与改写。

**Architecture:** 两条后端共用一个工具。live 后端生成只读 pyscript，把 `flp.score` 写成脚本目录内的 JSON，服务端轮询读取；autosave 后端用零依赖解析器读最新的 `.flp`。`source="auto"` 先 live，失败回退 autosave。

**Tech Stack:** Python 3.10+、FastMCP、mido（既有）、pytest、stdlib（`struct` / `json` / `pathlib`）。

**Spec:** `docs/superpowers/specs/2026-11-09-fl-read-notes-design.md`

## Global Constraints

- 不新增第三方依赖；`.flp` 解析只用 stdlib。
- 不引入 PyFLP（GPL-3.0，且 2.2.1 在 Python 3.12+ 报错）。
- 读取工具是只读：不调用 `addNote` / `clearNotes` / `deleteNote`，不进 `safety.safe_write`。
- 不 bump `PROTOCOL_VERSION`；live 复用 `MCP_Apply.pyscript` 与现有触发。
- pyscript 只能读写 `Settings/Piano roll scripts/`；所有落盘路径必须在该目录内。
- JSON 一律 `ensure_ascii=True`；dump 文件用数组而非对象存音符。
- 拍号与 PPQ 必须来自数据，不得写死 4/4、480 或 96。
- 每个任务收尾：`python -m pytest tests/ -q` 与 `ruff check` 全绿后才 commit（任务 8 需要 FL，单独说明）。
- 文档中文：全角标点优先，中英文之间半角空格，代码标识符保留英文。

## Review Focus

1. **残留 dump**：上一轮的 `MCP_SCORE_DUMP.json` 还在，触发没落地时不得返回旧数据（nonce + 触发前删旧文件）。
2. **多个工程 / 自动备份**：`Projects/` 下有主文件与 `Backup/`，还有别的工程；必须选最新且可被 `FLSTUDIO_MCP_PROJECT` 覆盖。
3. **空 pattern**：没有任何音符时返回 `note_count: 0` 与空 `notes`，不是错误。
4. **非 4/4**：3/4、6/8 的 bars 换算不能套 4/4 公式。
5. **超大 pattern**：超过 `max_notes` 时截断并给摘要，不返回半截且不超时。

---

### Task 1: `flp_reader` 零依赖解析器

**Files:**
- Create: `src/fl_studio_mcp/music/flp_reader.py`
- Create: `tests/flp_fixture.py`
- Test: `tests/test_flp_reader.py`

**Interfaces:**
- Produces:
  - `class FlpError(ValueError)`
  - `@dataclass FlpNote`: `position:int`、`length:int`、`key:int`、`velocity:int`、`rack_channel:int`、`midi_channel:int`、`pan:int`、`fine_pitch:int`、`slide:bool`、`group:int`
  - `@dataclass FlpChannel`: `iid:int`、`name:str`
  - `@dataclass FlpPattern`: `iid:int`、`name:str`、`length:int`、`notes:list[FlpNote]`
  - `@dataclass FlpProject`: `version:str`、`ppq:int`、`tempo_bpm:float|None`、`channel_count:int`、`selected_pattern:int|None`、`channels:list[FlpChannel]`、`patterns:list[FlpPattern]`
  - `parse_flp(path: str | os.PathLike) -> FlpProject`
  - `find_project_file(root: str | os.PathLike | None = None) -> tuple[Path, float] | None`（返回 `(path, age_seconds)`）
  - `tests/flp_fixture.py` 提供 `build_flp(*, ppq=96, tempo=98000, patterns=..., channels=..., selected=1) -> bytes`，供本仓测试复用（`scripts/` 不能 import `tests/`，脚本需自带一份）

**Format facts（实现所需，来自真实文件验证）：**
- 头部 14 字节：`b"FLhd"` + `<I` `hsize`(=6) + `<h` `format` + `<H` `channel_count` + `<H` `ppq`；随后 `b"FLdt"` + `<I` 事件总长。
- 事件编码：id < 64 读 1 字节值；< 128 读 2 字节（u16）；< 192 读 4 字节（u32）；≥ 192 先读 varint 长度再读内容。
- varint：protobuf 风格，**首组为最高位**。解码：收集 7-bit 组，`for g in reversed(groups): n = (n << 7) | g`。
- 事件 id：`64` ChannelID.New（u16）、`192` ChannelID.Name（UTF-16LE）、`65` PatternID.New（u16）、`193` PatternID.Name（UTF-16LE）、`224` PatternID.Notes、`164` PatternID.Length（u32）、`67` PatternsID.CurrentlySelected（u16）、`156` ProjectID.Tempo（u32，BPM×1000）。
- `PatternID.New` 每个 pattern 出现两次（一次带 notes，一次带 name），必须按 `iid` 合并，不能重复追加。
- 音符记录 24 字节，`struct.Struct("<IHHIHHBBBBBBBB")`：position、flags、rack_channel、length、key、group、fine_pitch、保留、release、midi_channel、pan、velocity、mod_x、mod_y。`flags & 8` = slide。
- 未知事件跳过；文件截断 / 头部错误抛 `FlpError`。

- [ ] **Step 1: 写失败测试**

`tests/flp_fixture.py`（构造器单独放一个模块，Task 6 的测试复用）：

```python
import struct


def _varint(n):
    groups = []
    while True:
        groups.append(n & 0x7F)
        n >>= 7
        if not n:
            break
    groups.reverse()
    return bytes(g | (0x80 if i < len(groups) - 1 else 0)
                 for i, g in enumerate(groups))


def _event(eid, value):
    if eid < 64:
        return bytes([eid, value & 0xFF])
    if eid < 128:
        return bytes([eid]) + struct.pack("<H", value)
    if eid < 192:
        return bytes([eid]) + struct.pack("<I", value)
    data = bytes(value)
    return bytes([eid]) + _varint(len(data)) + data


def _text(eid, s):
    return _event(eid, s.encode("utf-16-le") + b"\x00\x00")


def _note(pos, length, key, vel, rack=0):
    return struct.pack("<IHHIHHBBBBBBBB", pos, 0, rack, length, key, 0,
                       120, 0, 64, 0, 64, vel, 128, 128)


def build_flp(*, ppq=96, tempo=98000, patterns=(("Chord", 1, [_note(0, 384, 60, 100)]),),
              channels=(("piano", 0), ("melody key", 1)), selected=1):
    ev = bytearray()
    for name, iid in channels:
        ev += _event(64, iid) + _text(192, name)
    for name, iid, notes in patterns:
        ev += _event(65, iid) + _text(193, name) + _event(164, 384)
        ev += _event(224, b"".join(notes))
        ev += _event(65, iid)
    ev += _event(67, selected) + _event(156, tempo)
    header = b"FLhd" + struct.pack("<I", 6) + struct.pack("<hHH", 0, len(channels), ppq)
    return header + b"FLdt" + struct.pack("<I", len(ev)) + bytes(ev)
```

`tests/test_flp_reader.py`：

```python
import pytest
from fl_studio_mcp.music import flp_reader
from flp_fixture import build_flp, _note


def test_parses_channels_patterns_notes_and_tempo(tmp_path):
    p = tmp_path / "x.flp"
    p.write_bytes(build_flp())
    proj = flp_reader.parse_flp(p)
    assert proj.ppq == 96 and proj.tempo_bpm == 98.0
    assert [(c.iid, c.name) for c in proj.channels] == [(0, "piano"), (1, "melody key")]
    assert proj.selected_pattern == 1
    assert len(proj.patterns) == 1
    pat = proj.patterns[0]
    assert (pat.iid, pat.name, pat.length) == (1, "Chord", 384)
    n = pat.notes[0]
    assert (n.key, n.position, n.length, n.velocity, n.rack_channel) == (60, 0, 384, 100, 0)


def test_rejects_bad_header(tmp_path):
    p = tmp_path / "x.flp"
    p.write_bytes(b"nope")
    with pytest.raises(flp_reader.FlpError):
        flp_reader.parse_flp(p)


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
```

- [ ] **Step 2: 运行确认失败** — `python -m pytest tests/test_flp_reader.py -q` → ImportError
- [ ] **Step 3: 实现 `src/fl_studio_mcp/music/flp_reader.py`** — dataclasses、varint、事件循环、按 `iid` 合并 pattern、`find_project_file`（默认 `paths.fl_settings_dir().parent / "Projects"`，递归 `*.flp`，按 mtime 取最新，mtime 缺失时跳过）。
- [ ] **Step 4: 运行确认通过** — 同上 → PASS
- [ ] **Step 5: Commit** — `git add src/fl_studio_mcp/music/flp_reader.py tests/test_flp_reader.py && git commit -m "feat(flp): dependency-free FLP pattern/note reader"`

---

### Task 2: 归一化层 `score_notes`

**Files:**
- Create: `src/fl_studio_mcp/music/score_notes.py`
- Test: `tests/test_score_notes.py`

**Interfaces:**
- Consumes: Task 1 的 `FlpProject` / `FlpPattern` / `FlpNote`。
- Produces:
  - `NOTE_FIELDS: tuple[str, ...]`（dump 载荷的音符字段顺序，Task 3 的脚本直接引用它）
  - `NOTE_NAMES: tuple[str, ...]`（12 个音名，用于 `name` 字段）
  - `beats_per_bar(tsnum: int, tsden: int) -> float`
  - `make_note(*, pitch, start_ticks, length_ticks, velocity, pan, cents, slide, porta, ppq, beats_per_bar) -> dict`
  - `normalize_live(payload: dict, *, max_notes: int = 2000) -> dict`
  - `normalize_flp(project, *, pattern: int | None = None, channel: int | None = None, max_notes: int = 2000) -> dict`
  - `summarize(notes: list[dict], *, ppq: int, beats_per_bar: float) -> dict`

**统一音符形状：**

```python
{"pitch": int, "name": str, "start_ticks": int, "length_ticks": int,
 "start_beats": float, "length_beats": float, "start_bars": float,
 "length_bars": float, "velocity": int, "pan": float, "cents": int,
 "slide": bool, "porta": bool}
```

**换算：** `start_beats = start_ticks / ppq`；`start_bars = start_beats / beats_per_bar`；live velocity `round(v * 128)`；flp velocity 原值；pan：live 原值（0..1），flp `pan / 128`；cents：live `pitchofs`，flp `fine_pitch - 120`。

- [ ] **Step 1: 写失败测试**

```python
from fl_studio_mcp.music import score_notes, flp_reader


def test_beats_per_bar_handles_non_4_4():
    assert score_notes.beats_per_bar(4, 4) == 4.0
    assert score_notes.beats_per_bar(3, 4) == 3.0
    assert score_notes.beats_per_bar(6, 8) == 3.0


def test_normalize_live_converts_ticks_velocity_pan_cents():
    payload = {"ppq": 96, "tsnum": 4, "tsden": 4, "fields": list(score_notes.NOTE_FIELDS),
               "notes": [[60, 0, 48, 0.671875, 0.5, 0, False, False, -20,
                          0.5, 0.5, 0.5, 0, 0, False, False]]}
    out = score_notes.normalize_live(payload)
    n = out["notes"][0]
    assert (n["pitch"], n["start_ticks"], n["length_ticks"]) == (60, 0, 48)
    assert n["start_beats"] == 0.0 and n["length_bars"] == 0.125
    assert n["velocity"] == 86 and n["pan"] == 0.5 and n["cents"] == -20


def test_normalize_flp_filters_channel_and_pattern():
    proj = flp_reader.FlpProject(
        version="24.2.2", ppq=96, tempo_bpm=98.0, channel_count=2,
        selected_pattern=1,
        channels=[flp_reader.FlpChannel(0, "piano"), flp_reader.FlpChannel(1, "melody key")],
        patterns=[
            flp_reader.FlpPattern(1, "Chord", 384, [flp_reader.FlpNote(0, 384, 60, 100, 0, 0, 64, 120, False, 0)]),
            flp_reader.FlpPattern(2, "Melody", 768, [flp_reader.FlpNote(0, 96, 62, 90, 1, 0, 64, 120, False, 0)]),
        ])
    out = score_notes.normalize_flp(proj, pattern=2, channel=1)
    assert out["pattern_name"] == "Melody" and out["channel_name"] == "melody key"
    assert out["note_count"] == 1
    assert out["notes"][0]["pitch"] == 62 and out["notes"][0]["velocity"] == 90


def test_normalize_truncates_and_summarizes():
    payload = {"ppq": 96, "tsnum": 4, "tsden": 4, "fields": list(score_notes.NOTE_FIELDS),
               "notes": [[60 + i, i * 48, 48, 0.5, 0.5, 0, False, False, 0,
                          0.5, 0.5, 0.5, 0, 0, False, False] for i in range(3)]}
    out = score_notes.normalize_live(payload, max_notes=2)
    assert out["truncated"] is True and len(out["notes"]) == 2
    assert out["summary"]["pitch_min"] == 60 and out["summary"]["pitch_max"] == 62


def test_empty_score_is_not_an_error():
    out = score_notes.normalize_live({"ppq": 96, "tsnum": 4, "tsden": 4,
                                      "fields": list(score_notes.NOTE_FIELDS), "notes": []})
    assert out["ok"] is True and out["note_count"] == 0 and out["notes"] == []
    assert out["summary"]["pitch_min"] is None
```

- [ ] **Step 2: 运行确认失败** — `python -m pytest tests/test_score_notes.py -q` → ImportError
- [ ] **Step 3: 实现 `score_notes.py`** — `NOTE_FIELDS` 与 Task 3 的 dump 字段顺序保持一致；`summarize` 计算 `pitch_min/max`、12 格 `pitch_class_histogram`、`span_bars`、`notes_per_bar`（无音符时前两项为 `None`、直方图为 12 个 0）。
- [ ] **Step 4: 运行确认通过** — 同上 → PASS
- [ ] **Step 5: Commit** — `git add src/fl_studio_mcp/music/score_notes.py tests/test_score_notes.py && git commit -m "feat(score): normalize live dump and FLP notes"`

---

### Task 3: dump pyscript 生成

**Files:**
- Modify: `src/fl_studio_mcp/pyscript_gen.py`
- Test: `tests/test_pyscript_dump.py`

**Interfaces:**
- Produces:
  - `DUMP_FILE_NAME = "MCP_SCORE_DUMP.json"`
  - `render_dump_script(nonce: str, dump_path: str) -> str`
  - `write_dump_script(nonce: str, dump_path: str, scripts_dir: str | None = None) -> str`

`nonce` 只允许 `[a-z0-9]{8}`，否则 `ValueError`。脚本写入 `MCP_Apply.pyscript`，只读 `flp.score`。

- [ ] **Step 1: 写失败测试**

```python
import re
import pytest
from fl_studio_mcp import pyscript_gen


def test_dump_script_embeds_nonce_path_and_reads_score():
    src = pyscript_gen.render_dump_script("ab12cd34", r"C:\x\Piano roll scripts\MCP_SCORE_DUMP.json")
    assert "ab12cd34" in src
    assert "MCP_SCORE_DUMP.json" in src
    assert "flpianoroll" in src and "score" in src and "getNote" in src
    for forbidden in ("addNote", "clearNotes", "deleteNote"):
        assert forbidden not in src


def test_dump_script_rejects_bad_nonce():
    with pytest.raises(ValueError):
        pyscript_gen.render_dump_script("bad nonce!", "x.json")


def test_write_dump_script_overwrites_mcp_apply(tmp_path):
    path = pyscript_gen.write_dump_script("ab12cd34", str(tmp_path / "d.json"), str(tmp_path))
    assert path == str(tmp_path / "MCP_Apply.pyscript")
    assert "ab12cd34" in open(path, encoding="ascii").read()
```

- [ ] **Step 2: 运行确认失败** — `python -m pytest tests/test_pyscript_dump.py -q` → AttributeError
- [ ] **Step 3: 实现** `render_dump_script` / `write_dump_script`。脚本内容按 spec §5.2；路径用 `repr()` 烘焙；字段顺序从 `.music.score_notes` import `NOTE_FIELDS` 后拼成脚本里的字面量（pyscript 运行时不能 import 服务端模块）。
- [ ] **Step 4: 运行确认通过** — 同上 → PASS
- [ ] **Step 5: Commit** — `git add src/fl_studio_mcp/pyscript_gen.py tests/test_pyscript_dump.py && git commit -m "feat(pyscript): generate read-only score dump script"`

---

### Task 4: `dump_score` 编排

**Files:**
- Modify: `src/fl_studio_mcp/pianoroll.py`
- Test: `tests/test_dump_score.py`

**Interfaces:**
- Consumes: Task 3 的 `write_dump_script`、既有 `pyscript_trigger.trigger_run_last_script`。
- Produces: `dump_score(nonce: str | None = None, timeout: float = 8.0, scripts_dir: str | None = None, trigger=None) -> dict`
  - 成功：`{"ok": True, "payload": dict, "dump_path": str}`
  - 失败：`{"ok": False, "error": "dump timeout" | "trigger failed", "detail": str}`

行为：
1. 生成 8 位 nonce（未传时）。
2. 删除旧 dump（失败忽略）。
3. `write_dump_script(nonce, dump_path)`。
4. `trigger()`（默认 `trigger_run_last_script`）。
5. 每 100 ms 读 dump，直到 JSON 可解析且 `nonce` 匹配；超时返回错误。

- [ ] **Step 1: 写失败测试**

```python
import json
from fl_studio_mcp import pianoroll


def _trigger_writes(path, nonce):
    def go():
        path.write_text(json.dumps({"ok": True, "nonce": nonce, "notes": []}))
        return {"sent_hotkey": True, "focused": True}
    return go


def test_dump_score_returns_matching_payload(tmp_path):
    dump = tmp_path / "MCP_SCORE_DUMP.json"
    res = pianoroll.dump_score(nonce="ab12cd34", timeout=1.0,
                               scripts_dir=str(tmp_path), trigger=_trigger_writes(dump, "ab12cd34"))
    assert res["ok"] is True and res["payload"]["nonce"] == "ab12cd34"


def test_stale_dump_is_deleted_before_trigger(tmp_path):
    dump = tmp_path / "MCP_SCORE_DUMP.json"
    dump.write_text(json.dumps({"ok": True, "nonce": "ab12cd34"}))
    res = pianoroll.dump_score(nonce="ab12cd34", timeout=0.3,
                               scripts_dir=str(tmp_path), trigger=lambda: {"sent_hotkey": True})
    assert res["ok"] is False and res["error"] == "dump timeout"


def test_trigger_failure_is_reported(tmp_path):
    res = pianoroll.dump_score(nonce="ab12cd34", timeout=0.3, scripts_dir=str(tmp_path),
                               trigger=lambda: {"sent_hotkey": False, "error": "no focus"})
    assert res["ok"] is False and "no focus" in (res.get("detail") or "")
```

- [ ] **Step 2: 运行确认失败** — `python -m pytest tests/test_dump_score.py -q` → AttributeError
- [ ] **Step 3: 实现 `dump_score`**；`scripts_dir` 默认 `pyscript_gen.PIANO_ROLL_SCRIPTS_DIR`；`dump_path = os.path.join(scripts_dir, DUMP_FILE_NAME)`。
- [ ] **Step 4: 运行确认通过** — 同上 → PASS
- [ ] **Step 5: Commit** — `git add src/fl_studio_mcp/pianoroll.py tests/test_dump_score.py && git commit -m "feat(pianoroll): orchestrate live score dump"`

---

### Task 5: bridge 与 daemon 接线

**Files:**
- Modify: `src/fl_studio_mcp/connection.py`
- Modify: `src/fl_studio_mcp/daemon.py`
- Test: `tests/test_read_score_bridge.py`

**Interfaces:**
- Produces:
  - `FLBridge.read_score(self, channel: int | None = None, timeout: float = 8.0) -> dict`
  - `TCPBridge.read_score(self, channel: int | None = None, timeout: float = 8.0) -> dict`
  - daemon op `read_score`：`{"op": "read_score", "channel": int|null, "timeout": float}`

`FLBridge.read_score` 顺序：`channel` 非空时先 `CMD_CHANNEL_SELECT`，再 `CMD_ENSURE_PIANO_ROLL`，最后 `pianoroll.dump_score(...)`。两者失败都返回 `{"ok": False, "error": ...}`，不抛异常给工具层。

- [ ] **Step 1: 写失败测试**

```python
from fl_studio_mcp import connection, daemon


def test_fl_bridge_read_score_selects_channel_then_dumps(monkeypatch):
    b = connection.FLBridge.__new__(connection.FLBridge)
    calls = []
    monkeypatch.setattr(b, "call", lambda cmd, params=None, **kw: calls.append((cmd, params)) or {"ok": True})
    monkeypatch.setattr(connection.pianoroll, "dump_score", lambda **kw: {"ok": True, "payload": {"x": 1}})
    res = b.read_score(channel=3)
    assert res["ok"] is True
    assert calls[0][0] == "channel_select" and calls[1][0] == "ensure_piano_roll"


def test_tcp_bridge_read_score_sends_op(monkeypatch):
    b = connection.TCPBridge.__new__(connection.TCPBridge)
    seen = {}
    monkeypatch.setattr(b, "_rpc", lambda req, timeout: seen.update(req) or {"ok": True, "payload": {}})
    b.read_score(channel=2, timeout=5.0)
    assert seen["op"] == "read_score" and seen["channel"] == 2


def test_daemon_read_score_delegates(monkeypatch):
    class B:
        def read_score(self, channel=None, timeout=8.0):
            return {"ok": True, "payload": {"channel": channel}}
    monkeypatch.setattr(daemon, "_get_bridge", lambda: B())
    res = daemon._handle_request({"op": "read_score", "channel": 4})
    assert res["ok"] is True and res["data"]["payload"]["channel"] == 4
```

- [ ] **Step 2: 运行确认失败** — `python -m pytest tests/test_read_score_bridge.py -q` → AttributeError
- [ ] **Step 3: 实现**。`connection.py` 顶部 import `from . import pianoroll`（避免循环依赖：`pianoroll` 只依赖 `pyscript_gen` / `pyscript_trigger`）。`daemon.py` 的 `read_score` 分支复用现有异常映射，返回 `{"ok": True, "data": res}`。
- [ ] **Step 4: 运行确认通过** — 同上 → PASS
- [ ] **Step 5: Commit** — `git add src/fl_studio_mcp/connection.py src/fl_studio_mcp/daemon.py tests/test_read_score_bridge.py && git commit -m "feat(bridge): read_score over direct and TCP transports"`

---

### Task 6: 工具 `fl_get_piano_roll_notes`

**Files:**
- Modify: `src/fl_studio_mcp/tools/pianoroll.py`
- Test: `tests/test_read_notes_tool.py`

**Interfaces:**
- Consumes: Task 1 `find_project_file` / `parse_flp`；Task 2 `normalize_live` / `normalize_flp`；Task 5 `read_score`。
- Produces: `fl_get_piano_roll_notes(channel=None, pattern=None, source="auto", max_notes=2000) -> dict`

流程：

1. `source in ("auto", "live")`：`bridge = get_bridge()`；`res = bridge.read_score(channel=channel)`。
2. live 成功 → `live_meta = _live_meta(bridge, channel)`（`get_project_state` / `pattern_list` / `channel_list`，失败字段置 `None`）；`pattern` 与当前 pattern 不一致且 `source="live"` → `pattern_mismatch`。
3. live 失败且 `source="auto"` → `fallback_reason = res["error"]`，继续 autosave；`source="live"` → 直接返回失败。
4. autosave：`found = find_project_file()`；`parse_flp`；`normalize_flp(pattern=pattern, channel=channel, max_notes=max_notes)`；补 `source="autosave"`、`file`、`age_seconds`、`fallback_reason`。
5. `source="auto"` 且给了非当前 pattern 时跳过 live，直接 autosave。

- [ ] **Step 1: 写失败测试**

```python
import json
import pytest
from fastmcp import Client
from fl_studio_mcp import connection
from fl_studio_mcp.server import build_server


class Daw:
    def __init__(self, result):
        self.result = result
    def call(self, cmd, params=None, **kw):
        return {"pattern_number": 2, "tempo_bpm": 98.0}
    def read_score(self, channel=None, timeout=8.0):
        return self.result


@pytest.mark.asyncio
async def test_live_read(monkeypatch):
    payload = {"ppq": 96, "tsnum": 4, "tsden": 4,
               "fields": ["number","time","length","velocity","pan","color","slide","porta",
                          "pitchofs","release","fcut","fres","group","repeats","selected","muted"],
               "notes": [[60, 0, 48, 0.78125, 0.5, 0, False, False, 0, 0.5, 0.5, 0.5, 0, 0, False, False]]}
    monkeypatch.setattr(connection, "_bridge", Daw({"ok": True, "payload": payload}))
    async with Client(build_server()) as c:
        r = await c.call_tool("fl_get_piano_roll_notes", {"source": "live"})
    out = r.data
    assert out["ok"] is True and out["source"] == "live" and out["note_count"] == 1
    assert out["notes"][0]["velocity"] == 100


@pytest.mark.asyncio
async def test_auto_falls_back_to_autosave(monkeypatch, tmp_path):
    from flp_fixture import build_flp
    f = tmp_path / "x.flp"; f.write_bytes(build_flp())
    monkeypatch.setenv("FLSTUDIO_MCP_PROJECT", str(f))
    monkeypatch.setattr(connection, "_bridge", Daw({"ok": False, "error": "dump timeout"}))
    async with Client(build_server()) as c:
        r = await c.call_tool("fl_get_piano_roll_notes", {"source": "auto"})
    out = r.data
    assert out["source"] == "autosave" and out["fallback_reason"] == "dump timeout"
    assert out["note_count"] == 1


@pytest.mark.asyncio
async def test_empty_pattern_truncation_and_missing_pattern(monkeypatch, tmp_path):
    from flp_fixture import build_flp, _note
    f = tmp_path / "x.flp"
    f.write_bytes(build_flp(patterns=(
        ("Empty", 1, []),
        ("Two", 2, [_note(0, 48, 60, 90), _note(48, 48, 62, 91)]),
    )))
    monkeypatch.setenv("FLSTUDIO_MCP_PROJECT", str(f))
    async with Client(build_server()) as c:
        empty = await c.call_tool("fl_get_piano_roll_notes", {"source": "autosave", "pattern": 1})
        cut = await c.call_tool("fl_get_piano_roll_notes", {"source": "autosave", "pattern": 2, "max_notes": 1})
        missing = await c.call_tool("fl_get_piano_roll_notes", {"source": "autosave", "pattern": 99})
    assert empty.data["ok"] is True and empty.data["note_count"] == 0
    assert cut.data["truncated"] is True and len(cut.data["notes"]) == 1
    assert missing.data["ok"] is False and missing.data["error"] == "pattern_not_found"
```

- [ ] **Step 2: 运行确认失败** — `python -m pytest tests/test_read_notes_tool.py -q` → tool 不存在
- [ ] **Step 3: 实现工具**。`_live_meta` 用 `bridge.call("get_project_state", {})`，`pattern_list` / `channel_list` 取不到就置 `None`。`max_notes` 用 `Field(ge=1, le=20000)`。注解 `readOnlyHint=True`。失败时 `ok=False` 且带 `error`；回退时 `fallback_reason` 非空。
- [ ] **Step 4: 运行确认通过** — 同上 → PASS
- [ ] **Step 5: Commit** — `git add src/fl_studio_mcp/tools/pianoroll.py tests/test_read_notes_tool.py && git commit -m "feat(tool): fl_get_piano_roll_notes with auto fallback"`

---

### Task 7: 离线端到端脚本

**Files:**
- Create: `scripts/test_read_notes.py`
- Modify: `tests/test_offline.py`

**Interfaces:**
- Consumes: Task 6 的工具。
- Produces: `scripts/test_read_notes.py`，退出码 0/1，沿用 `scripts/test_midi_export.py` 的 `check()` 风格。

脚本内容：在同目录临时目录构造一个 1 pattern / 1 channel / 2 音符的 `.flp`，设 `FLSTUDIO_MCP_PROJECT`，调用 `fl_get_piano_roll_notes(source="autosave")`，断言 `note_count`、`pitch`、`velocity`、`ppq`、`time_signature`。构造器可复制 Task 1 的 `build_flp`（脚本不能 import tests）。

- [ ] **Step 1: 写脚本并先跑一次确认失败**（工具尚未接线时手写一个临时副本可跳过此步；接线后直接跑）
- [ ] **Step 2: 运行** — `python scripts/test_read_notes.py` → 全部 PASS
- [ ] **Step 3: 纳入离线清单** — `tests/test_offline.py` 的 `OFFLINE` 列表追加 `"read_notes"`
- [ ] **Step 4: 运行** — `python -m pytest tests/test_offline.py -q` → PASS
- [ ] **Step 5: Commit** — `git add scripts/test_read_notes.py tests/test_offline.py && git commit -m "test: offline read-notes autosave path"`

---

### Task 8: 在线 live 校验脚本

**Files:**
- Create: `scripts/test_read_notes_live.py`

**Interfaces:**
- Consumes: Task 5 的 `FLBridge.read_score` 或 Task 6 的工具。
- Produces: 现场脚本；无 FL / 无 arm 时退出码 77（skip），与 `scripts/test_daemon_tcp.py` 一致。

步骤：`fl_ping` 检查心跳 → 提示“钢琴卷帘打开并 arm 一次” → 调 `fl_get_piano_roll_notes(source="live", channel=...)` → 断言 `note_count > 0`、`ppq > 0`、`source == "live"`；再提示手动改一个音符后重跑，确认读到改动。失败时打印 arm 指引，不吞异常。

- [ ] **Step 1: 写脚本**
- [ ] **Step 2: 运行** — Windows + FL：`python scripts/test_read_notes_live.py` → PASS 或 77
- [ ] **Step 3: Commit** — `git add scripts/test_read_notes_live.py && git commit -m "test: live read-notes probe"`

---

### Task 9: 文档

**Files:**
- Modify: `README.md`
- Modify: `docs/NOTE_BRIDGE_SETUP.md`
- Modify: `ROADMAP.md`
- Modify: `CHANGELOG.md`

内容：

- README 功能列表加一条“Read the open piano roll / project notes”；Limitations 说明：live 需要 arm 一次，autosave 最多约 1 分钟延迟。
- `docs/NOTE_BRIDGE_SETUP.md`：读取复用同一个 `MCP_Apply` arm；失败时先跑一次菜单。
- `ROADMAP.md`：Phase 4 的 `fl_piano_get_notes` 标记完成（实际工具名 `fl_get_piano_roll_notes`）。
- `CHANGELOG.md`：Unreleased / Added 加一行。

- [ ] **Step 1: 改四处文档**
- [ ] **Step 2: 运行** — `python -m pytest tests/ -q` 与 `ruff check` → 全绿
- [ ] **Step 3: Commit** — `git add README.md docs/NOTE_BRIDGE_SETUP.md ROADMAP.md CHANGELOG.md && git commit -m "docs: document read-notes tool and arm requirement"`

---

## Self-Review

- **Spec coverage**：§5.1 工具 → Task 6；§5.2 dump 脚本 → Task 3/4；§5.3 bridge/daemon → Task 5；§5.4 flp_reader → Task 1；§6 数据契约 → Task 2/6；§7 错误处理 → Task 4/6；§9 测试 → Task 7/8；§10 风险 → Review Focus。
- **类型一致性**：`read_score` 返回 `{"ok", "payload", "dump_path"}`；`normalize_live(payload)` 只吃 `payload`；Task 5/6 的签名一致。
- **比例**：计划以任务与断言为主，未贴实现体。
- **开放项**：live 触发在 24.2.2 的可靠性只能由 Task 8 现场确认；若 `Ctrl+Alt+Y` 不可用，`source="auto"` 仍能工作（回退 autosave），只是 `source` 会显示 `autosave`。
