"""Phase 2.4.4: A-share market observations, independent of AI output."""
from __future__ import annotations
import math
import hashlib
import os
import re
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

INDEX_NAMES = {"sh000001":"上证指数","sz399001":"深证成指","sz399006":"创业板指",
               "sh000688":"科创50","sh000016":"上证50","sh000300":"沪深300"}
BREADTH_KEYS = ("up_count","down_count","flat_count","limit_up_count","limit_down_count","total_amount")
MIN_UNIVERSE = 3000  # plausibility floor, NOT a proof of full exchange coverage
SCOPE = "provider_returned_A_share_universe"
GROUPS = {"AkshareFetcher":"MULTI","EfinanceFetcher":"eastmoney","TushareFetcher":"MULTI","TickFlowFetcher":"tickflow"}

def enabled():
    return os.getenv("DSA_MARKET_DATA_QUALITY","").lower() == "true"

def number(value):
    if value is None or isinstance(value,bool): return None
    try:
        n=float(str(value).strip().replace(",","").removesuffix("%"))
        return n if math.isfinite(n) else None
    except (ValueError,TypeError): return None

def trade_day(value):
    s=str(value or "").strip()
    try:
        return (datetime.strptime(s,"%Y%m%d").date() if re.fullmatch(r"\d{8}",s)
                else date.fromisoformat(s[:10])).isoformat()
    except ValueError: return None

def china_now():
    return datetime.now(ZoneInfo("Asia/Shanghai"))

def date_state(value):
    d=trade_day(value)
    if not d: return "UNVERIFIED"
    today=china_now().date().isoformat()
    if d>today: return "INVALID"
    expected=expected_session()
    if expected is None: return "UNVERIFIED"
    return "CURRENT_DATE" if d==expected else "PRIOR_DATE" if d<expected else "INVALID"

def session_dates(now=None):
    from src.core.trading_calendar import get_trading_dates
    now=now or china_now()
    days=get_trading_dates("cn",now.date()-timedelta(days=30),now.date())
    return [d.date().isoformat() for d in days] if days is not None else []

def expected_session(now=None):
    now=now or china_now()
    days=session_dates(now)
    if days and days[-1]==now.date().isoformat() and now.strftime("%H:%M")<"09:30":
        days=days[:-1]
    return days[-1] if days else None

def equity_code(value):
    s=str(value).strip().lower()
    m=re.fullmatch(r"(?:(sh|sz|bj)[.]?)?(\d{6})(?:[.](sh|sz|bj))?",s)
    if not m: return None
    prefix,code,suffix=m.groups()
    exchange=("sh" if code.startswith(("600","601","603","605","688","689")) else
              "sz" if code.startswith(("000","001","002","003","300","301")) else
              "bj" if code.startswith(("43","83","87","88","92")) else None)
    if not exchange or any(x and x!=exchange for x in (prefix,suffix)): return None
    return exchange+code

