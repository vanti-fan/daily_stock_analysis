import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
import pandas as pd
from src import market_data_quality as q

def frame():
    return pd.DataFrame([
        {"代码":"002938","最新价":11,"昨收":10,"成交额":100000000,"涨停价":11,"跌停价":9},
        {"代码":"300394","最新价":9,"昨收":10,"成交额":200000000,"涨停价":12,"跌停价":8},
        {"代码":"600001","最新价":10,"昨收":10,"成交额":300000000,"涨停价":11,"跌停价":9},
        {"代码":"600002","最新价":10,"昨收":10,"成交额":0},
        {"代码":"510300","最新价":5,"昨收":4,"成交额":900000000},
    ])
def fixture():
    b=q.breadth_from_frame(frame(),"fixture",min_universe=4,known_date="2026-11-26")
    idx=q.index_rows([dict(code=k,name=v,current=101,prev_close=100,change_pct=1,trade_date="2026-11-26") for k,v in q.INDEX_NAMES.items()],"fixture")
    sectors=q.ranking_rows(([dict(name="PCB",change_pct=3.5),dict(name="钢铁",change_pct=-2)],[]),"fixture","industry",1)
    return dict(region="cn",date="2026-11-26",market_data_quality_version="2.4.4",
                indices=idx,breadth=q.public_breadth(b),sectors=dict(top=sectors[0],bottom=sectors[1]),
                concepts=dict(top=[dict(name="光通信",change_pct=2.5,classification="fixture")],bottom=[]))

