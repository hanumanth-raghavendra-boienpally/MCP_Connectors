"""Unit tests for the cluster-URL checks (no server needed, no network needed)."""

import asyncio

import pytest
from fastmcp.exceptions import ToolError

from connector_core.url_guard import validate_base_url


def check(url: str) -> str:
    return asyncio.run(validate_base_url(url))


@pytest.fixture(autouse=True)
def no_allowlist(monkeypatch):
    monkeypatch.delenv("ES_ALLOWED_HOSTS", raising=False)


@pytest.mark.parametrize(
    "url",
    [
        "https://169.254.169.254",      # cloud metadata
        "https://127.0.0.1:9200",       # loopback
        "https://[::1]:9200",           # IPv6 loopback
        "https://[::ffff:127.0.0.1]",   # IPv4 loopback hidden inside IPv6
        "https://10.0.0.5:9200",        # private network
        "https://192.168.1.10",
    ],
)
def test_internal_addresses_are_refused(url):
    with pytest.raises(ToolError, match="not allowed"):
        check(url)


def test_public_address_over_https_is_accepted():
    assert check("https://8.8.8.8:9243/") == "https://8.8.8.8:9243"


def test_plain_http_is_refused_without_an_allowlist():
    with pytest.raises(ToolError, match="https"):
        check("http://8.8.8.8:9200")


@pytest.mark.parametrize(
    "url",
    [
        "ftp://8.8.8.8",
        "8.8.8.8:9200",
        "https://user:pass@8.8.8.8",
        "https://8.8.8.8?x=1",
        "https://8.8.8.8#frag",
        "https://",
        "https://8.8.8.8:notaport",
    ],
)
def test_malformed_or_risky_urls_are_refused(url):
    with pytest.raises(ToolError):
        check(url)


def test_allowlist_exact_host(monkeypatch):
    monkeypatch.setenv("ES_ALLOWED_HOSTS", "es.example.com")
    assert check("https://es.example.com:9243") == "https://es.example.com:9243"
    with pytest.raises(ToolError, match="not allowed"):
        check("https://other.example.com")


def test_allowlist_wildcard_does_not_match_lookalikes(monkeypatch):
    monkeypatch.setenv("ES_ALLOWED_HOSTS", "*.es.cloud.io")
    assert check("https://abc.es.cloud.io")
    for bad in ["https://es.cloud.io", "https://abc.es.cloud.io.evil.com", "https://evilxes.cloud.io"]:
        with pytest.raises(ToolError, match="not allowed"):
            check(bad)


def test_allowlisted_host_may_use_http_and_keeps_its_path(monkeypatch):
    monkeypatch.setenv("ES_ALLOWED_HOSTS", "localhost")
    assert check("http://localhost:9200/") == "http://localhost:9200"
    assert check("http://localhost:9200/es/") == "http://localhost:9200/es"
