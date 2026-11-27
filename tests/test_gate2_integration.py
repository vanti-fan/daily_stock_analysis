"""Gate 2 combined offline acceptance. No vendor or LLM requests."""
import json
import os
import tempfile
import unittest
from datetime import date, datetime
from pathlib import Path
from unittest.mock import Mock, patch
from zoneinfo import ZoneInfo
import pandas as pd
from src import intelligent_fallback as ifb, chip_data_quality as cq, news_source_quality as nq
from src import market_data_quality as mq, financial_dashboard as fd
from src import raw_data_archive as ar, data_quality_manager as dq, provider_health_monitor as ph
from test_chip_data_quality import chip
from test_news_source_quality import fixture as news_fixture, DAY as NEWS_DAY
from test_market_data_quality import fixture as market_fixture

CODE="002938"
DAY="2026-11-26"
FLAGS={"DSA_INTELLIGENT_FALLBACK":"true","DSA_MARKET_DATA_QUALITY":"true",
       "DSA_CHIP_DATA_QUALITY":"true","DSA_NEWS_SOURCE_QUALITY":"true",
       "DSA_RAW_DATA_ARCHIVE":"true","DSA_CHIP_TUSHARE_FALLBACK":"false"}

def bars():
    return pd.DataFrame([dict(code=CODE,date=d.date().isoformat(),open=45,high=46,low=44,
        close=45.2,volume=100000,amount=4520000,data_source="BaostockFetcher")
        for d in pd.bdate_range(end=DAY,periods=65)])

def quote():
    return dict(code=CODE,name="鹏鼎控股",source="tencent",market="cn",currency="CNY",
        quote_date=DAY,is_stale=False,price=45.2,change_pct=0,volume=100000,
        amount=4520000,volume_ratio=1.1,turnover_rate=2,open_price=45,high=46,low=44,
        pe_ratio=20,field_sources={"turnover_rate":dict(source="akshare_sina",quote_date=DAY,
            timestamp=DAY+"T14:59:00+08:00")})

def inputs(root,query="current",evidenced=True,quote_override=None):
    df=ifb.mark_history(bars(),"BaostockFetcher")
    ifb.validate_history(df,"BaostockFetcher")
    return ar.archive_inputs(root,code=CODE,name="鹏鼎控股",market="cn",query_id=query,
        rows=df.to_dict("records"),quote=quote_override if quote_override is not None else quote(),
        trend=dict(code=CODE,ma5=45.2,ma10=45.2,ma20=45.2,ma60=45.2,macd_dif=0,macd_dea=0),
        trend_input_count=len(df),trend_trade_date=DAY,chip=chip(),expected_trade_date=DAY,
        history_metadata=df.attrs["dsa_history"] if evidenced else None)

def combined(root):
    root=Path(root)
    inputs(root)
    ar.archive_news(root,code=CODE,query_id="current",payload=news_fixture())
    ar.archive_news(root,code=CODE,query_id="current",payload=news_fixture("announcements"))
    ar.archive_news(root,code=CODE,query_id="old",payload=news_fixture())
    market=market_fixture()
    market["financial_market_data"]=mq.financial_market_model(market)
    ar.archive_market(root,region="cn",query_id="market-current",payload=market)
    rows=[dict(code=CODE,name="鹏鼎控股",query_id="current",status="TIMEOUT",expected_trade_date=DAY)]
    quality=dq.build_report(root,rows,scenario="GATE2-FIXTURE")
    rows=dq.attach_quality(rows,quality)
    return rows,quality,market

