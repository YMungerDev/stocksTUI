"""Tests for EDGAR XBRL statements, valuation history and earnings quality."""

import json
import unittest
from datetime import date
from pathlib import Path
from unittest.mock import MagicMock, patch

import pandas as pd

from stockstui.data_providers import edgar_fundamentals as ef
from stockstui.data_providers import edgar_provider, fundamentals_provider
from stockstui.presentation import quality, valuation

FIXTURE = Path(__file__).parent / "fixtures" / "aapl_companyfacts_trimmed.json"


def _fact(start, end, val, filed, form="10-Q", accn=None):
    row = {"end": end, "val": val, "form": form, "filed": filed, "accn": accn or f"A-{filed}"}
    if start:
        row["start"] = start
    return row


def _facts(concepts: dict[str, list[dict]], unit="USD", extra_units=None) -> dict:
    """companyfacts-shaped dict from {"us-gaap:Concept": [facts]}."""
    out = {"cik": 1, "entityName": "Test Co", "facts": {}}
    for concept, rows in concepts.items():
        tax, name = concept.split(":", 1)
        units = {unit: rows}
        if extra_units and concept in extra_units:
            units.update(extra_units[concept])
        out["facts"].setdefault(tax, {})[name] = {"units": units}
    return out


class TestPeriodSelection(unittest.TestCase):
    def test_latest_filing_wins_and_changed_values_are_flagged_restated(self):
        rows = [
            _fact("2024-01-01", "2024-12-31", 100, "2025-02-01", form="10-K"),
            _fact("2024-01-01", "2024-12-31", 90, "2026-02-01", form="10-K"),
        ]
        chosen = ef._choose_per_period(rows, instant=False)
        cell = chosen[(date(2024, 1, 1), date(2024, 12, 31))]
        self.assertEqual(cell.val, 90)
        self.assertTrue(cell.restated)

    def test_repeated_identical_values_are_not_restated(self):
        rows = [
            _fact("2024-01-01", "2024-12-31", 100, "2025-02-01", form="10-K"),
            _fact("2024-01-01", "2024-12-31", 100, "2026-02-01", form="10-K"),
        ]
        cell = ef._choose_per_period(rows, instant=False)[(date(2024, 1, 1), date(2024, 12, 31))]
        self.assertFalse(cell.restated)

    def test_non_periodic_forms_are_ignored(self):
        rows = [_fact("2024-01-01", "2024-12-31", 100, "2025-02-01", form="8-K")]
        self.assertEqual(ef._choose_per_period(rows, instant=False), {})

    def test_q4_is_derived_from_fy_minus_nine_months(self):
        periods = ef._choose_per_period(
            [
                _fact("2024-01-01", "2024-03-31", 10, "2024-05-01"),
                _fact("2024-01-01", "2024-06-30", 22, "2024-08-01"),
                _fact("2024-01-01", "2024-09-30", 36, "2024-11-01"),
                _fact("2024-01-01", "2024-12-31", 50, "2025-02-01", form="10-K"),
            ],
            instant=False,
        )
        annual, quarterly = ef.split_durations(periods, derive_quarters=True)
        self.assertEqual(annual[date(2024, 12, 31)].val, 50)
        self.assertEqual(quarterly[date(2024, 3, 31)].val, 10)
        self.assertEqual(quarterly[date(2024, 6, 30)].val, 12)
        self.assertEqual(quarterly[date(2024, 9, 30)].val, 14)
        self.assertEqual(quarterly[date(2024, 12, 31)].val, 14)
        self.assertTrue(quarterly[date(2024, 12, 31)].derived)
        self.assertFalse(quarterly[date(2024, 3, 31)].derived)

    def test_a_directly_reported_quarter_is_not_overwritten(self):
        periods = ef._choose_per_period(
            [
                _fact("2024-01-01", "2024-06-30", 22, "2024-08-01"),
                _fact("2024-04-01", "2024-06-30", 11.5, "2024-08-01"),
                _fact("2024-01-01", "2024-03-31", 10, "2024-05-01"),
            ],
            instant=False,
        )
        _, quarterly = ef.split_durations(periods, derive_quarters=True)
        self.assertEqual(quarterly[date(2024, 6, 30)].val, 11.5)

    def test_non_additive_items_are_never_derived(self):
        periods = ef._choose_per_period(
            [
                _fact("2024-01-01", "2024-09-30", 3.0, "2024-11-01"),
                _fact("2024-01-01", "2024-12-31", 4.0, "2025-02-01", form="10-K"),
            ],
            instant=False,
        )
        _, quarterly = ef.split_durations(periods, derive_quarters=False)
        self.assertNotIn(date(2024, 12, 31), quarterly)


