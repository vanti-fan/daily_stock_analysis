"""Phase 2.4.6: deterministic A-share news admission, routing and event records."""
from __future__ import annotations
import os
import re
import hashlib
import unicodedata
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo
from urllib.parse import urlsplit, urlunsplit, parse_qsl, urlencode
from dataclasses import replace

TIERS={
"L1":("sse.com.cn","szse.cn","bse.cn","cninfo.com.cn","csrc.gov.cn","pbc.gov.cn"),
"L2":("cls.cn","stcn.com","cs.com.cn","cnstock.com","yicai.com","21jingji.com"),
"L3":("eastmoney.com","10jqka.com.cn","qq.com","sina.com.cn","sohu.com","163.com"),
"L4":("xueqiu.com","guba.eastmoney.com","guba.sina.com.cn","guba.10jqka.com.cn","tieba.baidu.com"),
}
# Broad portal roots are not automatically financial publishers.
PORTALS={"qq.com":("finance.qq.com","stock.qq.com","new.qq.com"),
         "sina.com.cn":("finance.sina.com.cn",),"sohu.com":("business.sohu.com","finance.sohu.com","www.sohu.com"),
         "163.com":("money.163.com","stock.163.com","www.163.com")}
TRACKING_SAFE={"id","articleid","article_id","newsid","announcementid","announcement_id","docid","stockcode","code","page"}
MATERIAL=re.compile(r"减持|处罚|立案|诉讼|停牌|退市|业绩预告|业绩快报|财报|重大合同|并购|收购|回购|分红|重组|亏损|违约|订单")
EVENT_DATE=re.compile(r"(?:事件日期|发生日期|发生于|生效日期|实施日期)[：:\s]*((?:20\d{2})[-年]\d{1,2}[-月]\d{1,2}日?)")
def enabled():
    return os.getenv("DSA_NEWS_SOURCE_QUALITY","").lower()=="true"

def eligible(code):
    from src.intelligent_fallback import ordinary_a_share
    return ordinary_a_share(code)

def today_cn():
    return datetime.now(ZoneInfo("Asia/Shanghai")).date()

def safe_url(value):
    try:
        p=urlsplit(str(value or "").strip())
        h=(p.hostname or "").lower().rstrip(".")
        if p.scheme not in ("http","https") or not h or p.username or p.password or p.port not in (None,80,443): return None
        if not re.fullmatch(r"[a-z0-9.-]+",h) or "." not in h: return None
        if not p.path or p.path=="/" and not p.query: return None
        # Retain known article identifiers only; strip tracking, credentials and fragments.
        pairs=[(k,v) for k,v in parse_qsl(p.query,keep_blank_values=False)
               if k.lower() in TRACKING_SAFE and re.fullmatch(r"[\w.-]{1,100}",v)]
        if len(p.path)>600 or re.search(r"[\x00-\x20<>]",p.path): return None
        return urlunsplit((p.scheme,h,p.path,urlencode(sorted(pairs)),""))
    except (ValueError,TypeError): return None

def domain_match(host,root):
    return host==root or host.endswith("."+root)

def tier(url):
    u=safe_url(url)
    if not u: return "UNKNOWN"
    h=urlsplit(u).hostname
    for root in TIERS["L4"]:
        if domain_match(h,root): return "L4"
    for level in ("L1","L2","L3"):
        for root in TIERS[level]:
            if domain_match(h,root):
                if root in PORTALS and not any(domain_match(h,x) for x in PORTALS[root]): return "UNKNOWN"
                return level
    return "UNKNOWN"

def day(value):
    # Do not resolve relative dates against today's clock; cached dates must remain stable.
    try:
        s=str(value or "").strip()
        if not re.match(r"^20\d{2}-\d{2}-\d{2}(?:$|T|\s)",s): return None
        return date.fromisoformat(s[:10])
    except ValueError: return None

def event_day(text):
    matches=set()
    for raw in EVENT_DATE.findall(text):
        try:
            y,m,d=map(int,re.findall(r"\d+",raw))
            matches.add(date(y,m,d).isoformat())
        except ValueError: pass
    return next(iter(matches)) if len(matches)==1 else None

