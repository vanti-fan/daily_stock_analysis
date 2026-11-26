"""Phase 2.3 deterministic offline scenarios and regression checks."""
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

from src import financial_dashboard as fd
from src.data_quality_manager import (assess_field, compare_quotes, failure_status,
    build_report, attach_quality, dashboard_quote, unavailable_quality, write_report)
from src.raw_data_archive import archive_inputs, archive_research, write_index
from test_financial_dashboard import CODES, PAYLOAD, MARKET

DAY="2026-11-26"
NAMES=["鹏鼎控股","天孚通信","莱美药业","金钼股份"]
PRICES=[45.80,133.99,8.00,14.73]
CHANGES=[-0.87,2.38,-7.94,0.61]
SCENARIOS={"A-all-success":(4,4),"B-partial":(1,1),
           "C-all-failed":(0,0),"D-quote-success-ai-failed":(4,1)}


def scenario(root, label):
    root=Path(root)
    quote_count,ai_count=SCENARIOS[label]
    rows=[]; results=[]
    notifier=Mock()
    notifier.generate_single_stock_report.return_value="# 固定测试样本\n"+DAY
    for i,code in enumerate(CODES):
        name,price,change=NAMES[i],PRICES[i],CHANGES[i]
        query_id=label+"-"+code
        has_quote=i>=4-quote_count
        ai_ok=i>=4-ai_count
        rows.append(dict(code=code,name=name,query_id=query_id,
                         status="SUCCESS" if ai_ok else "TIMEOUT",
                         expected_trade_date=DAY))
        if has_quote:
            quote=dict(code=code,name=name,source="fixture-provider",market="cn",
                currency="CNY",quote_date=DAY,is_stale=False,price=price,
                change_pct=change,volume=100000,amount=price*100000,
                volume_ratio=1.1,turnover_rate=2.0,open_price=price,
                high=price*1.01,low=price*0.99)
            archive_inputs(root,code=code,name=name,market="cn",query_id=query_id,
                quote=quote,expected_trade_date=DAY)
        if ai_ok:
            # Deliberately conflicting AI price; only verified objective price may display.
            payload={**PAYLOAD,"code":code,"name":name,"success":True,
                     "current_price":99999.0,"sentiment_score":35,"model_used":"fixture"}
            archive_research(root,code=code,query_id=query_id,result=payload)
            results.append(SimpleNamespace(code=code,name=name,success=True,
                                            to_dict=lambda p=payload:p))
    quality=build_report(root,rows,scenario=label)
    write_report(root/"data-quality.json",quality)
    write_index(root)
    rows=attach_quality(rows,quality)
    return notifier, rows, results, quality


