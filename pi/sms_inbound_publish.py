#!/usr/bin/env python3
"""gammu-smsd RunOnReceive hook -> publish a received SMS to MQTT. See README.

Received messages are ALSO retained on disk in gammu-smsd's inboxpath, so a transient
MQTT failure here never loses the SMS. We always exit 0 so gammu-smsd marks the message
processed. Set DEBUG_ENV=1 to dump SMS_*/DECODED_* env vars to stderr (first-test aid).
"""
import json
import os
import sys
import time

import paho.mqtt.publish as publish

MQTT_HOST = os.environ.get("MQTT_HOST", "homeassistant.local")
MQTT_PORT = int(os.environ.get("MQTT_PORT", "1883"))
MQTT_USER = os.environ.get("MQTT_USER", "")
MQTT_PASS = os.environ.get("MQTT_PASS", "")
TOPIC_INBOUND = os.environ.get("TOPIC_INBOUND", "clouds/sms/inbound")


def collect():
    # DECODED_PARTS = number of decoded (reassembled) parts; the decoded text is in
    # DECODED_n_TEXT. SMS_MESSAGES = raw physical-segment count (a DIFFERENT counter).
    # Iterate DECODED_PARTS; fall back to the raw SMS_* set only if no DECODED_* exist.
    sender = os.environ.get("SMS_1_NUMBER", "")
    decoded_parts = int(os.environ.get("DECODED_PARTS", "0") or 0)
    if decoded_parts > 0:
        parts = [os.environ.get(f"DECODED_{i}_TEXT", "") or "" for i in range(1, decoded_parts + 1)]
    else:
        n = int(os.environ.get("SMS_MESSAGES", "1") or 1)
        parts = [os.environ.get(f"SMS_{i}_TEXT", "") or "" for i in range(1, n + 1)]
    return sender, "".join(parts)[:1000], (decoded_parts or len(parts))   # bound payload: inbound SMS is attacker-controlled


def main():
    if os.environ.get("DEBUG_ENV") == "1":
        for k in sorted(os.environ):
            if k.startswith("SMS_") or k.startswith("DECODED_"):
                sys.stderr.write(f"{k}={os.environ[k]!r}\n")
    sender, text, parts = collect()
    payload = {"from": sender, "text": text, "parts": parts, "ts": int(time.time())}
    auth = {"username": MQTT_USER, "password": MQTT_PASS} if MQTT_USER else None
    try:
        publish.single(TOPIC_INBOUND, json.dumps(payload, ensure_ascii=False), qos=1,
                       hostname=MQTT_HOST, port=MQTT_PORT, auth=auth, client_id=f"sms-inbound-{os.getpid()}")
    except Exception as e:  # noqa: BLE001
        sys.stderr.write(f"sms_inbound_publish: MQTT publish failed: {e}\n")
    sys.exit(0)


if __name__ == "__main__":
    main()