def breadth_from_frame(df, source, *, amount_multiplier=1, min_universe=MIN_UNIVERSE, known_date=None, require_row_date=False, completed_session=False):
    """Deduplicate; count valid traded shares; never estimate exchange limit rules.
    The provider universe is not an independently verified exchange master list.
    """
    if df is None or df.empty: return None
    aliases={"code":("代码","股票代码","ts_code","stock_code","symbol"),
             "price":("最新价","close","lastPrice","last_price"),
             "pre":("昨收","昨日收盘","pre_close","lastClose","prev_close"),
             "amount":("成交额","amount"),"date":("trade_date","交易日期","quote_date"),
             "upper":("涨停价","up_limit","limit_up_price"),"lower":("跌停价","down_limit","limit_down_price")}
    cols={k:next((v for v in vals if v in df.columns),None) for k,vals in aliases.items()}
    if not all(cols[k] for k in ("code","price","pre")): return None
    seen={}; duplicates=set(); dates=set(); missing_dates=0
    for row in df.to_dict("records"):
        code=equity_code(row.get(cols["code"]))
        if not code: continue
        d=trade_day(row.get(cols["date"]))
        if d: dates.add(d)
        else: missing_dates+=1
        vals=tuple(number(row.get(cols[k])) for k in ("price","pre","amount","upper","lower"))
        if code in seen and seen[code]!=vals: duplicates.add(code)
        seen[code]=vals
    if len(dates)>1 or (known_date and dates and dates!={trade_day(known_date)}): return None
    if require_row_date and (missing_dates or not dates): return None
    day=trade_day(known_date) or (next(iter(dates),None) if not missing_dates else None)
    if date_state(day)=="INVALID": return None
    up=down=flat=limit_up=limit_down=limit_eligible=amount_valid=valid=suspended=0
    amount=0.0
    for code,(p,pre,a,upper,lower) in seen.items():
        if code in duplicates: continue
        if a is not None and a==0:
            suspended+=1; amount_valid+=1; continue
        if p is None or pre is None or p<=0 or pre<=0 or (a is not None and a<0): continue
        valid+=1
        up+=int(p>pre); down+=int(p<pre); flat+=int(p==pre)
        if a is not None:
            if amount_multiplier is not None: amount+=a*amount_multiplier; amount_valid+=1
        if upper is not None and lower is not None and 0<lower<=pre<=upper and upper>lower:
            limit_eligible+=1
            limit_up+=int(abs(p-upper)<0.000001); limit_down+=int(abs(p-lower)<0.000001)
    if not valid: return None
    universe=len(seen)
    coverage=valid+suspended
    sufficient=universe>=min_universe and coverage/universe>=.95 and not duplicates
    complete_amount=sufficient and amount_multiplier is not None and amount_valid==universe and amount>0
    complete_limits=sufficient and limit_eligible==valid
    return {"up_count":up,"down_count":down,"flat_count":flat,
            "limit_up_count":limit_up if complete_limits else None,
            "limit_down_count":limit_down if complete_limits else None,
            "total_amount":amount/1e8 if complete_amount else None,
            "sample_amount":amount/1e8 if amount_valid and amount_multiplier is not None else None,
            "previous_total_amount":None,"turnover_change_pct":None,
            "universe_hash":hashlib.sha256("\n".join(sorted(seen)).encode()).hexdigest(),
            "previous_trade_date":None,"completed_session":bool(completed_session),
            "provider":source,"trade_date":day,"date_status":date_state(day),
            "observed_at":china_now().isoformat(),"scope":SCOPE,"turnover_unit":"CNY_100M",
            "universe_count":universe,"valid_count":valid,"zero_amount_count":suspended,
            "invalid_count":universe-coverage,"duplicate_conflicts":len(duplicates),
            "amount_coverage":amount_valid/universe,"limit_coverage":limit_eligible/valid,
            "status":"PARTIAL" if not sufficient or not complete_amount or not complete_limits else "UNVERIFIED",
            "breadth_usable":sufficient,"coverage_verified":False}

def normalize_stats(data, provider):
    if not isinstance(data,dict): return None
    d=dict(data)
    for k in BREADTH_KEYS:
        n=number(d.get(k))
        d[k]=(int(n) if k!="total_amount" else n) if n is not None and n>=0 and (k=="total_amount" or n.is_integer()) else None
    counts=[d[k] for k in BREADTH_KEYS[:3]]
    if any(v is None for v in counts) or not sum(counts): return None
    if d["limit_up_count"] is not None and d["limit_up_count"]>d["up_count"]: d["limit_up_count"]=None
    if d["limit_down_count"] is not None and d["limit_down_count"]>d["down_count"]: d["limit_down_count"]=None
    if not d.get("scope"):
        # Legacy provider aggregate has no verified universe or limit/turnover coverage.
        d.update(scope="unknown",breadth_usable=False,coverage_verified=False,
                 status="UNVERIFIED",total_amount=None,limit_up_count=None,limit_down_count=None)
    if not d["total_amount"]: d["total_amount"]=None
    d["provider"]=d.get("provider") or provider
    d["trade_date"]=trade_day(d.get("trade_date"))
    d["date_status"]=date_state(d["trade_date"])
    if d["date_status"]=="INVALID": return None
    d["observed_at"]=d.get("observed_at") or china_now().isoformat()
    if not d.get("previous_trade_date") or not d.get("completed_session"):
        d["turnover_change_pct"]=None
    return d

