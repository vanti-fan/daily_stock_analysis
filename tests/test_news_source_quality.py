import json
import os
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest.mock import patch
from src import news_source_quality as nq
from src.search_service import SearchService, SearchResult, SearchResponse

CODE="002938"
NAME="鹏鼎控股"
DAY=date(2026,11,27)

def item(url="https://www.cninfo.com.cn/article/123",title=NAME+"经营情况公告",pub="2026-11-27",snippet="事件日期：2026年11月25日"):
    return SearchResult(title=title,snippet=snippet,url=url,source="untrusted label",published_date=pub)

def response(provider,items=(),success=True):
    return SearchResponse(query="fixture",provider=provider,results=list(items),success=success)

def service():
    s=SearchService(tavily_keys=["fixture-only"],brave_keys=["fixture-only"],searxng_public_instances_enabled=False)
    return s

def providers(s):
    return {p.name:p for p in s._providers}

def event(x,dimension="latest_news",provider="Tavily"):
    return nq.event_record(x,provider,CODE,NAME,dimension,3,DAY)[0]

def fixture(dimension="latest_news",items=None):
    events=nq.dedup([event(x,dimension) for x in (items if items is not None else [item()])])
    return dict(schema_version="1.0",stock_code=CODE,stock_name=NAME,dimension=dimension,
                status="AVAILABLE" if any(e["source_tier"]!="L4" for e in events) else "COMMUNITY_ONLY" if events else "NO_USABLE_RESULTS",
                as_of=DAY.isoformat(),window_days=3,attempts=[dict(provider="Tavily",status="AVAILABLE" if events else "NO_USABLE_RESULTS",admitted=len(events))],events=events)