def _two_year_company(**overrides) -> dict:
    concepts = {
        # Concept switch: old years on Revenues, new on the ASC 606 concept.
        "us-gaap:Revenues": [_fact("2023-01-01", "2023-12-31", 900, "2024-02-01", form="10-K")],
        "us-gaap:RevenueFromContractWithCustomerExcludingAssessedTax": [
            _fact("2024-01-01", "2024-12-31", 1000, "2025-02-01", form="10-K"),
        ],
        "us-gaap:NetIncomeLoss": [
            _fact("2023-01-01", "2023-12-31", 90, "2024-02-01", form="10-K"),
            _fact("2024-01-01", "2024-12-31", 120, "2025-02-01", form="10-K"),
        ],
        "us-gaap:OperatingIncomeLoss": [_fact("2024-01-01", "2024-12-31", 150, "2025-02-01", form="10-K")],
        "us-gaap:DepreciationDepletionAndAmortization": [
            _fact("2024-01-01", "2024-12-31", 30, "2025-02-01", form="10-K")
        ],
        "us-gaap:NetCashProvidedByUsedInOperatingActivities": [
            _fact("2024-01-01", "2024-12-31", 140, "2025-02-01", form="10-K")
        ],
        "us-gaap:PaymentsToAcquirePropertyPlantAndEquipment": [
            _fact("2024-01-01", "2024-12-31", 40, "2025-02-01", form="10-K")
        ],
        "us-gaap:LongTermDebtNoncurrent": [_fact(None, "2024-12-31", 200, "2025-02-01", form="10-K")],
        "us-gaap:LongTermDebtCurrent": [_fact(None, "2024-12-31", 20, "2025-02-01", form="10-K")],
        "us-gaap:CommercialPaper": [_fact(None, "2024-12-31", 5, "2025-02-01", form="10-K")],
        "us-gaap:StockholdersEquity": [_fact(None, "2024-12-31", 500, "2025-02-01", form="10-K")],
    }
    concepts.update(overrides)
    return _facts(concepts)


class TestStatementsFromFacts(unittest.TestCase):
    def test_concepts_merge_per_period_in_priority_order(self):
        statements = ef.statements_from_facts(_two_year_company())
        income = statements["income"]
        self.assertEqual(income["columns"], ["2024-12-31", "2023-12-31"])
        self.assertEqual(income["rows"]["Total Revenue"], [1000, 900])

    def test_derived_rows(self):
        statements = ef.statements_from_facts(_two_year_company())
        self.assertEqual(statements["income"]["rows"]["EBITDA"][0], 180)
        self.assertEqual(statements["cash"]["rows"]["Capital Expenditure"][0], -40)
        self.assertEqual(statements["cash"]["rows"]["Free Cash Flow"][0], 100)
        balance = statements["balance"]["rows"]
        self.assertEqual(balance["Current Debt"][0], 25)
        self.assertEqual(balance["Total Debt"][0], 225)
        self.assertEqual(balance["Invested Capital"][0], 725)
        self.assertEqual(statements["meta"]["source"], "edgar")

    def test_formula_rows_are_not_marked_derived(self):
        statements = ef.statements_from_facts(_two_year_company())
        flags = statements["balance"]["provenance"]["Total Debt"][0][3]
        self.assertNotIn("D", flags)

    def test_values_render_in_millions(self):
        from stockstui.ui.views.fundamentals_view import format_statement_rows

        payload = {"columns": ["2024-12-31"], "rows": {"Total Revenue": [478_834_000_000], "Diluted EPS": [8.98]}}
        rows = format_statement_rows(
            payload, [("Total Revenue", ("Total Revenue",)), ("Diluted EPS", ("Diluted EPS",))], max_columns=1
        )
        self.assertEqual(rows[0][1].plain, "478,834")
        self.assertEqual(rows[1][1].plain, "8.98")

    def test_debt_current_total_wins_over_parts(self):
        statements = ef.statements_from_facts(
            _two_year_company(
                **{"us-gaap:DebtCurrent": [_fact(None, "2024-12-31", 30, "2025-02-01", form="10-K")]}
            )
        )
        self.assertEqual(statements["balance"]["rows"]["Current Debt"][0], 30)

    def test_reporting_currency_is_the_most_used(self):
        facts = _facts(
            {
                "ifrs-full:Revenue": [
                    _fact("2024-01-01", "2024-12-31", 36, "2025-02-01", form="20-F"),
                    _fact("2023-01-01", "2023-12-31", 34, "2024-02-01", form="20-F"),
                ],
                "ifrs-full:ProfitLoss": [_fact("2024-01-01", "2024-12-31", 7, "2025-02-01", form="20-F")],
            },
            unit="EUR",
            extra_units={
                "ifrs-full:Revenue": {
                    "USD": [_fact("2024-01-01", "2024-12-31", 40, "2025-02-01", form="20-F")]
                }
            },
        )
        statements = ef.statements_from_facts(facts)
        self.assertEqual(statements["meta"]["currency"], "EUR")
        self.assertEqual(statements["income"]["rows"]["Total Revenue"][0], 36)

    def test_no_income_statement_means_empty(self):
        facts = _facts({"us-gaap:Assets": [_fact(None, "2024-12-31", 1, "2025-02-01", form="10-K")]})
        self.assertEqual(ef.statements_from_facts(facts), {})