def index_rows(data, provider):
    if not isinstance(data,(list,tuple)): return []
    out={}; bad=set()
    for row in data:
        if not isinstance(row,dict): continue
        c=str(row.get("code","")).lower()
        if re.fullmatch(r"\d{6}\.(sh|sz)",c): c=c[-2:]+c[:6]
        if c not in INDEX_NAMES:
            # Bare index codes are accepted only from an index-specific API and matching name.
            c=next((k for k,v in INDEX_NAMES.items() if k[2:]==c and row.get("name")==v),None)
        if c not in INDEX_NAMES or row.get("name")!=INDEX_NAMES[c]: continue
        p,pre=number(row.get("current")),number(row.get("prev_close"))
        if p is None or pre is None or min(p,pre)<=0: continue
        change=(p/pre-1)*100
        supplied=number(row.get("change_pct"))
        if supplied is not None and abs(change-supplied)>.12: continue
        d=trade_day(row.get("trade_date"))
        if date_state(d)=="INVALID": continue
        item=dict(row,code=c,current=p,prev_close=pre,change=p-pre,change_pct=round(change,4),
                  source=row.get("source") or provider,trade_date=d,date_status=date_state(d))
        for key in ("open","high","low","volume","amount","amplitude"):
            n=number(row.get(key))
            item[key]=n if n is not None and n>0 else None
        if item["high"] is not None and item["low"] is not None:
            if not item["low"]<=p<=item["high"] or (item["open"] is not None and not item["low"]<=item["open"]<=item["high"]):
                for k in ("open","high","low","amplitude"): item[k]=None
        if c in out and out[c]!=item: bad.add(c)
        out[c]=item
    rows=[out[k] for k in INDEX_NAMES if k in out and k not in bad]
    dates={r["trade_date"] for r in rows if r["trade_date"]}
    if len(dates)>1 or (dates and any(not r["trade_date"] for r in rows)): return []
    return rows if len({r["source"] for r in rows})<=1 else []

def rank_frame(df,name_col,change_col,n,source,category,*,known_date=None,amount_multiplier=None):
    if df is None or df.empty or not {name_col,change_col}.issubset(df.columns): return None
    rows=[]
    dates=set()
    for row in df.to_dict("records"):
        name=row.get(name_col)
        pct=number(row.get(change_col))
        if not isinstance(name,str) or not name.strip() or pct is None: continue
        d=trade_day(row.get("trade_date") or row.get("交易日期"))
        if d: dates.add(d)
        amount=number(row.get("成交额",row.get("amount"))) if amount_multiplier is not None else None
        rows.append(dict(name=name.strip(),change_pct=pct,category=category,source=source,
                         classification=source,trade_date=d or trade_day(known_date),
                         amount=amount*amount_multiplier if amount is not None and amount>=0 else None))
    if len(dates)>1 or (known_date and dates and dates!={trade_day(known_date)}): return None
    return ranking_rows((rows,rows),source,category,n)

