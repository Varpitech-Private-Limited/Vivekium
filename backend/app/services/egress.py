"""Outbound egress control for candidate-supplied and third-party URLs
(RPN-AI-UP-001 W9.3).

THE CONCRETE HOLE THIS CLOSES
-------------------------------
`services/projects/repository.py` fetches public repositories from CANDIDATE
SUPPLIED URLs, from a task that also holds tenant data. That is a server side
request forgery primitive and an exfiltration primitive in one object.
`validate_repository_url` already refuses embedded credentials and an
unsupported host, and the tree is classified before a byte of content is
fetched, but a validator is IN BAND: it runs in the same process, on the same
string, before a redirect chain that it never sees.

WHAT IS ENFORCED HERE, AND WHY EACH ONE IS NOT REDUNDANT
----------------------------------------------------------
* **A host allowlist.** Not a denylist. The set of hosts this product has any
  business reaching is small, closed and boring, and every entry carries the
  reason it is there.
* **No redirect to a host outside the allowlist.** `follow_redirects` is False
  on the client, and each hop is re-validated from scratch. An allowlist that
  only checks the first URL is checked exactly once and then hands the
  attacker the rest of the chain: `https://github.com/...` answering 302 to
  `http://169.254.169.254/latest/meta-data/` is one header.
* **Refusal of internal, loopback, link local and metadata addresses, by
  RESOLVED ADDRESS.** Checking the hostname alone is checking a string an
  attacker chose. The metadata endpoint is reachable by name as easily as by
  number, and DNS rebinding makes a public looking name resolve to
  169.254.169.254 on the hop that matters. Every address the resolver returns
  is checked, not just the first: a name that resolves to one public address
  and one private one is a rebinding attack that a first-answer check passes.
* **A bounded response size.** An unbounded read of a hostile endpoint is a
  memory exhaustion primitive that needs no exploit at all.

THE RESIDUAL WINDOW, STATED RATHER THAN PAPERED OVER
------------------------------------------------------
Resolving a name and then asking `httpx` to connect to that name is a
time-of-check-to-time-of-use gap: the second lookup can answer differently.
Closing it in Python means dialling the checked IP with the hostname carried in
SNI and the Host header, which replaces the transport for every caller. It is
not closed here. **The network layer is the real boundary**, and the Terraform
change that makes it one is recorded with this workstream: the data subnets
already have no route to the internet in either direction, and the Fargate and
Lambda task egress gets the same treatment through a VPC endpoint plus an
egress security group that permits 443 to the allowlisted prefixes only. This
module is defence in depth on top of that, in the sense W9.1 uses the phrase:
never the boundary.

WHY THE ALLOWLIST IS SHORT
----------------------------
It lists exactly the hosts reached through THIS module. The model, embedding,
payment and messaging vendors are not here, because their call sites do not go
through this module and an entry naming a host nothing checks would be a
statement about coverage that is not true. The network-layer list is broader
and lives in the infrastructure, which is where the two are reconciled.
"""
from __future__ import annotations

import ipaddress
import logging
import socket
from dataclasses import dataclass
from urllib.parse import urljoin, urlparse

import httpx
from starlette.concurrency import run_in_threadpool

logger = logging.getLogger(__name__)