class NewsQualityTest(unittest.TestCase):
    def setUp(self):
        env=patch.dict(os.environ,{"DSA_NEWS_SOURCE_QUALITY":"true","DSA_RAW_DATA_ARCHIVE":"true","DSA_PROVIDER_HEALTH_JOURNAL":""})
        env.start();self.addCleanup(env.stop)
        clock=patch.object(nq,"today_cn",return_value=DAY)
        clock.start();self.addCleanup(clock.stop)
        net=patch("requests.sessions.Session.request",side_effect=AssertionError("unexpected network request"))
        net.start();self.addCleanup(net.stop)
    def test_tiers(self):
        for url,level in [("https://www.sse.com.cn/a","L1"),("https://www.cls.cn/a","L2"),("https://finance.sina.com.cn/a","L3"),("https://guba.eastmoney.com/a","L4"),("https://xueqiu.com/a","L4"),("https://cninfo.com.cn.evil.test/a","UNKNOWN"),("https://evil.test/a","UNKNOWN")]:
            self.assertEqual(nq.tier(url),level)
    def test_url(self):
        self.assertEqual(nq.safe_url("https://www.cninfo.com.cn/a?id=123&utm_source=x#abc"),"https://www.cninfo.com.cn/a?id=123")
        self.assertIsNone(nq.safe_url("https://user:secret@cninfo.com.cn/a"))
        self.assertIsNone(nq.safe_url("https://cninfo.com.cn/?utm_source=x"))
        self.assertIsNone(nq.safe_url("javascript:alert(1)"))
    def test_identity(self):
        self.assertIsNone(event(item(title="其他公司公告",snippet="股票代码：300394")))
        self.assertIsNone(event(item(snippet="证券代码：300394")))
        self.assertIsNotNone(event(item(title="002938经营情况",snippet="")))
    def test_dates(self):
        for d in ("2026-11-24","2026-11-28","昨天",None):
            self.assertIsNone(event(item(pub=d)))
        self.assertEqual(event(item())["event_date"],"2026-11-25")
        self.assertIsNone(event(item(snippet="没有明确事件日"))["event_date"])
    def test_material(self):
        e=event(item(url="https://www.cls.cn/a",title=NAME+"减持报道"))
        self.assertEqual(e["verification"],"NEEDS_OFFICIAL_CONFIRMATION")
    def test_community(self):
        e=event(item(url="https://xueqiu.com/a"))
        self.assertEqual(e["verification"],"COMMUNITY_ONLY")
        self.assertEqual(nq.as_results([e]),[])
    def test_dedup(self):
        a=event(item(),"latest_news")
        b=event(item(url="https://www.cls.cn/a"),"announcements","Brave")
        g=nq.dedup([a,b])
        self.assertEqual(len(g),1)
        self.assertEqual(g[0]["source_tier"],"L1")
        self.assertEqual(len(g[0]["evidence"]),2)
        self.assertEqual(set(g[0]["dimensions"]),{"latest_news","announcements"})
    def test_distinct_announcements(self):
        self.assertEqual(len(nq.dedup([event(item()),event(item(url="https://cninfo.com.cn/b",title=NAME+"另一公告"))])),2)
    def test_primary_success_and_cache(self):
        s=service();p=providers(s)
        with patch.object(p["Tavily"],"search",return_value=response("Tavily",[item()])) as t,patch.object(p["Brave"],"search") as b:
            a=s.search_stock_news(CODE,NAME)
            c=s.search_stock_news(CODE,NAME)
            self.assertEqual(a.news_quality["status"],"AVAILABLE")
            self.assertEqual(c.results[0].retrieved_at,a.results[0].retrieved_at)
            t.assert_called_once();b.assert_not_called()
    def test_fallback(self):
        s=service();p=providers(s)
        with patch.object(p["Tavily"],"search",return_value=response("Tavily",success=False)) as t,patch.object(p["Brave"],"search",return_value=response("Brave",[item()])) as b:
            a=s.search_stock_news(CODE,NAME)
            self.assertEqual(a.news_quality["status"],"AVAILABLE")
            self.assertEqual(a.results[0].url,item().url)
            t.assert_called_once();b.assert_called_once()
    def test_statuses(self):
        for ok,expected in ((True,"NO_USABLE_RESULTS"),(False,"SEARCH_FAILED")):
            s=service();p=providers(s)
            with patch.object(p["Tavily"],"search",return_value=response("Tavily",success=ok)),patch.object(p["Brave"],"search",return_value=response("Brave",success=ok)):
                self.assertEqual(s.search_stock_news(CODE,NAME).news_quality["status"],expected)
        s=SearchService(searxng_public_instances_enabled=False)
        self.assertEqual(s.search_stock_news(CODE,NAME).news_quality["status"],"NOT_CONFIGURED")
    def test_budget_and_cross_dimension(self):
        s=service();p=providers(s)
        with patch.object(p["Tavily"],"search",side_effect=lambda *a,**k:response("Tavily",[item()])) as t,patch.object(p["Brave"],"search") as b:
            out=s.search_comprehensive_intel(CODE,NAME,max_searches=3)
            self.assertEqual(t.call_count,3);b.assert_not_called()
            self.assertEqual(sum(len(x.results) for x in out.values()),1)
            self.assertEqual(sum(len(x.news_quality["events"]) for x in out.values()),3)
    def test_archive_current_query(self):
        from src.raw_data_archive import archive_news,verify
        from src.data_quality_manager import assess_stock
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)
            archive_news(root,code=CODE,query_id="current",payload=fixture())
            archive_news(root,code=CODE,query_id="current",payload=fixture("announcements"))
            archive_news(root,code=CODE,query_id="old",payload=fixture(items=[item(title=NAME+"旧批次",url="https://cninfo.com.cn/old")]))
            q=assess_stock(root,verify(root),dict(code=CODE,query_id="current",status="TIMEOUT"))
            self.assertEqual(q["news_quality"]["event_count"],1)
            self.assertEqual(q["ai_status"],"NOT_COMPLETED")
            self.assertEqual(len(q["news_quality"]["events"][0]["dimensions"]),2)
    def test_archive_invalid(self):
        from src.raw_data_archive import archive_news,verify
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);p=fixture();p["events"][0]["source_tier"]="L4"
            archive_news(root,code=CODE,query_id="bad",payload=p)
            self.assertEqual(nq.assess_archive(root,verify(root),CODE,"bad")["status"],"INVALID_ARCHIVE")
    def test_archive_hook(self):
        from src.raw_data_archive import verify
        from src.services.run_diagnostics import activate_run_diagnostic_context,reset_run_diagnostic_context
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);s=service();p=providers(s)
            token=activate_run_diagnostic_context(query_id="hook",stock_code=CODE)
            try:
                with patch("src.raw_data_archive.archive_root",return_value=root),patch.object(p["Tavily"],"search",return_value=response("Tavily",[item()])):
                    s.search_stock_news(CODE,NAME)
                self.assertEqual(nq.assess_archive(root,verify(root),CODE,"hook")["event_count"],1)
            finally: reset_run_diagnostic_context(token)
    def test_agent(self):
        from src.agent.tools.search_tools import _handle_search_stock_news
        s=service();p=providers(s)
        with patch.object(p["Tavily"],"search",return_value=response("Tavily",[item()])),patch("src.agent.tools.search_tools._get_search_service",return_value=s),patch("src.agent.tools.search_tools._resolve_search_subject",return_value=(CODE,NAME)),patch("src.agent.tools.search_tools._stock_run_cancelled",return_value=False),patch("src.agent.tools.search_tools._persist_news_response"),patch("src.agent.tools.search_tools.record_news_evidence"):
            out=_handle_search_stock_news(CODE,NAME)
            self.assertEqual(out["news_status"],"AVAILABLE")
            self.assertEqual(out["results"][0]["source_tier"],"L1")
            self.assertEqual(out["results"][0]["event_date"],"2026-11-25")
    def test_dashboard_timeout(self):
        from src.raw_data_archive import archive_news,verify
        from src.data_quality_manager import assess_stock
        from src.financial_dashboard import stock_pages
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);archive_news(root,code=CODE,query_id="current",payload=fixture())
            row=dict(code=CODE,name=NAME,query_id="current",status="TIMEOUT")
            row["data_quality"]=assess_stock(root,verify(root),row)
            pages=dict(stock_pages(None,[row],[],""))
            self.assertIn(NAME+"经营情况公告",pages["stock-002938-news-01"])
            self.assertNotIn("可进攻",pages["stock-002938-news-01"])

