"""Offline Phase 2.1 contract and adapter tests."""
import json
import unittest
from types import SimpleNamespace

from src.financial_data_model import StockIdentity, DataStatus, Origin
from src.financial_data_adapters import build_stock_data, history_fields, quote_fields, chip_fields, technical_fields


class FinancialModelTests(unittest.TestCase):
    def setUp(self):
        self.identity = StockIdentity("002938", "鹏鼎控股", "cn", "CNY")
        self.quote = dict(code="002938", market="cn", currency="CNY",
                          source="tencent", quote_date="2026-11-26",
                          fetched_at="2026-11-26T16:00:00+08:00", is_stale=False,
                          price=45.8, change_pct=0, volume=28849300,
                          amount=1326000000, volume_ratio=0.91, turnover_rate=1.25)

    def test_objective_and_ai_separation(self):
        model = build_stock_data(self.identity, quote=self.quote,
                                 result=dict(code="002938", success=True, sentiment_score=0,
                                             current_price=999, model_used="fixture-model",
                                             dashboard={"price":999}))
        self.assertEqual(model.snapshot.fields["price"].value, 45.8)
        self.assertEqual(model.snapshot.fields["change_pct"].value, 0)
        self.assertTrue(model.snapshot.fields["change_pct"].usable)
        self.assertEqual(model.research.score, 0)
        self.assertEqual(model.research.payload["dashboard"]["price"],999)
        self.assertEqual(model.execution.actual_model,"fixture-model")
        self.assertEqual(json.loads(model.to_json())["identity"]["code"],"002938")

    def test_missing_invalid_stale_and_unknown(self):
        q = dict(self.quote, price=float("nan"), volume=-1, high=0, is_stale=True)
        f = quote_fields(q,self.identity).fields
        self.assertEqual(f["price"].quality.status,DataStatus.INVALID)
        self.assertEqual(f["volume"].quality.status,DataStatus.INVALID)
        self.assertEqual(f["high"].quality.status,DataStatus.INVALID)
        self.assertEqual(f["open"].quality.status,DataStatus.NOT_PROVIDED)
        self.assertEqual(f["amount"].quality.status,DataStatus.STALE)
        self.assertFalse(f["amount"].usable)
        f = quote_fields(dict(self.quote, is_stale=None),self.identity).fields
        self.assertEqual(f["price"].quality.status,DataStatus.UNVERIFIED)
        f = quote_fields(dict(self.quote, missing_fields=["volume"]),self.identity).fields
        self.assertIsNone(f["volume"].value)

    def test_history_units_and_ohlc(self):
        bar = dict(date="2026-11-26",open=47,high=47.41,low=45.2,close=45.8,volume=0)
        h = history_fields([bar],identity=self.identity,source="tushare")
        self.assertEqual(h.status,DataStatus.PARTIAL)
        self.assertIsNone(h.bars[0].fields["volume"].unit)
        self.assertEqual(h.bars[0].fields["volume"].value,0)
        h = history_fields([bar],identity=self.identity,source="fixture",adjustment="unadjusted",volume_unit="share")
        self.assertEqual(h.status,DataStatus.AVAILABLE)
        bad = history_fields([dict(bar,high=44)],identity=self.identity)
        self.assertEqual(bad.bars[0].fields["close"].quality.status,DataStatus.INVALID)
        with self.assertRaises(ValueError):
            history_fields([bar,bar],identity=self.identity)

    def test_ma60_substitution_and_macd_defaults(self):
        trend = SimpleNamespace(code="002938", ma5=45.73,ma10=44.827,
                                ma20=47.6095,ma60=47.6095,macd_bar=0)
        fields = technical_fields(trend,identity=self.identity,sample_count=39).fields
        self.assertEqual(fields["ma20"].value,47.6095)
        self.assertEqual(fields["ma60"].quality.status,DataStatus.NOT_COMPUTED)
        self.assertIsNone(fields["ma60"].value)
        self.assertEqual(fields["macd_bar"].value,0)
        self.assertEqual(fields["macd_bar"].quality.origin,Origin.COMPUTED)
        fields = technical_fields(trend,identity=self.identity,sample_count=19).fields
        self.assertIsNone(fields["macd_bar"].value)
        fields = technical_fields(trend,identity=self.identity).fields
        self.assertIsNone(fields["ma5"].value)

    def test_chip_fraction_and_missing_default(self):
        obj=SimpleNamespace(code="002938",source="akshare",date="2026-11-26",profit_ratio=0,avg_cost=0)
        fields=chip_fields(obj,self.identity).fields
        self.assertEqual(fields["profit_ratio"].quality.status,DataStatus.UNVERIFIED)
        self.assertEqual(fields["avg_cost"].quality.status,DataStatus.INVALID)
        fields=chip_fields(dict(code="002938",source="fixture",date="2026-11-26",profit_ratio=0),self.identity).fields
        self.assertEqual(fields["profit_ratio"].value,0)
        self.assertEqual(fields["profit_ratio"].unit,"fraction")

    def test_identity_mismatch(self):
        with self.assertRaises(ValueError):
            quote_fields(dict(self.quote,code="300394"),self.identity)
        with self.assertRaises(ValueError):
            quote_fields(dict(self.quote,currency="USD"),self.identity)
        with self.assertRaises(ValueError):
            StockIdentity(2938)

    def test_empty_inputs_and_strict_json(self):
        model=build_stock_data(self.identity)
        self.assertEqual(model.history.status,DataStatus.NOT_PROVIDED)
        self.assertIsNone(model.execution.actual_model)
        self.assertIsNone(model.snapshot.fields["price"].value)
        self.assertEqual(model.research.status,DataStatus.NOT_PROVIDED)
        json.loads(model.to_json())


if __name__ == "__main__":
    unittest.main()
