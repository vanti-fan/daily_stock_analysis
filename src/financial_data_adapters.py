"""Adapters from upstream structured financial objects; no fetches or I/O."""
from __future__ import annotations

from datetime import date, datetime
from typing import Any, Mapping

from src.financial_data_model import (
    AIResearchResult, ChipDistribution, DataQualityRecord, DataStatus,
    ExecutionMetadata, FinancialField, FundamentalData, HistoricalOHLCV,
    MarketSnapshot, OHLCVBar, Origin, StockFinancialData, StockIdentity,
    TechnicalIndicators, finite_number, json_safe,
)


def read(obj, key, default=None):
    return obj.get(key, default) if isinstance(obj, Mapping) else getattr(obj, key, default)


def string(value):
    if value is None:
        return None
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    value = getattr(value, "value", value)
    return value.strip() or None if isinstance(value, str) else None


def day(value):
    value = string(value)
    if not value:
        return None
    try:
        return date.fromisoformat(value[:10]).isoformat()
    except ValueError:
        return None


def field(key, raw, unit, *, origin=Origin.PROVIDER, source=None,
          trade_date=None, observed_at=None, fetched_at=None, status=None,
          reason=None, minimum=None, maximum=None, positive=False):
    n = finite_number(raw)
    if raw is None:
        status = status or DataStatus.NOT_PROVIDED
    elif n is None or (minimum is not None and n < minimum) or (
        maximum is not None and n > maximum
    ) or (positive and n <= 0):
        status, reason, n = DataStatus.INVALID, reason or "invalid_numeric_value", None
    else:
        status = status or DataStatus.AVAILABLE
    if status in (DataStatus.NOT_COMPUTED, DataStatus.NOT_PROVIDED,
                  DataStatus.UNAVAILABLE, DataStatus.TIMEOUT, DataStatus.RATE_LIMITED,
                  DataStatus.INVALID):
        n = None
    return FinancialField(key, n, unit, DataQualityRecord(
        status, origin, source, trade_date, observed_at, fetched_at, reason))


def require_identity(obj, identity):
    code = read(obj, "code")
    if code is not None and code != identity.code:
        raise ValueError("Upstream symbol does not match requested identity")


def quote_fields(quote, identity):
    require_identity(quote, identity)
    source = string(read(quote, "source"))
    currency = string(read(quote, "currency")) or identity.currency
    if identity.currency and currency != identity.currency:
        raise ValueError("Quote currency does not match identity")
    if identity.market and read(quote, "market") and identity.market != read(quote, "market"):
        raise ValueError("Quote market does not match identity")
    missing = tuple(read(quote, "missing_fields") or ())
    stale = read(quote, "is_stale")
    provider_quality = string(read(quote, "data_quality"))
    metadata = dict(source=source, trade_date=day(read(quote, "quote_date")),
                    observed_at=string(read(quote, "provider_timestamp")),
                    fetched_at=string(read(quote, "fetched_at")))
    specs = {
        "price": ("price", currency, True, None),
        "open": ("open_price", currency, True, None),
        "high": ("high", currency, True, None),
        "low": ("low", currency, True, None),
        "previous_close": ("pre_close", currency, True, None),
        "change_pct": ("change_pct", "%", False, None),
        "change_amount": ("change_amount", currency, False, None),
        "volume": ("volume", "share", False, 0),
        "amount": ("amount", currency, False, 0),
        "volume_ratio": ("volume_ratio", "ratio", False, 0),
        "turnover_rate": ("turnover_rate", "%", False, 0),
        "amplitude": ("amplitude", "%", False, 0),
        "pe_ratio": ("pe_ratio", "ratio", False, None),
        "pb_ratio": ("pb_ratio", "ratio", False, None),
        "total_market_cap": ("total_mv", currency, False, 0),
        "float_market_cap": ("circ_mv", currency, False, 0),
        "change_60d": ("change_60d", "%", False, None),
        "high_52w": ("high_52w", currency, True, None),
        "low_52w": ("low_52w", currency, True, None),
    }
    fields = {}
    for key, (upstream, unit, positive, minimum) in specs.items():
        raw = read(quote, upstream)
        status, reason = None, None
        if upstream in missing or key in missing:
            status, reason = DataStatus.NOT_PROVIDED, "provider_missing_field"
        elif provider_quality == "unavailable":
            status, reason = DataStatus.UNAVAILABLE, "provider_unavailable"
        elif raw is not None:
            if stale is True:
                status, reason = DataStatus.STALE, "provider_marked_stale"
            elif stale is not False or source is None or metadata["trade_date"] is None and metadata["observed_at"] is None:
                status, reason = DataStatus.UNVERIFIED, "missing_freshness_or_provenance"
        fields[key] = field(key, raw, unit, status=status, reason=reason,
                            positive=positive, minimum=minimum, **metadata)
    return MarketSnapshot(fields, missing, string(read(quote, "fallback_from")), provider_quality)


