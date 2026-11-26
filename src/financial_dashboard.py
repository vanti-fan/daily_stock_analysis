"""Opt-in, mobile-first financial dashboards for the Telegram development workflow.

No market/LLM calls: render only existing structured results and report tables.
"""
from __future__ import annotations

import html
import json
import logging
import os
import re
import struct
import subprocess
import tempfile
from datetime import date
from pathlib import Path

import requests

from src.share_image import (
    _extract_date, _market_data, _market_data_from_payload, _parse_tables,
    _section, _stock_data_from_payload,
)

logger = logging.getLogger(__name__)
OUTPUT = Path("reports/dashboard")
WIDTH, MAX_HEIGHT = 1080, 1800
DASHBOARD_VERSION = "0.2.1-dev"
MISSING = "—"

CSS = """
*{box-sizing:border-box}html,body{margin:0;width:1080px;background:#f1f4f8}
body{font:30px/1.45 'Noto Sans CJK SC',Arial,sans-serif;color:#15263b}
main{padding:38px 40px 30px}
header{border-bottom:4px solid #174d79;padding-bottom:20px;margin-bottom:26px}
.eyebrow{font-size:23px;letter-spacing:1.4px;color:#45637d;font-weight:700}
h1{font-size:54px;line-height:1.18;margin:10px 0 8px;overflow-wrap:anywhere}
.meta,footer{font-size:23px;color:#53667b}
h2{font-size:34px;margin:0 0 14px;color:#174d79}
section{background:#fff;border:1px solid #d5dfe9;border-radius:16px;padding:24px;margin:18px 0}
.grid{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:12px}
.metric{background:#f4f7fb;border-left:4px solid #35638a;padding:14px;min-width:0}
.metric span{display:block;font-size:24px;color:#53667b}
.metric strong{display:block;font-size:36px;line-height:1.25;overflow-wrap:anywhere;font-variant-numeric:tabular-nums}
.up{color:#c72f3a!important}.down{color:#087a58!important}.neutral{color:#53667b}
.warn{color:#946000!important}.bad{color:#a52935!important}.ok{color:#087a58!important}
table{border-collapse:collapse;width:100%;font-size:28px;table-layout:fixed}
th,td{text-align:left;padding:12px 8px;border-bottom:1px solid #dae2eb;overflow-wrap:anywhere}
th{font-size:24px;color:#526980;background:#edf2f7}
td.num,th.num{text-align:right;font-variant-numeric:tabular-nums}
small{font-size:23px;color:#53667b}p{margin:0;overflow-wrap:anywhere}
ul{margin:0;padding-left:34px}li{margin:7px 0;overflow-wrap:anywhere}
.bar{height:12px;border-radius:5px;background:#dce5ee;margin-top:9px;overflow:hidden}
.bar i{display:block;height:100%;background:#35638a}
.rank{display:grid;grid-template-columns:1fr 140px;gap:14px;padding:8px 0;border-bottom:1px solid #e5eaf0}
.rank b{text-align:right}.rank .bar{height:8px}.rank .upbar{background:#c72f3a}.rank .downbar{background:#087a58}
.notice{border-left:5px solid #b47a19;background:#fff7e8;padding:14px 18px;font-size:27px;margin:15px 0}
footer{margin-top:18px;border-top:1px solid #ccd7e2;padding-top:16px;line-height:1.5}
"""


def enabled(notifier):
    return (os.getenv("DSA_FINANCIAL_DASHBOARD", "").lower() == "true"
            and "telegram" in getattr(notifier, "_markdown_to_image_channels", ())
            and getattr(getattr(notifier, "_config", None), "md2img_engine", "") == "playwright")


def e(value):
    return html.escape(str(value if value not in (None, "") else MISSING), quote=True)


def compact(value, limit=100):
    value = re.sub(r"\s+", " ", str(value or "")).strip()
    return value if len(value) <= limit else value[:limit-1] + "…"


def numeric(value):
    try:
        s = str(value).replace(",", "").replace("%", "").replace("元", "").strip()
        if not re.fullmatch(r"[+-]?\d+(?:\.\d+)?", s):
            return None
        return float(s)
    except (TypeError, ValueError):
        return None


def percent(value):
    match = re.search(r"[+-]?\d+(?:\.\d+)?\s*%", str(value or ""))
    return match.group().replace(" ", "") if match else MISSING


def change_class(value):
    n = numeric(percent(value))
    return "up" if n is not None and n > 0 else "down" if n is not None and n < 0 else "neutral"


