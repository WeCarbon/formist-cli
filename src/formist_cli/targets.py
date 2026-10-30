"""Which deployment a command talks to.

``fm`` talks to ``https://api.formist.ai`` unless ``--host`` names another
deployment. ``--host`` takes either a name from :data:`HOSTS` or an API origin
(``https://api.example.com``, ``127.0.0.1:8160``) for a custom deployment.

Spaces are addressed by UUID, never by name: names are not unique, so a name can
silently pick the wrong space.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import urlparse

#: Deployment kinds. ``env`` on a :class:`Target` is one of these; it decides how
#: the server is expected to behave (multi-region admin routing, local dev), not
#: where credentials are stored (that is :attr:`Target.name`).
REGION_LOCAL = "local"
REGION_GLOBAL = "global"
REGION_AE = "ae"
REGION_FORMIST = "formist"
REGION_CUSTOM = "custom"

VALID_ENVS = (REGION_LOCAL, REGION_AE, REGION_GLOBAL, REGION_FORMIST, REGION_CUSTOM)

#: Deployments that serve the whole ``/admin`` surface themselves. A regional
#: Evane host (``evane-ae``) serves only a region-local subset and answers 410
#: for the rest.
ADMIN_ONLY_ENVS = (REGION_GLOBAL, REGION_FORMIST, REGION_CUSTOM)

DEFAULT_HOST = "formist"


@dataclass(frozen=True)
class HostSpec:
    """One named deployment."""

    name: str
    api_base: str
    env: str
    product: str
    description: str


HOSTS: dict[str, HostSpec] = {
    "formist": HostSpec(
        "formist", "https://api.formist.ai", REGION_FORMIST, "Formist", "Formist"
    ),
    "evane": HostSpec(
        "evane", "https://api.evane.ai", REGION_GLOBAL, "Evane", "Evane, Global region"
    ),
    "evane-ae": HostSpec(
        "evane-ae", "https://ae.api.evane.ai", REGION_AE, "Evane", "Evane, UAE region"
    ),
    "formist-cn": HostSpec(
        "formist-cn",
        "https://formist-api.we-carbon.com",
        REGION_FORMIST,
        "Formist",
        "Formist China",
    ),
}

_UUID_RE = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
)
_LOOPBACK = {"127.0.0.1", "localhost", "::1"}


class TargetError(Exception):
    """A refusal to point at something. The message is written for the user."""


@dataclass(frozen=True)
class Target:
    """A resolved destination.

    ``name`` keys the stored credential and is what the user typed (or the
    default); ``env`` is the deployment kind (see :data:`VALID_ENVS`).
    """

    env: str
    api_base: str
    name: str = DEFAULT_HOST
    product: str | None = None

    @property
    def is_prod(self) -> bool:
        return self.env != REGION_LOCAL


def is_admin_env(env: str) -> bool:
    """True when a deployment of kind ``env`` serves ``/admin`` itself."""
    return env in ADMIN_ONLY_ENVS


def host_names() -> tuple[str, ...]:
    return tuple(HOSTS)


def _custom(value: str) -> Target:
    parsed = urlparse(value if "://" in value else f"//{value}", scheme="")
    host = (parsed.hostname or "").lower()
    if not host or parsed.path not in ("", "/") or parsed.query or parsed.fragment:
        raise TargetError(
            f"--host {value!r} is neither a known deployment "
            f"({', '.join(HOSTS)}) nor an API origin such as "
            "https://api.example.com or 127.0.0.1:8160."
        )
    loopback = host in _LOOPBACK
    scheme = parsed.scheme or ("http" if loopback else "https")
    if scheme not in ("http", "https"):
        raise TargetError(f"--host {value!r}: only http and https are supported.")
    netloc = f"{host}:{parsed.port}" if parsed.port else host
    if ":" in host:
        netloc = f"[{host}]:{parsed.port}" if parsed.port else f"[{host}]"
    return Target(
        env=REGION_LOCAL if loopback else REGION_CUSTOM,
        api_base=f"{scheme}://{netloc}",
        name=netloc,
    )


def resolve(host: str | None) -> Target:
    """Resolve ``--host`` to a :class:`Target`. ``None`` means :data:`DEFAULT_HOST`."""
    value = (host or DEFAULT_HOST).strip()
    if not value:
        value = DEFAULT_HOST
    spec = HOSTS.get(value.lower())
    if spec is None:
        return _custom(value)
    return Target(env=spec.env, api_base=spec.api_base, name=spec.name, product=spec.product)


def check_api_base(url: str) -> None:
    """Refuse a base URL that has no hostname."""
    parsed = urlparse(url if "://" in url else f"//{url}")
    if not parsed.hostname:
        raise TargetError(f"could not read a hostname out of {url!r}")


def require_space_id(value: str) -> str:
    """A space is addressed by its UUID, never by name."""
    value = value.strip()
    if not _UUID_RE.match(value):
        raise TargetError(
            f"--space needs a space id (a UUID), got {value!r}. fm never looks a "
            "space up by name, because names are not unique. Find the id with "
            "`fm spaces`."
        )
    return value
