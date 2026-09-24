"""Outbound URL guard (NFR-03 / NFR-09).

* only ``https`` (``http`` only when explicitly allowed for local tests)
* host must match the *global* allow-list (deployment config, not editable from the UI) AND the
  source's own ``allowed_hosts`` - checked on the initial URL and on every redirect hop
* IP-literal hosts and hosts resolving to private / loopback / link-local ranges are rejected
  (SSRF), unless ``allow_private_networks`` is set for tests
"""

from __future__ import annotations

import ipaddress
import socket
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from urllib.parse import urlsplit

import yaml

from sanctions_agent.http.errors import ErrorClass, FetchError
from sanctions_agent.settings import get_settings


def host_matches(host: str, pattern: str) -> bool:
    host = host.lower().rstrip(".")
    pattern = pattern.lower().rstrip(".")
    if pattern.startswith("*."):
        suffix = pattern[1:]  # ".example.com"
        return host.endswith(suffix) and host != suffix[1:]
    return host == pattern


@dataclass(frozen=True)
class GlobalAllowList:
    hosts: tuple[str, ...]
    purposes: dict[str, str] = field(default_factory=dict)

    def allows(self, host: str) -> bool:
        return any(host_matches(host, p) for p in self.hosts)


@lru_cache(maxsize=4)
def _load_allow_list(path: str) -> GlobalAllowList:
    data = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    entries = data.get("hosts", [])
    hosts = tuple(str(e["host"]) for e in entries)
    purposes = {str(e["host"]): str(e.get("purpose", "")) for e in entries}
    return GlobalAllowList(hosts=hosts, purposes=purposes)


def global_allow_list() -> GlobalAllowList:
    return _load_allow_list(str(get_settings().allowed_hosts_file))


class UrlGuard:
    def __init__(
        self,
        source_hosts: list[str] | tuple[str, ...],
        *,
        allow_http: bool = False,
        allow_private_networks: bool | None = None,
        global_list: GlobalAllowList | None = None,
    ) -> None:
        self.source_hosts = tuple(source_hosts)
        self.allow_http = allow_http
        s = get_settings()
        self.allow_private = (
            s.http_allow_private_networks if allow_private_networks is None else allow_private_networks
        )
        self.global_list = global_list or global_allow_list()

    def check(self, url: str, *, hop: int = 0) -> None:
        parts = urlsplit(url)
        scheme = parts.scheme.lower()
        if scheme != "https" and not (self.allow_http and scheme == "http"):
            raise FetchError(
                ErrorClass.REDIRECT_BLOCKED if hop else ErrorClass.HOST_NOT_ALLOWED,
                f"scheme {scheme!r} not allowed for {url}",
            )
        host = (parts.hostname or "").lower()
        if not host:
            raise FetchError(ErrorClass.HOST_NOT_ALLOWED, f"no host in {url}")
        err = ErrorClass.REDIRECT_BLOCKED if hop else ErrorClass.HOST_NOT_ALLOWED
        if not self.global_list.allows(host):
            raise FetchError(err, f"host {host} is not on the global allow-list")
        if not any(host_matches(host, p) for p in self.source_hosts):
            raise FetchError(err, f"host {host} is not in this source's allowed_hosts")
        self._check_ip(host, err)

    def _check_ip(self, host: str, err: ErrorClass) -> None:
        if self.allow_private:
            return
        try:
            ipaddress.ip_address(host)
            raise FetchError(err, f"IP-literal host {host} not allowed")
        except ValueError:
            pass
        try:
            infos = socket.getaddrinfo(host, 443, proto=socket.IPPROTO_TCP)
        except OSError:
            # Behind an egress proxy DNS may only resolve on the proxy; the allow-list already applies.
            return
        for info in infos:
            ip = ipaddress.ip_address(info[4][0])
            if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_multicast:
                raise FetchError(err, f"host {host} resolves to non-public address {ip}")
