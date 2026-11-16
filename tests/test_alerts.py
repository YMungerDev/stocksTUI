"""Tests for price alerts: rule evaluation, re-arm, persistence, notifier (#3)."""

import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from stockstui import alerts
from stockstui.alerts import (
    RULE_52W_HIGH,
    RULE_52W_LOW,
    RULE_ABOVE,
    RULE_BELOW,
    RULE_PCT_DOWN,
    RULE_PCT_UP,
    RULE_REL_VOLUME,
    evaluate_alerts,
    evaluate_rule,
    send_desktop_notification,
)
from stockstui.database.db_manager import DbManager


class TestRuleEvaluation(unittest.TestCase):
    """evaluate_rule against hand-built quotes."""

    def test_price_above(self):
        rule = {"rule_type": RULE_ABOVE, "threshold": 150.0}
        self.assertTrue(evaluate_rule(rule, {"price": 151.0}))
        self.assertFalse(evaluate_rule(rule, {"price": 149.0}))
        self.assertFalse(evaluate_rule(rule, {"price": 150.0}))  # strictly above

    def test_price_below(self):
        rule = {"rule_type": RULE_BELOW, "threshold": 100.0}
        self.assertTrue(evaluate_rule(rule, {"price": 99.0}))
        self.assertFalse(evaluate_rule(rule, {"price": 100.0}))

    def test_pct_move_up(self):
        rule = {"rule_type": RULE_PCT_UP, "threshold": 5.0}
        # 105 vs 100 = +5.0% -> "beyond" means strictly more than 5%.
        self.assertTrue(evaluate_rule(rule, {"price": 105.0, "previous_close": 100.0}))
        self.assertFalse(evaluate_rule(rule, {"price": 104.0, "previous_close": 100.0}))
        self.assertTrue(evaluate_rule(rule, {"price": 106.0, "previous_close": 100.0}))
        self.assertFalse(evaluate_rule(rule, {"price": 106.0, "previous_close": None}))

    def test_pct_move_down(self):
        rule = {"rule_type": RULE_PCT_DOWN, "threshold": 3.0}
        self.assertTrue(evaluate_rule(rule, {"price": 96.0, "previous_close": 100.0}))
        self.assertFalse(evaluate_rule(rule, {"price": 98.0, "previous_close": 100.0}))

    def test_52w_high_low(self):
        self.assertTrue(
            evaluate_rule(
                {"rule_type": RULE_52W_HIGH, "threshold": 0},
                {"price": 360.83, "fifty_two_week_high": 360.83},
            )
        )
        self.assertFalse(
            evaluate_rule(
                {"rule_type": RULE_52W_HIGH, "threshold": 0},
                {"price": 300.0, "fifty_two_week_high": 360.83},
            )
        )
        self.assertTrue(
            evaluate_rule(
                {"rule_type": RULE_52W_LOW, "threshold": 0},
                {"price": 243.42, "fifty_two_week_low": 243.42},
            )
        )

    def test_missing_price_is_never_firing(self):
        for rule_type in (RULE_ABOVE, RULE_BELOW, RULE_52W_HIGH, RULE_52W_LOW):
            self.assertFalse(evaluate_rule({"rule_type": rule_type, "threshold": 1}, {}))