class TestRealAppleFacts(unittest.TestCase):
    """Trimmed real companyfacts for Apple (FY2023 onwards)."""

    @classmethod
    def setUpClass(cls):
        cls.statements = ef.statements_from_facts(json.loads(FIXTURE.read_text()))

    def test_fiscal_year_values_match_the_10k(self):
        income = self.statements["income"]
        self.assertEqual(income["columns"][0], "2026-09-26")
        self.assertEqual(income["rows"]["Total Revenue"][0], 478_834_000_000)
        self.assertEqual(income["rows"]["Net Income"][0], 132_175_000_000)
        self.assertEqual(income["rows"]["Diluted EPS"][0], 8.98)

    def test_fiscal_q4_is_fy_minus_nine_months_and_flagged(self):
        quarterly = self.statements["income_q"]
        self.assertEqual(quarterly["columns"][0], "2026-09-26")
        self.assertEqual(quarterly["rows"]["Total Revenue"][0], 478_834_000_000 - 364_357_000_000)
        self.assertIn("D", quarterly["provenance"]["Total Revenue"][0][3])
        self.assertEqual(quarterly["rows"]["Total Revenue"][1], 109_417_000_000)

    def test_ttm_of_the_latest_four_quarters_equals_the_fiscal_year(self):
        ttm = valuation.ttm_series(self.statements["income_q"], ("Total Revenue",))
        self.assertAlmostEqual(ttm[date(2026, 9, 26)], 478_834_000_000)

    def test_cover_shares_and_provenance(self):
        cover = self.statements["meta"]["cover_shares"]
        self.assertEqual(cover[-1]["end"], "2026-10-23")
        self.assertEqual(cover[-1]["val"], 14_532_182_000)
        form, filed, accn, _ = self.statements["income"]["provenance"]["Total Revenue"][0]
        self.assertEqual((form, filed, accn), ("10-K", "2026-11-03", "0000320193-26-000026"))


class TestFetchStatementsSourceChoice(unittest.TestCase):
    def tearDown(self):
        edgar_provider.set_contact("")

    def test_edgar_is_used_when_a_contact_is_set(self):
        edgar_provider.set_contact("Ann Lee ann@example.org")
        with patch.object(ef, "fetch_statements", return_value={"income": {"columns": [], "rows": {}}, "meta": {"source": "edgar"}}) as edgar, patch.object(
            fundamentals_provider, "fetch_yfinance_statements"
        ) as yf:
            result = fundamentals_provider.fetch_statements("AAPL")
        edgar.assert_called_once_with("AAPL")
        yf.assert_not_called()
        self.assertEqual(result["meta"]["source"], "edgar")

    def test_edgar_failure_falls_back_to_yfinance(self):
        edgar_provider.set_contact("Ann Lee ann@example.org")
        with patch.object(ef, "fetch_statements", side_effect=edgar_provider.EdgarError("down")), patch.object(
            fundamentals_provider, "fetch_yfinance_statements", return_value={"income": {"columns": ["x"], "rows": {}}}
        ):
            result = fundamentals_provider.fetch_statements("AAPL")
        self.assertEqual(result["meta"]["source"], "yfinance")

    def test_no_contact_skips_edgar(self):
        edgar_provider.set_contact("")
        with patch.object(ef, "fetch_statements") as edgar, patch.object(
            fundamentals_provider, "fetch_yfinance_statements", return_value={}
        ):
            fundamentals_provider.fetch_statements("AAPL")
        edgar.assert_not_called()


