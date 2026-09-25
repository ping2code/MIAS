"""Technical engine configuration, snapshot and signal models (plain data; every threshold documented)."""
from dataclasses import asdict, dataclass, field

from market_data.models import format_decimal


@dataclass(frozen=True)
class TechnicalConfig:
    ema_periods: tuple = (9, 20, 50, 200)
    rsi_period: int = 14
    atr_period: int = 14
    volume_lookback: int = 20
    pivot_window: int = 2              # N bars left/right; a pivot is confirmed N bars after it.
    equal_tolerance_pct: float = 0.0   # |high - previous high| within this % labels EH (equal high); same for lows.
    cluster_pct: float = 0.35          # Level clustering tolerance: max(pct% x price, cluster_atr x ATR).
    cluster_atr: float = 0.5
    min_touches: int = 2               # Pivots needed for a support/resistance level.
    buffer_pct: float = 0.1            # Breakout buffer: max(pct% x price, buffer_atr x ATR).
    buffer_atr: float = 0.25
    failure_lookback: int = 3          # Bars after a breakout in which a close back through the level is a failure.
    gap_threshold_pct: float = 0.5     # |gap| below this percentage is no_gap.
    rsi_overbought: float = 70.0       # Descriptive zones only (never automatic sell/buy).
    rsi_strong: float = 55.0
    rsi_weak: float = 45.0
    rsi_oversold: float = 30.0
    elevated_relative_volume: float = 1.5
    max_pivot_history: int = 200       # Phase 4B: levels use at most this many most recent confirmed pivots.

    def __post_init__(self):
        if len(self.ema_periods) != 4 or sorted(self.ema_periods) != list(self.ema_periods):
            raise ValueError("ema_periods must be four ascending periods (fast, medium, slow, long)")
        if not self.rsi_oversold < self.rsi_weak <= self.rsi_strong < self.rsi_overbought:
            raise ValueError("RSI zones must satisfy oversold < weak <= strong < overbought")
        for name in ("rsi_period", "atr_period", "volume_lookback", "pivot_window", "min_touches", "failure_lookback",
                     "max_pivot_history"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                raise ValueError(f"{name} must be a positive integer")
        for name in ("equal_tolerance_pct", "cluster_pct", "cluster_atr", "buffer_pct", "buffer_atr", "gap_threshold_pct",
                     "elevated_relative_volume"):
            if getattr(self, name) < 0:
                raise ValueError(f"{name} must not be negative")


@dataclass(frozen=True)
class TechnicalSignal:
    state: str        # bullish_setup, bearish_setup, bullish_momentum, bearish_momentum, breakout_watch,
                      # breakdown_watch, range, mixed, insufficient_data (never BUY/SELL)
    confidence: str   # LOW, MEDIUM or HIGH (agreement of evidence with the state's direction)
    reasons: tuple
    agreeing: int = 0
    conflicting: int = 0

    def to_dict(self):
        return dict(state=self.state, confidence=self.confidence, reasons=list(self.reasons),
                    agreeing=self.agreeing, conflicting=self.conflicting)


@dataclass(frozen=True)
class TechnicalSnapshot:
    symbol: str
    timestamp: object
    interval: str
    index: int
    price: float
    ema: dict            # {"ema9": .., "ema20": .., "ema50": .., "ema200": ..} (None during warm-up)
    vwap: float
    rsi: float
    atr: float
    volume: object       # The bar's exact Decimal volume (may be fractional); indicator math uses float(volume).
    average_volume: float
    relative_volume: float
    trend: str
    last_high_type: str
    last_low_type: str
    significant_high: dict
    significant_low: dict
    support_levels: tuple
    resistance_levels: tuple
    gap_type: str
    gap_percent: float
    gap_absolute: float
    breakout_state: str
    breakout_level: float
    ema_state: dict
    vwap_state: dict
    momentum: str
    signal: TechnicalSignal = None
    evidence: dict = field(default_factory=dict)

    def to_dict(self):
        data = asdict(self)
        data["timestamp"] = self.timestamp.isoformat()
        data["volume"] = format_decimal(self.volume)  # Exact, exponent-free text (JSON has no Decimal type).
        data["signal"] = self.signal.to_dict() if self.signal else None
        data["support_levels"], data["resistance_levels"] = list(self.support_levels), list(self.resistance_levels)
        return data


@dataclass(frozen=True)
class MultiTimeframeSnapshot:
    """Independent per-timeframe snapshots side by side (Phase 4B). There is deliberately no combined state or score."""
    symbol: str
    timestamp: object     # The latest bar timestamp across the available timeframes (None if none).
    timeframes: dict      # interval label -> TechnicalSnapshot, or None when that timeframe is unavailable.
    missing: dict = field(default_factory=dict)  # interval label -> reason, for every None entry.

    def to_dict(self):
        return dict(symbol=self.symbol, timestamp=self.timestamp.isoformat() if self.timestamp else None,
                    timeframes={k: (v.to_dict() if v else None) for k, v in self.timeframes.items()},
                    missing=dict(self.missing))
