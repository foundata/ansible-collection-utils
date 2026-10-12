# SPDX-FileCopyrightText: foundata GmbH <https://foundata.com>
# SPDX-License-Identifier: GPL-3.0-or-later
"""Bounded byte transport over ``open_url`` for product API clients.

The client sends bytes and returns bytes. A product client encodes its
requests, sets its authentication and product headers, decodes the body and
checks the HTTP and the application status. This transport owns the safety
mechanics: the socket timeout bounded by the shared deadline, the body size
limit, the closed response, no redirects, no ``netrc``, no decompression, and
the translation of transport failures into :class:`ApiError`.
"""

from __future__ import annotations

import json
import math
import os
import re
import socket
from collections.abc import Mapping
from dataclasses import dataclass, field
from http.client import HTTPException
from ssl import SSLCertVerificationError
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import unquote, urlsplit

from ansible.module_utils.urls import ConnectionError as UrlConnectionError
from ansible.module_utils.urls import SSLValidationError, open_url

from ansible_collections.foundata.utils.plugins.module_utils.deadline import (
    MAX_SECONDS,
    Deadline,
)
from ansible_collections.foundata.utils.plugins.module_utils.errors import (
    ApiError,
    TransportTimeout,
)

HTTP_METHODS = frozenset({"DELETE", "GET", "HEAD", "OPTIONS", "PATCH", "POST", "PUT"})
"""Request methods :meth:`HttpClient.request` accepts."""

RESERVED_HEADERS = frozenset(
    {
        "accept-encoding",
        "connection",
        "content-length",
        "host",
        "transfer-encoding",
        "user-agent",
    }
)
"""Header names a product may not set; the transport or ``urllib`` owns them."""

ERROR_CODES: dict[str, frozenset[str]] = {
    "argument": frozenset(
        {"invalid_ca_path", "invalid_header", "invalid_timeout", "invalid_url"}
    ),
    "request": frozenset({"invalid_path"}),
    "response": frozenset(
        {"body_too_large", "content_encoding", "invalid_json", "invalid_utf8"}
    ),
    "timeout": frozenset({"deadline_exceeded", "socket_timeout"}),
    "transport": frozenset(
        {"connection_failed", "http_protocol", "io_error", "tls_validation"}
    ),
}
"""Categories and codes this module raises, for a product's error schema."""

DEFAULT_TIMEOUT_SECONDS = 30
DEFAULT_DEADLINE_SECONDS = 120
DEFAULT_MAX_BODY_BYTES = 4 * 1024 * 1024

_READ_SIZE = 65536
_TIMEOUT_TYPES = (socket.timeout, TimeoutError)
_HEADER_NAME = re.compile(r"[!#$%&'*+\-.^_`|~0-9A-Za-z]+")
_HEADER_VALUE = re.compile(r"[\t\x20-\x7e]*")


@dataclass(frozen=True)
class HttpResponse:
    """A bounded HTTP response: status, lower-cased header names, raw body.

    An error status is a response too; the product validates ``status``
    before it uses ``body``. Repeated headers are joined with a comma and a
    space. Neither headers nor body appear in the ``repr``.
    """

    status: int
    headers: Mapping[str, str] = field(repr=False)
    body: bytes = field(repr=False)


