import asyncio
import gzip
import socket
import zlib
from unittest.mock import AsyncMock, MagicMock, Mock

import aiohttp
import pytest
from yarl import URL

from core import urlfetcher


@pytest.fixture
def dns_resolver(monkeypatch):
    resolver = Mock(resolve=AsyncMock(return_value=[{"host": "93.184.216.34"}]), close=AsyncMock())
    monkeypatch.setattr(aiohttp.resolver, "DefaultResolver", Mock(return_value=resolver))
    return resolver


@pytest.fixture
def http_session(monkeypatch, dns_resolver):
    session = MagicMock()
    session.__aenter__.return_value = session
    factory = Mock(return_value=session)

    async def close_session(*args):
        await factory.call_args.kwargs["connector"].close()

    session.__aexit__.side_effect = close_session
    monkeypatch.setattr(aiohttp, "ClientSession", factory)
    return factory


def make_response(text="", status=200, headers=None, *, body=None):
    response = MagicMock(status=status, headers=headers or {})
    response.__aenter__.return_value = response
    response.text = AsyncMock(side_effect=AssertionError("Unexpected unbounded text read"))
    response.read = AsyncMock(side_effect=AssertionError("Unexpected unbounded body read"))
    content_type = aiohttp.helpers.parse_mimetype(response.headers.get("Content-Type", "application/octet-stream"))
    response.content_type = f"{content_type.type}/{content_type.subtype}"
    if content_type.suffix:
        response.content_type += f"+{content_type.suffix}"
    response.charset = content_type.parameters.get("charset")
    content_length = response.headers.get("Content-Length")
    response.content_length = int(content_length) if content_length is not None else None
    content = aiohttp.StreamReader(MagicMock(_reading_paused=False), limit=urlfetcher.RESPONSE_CHUNK_BYTES)
    content.feed_data(text.encode("utf-8") if body is None else body)
    content.feed_eof()
    response.content = Mock(wraps=content)
    response.content.read = AsyncMock(wraps=content.read)
    return response


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "url",
    [
        "file:///etc/passwd",
        "ftp://example.com/file",
        "/relative/path",
        "http:///missing-host",
        "https://user:secret@example.com/",
        "https://@example.com/",
        "https://:secret@example.com/",
        "http://example.com:0/",
        "http://example.com:65536/",
        "http://example.com:invalid/",
        "\nhttp://example.com/",
        "http://127.0.0.1/",
        "http://127.1/",
        "http://2130706433/",
        "http://0177.0.0.1/",
        "http://127.0.0.1./",
        "http://999.999.999.999/",
        "http://10.0.0.1/",
        "http://169.254.169.254/",
        "http://100.64.0.1/",
        "http://0.0.0.0/",
        "http://192.0.2.1/",
        "http://224.0.0.1/",
        "http://240.0.0.1/",
        "http://[::1]/",
        "http://[::]/",
        "http://[fc00::1]/",
        "http://[fe80::1]/",
        "http://[fec0::1]/",
        "http://[ff02::1]/",
        "http://[2001:db8::1]/",
        "http://[2002:7f00:1::1]/",
        "http://[64:ff9b::7f00:1]/",
        "http://[2606:4700:4700::1111%25en0]/",
        "http://[::ffff:127.0.0.1]/",
        "http://[::ffff:7f00:1]/",
        "http://[::ffff:10.0.0.1]/",
    ],
)
async def test_unsafe_url_never_opens_session(monkeypatch, url):
    session = Mock(side_effect=AssertionError("Unexpected HTTP request"))
    monkeypatch.setattr(urlfetcher.aiohttp, "ClientSession", session)

    assert await urlfetcher.extract_url_content(url) is None
    session.assert_not_called()


def test_find_urls():
    assert urlfetcher.find_urls("Read https://example.com/a and http://example.org/b?q=1") == [
        "https://example.com/a",
        "http://example.org/b?q=1",
    ]
    assert urlfetcher.find_urls("No URL here") == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "addresses",
    [
        [],
        ["127.0.0.1"],
        ["93.184.216.34", "10.0.0.1"],
        ["10.0.0.1", "93.184.216.34"],
        ["2606:4700:4700::1111", "::1"],
        ["::ffff:169.254.169.254"],
        ["100.64.0.1"],
        ["224.0.0.1"],
        ["fec0::1"],
        ["2002:7f00:1::1"],
        ["64:ff9b::7f00:1"],
        ["2606:4700:4700::1111%en0"],
        ["not-an-address"],
    ],
)
async def test_resolver_rejects_entire_unsafe_answer_set(dns_resolver, addresses):
    dns_resolver.resolve.return_value = [{"host": address} for address in addresses]
    resolver = urlfetcher.PublicResolver()
    try:
        with pytest.raises((OSError, ValueError)):
            await resolver.resolve("example.com", 443, family=socket.AF_UNSPEC)
    finally:
        await resolver.close()
    dns_resolver.close.assert_awaited_once()