class MarketQualityTest(unittest.TestCase):
    def setUp(self):
        from datetime import datetime
        from zoneinfo import ZoneInfo
        clock=patch.object(q,'china_now',return_value=datetime(2026,11,26,18,tzinfo=ZoneInfo('Asia/Shanghai')))
        self.addCleanup(clock.stop); clock.start()
        session=patch.object(q,'expected_session',return_value='2026-11-26')
        self.addCleanup(session.stop); session.start()
    def test_breadth_scope(self):
        r=q.breadth_from_frame(frame(),"fixture",min_universe=4)
        self.assertEqual((r["up_count"],r["down_count"],r["flat_count"]),(1,1,1))
        self.assertEqual(r["universe_count"],4)
        self.assertEqual(r["zero_amount_count"],1)
        self.assertEqual(r["total_amount"],6)
        self.assertEqual(r["limit_up_count"],1)
        self.assertEqual(r["limit_down_count"],0)
        self.assertFalse(r["coverage_verified"])
    def test_small_sample(self):
        r=q.breadth_from_frame(frame(),"fixture")
        self.assertFalse(r["breadth_usable"])
        self.assertIsNone(r["total_amount"])
    def test_missing_amount_and_limits(self):
        r=q.breadth_from_frame(frame().drop(columns=["成交额","涨停价","跌停价"]),"fixture",min_universe=4)
        self.assertIsNone(r["total_amount"])
        self.assertIsNone(r["limit_up_count"])
    def test_invalid_or_duplicate(self):
        f=frame()
        f=pd.concat([f,f.iloc[[0]].assign(最新价=15)])
        r=q.breadth_from_frame(f,"fixture",min_universe=4)
        self.assertEqual(r["duplicate_conflicts"],1)
        self.assertFalse(r["breadth_usable"])
    def test_date_conflict(self):
        f=frame().assign(trade_date=["20261125","20261126","20261126","20261126","20261126"])
        self.assertIsNone(q.breadth_from_frame(f,"fixture"))
    def test_index_identity_and_change(self):
        a=dict(code="000001.SZ",name="上证指数",current=101,prev_close=100,change_pct=1)
        self.assertEqual(q.index_rows([a],"fixture"),[])
        a.update(code="sh000001",change_pct=5)
        self.assertEqual(q.index_rows([a],"fixture"),[])
        a.update(change_pct=1)
        self.assertEqual(q.index_rows([a],"fixture")[0]["change_pct"],1)
    def test_rank_no_duplicates(self):
        a=[dict(name="PCB",change_pct=3),dict(name="煤炭",change_pct=-1)]
        top,bottom=q.ranking_rows((a,a),"fixture","concept",5)
        self.assertEqual(len(top)+len(bottom),2)
        self.assertEqual(top[0]["category"],"concept")
    def test_legacy_missing(self):
        r=q.normalize_stats(dict(up_count=1,down_count=1,flat_count=0,total_amount=0,limit_up_count=0,limit_down_count=0),"legacy")
        self.assertIsNone(r["total_amount"])
        self.assertIsNone(r["limit_up_count"])
        self.assertFalse(r["breadth_usable"])
    def test_manager_fallback(self):
        from data_provider.base import DataFetcherManager
        class Fake:
            def __init__(self,name,priority,data):
                self.name=name; self.priority=priority; self.data=data; self.calls=0
            def get_main_indices(self,region="cn"):
                self.calls+=1; return self.data
        full=[dict(code=k,name=v,current=101,prev_close=100,change_pct=1,trade_date=q.china_now().date().isoformat()) for k,v in q.INDEX_NAMES.items()]
        a=Fake("A",0,full[:1]); b=Fake("B",1,full)
        with patch.dict(os.environ,{"DSA_MARKET_DATA_QUALITY":"true","DSA_PROVIDER_HEALTH_JOURNAL":""}):
            m=DataFetcherManager([a,b])
            m._get_tickflow_fetcher=lambda:None
            self.assertEqual(len(m.get_main_indices("cn")),6)
            self.assertEqual((a.calls,b.calls),(1,1))
    def test_dashboard_no_ai_overwrite(self):
        from src.financial_dashboard import market_pages
        pages=dict(market_pages("| 指数 | 最新 | 涨跌幅 |\n|---|---|---|\n| 上证 | 99999 | 99% |",fixture()))
        self.assertNotIn("99999",pages["market-01"])
        self.assertIn("光通信",pages["market-concepts-top"])
        self.assertIn("市场宽度",pages["market-breadth"])
    def test_previous_turnover(self):
        a=q.breadth_from_frame(frame(),"fixture",min_universe=4,known_date="2026-11-26",completed_session=True)
        b=dict(a,trade_date="2026-11-25",total_amount=5)
        self.assertAlmostEqual(q.previous_turnover(a,b,"2026-11-25")["turnover_change_pct"],20)
        self.assertIsNone(q.previous_turnover(a,dict(b,universe_hash="different"),"2026-11-25")["turnover_change_pct"])
        self.assertIsNone(q.previous_turnover(a,dict(b,completed_session=False),"2026-11-25")["turnover_change_pct"])
    def test_limit_pool(self):
        self.assertEqual(q.limit_pool(pd.DataFrame({"代码":["002938","002938","300394"]}),"2026-11-26","up")["count"],2)
        self.assertIsNone(q.limit_pool(pd.DataFrame(),"2026-11-26","down")["count"])
    def test_sector_amount(self):
        df=pd.DataFrame({"板块":["A","B"],"涨跌幅":[1,-1],"成交额":[100000000,200000000]})
        top,bottom=q.rank_frame(df,"板块","涨跌幅",1,"fixture","industry",amount_multiplier=1)
        self.assertEqual(top[0]["amount"],100000000)
        self.assertEqual(bottom[0]["amount"],200000000)
    def test_required_dates(self):
        self.assertIsNone(q.breadth_from_frame(frame(),"fixture",known_date="2026-11-26",require_row_date=True))
    def test_model(self):
        m=q.financial_market_model(fixture())
        self.assertEqual(m["breadth"]["up_count"]["value"],1)
        self.assertEqual(m["breadth"]["up_count"]["quality"]["status"],"UNVERIFIED")
        self.assertEqual(len(m["indices"]),6)
    def test_archive_model(self):
        from src.raw_data_archive import archive_market,verify
        with tempfile.TemporaryDirectory() as tmp:
            data=fixture()
            data["financial_market_data"]=q.financial_market_model(data)
            p=archive_market(Path(tmp),region="cn",query_id="model",payload=data)
            self.assertEqual(json.loads((p/"market_data.json").read_text())["financial_market_data"],data["financial_market_data"])
            self.assertTrue(verify(Path(tmp)))
    def test_market_light_actual_scoring(self):
        from src.market_analyzer import MarketAnalyzer, MarketOverview, MarketIndex
        from src.core.market_profile import get_profile
        from types import SimpleNamespace
        analyzer=MarketAnalyzer.__new__(MarketAnalyzer)
        analyzer.region="cn"
        analyzer.profile=get_profile("cn")
        analyzer.config=SimpleNamespace(report_language="zh")
        indices=[MarketIndex(code=code,name=name,change_pct=2,date_status="CURRENT_DATE",trade_date="2026-11-26") for code,name in q.INDEX_NAMES.items()]
        for label,rows,quality,expected in [
            ("missing",[],{"status":"UNAVAILABLE"},"unavailable"),
            ("index_only",indices,{"coverage_verified":False},"partial"),
            ("missing_limits",indices,{"coverage_verified":True,"date_status":"CURRENT_DATE","limit_up_count":None,"limit_down_count":None},"partial"),
            ("complete",indices,{"coverage_verified":True,"date_status":"CURRENT_DATE","limit_up_count":80,"limit_down_count":20},"ok"),
        ]:
            with self.subTest(label=label):
                o=MarketOverview(date="2026-11-26",indices=rows,breadth_quality=quality,
                    up_count=800 if rows else 0,down_count=200 if rows else 0,
                    limit_up_count=80 if label=="complete" else 0,limit_down_count=20 if label=="complete" else 0)
                scores=analyzer._build_market_light_scores(o)
                snapshot=analyzer.build_market_light_snapshot(o)
                self.assertEqual(scores["data_quality"],expected)
                self.assertEqual(snapshot["data_quality"],expected)
                self.assertEqual(snapshot["dimensions"],scores["dimensions"])
                if expected!="ok":
                    self.assertEqual(snapshot["status"],"yellow")
                    self.assertEqual(scores["temperature_label"],"数据不完整")
                    self.assertEqual(snapshot["temperature_label"],"数据不完整")
                    self.assertFalse(scores["dimensions"]["limit"]["available"])
                else:
                    self.assertEqual(snapshot["status"],"green")
                    self.assertEqual(scores["score"],78)
                    self.assertTrue(all(x["available"] for x in scores["dimensions"].values()))
    def test_pool_model_archive_sync(self):
        from src.raw_data_archive import archive_market,verify
        from src.market_analyzer import MarketAnalyzer,MarketOverview
        from src.core.market_profile import get_profile
        from types import SimpleNamespace
        analyzer=MarketAnalyzer.__new__(MarketAnalyzer)
        analyzer.region="cn"
        analyzer.profile=get_profile("cn")
        analyzer.config=SimpleNamespace(report_language="zh")
        pools={"up":q.limit_pool(pd.DataFrame({"代码":["002938","300394"]}),"2026-11-26","up"),
               "down":q.limit_pool(pd.DataFrame(),"2026-11-26","down")}
        o=MarketOverview(date="2026-11-26",breadth_quality={"status":"PARTIAL","limit_pools":pools})
        payload=analyzer.build_market_review_payload(o,[],"# 固定样本")
        self.assertEqual(payload["financial_market_data"]["limit_pools"],pools)
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)
            p=archive_market(root,region="cn",query_id="pool-sync",payload=payload)
            data=json.loads((p/"market_data.json").read_text())
            self.assertEqual(data["financial_market_data"]["limit_pools"],data["breadth"]["limit_pools"])
            self.assertEqual(data["breadth"]["limit_pools"],pools)
            self.assertTrue(verify(root))
    def test_archive(self):
        from src.raw_data_archive import archive_market,verify
        with tempfile.TemporaryDirectory() as tmp:
            p=archive_market(Path(tmp),region="cn",query_id="fixture",payload=fixture())
            data=json.loads((p/"market_data.json").read_text())
            self.assertEqual(data["market_data_quality_version"],"2.4.4")
            self.assertEqual(data["breadth"]["provider"],"fixture")

