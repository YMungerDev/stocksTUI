import functools
import json
import logging
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

from stockstui.utils import merge_price_data

# Only load data into memory if it's less than a day old.
CACHE_LOAD_DURATION_SECONDS = 86400  # 24 hours

# Prune any data from the database file that is older than 7 days.
# This keeps the database file size manageable over time.
CACHE_PRUNE_EXPIRY_SECONDS = 604800  # 7 days

# Research/statement payloads refresh at most daily; they change with
# filings and analyst updates.
_FUNDAMENTALS_TTL_SECONDS = 86400  # 24 hours

# Ticker info (exchange, name) changes very rarely. Cache it for a long time.
INFO_CACHE_EXPIRY_SECONDS = 86400 * 30  # 30 days


def _serialized(method):
    """
    Runs a write method under the instance's write lock.

    The single sqlite3 connection is shared with background worker threads
    (check_same_thread=False), so two transactions on it must not interleave.
    """

    @functools.wraps(method)
    def wrapper(self, *args, **kwargs):
        with self._write_lock:
            return method(self, *args, **kwargs)

    return wrapper


class DbManager:
    """
    Manages the persistent SQLite database for caching application data, primarily
    stock prices and ticker metadata, to enable faster startups and reduce API calls.
    """

    def __init__(self, db_path: Path):
        """
        Initializes the DbManager and establishes a connection to the database.

        Args:
            db_path: The file path for the SQLite database.
        """
        self.db_path = db_path
        self.conn = None
        self._write_lock = threading.RLock()
        try:
            self.db_path.parent.mkdir(parents=True, exist_ok=True)
            # Mypy has trouble with some sqlite3.connect overloads
            self.conn = sqlite3.connect(
                str(self.db_path), isolation_level="", check_same_thread=False
            )  # type: ignore

            # WAL (Write-Ahead Logging) mode allows concurrent readers while a writer
            # is active, eliminating "database is locked" errors from background worker
            # threads using check_same_thread=False. WAL is persistent on the file, so
            # setting it on each connect is harmless and makes the intent explicit.
            # busy_timeout tells SQLite to retry for up to 5 seconds before raising on
            # any residual writer-vs-writer lock contention (e.g. if the OS is slow to
            # release locks), instead of failing immediately.
            self.conn.execute("PRAGMA journal_mode=WAL")
            self.conn.execute("PRAGMA busy_timeout=5000")

            self._create_tables()
            self._prune_expired_entries()
        except sqlite3.Error as e:
            logging.error(f"Database connection failed for '{self.db_path}': {e}")


    def _create_tables(self):
        """Creates the necessary tables in the database if they don't already exist."""
        if not self.conn:
            return
        try:
            cursor = self.conn.cursor()
            cursor.execute("BEGIN TRANSACTION")
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS price_cache (
                    ticker TEXT PRIMARY KEY,
                    data TEXT NOT NULL,
                    timestamp REAL NOT NULL
                )
            """)
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS ticker_info (
                    ticker TEXT PRIMARY KEY,
                    exchange TEXT,
                    short_name TEXT,
                    long_name TEXT,
                    currency TEXT,
                    timestamp REAL NOT NULL
                )
            """)
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS option_positions (
                    symbol TEXT PRIMARY KEY,
                    ticker TEXT NOT NULL,
                    quantity REAL NOT NULL,
                    avg_cost REAL,
                    timestamp REAL NOT NULL
                )
            """)
            # Daily OHLCV bars are deliberately never pruned: unlike the price
            # cache they are historical facts, and the offline history path in
            # market_provider depends on them staying available.
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS daily_bars (
                    ticker TEXT NOT NULL,
                    date TEXT NOT NULL,
                    open REAL,
                    high REAL,
                    low REAL,
                    close REAL,
                    volume INTEGER,
                    adjclose REAL,
                    PRIMARY KEY (ticker, date)
                )
            """)
            # Alert rules persist across restarts; 'last_state' holds the
            # re-arm bookkeeping (see alerts.py) so a rule that has fired
            # stays quiet until the condition resets.
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS alert_rules (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    ticker TEXT NOT NULL,
                    rule_type TEXT NOT NULL,
                    threshold REAL NOT NULL,
                    last_state TEXT,
                    created_at REAL NOT NULL
                )
            """)
            # Cached fundamental statements per ticker (JSON payload with a
            # fetch timestamp); refreshed at most daily.
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS fundamentals_cache (
                    ticker TEXT PRIMARY KEY,
                    payload TEXT NOT NULL,
                    timestamp REAL NOT NULL
                )
            """)
            # Migration guard to add currency column if the table existed but without it.
            try:
                cursor.execute("ALTER TABLE ticker_info ADD COLUMN currency TEXT")
            except sqlite3.OperationalError:
                # Column already exists or table does not exist
                pass
            self.conn.commit()
        except sqlite3.Error as e:
            logging.error(f"Failed to create database tables: {e}")
            self.conn.rollback()

    def _prune_expired_entries(self):
        """Removes entries from the persistent cache that are older than their prune expiry date."""
        if not self.conn:
            return
        try:
            cursor = self.conn.cursor()
            cursor.execute("BEGIN TRANSACTION")

            price_prune_ts = (
                datetime.now(timezone.utc).timestamp() - CACHE_PRUNE_EXPIRY_SECONDS
            )
            cursor.execute(
                "DELETE FROM price_cache WHERE timestamp < ?", (price_prune_ts,)
            )
            if cursor.rowcount > 0:
                logging.info(
                    f"Pruned {cursor.rowcount} expired entries from the price cache."
                )

            info_prune_ts = (
                datetime.now(timezone.utc).timestamp() - INFO_CACHE_EXPIRY_SECONDS
            )
            cursor.execute(
                "DELETE FROM ticker_info WHERE timestamp < ?", (info_prune_ts,)
            )
            if cursor.rowcount > 0:
                logging.info(
                    f"Pruned {cursor.rowcount} expired entries from the info cache."
                )

            self.conn.commit()
        except sqlite3.Error as e:
            logging.error(f"Failed to prune expired cache entries: {e}")
            self.conn.rollback()

    def load_price_cache_from_db(self) -> dict:
        """Loads the price cache from the database, filtering for entries fresh enough to use."""
        if not self.conn:
            return {}
        loaded_data = {}
        try:
            cursor = self.conn.cursor()
            load_after_ts = (
                datetime.now(timezone.utc).timestamp() - CACHE_LOAD_DURATION_SECONDS
            )
            cursor.execute(
                "SELECT ticker, data, timestamp FROM price_cache WHERE timestamp >= ?",
                (load_after_ts,),
            )
            rows = cursor.fetchall()
            for ticker, data_json, timestamp_float in rows:
                try:
                    # FIX: Load data into the standardized dictionary format, not a tuple.
                    # The expiry is calculated from the stored timestamp.
                    expiry_dt = datetime.fromtimestamp(timestamp_float, tz=timezone.utc)
                    data_dict = json.loads(data_json)
                    loaded_data[ticker] = {"expiry": expiry_dt, "data": data_dict}
                except (json.JSONDecodeError, ValueError, TypeError, OSError):
                    logging.warning(
                        f"Failed to decode or parse price data for '{ticker}' from DB."
                    )
        except sqlite3.Error as e:
            logging.error(f"Failed to load price cache from database: {e}")
        logging.info(
            f"Loaded {len(loaded_data)} fresh items from the persistent price cache."
        )
        return loaded_data

    def load_info_cache_from_db(self) -> dict:
        """Loads the ticker info cache (exchange, name, currency) from the database."""
        if not self.conn:
            return {}
        loaded_data = {}
        try:
            cursor = self.conn.cursor()
            # Only load items that are still fresh (within 24 hours) to match price cache freshness
            load_after_ts = (
                datetime.now(timezone.utc).timestamp() - CACHE_LOAD_DURATION_SECONDS
            )
            cursor.execute(
                "SELECT ticker, exchange, short_name, long_name, currency FROM ticker_info WHERE timestamp >= ?",
                (load_after_ts,),
            )
            rows = cursor.fetchall()
            for ticker, exchange, short_name, long_name, currency in rows:
                loaded_data[ticker] = {
                    "exchange": exchange,
                    "shortName": short_name,
                    "longName": long_name,
                    "currency": currency,
                }
        except sqlite3.Error as e:
            logging.error(f"Failed to load info cache from database: {e}")
        logging.info(f"Loaded {len(loaded_data)} items from the persistent info cache.")
        return loaded_data

    def load_daily_bars(self, ticker: str, start_date: str | None = None):
        """
        Loads stored daily bars for a ticker as an OHLCV DataFrame.

        The index is a tz-aware UTC DatetimeIndex of midnight timestamps; the
        currency from ticker_info is attached as ``attrs["currency"]`` and the
        stored adjusted closes (may contain None) as ``attrs["adjclose"]``.
        Returns an empty DataFrame when the store is unavailable or has no bars
        in range, so callers fall back to the network.
        """
        if not self.conn:
            return pd.DataFrame()
        try:
            cursor = self.conn.cursor()
            if start_date:
                cursor.execute(
                    "SELECT date, open, high, low, close, volume, adjclose "
                    "FROM daily_bars WHERE ticker = ? AND date >= ? ORDER BY date",
                    (ticker.upper(), start_date),
                )
            else:
                cursor.execute(
                    "SELECT date, open, high, low, close, volume, adjclose "
                    "FROM daily_bars WHERE ticker = ? ORDER BY date",
                    (ticker.upper(),),
                )
            rows = cursor.fetchall()
            currency_row = None
            if rows:
                cursor.execute(
                    "SELECT currency FROM ticker_info WHERE ticker = ?",
                    (ticker.upper(),),
                )
                currency_row = cursor.fetchone()
        except sqlite3.Error as e:
            logging.error(f"Failed to load daily bars for {ticker}: {e}")
            return pd.DataFrame()
        if not rows:
            return pd.DataFrame()

        index = pd.to_datetime([r[0] for r in rows]).tz_localize("UTC")
        frame = pd.DataFrame(
            [[r[1], r[2], r[3], r[4], r[5]] for r in rows],
            index=index,
            columns=["Open", "High", "Low", "Close", "Volume"],
        )
        frame.attrs["adjclose"] = [r[6] for r in rows]
        frame.attrs["currency"] = (
            currency_row[0] if currency_row and currency_row[0] else "USD"
        )
        return frame

    @_serialized
    def save_daily_bars(self, ticker: str, bars) -> None:
        """
        Upserts daily bars for one ticker.

        ``bars`` is a DataFrame with a DatetimeIndex of daily (midnight)
        timestamps and Open/High/Low/Close/Volume columns; an optional
        'Adj Close' column is stored alongside so later launches can detect
        split/dividend adjustments.
        """
        if not self.conn or bars is None:
            return
        try:
            frame = bars
            adjclose = None
            if "Adj Close" in frame.columns:
                adjclose = frame["Adj Close"].tolist()
                frame = frame.drop(columns=["Adj Close"])
            if frame.empty or not isinstance(frame.index, pd.DatetimeIndex):
                return
            # The trading date is the timestamp's own calendar date: yfinance
            # daily bars sit at midnight in the exchange timezone, so no UTC
            # conversion here (it would shift Asia/Pacific sessions a day back).
            dates = [d.date().isoformat() for d in frame.index]

            def _num(value):
                # NaN != NaN, so this maps pandas NaN and None both to NULL.
                if value is None or value != value:
                    return None
                return float(value)

            rows = []
            for pos, date_str in enumerate(dates):
                o, h, l, c, v = (
                    frame["Open"].iloc[pos],
                    frame["High"].iloc[pos],
                    frame["Low"].iloc[pos],
                    frame["Close"].iloc[pos],
                    frame["Volume"].iloc[pos],
                )
                volume = None if v is None or v != v else int(v)
                rows.append(
                    (
                        ticker.upper(),
                        date_str,
                        _num(o),
                        _num(h),
                        _num(l),
                        _num(c),
                        volume,
                        _num(adjclose[pos]) if adjclose else None,
                    )
                )
            cursor = self.conn.cursor()
            cursor.execute("BEGIN TRANSACTION")
            cursor.executemany(
                "INSERT OR REPLACE INTO daily_bars "
                "(ticker, date, open, high, low, close, volume, adjclose) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                rows,
            )
            self.conn.commit()
            logging.info(f"Stored {len(rows)} daily bars for {ticker.upper()}.")
        except sqlite3.Error as e:
            logging.error(f"Failed to save daily bars for {ticker}: {e}")
            if self.conn:
                self.conn.rollback()

    @_serialized
    def delete_daily_bars(self, ticker: str, start_date: str | None = None) -> None:
        """Deletes stored bars for a ticker, optionally only from start_date on."""
        if not self.conn:
            return
        try:
            cursor = self.conn.cursor()
            cursor.execute("BEGIN TRANSACTION")
            if start_date:
                cursor.execute(
                    "DELETE FROM daily_bars WHERE ticker = ? AND date >= ?",
                    (ticker.upper(), start_date),
                )
            else:
                cursor.execute(
                    "DELETE FROM daily_bars WHERE ticker = ?", (ticker.upper(),)
                )
            self.conn.commit()
        except sqlite3.Error as e:
            logging.error(f"Failed to delete daily bars for {ticker}: {e}")
            if self.conn:
                self.conn.rollback()

    def get_earliest_bar_date(self, ticker: str) -> str | None:
        """Returns the oldest stored bar date (ISO) for a ticker, if any."""
        if not self.conn:
            return None
        try:
            cursor = self.conn.cursor()
            cursor.execute(
                "SELECT MIN(date) FROM daily_bars WHERE ticker = ?",
                (ticker.upper(),),
            )
            row = cursor.fetchone()
            return row[0] if row and row[0] else None
        except sqlite3.Error as e:
            logging.error(f"Failed to read earliest bar date for {ticker}: {e}")
            return None

    def get_latest_bar_date(self, ticker: str) -> str | None:
        """Returns the newest stored bar date (ISO) for a ticker, if any."""
        if not self.conn:
            return None
        try:
            cursor = self.conn.cursor()
            cursor.execute(
                "SELECT MAX(date) FROM daily_bars WHERE ticker = ?",
                (ticker.upper(),),
            )
            row = cursor.fetchone()
            return row[0] if row and row[0] else None
        except sqlite3.Error as e:
            logging.error(f"Failed to read latest bar date for {ticker}: {e}")
            return None

    @_serialized
    def save_alert_rule(self, ticker: str, rule_type: str, threshold: float) -> int | None:
        """Inserts one alert rule and returns its row id."""
        if not self.conn:
            return None
        try:
            cursor = self.conn.cursor()
            cursor.execute("BEGIN TRANSACTION")
            cursor.execute(
                "INSERT INTO alert_rules (ticker, rule_type, threshold, last_state, created_at) "
                "VALUES (?, ?, ?, NULL, ?)",
                (ticker.upper(), rule_type, float(threshold), datetime.now(timezone.utc).timestamp()),
            )
            self.conn.commit()
            return cursor.lastrowid
        except sqlite3.Error as e:
            logging.error(f"Failed to save alert rule: {e}")
            if self.conn:
                self.conn.rollback()
            return None

    @_serialized
    def delete_alert_rule(self, rule_id: int) -> None:
        """Removes one alert rule by id."""
        if not self.conn:
            return
        try:
            cursor = self.conn.cursor()
            cursor.execute("BEGIN TRANSACTION")
            cursor.execute("DELETE FROM alert_rules WHERE id = ?", (rule_id,))
            self.conn.commit()
        except sqlite3.Error as e:
            logging.error(f"Failed to delete alert rule {rule_id}: {e}")
            if self.conn:
                self.conn.rollback()

    def load_alert_rules(self) -> list[dict]:
        """Loads every alert rule, ordered by ticker then type."""
        if not self.conn:
            return []
        try:
            cursor = self.conn.cursor()
            cursor.execute(
                "SELECT id, ticker, rule_type, threshold, last_state FROM alert_rules "
                "ORDER BY ticker, rule_type, id"
            )
            return [
                {"id": r[0], "ticker": r[1], "rule_type": r[2], "threshold": r[3], "last_state": r[4]}
                for r in cursor.fetchall()
            ]
        except sqlite3.Error as e:
            logging.error(f"Failed to load alert rules: {e}")
            return []

    @_serialized
    def update_alert_rule_state(self, rule_id: int, last_state: str | None) -> None:
        """Persists a rule's re-arm state (fired marker or reset)."""
        if not self.conn:
            return
        try:
            cursor = self.conn.cursor()
            cursor.execute("BEGIN TRANSACTION")
            cursor.execute(
                "UPDATE alert_rules SET last_state = ? WHERE id = ?", (last_state, rule_id)
            )
            self.conn.commit()
        except sqlite3.Error as e:
            logging.error(f"Failed to update alert rule state {rule_id}: {e}")
            if self.conn:
                self.conn.rollback()

    @_serialized
    def save_fundamentals_cache(self, ticker: str, payload_json: str) -> None:
        """Stores one ticker's fundamentals payload (JSON) with a timestamp."""
        if not self.conn:
            return
        try:
            cursor = self.conn.cursor()
            cursor.execute("BEGIN TRANSACTION")
            cursor.execute(
                "INSERT OR REPLACE INTO fundamentals_cache (ticker, payload, timestamp) "
                "VALUES (?, ?, ?)",
                (
                    ticker.upper(),
                    payload_json,
                    datetime.now(timezone.utc).timestamp(),
                ),
            )
            self.conn.commit()
        except sqlite3.Error as e:
            logging.error(f"Failed to save fundamentals cache for {ticker}: {e}")
            if self.conn:
                self.conn.rollback()

    def load_fundamentals_cache(self, ticker: str) -> str | None:
        """
        Returns the cached fundamentals JSON if fresh (under a day old),
        else None.
        """
        if not self.conn:
            return None
        try:
            cursor = self.conn.cursor()
            cursor.execute(
                "SELECT payload, timestamp FROM fundamentals_cache WHERE ticker = ?",
                (ticker.upper(),),
            )
            row = cursor.fetchone()
            if not row:
                return None
            payload, timestamp = row
            age = datetime.now(timezone.utc).timestamp() - timestamp
            if age > _FUNDAMENTALS_TTL_SECONDS:
                return None
            return payload
        except sqlite3.Error as e:
            logging.error(f"Failed to load fundamentals cache for {ticker}: {e}")
            return None

    @_serialized
    def save_price_cache_to_db(self, cache_data: dict):
        """
        Saves the in-memory price cache to the database, merging field-level updates
        to preserve valid data and prevent overwriting valid cached values with None.
        """
        if not self.conn or not cache_data:
            return

        try:
            cursor = self.conn.cursor()
            cursor.execute("BEGIN TRANSACTION")

            for ticker, cache_entry in cache_data.items():
                try:
                    data_dict = cache_entry.get("data", {})
                    expiry_dt = cache_entry.get("expiry")
                    if not data_dict or not expiry_dt:
                        continue

                    # Fetch the existing record from the database to perform a field-level merge.
                    cursor.execute("SELECT data FROM price_cache WHERE ticker = ?", (ticker,))
                    row = cursor.fetchone()

                    if row:
                        try:
                            existing_dict = json.loads(row[0])
                            # Merge existing persistent data with new memory update
                            merged_dict = merge_price_data(existing_dict, data_dict)
                        except (json.JSONDecodeError, ValueError, TypeError):
                            merged_dict = data_dict
                    else:
                        merged_dict = data_dict

                    cursor.execute(
                        "INSERT OR REPLACE INTO price_cache (ticker, data, timestamp) VALUES (?, ?, ?)",
                        (ticker, json.dumps(merged_dict), expiry_dt.timestamp()),
                    )
                except Exception as e:
                    logging.warning(
                        f"Could not save price data for '{ticker}' to database: {e}"
                    )

            self.conn.commit()
            logging.info(
                f"Saved/Updated {len(cache_data)} items in the persistent price cache with field-level merging."
            )
        except sqlite3.Error as e:
            logging.error(f"Failed to save price cache to database: {e}")
            if self.conn:
                self.conn.rollback()

    @_serialized
    def save_info_cache_to_db(self, cache_data: dict):
        """Saves the in-memory info cache to the database."""
        if not self.conn or not cache_data:
            return
        items_to_save = []
        now_ts = datetime.now(timezone.utc).timestamp()
        for ticker, data in cache_data.items():
            items_to_save.append(
                (
                    ticker,
                    data.get("exchange"),
                    data.get("shortName"),
                    data.get("longName"),
                    data.get("currency"),
                    now_ts,
                )
            )
        if not items_to_save:
            return
        try:
            cursor = self.conn.cursor()
            cursor.execute("BEGIN TRANSACTION")
            cursor.executemany(
                "INSERT OR REPLACE INTO ticker_info (ticker, exchange, short_name, long_name, currency, timestamp) VALUES (?, ?, ?, ?, ?, ?)",
                items_to_save,
            )
            self.conn.commit()
            logging.info(
                f"Saved/Updated {len(items_to_save)} items in the persistent info cache."
            )
        except sqlite3.Error as e:
            logging.error(f"Failed to save info cache to database: {e}")
            self.conn.rollback()

    # --- Option Positions Methods ---

    @_serialized
    def save_option_position(
        self, symbol: str, ticker: str, quantity: float, avg_cost: float
    ):
        """Saves or updates an option position."""
        if not self.conn:
            return
        try:
            cursor = self.conn.cursor()
            cursor.execute("BEGIN TRANSACTION")
            timestamp = datetime.now(timezone.utc).timestamp()
            cursor.execute(
                """
                INSERT OR REPLACE INTO option_positions (symbol, ticker, quantity, avg_cost, timestamp)
                VALUES (?, ?, ?, ?, ?)
            """,
                (symbol, ticker, quantity, avg_cost, timestamp),
            )
            self.conn.commit()
            logging.info(f"Saved option position: {symbol}")
        except sqlite3.Error as e:
            logging.error(f"Failed to save option position {symbol}: {e}")
            if self.conn:
                self.conn.rollback()

    def get_option_position(self, symbol: str) -> dict | None:
        """Retrieves a specific option position."""
        if not self.conn:
            return None
        try:
            cursor = self.conn.cursor()
            cursor.execute(
                "SELECT symbol, ticker, quantity, avg_cost FROM option_positions WHERE symbol = ?",
                (symbol,),
            )
            row = cursor.fetchone()
            if row:
                return {
                    "symbol": row[0],
                    "ticker": row[1],
                    "quantity": row[2],
                    "avg_cost": row[3],
                }
            return None
        except sqlite3.Error as e:
            logging.error(f"Failed to get option position {symbol}: {e}")
            return None

    @_serialized
    def delete_option_position(self, symbol: str):
        """Deletes an option position."""
        if not self.conn:
            return
        try:
            cursor = self.conn.cursor()
            cursor.execute("BEGIN TRANSACTION")
            cursor.execute("DELETE FROM option_positions WHERE symbol = ?", (symbol,))
            self.conn.commit()
            logging.info(f"Deleted option position: {symbol}")
        except sqlite3.Error as e:
            logging.error(f"Failed to delete option position {symbol}: {e}")
            if self.conn:
                self.conn.rollback()

    def get_all_option_positions(self) -> dict:
        """Retrieves all option positions, keyed by symbol."""
        if not self.conn:
            return {}
        positions = {}
        try:
            cursor = self.conn.cursor()
            cursor.execute(
                "SELECT symbol, ticker, quantity, avg_cost FROM option_positions"
            )
            rows = cursor.fetchall()
            for row in rows:
                positions[row[0]] = {
                    "symbol": row[0],
                    "ticker": row[1],
                    "quantity": row[2],
                    "avg_cost": row[3],
                }
            return positions
        except sqlite3.Error as e:
            logging.error(f"Failed to get all option positions: {e}")
            return {}

    @_serialized
    def close(self):
        """Closes the database connection if it's open."""
        if self.conn:
            self.conn.close()
            # Later writes from a still-running worker become no-ops.
            self.conn = None
            logging.info("Database connection closed.")