def history_fields(rows, *, identity, source=None, adjustment=None, volume_unit=None,
                   amount_unit=None, fetched_at=None):
    """Accept records or a DataFrame. Never infer historical volume/amount units."""
    if rows is None:
        return HistoricalOHLCV(source=source, adjustment=adjustment)
    if hasattr(rows, "to_dict") and not isinstance(rows, Mapping):
        rows = rows.to_dict("records")
    bars = []
    seen = set()
    invalid = 0
    for row in rows:
        require_identity(row, identity)
        d = day(read(row, "date"))
        if d is None or d in seen:
            raise ValueError("Invalid or duplicate daily bar date")
        seen.add(d)
        values = {}
        for key, unit, positive, minimum in (
            ("open", identity.currency, True, None), ("high", identity.currency, True, None),
            ("low", identity.currency, True, None), ("close", identity.currency, True, None),
            ("volume", volume_unit, False, 0), ("amount", amount_unit, False, 0),
            ("pct_chg", "%", False, None),
        ):
            values[key] = field(key, read(row, key), unit, positive=positive,
                                minimum=minimum, source=source, trade_date=d,
                                fetched_at=fetched_at)
        prices = [values[k].value for k in ("open", "high", "low", "close")]
        if any(x is None for x in prices) or not prices[2] <= min(prices[0], prices[3]) <= max(prices[0], prices[3]) <= prices[1]:
            invalid += 1
            for k in ("open", "high", "low", "close"):
                v = values[k]
                values[k] = FinancialField(k, v.value, v.unit, DataQualityRecord(
                    DataStatus.INVALID, Origin.PROVIDER, source, d,
                    fetched_at=fetched_at, reason="invalid_or_incomplete_ohlc"))
        bars.append(OHLCVBar(d, values))
    bars.sort(key=lambda x: x.trade_date)
    status = DataStatus.NOT_PROVIDED if not bars else (
        DataStatus.PARTIAL if invalid or not source or not adjustment or not volume_unit
        or any(not b.fields["volume"].usable for b in bars) else DataStatus.AVAILABLE)
    reason = "incomplete_history_or_metadata" if status == DataStatus.PARTIAL else None
    return HistoricalOHLCV(tuple(bars), "1d", adjustment, source, status, reason)


def technical_fields(trend, *, identity, sample_count=None, trade_date=None,
                     source=None):
    require_identity(trend, identity)
    if sample_count is not None and (type(sample_count) is not int or sample_count < 0):
        raise ValueError("sample_count must be a nonnegative integer")
    specs = {
        "ma5": (20, identity.currency, True), "ma10": (20, identity.currency, True),
        "ma20": (20, identity.currency, True), "ma60": (60, identity.currency, True),
        "bias_ma5": (20, "%", False), "bias_ma10": (20, "%", False),
        "bias_ma20": (20, "%", False), "volume_ratio_5d": (20, "ratio", False),
        "macd_dif": (26, identity.currency, False), "macd_dea": (26, identity.currency, False),
        "macd_bar": (26, identity.currency, False), "rsi_6": (24, "index", False),
        "rsi_12": (24, "index", False), "rsi_24": (24, "index", False),
        "trend_strength": (20, "score/100", False), "signal_score": (20, "score/100", False),
    }
    fields = {}
    for key, (required, unit, positive) in specs.items():
        status, reason = None, None
        if sample_count is None or sample_count < required:
            status, reason = DataStatus.NOT_COMPUTED, "insufficient_or_unknown_sample_count"
        raw = read(trend, key) if status is None else None
        bounded = key.startswith("rsi_") or key in ("trend_strength", "signal_score")
        fields[key] = field(key, raw, unit, origin=Origin.COMPUTED,
                            source=source, trade_date=trade_date, positive=positive,
                            minimum=0 if bounded or key=="volume_ratio_5d" else None,
                            maximum=100 if bounded else None, status=status, reason=reason)
    return TechnicalIndicators(fields, sample_count)


