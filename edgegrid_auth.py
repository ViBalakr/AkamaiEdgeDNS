"""
EdgeGrid (EG1-HMAC-SHA256) authentication implementation.

This module implements Akamai's OPEN EdgeGrid request-signing algorithm
well enough to (a) let the local collector sign outbound requests, and
(b) let the mock Akamai server independently recompute and verify those
signatures without trusting the caller. It is used ONLY with fake LAB
credentials (LAB_CLIENT_TOKEN / LAB_ACCESS_TOKEN / LAB_CLIENT_SECRET) —
see README.md. Never point this module at real Akamai credentials.

Algorithm (EG1-HMAC-SHA256):

    signing_key  = base64( HMAC-SHA256(secret=client_secret, msg=timestamp) )
    data_to_sign = "\t".join([method, scheme, host, path_and_query,
                              canonicalized_headers, content_hash]) + "\t"
    auth_stub    = "EG1-HMAC-SHA256 client_token=...;access_token=...;
                    timestamp=...;nonce=...;"
    signature    = base64( HMAC-SHA256(secret=signing_key,
                                        msg=data_to_sign + auth_stub) )

Both the signer (akamai_client.py, via sign_request) and the validator
(mock_akamai.py, via compute_expected_signature) call the same private
helpers below, so a correct implementation on one side is guaranteed to
be consistent with the other. The mock server never trusts the client's
signature — it always recomputes it from the actual inbound request.

SECURITY NOTE: do not log client_secret, access_token, nonce+signature
pairs, or the full Authorization header anywhere in this module or its
callers. Constant-time comparison is used for signature validation.
"""

import base64
import hashlib
import hmac
import secrets
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional
from urllib.parse import urlsplit

ALGORITHM = "EG1-HMAC-SHA256"
TIMESTAMP_FORMAT = "%Y%m%dT%H:%M:%S+0000"


class EdgeGridAuthenticationError(Exception):
    """Raised when EdgeGrid signing or validation cannot proceed / fails."""


@dataclass(frozen=True)
class EdgeGridCredentials:
    client_token: str
    access_token: str
    client_secret: str


def generate_timestamp(now: Optional[datetime] = None) -> str:
    now = now or datetime.now(timezone.utc)
    return now.strftime(TIMESTAMP_FORMAT)


def generate_nonce() -> str:
    """Cryptographically secure nonce (128 bits of randomness, hex-encoded)."""
    return secrets.token_hex(16)


def _make_signing_key(client_secret: str, timestamp: str) -> bytes:
    digest = hmac.new(
        client_secret.encode("utf-8"), timestamp.encode("utf-8"), hashlib.sha256
    ).digest()
    return base64.b64encode(digest)


def _canonical_path_and_query(path_and_query: str) -> str:
    # An empty query string canonicalizes by dropping the trailing '?'.
    if path_and_query.endswith("?"):
        return path_and_query[:-1]
    return path_and_query


def _make_data_to_sign(method: str, scheme: str, host: str, path_and_query: str,
                        content_hash: str = "") -> str:
    parts = [
        method.upper(),
        scheme,
        host,
        _canonical_path_and_query(path_and_query),
        "",             # canonicalized headers-to-sign (none used in this lab)
        content_hash,   # empty for GET / no request body
    ]
    return "\t".join(parts) + "\t"


def _auth_header_without_signature(client_token: str, access_token: str,
                                    timestamp: str, nonce: str) -> str:
    return (
        f"{ALGORITHM} client_token={client_token};"
        f"access_token={access_token};"
        f"timestamp={timestamp};"
        f"nonce={nonce};"
    )


def sign_request(method: str, url: str, credentials: EdgeGridCredentials,
                  timestamp: Optional[str] = None, nonce: Optional[str] = None) -> str:
    """
    Build a full EG1-HMAC-SHA256 Authorization header value for a GET
    request (no body) to `url`, using `credentials`.

    `timestamp` / `nonce` overrides exist only so tests can construct
    deliberately expired or duplicate values; production callers should
    omit both and let fresh values be generated.
    """
    parsed = urlsplit(url)
    host = parsed.netloc
    path_and_query = parsed.path + (("?" + parsed.query) if parsed.query else "")

    ts = timestamp or generate_timestamp()
    n = nonce or generate_nonce()

    auth_header_stub = _auth_header_without_signature(
        credentials.client_token, credentials.access_token, ts, n
    )
    data_to_sign = _make_data_to_sign(method, parsed.scheme or "http", host, path_and_query)
    signing_key = _make_signing_key(credentials.client_secret, ts)
    signature = base64.b64encode(
        hmac.new(signing_key, (data_to_sign + auth_header_stub).encode("utf-8"), hashlib.sha256).digest()
    ).decode("ascii")

    return auth_header_stub + f"signature={signature}"


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
    missing = [f for f in required if f not in fields or not fields[f]]
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


def validate_timestamp(timestamp: str, max_skew_seconds: int = 300,
                        now: Optional[datetime] = None) -> None:
    try:
        parsed = datetime.strptime(timestamp, TIMESTAMP_FORMAT).replace(tzinfo=timezone.utc)
    except ValueError:
        raise EdgeGridAuthenticationError("Malformed timestamp")

    current = now or datetime.now(timezone.utc)
    skew = abs((current - parsed).total_seconds())
    if skew > max_skew_seconds:
        raise EdgeGridAuthenticationError("Timestamp outside allowed clock-skew window")


def compute_expected_signature(method: str, scheme: str, host: str, path_and_query: str,
                                credentials: EdgeGridCredentials,
                                parsed_header: ParsedAuthHeader) -> str:
    """
    Server-side recomputation of the signature, using the method/scheme/
    host/path/query as observed on the actual inbound request (never as
    claimed by the client) plus the token/timestamp/nonce from the header.
    """
    auth_header_stub = _auth_header_without_signature(
        parsed_header.client_token, parsed_header.access_token,
        parsed_header.timestamp, parsed_header.nonce,
    )
    data_to_sign = _make_data_to_sign(method, scheme, host, path_and_query)
    signing_key = _make_signing_key(credentials.client_secret, parsed_header.timestamp)
    signature = base64.b64encode(
        hmac.new(signing_key, (data_to_sign + auth_header_stub).encode("utf-8"), hashlib.sha256).digest()
    ).decode("ascii")
    return signature


def constant_time_compare(a: str, b: str) -> bool:
    return hmac.compare_digest(a.encode("utf-8"), b.encode("utf-8"))
