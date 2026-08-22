"""AR/AD signal detection, ray lifecycle, and chart segment helpers.

AR (Auto Rally)
  Sharp drop, consecutive bear bars, bullish reversal on high volume.
  Ray level = signal bar HIGH.

AD
  Sharp rise, consecutive bull bars, bearish reversal on high volume.
  Ray level = signal bar LOW.

Ray rules
  Each AR/AD signal bar draws two horizontal wick rays (upper=high, lower=low).
  Both extend right from the signal bar; each stops at its first wick touch.

Alert rules (thresholds in daily-bar equivalents, converted per timeframe)
  觸碰: first primary-wick touch after >10 daily bars, in the latest 2 bars.
  晚觸碰: first primary-wick touch with bars_after ≥ 60 (and >20), within latest 10 daily bars.
  接近未觸: primary wick still active, 60 ≤ bars_after ≤ 200 daily, gap 0–1%.
"""

from __future__ import annotations

from typing import Any

LOOKBACK = 10
VOL_LEN = 20
DROP_PCT = 3.0
MIN_STREAK = 3
VOL_MULT = 1.2
USE_STRUCTURE = True
TOUCH_WINDOW_BARS = 5  # legacy default (1D days); prefer explicit params
FRESH_BARS = 2
NEAR_MISS_TOL_PCT = 0.01  # wick within 0–1% of ray level, no touch
NEAR_MISS_TOL_MIN = 0.0   # inclusive lower bound (gap > 0 required by touch check)


def bear_bar(c: list[dict], i: int) -> bool:
    return c[i]["close"] < c[i]["open"]


def bull_bar(c: list[dict], i: int) -> bool:
    return c[i]["close"] > c[i]["open"]


def streak(c: list[dict], i: int, bear: bool, length: int) -> bool:
    for j in range(1, length + 1):
        idx = i - j
        if idx < 0:
            return False
        if bear and not bear_bar(c, idx):
            return False
        if not bear and not bull_bar(c, idx):
            return False
    return True


def sma_vol(c: list[dict], i: int, length: int) -> float | None:
    if i < length - 1:
        return None
    return sum(c[i - j]["volume"] for j in range(length)) / length


def sharp_down(candles: list[dict], idx: int) -> bool:
    """Pine ``sharpDown`` at bar ``idx`` (drop + bear streak + optional lower high)."""
    if idx < LOOKBACK + MIN_STREAK:
        return False
    base = candles[idx - LOOKBACK]["close"]
    if not base:
        return False
    drop_pct = (base - candles[idx]["close"]) / base * 100
    if drop_pct < DROP_PCT:
        return False
    if not streak(candles, idx, True, MIN_STREAK):
        return False
    if USE_STRUCTURE:
        prior_max = max(candles[idx - k]["high"] for k in range(1, LOOKBACK + 1))
        if candles[idx]["high"] >= prior_max:
            return False
    return True


def sharp_up(candles: list[dict], idx: int) -> bool:
    """Pine ``sharpUp`` at bar ``idx`` (rise + bull streak + optional higher low)."""
    if idx < LOOKBACK + MIN_STREAK:
        return False
    base = candles[idx - LOOKBACK]["close"]
    if not base:
        return False
    rise_pct = (candles[idx]["close"] - base) / base * 100
    if rise_pct < DROP_PCT:
        return False
    if not streak(candles, idx, False, MIN_STREAK):
        return False
    if USE_STRUCTURE:
        prior_min = min(candles[idx - k]["low"] for k in range(1, LOOKBACK + 1))
        if candles[idx]["low"] <= prior_min:
            return False
    return True


