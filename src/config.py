"""AlgoBot — central configuration loader.

Reads all values from .env (via python-dotenv) and exposes them as typed
module-level constants.  Import this anywhere with:

    from src.config import MAX_RISK_PER_TRADE, ACCOUNT_BALANCE, ...
"""

import os
from pathlib import Path
from dotenv import load_dotenv

# ── Load .env from the project root ──────────────────────────────────────────
_root = Path(__file__).parent.parent
load_dotenv(_root / ".env")

# ── Database ──────────────────────────────────────────────────────────────────
SUPABASE_URL:       str = os.getenv("SUPABASE_URL", "")
SUPABASE_ANON_KEY:  str = os.getenv("SUPABASE_ANON_KEY", "")
SQLITE_PATH:        str = os.getenv("SQLITE_PATH", "data/algobot.db")

# ── Market Data APIs ──────────────────────────────────────────────────────────
FINNHUB_API_KEY:    str = os.getenv("FINNHUB_API_KEY", "")
POLYGON_API_KEY:    str = os.getenv("POLYGON_API_KEY", "")  # Polygon.io (api.massive.com)
BINANCE_API_KEY:    str = os.getenv("BINANCE_API_KEY", "")
BINANCE_SECRET:     str = os.getenv("BINANCE_SECRET", "")

# ── AI ────────────────────────────────────────────────────────────────────────
LM_STUDIO_URL:      str = os.getenv("LM_STUDIO_URL", os.getenv("LM_STUDIO_BASE_URL", "http://localhost:1234/v1"))
LM_STUDIO_MODEL:    str = os.getenv("LM_STUDIO_MODEL", "llama-3.1-8b-instruct")
OPENROUTER_API_KEY: str = os.getenv("OPENROUTER_API_KEY", "")
OPENROUTER_MODEL:   str = os.getenv("OPENROUTER_MODEL", "moonshotai/kimi-k2")

# ── Broker ────────────────────────────────────────────────────────────────────
FP_MARKETS_LOGIN:    str = os.getenv("FP_MARKETS_LOGIN", "")
FP_MARKETS_PASSWORD: str = os.getenv("FP_MARKETS_PASSWORD", "")
FP_MARKETS_SERVER:   str = os.getenv("FP_MARKETS_SERVER", "FPMarkets-Demo")
KRAKEN_API_KEY:      str = os.getenv("KRAKEN_API_KEY", "")
KRAKEN_SECRET:       str = os.getenv("KRAKEN_SECRET", "")

IBKR_HOST:           str = os.getenv("IBKR_HOST", "127.0.0.1")
IBKR_PORT:           int = int(os.getenv("IBKR_PORT", "7497"))
IBKR_CLIENT_ID:      int = int(os.getenv("IBKR_CLIENT_ID", "1"))

# Multi-broker routing
TRADING_MODE: str = os.getenv("TRADING_MODE", "paper").lower()  # paper|live
BROKER_PREFERENCE: str = os.getenv("BROKER_PREFERENCE", "").strip().lower()

ALPACA_API_KEY: str = os.getenv("ALPACA_API_KEY", "")
ALPACA_SECRET_KEY: str = os.getenv("ALPACA_SECRET_KEY", "")
ALPACA_BASE_URL: str = os.getenv("ALPACA_BASE_URL", "https://paper-api.alpaca.markets")

FXCM_API_KEY: str = os.getenv("FXCM_API_KEY", "")
FXCM_ACCESS_TOKEN: str = os.getenv("FXCM_ACCESS_TOKEN", "")
FXCM_ACCOUNT_TYPE: str = os.getenv("FXCM_ACCOUNT_TYPE", "demo")  # demo|real

