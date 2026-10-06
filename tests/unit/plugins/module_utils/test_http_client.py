# SPDX-License-Identifier: GPL-3.0-or-later
# SPDX-FileCopyrightText: foundata GmbH (https://foundata.com)
"""Tests of the bounded byte transport.

The loopback server cases exercise real sockets on 127.0.0.1 and print the
measured times; the fake opener cases cover the translation of every
exception type ``open_url`` can raise.
"""

from __future__ import annotations

import gzip
import io
import socket
import ssl
import time
from collections.abc import Callable, Iterator
from email.message import Message
from email.parser import BytesParser
from http.client import HTTPException
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode

import pytest
from ansible.module_utils.urls import ConnectionError as UrlConnectionError
from ansible.module_utils.urls import SSLValidationError

from ansible_collections.foundata.utils.plugins.module_utils import (
    http_client as http_module,
)
from ansible_collections.foundata.utils.plugins.module_utils.deadline import Deadline
from ansible_collections.foundata.utils.plugins.module_utils.errors import (
    ApiError,
    TransportTimeout,
)
from ansible_collections.foundata.utils.plugins.module_utils.http_client import (
    DEFAULT_DEADLINE_SECONDS,
    DEFAULT_TIMEOUT_SECONDS,
    ERROR_CODES,
    RESERVED_HEADERS,
    HttpClient,
    HttpResponse,
    decode_json,
    http_argument_spec,
)
from ansible_collections.foundata.utils.tests.unit.plugins.module_utils.loopback_server import (
    LoopbackServer,
    hold,
    response_bytes,
    send_all,
    trickle,
)

SECRET = "SYNTHETIC-SECRET-7b1e44d0"
AGENT = "foundata.utils-tests/1.0"
NO_HEADERS: dict[str, str] = {}


def timed(call: Callable[[], Any]) -> tuple[Any, float]:
    started = time.monotonic()
    result = call()
    return result, time.monotonic() - started


def timed_failure(
    call: Callable[[], Any], exception: type[BaseException]
) -> tuple[Any, float]:
    started = time.monotonic()
    with pytest.raises(exception) as excinfo:
        call()
    return excinfo.value, time.monotonic() - started


def report(label: str, seconds: float) -> None:
    print(f"[timing] {label}: {seconds:.3f} s")


@pytest.fixture
def server() -> Iterator[Callable[..., LoopbackServer]]:
    servers: list[LoopbackServer] = []

    def start(
        handler: Callable[[socket.socket, bytes], None], **kwargs: Any
    ) -> LoopbackServer:
        instance = LoopbackServer(handler, **kwargs)
        servers.append(instance)
        return instance

    yield start
    for instance in servers:
        instance.close()


def client_for(server: LoopbackServer, **kwargs: Any) -> HttpClient:
    options: dict[str, Any] = {"user_agent": AGENT, "timeout": 5}
    options.update(kwargs)
    return HttpClient(server.url, **options)


def respond(
    status: int, body: bytes = b"", headers: dict[str, str] | None = None
) -> Any:
    def handler(conn: socket.socket, request: bytes) -> None:
        send_all(conn, response_bytes(status, body, headers))

    return handler


# --- Construction and request validation -------------------------------------


@pytest.mark.parametrize(
    "base_url",
    [
        "ftp://host/x",
        "host/api",
        "//host/api",
        f"https://user:{SECRET}@host/api",
        "https://host/api?x=1",
        "https://host/api#frag",
        "https://host/api with space",
        "https://",
        "",
        None,
        42,
    ],
)
def test_invalid_base_url_is_an_argument_error(base_url: Any) -> None:
    with pytest.raises(ApiError) as excinfo:
        HttpClient(base_url, user_agent=AGENT)
    assert (excinfo.value.category, excinfo.value.code) == ("argument", "invalid_url")
    assert SECRET not in str(excinfo.value)


def test_base_url_is_normalized_and_kept_out_of_nothing_else() -> None:
    client = HttpClient("https://host:8443/api/", user_agent=AGENT)
    assert client.base_url == "https://host:8443/api"
    assert repr(client) == "HttpClient(base_url='https://host:8443/api')"


@pytest.mark.parametrize(
    "timeout", [0, -1, True, float("inf"), float("nan"), "5", None]
)
def test_invalid_timeout_is_an_argument_error(timeout: Any) -> None:
    with pytest.raises(ApiError) as excinfo:
        HttpClient("https://host", user_agent=AGENT, timeout=timeout)
    assert (excinfo.value.category, excinfo.value.code) == (
        "argument",
        "invalid_timeout",
    )


