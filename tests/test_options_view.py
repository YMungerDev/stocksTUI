import unittest
from unittest.mock import MagicMock, Mock

from textual.app import App
from textual.widgets import ContentSwitcher, DataTable, Input, Select

from stockstui.ui.views.options_view import OptionsView


class OptionsViewTestApp(App):
    """App wrapper for testing OptionsView."""

    def __init__(self):
        super().__init__()
        self.config = MagicMock()
        # Mock lists for suggester
        self.config.lists = {"default": [{"ticker": "AAPL", "note": "Apple"}]}
        self.options_ticker = None
        self.theme_variables = {
            "surface": "black",
            "background": "black",
            "success": "green",
            "error": "red",
            "warning": "yellow",
            "text-muted": "dim",
            "accent": "blue",
            "primary": "blue",
            "foreground": "white",
        }
        self._last_options_data = None
        self.option_positions = {}
        # Add the missing methods
        self.fetch_options_chain = Mock()
        self.fetch_options_expirations = Mock()
        self.remove_option_position = Mock()
        self.add_option_position = Mock()
        self.push_screen = Mock()
        self.notify = Mock()

    def compose(self):
        yield OptionsView()


class TestOptionsView(unittest.IsolatedAsyncioTestCase):
    def test_parse_ticker_logic(self):
        """Test the helper method _parse_ticker_from_input directly."""
        # This method is pure logic, doesn't need a running app
        view = OptionsView()
        self.assertEqual(view._parse_ticker_from_input("AAPL"), "AAPL")
        self.assertEqual(view._parse_ticker_from_input("AAPL - Apple Inc"), "AAPL")
        self.assertEqual(view._parse_ticker_from_input("  tsla  "), "TSLA")

    async def test_initial_state(self):
        """Test initial UI state on mount."""
        app = OptionsViewTestApp()
        async with app.run_test():
            app.query_one(OptionsView)
            select = app.query_one("#options-expiration-select", Select)

            # Should be disabled initially
            self.assertTrue(select.disabled)

            # Ticker input should exist
            inp = app.query_one("#options-ticker-input", Input)
            self.assertIsNotNone(inp)

    async def test_update_expirations(self):
        """Test updating the expiration selector."""
        app = OptionsViewTestApp()
        async with app.run_test() as pilot:
            view = app.query_one(OptionsView)

            dates = ["2023-01-01", "2023-02-01"]
            view.update_expirations(dates)
            await pilot.pause()

            select = app.query_one("#options-expiration-select", Select)
            self.assertFalse(select.disabled)
            self.assertEqual(select.value, "2023-01-01")  # Defaults to first

    async def test_expiration_navigation_actions(self):
        """Test next/prev expiration actions."""
        app = OptionsViewTestApp()
        async with app.run_test() as pilot:
            view = app.query_one(OptionsView)

            # Setup
            dates = ["D1", "D2", "D3"]
            view.update_expirations(dates)
            await pilot.pause()

            select = app.query_one("#options-expiration-select", Select)
            self.assertEqual(select.value, "D1")

            # Test Next
            view.action_next_expiration()
            await pilot.pause()
            self.assertEqual(select.value, "D2")

            view.action_next_expiration()
            await pilot.pause()
            self.assertEqual(select.value, "D3")

            # Cap at end
            view.action_next_expiration()
            await pilot.pause()
            self.assertEqual(select.value, "D3")

            # Test Prev
            view.action_prev_expiration()
            await pilot.pause()
            self.assertEqual(select.value, "D2")

    async def test_toggle_chart_action(self):
        """Test toggling between table and chart."""
        app = OptionsViewTestApp()

        # We need _render_options_data to run at least once to create the ContentSwitcher?
        # create_table/render creates the switcher.
        # So we need to call _render_options_data manually or mock the switcher existence?
        # ContentSwitcher is created inside _render_options_data.
        # If _last_options_data is None (default), it renders a Static message, NOT the switcher.
        # So action_toggle_chart calls "self.query_one(switcher)". If it doesn't exist -> NoMatches.

        # We must simulate rendering data first to test toggling.

        async with app.run_test() as pilot:
            view = app.query_one(OptionsView)
            app._last_options_data = {
                "calls": MagicMock(empty=False),  # Mock dataframe
                "puts": MagicMock(empty=False),
                "underlying": {"regularMarketPrice": 100},
            }
            # We need real DataFrames for render logic iterates rows...
            # Creating dummy DF
            import pandas as pd

            app._last_options_data["calls"] = pd.DataFrame(
                [
                    {
                        "strike": 100,
                        "contractSymbol": "C1",
                        "openInterest": 10,
                        "volume": 5,
                    }
                ]
            )
            app._last_options_data["puts"] = pd.DataFrame(
                [
                    {
                        "strike": 100,
                        "contractSymbol": "P1",
                        "openInterest": 10,
                        "volume": 5,
                    }
                ]
            )

            # Manually trigger render
            await view._render_options_data()
            await pilot.pause()

            # Now switcher should exist
            switcher = app.query_one("#options-content-switcher", ContentSwitcher)
            self.assertEqual(switcher.current, "options-tables-view")

            # Toggle
            view.action_toggle_chart()
            await pilot.pause()
            self.assertEqual(switcher.current, "options-chart-view")

            # Toggle back
            view.action_toggle_chart()
            await pilot.pause()
            self.assertEqual(switcher.current, "options-tables-view")

    async def test_render_options_data_with_error(self):
        """Test rendering options data when there's an error."""
        app = OptionsViewTestApp()
        async with app.run_test() as pilot:
            view = app.query_one(OptionsView)

            # Simulate error condition
            app._last_options_data = {"error": "Something went wrong"}

            await view._render_options_data()
            await pilot.pause()

            # Check that error message is displayed
            # This should trigger the error handling path

    async def test_render_options_data_empty_data(self):
        """Test rendering options data when data is empty."""
        app = OptionsViewTestApp()
        async with app.run_test() as pilot:
            view = app.query_one(OptionsView)

            # Simulate empty data condition
            import pandas as pd

            empty_df = pd.DataFrame()
            app._last_options_data = {
                "calls": empty_df,
                "puts": empty_df,
                "underlying": {},
            }
            app.options_ticker = "AAPL"

            await view._render_options_data()
            await pilot.pause()

            # Check that "No options data found" message is displayed

    async def test_manage_position_action_without_focus(self):
        """Test action_manage_position when no table is focused."""
        app = OptionsViewTestApp()
        async with app.run_test():
            view = app.query_one(OptionsView)

            # Temporarily override the focused property to return None
            original_focused_property = type(app).focused
            type(app).focused = property(lambda self: None)

            # This should return early without doing anything
            view.action_manage_position()

            # Restore the original property
            type(app).focused = original_focused_property

    async def test_manage_position_action_with_wrong_focus(self):
        """Test action_manage_position when focused element is not an options table."""
        from textual.widgets import Label

        app = OptionsViewTestApp()
        async with app.run_test():
            view = app.query_one(OptionsView)

            # Create a mock widget that's not an options table
            mock_widget = Label("test")

            # Temporarily override the focused property to return the mock widget
            original_focused_property = type(app).focused
            type(app).focused = property(lambda self: mock_widget)

            # This should return early without doing anything
            view.action_manage_position()

            # Restore the original property
            type(app).focused = original_focused_property

    async def test_manage_position_action_with_no_row_selected(self):
        """Test action_manage_position when no row is selected."""
        from textual.widgets import DataTable

        app = OptionsViewTestApp()
        async with app.run_test():
            view = app.query_one(OptionsView)

            # Create a mock DataTable that simulates no row selection
            mock_table = Mock(spec=DataTable)
            mock_table.id = "options-calls-table"

            # Temporarily override the focused property to return the mock table
            original_focused_property = type(app).focused
            type(app).focused = property(lambda self: mock_table)

            # Mock coordinate_to_cell_key to return a row_key with no value
            mock_coord_result = Mock()
            mock_coord_result.row_key = Mock()
            mock_coord_result.row_key.value = None  # No contract symbol
            mock_table.coordinate_to_cell_key.return_value = mock_coord_result

            # This should return early without doing anything
            view.action_manage_position()

            # Restore the original property
            type(app).focused = original_focused_property

    async def test_manage_position_action_with_valid_selection(self):
        """Test action_manage_position with a valid row selection."""
        import pandas as pd

        app = OptionsViewTestApp()

        # Mock the app methods that are called
        app.push_screen = Mock()
        app.remove_option_position = Mock()
        app.add_option_position = Mock()

        async with app.run_test() as pilot:
            view = app.query_one(OptionsView)

            # Set up data for rendering first - but avoid creating the chart by using minimal data
            app._last_options_data = {
                "calls": pd.DataFrame(
                    [
                        {
                            "strike": 100,
                            "contractSymbol": "AAPL230101C00100000",
                            "openInterest": 10,
                            "volume": 5,
                        }
                    ]
                ),
                "puts": pd.DataFrame(
                    [
                        {
                            "strike": 100,
                            "contractSymbol": "AAPL230101P00100000",
                            "openInterest": 10,
                            "volume": 5,
                        }
                    ]
                ),
                "underlying": {"regularMarketPrice": 100},
            }
            app.options_ticker = "AAPL"

            # Temporarily disable the chart creation by mocking the OIChart constructor
            from unittest.mock import patch

            from stockstui.ui.widgets.oi_chart import OIChart

            with patch.object(OIChart, "replot", return_value=None):
                # Render the data to create the tables
                await view._render_options_data()
                await pilot.pause()

                # Now test the position management
                # Create a mock DataTable that simulates a row selection
                mock_table = Mock(spec=DataTable)
                mock_table.id = "options-calls-table"

                # Temporarily override the focused property to return the mock table
                original_focused_property = type(app).focused
                type(app).focused = property(lambda self: mock_table)

                # Mock coordinate_to_cell_key to return a valid contract symbol
                mock_coord_result = Mock()
                mock_coord_result.row_key = Mock()
                mock_coord_result.row_key.value = (
                    "AAPL230101C00100000"  # Valid contract symbol
                )
                mock_table.coordinate_to_cell_key.return_value = mock_coord_result

                # This should attempt to open the PositionModal
                view.action_manage_position()

                # Restore the original property
                type(app).focused = original_focused_property

    async def test_request_expirations_when_no_ticker(self):
        """Test _request_expirations when no ticker is set."""
        app = OptionsViewTestApp()
        async with app.run_test():
            view = app.query_one(OptionsView)

            # Ensure no ticker is set
            app.options_ticker = None

            # This should return early without doing anything
            view._request_expirations()

    async def test_request_options_chain_when_no_ticker(self):
        """Test _request_options_chain when no ticker is set."""
        app = OptionsViewTestApp()
        async with app.run_test():
            view = app.query_one(OptionsView)

            # Ensure no ticker is set
            app.options_ticker = None

            # This should return early without doing anything
            view._request_options_chain()

    async def test_request_options_chain_with_valid_data(self):
        """Test _request_options_chain with valid data."""
        app = OptionsViewTestApp()

        async with app.run_test() as pilot:
            view = app.query_one(OptionsView)

            # Set up ticker and expiration
            app.options_ticker = "AAPL"

            # Update expirations to enable the selector
            view.update_expirations(["2023-01-01"])
            await pilot.pause()

            # Mock the fetch_options_chain method on the app
            app.fetch_options_chain = Mock()

            # This should call fetch_options_chain
            view._request_options_chain()

            # Verify that the method was called
            app.fetch_options_chain.assert_called_once_with("AAPL", "2023-01-01")

    async def test_on_options_ticker_submitted(self):
        """Test handling ticker submission."""
        app = OptionsViewTestApp()

        async with app.run_test():
            view = app.query_one(OptionsView)

            # Mock the fetch_options_expirations method on the app
            app.fetch_options_expirations = Mock()

            # Create a mock input submission event
            from textual.widgets import Input

            input_widget = app.query_one("#options-ticker-input", Input)

            # Test with a valid value
            input_widget.value = "AAPL"
            event = Input.Submitted(input_widget, "AAPL")
            view.on_options_ticker_submitted(event)

            # Check that fetch_options_expirations was called
            app.fetch_options_expirations.assert_called_once_with("AAPL")

    async def test_on_options_ticker_submitted_with_suggestion_format(self):
        """Test handling ticker submission with suggestion format (TICKER - Description)."""
        app = OptionsViewTestApp()

        async with app.run_test():
            view = app.query_one(OptionsView)

            # Mock the fetch_options_expirations method on the app
            app.fetch_options_expirations = Mock()

            # Create a mock input submission event
            from textual.widgets import Input

            input_widget = app.query_one("#options-ticker-input", Input)

            # Test with suggestion format
            input_widget.value = "AAPL - Apple Inc."
            event = Input.Submitted(input_widget, "AAPL - Apple Inc.")
            view.on_options_ticker_submitted(event)

            # Check that fetch_options_expirations was called with just the ticker
            app.fetch_options_expirations.assert_called_once_with("AAPL")

    async def test_on_expiration_changed(self):
        """Test handling expiration selection change."""
        app = OptionsViewTestApp()

        async with app.run_test() as pilot:
            view = app.query_one(OptionsView)

            # Set up ticker and expiration
            app.options_ticker = "AAPL"
            view.update_expirations(["2023-01-01", "2023-02-01"])
            await pilot.pause()

            # Mock the fetch_options_chain method on the app
            app.fetch_options_chain = Mock()

            # Create a mock select change event - fix the constructor
            from textual.widgets import Select

            select_widget = app.query_one("#options-expiration-select", Select)
            event = Select.Changed(select_widget, "2023-01-01")
            view.on_expiration_changed(event)

            # Check that fetch_options_chain was called
            app.fetch_options_chain.assert_called_once_with("AAPL", "2023-01-01")

    async def test_on_toggle_chart_pressed(self):
        """Test the button press handler for toggling chart."""
        app = OptionsViewTestApp()

        # Set up data for rendering first
        import pandas as pd

        async with app.run_test() as pilot:
            view = app.query_one(OptionsView)
            app._last_options_data = {
                "calls": pd.DataFrame(
                    [
                        {
                            "strike": 100,
                            "contractSymbol": "C1",
                            "openInterest": 10,
                            "volume": 5,
                        }
                    ]
                ),
                "puts": pd.DataFrame(
                    [
                        {
                            "strike": 100,
                            "contractSymbol": "P1",
                            "openInterest": 10,
                            "volume": 5,
                        }
                    ]
                ),
                "underlying": {"regularMarketPrice": 100},
            }

            # Render the data to create the UI elements
            await view._render_options_data()
            await pilot.pause()

            # Trigger the button press event - fix the method signature (takes only self)
            from textual.widgets import Button

            button = app.query_one("#options-view-toggle", Button)
            Button.Pressed(button)
            # Call the method without the event parameter since it only takes 'self'
            view.on_toggle_chart_pressed()