# Per-broker balances (used for sizing / reporting; execution adapters may ignore)
ALPACA_ACCOUNT_BALANCE: float = float(os.getenv("ALPACA_ACCOUNT_BALANCE", "50.0"))
KRAKEN_ACCOUNT_BALANCE: float = float(os.getenv("KRAKEN_ACCOUNT_BALANCE", "50.0"))
FXCM_ACCOUNT_BALANCE: float = float(os.getenv("FXCM_ACCOUNT_BALANCE", "50.0"))
IBKR_ACCOUNT_BALANCE: float = float(os.getenv("IBKR_ACCOUNT_BALANCE", "50.0"))

# Per-broker hourly trade caps (router-level; global cap still enforced by RiskManager)
ALPACA_MAX_TRADES_PER_HOUR: int = int(os.getenv("ALPACA_MAX_TRADES_PER_HOUR", "15"))
KRAKEN_MAX_TRADES_PER_HOUR: int = int(os.getenv("KRAKEN_MAX_TRADES_PER_HOUR", "15"))
FXCM_MAX_TRADES_PER_HOUR: int = int(os.getenv("FXCM_MAX_TRADES_PER_HOUR", "15"))
IBKR_MAX_TRADES_PER_HOUR: int = int(os.getenv("IBKR_MAX_TRADES_PER_HOUR", "15"))

# Enabled flags (placeholders until keys are configured)
ALPACA_ENABLED: bool = os.getenv("ALPACA_ENABLED", "true").lower() in ("1", "true", "yes")
KRAKEN_ENABLED: bool = os.getenv("KRAKEN_ENABLED", "true").lower() in ("1", "true", "yes")
FXCM_ENABLED: bool = os.getenv("FXCM_ENABLED", "true").lower() in ("1", "true", "yes")
IBKR_ENABLED: bool = os.getenv("IBKR_ENABLED", "true").lower() in ("1", "true", "yes")

# ── Notifications ─────────────────────────────────────────────────────────────
PUSHOVER_APP_TOKEN: str = os.getenv("PUSHOVER_APP_TOKEN", "")
PUSHOVER_USER_KEY:  str = os.getenv("PUSHOVER_USER_KEY", "")

# ── Risk Parameters ───────────────────────────────────────────────────────────
ACCOUNT_BALANCE:         float = float(os.getenv("ACCOUNT_BALANCE", "10000"))
MAX_RISK_PER_TRADE:      float = float(os.getenv("MAX_RISK_PER_TRADE", "0.01"))   # 1 %
STOP_LOSS_PCT:           float = float(os.getenv("STOP_LOSS_PCT", "0.02"))        # 2 %
MAX_DAILY_DRAWDOWN:      float = float(os.getenv("MAX_DAILY_DRAWDOWN", "0.10"))   # 10 %
MAX_TRADES_PER_HOUR:     int   = int(os.getenv("MAX_TRADES_PER_HOUR", "15"))      # hard cap
ML_CONFIDENCE_THRESHOLD: float = float(os.getenv("ML_CONFIDENCE_THRESHOLD", "0.0"))
AI_CONFIDENCE_THRESHOLD: float = float(os.getenv("AI_CONFIDENCE_THRESHOLD", "0.0"))

# Peak-giveback exit — bar-close retracement guard (formerly "trailing take-profit").
# The canonical env vars are PEAK_GIVEBACK_ENABLED / PEAK_GIVEBACK_FRACTION.
# The legacy names TRAILING_TP_ENABLED / TRAILING_TP_GIVEBACK are still read
# as a fallback so existing .env files continue to work without modification.
# Precedence: canonical env var > legacy env var > hard-coded default.
PEAK_GIVEBACK_ENABLED: bool  = (
    os.getenv("PEAK_GIVEBACK_ENABLED",
              os.getenv("TRAILING_TP_ENABLED", "true")).lower()
    in ("1", "true", "yes")
)
PEAK_GIVEBACK_FRACTION: float = float(
    os.getenv("PEAK_GIVEBACK_FRACTION",
              os.getenv("TRAILING_TP_GIVEBACK", "0.35"))
)  # fraction of max favorable move that must retrace before exit fires (0.35 = 35 %)
PEAK_GIVEBACK_MIN_MFE_PCT: float = float(
    os.getenv("PEAK_GIVEBACK_MIN_MFE_PCT", "0.001")
)  # min favorable excursion (0.1%) before giveback can fire — prevents exit on noise