class TestReArmLogic(unittest.TestCase):
    """The fires-once-per-crossing acceptance, including across restarts."""

    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.db = DbManager(Path(self.tmpdir.name) / "alerts.db")
        self.notifications: list[tuple[str, str]] = []
        self.rule_id = self.db.save_alert_rule("AAPL", RULE_ABOVE, 150.0)

    def tearDown(self):
        self.db.close()
        self.tmpdir.cleanup()

    def _notify(self, ticker: str, description: str) -> None:
        self.notifications.append((ticker, description))

    def _quotes(self, price: float) -> dict:
        return {"AAPL": {"price": price}}

    def test_fires_once_per_crossing(self):
        # Crossing: 149 -> 151 fires once.
        evaluate_alerts(self.db, self._quotes(149.0), self._notify)
        self.assertEqual(self.notifications, [])
        evaluate_alerts(self.db, self._quotes(151.0), self._notify)
        self.assertEqual(len(self.notifications), 1)
        # Holding: repeated refreshes while the condition holds stay quiet.
        evaluate_alerts(self.db, self._quotes(152.0), self._notify)
        evaluate_alerts(self.db, self._quotes(153.0), self._notify)
        self.assertEqual(len(self.notifications), 1)
        # Reset: price falls back, then crosses again -> fires a second time.
        evaluate_alerts(self.db, self._quotes(149.0), self._notify)
        evaluate_alerts(self.db, self._quotes(151.0), self._notify)
        self.assertEqual(len(self.notifications), 2)

    def test_state_survives_restart(self):
        evaluate_alerts(self.db, self._quotes(151.0), self._notify)
        self.assertEqual(len(self.notifications), 1)
        # A brand-new DbManager over the same file: fired state persists.
        db2 = DbManager(Path(self.tmpdir.name) / "alerts.db")
        try:
            evaluate_alerts(db2, self._quotes(155.0), self._notify)
        finally:
            db2.close()
        self.assertEqual(len(self.notifications), 1)

    def test_volume_rule_uses_bars(self):
        import pandas as pd

        rule_id = self.db.save_alert_rule("TSLA", RULE_REL_VOLUME, 2.0)
        # 21 bars: 20 quiet days at 1000, today at 3000 (3x).
        volumes = [1000.0] * 20 + [3000.0]
        index = pd.bdate_range("2026-01-01", periods=len(volumes))
        frame = pd.DataFrame(
            {
                "Open": 100.0,
                "High": 100.0,
                "Low": 100.0,
                "Close": 100.0,
                "Volume": volumes,
            },
            index=index,
        )
        frame["Adj Close"] = 100.0
        self.db.save_daily_bars("TSLA", frame)

        evaluate_alerts(self.db, {"TSLA": {"price": 100.0}}, self._notify)
        self.assertEqual(len(self.notifications), 1)

        # Next refresh, still 3x: quiet.
        evaluate_alerts(self.db, {"TSLA": {"price": 100.0}}, self._notify)
        self.assertEqual(len(self.notifications), 1)

    def test_volume_rule_without_bars_never_fires(self):
        self.db.save_alert_rule("NOPE", RULE_REL_VOLUME, 2.0)
        fired = evaluate_alerts(self.db, {"NOPE": {"price": 10.0}}, self._notify)
        self.assertEqual(fired, [])
        self.assertEqual(self.notifications, [])

    def test_rule_for_absent_ticker_is_skipped(self):
        evaluate_alerts(self.db, {"MSFT": {"price": 400.0}}, self._notify)
        self.assertEqual(self.notifications, [])


