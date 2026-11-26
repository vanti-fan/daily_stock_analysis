"""Phase 2.3 offline data quality assessment of verified archive bundles.

Never requests provider/LLM data, never mutates original evidence. A missing
archive is not evidence of a provider failure. Dates are compared only to an
explicit expected exchange trading date (not UTC wall-clock date).
"""
from __future__ import annotations

from collections import Counter, defaultdict
from datetime import date
import json
import os
from pathlib import Path
import tempfile

from src.financial_data_model import finite_number, DataStatus
from src.raw_data_archive import verify

QUALITY_VERSION = "1.0"
USABLE = {"AVAILABLE", "VALIDATED"}
PRICE_KEYS = {"price", "open", "high", "low", "previous_close", "ma5", "ma10", "ma20", "ma60", "avg_cost", "cost_90_low", "cost_90_high", "cost_70_low", "cost_70_high"}
NONNEG = {"volume", "amount", "volume_ratio", "turnover_rate", "amplitude", "total_market_cap", "float_market_cap", "volume_ratio_5d"}
QUOTE_REQUIRED = ("price", "change_pct", "volume", "amount", "volume_ratio", "turnover_rate")
STATUS_MAP = {x.value for x in DataStatus}


def day(value):
    try:
        return date.fromisoformat(value) if isinstance(value, str) else None
    except ValueError:
        return None


def failure_status(*, http_status=None, exception=None):
    """Classify only explicit evidence, not arbitrary log/message substrings."""
    if http_status == 429:
        return "RATE_LIMITED"
    if http_status == 402:
        return "PAYMENT_REQUIRED"
    if http_status in (401, 403):
        return "ACCESS_DENIED"
    if http_status in (408, 504) or isinstance(exception, TimeoutError):
        return "TIMEOUT"
    if isinstance(http_status, int) and 500 <= http_status < 600:
        return "UPSTREAM_ERROR"
    return "UNKNOWN"


def assess_field(key, field, *, expected_date=None, require_date=False, currency=None):
    field = field if isinstance(field, dict) else {}
    q = field.get("quality") or {}
    value = finite_number(field.get("value"))
    status = q.get("status", "NOT_PROVIDED")
    reason = q.get("reason")
    source = q.get("source")
    unit = field.get("unit")
    origin = q.get("origin")
    actual = day(q.get("trade_date"))
    expected = day(expected_date)
    if status not in STATUS_MAP:
        status, reason = "UNVERIFIED", "unknown_status"
    if status in USABLE:
        if value is None or (key in PRICE_KEYS and value <= 0) or (key in NONNEG and value < 0):
            status, reason = "INVALID", "numeric_domain"
        elif origin not in ("PROVIDER", "COMPUTED") or not source or not unit:
            status, reason = "UNVERIFIED", "missing_provenance_or_unit"
        elif currency and key in PRICE_KEYS | {"amount", "change_amount", "total_market_cap", "float_market_cap"} and unit != currency:
            status, reason = "CONFLICT", "currency_mismatch"
        elif require_date:
            if not actual or not expected:
                status, reason = "UNVERIFIED", "exchange_date_not_confirmed"
            elif actual < expected:
                status, reason = "STALE", "before_expected_trade_date"
            elif actual > expected:
                status, reason = "CONFLICT", "after_expected_trade_date"
        if status in USABLE and (key in ("profit_ratio","concentration_90","concentration_70") and not 0 <= value <= 1):
            status, reason = "INVALID", "fraction_out_of_range"
    return dict(key=key, value=value, unit=unit, source=source, origin=origin,
                trade_date=q.get("trade_date"), status=status, reason=reason,
                usable=status in USABLE and value is not None)


def compare_quotes(a, b, *, relative_tolerance=0.001):
    """Compare two independently sourced observations only when units/date/time match."""
    if relative_tolerance < 0:
        raise ValueError("negative tolerance")
    keys=("key","unit","trade_date","observed_at")
    if any(not a.get(k) or a.get(k)!=b.get(k) for k in keys):
        return {"status":"UNVERIFIED","reason":"incomparable_observations"}
    if not a.get("source") or not b.get("source") or a["source"]==b["source"]:
        return {"status":"UNVERIFIED","reason":"independence_not_established"}
    x,y=finite_number(a.get("value")),finite_number(b.get("value"))
    if x is None or y is None:
        return {"status":"UNVERIFIED","reason":"missing_value"}
    delta=abs(x-y)
    threshold=relative_tolerance*max(abs(x),abs(y))
    return {"status":"CONFLICT" if delta>threshold else "VALIDATED",
            "absolute_difference":delta,"tolerance":threshold}


def summarize(fields):
    fields=list(fields)
    counts=Counter(x["status"] for x in fields)
    usable=sum(x["usable"] for x in fields)
    return {"expected":len(fields),"usable":usable,
            "coverage":usable/len(fields) if fields else None,
            "status_counts":dict(sorted(counts.items()))}


def load_json(folder, name):
    p=folder/name
    return json.loads(p.read_text(encoding="utf-8")) if p.is_file() else None


def matching_bundle(index, root, kind, code, query_id):
    if not query_id:
        return None, "query_id_missing"
    matches=[x for x in index if x["kind"]==kind and x["identity"]==code and x["query_id"]==query_id]
    if len(matches)!=1:
        return None, "ambiguous_archive" if matches else "archive_not_found"
    return (root/matches[0]["manifest"]).parent, None


