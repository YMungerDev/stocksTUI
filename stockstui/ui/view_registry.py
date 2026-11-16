from dataclasses import dataclass

from textual.widget import Widget

from stockstui.ui.views.calendar_view import CalendarView
from stockstui.ui.views.debug_view import DebugView
from stockstui.ui.views.filings_view import FilingsView
from stockstui.ui.views.fred_view import FredView
from stockstui.ui.views.history_view import HistoryView
from stockstui.ui.views.news_view import NewsView
from stockstui.ui.views.options_view import OptionsView


@dataclass(frozen=True)
class TabViewSpec:
    """Declarative metadata for a non-watchlist application tab."""

    category: str
    view_id: str | None = None
    view_class: type[Widget] | None = None
    display_name: str | None = None
    primary_widget_selector: str | None = None
    scrollable_selector: str | None = None
    search_table_selector: str | None = None
    can_be_default_tab: bool = False
    clears_refresh_status: bool = True
    resets_config_sub_view: bool = False

    @property
    def label(self) -> str:
        if self.display_name:
            return self.display_name
        return self.category.replace("_", " ").capitalize()


SPECIAL_TAB_SPECS: tuple[TabViewSpec, ...] = (
    TabViewSpec(
        category="calendar",
        view_id="calendar-view",
        view_class=CalendarView,
        primary_widget_selector="#calendar-table",
        search_table_selector="#calendar-table",
        can_be_default_tab=False,
        clears_refresh_status=True,
        resets_config_sub_view=True,
    ),
    TabViewSpec(
        category="history",
        view_id="history-view",
        view_class=HistoryView,
        primary_widget_selector="#history-ticker-input",
        scrollable_selector="#history-display-container",
        can_be_default_tab=False,
        clears_refresh_status=True,
        resets_config_sub_view=True,
    ),
    TabViewSpec(
        category="news",
        view_id="news-view",
        view_class=NewsView,
        primary_widget_selector="#news-ticker-input",
        scrollable_selector="#news-output-display",
        can_be_default_tab=False,
        clears_refresh_status=True,
        resets_config_sub_view=True,
    ),
    TabViewSpec(
        category="filings",
        view_id="filings-view",
        view_class=FilingsView,
        primary_widget_selector="#filings-ticker-input",
        search_table_selector="#filings-table",
        can_be_default_tab=False,
        clears_refresh_status=True,
        resets_config_sub_view=True,
    ),
    TabViewSpec(
        category="options",
        view_id="options-view",
        view_class=OptionsView,
        primary_widget_selector="#options-ticker-input",
        can_be_default_tab=True,
        clears_refresh_status=True,
        resets_config_sub_view=False,
    ),
    TabViewSpec(
        category="fred",
        view_id="fred-view",
        view_class=FredView,
        display_name="FRED",
        primary_widget_selector="#fred-summary-table",
        can_be_default_tab=True,
        clears_refresh_status=False,
        resets_config_sub_view=False,
    ),
    TabViewSpec(
        category="debug",
        view_id="debug-view",
        view_class=DebugView,
        primary_widget_selector="#debug-table",
        search_table_selector="#debug-table",
        can_be_default_tab=False,
        clears_refresh_status=True,
        resets_config_sub_view=True,
    ),
)

CONFIGS_TAB_SPEC = TabViewSpec(
    category="configs",
    display_name="Configs",
    primary_widget_selector="#config-container",
    scrollable_selector="#config-container",
    can_be_default_tab=False,
    clears_refresh_status=True,
    resets_config_sub_view=False,
)

SPECIAL_TAB_BY_CATEGORY: dict[str, TabViewSpec] = {
    spec.category: spec for spec in (*SPECIAL_TAB_SPECS, CONFIGS_TAB_SPEC)
}

ORDERED_TOGGLEABLE_SPECIAL_CATEGORIES: tuple[str, ...] = tuple(
    spec.category for spec in SPECIAL_TAB_SPECS
)


def is_watchlist_category(category: str | None) -> bool:
    """Returns True when `category` represents 'all' or a user watchlist tab."""
    return bool(category) and category not in SPECIAL_TAB_BY_CATEGORY