def detect_signals(candles: list[dict]) -> list[dict]:
    """Find AR/AD reversal bars (matches Pine: sharpDown[1] / sharpUp[1])."""
    signals: list[dict] = []
    start = LOOKBACK + MIN_STREAK + 1
    if len(candles) < start + 1:
        return signals
    for i in range(start, len(candles)):
        vol_ma = sma_vol(candles, i, VOL_LEN)
        high_vol = vol_ma is None or candles[i]["volume"] >= vol_ma * VOL_MULT
        if not high_vol:
            continue
        prev = i - 1
        if bull_bar(candles, i) and bear_bar(candles, prev) and sharp_down(candles, prev):
            signals.append(
                {
                    "type": "AR",
                    "index": i,
                    "time": candles[i]["time"],
                    "level": candles[i]["high"],
                    "close": candles[i]["close"],
                    "volume": candles[i]["volume"],
                }
            )
        if bear_bar(candles, i) and bull_bar(candles, prev) and sharp_up(candles, prev):
            signals.append(
                {
                    "type": "AD",
                    "index": i,
                    "time": candles[i]["time"],
                    "level": candles[i]["low"],
                    "close": candles[i]["close"],
                    "volume": candles[i]["volume"],
                }
            )
    return signals


def resolve_wick_ray(candles: list[dict], sig_idx: int, level: float, side: str) -> dict[str, Any]:
    """One wick ray from signal bar; stop at first touch."""
    start_time = candles[sig_idx]["time"]
    is_upper = side == "upper"
    for j in range(sig_idx + 1, len(candles)):
        bar = candles[j]
        touched = bar["high"] >= level if is_upper else bar["low"] <= level
        if touched:
            return {
                "side": side,
                "level": float(level),
                "startTime": int(start_time),
                "endTime": int(bar["time"]),
                "touch_index": j,
                "active": False,
            }
    last = candles[-1]
    return {
        "side": side,
        "level": float(level),
        "startTime": int(start_time),
        "endTime": int(last["time"]),
        "touch_index": None,
        "active": True,
    }


def resolve_signal_rays(candles: list[dict], item: dict) -> dict[str, Any]:
    """Upper (high) and lower (low) wick rays for one AR/AD signal."""
    idx = item["index"]
    bar = candles[idx]
    return {
        "upper": resolve_wick_ray(candles, idx, bar["high"], "upper"),
        "lower": resolve_wick_ray(candles, idx, bar["low"], "lower"),
    }


def primary_wick_ray(rays: dict[str, Any], sig_type: str) -> dict[str, Any]:
    return rays["upper"] if sig_type == "AR" else rays["lower"]


def fresh_range(n: int, fresh_bars: int = FRESH_BARS) -> tuple[int, int]:
    last = n - 1
    lo = max(0, last - (fresh_bars - 1))
    return lo, last


def collect_ar_dr_touches(
    candles: list[dict],
    signals: list[dict],
    *,
    touch_after_bars: int,
    late_min_bars: int,
    late_after_bars: int,
    touch_fresh_bars: int = FRESH_BARS,
    late_fresh_bars: int,
) -> list[dict]:
    """Primary-wick first touches: 觸碰 (>touch_after, fresh short) or 晚觸碰 (≥late_min)."""
    if not candles:
        return []
    touch_lo, touch_last = fresh_range(len(candles), touch_fresh_bars)
    late_lo, late_last = fresh_range(len(candles), late_fresh_bars)
    hits: list[dict] = []
    for sig in signals:
        rays = resolve_signal_rays(candles, sig)
        ray = primary_wick_ray(rays, sig["type"])
        ti = ray.get("touch_index")
        if ti is None:
            continue
        bars_after = ti - sig["index"]
        if bars_after >= late_min_bars and bars_after > late_after_bars:
            if not (late_lo <= ti <= late_last):
                continue
            hits.append(
                {
                    "kind": "ar_dr_late",
                    "label": f"{sig['type']} 晚觸碰",
                    "type": sig["type"],
                    "wick": ray["side"],
                    "signal_time": sig["time"],
                    "signal_index": sig["index"],
                    "bars_after_signal": bars_after,
                    "time": candles[ti]["time"],
                    "index": ti,
                    "level": ray["level"],
                    "close": candles[ti]["close"],
                }
            )
            continue
        if bars_after <= touch_after_bars:
            continue
        if not (touch_lo <= ti <= touch_last):
            continue
        hits.append(
            {
                "kind": "ar_dr_touch",
                "label": f"{sig['type']} 觸碰",
                "type": sig["type"],
                "wick": ray["side"],
                "signal_time": sig["time"],
                "signal_index": sig["index"],
                "bars_after_signal": bars_after,
                "time": candles[ti]["time"],
                "index": ti,
                "level": ray["level"],
                "close": candles[ti]["close"],
            }
        )
    return hits


