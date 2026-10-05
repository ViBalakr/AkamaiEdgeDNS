"""
EdgeGrid (EG1-HMAC-SHA256) authentication — Akamai spec compliant.

Signing string is 8 tab-separated fields, in this exact order:

    method          GET | POST | ...
    scheme          http | https
    host            akab-xxxx.luna.akamaiapis.net   (no scheme, no path)
    relative_url    /event-viewer-api/v1/events     (path only)
    canonical_query k1=v1&k2=v2                     (sorted, url-encoded, "" if none)
    canonical_hdrs  content-type:...                ("" if none)
    content_hash    base64(sha256(body))            (base64(sha256("")) for GET)
    auth_header     EG1-HMAC-SHA256 client_token=..;access_token=..;timestamp=..;nonce=..;

The final string has a trailing tab, then the whole thing is HMAC'd.
signing_key = base64(HMAC-SHA256(client_secret, timestamp))
signature   = base64(HMAC-SHA256(signing_key, signing_string))
"""

import base64
import hashlib
import hmac
import secrets
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional
from urllib.parse import urlsplit, parse_qsl, quote

ALGORITHM = "EG1-HMAC-SHA256"
TIMESTAMP_FORMAT = "%Y%m%dT%H:%M:%S+0000"


class EdgeGridAuthenticationError(Exception):
    pass


@dataclass(frozen=True)
class EdgeGridCredentials:
    client_token: str
    access_token: str
    client_secret: str


def generate_timestamp(now: Optional[datetime] = None) -> str:
    now = now or datetime.now(timezone.utc)
    return now.strftime(TIMESTAMP_FORMAT)


def generate_nonce() -> str:
    return secrets.token_hex(16)


# ----------------------------------------------------------------------
# Canonicalization helpers (used by BOTH signer and validator)
# ----------------------------------------------------------------------
def _canonical_query(raw_query: str) -> str:
    """
    Akamai canonical query: sort by key, then by value; URL-encode both;
    join with '&'.  Empty string if there is no query.
    """
    if not raw_query:
        return ""
    pairs = parse_qsl(raw_query, keep_blank_values=True)
    pairs.sort()
    return "&".join(f"{quote(k, safe='')}={quote(v, safe='')}" for k, v in pairs)


def _canonical_headers(headers: dict) -> str:
    """
    Akamai signs only a whitelist of headers. For our GET-with-no-body
    flow, the canonical headers string is empty. If you ever POST with a
    Content-Type, add it here on both signer and validator sides.
    """
    return ""


def _content_hash(body: bytes) -> str:
    """base64(sha256(body)). For GET / no body → base64(sha256(b''))."""
    return base64.b64encode(hashlib.sha256(body or b"").digest()).decode("ascii")


def _make_signing_key(client_secret: str, timestamp: str) -> bytes:
    digest = hmac.new(
        client_secret.encode("utf-8"),
        timestamp.encode("utf-8"),
        hashlib.sha256,
    ).digest()
    return base64.b64encode(digest)


def _build_signing_string(method: str,
                          scheme: str,
                          host: str,
                          relative_url: str,
                          canonical_query: str,
                          canonical_headers: str,
                          content_hash: str,
                          auth_header_stub: str) -> str:
    """
    Returns the exact 8-field tab-joined string with a trailing tab.
    """
    fields = [
        method.upper(),
        scheme,
        host,
        relative_url,
        canonical_query,
        canonical_headers,
        content_hash,
        auth_header_stub,
    ]
    return "\t".join(fields) + "\t"


def _auth_stub(client_token: str, access_token: str,
               timestamp: str, nonce: str) -> str:
    return (
        f"{ALGORITHM} client_token={client_token};"
        f"access_token={access_token};"
        f"timestamp={timestamp};"
        f"nonce={nonce};"
    )