class TestPersistence(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.path = Path(self.tmpdir.name) / "alerts.db"
        self.db = DbManager(self.path)

    def tearDown(self):
        self.db.close()
        self.tmpdir.cleanup()

    def test_rules_survive_restart(self):
        self.db.save_alert_rule("AAPL", RULE_ABOVE, 150.0)
        self.db.save_alert_rule("MSFT", RULE_PCT_UP, 5.0)
        self.db.close()

        db2 = DbManager(self.path)
        try:
            rules = db2.load_alert_rules()
        finally:
            db2.close()
        self.assertEqual(len(rules), 2)
        self.assertEqual({r["ticker"] for r in rules}, {"AAPL", "MSFT"})
        self.assertEqual(rules[0]["threshold"], 150.0)

    def test_delete_rule(self):
        rule_id = self.db.save_alert_rule("AAPL", RULE_ABOVE, 150.0)
        self.db.delete_alert_rule(rule_id)
        self.assertEqual(self.db.load_alert_rules(), [])

    def test_update_state(self):
        rule_id = self.db.save_alert_rule("AAPL", RULE_ABOVE, 150.0)
        self.db.update_alert_rule_state(rule_id, "fired")
        self.assertEqual(self.db.load_alert_rules()[0]["last_state"], "fired")
        self.db.update_alert_rule_state(rule_id, None)
        self.assertIsNone(self.db.load_alert_rules()[0]["last_state"])


class TestDesktopNotifier(unittest.TestCase):
    @patch("stockstui.alerts.shutil.which", return_value=None)
    def test_missing_notify_send_skips_silently(self, _mock_which):
        self.assertFalse(send_desktop_notification("t", "m"))

    @patch("stockstui.alerts.subprocess.run")
    @patch("stockstui.alerts.shutil.which", return_value="/usr/bin/notify-send")
    def test_calls_notify_send(self, _mock_which, mock_run):
        mock_run.return_value = MagicMock(returncode=0)
        self.assertTrue(send_desktop_notification("title", "body"))
        mock_run.assert_called_once()
        args = mock_run.call_args[0][0]
        self.assertEqual(args[0], "/usr/bin/notify-send")
        self.assertEqual(args[1:], ["title", "body"])

    @patch("stockstui.alerts.subprocess.run", side_effect=OSError("boom"))
    @patch("stockstui.alerts.shutil.which", return_value="/usr/bin/notify-send")
    def test_failure_returns_false_silently(self, _mock_which, _mock_run):
        self.assertFalse(send_desktop_notification("t", "m"))


if __name__ == "__main__":
    unittest.main()


class TestAlertModalAndAppFlow(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        import tempfile

        self.tmpdir = tempfile.TemporaryDirectory()
        self.db = DbManager(Path(self.tmpdir.name) / "alerts.db")

    def tearDown(self):
        self.db.close()
        self.tmpdir.cleanup()

    async def test_modal_adds_and_deletes_rules(self):
        from textual.app import App
        from textual.widgets import Button, Input, Select

        from stockstui.ui.alert_modal import AlertModal

        class HostApp(App):
            db_manager = MagicMock()
            notify = MagicMock()

            def compose(self):
                yield AlertModal("AAPL", [])

        app = HostApp()
        app.db_manager = self.db
        # The modal is the app's default screen; push it as a screen so
        # click targeting works, mirroring push_screen usage in the app.
        async with app.run_test() as pilot:
            modal = AlertModal("AAPL", [])
            await app.push_screen(modal)
            await pilot.pause()
            # Add a rule via the modal controls.
            modal.query_one("#alert-type-select", Select).value = RULE_ABOVE
            modal.query_one("#alert-threshold-input", Input).value = "150"
            await pilot.click("#alert-add")
            await pilot.pause()

            rules = self.db.load_alert_rules()
            self.assertEqual(len(rules), 1)
            self.assertEqual(rules[0]["ticker"], "AAPL")
            self.assertEqual(rules[0]["threshold"], 150.0)

            # Delete it via the row button.
            from textual.widgets import Button

            delete_button = modal.query_one(
                "#delete-" + str(rules[0]["id"]), Button
            )
            modal.on_button_pressed(Button.Pressed(delete_button))
            await pilot.pause()
            self.assertEqual(self.db.load_alert_rules(), [])

    async def test_app_refresh_flow_evaluates_alerts(self):
        """Full path: price refresh -> evaluation -> toast + desktop."""
        import pandas as pd

        from stockstui.data_providers import market_provider
        from stockstui.main import StocksTUI

        market_provider._price_cache.clear()
        market_provider._calendar_cache.clear()
        self.db.save_alert_rule("AAPL", RULE_BELOW, 100.0)

        fired_desktop: list[tuple[str, str]] = []
        original = alerts.send_desktop_notification
        alerts.send_desktop_notification = lambda t, m: fired_desktop.append((t, m)) or True

        try:
            app = StocksTUI()
            with patch("stockstui.config_manager.PlatformDirs") as mock_dirs:
                mock_dirs.return_value.user_config_dir = self.tmpdir.name
                app.config = __import__(
                    "stockstui.config_manager", fromlist=["ConfigManager"]
                ).ConfigManager(app_root=Path("stockstui").resolve().parent)
            app._load_and_register_themes()
            app.db_manager.close()
            app.db_manager = self.db
            app.notify = MagicMock()

            market_provider._price_cache["AAPL"] = {
                "expiry": pd.Timestamp.utcnow().to_pydatetime(),
                "data": {"symbol": "AAPL", "price": 95.0},
            }

            app._evaluate_alerts()

            app.notify.assert_called_once()
            call_kwargs = app.notify.call_args
            self.assertIn("AAPL", str(call_kwargs))
            self.assertEqual(len(fired_desktop), 1)
            # Second evaluation while still below 100: no new notifications.
            app.notify.reset_mock()
            app._evaluate_alerts()
            app.notify.assert_not_called()
        finally:
            alerts.send_desktop_notification = original
            market_provider._price_cache.clear()