# Back-compat alias
def collect_late_ar_dr_touches(
    candles: list[dict],
    signals: list[dict],
    touch_window_bars: int = TOUCH_WINDOW_BARS,
    fresh_bars: int = FRESH_BARS,
) -> list[dict]:
    return collect_ar_dr_touches(
        candles,
        signals,
        touch_after_bars=touch_window_bars,
        late_min_bars=max(touch_window_bars + 1, 60),
        late_after_bars=20,
        touch_fresh_bars=fresh_bars,
        late_fresh_bars=max(fresh_bars, 10),
    )


def wick_near_miss(bar: dict, level: float, is_upper: bool) -> tuple[bool, float]:
    """Return (is_near_miss, gap_pct) when wick is close but did not touch the ray."""
    if not level:
        return False, 0.0
    tol = abs(level) * NEAR_MISS_TOL_PCT
    if is_upper:
        gap = level - bar["high"]
        if gap <= 0 or gap > tol:
            return False, 0.0
        return True, gap / level * 100
    gap = bar["low"] - level
    if gap <= 0 or gap > tol:
        return False, 0.0
    return True, gap / level * 100


def collect_late_ar_dr_near_misses(
    candles: list[dict],
    signals: list[dict],
    touch_window_bars: int = TOUCH_WINDOW_BARS,
    fresh_bars: int = FRESH_BARS,
    near_min_bars: int | None = None,
    near_max_bars: int | None = None,
) -> list[dict]:
    """Report fresh near-misses: near_min ≤ bars_after ≤ near_max, gap 0–1%."""
    if not candles:
        return []
    lo, last = fresh_range(len(candles), fresh_bars)
    min_bars = touch_window_bars if near_min_bars is None else near_min_bars
    max_bars = near_max_bars
    hits: list[dict] = []
    for sig in signals:
        rays = resolve_signal_rays(candles, sig)
        ray = primary_wick_ray(rays, sig["type"])
        if not ray.get("active"):
            continue
        level = float(ray["level"])
        is_upper = sig["type"] == "AR"
        best: dict | None = None
        for i in range(last, lo - 1, -1):
            bars_after = i - sig["index"]
            if bars_after < min_bars:
                continue
            if max_bars is not None and bars_after > max_bars:
                continue
            near, gap_pct = wick_near_miss(candles[i], level, is_upper)
            if not near:
                continue
            best = {
                "kind": "ar_dr_near",
                "label": f"{sig['type']} 接近未觸",
                "type": sig["type"],
                "wick": ray["side"],
                "signal_time": sig["time"],
                "signal_index": sig["index"],
                "bars_after_signal": bars_after,
                "gap_pct": gap_pct,
                "time": candles[i]["time"],
                "index": i,
                "level": level,
                "close": candles[i]["close"],
            }
            break
        if best:
            hits.append(best)
    return hits


def wick_ray_segments(rays: dict[str, Any], last_time: int) -> list[dict[str, Any]]:
    segs: list[dict[str, Any]] = []
    for side in ("upper", "lower"):
        r = rays[side]
        t0 = int(r["startTime"])
        t1 = int(last_time if r["active"] else r["endTime"])
        if t1 > t0:
            segs.append(
                {
                    "t0": t0,
                    "t1": t1,
                    "price": float(r["level"]),
                    "active": bool(r["active"]),
                    "side": r["side"],
                }
            )
    return segs


def signal_to_chart_ray(sig: dict, candles: list[dict], last_time: int) -> dict[str, Any]:
    """Compact ray payload for HTML chart packs."""
    rays = resolve_signal_rays(candles, sig)
    return {
        "type": sig["type"],
        "time": int(sig["time"]),
        "active": bool(rays["upper"]["active"] or rays["lower"]["active"]),
        "segments": wick_ray_segments(rays, last_time),
    }
