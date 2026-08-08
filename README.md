<h1 align="center">ha-sms-bridge</h1>

<p align="center">
  <a href="https://github.com/kai-wedekind/ha-sms-bridge/actions/workflows/test.yml"><img src="https://github.com/kai-wedekind/ha-sms-bridge/actions/workflows/test.yml/badge.svg" alt="Tests"></a>
  <a href="LICENSE"><img src="https://img.shields.io/badge/license-MIT-blue.svg" alt="MIT license"></a>
  <img src="https://img.shields.io/badge/python-3.9%2B-3776AB.svg" alt="Python 3.9+">
  <img src="https://img.shields.io/badge/Home%20Assistant-MQTT%20package-03A9F4.svg" alt="Home Assistant MQTT package">
</p>

A small, **bidirectional SMS channel for Home Assistant** over a USB cellular modem that lives on a **separate Raspberry Pi** (not the HA host). gammu-smsd owns the modem; an MQTT bridge connects it to HA.

**What you need:** a Raspberry Pi (or any Debian box) with a USB cellular modem and a SIM · an MQTT broker Home Assistant already talks to · about twenty minutes.

> Why this exists: Home Assistant's built-in `sms` integration only works when the modem is plugged into the HA host itself. If your modem is on another Pi (a print server, an LTE box, a Pi running other projects), there's no clean off-the-shelf path. This is that path — and it does **both directions**.

```
HA  ──mqtt clouds/sms/send {to,message,token}──▶  bridge (systemd)  ──▶ gammu-smsd ──▶ 📲 out
📲 in ──▶ gammu-smsd (RunOnReceive hook) ──publish──▶ mqtt clouds/sms/inbound {from,text} ──▶ HA sensor
```

## What you get
- **Outbound**: HA calls `script.send_sms` (or publishes JSON to `clouds/sms/send`) → an SMS goes out.
- **Inbound**: a received SMS appears in HA as `sensor.sms_inbound` (with `from` / `ts` attributes).
- **Bridge health**: `binary_sensor.sms_bridge_online` (MQTT LWT).
- Cost/abuse controls baked in (see Security).

