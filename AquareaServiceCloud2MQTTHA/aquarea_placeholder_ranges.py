"""
Dynamic placeholder range computation for Aquarea user settings.

At startup, fetches and parses the Panasonic Aquarea Service Cloud JS bundle to
extract all placeholder setting option ranges and the statusNo computation rules.
Falls back to hardcoded defaults if the fetch fails.

The JS contains:
  V = {userXXX: {type:"placeholder", options:{0:{},1:{},...}}, ...}
  (a=V.userXXX.options)["N"] = L(min, max, startHex, step?)  ← ascending range
  (a=V.userXXX.options)["N"] = K(start, end, startHex)       ← descending range
  R(zone, currentValues, bgData) → statusNo for heat zone targets
  D(zone, currentValues, bgData) → statusNo for cool zone targets
  B = {userXXX: {getRules: (e,t) => {... statusNo=R(1,e,t) ...}}, ...}
"""

import logging
import re
from typing import NamedTuple

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Types
# ---------------------------------------------------------------------------

class PlaceholderRange(NamedTuple):
    min: int
    max: int
    step: int


class _StatusNoRule(NamedTuple):
    """How to compute statusNo for a given placeholder setting."""
    func: str   # "R", "D", "user013", or "fixed"
    zone: int   # 1 or 2 (for R/D)
    fixed: int  # value for "fixed" func


# ---------------------------------------------------------------------------
# Fallback hardcoded values (extracted from JS as of 2026-04)
# Used when the JS fetch fails.
# ---------------------------------------------------------------------------

_FALLBACK_OPTIONS: dict[str, dict[int, PlaceholderRange]] = {
    "user008": {
        0: PlaceholderRange(-5, 5, 1),
        1: PlaceholderRange(20, 55, 1),
        2: PlaceholderRange(20, 60, 1),
        3: PlaceholderRange(20, 65, 1),
        4: PlaceholderRange(10, 30, 1),
        5: PlaceholderRange(15, 35, 1),
        6: PlaceholderRange(20, 75, 1),
        8: PlaceholderRange(20, 75, 1),
        9: PlaceholderRange(25, 75, 1),
    },
    "user009": {
        0: PlaceholderRange(-5, 5, 1),
        1: PlaceholderRange(20, 55, 1),
        2: PlaceholderRange(20, 60, 1),
        3: PlaceholderRange(20, 65, 1),
        4: PlaceholderRange(10, 30, 1),
        5: PlaceholderRange(15, 35, 1),
        6: PlaceholderRange(20, 75, 1),
        8: PlaceholderRange(20, 75, 1),
        9: PlaceholderRange(25, 75, 1),
    },
    "user010": {
        0: PlaceholderRange(-5, 5, 1),
        1: PlaceholderRange(5, 20, 1),
        2: PlaceholderRange(18, 35, 1),
        3: PlaceholderRange(-5, 5, 1),
    },
    "user011": {
        0: PlaceholderRange(-5, 5, 1),
        1: PlaceholderRange(5, 20, 1),
        2: PlaceholderRange(18, 35, 1),
        3: PlaceholderRange(-5, 5, 1),
    },
    "user013": {
        0: PlaceholderRange(40, 65, 1),
        1: PlaceholderRange(40, 75, 1),
    },
    "user023": {0: PlaceholderRange(-25, 15, 1)},
    "user024": {0: PlaceholderRange(-25, 15, 1)},
}

_FALLBACK_RULES: dict[str, _StatusNoRule] = {
    "user008": _StatusNoRule("R", 1, 0),
    "user009": _StatusNoRule("R", 2, 0),
    "user010": _StatusNoRule("D", 1, 0),
    "user011": _StatusNoRule("D", 2, 0),
    "user013": _StatusNoRule("user013", 0, 0),
    "user023": _StatusNoRule("fixed", 0, 0),
    "user024": _StatusNoRule("fixed", 0, 0),
}

# Runtime cache — populated from JS at startup
_options: dict[str, dict[int, PlaceholderRange]] = {}
_rules: dict[str, _StatusNoRule] = {}