def test_missing_ca_path_is_an_argument_error(tmp_path: Path) -> None:
    with pytest.raises(ApiError) as excinfo:
        HttpClient(
            "https://host", user_agent=AGENT, ca_path=str(tmp_path / "missing.pem")
        )
    assert (excinfo.value.category, excinfo.value.code) == (
        "argument",
        "invalid_ca_path",
    )
    with pytest.raises(ApiError):
        HttpClient("https://host", user_agent=AGENT, ca_path=str(tmp_path))


@pytest.mark.parametrize("user_agent", ["", "  ", None, 3])
def test_invalid_user_agent_is_a_programming_error(user_agent: Any) -> None:
    with pytest.raises(ValueError):
        HttpClient("https://host", user_agent=user_agent)


@pytest.mark.parametrize("limit", [0, -1, True, 1.5, "1"])
def test_invalid_body_limit_is_a_programming_error(limit: Any) -> None:
    with pytest.raises(ValueError):
        HttpClient("https://host", user_agent=AGENT, max_body_bytes=limit)


@pytest.mark.parametrize(
    "path",
    [
        "x",
        "",
        "//evil.example/x",
        "/a/../b",
        "/..",
        "/a/./b",
        "/%2e%2e/b",
        "/a/.%2E/b",
        "/a#b",
        "/a\\b",
        "/a b",
        "/a\r\nX: y",
        "/a\x7f",
        None,
    ],
)
def test_path_that_leaves_the_endpoint_is_rejected(
    monkeypatch: pytest.MonkeyPatch, path: Any
) -> None:
    monkeypatch.setattr(http_module, "open_url", pytest.fail)
    client = HttpClient("https://host/api", user_agent=AGENT)
    with pytest.raises(ApiError) as excinfo:
        client.request("GET", path, deadline=Deadline(5), headers=NO_HEADERS)
    assert (excinfo.value.category, excinfo.value.code) == ("request", "invalid_path")


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("Accept-Encoding", "gzip"),
        ("user-agent", "other"),
        ("Host", "evil.example"),
        ("Content-Length", "1"),
        ("Transfer-Encoding", "chunked"),
        ("Connection", "keep-alive"),
        ("X-Bad Name", "x"),
        ("", "x"),
        ("X-Injected", "a\r\nX-Other: b"),
        ("X-Nonascii", "ä"),
    ],
)
def test_reserved_or_malformed_headers_are_rejected(
    monkeypatch: pytest.MonkeyPatch, name: str, value: str
) -> None:
    monkeypatch.setattr(http_module, "open_url", pytest.fail)
    client = HttpClient("https://host", user_agent=AGENT)
    with pytest.raises(ValueError):
        client.request("GET", "/", deadline=Deadline(5), headers={name: value})


def test_non_str_headers_and_bad_body_types_are_rejected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(http_module, "open_url", pytest.fail)
    client = HttpClient("https://host", user_agent=AGENT)
    with pytest.raises(TypeError):
        client.request("GET", "/", deadline=Deadline(5), headers={"X": 1})  # type: ignore[dict-item]
    text_body: Any = "text"
    with pytest.raises(TypeError):
        client.request(
            "POST", "/", deadline=Deadline(5), headers=NO_HEADERS, body=text_body
        )
    with pytest.raises(ValueError):
        client.request("TRACE", "/", deadline=Deadline(5), headers=NO_HEADERS)