class EgressRefused(RuntimeError):
    """An outbound request was refused. `reason` is safe to show a candidate."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


#: host -> why this product reaches it. DATA, so adding a provider is a row
#: with a stated reason rather than a condition somebody buried in a fetcher.
ALLOWED_HOSTS: dict[str, str] = {
    "api.github.com": (
        "Public repository metadata and tree listing for Project Evidence "
        "Intelligence."
    ),
    "raw.githubusercontent.com": (
        "Public repository file content for Project Evidence Intelligence."
    ),
    "api.tavily.com": (
        "The AI Reach and Company Research web search provider. Reached through "
        "the vendor's own client, so the allowlist entry is what the network "
        "layer enforces rather than what this module checks."
    ),
}

#: The only scheme and port this product egresses on. `http` is absent
#: deliberately: a plaintext hop is a hop an on-path attacker rewrites, and
#: every allowlisted host serves https.
_SCHEME = "https"
_PORT = 443


@dataclass(frozen=True)
class EgressLimits:
    """Ceilings for one outbound request.

    PROVENANCE. `core/config.py` is owned by another workstream in this release
    and the `egress_*` settings are reported with this change rather than added
    here, so `LIMITS` is the single statement of each value. Every function
    below takes the dataclass, so wiring `from_settings()` later changes no call
    site.
    """

    #: Bytes read from one response before it is refused. Above the largest
    #: single file `project_repo_max_file_bytes` allows, so the transport limit
    #: never silently pre-empts the product limit and turn a "file too large"
    #: limitation into a security refusal.
    max_response_bytes: int
    #: Redirect hops followed. Each one is re-validated in full.
    max_redirects: int
    connect_timeout_seconds: float
    read_timeout_seconds: float


LIMITS = EgressLimits(
    max_response_bytes=8 * 1024 * 1024,
    max_redirects=3,
    connect_timeout_seconds=5.0,
    read_timeout_seconds=25.0,
)


# ── Host and address checks ──────────────────────────────────────────────────


def is_allowed_host(host: str) -> bool:
    """Exact-match only. No suffix matching, ever.

    A suffix rule is how `github.com.attacker.example` gets through, and it is
    also how a subdomain takeover becomes an egress path. Every host this
    product reaches is spelled out.
    """
    return (host or "").strip().lower() in ALLOWED_HOSTS


def address_refusal(address: str) -> str | None:
    """Name why an IP address may not be reached, or None if it may.

    The umbrella test is `is_global`, which is the property that actually
    matters: reachable on the public internet. The specific names come first
    because the message a reviewer reads should say which class fired.
    """
    try:
        ip = ipaddress.ip_address(address)
    except ValueError:
        return "the address could not be parsed"
    mapped = getattr(ip, "ipv4_mapped", None)
    if mapped is not None:
        # `::ffff:169.254.169.254` is the metadata endpoint wearing a v6 hat.
        return address_refusal(str(mapped))
    if ip.is_unspecified:
        return "the unspecified address"
    if ip.is_loopback:
        return "a loopback address"
    if ip.is_link_local:
        # 169.254.0.0/16 and fe80::/10. This is the cloud metadata endpoint's
        # range on every provider this product could be deployed to.
        return "a link-local address, which is where cloud metadata lives"
    if ip.is_private:
        # Also covers unique-local fc00::/7, so the IMDSv6 address
        # fd00:ec2::254 lands here.
        return "a private address"
    if ip.is_multicast:
        return "a multicast address"
    if ip.is_reserved:
        return "a reserved address"
    if not ip.is_global:
        return "an address that is not routable on the public internet"
    return None


def _resolve(host: str) -> list[str]:
    """Every address the resolver offers for this host, as strings."""
    try:
        results = socket.getaddrinfo(
            host, _PORT, proto=socket.IPPROTO_TCP, type=socket.SOCK_STREAM
        )
    except socket.gaierror as exc:
        raise EgressRefused(
            f"The host {host} could not be resolved."
        ) from exc
    return [str(entry[4][0]) for entry in results]


async def check_host_resolves_publicly(host: str) -> tuple[str, ...]:
    """Resolve `host` and refuse if ANY answer is not publicly routable.

    ANY, not the first. A rebinding attack answers with one public address and
    one internal one and lets the connection pick; a check that reads
    `addresses[0]` passes it half the time, which is worse than not checking,
    because it looks like it works.
    """
    addresses = await run_in_threadpool(_resolve, host)
    if not addresses:
        raise EgressRefused(f"The host {host} could not be resolved.")
    for address in addresses:
        refusal = address_refusal(address)
        if refusal is not None:
            raise EgressRefused(
                f"The host {host} resolves to {refusal}, which this service "
                "will not connect to."
            )
    return tuple(addresses)


def check_url(url: str) -> str:
    """Validate one absolute URL and return its host. Raises `EgressRefused`.

    No DNS here: this is the cheap string half, and it runs on every redirect
    hop before anything is resolved or connected.
    """
    raw = (url or "").strip()
    if not raw:
        raise EgressRefused("No URL was provided.")
    parsed = urlparse(raw)
    if parsed.scheme != _SCHEME:
        raise EgressRefused("Only https requests are permitted.")
    if parsed.username or parsed.password:
        raise EgressRefused("A URL carrying credentials is refused.")
    host = (parsed.hostname or "").lower()
    if not host:
        raise EgressRefused("The URL names no host.")
    if parsed.port is not None and parsed.port != _PORT:
        raise EgressRefused("Only the standard https port is permitted.")
    # A bare IP address never matches the allowlist, but naming the reason is
    # what stops the next reader from adding one "just for testing".
    refusal_if_literal = _literal_address_refusal(host)
    if refusal_if_literal is not None:
        raise EgressRefused(refusal_if_literal)
    if not is_allowed_host(host):
        raise EgressRefused(
            f"The host {host} is not on this service's outbound allowlist."
        )
    return host


def _literal_address_refusal(host: str) -> str | None:
    stripped = host.strip("[]")
    try:
        ipaddress.ip_address(stripped)
    except ValueError:
        return None
    return (
        "A URL that names an IP address directly is refused; only named hosts "
        "on the outbound allowlist are permitted."
    )


# ── The request path ─────────────────────────────────────────────────────────


def build_client(
    *, headers: dict[str, str] | None = None, limits: EgressLimits = LIMITS
) -> httpx.AsyncClient:
    """An `httpx.AsyncClient` that cannot follow a redirect on its own.

    `follow_redirects=False` is the whole point: the allowlist is re-applied per
    hop by `request` below, and a client that followed redirects itself would
    make that check unreachable.
    """
    return httpx.AsyncClient(
        headers=headers or {},
        follow_redirects=False,
        timeout=httpx.Timeout(
            limits.read_timeout_seconds, connect=limits.connect_timeout_seconds
        ),
    )


async def _read_bounded(
    response: httpx.Response, url: str, limits: EgressLimits
) -> bytes:
    declared = response.headers.get("content-length")
    if declared is not None and declared.isdigit():
        if int(declared) > limits.max_response_bytes:
            raise EgressRefused(
                "The remote response declares more content than this service "
                "will read."
            )
    chunks: list[bytes] = []
    total = 0
    async for chunk in response.aiter_bytes():
        total += len(chunk)
        if total > limits.max_response_bytes:
            raise EgressRefused(
                f"The response from {url} exceeded the outbound read limit."
            )
        chunks.append(chunk)
    return b"".join(chunks)


async def request(
    client: httpx.AsyncClient,
    method: str,
    url: str,
    *,
    limits: EgressLimits = LIMITS,
) -> httpx.Response:
    """Perform one guarded outbound request and return the final response.

    Every hop, including the first, passes `check_url` and
    `check_host_resolves_publicly` before a connection is opened. The body is
    read under `max_response_bytes` and returned on a fresh `Response`, so the
    caller's `.status_code`, `.json()` and `.content` behave exactly as they did
    before this module existed.

    A refusal RAISES. It is not a degraded result and must not read as one: a
    caller that treated "we refused to fetch this" as "the file was empty" would
    build evidence from an absence it caused itself.
    """
    target = url
    for _hop in range(limits.max_redirects + 1):
        host = check_url(target)
        await check_host_resolves_publicly(host)
        async with client.stream(method, target) as response:
            if response.is_redirect:
                location = response.headers.get("location", "")
                if not location:
                    raise EgressRefused(
                        "The remote host answered with a redirect naming no "
                        "destination."
                    )
                # Resolved against the CURRENT url so a relative Location is
                # handled, then re-validated from scratch on the next pass.
                target = urljoin(target, location)
                logger.info(
                    "egress.redirect from=%s to_host=%s",
                    host,
                    (urlparse(target).hostname or "").lower(),
                )
                continue
            body = await _read_bounded(response, target, limits)
            # `_read_bounded` returns the already decoded body, so the encoding
            # headers must not travel with it: httpx would decode it twice
            # (every gzip response from GitHub failed with a zlib error).
            decoded_headers = [
                (k, v)
                for k, v in response.headers.multi_items()
                if k.lower() not in {"content-encoding", "content-length", "transfer-encoding"}
            ]
            return httpx.Response(
                status_code=response.status_code,
                headers=decoded_headers,
                content=body,
                request=response.request,
            )
    raise EgressRefused("The remote host redirected more times than permitted.")


async def get(
    client: httpx.AsyncClient, url: str, *, limits: EgressLimits = LIMITS
) -> httpx.Response:
    """`request` with the method this product actually uses."""
    return await request(client, "GET", url, limits=limits)
