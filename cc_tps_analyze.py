#!/usr/bin/env python3
# Copyright 2025 Claude Code TPS Monitor Contributors
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""
Claude Code TPS Analyzer — post-hoc analysis of TPS monitor logs.

Analyze JSONL log files produced by cc_tps_monitor.py and produce
summary reports, per-model breakdowns, time-series, distributions,
and cost estimates.

Usage:
    python cc_tps_analyze.py                         # analyze cc_tps.log
    python cc_tps_analyze.py path/to/logs/*.jsonl    # glob multiple files
    python cc_tps_analyze.py --json                  # JSON output
    python cc_tps_analyze.py --csv                   # CSV export
    python cc_tps_analyze.py --cost                  # include cost estimate
    python cc_tps_analyze.py --model sonnet          # filter by model name
"""

from __future__ import annotations

import csv
import json
import os
import sys
import glob
import statistics
import time as time_module
from argparse import ArgumentParser, Namespace
from collections import defaultdict
from datetime import datetime, timedelta
from typing import Any, TextIO

# ---------------------------------------------------------------------------
# Pricing data  (per-million-token rates, in USD)
# Update these as Anthropic publishes new pricing.
# Sources: https://docs.anthropic.com/en/docs/about-claude/models
# ---------------------------------------------------------------------------
MODEL_PRICES: dict[str, tuple[float, float]] = {
    # model_substring -> (input_per_mtok, output_per_mtok)
    "claude-sonnet-4-20250514": (3.0, 15.0),
    "claude-sonnet-4": (3.0, 15.0),
    "claude-3-opus": (15.0, 75.0),
    "claude-3-sonnet": (3.0, 15.0),
    "claude-3-haiku": (0.25, 1.25),
    "claude-3-5-sonnet": (3.0, 15.0),
    "claude-3-5-haiku": (0.80, 4.0),
}

SESSION_GAP_SECS = 300  # 5 minutes without activity = new session


# ═══════════════════════════════════════════════════════════════════════════
# Data loading
# ═══════════════════════════════════════════════════════════════════════════

def load_records(paths: list[str]) -> list[dict[str, Any]]:
    """Load JSONL records from one or more file paths (supports globs)."""
    seen = set()
    records: list[dict[str, Any]] = []
    for pattern in paths:
        for fp in sorted(glob.glob(pattern)):
            abspath = os.path.abspath(fp)
            if abspath in seen:
                continue
            seen.add(abspath)
            with open(fp) as f:
                for line in f:
                    line = line.strip()
                    if line:
                        records.append(json.loads(line))
    return records


def filter_records(
    records: list[dict[str, Any]],
    model_substr: str | None = None,
    after: str | None = None,
    before: str | None = None,
) -> list[dict[str, Any]]:
    """Apply optional filters to the record list."""
    result = records

    if model_substr:
        result = [r for r in result if model_substr.lower() in r.get("model", "").lower()]

    if after or before:
        parsed = []
        for r in result:
            ts = _parse_timestamp(r)
            if ts is None:
                continue
            if after and ts < _parse_timestamp({"iso_timestamp": after, "timestamp": after}):
                continue
            if before and ts >= _parse_timestamp({"iso_timestamp": before, "timestamp": before}):
                continue
            parsed.append(r)
        result = parsed

    return result


def _parse_timestamp(record: dict[str, Any]) -> datetime | None:
    """Parse the best available timestamp from a record."""
    iso = record.get("iso_timestamp")
    if iso:
        try:
            return datetime.fromisoformat(iso)
        except (ValueError, TypeError):
            pass
    ts = record.get("timestamp")
    if ts:
        try:
            return datetime.strptime(ts, "%H:%M:%S")
        except (ValueError, TypeError):
            pass
    return None


# ═══════════════════════════════════════════════════════════════════════════
# Session detection
# ═══════════════════════════════════════════════════════════════════════════

def detect_sessions(records: list[dict[str, Any]]) -> list[list[dict[str, Any]]]:
    """Split records into sessions based on time gaps."""
    if not records:
        return []

    sessions: list[list[dict[str, Any]]] = [[records[0]]]
    for i in range(1, len(records)):
        prev_ts = _parse_timestamp(records[i - 1])
        curr_ts = _parse_timestamp(records[i])
        gap = (
            (curr_ts - prev_ts).total_seconds()
            if prev_ts and curr_ts
            else 0
        )
        if gap > SESSION_GAP_SECS:
            sessions.append([])
        sessions[-1].append(records[i])
    return sessions


# ═══════════════════════════════════════════════════════════════════════════
# Statistics
# ═══════════════════════════════════════════════════════════════════════════

def _percentile(sorted_vals: list[float], pct: float) -> float:
    """Nearest-rank percentile."""
    if not sorted_vals:
        return 0.0
    k = max(1, round(len(sorted_vals) * pct / 100))
    return sorted_vals[k - 1]


def compute_latency_distribution(latencies_ms: list[float]) -> dict[str, float]:
    """Compute latency distribution in seconds."""
    secs = sorted(latencies_ms)
    return {
        "p50": _percentile(secs, 50),
        "p75": _percentile(secs, 75),
        "p90": _percentile(secs, 90),
        "p95": _percentile(secs, 95),
        "p99": _percentile(secs, 99),
        "max": secs[-1] if secs else 0,
    }


def compute_tps_distribution(tps_vals: list[float]) -> dict[str, float]:
    """Compute TPS distribution."""
    sorted_tps = sorted(tps_vals)
    return {
        "p50": _percentile(sorted_tps, 50),
        "p75": _percentile(sorted_tps, 75),
        "p90": _percentile(sorted_tps, 90),
        "p95": _percentile(sorted_tps, 95),
        "p99": _percentile(sorted_tps, 99),
        "max": sorted_tps[-1] if sorted_tps else 0,
    }


def compute_summary(records: list[dict[str, Any]]) -> dict[str, Any]:
    """Compute top-level summary statistics."""
    if not records:
        return {}

    total_in = sum(r.get("input_tokens", 0) for r in records)
    total_out = sum(r.get("output_tokens", 0) for r in records)
    durations_ms = [r.get("duration_ms", 0) for r in records]
    total_wall = sum(durations_ms)
    tps_vals = [r.get("tps", 0) for r in records if r.get("tps", 0) > 0]

    session_count = len(detect_sessions(records))

    # Time span
    timestamps = [_parse_timestamp(r) for r in records if _parse_timestamp(r)]
    span_start = min(timestamps) if timestamps else None
    span_end = max(timestamps) if timestamps else None

    # Latency dist
    latencies_ms_sorted = sorted(durations_ms)
    lat_dist = compute_latency_distribution(durations_ms)
    tps_dist = compute_tps_distribution(tps_vals)

    return {
        "request_count": len(records),
        "session_count": session_count,
        "total_input_tokens": total_in,
        "total_output_tokens": total_out,
        "total_wall_time_ms": total_wall,
        "average_tps": total_out / (total_wall / 1000) if total_wall > 0 else 0,
        "average_latency_ms": statistics.mean(durations_ms) if durations_ms else 0,
        "span_start": span_start.isoformat() if span_start else None,
        "span_end": span_end.isoformat() if span_end else None,
        "latency_distribution_ms": lat_dist,
        "tps_distribution": tps_dist,
    }


def compute_per_model(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Group records by model and compute per-model stats."""
    groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for r in records:
        groups.setdefault(r.get("model", "unknown"), []).append(r)

    result = []
    for model, reqs in sorted(groups.items()):
        in_tok = sum(r.get("input_tokens", 0) for r in reqs)
        out_tok = sum(r.get("output_tokens", 0) for r in reqs)
        wall = sum(r.get("duration_ms", 0) for r in reqs)
        tps_list = [r.get("tps", 0) for r in reqs if r.get("tps", 0) > 0]
        lat_list = [r.get("duration_ms", 0) for r in reqs]
        result.append({
            "model": model,
            "request_count": len(reqs),
            "total_input_tokens": in_tok,
            "total_output_tokens": out_tok,
            "average_tps": out_tok / (wall / 1000) if wall > 0 else 0,
            "average_latency_ms": statistics.mean(lat_list) if lat_list else 0,
        })
    return result


def compute_per_session(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Group records by their session_id field."""
    groups: dict[int, list[dict[str, Any]]] = defaultdict(list)
    for r in records:
        sid = r.get("session_id", 0) or 0
        groups[sid].append(r)

    result = []
    for sid in sorted(groups):
        reqs = groups[sid]
        in_tok = sum(r.get("input_tokens", 0) for r in reqs)
        out_tok = sum(r.get("output_tokens", 0) for r in reqs)
        wall = sum(r.get("duration_ms", 0) for r in reqs)
        tps_list = [r.get("tps", 0) for r in reqs if r.get("tps", 0) > 0]
        result.append({
            "session_id": sid,
            "request_count": len(reqs),
            "total_input_tokens": in_tok,
            "total_output_tokens": out_tok,
            "average_tps": out_tok / (wall / 1000) if wall > 0 else 0,
            "average_latency_ms": statistics.mean([r.get("duration_ms", 0) for r in reqs]) if reqs else 0,
        })
    return result


def compute_timeseries(
    records: list[dict[str, Any]],
    window_secs: int = 60,
) -> list[dict[str, Any]]:
    """Bin records into fixed-size time windows."""
    buckets: dict[int, list[dict[str, Any]]] = defaultdict(list)
    for r in records:
        ts = _parse_timestamp(r)
        if ts is None:
            continue
        epoch = ts.timestamp()
        bucket = int(epoch // window_secs)
        buckets[bucket].append(r)

    if not buckets:
        return []

    result = []
    for bucket_id in sorted(buckets):
        reqs = buckets[bucket_id]
        out_tok = sum(r.get("output_tokens", 0) for r in reqs)
        wall = sum(r.get("duration_ms", 0) for r in reqs)
        tps_list = [r.get("tps", 0) for r in reqs if r.get("tps", 0) > 0]
        bucket_start = datetime.fromtimestamp(bucket_id * window_secs)
        bucket_end = bucket_start + timedelta(seconds=window_secs)
        result.append({
            "window_start": bucket_start.isoformat(),
            "window_end": bucket_end.isoformat(),
            "request_count": len(reqs),
            "total_output_tokens": out_tok,
            "average_tps": out_tok / (wall / 1000) if wall > 0 else 0,
            "average_latency_ms": statistics.mean([r.get("duration_ms", 0) for r in reqs]) if reqs else 0,
        })
    return result


def estimate_cost(records: list[dict[str, Any]]) -> dict[str, Any]:
    """Estimate API cost using built-in pricing table."""
    by_model: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for r in records:
        by_model.setdefault(r.get("model", "unknown"), []).append(r)

    model_breakdown: list[dict[str, Any]] = []
    total_cost = 0.0

    for model, reqs in sorted(by_model.items()):
        in_tok = sum(r.get("input_tokens", 0) for r in reqs)
        out_tok = sum(r.get("output_tokens", 0) for r in reqs)

        # Match pricing by substring
        price = None
        for key, (in_price, out_price) in MODEL_PRICES.items():
            if key in model:
                price = (in_price, out_price)
                break

        if price:
            cost = (in_tok / 1_000_000 * price[0]) + (out_tok / 1_000_000 * price[1])
            total_cost += cost
            model_breakdown.append({
                "model": model,
                "input_tokens": in_tok,
                "output_tokens": out_tok,
                "input_rate": price[0],
                "output_rate": price[1],
                "cost": round(cost, 4),
            })
        else:
            model_breakdown.append({
                "model": model,
                "input_tokens": in_tok,
                "output_tokens": out_tok,
                "input_rate": None,
                "output_rate": None,
                "cost": None,
            })

    return {
        "total_cost_usd": round(total_cost, 4),
        "models": model_breakdown,
    }


# ═══════════════════════════════════════════════════════════════════════════
# Formatting & output
# ═══════════════════════════════════════════════════════════════════════════

def fmt_duration(ms: float) -> str:
    """Human-readable duration."""
    if ms >= 60_000:
        return f"{ms / 60_000:.1f}m"
    if ms >= 1000:
        return f"{ms / 1000:.2f}s"
    return f"{ms:.0f}ms"


def print_table(
    headers: list[str],
    rows: list[list[str]],
    file: TextIO = sys.stdout,
) -> None:
    """Print an aligned table with Unicode box-drawing separators."""
    if not rows:
        return

    # Compute column widths
    widths = [len(h) for h in headers]
    for row in rows:
        for i, cell in enumerate(row):
            widths[i] = max(widths[i], len(cell))

    # Build format strings
    sep = "  "
    fmt = sep.join(f"{{:<{w}}}" for w in widths)

    hdr = "  " + fmt.format(*headers)
    sep_line = "  " + sep.join("─" * w for w in widths)

    print(hdr, file=file)
    print(sep_line, file=file)
    for row in rows:
        print("  " + fmt.format(*row), file=file)


def report_table(report: dict[str, Any]) -> None:
    """Print the full report as formatted tables."""
    summary = report.get("summary", {})
    per_model = report.get("per_model", [])
    timeseries = report.get("timeseries", [])
    cost_info = report.get("cost")

    if not summary:
        print("  (no data)")
        return

    # ── Summary block ──
    span = ""
    if summary.get("span_start") and summary.get("span_end"):
        s = summary["span_start"][:19]
        e = summary["span_end"][:19]
        span = f"{s}  to  {e}"

    print()
    print("  ╔══════════════════════════════════════════════════════════════════════╗")
    print("  ║                      TPS Analysis Report                           ║")
    print("  ╚══════════════════════════════════════════════════════════════════════╝")
    print()
    print(f"  Source files:  {report.get('source_count', 0)}")
    print(f"  Sessions:      {summary.get('session_count', 0)}")
    print(f"  Requests:      {summary.get('request_count', 0)}")
    if span:
        print(f"  Time span:     {span}")
    print()

    # Summary table
    rows_sum = [
        ["Total input tokens", f"{summary['total_input_tokens']:,}"],
        ["Total output tokens", f"{summary['total_output_tokens']:,}"],
        ["Total wall time", fmt_duration(summary["total_wall_time_ms"])],
        ["Average TPS", f"{summary['average_tps']:.1f}"],
        ["Average latency", fmt_duration(summary["average_latency_ms"])],
    ]

    print("  Summary")
    print("  " + "─" * 60)
    print_table(["Metric", "Value"], rows_sum)
    print()

    # Latency distribution
    lat = summary.get("latency_distribution_ms", {})
    if lat:
        lat_headers = ["p50", "p75", "p90", "p95", "p99", "max"]
        lat_row = [fmt_duration(lat.get(h, 0)) for h in lat_headers]
        print("  Latency distribution")
        print("  " + "─" * 60)
        print_table(lat_headers, [lat_row])
        print()

    # TPS distribution
    tps_dist = summary.get("tps_distribution", {})
    if tps_dist:
        tps_headers = ["p50", "p75", "p90", "p95", "p99", "max"]
        tps_row = [f"{tps_dist.get(h, 0):.1f}" for h in tps_headers]
        print("  TPS distribution")
        print("  " + "─" * 60)
        print_table(tps_headers, [tps_row])
        print()

    # ── Per-model breakdown ──
    if per_model:
        h = ["Model", "Req", "In Tokens", "Out Tokens", "Avg TPS", "Avg Lat"]
        r = []
        for m in per_model:
            r.append([
                m["model"][:44],
                str(m["request_count"]),
                f"{m['total_input_tokens']:,}",
                f"{m['total_output_tokens']:,}",
                f"{m['average_tps']:.1f}",
                fmt_duration(m["average_latency_ms"]),
            ])
        print("  Per-model breakdown")
        print("  " + "─" * 60)
        print_table(h, r)
        print()

    # ── Per-session breakdown ──
    per_session = report.get("per_session", [])
    if len(per_session) > 1:
        h = ["Session", "Req", "In Tokens", "Out Tokens", "Avg TPS", "Avg Lat"]
        r = []
        for s in per_session:
            r.append([
                f"S{s['session_id']}" if s["session_id"] else "-",
                str(s["request_count"]),
                f"{s['total_input_tokens']:,}",
                f"{s['total_output_tokens']:,}",
                f"{s['average_tps']:.1f}",
                fmt_duration(s["average_latency_ms"]),
            ])
        print("  Per-session breakdown")
        print("  " + "─" * 60)
        print_table(h, r)
        print()

    # ── Time series ──
    if timeseries:
        h = ["Time Window", "Req", "Avg TPS", "Avg Lat", "Output"]
        r = []
        for t in timeseries:
            start = t["window_start"][11:19]
            end = t["window_end"][11:19]
            r.append([
                f"{start}–{end}",
                str(t["request_count"]),
                f"{t['average_tps']:.1f}",
                fmt_duration(t["average_latency_ms"]),
                f"{t['total_output_tokens']:,}",
            ])
        print("  TPS over time")
        print("  " + "─" * 60)
        print_table(h, r)
        print()

    # ── Cost estimate ──
    if cost_info:
        print("  Cost estimate")
        print("  " + "─" * 60)
        has_pricing = any(m.get("cost") is not None for m in cost_info.get("models", []))
        if has_pricing:
            h = ["Model", "In Tokens", "Out Tokens", "Rate (in/out)", "Cost"]
            r = []
            for m in cost_info["models"]:
                if m["cost"] is not None:
                    rate = f"${m['input_rate']}/M  ${m['output_rate']}/M"
                    r.append([
                        m["model"][:38],
                        f"{m['input_tokens']:,}",
                        f"{m['output_tokens']:,}",
                        rate,
                        f"${m['cost']:.4f}",
                    ])
                else:
                    r.append([m["model"][:38], f"{m['input_tokens']:,}", f"{m['output_tokens']:,}", "?", "?"])
            print_table(h, r)
            print(f"\n  Total estimated cost:  ${cost_info['total_cost_usd']:.4f}")
        else:
            print("  No pricing data available for models used.")
            print("  Update MODEL_PRICES in cc_tps_analyze.py to enable cost estimation.")
        print()


def report_json(report: dict[str, Any], file: TextIO = sys.stdout) -> None:
    """Print the report as JSON."""
    json.dump(report, file, indent=2, default=str)
    file.write("\n")


def report_csv(report: dict[str, Any], file: TextIO = sys.stdout) -> None:
    """Print per-request records as CSV."""
    records = report.get("records", [])
    if not records:
        return

    writer = csv.DictWriter(
        file,
        fieldnames=["timestamp", "iso_timestamp", "model", "input_tokens", "output_tokens", "duration_ms", "tps"],
        extrasaction="ignore",
    )
    writer.writeheader()
    writer.writerows(records)


# ═══════════════════════════════════════════════════════════════════════════
# Build report
# ═══════════════════════════════════════════════════════════════════════════

def build_report(
    records: list[dict[str, Any]],
    source_count: int,
    window_secs: int = 60,
    include_cost: bool = False,
    include_timeseries: bool = True,
    include_per_model: bool = True,
) -> dict[str, Any]:
    """Assemble the full analysis report dict."""
    report: dict[str, Any] = {
        "source_count": source_count,
        "record_count": len(records),
    }

    report["records"] = records
    report["summary"] = compute_summary(records)

    if include_per_model:
        report["per_model"] = compute_per_model(records)

    report["per_session"] = compute_per_session(records)

    if include_timeseries:
        report["timeseries"] = compute_timeseries(records, window_secs)

    if include_cost:
        report["cost"] = estimate_cost(records)

    return report


# ═══════════════════════════════════════════════════════════════════════════
# CLI
# ═══════════════════════════════════════════════════════════════════════════

def parse_args(argv: list[str] | None = None) -> Namespace:
    parser = ArgumentParser(
        prog="cc_tps_analyze",
        description="Analyze Claude Code TPS monitor logs.",
    )
    parser.add_argument(
        "log_files",
        nargs="*",
        default=["cc_tps.log"],
        metavar="FILE",
        help="JSONL log file(s) (glob patterns supported). Default: cc_tps.log",
    )
    parser.add_argument(
        "-o", "--output",
        choices=["table", "json", "csv"],
        default="table",
        help="Output format (default: table)",
    )
    parser.add_argument(
        "-m", "--model",
        default=None,
        metavar="NAME",
        help="Filter by model name (substring match)",
    )
    parser.add_argument(
        "-w", "--window",
        type=int,
        default=60,
        metavar="SECS",
        help="Time window in seconds for timeseries bins (default: 60)",
    )
    parser.add_argument(
        "--cost",
        action="store_true",
        help="Include cost estimate",
    )
    parser.add_argument(
        "--no-timeseries",
        action="store_true",
        help="Skip timeseries section",
    )
    parser.add_argument(
        "--no-model",
        action="store_true",
        help="Skip per-model breakdown",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)

    records = load_records(args.log_files)
    records = filter_records(records, model_substr=args.model)

    if not records:
        print("No records found.", file=sys.stderr)
        return 1

    report = build_report(
        records,
        source_count=len(args.log_files),
        window_secs=args.window,
        include_cost=args.cost,
        include_timeseries=not args.no_timeseries,
        include_per_model=not args.no_model,
    )

    if args.output == "json":
        report_json(report)
    elif args.output == "csv":
        report_csv(report)
    else:
        report_table(report)

    return 0


if __name__ == "__main__":
    sys.exit(main())
