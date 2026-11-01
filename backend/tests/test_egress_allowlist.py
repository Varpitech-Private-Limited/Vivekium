"""Egress control for candidate-supplied and third-party URLs (W9.3).

The hole this closes is concrete: `services/projects/repository.py` fetches
public repositories from CANDIDATE SUPPLIED URLs, from a task that also holds
tenant data. Every test here is written against that shape rather than against
an abstract SSRF: an attacker who controls a URL, a DNS answer, or a redirect
header, and a service that must not connect to the result.

DNS IS ALWAYS STUBBED. A test that resolved a real name would be a test that
passes or fails on somebody's resolver, and the rebinding case cannot be
expressed at all without controlling the answer.
"""
from __future__ import annotations

import re

import httpx
import pytest

from app.services import egress
from app.services.projects import repository
from app.services.projects.limits import ProjectLimits

PUBLIC = "140.82.121.4"
PRIVATE = "10.0.0.7"
METADATA_V4 = "169.254.169.254"
LOOPBACK = "127.0.0.1"


@pytest.fixture(autouse=True)
def _no_real_dns(monkeypatch):
    """Default every test to a public answer, so a test that cares about DNS
    says so by overriding it rather than by depending on the network."""
    monkeypatch.setattr(egress, "_resolve", lambda host: [PUBLIC])


def resolving_to(monkeypatch, *addresses: str) -> None:
    monkeypatch.setattr(egress, "_resolve", lambda host: list(addresses))


# ── Address classification ───────────────────────────────────────────────────


@pytest.mark.parametrize(
    "address,expected",
    [
        (LOOPBACK, "loopback"),
        ("::1", "loopback"),
        (PRIVATE, "private"),
        ("192.168.1.5", "private"),
        ("172.16.4.4", "private"),
        (METADATA_V4, "link-local"),
        ("fe80::1", "link-local"),
        # The AWS IMDSv6 endpoint. Unique-local, so the private branch catches
        # it, and the point of the case is that it IS caught.
        ("fd00:ec2::254", "private"),
        # The metadata endpoint wearing an IPv6 hat. A check that only parsed
        # the string would see a global v6 address.
        ("::ffff:169.254.169.254", "link-local"),
        ("0.0.0.0", "unspecified"),
        ("224.0.0.1", "multicast"),
    ],
)
def test_non_public_addresses_are_refused_by_name(address: str, expected: str) -> None:
    refusal = egress.address_refusal(address)
    assert refusal is not None
    assert expected in refusal


@pytest.mark.parametrize("address", [PUBLIC, "8.8.8.8", "2606:4700:4700::1111"])
def test_public_addresses_are_permitted(address: str) -> None:
    assert egress.address_refusal(address) is None


# ── URL validation ───────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "url",
    [
        "http://api.github.com/repos/owner/name",
        "https://evil.example/repos/owner/name",
        "https://api.github.com.evil.example/repos",
        "https://user:token@api.github.com/repos",
        "https://api.github.com:8443/repos",
        f"https://{METADATA_V4}/latest/meta-data/",
        f"https://{LOOPBACK}:443/admin",
        "file:///etc/passwd",
        "",
    ],
)
def test_a_url_outside_the_allowlist_is_refused(url: str) -> None:
    with pytest.raises(egress.EgressRefused):
        egress.check_url(url)


def test_an_allowlisted_url_is_accepted() -> None:
    assert egress.check_url("https://api.github.com/repos/a/b") == "api.github.com"


def test_the_allowlist_is_exact_and_never_a_suffix_rule() -> None:
    """A suffix rule is how `github.com.attacker.example` gets through, and it
    is also how a subdomain takeover becomes an egress path."""
    assert egress.is_allowed_host("api.github.com") is True
    assert egress.is_allowed_host("evil.api.github.com") is False
    assert egress.is_allowed_host("api.github.com.evil.example") is False


def test_every_allowlist_entry_states_why_it_is_there() -> None:
    assert egress.ALLOWED_HOSTS
    for host, reason in egress.ALLOWED_HOSTS.items():
        assert host == host.lower().strip()
        assert len(reason) > 30


