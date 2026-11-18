import argparse
import contextlib
import shutil
import subprocess
from pathlib import Path
from typing import Any

from rich.console import Console
from rich.table import Table
from rich.text import Text

from stockstui.config_manager import ConfigManager
from stockstui.data_providers import fred_provider, market_provider
from stockstui.presentation import formatter, screening
from stockstui.presentation import metrics as metrics_module
from stockstui.presentation.metrics import compute_metrics_for_ticker
from stockstui.utils import parse_tags


def show_manual() -> None:
    """Displays the help file content using a pager like 'less' if available."""
    help_path = Path(__file__).resolve().parent / "documents" / "help.txt"
    try:
        pager = shutil.which("less")
        if pager:
            subprocess.run([pager, str(help_path)], check=False)
        else:
            with open(help_path, "r") as f:
                print(f.read())
    except FileNotFoundError:
        print(f"Error: Help file not found at {help_path}")
    except Exception as e:  # noqa: BLE001
        print(f"An unexpected error occurred while trying to show help: {e}")


def _attach_cli_metrics(quotes: list[dict], bar_store) -> None:
    """Adds computed metrics as raw numbers for json/csv output."""
    for quote in quotes:
        ticker = quote.get("symbol")
        if not ticker:
            continue
        values = compute_metrics_for_ticker(bar_store, ticker)
        for key, value in values.items():
            if key == "Sparkline":
                continue
            quote[key] = None if value == "" else value


def run_cli_output(args: argparse.Namespace) -> None:
    """
    Handles fetching and displaying stock data directly to the terminal, bypassing the TUI.
    """
    console = Console()
    app_root = Path(__file__).resolve().parent
    config = ConfigManager(app_root)

    # Load header color from theme
    theme_name = config.settings.get("theme", "gruvbox_soft_dark")
    theme_data = config.themes.get(theme_name, {})
    header_color = theme_data.get("palette", {}).get("magenta", "magenta")
    header_style = f"bold {header_color}"

    from stockstui.database.db_manager import DbManager

    bar_store = DbManager(config.db_path)
    try:
        _run_cli_output_inner(args, config, bar_store, console, header_style)
    finally:
        bar_store.close()


