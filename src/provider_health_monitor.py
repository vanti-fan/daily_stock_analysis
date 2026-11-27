"""Phase 2.4.2: observe existing provider attempts; no network probes.

Metrics describe observed manager/adapter calls, not underlying HTTP calls.
Unpaired outcomes, cached results, unsupported paths and unfinished attempts
are kept separate from the completed-call success-rate denominator.
"""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
import json
import logging
import math
import os
from pathlib import Path
import re
from threading import Lock, local
import time
from uuid import uuid4

LOG = logging.getLogger(__name__)
_LOCK = Lock()
_LOCAL = local()
CATEGORIES = ("daily_data", "realtime_quote", "chip", "market_indices",
              "market_stats", "sector_rankings", "concept_rankings", "fundamental")
ALIASES = {"daily_bars":"daily_data", "fundamentals":"fundamental"}
BASELINE = "d0ba82052c6930ef8cd5f420987fa97b91095289"


def enabled():
    return bool(os.environ.get("DSA_PROVIDER_HEALTH_JOURNAL"))


def identifier(value):
    # Only short structured identifiers; never store exception messages or URLs.
    s = str(value or "")
    return s if re.fullmatch(r"[A-Za-z0-9_.:-]{1,80}", s) else "UNKNOWN"


def classify(error_type=None, exception=None, http_status=None):
    if exception is not None:
        response = getattr(exception, "response", None)
        http_status = getattr(response, "status_code", None) or getattr(exception, "code", None) or http_status
        error_type = type(exception).__name__
    t = re.sub(r"[^a-z0-9]", "", str(error_type or "").lower())
    if t in {"unsupported","notsupported","unavailable","circuitopen","skipped","cancelled","budgetexhausted","quarantined","poolexhausted"}:
        return "SKIPPED"
    if http_status == 402 or t in {"paymentrequired","insufficientbalance"}: return "PAYMENT_REQUIRED"
    if http_status in (401,403) or t in {"permissionerror","accessdenied","permissiondenied","authenticationerror","authorizationerror","unauthorized","forbidden"}: return "ACCESS_DENIED"
    if http_status == 429 or t in {"ratelimited","ratelimiterror","ratelimit","toomanyrequests"}: return "RATE_LIMITED"
    if http_status in (408,504) or isinstance(exception, TimeoutError) or t in {"timeout","timeouterror","readtimeout","connecttimeout","readtimeouterror","connecttimeouterror","timeoutexception"}: return "TIMEOUT"
    if t in {"empty","emptydataerror","noresults"}: return "EMPTY"
    if isinstance(exception, ConnectionError) or t in {"connectionerror","connecterror","networkerror","proxyerror","sslerror","gaierror","urlerror"}: return "NETWORK_ERROR"
    if isinstance(http_status,int) and 500 <= http_status < 600: return "UPSTREAM_ERROR"
    if t in {"partial","incomplete"}: return "PARTIAL"
    return "UNKNOWN_ERROR"


def _append(row):
    path = os.environ.get("DSA_PROVIDER_HEALTH_JOURNAL")
    if not path:
        return
    try:
        with _LOCK:
            p=Path(path)
            p.parent.mkdir(parents=True,exist_ok=True)
            with p.open("a",encoding="utf-8") as f:
                f.write(json.dumps(row,ensure_ascii=True,allow_nan=False,separators=(",",":"))+"\n")
    except Exception:
        LOG.warning("Provider health journal write failed")


def _key(data_type, provider, operation):
    return tuple(identifier(x) for x in (data_type,provider,operation))


def start(data_type, provider, operation):
    if not enabled():
        return
    key=_key(data_type,provider,operation)
    pending=getattr(_LOCAL,"pending",None)
    if pending is None:
        pending=_LOCAL.pending=defaultdict(list)
    attempt=uuid4().hex
    pending[key].append((attempt,time.monotonic()))
    _append(dict(event="START",attempt=attempt,data_type=key[0],provider=key[1],operation=key[2]))


def finish(data_type,provider,operation,success,*,latency_ms=None,error_type=None,
           cache_hit=None,fallback_from=None,fallback_to=None,record_count=None):
    if not enabled():
        return
    key=_key(data_type,provider,operation)
    pending=getattr(_LOCAL,"pending",{})
    queue=pending.get(key,[])
    paired=bool(queue)
    attempt,started=queue.pop(0) if paired else (uuid4().hex,None)
    if not queue: pending.pop(key,None)
    if started is not None:
        latency_ms=max(0,round((time.monotonic()-started)*1000))
    else:
        latency_ms=None
    status=("CACHE_HIT" if cache_hit is True else "SUCCESS" if success is True
            else classify(error_type))
    _append(dict(event="END",attempt=attempt,paired=paired,data_type=key[0],provider=key[1],
                 operation=key[2],status=status,latency_ms=latency_ms,
                 fallback_from=identifier(fallback_from) if fallback_from else None,
                 fallback_to=identifier(fallback_to) if fallback_to else None,
                 record_count=record_count if type(record_count) is int and record_count>=0 else None))