def _quarterly(columns, rows):
    return {"columns": columns, "rows": rows}


Q_ENDS = ["2025-12-31", "2025-09-30", "2025-06-30", "2025-03-31", "2024-12-31"]


class TestValuation(unittest.TestCase):
    def test_ttm_requires_four_consecutive_quarters(self):
        payload = _quarterly(Q_ENDS, {"Net Income": [4, 3, 2, 1, 10]})
        ttm = valuation.ttm_series(payload, ("Net Income",))
        self.assertEqual(ttm[date(2025, 12, 31)], 10)
        self.assertEqual(ttm[date(2025, 9, 30)], 16)
        self.assertNotIn(date(2025, 6, 30), ttm)
        gapped = _quarterly(["2025-12-31", "2025-06-30", "2025-03-31", "2024-12-31"], {"Net Income": [1, 1, 1, 1]})
        self.assertEqual(valuation.ttm_series(gapped, ("Net Income",)), {})

    def test_shares_are_scaled_by_splits_after_the_cover_date(self):
        cover = [{"end": "2020-07-17", "val": 4_300_000_000, "filed": "2020-07-31"}]
        splits = [(date(2014, 6, 9), 7.0), (date(2020, 8, 31), 4.0)]
        self.assertEqual(valuation.shares_at(date(2020, 6, 27), cover, splits), 17_200_000_000)
        self.assertIsNone(valuation.shares_at(date(2019, 1, 1), cover, splits))

    def test_close_lookup_tolerates_weekends_only(self):
        closes = [(date(2025, 12, 26), 10.0), (date(2025, 12, 29), 11.0)]
        self.assertEqual(valuation.close_on_or_before(closes, date(2025, 12, 31)), 11.0)
        self.assertIsNone(valuation.close_on_or_before(closes, date(2026, 2, 1)))

    def test_multiples_hand_checked(self):
        m = valuation.multiples(1000, net_income=50, ebitda=100, revenue=400, fcf=40, debt=300, cash=100, equity=250)
        self.assertEqual(m["pe"], 20)
        self.assertEqual(m["ev_ebitda"], 12)  # EV = 1000 + 300 - 100
        self.assertEqual(m["ev_sales"], 3)
        self.assertEqual(m["fcf_yield"], 0.04)
        self.assertEqual(m["pb"], 4)

    def test_losses_give_no_multiple(self):
        m = valuation.multiples(1000, net_income=-5, ebitda=0, revenue=400, fcf=-10, debt=None, cash=None, equity=-1)
        self.assertIsNone(m["pe"])
        self.assertIsNone(m["ev_ebitda"])
        self.assertIsNone(m["ev_sales"])
        self.assertIsNone(m["pb"])
        self.assertEqual(m["fcf_yield"], -0.01)

    def _statements(self):
        income_q = _quarterly(
            Q_ENDS,
            {"Net Income": [10, 10, 10, 10, 10], "Total Revenue": [100] * 5, "EBITDA": [20] * 5},
        )
        balance_q = _quarterly(Q_ENDS, {"Total Debt": [50] * 5, "Cash And Cash Equivalents": [10] * 5, "Stockholders Equity": [200] * 5})
        return {
            "income_q": income_q,
            "balance_q": balance_q,
            "meta": {
                "source": "edgar",
                "currency": "USD",
                "cover_shares": [
                    {"end": "2025-10-20", "val": 10, "filed": "2025-10-30"},
                    {"end": "2026-01-20", "val": 10, "filed": "2026-01-30"},
                ],
            },
        }

    def test_history_and_current_multiples(self):
        closes = [(date(2025, 9, 30), 16.0), (date(2025, 12, 31), 20.0)]
        points = valuation.history_points(self._statements(), closes, [])
        self.assertEqual([p["date"] for p in points], [date(2025, 9, 30), date(2025, 12, 31)])
        self.assertEqual(points[0]["pe"], 4)  # 160 / 40
        self.assertEqual(points[1]["pe"], 5)  # 200 / 40
        current = valuation.current_multiples(self._statements(), 30.0, [])
        self.assertEqual(current["pe"], 7.5)
        self.assertEqual(current["ev_ebitda"], (300 + 50 - 10) / 80)

    def test_summary_percentile(self):
        today = date(2026, 1, 1)
        points = [{"date": date(2025, 1, 1), **{k: v for k, v in zip(("pe", "ev_ebitda", "ev_sales", "fcf_yield", "pb"), (x, None, None, None, None))}} for x in (10, 20, 30, 40)]
        summary = valuation.summarize(points, {"pe": 30}, today)
        self.assertEqual(summary["pe"]["percentile_10y"], 0.75)
        self.assertEqual(summary["pe"]["median_10y"], 25)
        self.assertIsNone(summary["ev_ebitda"]["current"])

    def test_get_valuation_refuses_currency_mismatch_and_non_edgar(self):
        statements = self._statements()
        statements["meta"]["currency"] = "EUR"
        db = MagicMock()
        bars = pd.DataFrame({"Close": [20.0]}, index=pd.to_datetime(["2025-12-31"]).tz_localize("UTC"))
        bars.attrs["currency"] = "USD"
        db.load_daily_bars.return_value = bars
        with patch("stockstui.data_providers.market_provider.ensure_daily_history"):
            result = fundamentals_provider.get_valuation("SAP", statements, db, today=date(2026, 1, 1))
        self.assertIn("EUR", result["unavailable"])
        self.assertIn(
            "unavailable",
            fundamentals_provider.get_valuation("X", {"meta": {"source": "yfinance"}}, db),
        )


