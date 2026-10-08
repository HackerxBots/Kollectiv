"""AWS Signature Version 4 signing for S3-compatible object storage.

Cloudflare R2, Backblaze B2, MinIO and AWS S3 all speak the same REST dialect
but require every request to be signed. Kollektiv keeps its "no SDKs, only
``httpx``" rule by signing the requests itself:

* :func:`sign_request` returns the headers for a request (including
  ``Authorization``), so uploads stay plain ``httpx`` calls.
* :func:`presign_url` builds a time-limited URL that can be handed to a browser
  or to another agent without sharing credentials.

The implementation follows the AWS "Signature Version 4" specification
(payload hash, canonical request, string to sign, derived signing key) and is
verified against AWS's published vectors and ``botocore`` in the test suite.

Usage::

    headers = sign_request(
        "PUT",
        "https://acct.r2.cloudflarestorage.com/bucket/key",
        body=b"hello",
        access_key="...",
        secret_key="...",
        region="auto",
    )
"""

from __future__ import annotations

import hashlib
import hmac
import re
from datetime import UTC, datetime
from typing import Any, Dict, Mapping, Optional, Tuple
from urllib.parse import quote, urlparse

ALGORITHM = "AWS4-HMAC-SHA256"
UNSIGNED_PAYLOAD = "UNSIGNED-PAYLOAD"
EMPTY_PAYLOAD_SHA256 = hashlib.sha256(b"").hexdigest()

#: Headers that are always signed when present. Anything else (``user-agent``,
#: hop-by-hop headers, ...) is intentionally left out of ``SignedHeaders``.
_SIGNABLE_PREFIXES = ("x-amz-",)
_SIGNABLE_HEADERS = {"host", "content-type", "content-md5"}


def _hmac(key: bytes, message: str) -> bytes:
    """Return ``HMAC-SHA256(key, message)`` as raw bytes."""
    return hmac.new(key, message.encode("utf-8"), hashlib.sha256).digest()


def sha256_hex(payload: bytes) -> str:
    """Return the lowercase hex SHA-256 digest of ``payload``."""
    return hashlib.sha256(payload).hexdigest()


def uri_encode(value: str, encode_slash: bool = True) -> str:
    """Encode ``value`` the way SigV4 requires (RFC 3986, uppercase hex).

    Args:
        value: The raw string.
        encode_slash: ``False`` keeps ``/`` separators (used for paths).

    Returns:
        The percent-encoded string.
    """
    safe = "-_.~" if encode_slash else "-_.~/"
    return quote(value, safe=safe)


#: Characters that stay literal in a canonical URI path. Percent escapes are
#: preserved: S3 and R2 must not double-encode an already escaped key.
_PATH_SAFE = re.compile(r"[^A-Za-z0-9\-_.~/!$&'()*+,;=:@%]")


def canonical_path(path: str) -> str:
    """Return the canonical URI for ``path`` without double-encoding.

    Callers are expected to hand over the *encoded* URL they will send on the
    wire (``https://host/kollektiv/a%20b.txt``). Percent escapes are therefore
    preserved and only characters that are illegal in a URI path are escaped.
    """
    if not path:
        return "/"
    encoded = _PATH_SAFE.sub(lambda match: quote(match.group(0), safe=""), path)
    return encoded if encoded.startswith("/") else "/" + encoded


def canonical_query_string(params: Mapping[str, Any]) -> str:
    """Build the canonical ``key=value&...`` query string (sorted, encoded)."""
    pairs: list[Tuple[str, str]] = []
    for key, value in params.items():
        if value is None:
            continue
        if isinstance(value, (list, tuple)):
            pairs.extend((str(key), str(item)) for item in value)
        else:
            pairs.append((str(key), str(value)))
    encoded = [(uri_encode(key), uri_encode(item)) for key, item in pairs]
    encoded.sort()
    return "&".join(f"{key}={value}" for key, value in encoded)


def canonical_headers(headers: Mapping[str, str]) -> Tuple[str, str]:
    """Return ``(canonical_headers, signed_headers)`` for the given headers."""
    normalised: Dict[str, str] = {}
    for name, value in headers.items():
        key = name.strip().lower()
        if key in _SIGNABLE_HEADERS or key.startswith(_SIGNABLE_PREFIXES):
            normalised[key] = " ".join(str(value).strip().split())
    ordered = sorted(normalised.items())
    canonical = "".join(f"{key}:{value}\n" for key, value in ordered)
    signed = ";".join(key for key, _ in ordered)
    return canonical, signed


def derive_signing_key(
    secret_key: str, date_stamp: str, region: str, service: str = "s3"
) -> bytes:
    """Derive the SigV4 signing key (``AWS4`` chain of HMACs)."""
    key = ("AWS4" + secret_key).encode("utf-8")
    return _hmac(_hmac(_hmac(_hmac(key, date_stamp), region), service), "aws4_request")