def render_fixture(output):
    from src.raw_data_archive import archive_news,verify
    from src.financial_dashboard import news_pages,render_pages
    out=Path(output);out.mkdir(parents=True,exist_ok=True)
    pages=[]
    with patch.object(nq,"today_cn",return_value=DAY):
        for label,items,status in (("available",[item(),item(url="https://www.cls.cn/a",title=NAME+"减持报道"),item(url="https://xueqiu.com/a",title=NAME+"投资者讨论")],None),("empty",[],None),("failed",[],"SEARCH_FAILED"),("community",[item(url="https://xueqiu.com/a")],None)):
            root=out/label/"raw";p=fixture(items=items)
            if status:
                p["status"]=status;p["attempts"]=[dict(provider="Tavily",status=status)]
            archive_news(root,code=CODE,query_id=label,payload=p)
            q=nq.assess_archive(root,verify(root),CODE,label)
            assert q["status"]==p["status"],q
            (out/(label+"-quality.json")).write_text(json.dumps(q,ensure_ascii=False,indent=2),encoding="utf8")
            pages.extend((label+"-"+name,html) for name,html in news_pages(CODE,{"name":NAME,"news_quality":q},DAY.isoformat()))
        render_pages(pages,out)
    (out/"source-audit.json").write_text(json.dumps(nq.audit(),ensure_ascii=False,indent=2),encoding="utf8")
