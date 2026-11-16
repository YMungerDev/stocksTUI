"""Price alert rules: evaluation, re-arm logic, and notifications (#3).

Rules are stored in the alert_rules table and evaluated on every price
refresh. An alert fires once per crossing: while the condition keeps
holding, `last_state` marks the rule as fired and it stays quiet; when the
condition resets, the marker clears and the rule can fire again.
"""

import logging
import shutil
import subprocess
from typing import Any, Callable

from stockstui.database.db_manager import DbManager
from stockstui.presentation.metrics import relative_volume

logger = logging.getLogger(__name__)

# Rule types:
RULE_ABOVE = "above"
RULE_BELOW = "below"
RULE_PCT_UP = "pct_up"
RULE_PCT_DOWN = "pct_down"
RULE_52W_HIGH = "new_52w_high"
RULE_52W_LOW = "new_52w_low"
RULE_REL_VOLUME = "volume_above"

RULE_TYPES: dict[str, str] = {
    RULE_ABOVE: "Price above",
    RULE_BELOW: "Price below",
    RULE_PCT_UP: "Day move above",
    RULE_PCT_DOWN: "Day move below",
    RULE_52W_HIGH: "New 52-week high",
    RULE_52W_LOW: "New 52-week low",
    RULE_REL_VOLUME: "Volume above",
}
# Rules whose threshold is a percentage move rather than a price level.
PCT_RULES = frozenset({RULE_PCT_UP, RULE_PCT_DOWN})


def evaluate_rule(rule: dict[str, Any], quote: dict[str, Any]) -> bool:
    """
    Whether the rule's condition currently holds for this quote.

    Pure function: takes the rule row and a price-cache entry, returns
    True/False. Volume rules need bars via the db_manager passed separately
    by the caller (evaluate_alerts).
    """
    rule_type = rule["rule_type"]
    threshold = rule["threshold"]
    price = quote.get("price")
    if price is None:
        return False
    if rule_type == RULE_ABOVE:
        return price > threshold
    if rule_type == RULE_BELOW:
        return price < threshold
    if rule_type in (RULE_PCT_UP, RULE_PCT_DOWN):
        previous_close = quote.get("previous_close")
        if previous_close is None or previous_close == 0:
            return False
        day_change_pct = (price / previous_close - 1) * 100
        if rule_type == RULE_PCT_UP:
            return day_change_pct > threshold
        return day_change_pct < -threshold
    if rule_type == RULE_52W_HIGH:
        high = quote.get("fifty_two_week_high")
        return high is not None and price >= high
    if rule_type == RULE_52W_LOW:
        low = quote.get("fifty_two_week_low")
        return low is not None and price <= low
    if rule_type == RULE_REL_VOLUME:
        return False  # evaluated with bars; see evaluate_alerts
    return False


def _rule_description(rule: dict[str, Any], quote: dict[str, Any]) -> str:
    """Human-readable line for the notification."""
    rule_type = rule["rule_type"]
    threshold = rule["threshold"]
    label = RULE_TYPES.get(rule_type, rule_type)
    if rule_type in (RULE_ABOVE, RULE_BELOW):
        return f"{label} {threshold:g}"
    if rule_type in (RULE_PCT_UP, RULE_PCT_DOWN):
        return f"{label} {threshold:g}%"
    if rule_type == RULE_REL_VOLUME:
        return f"{label} {threshold:g}x average"
    return label


def evaluate_alerts(
    db_manager: DbManager,
    quotes: dict[str, dict[str, Any]],
    notify: Callable[[str, str], None],
) -> list[int]:
    """
    Evaluates every stored rule against the given quotes (price-cache
    state keyed by ticker) and fires notifications for crossings.

    Re-arm logic: a rule fires only when its condition transitions from
    not-holding to holding (or the rule has never fired). The transition is
    tracked in last_state so it survives restarts. Returns fired rule ids.
    """
    if db_manager is None or not quotes:
        return []
    rules = db_manager.load_alert_rules()
    fired: list[int] = []
    for rule in rules:
        ticker = rule["ticker"]
        entry = quotes.get(ticker)
        if entry is None:
            continue
        # Accept either the cache entry ({expiry, data: {...}}) or the bare
        # quote dict, so callers can pass either shape.
        quote = entry.get("data") if isinstance(entry.get("data"), dict) else entry
        if quote is None:
            continue
        if rule["rule_type"] == RULE_REL_VOLUME:
            # Volume rules read the bar store (#1): today vs 20-day average.
            try:
                bars = db_manager.load_daily_bars(ticker)
            except Exception as e:  # noqa: BLE001 - alert eval must not crash
                logger.warning(f"Failed to load bars for alert {rule['id']}: {e}")
                continue
            if bars is None or bars.empty:
                continue
            rel_vol = relative_volume(bars["Volume"])
            if rel_vol is None or rel_vol <= rule["threshold"]:
                condition = False
            else:
                condition = True
        else:
            condition = evaluate_rule(rule, quote)
        if not condition:
            if rule["last_state"] is not None:
                # Condition reset: re-arm the rule for the next crossing.
                db_manager.update_alert_rule_state(rule["id"], None)
            continue
        if rule["last_state"] == "fired":
            continue  # already fired while holding; do not re-notify
        db_manager.update_alert_rule_state(rule["id"], "fired")
        fired.append(rule["id"])
        try:
            notify(ticker, _rule_description(rule, quote))
        except Exception as e:  # noqa: BLE001 - notification must not break eval
            logger.warning(f"Alert notification failed for {ticker}: {e}")
    return fired


def send_desktop_notification(title: str, message: str) -> bool:
    """
    Sends a desktop notification via notify-send. Returns False silently
    when the tool is unavailable or fails (per #3: skip silently).
    """
    binary = shutil.which("notify-send")
    if not binary:
        return False
    try:
        result = subprocess.run(
            [binary, title, message],
            capture_output=True,
            timeout=5,
            check=False,
        )
        return result.returncode == 0
    except (subprocess.SubprocessError, OSError) as e:
        logger.debug(f"notify-send failed: {e}")
        return False


def describe_rule_short(rule: dict[str, Any]) -> str:
    """Compact label for row indicators and the modal list."""
    rule_type = rule["rule_type"]
    threshold = rule["threshold"]
    label = RULE_TYPES.get(rule_type, rule_type)
    if rule_type in (RULE_ABOVE, RULE_BELOW):
        return f"{label} {threshold:g}"
    if rule_type in PCT_RULES:
        return f"{label} {threshold:g}%"
    if rule_type == RULE_REL_VOLUME:
        return f"{label} {threshold:g}x"
    return label