def chip_fields(chip, identity):
    require_identity(chip, identity)
    fields = {}
    source = string(read(chip, "source"))
    d = day(read(chip, "date"))
    # Upstream object initializes ratios to 0; no individual presence contract.
    # Require explicit mapping or mark default-valued object ratios unverified.
    for key in ("profit_ratio", "avg_cost", "cost_90_low", "cost_90_high",
                "cost_70_low", "cost_70_high", "concentration_90", "concentration_70"):
        ratio = key == "profit_ratio" or key.startswith("concentration")
        raw = read(chip, key)
        status = DataStatus.UNVERIFIED if raw is not None and (
            not source or not d or (ratio and raw == 0 and not isinstance(chip, Mapping))) else None
        fields[key] = field(key, raw, "fraction" if ratio else identity.currency,
                            source=source, trade_date=d, minimum=0,
                            maximum=1 if ratio else None, positive=not ratio,
                            status=status, reason="unconfirmed_chip_field" if status else None)
    return ChipDistribution(fields)


RESEARCH_KEYS = (
    "dashboard", "analysis_summary", "trend_analysis", "short_term_outlook",
    "medium_term_outlook", "technical_analysis", "ma_analysis", "volume_analysis",
    "pattern_analysis", "fundamental_analysis", "sector_position", "company_highlights",
    "news_summary", "market_sentiment", "hot_topics", "key_points", "risk_warning",
    "buy_reason", "search_performed", "news_result_count", "news_result_count_known",
    "news_evidence_present", "market_structure_context",
)


def research_fields(result, identity):
    require_identity(result, identity)
    if result is None:
        return AIResearchResult(), ExecutionMetadata()
    success = read(result, "success")
    score = finite_number(read(result, "sentiment_score"))
    if score is not None and not 0 <= score <= 100:
        score = None
    status = DataStatus.AVAILABLE if success is True else (
        DataStatus.UNAVAILABLE if success is False else DataStatus.UNVERIFIED)
    payload = {k:json_safe(read(result, k)) for k in RESEARCH_KEYS if read(result, k) is not None}
    research = AIResearchResult(status, score if success is True else None,
                                string(read(result, "trend_prediction")) if success is True else None,
                                string(read(result, "operation_advice")) if success is True else None,
                                string(read(result, "confidence_level")) if success is True else None,
                                payload)
    execution = ExecutionMetadata(query_id=string(read(result, "query_id")),
                                  actual_model=string(read(result, "model_used")),
                                  analysis_success=success if type(success) is bool else None)
    return research, execution


def build_stock_data(identity, *, quote=None, history=None, history_source=None,
                     adjustment=None, volume_unit=None, amount_unit=None, trend=None,
                     trend_sample_count=None, trend_trade_date=None, chip=None, result=None):
    """Keep objective inputs separate from AI payload; do not fetch absent fields."""
    snapshot = quote_fields(quote, identity)
    bars = history_fields(history, identity=identity, source=history_source,
                          adjustment=adjustment, volume_unit=volume_unit, amount_unit=amount_unit)
    technical = technical_fields(trend, identity=identity, sample_count=trend_sample_count,
                                 trade_date=trend_trade_date, source="StockTrendAnalyzer")
    research, execution = research_fields(result, identity)
    fundamentals = FundamentalData({k:v for k,v in snapshot.fields.items() if k in (
        "pe_ratio", "pb_ratio", "total_market_cap", "float_market_cap")})
    return StockFinancialData(identity, snapshot, bars, technical,
                              chip_fields(chip, identity), fundamentals, research, execution)
