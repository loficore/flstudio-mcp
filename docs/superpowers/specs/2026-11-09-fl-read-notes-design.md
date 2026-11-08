# 读取 FL 工程音符（read notes）设计

**Status:** Approved in conversation (2026-11-09) — 方案 A + B（live 实时优先，autosave 回退）
**Created:** 2026-11-09
**Spec location:** `docs/superpowers/specs/2026-11-09-fl-read-notes-design.md`
**Planned implementation:** `docs/superpowers/plans/2026-11-09-fl-read-notes.md`
**Target:** FL Studio 2024 (24.2.2.4597) / Windows 为主，macOS 同构

---

## 1. Intent

fl-studio-mcp 现在只能往 FL Studio 写音符，读不回来。写旋律卡住时，AI 看不到工程里已有的音符，只能给通用建议。

本设计补一条读取通道：AI 读出当前钢琴卷帘（或工程内指定 pattern / channel）的音符，用于指出问题、给出变体，或配合既有的写工具直接改写。面向使用 fl-studio-mcp 的编曲者，以及维护本仓的开发者。

## 2. 已确认的事实

探针环境：FL Studio 2024 v24.2.2.4597，工程 `练习35.flp`，98 BPM，PPQ 96，4/4。

| 事实 | 结果 |
|---|---|
| pyscript 读 `flpianoroll.score` | 可用。实测 `melody key` 通道 59 个音符，字段含 number / time / length / velocity / pan / color / slide / porta / pitchofs / release / fcut / fres / group / repeats / selected / muted |
| pyscript 写文件 | 仅 `Settings/Piano roll scripts/` 目录内可用 |
| pyscript 读文件 | 同上，仅该目录内可用 |
| pyscript 写父目录 / `%TEMP%` / 用户目录 | `SystemError`，被沙箱拒绝 |
| `os.makedirs` | 被拒绝 |
| `.flp` 解析 | 自研零依赖解析器可用：pattern `Chord` 38 音符、`Melody` 150 音符、PPQ 96、98 BPM、通道名 |
| 自动备份 | 约每分钟一次 |
| 现有 note bridge | `MCP_Apply.pyscript` + 菜单 arm 一次 + `Ctrl+Alt+Y` 触发 |

两个附带结论：

- pyscript 的 velocity 是 0..1 且 1.0 = 128；换算 MIDI 力度用 `round(v * 128)`。`.flp` 里的 velocity 已经是 0..127 的整数。
- PyFLP 不采用：GPL-3.0，且 2.2.1 在 Python 3.12+ 直接报错（`EventEnum` 为空枚举）。

## 3. 核心决策一览

| 决策点 | 取值 | 理由 |
|---|---|---|
| 读取通道 | live 实时 + autosave 回退，`source="auto"` | live 精确但依赖 arm 与触发；autosave 全自动但有延迟 |
| live 的数据出口 | 脚本目录内的 JSON 文件 | pyscript 沙箱只允许该目录；绕开 SysEx 1.5 KB 限制 |
| 触发方式 | 复用现有 `MCP_Apply.pyscript` + `trigger_run_last_script` | 与写音符共用同一个 arm，不新增设置项 |
| `.flp` 解析 | 自研零依赖 | 避开 GPL 与 Python 版本问题 |
| 时间单位 | ticks 为准，另给 beats / bars | PPQ 随工程变（实测 96），不能写死 480；拍号同理不能写死 4/4 |
| 安全层 | 读取工具不进 snapshot / changelog | 不修改工程，没有可回滚对象 |
| 大 pattern | `max_notes` 默认 2000，超出附摘要 | 避免单次响应过大 |
| 协议版本 | 不变 | 新增的是工具层与 daemon op，不是 SysEx 命令 |

## 4. 架构与数据流

```
fl_get_piano_roll_notes(source="auto")
  │
  ├─ live ────────────────────────────────────────────────
  │    channel_select(n)?            # 换目标通道（钢琴卷帘跟随）
  │    ensure_piano_roll             # ui.showWindow(widPianoRoll)
  │    写 MCP_Apply.pyscript = dump 脚本（nonce 内嵌）
  │    trigger_run_last_script()     # Ctrl+Alt+Y / Cmd+Opt+Y
  │    pyscript: 读 flp.score → 写 MCP_SCORE_DUMP.json（脚本目录内）
  │    服务端: 轮询 dump 文件 → 校验 nonce → 解析
  │
  └─ autosave ────────────────────────────────────────────
       定位最新 .flp（Projects/ 下，含 Backup/）
       flp_reader 解析 → 按 pattern / channel 过滤 → 归一化
```