@pytest.mark.asyncio
async def test_resolver_returns_only_numeric_public_destinations(dns_resolver):
    dns_resolver.resolve.return_value = [{"host": "93.184.216.34"}, {"host": "2606:4700:4700::1111"}]
    resolver = urlfetcher.PublicResolver()
    try:
        destinations = await resolver.resolve("example.com", 8443, family=socket.AF_UNSPEC)
    finally:
        await resolver.close()
    assert [destination["host"] for destination in destinations] == ["93.184.216.34", "2606:4700:4700::1111"]
    assert [destination["family"] for destination in destinations] == [socket.AF_INET, socket.AF_INET6]
    assert all(destination["hostname"] == "example.com" and destination["port"] == 8443 for destination in destinations)
    assert all(destination["flags"] & socket.AI_NUMERICHOST for destination in destinations)
    dns_resolver.resolve.assert_awaited_once_with("example.com", 8443, family=socket.AF_UNSPEC)


@pytest.mark.asyncio
async def test_connector_uses_validated_address_without_second_dns_lookup(monkeypatch, dns_resolver):
    dns_resolver.resolve.side_effect = [[{"host": "93.184.216.34"}], [{"host": "127.0.0.1"}]]
    destinations = []

    async def stop_before_socket(*, addr_infos, **kwargs):
        destinations.extend(address_info[4][0] for address_info in addr_infos)
        raise OSError("Stopped before socket creation")

    connect = AsyncMock(side_effect=stop_before_socket)
    monkeypatch.setattr(aiohttp.connector.aiohappyeyeballs, "start_connection", connect)

    assert await urlfetcher.extract_url_content("https://example.com/") is None
    assert destinations == ["93.184.216.34"]
    dns_resolver.resolve.assert_awaited_once_with("example.com", 443, family=socket.AF_UNSPEC)
    dns_resolver.close.assert_awaited_once()


@pytest.mark.asyncio
async def test_connector_rejects_mixed_dns_before_socket_creation(monkeypatch, dns_resolver):
    dns_resolver.resolve.return_value = [{"host": "93.184.216.34"}, {"host": "127.0.0.1"}]
    connect = AsyncMock(side_effect=AssertionError("Unexpected socket creation"))
    monkeypatch.setattr(aiohttp.connector.aiohappyeyeballs, "start_connection", connect)

    assert await urlfetcher.extract_url_content("https://example.com/") is None
    connect.assert_not_awaited()
    dns_resolver.close.assert_awaited_once()


@pytest.mark.asyncio
async def test_public_response(http_session, dns_resolver):
    http_session.return_value.get.return_value = make_response("<p>Hello <b>world</b> &amp; more</p>")

    assert await urlfetcher.extract_url_content("https://example.com/") == "Hello world & more"
    http_session.return_value.get.assert_called_once_with(URL("https://example.com/"), allow_redirects=False)
    assert isinstance(http_session.call_args.kwargs["connector"]._resolver, urlfetcher.PublicResolver)
    assert http_session.call_args.kwargs["trust_env"] is False
    assert isinstance(http_session.call_args.kwargs["cookie_jar"], aiohttp.DummyCookieJar)
    assert http_session.call_args.kwargs["auto_decompress"] is False
    assert http_session.call_args.kwargs["headers"]["Accept-Encoding"] == "gzip, deflate"
    dns_resolver.close.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "headers",
    [None, {"Transfer-Encoding": "chunked"}, {"Content-Length": "1"}, {"Content-Length": "0"}],
)
async def test_oversize_unannounced_body_is_rejected(monkeypatch, http_session, dns_resolver, headers):
    response = make_response("x" * (urlfetcher.MAX_RESPONSE_BYTES + urlfetcher.RESPONSE_CHUNK_BYTES), headers=headers)
    http_session.return_value.get.return_value = response
    extractor = Mock()
    monkeypatch.setattr(urlfetcher, "_HTMLTextExtractor", extractor)

    assert await urlfetcher.extract_url_content("https://example.com/", max_chars=1) is None
    read_sizes = [call.args[0] for call in response.content.read.await_args_list]
    assert read_sizes and all(0 < size <= urlfetcher.RESPONSE_CHUNK_BYTES for size in read_sizes)
    assert sum(read_sizes) == urlfetcher.MAX_RESPONSE_BYTES + 1
    assert not response.content.at_eof()
    extractor.assert_not_called()
    response.text.assert_not_awaited()
    response.read.assert_not_awaited()
    response.__aexit__.assert_awaited_once()
    dns_resolver.close.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("encoding", ["identity", "gzip"])
