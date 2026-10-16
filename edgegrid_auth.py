"""
EdgeGrid (EG1-HMAC-SHA256) authentication — Akamai documentation spec.

This is the ORIGINAL 5-field format described at:
https://techdocs.akamai.com/developer/docs/authenticate-with-edgegrid

dataToSign is the following fields joined by a literal tab:

    <METHOD>                                    GET, POST, ...
    <scheme>                                    http or https
    <host>                                      host only, no scheme or path
    <path-with-query>                           /event-viewer-api/v1/events?limit=50
    <EG1-HMAC-SHA256 client_token=..;access_token=..;timestamp=..;nonce=..;>

The final field ends with a semicolon and NO trailing tab.
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


def _make_signing_key(client_secret: str, timestamp: str) -> bytes:
    digest = hmac.new(
        client_secret.encode("utf-8"),
        timestamp.encode("utf-8"),
        hashlib.sha256,
    ).digest()
    return base64.b64encode(digest)


def _auth_stub(client_token: str, access_token: str,
               timestamp: str, nonce: str) -> str:
    # Note: this string ends with a semicolon. It is the entire 5th field.
    return (
        f"{ALGORITHM} "
        f"client_token={client_token};"
        f"access_token={access_token};"
        f"timestamp={timestamp};"
        f"nonce={nonce};"
    )


def _build_signing_string(method: str,
                          scheme: str,
                          host: str,
                          path_with_query: str,
                          auth_stub: str) -> str:
    # 5 fields, tab-joined. No trailing tab.
    return "\t".join([
        method.upper(),
        scheme,
        host,
        path_with_query,
        auth_stub,
    ])


def sign_request(method: str,
                 url: str,
                 credentials: EdgeGridCredentials,
                 timestamp: Optional[str] = None,
                 nonce: Optional[str] = None) -> str:
    parsed = urlsplit(url)
    scheme = parsed.scheme or "https"
    host = parsed.netloc
    # Path WITH query, exactly as it appears in the URL.
    path_with_query = parsed.path or "/"
    if parsed.query:
        path_with_query = f"{path_with_query}?{parsed.query}"

    ts = timestamp or generate_timestamp()
    n = nonce or generate_nonce()

    auth_stub = _auth_stub(credentials.client_token,
                           credentials.access_token,
                           ts, n)

    signing_string = _build_signing_string(
        method=method,
        scheme=scheme,
        host=host,
        path_with_query=path_with_query,
        auth_stub=auth_stub,
    )

    signing_key = _make_signing_key(credentials.client_secret, ts)
    signature = base64.b64encode(
        hmac.new(signing_key,
                 signing_string.encode("utf-8"),
                 hashlib.sha256).digest()
    ).decode("ascii")

    return auth_stub + f"signature={signature}"


# ----------------------------------------------------------------------
# Server side
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
        raise EdgeGridAuthenticationError(
            f"Authorization header missing fields: {missing}"
        )

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
        parsed = datetime.strptime(timestamp, TIMESTAMP_FORMAT).replace(
            tzinfo=timezone.utc
        )
    except ValueError:
        raise EdgeGridAuthenticationError("Malformed timestamp")
    current = now or datetime.now(timezone.utc)
    if abs((current - parsed).total_seconds()) > max_skew_seconds:
        raise EdgeGridAuthenticationError("Timestamp outside allowed clock-skew window")


def compute_expected_signature(method: str,
                               scheme: str,
                               host: str,
                               path_with_query: str,
                               credentials: EdgeGridCredentials,
                               parsed_header: ParsedAuthHeader) -> str:
    """
    Server-side recomputation.

    IMPORTANT: caller must pass `path_with_query` exactly as it was seen
    on the inbound request. Do not split into path and query; the Akamai
    doc format keeps them as one field.
    """
    auth_stub = _auth_stub(parsed_header.client_token,
                           parsed_header.access_token,
                           parsed_header.timestamp,
                           parsed_header.nonce)

    signing_string = _build_signing_string(
        method=method,
        scheme=scheme,
        host=host,
        path_with_query=path_with_query,
        auth_stub=auth_stub,
    )

    signing_key = _make_signing_key(credentials.client_secret,
                                    parsed_header.timestamp)
    return base64.b64encode(
        hmac.new(signing_key,
                 signing_string.encode("utf-8"),
                 hashlib.sha256).digest()
    ).decode("ascii")


def constant_time_compare(a: str, b: str) -> bool:
    return hmac.compare_digest(a.encode("utf-8"), b.encode("utf-8"))