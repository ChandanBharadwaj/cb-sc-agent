"""HTTP fetching with the evidence the BRD asks for (section 9.4 / NFR-03).

* descriptive User-Agent on every request (OFAC and France reject requests without one)
* redirects followed manually so every hop is checked against the allow-list and logged
* signed redirect URLs (UN -> Azure blob) are never cached or reused; tokens are redacted in evidence
* conditional GET (ETag / Last-Modified) when a previous fetch supplied validators
* body streamed to disk while hashing (SHA-256) with a per-source size cap
* TLS certificate chain captured (Python 3.13 ``get_verified_chain``)
"""

from __future__ import annotations

import hashlib
import random
import ssl
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit

import httpx

from sanctions_agent.http.errors import ErrorClass, FetchError, classify_status
from sanctions_agent.http.guard import UrlGuard
from sanctions_agent.settings import get_settings

REDIRECT_STATUSES = {301, 302, 303, 307, 308}
MAX_REDIRECTS = 10
_SENSITIVE_PARAMS = {
    "sig",
    "signature",
    "x-amz-signature",
    "x-amz-credential",
    "api_key",
    "apikey",
    "se",
    "skoid",
}

ProgressCallback = Callable[[int, int | None], None]


def redact_url(url: str) -> str:
    parts = urlsplit(url)
    if not parts.query:
        return url
    q = [
        (k, "REDACTED" if k.lower() in _SENSITIVE_PARAMS else v)
        for k, v in parse_qsl(parts.query, keep_blank_values=True)
    ]
    return urlunsplit(parts._replace(query=urlencode(q)))


@dataclass
class FetchResult:
    requested_url: str
    final_url: str
    redirect_chain: list[dict[str, Any]]
    http_status: int
    headers: dict[str, str]
    path: Path | None
    sha256: str | None
    size_bytes: int
    not_modified: bool
    tls_chain_pem: str | None
    tls_leaf_sha256: str | None
    fetched_at: datetime
    duration_ms: int
    conditional: dict[str, str] = field(default_factory=dict)
    content_type: str | None = None

    def evidence(self) -> dict[str, Any]:
        d = asdict(self)
        d.pop("path")
        return d


@dataclass
class RetryPolicy:
    max_attempts: int = 4
    base_delay_s: float = 5.0
    max_delay_s: float = 300.0

    def delay(self, attempt: int, retry_after: float | None = None) -> float:
        if retry_after is not None:
            return min(self.max_delay_s, max(0.0, retry_after))
        raw = min(self.max_delay_s, self.base_delay_s * (2 ** (attempt - 1)))
        return raw * (0.5 + random.random() / 2)  # full-jitter-ish, never below half


def _tls_chain(response: httpx.Response) -> tuple[str | None, str | None]:
    try:
        stream = response.extensions.get("network_stream")
        ssl_obj = stream.get_extra_info("ssl_object") if stream is not None else None
        if ssl_obj is None:
            return None, None
        chain_der: list[bytes] = list(ssl_obj.get_verified_chain() or [])
        if not chain_der:
            leaf = ssl_obj.getpeercert(binary_form=True)
            chain_der = [leaf] if leaf else []
        if not chain_der:
            return None, None
        pem = "".join(ssl.DER_cert_to_PEM_cert(c) for c in chain_der)
        return pem, hashlib.sha256(chain_der[0]).hexdigest()
    except Exception:  # evidence capture must never break the fetch
        return None, None