async def test_declared_oversize_body_is_not_read(http_session, encoding):
    response = make_response(
        "Unread body",
        headers={"Content-Length": str(urlfetcher.MAX_RESPONSE_BYTES + 1), "Content-Encoding": encoding},
    )
    http_session.return_value.get.return_value = response

    assert await urlfetcher.extract_url_content("https://example.com/") is None
    response.content.read.assert_not_awaited()
    response.__aexit__.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "headers", [None, {"Transfer-Encoding": "chunked"}, {"Content-Length": str(urlfetcher.MAX_RESPONSE_BYTES)}]
)
async def test_exact_byte_limit_is_accepted(http_session, headers):
    response = make_response("x" * urlfetcher.MAX_RESPONSE_BYTES, headers=headers)
    http_session.return_value.get.return_value = response

    assert await urlfetcher.extract_url_content("https://example.com/", max_chars=7) == "xxxxxxx"
    read_sizes = [call.args[0] for call in response.content.read.await_args_list]
    assert all(0 < size <= urlfetcher.RESPONSE_CHUNK_BYTES for size in read_sizes)
    assert max(read_sizes) == urlfetcher.RESPONSE_CHUNK_BYTES
    assert read_sizes[-1] == 1
    assert response.content.at_eof()


@pytest.mark.asyncio
@pytest.mark.parametrize(("encoding", "compress"), [("gzip", gzip.compress), ("deflate", zlib.compress)])
@pytest.mark.parametrize("extra_bytes", [-1, 0, 1])
async def test_decompressed_byte_budget(monkeypatch, http_session, encoding, compress, extra_bytes):
    compressed_body = compress(b"x" * (urlfetcher.MAX_RESPONSE_BYTES + extra_bytes))
    assert len(compressed_body) < urlfetcher.MAX_RESPONSE_BYTES
    response = make_response(
        headers={"Content-Encoding": encoding, "Content-Length": str(len(compressed_body))}, body=compressed_body
    )
    http_session.return_value.get.return_value = response
    extractor = Mock(wraps=urlfetcher._HTMLTextExtractor)
    monkeypatch.setattr(urlfetcher, "_HTMLTextExtractor", extractor)

    result = await urlfetcher.extract_url_content("https://example.com/", max_chars=7)

    if extra_bytes > 0:
        assert result is None
        extractor.assert_not_called()
        assert response.content.read.await_count == 1
    else:
        assert result == "xxxxxxx"
        extractor.assert_called_once()


@pytest.mark.asyncio
async def test_decompression_output_chunks_are_bounded(monkeypatch, http_session):
    compressed_body = gzip.compress(b"x" * (urlfetcher.MAX_RESPONSE_BYTES + 1))
    real_decompressor = zlib.decompressobj(16 + zlib.MAX_WBITS)
    decompressor = Mock(eof=False, unconsumed_tail=b"", unused_data=b"")

    def decompress(data, max_length=0):
        decoded = real_decompressor.decompress(data, max_length)
        decompressor.eof = real_decompressor.eof
        decompressor.unconsumed_tail = real_decompressor.unconsumed_tail
        decompressor.unused_data = real_decompressor.unused_data
        return decoded

    decompressor.decompress.side_effect = decompress
    monkeypatch.setattr(urlfetcher.zlib, "decompressobj", Mock(return_value=decompressor))
    http_session.return_value.get.return_value = make_response(
        headers={"Content-Encoding": "gzip"}, body=compressed_body
    )

    assert await urlfetcher.extract_url_content("https://example.com/") is None
    output_limits = [call.args[1] for call in decompressor.decompress.call_args_list]
    assert len(output_limits) > 1
    assert all(0 < limit <= urlfetcher.RESPONSE_CHUNK_BYTES for limit in output_limits)
    assert output_limits[-1] == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(("encoding", "window_bits"), [("gzip", 16 + zlib.MAX_WBITS), ("deflate", zlib.MAX_WBITS)])
