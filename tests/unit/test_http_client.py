import hashlib

import httpx
import pytest
import respx

from sanctions_agent.http.client import HttpFetcher, RetryPolicy, fetch_with_retry, redact_url
from sanctions_agent.http.errors import ErrorClass, FetchError
from sanctions_agent.http.guard import GlobalAllowList, UrlGuard

GL = GlobalAllowList(hosts=("list.example.gov", "*.blob.example.net", "other.example.org"))


def _fetcher(hosts=("list.example.gov", "*.blob.example.net")):
    return HttpFetcher(
        UrlGuard(list(hosts), allow_private_networks=True, global_list=GL), user_agent="test-agent/1.0"
    )


@respx.mock
def test_fetch_streams_hashes_and_sends_user_agent(tmp_path):
    route = respx.get("https://list.example.gov/list.xml").mock(
        return_value=httpx.Response(200, content=b"<list/>", headers={"ETag": '"v1"'})
    )
    with _fetcher() as f:
        res = f.fetch_to_file("https://list.example.gov/list.xml", tmp_path / "a.xml")
    assert route.calls[0].request.headers["user-agent"] == "test-agent/1.0"
    assert res.sha256 == hashlib.sha256(b"<list/>").hexdigest()
    assert res.size_bytes == 7 and res.headers["etag"] == '"v1"'
    assert (tmp_path / "a.xml").read_bytes() == b"<list/>"


@respx.mock
def test_redirect_to_allowed_signed_host_is_followed_and_token_redacted(tmp_path):
    respx.get("https://list.example.gov/consolidated.xml").mock(
        return_value=httpx.Response(
            302, headers={"Location": "https://acct.blob.example.net/f.xml?sv=1&sig=SECRET"}
        )
    )
    respx.get("https://acct.blob.example.net/f.xml?sv=1&sig=SECRET").mock(
        return_value=httpx.Response(200, content=b"x")
    )
    with _fetcher() as f:
        res = f.fetch_to_file("https://list.example.gov/consolidated.xml", tmp_path / "b")
    assert "SECRET" not in res.final_url and "sig=REDACTED" in res.final_url
    assert [h["status"] for h in res.redirect_chain] == [302, 200]


@respx.mock
def test_redirect_to_non_allowed_host_is_blocked(tmp_path):
    respx.get("https://list.example.gov/x").mock(
        return_value=httpx.Response(301, headers={"Location": "https://evil.example.com/x"})
    )
    with _fetcher() as f, pytest.raises(FetchError) as e:
        f.fetch_to_file("https://list.example.gov/x", tmp_path / "c")
    assert e.value.error_class == ErrorClass.REDIRECT_BLOCKED


def test_non_https_and_unknown_hosts_rejected(tmp_path):
    with _fetcher() as f:
        with pytest.raises(FetchError) as e1:
            f.fetch_to_file("http://list.example.gov/x", tmp_path / "d")
        with pytest.raises(FetchError) as e2:
            f.fetch_to_file("https://other.example.org/x", tmp_path / "d")  # global ok, not source-allowed
    assert e1.value.error_class == ErrorClass.HOST_NOT_ALLOWED
    assert e2.value.error_class == ErrorClass.HOST_NOT_ALLOWED


def test_private_ip_literal_rejected(tmp_path):
    gl = GlobalAllowList(hosts=("127.0.0.1",))
    f = HttpFetcher(UrlGuard(["127.0.0.1"], allow_private_networks=False, global_list=gl))
    with pytest.raises(FetchError, match="IP-literal"):
        f.fetch_to_file("https://127.0.0.1/x", tmp_path / "e")


@respx.mock
def test_error_classification_and_retry_on_5xx_burst(tmp_path):
    route = respx.get("https://list.example.gov/fsf.xml").mock(
        side_effect=[httpx.Response(500), httpx.Response(503), httpx.Response(200, content=b"<ok/>")]
    )
    attempts = []
    with _fetcher() as f:
        _res, n = fetch_with_retry(
            lambda: f.fetch_to_file("https://list.example.gov/fsf.xml", tmp_path / "f"),
            RetryPolicy(max_attempts=5, base_delay_s=1),
            on_attempt=lambda a, r, e: attempts.append((a, e.error_class if e else "ok")),
            sleep=lambda s: None,
        )
    assert n == 3 and route.call_count == 3
    assert attempts == [(1, ErrorClass.HTTP_5XX), (2, ErrorClass.HTTP_5XX), (3, "ok")]


@respx.mock
def test_403_is_not_retried(tmp_path):
    route = respx.get("https://list.example.gov/sdn.xml").mock(
        return_value=httpx.Response(403, text="Forbidden")
    )
    with _fetcher() as f, pytest.raises(FetchError) as e:
        fetch_with_retry(
            lambda: f.fetch_to_file("https://list.example.gov/sdn.xml", tmp_path / "g"),
            RetryPolicy(max_attempts=5),
            sleep=lambda s: None,
        )
    assert e.value.error_class == ErrorClass.HTTP_403 and route.call_count == 1


@respx.mock
def test_conditional_get_not_modified(tmp_path):
    route = respx.get("https://list.example.gov/l.xml").mock(return_value=httpx.Response(304))
    with _fetcher() as f:
        res = f.fetch_to_file("https://list.example.gov/l.xml", tmp_path / "h", conditional={"etag": '"v1"'})
    assert res.not_modified and route.calls[0].request.headers["if-none-match"] == '"v1"'


@respx.mock
def test_size_cap(tmp_path):
    respx.get("https://list.example.gov/big").mock(return_value=httpx.Response(200, content=b"x" * 5000))
    with _fetcher() as f, pytest.raises(FetchError) as e:
        f.fetch_to_file("https://list.example.gov/big", tmp_path / "i", max_bytes=1000)
    assert e.value.error_class == ErrorClass.TOO_LARGE


@respx.mock
def test_timeout_is_transient(tmp_path):
    respx.get("https://list.example.gov/slow").mock(side_effect=httpx.ReadTimeout("slow"))
    with _fetcher() as f, pytest.raises(FetchError) as e:
        f.fetch_to_file("https://list.example.gov/slow", tmp_path / "j")
    assert e.value.error_class == ErrorClass.TRANSIENT_NETWORK and e.value.retryable


def test_redact_url():
    assert redact_url("https://a/b?x=1&sig=abc&api_key=k") == "https://a/b?x=1&sig=REDACTED&api_key=REDACTED"
    assert redact_url("https://a/b?token=dG9rZW4") == "https://a/b?token=dG9rZW4"