# Deprecated aliases kept so any code that still reads the old names doesn't break.
# Remove in a future release after all call-sites are updated to canonical names.
TRAILING_TP_ENABLED:  bool  = PEAK_GIVEBACK_ENABLED
TRAILING_TP_GIVEBACK: float = PEAK_GIVEBACK_FRACTION

# ── Phase 4: mode-specific exit intelligence ──────────────────────────────────
# ATR trail (SCALP only, eligible after stage-2 lock)
SCALP_ATR_MULTIPLIER: float = float(os.getenv("SCALP_ATR_MULTIPLIER", "1.5"))

# Candle momentum-fade tightening (SCALP + INTERMEDIATE)
# Number of consecutive bars to scan for shrinking-body sequence
SCALP_MOMENTUM_FADE_WINDOW: int   = int(os.getenv("SCALP_MOMENTUM_FADE_WINDOW", "3"))
# Giveback fraction applied to tighter candle-trail candidate when fade fires
# (smaller than policy.giveback_frac = 0.25 to capture more profit)
SCALP_MOMENTUM_FADE_TIGHTEN_FRAC: float = float(
    os.getenv("SCALP_MOMENTUM_FADE_TIGHTEN_FRAC", "0.30")
)

# ── Timezone ──────────────────────────────────────────────────────────────────
TIMEZONE: str = os.getenv("TIMEZONE", "America/Toronto")

# ── Paths ─────────────────────────────────────────────────────────────────────
LOG_DIR:    Path = Path(os.getenv("LOG_DIR",    "logs"))
DATA_DIR:   Path = Path(os.getenv("DATA_DIR",   "data"))
MODELS_DIR: Path = Path(os.getenv("MODELS_DIR", "models"))

# Auto-create directories so they always exist at import time
for _d in (LOG_DIR, DATA_DIR, MODELS_DIR, DATA_DIR / "historical"):
    _d.mkdir(parents=True, exist_ok=True)

# ── Phase 11: Regime Engine ───────────────────────────────────────────────────

# Minimum confidence required before a regime label is trusted for soft modifiers.
# Below this threshold the engine is fail-open (no modifiers applied).
REGIME_MIN_CONFIDENCE: float = float(os.getenv("REGIME_MIN_CONFIDENCE", "0.40"))

# Minimum change in confidence before an unchanged label is re-persisted.
# Set higher to reduce DB writes on stable markets.
REGIME_CHANGE_CONFIDENCE_DELTA: float = float(
    os.getenv("REGIME_CHANGE_CONFIDENCE_DELTA", "0.15")
)

# Rolling candle window used as the feature-extraction source.
REGIME_SOURCE_WINDOW: int = int(os.getenv("REGIME_SOURCE_WINDOW", "50"))

# ── Regime soft modifiers (additive deltas for thresholds; factors for size) ─
# These are defaults; individual regime labels may override them via env vars.

# CHOPPY_LOW_VOL: raise ML/AI bars and reduce position size
REGIME_CHOPPY_LOW_VOL_ML_DELTA:   float = float(os.getenv("REGIME_CHOPPY_LOW_VOL_ML_DELTA",  "0.05"))
REGIME_CHOPPY_LOW_VOL_AI_DELTA:   float = float(os.getenv("REGIME_CHOPPY_LOW_VOL_AI_DELTA",  "0.05"))
REGIME_CHOPPY_LOW_VOL_SIZE_FACTOR:float = float(os.getenv("REGIME_CHOPPY_LOW_VOL_SIZE_FACTOR","0.75"))