async def test_compressed_wire_bytes_are_also_bounded(monkeypatch, http_session, encoding, window_bits):
    monkeypatch.setattr(urlfetcher, "MAX_RESPONSE_BYTES", 128)
    compressor = zlib.compressobj(wbits=window_bits)
    compressed_body = b"".join(compressor.compress(b"x") + compressor.flush(zlib.Z_SYNC_FLUSH) for _ in range(64))
    compressed_body += compressor.flush()
    assert len(compressed_body) > urlfetcher.MAX_RESPONSE_BYTES
    assert zlib.decompress(compressed_body, window_bits) == b"x" * 64
    response = make_response(headers={"Content-Encoding": encoding}, body=compressed_body)
    http_session.return_value.get.return_value = response

    assert await urlfetcher.extract_url_content("https://example.com/") is None
    response.content.read.assert_awaited_once_with(urlfetcher.MAX_RESPONSE_BYTES + 1)
    assert not response.content.at_eof()


@pytest.mark.asyncio
@pytest.mark.parametrize(("encoding", "compress"), [("gzip", gzip.compress), ("deflate", zlib.compress)])
@pytest.mark.parametrize("damage", ["invalid", "truncated", "trailing", "concatenated", "checksum"])
async def test_invalid_compressed_body_fails_closed(http_session, encoding, compress, damage):
    compressed_body = compress(b"<p>Developer</p>")
    if damage == "invalid":
        compressed_body = b"Not compressed data"
    elif damage == "truncated":
        compressed_body = compressed_body[:-1]
    elif damage == "trailing":
        compressed_body += b"Trailing data"
    elif damage == "concatenated":
        compressed_body += compress(b"<p>Extra data</p>")
    else:
        compressed_body = compressed_body[:-1] + bytes([compressed_body[-1] ^ 255])
    http_session.return_value.get.return_value = make_response(
        headers={"Content-Encoding": encoding}, body=compressed_body
    )

    assert await urlfetcher.extract_url_content("https://example.com/") is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "content_type", [None, "text/html", "text/plain", "text/markdown", "application/xhtml+xml", "TEXT/HTML"]
)
async def test_textual_content_types_are_accepted(http_session, content_type):
    headers = {"Content-Type": content_type} if content_type is not None else None
    http_session.return_value.get.return_value = make_response("<p>Developer</p>", headers=headers)

    assert await urlfetcher.extract_url_content("https://example.com/") == "Developer"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "headers",
    [
        {"Content-Type": "application/pdf"},
        {"Content-Type": "image/png"},
        {"Content-Type": "application/octet-stream"},
        {"Content-Encoding": "br"},
        {"Content-Encoding": "gzip, deflate"},
    ],
)
async def test_unsupported_content_is_not_read(http_session, headers):
    response = make_response("<p>Unread body</p>", headers=headers)
    http_session.return_value.get.return_value = response

    assert await urlfetcher.extract_url_content("https://example.com/") is None
    response.content.read.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("content_type", "body", "expected"),
    [
        ("text/html", b"<p>caf\xc3\xa9</p>", "caf\u00e9"),
        ("text/html; charset=windows-1252", b"<p>caf\xe9</p>", "caf\u00e9"),
        ("text/html; charset=invalid-charset", b"<p>caf\xc3\xa9</p>", "caf\u00e9"),
        ("text/html; charset=base64_codec", b"<p>caf\xc3\xa9</p>", "caf\u00e9"),
        ("text/plain; charset=utf-8", b"bad\xffbyte", "bad\ufffdbyte"),
        ("application/xhtml+xml; charset=utf-16", "<p>Developer</p>".encode("utf-16"), "Developer"),
    ],
)
async def test_response_charset_and_fallback(http_session, content_type, body, expected):
    http_session.return_value.get.return_value = make_response(headers={"Content-Type": content_type}, body=body)

    assert await urlfetcher.extract_url_content("https://example.com/") == expected


