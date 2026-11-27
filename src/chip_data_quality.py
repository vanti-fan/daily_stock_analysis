"""Phase 2.4.5: fail-closed chip observations; no external calls in this module."""
from __future__ import annotations
import os
from datetime import date, datetime
from zoneinfo import ZoneInfo
from src.financial_data_model import finite_number

FIELDS=("profit_ratio","avg_cost","cost_90_low","cost_90_high","cost_70_low",
        "cost_70_high","concentration_90","concentration_70")
RATIOS={"profit_ratio","concentration_90","concentration_70"}
AK_COLUMNS=dict(zip(FIELDS,("获利比例","平均成本","90成本-低","90成本-高",
                             "70成本-低","70成本-高","90集中度","70集中度")))
SOURCES={"AkshareFetcher":("akshare_eastmoney","LIBRARY_ESTIMATE"),
         "TushareFetcher":("tushare_cyq_chips","PROVIDER_DISTRIBUTION_DERIVED")}

def enabled():
    return os.getenv("DSA_CHIP_DATA_QUALITY","").lower()=="true"

def eligible(code):
    from src.intelligent_fallback import ordinary_a_share
    return ordinary_a_share(code)

def tushare_enabled():
    # Explicit entitlement opt-in; unknown points are not entitlement proof.
    return os.getenv("DSA_CHIP_TUSHARE_FALLBACK","").lower()=="true"

def day(v):
    try:
        s=str(v).strip()
        return (datetime.strptime(s,"%Y%m%d").date() if len(s)==8 and s.isdigit()
                else date.fromisoformat(s[:10])).isoformat()
    except (ValueError,TypeError): return None

def expected_day():
    """Existing frozen target takes precedence. Never guess a trading session."""
    try:
        from src.services.history_loader import get_frozen_target_date
        frozen=get_frozen_target_date()
        if frozen: return day(frozen)
        from src.market_data_quality import session_dates
        now=datetime.now(ZoneInfo("Asia/Shanghai"))
        days=session_dates(now)
        if days and days[-1]==now.date().isoformat() and now.strftime("%H:%M")<"15:00":
            days=days[:-1]
        return days[-1] if days else None
    except Exception:
        return None

def ordered_candidates(fetchers):
    from src.intelligent_fallback import ranked
    allowed=[f for f in fetchers if f.name in SOURCES and
             (f.name!="TushareFetcher" or tushare_enabled())]
    return ranked(allowed,lambda f:f.name,
                  {"AkshareFetcher":"eastmoney","TushareFetcher":"tushare"},"chip")

def read(obj,key):
    return obj.get(key) if isinstance(obj,dict) else getattr(obj,key,None)

def fraction(value):
    # Bare numerics are fractions, explicit percent strings are divided by 100.
    if isinstance(value,str) and value.strip().endswith("%"):
        n=finite_number(value.strip()[:-1])
        return n/100 if n is not None else None
    return finite_number(value)

def metric_error(values):
    vals={k:finite_number(read(values,k)) for k in FIELDS}
    if any(v is None for v in vals.values()): return "missing_or_nonfinite_metric"
    if any(not 0<=vals[k]<=1 for k in RATIOS): return "fraction_out_of_range"
    if any(vals[k]<=0 for k in FIELDS if k not in RATIOS): return "nonpositive_cost"
    a,b,c,d=(vals[k] for k in ("cost_90_low","cost_70_low","cost_70_high","cost_90_high"))
    if not a<=b<=c<=d: return "cost_quantiles_not_nested"
    # A mean need not lie within the central 90% interval.
    for low,high,k in ((a,d,"concentration_90"),(b,c,"concentration_70")):
        # Allow price rounding (cent) and ratio rounding, not unit confusion.
        tolerance=.001+ .02/(low+high)
        if abs(vals[k]-(high-low)/(high+low))>tolerance: return "concentration_formula_conflict"
    return None