def build_canonical_request(
    method: str,
    path: str,
    query: Mapping[str, Any],
    headers: Mapping[str, str],
    payload_hash: str,
) -> Tuple[str, str]:
    """Return ``(canonical_request, signed_headers)`` for a request."""
    canonical_uri = canonical_path(path)
    canonical_qs = canonical_query_string(query)
    canonical_hdr, signed = canonical_headers(headers)
    request = "\n".join(
        [
            method.upper(),
            canonical_uri,
            canonical_qs,
            canonical_hdr,
            signed,
            payload_hash,
        ]
    )
    return request, signed


def sign_request(
    method: str,
    url: str,
    *,
    access_key: str,
    secret_key: str,
    region: str = "auto",
    service: str = "s3",
    headers: Optional[Mapping[str, str]] = None,
    params: Optional[Mapping[str, Any]] = None,
    body: Optional[bytes] = None,
    payload_hash: Optional[str] = None,
    session_token: str = "",
    now: Optional[datetime] = None,
) -> Dict[str, str]:
    """Sign a request and return the headers to send with it.

    Args:
        method: HTTP method.
        url: Full request URL (scheme, host, path).
        access_key: Access key id.
        secret_key: Secret access key.
        region: Signing region (``auto`` for R2).
        service: Signing service name (``s3``).
        headers: Headers that will be sent (``host`` is derived from ``url``).
        params: Query parameters that will be sent.
        body: Request body, hashed to produce the payload hash.
        payload_hash: Explicit payload hash (e.g. ``UNSIGNED-PAYLOAD``).
        session_token: Optional STS session token.
        now: Timestamp override (tests).

    Returns:
        The complete header dict, including ``Authorization``,
        ``x-amz-content-sha256`` and ``x-amz-date``.
    """
    parsed = urlparse(url)
    moment = (now or datetime.now(UTC)).astimezone(UTC)
    amz_date = moment.strftime("%Y%m%dT%H%M%SZ")
    date_stamp = moment.strftime("%Y%m%d")

    if payload_hash is None:
        payload_hash = sha256_hex(body) if body is not None else EMPTY_PAYLOAD_SHA256

    signed: Dict[str, str] = {"host": parsed.netloc}
    for name, value in (headers or {}).items():
        signed[name] = value
    signed["x-amz-content-sha256"] = payload_hash
    signed["x-amz-date"] = amz_date
    if session_token:
        signed["x-amz-security-token"] = session_token

    canonical_request, signed_headers = build_canonical_request(
        method, parsed.path, params or {}, signed, payload_hash
    )
    scope = f"{date_stamp}/{region}/{service}/aws4_request"
    string_to_sign = "\n".join(
        [
            ALGORITHM,
            amz_date,
            scope,
            sha256_hex(canonical_request.encode("utf-8")),
        ]
    )
    signing_key = derive_signing_key(secret_key, date_stamp, region, service)
    signature = hmac.new(
        signing_key, string_to_sign.encode("utf-8"), hashlib.sha256
    ).hexdigest()

    signed["Authorization"] = (
        f"{ALGORITHM} Credential={access_key}/{scope}, "
        f"SignedHeaders={signed_headers}, Signature={signature}"
    )
    # ``host`` is set by httpx from the URL; keep it out of the returned headers.
    signed.pop("host", None)
    return signed


def presign_url(
    method: str,
    url: str,
    *,
    access_key: str,
    secret_key: str,
    region: str = "auto",
    service: str = "s3",
    expires: int = 3600,
    session_token: str = "",
    now: Optional[datetime] = None,
    params: Optional[Mapping[str, Any]] = None,
) -> str:
    """Return a presigned URL for ``method`` on ``url``.

    Presigned URLs use ``UNSIGNED-PAYLOAD`` so they never require the caller to
    hash the body. The signature travels in the query string, which is what
    browsers and ``<img>``/download links can use directly.
    """
    parsed = urlparse(url)
    moment = (now or datetime.now(UTC)).astimezone(UTC)
    amz_date = moment.strftime("%Y%m%dT%H%M%SZ")
    date_stamp = moment.strftime("%Y%m%d")
    scope = f"{date_stamp}/{region}/{service}/aws4_request"

    query: Dict[str, Any] = dict(params or {})
    query.update(
        {
            "X-Amz-Algorithm": ALGORITHM,
            "X-Amz-Credential": f"{access_key}/{scope}",
            "X-Amz-Date": amz_date,
            "X-Amz-Expires": int(expires),
            "X-Amz-SignedHeaders": "host",
        }
    )
    if session_token:
        query["X-Amz-Security-Token"] = session_token

    canonical_request, _ = build_canonical_request(
        method, parsed.path, query, {"host": parsed.netloc}, UNSIGNED_PAYLOAD
    )
    string_to_sign = "\n".join(
        [ALGORITHM, amz_date, scope, sha256_hex(canonical_request.encode("utf-8"))]
    )
    signature = hmac.new(
        derive_signing_key(secret_key, date_stamp, region, service),
        string_to_sign.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    query["X-Amz-Signature"] = signature
    return f"{parsed.scheme}://{parsed.netloc}{parsed.path}?{canonical_query_string(query)}"