class QualityContract(unittest.TestCase):
    def test_four_scenarios(self):
        for label,(quotes,ai) in SCENARIOS.items():
            with self.subTest(label=label), tempfile.TemporaryDirectory() as d:
                notifier,rows,results,quality=scenario(d,label)
                self.assertEqual(quality["quote_price_usable"],quotes)
                self.assertEqual(quality["ai_completed"],ai)
                pages=dict(fd.stock_pages(notifier,rows,results,""))
                self.assertNotIn("99999", "".join(pages.values()))
                for i,code in enumerate(CODES):
                    if i>=4-quotes:
                        self.assertIn(f"{PRICES[i]:.2f}", pages["watchlist-01"])
                    if i<4-ai:
                        self.assertNotIn("stock-"+code+"-02",pages)
                        self.assertEqual(rows[i]["data_quality"]["ai_status"],"NOT_COMPLETED")
                if label.startswith("D"):
                    self.assertEqual(sum(k.endswith("quote-only") for k in pages),3)
                self.assertTrue(fd.quality_pages(rows,DAY,scenario=label))
    def field(self, v=10, **kwargs):
        return dict(value=v,unit="CNY",quality=dict(status="AVAILABLE",origin="PROVIDER",
                     source="fixture",trade_date=DAY,**kwargs))
    def test_zero_negative_and_nonfinite(self):
        self.assertFalse(assess_field("price",self.field(0))["usable"])
        self.assertFalse(assess_field("price",self.field(float("nan")))["usable"])
        self.assertFalse(assess_field("volume",self.field(-1))["usable"])
        self.assertTrue(assess_field("change_pct",self.field(0))["usable"])
    def test_dates_and_currency(self):
        f=self.field()
        self.assertEqual(assess_field("price",f,expected_date="2026-11-27",require_date=True)["status"],"STALE")
        self.assertEqual(assess_field("price",f,expected_date="2026-11-25",require_date=True)["status"],"CONFLICT")
        self.assertEqual(assess_field("price",f,require_date=True)["status"],"UNVERIFIED")
        self.assertEqual(assess_field("price",f,currency="USD")["status"],"CONFLICT")
    def test_failure_classification(self):
        for code,status in [(402,"PAYMENT_REQUIRED"),(403,"ACCESS_DENIED"),(429,"RATE_LIMITED"),(504,"TIMEOUT"),(503,"UPSTREAM_ERROR")]:
            self.assertEqual(failure_status(http_status=code),status)
        self.assertEqual(failure_status(exception=TimeoutError()),"TIMEOUT")
        self.assertEqual(failure_status(),"UNKNOWN")
    def test_comparison(self):
        a=dict(key="price",unit="CNY",trade_date=DAY,observed_at=DAY+"T15:00:00+08:00",source="a",value=10)
        self.assertEqual(compare_quotes(a,dict(a,source="b",value=11))["status"],"CONFLICT")
        self.assertEqual(compare_quotes(a,dict(a,source="b",value=10.001))["status"],"VALIDATED")
        self.assertEqual(compare_quotes(a,dict(a,value=10.001))["status"],"UNVERIFIED")
    def test_query_isolation_and_tamper(self):
        with tempfile.TemporaryDirectory() as d:
            _,rows,_,_=scenario(d,"A-all-success")
            wrong=[dict(rows[0],query_id="old")]
            self.assertEqual(build_report(d,wrong)["quote_price_usable"],0)
            self.assertIsNone(dashboard_quote(unavailable_quality(rows)[0],"price"))
            p=next(Path(d).rglob("quote.json"))
            p.write_text("tampered")
            with self.assertRaises(ValueError):
                build_report(d,rows)
    def test_no_ai_on_failed_assessment(self):
        with tempfile.TemporaryDirectory() as d:
            notifier,rows,results,_=scenario(d,"A-all-success")
            pages=fd.stock_pages(notifier,unavailable_quality(rows),results,"")
            self.assertTrue(all(name.startswith("watchlist") for name,_ in pages))
            self.assertNotIn("99999", "".join(doc for _,doc in pages))


def render_scenarios(output):
    output=Path(output)
    summary=[]
    for label,(quotes,ai) in SCENARIOS.items():
        folder=output/label
        notifier,rows,results,quality=scenario(folder/"archive",label)
        assert (quality["quote_price_usable"],quality["ai_completed"])==(quotes,ai)
        fd.save_status(rows,folder)
        pages=fd.stock_pages(notifier,rows,results,"# 固定样本 "+DAY)
        pages+=fd.quality_pages(rows,DAY,scenario=label)
        # Every page explicitly marked as synthetic, including individual stock pages.
        pages=[(name,doc.replace("非实时交易指令","固定样本 / 非实时行情 / "+label))
               for name,doc in pages]
        paths=fd.render_pages(pages,folder)
        assert len(paths)==len(pages) and len({p.name for p in paths})==len(pages)
        summary.append(dict(scenario=label,quote_count=quotes,ai_count=ai,
                            png=[p.name for p in paths]))
    write_report(output/"scenario-summary.json",summary)
    return summary

if __name__=="__main__":
    unittest.main()
