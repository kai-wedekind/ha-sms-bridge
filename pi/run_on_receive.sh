#!/bin/sh
# gammu-smsd RunOnReceive wrapper: load the bridge config env, then run the
# MQTT publisher with the bridge's Python venv. gammu-smsd runs this with a
# minimal environment, so we source config.env to get the broker credentials.
set -a
. /opt/ha-sms-bridge/config.env
set +a
exec /opt/ha-sms-bridge/venv/bin/python /opt/ha-sms-bridge/sms_inbound_publish.py
