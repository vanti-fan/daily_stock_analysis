"""Phase 2.4.1: offline, source-grounded capability audit.

No provider imports, config singleton, secrets, network, or runtime routing changes.
Implementation presence is distinct from account entitlement and observed health.
"""
from __future__ import annotations

import argparse
import ast
import json
from pathlib import Path
from hashlib import sha256

BASELINE = "8455df42ab69001cc855ac6d24706bf74b65fa88"
VERSION = "1.0"
METHODS = {
    "daily": "_fetch_raw_data", "quote": "get_realtime_quote",
    "indices": "get_main_indices", "breadth": "get_market_stats",
    "industry": "get_sector_rankings", "concept": "get_concept_rankings",
    "chip": "get_chip_distribution", "boards": "get_belong_boards",
}
# Only reviewed paths are assigned a dependency group. Unknown != independent.
# A provider can have multiple distinct upstreams for one capability.
PATHS = (
    ("efinance","daily","eastmoney","ef.stock.get_quote_history","QFQ_REQUESTED"),
    ("efinance","quote","eastmoney","ef.stock.get_realtime_quotes",None),
    ("efinance","breadth","eastmoney","ef.stock.get_realtime_quotes",None),
    ("akshare","daily","eastmoney","stock_zh_a_hist","QFQ_REQUESTED"),
    ("akshare","daily","sina","stock_zh_a_daily","QFQ_REQUESTED"),
    ("akshare","daily","tencent","stock_zh_a_hist_tx","QFQ_REQUESTED"),
    ("akshare","quote","eastmoney","stock_zh_a_spot_em",None),
    ("akshare","quote","sina","stock_zh_a_spot",None),
    ("akshare","quote","tencent","qt.gtimg.cn/q",None),
    ("akshare","breadth","eastmoney","stock_zh_a_spot_em",None),
    ("akshare","breadth","sina","stock_zh_a_spot",None),
    ("akshare","industry","eastmoney","stock_board_industry_name_em",None),
    ("akshare","industry","sina","stock_sector_spot",None),
    ("akshare","concept","eastmoney","stock_board_concept_name_em",None),
    ("akshare","chip","eastmoney","stock_cyq_em",None),
    ("tencent","daily","tencent","appstock/app/fqkline/get","QFQ_REQUESTED"),
    ("baostock","daily","baostock","query_history_k_data_plus","QFQ_REQUESTED"),
    ("tushare","daily","tushare","daily","UNADJUSTED"),
    ("tushare","indices","tushare","index_daily",None),
    ("tushare","chip","tushare","cyq_chips",None),
    ("tushare","industry","tushare","moneyflow_ind_ths",None),
    ("tushare","industry","tushare","moneyflow_ind_dc",None),
)
# Explicit A-share quote route mapping from manager; these are route identifiers,
# not Python package names. Do not change the actual manager's order.
QUOTE_ROUTES = {
    "tencent":("akshare","tencent"), "akshare_qq":("akshare","tencent"),
    "akshare_sina":("akshare","sina"), "akshare_em":("akshare","eastmoney"),
    "efinance":("efinance","eastmoney"), "tushare":("tushare","UNKNOWN"),
    "tickplus":("tickplus","UNKNOWN"), "tickflow":("tickflow","UNKNOWN"),
}
DEFAULT_QUOTE_ORDER = "tencent,akshare_sina,efinance,akshare_em"
# Documentation snapshot, NOT an account entitlement check or API response.
TUSHARE_DOCS = (
    ("daily",120,27),("stock_basic",2000,25),("trade_cal",2000,26),
    ("daily_basic",2000,32),("fund_daily",5000,127),("cyq_perf",5000,293),
    ("cyq_chips",None,None),("quotation",None,None),("rt_k",None,None),
    ("index_daily",None,None),("moneyflow_ind_ths",None,None),
    ("moneyflow_ind_dc",None,None),
)
OPTIONAL = {"tushare","tickplus","tickflow","miaoxiang","longbridge","futu","finnhub","alphavantage"}
DEFAULT = {"efinance","akshare","pytdx","baostock","yfinance","tencent"}

def _source(root, path):
    return (Path(root)/path).read_text(encoding="utf-8")

def _classes(source):
    return [node for node in ast.parse(source).body if isinstance(node,ast.ClassDef)]

