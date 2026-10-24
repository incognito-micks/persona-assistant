import asyncio
import ipaddress
import re
import socket
import zlib
from html.parser import HTMLParser
from urllib.parse import urlsplit

import aiohttp
from aiohttp.abc import AbstractResolver, ResolveResult
from yarl import URL

URL_PATTERN = re.compile(r"https?://\S+")
FETCH_TIMEOUT_SECONDS = 10
MAX_REDIRECTS = 3
MAX_RESPONSE_BYTES = 2 * 1024 * 1024
RESPONSE_CHUNK_BYTES = 64 * 1024
REDIRECT_STATUSES = {301, 302, 303, 307, 308}


def is_public_address(address: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    if isinstance(address, ipaddress.IPv6Address):
        if address.scope_id is not None:
            return False
        if address.ipv4_mapped is not None:
            address = address.ipv4_mapped
        elif address.is_site_local or address.sixtofour is not None or address.teredo is not None:
            return False
    return address.is_global and not address.is_multicast and not address.is_reserved


def validate_url(url: str | URL, base: URL | None = None) -> URL:
    if any(ord(character) <= 32 or ord(character) == 127 for character in str(url)):
        raise ValueError("Invalid URL characters")
    if urlsplit(str(url)).username is not None:
        raise ValueError("URL credentials are not allowed")
    parsed = URL(url)
    if base is not None:
        parsed = base.join(parsed)
    host = parsed.raw_host
    if parsed.scheme not in {"http", "https"} or not host:
        raise ValueError("Only absolute HTTP(S) URLs are allowed")
    if parsed.raw_user is not None or parsed.raw_password is not None or "%" in host:
        raise ValueError("Credentials and scoped addresses are not allowed")
    port = parsed.port
    if port is None or not 0 < port <= 65535:
        raise ValueError("Invalid URL port")
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        if ":" in host or host.replace(".", "").isdigit():
            raise ValueError("Invalid IP address") from None
    else:
        if not is_public_address(address):
            raise ValueError("Destination must be a public address")
    return parsed.with_fragment(None)


class PublicResolver(AbstractResolver):
    """Validate all DNS answers and return numeric destinations to the connector."""

    def __init__(self) -> None:
        self.resolver = aiohttp.resolver.DefaultResolver()

    async def resolve(
        self, host: str, port: int = 0, family: socket.AddressFamily = socket.AF_INET
    ) -> list[ResolveResult]:
        answers = await self.resolver.resolve(host, port, family=family)
        if not answers:
            raise OSError("No destination addresses")
        destinations = []
        for answer in answers:
            address = ipaddress.ip_address(answer["host"])
            if not is_public_address(address):
                raise OSError("Destination must be a public address")
            destinations.append(
                ResolveResult(
                    hostname=host,
                    host=address.compressed,
                    port=port,
                    family=socket.AF_INET if address.version == 4 else socket.AF_INET6,
                    proto=socket.IPPROTO_TCP,
                    flags=socket.AI_NUMERICHOST | socket.AI_NUMERICSERV,
                )
            )
        return destinations

    async def close(self) -> None:
        await self.resolver.close()


class _HTMLTextExtractor(HTMLParser):
    """Simple HTML to plain text extractor that strips tags."""

    SKIP_TAGS = {"script", "style", "noscript", "head"}

    def __init__(self):
        super().__init__()
        self._pieces: list[str] = []
        self._skip_depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]):
        if tag.lower() in self.SKIP_TAGS:
            self._skip_depth += 1

    def handle_endtag(self, tag: str):
        if tag.lower() in self.SKIP_TAGS and self._skip_depth > 0:
            self._skip_depth -= 1

    def handle_data(self, data: str):
        if self._skip_depth == 0:
            self._pieces.append(data)

    def get_text(self) -> str:
        raw = " ".join(self._pieces)
        # Collapse whitespace
        return re.sub(r"\s+", " ", raw).strip()


def find_urls(text: str) -> list[str]:
    """Find all URLs in text."""
    return URL_PATTERN.findall(text)


async def read_response(response: aiohttp.ClientResponse) -> str | None:
    """Bound both wire bytes and decompressed bytes before decoding the body."""
    if response.content_length is not None and response.content_length > MAX_RESPONSE_BYTES:
        return None
    if "Content-Type" in response.headers and not (
        response.content_type.startswith("text/") or response.content_type == "application/xhtml+xml"
    ):
        return None
    content_encoding = response.headers.get("Content-Encoding", "identity").strip().lower()
    decompressor = None
    if content_encoding == "gzip":
        decompressor = zlib.decompressobj(16 + zlib.MAX_WBITS)
    elif content_encoding == "deflate":
        decompressor = zlib.decompressobj()
    elif content_encoding not in {"", "identity"}:
        return None

    body = bytearray()
    downloaded_bytes = 0
    while True:
        chunk = await response.content.read(min(RESPONSE_CHUNK_BYTES, MAX_RESPONSE_BYTES - downloaded_bytes + 1))
        if not chunk:
            break
        downloaded_bytes += len(chunk)
        if downloaded_bytes > MAX_RESPONSE_BYTES:
            return None
        if decompressor is None:
            body.extend(chunk)
        else:
            while chunk:
                decoded_chunk = decompressor.decompress(
                    chunk, min(RESPONSE_CHUNK_BYTES, MAX_RESPONSE_BYTES - len(body) + 1)
                )
                if len(body) + len(decoded_chunk) > MAX_RESPONSE_BYTES:
                    return None
                body.extend(decoded_chunk)
                if decompressor.unused_data:
                    return None
                chunk = decompressor.unconsumed_tail
    if decompressor is not None and not decompressor.eof:
        return None
    try:
        return body.decode(response.charset or "utf-8", errors="replace")
    except LookupError:
        return body.decode("utf-8", errors="replace")


async def fetch_html(url: URL) -> str | None:
    resolver = PublicResolver()
    try:
        connector = aiohttp.TCPConnector(resolver=resolver, use_dns_cache=False)
        async with aiohttp.ClientSession(
            connector=connector,
            timeout=aiohttp.ClientTimeout(total=FETCH_TIMEOUT_SECONDS),
            trust_env=False,
            cookie_jar=aiohttp.DummyCookieJar(),
            auto_decompress=False,
            headers={"Accept-Encoding": "gzip, deflate"},
        ) as session:
            for redirects in range(MAX_REDIRECTS + 1):
                async with session.get(url, allow_redirects=False) as response:
                    if response.status in REDIRECT_STATUSES:
                        location = response.headers.get("Location")
                        if not location or redirects == MAX_REDIRECTS:
                            return None
                        url = validate_url(location, base=url)
                        continue
                    if response.status != 200:
                        return None
                    return await read_response(response)
        return None
    finally:
        await resolver.close()


async def extract_url_content(url: str, max_chars: int = 8000) -> str | None:
    """Fetch a URL and return extracted text content, or None on failure."""
    try:
        validated_url = validate_url(url)
        html = await asyncio.wait_for(fetch_html(validated_url), timeout=FETCH_TIMEOUT_SECONDS)
        if html is None:
            return None
        extractor = _HTMLTextExtractor()
        extractor.feed(html)
        text = extractor.get_text()
        return text[:max_chars] if text else None
    except Exception:
        return None