class Gate2Integration(unittest.TestCase):
    def setUp(self):
        for p in (patch.dict(os.environ,FLAGS|{"DSA_PROVIDER_HEALTH_JOURNAL":""}),
                  patch.object(nq,"today_cn",return_value=NEWS_DAY),
                  patch.object(mq,"china_now",return_value=datetime(2026,11,26,18,tzinfo=ZoneInfo("Asia/Shanghai"))),
                  patch.object(mq,"expected_session",return_value=DAY),
                  patch("requests.sessions.Session.request",side_effect=AssertionError("Unexpected network"))):
            p.start(); self.addCleanup(p.stop)
        tmp=tempfile.TemporaryDirectory();self.addCleanup(tmp.cleanup);self.root=Path(tmp.name)
    def test_combined_timeout(self):
        rows,q,m=combined(self.root)
        s=q["stocks"][0]
        self.assertEqual(s["history"]["adjustment"],"QFQ")
        self.assertEqual(s["history"]["status"],"AVAILABLE")
        self.assertTrue(s["groups"]["technical"]["ma60"]["usable"])
        self.assertEqual(s["quote_status"],"AVAILABLE")
        self.assertEqual(s["groups"]["snapshot"]["turnover_rate"]["source"],"akshare_sina")
        self.assertEqual(s["coverage"]["chips"]["usable"],8)
        self.assertEqual(s["news_quality"]["event_count"],1)
        self.assertEqual(len(s["news_quality"]["events"][0]["dimensions"]),2)
        self.assertEqual(s["ai_status"],"NOT_COMPLETED")
        pages=dict(fd.stock_pages(Mock(),rows,[],""))
        for key in ("stock-002938-quote-only","stock-002938-chips","stock-002938-news-01"):
            self.assertIn(key,pages)
        self.assertNotIn("stock-002938-02",pages)
        self.assertTrue(fd.market_pages("",m))
        self.assertEqual(m["financial_market_data"]["breadth"]["total_amount"]["unit"],"CNY_100M")
    def test_unknown_adjustment(self):
        inputs(self.root,evidenced=False)
        s=dq.assess_stock(self.root,ar.verify(self.root),dict(code=CODE,query_id="current",status="TIMEOUT"))
        self.assertIsNone(s["history"]["adjustment"])
        self.assertFalse(s["groups"]["technical"]["ma5"]["usable"])
        self.assertTrue(s["groups"]["snapshot"]["price"]["usable"])
    def test_lineage_conflict(self):
        q=quote();q["field_sources"]["turnover_rate"]["quote_date"]="2026-11-25"
        inputs(self.root,quote_override=q)
        s=dq.assess_stock(self.root,ar.verify(self.root),dict(code=CODE,query_id="current",status="TIMEOUT"))
        self.assertFalse(s["groups"]["snapshot"]["turnover_rate"]["usable"])
        self.assertTrue(s["groups"]["snapshot"]["price"]["usable"])
    def test_archive_identity(self):
        p=inputs(self.root);m=p/"manifest.json";obj=json.loads(m.read_text());obj["query_id"]="different"
        m.write_text(json.dumps(obj))
        with self.assertRaises(ValueError): ar.verify(self.root)
    def test_archive_symlink(self):
        p=inputs(self.root);f=p/"quote.json";target=self.root/"outside.json";target.write_bytes(f.read_bytes());f.unlink();f.symlink_to(target)
        with self.assertRaises(ValueError): ar.verify(self.root)
    def test_digest_fail_closed(self):
        rows,q,m=combined(self.root)
        next(self.root.rglob("chip.json")).write_text("tampered")
        with self.assertRaises(ValueError): dq.build_report(self.root,rows)
        self.assertIsNone(dq.dashboard_quote(dq.unavailable_quality(rows)[0],"price"))
    def test_health_and_field_coverage(self):
        rows,q,m=combined(self.root)
        journal=self.root/"health.jsonl"
        with patch.dict(os.environ,{"DSA_PROVIDER_HEALTH_JOURNAL":str(journal)}):
            for provider,ok in (("tencent",False),("akshare_sina",True)):
                ph.start("realtime_quote",provider,"get_realtime_quote")
                ph.finish("realtime_quote",provider,"get_realtime_quote",ok,error_type=None if ok else "timeout")
        report=ph.build_report(journal,quality=q,profile="FIXTURE")
        self.assertEqual(sum(x["completed"] for x in report["providers"]),2)
        self.assertEqual(sum(x["success"] for x in report["providers"]),1)
        self.assertTrue(any(x["source"]=="akshare_sina" for x in report["field_coverage"]))
    def test_sqlite_replacement(self):
        from src.storage import DatabaseManager
        DatabaseManager.reset_instance();self.addCleanup(DatabaseManager.reset_instance)
        db=DatabaseManager(db_url="sqlite:///"+str(self.root/"gate2.db"))
        df=ifb.mark_history(bars().tail(3).copy(),"BaostockFetcher")
        db.save_daily_data(df,CODE,"BaostockFetcher")
        def read():
            return [(x.date,x.close,x.data_source) for x in db.get_data_range(CODE,date(2026,11,1),date(2026,11,30))]
        before=read();self.assertEqual(len(before),3)
        short=ifb.mark_history(df.tail(2).copy(),"TencentFetcher")
        short["close"]=45.8
        with self.assertRaises(ifb.AdjustmentError): db.save_daily_data(short,CODE,"TencentFetcher")
        self.assertEqual(read(),before)
        full=ifb.mark_history(df.copy(),"TencentFetcher");full["close"]=45.8
        db.save_daily_data(full,CODE,"TencentFetcher")
        self.assertEqual(len(read()),3)
        self.assertTrue(all(x[1]==45.8 and x[2]=="TencentFetcher" for x in read()))

def render_fixture(output):
    out=Path(output);out.mkdir(parents=True,exist_ok=True)
    with patch.dict(os.environ,FLAGS),patch.object(nq,"today_cn",return_value=NEWS_DAY),patch.object(mq,"china_now",return_value=datetime(2026,11,26,18,tzinfo=ZoneInfo("Asia/Shanghai"))),patch.object(mq,"expected_session",return_value=DAY):
        root=out/"archive";rows,q,m=combined(root)
        dq.write_report(root/"data-quality.json",q);ar.write_index(root)
        pages=fd.stock_pages(Mock(),rows,[],"# 固定样本 "+DAY)+fd.quality_pages(rows,DAY)+fd.market_pages("",m)
        paths=fd.render_pages(pages,out)
        assert len(paths)==len(pages)
        (out/"summary.json").write_text(json.dumps(dict(profile="FIXTURE",pages=[p.name for p in paths],archive_bundles=len(ar.verify(root)),quote_price_usable=q["quote_price_usable"],ai_completed=q["ai_completed"]),indent=2),encoding="utf8")
