"""Phase 2.2: immutable, per-stage archive of already acquired financial data.

No provider, database, LLM or network calls. Opt-in via DSA_RAW_DATA_ARCHIVE.
The archive is normalized upstream input, not original vendor HTTP responses.
"""
from __future__ import annotations

import csv
import hashlib
import io
import json
import logging
import os
import re
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Mapping
from uuid import uuid4

from src.financial_data_model import SCHEMA_VERSION, StockIdentity, json_safe
from src.financial_data_adapters import build_stock_data, research_fields, read, string

logger = logging.getLogger(__name__)
ARCHIVE_VERSION = "1.0"
HISTORY_KEYS = ("code", "date", "open", "high", "low", "close", "volume",
                "amount", "pct_chg", "ma5", "ma10", "ma20", "volume_ratio", "data_source")
QUOTE_KEYS = ("code", "name", "source", "fetched_at", "provider_timestamp", "quote_date",
              "is_stale", "stale_seconds", "fallback_from", "market", "currency", "data_quality",
              "missing_fields", "price", "change_pct", "change_amount", "volume", "amount",
              "volume_ratio", "turnover_rate", "amplitude", "open_price", "high", "low",
              "pre_close", "pe_ratio", "pb_ratio", "total_mv", "circ_mv", "change_60d",
              "high_52w", "low_52w", "field_sources")
CHIP_KEYS = ("code", "date", "source", "profit_ratio", "avg_cost", "cost_90_low",
             "cost_90_high", "cost_70_low", "cost_70_high", "concentration_90", "concentration_70",
             "data_kind", "price_basis", "present_fields", "evidence")
TREND_KEYS = ("code", "ma5", "ma10", "ma20", "ma60", "bias_ma5", "bias_ma10", "bias_ma20",
              "volume_ratio_5d", "macd_dif", "macd_dea", "macd_bar", "rsi_6", "rsi_12",
              "rsi_24", "trend_strength", "signal_score", "trend_status", "buy_signal")
FUNDAMENTAL_KEYS = ("code", "status", "coverage", "source_chain", "fundamental",
                    "valuation", "financial", "belong_boards", "capital_flow", "lhb",
                    "corporate_events", "growth", "earnings", "institution", "dragon_tiger", "boards", "errors")
MARKET_KEYS = ("region", "date", "generated_at", "indices", "breadth", "sectors",
               "concepts", "market_light", "market_scope", "market_data_quality_version", "index_coverage", "financial_market_data")
RESEARCH_KEYS = ("code", "success", "sentiment_score", "trend_prediction",
                 "operation_advice", "confidence_level", "model_used", "query_id",
                 "analysis_summary", "trend_analysis", "technical_analysis",
                 "fundamental_analysis", "news_summary", "risk_warning",
                 "dashboard", "key_points", "search_performed", "news_result_count")
# Defense in depth for nested upstream payloads; never archive config, raw prompts,
# portfolio context, request headers, full diagnostics, or environment variables.
SENSITIVE = re.compile(r"token|api[_-]?key|secret|password|authorization|credential|cookie|"
                       r"portfolio|account|headers|prompt|raw_response", re.I)


def enabled():
    return os.environ.get("DSA_RAW_DATA_ARCHIVE", "").lower() == "true"


def sanitize(value):
    if isinstance(value, Mapping):
        return {k:sanitize(v) for k,v in value.items()
                if isinstance(k,str) and not SENSITIVE.search(k)}
    if isinstance(value,(list,tuple)):
        return [sanitize(v) for v in value]
    return json_safe(value)


def project(obj, keys):
    if obj is None:
        return None
    return sanitize({k:read(obj,k) for k in keys if read(obj,k) is not None})


def json_bytes(obj):
    return (json.dumps(sanitize(obj), ensure_ascii=False, allow_nan=False,
                       indent=2, sort_keys=True)+"\n").encode("utf-8")


def csv_bytes(rows):
    out = io.StringIO(newline="")
    writer = csv.DictWriter(out, fieldnames=HISTORY_KEYS, lineterminator="\n")
    writer.writeheader()
    writer.writerows(rows)
    return out.getvalue().encode("utf-8")


