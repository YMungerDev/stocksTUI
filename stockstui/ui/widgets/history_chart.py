import pandas as pd
import numpy as np
from textual_plotext import PlotextPlot

from stockstui.presentation.overlays import (
    drawdown_from_peak,
    earnings_dates_in_range,
    moving_average,
    relative_volume_series,
)


class HistoryChart(PlotextPlot):
    """A custom widget to display a historical stock chart using plotext."""

    def __init__(self, data: pd.DataFrame, period: str, *args, **kwargs):
        """
        Initializes the HistoryChart widget.

        Args:
            data: A pandas DataFrame containing the historical data (must have a 'Close' column).
            period: The period of the historical data (e.g., "1mo", "1y"), used for date tick formatting.
            *args, **kwargs: Arguments passed to the parent PlotextPlot class.
        """
        super().__init__(*args, **kwargs)
        self._data = data
        self._period = period
        self._overlay_states = {
            "sma50": False,
            "sma200": False,
            "volume": False,
            "drawdown": False,
            "earnings": False,
        }

    def toggle_overlay(self, overlay: str) -> None:
        """Toggles one overlay and redraws (called from the view's keybindings)."""
        if overlay in self._overlay_states:
            self._overlay_states[overlay] = not self._overlay_states[overlay]
            self.replot()

    def on_mount(self) -> None:
        """Draws the plot when the widget is mounted to the application."""
        self.replot()

    def replot(self) -> None:
        """Draws (or redraws) the price line and any active overlays."""
        plt = self.plt  # Get the plotext plotting object
        plt.clear_data()  # Clear any existing data from the plot

        if self._data is None or self._data.empty:
            return  # Nothing to plot if no data

        # Convert datetime index to numerical unix timestamps for plotting on the x-axis
        x_data = [d.timestamp() for d in self._data.index]

        # Try to get closing prices, fall back to other columns if Close is missing
        if "Close" in self._data.columns:
            y_data = self._data["Close"].tolist()
        elif "Open" in self._data.columns:
            y_data = self._data["Open"].tolist()
        elif len(self._data.columns) > 0:
            # Use the first available numeric column
            y_data = self._data.iloc[:, 0].tolist()
        else:
            return  # No data to plot

        closes = pd.Series(y_data, index=self._data.index, dtype=float)
        theme_vars = self.app.theme_variables
        price_color = theme_vars.get("orange", "orange")
        accent_color = theme_vars.get("accent", "blue")
        muted_color = theme_vars.get("text-muted", "dim")
        success_color = theme_vars.get("success", "green")
        error_color = theme_vars.get("error", "red")
        warning_color = theme_vars.get("warning", "yellow")

        # Plot the price history first so overlays draw on top.
        plt.plot(x_data, y_data, color=price_color, label="Close")

        # Moving averages: drawn only when a full window of closes exists,
        # so the line never shows a truncated average.
        for key, window, color in (
            ("sma50", 50, accent_color),
            ("sma200", 200, warning_color),
        ):
            if self._overlay_states.get(key):
                sma = moving_average(closes, window)
                if sma is not None:
                    sma_x = [d.timestamp() for d in sma.index]
                    plt.plot(sma_x, sma.tolist(), color=color, label=f"SMA{window}")

        # Volume panel below the price plot.
        if self._overlay_states.get("volume") and "Volume" in self._data.columns:
            volumes = self._data["Volume"].astype(float)
            plt.plot(
                x_data,
                volumes.tolist(),
                color=muted_color,
                label="Volume",
                yside="right",
            )

        # Drawdown-from-peak panel.
        if self._overlay_states.get("drawdown"):
            dd = drawdown_from_peak(closes)
            plt.plot(
                x_data,
                dd.tolist(),
                color=error_color,
                label="Drawdown",
                yside="right",
            )

        # Vertical markers on earnings dates inside the visible range.
        if self._overlay_states.get("earnings"):
            start = self._data.index.min().date()
            end = self._data.index.max().date()
            for value in earnings_dates_in_range(self._data, start, end):
                timestamp = pd.Timestamp(value).timestamp()
                plt.vertical_line(timestamp, color=success_color)

        self._set_date_ticks()  # Configure x-axis (date) ticks and labels
        self._set_price_ticks()  # Configure y-axis (price) ticks and labels

        plt.title("Price History")  # Set chart title
        plt.ylabel("Price (USD)")  # Set y-axis label
        plt.grid(True, True)  # Enable grid lines for better readability

    def _get_nice_y_ticks(
        self, y_min: float, y_max: float, num_ticks: int = 5
    ) -> list[float]:
        """
        Calculates 'nice' (human-readable) tick values for the y-axis.
        This ensures that the price labels are well-spaced and easy to read.

        Args:
            y_min: The minimum value on the y-axis.
            y_max: The maximum value on the y-axis.
            num_ticks: The desired approximate number of ticks.

        Returns:
            A list of calculated tick values.
        """
        y_range = y_max - y_min
        if y_range == 0:
            return [y_min]  # Handle cases with no range

        # Calculate a rough step size
        rough_step = y_range / (num_ticks - 1)

        if rough_step == 0:  # Avoid division by zero if range is tiny
            return [y_min]

        # Determine the magnitude of the rough step
        power = 10 ** -np.floor(np.log10(abs(rough_step)))
        nice_rough_step = rough_step * power

        # Define preferred multipliers for nice steps
        nice_multipliers = [0.1, 0.2, 0.25, 0.5, 1, 2, 5, 10]
        nice_step = rough_step
        # Find the smallest 'nice' multiplier that is greater than or equal to the rough step
        for mult in nice_multipliers:
            if mult >= nice_rough_step:
                nice_step = mult / power
                break

        # Calculate the tick values, starting from a "nice" number below y_min
        start_tick = np.floor(y_min / nice_step) * nice_step
        end_tick = np.ceil(y_max / nice_step) * nice_step

        ticks = np.arange(start_tick, end_tick + nice_step, nice_step)

        return list(ticks)

    def _set_price_ticks(self):
        """
        Sets the y-axis ticks and labels to human-readable, formatted price values.
        It calculates appropriate tick positions and forces plotext to use them.
        """
        plt = self.plt

        # Find the first numeric column to use for y-axis range
        price_column = None
        if "Close" in self._data.columns:
            price_column = "Close"
        elif "Open" in self._data.columns:
            price_column = "Open"
        elif len(self._data.columns) > 0:
            price_column = self._data.columns[0]

        if price_column is None:
            return

        y_min, y_max = self._data[price_column].min(), self._data[price_column].max()

        if y_min is not None and y_max is not None and y_max >= y_min:
            # Determine the number of ticks based on available vertical space.
            num_ticks = 4 if self.size.height < 15 else 6

            # First, calculate our own "nice" ticks.
            ticks = self._get_nice_y_ticks(y_min, y_max, num_ticks)

            # Create the corresponding labels, formatted as currency.
            labels = [f"{tick:,.2f}" for tick in ticks]

            # Then, set the y-axis limits to match our calculated ticks. This is the crucial step
            # to ensure plotext uses our custom ticks correctly.
            if ticks:
                if len(ticks) > 1:
                    plt.ylim(ticks[0], ticks[-1])
                else:
                    # Handle single tick case (flat line) to avoid plotext error (division by zero)
                    val = ticks[0]
                    delta = abs(val) * 0.05 if val != 0 else 1.0
                    plt.ylim(val - delta, val + delta)

            # Finally, apply our calculated ticks and labels.
            plt.yticks(ticks, labels)
        elif y_min is not None:
            # Fallback for single data point or flat line, just show the single price.
            plt.yticks([y_min], [f"{y_min:,.2f}"])

    def _set_date_ticks(self):
        """
        Sets the x-axis (date) ticks and labels dynamically based on the data period.
        It aims to provide readable date labels for different timeframes.
        """
        plt = self.plt

        start_date = self._data.index.min()
        end_date = self._data.index.max()

        freq = None  # Frequency for pandas date_range
        label_format = ""  # Format string for datetime objects
        ticks = []  # List to store calculated tick datetime objects

        # Determine appropriate frequency and label format based on the data period
        if self._period in ["5y", "max"]:
            freq = "YS"  # Year Start frequency
            label_format = "%Y"  # Year (e.g., 2020)
        elif self._period in ["6mo", "ytd", "1y"]:
            freq = "MS"  # Month Start frequency
            label_format = "%b %Y"  # Abbreviated month and year (e.g., Jan 2023)
        elif self._period == "1mo":
            freq = "W-MON"  # Weekly on Monday frequency
            label_format = "%b %d"  # Abbreviated month and day (e.g., Jan 01)
        elif self._period == "5d":
            freq = "D"  # Daily frequency
            label_format = "%a %d"  # Abbreviated weekday and day (e.g., Mon 01)
        elif self._period == "1d":
            # Special handling for 1-day period to show hourly ticks
            hour_ticks = []
            current_date = start_date.floor("h")  # Start from the beginning of the hour
            if current_date < start_date:
                current_date += pd.Timedelta(
                    hours=1
                )  # Adjust if start_date is mid-hour

            while current_date <= end_date:
                hour_ticks.append(current_date)
                current_date += pd.Timedelta(
                    hours=2
                )  # Use 2-hour increments for clarity

            # Downsample ticks if too many for display
            if len(hour_ticks) > 8:
                step = round(len(hour_ticks) / 6) or 1
                ticks = hour_ticks[::step]
            else:
                ticks = hour_ticks

            label_format = "%H:%M"  # Hour and minute (e.g., 10:30)

        # Generate ticks using pandas date_range if a frequency is defined
        if freq:
            ticks = pd.date_range(start=start_date, end=end_date, freq=freq).to_list()

        if ticks:
            # Filter out ticks outside the actual data range just in case
            ticks = [t for t in ticks if start_date <= t <= end_date]
            labels = [d.strftime(label_format) for d in ticks]

            # Convert datetime tick objects to numerical timestamps for positioning on plotext
            tick_positions = [t.timestamp() for t in ticks]
            plt.xticks(tick_positions, labels)