def page(title, stamp, body, label):
    return ('<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">'
            '<meta name="viewport" content="width=1080"><style>' + CSS +
            '</style></head><body><main><header><div class="eyebrow">DSA / FINANCIAL RESEARCH · '
            + e(label) + '</div><h1>' + e(title) + '</h1><div class="meta">'
            + e(stamp) + ' · Dashboard ' + DASHBOARD_VERSION
            + '</div></header>' + body
            + '<footer>基于现有分析结果，非实时交易指令；数据与观点须独立核验。'
            '<br>完整报告另附 · 开源基础：ZhuLinsen/daily_stock_analysis</footer></main></body></html>')


def section(title, body):
    return '<section><h2>' + e(title) + '</h2>' + body + '</section>'


def metric(label, value, cls=""):
    return '<div class="metric"><span>' + e(label) + '</span><strong class="' + cls + '">' + e(value) + '</strong></div>'


def grid(items):
    return '<div class="grid">' + "".join(metric(*item) for item in items) + '</div>'


def bullets(items, limit=100):
    vals = [compact(x, limit) for x in items if x]
    return '<ul>' + "".join('<li>'+e(x)+'</li>' for x in vals) + '</ul>' if vals else '<p class="neutral">未提供</p>'


def score_bar(value):
    n = numeric(value)
    return '<div class="bar"><i style="width:' + str(max(0, min(100, n))) + '%"></i></div>' if n is not None else ""


def table(headers, rows, classes=None):
    cells = []
    for row in rows:
        cells.append('<tr>' + "".join('<td class="' + (classes(i, v) if classes else '') + '">' + e(v) + '</td>' for i, v in enumerate(row)) + '</tr>')
    return '<table><thead><tr>' + "".join('<th>' + e(h) + '</th>' for h in headers) + '</tr></thead><tbody>' + "".join(cells) + '</tbody></table>'


def value_for(metrics, *labels):
    return next((v for k, v, _ in metrics if k in labels and v not in ("", None)), MISSING)


def batch_status(codes, results, failures):
    by_code = {str(r.code): r for r in results if getattr(r, "success", False)}
    rows = []
    for code in dict.fromkeys(str(c) for c in codes):
        r = by_code.get(code)
        rows.append({"code": code, "name": getattr(r, "name", None) or code,
                     "status": "SUCCESS" if r is not None else failures.get(code, "INCOMPLETE")})
    return rows


STATUS_LABEL = {"SUCCESS":"分析完成","TIMEOUT":"分析超时","FAILED":"分析失败",
                "ERROR":"执行异常","INCOMPLETE":"未取得有效结果","UNSUPPORTED":"不支持的标的"}


def status_text(rows):
    count = sum(x["status"] == "SUCCESS" for x in rows)
    return ("自选股分析状态：" + str(count) + "/" + str(len(rows)) + " 完成\n"
            + "\n".join(x["code"] + " " + STATUS_LABEL.get(x["status"], "未完成") for x in rows))


def save_status(rows, output=OUTPUT):
    output = Path(output)
    output.mkdir(parents=True, exist_ok=True)
    (output/"batch-status.json").write_text(json.dumps(
        {"version":DASHBOARD_VERSION,"planned":len(rows),
         "succeeded":sum(x["status"] == "SUCCESS" for x in rows),
         "stocks":rows}, ensure_ascii=False, indent=2), encoding="utf-8")


def report_for_code(report, code, fallback):
    """Use the aggregate report's complete stock section, not its poster summary."""
    starts = list(re.finditer(r"^##\s+.*$", report or "", re.M))
    for i, match in enumerate(starts):
        if re.search(r"(?<![A-Za-z0-9])" + re.escape(str(code)) + r"(?![A-Za-z0-9])", match.group()):
            end = starts[i+1].start() if i+1 < len(starts) else len(report)
            return report[match.start():end]
    return fallback


def raw_tables(markdown):
    """Retain original table cell values and units."""
    for t in _parse_tables(markdown or ""):
        yield t.headers, t.raw_rows or t.rows


def plain(value):
    return re.sub(r"\s+", " ", str(value or "").replace("**", "").replace("\x60", "")).strip()


def named_block(markdown, label):
    """Read a bold-labeled block until the next label or heading."""
    lines = (markdown or "").splitlines()
    for i, line in enumerate(lines):
        m = re.match(r"^\s*(?:[-*]\s+)?\*\*([^*]+)\*\*\s*[:：]\s*(.*)$", line)
        if not m or label not in m.group(1):
            continue
        parts = [m.group(2)] if m.group(2) else []
        for following in lines[i+1:]:
            if re.match(r"^\s*(?:#{1,6}\s|---\s*$|(?:[-*]\s+)?\*\*[^*]+\*\*\s*[:：])", following):
                break
            if following.strip():
                parts.append(re.sub(r"^\s*[-*]\s+", "", following))
        return [plain(x) for x in parts if plain(x)]
    return []