def observe_call(data_type,provider,operation,call,*,supported=True,ranking=False):
    """Instrument a single existing manager call, without altering its result."""
    if not enabled() or not supported:
        return call()
    from src.services.run_diagnostics import record_provider_run_started,record_provider_run
    record_provider_run_started(data_type=data_type,provider=provider,operation=operation)
    success=False
    error_type="empty"
    try:
        result=call()
        success=bool(any(result[:2])) if ranking and isinstance(result,(tuple,list)) else bool(result)
        return result
    except Exception as exc:
        error_type=classify(exception=exc)
        raise
    finally:
        record_provider_run(data_type=data_type,provider=provider,operation=operation,
                            success=success,error_type=None if success else error_type)


def percentile(values,p):
    if not values: return None
    return sorted(values)[max(0,math.ceil(p*len(values))-1)]


def build_report(journal, *, quality=None, profile="LIVE"):
    starts={}
    ends={}
    malformed=0
    journal=Path(journal)
    if journal.is_file():
        for line in journal.read_text(encoding="utf-8").splitlines():
            try:
                row=json.loads(line)
                if row.get("event") not in {"START","END"} or not row.get("attempt"):
                    raise ValueError("invalid event")
                target=starts if row["event"]=="START" else ends
                if row["attempt"] in target: raise ValueError("duplicate event")
                target[row["attempt"]]=row
            except (ValueError,TypeError,AttributeError):
                malformed+=1
    groups=defaultdict(list)
    for attempt,row in ends.items():
        item=dict(row)
        item["paired"]=attempt in starts and row.get("paired") is True and all(
            starts[attempt].get(k)==row.get(k) for k in ("data_type","provider","operation"))
        if not item["paired"] and item["status"] not in {"CACHE_HIT","SKIPPED"}:
            item["status"]="UNPAIRED"
        groups[(ALIASES.get(item["data_type"],item["data_type"]),item["provider"],item["operation"])].append(item)
    for attempt,row in starts.items():
        if attempt not in ends:
            groups[(ALIASES.get(row["data_type"],row["data_type"]),row["provider"],row["operation"])].append(dict(row,status="INCOMPLETE",paired=False))
    summaries=[]
    exclusions={"CACHE_HIT","SKIPPED","INCOMPLETE","UNPAIRED"}
    for (category,provider,operation), rows in sorted(groups.items()):
        counts=Counter(r["status"] for r in rows)
        completed=[r for r in rows if r["status"] not in exclusions and r.get("paired")]
        n=len(completed)
        durations=[r["latency_ms"] for r in completed if type(r.get("latency_ms")) in (int,float) and math.isfinite(r["latency_ms"]) and r["latency_ms"]>=0]
        summaries.append(dict(category=category,provider=provider,operation=operation,
            observed_events=len(rows),completed=n,success=counts["SUCCESS"],
            success_rate=counts["SUCCESS"]/n if n else None,status_counts=dict(counts),
            latency_samples=len(durations),mean_ms=round(sum(durations)/len(durations),1) if durations else None,
            p50_ms=percentile(durations,.50),p95_ms=percentile(durations,.95),
            max_ms=max(durations,default=None)))
    return dict(schema_version="1.0",accepted_baseline=BASELINE,profile=profile,
        observation_scope="manager_and_adapter_calls_not_http_requests",
        journal_present=journal.is_file(),malformed_events=malformed,
        categories=[dict(category=c,status="OBSERVED" if any(r["category"]==c for r in summaries) else "NOT_OBSERVED") for c in CATEGORIES],
        providers=summaries,
        fallback_hints=[{k:r[k] for k in ("data_type","provider","operation","fallback_from","fallback_to") if r.get(k)}
                        for r in ends.values() if r.get("fallback_from") or r.get("fallback_to")],
        field_coverage=(quality or {}).get("source_field_coverage",[]),
        limitations=["Fallback hints do not prove a subsequent source was called.",
                     "Provider-internal retries, cache hits and HTTP failures hidden by adapters are not observable here.",
                     "Fundamental bundle observations are adapter calls, not endpoint-level requests.",
                     "Unpaired outcomes, explicit cache hits, skipped and unfinished attempts are excluded from success rates.",
                     "Field coverage is not request success rate."])


def write_report(journal,output,*,quality=None,profile="LIVE"):
    report=build_report(journal,quality=quality,profile=profile)
    p=Path(output); p.parent.mkdir(parents=True,exist_ok=True)
    temp=p.with_name(p.name+".tmp")
    temp.write_text(json.dumps(report,ensure_ascii=False,allow_nan=False,indent=2)+"\n",encoding="utf-8")
    os.replace(temp,p)
    return report


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument("--journal",required=True,type=Path)
    p.add_argument("--output",required=True,type=Path)
    p.add_argument("--quality-root",type=Path)
    args=p.parse_args()
    quality=None
    if args.quality_root:
        files=list(args.quality_root.rglob("data-quality.json"))
        if len(files)==1:
            quality=json.loads(files[0].read_text(encoding="utf-8"))
    report=write_report(args.journal,args.output,quality=quality)
    print("Provider health observations:",len(report["providers"]),"malformed:",report["malformed_events"])

if __name__=="__main__":
    main()