def _annual(columns, rows):
    return {"columns": columns, "rows": rows}


Y = ["2025-12-31", "2024-12-31", "2023-12-31"]


def _quality_statements():
    income = _annual(Y, {
        "Total Revenue": [1000, 800, 700],
        "Cost Of Revenue": [600, 500, 450],
        "Selling General And Administration": [100, 90, 80],
        "Operating Income": [200, 150, 120],
        "Net Income": [150, 100, 80],
        "Reconciled Depreciation": [50, 40, 35],
    })
    balance = _annual(Y, {
        "Total Assets": [2000, 1800, 1600],
        "Current Assets": [800, 700, 600],
        "Current Liabilities": [400, 400, 350],
        "Total Liabilities Net Minority Interest": [1000, 1000, 900],
        "Retained Earnings": [600, 500, 400],
        "Accounts Receivable": [100, 90, 80],
        "Inventory": [60, 50, 45],
        "Accounts Payable": [90, 80, 70],
        "Net PPE": [500, 450, 400],
        "Long Term Debt": [300, 350, 350],
        "Ordinary Shares Number": [100, 100, 100],
    })
    cash = _annual(Y, {
        "Operating Cash Flow": [180, 120, 100],
        "Capital Expenditure": [-75, -60, -50],
        "Depreciation And Amortization": [50, 40, 35],
    })
    return {"income": income, "balance": balance, "cash": cash}


class TestQuality(unittest.TestCase):
    def test_accruals_and_cash_conversion(self):
        s = _quality_statements()
        self.assertAlmostEqual(quality.sloan_accruals(s), (150 - 180) / 1900)
        self.assertAlmostEqual(quality.cash_conversion(s), 1.2)

    def test_working_capital_days(self):
        days = quality.working_capital_days(_quality_statements())
        self.assertAlmostEqual(days["dso"], 36.5)
        self.assertAlmostEqual(days["dio"], 60 / 600 * 365)
        self.assertAlmostEqual(days["dpo"], 90 / 600 * 365)
        self.assertAlmostEqual(days["ccc"], 36.5 + 36.5 - 54.75)

    def test_capex_to_depreciation(self):
        self.assertAlmostEqual(quality.capex_to_depreciation(_quality_statements()), 1.5)

    def test_piotroski_hand_checked(self):
        # Signals for 2025 vs 2024 (beginning assets = prior year end):
        # ROA 150/1800>0 ✓, CFO 180/1800>0 ✓, ROA up (0.083 vs 100/1600=0.0625) ✓,
        # CFO>ROA ✓, leverage down (0.15 vs 0.194) ✓, current ratio up (2.0 vs 1.75) ✓,
        # no dilution ✓, gross margin up (0.40 vs 0.375) ✓,
        # turnover up (1000/1800=0.556 vs 800/1600=0.5) ✓ -> 9/9.
        self.assertEqual(quality.piotroski(_quality_statements()), (9, 9))

    def test_piotroski_skips_missing_signals(self):
        s = _quality_statements()
        del s["balance"]["rows"]["Ordinary Shares Number"]
        self.assertEqual(quality.piotroski(s), (8, 8))

    def test_altman_z_hand_checked(self):
        z = quality.altman_z(_quality_statements(), market_cap=3000)
        expected = 1.2 * 400 / 2000 + 1.4 * 600 / 2000 + 3.3 * 200 / 2000 + 0.6 * 3000 / 1000 + 1000 / 2000
        self.assertAlmostEqual(z, expected)
        self.assertIsNone(quality.altman_z(_quality_statements(), market_cap=None))

    def test_beneish_eight_variable_hand_checked(self):
        m, model = quality.beneish(_quality_statements())
        dsri = (100 / 1000) / (90 / 800)
        gmi = (300 / 800) / (400 / 1000)
        aqi = (1 - 1300 / 2000) / (1 - 1150 / 1800)
        sgi = 1000 / 800
        depi = (40 / 490) / (50 / 550)
        sgai = (100 / 1000) / (90 / 800)
        lvgi = (700 / 2000) / (750 / 1800)
        tata = (150 - 180) / 2000
        expected = (
            -4.84 + 0.920 * dsri + 0.528 * gmi + 0.404 * aqi + 0.892 * sgi
            + 0.115 * depi - 0.172 * sgai + 4.679 * tata - 0.327 * lvgi
        )
        self.assertEqual(model, "8-var")
        self.assertAlmostEqual(m, expected)

    def test_beneish_falls_back_to_five_variables_without_sga(self):
        s = _quality_statements()
        del s["income"]["rows"]["Selling General And Administration"]
        _, model = quality.beneish(s)
        self.assertEqual(model, "5-var")

    def test_missing_inputs_give_none_not_guesses(self):
        s = {"income": _annual(Y[:1], {"Total Revenue": [1000]})}
        report = quality.quality_report(s)
        self.assertIsNone(report["piotroski"])
        self.assertIsNone(report["beneish"])
        self.assertIsNone(report["dso"])