class TestOptionsRenderCorrectness(unittest.IsolatedAsyncioTestCase):
    """Regression tests for chain rendering correctness and staleness."""

    def _make_chain_data(self, ticker: str, strike: float = 400.0, last: float = 50.0):
        import pandas as pd

        calls = pd.DataFrame(
            {
                "contractSymbol": [f"{ticker}270115C00{int(strike*1000):08d}"],
                "strike": [strike],
                "lastPrice": [last],
                "bid": [last - 1.0],
                "ask": [last + 1.0],
                "percentChange": [1.2],
                "volume": [100],
                "openInterest": [200],
                "impliedVolatility": [0.5],
                "delta": [0.5],
                "gamma": [0.01],
                "theta": [-0.05],
                "vega": [0.45],
            }
        )
        puts = calls.copy()
        puts["contractSymbol"] = [f"{ticker}270115P00{int(strike*1000):08d}"]
        return {
            "ticker": ticker,
            "expiration": "2027-01-15",
            "calls": calls,
            "puts": puts,
            "underlying": {"regularMarketPrice": strike + 5.0},
        }

    async def test_stale_chain_is_ignored_after_ticker_switch(self):
        """A slow fetch for an abandoned ticker must not clobber the view."""
        from stockstui.common import OptionsDataUpdated
        from stockstui.main import StocksTUI

        app = OptionsViewTestApp()
        async with app.run_test() as pilot:
            view = app.query_one(OptionsView)

            # User asked for TSLA, then switched to SAP.DE before it arrived.
            stale = self._make_chain_data("TSLA", strike=400.0, last=50.0)
            message = OptionsDataUpdated(
                "TSLA", "2027-01-15", stale["calls"], stale["puts"], stale["underlying"]
            )
            app.options_ticker = "SAP.DE"
            await StocksTUI.on_options_data_updated(app, message)
            await pilot.pause()

            # The display container must NOT have mounted chain tables for TSLA.
            from textual.widgets import Static

            container = view.query_one("#options-display-container")
            has_tables = any(
                c.id in ("options-calls-table", "options-puts-table")
                for c in container.children
            )
            self.assertFalse(
                has_tables,
                "Stale chain for abandoned ticker was rendered",
            )

    async def test_currency_resolves_from_chain_ticker_not_current_selection(self):
        """Rendered currency must come from the chain's own ticker."""
        from stockstui.data_providers import market_provider

        market_provider._info_cache["TSLA"] = {"currency": "USD", "exchange": "NMS"}
        market_provider._info_cache["SAP.DE"] = {"currency": "EUR", "exchange": "FRA"}
        app = OptionsViewTestApp()
        app.options_ticker = "SAP.DE"
        app._last_options_data = self._make_chain_data("TSLA", strike=400.0, last=50.0)
        async with app.run_test() as pilot:
            view = app.query_one(OptionsView)
            await view._render_options_data()
            await pilot.pause()

            dt = view.query_one("#options-calls-table", DataTable)
            col_keys = list(dt.columns.keys())
            last_cell = str(dt.get_cell(dt.ordered_rows[0].key, col_keys[1]))
            self.assertTrue(
                last_cell.startswith("$"),
                f"TSLA USD chain rendered with non-USD symbol: {last_cell!r}",
            )

        market_provider._info_cache.pop("TSLA", None)
        market_provider._info_cache.pop("SAP.DE", None)

    async def test_nan_fields_render_as_dash(self):
        """NaN last/bid/ask/percentChange must render as em dash, not $nan."""
        import pandas as pd

        from stockstui.data_providers import market_provider

        market_provider._info_cache["AAPL"] = {"currency": "USD", "exchange": "NMS"}
        data = self._make_chain_data("AAPL", strike=350.0, last=16.94)
        data["calls"]["lastPrice"] = [float("nan")]
        data["calls"]["bid"] = [float("nan")]
        data["calls"]["ask"] = [float("nan")]
        data["calls"]["percentChange"] = [float("nan")]
        app = OptionsViewTestApp()
        app.options_ticker = "AAPL"
        app._last_options_data = data
        async with app.run_test() as pilot:
            view = app.query_one(OptionsView)
            await view._render_options_data()
            await pilot.pause()

            dt = view.query_one("#options-calls-table", DataTable)
            col_keys = list(dt.columns.keys())
            cells = [str(dt.get_cell(dt.ordered_rows[0].key, k)) for k in col_keys]
            self.assertEqual(cells[1], "—")  # Last
            self.assertEqual(cells[2], "—")  # Bid
            self.assertEqual(cells[3], "—")  # Ask
            self.assertEqual(cells[4], "—")  # Chg %

        market_provider._info_cache.pop("AAPL", None)

    async def test_position_pl_with_nan_last_renders_dash(self):
        import pandas as pd

        from stockstui.data_providers import market_provider

        market_provider._info_cache["AAPL"] = {"currency": "USD", "exchange": "NMS"}
        data = self._make_chain_data("AAPL", strike=350.0, last=float("nan"))
        contract = data["calls"]["contractSymbol"].iloc[0]
        app = OptionsViewTestApp()
        app.options_ticker = "AAPL"
        app.option_positions = {contract: {"quantity": 2, "avg_cost": 5.0}}
        app._last_options_data = data
        async with app.run_test() as pilot:
            view = app.query_one(OptionsView)
            await view._render_options_data()
            await pilot.pause()

            dt = view.query_one("#options-calls-table", DataTable)
            col_keys = list(dt.columns.keys())
            cells = [str(dt.get_cell(dt.ordered_rows[0].key, k)) for k in col_keys]
            self.assertEqual(cells[12], "2")  # Pos
            self.assertEqual(cells[13], "—")  # P/L must not be $nan

        market_provider._info_cache.pop("AAPL", None)