def safe_segment(value):
    value = str(value)
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,100}", value):
        return "id-" + hashlib.sha256(value.encode()).hexdigest()[:24]
    return value


def archive_root():
    run = os.environ.get("GITHUB_RUN_ID") or "local"
    attempt = os.environ.get("GITHUB_RUN_ATTEMPT") or "1"
    return Path("reports/raw_data") / (safe_segment(run)+"-"+safe_segment(attempt))


def publish(root, *, kind, query_id, identity, files, metadata=None):
    """Atomic publication of a complete bundle. No overwriting, safe for workers."""
    root = Path(root)
    parent = root / safe_segment(kind) / safe_segment(identity) / safe_segment(query_id)
    parent.mkdir(parents=True, exist_ok=True)
    target = parent / uuid4().hex
    with tempfile.TemporaryDirectory(prefix=".staging-", dir=parent) as tmp:
        stage = Path(tmp) / "bundle"
        stage.mkdir()
        entries = []
        for name, content in sorted(files.items()):
            if not re.fullmatch(r"[A-Za-z0-9_-]+\.(json|csv)",name) or name=="manifest.json":
                raise ValueError("Invalid archive file name")
            (stage/name).write_bytes(content)
            entries.append({"path":name,"bytes":len(content),"sha256":hashlib.sha256(content).hexdigest()})
        manifest = {"archive_version":ARCHIVE_VERSION,"schema_version":SCHEMA_VERSION,
                    "kind":kind,"identity":identity,"query_id":query_id,
                    "archived_at":datetime.now(timezone.utc).isoformat(),
                    "files":entries,"metadata":metadata or {}}
        (stage/"manifest.json").write_bytes(json_bytes(manifest))
        stage.rename(target)
    return target


def archive_inputs(root, *, code, name, market, query_id, rows=None, quote=None,
                   trend=None, trend_input_count=None, trend_trade_date=None,
                   chip=None, fundamental=None, expected_trade_date=None, history_metadata=None):
    raw_rows = [project(row,HISTORY_KEYS) for row in (rows if rows is not None else [])]
    sources = sorted({r["data_source"] for r in raw_rows if r.get("data_source")})
    currency = string(read(quote,"currency")) or {
        "cn":"CNY","hk":"HKD","us":"USD","jp":"JPY","kr":"KRW","tw":"TWD"
    }.get(market)
    identity = StockIdentity(code,name,market,currency)
    # The DB schema specifies share and currency units for normalized daily rows.
    # Adjustment is not retained by StockDaily; do not label these as unadjusted.
    files = {
        "ohlcv_daily.csv":csv_bytes(raw_rows),
        "quote.json":json_bytes(project(quote,QUOTE_KEYS)),
        "technical.json":json_bytes({"input_count":trend_input_count,
                                     "input_trade_date":trend_trade_date,
                                     "values":project(trend,TREND_KEYS)}),
        "chip.json":json_bytes(project(chip,CHIP_KEYS)),
        "fundamental.json":json_bytes(project(fundamental,FUNDAMENTAL_KEYS)),
    }
    metadata = {"history_count":len(raw_rows),"history_sources":sources,
                "history_origin":"stored_daily_bars_before_realtime_augmentation",
                "history_adjustment":(history_metadata or {}).get("adjustment"),"history_evidence":history_metadata,
                "volume_unit":"share","amount_unit":currency,
                "expected_trade_date":expected_trade_date,
                "availability":{"history":bool(raw_rows),"quote":quote is not None,
                                "technical":trend is not None,"chip":chip is not None,
                                "fundamental":fundamental is not None}}
    try:
        model=build_stock_data(identity,quote=quote,history=raw_rows or None,
                              history_source=sources[0] if len(sources)==1 and all(r.get("data_source") for r in raw_rows) else None,
                              volume_unit="share",amount_unit=currency,trend=trend,
                              trend_sample_count=trend_input_count,
                              trend_trade_date=trend_trade_date,chip=chip)
        files["financial_data.json"]=json_bytes(model.to_dict())
        metadata["normalization_status"]="AVAILABLE"
    except Exception as exc:
        # Preserve raw input even when the normalized contract rejects it.
        metadata["normalization_status"]="INVALID"
        metadata["normalization_error_type"]=type(exc).__name__
    return publish(root,kind="stock-inputs",query_id=query_id,identity=code,
                   files=files,metadata=metadata)