def render_fixture(output):
    from src.financial_dashboard import market_pages,render_pages
    out=Path(output); out.mkdir(parents=True,exist_ok=True)
    with patch.object(q,"date_state",return_value="CURRENT_DATE"):
        p=fixture()
    p["breadth"]["limit_pools"]={"up":{"count":2,"status":"UNVERIFIED","requested_trade_date":"2026-11-26"},"down":{"count":None,"status":"UNAVAILABLE","requested_trade_date":"2026-11-26"}}
    p["financial_market_data"]=q.financial_market_model(p)
    assert p["financial_market_data"]["limit_pools"]==p["breadth"]["limit_pools"]
    (out/"structured-market.json").write_text(json.dumps(p,ensure_ascii=False,indent=2),encoding="utf8")
    render_pages(market_pages("",p),out)
    p=dict(p,indices=[],breadth={"status":"UNAVAILABLE"},sectors={},concepts={})
    p["financial_market_data"]=q.financial_market_model(p)
    render_pages([("missing-"+n,h) for n,h in market_pages("",p)],out)
    partial=dict(p,breadth={"status":"PARTIAL","up_count":2,"down_count":1,"flat_count":0})
    partial["financial_market_data"]=q.financial_market_model(partial)
    render_pages([("partial-"+n,h) for n,h in market_pages("",partial)],out)