`source="auto"` 先试 live；失败（未 arm、触发没落地、钢琴卷帘打不开）时回退 autosave，并在返回值里给出 `fallback_reason`。`source="live"` / `"autosave"` 强制单条路径，失败即报错，不静默降级。

live 只能读当前 pattern（钢琴卷帘跟随 FL 当前选中的 pattern）；autosave 可读任意 pattern 与 channel。

## 5. 组件契约

### 5.1 工具 `fl_get_piano_roll_notes`

注册在 `src/fl_studio_mcp/tools/pianoroll.py`，与 `fl_write_piano_roll_notes` 同文件。

参数：

| 参数 | 类型 | 默认 | 说明 |
|---|---|---|---|
| `channel` | `int \| None` | `None` | 目标通道号。`None` 表示当前打开的钢琴卷帘。仅 live 有效 |
| `pattern` | `int \| None` | `None` | 目标 pattern 号。`None` 表示当前 pattern（live）或最新 pattern（autosave）。live 下只能等于当前 pattern |
| `source` | `"auto" \| "live" \| "autosave"` | `"auto"` | 数据来源 |
| `max_notes` | `int` | `2000` | 返回音符上限，超出截断并附摘要 |

行为约定：

- `source="auto"` 且给了非当前 `pattern` 时，直接走 autosave，因为 live 无法切换 pattern。
- `source="live"` 且 `pattern` 与当前 pattern 不一致时，返回 `pattern_mismatch` 错误，不静默读错对象。
- `source="live"` 的 `pattern_number` 取自控制器 `get_project_state`；`pattern_name` 与 `channel_name` 来自 `pattern_list` / `channel_list`（读不到时为 `null`，不影响音符数据）。

注解：`readOnlyHint: true`、`destructiveHint: false`、`idempotentHint: true`、`openWorldHint: true`。读取不改工程，不进 `safety.safe_write`。

### 5.2 dump pyscript

在 `src/fl_studio_mcp/pyscript_gen.py` 增加 `render_dump_script(nonce, dump_path)` 与 `write_dump_script(nonce, dump_path)`，写进 `MCP_Apply.pyscript`（与写音符同一个文件、同一个 arm）。

脚本只读不写工程：

```python
import flpianoroll as flp
import json

DUMP_PATH = r"...\Settings\Piano roll scripts\MCP_SCORE_DUMP.json"
NONCE = "a1b2c3d4"

FIELDS = ["number", "time", "length", "velocity", "pan", "color",
          "slide", "porta", "pitchofs", "release", "fcut", "fres",
          "group", "repeats", "selected", "muted"]

score = flp.score
notes = []
for i in range(score.noteCount):
    n = score.getNote(i)
    notes.append([getattr(n, f, None) for f in FIELDS])
payload = {"schema": 1, "ok": True, "nonce": NONCE, "ppq": score.PPQ,
           "tsnum": score.tsnum, "tsden": score.tsden,
           "note_count": score.noteCount, "fields": FIELDS, "notes": notes}
with open(DUMP_PATH, "w") as f:
    f.write(json.dumps(payload, ensure_ascii=True))
```

要点：

- 音符用定长数组而非对象，10 000 个音符的文件仍在 1 MB 量级。
- `nonce` 每次请求新生成，服务端只接受 nonce 匹配的 dump，避免读到上一次残留。
- 脚本内不调用 `clearNotes` / `addNote`，不改工程。
- 绝对路径在生成时烘焙进脚本，不依赖 `__file__`。

### 5.3 bridge 与 daemon

| 位置 | 新增 |
|---|---|
| `src/fl_studio_mcp/pianoroll.py` | `dump_score(nonce, timeout)`：删旧 dump → 写脚本 → 触发 → 轮询 dump → 解析 |
| `src/fl_studio_mcp/connection.py` | `FLBridge.read_score(channel, timeout)`；`TCPBridge.read_score(channel, timeout)` |
| `src/fl_studio_mcp/daemon.py` | `op: "read_score"` |

