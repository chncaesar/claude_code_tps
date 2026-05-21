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
Claude Code TPS Monitor — local MITM proxy for tracking tokens-per-second.

Usage:
    # Terminal 1: start the monitor
    python cc_tps_monitor.py

    # Terminal 2: run Claude Code pointing at the proxy
    # (overrides your global ANTHROPIC_BASE_URL just for this command)
    ANTHROPIC_BASE_URL=http://localhost:18384 claude

The monitor reads your environment's ANTHROPIC_BASE_URL (if set) to
determine the upstream, or defaults to api.anthropic.com.
"""

import os
import sys
import json
import time
import signal
import ssl
import http.client
import http.server
import threading
from datetime import datetime
from urllib.parse import urlparse

# ── Configuration (via env vars) ────────────────────────────────────────
PORT = int(os.environ.get("CC_TPS_PORT", "18384"))
LOG_FILE = os.environ.get("CC_TPS_LOG", "cc_tps.log")

# Upstream: infer from ANTHROPIC_BASE_URL, then explicit overrides, then defaults.
_base_url = os.environ.get("ANTHROPIC_BASE_URL", "")
if _base_url:
    _parsed = urlparse(_base_url)
    UPSTREAM_SCHEME = _parsed.scheme  # http or https
    UPSTREAM_HOST = _parsed.hostname or "api.anthropic.com"
    UPSTREAM_PORT = _parsed.port or (443 if UPSTREAM_SCHEME == "https" else 80)
    UPSTREAM_PATH_PREFIX = _parsed.path.rstrip("/")  # e.g. "" or "/anthropic"
else:
    UPSTREAM_SCHEME = "https"
    UPSTREAM_HOST = "api.anthropic.com"
    UPSTREAM_PORT = 443
    UPSTREAM_PATH_PREFIX = ""

# Explicit overrides take precedence.
UPSTREAM_HOST = os.environ.get("CC_TPS_UPSTREAM_HOST", UPSTREAM_HOST)
UPSTREAM_PORT = int(os.environ.get("CC_TPS_UPSTREAM_PORT", str(UPSTREAM_PORT)))

# ── Stats data model ────────────────────────────────────────────────────
stats_lock = threading.Lock()
all_stats: list[dict] = []
session_start = time.time()

# Session tracking: each unique TCP connection gets a sequential ID.
_next_session_id: int = 1
_session_id_lock = threading.Lock()
# Map client_address -> assigned session ID (cleaned up on handler close)
_active_sessions: dict[str, int] = {}


def _new_session_id(client_addr: tuple[str, int]) -> int:
    """Assign a new session ID for a client address."""
    global _next_session_id
    with _session_id_lock:
        sid = _next_session_id
        _next_session_id += 1
        key = f"{client_addr[0]}:{client_addr[1]}"
        _active_sessions[key] = sid
        return sid


def release_session(client_addr: tuple[str, int]) -> None:
    """Release a session ID when the connection closes."""
    key = f"{client_addr[0]}:{client_addr[1]}"
    with _session_id_lock:
        _active_sessions.pop(key, None)


def record_stat(
    model: str,
    input_tokens: int,
    output_tokens: int,
    duration_ms: float,
    session_id: int = 0,
) -> None:
    """Record a completed request and print a live stat line to stderr."""
    tps = output_tokens / (duration_ms / 1000) if duration_ms > 0 else 0.0
    ts = datetime.now().strftime("%H:%M:%S")

    sid_tag = f"S{session_id:>3d}" if session_id else "   -"
    line = (f"  {sid_tag}  {ts}  {model[:40]:40s} │ "
            f"in:{input_tokens:>6}  out:{output_tokens:>6}  "
            f"t:{duration_ms / 1000:>5.2f}s  "
            f"TPS:{tps:>7.1f}")

    entry: dict[str, object] = {
        "timestamp": ts,
        "iso_timestamp": datetime.now().isoformat(),
        "unix_ts": time.time(),
        "session_id": session_id,
        "model": model,
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "duration_ms": round(duration_ms, 1),
        "tps": round(tps, 1),
    }

    with stats_lock:
        all_stats.append(entry)

    # Write log
    with open(LOG_FILE, "a") as f:
        f.write(json.dumps(entry) + "\n")

    # Live display to stderr
    print(line, file=sys.stderr)


def print_summary() -> None:
    """Print aggregated session summary to stderr."""
    with stats_lock:
        if not all_stats:
            return

        elapsed = time.time() - session_start
        total_in = sum(s["input_tokens"] for s in all_stats)
        total_out = sum(s["output_tokens"] for s in all_stats)
        total_wall = sum(s["duration_ms"] for s in all_stats) / 1000
        avg_tps = total_out / total_wall if total_wall > 0 else 0

        by_model: dict[str, list[dict]] = {}
        for s in all_stats:
            by_model.setdefault(s["model"], []).append(s)

        print(file=sys.stderr)
        print("  ╔════════════════════════════════════════════════════════════════╗",
              file=sys.stderr)
        print("  ║               TPS Monitor — Session Summary                  ║",
              file=sys.stderr)
        print("  ╚════════════════════════════════════════════════════════════════╝",
              file=sys.stderr)
        print(f"  Total requests:    {len(all_stats)}", file=sys.stderr)
        print(f"  Session duration:  {elapsed:.1f}s", file=sys.stderr)
        print(f"  Total input:       {total_in:>8,} tokens", file=sys.stderr)
        print(f"  Total output:      {total_out:>8,} tokens", file=sys.stderr)
        print(f"  Total wall time:   {total_wall:.1f}s", file=sys.stderr)
        print(f"  Average TPS:       {avg_tps:.1f}", file=sys.stderr)
        print(file=sys.stderr)

        # Model breakdown
        header = f"  {'Model':42s} {'Req':>5} {'In Tokens':>11} {'Out Tokens':>11} {'Avg TPS':>9}"
        sep = f"  {'─' * 42} {'─' * 5} {'─' * 11} {'─' * 11} {'─' * 9}"
        print(header, file=sys.stderr)
        print(sep, file=sys.stderr)
        for model, reqs in sorted(by_model.items()):
            m_in = sum(r["input_tokens"] for r in reqs)
            m_out = sum(r["output_tokens"] for r in reqs)
            m_time = sum(r["duration_ms"] for r in reqs) / 1000
            m_tps = m_out / m_time if m_time > 0 else 0
            print(f"  {model[:42]:42s} {len(reqs):>5} {m_in:>11,} {m_out:>11,} {m_tps:>9.1f}",
                  file=sys.stderr)
        print(file=sys.stderr)

        # Session breakdown (only shown when multiple sessions exist)
        by_session: dict[int, list[dict]] = {}
        for s in all_stats:
            sid = s.get("session_id", 0) or 0
            by_session.setdefault(sid, []).append(s)
        if len(by_session) > 1:
            print("  Per-session breakdown", file=sys.stderr)
            print("  " + "─" * 60, file=sys.stderr)
            sh = f"  {'Sess':>6} {'Req':>5} {'In Tokens':>11} {'Out Tokens':>11} {'Avg TPS':>9}"
            ss = f"  {'─' * 6} {'─' * 5} {'─' * 11} {'─' * 11} {'─' * 9}"
            print(sh, file=sys.stderr)
            print(ss, file=sys.stderr)
            for sid in sorted(by_session):
                reqs = by_session[sid]
                s_in = sum(r["input_tokens"] for r in reqs)
                s_out = sum(r["output_tokens"] for r in reqs)
                s_time = sum(r["duration_ms"] for r in reqs) / 1000
                s_tps = s_out / s_time if s_time > 0 else 0
                print(f"  S{sid:>4d}  {len(reqs):>5} {s_in:>11,} {s_out:>11,} {s_tps:>9.1f}",
                      file=sys.stderr)
            print(file=sys.stderr)


# ── SSE parser ──────────────────────────────────────────────────────────

class SSETokenParser:
    """Extract token usage & model from an Anthropic SSE response stream.

    Feeds:
      - ``message_start``  → input_tokens, model
      - ``message_delta``  → output_tokens
      - ``message_stop``   → marks completion
    """

    def __init__(self) -> None:
        self.buf = b""
        self.input_tokens: int = 0
        self.output_tokens: int = 0
        self.model: str = "unknown"
        self._done: bool = False

    @property
    def done(self) -> bool:
        return self._done

    def feed(self, data: bytes) -> None:
        if self._done or not data:
            return
        self.buf += data
        while b"\n\n" in self.buf:
            raw_event, self.buf = self.buf.split(b"\n\n", 1)
            self._parse(raw_event)

    @staticmethod
    def _extract(line: bytes, prefix: bytes) -> bytes | None:
        if line.startswith(prefix):
            return line[len(prefix):]
        return None

    def _parse(self, raw: bytes) -> None:
        event_type: str | None = None
        data_line: bytes | None = None

        for line in raw.split(b"\n"):
            stripped = line.strip()
            if stripped.startswith(b"event: "):
                event_type = stripped[7:].decode()
            elif stripped.startswith(b"data: "):
                data_line = stripped[6:]

        if data_line is None:
            return

        try:
            payload = json.loads(data_line)
        except json.JSONDecodeError:
            return

        if event_type == "message_start":
            msg = payload.get("message", {})
            usage = msg.get("usage", {})
            self.input_tokens = usage.get("input_tokens", 0) or 0
            if usage.get("output_tokens"):
                self.output_tokens = usage["output_tokens"]
            if msg.get("model"):
                self.model = msg["model"]

        elif event_type == "message_delta":
            usage = payload.get("usage", {})
            if usage.get("output_tokens"):
                self.output_tokens = usage["output_tokens"]

        elif event_type == "message_stop":
            self._done = True


# ── HTTP proxy handler ─────────────────────────────────────────────────

class TPSProxyHandler(http.server.BaseHTTPRequestHandler):
    """Forward requests to api.anthropic.com and extract token usage."""

    # Each handler instance serves one TCP connection.
    # Assign a session ID for every new connection.
    def setup(self) -> None:
        super().setup()
        self._session_id = _new_session_id(self.client_address)

    def finish(self) -> None:
        release_session(self.client_address)
        super().finish()

    # Silence the default "GET / ..." log lines
    def log_message(self, fmt: str, *args: object) -> None:
        pass

    # ── HTTP method dispatchers ──────────────────────────────────────

    def do_GET(self) -> None:
        self._proxy("GET")

    def do_POST(self) -> None:
        self._proxy("POST")

    def do_PUT(self) -> None:
        self._proxy("PUT")

    def do_DELETE(self) -> None:
        self._proxy("DELETE")

    # ── Core proxy logic ─────────────────────────────────────────────

    def _proxy(self, method: str) -> None:
        body = b""
        cl = int(self.headers.get("Content-Length", 0))
        if cl > 0:
            body = self.rfile.read(cl)

        headers = {k: v for k, v in self.headers.items() if k.lower() != "host"}
        is_messages = (method == "POST" and "/v1/messages" in self.path)

        # Forwarded path includes the upstream prefix (e.g. /anthropic/v1/messages)
        upstream_path = UPSTREAM_PATH_PREFIX + self.path

        try:
            if UPSTREAM_SCHEME == "https":
                conn: http.client.HTTPConnection = http.client.HTTPSConnection(
                    UPSTREAM_HOST, UPSTREAM_PORT,
                    context=ssl.create_default_context(),
                    timeout=600,
                )
            else:
                conn = http.client.HTTPConnection(
                    UPSTREAM_HOST, UPSTREAM_PORT,
                    timeout=600,
                )

            t0 = time.monotonic()
            conn.request(method, upstream_path, body=body, headers=headers)
            upstream = conn.getresponse()

            # ── Read full response ───────────────────────────────
            resp_body = upstream.read()
            duration_ms = (time.monotonic() - t0) * 1000

            # ── Extract token usage if applicable ────────────────
            if is_messages:
                ct = upstream.getheader("Content-Type", "")
                if "text/event-stream" in ct:
                    parser = SSETokenParser()
                    parser.feed(resp_body)
                    if parser.input_tokens or parser.output_tokens:
                        record_stat(
                            parser.model,
                            parser.input_tokens,
                            parser.output_tokens,
                            duration_ms,
                            session_id=self._session_id,
                        )
                elif "application/json" in ct:
                    try:
                        data = json.loads(resp_body)
                        usage = data.get("usage") or {}
                        if usage.get("input_tokens") is not None or usage.get("output_tokens") is not None:
                            record_stat(
                                data.get("model", "unknown"),
                                usage.get("input_tokens", 0) or 0,
                                usage.get("output_tokens", 0) or 0,
                                duration_ms,
                                session_id=self._session_id,
                            )
                    except json.JSONDecodeError:
                        pass

            # ── Forward response to client ──────────────────────
            self.send_response(upstream.status)
            for key, value in upstream.getheaders():
                kl = key.lower()
                if kl not in ("transfer-encoding", "content-encoding", "content-length", "alt-svc"):
                    self.send_header(key, value)
            self.end_headers()
            self.wfile.write(resp_body)

        except http.client.HTTPException as e:
            self._error(502, f"Upstream HTTP error: {e}")
        except (OSError, ssl.SSLError) as e:
            self._error(502, f"Upstream connection error: {e}")
        except Exception as e:
            self._error(502, f"Proxy error: {e}")

    def _error(self, status: int, msg: str) -> None:
        body = json.dumps({"error": {"message": msg}}).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)
        print(f"  [!] {msg}", file=sys.stderr)


# ── Entrypoint ──────────────────────────────────────────────────────────

def main() -> None:
    server = http.server.ThreadingHTTPServer(("", PORT), TPSProxyHandler)
    server.timeout = 0.5  # allows clean KeyboardInterrupt

    banner = f"""\

  ╔══════════════════════════════════════════════════════════╗
  ║              Claude Code TPS Monitor                    ║
  ║                                                          ║
  ║  Proxy addr  →  http://localhost:{PORT:<5}               ║
  ║  Upstream    →  {UPSTREAM_HOST}:{UPSTREAM_PORT:<5}{UPSTREAM_PATH_PREFIX:<12}  ║
  ║  Run →  ANTHROPIC_BASE_URL=localhost:{PORT} claude       ║
  ║  Log file    →  {LOG_FILE:<47}║
  ╚══════════════════════════════════════════════════════════╝
"""
    # Column header
    header = f"  {'Sess':>4}  {'Time':8s}  {'Model':40s} │ {'In':>6} {'Out':>6} {'Time':>6} {'TPS':>7}"
    sep = f"  {'─' * 4}  {'─' * 8}  {'─' * 40} │ {'─' * 6} {'─' * 6} {'─' * 6} {'─' * 7}"

    print(banner, file=sys.stderr)
    print(header, file=sys.stderr)
    print(sep, file=sys.stderr)

    # ── Ctrl+C handler ────────────────────────────────────────────
    def shutdown(sig: int, frame: object) -> None:
        print(file=sys.stderr)
        print_summary()
        sys.exit(0)

    signal.signal(signal.SIGINT, shutdown)

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        shutdown(0, None)


if __name__ == "__main__":
    main()