def _methods(cls):
    return {node.name:node for node in cls.body if isinstance(node,(ast.FunctionDef,ast.AsyncFunctionDef))}

def _calls(node):
    return {x.func.id if isinstance(x.func,ast.Name) else x.func.attr
            for x in ast.walk(node) if isinstance(x,ast.Call)
            and isinstance(x.func,(ast.Name,ast.Attribute))}

def inventory(root):
    """Report locally declared methods only; do not count inherited base stubs."""
    out=[]
    for path in sorted((Path(root)/"data_provider").glob("*_fetcher.py")):
        source=path.read_text(encoding="utf-8")
        rel=path.relative_to(root).as_posix()
        for cls in _classes(source):
            if not cls.name.endswith("Fetcher"):
                continue
            methods=_methods(cls)
            key=path.stem.removesuffix("_fetcher")
            out.append({"provider":key,"class":cls.name,"source":rel,
                "registration":"DEFAULT" if key in DEFAULT else "CONDITIONAL" if key in OPTIONAL else "NOT_REVIEWED",
                "capabilities":{k:{"implementation":"DECLARED" if m in methods else "NOT_DECLARED",
                    "line":methods[m].lineno if m in methods else None}
                    for k,m in METHODS.items()},
                "entitlement":"UNVERIFIED","runtime_health":"NOT_OBSERVED"})
    return out

def quote_routes(priority):
    routes=[]
    seen=set()
    for token in priority.split(","):
        key=token.strip().lower()
        if not key:
            continue
        if key not in QUOTE_ROUTES:
            # Do not emit arbitrary config values or potential secrets.
            routes.append({"route":"UNKNOWN","provider":"UNKNOWN","dependency":"UNKNOWN","status":"UNREVIEWED"})
            continue
        provider,dependency=QUOTE_ROUTES[key]
        routes.append({"route":key,"provider":provider,"dependency":dependency,
                       "status":"DUPLICATE_ALIAS" if (provider,dependency) in seen else "REVIEWED"})
        seen.add((provider,dependency))
    return routes

def tushare_permissions(points):
    if points is not None and (type(points) is not int or points<0):
        raise ValueError("points must be a nonnegative integer or None")
    rows=[]
    for endpoint,threshold,doc in TUSHARE_DOCS:
        status=("UNKNOWN" if points is None or threshold is None else
                "DOCUMENTED_THRESHOLD_MET" if points>=threshold else "BELOW_DOCUMENTED_THRESHOLD")
        rows.append(dict(endpoint=endpoint,min_points=threshold,assessment=status,
                         account_entitlement="UNVERIFIED",
                         documentation="https://tushare.pro/document/2?doc_id="+str(doc) if doc else None))
    return rows

def reviewed_paths(root):
    rows=[]
    for provider,category,dependency,endpoint,adjustment in PATHS:
        path=f"data_provider/{provider}_fetcher.py"
        source=_source(root,path)
        # Endpoint anchors are a conservative drift signal, not proof of reachability.
        present=endpoint in source
        rows.append(dict(provider=provider,category=category,dependency=dependency,
                         endpoint=endpoint,adjustment=adjustment,source=path,
                         source_anchor_present=present,runtime_health="NOT_OBSERVED"))
    return rows

def common_dependencies(paths):
    groups={}
    for p in paths:
        if p["source_anchor_present"]:
            groups.setdefault((p["category"],p["dependency"]),set()).add(p["provider"])
    return [{"category":category,"dependency":dependency,"providers":sorted(providers)}
            for (category,dependency),providers in sorted(groups.items()) if len(providers)>1]

def manager_audit(root):
    path="data_provider/base.py"
    source=_source(root,path)
    manager=next(c for c in _classes(source) if c.name=="DataFetcherManager")
    methods=_methods(manager)
    names=("get_daily_data","get_realtime_quote","get_chip_distribution","get_main_indices",
           "get_market_stats","_get_sector_rankings_with_meta","get_concept_rankings",
           "get_fundamental_context")
    return [{"method":name,"line":methods[name].lineno if name in methods else None,
             "records_provider_start":"record_provider_run_started" in _calls(methods[name]) if name in methods else False,
             "records_provider_result":"record_provider_run" in _calls(methods[name]) if name in methods else False}
            for name in names]