daemon 侧执行读取的理由：MSIX 版 Claude Desktop 启动的 MCP server 可能读不到 `Documents`，daemon 是普通进程，能写脚本目录、能读 dump。direct 模式下 `FLBridge.read_score` 在 MCP server 进程内执行。

轮询策略：触发后每 100 ms 看一次 dump 文件；命中条件是文件存在、JSON 可解析、`nonce` 匹配。超时返回 `timeout`，并附 arm 提示。

### 5.4 `flp_reader`

新增 `src/fl_studio_mcp/music/flp_reader.py`，纯 stdlib，无第三方依赖。

事件解析（FLP 为事件流）：

| 事件 ID | 含义 | 取值 |
|---|---|---|
| 64 | `ChannelID.New` | u16，通道 iid |
| 192 | `ChannelID._Name` | 文本，通道名 |
| 65 | `PatternID.New` | u16，pattern iid（同一 iid 出现两次，按 iid 合并） |
| 193 | `PatternID.Name` | 文本，pattern 名 |
| 224 | `PatternID.Notes` | 音符块 |
| 164 | `PatternID.Length` | u32，pattern 长度（ticks） |
| 67 | `PatternsID.CurrentlySelected` | u16，当前 pattern |
| 156 | `ProjectID.Tempo` | u32，BPM × 1000 |

结构约定：

- 头部 `FLhd`（14 字节）给出 `ppq`；随后 `FLdt` 块给出事件总长度。
- 事件 ID < 64 读 1 字节值，< 128 读 2 字节，< 192 读 4 字节，≥ 192 读 varint 长度后跟内容。
- 音符记录定长 24 字节：position u32、flags u16、rack_channel u16、length u32、key u16、group u16、fine_pitch i8、保留 u8、release u8、midi_channel u8、pan u8、velocity u8、mod_x u8、mod_y u8。

工程定位：`fl_settings_dir().parent / "Projects"` 下递归找最新的 `*.flp`（含 `Backup/`），按 mtime 取最大；`FLSTUDIO_MCP_PROJECT` 环境变量可覆盖。返回值带 `file` 与 `age_seconds`。

## 6. 数据契约

```json
{
  "ok": true,
  "source": "live",
  "pattern_number": 2,
  "pattern_name": "Melody",
  "channel": 1,
  "channel_name": "melody key",
  "ppq": 96,
  "time_signature": {"numerator": 4, "denominator": 4},
  "tempo_bpm": 98.0,
  "note_count": 59,
  "notes": [
    {
      "pitch": 60,
      "name": "C5",
      "start_ticks": 0,
      "length_ticks": 48,
      "start_beats": 0.0,
      "length_beats": 0.5,
      "start_bars": 0.0,
      "length_bars": 0.125,
      "velocity": 86,
      "pan": 0.5,
      "cents": 0,
      "slide": false,
      "porta": false
    }
  ],
  "truncated": false,
  "summary": {
    "pitch_min": 41, "pitch_max": 79,
    "pitch_class_histogram": [3, 5, 0, 0, 4, 0, 0, 6, 0, 0, 0, 7],
    "span_bars": 8.0,
    "notes_per_bar": 7.4
  },
  "fallback_reason": null,
  "age_seconds": null
}
```

换算约定：

- ticks → beats：`ticks / ppq`。
- beats → bars：`beats / (tsnum * 4 / tsden)`。
- velocity：live 用 `round(v * 128)`；autosave 直接取 `.flp` 的 0..127 值。
- pan 统一 0..1（0.5 为中）；autosave 用 `pan / 128`。
- cents 统一半音偏移；autosave 用 `fine_pitch - 120`。
- `summary` 在 `truncated` 为真时给出，未截断时同样给出（音符少时几乎零成本）。

`source="autosave"` 时补 `file`（绝对路径）与 `age_seconds`（距最近一次自动备份的秒数）；`source="live"` 时两者为 `null`。

`tempo_bpm` 的来源：live 由控制器 `get_tempo` 读取（读不到时为 `null`）；autosave 从 `.flp` 的 `ProjectID.Tempo` 解析。