def stock_fields(markdown):
    fields = {}
    for headers, rows in raw_tables(markdown):
        if not rows:
            continue
        if {"收盘", "开盘", "最高", "最低"}.issubset(headers):
            fields.update({k:plain(v) for k,v in zip(headers, rows[0])})
        elif {"当前价", "量比", "换手率"}.issubset(headers):
            fields.update({k:plain(v) for k,v in zip(headers, rows[0])})
        elif "价格指标" in headers:
            for row in rows:
                if len(row)>=2:
                    fields[plain(row[0])] = plain(row[1])
    return fields


def leading_price(value):
    m = re.match(r"^\s*([0-9]+(?:\.[0-9]+)?)\s*(?:元)?(?:\s*$|\s*[（(])", str(value or ""))
    return float(m.group(1)) if m and float(m.group(1)) > 0 else None


def ma_structure(fields):
    values = [numeric(fields.get(k)) for k in ("MA5","MA10","MA20")]
    if any(x is None or x <= 0 for x in values):
        return "未取得完整均线数值，不能确认排列"
    a,b,c = values
    if a>b>c:
        return "MA5 > MA10 > MA20（多头排列）"
    if a<b<c:
        return "MA5 < MA10 < MA20（空头排列）"
    if a>b and b<=c:
        return "弱势多头（未形成完整多头排列）"
    if a<b and b>=c:
        return "弱势空头（未形成完整空头排列）"
    return "均线混合 / 持平"


def trade_candidates(poster, markdown):
    values = {k:v for k,v,_ in poster.sniper}
    for headers, rows in raw_tables(markdown):
        if "操作点位" not in headers:
            continue
        for row in rows:
            if len(row)<2:
                continue
            for label,key in (("理想买入","理想买入"),("次优买入","确认买入"),("止损","止损"),("目标","目标")):
                if label in row[0]:
                    values[key] = plain(row[1])
    stop = leading_price(values.get("止损"))
    target = leading_price(values.get("目标"))
    plans = []
    for key,label in (("理想买入","理想买入"),("确认买入","次优买入")):
        raw = values.get(key)
        if not raw:
            continue
        entry = leading_price(raw)
        if entry is None or stop is None or target is None:
            status = "UNVERIFIED"
        elif not stop < entry < target:
            status = "INVALID"
        else:
            status = "VALID"
        plans.append(dict(label=label,raw=raw,entry=entry,stop=stop,target=target,status=status,
                          rr=(target-entry)/(entry-stop) if status=="VALID" else None))
    return plans


def trade_html(plans):
    if not plans:
        return '<p>未提供有效候选方案</p>'
    parts = []
    for p in plans:
        parts.append('<h3>'+e(p["label"])+'</h3>')
        if p["status"] != "VALID":
            reason = "买入、止损或目标价存在多头顺序冲突" if p["status"]=="INVALID" else "价格缺失或无法唯一解析"
            parts.append('<div class="notice">交易计划待核实：'+e(reason)+'，该方案不展示可执行点位。</div>')
            continue
        parts.append(grid([("候选入场",f'{p["entry"]:.2f}',""),("止损",f'{p["stop"]:.2f}',""),
                           ("目标",f'{p["target"]:.2f}',""),("理论收益风险比",f'{p["rr"]:.2f}:1',"")]))
    parts.append('<small>仅校验多头价格顺序；不代表入场确认，不含费用、滑点或跳空风险。原始点位见 Markdown。</small>')
    return "".join(parts)


