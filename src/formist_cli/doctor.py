"""``fm doctor`` — one readable answer to "is the deployment all right".

``fm health`` answers about one environment and is the machine-shaped command.
``doctor`` answers across all of them, which means it has to survive the ones that
do not answer at all.

THE DISTINCTION THIS MODULE EXISTS TO KEEP
------------------------------------------
"could not reach ae" and "reached ae and it is unhealthy" are different facts with
different next actions, and a rollup that renders both as a red dot has destroyed
the only information the operator needed. It is the same failure as reporting an
exit code instead of test counts — the summary that loses the distinction is
precisely the one people trust.

So :class:`Status` separates UNREACHABLE from DEGRADED from FAILED, and an
unexpected payload SHAPE is its own status rather than a pass. That last one is not
hypothetical here: the retired we-carbon stack still answers ``200`` with the
pre-Evane health schema (``{"status": "ok"}`` and no ``checks``), so a rollup that
shrugs at an unfamiliar shape and prints "ok" would launder a stale deployment into
a green tick. ``formist_cli.targets`` refuses those hosts, and this is the second line.

Stdlib only.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import IntEnum


class Status(IntEnum):
    """Ordered worst-last, so :meth:`worst` is a max."""

    OK = 0
    UNKNOWN = 1
    DEGRADED = 2
    FAILED = 3
    UNREACHABLE = 4

    @classmethod
    def worst(cls, statuses) -> Status:
        return max(statuses, default=cls.OK)

    @property
    def glyph(self) -> str:
        return {
            Status.OK: "ok",
            Status.UNKNOWN: "??",
            Status.DEGRADED: "!!",
            Status.FAILED: "XX",
            Status.UNREACHABLE: "--",
        }[self]


@dataclass(frozen=True)
class EnvReport:
    """One environment's answer, and why it is what it is."""

    env: str
    status: Status
    detail: str
    #: Alembic identity, when ``/admin/instance`` was reachable and authorised.
    migrations: str | None = None
    #: Whether a usable credential is stored for this environment.
    credential: str | None = None

    @classmethod
    def unreachable(cls, env: str, reason: str) -> EnvReport:
        return cls(env=env, status=Status.UNREACHABLE, detail=reason)

    @classmethod
    def from_health(cls, env: str, payload: object) -> EnvReport:
        """Read a ``/health`` body.

        A payload that is not the current schema is UNKNOWN, never OK — see the
        module docstring on why a familiar-looking 200 is not evidence.
        """
        if not isinstance(payload, dict):
            return cls(
                env=env,
                status=Status.UNKNOWN,
                detail=(
                    f"/health returned {type(payload).__name__}, not the current "
                    "schema (expected an object with checked_at + checks)."
                ),
            )

        checks = payload.get("checks")
        if not isinstance(checks, list):
            return cls(
                env=env,
                status=Status.UNKNOWN,
                detail=(
                    "/health has no `checks` list — this is the legacy schema. "
                    "The current build returns checked_at + checks, so this is "
                    "either a very old deployment or the retired we-carbon stack."
                ),
            )

        bad = [c for c in checks if isinstance(c, dict) and c.get("status") != "ok"]
        if not bad:
            return cls(
                env=env,
                status=Status.OK,
                detail=f"{len(checks)} check{'s' if len(checks) != 1 else ''} ok",
            )

        fatal = [c for c in bad if c.get("fatal")]
        named = ", ".join(
            f"{c.get('name')}={c.get('status')}"
            + (f" ({c.get('detail')})" if c.get("detail") else "")
            for c in bad
        )
        return cls(
            env=env,
            status=Status.FAILED if fatal else Status.DEGRADED,
            detail=named,
        )


def rollup(reports) -> str:
    """Render every report, including the ones that failed.

    Every environment appears. A rollup that stops at the first failure answers a
    different question than the one asked, and one region being down is exactly
    when the others matter most.
    """
    lines = []
    for report in reports:
        lines.append(f"  [{report.status.glyph}] {report.env:<8} {report.detail}")
        if report.migrations:
            lines.append(f"          migrations: {report.migrations}")
        if report.credential:
            lines.append(f"          credential: {report.credential}")
    return "\n".join(lines)