def test_every_host_the_repository_fetcher_reaches_is_on_the_allowlist() -> None:
    """The structural version of the check. A fetcher pointed at a new host
    without an allowlist entry fails here rather than at runtime, and an
    allowlist entry nothing reaches is equally visible."""
    with open(repository.__file__, "r", encoding="utf-8") as handle:
        body = handle.read()
    hosts = set(re.findall(r'https://([a-z0-9.\-]+)/', body))
    # `github.com` appears in candidate-facing help text and in the normalised
    # ref URL, which is stored and never fetched.
    hosts -= {"github.com"}
    assert hosts, "the fetcher should name at least one host"
    for host in hosts:
        assert egress.is_allowed_host(host), host


# ── DNS rebinding ────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_a_hostname_resolving_to_a_private_address_is_refused(monkeypatch) -> None:
    """The allowlisted name is real; the answer is not. Checking the hostname
    alone is checking a string the attacker chose."""
    resolving_to(monkeypatch, PRIVATE)
    with pytest.raises(egress.EgressRefused) as excinfo:
        await egress.check_host_resolves_publicly("api.github.com")
    assert "private" in excinfo.value.reason


@pytest.mark.asyncio
async def test_a_hostname_resolving_to_the_metadata_endpoint_is_refused(
    monkeypatch,
) -> None:
    resolving_to(monkeypatch, METADATA_V4)
    with pytest.raises(egress.EgressRefused) as excinfo:
        await egress.check_host_resolves_publicly("api.github.com")
    assert "metadata" in excinfo.value.reason


@pytest.mark.asyncio
async def test_every_resolved_address_is_checked_not_only_the_first(
    monkeypatch,
) -> None:
    """A rebinding attack answers with one public address and one internal one
    and lets the connection pick. A check reading `addresses[0]` passes it half
    the time, which is worse than not checking, because it looks like it works."""
    resolving_to(monkeypatch, PUBLIC, PRIVATE)
    with pytest.raises(egress.EgressRefused):
        await egress.check_host_resolves_publicly("api.github.com")


@pytest.mark.asyncio
async def test_a_name_that_does_not_resolve_is_refused_rather_than_attempted(
    monkeypatch,
) -> None:
    resolving_to(monkeypatch)
    with pytest.raises(egress.EgressRefused):
        await egress.check_host_resolves_publicly("api.github.com")


# ── The request path ─────────────────────────────────────────────────────────


def client_over(handler) -> httpx.AsyncClient:
    """A client with the module's own redirect policy and a stubbed transport."""
    return httpx.AsyncClient(
        transport=httpx.MockTransport(handler), follow_redirects=False
    )


@pytest.mark.asyncio
async def test_a_redirect_to_a_host_off_the_allowlist_is_refused() -> None:
    """An allowlist that checks only the first URL is checked exactly once and
    then hands the attacker the rest of the chain."""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "api.github.com":
            return httpx.Response(
                302, headers={"location": "https://evil.example/exfiltrate"}
            )
        return httpx.Response(200, content=b"reached")

    async with client_over(handler) as client:
        with pytest.raises(egress.EgressRefused) as excinfo:
            await egress.get(client, "https://api.github.com/repos/a/b")
    assert "allowlist" in excinfo.value.reason


@pytest.mark.asyncio
async def test_a_redirect_to_the_metadata_endpoint_is_refused() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            302, headers={"location": f"https://{METADATA_V4}/latest/meta-data/"}
        )

    async with client_over(handler) as client:
        with pytest.raises(egress.EgressRefused):
            await egress.get(client, "https://api.github.com/repos/a/b")


@pytest.mark.asyncio
async def test_a_redirect_within_the_allowlist_is_followed() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "api.github.com":
            return httpx.Response(
                302, headers={"location": "https://raw.githubusercontent.com/a/b"}
            )
        return httpx.Response(200, content=b"file body")

    async with client_over(handler) as client:
        response = await egress.get(client, "https://api.github.com/repos/a/b")
    assert response.status_code == 200
    assert response.content == b"file body"


@pytest.mark.asyncio
async def test_a_redirect_loop_is_bounded() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            302, headers={"location": "https://api.github.com/again"}
        )

    async with client_over(handler) as client:
        with pytest.raises(egress.EgressRefused) as excinfo:
            await egress.get(client, "https://api.github.com/repos/a/b")
    assert "redirected more times" in excinfo.value.reason


@pytest.mark.asyncio
async def test_a_redirect_naming_no_destination_is_refused() -> None:
    async with client_over(lambda request: httpx.Response(302)) as client:
        with pytest.raises(egress.EgressRefused):
            await egress.get(client, "https://api.github.com/repos/a/b")