class TestFundamentalsViewDepth(unittest.IsolatedAsyncioTestCase):
    async def _mount(self):
        from textual.app import App

        from stockstui.ui.views.fundamentals_view import FundamentalsView

        class HostApp(App):
            config = MagicMock()
            config.lists = {}
            fundamentals_ticker = None

            def compose(self):
                yield FundamentalsView()

        return HostApp(), FundamentalsView

    async def test_edgar_statements_render_ttm_valuation_and_quality(self):
        from stockstui.ui.widgets.navigable_data_table import NavigableDataTable

        statements = ef.statements_from_facts(json.loads(FIXTURE.read_text()))
        app, view_cls = await self._mount()
        async with app.run_test() as pilot:
            view = app.query_one(view_cls)
            view.update_statements(statements)
            view.update_valuation(
                {
                    "summary": valuation.summarize(
                        [], {"pe": 30.0, "ev_ebitda": None, "ev_sales": None, "fcf_yield": None, "pb": None}, date(2026, 11, 18)
                    ),
                    "market_cap": 4e12,
                }
            )
            await pilot.pause()
            table = view.query_one("#fundamentals-table", NavigableDataTable)
            headers = [str(c.label) for c in table.columns.values()]
            self.assertEqual(headers[:3], ["Line Item", "TTM", "2026-09-26"])
            revenue = [str(c) for c in table.get_row_at(0)]
            self.assertIn("478834", revenue[1].replace(",", ""))
            valuation_table = view.query_one("#valuation-table", NavigableDataTable)
            self.assertIn("30.0x", str(valuation_table.get_row_at(0)[1]))
            quality_table = view.query_one("#quality-table", NavigableDataTable)
            labels = [str(quality_table.get_row_at(i)[0]) for i in range(quality_table.row_count)]
            self.assertIn("Piotroski F-score", labels)
            altman = str(quality_table.get_row_at(labels.index("Altman Z"))[1])
            self.assertNotEqual(altman, "—")

    async def test_banks_get_a_note_instead_of_scores(self):
        from stockstui.ui.widgets.navigable_data_table import NavigableDataTable

        app, view_cls = await self._mount()
        async with app.run_test() as pilot:
            view = app.query_one(view_cls)
            view.update_statements(
                {"income": _annual(Y, {"Total Revenue": [1, 1, 1], "Net Income": [1, 1, 1]}), "balance": _annual(Y, {"Total Assets": [9, 9, 9]})}
            )
            await pilot.pause()
            table = view.query_one("#quality-table", NavigableDataTable)
            self.assertEqual(table.row_count, 0)
            self.assertIn("banks", str(view.query_one("#quality-note").render()))


if __name__ == "__main__":
    unittest.main()