> ### ⚠ This software spends your money
> Every outbound message is a billed SMS on your SIM. A misconfiguration, a runaway automation or anyone who can reach your MQTT broker can run up a real phone bill, and neither this project nor its author can refund it. The bridge ships with a default-deny allowlist, an hourly rate limit and a persistent daily cap — **set them before you point anything at it**, and read [Securing the broker](#securing-the-broker-not-optional) first. The shared secret is a replayable bearer token: anything that can *read* the send topic can spend your money.

## Install — Pi side

**Step 0, before anything else.** These are the steps people skip, and each one is load-bearing:

- [ ] `sudo apt install gammu-smsd` — the bridge injects into its spool and does nothing without it.
- [ ] **Your broker refuses anonymous connections.** Check it rather than assume it: connect a client with no credentials and confirm you get `CONNACK rc=5 (not authorised)`. If you get `rc=0`, stop and fix the broker first — the shared secret cannot protect a topic that strangers may read.
- [ ] **Give the bridge and Home Assistant their own broker identities**, not a shared house account, and ACL the four `clouds/sms/*` topics to just those two. See [Securing the broker](#securing-the-broker-not-optional).
- [ ] **Set `ALLOWLIST`, `RATE_MAX` and `DAILY_MAX` before the first start.** The service refuses to run without a real `SHARED_SECRET`, but it will happily send to whatever you allow.
- [ ] Home Assistant loads packages — `homeassistant: packages: !include_dir_named packages` in `configuration.yaml`.

Requires **Python 3.9+** (the code uses `list[str]` / `X | None` annotations). Raspberry Pi OS Bullseye or newer is fine; older images are not.

1. **Deploy the code** — note `requirements.txt` is in the copy list; the venv install needs it:
   ```sh
   sudo mkdir -p /opt/ha-sms-bridge /var/lib/ha-sms-bridge
   sudo cp pi/*.py pi/run_on_receive.sh pi/requirements.txt /opt/ha-sms-bridge/
   sudo chmod +x /opt/ha-sms-bridge/run_on_receive.sh
   sudo python3 -m venv /opt/ha-sms-bridge/venv
   sudo /opt/ha-sms-bridge/venv/bin/pip install -r /opt/ha-sms-bridge/requirements.txt
   ```
2. **gammu-smsd**: copy `gammu-smsdrc.example` → `/etc/gammu-smsdrc`, set `device` (find yours with `sudo gammu-detect` or check `ls /dev/ttyUSB*`) and `pin`, then:
   ```sh
   sudo chown root:gammu /etc/gammu-smsdrc && sudo chmod 640 /etc/gammu-smsdrc
   sudo systemctl enable --now gammu-smsd
   ```
   The send wrapper is already installed by step 1. It sends text **as‑is (UTF‑8)**, so German umlauts (ä ö ü ß) survive — gammu picks GSM‑7 or UCS‑2 automatically. If your modem or SMSC mangles non‑ASCII, set `SMS_GSM_SAFE=1` to force ASCII transliteration (ue/ae/oe/ss).

3. **Broker identity**: create an MQTT user for the bridge on your broker — with the Home Assistant Mosquitto add-on, add it under the add-on's `logins:` option (that list is *additive*, so existing Home Assistant user logins keep working). Use those credentials in the next step.

4. **Config**: copy `config.example.env` → `/opt/ha-sms-bridge/config.env`, fill `MQTT_USER` / `MQTT_PASS`, `SHARED_SECRET` (`openssl rand -hex 16`) and `ALLOWLIST` (your own number), then:
   ```sh
   sudo chown root:gammu /opt/ha-sms-bridge/config.env && sudo chmod 640 /opt/ha-sms-bridge/config.env
   ```
   ⚠ **`root:gammu` 640, not `600 root:root` — inbound depends on it.** The receive hook is run by gammu-smsd, which on a standard Debian/Raspberry Pi OS install runs as user `gammu`, and `run_on_receive.sh` *sources* this file to get the broker credentials. Root-only permissions mean the hook cannot read it and **inbound publishing fails silently** — the publisher exits 0, so you get a working outbound path and a dead inbound one with nothing in the logs to explain it.

5. **Service**:
   ```sh
   sudo cp pi/systemd/sms-mqtt-bridge.service /etc/systemd/system/
   sudo systemctl daemon-reload
   sudo systemctl enable --now sms-mqtt-bridge
   ```
   To run it unprivileged (recommended), uncomment `User=`/`Group=` in that unit first and follow [Run the bridge unprivileged](#securing-the-broker-not-optional) below.

6. **Check it before trusting it**:
   ```sh
   systemctl is-active sms-mqtt-bridge          # active
   journalctl -u sms-mqtt-bridge -n 20          # "connected to <broker>"
   python tests/test_sms_bridge.py              # offline suite: no broker or modem needed
   ```

## Install — HA side
1. Add `homeassistant/packages/sms_bridge.yaml` to `config/packages/`.
2. Add `sms_bridge_token: <the SHARED_SECRET>` to `secrets.yaml`.
3. Ensure the MQTT integration points at the same broker. Reload / restart. → `script.send_sms` (fields `number`, `message`), `sensor.sms_inbound`, `binary_sensor.sms_bridge_online`.

## MQTT topics
| Topic | Dir | Payload |
|---|---|---|
| `clouds/sms/send` | HA → Pi | `{"to":"+49…","message":"…","token":"…"}` |
| `clouds/sms/send/result` | Pi → HA | `{"ok":true,"to":[…],"segments":N,"detail":"…"}` |
| `clouds/sms/inbound` | Pi → HA | `{"from":"+49…","text":"…","parts":N,"ts":…}` |
| `clouds/sms/bridge/status` | Pi → HA | `online` / `offline` (retained, LWT) |

## Testing
- **Outbound**: `mosquitto_pub`/the HA script with a valid `token` to your allow-listed number → SMS arrives.
- **Inbound**: text the SIM's number → `sensor.sms_inbound` updates. (First time, set `DEBUG_ENV=1` in `config.env` to log the exact gammu env vars.)

## Security model
The send topic spends real money, so the bridge enforces, in code:
- **Shared-secret token** in every payload — blind topic-write access alone cannot trigger a send. ⚠ It is a *bearer* token with no nonce or timestamp, sent in the clear inside the payload, so anyone who can **read** `clouds/sms/send` can capture and replay it. See [Securing the broker](#securing-the-broker-not-optional) — this control depends on broker auth.
- **Default-deny allowlist** — an empty `ALLOWLIST` with `ALLOW_ANY=0` refuses everything; put your number in.
- **Strict recipient regex**, **message length cap**, **sliding hourly rate limit**, and a **persistent absolute daily cap** (survives restarts; charged per billed segment per recipient).
- **No shell** anywhere (argument-list subprocess only).
- Sends run on a **worker thread** so a slow modem can't starve the MQTT keepalive.

### Inbound is untrusted input
A received SMS comes from *anyone* and its text is fully attacker-controlled. This channel treats it as **data, never code**: the receive hook only sources the trusted config file (never the SMS), and the publisher JSON-encodes the text — no `eval`, no shell, nothing `subprocess`es the content. On the outbound side, recipients are regex-validated and message+number are passed to `subprocess` as an argument list (never `shell=True`), so a hostile message body is just literal SMS text. ⚠ **Scope of that claim:** it rests on reading the code plus one manual adversarial payload (shell metacharacters and command substitution, none of which executed). There is **no automated test** covering inbound injection — the suite in `tests/` covers segment maths, the retain guard and the charging rules, not this. Treat it as reviewed, not as continuously verified.

⚠ **Inbound can be forged by anything that may publish to the topic.** `clouds/sms/inbound` carries no token — the bridge is the only intended publisher, but MQTT does not enforce that by itself. On a broker without ACLs, any client can publish a fake "SMS" that lands in `sensor.sms_inbound` with an attacker's chosen text and sender. Nothing here acts on that, but *your* automations might. This is a second reason the topic ACLs below are not optional. **Multipart inbound is reassembled, not fragmented.** The receive hook iterates gammu's `DECODED_PARTS` / `DECODED_n_TEXT` (the decoded, reassembled parts) and only falls back to the raw `SMS_MESSAGES` / `SMS_n_TEXT` counters when no decoded set exists. Those are two different counters, and using the raw one delivers a long SMS to Home Assistant as several truncated fragments.

⚠ **Prompt-injection rule for anything built on `sensor.sms_inbound`:** never feed inbound SMS text to an LLM/agent as *instructions* — frame it explicitly as untrusted data, and don't give an SMS-*reading* agent send/spend/run-command capabilities without a human gate.

### Securing the broker (not optional)

An earlier version of this README called the following "recommended for a shared/hostile LAN, not required on a trusted home LAN". That understated it. Here is the actual reasoning, including the part that is *less* alarming than it first looks.

**The token is a replayable bearer secret.** It travels in cleartext inside the payload on `clouds/sms/send`, with no nonce and no timestamp. Anyone who can **subscribe** to that topic can wait for one legitimate send, read the token, and reuse it indefinitely. Write access is not the boundary — read access is.

**The good news:** the official Home Assistant *Mosquitto broker* add-on requires credentials out of the box; anonymous connections are refused (`CONNACK rc=5`). If that is your broker, the drive-by case does not apply to you.

**The part that still applies even with authentication on:** authentication is not authorisation. Many setups give every device the *same* MQTT user — the Tasmota plugs, the ESP nodes, a second Pi. With one shared identity and no ACL, every one of those devices can subscribe to `clouds/sms/send` and lift the token. One compromised smart plug is then a phone bill. So:

1. **Anonymous access off, every client its own credentials.** `allow_anonymous false` plus a `password_file` if you run your own broker; already the case with the HA add-on.
2. **ACL the SMS topics — this is the one that matters even on a well-run broker.** Home Assistant gets write-only on `clouds/sms/send`; the bridge gets read-only there plus write on `result`/`inbound`/`status`; no other identity touches them at all. Mosquitto ACLs have no deny rule, so this means enumerating what each identity may reach. Budget real time for it and expect to break something the first time — every existing client needs its topics listed.
3. **Keep 1883 off any interface that is not your LAN**, and prefer TLS on 8883 with certificate verification if the broker is reachable from anywhere else.

Two further steps that limit the damage rather than preventing the access:

- **Run the bridge unprivileged — and you do not need sudo for it.** An earlier version of this README told you to add a NOPASSWD sudoers entry and remove `NoNewPrivileges`. Don't: that trades a hardening flag for a privilege-escalation path, and on a standard gammu-smsd install it buys nothing. The spool directories are already `gammu:gammu` mode 770 and `gammu-smsd-inject` is world-executable, so **membership of group `gammu` is the whole permission you need**:

  ```sh
  sudo useradd --system --no-create-home --shell /usr/sbin/nologin smsbridge
  sudo usermod -aG gammu smsbridge
  sudo chown root:gammu /etc/gammu-smsdrc && sudo chmod 640 /etc/gammu-smsdrc   # was 600 root:root
  sudo chown root:gammu /opt/ha-sms-bridge/config.env && sudo chmod 640 /opt/ha-sms-bridge/config.env
  sudo chown -R smsbridge:gammu /var/lib/ha-sms-bridge
  ```
  Then add `User=smsbridge` and `Group=gammu` to the unit, leave `WRAPPER_SUDO=0`, and **keep `NoNewPrivileges=yes`**. The only trade is that group `gammu` can now read the SIM PIN in `gammu-smsdrc` — which costs nothing, because a member of that group can already write the spool, i.e. already send SMS. The PIN grants no capability they did not already have.

  Verify without sending anything:
  ```sh
  sudo -u smsbridge test -r /etc/gammu-smsdrc && sudo -u smsbridge test -w /var/spool/gammu/outbox && echo ok
  ```
  *(This path is exercised on the author's own Pi — the bridge runs as `smsbridge` with `NoNewPrivileges=yes` and no sudoers entry.)*

- **Set the caps low first.** `DAILY_MAX` and `RATE_MAX` are the last line between a bug and a bill. Start lower than you think you need and raise them once you have watched it work. Both survive a restart: the daily count and the hourly window are stored in `STATE_FILE`, so a crash loop, `Restart=always` or anyone able to bounce the unit does **not** get a fresh allowance. The one remaining bypass is moving the system clock, which needs root.

### What is committed
Every tracked file is a template: the `*.example` files contain **placeholders only**, and real values live exclusively in your own `config.env` and Home Assistant's `secrets.yaml`, neither of which is tracked. No credentials, phone numbers, PINs or tokens are in this repository or its history. Copy the examples, fill them in, done.

## Repository layout
```
pi/
  sms_mqtt_bridge.py      # systemd service: MQTT clouds/sms/send -> wrapper (worker thread)
  sms_inbound_publish.py  # gammu-smsd RunOnReceive hook -> publish clouds/sms/inbound
  run_on_receive.sh       # loads config.env, runs the inbound publisher
  send_sms_wrapper.py     # send primitive: gammu-smsd-inject; sends UTF-8 as-is (umlauts preserved), SMS_GSM_SAFE=1 = ASCII transliterate
  gammu-smsdrc.example    # gammu-smsd config (device, PIN, RunOnReceive)
  config.example.env      # bridge runtime config (broker, secret, caps, allowlist)
  requirements.txt        # paho-mqtt>=1.6,<2.0
  systemd/sms-mqtt-bridge.service
homeassistant/
  packages/sms_bridge.yaml  # inbound sensor + reusable script.send_sms (token via !secret)
```
