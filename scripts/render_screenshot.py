#!/usr/bin/env python3
"""Render docs/screenshot.svg (+ .png) from real `fm` output.

Runs the actual CLI, as a subprocess, against a throwaway HTTP server on
127.0.0.1 that answers with mock JSON (fake spaces, fake people, no real
account). Nothing here talks to the network. The server's loopback address
is then replaced with the public hostname it stands in for, so the image
shows what a session against api.formist.ai looks like, not the test
harness. `fm`'s own output is otherwise printed exactly as it runs.

Usage: python3 scripts/render_screenshot.py [--out docs/screenshot]

Requires rsvg-convert for the .png (skipped with a note if it is missing).
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import urlparse

REPO = Path(__file__).resolve().parent.parent

# Mock data only: no real customer, person, event, id or hostname.
SPACE_LAUNCH = "3f1b2c9a-7e4d-4b8a-9c21-5a6b7c8d9e0f"
SPACE_SUMMIT = "9d4e7a21-1f3c-4a6b-8e2d-0c5f9a1b3d7e"

HEALTH_PAYLOAD = {
    "status": "ok",
    "checked_at": "2026-01-15T09:00:03Z",
    "checks": [
        {"name": "database", "status": "ok", "detail": "12ms", "fatal": True},
        {"name": "scheduler", "status": "ok", "detail": "last tick 4s ago", "fatal": False},
        {"name": "storage", "status": "ok", "detail": "", "fatal": False},
    ],
}

SPACES_PAYLOAD = {
    "items": [
        {"id": SPACE_LAUNCH, "region": "ae", "member_count": 12, "name": "Product launch 2027"},
        {"id": SPACE_SUMMIT, "region": "global", "member_count": 7, "name": "Annual partner summit"},
    ],
    "total": 2,
    "offset": 0,
}

SEARCH_PAYLOAD = {
    "items": [
        {
            "source": "file",
            "path": "Contracts/Venue Agreement - Riverside Hall.docx",
            "file_id": "a1c2e3f4-5b6d-4a7e-8f9c-0d1e2f3a4b5c",
            "snippet": "the venue agreement for Riverside Hall confirms a deposit "
            "and catering for 150 guests",
        },
        {
            "source": "message",
            "subject": "Carol -> Bob: venue contract",
            "snippet": "the venue contract needs one more signature before Friday",
        },
    ],
    "total": 2,
}


class MockHandler(BaseHTTPRequestHandler):
    def log_message(self, *a) -> None:  # silence
        pass

    def _json(self, payload: object) -> None:
        body = json.dumps(payload).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802 - http.server's naming
        path = urlparse(self.path).path
        if path == "/health":
            self._json(HEALTH_PAYLOAD)
        elif path == "/admin/spaces":
            self._json(SPACES_PAYLOAD)
        elif path.startswith("/projects/") and path.endswith("/search"):
            self._json(SEARCH_PAYLOAD)
        else:
            self.send_response(404)
            self.end_headers()


def run_fm(args: list[str], *, server_addr: str, token: str | None) -> str:
    """Run `fm` for real, against the mock server, and return combined output."""
    env = {"FORMIST_CONFIG_DIR": tempfile.mkdtemp(prefix="fm-shot-config-")}
    if token:
        env["FORMIST_TOKEN"] = token
    cmd = [sys.executable, "-m", "formist_cli", "--host", server_addr, *args]
    # stdout and stderr merged into one stream, in the order `fm` actually
    # wrote them (the provenance banner first, same as a real terminal).
    result = subprocess.run(
        cmd, cwd=REPO / "src", env=env, stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT, text=True, timeout=30,
    )
    return result.stdout


def staged(server_addr: str, api_base: str) -> list[tuple[str, str]]:
    """The (displayed prompt, raw fm output) pairs, in session order."""
    # An opaque, dot-free string: `fm login` tokens never contain a dot (see
    # formist_cli.tokens), and this stands in for one via $FORMIST_TOKEN, the
    # documented CI path.
    mock_token = "fm_mock_screenshot_token_do_not_use"
    return [
        ("fm health", run_fm(["health"], server_addr=server_addr, token=None)),
        ("fm spaces", run_fm(["spaces"], server_addr=server_addr, token=mock_token)),
        (
            f'fm kb search "venue contract" --space {SPACE_LAUNCH}',
            run_fm(
                ["kb", "search", "venue", "contract", "--space", SPACE_LAUNCH],
                server_addr=server_addr, token=mock_token,
            ),
        ),
    ]


def delabel(text: str, *, server_addr: str, api_base: str) -> str:
    """Swap the loopback test server for the public host it stands in for.

    Order matters: the longer `http://host:port` form is replaced before the
    bare `host:port` form, since the bare form is a substring of it.
    """
    text = text.replace(api_base, "https://api.formist.ai")
    text = text.replace(server_addr, "formist")
    text = text.replace("[host: --host flag]", "[host: default]")
    return text


# --- SVG rendering -----------------------------------------------------------

CW = 8.6   # column (character) width in px, for the monospace font below
LH = 20    # line height
PAD = 22
BAR = 34
FONT = 14.5
THEME = {
    "bg": "#16181d",
    "fg": "#d8dee9",
    "dim": "#6b7280",
    "chrome": "#22252c",
    "green": "#a3d98b",
}


def esc(s: str) -> str:
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def build_lines(sessions: list[tuple[str, str]]) -> list[tuple[str, str, bool]]:
    """Flatten sessions into (text, color, bold) rows, prompt lines marked bold."""
    lines: list[tuple[str, str, bool]] = []
    for i, (prompt, output) in enumerate(sessions):
        if i:
            lines.append(("", THEME["fg"], False))
        lines.append((f"$ {prompt}", THEME["green"], True))
        for raw in output.rstrip("\n").splitlines():
            color = THEME["dim"] if raw.startswith("->") or raw.startswith("  ") else THEME["fg"]
            lines.append((raw, color, False))
    return lines


def render_svg(lines: list[tuple[str, str, bool]], out: Path) -> None:
    cols = max((len(t) for t, _, _ in lines), default=60) + 2
    width = round(cols * CW + PAD * 2)
    height = round(len(lines) * LH + PAD * 2 + BAR)
    parts = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" '
        f'viewBox="0 0 {width} {height}" xml:space="preserve">',
        f'<rect width="{width}" height="{height}" rx="12" fill="{THEME["bg"]}"/>',
        f'<path d="M0 12a12 12 0 0 1 12-12h{width - 24}a12 12 0 0 1 12 12v{BAR - 12}'
        f'h-{width}z" fill="{THEME["chrome"]}"/>',
    ]
    for i, c in enumerate(["#ff5f57", "#febc2e", "#28c840"]):
        parts.append(f'<circle cx="{22 + i * 20}" cy="{BAR / 2}" r="6" fill="{c}"/>')
    parts.append(
        f'<text x="{width / 2}" y="{BAR / 2 + 5}" fill="{THEME["dim"]}" '
        f'font-family="Menlo, monospace" font-size="13" text-anchor="middle">fm</text>'
    )
    parts.append(
        '<g font-family="&apos;JetBrains Mono&apos;, &apos;SF Mono&apos;, Menlo, '
        f'Consolas, monospace" font-size="{FONT}">'
    )
    for row, (text, color, bold) in enumerate(lines):
        y = PAD + BAR + row * LH + LH - 5
        weight = ' font-weight="700"' if bold else ""
        parts.append(f'<text x="{PAD}" y="{y}" fill="{color}"{weight}>{esc(text)}</text>')
    parts.append("</g></svg>")
    out.write_text("\n".join(parts) + "\n")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", default=str(REPO / "docs" / "screenshot"))
    args = parser.parse_args()

    server = HTTPServer(("127.0.0.1", 0), MockHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        host, port = server.server_address
        server_addr = f"{host}:{port}"
        api_base = f"http://{server_addr}"
        sessions = staged(server_addr, api_base)
    finally:
        server.shutdown()
        thread.join(timeout=5)

    sessions = [(p, delabel(o, server_addr=server_addr, api_base=api_base)) for p, o in sessions]
    lines = build_lines(sessions)

    out_svg = Path(f"{args.out}.svg")
    out_png = Path(f"{args.out}.png")
    render_svg(lines, out_svg)

    png = subprocess.run(["rsvg-convert", "-z", "2", "-o", str(out_png), str(out_svg)])
    if png.returncode == 0:
        print(f"{out_svg} + {out_png}")
    else:
        print(f"{out_svg} (no rsvg-convert: PNG skipped)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