class HttpClient:
    """A bounded byte transport for one API endpoint.

    Requests stay within ``base_url``: the path is validated against it and
    credentials never enter a URL. Every request takes the operation's
    :class:`Deadline`; the socket timeout is the smaller of ``timeout`` and
    the remaining budget. Responses are read with ``read1`` under a budget
    check before and after each read and a byte limit, and are closed in
    every case. Redirects are not followed, ``netrc`` is not read, and only
    the ``identity`` content encoding is accepted.
    """

    def __init__(
        self,
        base_url: str,
        *,
        user_agent: str,
        validate_certs: bool = True,
        ca_path: str | None = None,
        use_proxy: bool = True,
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
        max_body_bytes: int = DEFAULT_MAX_BODY_BYTES,
    ) -> None:
        """Configure the transport for one endpoint.

        Raises:
            ApiError: With category ``argument`` when ``base_url``,
                ``timeout`` or ``ca_path`` is invalid. A host that IDNA cannot
                encode, such as one with an empty label, makes the URL
                invalid; ``timeout`` must be positive, finite and at most
                :data:`deadline.MAX_SECONDS`. These values usually come from
                module options, so the module boundary reports them like any
                other expected failure.
            ValueError: When ``user_agent`` or ``max_body_bytes`` is invalid;
                these come from the product's code.
        """
        self._base_url, self._base_path = _parse_base_url(base_url)
        if not isinstance(user_agent, str) or not user_agent.strip():
            raise ValueError("user_agent must be a non-empty string")
        # The comparisons come before math.isfinite, which cannot convert an
        # integer beyond the float range.
        if (
            isinstance(timeout, bool)
            or not isinstance(timeout, (int, float))
            or timeout <= 0
            or timeout > MAX_SECONDS
            or not math.isfinite(timeout)
        ):
            raise ApiError(
                "The socket timeout must be a positive number of at most "
                f"{MAX_SECONDS} seconds",
                category="argument",
                code="invalid_timeout",
            )
        if ca_path is not None and not os.path.isfile(ca_path):
            raise ApiError(
                "The certificate authority path is not an existing regular file",
                category="argument",
                code="invalid_ca_path",
            )
        if isinstance(max_body_bytes, bool) or not isinstance(max_body_bytes, int):
            raise ValueError("max_body_bytes must be an int")
        if max_body_bytes <= 0:
            raise ValueError("max_body_bytes must be positive")
        self._user_agent = user_agent
        self._validate_certs = bool(validate_certs)
        self._ca_path = ca_path
        self._use_proxy = bool(use_proxy)
        self._timeout = float(timeout)
        self._max_body_bytes = max_body_bytes

    @property
    def base_url(self) -> str:
        """The normalized endpoint every request path is appended to."""
        return self._base_url

    def __repr__(self) -> str:
        return f"HttpClient(base_url={self._base_url!r})"

    def request(
        self,
        method: str,
        path: str,
        *,
        deadline: Deadline,
        headers: Mapping[str, str],
        body: bytes | None = None,
    ) -> HttpResponse:
        """Send one request and return its bounded response.

        ``path`` is appended to the base URL and may carry a query string.
        ``headers`` carries the product's authentication and content headers;
        names in :data:`RESERVED_HEADERS` are refused. The returned response
        may carry any HTTP status.

        Raises:
            ApiError: With category ``argument`` and code ``invalid_header``
                before any request when a header value holds a character
                other than a tab or printable ASCII; header values often carry
                credentials from module options. With category ``request``
                when the path leaves the endpoint, ``response`` when the body
                is too large or encoded, and ``transport`` when the
                connection, TLS validation, I/O or the HTTP protocol fails.
            TransportTimeout: When the socket times out or the deadline is
                exhausted before or after a read.
            ValueError: When ``method`` or a header name is invalid or
                reserved; these come from the product's code.
            TypeError: When ``body`` is neither bytes nor ``None``.
        """
        if method not in HTTP_METHODS:
            raise ValueError(f"Unsupported HTTP method {method!r}")
        if body is not None and not isinstance(body, (bytes, bytearray)):
            raise TypeError("body must be bytes or None")
        request_headers = _checked_headers(headers)
        request_headers["Accept-Encoding"] = "identity"
        url = self._url_for(path)
        socket_timeout = min(self._timeout, deadline.remaining())

        response: Any = None
        try:
            try:
                response = open_url(
                    url,
                    data=body,
                    headers=request_headers,
                    method=method,
                    use_proxy=self._use_proxy,
                    timeout=socket_timeout,
                    validate_certs=self._validate_certs,
                    http_agent=self._user_agent,
                    follow_redirects="none",
                    ca_path=self._ca_path,
                    decompress=False,
                    use_netrc=False,
                )
            except HTTPError as error:
                # The error carries the response; its body is read under the
                # same bounds so that a product parser can derive a diagnostic.
                response = error
            raw_headers = getattr(response, "headers", None)
            response_headers = _header_mapping(raw_headers)
            encodings = response_headers.get("content-encoding", "identity").split(",")
            if any(encoding.strip().lower() != "identity" for encoding in encodings):
                raise ApiError(
                    "Unsupported response content encoding",
                    category="response",
                    code="content_encoding",
                )
            data = self._read_body(response, deadline, method, raw_headers)
            return HttpResponse(
                status=int(response.code),
                headers=response_headers,
                body=data,
            )
        except _TIMEOUT_TYPES:
            raise TransportTimeout(code="socket_timeout") from None
        except URLError as error:
            reason = error.reason
            if isinstance(reason, _TIMEOUT_TYPES):
                raise TransportTimeout(code="socket_timeout") from None
            if isinstance(reason, SSLCertVerificationError):
                raise ApiError(
                    "TLS validation failed", category="transport", code="tls_validation"
                ) from None
            raise ApiError(
                "HTTP connection failed", category="transport", code="connection_failed"
            ) from None
        except SSLValidationError:
            raise ApiError(
                "TLS validation failed", category="transport", code="tls_validation"
            ) from None
        except UrlConnectionError:
            raise ApiError(
                "HTTP connection failed", category="transport", code="connection_failed"
            ) from None
        except OSError:
            raise ApiError(
                "HTTP transport I/O failed", category="transport", code="io_error"
            ) from None
        except HTTPException:
            raise ApiError(
                "Invalid or incomplete HTTP response",
                category="transport",
                code="http_protocol",
            ) from None
        finally:
            # An HTTPError without a body stream has no file to close.
            if response is not None and getattr(response, "fp", None) is not None:
                response.close()

    def _url_for(self, path: str) -> str:
        """Return the request URL, refusing a path that leaves the endpoint."""
        if (
            not isinstance(path, str)
            or not path.startswith("/")
            or path.startswith("//")
            or "#" in path
            or "\\" in path
            or any(ord(char) < 0x21 or ord(char) == 0x7F for char in path)
        ):
            raise ApiError(
                "The request path must stay within the endpoint",
                category="request",
                code="invalid_path",
            )
        resource = path.split("?", 1)[0]
        if any(unquote(segment) in (".", "..") for segment in resource.split("/")):
            raise ApiError(
                "The request path must not contain dot segments",
                category="request",
                code="invalid_path",
            )
        url = self._base_url + path
        parts = urlsplit(url)
        base = urlsplit(self._base_url)
        if (parts.scheme, parts.netloc) != (
            base.scheme,
            base.netloc,
        ) or not parts.path.startswith(self._base_path + "/"):
            raise ApiError(
                "The request path must stay within the endpoint",
                category="request",
                code="invalid_path",
            )
        return url

    def _read_body(
        self, response: Any, deadline: Deadline, method: str, raw_headers: Any
    ) -> bytes:
        """Read the body with ``read1`` under the deadline and the byte limit.

        A declared ``Content-Length`` beyond the limit is refused before the
        first read; a body shorter than its declared length is a protocol
        failure, because ``read1`` reports a closed connection as the end.
        HEAD and 304 carry no body; their length describes a representation.
        """
        if method == "HEAD" or response.code == 304:
            deadline.remaining()
            return b""
        if getattr(response, "fp", None) is None:
            return b""
        declared = _declared_length(raw_headers)
        if declared is not None and declared > self._max_body_bytes:
            raise ApiError(
                "Response exceeds the size limit",
                category="response",
                code="body_too_large",
            )
        chunks: list[bytes] = []
        size = 0
        while True:
            deadline.remaining()
            chunk = response.read1(min(_READ_SIZE, self._max_body_bytes + 1 - size))
            deadline.remaining()
            if not chunk:
                break
            size += len(chunk)
            if size > self._max_body_bytes:
                raise ApiError(
                    "Response exceeds the size limit",
                    category="response",
                    code="body_too_large",
                )
            chunks.append(chunk)
        if declared is not None and size < declared:
            raise ApiError(
                "Incomplete HTTP response body",
                category="transport",
                code="http_protocol",
            )
        return b"".join(chunks)


