#!/usr/bin/env python3
"""MQTT -> SMS bridge (outbound) for ha-sms-bridge.

Subscribes to an MQTT 'send' topic and sends each request as an SMS through the
local gammu-smsd, by calling send_sms_wrapper.py (which injects into the spool).

Defense in depth (a real SMS channel costs money):
  - shared-secret token required in the payload  -> topic-write alone is not enough
  - strict recipient regex + default-DENY allowlist (empty allowlist = refuse,
    unless ALLOW_ANY=1)
  - message length cap
  - sliding hourly rate limit AND a persistent absolute DAILY cap (survives restart)
  - rate is charged per estimated billed segment per recipient, not per recipient
  - subprocess via argument list, never a shell
  - sends run on a WORKER thread so a slow modem never blocks the MQTT keepalive loop
See README for the stronger broker-side controls (Mosquitto ACL, dedicated user, TLS).
"""
from __future__ import annotations

import hmac
import json
import logging
import math
import os
import queue
import re
import subprocess
import sys
import threading
import time

import paho.mqtt.client as mqtt

LOG = logging.getLogger("sms-bridge")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

MQTT_HOST = os.environ.get("MQTT_HOST", "homeassistant.local")
MQTT_PORT = int(os.environ.get("MQTT_PORT", "1883"))
MQTT_USER = os.environ.get("MQTT_USER", "")
MQTT_PASS = os.environ.get("MQTT_PASS", "")
TOPIC_SEND = os.environ.get("TOPIC_SEND", "clouds/sms/send")
TOPIC_RESULT = os.environ.get("TOPIC_RESULT", "clouds/sms/send/result")
TOPIC_STATUS = os.environ.get("TOPIC_STATUS", "clouds/sms/bridge/status")
WRAPPER = os.environ.get("WRAPPER_PATH", "/opt/ha-sms-bridge/send_sms_wrapper.py")
WRAPPER_SUDO = os.environ.get("WRAPPER_SUDO", "0") == "1"
SHARED_SECRET = os.environ.get("SHARED_SECRET", "")
MAX_LEN = int(os.environ.get("MAX_MESSAGE_LEN", "160"))
RATE_MAX = int(os.environ.get("RATE_MAX", "10"))             # segments per window
RATE_WINDOW = int(os.environ.get("RATE_WINDOW_SEC", "3600"))
DAILY_MAX = int(os.environ.get("DAILY_MAX", "30"))           # absolute segments/day
STATE_FILE = os.environ.get("STATE_FILE", "/var/lib/ha-sms-bridge/rate.json")
INJECT_TIMEOUT = int(os.environ.get("INJECT_TIMEOUT", "15"))
KEEPALIVE = int(os.environ.get("MQTT_KEEPALIVE", "120"))
ALLOWLIST = [n.strip() for n in os.environ.get("ALLOWLIST", "").split(";") if n.strip()]
ALLOW_ANY = os.environ.get("ALLOW_ANY", "0") == "1"

NUM_RE = re.compile(r"^\+?[0-9]{5,15}$")
# NB: the hourly window is no longer held in memory -- it lives in STATE_FILE alongside the
# daily count, so a restart cannot hand out a fresh allowance. See _load_daily().
_lock = threading.Lock()
_q: queue.Queue = queue.Queue(maxsize=256)         # bounded: a publish flood is shed, not buffered to OOM


def _load_daily() -> tuple[str, int, list[float]]:
    """date, daily count, and the hourly window as WALL-CLOCK timestamps.

    The hourly window used to live only in an in-memory deque keyed on time.monotonic(),
    which meant every restart handed out a fresh hourly allowance -- a crash loop, a
    Restart=always cycle or anyone who can bounce the unit got RATE_MAX again, so the real
    ceiling was DAILY_MAX rather than RATE_MAX. Persisting it costs one list in the file
    that already exists for the daily count.

    Wall clock rather than monotonic is deliberate and is the trade: monotonic cannot
    survive a restart at all, while wall clock can be moved by NTP or by root. The daily
    cap already had that exposure, so this adds no new class of bypass.
    """
    try:
        with open(STATE_FILE) as f:
            d = json.load(f)
        hourly = [float(t) for t in d.get("hourly", [])]
        return d.get("date", ""), int(d.get("count", 0)), hourly
    except Exception:  # noqa: BLE001
        return "", 0, []


def _save_daily(date: str, count: int, hourly: list[float] | None = None) -> None:
    try:
        os.makedirs(os.path.dirname(STATE_FILE), exist_ok=True)
        tmp = STATE_FILE + ".tmp"
        with open(tmp, "w") as f:
            json.dump({"date": date, "count": count, "hourly": hourly or []}, f)
        os.replace(tmp, STATE_FILE)
    except Exception as e:  # noqa: BLE001
        LOG.warning("could not persist rate state: %s", e)


