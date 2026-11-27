"""Phase 2.4.3 A-share routing policy. No network calls in policy functions."""
from __future__ import annotations
import math
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from contextvars import ContextVar

history_evidence = ContextVar("dsa_history_evidence", default=None)

BASELINE = "f199f645607b6626914bdb1756254c0c9daa12c9"
HISTORY = {"AkshareFetcher":"QFQ", "EfinanceFetcher":"QFQ", "BaostockFetcher":"QFQ",
           "TencentFetcher":"QFQ", "TushareFetcher":"UNADJUSTED", "PytdxFetcher":"UNKNOWN"}
GROUPS = {"tencent":"tencent", "akshare_qq":"tencent", "akshare_sina":"sina",
          "akshare_em":"eastmoney", "efinance":"eastmoney", "akshare_xq":"xueqiu",
          "pytdx":"tdx","tushare":"tushare", "tickplus":"tickplus", "tickflow":"tickflow"}
DAILY_GROUPS = {"EfinanceFetcher":"eastmoney","BaostockFetcher":"baostock",
                "TencentFetcher":"tencent","AkshareFetcher":"MULTI"}
class AdjustmentError(ValueError):
    pass

def enabled():
    return os.getenv("DSA_INTELLIGENT_FALLBACK","").lower()=="true"

def ordinary_a_share(code):
    # Deliberately limited to ordinary Shanghai/Shenzhen equities; ETFs,
    # indexes, BSE and offshore assets retain upstream policy.
    return isinstance(code,str) and len(code)==6 and code.isdigit() and code.startswith(("000","001","002","003","300","301","600","601","603","605","688","689"))

def history_order(fetchers):
    """Exclude unreviewed or non-QFQ paths, preserve configured priority as tie-breaker."""
    candidates=[f for f in fetchers if HISTORY.get(f.name)=="QFQ"]
    return ranked(candidates,lambda f:f.name,DAILY_GROUPS,"daily_data")

def ranked(items,key,groups,category):
    items=list(items)
    # Only use run-local evidence. Sparse data is not a reason to change order.
    path=os.getenv("DSA_PROVIDER_HEALTH_JOURNAL")
    evidence={}
    if path and Path(path).is_file():
        try:
            from src.provider_health_monitor import build_report
            for r in build_report(path)["providers"]:
                if r["category"]==category and r["operation"]=="get_"+category and r["completed"]>=3 and r["success_rate"] is not None:
                    evidence[r["provider"]]=r
        except (OSError,ValueError,KeyError,TypeError,AttributeError):
            pass
    def rank(item):
        name=key(item)
        r=evidence.get(name)
        # Defer demonstrated low-success routes; do not punish unobserved ones.
        return int(bool(r and r["success_rate"]<.5))
    ordered=sorted(items,key=rank)
    # Prefer distinct reviewed dependencies ahead of redundant routes,
    # without skipping them entirely. MULTI/unknown are not independent proof.
    first=[]; deferred=[]; seen=set()
    for item in ordered:
        group=groups.get(key(item))
        if group and group!="MULTI" and group in seen:
            deferred.append(item)
        else:
            first.append(item)
            if group and group!="MULTI": seen.add(group)
    return sorted(first+deferred,key=rank)

def quote_order(routes):
    out=[]
    for route in routes:
        route="tencent" if route=="akshare_qq" else route
        if route not in out: out.append(route)
    # New Xueqiu route is opt-in and only considered after existing routes fail.
    optional={"akshare_xq","pytdx"}
    return ranked([x for x in out if x not in optional],lambda x:x,GROUPS,"realtime_quote") + [x for x in out if x in optional]

def mark_history(df,provider,route=None):
    df.attrs["dsa_history"]={"provider":provider,"route":route or provider,
        "adjustment":"QFQ","evidence":"REVIEWED_REQUEST_CONTRACT",
        "anchor":"PROVIDER_CURRENT_BASE_UNVERIFIED",
        "currency":"CNY"}
    return df

def validate_history(df,provider,*,start_date=None,end_date=None):
    import pandas as pd
    if HISTORY.get(provider)!="QFQ":
        raise AdjustmentError("Provider does not have a reviewed QFQ contract")
    if df is None or df.empty: raise AdjustmentError("Empty history")
    meta=df.attrs.get("dsa_history",{})
    if meta.get("adjustment")!="QFQ" or meta.get("provider")!=provider:
        raise AdjustmentError("Missing or incompatible adjustment evidence")
    dates=pd.to_datetime(df["date"],errors="coerce")
    if dates.isna().any() or dates.duplicated().any() or not dates.is_monotonic_increasing:
        raise AdjustmentError("Invalid history dates")
    if (start_date and dates.min().date()<pd.Timestamp(start_date).date()) or (end_date and dates.max().date()>pd.Timestamp(end_date).date()):
        raise AdjustmentError("History outside requested range")
    prices=df[["open","high","low","close"]].apply(pd.to_numeric,errors="coerce")
    if not all(math.isfinite(v) and v>0 for v in prices.to_numpy().ravel()):
        raise AdjustmentError("Invalid OHLC")
    if ((prices.high<prices[["open","close","low"]].max(axis=1)) |
        (prices.low>prices[["open","close","high"]].min(axis=1))).any():
        raise AdjustmentError("OHLC ordering conflict")
    meta["first_date"]=dates.iloc[0].date().isoformat()
    meta["last_date"]=dates.iloc[-1].date().isoformat()
    return df

