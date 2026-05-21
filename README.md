# Claude Code TPS Monitor

A local MITM proxy that intercepts Claude Code's API calls, extracts token usage from streaming responses, and displays real-time tokens-per-second metrics. Includes a log analyzer for post-hoc reporting.

```
                  POST /v1/messages                         POST /v1/messages
Claude Code ─────────────────────►  TPS Monitor (:18384) ───────────────────► api.anthropic.com
                                      │
                                      │  ◄── SSE response (with usage stats)
                                      │
                                      ▼
                              ┌ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─
                               │  14:32:15  sonnet-4.6  │ in:  123  out:  45  TPS: 36.6
                              │  14:33:02  sonnet-4.6  │ in:   89  out: 234  TPS: 67.8
                               │  14:33:45      haiku  │ in:   12  out:  67  TPS: 134.0
                              └ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─
```

Zero external dependencies — uses only Python standard library.

## Quick Start

```bash
# Terminal 1: start the monitor
python3 cc_tps_monitor.py

# Terminal 2: run Claude Code normally
ANTHROPIC_BASE_URL=http://localhost:18384 claude
```

Every LLM request that Claude Code makes will be logged with its TPS in Terminal 1.

Press `Ctrl+C` in the monitor to print an aggregated session summary.

## Live Display

### Per-request output (stderr)

```
  Time      Model                                      │     In    Out   Time     TPS
  ────────  ────────────────────────────────────────── │ ────── ────── ────── ───────
  14:32:15  claude-sonnet-4-20250514                   │ in:   123  out:    45  t: 1.23s  TPS:   36.6
  14:32:18  claude-sonnet-4-20250514                   │ in:    89  out:   234  t: 3.45s  TPS:   67.8
  14:33:02  claude-3-haiku-20240307                    │ in:    12  out:    67  t: 0.50s  TPS:  134.0
```

### Session summary (Ctrl+C)

```
  ╔════════════════════════════════════════════════════════════════╗
  ║               TPS Monitor — Session Summary                  ║
  ╚════════════════════════════════════════════════════════════════╝
  Total requests:    23
  Session duration:  145.3s
  Total input:       4,234 tokens
  Total output:      8,901 tokens
  Total wall time:   112.0s
  Average TPS:       79.5

  Model                                          Req   In Tokens   Out Tokens    Avg TPS
  ──────────────────────────────────────────     ───   ──────────   ──────────   ────────
  claude-sonnet-4-20250514                       18        3,912        7,834       68.2
  claude-3-haiku-20240307                         5          322        1,067      134.0
```

## Log Format

Every request is appended to `cc_tps.log` as a newline-delimited JSON (JSONL) line:

```jsonl
{"timestamp":"14:32:15","iso_timestamp":"2025-05-21T14:32:15.123456","unix_ts":1747823535.123,"model":"claude-sonnet-4-20250514","input_tokens":123,"output_tokens":45,"duration_ms":1230.0,"tps":36.6}
{"timestamp":"14:33:02","iso_timestamp":"2025-05-21T14:33:02.654321","unix_ts":1747823582.654,"model":"claude-3-haiku-20240307","input_tokens":12,"output_tokens":67,"duration_ms":500.0,"tps":134.0}
```

## Analysis

Use the built-in analyzer for post-hoc reporting:

```bash
python3 cc_tps_analyze.py                # analyze cc_tps.log
python3 cc_tps_analyze.py path/to/*.log  # glob multiple files
python3 cc_tps_analyze.py --json         # machine-readable output
python3 cc_tps_analyze.py --csv          # export to CSV
python3 cc_tps_analyze.py --cost         # include cost estimate
```

## Configuration

| Variable | Default | Description |
|---|---|---|
| `CC_TPS_PORT` | `18384` | Local proxy port |
| `CC_TPS_LOG` | `cc_tps.log` | JSONL log path |
| `CC_TPS_UPSTREAM_HOST` | *(see below)* | Upstream host override |
| `CC_TPS_UPSTREAM_PORT` | *(see below)* | Upstream port override |

The proxy auto-detects the upstream from your environment:

1. If `ANTHROPIC_BASE_URL` is set (e.g. `https://api.deepseek.com/anthropic`), the proxy parses the host, port, and path prefix from it. This means **no extra configuration is needed** when using a custom upstream.
2. `CC_TPS_UPSTREAM_HOST` / `CC_TPS_UPSTREAM_PORT` explicitly override the detected values.
3. Falls back to `api.anthropic.com:443`.

```bash
# ANTHROPIC_BASE_URL already set → proxy uses it automatically
python3 cc_tps_monitor.py

# Explicit override if needed
CC_TPS_UPSTREAM_HOST=custom.host.com python3 cc_tps_monitor.py
```

## How It Works

1. The proxy starts an HTTP server on `localhost:18384`.
2. Claude Code is configured via `ANTHROPIC_BASE_URL` to send requests to the proxy instead of directly to `api.anthropic.com`.
3. The proxy forwards all requests (preserving `x-api-key`, `anthropic-version`, etc.) and returns the response transparently.
4. For `POST /v1/messages` responses, the proxy parses the SSE event stream to extract:
   - `message_start.message.usage.input_tokens` — input token count
   - `message_delta.usage.output_tokens` — output token count
   - `message.message.model` — model identifier
5. TPS is calculated as `output_tokens / (response_duration / 1000)`.

## Requirements

- Python 3.10+
- Network access to `api.anthropic.com`

## License

Apache License, Version 2.0. See [LICENSE](LICENSE) for details.