# GSM 7-bit default alphabet (single SMS = 160 septets, concatenated part = 153); the extension
# chars each cost 2 septets. Anything outside these => the modem switches to UCS-2 (70 / 67 chars,
# astral code points = 2 UTF-16 units). Estimating cost from the actual alphabet is what keeps the
# money cap honest (a flat /153 over-charged 154-160 GSM and UNDER-counted UCS-2).
_GSM_BASE = set("@£$¥èéùìòÇ\nØø\rÅåΔ_ΦΓΛΩΠΨΣΘΞ\x1bÆæßÉ !\"#¤%&'()*+,-./0123456789:;<=>?"
                "¡ABCDEFGHIJKLMNOPQRSTUVWXYZÄÖÑÜ§¿abcdefghijklmnopqrstuvwxyzäöñüà")
_GSM_EXT = set("^{}\\[~]|€")


def _segments(message: str) -> int:
    if all(c in _GSM_BASE or c in _GSM_EXT for c in message):
        septets = sum(2 if c in _GSM_EXT else 1 for c in message)
        per = 160 if septets <= 160 else 153
        return max(1, -(-septets // per))
    units = sum(2 if ord(c) > 0xFFFF else 1 for c in message)   # UCS-2 (UTF-16 code units)
    per = 70 if units <= 70 else 67
    return max(1, -(-units // per))


def _rate_check(segments: int) -> tuple[bool, str]:
    """Would this send fit the hourly + persisted-daily caps? READ-ONLY — does not charge.
    (A failed inject costs no money, so we charge only AFTER a successful send via _rate_commit.
    Safe because there is a single worker thread: check and commit never interleave with another send.)"""
    now = time.time()
    with _lock:
        today = time.strftime("%Y-%m-%d")
        day, cnt, hourly = _load_daily()
        hourly = [t for t in hourly if now - t <= RATE_WINDOW]
        if len(hourly) + segments > RATE_MAX:
            return False, "hourly cap"
        if day != today:
            cnt = 0
        if cnt + segments > DAILY_MAX:
            return False, "daily cap"
        return True, ""


def _rate_commit(segments: int) -> None:
    """Charge the caps after a send actually went out."""
    now = time.time()
    with _lock:
        today = time.strftime("%Y-%m-%d")
        day, cnt, hourly = _load_daily()
        hourly = [t for t in hourly if now - t <= RATE_WINDOW]
        hourly.extend([now] * segments)
        if day != today:
            cnt = 0
        _save_daily(today, cnt + segments, hourly)


def _valid_number(num: str) -> bool:
    if not NUM_RE.match(num):
        return False
    if ALLOWLIST:
        return num in ALLOWLIST
    return ALLOW_ANY  # empty allowlist => deny unless explicitly opted in


def _send(numbers: list[str], message: str) -> tuple[bool, str, int]:
    """Returns (all_ok, detail, recipients_actually_queued).

    The third value exists because the wrapper loops over recipients and returns failure
    if ANY of them failed. The caller used to skip charging entirely on that result, so a
    send to three numbers where two went out was billed as zero -- money spent, cap not
    charged. The wrapper prints one 'queued SMS to <number>' line per success, so the real
    count is recoverable and the caps can charge what was actually sent.
    """
    cmd = (["sudo", "-n"] if WRAPPER_SUDO else []) + ["python3", WRAPPER, ";".join(numbers), message]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=INJECT_TIMEOUT)
        out = (r.stdout + r.stderr).strip()
        queued = out.count("queued SMS to ")
        # Fall back to the optimistic count only when the wrapper reports total success but
        # printed nothing we recognise (e.g. an older wrapper), never on partial failure.
        if r.returncode == 0 and queued == 0:
            queued = len(numbers)
        return r.returncode == 0, out[:500], min(queued, len(numbers))
    except Exception as e:  # noqa: BLE001
        return False, f"exception: {e}"[:500], 0


def _process(raw: str) -> dict:
    try:
        data = json.loads(raw)
        if SHARED_SECRET and not hmac.compare_digest(str(data.get("token", "")), SHARED_SECRET):
            raise ValueError("bad or missing token")
        to = data.get("to") or data.get("numbers") or ""
        message = data.get("message") or data.get("text") or ""
        numbers = [n.strip() for n in re.split(r"[;,]", str(to)) if n.strip()]
        if not numbers:
            raise ValueError("no recipient")
        if not message:
            raise ValueError("empty message")
        if len(message) > MAX_LEN:
            raise ValueError(f"message too long ({len(message)}>{MAX_LEN})")
        bad = [n for n in numbers if not _valid_number(n)]
        if bad:
            raise ValueError(f"rejected recipients (regex/allowlist): {bad}")
        cost = len(numbers) * _segments(message)
        ok_rate, why = _rate_check(cost)
        if not ok_rate:
            raise ValueError(f"rate limit: {why}")
        ok, detail, queued = _send(numbers, message)
        # Charge what actually went out, not all-or-nothing. The check above reserved the
        # worst case (every recipient); this charges the recipients the wrapper confirmed.
        charged = queued * _segments(message)
        if charged:
            _rate_commit(charged)
        LOG.info("send ok=%s to=%s queued=%s/%s segments_charged=%s %s",
                 ok, numbers, queued, len(numbers), charged, detail)
        return {"ok": ok, "to": numbers, "queued": queued, "segments": charged, "detail": detail}
    except Exception as e:  # noqa: BLE001
        # NEVER echo the request back. The result topic is published to the broker, and the
        # request carries the shared secret -- so returning raw[:200] handed the token to
        # anyone who could subscribe to clouds/sms/send/result. An attacker did not even need
        # to watch the send topic: provoke one rejection (oversize message, rate limit) and
        # read the token out of the failure. Only the exception text goes out now, truncated,
        # because a JSON decode error can quote the offending fragment.
        LOG.warning("rejected: %s", e)
        return {"ok": False, "error": str(e)[:200]}


def _worker(client):
    while True:
        raw = _q.get()
        try:
            result = _process(raw)
            client.publish(TOPIC_RESULT, json.dumps(result), qos=1)
        except Exception as e:  # noqa: BLE001 -- the SOLE worker must never die on a stray exception
            LOG.exception("worker error (continuing): %s", e)


def on_connect(client, userdata, flags, rc, *_):
    rc_val = getattr(rc, "value", rc)
    if rc_val == 0:
        LOG.info("connected to %s:%s", MQTT_HOST, MQTT_PORT)
        client.subscribe(TOPIC_SEND, qos=1)
        client.publish(TOPIC_STATUS, "online", qos=1, retain=True)
    else:
        LOG.error("connect failed rc=%s", rc)


def on_message(client, userdata, msg):
    # A RETAINED send would be redelivered on every reconnect -> a real, billed SMS each time,
    # draining the daily cap forever. A fire-once command topic must never honor retained delivery.
    if msg.retain:
        LOG.warning("ignoring RETAINED send payload on %s (would re-bill on every reconnect)", msg.topic)
        return
    # fast: just enqueue (bounded, non-blocking) so a slow modem never blocks the MQTT keepalive
    # loop and a flood is shed rather than buffered to OOM. The worker thread does the slow send.
    try:
        _q.put_nowait(msg.payload.decode("utf-8", "replace"))
    except queue.Full:
        LOG.warning("send queue full (%d) — dropping request", _q.maxsize)


def _make_client():
    try:  # paho-mqtt 2.x
        return mqtt.Client(
            callback_api_version=mqtt.CallbackAPIVersion.VERSION1,
            client_id="sms-bridge", protocol=mqtt.MQTTv311,
        )
    except (AttributeError, TypeError):  # paho-mqtt 1.x
        return mqtt.Client(client_id="sms-bridge", protocol=mqtt.MQTTv311)


def main():
    # Fail closed. Previously an empty secret only logged a warning and the bridge kept serving,
    # which means a config mistake silently removed the only application-level control on a
    # money-spending topic -- and a warning in a systemd journal is not a control. Refusing to
    # start is loud, immediate and cannot be missed.
    if not SHARED_SECRET or SHARED_SECRET in {"CHANGE_ME", "changeme"}:
        LOG.error(
            "SHARED_SECRET is empty or still the placeholder. Refusing to start: without it, "
            "anything that can publish to %s can spend money. Set it in config.env "
            "(openssl rand -hex 16).",
            TOPIC_SEND,
        )
        return 2
    if not ALLOWLIST and not ALLOW_ANY:
        LOG.warning("ALLOWLIST empty and ALLOW_ANY=0 -> ALL sends will be refused (default-deny)")
    client = _make_client()
    if MQTT_USER:
        client.username_pw_set(MQTT_USER, MQTT_PASS)
    client.will_set(TOPIC_STATUS, "offline", qos=1, retain=True)
    client.on_connect = on_connect
    client.on_message = on_message
    client.reconnect_delay_set(min_delay=1, max_delay=120)
    threading.Thread(target=_worker, args=(client,), daemon=True).start()
    while True:
        try:
            client.connect(MQTT_HOST, MQTT_PORT, keepalive=KEEPALIVE)
            client.loop_forever()
        except Exception as e:  # noqa: BLE001
            LOG.error("mqtt loop error: %s; retry in 10s", e)
            time.sleep(10)


if __name__ == "__main__":
    # sys.exit(main()), not a bare main(): the fail-closed check returns 2, and a bare call
    # would discard it and exit 0 -- systemd would record a clean stop for a refusal to run.
    sys.exit(main())