# CHOPPY_HIGH_VOL: raise bars more and reduce size more aggressively
REGIME_CHOPPY_HIGH_VOL_ML_DELTA:   float = float(os.getenv("REGIME_CHOPPY_HIGH_VOL_ML_DELTA",  "0.07"))
REGIME_CHOPPY_HIGH_VOL_AI_DELTA:   float = float(os.getenv("REGIME_CHOPPY_HIGH_VOL_AI_DELTA",  "0.07"))
REGIME_CHOPPY_HIGH_VOL_SIZE_FACTOR:float = float(os.getenv("REGIME_CHOPPY_HIGH_VOL_SIZE_FACTOR","0.65"))

# NEWS_DRIVEN_UNSTABLE: most conservative
REGIME_NEWS_UNSTABLE_ML_DELTA:    float = float(os.getenv("REGIME_NEWS_UNSTABLE_ML_DELTA",   "0.10"))
REGIME_NEWS_UNSTABLE_AI_DELTA:    float = float(os.getenv("REGIME_NEWS_UNSTABLE_AI_DELTA",   "0.10"))
REGIME_NEWS_UNSTABLE_SIZE_FACTOR: float = float(os.getenv("REGIME_NEWS_UNSTABLE_SIZE_FACTOR","0.50"))

# REVERSAL_TRANSITION: moderate caution
REGIME_REVERSAL_ML_DELTA:    float = float(os.getenv("REGIME_REVERSAL_ML_DELTA",   "0.05"))
REGIME_REVERSAL_AI_DELTA:    float = float(os.getenv("REGIME_REVERSAL_AI_DELTA",   "0.05"))
REGIME_REVERSAL_SIZE_FACTOR: float = float(os.getenv("REGIME_REVERSAL_SIZE_FACTOR","0.80"))

# TRENDING_HIGH_VOL: slight positive biasing (lower needed bar; slightly bigger ok)
REGIME_TRENDING_HIGH_VOL_ML_DELTA:   float = float(os.getenv("REGIME_TRENDING_HIGH_VOL_ML_DELTA",   "-0.03"))
REGIME_TRENDING_HIGH_VOL_AI_DELTA:   float = float(os.getenv("REGIME_TRENDING_HIGH_VOL_AI_DELTA",   "-0.03"))
REGIME_TRENDING_HIGH_VOL_SIZE_FACTOR:float = float(os.getenv("REGIME_TRENDING_HIGH_VOL_SIZE_FACTOR","1.10"))

# TRENDING_LOW_VOL: neutral (no modifier)
REGIME_TRENDING_LOW_VOL_ML_DELTA:   float = float(os.getenv("REGIME_TRENDING_LOW_VOL_ML_DELTA",   "0.0"))
REGIME_TRENDING_LOW_VOL_AI_DELTA:   float = float(os.getenv("REGIME_TRENDING_LOW_VOL_AI_DELTA",   "0.0"))
REGIME_TRENDING_LOW_VOL_SIZE_FACTOR:float = float(os.getenv("REGIME_TRENDING_LOW_VOL_SIZE_FACTOR","1.0"))

# Hard clamp bounds for dynamic ML/AI thresholds (safety rails)
REGIME_ML_THRESHOLD_MIN:  float = float(os.getenv("REGIME_ML_THRESHOLD_MIN",  "0.45"))
REGIME_ML_THRESHOLD_MAX:  float = float(os.getenv("REGIME_ML_THRESHOLD_MAX",  "0.90"))
REGIME_AI_THRESHOLD_MIN:  float = float(os.getenv("REGIME_AI_THRESHOLD_MIN",  "0.40"))
REGIME_AI_THRESHOLD_MAX:  float = float(os.getenv("REGIME_AI_THRESHOLD_MAX",  "0.90"))
REGIME_SIZE_FACTOR_MIN:   float = float(os.getenv("REGIME_SIZE_FACTOR_MIN",   "0.40"))
REGIME_SIZE_FACTOR_MAX:   float = float(os.getenv("REGIME_SIZE_FACTOR_MAX",   "1.25"))