# ----------------------------------------------------------------------
# Client-side: sign an outgoing request
# ----------------------------------------------------------------------
def sign_request(method: str,
                 url: str,
                 credentials: EdgeGridCredentials,
                 body: bytes = b"",
                 timestamp: Optional[str] = None,
                 nonce: Optional[str] = None) -> str:
    parsed = urlsplit(url)
    scheme = parsed.scheme or "https"
    host = parsed.netloc
    relative_url = parsed.path or "/"
    canonical_query = _canonical_query(parsed.query)

    ts = timestamp or generate_timestamp()
    n = nonce or generate_nonce()

    auth_stub = _auth_stub(credentials.client_token,
                           credentials.access_token,
                           ts, n)

    signing_string = _build_signing_string(
        method=method,
        scheme=scheme,
        host=host,
        relative_url=relative_url,
        canonical_query=canonical_query,
        canonical_headers=_canonical_headers({}),
        content_hash=_content_hash(body),
        auth_header_stub=auth_stub,
    )

    signing_key = _make_signing_key(credentials.client_secret, ts)
    signature = base64.b64encode(
        hmac.new(signing_key, signing_string.encode("utf-8"), hashlib.sha256).digest()
    ).decode("ascii")

    return auth_stub + f"signature={signature}"


# ----------------------------------------------------------------------
# Server-side: parse and validate an inbound request
# ----------------------------------------------------------------------
@dataclass(frozen=True)
class ParsedAuthHeader:
    algorithm: str
    client_token: str
    access_token: str
    timestamp: str
    nonce: str
    signature: str


def parse_auth_header(header_value: str) -> ParsedAuthHeader:
    if not header_value:
        raise EdgeGridAuthenticationError("Missing Authorization header")

    try:
        algorithm, rest = header_value.split(" ", 1)
    except ValueError:
        raise EdgeGridAuthenticationError("Malformed Authorization header")

    if algorithm != ALGORITHM:
        raise EdgeGridAuthenticationError("Unsupported authentication algorithm")

    fields = {}
    for chunk in rest.split(";"):
        chunk = chunk.strip()
        if not chunk or "=" not in chunk:
            continue
        key, _, value = chunk.partition("=")
        fields[key.strip()] = value.strip()

    required = ("client_token", "access_token", "timestamp", "nonce", "signature")
    missing = [f for f in required if not fields.get(f)]
    if missing:
        raise EdgeGridAuthenticationError(f"Authorization header missing fields: {missing}")

    return ParsedAuthHeader(
        algorithm=algorithm,
        client_token=fields["client_token"],
        access_token=fields["access_token"],
        timestamp=fields["timestamp"],
        nonce=fields["nonce"],
        signature=fields["signature"],
    )


def validate_timestamp(timestamp: str,
                       max_skew_seconds: int = 300,
                       now: Optional[datetime] = None) -> None:
    try:
        parsed = datetime.strptime(timestamp, TIMESTAMP_FORMAT).replace(tzinfo=timezone.utc)
    except ValueError:
        raise EdgeGridAuthenticationError("Malformed timestamp")
    current = now or datetime.now(timezone.utc)
    if abs((current - parsed).total_seconds()) > max_skew_seconds:
        raise EdgeGridAuthenticationError("Timestamp outside allowed clock-skew window")


def compute_expected_signature(method: str,
                                scheme: str,
                                host: str,
                                relative_url: str,
                                raw_query: str,
                                credentials: EdgeGridCredentials,
                                parsed_header: ParsedAuthHeader,
                                body: bytes = b"") -> str:
    """
    Server-side recomputation. Caller MUST pass method/scheme/host/path/query
    as observed on the inbound request (never as claimed by the client), and
    must pass the SAME body bytes the client signed.
    """
    auth_stub = _auth_stub(parsed_header.client_token,
                           parsed_header.access_token,
                           parsed_header.timestamp,
                           parsed_header.nonce)

    signing_string = _build_signing_string(
        method=method,
        scheme=scheme,
        host=host,
        relative_url=relative_url,
        canonical_query=_canonical_query(raw_query),
        canonical_headers=_canonical_headers({}),
        content_hash=_content_hash(body),
        auth_header_stub=auth_stub,
    )

    signing_key = _make_signing_key(credentials.client_secret, parsed_header.timestamp)
    return base64.b64encode(
        hmac.new(signing_key, signing_string.encode("utf-8"), hashlib.sha256).digest()
    ).decode("ascii")


def constant_time_compare(a: str, b: str) -> bool:
    return hmac.compare_digest(a.encode("utf-8"), b.encode("utf-8"))
