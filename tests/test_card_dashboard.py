import unittest
from datetime import date

from app.services.card_dashboard import Charge, Statement, build_dashboard, shift_month


def charge(id, month, day, amount, cat="groceries", name="Groceries", desc="REWE 123",
           counts=True, **kw):
    return Charge(id=id, month=month, day=date.fromisoformat(day), description=desc, amount=amount,
                  category_slug=cat, category_name=name, counts_as_spending=counts, **kw)


class ShiftMonthTest(unittest.TestCase):
    def test_year_boundaries(self):
        self.assertEqual(shift_month("2026-01", -1), "2025-12")
        self.assertEqual(shift_month("2025-12", 1), "2026-01")
        self.assertEqual(shift_month("2026-03", -14), "2025-01")


class BuildDashboardTest(unittest.TestCase):
    def setUp(self):
        self.charges = [
            # previous month
            charge(1, "2026-09", "2026-09-03", 50.0),
            charge(2, "2026-09", "2026-09-10", 30.0, "dining", "Dining Out", "BURGER KING 8577"),
            # selected month
            charge(3, "2026-10", "2026-10-05", 80.0, linked=True),                 # Monday
            charge(4, "2026-10", "2026-10-06", 20.0, desc="REWE 999", expects_receipt=True),
            charge(5, "2026-10", "2026-10-07", -10.0, desc="REWE refund"),         # refund
            charge(6, "2026-10", "2026-10-08", 40.0, "dining", "Dining Out", "BURGER KING 8577"),
            charge(7, "2026-10", "2026-10-09", 500.0, "transfers", "Transfers", "EINZAHLUNG", counts=False),
            charge(8, "2026-10", "2026-10-10", 15.0, "uncategorized", "Uncategorized", "XYZ"),
        ]
        self.statements = [
            Statement("amazon", "2026-09", points_earned=100, points_redeemed=0, current_points=900),
            Statement("amazon", "2026-10", points_earned=150, points_earned_eur=1.5,
                      points_redeemed=250, current_points=800, current_points_eur=8.0),
        ]
        self.d = build_dashboard(self.charges, self.statements, "2026-10")

    def test_empty(self):
        self.assertTrue(build_dashboard([], [])["empty"])

    def test_defaults_to_newest_month(self):
        self.assertEqual(build_dashboard(self.charges, self.statements)["month"], "2026-10")
        self.assertEqual(build_dashboard(self.charges, self.statements, "1999-01")["month"], "2026-10")

    def test_total_excludes_transfers_and_nets_refunds(self):
        k = self.d["kpis"]
        self.assertEqual(k["spend"]["value"], 145.0)          # 80 + 20 - 10 + 40 + 15
        self.assertEqual(k["spend"]["previous"], 80.0)
        self.assertEqual(k["spend"]["change"], 81.2)

    def test_transaction_count_only_charges(self):
        t = self.d["kpis"]["transactions"]
        self.assertEqual(t["value"], 4)                       # refund and transfer excluded
        self.assertEqual(t["average"], 38.75)

    def test_top_category_and_shares(self):
        top = self.d["kpis"]["top_category"]
        self.assertEqual((top["slug"], top["total"]), ("groceries", 90.0))
        self.assertEqual(top["share"], round(100 * 90 / 145, 1))
        self.assertNotIn("transfers", [c["slug"] for c in self.d["by_category"]])

    def test_receipts_coverage(self):
        self.assertEqual(self.d["kpis"]["receipts"], {"matched": 1, "expected": 2, "pct": 50})

    def test_points(self):
        p = self.d["kpis"]["points"]
        self.assertEqual((p["value"], p["eur"], p["previous"]), (150, 1.5, 100))
        r = self.d["rewards"]
        self.assertEqual((r["balance"], r["earned"], r["redeemed"], r["redemption_pct"]), (800, 250, 250, 100))

    def test_points_from_transactions_without_statements(self):
        charges = [charge(1, "2026-10", "2026-10-05", 80.0, points=8), charge(2, "2026-10", "2026-10-06", 20.0, points=2)]
        d = build_dashboard(charges, [], "2026-10")
        self.assertEqual(d["kpis"]["points"]["value"], 10)
        self.assertIsNone(d["kpis"]["points"]["eur"])
        self.assertEqual(d["rewards"]["earned"], 10)
        self.assertIsNone(d["rewards"]["redeemed"])
        self.assertIsNone(d["rewards"]["redemption_pct"])
        self.assertIsNone(d["rewards"]["balance"])

    def test_no_previous_month_means_no_change(self):
        d = build_dashboard(self.charges, self.statements, "2026-09")
        self.assertIsNone(d["kpis"]["spend"]["change"])
        self.assertIsNone(d["previous_month"])
        self.assertIsNone(d["category_table"][0]["last_month"])

    def test_trend_starts_at_oldest_data(self):
        self.assertEqual([t["month"] for t in self.d["trend"]], ["2026-09", "2026-10"])

    def test_top_merchants_group_by_keyword(self):
        names = {m["name"]: m["total"] for m in self.d["top_merchants"]}
        # REWE 123 / REWE 999 / REWE refund fold into one merchant
        self.assertEqual(sum(1 for n in names if n.startswith("REWE")), 1)
        self.assertEqual(self.d["top_merchants"][0]["total"], 90.0)

    def test_category_table(self):
        rows = {r["slug"]: r for r in self.d["category_table"]}
        self.assertEqual(rows["dining"]["this_month"], 40.0)
        self.assertEqual(rows["dining"]["last_month"], 30.0)
        self.assertEqual(rows["dining"]["average"], 30.0)
        self.assertEqual(rows["groceries"]["count"], 2)

    def test_biggest_and_weekdays(self):
        self.assertEqual([b["amount"] for b in self.d["biggest"]], [80.0, 40.0, 20.0, 15.0])
        week = {w["day"]: w["total"] for w in self.d["weekdays"]}
        self.assertEqual(week["Mon"], 80.0)
        self.assertEqual(sum(week.values()), 155.0)           # charges only, no refund

    def test_uncategorized(self):
        self.assertEqual(self.d["uncategorized"], {"count": 1, "total": 15.0})


if __name__ == "__main__":
    unittest.main()