# ── Phase 12: Regime-Aware Strategy Adaptation ───────────────────────────────

# Score bias per macro regime (additive, added after compute_score)
REGIME_SCORE_BIAS_TRENDING:   float = float(os.getenv("REGIME_SCORE_BIAS_TRENDING",   "3.0"))
REGIME_SCORE_BIAS_RANGING:    float = float(os.getenv("REGIME_SCORE_BIAS_RANGING",    "-5.0"))
REGIME_SCORE_BIAS_HIGH_VOL:   float = float(os.getenv("REGIME_SCORE_BIAS_HIGH_VOL",   "-3.0"))
REGIME_SCORE_BIAS_LOW_VOL:    float = float(os.getenv("REGIME_SCORE_BIAS_LOW_VOL",    "-2.0"))
REGIME_SCORE_BIAS_UNCERTAIN:  float = float(os.getenv("REGIME_SCORE_BIAS_UNCERTAIN",  "0.0"))

# Entry filter: enable/disable + minimum score per macro regime
REGIME_ENTRY_FILTER_ENABLED:  bool  = os.getenv("REGIME_ENTRY_FILTER_ENABLED", "false").lower() in ("1", "true", "yes")
REGIME_ENTRY_MIN_SCORE_TRENDING:  float = float(os.getenv("REGIME_ENTRY_MIN_SCORE_TRENDING",  "30.0"))
REGIME_ENTRY_MIN_SCORE_RANGING:   float = float(os.getenv("REGIME_ENTRY_MIN_SCORE_RANGING",   "50.0"))
REGIME_ENTRY_MIN_SCORE_HIGH_VOL:  float = float(os.getenv("REGIME_ENTRY_MIN_SCORE_HIGH_VOL",  "45.0"))
REGIME_ENTRY_MIN_SCORE_LOW_VOL:   float = float(os.getenv("REGIME_ENTRY_MIN_SCORE_LOW_VOL",   "40.0"))
REGIME_ENTRY_MIN_SCORE_UNCERTAIN: float = float(os.getenv("REGIME_ENTRY_MIN_SCORE_UNCERTAIN", "35.0"))

# Exit parameter multipliers per macro regime (applied to policy parameters)
# giveback_frac multiplier (>1 = wider giveback = more room; <1 = tighter)
REGIME_EXIT_GIVEBACK_MULT_TRENDING:   float = float(os.getenv("REGIME_EXIT_GIVEBACK_MULT_TRENDING",   "1.20"))
REGIME_EXIT_GIVEBACK_MULT_RANGING:    float = float(os.getenv("REGIME_EXIT_GIVEBACK_MULT_RANGING",    "0.80"))
REGIME_EXIT_GIVEBACK_MULT_HIGH_VOL:   float = float(os.getenv("REGIME_EXIT_GIVEBACK_MULT_HIGH_VOL",   "0.85"))
REGIME_EXIT_GIVEBACK_MULT_LOW_VOL:    float = float(os.getenv("REGIME_EXIT_GIVEBACK_MULT_LOW_VOL",    "1.10"))
# break_even_pct multiplier (<1 = arm break-even earlier; >1 = delay)
REGIME_EXIT_BE_MULT_TRENDING:   float = float(os.getenv("REGIME_EXIT_BE_MULT_TRENDING",   "1.10"))
REGIME_EXIT_BE_MULT_RANGING:    float = float(os.getenv("REGIME_EXIT_BE_MULT_RANGING",    "0.80"))
REGIME_EXIT_BE_MULT_HIGH_VOL:   float = float(os.getenv("REGIME_EXIT_BE_MULT_HIGH_VOL",   "0.75"))
REGIME_EXIT_BE_MULT_LOW_VOL:    float = float(os.getenv("REGIME_EXIT_BE_MULT_LOW_VOL",    "1.15"))
# fade_tighten_frac multiplier (<1 = less aggressive fade; >1 = more aggressive)
REGIME_EXIT_FADE_MULT_TRENDING:   float = float(os.getenv("REGIME_EXIT_FADE_MULT_TRENDING",   "0.80"))
REGIME_EXIT_FADE_MULT_RANGING:    float = float(os.getenv("REGIME_EXIT_FADE_MULT_RANGING",    "1.25"))
REGIME_EXIT_FADE_MULT_HIGH_VOL:   float = float(os.getenv("REGIME_EXIT_FADE_MULT_HIGH_VOL",   "1.15"))
REGIME_EXIT_FADE_MULT_LOW_VOL:    float = float(os.getenv("REGIME_EXIT_FADE_MULT_LOW_VOL",    "0.90"))