def validate_storage_window(incoming,existing):
    """No partial upsert over old price bases. Refuse rather than delete old data."""
    if not set(existing).issubset(set(incoming)):
        raise AdjustmentError("Existing history extends outside replacement window; refusing mixed snapshot")

def source_name(quote):
    src=getattr(quote,"source",None)
    return getattr(src,"value",str(src or "UNKNOWN"))

def quote_lineage(quote):
    fields=("price","change_pct","change_amount","volume","amount","open_price","high","low","pre_close",
            "volume_ratio","turnover_rate","pe_ratio","pb_ratio","total_mv","circ_mv","amplitude")
    lineage=getattr(quote,"field_sources",None)
    if lineage is None:
        lineage={}
        quote.field_sources=lineage
    for field in fields:
        if getattr(quote,field,None) is not None and field not in lineage:
            lineage[field]={"source":source_name(quote),"timestamp":getattr(quote,"provider_timestamp",None),
                            "quote_date":getattr(quote,"quote_date",None)}
    return lineage

def _timestamp(value):
    try:
        dt=datetime.fromisoformat(str(value).replace("Z","+00:00"))
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone(timedelta(hours=8)))
    except (ValueError,TypeError): return None

def compatible_quotes(primary,secondary):
    if getattr(primary,"code",None)!=getattr(secondary,"code",None): return False
    if getattr(primary,"is_stale",None) is True or getattr(secondary,"is_stale",None) is True: return False
    if getattr(primary,"currency",None) and getattr(secondary,"currency",None) and primary.currency!=secondary.currency: return False
    a,b=(_timestamp(getattr(q,"provider_timestamp",None)) for q in (primary,secondary))
    # No fabricated fetch-time or same-day substitution for market timestamps.
    if a is None or b is None or abs((a-b).total_seconds())>600: return False
    cn=timezone(timedelta(hours=8))
    if a.astimezone(cn).date()!=b.astimezone(cn).date(): return False
    try:
        p,s=float(primary.price),float(secondary.price)
        return math.isfinite(p) and math.isfinite(s) and p>0 and abs(p-s)/p<=.01
    except (ValueError,TypeError): return False

def valid_quote_value(field,value):
    try:
        n=float(value)
        return math.isfinite(n) and (n>0 if field=="price" else field=="pe_ratio" or n>=0)
    except (ValueError,TypeError): return False


def strict_history(code):
    return enabled() and ordinary_a_share(code)

def merge_fields(primary,secondary,fields):
    if not compatible_quotes(primary,secondary): return []
    a,b=quote_lineage(primary),quote_lineage(secondary)
    filled=[]
    for field in fields:
        value=getattr(secondary,field,None)
        if not valid_quote_value(field,getattr(primary,field,None)) and valid_quote_value(field,value):
            setattr(primary,field,value)
            a[field]=dict(b[field])
            filled.append(field)
    return filled

def parse_xueqiu(frame,code):
    """AKShare item/value contract; avoid timezone and valuation assumptions."""
    from data_provider.realtime_types import UnifiedRealtimeQuote,RealtimeSource
    if frame is None or frame.empty or not {"item","value"}.issubset(frame.columns):
        return None
    if frame["item"].duplicated().any(): return None
    row=dict(zip(frame["item"],frame["value"]))
    symbol=("SH" if code.startswith("6") else "SZ")+code
    if str(row.get("代码","")).upper()!=symbol: return None
    if str(row.get("货币","")).upper() not in ("CNY","人民币"): return None
    def number(label, signed=False):
        try:
            v=float(row[label])
            return v if math.isfinite(v) and (signed or v>=0) else None
        except (KeyError,ValueError,TypeError): return None
    price=number("现价")
    if not price: return None
    # Upstream converts epoch to host-local naive string. Do not assume zone.
    return UnifiedRealtimeQuote(code=code,name=str(row.get("名称") or ""),source=RealtimeSource.XUEQIU,
        market="cn",currency="CNY",price=price,change_pct=number("涨幅",True),
        open_price=number("今开"),high=number("最高"),low=number("最低"),pre_close=number("昨收"),
        pe_ratio=number("市盈率(动)",True),pb_ratio=number("市净率"),
        total_mv=number("资产净值/总市值"),circ_mv=number("流通值"),
        turnover_rate=number("周转率"),amplitude=number("振幅"))


def validate_baostock_flags(frame):
    if "adjustflag" not in frame or frame.empty or not frame["adjustflag"].astype(str).eq("2").all():
        raise AdjustmentError("Baostock response does not confirm QFQ")

def parse_pytdx(row,code):
    from data_provider.realtime_types import UnifiedRealtimeQuote,RealtimeSource
    if not isinstance(row,dict) or row.get("code")!=code or row.get("identity_verified") is not True:
        return None
    def n(key):
        v=row.get(key)
        return float(v) if valid_quote_value(key,v) else None
    price=n("price")
    if price is None: return None
    pre=n("pre_close")
    return UnifiedRealtimeQuote(code=code,name=row.get("name") or "",source=RealtimeSource.PYTDX,
        market="cn",currency="CNY",price=price,open_price=n("open"),high=n("high"),low=n("low"),
        pre_close=pre,change_pct=(price/pre-1)*100 if pre and pre>0 else None)

def history_metadata(code):
    value=history_evidence.get()
    return value[1] if value is not None and value[0]==code else None