def ranking_rows(data,provider,category,n):
    if not isinstance(data,(list,tuple)) or len(data)!=2: return None
    rows={}; conflicts=set()
    for side in data:
        if not isinstance(side,(list,tuple)): continue
        for row in side:
            if not isinstance(row,dict): continue
            name=row.get("name"); pct=number(row.get("change_pct"))
            if not isinstance(name,str) or not name.strip() or pct is None: continue
            name=name.strip()
            d=trade_day(row.get("trade_date"))
            if date_state(d)=="INVALID": continue
            amt=number(row.get("amount"))
            item=dict(name=name,change_pct=pct,source=row.get("source") or provider,
                      category=category,classification=row.get("classification") or provider,
                      trade_date=d,date_status=date_state(d),amount=amt if amt is not None and amt>=0 else None)
            if name in rows and rows[name]!=item: conflicts.add(name)
            rows[name]=item
    valid=[r for k,r in rows.items() if k not in conflicts]
    if len({r["classification"] for r in valid})>1 or len({r["trade_date"] for r in valid})>1: return None
    valid.sort(key=lambda r:(-r["change_pct"],r["name"]))
    if not valid: return None
    # Do not list the same sector on both sides of a small partial response.
    cut=min(n,(len(valid)+1)//2) if len(valid)<2*n else n
    top=valid[:cut]; remaining=valid[cut:]
    return top, list(reversed(remaining[-n:]))

def quality_route(fetchers, category, method, call, normalize, complete, quality):
    """One attempt per existing provider; retain best partial, no mixed snapshots."""
    from src.provider_health_monitor import observe_call
    from src.intelligent_fallback import ranked
    best=None
    for f in ranked(fetchers,lambda f:f.name,GROUPS,category):
        try:
            result=observe_call(category,f.name,method,lambda:normalize(call(f),f.name))
            if result and (best is None or quality(result)>quality(best)): best=result
            if result and complete(result): return result
        except Exception:
            continue
    return best

def public_breadth(data):
    if not data: return {"status":"UNAVAILABLE"}
    return {k:data.get(k) for k in (*BREADTH_KEYS,"status","provider","trade_date","date_status",
           "observed_at","scope","turnover_unit","universe_count","valid_count","zero_amount_count",
           "invalid_count","duplicate_conflicts","amount_coverage","limit_coverage","breadth_usable",
           "coverage_verified","turnover_change_pct","sample_amount","previous_total_amount","previous_trade_date","universe_hash","completed_session","limit_pools")}

def stats_text(data, english=False):
    def show(k): return str(data.get(k)) if data.get(k) is not None else ("N/A" if english else "未取得")
    if english:
        return ("Provider sample (not verified full market): advancing/declining/flat "
                + "/".join(show(k) for k in BREADTH_KEYS[:3])+"; limit-up/down "
                +"/".join(show(k) for k in BREADTH_KEYS[3:5])+"; turnover CNY 100M "+show("total_amount")
                +"; source "+show("provider")+"; trade date "+show("trade_date")
                +". Do not infer missing values or full-market sentiment.")
    return ("供应商样本（未独立核实全市场覆盖）：上涨/下跌/平盘 "
            +"/".join(show(k) for k in BREADTH_KEYS[:3])+"；涨停/跌停 "
            +"/".join(show(k) for k in BREADTH_KEYS[3:5])+"；样本成交额(亿元) "+show("total_amount")
            +"；来源 "+show("provider")+"；交易日 "+show("trade_date")+"。不得推测缺失值或全市场情绪。")

def previous_turnover(current, prior, previous_session):
    """Compare completed sessions only with identical provider universe and units."""
    result=dict(current or {})
    if not current or not prior: return result
    keys=("provider","scope","turnover_unit","universe_hash")
    a,b=number(current.get("total_amount")),number(prior.get("total_amount"))
    day=trade_day(previous_session)
    if (not day or not trade_day(current.get("trade_date"))
        or day>=trade_day(current["trade_date"]) or trade_day(prior.get("trade_date"))!=day
        or not all(current.get(k) and current.get(k)==prior.get(k) for k in keys)
        or not all(d.get("completed_session") and d.get("breadth_usable") for d in (current,prior))
        or a is None or b is None or min(a,b)<=0): return result
    result.update(previous_total_amount=b,previous_trade_date=day,turnover_change_pct=round((a/b-1)*100,4))
    return result

def limit_pool(df, day, side):
    out=dict(source="akshare_em_"+side+"_pool",requested_trade_date=trade_day(day),
             scope="provider_pool",status="UNAVAILABLE",count=None)
    if not out["requested_trade_date"] or df is None or df.empty or "代码" not in df.columns: return out
    codes=[equity_code(x) for x in df["代码"]]
    if any(c is None for c in codes): return out
    if "交易日期" in df.columns and any(trade_day(d)!=out["requested_trade_date"] for d in df["交易日期"]): return out
    out.update(status="UNVERIFIED",count=len(set(codes)))
    return out

def financial_market_model(payload):
    from src.financial_data_adapters import field
    from src.financial_data_model import DataStatus, Origin, json_safe
    from dataclasses import asdict
    def rec(k,v,unit,row,computed=False):
        status=DataStatus.NOT_PROVIDED if v is None else DataStatus.STALE if row.get("date_status")=="PRIOR_DATE" else DataStatus.UNVERIFIED
        return json_safe(asdict(field(k,v,unit,source=row.get("source") or row.get("provider"),
            trade_date=row.get("trade_date"),observed_at=row.get("observed_at"),status=status,
            origin=Origin.COMPUTED if computed else Origin.PROVIDER,
            reason="provider_scope_or_date_not_independently_verified" if v is not None else "missing_provider_field")))
    b=payload.get("breadth") or {}
    breadth={k:rec(k,b.get(k),"CNY_100M" if "amount" in k else "%" if k.endswith("pct") else "count",b,True)
             for k in (*BREADTH_KEYS,"previous_total_amount","turnover_change_pct")}
    indices=[dict(code=r["code"],name=r["name"],fields={k:rec(k,r.get(k),"%" if k in ("change_pct","amplitude") else "index_points",r,k in ("change","change_pct"))
              for k in ("current","prev_close","change","change_pct","open","high","low","amplitude")}) for r in payload.get("indices") or []]
    sectors=[]
    for category,key in (("industry","sectors"),("concept","concepts")):
        for side in ("top","bottom"):
            for r in (payload.get(key) or {}).get(side,[]):
                sectors.append(dict(name=r["name"],category=category,classification=r.get("classification"),direction=side,
                    fields={k:rec(k,r.get(k),"%" if k=="change_pct" else "CNY",r) for k in ("change_pct","amount")}))
    return dict(schema_version="1.0",market="cn",breadth=breadth,indices=indices,sectors=sectors,limit_pools=b.get("limit_pools"))
