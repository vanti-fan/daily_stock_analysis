"""Financial data contract v1 for dashboard 0.2.1, independent of AI and rendering.

No network, database, pandas or LLM dependency. Adapters consume already acquired
objects. Archive and runtime integration are deliberately separate phases.
"""
from __future__ import annotations

import json
import math
from dataclasses import asdict, dataclass, field
from datetime import date, datetime
from enum import Enum
from typing import Any, Mapping

SCHEMA_VERSION = "1.0"


class DataStatus(str, Enum):
    AVAILABLE = "AVAILABLE"
    VALIDATED = "VALIDATED"
    PARTIAL = "PARTIAL"
    UNAVAILABLE = "UNAVAILABLE"
    RATE_LIMITED = "RATE_LIMITED"
    TIMEOUT = "TIMEOUT"
    STALE = "STALE"
    CONFLICT = "CONFLICT"
    NOT_ARCHIVED = "NOT_ARCHIVED"
    NOT_PROVIDED = "NOT_PROVIDED"
    NOT_COMPUTED = "NOT_COMPUTED"
    INVALID = "INVALID"
    UNVERIFIED = "UNVERIFIED"


class Origin(str, Enum):
    PROVIDER = "PROVIDER"
    COMPUTED = "COMPUTED"
    AI = "AI"
    UNKNOWN = "UNKNOWN"


def finite_number(value: Any) -> float | int | None:
    """Do not coerce booleans, blanks, infinities, or decorated strings to zero."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    try:
        n = float(value)
        return n if math.isfinite(n) else None
    except (TypeError, ValueError, OverflowError):
        return None


def json_safe(value: Any) -> Any:
    """Strict JSON projection; reject unsupported objects rather than stringify them."""
    if isinstance(value, Enum):
        return json_safe(value.value)
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if isinstance(value, Mapping):
        if any(not isinstance(k, str) for k in value):
            raise TypeError("JSON object keys must be strings")
        return {k: json_safe(v) for k, v in value.items()}
    if isinstance(value, (tuple, list)):
        return [json_safe(v) for v in value]
    # numpy scalars (if a caller supplies a DataFrame) without importing numpy
    if callable(getattr(value, "item", None)):
        converted = value.item()
        if converted is not value:
            return json_safe(converted)
    raise TypeError("Unsupported financial JSON value: " + type(value).__name__)


@dataclass(frozen=True)
class StockIdentity:
    code: str
    name: str | None = None
    market: str | None = None
    currency: str | None = None

    def __post_init__(self):
        if not isinstance(self.code, str) or not self.code.strip() or self.code != self.code.strip():
            raise ValueError("Stock code must be a non-empty string; preserve leading zeros")


@dataclass(frozen=True)
class DataQualityRecord:
    status: DataStatus
    origin: Origin
    source: str | None = None
    trade_date: str | None = None
    observed_at: str | None = None
    fetched_at: str | None = None
    reason: str | None = None


@dataclass(frozen=True)
class FinancialField:
    key: str
    value: float | int | None
    unit: str | None
    quality: DataQualityRecord

    @property
    def usable(self) -> bool:
        return self.value is not None and self.quality.status in (
            DataStatus.AVAILABLE, DataStatus.VALIDATED)


@dataclass(frozen=True)
class MarketSnapshot:
    fields: dict[str, FinancialField] = field(default_factory=dict)
    provider_missing_fields: tuple[str, ...] = ()
    fallback_from: str | None = None
    provider_quality: str | None = None


@dataclass(frozen=True)
class OHLCVBar:
    trade_date: str
    fields: dict[str, FinancialField]


@dataclass(frozen=True)
class HistoricalOHLCV:
    bars: tuple[OHLCVBar, ...] = ()
    timeframe: str = "1d"
    adjustment: str | None = None
    source: str | None = None
    status: DataStatus = DataStatus.NOT_PROVIDED
    reason: str | None = None


@dataclass(frozen=True)
class TechnicalIndicators:
    fields: dict[str, FinancialField] = field(default_factory=dict)
    sample_count: int | None = None
    timeframe: str = "1d"
    # Upstream MACD histogram = 2 * (DIF - DEA), not TradingView's unscaled bar.
    macd_convention: str = "2*(DIF-DEA)"


@dataclass(frozen=True)
class ChipDistribution:
    fields: dict[str, FinancialField] = field(default_factory=dict)


@dataclass(frozen=True)
class FundamentalData:
    fields: dict[str, FinancialField] = field(default_factory=dict)


@dataclass(frozen=True)
class MarketBreadth:
    market: str
    fields: dict[str, FinancialField] = field(default_factory=dict)


@dataclass(frozen=True)
class SectorPerformance:
    name: str
    category: str
    fields: dict[str, FinancialField] = field(default_factory=dict)


@dataclass(frozen=True)
class AIResearchResult:
    status: DataStatus = DataStatus.NOT_PROVIDED
    score: float | int | None = None
    trend: str | None = None
    action: str | None = None
    confidence: str | None = None
    # Preserve structured AI evidence without treating it as market observations.
    payload: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ExecutionMetadata:
    query_id: str | None = None
    configured_model: str | None = None
    actual_model: str | None = None
    analysis_success: bool | None = None
    elapsed_seconds: float | None = None
    fallback_reason: str | None = None


@dataclass(frozen=True)
class StockFinancialData:
    identity: StockIdentity
    snapshot: MarketSnapshot = field(default_factory=MarketSnapshot)
    history: HistoricalOHLCV = field(default_factory=HistoricalOHLCV)
    technical: TechnicalIndicators = field(default_factory=TechnicalIndicators)
    chips: ChipDistribution = field(default_factory=ChipDistribution)
    fundamentals: FundamentalData = field(default_factory=FundamentalData)
    research: AIResearchResult = field(default_factory=AIResearchResult)
    execution: ExecutionMetadata = field(default_factory=ExecutionMetadata)
    schema_version: str = SCHEMA_VERSION

    def to_dict(self) -> dict[str, Any]:
        return json_safe(asdict(self))

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, allow_nan=False, indent=2)


@dataclass(frozen=True)
class MarketFinancialData:
    market: str
    breadth: MarketBreadth
    sectors: tuple[SectorPerformance, ...] = ()
    schema_version: str = SCHEMA_VERSION

    def to_dict(self) -> dict[str, Any]:
        return json_safe(asdict(self))
