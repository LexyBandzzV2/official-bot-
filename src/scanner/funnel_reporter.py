"""Funnel reporter — visualises which symbols survived each prefilter stage.

Output formats:
  • Terminal (rich table printed to stdout)
  • Markdown (string returned)
  • JSON    (dict returned, serialisable)

The reporter is stateless — it takes a list of PrefilterResult objects and
the original symbol count and produces the report.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any, Optional

from src.scanner.prefilters import (
    PrefilterResult,
    SKIP_LOW_VOLATILITY,
    SKIP_WEAK_VOLUME,
    SKIP_MEME_LOW_VOLATILITY,
    SKIP_MEME_WEAK_VOLUME,
    SKIP_MEME_LOW_LIQUIDITY,
    SKIP_BELOW_RANK_CUTOFF,
)
from src.scanner.asset_universe import (
    UniverseGroup,
    registry_snapshot,
    is_group_enabled,
    all_groups,
)

log = logging.getLogger(__name__)


# ── Data assembly ────────────────────────────────────────────────────────────

def build_funnel_data(
    results: list[PrefilterResult],
    total_symbols: int,
    timeframe: str = "",
    mode: str = "",
) -> dict[str, Any]:
    """Build a structured funnel dict from prefilter results.

    Keys:
        timestamp, timeframe, mode, total_symbols,
        universe_coverage, volatility_report, volume_report,
        meme_lane_report, rank_report, survivors
    """
    ts = datetime.now(timezone.utc).isoformat()

    # Universe coverage: how many symbols per group
    group_counts: dict[str, int] = {g.value: 0 for g in all_groups()}
    group_enabled: dict[str, bool] = {g.value: is_group_enabled(g) for g in all_groups()}
    unknown_count = 0
    for r in results:
        if r.universe_group and r.universe_group in group_counts:
            group_counts[r.universe_group] += 1
        else:
            unknown_count += 1

    universe_coverage = {
        "groups": {
            g: {"count": group_counts[g], "enabled": group_enabled[g]}
            for g in group_counts
        },
        "unknown_symbols": unknown_count,
    }

    # Volatility report
    vol_blocked = [r for r in results if r.skip_reason in (SKIP_LOW_VOLATILITY, SKIP_MEME_LOW_VOLATILITY)]
    vol_passed = [r for r in results if r.skip_reason not in (SKIP_LOW_VOLATILITY, SKIP_MEME_LOW_VOLATILITY)]
    volatility_report = {
        "blocked": len(vol_blocked),
        "passed": len(vol_passed),
        "blocked_symbols": [{"symbol": r.symbol, "atr_pct": round(r.atr_pct, 4)} for r in vol_blocked],
    }

    # Volume report
    vol_weak = [r for r in results if r.skip_reason in (SKIP_WEAK_VOLUME, SKIP_MEME_WEAK_VOLUME)]
    volume_report = {
        "blocked": len(vol_weak),
        "blocked_symbols": [
            {"symbol": r.symbol, "volume_ratio": round(r.volume_ratio, 4)}
            for r in vol_weak
        ],
    }

    # Meme lane report
    meme_results = [r for r in results if r.is_meme]
    meme_blocked = [r for r in meme_results if not r.passed]
    meme_lane_report = {
        "total_meme": len(meme_results),
        "blocked": len(meme_blocked),
        "passed": len(meme_results) - len(meme_blocked),
        "blocked_symbols": [
            {"symbol": r.symbol, "skip_reason": r.skip_reason, "atr_pct": round(r.atr_pct, 4)}
            for r in meme_blocked
        ],
    }

    # Rank cutoff report
    rank_cut = [r for r in results if r.skip_reason == SKIP_BELOW_RANK_CUTOFF]
    rank_report = {
        "cut_by_rank": len(rank_cut),
        "cut_symbols": [
            {"symbol": r.symbol, "rank_score": round(r.rank_score, 4)}
            for r in rank_cut
        ],
    }

    # Survivors
    survivors = [r for r in results if r.passed]
    survivor_list = [
        {
            "symbol": r.symbol,
            "atr_pct": round(r.atr_pct, 4),
            "volume_ratio": round(r.volume_ratio, 4),
            "rank_score": round(r.rank_score, 4),
            "universe_group": r.universe_group,
        }
        for r in sorted(survivors, key=lambda x: x.rank_score, reverse=True)
    ]

    return {
        "timestamp": ts,
        "timeframe": timeframe,
        "mode": mode,
        "total_symbols": total_symbols,
        "universe_coverage": universe_coverage,
        "volatility_report": volatility_report,
        "volume_report": volume_report,
        "meme_lane_report": meme_lane_report,
        "rank_report": rank_report,
        "survivors": survivor_list,
        "survivor_count": len(survivor_list),
    }


# ── Terminal output ──────────────────────────────────────────────────────────

def print_funnel_report(data: dict[str, Any]) -> None:
    """Print a concise funnel report to the terminal."""
    header = (
        f"=== Prefilter Funnel [{data.get('timeframe', '?')} / {data.get('mode', '?')}] "
        f"@ {data.get('timestamp', '')} ==="
    )
    print(header)
    print(f"  Total symbols scanned:  {data['total_symbols']}")
    print(f"  Volatility blocked:     {data['volatility_report']['blocked']}")
    print(f"  Volume blocked:         {data['volume_report']['blocked']}")
    ml = data["meme_lane_report"]
    print(f"  Meme lane:              {ml['total_meme']} total, {ml['blocked']} blocked, {ml['passed']} passed")
    print(f"  Rank cutoff:            {data['rank_report']['cut_by_rank']}")
    print(f"  Survivors:              {data['survivor_count']}")
    if data["survivors"]:
        print("  Top survivors:")
        for s in data["survivors"][:5]:
            print(f"    {s['symbol']:20s}  ATR={s['atr_pct']:.2f}%  Vol={s['volume_ratio']:.2f}x  Score={s['rank_score']:.3f}")
    print("=" * len(header))


# ── Markdown output ──────────────────────────────────────────────────────────

def funnel_to_markdown(data: dict[str, Any]) -> str:
    """Return a Markdown-formatted funnel report string."""
    lines: list[str] = []
    lines.append(f"# Prefilter Funnel — {data.get('timeframe', '?')} / {data.get('mode', '?')}")
    lines.append(f"*Generated: {data.get('timestamp', '')}*\n")

    lines.append("## Summary")
    lines.append(f"- **Total symbols**: {data['total_symbols']}")
    lines.append(f"- **Volatility blocked**: {data['volatility_report']['blocked']}")
    lines.append(f"- **Volume blocked**: {data['volume_report']['blocked']}")
    ml = data["meme_lane_report"]
    lines.append(f"- **Meme lane**: {ml['total_meme']} total, {ml['blocked']} blocked")
    lines.append(f"- **Rank cutoff**: {data['rank_report']['cut_by_rank']}")
    lines.append(f"- **Survivors**: {data['survivor_count']}\n")

    # Universe breakdown
    uc = data.get("universe_coverage", {}).get("groups", {})
    if uc:
        lines.append("## Universe Coverage")
        lines.append("| Group | Count | Enabled |")
        lines.append("|-------|------:|:-------:|")
        for g, info in uc.items():
            en = "yes" if info["enabled"] else "no"
            lines.append(f"| {g} | {info['count']} | {en} |")
        lines.append("")

    # Survivors table
    if data["survivors"]:
        lines.append("## Survivors")
        lines.append("| Symbol | ATR% | Vol Ratio | Score | Group |")
        lines.append("|--------|-----:|----------:|------:|-------|")
        for s in data["survivors"]:
            lines.append(
                f"| {s['symbol']} | {s['atr_pct']:.2f} | {s['volume_ratio']:.2f} | "
                f"{s['rank_score']:.3f} | {s.get('universe_group', '?')} |"
            )
        lines.append("")

    return "\n".join(lines)


# ── JSON output ──────────────────────────────────────────────────────────────

def funnel_to_json(data: dict[str, Any]) -> str:
    """Return a JSON string of the funnel report."""
    return json.dumps(data, indent=2, default=str)