def decode_json(body: bytes) -> Any:
    """Decode a JSON response body, or raise a local ``response`` error.

    JSON travels as UTF-8; an empty body is not a document, so a product that
    answers a write without a body checks ``HttpResponse.body`` before it
    decodes. The decoded value is returned as is; the product validates its
    shape.

    Raises:
        ApiError: With category ``response`` and code ``invalid_utf8`` when
            the bytes are not UTF-8, or ``invalid_json`` when the text is not
            a JSON document. Neither message carries the body.
        TypeError: When ``body`` is not bytes.
    """
    if not isinstance(body, (bytes, bytearray)):
        raise TypeError("body must be bytes")
    try:
        text = bytes(body).decode("utf-8")
    except UnicodeDecodeError:
        raise ApiError(
            "Response is not valid UTF-8", category="response", code="invalid_utf8"
        ) from None
    try:
        return json.loads(text)
    except (ValueError, RecursionError):
        raise ApiError(
            "Response is not a JSON document", category="response", code="invalid_json"
        ) from None


def http_argument_spec() -> dict[str, Any]:
    """Return a fresh argument specification of the mechanical options.

    The options match the ``foundata.utils.http_client`` documentation fragment. A
    product adds its credential options to the copy and may override an
    entry; each call builds a new specification, so extending it affects no
    other module.
    """
    return {
        "url": {"type": "str", "required": True},
        "validate_certs": {"type": "bool", "default": True},
        "ca_path": {"type": "path"},
        "use_proxy": {"type": "bool", "default": True},
        "timeout": {"type": "int", "default": DEFAULT_TIMEOUT_SECONDS},
        "deadline": {"type": "int", "default": DEFAULT_DEADLINE_SECONDS},
    }


