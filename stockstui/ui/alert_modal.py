"""Modal for managing price alert rules for one ticker (#3)."""

import logging

from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Button, Input, Label, Select, Static

from stockstui.alerts import PCT_RULES, RULE_TYPES

logger = logging.getLogger(__name__)


class AlertModal(ModalScreen[None]):
    """Lists a ticker's alert rules and offers add/delete controls."""

    def __init__(self, ticker: str, rules: list[dict]) -> None:
        """
        Args:
            ticker: The selected watchlist symbol.
            rules: Existing rule rows for the ticker (id/ticker/rule_type/threshold).
        """
        super().__init__()
        self.ticker = ticker
        self.rules = rules
        self.dirty = False

    def compose(self) -> ComposeResult:
        with Vertical(id="dialog"):
            yield Label(f"Price Alerts: {self.ticker}", classes="modal-header")
            with VerticalScroll(id="alert-list"):
                if self.rules:
                    for rule in self.rules:
                        with Horizontal(classes="alert-rule-row"):
                            yield Static(
                                self._describe(rule), classes="alert-rule-label"
                            )
                            yield Button(
                                "Delete",
                                variant="error",
                                id=f"delete-{rule['id']}",
                            )
                else:
                    yield Static(
                        "No alerts for this ticker yet.", classes="alert-rule-label"
                    )
            with Horizontal(classes="input-grid"):
                yield Label("Type:", classes="field-label")
                yield Select(
                    [(label, key) for key, label in RULE_TYPES.items()],
                    prompt="Rule type",
                    id="alert-type-select",
                    allow_blank=False,
                )
                yield Label("Threshold:", classes="field-label")
                yield Input(
                    placeholder="e.g. 150 or 5",
                    id="alert-threshold-input",
                    type="number",
                )
            with Horizontal(id="dialog-buttons"):
                yield Button("Add Alert", variant="primary", id="alert-add")
                yield Button("Close", id="alert-close")

    @staticmethod
    def _describe(rule: dict) -> str:
        rule_type = rule["rule_type"]
        label = RULE_TYPES.get(rule_type, rule_type)
        threshold = rule["threshold"]
        if rule_type in PCT_RULES:
            return f"{label} {threshold:g}%"
        if rule_type == "volume_above":
            return f"{label} {threshold:g}x"
        return f"{label} {threshold:g}"

    def _selected_rule_type(self) -> str | None:
        try:
            select = self.query_one("#alert-type-select", Select)
            return select.value if select.value else None
        except Exception:  # noqa: BLE001
            return None

    def on_button_pressed(self, event: Button.Pressed) -> None:
        button_id = event.button.id or ""
        if button_id == "alert-close":
            self.dismiss(None)
            return
        if button_id.startswith("delete-"):
            rule_id = int(button_id.split("-", 1)[1])
            self.app.db_manager.delete_alert_rule(rule_id)
            self.rules = [
                r for r in self.rules if r["id"] != rule_id
            ]
            self.dirty = True
            self._refresh_list()
            return
        if button_id == "alert-add":
            rule_type = self._selected_rule_type()
            threshold_input = self.query_one("#alert-threshold-input", Input)
            try:
                threshold = float(threshold_input.value)
            except ValueError:
                self.app.notify(
                    "Enter a numeric threshold.", severity="warning"
                )
                return
            if rule_type is None:
                return
            new_id = self.app.db_manager.save_alert_rule(
                self.ticker, rule_type, threshold
            )
            if new_id is not None:
                self.rules.append(
                    {
                        "id": new_id,
                        "ticker": self.ticker,
                        "rule_type": rule_type,
                        "threshold": threshold,
                        "last_state": None,
                    }
                )
                self.dirty = True
                threshold_input.value = ""
                self._refresh_list()
                self.app.notify(f"Alert added for {self.ticker}.")

    def _refresh_list(self) -> None:
        """Re-mounts the rule list after add/delete."""
        try:
            list_view = self.query_one("#alert-list", VerticalScroll)
            children = list(list_view.children)
            for child in children:
                child.remove()
            if self.rules:
                for rule in self.rules:
                    list_view.mount(
                        Horizontal(
                            Static(self._describe(rule), classes="alert-rule-label"),
                            Button(
                                "Delete",
                                variant="error",
                                id=f"delete-{rule['id']}",
                            ),
                            classes="alert-rule-row",
                        )
                    )
            else:
                list_view.mount(
                    Static(
                        "No alerts for this ticker yet.", classes="alert-rule-label"
                    )
                )
        except Exception as e:  # noqa: BLE001
            logger.warning(f"Failed to refresh alert list: {e}")
