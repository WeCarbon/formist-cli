"""`scripts/render_screenshot.py`: the mock server never ends up in the picture.

The script runs the real `fm` against a loopback HTTP server so the screenshot
shows genuine output, not handwritten text. The one way that could go wrong
unnoticed is the loopback address leaking into the rendered image, since
nothing downstream would flag it as a bug. `delabel` is the line that stands
between "real output" and "a published screenshot naming a local test port",
so it is the one piece of this script worth a unit test; the rest is
subprocess/HTTP/SVG plumbing exercised by actually running it.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "render_screenshot.py"
spec = importlib.util.spec_from_file_location("render_screenshot", SCRIPT)
assert spec and spec.loader
render_screenshot = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = render_screenshot
spec.loader.exec_module(render_screenshot)


def test_delabel_replaces_the_full_origin_before_the_bare_host() -> None:
    server_addr = "127.0.0.1:54321"
    api_base = f"http://{server_addr}"
    text = (
        f"-> read {server_addr}  {api_base}   [host: --host flag]\n"
        f"  space 11111111-1111-1111-1111-111111111111   [space: --space flag]"
    )

    out = render_screenshot.delabel(text, server_addr=server_addr, api_base=api_base)

    assert server_addr not in out
    assert "https://api.formist.ai" in out
    assert "-> read formist  https://api.formist.ai   [host: default]" in out


def test_delabel_is_a_noop_on_text_with_no_loopback_address() -> None:
    server_addr = "127.0.0.1:54321"
    api_base = f"http://{server_addr}"
    text = "=== 2 hit(s) (of 2) ===\n[file] Contracts/Venue Agreement.docx"

    assert render_screenshot.delabel(text, server_addr=server_addr, api_base=api_base) == text


def test_build_lines_marks_prompts_and_separates_sessions_with_a_blank_line() -> None:
    sessions = [("fm health", "line one\nline two"), ("fm spaces", "line three")]

    lines = render_screenshot.build_lines(sessions)

    prompts = [text for text, _color, bold in lines if bold]
    assert prompts == ["$ fm health", "$ fm spaces"]
    blanks = [text for text, _color, _bold in lines if text == ""]
    assert len(blanks) == 1  # one separator between the two sessions, none before the first