# ── Phase 14: Live Suitability Activation ─────────────────────────────────────

# Master on/off switch — when False the resolver is bypassed and every signal is
# treated as ACTIVE / no friction (safe-fail open for backwards compatibility).
SUITABILITY_GATING_ENABLED: bool = (
    os.getenv("SUITABILITY_GATING_ENABLED", "false").lower() in ("1", "true", "yes")
)

# Threshold-raise: add these many score-points to the entry-filter minimum for
# MEDIUM / LOW suitability respectively.  Set to 0.0 to disable per-rating.
SUITABILITY_THRESHOLD_RAISE_ENABLED: bool = (
    os.getenv("SUITABILITY_THRESHOLD_RAISE_ENABLED", "true").lower() in ("1", "true", "yes")
)
SUITABILITY_MEDIUM_THRESHOLD_DELTA: float = float(
    os.getenv("SUITABILITY_MEDIUM_THRESHOLD_DELTA", "5.0")
)
SUITABILITY_LOW_THRESHOLD_DELTA: float = float(
    os.getenv("SUITABILITY_LOW_THRESHOLD_DELTA", "10.0")
)

# Score penalty: subtract these many score-points from score_total for
# MEDIUM / LOW suitability respectively.
SUITABILITY_SCORE_PENALTY_ENABLED: bool = (
    os.getenv("SUITABILITY_SCORE_PENALTY_ENABLED", "true").lower() in ("1", "true", "yes")
)
SUITABILITY_MEDIUM_SCORE_PENALTY: float = float(
    os.getenv("SUITABILITY_MEDIUM_SCORE_PENALTY", "4.0")
)
SUITABILITY_LOW_SCORE_PENALTY: float = float(
    os.getenv("SUITABILITY_LOW_SCORE_PENALTY", "8.0")
)

# ── Asset Universe ────────────────────────────────────────────────────────────
# Universe group enable/disable flags (all on by default)
UNIVERSE_CORE_CRYPTO_ENABLED: bool = (
    os.getenv("UNIVERSE_CORE_CRYPTO_ENABLED", "true").lower() in ("1", "true", "yes")
)
UNIVERSE_CORE_MOMENTUM_STOCKS_ENABLED: bool = (
    os.getenv("UNIVERSE_CORE_MOMENTUM_STOCKS_ENABLED", "true").lower() in ("1", "true", "yes")
)
UNIVERSE_CORE_INDEX_MOMENTUM_ENABLED: bool = (
    os.getenv("UNIVERSE_CORE_INDEX_MOMENTUM_ENABLED", "true").lower() in ("1", "true", "yes")
)
UNIVERSE_HIGH_BETA_ETFS_ENABLED: bool = (
    os.getenv("UNIVERSE_HIGH_BETA_ETFS_ENABLED", "true").lower() in ("1", "true", "yes")
)
UNIVERSE_MEME_COIN_LANE_ENABLED: bool = (
    os.getenv("UNIVERSE_MEME_COIN_LANE_ENABLED", "true").lower() in ("1", "true", "yes")
)