def text_sections(pages, prefix, title, stamp, items, label):
    """Bound page content; keep complete text in the original report."""
    chunks = []
    for heading, values in items:
        for value in values:
            value = plain(value)
            for i in range(0, len(value), 180):
                chunks.append((heading if i==0 else heading+"（续）",value[i:i+180]))
    if not chunks:
        return
    for i in range(0,len(chunks),3):
        body = "".join(section(heading,"<p>"+e(value)+"</p>") for heading,value in chunks[i:i+3])
        pages.append((prefix+"-"+str(i//3+1).zfill(2),page(title,stamp,body,label)))


def invalid_long_plan(poster):
    stop = numeric(value_for(poster.sniper, "止损"))
    target = numeric(value_for(poster.sniper, "目标"))
    entries = [numeric(value_for(poster.sniper, key)) for key in ("理想买入","确认买入")]
    return any(x is not None and ((stop is not None and stop >= x) or
                                  (target is not None and target <= x)) for x in entries)


def quality_metric(field, key):
    if not field.get("usable"):
        return MISSING
    v = field["value"]
    if key in ("change_pct","turnover_rate","amplitude"):
        return f"{v:+.2f}%" if key=="change_pct" else f"{v:.2f}%"
    if key in ("volume","amount"):
        unit=field.get("unit") or ""
        return f"{v/10000:,.2f} 万{unit}"
    return f"{v:,.2f}"


def quality_pages(rows, stamp, *, scenario=None):
    from src.data_quality_manager import source_coverage
    stocks=[r["data_quality"] for r in rows if "data_quality" in r]
    if not stocks:
        return []
    label="DATA QUALITY / "+(scenario or "CURRENT RUN")
    body=section("数据质量概览",grid([
        ("计划标的",len(rows),""),
        ("有效价格",sum(bool(s.get("groups",{}).get("snapshot",{}).get("price",{}).get("usable")) for s in stocks),""),
        ("AI已归档完成",sum(s.get("ai_status")=="SUCCESS" for s in stocks),"")]))
    body+=section("统计边界","<p>字段覆盖率不是接口请求成功率。缺失字段不代表供应商故障；未取得独立数据时不声明交叉验证。</p>")
    if scenario:
        body+=section("测试声明","<p>固定样本 / 非实时行情 · "+e(scenario)+"</p>")
    pages=[("quality-overview",page("数据源健康度",stamp,body,label))]
    records=[]
    for x in source_coverage(stocks):
        records.append([x["source"],{"snapshot":"行情","technical":"技术计算","chips":"筹码"}.get(x["category"],x["category"]),
                        f'{x["usable"]}/{x["expected"]}',f'{x["coverage"]:.0%}' if x["coverage"] is not None else MISSING])
    if not records:
        records=[["UNKNOWN","无有效归档",MISSING,MISSING]]
    for i in range(0,len(records),8):
        body=section("已归档字段覆盖率",table(["来源","类别","有效/总数","覆盖率"],records[i:i+8]))
        body+=section("说明","<p>UNKNOWN 表示来源未记录，不等同于供应商失败。技术计算不是外部接口。</p>")
        pages.append(("quality-sources-"+str(i//8+1).zfill(2),page("数据来源与覆盖率",stamp,body,label)))
    for i in range(0,len(stocks),6):
        rec=[[x["code"],x.get("quote_status"),x.get("history",{}).get("status"),x.get("ai_status"),x.get("batch_status")] for x in stocks[i:i+6]]
        body=section("标的状态",table(["代码","行情","日线","AI归档","任务"],rec))
        body+=section("状态定义","<p>NOT_COMPLETED：未完成；UNVERIFIED：未核实；STALE：过期；PARTIAL：部分有效。</p>")
        pages.append(("quality-stocks-"+str(i//6+1).zfill(2),page("标的数据质量",stamp,body,label)))
    return pages


def stock_pages(notifier, rows, results, report):
    stamp = _extract_date(report, date.today())
    by_code = {}
    quality_mode = any("data_quality" in row for row in rows)
    quality_by_code = {row["code"]:row.get("data_quality",{}) for row in rows}
    for result in results:
        if not getattr(result, "success", False):
            continue
        if quality_mode and quality_by_code.get(str(result.code),{}).get("ai_status") != "SUCCESS":
            continue
        payload = result.to_dict()
        if not isinstance(payload, dict):
            raise ValueError("Expected structured stock result")
        md = report_for_code(report, result.code, notifier.generate_single_stock_report(result))
        by_code[str(result.code)] = (_stock_data_from_payload(payload, md, date.today()), md)
    success = sum(x["status"] == "SUCCESS" for x in rows)
    from src.data_quality_manager import dashboard_quote
    pages = []
    # Chunk watchlists so the document is readable even with more stocks.
    for offset in range(0, len(rows), 6):
        body = section("执行覆盖率", grid([
            ("计划标的",len(rows),""),("成功",success,"ok"),("未完成",len(rows)-success,"warn")
        ]))
        records = []
        for row in rows[offset:offset+6]:
            item = by_code.get(row["code"])
            p = item[0] if item else None
            display_name = row["name"]+" / "+row["code"] if row["name"] != row["code"] else row["code"]
            if quality_mode:
                q = row.get("data_quality", {})
                price = dashboard_quote(row, "price")
                change = dashboard_quote(row, "change_pct")
                ai_ok = q.get("ai_status") == "SUCCESS" and p is not None
                display_name = q.get("name") or row["name"]
                if display_name != row["code"]:
                    display_name += " / " + row["code"]
                records.append([display_name, f"{price:.2f}" if price is not None else MISSING,
                                f"{change:+.2f}%" if change is not None else MISSING,
                                q.get("ai_score") if ai_ok and q.get("ai_score") is not None else MISSING,
                                (p.action or "分析完成") if ai_ok else STATUS_LABEL.get(row["status"],"未完成") if row["status"]!="SUCCESS" else "AI结果待核实"])
            else:
                records.append([display_name,
                                value_for(p.snapshot,"现价") if p else MISSING,
                                percent(value_for(p.snapshot,"涨跌","涨跌幅")) if p else MISSING,
                                (p.score if p.score not in ("", None) else MISSING) if p else MISSING,
                                p.action or "分析完成" if p else STATUS_LABEL.get(row["status"],"未完成")])
        if quality_mode:
            body += section("行情与AI独立覆盖率",grid([
                ("有效价格",f"{sum(dashboard_quote(r, 'price') is not None for r in rows)}/{len(rows)}",""),
                ("AI已归档完成",f"{sum(r.get('data_quality',{}).get('ai_status')=='SUCCESS' for r in rows)}/{len(rows)}",""),
                ("数据口径","当前归档","")]))
        body += section("自选股总览", table(["标的","价格","涨跌幅","评分","状态 / AI观点"], records,
                                          lambda i,v: change_class(v) if i==2 else ""))
        if success < len(rows):
            body += '<div class="notice">未完成标的不输出评分或交易点位；不得将缺失结果当作中性信号。</div>'
        pages.append(("watchlist-"+str(offset//6+1).zfill(2), page("自选股分析总览",stamp,body,"WATCHLIST")))
    for row in rows:
        if not quality_mode or row["code"] in by_code:
            continue
        quality = row.get("data_quality",{})
        fields = quality.get("groups",{}).get("snapshot",{})
        vals = [(label, quality_metric(fields.get(key,{}),key),"")
                for key,label in (("price","价格"),("change_pct","涨跌幅"),("volume_ratio","量比"),("turnover_rate","换手率"),("volume","成交量"),("amount","成交额"))]
        if not any(x.get("usable") for x in fields.values()):
            continue
        body = section("已归档客观行情",grid(vals))
        body += section("分析状态", "<p>"+e(STATUS_LABEL.get(row["status"],"未完成"))+"</p><p>AI未完成，不展示评分或交易计划。</p>")
        body += section("数据日期", "<p>预期交易日："+e(quality.get("expected_trade_date"))+" · 行情日期："+e(fields.get("price",{}).get("trade_date"))+"</p>")
        key=re.sub(r"[^A-Za-z0-9._-]","_",row["code"])
        pages.append(("stock-"+key+"-quote-only",page(quality.get("name",row["code"])+" "+row["code"],stamp,body,"STOCK / DATA ONLY")))
    for code, (p, md) in by_code.items():
        fields = stock_fields(md)
        snapshot = [(k,v,change_class(v) if k in ("涨跌","涨跌幅") else "") for k,v,_ in p.snapshot]
        technical = [(k,v,"") for k,v,_ in p.technical[:6] if k != "均线"]
        technical.insert(0, ("均线", ma_structure(fields) if all(k in fields for k in ("MA5","MA10","MA20")) else "未取得完整数值", ""))
        q = quality_by_code.get(code,{})
        score = q.get("ai_score") if quality_mode else p.score
        if quality_mode:
            sf=q.get("groups",{}).get("snapshot",{})
            tf=q.get("groups",{}).get("technical",{})
            snapshot=[(label,quality_metric(sf.get(k,{}),k),change_class(quality_metric(sf.get(k,{}),k)) if k=="change_pct" else "")
                      for k,label in (("price","现价"),("change_pct","涨跌"),("volume_ratio","量比"),("turnover_rate","换手"),("volume","成交量"),("amount","成交额"))]
            technical=[(label,quality_metric(tf.get(k,{}),k),"") for k,label in (("ma5","MA5"),("ma10","MA10"),("ma20","MA20"),("ma60","MA60"),("macd_dif","DIF"),("macd_dea","DEA"))]
        body = section("核心判断", grid([("AI观点",p.action,""),("评分",score,""),("趋势",p.trend,"")])
                       + score_bar(score) + '<p style="margin-top:15px">'+e(p.conclusion)+'</p>')
        body += section("市场快照",grid(snapshot or [("行情","未提供","neutral")]))
        body += section("技术参考",grid(technical or [("技术数据","未提供","neutral")]))
        body += '<div class="notice">数据源：'+e((q.get("groups",{}).get("snapshot",{}).get("price",{}).get("source") if quality_mode else p.data_source) or "未注明")+' · 完整数据限制见原始报告。</div>'
        key = re.sub(r"[^A-Za-z0-9._-]", "_", code)
        title = p.title+" "+code
        pages.append(("stock-"+key+"-01",page(title,stamp,body,"STOCK / SNAPSHOT")))
        # Keep execution/risk separate; suppress unsafe numerical plans.
        plans = trade_candidates(p, md)
        body = section("交易计划校验", "<p>有效行情不足，暂不展示数值交易计划。</p>" if quality_mode and q.get("quote_status")!="AVAILABLE" else trade_html(plans))
        body += section("观察条件",bullets([k+"："+v for k,v,_ in p.watch_items],76))
        pages.append(("stock-"+key+"-02",page(title,stamp,body,"STOCK / RISK")))
        if fields and not quality_mode:
            labels = ("收盘","昨收","开盘","最高","最低","涨跌幅","涨跌额","振幅","成交量","成交额","量比","换手率","行情来源","MA5","MA10","MA20","乖离率(MA5)","支撑位","压力位")
            for offset in range(0,len(labels),10):
                rows_detail = [[k,fields.get(k,MISSING)] for k in labels[offset:offset+10]]
                body = section("原报告行情及指标",table(["字段","数值 / 单位"],rows_detail))
                body += '<div class="notice">来源：原始报告行情及技术表；未归档的历史序列不作推算。</div>'
                pages.append(("stock-"+key+"-data-"+str(offset//10+1).zfill(2),page(title,stamp,body,"STOCK / DATA")))
        risks = named_block(md,"风险警报")
        catalysts = named_block(md,"利好催化")
        limits = named_block(md,"数据限制")
        extra = [("风险警报",risks),("利好催化",catalysts),("数据限制",limits),("业绩预期",named_block(md,"业绩预期")),("最新动态",named_block(md,"最新动态")),("舆情情绪",named_block(md,"舆情情绪")),("置信度理由",named_block(md,"置信度理由")),("仓位及策略",named_block(md,"仓位建议")),("信号归因（AI）",named_block(md,"归因权重")),("关联板块",[_section(md,"关联板块")] if _section(md,"关联板块") else []),("持仓观察",[( "未持仓："+p.no_position) if p.no_position else "",("已持仓："+p.has_position) if p.has_position else ""])]
        text_sections(pages,"stock-"+key+"-research",title,stamp,extra,"STOCK / RESEARCH")
    return pages


def is_market_report(content, payload=None):
    """Only customize the supported Chinese A-share single-market report."""
    content = content or ""
    region = str(payload.get("region") or "").lower() if isinstance(payload, dict) else ""
    if region and region != "cn":
        return False
    if isinstance(payload, dict) and payload.get("markets"):
        return False
    headings = re.findall(r"^#{1,2}\s+(.+)$", content, re.M)
    metadata = re.search(r"^\[dsa-market-region\]:\s+#\s+\(\s*([a-z,]+)\s*\)", content, re.M | re.I)
    if metadata and metadata.group(1).lower() != "cn":
        return False
    if any(x in content for x in ("美股大盘复盘", "港股大盘复盘", "日股大盘复盘", "韩股大盘复盘", "台股大盘复盘", "个股决策仪表盘")):
        return False
    return bool(("大盘复盘" in content[:300] or any("大盘复盘" in h for h in headings[:2]))
                and (region == "cn" or metadata or "A股" in content[:500])
                and "指数结构" in content and "板块主线" in content)


def market_pages(content, payload=None):
    p = (_market_data_from_payload(payload, content, date.today()) if isinstance(payload, dict)
         else _market_data(content, date.today()))
    stamp = p.report_date or _extract_date(content,date.today())
    indices = [[n,v,percent(c)] for n,v,c,_ in p.indices]
    sector_groups = []
    for t in _parse_tables(content):
        if "指数" in t.headers and "最新" in t.headers and "涨跌幅" in t.headers:
            ix = [t.headers.index(k) for k in ("指数","最新","涨跌幅")]
            indices = [[r[ix[0]],r[ix[1]],percent(r[ix[2]])] for r in t.rows]
        if "行业板块" in t.headers and "涨跌幅" in t.headers:
            ni,ci = t.headers.index("行业板块"),t.headers.index("涨跌幅")
            group = [(r[ni],percent(r[ci])) for r in t.rows if numeric(percent(r[ci])) is not None]
            if group:
                sector_groups.append(group)
    pages = []
    overview = _section(content,"盘面总览")
    unavailable = any(k in overview for k in ("暂无有效统计","数据缺失","数据暂缺","未完整","暂无有效数据","不可用"))
    body = section("市场状态",grid([("市场评分",p.score,""),("温度",p.temperature,""),("信号",p.signal,"")])
                   + score_bar(p.score) + '<p style="margin-top:15px">'+e(p.guidance or p.summary)+'</p>')
    body += section("主要指数",table(["指数","点位","涨跌幅"],indices[:8],lambda i,v: change_class(v) if i==2 else ""))
    breadth = [] if unavailable else [(k,v,"") for k,v,_ in p.breadth]
    body += section("市场宽度",grid(breadth[:6]) if breadth else '<p class="neutral">未取得有效市场宽度 / 成交额数据</p>')
    if unavailable or not breadth:
        body += '<div class="notice">市场统计数据未完整提供；市场评分为原报告模型观点，不代表完整市场宽度。</div>'
    pages.append(("market-01",page("A股市场总览",stamp,body,"MARKET / OVERVIEW")))
    leaders = sector_groups[0] if sector_groups else [(n,percent(c)) for n,c,_ in p.sectors]
    laggards = sector_groups[1] if len(sector_groups)>1 else [(n,percent(c)) for n,c,_ in p.laggards]
    all_values = [abs(numeric(c)) for _,c in leaders+laggards if numeric(c) is not None]
    scale = max(all_values,default=1) or 1
    def ranking(rows):
        parts = []
        for n,c in rows[:5]:
            v = numeric(c)
            cls = change_class(c)
            w = abs(v)/scale*100 if v is not None else 0
            bar = '<div class="bar"><i class="'+("upbar" if cls=="up" else "downbar")+'" style="width:'+str(w)+'%"></i></div>'
            parts.append('<div class="rank"><div>'+e(n)+bar+'</div><b class="'+cls+'">'+e(c)+'</b></div>')
        return "".join(parts) or '<p class="neutral">未取得有效数据</p>'
    body = section("行业领涨",ranking(leaders)) + section("行业领跌",ranking(laggards))
    if "概念板块" in content and any(k in content for k in ("概念板块领涨领跌数据暂缺","概念板块数据缺失","概念板块数据暂缺")):
        body += '<div class="notice">概念板块：数据缺失。</div>'
    pages.append(("market-02",page("行业板块强弱",stamp,body,"MARKET / SECTORS")))
    body = section("策略与关注方向",bullets(p.plan + p.focus + p.avoid,90))
    body += section("消息催化",bullets(p.catalysts,85))
    body += section("风险与数据边界",bullets(p.risks or [compact(_section(content,"风险提示"),130)],100))
    pages.append(("market-03",page("市场观察与风险",stamp,body,"MARKET / OUTLOOK")))
    # Additional detail only when present in the real source; preserve full units.
    for headers, source_rows in raw_tables(content):
        if {"指数","开盘","最高","最低"}.issubset(headers):
            cols = [k for k in ("指数","开盘","最高","最低","振幅","成交额(亿)") if k in headers]
            ix = [headers.index(k) for k in cols]
            details = [[plain(row[j]) if j<len(row) else MISSING for j in ix] for row in source_rows]
            body = section("指数详细行情",table(cols,details))
            body += '<div class="notice">各指数成交额并非互斥口径，不直接相加作为全市场成交额。</div>'
            pages.append(("market-index-detail",page("指数详细行情",stamp,body,"MARKET / DATA")))
            break
    if any(k in content for k in ("### 五、消息催化","### 六、明日交易计划")):
        text_sections(pages,"market-research","市场研究详情",stamp,[(k,[_section(content,k)]) for k in ("资金与情绪","消息催化","明日交易计划","风险提示","数据边界") if _section(content,k)],"MARKET / RESEARCH")
    return pages


NODE_RENDER = """
const fs=require('fs'), path=require('path');
const {chromium}=require(path.join(process.argv[1],'playwright'));
const manifest=JSON.parse(fs.readFileSync(process.argv[2],'utf8'));
(async()=>{
 const browser=await chromium.launch({headless:true});
 try {
  const page=await browser.newPage({viewport:{width:1080,height:900},deviceScaleFactor:2});
  await page.route('**/*',route=>route.abort());
  for(const item of manifest){
   await page.setContent(fs.readFileSync(item.html,'utf8'));
   await page.evaluate(()=>document.fonts.ready);
   const size=await page.evaluate(()=>({w:document.documentElement.scrollWidth,h:document.documentElement.scrollHeight}));
   if(size.w>1080||size.h>1800)throw new Error('Dashboard page exceeds layout bounds: '+item.name+' '+JSON.stringify(size));
   await page.screenshot({path:item.png,fullPage:true,scale:'device',animations:'disabled'});
  }
 }finally{await browser.close();}
})().catch(e=>{console.error(String(e));process.exit(1);});
"""


def render_pages(pages, output=OUTPUT):
    output=Path(output)
    output.mkdir(parents=True,exist_ok=True)
    if not pages:
        raise ValueError("No dashboard pages")
    root=subprocess.check_output(["npm","root","-g"],text=True,timeout=15).strip()
    paths=[]
    with tempfile.TemporaryDirectory() as directory:
        manifest=[]
        for name, doc in pages:
            if not re.fullmatch(r"[A-Za-z0-9._-]+",name):
                raise ValueError("Invalid dashboard filename")
            source=Path(directory)/(name+".html")
            source.write_text(doc,encoding="utf-8")
            png=(output/(name+".png")).resolve()
            png.unlink(missing_ok=True)
            paths.append(png)
            manifest.append({"name":name,"html":str(source),"png":str(png)})
        manifest_path=Path(directory)/"manifest.json"
        manifest_path.write_text(json.dumps(manifest),encoding="utf-8")
        subprocess.run(["node","-e",NODE_RENDER,root,str(manifest_path)],check=True,timeout=150)
    for path in paths:
        with path.open("rb") as f:
            prefix=f.read(24)
        if prefix[:8]!=b"\x89PNG\r\n\x1a\n" or prefix[12:16]!=b"IHDR":
            raise ValueError("Invalid PNG")
        w,h=struct.unpack(">II",prefix[16:24])
        if w!=WIDTH*2 or not 0<h<=MAX_HEIGHT*2 or path.stat().st_size>10_000_000:
            raise ValueError("Dashboard PNG outside bounds")
        logger.info("HD dashboard generated: %s (%dx%d)",path.name,w,h)
    return paths


def send_file(notifier, filename, data, *, photo=False, caption=""):
    if not notifier._is_telegram_configured():
        return False
    config=notifier._telegram_config
    method, field = ("sendPhoto","photo") if photo else ("sendDocument","document")
    mime="image/png" if filename.endswith(".png") else "text/markdown"
    fields={"chat_id":config["chat_id"],"caption":caption[:900]}
    if config.get("message_thread_id"):
        fields["message_thread_id"]=config["message_thread_id"]
    try:
        response=requests.post("https://api.telegram.org/bot"+config["bot_token"]+"/"+method,
                               data=fields,files={field:(filename,data,mime)},timeout=45)
        ok=response.status_code==200 and bool(response.json().get("ok"))
        logger.info("Dashboard %s %s: HTTP %s, ok=%s",method,filename,response.status_code,ok)
        return ok
    except Exception as exc:
        # Never log a request URL, response body or credentials.
        logger.warning("Dashboard %s failed (%s)",method,type(exc).__name__)
        return False


def deliver(notifier, pages, report, label, status=""):
    fallback=(status+"\n\n" if status else "")+report
    try:
        OUTPUT.mkdir(parents=True, exist_ok=True)
        if report.strip():
            (OUTPUT/(label+"-full-report.md")).write_text(report, encoding="utf-8")
        paths=render_pages(pages)
        # Preview first overview; every original is separately available without photo compression.
        send_file(notifier,paths[0].name,paths[0].read_bytes(),photo=True,caption=label+" · 预览")
        success=True
        for path in paths:
            ok=send_file(notifier,path.name,path.read_bytes(),caption=label+" · 高清原图")
            success=ok and success
        if report.strip():
            ok=send_file(notifier,label+"-full-report.md",report.encode("utf-8"),caption="完整原始报告")
            success=ok and success
        if success:
            return True
        logger.warning("Dashboard delivery partially failed; falling back to text")
    except Exception as exc:
        logger.warning("Dashboard rendering failed (%s); falling back to text",type(exc).__name__)
    return bool(notifier.send_to_telegram(fallback))


def send_batch(notifier, rows, results, report):
    try:
        pages=stock_pages(notifier,rows,results,report)
        pages+=quality_pages(rows,_extract_date(report,date.today()))
    except Exception as exc:
        logger.warning("Stock dashboard build failed (%s)",type(exc).__name__)
        return bool(notifier.send_to_telegram(status_text(rows)+"\n\n"+report))
    return deliver(notifier,pages,report,"watchlist",status_text(rows))


def send_market(notifier, report, payload):
    try:
        pages=market_pages(report,payload)
    except Exception as exc:
        logger.warning("Market dashboard build failed (%s)",type(exc).__name__)
        return bool(notifier.send_to_telegram(report))
    return deliver(notifier,pages,report,"market")


def notify_status_only(notifier, rows):
    """Status notification for zero results and non-aggregate modes; honor routing."""
    if not rows or not notifier.is_available():
        return False
    if not notifier.should_broadcast_static_channels():
        return bool(notifier.send(status_text(rows), route_type="report"))
    channels = notifier.get_channels_for_route("report", channels=notifier.get_available_channels())
    if not any(getattr(ch, "value", None) == "telegram" for ch in channels):
        return False
    return send_batch(notifier, rows, [], "") if all(
        r["status"] != "SUCCESS" for r in rows
    ) else bool(notifier.send_to_telegram(status_text(rows)))
