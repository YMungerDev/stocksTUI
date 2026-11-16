"""Modal for managing price alert rules for one ticker (#3)."""

import logging
from typing import ClassVar

from textual.app import ComposeResult
from textual.binding import Binding, BindingType
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Button, Input, Label, Select, Static

from stockstui.alerts import (
    RULE_52W_HIGH,
    RULE_52W_LOW,
    RULE_TYPES,
    describe_rule_short,
)

logger = logging.getLogger(__name__)


class AlertModal(ModalScreen[bool]):
    """Lists a ticker's alert rules and offers add/delete controls."""

    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("escape", "close_modal", "Close", show=False)
    ]

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

    def action_close_modal(self) -> None:
        """Closes the modal, returning whether any alert rules changed."""
        self.dismiss(self.dirty)

    @staticmethod
    def _describe(rule: dict) -> str:
        return describe_rule_short(rule)

    def _selected_rule_type(self) -> str | None:
        try:
            select = self.query_one("#alert-type-select", Select)
            if select.value is Select.BLANK or not select.value:
                return None
            return str(select.value)
        except Exception:  # noqa: BLE001
            return None

    def on_button_pressed(self, event: Button.Pressed) -> None:
        button_id = event.button.id or ""
        if button_id == "alert-close":
            self.dismiss(self.dirty)
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
            if rule_type is None:
                self.app.notify("Select a rule type.", severity="warning")
                return
            threshold_input = self.query_one("#alert-threshold-input", Input)
            if (
                rule_type in (RULE_52W_HIGH, RULE_52W_LOW)
                and not threshold_input.value.strip()
            ):
                threshold = 0.0
            else:
                try:
                    threshold = float(threshold_input.value)
                except ValueError:
                    self.app.notify(
                        "Enter a numeric threshold.", severity="warning"
                    )
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