def clean(value,limit=500):
    return re.sub(r"\s+"," ",unicodedata.normalize("NFKC",str(value or ""))).strip()[:limit]

def title_key(title):
    return re.sub(r"[\W_]+","",clean(title,240).lower())

def direct_identity(title,snippet,code,name):
    text=title+" "+snippet
    return bool(re.search(r"(?<!\d)"+re.escape(code)+r"(?!\d)",text) or
                (name and len(name)>=3 and name in text))

def event_record(item,provider,code,name,dimension,days,asof):
    url=safe_url(item.url)
    level=tier(url)
    title=clean(item.title,240)
    snippet=clean(item.snippet,500)
    pub=day(item.published_date)
    if not url or level=="UNKNOWN" or not title: return None,"unreviewed_source_or_url"
    if not pub or not asof-timedelta(days=max(0,days-1))<=pub<=asof: return None,"publication_date_outside_window"
    if not direct_identity(title,snippet,code,name): return None,"identity_not_in_article_text"
    issuer=set(re.findall(r"(?:证券代码|股票代码)[：:\s]*(\d{6})",title+" "+snippet))
    if issuer and code not in issuer: return None,"issuer_conflict"
    material=bool(MATERIAL.search(title+" "+snippet))
    # L1 indicates official-domain evidence, not independent verification of the full claim.
    status="COMMUNITY_ONLY" if level=="L4" else "OFFICIAL_SOURCE_REFERENCE" if level=="L1" else "NEEDS_OFFICIAL_CONFIRMATION" if material else "MEDIA_REPORT_UNVERIFIED"
    evday=event_day(title+" "+snippet)
    return {"title":title,"snippet":snippet,"url":url,"publisher_domain":urlsplit(url).hostname,
            "source_tier":level,"search_provider":provider,"published_date":pub.isoformat(),
            "event_date":evday,"event_date_status":"EXPLICIT_TEXT_UNVERIFIED" if evday else "UNKNOWN",
            "retrieved_at":getattr(item,"retrieved_at",None),"stock_code":code,"stock_name":name,
            "dimension":dimension,"relevance":"DIRECT_TEXT_MATCH","material_candidate":material,
            "verification":status,"publication_date_basis":"SEARCH_PROVIDER_METADATA"},None

def dedup(events):
    """Conservative same-URL or normalized headline+publication day grouping.
    Keep alternate evidence; no fuzzy merger of distinct corporate announcements.
    """
    groups=[]
    for e in sorted(events,key=lambda e:(e["source_tier"],e["published_date"],e["url"])):
        key=(e["stock_code"],e["published_date"],title_key(e["title"]))
        match=next((g for g in groups if e["url"] in g["evidence_urls"] or key in g["_keys"]),None)
        evidence={k:e.get(k) for k in ("url","publisher_domain","source_tier","search_provider","published_date","dimension","verification")}
        if match:
            if evidence not in match["evidence"]: match["evidence"].append(evidence)
            if e["url"] not in match["evidence_urls"]: match["evidence_urls"].append(e["url"])
            if key not in match["_keys"]: match["_keys"].append(key)
            continue
        groups.append(dict(e,event_id=hashlib.sha256(("|".join(key)).encode()).hexdigest()[:20],
                           evidence=[evidence],evidence_urls=[e["url"]],_keys=[key]))
    for g in groups: g.pop("_keys",None)
    return groups