def snapshot_error(chip,code,expected,provider=None):
    if chip is None: return "empty"
    if read(chip,"code")!=code: return "identity_mismatch"
    d=day(read(chip,"date")); target=day(expected)
    if not d or not target: return "trade_date_unverified"
    if d<target: return "stale"
    if d>target: return "future_or_wrong_session"
    source,kind=read(chip,"source"),read(chip,"data_kind")
    if (source,kind) not in SOURCES.values(): return "unknown_chip_provenance"
    if provider and SOURCES.get(provider)!=(source,kind): return "source_mismatch"
    if read(chip,"price_basis")!="UNADJUSTED_REQUESTED": return "unknown_price_basis"
    if set(read(chip,"present_fields") or ())!=set(FIELDS): return "missing_explicit_fields"
    return metric_error(chip)

def akshare_snapshot(df,code,expected):
    """Use explicitly selected exchange date, not row position; no mixed snapshots."""
    if df is None or df.empty or not {"日期",*AK_COLUMNS.values()}.issubset(df.columns): return None
    dates=df["日期"].map(day)
    target=day(expected)
    if not target: return None
    rows=df.loc[dates==target]
    if len(rows)!=1: return None
    row=rows.iloc[0]
    values={k:(fraction(row[c]) if k in RATIOS else finite_number(row[c])) for k,c in AK_COLUMNS.items()}
    if metric_error(values): return None
    from data_provider.realtime_types import ChipDistribution
    return ChipDistribution(code=code,date=target,source="akshare_eastmoney",**values,
        data_kind="LIBRARY_ESTIMATE",price_basis="UNADJUSTED_REQUESTED",present_fields=list(FIELDS),
        evidence={"endpoint":"stock_cyq_em","adjust":"","selected_row":{c:float(values[k]) for k,c in AK_COLUMNS.items()},
                  "date":target,"row_count":len(df)})

def validate_tushare_inputs(bins, daily, ts_code, expected):
    """Validate returned identity/date and complete histogram mass before computing."""
    if bins is None or daily is None or bins.empty or len(daily)!=1: raise ValueError("empty_or_duplicate_daily")
    for frame in (bins,daily):
        if not {"ts_code","trade_date"}.issubset(frame.columns): raise ValueError("missing_identity_date")
        if not frame["ts_code"].eq(ts_code).all() or not frame["trade_date"].map(day).eq(day(expected)).all():
            raise ValueError("identity_or_date_conflict")
    if not {"price","percent"}.issubset(bins.columns): raise ValueError("missing_bins")
    rows=[(finite_number(p),finite_number(w)) for p,w in zip(bins["price"],bins["percent"])]
    if any(p is None or w is None or p<=0 or w<0 for p,w in rows): raise ValueError("invalid_bins")
    if len({p for p,w in rows})!=len(rows): raise ValueError("duplicate_price_bins")
    if not 99<=sum(w for p,w in rows)<=101: raise ValueError("incomplete_histogram_mass")
    close=finite_number(daily.iloc[0].get("close"))
    if close is None or close<=0: raise ValueError("invalid_reference_close")
    return {"endpoint":"cyq_chips","ts_code":ts_code,"trade_date":day(expected),
            "reference_close":close,"bins":[{"price":p,"percent":w} for p,w in rows]}

def audit():
    return {"schema_version":"1.0","baseline":"ecbad12",
            "scope":"ordinary Shanghai/Shenzhen A-share equities",
            "providers":[
                {"provider":"AkshareFetcher","endpoint":"stock_cyq_em","kind":"LIBRARY_ESTIMATE","policy":"primary","entitlement":"live_unverified"},
                {"provider":"TushareFetcher","endpoint":"cyq_chips + daily","kind":"PROVIDER_DISTRIBUTION_DERIVED","policy":"explicit_opt_in","entitlement":"unknown"},
                {"provider":"MiaoxiangFetcher","endpoint":"query_tables","policy":"excluded_strict","reason":"incomplete cost bands and unverified identity/metric basis"},
                {"provider":"efinance/pytdx/xueqiu","policy":"no_reviewed_chip_endpoint"}],
            "local_estimate":"not_implemented; no validated turnover-adjusted history model",
            "limitations":["No true investor cost records","Provider-specific algorithms may differ","Unadjusted request does not establish independent price-basis proof","No live quota check"]}