# ---------------------------------------------------------------------------
# JS parser
# ---------------------------------------------------------------------------

def _parse_options_block(block: str) -> dict[str, dict[int, PlaceholderRange]]:
    """
    Parse all L()/K() option assignments from the JS options block.
    Handles both:
      (a=V.userXXX.options)["N"] = L(...)   ← first assignment, sets current key
      a["N"] = L(...)                        ← continuation for same key
    """
    result: dict[str, dict[int, PlaceholderRange]] = {}
    current_key: str | None = None

    pattern = re.compile(
        r'(?:\(a=V\.(user\d+)\.options\)|\ba)\["(\d+)"\]=(L|K)\((-?\d+),(-?\d+),(\d+)(?:,(\d+))?\)'
    )
    for m in pattern.finditer(block):
        user_key = m.group(1)
        if user_key:
            current_key = user_key
            result.setdefault(current_key, {})
        if not current_key:
            continue

        status_no = int(m.group(2))
        func = m.group(3)
        args = [int(m.group(i)) for i in range(4, 8) if m.group(i) is not None]

        if func == "L":
            mn, mx = args[0], args[1]
            step = args[3] if len(args) > 3 else 1
            result[current_key][status_no] = PlaceholderRange(mn, mx, step)
        elif func == "K":
            # K(start, end, hex): end=min, start=max
            result[current_key][status_no] = PlaceholderRange(args[1], args[0], 1)

    return result


def _parse_rules(b_block: str) -> dict[str, _StatusNoRule]:
    """
    Extract statusNo computation rules from the B object.
    Detects R(zone,...), D(zone,...), and user013 special case.
    """
    rules: dict[str, _StatusNoRule] = {}

    # R(zone, e, t) assignments
    for m in re.finditer(r'(user\d+):\{getRules.*?statusNo=R\((\d+),', b_block):
        rules[m.group(1)] = _StatusNoRule("R", int(m.group(2)), 0)

    # D(zone, e, t) assignments
    for m in re.finditer(r'(user\d+):\{getRules.*?statusNo=D\((\d+),', b_block):
        rules[m.group(1)] = _StatusNoRule("D", int(m.group(2)), 0)

    # user013 special: statusNo=1 default, =0 if system021=="0x01"
    if re.search(r'user013:\{getRules.*?system021', b_block):
        rules["user013"] = _StatusNoRule("user013", 0, 0)

    # Settings with fixed statusNo=0 (no getRules or no statusNo assignment)
    for m in re.finditer(r'(user\d+):\{\}', b_block):
        key = m.group(1)
        if key not in rules:
            rules[key] = _StatusNoRule("fixed", 0, 0)

    return rules


def load_from_js(js: str) -> bool:
    """
    Parse the Panasonic settings JS bundle and populate _options and _rules.
    Returns True if successful, False if parsing failed.
    """
    global _options, _rules

    try:
        # Find user options block: starts at first (a=V.user008.options)
        # ends at first system setting
        idx_start = js.find("(a=V.user008.options)")
        idx_end = js.find(",(a=V.system", idx_start)
        if idx_start < 0 or idx_end < 0:
            logger.warning("load_from_js: could not find user options block")
            return False

        block = js[idx_start:idx_end]
        parsed_options = _parse_options_block(block)
        if not parsed_options:
            logger.warning("load_from_js: parsed empty options")
            return False

        # Find B object for rules
        b_start = js.find("let B={")
        b_end = js.find(",P=function", b_start)
        if b_start < 0 or b_end < 0:
            logger.warning("load_from_js: could not find B rules object")
            return False

        parsed_rules = _parse_rules(js[b_start:b_end])

        # Only apply if we got something meaningful
        _options = parsed_options
        _rules = {**_FALLBACK_RULES, **parsed_rules}  # fallback for any missing

        logger.info(
            "Placeholder options loaded from JS: %d settings, %d rules",
            len(_options), len(_rules),
        )
        for key, sets in sorted(_options.items()):
            for sno, r in sorted(sets.items()):
                logger.debug("  %s[%d]: min=%d, max=%d, step=%d", key, sno, r.min, r.max, r.step)
        return True

    except Exception as exc:
        logger.warning("load_from_js failed (%s) — using hardcoded fallback", exc)
        return False


