"""
Diagnostics for function/setting/get failures.

Every attempt is appended as one JSON line to DIAG_FILE (persistent /data in
the add-on container) and aggregated counters are published to MQTT /
logged hourly, so failures can be correlated afterwards with:
  - latency (fast refusal vs slow backend/adapter timeout)
  - HTTP status / response headers / raw body
  - time since login and since the function/status call of the same poll
  - whether an immediate retry on the same session succeeds
"""

import json
import logging
import os
import statistics
import time
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

DIAG_FILE = "/data/settings_diag.jsonl" if os.path.isdir("/data") else "settings_diag.jsonl"
DIAG_MAX_BYTES = 2 * 1024 * 1024  # rotate to .1 above this size
SUMMARY_EVERY_SEC = 3600

# Response headers worth keeping (lower-case); everything x-* is kept too.
_KEEP_HEADERS = {"date", "server", "content-type", "content-length", "via",
                 "cf-ray", "x-amzn-requestid", "x-amz-cf-id", "x-cache"}


def pick_headers(headers) -> dict:
    out = {}
    for k, v in headers.items():
        lk = k.lower()
        if lk in _KEEP_HEADERS or lk.startswith("x-"):
            out[lk] = v
        elif lk == "set-cookie":
            # cookie names only, never values
            out.setdefault("set-cookie-names", []).append(v.split("=", 1)[0])
    return out


class SettingsDiag:
    """Per-device counters.  The JSONL file is opt-in (SettingsDiagFile):
    ~1 line/min/device would otherwise wear SD cards for every user."""

    def __init__(self, gwid: str, write_file: bool = False, delay_sec: float = 0.0):
        self.gwid = gwid
        self.delay_sec = delay_sec
        self.write_file = write_file
        self.attempts = 0
        self.first_fail = 0        # polls whose 1st attempt failed
        self.rescued = 0           # ... and whose retry succeeded
        self.final_fail = 0        # polls failed after retry
        self.ms_ok: list[float] = []
        self.ms_fail: list[float] = []
        self.codes: dict[str, int] = {}
        self.last_error = ""
        self.last_ms = 0.0
        self._win = {"polls": 0, "first_fail": 0, "rescued": 0, "ms_ok": [], "ms_fail": [], "codes": {}}
        self._last_summary = time.monotonic()

    # ------------------------------------------------------------------
    def record(self, **rec) -> None:
        """Append one attempt record to the JSONL file."""
        self.attempts += 1
        rec = {"ts": datetime.now(timezone.utc).isoformat(timespec="milliseconds"), **rec}
        ms = rec.get("ms", 0.0)
        self.last_ms = ms
        if rec.get("ok"):
            self.ms_ok.append(ms); self._win["ms_ok"].append(ms)
        else:
            self.ms_fail.append(ms); self._win["ms_fail"].append(ms)
            for c in rec.get("codes") or ["none"]:
                self.codes[c] = self.codes.get(c, 0) + 1
                self._win["codes"][c] = self._win["codes"].get(c, 0) + 1
            self.last_error = f"{','.join(rec.get('codes') or ['none'])} http={rec.get('http')} {ms:.0f}ms attempt={rec.get('attempt')}"
        # keep lists bounded
        self.ms_ok = self.ms_ok[-2000:]
        self.ms_fail = self.ms_fail[-2000:]
        if not self.write_file:
            return
        try:
            if os.path.exists(DIAG_FILE) and os.path.getsize(DIAG_FILE) > DIAG_MAX_BYTES:
                os.replace(DIAG_FILE, DIAG_FILE + ".1")
            with open(DIAG_FILE, "a", encoding="utf-8") as f:
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        except OSError as e:
            logger.debug("diag write failed: %s", e)

    def poll_result(self, first_ok: bool, final_ok: bool) -> None:
        self._win["polls"] += 1
        if not first_ok:
            self.first_fail += 1
            self._win["first_fail"] += 1
            if final_ok:
                self.rescued += 1
                self._win["rescued"] += 1
            else:
                self.final_fail += 1

    # ------------------------------------------------------------------
    @staticmethod
    def _med(v: list[float]) -> str:
        return f"{statistics.median(v):.0f}ms" if v else "-"

    def maybe_log_summary(self) -> None:
        if time.monotonic() - self._last_summary < SUMMARY_EVERY_SEC:
            return
        w = self._win
        logger.info(
            "setting/get last hour for %s (SettingsDelay=%.0fs): %d polls, %d failed 1st attempt "
            "(%d rescued by retry), latency ok=%s fail=%s, codes=%s%s",
            self.gwid, self.delay_sec, w["polls"], w["first_fail"], w["rescued"],
            self._med(w["ms_ok"]), self._med(w["ms_fail"]), w["codes"],
            f" — details in {DIAG_FILE}" if self.write_file else "",
        )
        self._win = {"polls": 0, "first_fail": 0, "rescued": 0, "ms_ok": [], "ms_fail": [], "codes": {}}
        self._last_summary = time.monotonic()

    def mqtt_payload(self) -> dict[str, str]:
        base = f"aquarea/{self.gwid}/diag"
        return {
            f"{base}/settings_fail_first": str(self.first_fail),
            f"{base}/settings_rescued": str(self.rescued),
            f"{base}/settings_fail_final": str(self.final_fail),
            f"{base}/settings_last_ms": f"{self.last_ms:.0f}",
            f"{base}/settings_last_error": self.last_error[:250] or "none",
        }