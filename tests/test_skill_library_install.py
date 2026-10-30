"""``fm skill-library-install``: installing one skill-library package into a
space, idempotently.

Its own file, matching `test_formist_cli_skill_library_sync.py`'s reasoning: that
file guards `ev skill-library-sync` (the GLOBAL admin-surface command);
`skill-library-install` is a different, PROJECT-scoped surface added later, and
neither module's tests should have to know about the other.

Run: uv run pytest tests/test_formist_cli_skill_library_install.py -q
"""

from __future__ import annotations

from uuid import uuid4

import pytest

from formist_cli import skill_library
from formist_cli.targets import resolve as resolve_target

LOCAL = resolve_target("127.0.0.1:8160")
SPACE = "452093ea-7b91-491e-88b6-3285edd48a31"


# ===========================================================================
# resolve_package / _version_sort_key: pure, no HTTP
# ===========================================================================
class TestVersionSortKey:
    def test_numeric_segments_sort_numerically_not_lexically(self):
        versions = ["1.9.0", "1.10.0", "1.2.0"]
        ordered = sorted(versions, key=skill_library._version_sort_key)
        assert ordered == ["1.2.0", "1.9.0", "1.10.0"]

    def test_a_non_numeric_segment_never_crashes_and_sorts_below_numeric_ones(self):
        versions = ["1.0.0", "1.0.0-beta"]
        ordered = sorted(versions, key=skill_library._version_sort_key)
        assert ordered == ["1.0.0-beta", "1.0.0"]


class TestResolvePackage:
    def test_a_uuid_matches_the_exact_row_no_version_comparison(self):
        id1, id2 = str(uuid4()), str(uuid4())
        p1 = {"id": id1, "package": "com.x.a", "version": "1.0.0"}
        p2 = {"id": id2, "package": "com.x.a", "version": "2.0.0"}
        chosen, matches = skill_library.resolve_package([p1, p2], id1)
        assert chosen is p1
        assert matches == [p1]

    def test_a_reverse_dns_id_with_one_row_resolves_to_it(self):
        p1 = {"id": str(uuid4()), "package": "com.x.a", "version": "1.0.0"}
        chosen, matches = skill_library.resolve_package([p1], "com.x.a")
        assert chosen is p1
        assert matches == [p1]

    def test_a_reverse_dns_id_shared_by_several_rows_picks_the_newest_version(self):
        p1 = {"id": "id-1", "package": "com.x.a", "version": "1.0.0", "created_at": "2026-01-01"}
        p2 = {"id": "id-2", "package": "com.x.a", "version": "2.0.0", "created_at": "2026-02-01"}
        p3 = {"id": "id-3", "package": "com.x.b", "version": "9.0.0", "created_at": "2026-03-01"}
        chosen, matches = skill_library.resolve_package([p1, p2, p3], "com.x.a")
        assert chosen is p2
        assert matches == [p1, p2]  # p3 is a DIFFERENT package id, never a candidate

    def test_an_unknown_package_ref_refuses(self):
        with pytest.raises(skill_library.SkillLibraryError):
            skill_library.resolve_package([], "com.x.nope")

    def test_an_unknown_uuid_refuses_even_when_other_rows_exist(self):
        p1 = {"id": "id-1", "package": "com.x.a", "version": "1.0.0"}
        with pytest.raises(skill_library.SkillLibraryError):
            skill_library.resolve_package([p1], str(uuid4()))


class TestAlreadyInstalled:
    def test_a_matching_package_id_is_found(self):
        skills = [{"id": "s1", "name": "x", "package_id": "pkg-1"}]
        assert skill_library.already_installed(skills, "pkg-1") is not None

    def test_no_match_returns_none(self):
        skills = [{"id": "s1", "name": "x", "package_id": "pkg-other"}]
        assert skill_library.already_installed(skills, "pkg-1") is None

    def test_a_skill_with_no_package_id_at_all_is_not_a_match(self):
        skills = [{"id": "s1", "name": "x", "package_id": None}]
        assert skill_library.already_installed(skills, "pkg-1") is None


# ===========================================================================
# The fake transport: member-facing project routes only, matching the real
# response shapes (routes/skill_library.py::SkillPackageRead / InstallResult,
# routes/skills.py's generic list envelope).
# ===========================================================================
class FakeLibraryServer:
    def __init__(self, *, packages=None, skills=None):
        self.packages: list[dict] = list(packages or [])
        self.skills: list[dict] = list(skills or [])
        self.calls: list[tuple[str, str]] = []

    def get(self, target, path, *, token=None, params=None, **kw):
        self.calls.append(("GET", path))
        if path.endswith("/skill-library/packages"):
            return {"items": list(self.packages)}
        if path.endswith("/skills"):
            limit = int((params or {}).get("limit", 200))
            offset = int((params or {}).get("offset", 0))
            page = self.skills[offset : offset + limit]
            return {"items": page, "total": len(self.skills), "limit": limit, "offset": offset}
        raise AssertionError(f"unexpected GET {path}")

    def post(self, target, path, body, *, token=None, **kw):
        self.calls.append(("POST", path))
        assert path.endswith("/install") and "/skill-library/packages/" in path
        package_id = path.split("/skill-library/packages/")[1].split("/")[0]
        skill = {"id": str(uuid4()), "name": "installed skill", "package_id": package_id}
        self.skills.append(skill)
        return {"item": skill}