def route(service,code,name,query,dimension,days,max_results,budget=2):
    """Reuse existing provider clients. Budget is logical provider invocations, not HTTP retries."""
    from src.search_service import SearchResponse, TavilySearchProvider, BraveSearchProvider
    from src.services.run_diagnostics import record_provider_run_started
    from src.search_service import raise_if_stock_cancelled
    import time
    attempts=[]; accepted=[]; used=0; had_success=False
    asof=today_cn()
    providers=sorted((p for p in service._providers if p.name.lower() in ("tavily","brave") and p.is_available),
                     key=lambda p:p.name.lower()!="tavily")
    for p in providers:
        if used>=budget: break
        raise_if_stock_cancelled()
        used+=1
        started=time.monotonic()
        record_provider_run_started(data_type="news_search",provider=p.name,operation="news_quality_search")
        try:
            kwargs={"topic":"news"} if isinstance(p,TavilySearchProvider) else service._brave_search_locale(code,prefer_chinese=True) if isinstance(p,BraveSearchProvider) else {}
            raw=p.search(query,service._provider_request_size(max_results),days=days,**kwargs)
            raise_if_stock_cancelled()
            had_success=had_success or bool(raw.success)
            rejected={}; admitted=[]
            if raw.success:
                for item in raw.results:
                    event,reason=event_record(item,p.name,code,name,dimension,days,asof)
                    if event: admitted.append(event)
                    else: rejected[reason]=rejected.get(reason,0)+1
            status="AVAILABLE" if admitted else "NO_USABLE_RESULTS" if raw.success else "SEARCH_FAILED"
            attempts.append({"provider":p.name,"status":status,"returned":len(raw.results or []),"admitted":len(admitted),"rejected":rejected})
            accepted.extend(admitted)
            service._record_news_search_run(provider=p.name,operation="news_quality_search",success=bool(raw.success),
                latency_ms=service._elapsed_ms(started),record_count=len(admitted),error_type=None if raw.success else "SearchFailed")
            # Community-only or unconfirmed material reports justify fallback within budget.
            if any(e["source_tier"] in ("L1","L2","L3") and e["verification"]!="NEEDS_OFFICIAL_CONFIRMATION" for e in admitted): break
        except Exception as exc:
            raise_if_stock_cancelled()
            attempts.append({"provider":p.name,"status":"SEARCH_FAILED","error_type":type(exc).__name__})
            service._record_news_search_run(provider=p.name,operation="news_quality_search",success=False,
                latency_ms=service._elapsed_ms(started),record_count=0,error_type=type(exc).__name__)
    events=dedup(accepted)[:max_results]
    # Community opinions are never promoted into factual AI news context.
    from src.search_service import SearchResult
    results=[SearchResult(title=e["title"],snippet=e["snippet"],url=e["url"],source=e["publisher_domain"],
                published_date=e["published_date"],retrieved_at=e["retrieved_at"],
                relevance_category="direct_company_news",relevance_score=None,
                relevance_reasons=[e["source_tier"],e["verification"],"事件日期："+(e["event_date"] or "未知")])
             for e in events if e["source_tier"]!="L4"]
    status="AVAILABLE" if results else "COMMUNITY_ONLY" if events else "NO_USABLE_RESULTS" if had_success else "SEARCH_FAILED" if used else "NOT_CONFIGURED"
    response=SearchResponse(query=query,results=results,provider=events[0]["search_provider"] if events else "Filtered" if had_success else "None",
                            success=had_success,error_message=None if had_success else status)
    response.news_quality={"schema_version":"1.0","stock_code":code,"stock_name":name,"dimension":dimension,
                           "status":status,"as_of":asof.isoformat(),"window_days":days,"attempts":attempts,"events":events}
    return response,used

def archive_response(response,code):
    from src.services.run_diagnostics import get_current_diagnostic_context
    from src.raw_data_archive import record
    ctx=get_current_diagnostic_context()
    if ctx and ctx.query_id and ctx.stock_code==code:
        data=getattr(response,"news_quality",None)
        if data: record("news",code=code,query_id=ctx.query_id,payload=data)

def audit():
    return {"schema_version":"1.0","baseline":"0299b5c","scope":"ordinary Shanghai/Shenzhen equities",
            "providers":["Tavily","Brave"],"bocha":"excluded_strict_route",
            "source_domains":TIERS,"limits":["Source tier is not proof of article truth",
            "Company-owned websites need separately verified domain mapping; unknown domains excluded",
            "Publication metadata is not event date","Exact URL/headline dedup only",
            "No additional LLM calls; provider retry behavior unchanged","No live quota test"]}