def _parse_base_url(base_url: str) -> tuple[str, str]:
    """Return the normalized base URL and its path prefix."""
    try:
        parts = urlsplit(base_url) if isinstance(base_url, str) else None
        hostname = parts.hostname if parts is not None else None
        # getaddrinfo would refuse such a host later with an untranslated
        # UnicodeError, for example an empty or overlong label.
        if hostname:
            hostname.encode("idna")
    except (UnicodeError, ValueError):
        parts, hostname = None, None
    if (
        parts is None
        or parts.scheme not in ("http", "https")
        or not parts.netloc
        or not hostname
        or "@" in parts.netloc
        or parts.query
        or parts.fragment
        or any(ord(char) < 0x21 or ord(char) == 0x7F for char in base_url)
    ):
        raise ApiError(
            "The endpoint URL must be an absolute http or https URL without "
            "credentials, query or fragment",
            category="argument",
            code="invalid_url",
        )
    base_path = parts.path.rstrip("/")
    return f"{parts.scheme}://{parts.netloc}{base_path}", base_path


def _declared_length(raw_headers: Any) -> int | None:
    """Return the declared ``Content-Length`` when it is a plain number."""
    value = raw_headers.get("Content-Length") if raw_headers else None
    if value is None or not str(value).strip().isdigit():
        return None
    return int(str(value).strip())


def _checked_headers(headers: Mapping[str, str]) -> dict[str, str]:
    """Return a copy of the product's headers after validating each one."""
    checked: dict[str, str] = {}
    for name, value in headers.items():
        if not isinstance(name, str) or not isinstance(value, str):
            raise TypeError("Header names and values must be str")
        if not _HEADER_NAME.fullmatch(name):
            raise ValueError("Invalid HTTP header name")
        if name.lower() in RESERVED_HEADERS:
            raise ValueError(f"HTTP header {name!r} is owned by the transport")
        if not _HEADER_VALUE.fullmatch(value):
            raise ApiError(
                "Invalid HTTP header value", category="argument", code="invalid_header"
            )
        checked[name] = value
    return checked


def _header_mapping(raw_headers: Any) -> dict[str, str]:
    """Return response headers with lower-cased names, repeats joined."""
    mapping: dict[str, str] = {}
    for name, value in raw_headers.items() if raw_headers else ():
        key = str(name).lower()
        mapping[key] = f"{mapping[key]}, {value}" if key in mapping else str(value)
    return mapping
