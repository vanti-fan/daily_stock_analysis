"""Offline policy and integration tests. No provider API calls."""
import os
import tempfile
import unittest
from unittest.mock import patch
from types import SimpleNamespace
import pandas as pd
from src import intelligent_fallback as p

def frame():
    return pd.DataFrame({"date":["2026-11-25","2026-11-26"],"open":[10,11],"high":[12,13],"low":[9,10],"close":[11,12]})

class PolicyTests(unittest.TestCase):
    def test_history_allowlist(self):
        names=["TushareFetcher","PytdxFetcher","BaostockFetcher","TencentFetcher","AkshareFetcher","EfinanceFetcher"]
        with patch.dict(os.environ,{"DSA_PROVIDER_HEALTH_JOURNAL":""}):
            self.assertEqual([x.name for x in p.history_order([SimpleNamespace(name=n) for n in names])],names[2:])
    def test_quote_dependencies(self):
        with patch.dict(os.environ,{"DSA_PROVIDER_HEALTH_JOURNAL":""}):
            self.assertEqual(p.quote_order(["tencent","akshare_qq","efinance","akshare_em","akshare_sina","akshare_xq"]),["tencent","efinance","akshare_sina","akshare_em","akshare_xq"])
    def test_health_reorder(self):
        from src.provider_health_monitor import start,finish
        with tempfile.TemporaryDirectory() as d, patch.dict(os.environ,{"DSA_PROVIDER_HEALTH_JOURNAL":d+"/h.jsonl"}):
            for _ in range(3):
                start("realtime_quote","tencent","get_realtime_quote")
                finish("realtime_quote","tencent","get_realtime_quote",False,error_type="timeout")
            self.assertEqual(p.quote_order(["tencent","akshare_sina"]),["akshare_sina","tencent"])
    def test_history_valid(self):
        d=p.mark_history(frame(),"BaostockFetcher")
        self.assertIs(p.validate_history(d,"BaostockFetcher"),d)
        self.assertEqual(d.attrs["dsa_history"]["last_date"],"2026-11-26")
    def test_history_missing_metadata(self):
        with self.assertRaises(p.AdjustmentError): p.validate_history(frame(),"BaostockFetcher")
    def test_history_unadjusted(self):
        with self.assertRaises(p.AdjustmentError): p.validate_history(p.mark_history(frame(),"TushareFetcher"),"TushareFetcher")
    def test_history_invalid_ohlc(self):
        d=p.mark_history(frame(),"BaostockFetcher")
        d.loc[0,"high"]=1
        with self.assertRaises(p.AdjustmentError): p.validate_history(d,"BaostockFetcher")
    def test_history_duplicate_dates(self):
        d=p.mark_history(frame(),"BaostockFetcher")
        d.loc[1,"date"]=d.loc[0,"date"]
        with self.assertRaises(p.AdjustmentError): p.validate_history(d,"BaostockFetcher")
    def test_history_outside_range(self):
        with self.assertRaises(p.AdjustmentError): p.validate_history(p.mark_history(frame(),"BaostockFetcher"),"BaostockFetcher",end_date="2026-11-25")
    def test_storage_no_mixed_window(self):
        p.validate_storage_window([1,2,3],[1,2])
        with self.assertRaises(p.AdjustmentError): p.validate_storage_window([2,3],[1,2])
    def test_tencent_no_raw_fallback(self):
        from data_provider.tencent_fetcher import _extract_kline_rows
        payload={"data":{"sz002938":{"day":[["2026-11-26","10","11","12","9","100"]]}}}
        self.assertEqual(_extract_kline_rows(payload,symbol="sz002938",require_qfq=True),[])
        self.assertEqual(len(_extract_kline_rows(payload,symbol="sz002938")),1)
        payload["data"]["sz002938"]["qfqday"]=payload["data"]["sz002938"]["day"]
        self.assertEqual(len(_extract_kline_rows(payload,symbol="sz002938",require_qfq=True)),1)
    def test_field_lineage(self):
        from data_provider.realtime_types import UnifiedRealtimeQuote,RealtimeSource
        a=UnifiedRealtimeQuote("002938",source=RealtimeSource.TENCENT,price=10,provider_timestamp="2026-11-26T15:00:00+08:00")
        b=UnifiedRealtimeQuote("002938",source=RealtimeSource.AKSHARE_SINA,price=10,provider_timestamp="2026-11-26T14:59:00+08:00",turnover_rate=1.2)
        self.assertEqual(p.merge_fields(a,b,["turnover_rate"]),["turnover_rate"])
        self.assertEqual(a.to_dict()["field_sources"]["turnover_rate"]["source"],"akshare_sina")
        self.assertEqual(a.to_dict()["field_sources"]["price"]["source"],"tencent")
    def test_quote_conflicts(self):
        a=SimpleNamespace(code="002938",price=10,provider_timestamp="2026-11-26T15:00:00+08:00",currency="CNY")
        for changed in ({"code":"300394"},{"price":11},{"provider_timestamp":None},{"provider_timestamp":"2026-11-25T15:00:00+08:00"},{"currency":"USD"}):
            b=SimpleNamespace(**(vars(a)|changed))
            self.assertFalse(p.compatible_quotes(a,b))
    def test_xueqiu_contract(self):
        d=pd.DataFrame({"item":["代码","货币","名称","现价","涨幅","市盈率(动)"],"value":["SZ002938","CNY","fixture",45.8,-.87,20]})
        q=p.parse_xueqiu(d,"002938")
        self.assertEqual(q.price,45.8)
        self.assertEqual(q.source.value,"xueqiu")
        self.assertIsNone(q.provider_timestamp)
        self.assertIsNone(p.parse_xueqiu(d,"300394"))
    def test_gate(self):
        with patch.dict(os.environ,{"DSA_INTELLIGENT_FALLBACK":"true"}):
            self.assertTrue(p.strict_history("002938"))
            for code in ["510300","00700","AAPL","sh000300","920001"]:
                self.assertFalse(p.strict_history(code))
        with patch.dict(os.environ,{"DSA_INTELLIGENT_FALLBACK":"false"}):
            self.assertFalse(p.strict_history("002938"))

if __name__=="__main__": unittest.main()