def archive_research(root, *, code, query_id, result):
    payload=project(result,RESEARCH_KEYS)
    return publish(root,kind="stock-research",query_id=query_id,identity=code,
                   files={"ai_research.json":json_bytes(payload)},
                   metadata={"analysis_success":read(result,"success"),
                             "actual_model":string(read(result,"model_used"))})


def archive_market(root, *, region, query_id, payload=None, snapshot=None, reused=False):
    data=project(payload,MARKET_KEYS) or {}
    nested=read(payload,"markets")
    if isinstance(nested,Mapping):
        data["markets"]={k:project(v,MARKET_KEYS) for k,v in nested.items()}
    return publish(root,kind="market",query_id=query_id,identity=region,
                   files={"market_data.json":json_bytes(data or None),
                          "market_snapshot.json":json_bytes(snapshot),
                          "market_research.json":json_bytes(project(payload,("region","date","title","sections","markdown_report")))},
                   metadata={"reused":reused,"structured_available":bool(data.get("indices") or data.get("markets")),
                             "breadth_available":bool(data.get("breadth")),
                             "snapshot_available":snapshot is not None})


def archive_news(root, *, code, query_id, payload):
    if payload.get("stock_code") != code or payload.get("schema_version") != "1.0":
        raise ValueError("News identity or schema mismatch")
    return publish(root,kind="stock-news",query_id=query_id,identity=code,
                   files={"news_events.json":json_bytes(payload)},
                   metadata={"status":payload.get("status"),"dimension":payload.get("dimension")})

def record(stage, **kwargs):
    """Production hook: fail-open; never perform external requests or log payloads."""
    if not enabled():
        return None
    try:
        fn={"inputs":archive_inputs,"research":archive_research,"market":archive_market,"news":archive_news}[stage]
        path=fn(archive_root(),**kwargs)
        logger.info("Raw archive published: %s",path)
        return path
    except Exception as exc:
        logger.warning("Raw archive failed: stage=%s error_type=%s",stage,type(exc).__name__)
        return None


def verify(root):
    """Verify exact file set and SHA256 of every completed bundle."""
    root=Path(root)
    manifests=sorted(p for p in root.rglob("manifest.json")
                     if not any(x.startswith(".staging-") for x in p.relative_to(root).parts))
    index=[]
    for path in manifests:
        manifest=json.loads(path.read_text(encoding="utf-8"))
        expected={x["path"] for x in manifest["files"]}
        actual={p.name for p in path.parent.iterdir() if p.name!="manifest.json"}
        if actual!=expected or len(expected)!=len(manifest["files"]):
            raise ValueError("Archive file set mismatch")
        for entry in manifest["files"]:
            name=entry["path"]
            if not re.fullmatch(r"[A-Za-z0-9_-]+\.(json|csv)",name):
                raise ValueError("Invalid manifest path")
            data=(path.parent/name).read_bytes()
            if len(data)!=entry["bytes"] or hashlib.sha256(data).hexdigest()!=entry["sha256"]:
                raise ValueError("Archive digest mismatch: "+name)
        index.append({"manifest":path.relative_to(root).as_posix(),
                      "sha256":hashlib.sha256(path.read_bytes()).hexdigest(),
                      "kind":manifest["kind"],"identity":manifest["identity"],
                      "query_id":manifest["query_id"]})
    return index


def write_index(root):
    root=Path(root)
    index=verify(root)
    root.mkdir(parents=True,exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=root,delete=False) as tmp:
        tmp.write(json_bytes({"archive_version":ARCHIVE_VERSION,"bundles":index,
                              "bundle_count":len(index)}))
        name=tmp.name
    os.replace(name,root/"run-index.json")
    return len(index)
