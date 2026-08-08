#!/usr/bin/env python3
"""Offline unit tests for sms_mqtt_bridge money-logic + retain guard.

Stubs paho (absent on the dev box) and pins tiny caps + a temp state file BEFORE import,
so the module-level config reads pick them up. Run: python tests/test_sms_bridge.py
"""
import json
import os
import sys
import tempfile
import types

# --- stub paho.mqtt.client before importing the bridge ---
_paho = types.ModuleType("paho")
_mqtt = types.ModuleType("paho.mqtt")
_client = types.ModuleType("paho.mqtt.client")
_client.Client = type("Client", (), {})
_client.CallbackAPIVersion = type("CallbackAPIVersion", (), {"VERSION1": 1, "VERSION2": 2})
_client.MQTTv311 = 4
sys.modules["paho"] = _paho
sys.modules["paho.mqtt"] = _mqtt
sys.modules["paho.mqtt.client"] = _client

os.environ["RATE_MAX"] = "100"
os.environ["DAILY_MAX"] = "100"
os.environ["ALLOW_ANY"] = "1"   # let a valid recipient pass so _process can be exercised end-to-end
_state = os.path.join(tempfile.gettempdir(), "sms_rate_test.json")
os.environ["STATE_FILE"] = _state
try:
    os.remove(_state)
except OSError:
    pass

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "pi"))
import sms_mqtt_bridge as B  # noqa: E402

_fail = 0


def check(name, cond):
    global _fail
    if not cond:
        _fail += 1
        print("FAIL", name)
    else:
        print("PASS", name)


# --- segment math (GSM-7 vs UCS-2) ---
check("gsm 160 = 1 seg", B._segments("a" * 160) == 1)
check("gsm 161 = 2 seg", B._segments("a" * 161) == 2)
check("gsm 306 = 2 seg", B._segments("a" * 306) == 2)          # 2 * 153
check("gsm 307 = 3 seg", B._segments("a" * 307) == 3)
check("gsm ext '€' costs 2 septets (79+2=81<=160 -> 1)", B._segments("a" * 79 + "€") == 1)
check("gsm ext pushes over (159+2=161 -> 2)", B._segments("a" * 159 + "€") == 2)
check("ucs2 (cyrillic) 70 units = 1 seg", B._segments("я" * 70) == 1)
check("ucs2 71 units = 2 seg", B._segments("я" * 71) == 2)
check("ucs2 astral emoji = 2 units each (35 -> 1 seg, 36 -> 2)",
      B._segments("😀" * 35) == 1 and B._segments("😀" * 36) == 2)

# --- rate check is read-only; commit charges; caps enforced ---
try:
    os.remove(_state)
except OSError:
    pass
check("check 50 -> ok", B._rate_check(50)[0] is True)
check("check 100 again -> still ok (check does NOT charge)", B._rate_check(100)[0] is True)
B._rate_commit(60)
check("after commit 60: check 50 rejected (60+50>100)", B._rate_check(50)[0] is False)
check("after commit 60: check 40 ok (60+40==100)", B._rate_check(40)[0] is True)

# --- retain guard: a retained send is dropped, a live one is enqueued ---
class _Msg:
    def __init__(self, payload, retain):
        self.payload = payload.encode()
        self.retain = retain
        self.topic = "clouds/sms/send"

_before = B._q.qsize()
B.on_message(None, None, _Msg('{"to":"+491234567","message":"hi","token":"x"}', True))
check("retained send dropped (queue unchanged)", B._q.qsize() == _before)
B.on_message(None, None, _Msg('{"to":"+491234567","message":"hi","token":"x"}', False))
check("live send enqueued", B._q.qsize() == _before + 1)

# ---- end-to-end: a FAILED send must NOT charge; a SUCCESSFUL send charges exactly once ----
_orig_send = B._send
try:
    os.remove(_state)
except OSError:
    pass
B._send = lambda numbers, message: (False, "simulated inject failure", 0)
try:
    res = B._process(json.dumps({"to": "+491234567", "message": "hi"}))
finally:
    B._send = _orig_send
check("FAILED send does NOT charge the daily cap", B._load_daily()[1] == 0 and res.get("ok") is False)

try:
    os.remove(_state)
except OSError:
    pass
B._send = lambda numbers, message: (True, "queued", 1)
try:
    res2 = B._process(json.dumps({"to": "+491234567", "message": "hi"}))
finally:
    B._send = _orig_send
check("SUCCESSFUL send charges the daily cap once", B._load_daily()[1] == 1 and res2.get("ok") is True)

# --- the hourly window must SURVIVE a restart ---
# It used to live only in a process-memory deque keyed on time.monotonic(), so every
# restart handed out a fresh RATE_MAX and the real ceiling was DAILY_MAX. These two
# assertions are the regression guard: the window is on disk, and a fresh process
# reading the file sees the same entries.
try:
    os.remove(_state)
except OSError:
    pass
B._rate_commit(3)
check("hourly window is persisted to disk", len(B._load_daily()[2]) == 3)
with open(_state) as _f:
    check("a fresh process would see the same hourly window",
          len(json.load(_f).get("hourly", [])) == 3)

# --- a PARTIAL multi-send charges what actually went out, not zero ---
# The wrapper returns failure if any recipient failed, and the caller used to skip
# charging entirely: money spent, cap uncharged.
try:
    os.remove(_state)
except OSError:
    pass
_orig_partial = B._send
B._send = lambda numbers, message: (
    False, "queued SMS to +491234567\ninject failed for +491234568", 1)
try:
    res3 = B._process(json.dumps({"to": "+491234567;+491234568", "message": "hi"}))
finally:
    B._send = _orig_partial
check("partial send charges only the delivered recipient",
      B._load_daily()[1] == 1 and res3.get("queued") == 1)

print("\n%s" % ("ALL PASS" if _fail == 0 else "%d FAILURES" % _fail))
sys.exit(1 if _fail else 0)
