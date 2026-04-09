"""
Trade Candidate Logger

Comprehensive logging for each trade candidate with all required details:
- Symbol
- Timeframe
- Candle timestamp
- Indicator values (Alligator jaw/teeth/lips, Vortex vi_plus/vi_minus, Stochastic k/d)
- Signal direction
- Entry price
- Stop loss
- Exit condition
- Whether trade was sent to IBKR
"""

import logging
from datetime import datetime
from typing import Optional

import pandas as pd

logger = logging.getLogger(__name__)


class TradeCandidateLogger:
    """
    Logs comprehensive details for each trade candidate.
    
    Captures all indicator values, signal details, and execution status
    for analysis and debugging.
    """
    
    def __init__(self, log_file: str = "logs/trade_candidates.log"):
        """
        Initialize trade candidate logger.
        
        Args:
            log_file: Path to log file for trade candidates
        """
        self.log_file = log_file
        
        # Create dedicated logger for trade candidates
        self.candidate_logger = logging.getLogger("trade_candidates")
        self.candidate_logger.setLevel(logging.INFO)
        
        # Create file handler
        handler = logging.FileHandler(log_file)
        handler.setLevel(logging.INFO)
        
        # Create formatter
        formatter = logging.Formatter(
            '%(asctime)s | %(message)s',
            datefmt='%Y-%m-%d %H:%M:%S'
        )
        handler.setFormatter(formatter)
        
        # Add handler to logger
        if not self.candidate_logger.handlers:
            self.candidate_logger.addHandler(handler)
        
        logger.info(f"TradeCandidateLogger initialized, logging to {log_file}")
    
    def log_candidate(
        self,
        symbol: str,
        timeframe: str,
        candle_timestamp: datetime,
        signal_direction: str,
        entry_price: float,
        stop_loss: float,
        exit_condition: str,
        trade_sent_to_ibkr: bool,
        strategy_mode: str = "UNKNOWN",
        alligator_jaw: Optional[float] = None,
        alligator_teeth: Optional[float] = None,
        alligator_lips: Optional[float] = None,
        vortex_vi_plus: Optional[float] = None,
        vortex_vi_minus: Optional[float] = None,
        stochastic_k: Optional[float] = None,
        stochastic_d: Optional[float] = None,
        ml_confidence: Optional[float] = None,
        ai_confidence: Optional[float] = None,
        rejection_reason: Optional[str] = None,
    ):
        """
        Log a trade candidate with all details.
        
        Args:
            symbol: Trading symbol
            timeframe: Candle timeframe
            candle_timestamp: Timestamp of the signal candle
            signal_direction: 'BUY' or 'SELL'
            entry_price: Entry price for the trade
            stop_loss: Stop loss price
            exit_condition: Exit condition description
            trade_sent_to_ibkr: Whether trade was sent to IBKR
            alligator_jaw: Alligator jaw value
            alligator_teeth: Alligator teeth value
            alligator_lips: Alligator lips value
            vortex_vi_plus: Vortex VI+ value
            vortex_vi_minus: Vortex VI- value
            stochastic_k: Stochastic %K value
            stochastic_d: Stochastic %D value
            ml_confidence: ML model confidence score
            ai_confidence: AI confidence score
            rejection_reason: Reason if trade was rejected
        """
        # Format indicator values
        alligator_str = f"jaw={alligator_jaw:.4f}, teeth={alligator_teeth:.4f}, lips={alligator_lips:.4f}" if alligator_jaw else "N/A"
        vortex_str = f"vi+={vortex_vi_plus:.4f}, vi-={vortex_vi_minus:.4f}" if vortex_vi_plus else "N/A"
        stochastic_str = f"k={stochastic_k:.2f}, d={stochastic_d:.2f}" if stochastic_k else "N/A"
        
        # Format confidence scores
        ml_str = f"{ml_confidence:.2%}" if ml_confidence is not None else "N/A"
        ai_str = f"{ai_confidence:.2%}" if ai_confidence is not None else "N/A"
        
        # Format execution status
        if trade_sent_to_ibkr:
            status = "SENT_TO_IBKR"
        elif rejection_reason:
            status = "REJECTED"
        else:
            status = "DRY_RUN"

        rejection_field = rejection_reason or "none"
        
        # Build log message
        log_msg = (
            f"CANDIDATE | "
            f"symbol={symbol} | "
            f"timeframe={timeframe} | "
            f"mode={strategy_mode} | "
            f"candle_time={candle_timestamp.strftime('%Y-%m-%d %H:%M:%S')} | "
            f"direction={signal_direction} | "
            f"entry={entry_price:.4f} | "
            f"stop_loss={stop_loss:.4f} | "
            f"exit_condition={exit_condition} | "
            f"alligator=[{alligator_str}] | "
            f"vortex=[{vortex_str}] | "
            f"stochastic=[{stochastic_str}] | "
            f"ml_conf={ml_str} | "
            f"ai_conf={ai_str} | "
            f"status={status} | "
            f"rejection_reason={rejection_field}"
        )
        
        self.candidate_logger.info(log_msg)
    
    def log_from_signal(
        self,
        signal,
        candle_df: pd.DataFrame,
        trade_sent_to_ibkr: bool,
        rejection_reason: Optional[str] = None,
    ):
        """
        Log a trade candidate from a signal object.
        
        Extracts indicator values from the signal and candle DataFrame.
        
        Args:
            signal: BuySignalResult or SellSignalResult object
            candle_df: DataFrame with indicator values
            trade_sent_to_ibkr: Whether trade was sent to IBKR
            rejection_reason: Reason if trade was rejected
        """
        rejection_reason = rejection_reason or getattr(signal, "rejection_reason", None)

        # Get latest candle for timestamp and indicator values
        if candle_df.empty:
            logger.warning("Cannot log candidate: empty candle DataFrame")
            return
        
        latest = candle_df.iloc[-1]
        
        # Extract indicator values from latest candle
        alligator_jaw = latest.get('jaw')
        alligator_teeth = latest.get('teeth')
        alligator_lips = latest.get('lips')
        vortex_vi_plus = latest.get('vi_plus')
        vortex_vi_minus = latest.get('vi_minus')
        stochastic_k = latest.get('stoch_k')
        stochastic_d = latest.get('stoch_d')
        
        # Get candle timestamp
        candle_timestamp = latest.get('time', datetime.now())
        
        # Determine exit condition
        exit_condition = "lips_touch_teeth"  # Default for Alligator-based exits
        
        # Log the candidate
        self.log_candidate(
            symbol=signal.asset,
            timeframe=signal.timeframe,
            candle_timestamp=candle_timestamp,
            signal_direction=signal.signal_type,
            entry_price=signal.entry_price,
            stop_loss=signal.stop_loss,
            exit_condition=exit_condition,
            trade_sent_to_ibkr=trade_sent_to_ibkr,
            strategy_mode=getattr(signal, "strategy_mode", "UNKNOWN"),
            alligator_jaw=alligator_jaw,
            alligator_teeth=alligator_teeth,
            alligator_lips=alligator_lips,
            vortex_vi_plus=vortex_vi_plus,
            vortex_vi_minus=vortex_vi_minus,
            stochastic_k=stochastic_k,
            stochastic_d=stochastic_d,
            ml_confidence=signal.ml_confidence,
            ai_confidence=signal.ai_confidence,
            rejection_reason=rejection_reason,
        )

        # Phase 14: log suitability / skip-reason enrichment when present
        suit_rating  = getattr(signal, "suitability_rating",   None)
        suit_score   = getattr(signal, "suitability_score",    None)
        skip_code    = getattr(signal, "skip_reason_code",     None) or ""
        suit_source  = getattr(signal, "suitability_source_summary", None) or ""
        snap_id      = getattr(signal, "active_profile_snapshot_id", None) or ""
        if suit_rating or skip_code:
            suit_score_str = f"{suit_score:.3f}" if suit_score is not None else "N/A"
            self.candidate_logger.info(
                "SUITABILITY | symbol=%s | rating=%s | score=%s | skip=%s | source=%s | snapshot=%s",
                getattr(signal, "asset", "?"),
                suit_rating or "UNKNOWN",
                suit_score_str,
                skip_code or "(none)",
                suit_source or "(default)",
                snap_id or "(none)",
            )


# Global instance
_trade_candidate_logger: Optional[TradeCandidateLogger] = None


def get_trade_candidate_logger() -> TradeCandidateLogger:
    """Get or create the global trade candidate logger instance."""
    global _trade_candidate_logger
    
    if _trade_candidate_logger is None:
        _trade_candidate_logger = TradeCandidateLogger()
    
    return _trade_candidate_logger
