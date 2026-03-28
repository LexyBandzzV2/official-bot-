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
BINANCE_API_KEY:    str = os.getenv("BINANCE_API_KEY", "")
BINANCE_SECRET:     str = os.getenv("BINANCE_SECRET", "")

# ── AI ────────────────────────────────────────────────────────────────────────
LM_STUDIO_URL:      str = os.getenv("LM_STUDIO_URL", "http://localhost:1234/v1")
LM_STUDIO_MODEL:    str = os.getenv("LM_STUDIO_MODEL", "llama-3.1-8b-instruct")
OPENROUTER_API_KEY: str = os.getenv("OPENROUTER_API_KEY", "")
OPENROUTER_MODEL:   str = os.getenv("OPENROUTER_MODEL", "moonshotai/kimi-k2")

# ── Broker ────────────────────────────────────────────────────────────────────
FP_MARKETS_LOGIN:    str = os.getenv("FP_MARKETS_LOGIN", "")
FP_MARKETS_PASSWORD: str = os.getenv("FP_MARKETS_PASSWORD", "")
FP_MARKETS_SERVER:   str = os.getenv("FP_MARKETS_SERVER", "FPMarkets-Demo")
KRAKEN_API_KEY:      str = os.getenv("KRAKEN_API_KEY", "")
KRAKEN_SECRET:       str = os.getenv("KRAKEN_SECRET", "")

# ── Notifications ─────────────────────────────────────────────────────────────
PUSHOVER_APP_TOKEN: str = os.getenv("PUSHOVER_APP_TOKEN", "")
PUSHOVER_USER_KEY:  str = os.getenv("PUSHOVER_USER_KEY", "")

# ── Risk Parameters ───────────────────────────────────────────────────────────
ACCOUNT_BALANCE:         float = float(os.getenv("ACCOUNT_BALANCE", "10000"))
MAX_RISK_PER_TRADE:      float = float(os.getenv("MAX_RISK_PER_TRADE", "0.01"))   # 1 %
STOP_LOSS_PCT:           float = float(os.getenv("STOP_LOSS_PCT", "0.02"))        # 2 %
MAX_DAILY_DRAWDOWN:      float = float(os.getenv("MAX_DAILY_DRAWDOWN", "0.10"))   # 10 %
MAX_TRADES_PER_HOUR:     int   = int(os.getenv("MAX_TRADES_PER_HOUR", "15"))      # hard cap
ML_CONFIDENCE_THRESHOLD: float = float(os.getenv("ML_CONFIDENCE_THRESHOLD", "0.60"))
AI_CONFIDENCE_THRESHOLD: float = float(os.getenv("AI_CONFIDENCE_THRESHOLD", "0.60"))

# Trailing take-profit (peak giveback) — exit after retracing this fraction of max favorable move
TRAILING_TP_ENABLED:   bool  = os.getenv("TRAILING_TP_ENABLED", "true").lower() in ("1", "true", "yes")
TRAILING_TP_GIVEBACK:  float = float(os.getenv("TRAILING_TP_GIVEBACK", "0.35"))  # 35 %

# ── Timezone ──────────────────────────────────────────────────────────────────
TIMEZONE: str = os.getenv("TIMEZONE", "America/Toronto")

# ── Paths ─────────────────────────────────────────────────────────────────────
LOG_DIR:    Path = Path(os.getenv("LOG_DIR",    "logs"))
DATA_DIR:   Path = Path(os.getenv("DATA_DIR",   "data"))
MODELS_DIR: Path = Path(os.getenv("MODELS_DIR", "models"))

# Auto-create directories so they always exist at import time
for _d in (LOG_DIR, DATA_DIR, MODELS_DIR, DATA_DIR / "historical"):
    _d.mkdir(parents=True, exist_ok=True)