def _get_options() -> dict[str, dict[int, PlaceholderRange]]:
    return _options if _options else _FALLBACK_OPTIONS


def _get_rules() -> dict[str, _StatusNoRule]:
    return _rules if _rules else _FALLBACK_RULES


# ---------------------------------------------------------------------------
# statusNo computation (R, D, user013 — ported from Panasonic JS)
# ---------------------------------------------------------------------------

def _R(zone: int, cur: dict, bg: dict) -> int:
    n = cur.get("operation003")
    o = cur.get("system005") if zone == 1 else cur.get("system008")
    r = cur.get("system006") if zone == 1 else cur.get("system009")
    a = 0
    if o == "0x01":
        if r == "0x01":
            if n == "0x01":
                a = 0
            elif n == "0x02":
                s8A = bg.get("data0x8A")
                s8C = bg.get("data0x8C")
                if s8A == "0x01":
                    a = 1 if s8C == "0x01" else (8 if s8C == "0x05" else 2)
                elif s8A == "0x02":
                    a = 9 if s8C == "0x05" else 3
                elif s8A == "0x03":
                    a = 2 if s8C == "0x01" else (6 if s8C == "0x04" else (9 if s8C == "0x05" else 1))
        elif r == "0x02":
            a = 7
        elif r in ("0x03", "0x04"):
            a = 4
    elif o == "0x02":
        a = 5
    return a


def _D(zone: int, cur: dict, bg: dict) -> int:
    n = cur.get("operation023")
    o = cur.get("system005") if zone == 1 else cur.get("system008")
    r = cur.get("system006") if zone == 1 else cur.get("system009")
    a = 0
    if o == "0x01":
        if r == "0x01":
            if n == "0x01":
                a = 0
            elif n == "0x02":
                a = 1
        elif r == "0x02":
            a = 3
        elif r in ("0x03", "0x04"):
            a = 2
    elif o == "0x02":
        a = 3
    return a


def _compute_status_no(user_key: str, rule: _StatusNoRule, cur: dict, bg: dict) -> int:
    if rule.func == "R":
        return _R(rule.zone, cur, bg)
    elif rule.func == "D":
        return _D(rule.zone, cur, bg)
    elif rule.func == "user013":
        return 0 if cur.get("system021") == "0x01" else 1
    return rule.fixed


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def compute_placeholder_ranges(
    setting_data_info: dict,
    setting_background_data: dict,
) -> dict[str, PlaceholderRange]:
    """
    Compute correct min/max/step for each placeholder user setting.

    Uses options loaded from Panasonic JS (or hardcoded fallback).
    Returns {translation_key: PlaceholderRange}.
    """
    cur: dict[str, str | None] = {}
    for k, v in setting_data_info.items():
        m = re.match(r"function-setting-(system|user|operation)-select-(\d+)", k)
        if m:
            cur[f"{m.group(1)}{int(m.group(2)):03d}"] = (
                v.get("selectedValue") if isinstance(v, dict) else v
            )

    bg: dict[str, str | None] = {}
    for k, v in setting_background_data.items():
        bg[f"data{k}"] = v.get("value") if isinstance(v, dict) else v

    options = _get_options()
    rules = _get_rules()

    result: dict[str, PlaceholderRange] = {}
    for user_key, opts_sets in options.items():
        rule = rules.get(user_key, _StatusNoRule("fixed", 0, 0))
        status_no = _compute_status_no(user_key, rule, cur, bg)

        r = opts_sets.get(status_no) or opts_sets.get(0)
        if not r:
            continue

        tr_key = f"function-setting-user-select-{user_key.removeprefix('user')}"
        result[tr_key] = r
        logger.debug(
            "Placeholder range %s [statusNo=%d]: min=%d, max=%d, step=%d",
            tr_key, status_no, r.min, r.max, r.step,
        )

    return result