@pytest.mark.asyncio
async def test_an_oversized_response_is_refused_rather_than_truncated() -> None:
    """Truncating would hand the caller a partial file it cannot tell from a
    complete one, and evidence would be built from an absence we caused."""
    limits = egress.EgressLimits(
        max_response_bytes=16,
        max_redirects=1,
        connect_timeout_seconds=1.0,
        read_timeout_seconds=1.0,
    )

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"x" * 1024)

    async with client_over(handler) as client:
        with pytest.raises(egress.EgressRefused):
            await egress.get(client, "https://api.github.com/repos/a/b", limits=limits)


@pytest.mark.asyncio
async def test_a_declared_oversize_response_is_refused_before_it_is_read() -> None:
    limits = egress.EgressLimits(
        max_response_bytes=16,
        max_redirects=1,
        connect_timeout_seconds=1.0,
        read_timeout_seconds=1.0,
    )

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, headers={"content-length": "999999"}, content=b"x" * 999999
        )

    async with client_over(handler) as client:
        with pytest.raises(egress.EgressRefused) as excinfo:
            await egress.get(client, "https://api.github.com/repos/a/b", limits=limits)
    assert "declares more content" in excinfo.value.reason


@pytest.mark.asyncio
async def test_a_permitted_response_keeps_the_httpx_contract() -> None:
    """The caller's `.status_code`, `.json()` and `.content` behave exactly as
    they did before this module existed, so wiring it in changed no branch in
    `repository.py`."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"default_branch": "main"})

    async with client_over(handler) as client:
        response = await egress.get(client, "https://api.github.com/repos/a/b")
    assert response.status_code == 200
    assert response.json()["default_branch"] == "main"


@pytest.mark.asyncio
async def test_a_gzip_response_is_decoded_once_not_twice() -> None:
    """GitHub always gzips. The body handed back is already decoded, so the
    encoding headers must not travel with it, or httpx decodes it again and
    every repository fetch fails with a zlib error."""
    import gzip

    payload = b'{"default_branch": "main"}'

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"content-encoding": "gzip", "content-type": "application/json"},
            content=gzip.compress(payload),
        )

    async with client_over(handler) as client:
        response = await egress.get(client, "https://api.github.com/repos/a/b")
    assert response.content == payload
    assert response.json()["default_branch"] == "main"
    assert "content-encoding" not in response.headers


@pytest.mark.asyncio
async def test_the_default_client_cannot_follow_a_redirect_by_itself() -> None:
    """If it could, the per-hop allowlist check would be unreachable."""
    async with egress.build_client() as client:
        assert client.follow_redirects is False


# ── The wiring into the candidate-supplied fetch path ────────────────────────


def small_limits() -> ProjectLimits:
    return ProjectLimits(
        max_projects_per_candidate=10,
        max_files=20,
        max_file_bytes=5 * 1024 * 1024,
        max_total_bytes=20 * 1024 * 1024,
        max_archive_depth=1,
        max_archive_entries=50,
        max_extracted_bytes=20 * 1024 * 1024,
        max_compression_ratio=120,
        max_text_chars_per_file=20_000,
        max_evidence_units=40,
        max_ai_context_chars=8_000,
        repo_max_files=10,
        repo_max_file_bytes=100_000,
    )


@pytest.mark.asyncio
async def test_an_egress_refusal_becomes_a_terminal_repository_rejection(
    monkeypatch,
) -> None:
    """Not `RepositoryUnavailable`. Waiting will not make a link-local address
    acceptable, and reporting it as an outage would retry an attack on a
    schedule."""
    resolving_to(monkeypatch, METADATA_V4)
    ref = repository.validate_repository_url("https://github.com/owner/project")
    with pytest.raises(repository.RepositoryRejected):
        await repository.fetch_repository(ref, small_limits())


@pytest.mark.asyncio
async def test_the_repository_fetcher_uses_the_guarded_client(monkeypatch) -> None:
    """The check that the wiring is real rather than the module merely existing:
    every outbound call in the fetch path passes through `egress.get`."""
    seen: list[str] = []

    async def spy(client, url, *, limits=egress.LIMITS):
        seen.append(url)
        raise egress.EgressRefused("stopped after the first guarded call")

    monkeypatch.setattr(egress, "get", spy)
    monkeypatch.setattr(repository.egress, "get", spy)
    ref = repository.validate_repository_url("https://github.com/owner/project")
    with pytest.raises(repository.RepositoryRejected):
        await repository.fetch_repository(ref, small_limits())
    assert seen == ["https://api.github.com/repos/owner/project"]