class HttpFetcher:
    def __init__(
        self,
        guard: UrlGuard,
        *,
        user_agent: str | None = None,
        connect_timeout: float | None = None,
        read_timeout: float | None = None,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        s = get_settings()
        self.guard = guard
        self.user_agent = user_agent or s.user_agent
        timeout = httpx.Timeout(
            connect=connect_timeout or s.http_connect_timeout,
            read=read_timeout or s.http_read_timeout,
            write=30.0,
            pool=30.0,
        )
        self._client = httpx.Client(
            timeout=timeout,
            follow_redirects=False,
            transport=transport,
            headers={"User-Agent": self.user_agent, "Accept": "*/*", "Accept-Encoding": "gzip, deflate"},
        )

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> HttpFetcher:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # ------------------------------------------------------------------------------------------
    def fetch_to_file(
        self,
        url: str,
        dest: Path,
        *,
        conditional: dict[str, str] | None = None,
        max_bytes: int = 500 * 1024 * 1024,
        progress: ProgressCallback | None = None,
        extra_headers: dict[str, str] | None = None,
        should_cancel: Callable[[], bool] | None = None,
    ) -> FetchResult:
        """GET ``url`` into ``dest``. Raises FetchError with an ErrorClass on any failure."""
        started = time.monotonic()
        fetched_at = datetime.now(UTC)
        self.guard.check(url, hop=0)
        headers = dict(extra_headers or {})
        cond_used: dict[str, str] = {}
        if conditional:
            if conditional.get("etag"):
                headers["If-None-Match"] = conditional["etag"]
                cond_used["If-None-Match"] = conditional["etag"]
            if conditional.get("last_modified"):
                headers["If-Modified-Since"] = conditional["last_modified"]
                cond_used["If-Modified-Since"] = conditional["last_modified"]

        chain: list[dict[str, Any]] = []
        current = url
        for hop in range(MAX_REDIRECTS + 1):
            try:
                with self._client.stream("GET", current, headers=headers) as resp:
                    if resp.status_code in REDIRECT_STATUSES:
                        location = resp.headers.get("location")
                        chain.append({"url": redact_url(current), "status": resp.status_code})
                        if not location:
                            raise FetchError(
                                ErrorClass.HTTP_4XX, "redirect without Location", http_status=resp.status_code
                            )
                        nxt = urljoin(current, location)
                        self.guard.check(nxt, hop=hop + 1)
                        current = nxt
                        continue
                    chain.append({"url": redact_url(current), "status": resp.status_code})
                    resp_headers = {k.lower(): v for k, v in resp.headers.items()}
                    evidence_base = {
                        "requested_url": url,
                        "final_url": redact_url(current),
                        "redirect_chain": chain,
                        "http_status": resp.status_code,
                        "headers": resp_headers,
                    }
                    if resp.status_code == 304:
                        pem, leaf = _tls_chain(resp)
                        return FetchResult(
                            requested_url=url,
                            final_url=redact_url(current),
                            redirect_chain=chain,
                            http_status=304,
                            headers=resp_headers,
                            path=None,
                            sha256=None,
                            size_bytes=0,
                            not_modified=True,
                            tls_chain_pem=pem,
                            tls_leaf_sha256=leaf,
                            fetched_at=fetched_at,
                            duration_ms=int((time.monotonic() - started) * 1000),
                            conditional=cond_used,
                            content_type=resp_headers.get("content-type"),
                        )
                    if resp.status_code >= 400:
                        retry_after = _parse_retry_after(resp.headers.get("retry-after"))
                        body_preview = resp.read()[:300].decode("utf-8", "replace")
                        raise FetchError(
                            classify_status(resp.status_code),
                            f"HTTP {resp.status_code} from {urlsplit(current).hostname}: {body_preview!r}",
                            http_status=resp.status_code,
                            retry_after_s=retry_after,
                            evidence=evidence_base,
                        )
                    pem, leaf = _tls_chain(resp)
                    total = _int_or_none(resp.headers.get("content-length"))
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    h = hashlib.sha256()
                    size = 0
                    with open(dest, "wb") as fh:
                        for chunk in resp.iter_bytes(chunk_size=1024 * 256):
                            size += len(chunk)
                            if size > max_bytes:
                                raise FetchError(
                                    ErrorClass.TOO_LARGE,
                                    f"body exceeds {max_bytes} bytes",
                                    evidence=evidence_base,
                                )
                            h.update(chunk)
                            fh.write(chunk)
                            if progress:
                                progress(size, total)
                            if should_cancel and should_cancel():
                                raise FetchError(ErrorClass.CANCELLED, "cancel requested during download")
                    if size == 0:
                        raise FetchError(
                            ErrorClass.EMPTY_BODY,
                            "empty response body",
                            http_status=resp.status_code,
                            evidence=evidence_base,
                        )
                    return FetchResult(
                        requested_url=url,
                        final_url=redact_url(current),
                        redirect_chain=chain,
                        http_status=resp.status_code,
                        headers=resp_headers,
                        path=dest,
                        sha256=h.hexdigest(),
                        size_bytes=size,
                        not_modified=False,
                        tls_chain_pem=pem,
                        tls_leaf_sha256=leaf,
                        fetched_at=fetched_at,
                        duration_ms=int((time.monotonic() - started) * 1000),
                        conditional=cond_used,
                        content_type=resp_headers.get("content-type"),
                    )
            except FetchError:
                raise
            except httpx.TimeoutException as e:
                raise FetchError(ErrorClass.TRANSIENT_NETWORK, f"timeout: {e!r}") from e
            except httpx.ConnectError as e:
                if "CERTIFICATE_VERIFY_FAILED" in str(e) or "SSL" in str(e).upper():
                    raise FetchError(ErrorClass.TLS_ERROR, f"TLS error: {e}") from e
                raise FetchError(ErrorClass.TRANSIENT_NETWORK, f"connect error: {e}") from e
            except httpx.TransportError as e:
                raise FetchError(ErrorClass.TRANSIENT_NETWORK, f"transport error: {e!r}") from e
        raise FetchError(ErrorClass.TOO_MANY_REDIRECTS, f"more than {MAX_REDIRECTS} redirects from {url}")

    # ------------------------------------------------------------------------------------------
    def get(
        self,
        url: str,
        *,
        params: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
        max_bytes: int = 50 * 1024 * 1024,
    ) -> tuple[bytes, FetchResult]:
        """Small in-memory GET (APIs, RSS, HTML notices) with the same guard and redirect checks."""
        if params:
            sep = "&" if urlsplit(url).query else "?"
            url = f"{url}{sep}{urlencode(params, doseq=True)}"
        import tempfile

        with tempfile.TemporaryDirectory() as td:
            res = self.fetch_to_file(url, Path(td) / "body", max_bytes=max_bytes, extra_headers=headers)
            body = res.path.read_bytes() if res.path else b""
            res.path = None
            return body, res


def fetch_with_retry(
    fn: Callable[[], FetchResult],
    policy: RetryPolicy,
    *,
    on_attempt: Callable[[int, FetchResult | None, FetchError | None], None] | None = None,
    sleep: Callable[[float], None] = time.sleep,
    should_cancel: Callable[[], bool] | None = None,
) -> tuple[FetchResult, int]:
    """Run ``fn`` with retries for retryable error classes. Every attempt is reported to ``on_attempt``."""
    last: FetchError | None = None
    for attempt in range(1, policy.max_attempts + 1):
        try:
            result = fn()
            if on_attempt:
                on_attempt(attempt, result, None)
            return result, attempt
        except FetchError as e:
            last = e
            if on_attempt:
                on_attempt(attempt, None, e)
            if not e.retryable or attempt == policy.max_attempts:
                raise
            if should_cancel and should_cancel():
                raise FetchError(ErrorClass.CANCELLED, "cancel requested between retries") from e
            sleep(policy.delay(attempt, e.retry_after_s))
    assert last is not None
    raise last


def retry_call[T](
    fn: Callable[[], T], policy: RetryPolicy, *, sleep: Callable[[float], None] = time.sleep
) -> T:
    """Generic retry for API calls that raise FetchError (same retryable classes as downloads)."""
    for attempt in range(1, policy.max_attempts + 1):
        try:
            return fn()
        except FetchError as e:
            if not e.retryable or attempt == policy.max_attempts:
                raise
            sleep(policy.delay(attempt, e.retry_after_s))
    raise AssertionError("unreachable")


def _int_or_none(v: str | None) -> int | None:
    try:
        return int(v) if v is not None else None
    except ValueError:
        return None


def _parse_retry_after(v: str | None) -> float | None:
    if not v:
        return None
    try:
        return float(v)
    except ValueError:
        from email.utils import parsedate_to_datetime

        try:
            return max(0.0, (parsedate_to_datetime(v) - datetime.now(UTC)).total_seconds())
        except (TypeError, ValueError):
            return None