@pytest.mark.asyncio
async def test_multibyte_character_split_across_chunks(http_session):
    text = "x" * (urlfetcher.RESPONSE_CHUNK_BYTES - 1) + "\u00e9"
    http_session.return_value.get.return_value = make_response(text)

    assert await urlfetcher.extract_url_content("https://example.com/", max_chars=len(text)) == text


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("html", "max_chars", "expected"),
    [
        (
            "<head><style>Hidden</style></head><p>Developer</p><script>Hidden</script><noscript>Hidden</noscript>",
            None,
            "Developer",
        ),
        ("<p>A</p>\n<p> B &amp; C </p>", None, "A B & C"),
        ("<p>" + "x" * 9000 + "</p>", None, "x" * 8000),
        ("<p>Developer</p>", 3, "Dev"),
        ("<p>Developer</p>", 0, ""),
        ("<script>Hidden</script>", None, None),
        ("", None, None),
    ],
)
async def test_text_extraction_and_max_chars_contract(http_session, html, max_chars, expected):
    http_session.return_value.get.return_value = make_response(html)

    if max_chars is None:
        assert await urlfetcher.extract_url_content("https://example.com/") == expected
    else:
        assert await urlfetcher.extract_url_content("https://example.com/", max_chars=max_chars) == expected


@pytest.mark.asyncio
@pytest.mark.parametrize("error", [aiohttp.ClientPayloadError, OSError, asyncio.TimeoutError])
async def test_stream_error_does_not_return_partial_content(monkeypatch, http_session, dns_resolver, error):
    response = make_response()
    response.content.read.side_effect = [b"<p>Partial", error("Stream failed")]
    http_session.return_value.get.return_value = response
    extractor = Mock()
    monkeypatch.setattr(urlfetcher, "_HTMLTextExtractor", extractor)

    assert await urlfetcher.extract_url_content("https://example.com/") is None
    assert response.content.read.await_count == 2
    extractor.assert_not_called()
    response.__aexit__.assert_awaited_once()
    dns_resolver.close.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [301, 302, 303, 307, 308])
async def test_public_relative_redirect(http_session, status):
    http_session.return_value.get.side_effect = [
        make_response(status=status, headers={"Location": "../next"}),
        make_response("<p>Redirected</p>"),
    ]

    assert await urlfetcher.extract_url_content("https://example.com/start/page") == "Redirected"
    assert [request.args[0] for request in http_session.return_value.get.call_args_list] == [
        URL("https://example.com/start/page"),
        URL("https://example.com/next"),
    ]
    assert all(request.kwargs["allow_redirects"] is False for request in http_session.return_value.get.call_args_list)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "location",
    [
        "http://127.0.0.1/",
        "//169.254.169.254/",
        "http://[::ffff:127.0.0.1]/",
        "file:///etc/passwd",
        "https://user:secret@example.com/",
        "https://@example.com/",
        "",
    ],
)
async def test_unsafe_redirect_is_never_requested(http_session, location):
    http_session.return_value.get.return_value = make_response(status=302, headers={"Location": location})

    assert await urlfetcher.extract_url_content("https://example.com/") is None
    assert http_session.return_value.get.call_count == 1


@pytest.mark.asyncio
async def test_redirect_limit(http_session):
    http_session.return_value.get.return_value = make_response(status=302, headers={"Location": "/again"})

    assert await urlfetcher.extract_url_content("https://example.com/") is None
    assert http_session.return_value.get.call_count == urlfetcher.MAX_REDIRECTS + 1


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [201, 204, 400, 404, 500])
async def test_non_200_response_is_not_read(http_session, status):
    response = make_response("Not a successful document", status=status)
    http_session.return_value.get.return_value = response

    assert await urlfetcher.extract_url_content("https://example.com/") is None
    response.text.assert_not_awaited()
    response.content.read.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("partial_body", [False, True])
@pytest.mark.parametrize("redirect", [False, True])
async def test_timeout_covers_response_streaming(monkeypatch, http_session, dns_resolver, partial_body, redirect):
    monkeypatch.setattr(urlfetcher, "FETCH_TIMEOUT_SECONDS", 0.01)
    response = make_response()
    cancelled = False

    async def stalled_body(size):
        nonlocal cancelled
        if partial_body and response.content.read.await_count == 1:
            return b"<p>Partial"
        try:
            await asyncio.Future()
        finally:
            cancelled = True

    response.content.read.side_effect = stalled_body
    responses = [make_response(status=302, headers={"Location": "/next"})] if redirect else []
    http_session.return_value.get.side_effect = responses + [response]

    assert await urlfetcher.extract_url_content("https://example.com/") is None
    assert http_session.return_value.get.call_count == (2 if redirect else 1)
    assert response.content.read.await_count == (2 if partial_body else 1)
    assert cancelled
    response.__aexit__.assert_awaited_once()
    dns_resolver.close.assert_awaited_once()