def _run_cli_output_inner(args, config, bar_store, console, header_style) -> None:

    if session_lists := args.session_list:
        for name, tickers in session_lists.items():
            config.lists[name] = [
                {"ticker": ticker, "alias": ticker, "note": ""} for ticker in tickers
            ]

    target_names = []
    if args.output != "all":
        target_names.extend([name.strip().lower() for name in args.output.split(",")])
    if args.session_list:
        target_names.extend(args.session_list.keys())

    # Load hidden tabs to filter content if 'all' is requested
    hidden_tabs = config.get_setting("hidden_tabs", [])

    # Logic to determine if FRED should be shown
    fred_requested = False
    if "fred" in target_names:
        fred_requested = True
    elif args.output == "all":
        fred_requested = "fred" not in hidden_tabs

    lists_to_iterate: list[tuple[str, list[dict]]] = []
    if not target_names:
        # We need to filter these lists against hidden_tabs
        all_lists = list(config.lists.items())
        lists_to_iterate = [(n, lst) for n, lst in all_lists if n not in hidden_tabs]
    else:
        unique_target_names = list(set(target_names))
        lists_to_iterate = [
            (name, lst)
            for name, lst in config.lists.items()
            if name in unique_target_names
        ]

    if not lists_to_iterate and not fred_requested:
        console.print(
            "[bold red]Error:[/] No visible lists found matching your criteria."
        )
        return

    ordered_tickers = []
    seen_tickers = set()
    alias_map = {}

    # Process Tags
    requested_tags = set()
    if args.tags:
        # Use util function to parse requested tags too, ensuring consistency
        requested_tags = set(parse_tags(args.tags))

    for _, list_content in lists_to_iterate:
        for item in list_content:
            ticker = item.get("ticker")

            # Apply Tag Filtering
            if requested_tags:
                raw_tags = item.get("tags")
                if isinstance(raw_tags, str):
                    item_tags = set(parse_tags(raw_tags))
                elif isinstance(raw_tags, list):
                    item_tags = {str(t).lower() for t in raw_tags}
                else:
                    item_tags = set()

                # If disjoint (no intersection), then item does NOT have any of the requested tags.
                # So if disjoint, skip.
                if requested_tags.isdisjoint(item_tags):
                    continue

            if ticker and ticker not in seen_tickers:
                ordered_tickers.append(ticker)
                seen_tickers.add(ticker)
                # Map missing or empty aliases to empty string so description falls back to provider description
                alias_map[ticker] = item.get("alias") or ""

    if not ordered_tickers and not fred_requested:
        console.print(
            "[yellow]No tickers found for the specified lists and tags.[/yellow]"
        )
        return

    had_ticker_output = False
    if ordered_tickers:
        output_format = getattr(args, "format", "table") or "table"
        # The spinner writes cursor codes and frames to stdout on a terminal,
        # which would corrupt machine-readable output, so json/csv skip it.
        status = (
            contextlib.nullcontext()
            if output_format in ("json", "csv")
            else console.status("[bold green]Fetching data...[/]")
        )
        with status:
            try:
                enable_pre_post = config.get_setting("enable_pre_post_market", False)
                data = market_provider.get_market_price_data(
                    ordered_tickers, enable_pre_post_market=enable_pre_post
                )
                # Apply the screening expression (--filter) before mapping.
                filter_expr = getattr(args, "filter", None)
                if filter_expr:
                    try:
                        data = screening.apply_filter(data, filter_expr)
                    except screening.FilterError as e:
                        console.print(f"[bold red]Filter error:[/] {e}")
                        return
                    if not isinstance(filter_expr, str) or filter_expr.strip():
                        matched_symbols = {
                            item["symbol"] for item in data if "symbol" in item
                        }
                        ordered_tickers = [
                            t for t in ordered_tickers if t in matched_symbols
                        ]
                # Map data by symbol for easier lookup
                data_map = {item["symbol"]: item for item in data if "symbol" in item}
                if output_format in ("json", "csv"):
                    quotes = [data_map[t] for t in ordered_tickers if t in data_map]
                    _attach_cli_metrics(quotes, bar_store)
                    if output_format == "json":
                        console.print(
                            screening.to_json(quotes),
                            soft_wrap=True,
                            crop=False,
                            highlight=False,
                            markup=False,
                        )
                    else:
                        console.print(
                            screening.to_csv(quotes).rstrip("\n"),
                            soft_wrap=True,
                            crop=False,
                            highlight=False,
                            markup=False,
                        )
                    return
            except Exception as e:  # noqa: BLE001
                console.print(f"[bold red]Failed to fetch data from API:[/] {e}")
                return

        had_ticker_output = True
        if not ordered_tickers:
            console.print(
                "[yellow]No tickers matched the filter expression.[/yellow]"
            )
        else:
            rows: list[tuple[Any, ...]] = []
            for ticker in ordered_tickers:
                ticker_data: dict[str, Any] | None = data_map.get(ticker)
                if not ticker_data or ticker_data.get("description") in (
                    "Invalid Ticker",
                    "Data Unavailable",
                ):
                    rows.append(
                        (
                            ticker_data.get("description", "Invalid Ticker")
                            if ticker_data
                            else "Invalid Ticker",
                            None,
                            None,
                            None,
                            "N/A",
                            "N/A",
                            ticker,
                            "$",
                            {},
                        )
                    )
                    continue

                price = ticker_data.get("price")
                previous_close = ticker_data.get("previous_close")

                change, change_percent = None, None
                if price is not None and previous_close is not None and previous_close != 0:
                    change = price - previous_close
                    change_percent = change / previous_close

                # Resolve currency symbol using the fetched ticker data, not the last item in the list iteration
                currency_code = ticker_data.get("currency", "USD")
                sym = formatter.get_currency_symbol(currency_code)

                day_low = ticker_data.get("day_low")
                day_high = ticker_data.get("day_high")
                day_range = (
                    f"{sym}{day_low:,.2f} - {sym}{day_high:,.2f}"
                    if day_low is not None and day_high is not None
                    else "N/A"
                )

                fifty_two_week_low = ticker_data.get("fifty_two_week_low")
                fifty_two_week_high = ticker_data.get("fifty_two_week_high")
                wk_range = (
                    f"{sym}{fifty_two_week_low:,.2f} - {sym}{fifty_two_week_high:,.2f}"
                    if fifty_two_week_low is not None and fifty_two_week_high is not None
                    else "N/A"
                )
                description = alias_map.get(ticker) or ticker_data.get(
                    "description", ticker
                )
                metrics_row = compute_metrics_for_ticker(bar_store, ticker)
                rows.append(
                    (
                        description,
                        price,
                        change,
                        change_percent,
                        day_range,
                        wk_range,
                        ticker,
                        sym,
                        metrics_row,
                    )
                )

            table = Table(
                title="Ticker Overview", show_header=True, header_style=header_style
            )
            table.add_column("Description", style="dim", no_wrap=False)  # Allow wrapping
            table.add_column("Price", justify="right")
            table.add_column("Change", justify="right")
            table.add_column("% Change", justify="right")
            table.add_column("Day's Range", justify="right")
            table.add_column("52-Wk Range", justify="right")
            table.add_column("Ticker", style="dim")
            metric_columns = [
                key
                for key in metrics_module.METRIC_COLUMNS
                if key != "Sparkline"
            ]
            for metric_key in metric_columns:
                table.add_column(metric_key, justify="right")

            for desc, price, change, pct, day_r, wk_r, symbol, sym, metrics_row in rows:
                price_text = f"{sym}{price:,.2f}" if price is not None else "N/A"
                style, change_text, pct_text = ("dim", "N/A", "N/A")
                if change is not None and pct is not None:
                    if change > 0:
                        style, change_text, pct_text = (
                            "green",
                            f"{change:,.2f}",
                            f"+{pct:.2%}",
                        )
                    elif change < 0:
                        style, change_text, pct_text = "red", f"{change:,.2f}", f"{pct:.2%}"
                    else:
                        style, change_text, pct_text = "", "0.00", "0.00%"
                table.add_row(
                    desc,
                    Text(price_text, style="cyan"),
                    Text(change_text, style=style),
                    Text(pct_text, style=style),
                    day_r,
                    wk_r,
                    symbol,
                    *[metrics_row.get(key, "") for key in metric_columns],
                )

            console.print(table)

    # --- FRED Data Section ---
    if fred_requested:
        if had_ticker_output:
            console.print()  # Spacer
        fred_settings = config.settings.get("fred_settings", {})
        api_key = fred_settings.get("api_key")

        if not api_key:
            console.print(
                "[bold red]Error:[/] FRED API key is missing. Configure it in the TUI or config file."
            )
        else:
            series_list = fred_settings.get("series_list", [])
            # Load aliases and cached descriptions
            aliases = fred_settings.get("series_aliases", {})
            cached_desc = fred_settings.get("series_descriptions", {})

            if series_list:
                with console.status("[bold green]Fetching FRED data...[/]"):
                    fred_data = []
                    for sid in series_list:
                        s = fred_provider.get_series_summary(sid, api_key)
                        fred_data.append(s)

                # Use same header style as Ticker table for consistency
                fred_table = Table(
                    title="Economic Data (FRED)",
                    show_header=True,
                    header_style=header_style,
                )
                fred_table.add_column(
                    "Description", style="dim", no_wrap=False
                )  # Allow wrapping
                fred_table.add_column("Current", justify="right")
                fred_table.add_column("YoY %", justify="right")
                fred_table.add_column("Z-10Y", justify="right")
                fred_table.add_column(
                    "% Rng", justify="right", width=5
                )  # Tight width, renamed header
                fred_table.add_column("Date", justify="center")
                fred_table.add_column("Series ID", style="dim")

                for item in fred_data:
                    sid = item.get("id")
                    current = item.get("current")
                    current_str = (
                        f"{current:,.2f}"
                        if isinstance(current, (int, float))
                        else "N/A"
                    )

                    yoy = item.get("yoy_pct")
                    yoy_str = f"{yoy:+.1f}%" if yoy is not None else "N/A"
                    yoy_style = (
                        "green"
                        if yoy and yoy > 0
                        else ("red" if yoy and yoy < 0 else "")
                    )

                    z = item.get("z_10y")
                    z_str = f"{z:+.2f}" if z is not None else "N/A"
                    z_style = (
                        "bold red"
                        if z and abs(z) > 2
                        else ("yellow" if z and abs(z) > 1 else "")
                    )

                    rng = item.get("pct_of_range")
                    rng_str = f"{rng:.0f}%" if rng is not None else "N/A"

                    # Determine description: Alias > Cache > Title from API > ID
                    description = (
                        aliases.get(sid)
                        or cached_desc.get(sid)
                        or item.get("title")
                        or sid
                    )

                    fred_table.add_row(
                        description,
                        Text(current_str, style="cyan"),  # Match Ticker price color
                        Text(yoy_str, style=yoy_style),
                        Text(z_str, style=z_style),
                        rng_str,
                        item.get("date", "N/A"),
                        sid,
                    )

                console.print(fred_table)
