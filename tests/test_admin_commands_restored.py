"""The ten operator commands run in `fm` again.

`instance`, `users`, `logs`, `space-plan`, `usage-tokens`, `alerts`, `settings`,
`update`, `airtable` and `whatsapp` had been replaced by stubs that printed a
pointer to a separate tool and exited, while that tool was not yet released. Their
implementations never left this package; these tests drive each one through the
real `cli.main` against a fake server and check that the request reaches the
command's own endpoint.
"""

from __future__ import annotations

import io
import json
import urllib.request

import pytest

from formist_cli import cli, privileges

SPACE = "11111111-2222-3333-4444-555555555555"
HOST = ["--host", "127.0.0.1:8160"]

TEN = (
    "instance", "users", "logs", "space-plan", "usage-tokens",
    "alerts", "settings", "update", "airtable", "whatsapp",
)


class _Resp(io.BytesIO):

    def __enter__(self):
        return self

    def __exit__(self, *a):
        self.close()

    def getheader(self, *_a, **_k):
        return None


@pytest.fixture
def server(monkeypatch, tmp_path):
    monkeypatch.setenv("FORMIST_TOKEN", "fmtok_test")
    monkeypatch.setenv("FORMIST_CONFIG_DIR", str(tmp_path / "cfg"))
    calls: list[tuple[str, str]] = []

    def urlopen(request, timeout=None):
        path = request.full_url.split("127.0.0.1:8160", 1)[-1]
        calls.append((request.get_method(), path))
        if "/events/stream" in path:
            return _Resp(b'event: ready\ndata: {"deployment": {"build": "test"}}\n\n')
        return _Resp(json.dumps({"items": [], "total": 0, "ops": [], "plan_token": "pt"}).encode())

    monkeypatch.setattr(urllib.request, "urlopen", urlopen)
    return calls


@pytest.mark.parametrize("name", TEN)
def test_help_describes_the_real_command(name, capsys):
    with pytest.raises(SystemExit) as exc:
        cli.main([name, "--help"])
    assert exc.value.code == 0
    out = capsys.readouterr().out
    assert out.startswith(f"usage: fm {name}")
    assert "--admin" in out
    assert "no longer runs here" not in out


@pytest.mark.parametrize("name", TEN)
def test_each_is_platform_admin_only_again(name, capsys):
    assert privileges.requires_admin(name)
    assert cli.main([name, *HOST, *_minimal_args(name)]) == 2
    assert "needs --admin" in capsys.readouterr().err


def _minimal_args(name: str) -> list[str]:
    return {
        "space-plan": ["--space", SPACE],
        "airtable": ["--space", SPACE, "direction"],
        "update": ["--space", SPACE, "payload.json"],
    }.get(name, [])


READ_ONLY = [
    ("instance", [], ("GET", "/admin/instance")),
    ("users", [], ("GET", "/admin/users")),
    ("space-plan", ["--space", SPACE], ("GET", f"/admin/spaces/{SPACE}/plan")),
    ("usage-tokens", ["--json"], ("GET", "/admin/usage/tokens")),
    ("alerts", ["--json"], ("GET", "/admin/alerts")),
    ("settings", ["--json"], ("GET", "/admin/settings")),
    ("airtable", ["--space", SPACE, "direction"], ("GET", f"/projects/{SPACE}/admin/airtable/direction")),
    ("whatsapp", ["--json"], ("GET", "/admin/whatsapp")),
]


@pytest.mark.parametrize(("name", "extra", "expected"), READ_ONLY, ids=[r[0] for r in READ_ONLY])
def test_a_read_reaches_the_commands_own_endpoint(name, extra, expected, server):
    rc = cli.main([name, "--admin", *HOST, *extra])
    assert rc == 0
    assert any(m == expected[0] and p.startswith(expected[1]) for m, p in server), server


def test_update_without_apply_is_a_dry_run(server, tmp_path):
    payload = tmp_path / "ops.json"
    payload.write_text(json.dumps({"ops": [{"$action": "update", "$kind": "TASK", "$id": SPACE, "$set": {"title": "x"}}]}))
    assert cli.main(["update", "--admin", *HOST, "--space", SPACE, str(payload)]) == 0
    assert server == [("POST", f"/projects/{SPACE}/admin/data-changes")]


def test_logs_subscribes_to_the_event_stream(server, capsys):
    """The stream opens, the server's banner is read (so the real follower ran),
    and the fake server's end of stream is reported as such."""
    cli.main(["logs", "--admin", *HOST, "--mode", "jobs", "--json"])
    assert server and server[0][0] == "GET" and server[0][1].startswith("/admin/events/stream")
    err = capsys.readouterr().err
    assert "NOT LISTENING" not in err
    assert "STREAM CLOSED BY THE SERVER" in err