def build_audit(root, *, points=None, priority=DEFAULT_QUOTE_ORDER, profile="STATIC"):
    root=Path(root)
    paths=reviewed_paths(root)
    provider_rows=inventory(root)
    if not provider_rows or any(not row["source_anchor_present"] for row in paths):
        raise ValueError("Missing reviewed source evidence; audit requires re-review")
    sources={p["source"] for p in paths}|{p["source"] for p in provider_rows}|{
        "data_provider/base.py","src/config.py","src/services/run_diagnostics.py",
        "data_provider/fundamental_adapter.py","src/search_service.py",".github/workflows/00-daily-analysis.yml"}
    return {"schema_version":VERSION,"accepted_baseline":BASELINE,"profile":profile,
            "source_sha256":{p:sha256((root/p).read_bytes()).hexdigest() for p in sorted(sources)},
            "provider_inventory":provider_rows,"reviewed_paths":paths,
            "shared_dependencies":common_dependencies(paths),"quote_routes":quote_routes(priority),
            "tushare":{"declared_points":points,"documentation_checked":"2026-11-27",
                       "account_entitlement":"UNVERIFIED","interfaces":tushare_permissions(points)},
            "manager_instrumentation":manager_audit(root),
            "supplementary_paths":[
                {"category":"fundamental","provider":"AkshareFundamentalAdapter","source":"data_provider/fundamental_adapter.py","runtime_health":"NOT_OBSERVED"},
                {"category":"news","provider":"BochaSearchProvider","source":"src/search_service.py","runtime_health":"NOT_OBSERVED"},
                {"category":"news","provider":"TavilySearchProvider","source":"src/search_service.py","runtime_health":"NOT_OBSERVED"}],
            "findings":[
                "Tushare daily unadjusted differs from qfq-requesting paths; do not splice without explicit adjustment reconciliation.",
                "Quote supplementation can copy fields from another source; field-level lineage and comparable timestamps require review.",
                "Reuse existing run_diagnostics and circuit breakers before adding request instrumentation.",
                "Provider declarations do not prove active routing, market coverage, permissions or live success.",
                "Unknown upstream dependency must not be treated as independent."],
            "network_requests":0}

def render_markdown(report):
    lines=["# Phase 2.4.1 — Data Source Capability Audit","",
           "Accepted baseline: "+report["accepted_baseline"],
           "Profile: "+report["profile"],"",
           "Static source audit only. Runtime health and entitlements are unverified.","",
           "| Provider | Registration | Declared capabilities |","|---|---|---|"]
    for row in report["provider_inventory"]:
        lines.append("| "+row["class"]+" | "+row["registration"]+" | "+
                     ", ".join(k for k,v in row["capabilities"].items() if v["implementation"]=="DECLARED")+" |")
    lines+=["","## Reviewed quote routes","", "| Route | Provider | Dependency |","|---|---|---|"]
    for r in report["quote_routes"]:
        lines.append("| "+r["route"]+" | "+r["provider"]+" | "+r["dependency"]+" |")
    lines+=["","## Tushare documented thresholds","", "| Endpoint | Minimum points | Assessment |","|---|---|---|"]
    for r in report["tushare"]["interfaces"]:
        lines.append(f'| {r["endpoint"]} | {r["min_points"] if r["min_points"] is not None else "UNKNOWN"} | {r["assessment"]} |')
    lines+=["","## Shared dependencies",""]
    lines += [f'- {r["category"]}: {r["dependency"]} — {", ".join(r["providers"])}' for r in report["shared_dependencies"]]
    lines+=["","## Findings",""]+["- "+x for x in report["findings"]]
    return "\n".join(lines)+"\n"

def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root",type=Path,default=Path("."))
    parser.add_argument("--output",type=Path,required=True)
    parser.add_argument("--tushare-points",type=int)
    parser.add_argument("--realtime-priority",default=DEFAULT_QUOTE_ORDER)
    args=parser.parse_args()
    report=build_audit(args.root,points=args.tushare_points,priority=args.realtime_priority,
                       profile="DECLARED_INPUTS_NOT_LIVE_CONFIG")
    args.output.mkdir(parents=True,exist_ok=True)
    (args.output/"capability-matrix.json").write_text(json.dumps(report,ensure_ascii=False,indent=2)+"\n",encoding="utf-8")
    (args.output/"capability-matrix.md").write_text(render_markdown(report),encoding="utf-8")
    print("Static capability audit complete; providers:",len(report["provider_inventory"]))

if __name__=="__main__":
    main()
