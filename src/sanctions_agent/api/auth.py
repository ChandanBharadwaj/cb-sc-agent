"""Authentication and role checks for the API and console.

Roles are cumulative: viewer < operator < reviewer < admin.
* ``basic`` - users from SANCTIONS_UI_USERS ("user:password:role,...") - dev / small deployments
* ``proxy`` - trust X-Forwarded-User / X-Forwarded-Roles set by an SSO gateway (e.g. oauth2-proxy);
              only safe when the app is reachable exclusively through that gateway
* ``none``  - everyone is admin (local development only; refused when environment=prod)
"""

from __future__ import annotations

import base64
import secrets
from collections.abc import Callable
from dataclasses import dataclass

from fastapi import Depends, HTTPException, Request, status

from sanctions_agent.settings import get_settings

ROLE_RANK = {"viewer": 0, "operator": 1, "reviewer": 2, "admin": 3}


@dataclass(frozen=True)
class Principal:
    user: str
    role: str

    def has(self, role: str) -> bool:
        return ROLE_RANK[self.role] >= ROLE_RANK[role]


def _users() -> dict[str, tuple[str, str]]:
    raw = get_settings().ui_users.get_secret_value()
    out: dict[str, tuple[str, str]] = {}
    for entry in filter(None, (e.strip() for e in raw.split(","))):
        user, _, rest = entry.partition(":")
        pw, _, role = rest.rpartition(":")
        if user and pw and role in ROLE_RANK:
            out[user] = (pw, role)
    return out


def _unauthorized() -> HTTPException:
    return HTTPException(
        status.HTTP_401_UNAUTHORIZED,
        "authentication required",
        headers={"WWW-Authenticate": 'Basic realm="sanctions-agent"'},
    )


def current_principal(request: Request) -> Principal:
    s = get_settings()
    if s.auth_mode == "none":
        if s.environment == "prod":
            raise HTTPException(500, "auth_mode=none is not allowed in prod")
        return Principal("dev", "admin")
    if s.auth_mode == "proxy":
        user = request.headers.get("x-forwarded-user") or request.headers.get("x-forwarded-email")
        if not user:
            raise _unauthorized()
        roles = [r.strip() for r in (request.headers.get("x-forwarded-roles") or "viewer").split(",")]
        best = max((r for r in roles if r in ROLE_RANK), key=lambda r: ROLE_RANK[r], default="viewer")
        return Principal(user, best)
    header = request.headers.get("authorization", "")
    if not header.lower().startswith("basic "):
        raise _unauthorized()
    try:
        user, _, pw = base64.b64decode(header[6:]).decode("utf-8").partition(":")
    except (ValueError, UnicodeDecodeError) as e:
        raise _unauthorized() from e
    known = _users().get(user)
    if known is None or not secrets.compare_digest(known[0].encode(), pw.encode()):
        raise _unauthorized()
    return Principal(user, known[1])


def require(role: str) -> Callable[[Principal], Principal]:
    def dep(p: Principal = Depends(current_principal)) -> Principal:
        if not p.has(role):
            raise HTTPException(status.HTTP_403_FORBIDDEN, f"requires role {role} (you are {p.role})")
        return p

    return dep