def assess_stock(root, index, row):
    code,query_id=row["code"],row.get("query_id")
    folder,reason=matching_bundle(index,root,"stock-inputs",code,query_id)
    result_folder,research_reason=matching_bundle(index,root,"stock-research",code,query_id)
    model=load_json(folder,"financial_data.json") if folder else None
    manifest=load_json(folder,"manifest.json") if folder else None
    metadata=(manifest or {}).get("metadata",{})
    expected=row.get("expected_trade_date") or metadata.get("expected_trade_date")
    identity=(model or {}).get("identity",{})
    model_reason=reason
    if folder and not model:
        model_reason="normalization_failed_or_not_archived"
    if model and (identity.get("code")!=code or model.get("schema_version")!="1.0"):
        model=None
        model_reason="identity_or_schema_mismatch"
    groups={}
    for group in ("snapshot","technical","chips","fundamentals"):
        data=((model or {}).get(group) or {}).get("fields",{})
        keys=list(dict.fromkeys((list(QUOTE_REQUIRED) if group=="snapshot" else [])+list(data)))
        groups[group]={k:assess_field(k,data.get(k),expected_date=expected,
                       require_date=group in ("snapshot","technical","chips","fundamentals"),
                       currency=identity.get("currency")) for k in keys}
    snapshot=groups["snapshot"]
    prices=[snapshot.get(k,{}) for k in ("low","open","price","high")]
    if all(x.get("usable") for x in prices):
        low,op,price,high=[x["value"] for x in prices]
        if not low<=min(op,price)<=max(op,price)<=high:
            for x in prices:
                x.update(status="CONFLICT",reason="quote_ohlc_order",usable=False)
    history=(model or {}).get("history") or {}
    bars=history.get("bars",[])
    historical_status=history.get("status","NOT_ARCHIVED")
    last_date=max((b["trade_date"] for b in bars),default=None)
    if day(last_date) and day(expected) and day(last_date)>day(expected):
        historical_status="CONFLICT"
    elif bars and (not day(last_date) or not day(expected)):
        historical_status="UNVERIFIED"
    elif day(last_date) and day(expected) and day(last_date)<day(expected):
        historical_status="STALE"
    history_summary={"status":historical_status,"bar_count":len(bars),"latest_trade_date":last_date,
                     "adjustment":history.get("adjustment"),"source":history.get("source")}
    research=load_json(result_folder,"ai_research.json") if result_folder else None
    batch_status=row.get("status","INCOMPLETE")
    # A pipeline-level timeout is not an independently observed LLM timeout.
    ai_status=("SUCCESS" if batch_status=="SUCCESS" and research and research.get("success") is True
               else "FAILED" if research and research.get("success") is False
               else "UNVERIFIED" if batch_status=="SUCCESS" else "NOT_COMPLETED")
    required=summarize(snapshot[k] for k in QUOTE_REQUIRED)
    quote_status=("AVAILABLE" if required["usable"]==required["expected"] else
                  "PARTIAL" if required["usable"] else "UNAVAILABLE" if folder else "NOT_ARCHIVED")
    return {"code":code,"name":identity.get("name") or row.get("name") or code,"query_id":query_id,
            "expected_trade_date":expected,"archive_reason":model_reason,"quote_status":quote_status,
            "groups":groups,"coverage":{k:summarize(v.values()) for k,v in groups.items()},
            "required_quote_coverage":required,"history":history_summary,
            "batch_status":batch_status,"ai_status":ai_status,
            "actual_model":research.get("model_used") if research else None,
            "research_archive_reason":research_reason}


def build_report(root, rows, *, scenario="LIVE"):
    root=Path(root)
    index=verify(root)
    stocks=[assess_stock(root,index,row) for row in rows]
    sources=defaultdict(list)
    for stock in stocks:
        # Fundamentals intentionally omitted: these mirror quote valuation fields.
        for group in ("snapshot","technical","chips"):
            for field in stock["groups"][group].values():
                if field.get("source"):
                    sources[(field["source"],group)].append(field)
    market=[]
    for item in index:
        if item["kind"]!="market":
            continue
        m=load_json((root/item["manifest"]).parent,"manifest.json")["metadata"]
        market.append({"region":item["identity"],"reused":m.get("reused"),
                       "structured_available":m.get("structured_available"),
                       "breadth_available":m.get("breadth_available"),
                       "snapshot_available":m.get("snapshot_available")})
    return {"quality_version":QUALITY_VERSION,"scenario":scenario,"planned":len(stocks),
            "quote_price_usable":sum(bool(s["groups"]["snapshot"]["price"]["usable"]) for s in stocks),
            "ai_completed":sum(s["ai_status"]=="SUCCESS" for s in stocks),
            "stocks":stocks,"market":market,
            "source_field_coverage":[{"source":source,"category":group,**summarize(fields)}
                                    for (source,group),fields in sorted(sources.items())],
            "limitations":["Field coverage is not provider request success rate.",
                           "No independent second observation implies no cross-source validation.",
                           "Missing archive or batch timeout does not establish provider/LLM root cause."]}


def write_report(path, report):
    path=Path(path)
    path.parent.mkdir(parents=True,exist_ok=True)
    with tempfile.NamedTemporaryFile(mode="w",encoding="utf-8",dir=path.parent,delete=False) as tmp:
        json.dump(report,tmp,ensure_ascii=False,allow_nan=False,indent=2)
        name=tmp.name
    os.replace(name,path)


def attach_quality(rows, report):
    """Join only assessed current-batch evidence; never overwrite original batch status."""
    by_code={s["code"]:s for s in report["stocks"]}
    return [{**row,"data_quality":by_code[row["code"]]} if row["code"] in by_code else row for row in rows]


def dashboard_quote(row, key):
    q=row.get("data_quality")
    if not q:
        return None
    field=q.get("groups",{}).get("snapshot",{}).get(key,{})
    return field.get("value") if field.get("usable") else None
