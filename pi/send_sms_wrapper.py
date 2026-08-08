#!/usr/bin/env python3
"""Send SMS via the local gammu-smsd spool (gammu-smsd-inject).
Drop-in, SAME interface so existing callers are unchanged:
    send_sms_wrapper.py "<numbers>" "<message>"    (numbers ;-separated, exit 0 = QUEUED)
Injects into gammu-smsd's outbox (gammu-smsd owns the modem -> coexists with SMS receiving).
Async: exit 0 means QUEUED; gammu-smsd transmits within ~0-15 s. gammu-smsd must be running.
Encoding (changed 2026-06-24): the message is sent AS-IS so REAL German umlauts (ä ö ü ß
Ä Ö Ü) are preserved — verified that this modem/SMSC delivers them intact in GSM 7-bit and
Unicode. gammu auto-selects the coding (GSM default alphabet for German -> a single 160-char
SMS; UCS-2 only if the text contains characters outside the GSM alphabet). To restore the old
ASCII transliteration (ue/ae/oe/ss), set the env var SMS_GSM_SAFE=1.
"""
import logging
import os
import re
import subprocess
import sys
import unicodedata

logging.basicConfig(level=logging.INFO)

_MAP = {
    "ä": "ae", "ö": "oe", "ü": "ue", "Ä": "Ae", "Ö": "Oe", "Ü": "Ue",
    "ß": "ss", "€": "EUR",
}


def gsm_safe(s: str) -> str:
    """German-aware transliteration to ASCII. Only applied when SMS_GSM_SAFE=1."""
    out = []
    for ch in s:
        if ch in _MAP:
            out.append(_MAP[ch])
            continue
        a = "".join(c for c in unicodedata.normalize("NFKD", ch) if not unicodedata.combining(c))
        out.append(a if a.isascii() else "?")
    return "".join(out)


NUM_RE = re.compile(r"^\+?[0-9]{5,15}$")


def send_sms(phone_numbers: str, message: str, smsdrc: str = "/etc/gammu-smsdrc") -> bool:
    numbers = [n.strip() for n in phone_numbers.split(";") if n.strip()]
    if not numbers:
        print("Error: no valid phone numbers")
        return False
    bad = [n for n in numbers if not NUM_RE.match(n)]
    if bad:   # reject garbage / option-injection recipients (the bridge validates too; guard the primitive)
        print(f"Error: invalid recipient(s): {bad}")
        return False
    if not (message or "").strip():
        print("Error: empty message")
        return False
    msg = gsm_safe(message) if os.environ.get("SMS_GSM_SAFE") == "1" else message
    ok = True
    for number in numbers:
        try:
            subprocess.run(
                ["gammu-smsd-inject", "-c", smsdrc, "TEXT", number, "-text", msg],
                check=True, capture_output=True, text=True,
            )
            logging.info("queued SMS to %s", number)
            print(f"queued SMS to {number}")
        except subprocess.CalledProcessError as e:
            logging.error("inject failed for %s: %s", number, (e.stderr or "").strip())
            print(f"inject failed for {number}: {(e.stderr or '').strip()}")
            ok = False
        except FileNotFoundError:
            print("gammu-smsd-inject not found (is gammu-smsd installed?)")
            ok = False
    return ok


if __name__ == "__main__":
    if len(sys.argv) != 3:
        print('Usage: send_sms_wrapper.py "<num1>;<num2>" "<message>"')
        sys.exit(2)
    sys.exit(0 if send_sms(sys.argv[1], sys.argv[2]) else 1)
