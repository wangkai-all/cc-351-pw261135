"""HTTP client for the collector and relation checker.

Deliberately small and strict:

* only http/https
* every redirect must stay on the original origin
* bounded response size and timeout
* content-type checked before parsing
* no cookies, proxies or credentials from the environment
"""

from __future__ import annotations

import hashlib
import ipaddress
import socket
import urllib.error
import urllib.request
from dataclasses import dataclass
from urllib.parse import urljoin, urlsplit

MAX_BYTES = 2 * 1024 * 1024
DEFAULT_TIMEOUT = 10.0
USER_AGENT = "CorpusAudit/1.0 (fictional local source; testing)"


class FetchError(Exception):
    def __init__(self, code: str, message: str, *, status: int | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status


@dataclass(frozen=True)
class Fetched:
    url: str
    status: int
    body: bytes | None
    content_type: str
    etag: str | None
    last_modified: str | None
    not_modified: bool = False

    @property
    def sha256(self) -> str:
        return hashlib.sha256(self.body or b"").hexdigest()


class _SameOriginRedirect(urllib.request.HTTPRedirectHandler):
    def __init__(self, origin: str) -> None:
        self.origin = origin

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        target = urlsplit(urljoin(req.full_url, newurl))
        if f"{target.scheme}://{target.netloc}" != self.origin:
            raise FetchError(
                "cross_origin_redirect",
                f"重定向离开来源站点: {newurl}",
                status=code,
            )
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def origin_of(url: str) -> str:
    parts = urlsplit(url)
    return f"{parts.scheme}://{parts.netloc}"


def _validate_url(url: str, *, allowed_origin: str) -> None:
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https"):
        raise FetchError("bad_url", f"不支持的协议: {parts.scheme}")
    if not parts.hostname:
        raise FetchError("bad_url", "URL 缺少主机名")
    if parts.username or parts.password:
        raise FetchError("bad_url", "URL 不允许包含凭据")
    if origin_of(url) != allowed_origin:
        raise FetchError("cross_origin", f"只允许抓取来源站同源地址: {url}")
    # The local test source resolves to loopback; other non-public address
    # literals are refused so this tool never becomes an SSRF gadget.
    try:
        ip = ipaddress.ip_address(parts.hostname)
    except ValueError:
        return
    if (
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_reserved
        or ip.is_multicast
    ) and not (ip.is_loopback and parts.hostname in ("127.0.0.1", "::1")):
        raise FetchError("bad_url", f"不允许抓取非公网地址: {parts.hostname}")


class Client:
    def __init__(
        self,
        base_url: str,
        *,
        timeout: float = DEFAULT_TIMEOUT,
        max_bytes: int = MAX_BYTES,
    ) -> None:
        self.base_url = base_url
        self.origin = origin_of(base_url)
        _validate_url(base_url, allowed_origin=self.origin)
        self.timeout = timeout
        self.max_bytes = max_bytes
        self._opener = urllib.request.build_opener(
            urllib.request.ProxyHandler({}),
            _SameOriginRedirect(self.origin),
        )

    def absolute(self, url: str) -> str:
        absolute = urljoin(self.base_url, url)
        _validate_url(absolute, allowed_origin=self.origin)
        return absolute

    def get(
        self,
        url: str,
        *,
        etag: str | None = None,
        last_modified: str | None = None,
    ) -> Fetched:
        absolute = self.absolute(url)
        request = urllib.request.Request(
            absolute,
            headers={
                "User-Agent": USER_AGENT,
                "Accept": "text/html,application/xhtml+xml;q=0.9",
            },
        )
        if etag:
            request.add_header("If-None-Match", etag)
        if last_modified:
            request.add_header("If-Modified-Since", last_modified)
        try:
            response = self._opener.open(request, timeout=self.timeout)
        except FetchError:
            raise
        except urllib.error.HTTPError as exc:
            if exc.code == 304:
                return Fetched(
                    url=absolute,
                    status=304,
                    body=None,
                    content_type=exc.headers.get("Content-Type", ""),
                    etag=exc.headers.get("ETag"),
                    last_modified=exc.headers.get("Last-Modified"),
                    not_modified=True,
                )
            raise FetchError(
                "http_error",
                f"HTTP {exc.code} {exc.reason}",
                status=exc.code,
            ) from exc
        except (urllib.error.URLError, socket.timeout, TimeoutError) as exc:
            raise FetchError("network_error", f"抓取失败: {exc}") from exc
        except OSError as exc:
            raise FetchError("network_error", f"抓取失败: {exc}") from exc

        with response:
            content_type = response.headers.get("Content-Type", "").split(";")[0].strip().lower()
            declared = response.headers.get("Content-Length")
            if declared and declared.isdigit() and int(declared) > self.max_bytes:
                raise FetchError("too_large", f"响应超过 {self.max_bytes} 字节")
            chunks = []
            total = 0
            while True:
                chunk = response.read(min(65536, self.max_bytes + 1 - total))
                if not chunk:
                    break
                total += len(chunk)
                if total > self.max_bytes:
                    raise FetchError("too_large", f"响应超过 {self.max_bytes} 字节")
                chunks.append(chunk)
            if content_type not in ("text/html", "application/xhtml+xml"):
                raise FetchError(
                    "bad_content_type",
                    f"期望 HTML，实际 Content-Type={content_type or '缺失'}",
                )
            return Fetched(
                url=absolute,
                status=response.status,
                body=b"".join(chunks),
                content_type=content_type,
                etag=response.headers.get("ETag"),
                last_modified=response.headers.get("Last-Modified"),
            )