def test_exhausted_deadline_fails_before_opening(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(http_module, "open_url", pytest.fail)
    deadline = Deadline(0.001)
    time.sleep(0.01)
    client = HttpClient("https://host", user_agent=AGENT)
    with pytest.raises(TransportTimeout) as excinfo:
        client.request("GET", "/", deadline=deadline, headers=NO_HEADERS)
    assert excinfo.value.code == "deadline_exceeded"


# --- The fake opener: arguments and exception translation ---------------------


def raising(error: BaseException) -> Callable[..., Any]:
    """Return a fake opener that raises ``error``."""

    def fake_open_url(url: str, **kwargs: Any) -> Any:
        raise error

    return fake_open_url


def message(headers: dict[str, str]) -> Message:
    result = Message()
    for name, value in headers.items():
        result[name] = value
    return result


class FakeResponse(io.BytesIO):
    """Mimics the parts of http.client.HTTPResponse the transport uses."""

    def __init__(
        self, status: int, body: bytes, headers: dict[str, str] | None = None
    ) -> None:
        super().__init__(body)
        self.code = status
        self.headers = headers or {}
        self.fp = self
        self.closed_count = 0

    def close(self) -> None:
        self.closed_count += 1
        super().close()


def test_open_url_receives_the_safety_settings(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    calls: list[dict[str, Any]] = []
    ca_file = tmp_path / "ca.pem"
    ca_file.write_text("x")

    def fake_open_url(url: str, **kwargs: Any) -> FakeResponse:
        calls.append({"url": url, **kwargs})
        return FakeResponse(200, b"ok", {"Content-Type": "text/plain"})

    monkeypatch.setattr(http_module, "open_url", fake_open_url)
    client = HttpClient(
        "https://host/api",
        user_agent=AGENT,
        validate_certs=False,
        ca_path=str(ca_file),
        use_proxy=False,
        timeout=7,
    )
    deadline = Deadline(100)
    response = client.request(
        "POST",
        "/x?q=1",
        deadline=deadline,
        headers={"Authorization": f"Bearer {SECRET}"},
        body=b"{}",
    )
    assert response == HttpResponse(200, {"content-type": "text/plain"}, b"ok")
    call = calls[0]
    assert call["url"] == "https://host/api/x?q=1"
    assert call["method"] == "POST"
    assert call["data"] == b"{}"
    assert call["headers"] == {
        "Authorization": f"Bearer {SECRET}",
        "Accept-Encoding": "identity",
    }
    assert call["validate_certs"] is False
    assert call["ca_path"] == str(ca_file)
    assert call["use_proxy"] is False
    assert call["http_agent"] == AGENT
    assert call["follow_redirects"] == "none"
    assert call["use_netrc"] is False
    assert call["decompress"] is False
    assert 0 < call["timeout"] <= 7


def test_socket_timeout_is_the_smaller_of_timeout_and_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    timeouts: list[float] = []

    def fake_open_url(url: str, **kwargs: Any) -> FakeResponse:
        timeouts.append(kwargs["timeout"])
        return FakeResponse(200, b"")

    monkeypatch.setattr(http_module, "open_url", fake_open_url)
    client = HttpClient("https://host", user_agent=AGENT, timeout=30)
    client.request("GET", "/", deadline=Deadline(2), headers=NO_HEADERS)
    client.request("GET", "/", deadline=Deadline(60), headers=NO_HEADERS)
    assert 0 < timeouts[0] <= 2
    assert 30 <= timeouts[1] <= 30.0


@pytest.mark.parametrize(
    ("raised", "expected"),
    [
        (socket.timeout(), ("timeout", "socket_timeout")),
        (TimeoutError(), ("timeout", "socket_timeout")),
        (URLError(socket.timeout()), ("timeout", "socket_timeout")),
        (URLError(TimeoutError()), ("timeout", "socket_timeout")),
        (URLError(ssl.SSLCertVerificationError()), ("transport", "tls_validation")),
        (URLError(ConnectionRefusedError()), ("transport", "connection_failed")),
        (URLError("unreachable"), ("transport", "connection_failed")),
        (SSLValidationError("x"), ("transport", "tls_validation")),
        (UrlConnectionError("x"), ("transport", "connection_failed")),
        (ConnectionResetError(), ("transport", "io_error")),
        (OSError("x"), ("transport", "io_error")),
        (HTTPException("x"), ("transport", "http_protocol")),
    ],
)
def test_opening_failures_are_translated(
    monkeypatch: pytest.MonkeyPatch, raised: BaseException, expected: tuple[str, str]
) -> None:
    monkeypatch.setattr(http_module, "open_url", raising(raised))
    client = HttpClient(f"https://host/{SECRET}", user_agent=AGENT)
    with pytest.raises(ApiError) as excinfo:
        client.request("GET", "/", deadline=Deadline(5), headers={"X-Token": SECRET})
    assert (excinfo.value.category, excinfo.value.code) == expected
    assert isinstance(excinfo.value, TransportTimeout) == (expected[0] == "timeout")
    assert excinfo.value.code in ERROR_CODES[expected[0]]
    assert excinfo.value.__cause__ is None and excinfo.value.__suppress_context__
    assert SECRET not in str(excinfo.value) + repr(excinfo.value)


@pytest.mark.parametrize(
    ("raised", "expected"),
    [
        (socket.timeout(), ("timeout", "socket_timeout")),
        (ConnectionResetError(), ("transport", "io_error")),
        (HTTPException("incomplete"), ("transport", "http_protocol")),
    ],
)
def test_read_failures_are_translated_and_the_response_closed(
    monkeypatch: pytest.MonkeyPatch, raised: BaseException, expected: tuple[str, str]
) -> None:
    class FailingResponse(FakeResponse):
        def read1(self, size: int | None = -1) -> bytes:
            raise raised

    response = FailingResponse(200, b"")
    monkeypatch.setattr(http_module, "open_url", lambda url, **kwargs: response)
    client = HttpClient("https://host", user_agent=AGENT)
    with pytest.raises(ApiError) as excinfo:
        client.request("GET", "/", deadline=Deadline(5), headers=NO_HEADERS)
    assert (excinfo.value.category, excinfo.value.code) == expected
    assert response.closed_count == 1


def test_http_error_is_a_response_and_gets_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    body = FakeResponse(503, b'{"error": "busy"}')
    error = HTTPError(
        "https://host/",
        503,
        "busy",
        message({"Content-Type": "application/json", "Retry-After": "1"}),
        body,
    )
    monkeypatch.setattr(http_module, "open_url", raising(error))
    client = HttpClient("https://host", user_agent=AGENT)
    response = client.request("GET", "/", deadline=Deadline(5), headers=NO_HEADERS)
    assert response.status == 503
    assert response.body == b'{"error": "busy"}'
    assert response.headers == {"content-type": "application/json", "retry-after": "1"}
    assert body.closed_count == 1


def test_http_error_without_a_body_stream_is_handled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    error = HTTPError("https://host/", 502, "bad gateway", Message(), None)
    monkeypatch.setattr(http_module, "open_url", raising(error))
    client = HttpClient("https://host", user_agent=AGENT)
    response = client.request("GET", "/", deadline=Deadline(5), headers=NO_HEADERS)
    assert response == HttpResponse(502, {}, b"")


def test_response_repr_hides_headers_and_body() -> None:
    response = HttpResponse(200, {"authorization": SECRET}, SECRET.encode())
    assert SECRET not in repr(response)
    assert repr(response) == "HttpResponse(status=200)"


def test_argument_spec_is_fresh() -> None:
    first, second = http_argument_spec(), http_argument_spec()
    assert first == second and first is not second
    first["url"]["default"] = "mutated"
    assert "default" not in http_argument_spec()["url"]
    assert set(first) == {
        "url",
        "validate_certs",
        "ca_path",
        "use_proxy",
        "timeout",
        "deadline",
    }
    assert first["timeout"]["default"] == DEFAULT_TIMEOUT_SECONDS
    assert first["deadline"]["default"] == DEFAULT_DEADLINE_SECONDS
    assert first["url"]["required"] is True
    assert first["ca_path"]["type"] == "path"
    assert RESERVED_HEADERS >= {"accept-encoding", "user-agent"}


# --- Loopback server cases ----------------------------------------------------


def test_success_returns_bounded_bytes_and_closes(
    server: Callable[..., LoopbackServer],
) -> None:
    instance = server(
        respond(
            200, b'{"ok": true}', {"Content-Type": "application/json", "X-Dup": "a"}
        )
    )
    client = client_for(instance)
    response = client.request(
        "GET",
        "/api/x?y=1",
        deadline=Deadline(5),
        headers={"Authorization": f"Bearer {SECRET}"},
    )
    assert response.status == 200
    assert response.body == b'{"ok": true}'
    assert response.headers["content-type"] == "application/json"
    assert response.headers["content-length"] == "12"
    request = instance.requests[0].decode()
    assert request.startswith("GET /api/x?y=1 HTTP/1.1\r\n")
    assert f"Authorization: Bearer {SECRET}\r\n" in request
    assert "Accept-Encoding: identity\r\n" in request
    assert (
        f"User-agent: {AGENT}\r\n" in request or f"User-Agent: {AGENT}\r\n" in request
    )
    assert instance.wait_closed() is True


@pytest.mark.parametrize(
    "body",
    [
        None,
        b"",
        urlencode(
            [
                ("role", "admin"),
                ("role", "user"),
                ("name", "J\u00f6rg"),
            ]
        ).encode("ascii"),
    ],
)
def test_post_sends_the_bytes_unchanged(
    server: Callable[..., LoopbackServer], body: bytes | None
) -> None:
    received: list[tuple[Message, bytes]] = []

    def handler(conn: socket.socket, request: bytes) -> None:
        head, _separator, data = request.partition(b"\r\n\r\n")
        headers = BytesParser().parsebytes(head.split(b"\r\n", 1)[1])
        length = int(headers.get("Content-Length", "0"))
        while len(data) < length:
            chunk = conn.recv(length - len(data))
            if not chunk:
                raise AssertionError("The request body ended early")
            data += chunk
        assert len(data) == length
        received.append((headers, data))
        send_all(conn, response_bytes(201, b""))

    instance = server(handler)
    headers = {"OCS-APIRequest": "true"}
    if body is not None:
        headers["Content-Type"] = "application/x-www-form-urlencoded"
    response = client_for(instance).request(
        "GET" if body is None else "POST",
        "/",
        deadline=Deadline(5),
        headers=headers,
        body=body,
    )
    assert response.status == 201 and response.body == b""
    wire_headers, wire_body = received[0]
    assert wire_body == (body or b"")
    assert wire_headers["OCS-APIRequest"] == "true"
    if body is None:
        assert wire_headers["Content-Type"] is None
        assert wire_headers["Content-Length"] is None
    else:
        assert wire_headers["Content-Type"] == "application/x-www-form-urlencoded"
        assert wire_headers["Content-Length"] == str(len(body))
    if body:
        assert wire_body == b"role=admin&role=user&name=J%C3%B6rg"


def test_error_status_with_body_is_returned(
    server: Callable[..., LoopbackServer],
) -> None:
    instance = server(
        respond(
            500,
            b'{"result": "failed", "detail": "x"}',
            {"Content-Type": "application/json"},
        )
    )
    response = client_for(instance).request(
        "GET", "/", deadline=Deadline(5), headers=NO_HEADERS
    )
    assert response.status == 500
    assert response.body == b'{"result": "failed", "detail": "x"}'
    assert instance.wait_closed() is True


@pytest.mark.parametrize(
    "handler",
    [respond(204), respond(200, b"", {"Content-Type": "application/json"})],
    ids=["204", "200-empty"],
)
def test_empty_response_yields_empty_bytes(
    server: Callable[..., LoopbackServer], handler: Any
) -> None:
    instance = server(handler)
    response = client_for(instance).request(
        "GET", "/", deadline=Deadline(5), headers=NO_HEADERS
    )
    assert response.body == b""


def test_head_has_no_body(server: Callable[..., LoopbackServer]) -> None:
    def handler(conn: socket.socket, request: bytes) -> None:
        send_all(
            conn, b"HTTP/1.1 200 OK\r\nContent-Length: 10\r\nConnection: close\r\n\r\n"
        )

    instance = server(handler)
    response = client_for(instance).request(
        "HEAD", "/", deadline=Deadline(5), headers=NO_HEADERS
    )
    assert response.status == 200 and response.body == b""
    assert response.headers["content-length"] == "10"


def test_cross_origin_redirect_is_returned_not_followed(
    server: Callable[..., LoopbackServer],
) -> None:
    other = server(respond(200, b"leaked"))
    instance = server(respond(302, b"moved", {"Location": f"{other.url}/login"}))
    response = client_for(instance).request(
        "GET", "/", deadline=Deadline(5), headers={"Authorization": f"Bearer {SECRET}"}
    )
    assert response.status == 302
    assert response.headers["location"] == f"{other.url}/login"
    assert response.body == b"moved"
    assert other.requests == []
    assert instance.wait_closed() is True


def test_same_origin_redirect_is_not_followed_either(
    server: Callable[..., LoopbackServer],
) -> None:
    instance = server(respond(301, b"", {"Location": "/elsewhere"}))
    response = client_for(instance).request(
        "POST", "/", deadline=Deadline(5), headers=NO_HEADERS, body=b"x"
    )
    assert response.status == 301
    assert len(instance.requests) == 1


def test_oversized_body_with_content_length_is_rejected_before_reading(
    server: Callable[..., LoopbackServer],
) -> None:
    instance = server(respond(200, b"x" * 1001))
    client = client_for(instance, max_body_bytes=1000)
    with pytest.raises(ApiError) as excinfo:
        client.request("GET", "/", deadline=Deadline(5), headers=NO_HEADERS)
    assert (excinfo.value.category, excinfo.value.code) == (
        "response",
        "body_too_large",
    )
    assert instance.wait_closed() is True


def test_oversized_body_without_content_length_is_rejected_while_reading(
    server: Callable[..., LoopbackServer],
) -> None:
    def handler(conn: socket.socket, request: bytes) -> None:
        send_all(conn, b"HTTP/1.1 200 OK\r\nConnection: close\r\n\r\n" + b"y" * 1001)

    instance = server(handler)
    client = client_for(instance, max_body_bytes=1000)
    with pytest.raises(ApiError) as excinfo:
        client.request("GET", "/", deadline=Deadline(5), headers=NO_HEADERS)
    assert excinfo.value.code == "body_too_large"


def test_body_at_the_limit_is_accepted(server: Callable[..., LoopbackServer]) -> None:
    instance = server(respond(200, b"z" * 1000))
    response = client_for(instance, max_body_bytes=1000).request(
        "GET", "/", deadline=Deadline(5), headers=NO_HEADERS
    )
    assert len(response.body) == 1000


def test_compressed_response_is_rejected_and_closed(
    server: Callable[..., LoopbackServer],
) -> None:
    instance = server(respond(200, gzip.compress(b"{}"), {"Content-Encoding": "gzip"}))
    with pytest.raises(ApiError) as excinfo:
        client_for(instance).request(
            "GET", "/", deadline=Deadline(5), headers=NO_HEADERS
        )
    assert (excinfo.value.category, excinfo.value.code) == (
        "response",
        "content_encoding",
    )
    assert instance.wait_closed() is True


def test_short_body_is_a_protocol_failure(
    server: Callable[..., LoopbackServer],
) -> None:
    def handler(conn: socket.socket, request: bytes) -> None:
        send_all(
            conn,
            b"HTTP/1.1 200 OK\r\nContent-Length: 100\r\nConnection: close\r\n\r\nshort",
        )

    instance = server(handler)
    with pytest.raises(ApiError) as excinfo:
        client_for(instance).request(
            "GET", "/", deadline=Deadline(5), headers=NO_HEADERS
        )
    assert (excinfo.value.category, excinfo.value.code) == (
        "transport",
        "http_protocol",
    )


def test_chunked_body_is_read_and_a_broken_chunk_fails(
    server: Callable[..., LoopbackServer],
) -> None:
    def good(conn: socket.socket, request: bytes) -> None:
        send_all(
            conn,
            b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n5\r\nhello\r\n1\r\n!\r\n0\r\n\r\n",
        )

    def broken(conn: socket.socket, request: bytes) -> None:
        send_all(conn, b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n5\r\nhel")

    assert (
        client_for(server(good))
        .request("GET", "/", deadline=Deadline(5), headers=NO_HEADERS)
        .body
        == b"hello!"
    )
    with pytest.raises(ApiError) as excinfo:
        client_for(server(broken)).request(
            "GET", "/", deadline=Deadline(5), headers=NO_HEADERS
        )
    assert excinfo.value.code in {"http_protocol", "io_error"}


def test_garbage_status_line_is_a_protocol_failure(
    server: Callable[..., LoopbackServer],
) -> None:
    def handler(conn: socket.socket, request: bytes) -> None:
        send_all(conn, b"NOT HTTP AT ALL\r\n\r\n")

    with pytest.raises(ApiError) as excinfo:
        client_for(server(handler)).request(
            "GET", "/", deadline=Deadline(5), headers=NO_HEADERS
        )
    assert (excinfo.value.category, excinfo.value.code) == (
        "transport",
        "http_protocol",
    )


def test_connection_closed_without_response_is_a_connection_failure(
    server: Callable[..., LoopbackServer],
) -> None:
    def handler(conn: socket.socket, request: bytes) -> None:
        conn.close()

    with pytest.raises(ApiError) as excinfo:
        client_for(server(handler)).request(
            "GET", "/", deadline=Deadline(5), headers=NO_HEADERS
        )
    assert excinfo.value.category == "transport"
    assert excinfo.value.code in {"connection_failed", "io_error"}


def test_refused_connection_is_a_connection_failure() -> None:
    probe = socket.socket()
    probe.bind(("127.0.0.1", 0))
    port = probe.getsockname()[1]
    probe.close()
    client = HttpClient(f"http://127.0.0.1:{port}", user_agent=AGENT, timeout=2)
    with pytest.raises(ApiError) as excinfo:
        client.request("GET", "/", deadline=Deadline(5), headers=NO_HEADERS)
    assert (excinfo.value.category, excinfo.value.code) == (
        "transport",
        "connection_failed",
    )


def test_stalled_header_times_out_by_socket_timeout(
    server: Callable[..., LoopbackServer],
) -> None:
    instance = server(lambda conn, request: hold(conn, 3))
    client = client_for(instance, timeout=0.5)
    error, elapsed = timed_failure(
        lambda: client.request("GET", "/", deadline=Deadline(10), headers=NO_HEADERS),
        TransportTimeout,
    )
    report("stalled header, timeout 0.5 s, deadline 10 s", elapsed)
    assert error.code == "socket_timeout"
    assert 0.4 <= elapsed < 5.0


def test_stalled_header_times_out_by_the_shorter_deadline(
    server: Callable[..., LoopbackServer],
) -> None:
    instance = server(lambda conn, request: hold(conn, 3))
    client = client_for(instance, timeout=30)
    error, elapsed = timed_failure(
        lambda: client.request("GET", "/", deadline=Deadline(0.5), headers=NO_HEADERS),
        TransportTimeout,
    )
    report("stalled header, timeout 30 s, deadline 0.5 s", elapsed)
    assert error.code == "socket_timeout"
    assert 0.4 <= elapsed < 5.0


def test_trickled_header_overruns_the_budget_as_documented(
    server: Callable[..., LoopbackServer],
) -> None:
    head = response_bytes(200, b"ok", {"X-Padding": "p" * 20})

    instance = server(lambda conn, request: trickle(conn, head, 0.02))
    client = client_for(instance, timeout=5)
    deadline = Deadline(0.5)
    error, elapsed = timed_failure(
        lambda: client.request("GET", "/", deadline=deadline, headers=NO_HEADERS),
        TransportTimeout,
    )
    report(f"trickled header of {len(head)} bytes at 20 ms, deadline 0.5 s", elapsed)
    # The header arrives one byte per 20 ms, so the socket timeout never fires
    # and the budget is only checked once the header is complete.
    assert error.code == "deadline_exceeded"
    assert elapsed > 0.5


def test_trickled_body_stops_at_the_budget(
    server: Callable[..., LoopbackServer],
) -> None:
    def handler(conn: socket.socket, request: bytes) -> None:
        send_all(
            conn, b"HTTP/1.1 200 OK\r\nContent-Length: 100\r\nConnection: close\r\n\r\n"
        )
        trickle(conn, b"b" * 100, 0.1)

    instance = server(handler)
    client = client_for(instance, timeout=5)
    error, elapsed = timed_failure(
        lambda: client.request("GET", "/", deadline=Deadline(0.5), headers=NO_HEADERS),
        TransportTimeout,
    )
    report("trickled body at 100 ms per byte, deadline 0.5 s", elapsed)
    assert error.code == "deadline_exceeded"
    assert 0.5 <= elapsed < 5.0
    assert instance.wait_closed() is True


def test_stalled_body_overruns_by_at_most_the_socket_timeout(
    server: Callable[..., LoopbackServer],
) -> None:
    def handler(conn: socket.socket, request: bytes) -> None:
        send_all(
            conn,
            b"HTTP/1.1 200 OK\r\nContent-Length: 10\r\nConnection: close\r\n\r\nab",
        )
        hold(conn, 3)

    instance = server(handler)
    client = client_for(instance, timeout=0.6)
    error, elapsed = timed_failure(
        lambda: client.request("GET", "/", deadline=Deadline(0.3), headers=NO_HEADERS),
        TransportTimeout,
    )
    report("stalled body, timeout 0.6 s, deadline 0.3 s", elapsed)
    # The socket timeout at open time was min(0.6, 0.3), so the stall is
    # noticed within the budget plus one read.
    assert error.code in {"socket_timeout", "deadline_exceeded"}
    assert elapsed < 5.0


@pytest.mark.parametrize(
    ("body", "expected"),
    [
        (b"{}", {}),
        (b'{"rows": [1, "x", null]}', {"rows": [1, "x", None]}),
        (b"[]", []),
        (b"null", None),
        (bytearray(b'"\\u00e4"'), "\u00e4"),
    ],
)
def test_decode_json_returns_the_document(body: bytes, expected: Any) -> None:
    assert decode_json(body) == expected


@pytest.mark.parametrize(
    ("body", "code"),
    [
        (b"\xff\xfe{}", "invalid_utf8"),
        (b"", "invalid_json"),
        (b"{", "invalid_json"),
        (b"<html>not json</html>", "invalid_json"),
    ],
)
def test_decode_json_failures_are_local_response_errors(body: bytes, code: str) -> None:
    with pytest.raises(ApiError) as excinfo:
        decode_json(body)
    assert (excinfo.value.category, excinfo.value.code) == ("response", code)
    assert excinfo.value.__cause__ is None
    assert "html" not in str(excinfo.value)
    assert code in ERROR_CODES["response"]


def test_decode_json_rejects_non_bytes() -> None:
    with pytest.raises(TypeError):
        decode_json("{}")  # type: ignore[arg-type]


def test_deep_json_never_leaks_parser_recursion() -> None:
    body = b"[" * 20000 + b"0" + b"]" * 20000
    assert len(body) < http_module.DEFAULT_MAX_BODY_BYTES
    try:
        document = decode_json(body)
    except ApiError as error:
        assert (error.category, error.code) == ("response", "invalid_json")
        assert error.__suppress_context__
    else:
        # Some runtimes can parse this depth; no helper nesting policy is imposed.
        for _index in range(20000):
            assert isinstance(document, list) and len(document) == 1
            document = document[0]
        assert document == 0


def test_parser_recursion_is_a_local_response_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def exhausted_parser(document: str) -> Any:
        raise RecursionError("SYNTHETIC-PARSER-SECRET")

    monkeypatch.setattr(http_module.json, "loads", exhausted_parser)
    with pytest.raises(ApiError) as caught:
        decode_json(b"[]")
    assert (caught.value.category, caught.value.code) == ("response", "invalid_json")
    assert caught.value.__suppress_context__
    assert "SYNTHETIC-PARSER-SECRET" not in str(caught.value)


@pytest.mark.parametrize("length", [3, 1000])
def test_not_modified_length_describes_the_representation(
    server: Callable[..., LoopbackServer], length: int
) -> None:
    def handler(conn: socket.socket, request: bytes) -> None:
        send_all(
            conn,
            (
                f"HTTP/1.1 304 Not Modified\r\nContent-Length: {length}\r\n"
                "Connection: close\r\n\r\n"
            ).encode("ascii"),
        )

    instance = server(handler)
    result = client_for(instance, max_body_bytes=8).request(
        "GET", "/", deadline=Deadline(5), headers={"If-None-Match": "test"}
    )
    assert result.status == 304 and result.body == b""
    assert result.headers["content-length"] == str(length)
    assert instance.wait_closed()


@pytest.mark.parametrize(
    "encodings",
    [
        ["identity", "gzip"],
        ["gzip", "identity"],
        ["identity, gzip"],
        ["gzip, identity"],
        ["identity", "identity"],
    ],
)
def test_every_content_encoding_is_checked_before_reading(
    monkeypatch: pytest.MonkeyPatch, encodings: list[str]
) -> None:
    response = FakeResponse(200, b"")
    raw = Message()
    for encoding in encodings:
        raw["Content-Encoding"] = encoding
    response.headers = raw  # type: ignore[assignment]
    monkeypatch.setattr(http_module, "open_url", lambda *args, **kwargs: response)
    if encodings == ["identity", "identity"]:
        result = HttpClient("https://host", user_agent=AGENT).request(
            "GET", "/", deadline=Deadline(5), headers={}
        )
        assert result.headers["content-encoding"] == "identity, identity"
    else:

        def unexpected_read(size: int) -> bytes:
            pytest.fail("Encoded responses must be refused before reading")

        monkeypatch.setattr(response, "read1", unexpected_read)
        with pytest.raises(ApiError) as caught:
            HttpClient("https://host", user_agent=AGENT).request(
                "GET", "/", deadline=Deadline(5), headers={}
            )
        assert caught.value.code == "content_encoding"
    assert response.closed_count == 1


def test_declared_oversize_never_reads(monkeypatch: pytest.MonkeyPatch) -> None:
    response = FakeResponse(200, b"", {"Content-Length": "9"})

    def unexpected_read(size: int) -> bytes:
        pytest.fail("Declared oversize must be refused before the first read")

    monkeypatch.setattr(response, "read1", unexpected_read)
    monkeypatch.setattr(http_module, "open_url", lambda *args, **kwargs: response)
    with pytest.raises(ApiError) as caught:
        HttpClient("https://host", user_agent=AGENT, max_body_bytes=8).request(
            "GET", "/", deadline=Deadline(5), headers={}
        )
    assert caught.value.code == "body_too_large"
    assert response.closed_count == 1


@pytest.mark.parametrize("expire_at", ["before", "after"])
def test_read_budget_checks_and_close_are_deterministic(
    monkeypatch: pytest.MonkeyPatch, expire_at: str
) -> None:
    now = [0.0]
    monkeypatch.setattr(
        "ansible_collections.foundata.utils.plugins.module_utils.deadline.time.monotonic",
        lambda: now[0],
    )
    deadline = Deadline(1)
    response = FakeResponse(200, b"abc")
    reads: list[int] = []

    def read(size: int) -> bytes:
        reads.append(size)
        now[0] = 2.0
        return b"abc"

    def open_response(*args: Any, **kwargs: Any) -> FakeResponse:
        if expire_at == "before":
            now[0] = 2.0
        return response

    monkeypatch.setattr(response, "read1", read)
    monkeypatch.setattr(http_module, "open_url", open_response)
    with pytest.raises(TransportTimeout) as caught:
        HttpClient("https://host", user_agent=AGENT).request(
            "GET", "/", deadline=deadline, headers={}
        )
    assert caught.value.code == "deadline_exceeded"
    assert len(reads) == (0 if expire_at == "before" else 1)
    assert response.closed_count == 1