class _Args:
    def __init__(self, **kw):
        self.__dict__.update(kw)


def _install_args(**overrides):
    defaults = dict(env="local", space=SPACE, package=None, apply=False, json=False)
    defaults.update(overrides)
    return _Args(**defaults)


def _patch_effective(monkeypatch, server):
    from formist_cli import cli

    class _Eff:
        target = LOCAL
        space = SPACE

    monkeypatch.setattr(skill_library, "api", server)
    monkeypatch.setattr(cli, "_effective", lambda args, **kw: _Eff())
    monkeypatch.setattr(cli, "_token_for", lambda eff: "t")


# ===========================================================================
# run_install: dry run
# ===========================================================================
class TestInstallDryRun:
    def test_dry_run_makes_no_post(self, monkeypatch, capsys):
        server = FakeLibraryServer(
            packages=[{"id": "pkg-1", "package": "com.x.a", "name": "ESG Report", "version": "1.0.0"}]
        )
        _patch_effective(monkeypatch, server)

        rc = skill_library.run_install(_install_args(package="com.x.a"))

        assert rc == 0
        assert not any(c[0] == "POST" for c in server.calls)
        assert "DRY RUN" in capsys.readouterr().err


# ===========================================================================
# run_install --apply
# ===========================================================================
class TestInstallApply:
    def test_apply_installs_through_the_member_route(self, monkeypatch, capsys):
        server = FakeLibraryServer(
            packages=[{"id": "pkg-1", "package": "com.x.a", "name": "ESG Report", "version": "1.0.0"}]
        )
        _patch_effective(monkeypatch, server)

        rc = skill_library.run_install(_install_args(package="com.x.a", apply=True))

        assert rc == 0
        assert any(c == ("POST", f"/projects/{SPACE}/skill-library/packages/pkg-1/install") for c in server.calls)
        assert "installed." in capsys.readouterr().out

    def test_picking_the_newest_version_is_announced_with_every_candidate(self, monkeypatch, capsys):
        server = FakeLibraryServer(
            packages=[
                {"id": "pkg-1", "package": "com.x.a", "name": "ESG Report", "version": "1.0.0", "created_at": "2026-01-01"},
                {"id": "pkg-2", "package": "com.x.a", "name": "ESG Report", "version": "2.0.0", "created_at": "2026-02-01"},
            ]
        )
        _patch_effective(monkeypatch, server)

        rc = skill_library.run_install(_install_args(package="com.x.a"))

        assert rc == 0
        err = capsys.readouterr().err
        assert "2 package rows share package id" in err
        assert "pkg-1" in err and "pkg-2" in err

    def test_json_summary_shape(self, monkeypatch, capsys):
        import json as json_mod

        server = FakeLibraryServer(
            packages=[{"id": "pkg-1", "package": "com.x.a", "name": "ESG Report", "version": "1.0.0"}]
        )
        _patch_effective(monkeypatch, server)

        rc = skill_library.run_install(_install_args(package="com.x.a", apply=True, json=True))

        assert rc == 0
        payload = json_mod.loads(capsys.readouterr().out)
        assert payload["already_installed"] is False
        assert payload["package"]["id"] == "pkg-1"
        assert "skill" in payload and payload["skill"]["id"]


# ===========================================================================
# The idempotency contract: the required failure-path test: an already-
# installed package must send NOTHING.
# ===========================================================================
class TestInstallIsIdempotent:
    def test_an_already_installed_package_sends_no_post(self, monkeypatch, capsys):
        server = FakeLibraryServer(
            packages=[{"id": "pkg-1", "package": "com.x.a", "name": "ESG Report", "version": "1.0.0"}],
            skills=[{"id": "skill-1", "name": "ESG Report", "package_id": "pkg-1"}],
        )
        _patch_effective(monkeypatch, server)

        rc = skill_library.run_install(_install_args(package="com.x.a", apply=True))

        assert rc == 0
        assert not any(c[0] == "POST" for c in server.calls), server.calls
        assert "already installed" in capsys.readouterr().out

    def test_idempotency_is_checked_even_under_dry_run(self, monkeypatch, capsys):
        """Idempotence is a fact about the space, not a side effect of --apply
       : a dry run must say so too, not just discover it a run later."""
        server = FakeLibraryServer(
            packages=[{"id": "pkg-1", "package": "com.x.a", "name": "ESG Report", "version": "1.0.0"}],
            skills=[{"id": "skill-1", "name": "ESG Report", "package_id": "pkg-1"}],
        )
        _patch_effective(monkeypatch, server)

        rc = skill_library.run_install(_install_args(package="com.x.a", apply=False))

        assert rc == 0
        out = capsys.readouterr().out
        assert "already installed" in out
        assert "DRY RUN" not in out, "there is nothing left to dry-run: say so plainly"