## 7. 错误处理

| 情况 | 返回 |
|---|---|
| 未 arm（`Ctrl+Alt+Y` 没落地） | `ok: false`，`error: "note bridge not armed"`，附 arm 指引；`source="auto"` 时回退 autosave |
| 钢琴卷帘打不开 | 同上，错误文案区分 `piano roll unavailable` |
| dump 超时 | `ok: false`，`error: "dump timeout"`，附 dump 路径与 arm 指引 |
| dump 的 nonce 不匹配 | 视为未就绪，继续轮询直至超时 |
| `.flp` 找不到 | `ok: false`，`error: "no project file found"`，附查找路径 |
| `.flp` 解析失败 | `ok: false`，`error` 带异常类型与文件路径，不吞异常 |
| `pattern` 与当前 pattern 不一致（live） | `ok: false`，`error: "pattern_mismatch"`，附当前 pattern 号 |

任何失败都不返回半截数据。

## 8. 安全与回滚

- 读取工具不改工程，不进 snapshot / changelog，不产生回滚单元。
- dump pyscript 只读 `flp.score`，不调用 `addNote` / `clearNotes`。
- 服务端只写 `Settings/Piano roll scripts/` 下的 `MCP_Apply.pyscript` 与 `MCP_SCORE_DUMP.json`，与既有 note bridge 同一目录，无新增写面。
- 不引入 PyFLP，避免 GPL 传染。

## 9. 测试与门禁

离线（Linux 可跑）：

- `tests/test_flp_reader.py`：构造合成 `.flp` 字节流，断言 pattern 名、音符数、音符值、通道名、当前 pattern；再解析真实工程副本作为回归样本（样本不入库，改用合成 fixture + 可选本地文件）。
- `tests/test_score_read.py`：dump 脚本渲染（nonce、绝对路径、无写工程的 API 调用）；dump JSON → 工具返回结构的换算；velocity / pan / cents 归一；4/4 与 3/4 的 bars 换算；截断与摘要。
- `scripts/test_read_notes.py`：autosave 路径端到端（合成 `.flp` → 工具返回）。

在线（Windows + FL）：

- `scripts/test_read_notes_live.py`：在真实工程上跑 live 路径，比对钢琴卷帘可见音符数与返回的 `note_count`；随后改一个音符再读，确认读到未保存的改动。
- 手动按一次 `Ctrl+Alt+Y`，确认 `Run last script again` 在 24.2.2 上确实重新执行脚本（当前会话的按键注入不可信，见 §10）。

门禁：`python -m pytest` 全绿；`ruff check` 全绿；新脚本纳入 `tests/test_offline.py` 的离线清单。

## 10. 风险与待验证

1. **触发可靠性未定**：`Ctrl+Alt+Y` 在 24.2.2 上未验证。本次会话从 SSH / 计划任务注入的按键连 F7 都不生效，说明注入方式不可信，不能据此判断 FL 行为。需在正常进程或用户手动按键下确认。若不可用，live 退化为"用户点一次菜单"，autosave 成为主路径。
2. **自动备份设置**：用户若关掉 FL 自动备份，autosave 回退不可用；工具需明确报错而不是返回空。
3. **多工程**：同一台机器上有多个 `.flp` 时按 mtime 取最新，可能不是当前打开的工程；返回值带 `file` 供核对，`FLSTUDIO_MCP_PROJECT` 可显式指定。
4. **沙箱边界**：pyscript 只能读写脚本目录；MSIX 场景下必须由 daemon 执行读取。
5. **大工程**：上万音符时 dump 文件到 MB 量级；`max_notes` 兜底，必要时再加 dump 侧截断。
6. **FL 版本漂移**：`.flp` 事件 ID 随 FL 版本变化；解析器对未知事件跳过而非报错，版本变化时退化为"读不到该字段"，不整体失败。

## 11. 明确不做

- 不读自动化曲线、事件编辑器数据、Playlist 音频 clip。
- 不写工程（本仓已有 `fl_write_piano_roll_notes` 等写工具）。
- 不做 GUI 自动化导出 MIDI（Save As 对话框太脆）。
- 不支持 FL 20 以下版本。
- 不做调性推断、相似度检索等音乐分析；那些属于 AI 侧的判断。
