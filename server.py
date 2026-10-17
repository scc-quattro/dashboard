#!/usr/bin/env python3
"""Run a dependency-free, read-only dashboard, using synthetic data by default."""
from __future__ import annotations

import argparse
import ipaddress
import json
import re
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from collector import DEFAULT_CONFIG, collect, load_config
from traces import SESSION_ID, TraceReader

HERE = Path(__file__).resolve().parent
STATIC = HERE / "static"
ASSETS = {"/": ("index.html", "text/html"), "/index.html": ("index.html", "text/html"),
          "/app.js": ("app.js", "text/javascript"), "/style.css": ("style.css", "text/css"),
          "/favicon.svg": ("favicon.svg", "image/svg+xml"),
          "/vendor/echarts.min.js": ("vendor/echarts.min.js", "text/javascript")}


class SnapshotCache:
    def __init__(self, config_path, ttl=10):
        self.config_path, self.ttl = config_path, ttl
        self.lock = threading.Lock()
        self.value, self.updated = None, 0

    def get(self):
        with self.lock:
            if self.value is None or time.monotonic() - self.updated >= self.ttl:
                self.value = collect(self.config_path)
                self.updated = time.monotonic()
            return self.value


def standalone(snapshot):
    """Export curated dashboard data; never include local session contents."""
    html = (STATIC / "index.html").read_text(encoding="utf-8")
    css = (STATIC / "style.css").read_text(encoding="utf-8")
    js = (STATIC / "app.js").read_text(encoding="utf-8").replace("</script", "<\\/script")
    chart = (STATIC / "vendor/echarts.min.js").read_text(encoding="utf-8").replace("</script", "<\\/script")
    safe_data = json.dumps(snapshot, ensure_ascii=False, allow_nan=False).replace("<", "\\u003c")
    html = html.replace('<link rel="stylesheet" href="/style.css">', f"<style>{css}</style>")
    html = html.replace('<link rel="icon" href="/favicon.svg" type="image/svg+xml">', "")
    html = html.replace('<script src="/vendor/echarts.min.js" defer></script>', f"<script>{chart}</script>")
    return html.replace('<script src="/app.js" defer></script>',
                        f"<script>window.DASHBOARD_SNAPSHOT={safe_data};</script>"
                        f"<script>document.addEventListener('DOMContentLoaded', function(){{{js}}});</script>")


class Handler(BaseHTTPRequestHandler):
    server_version = "ProjectObservatory/1.0"

    def do_GET(self):
        parsed = urlsplit(self.path)
        route = parsed.path
        if route == "/api/traces" or route.startswith("/api/traces/"):
            if not self.trace_allowed():
                self.json_response(403, {"error": "详细会话仅允许从本机或 SSH 隧道查看。"})
                return
            reader = self.server.trace_reader
            if reader is None:
                self.json_response(200 if route == "/api/traces" else 404,
                                   {"enabled": False, "workspaces": [], "sessions": []})
                return
            try:
                if route == "/api/traces":
                    result = {"enabled": True, **reader.list_sessions()}
                else:
                    flow = route.endswith("/flow")
                    session_id = route.removeprefix("/api/traces/")
                    if flow:
                        session_id = session_id.removesuffix("/flow")
                    if not SESSION_ID.fullmatch(session_id):
                        raise FileNotFoundError
                    result = reader.read_flow(session_id) if flow else reader.read_session(
                        session_id, int(parse_qs(parsed.query).get("cursor", ["0"])[0]))
                self.json_response(200, result)
            except (FileNotFoundError, ValueError):
                self.json_response(404, {"error": "not found"})
            except Exception:
                self.json_response(503, {"error": "trace temporarily unavailable"})
            return
        if route in {"/api/dashboard", "/api/health", "/download.json", "/download.html"}:
            try:
                data = self.server.snapshot_cache.get()
                if route == "/download.html":
                    self.respond(200, standalone(data).encode(), "text/html", "project-dashboard.html")
                elif route == "/api/health":
                    self.json_response(200, {"ok": True, "demo": data["demo"],
                                             "generated_at": data["generated_at"], "warnings": len(data["warnings"])})
                else:
                    self.json_response(200, data, "project-dashboard.json" if route == "/download.json" else None)
            except Exception:
                self.json_response(503, {"error": "项目报告暂时无法读取，请检查配置。"})
            return
        if route in ASSETS:
            name, mime = ASSETS[route]
            self.respond(200, (STATIC / name).read_bytes(), mime)
            return
        self.respond(404, b"Not found", "text/plain")

    def trace_allowed(self):
        try:
            if not ipaddress.ip_address(self.client_address[0]).is_loopback:
                return False
        except ValueError:
            return False
        host = self.headers.get("Host", "").lower()
        if not re.fullmatch(r"(?:localhost|127\.0\.0\.1|\[::1\])(?::\d+)?", host):
            return False
        if self.headers.get("Sec-Fetch-Site", "").lower() == "cross-site":
            return False
        origin = self.headers.get("Origin")
        return not origin or bool(re.fullmatch(r"https?://(?:localhost|127\.0\.0\.1|\[::1\])(?::\d+)?", origin.lower()))

    def json_response(self, status, data, filename=None):
        self.respond(status, json.dumps(data, ensure_ascii=False, allow_nan=False).encode(), "application/json", filename)

    def respond(self, status, body, mime, filename=None):
        self.send_response(status)
        self.send_header("Content-Type", mime + "; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'")
        if filename:
            self.send_header("Content-Disposition", f'attachment; filename="{filename}"')
        self.end_headers()
        self.wfile.write(body)


def create_server(host="127.0.0.1", port=8790, config_path=DEFAULT_CONFIG, enable_traces=False):
    config_path = Path(config_path).resolve()
    config = load_config(config_path)
    server = ThreadingHTTPServer((host, port), Handler)
    server.snapshot_cache = SnapshotCache(config_path)
    server.trace_reader = None
    if enable_traces and config["traces"].get("enabled") is True:
        sessions = Path(config["traces"].get("sessions_dir") or "~/.codex/sessions").expanduser()
        sessions = (config["config_dir"] / sessions).resolve()
        workspaces = {item["id"]: item["root"] for item in config["projects"]}
        server.trace_reader = TraceReader(workspaces, sessions)
    return server


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8790)
    parser.add_argument("--config", type=Path, default=HERE / "dashboard.local.json" if (HERE / "dashboard.local.json").exists() else DEFAULT_CONFIG)
    parser.add_argument("--enable-traces", action="store_true", help="opt in to local traces (also requires config traces.enabled)")
    parser.add_argument("--export", type=Path, help="write a standalone HTML snapshot, then exit")
    args = parser.parse_args()
    if args.export:
        args.export.parent.mkdir(parents=True, exist_ok=True)
        args.export.write_text(standalone(collect(args.config)), encoding="utf-8")
        print(f"Snapshot saved: {args.export}")
        return
    server = create_server(args.host, args.port, args.config, args.enable_traces)
    print(f"Project Observatory listening on http://{args.host}:{server.server_port}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
