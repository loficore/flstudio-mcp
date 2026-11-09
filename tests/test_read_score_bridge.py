from fl_studio_mcp import connection, daemon


def test_fl_bridge_read_score_selects_channel_then_dumps(monkeypatch):
    b = connection.FLBridge.__new__(connection.FLBridge)
    calls = []
    monkeypatch.setattr(b, "call",
                        lambda cmd, params=None, **kw: calls.append((cmd, params)) or {"ok": True})
    monkeypatch.setattr(connection.pianoroll, "dump_score",
                        lambda **kw: {"ok": True, "payload": {"x": 1}})
    res = b.read_score(channel=3)
    assert res["ok"] is True
    assert [c[0] for c in calls][:2] == ["channel_select", "ensure_piano_roll"]
    assert calls[0][1] == {"channel": 3}


def test_fl_bridge_read_score_skips_selection_without_channel(monkeypatch):
    b = connection.FLBridge.__new__(connection.FLBridge)
    calls = []
    monkeypatch.setattr(b, "call",
                        lambda cmd, params=None, **kw: calls.append((cmd, params)) or {"ok": True})
    monkeypatch.setattr(connection.pianoroll, "dump_score",
                        lambda **kw: {"ok": True, "payload": {}})
    b.read_score()
    assert [c[0] for c in calls] == ["ensure_piano_roll"]


def test_fl_bridge_read_score_reports_piano_roll_failure(monkeypatch):
    b = connection.FLBridge.__new__(connection.FLBridge)

    def call(cmd, params=None, **kw):
        if cmd == "ensure_piano_roll":
            raise connection.FLCommandFailed("no ui")
        return {"ok": True}

    monkeypatch.setattr(b, "call", call)
    res = b.read_score()
    assert res["ok"] is False and res["error"] == "piano roll unavailable"
    assert "no ui" in res["detail"]


def test_tcp_bridge_read_score_sends_op(monkeypatch):
    b = connection.TCPBridge.__new__(connection.TCPBridge)
    seen = {}
    monkeypatch.setattr(b, "_rpc",
                        lambda req, timeout: seen.update(req) or {"ok": True, "payload": {}})
    res = b.read_score(channel=2, timeout=5.0)
    assert res["ok"] is True
    assert seen["op"] == "read_score" and seen["channel"] == 2


def test_daemon_read_score_delegates(monkeypatch):
    calls = {}

    class B:
        def read_score(self, channel=None, timeout=8.0):
            calls["args"] = (channel, timeout)
            return {"ok": True, "payload": {"channel": channel}}

    monkeypatch.setattr(daemon, "_get_bridge", lambda: B())
    res = daemon._handle_request({"op": "read_score", "channel": 4, "timeout": 3.0})
    assert res["ok"] is True and res["data"]["payload"]["channel"] == 4
    assert calls["args"] == (4, 3.0)


def test_daemon_read_score_surfaces_bridge_errors(monkeypatch):
    class B:
        def read_score(self, channel=None, timeout=8.0):
            raise connection.FLNotRunning("FL is not running")

    monkeypatch.setattr(daemon, "_get_bridge", lambda: B())
    res = daemon._handle_request({"op": "read_score"})
    assert res["ok"] is False and res["exc"] == "FLNotRunning"
