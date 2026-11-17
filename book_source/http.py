# -*- coding: utf-8 -*-
"""网络请求层。

对标 legado 的 OkHttp 封装，统一处理：

* 请求头 / Referer / User-Agent；
* Cookie（每个书源独立 CookieJar）；
* 代理、超时、重试；
* 编码自动识别（BOM / 响应头 / meta / UTF-8 / GB18030）；
* gzip、deflate 解压（安装了 brotli 时也支持 br）；
* 按域名限速与并发上限；
* 可选的上层缓存（书源引擎会传入磁盘缓存）。

只使用 Python 标准库，线程安全，可被多个书源共享。
"""

from __future__ import annotations

import codecs
import gzip
import hashlib
import http.cookiejar
import json as _json
import re
import socket
import ssl
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import zlib
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable, Mapping, Protocol

__all__ = [
    "NetworkError",
    "Response",
    "HttpClient",
    "RateLimiter",
    "ResponseCache",
    "decode_bytes",
    "parse_headers",
    "guess_encoding",
]

DEFAULT_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

_CHARSET_RE = re.compile(r"charset\s*=\s*[\"']?([\w\-]+)", re.I)
_META_CHARSET_RE = re.compile(
    rb"<meta[^>]+charset\s*=\s*[\"']?([\w\-]+)", re.I
)
_RETRY_STATUS = {408, 425, 429, 500, 502, 503, 504}


class NetworkError(Exception):
    """网络请求失败（连接、超时、TLS、重试耗尽等）。"""

    def __init__(self, message: str, *, url: str = "", cause: BaseException | None = None):
        super().__init__(message)
        self.url = url
        self.cause = cause


class ResponseCache(Protocol):
    """响应缓存接口；由 book_source.store 提供磁盘实现。"""

    def get(self, key: str) -> Any | None: ...

    def set(self, key: str, value: Any, ttl: float | None = None) -> None: ...


def parse_headers(raw: str | Mapping[str, str] | None) -> dict[str, str]:
    """把书源里的 header 字段规范成字典。

    支持两种写法：

    * JSON 对象：``{"User-Agent": "...", "Referer": "..."}``；
    * 纯文本：每行 ``Key: Value``。
    """
    if not raw:
        return {}
    if isinstance(raw, Mapping):
        return {str(k).strip(): str(v) for k, v in raw.items() if str(k).strip()}
    text = str(raw).strip()
    if not text:
        return {}
    if text.startswith("{"):
        try:
            data = _json.loads(text)
        except ValueError:
            data = None
        if isinstance(data, dict):
            return {str(k).strip(): str(v) for k, v in data.items() if str(k).strip()}
    headers: dict[str, str] = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if ":" in line:
            key, value = line.split(":", 1)
        elif "：" in line:
            key, value = line.split("：", 1)
        else:
            continue
        key = key.strip()
        if key:
            headers[key] = value.strip()
    return headers


def _header_lookup(headers: Mapping[str, str], name: str) -> str:
    lowered = name.lower()
    for key, value in headers.items():
        if key.lower() == lowered:
            return str(value)
    return ""


def guess_encoding(data: bytes, content_type: str = "", hint: str = "") -> str:
    """猜测字节串编码，返回 Python 编码名。"""
    if data.startswith(codecs.BOM_UTF8):
        return "utf-8-sig"
    if data.startswith(codecs.BOM_UTF16_LE):
        return "utf-16-le"
    if data.startswith(codecs.BOM_UTF16_BE):
        return "utf-16-be"
    if hint:
        normalized = hint.strip().lower()
        if normalized in ("gbk", "gb2312", "gb_2312-80"):
            return "gb18030"
        try:
            codecs.lookup(normalized)
            return normalized
        except LookupError:
            pass
    if content_type:
        match = _CHARSET_RE.search(content_type)
        if match:
            name = match.group(1).strip().lower()
            if name in ("gbk", "gb2312", "gb_2312-80"):
                return "gb18030"
            try:
                codecs.lookup(name)
                return name
            except LookupError:
                pass
    head = data[:4096]
    for pattern in (_META_CHARSET_RE,):
        match = pattern.search(head)
        if match:
            name = match.group(1).decode("ascii", "ignore").strip().lower()
            if name in ("gbk", "gb2312", "gb_2312-80"):
                return "gb18030"
            try:
                codecs.lookup(name)
                return name
            except LookupError:
                pass
    for encoding in ("utf-8", "gb18030"):
        try:
            data.decode(encoding)
            return encoding
        except UnicodeDecodeError:
            continue
    return "utf-8"


def decode_bytes(
    data: bytes,
    *,
    content_type: str = "",
    encoding: str = "",
    errors: str = "replace",
) -> tuple[str, str]:
    """解码字节串，返回 ``(文本, 实际编码)``。"""
    if not data:
        return "", encoding or "utf-8"
    chosen = guess_encoding(data, content_type, encoding)
    try:
        return data.decode(chosen, errors), chosen
    except (UnicodeDecodeError, LookupError):
        pass
    for fallback in ("utf-8", "gb18030", "big5", "latin-1"):
        try:
            return data.decode(fallback, errors), fallback
        except (UnicodeDecodeError, LookupError):
            continue
    return data.decode("utf-8", "replace"), "utf-8"


class RateLimiter:
    """按域名的请求限速器（最小间隔 + 全局并发上限）。"""

    def __init__(self, min_interval: float = 0.0, max_concurrency: int = 8) -> None:
        self.min_interval = max(0.0, float(min_interval or 0.0))
        self.max_concurrency = max(1, int(max_concurrency or 1))
        self._lock = threading.Lock()
        self._next_allowed: dict[str, float] = {}
        self._semaphore = threading.BoundedSemaphore(self.max_concurrency)

    def set_interval(self, min_interval: float) -> None:
        with self._lock:
            self.min_interval = max(0.0, float(min_interval or 0.0))

    def wait(self, host: str) -> None:
        """阻塞到该域名下一次允许请求的时刻。"""
        if self.min_interval <= 0:
            return
        with self._lock:
            now = time.monotonic()
            target = max(now, self._next_allowed.get(host, 0.0))
            self._next_allowed[host] = target + self.min_interval
        delay = target - now
        if delay > 0:
            time.sleep(delay)

    def __enter__(self) -> "RateLimiter":
        self._semaphore.acquire()
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self._semaphore.release()


@dataclass
class Response:
    """一次 HTTP 响应。"""

    url: str
    status: int
    headers: dict[str, str]
    content: bytes
    encoding: str = ""
    request_url: str = ""
    elapsed: float = 0.0
    from_cache: bool = False
    _text: str | None = field(default=None, repr=False)

    @property
    def text(self) -> str:
        if self._text is None:
            self._text, self.encoding = decode_bytes(
                self.content,
                content_type=_header_lookup(self.headers, "Content-Type"),
                encoding=self.encoding,
            )
        return self._text

    def json(self) -> Any:
        return _json.loads(self.text)

    @property
    def ok(self) -> bool:
        return 200 <= self.status < 400

    def raise_for_status(self) -> "Response":
        if not self.ok:
            raise NetworkError(f"HTTP {self.status}: {self.url}", url=self.url)
        return self

    def header(self, name: str, default: str = "") -> str:
        return _header_lookup(self.headers, name) or default

    def __repr__(self) -> str:  # pragma: no cover - 调试用
        return f"<Response {self.status} {self.url} {len(self.content)}B>"


class HttpClient:
    """线程安全的同步 HTTP 客户端。"""

    def __init__(
        self,
        *,
        timeout: float = 15.0,
        retries: int = 2,
        proxy: str | None = None,
        user_agent: str = DEFAULT_UA,
        max_connections: int = 8,
        min_interval: float = 0.0,
        cache: ResponseCache | None = None,
        trust_env_proxy: bool = True,
        verify_ssl: bool = True,
        logger: Callable[[str], None] | None = None,
    ) -> None:
        self.timeout = max(0.5, float(timeout))
        self.retries = max(0, int(retries))
        self.proxy = (proxy or "").strip() or None
        self.user_agent = user_agent or DEFAULT_UA
        self.rate_limiter = RateLimiter(min_interval, max_connections)
        self.cache = cache
        self.logger = logger
        self.cookie_jar = http.cookiejar.CookieJar()
        self._cookie_lock = threading.Lock()
        self._opener = self._build_opener(trust_env_proxy, verify_ssl)

    # ------------------------------------------------------------------
    # 底层
    # ------------------------------------------------------------------
    def _build_opener(self, trust_env_proxy: bool, verify_ssl: bool) -> urllib.request.OpenerDirector:
        handlers: list[Any] = [urllib.request.HTTPCookieProcessor(self.cookie_jar)]
        if self.proxy:
            handlers.append(
                urllib.request.ProxyHandler({"http": self.proxy, "https": self.proxy})
            )
        elif trust_env_proxy:
            handlers.append(urllib.request.ProxyHandler())
        if not verify_ssl:
            context = ssl.create_default_context()
            context.check_hostname = False
            context.verify_mode = ssl.CERT_NONE
            handlers.append(urllib.request.HTTPSHandler(context=context))
        return urllib.request.build_opener(*handlers)

    def _cache_key(self, method: str, url: str, body: bytes | None, headers: dict[str, str]) -> str:
        digest = hashlib.sha1()
        digest.update(method.upper().encode("utf-8"))
        digest.update(b"\n")
        digest.update(url.encode("utf-8"))
        digest.update(b"\n")
        if body:
            digest.update(body)
        digest.update(b"\n")
        for key in sorted(headers):
            if key.lower() in ("cookie", "authorization"):
                continue
            digest.update(f"{key.lower()}={headers[key]}\n".encode("utf-8"))
        return digest.hexdigest()

    # ------------------------------------------------------------------
    # 公共 API
    # ------------------------------------------------------------------
    def get(self, url: str, **kwargs: Any) -> Response:
        return self.request(url, method="GET", **kwargs)

    def post(self, url: str, data: Any = None, **kwargs: Any) -> Response:
        return self.request(url, method="POST", body=data, **kwargs)

    def request(
        self,
        url: str,
        *,
        method: str = "GET",
        headers: str | Mapping[str, str] | None = None,
        body: Any = None,
        encoding: str = "",
        timeout: float | None = None,
        retries: int | None = None,
        use_cache: bool = False,
        cache_ttl: float | None = None,
    ) -> Response:
        """发起请求。

        ``headers`` 支持字典或 ``Key: Value`` 文本；``body`` 支持 bytes、str、
        dict（自动 urlencode）与 list[tuple]。
        """
        method = (method or "GET").upper()
        request_headers = self._merge_headers(headers)
        payload = self._encode_body(body, request_headers)

        cache_key = ""
        if use_cache and self.cache is not None and method == "GET":
            cache_key = self._cache_key(method, url, payload, request_headers)
            cached = self.cache.get(cache_key)
            if cached:
                return self._response_from_cache(url, cached)

        attempts = self.retries if retries is None else max(0, int(retries))
        last_error: Exception | None = None
        started = time.monotonic()
        final_url = url
        for attempt in range(attempts + 1):
            final_url = url
            self.rate_limiter.wait(urllib.parse.urlsplit(url).netloc.lower())
            try:
                with self.rate_limiter:
                    response, final_url = self._send_once(
                        url, method, request_headers, payload, timeout, encoding
                    )
            except urllib.error.HTTPError as exc:  # HTTP 状态异常也带回响应体
                response = self._response_from_http_error(exc, url, encoding)
                response.elapsed = time.monotonic() - started
                if response.status in _RETRY_STATUS and attempt < attempts:
                    last_error = exc
                    self._sleep_before_retry(attempt, response.header("Retry-After"))
                    continue
                if response.status in _RETRY_STATUS:
                    raise NetworkError(
                        f"HTTP {response.status}: {url}", url=url, cause=exc
                    ) from exc
                if cache_key and response.ok:
                    self._store_cache(cache_key, response, cache_ttl)
                return response
            except (urllib.error.URLError, socket.timeout, TimeoutError, ssl.SSLError, OSError) as exc:
                last_error = exc
                if attempt < attempts:
                    self._sleep_before_retry(attempt, None)
                    continue
                reason = getattr(exc, "reason", None) or exc
                raise NetworkError(
                    f"请求失败：{reason}（{url}）", url=url, cause=exc
                ) from exc

            response.elapsed = time.monotonic() - started
            response.request_url = url
            if response.status in _RETRY_STATUS and attempt < attempts:
                self._sleep_before_retry(attempt, response.header("Retry-After"))
                continue
            if response.status in _RETRY_STATUS:
                raise NetworkError(f"HTTP {response.status}: {url}", url=url)
            if cache_key and response.ok:
                self._store_cache(cache_key, response, cache_ttl)
            return response

        raise NetworkError(f"请求失败：{last_error}（{url}）", url=url, cause=last_error)

    def request_text(self, url: str, **kwargs: Any) -> str:
        return self.request(url, **kwargs).text

    # ------------------------------------------------------------------
    # Cookie
    # ------------------------------------------------------------------
    def get_cookie(self, url: str = "") -> str:
        with self._cookie_lock:
            cookies = list(self.cookie_jar)
        if not url:
            return "; ".join(f"{c.name}={c.value}" for c in cookies)
        parts = urllib.parse.urlsplit(url)
        host = parts.hostname or ""
        path = parts.path or "/"
        matched = []
        for cookie in cookies:
            domain = (cookie.domain or "").lstrip(".")
            if domain and not (host == domain or host.endswith("." + domain)):
                continue
            cookie_path = cookie.path or "/"
            if path.startswith(cookie_path):
                matched.append(f"{cookie.name}={cookie.value}")
        return "; ".join(matched)

    def set_cookie(self, url: str, cookie_text: str) -> None:
        """写入形如 ``a=1; b=2`` 的 Cookie。"""
        if not cookie_text:
            return
        parts = urllib.parse.urlsplit(url)
        host = parts.hostname or ""
        path = parts.path or "/"
        for pair in cookie_text.split(";"):
            pair = pair.strip()
            if not pair or "=" not in pair:
                continue
            name, value = pair.split("=", 1)
            cookie = http.cookiejar.Cookie(
                version=0,
                name=name.strip(),
                value=value.strip(),
                port=None,
                port_specified=False,
                domain=host,
                domain_specified=False,
                domain_initial_dot=False,
                path=path,
                path_specified=True,
                secure=parts.scheme == "https",
                expires=None,
                discard=True,
                comment=None,
                comment_url=None,
                rest={},
                rfc2109=False,
            )
            with self._cookie_lock:
                self.cookie_jar.set_cookie(cookie)

    def clear_cookies(self) -> None:
        with self._cookie_lock:
            self.cookie_jar.clear()

    def remove_cookies(self, url: str = "") -> None:
        """删除某个域名下的 Cookie（对应 legado 的 cookie.removeCookie）。"""
        if not url:
            self.clear_cookies()
            return
        parts = urllib.parse.urlsplit(url)
        host = (parts.hostname or "").lower()
        if not host:
            return
        with self._cookie_lock:
            keep = []
            for cookie in self.cookie_jar:
                domain = (cookie.domain or "").lstrip(".").lower()
                if domain and (host == domain or host.endswith("." + domain)):
                    continue
                keep.append(cookie)
            self.cookie_jar.clear()
            for cookie in keep:
                self.cookie_jar.set_cookie(cookie)

    def close(self) -> None:
        """兼容 OkHttp 的 close；urllib 无需显式关闭连接池。"""

    # ------------------------------------------------------------------
    # 内部
    # ------------------------------------------------------------------
    def _merge_headers(self, headers: str | Mapping[str, str] | None) -> dict[str, str]:
        merged: dict[str, str] = {
            "User-Agent": self.user_agent,
            "Accept": "text/html,application/xhtml+xml,application/json;q=0.9,*/*;q=0.8",
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.6",
            "Accept-Encoding": self._accept_encoding(),
            "Connection": "keep-alive",
        }
        for key, value in parse_headers(headers).items():
            existing = _find_key(merged, key)
            if existing:
                merged[existing] = value
            else:
                merged[key] = value
        return merged

    @staticmethod
    def _accept_encoding() -> str:
        encodings = ["gzip", "deflate"]
        try:  # pragma: no cover - 取决于运行环境
            import brotli  # type: ignore  # noqa: F401

            encodings.append("br")
        except ImportError:
            pass
        return ", ".join(encodings)

    @staticmethod
    def _encode_body(body: Any, headers: dict[str, str]) -> bytes | None:
        if body is None:
            return None
        if isinstance(body, bytes):
            return body
        if isinstance(body, bytearray):
            return bytes(body)
        if isinstance(body, str):
            return body.encode("utf-8")
        if isinstance(body, dict):
            data = urllib.parse.urlencode(body, doseq=True)
            if not _find_key(headers, "Content-Type"):
                headers["Content-Type"] = "application/x-www-form-urlencoded; charset=UTF-8"
            return data.encode("utf-8")
        if isinstance(body, (list, tuple)):
            data = urllib.parse.urlencode(body, doseq=True)
            if not _find_key(headers, "Content-Type"):
                headers["Content-Type"] = "application/x-www-form-urlencoded; charset=UTF-8"
            return data.encode("utf-8")
        return str(body).encode("utf-8")

    def _send_once(
        self,
        url: str,
        method: str,
        headers: dict[str, str],
        payload: bytes | None,
        timeout: float | None,
        encoding: str,
    ) -> tuple[Response, str]:
        request = urllib.request.Request(url, data=payload, headers=headers, method=method)
        with self._opener.open(request, timeout=timeout or self.timeout) as raw:
            raw_bytes = raw.read()
            status = getattr(raw, "status", 200) or 200
            response_headers = {k: v for k, v in raw.headers.items()}
            content = self._decompress(raw_bytes, response_headers)
            response = Response(
                url=raw.geturl(),
                status=status,
                headers=response_headers,
                content=content,
                encoding=encoding,
                request_url=url,
            )
            return response, raw.geturl()

    def _response_from_http_error(
        self, exc: urllib.error.HTTPError, url: str, encoding: str
    ) -> Response:
        try:
            raw_bytes = exc.read()
        except Exception:  # pragma: no cover - 极端情况
            raw_bytes = b""
        headers = {k: v for k, v in (exc.headers or {}).items()}
        content = self._decompress(raw_bytes, headers)
        return Response(
            url=exc.geturl() or url,
            status=exc.code,
            headers=headers,
            content=content,
            encoding=encoding,
            request_url=url,
        )

    @staticmethod
    def _decompress(data: bytes, headers: Mapping[str, str]) -> bytes:
        if not data:
            return data
        content_encoding = _header_lookup(headers, "Content-Encoding").lower()
        if not content_encoding:
            return data
        encodings = [item.strip() for item in content_encoding.split(",") if item.strip()]
        for encoding in reversed(encodings):
            try:
                if encoding in ("gzip", "x-gzip"):
                    data = gzip.decompress(data)
                elif encoding == "deflate":
                    try:
                        data = zlib.decompress(data)
                    except zlib.error:
                        data = zlib.decompress(data, -zlib.MAX_WBITS)
                elif encoding == "br":
                    import brotli  # type: ignore

                    data = brotli.decompress(data)
            except ImportError:
                raise NetworkError("服务器返回 br 压缩，但本机未安装 brotli，无法解压")
            except (OSError, zlib.error) as exc:
                raise NetworkError(f"响应解压失败（{encoding}）：{exc}") from exc
        return data

    def _sleep_before_retry(self, attempt: int, retry_after: str | None) -> None:
        delay = 0.4 * (2**attempt)
        if retry_after:
            try:
                delay = max(delay, min(10.0, float(retry_after)))
            except ValueError:
                pass
        if self.logger:
            self.logger(f"请求失败，{delay:.1f}s 后重试（第 {attempt + 1} 次）")
        time.sleep(delay)

    def _store_cache(self, key: str, response: Response, ttl: float | None) -> None:
        if self.cache is None:
            return
        try:
            self.cache.set(
                key,
                {
                    "url": response.url,
                    "status": response.status,
                    "headers": response.headers,
                    "content": response.content,
                    "encoding": response.encoding,
                },
                ttl,
            )
        except Exception:  # pragma: no cover - 缓存失败不影响请求
            pass

    @staticmethod
    def _response_from_cache(url: str, cached: Any) -> Response:
        if isinstance(cached, Response):
            return cached
        if isinstance(cached, dict) and "content" in cached:
            content = cached.get("content") or b""
            if isinstance(content, str):
                content = content.encode("utf-8")
            return Response(
                url=str(cached.get("url") or url),
                status=int(cached.get("status") or 200),
                headers=dict(cached.get("headers") or {}),
                content=content,
                encoding=str(cached.get("encoding") or ""),
                request_url=url,
                from_cache=True,
            )
        raise NetworkError("缓存内容格式不正确", url=url)


def _find_key(headers: Mapping[str, str], name: str) -> str:
    lowered = name.lower()
    for key in headers:
        if key.lower() == lowered:
            return key
    return ""


def quote(text: str) -> str:
    """URL 编码（供引擎拼搜索地址）。"""
    return urllib.parse.quote(str(text), safe="")


def absolute_url(base: str, url: str) -> str:
    """把相对地址补全为绝对地址。"""
    url = (url or "").strip()
    if not url:
        return base
    lowered = url.lower()
    if lowered.startswith("javascript"):
        return ""
    if lowered.startswith("data:"):
        return url
    if url.startswith("//"):
        scheme = urllib.parse.urlsplit(base).scheme or "https"
        return f"{scheme}:{url}"
    if lowered.startswith(("http://", "https://")):
        return url
    return urllib.parse.urljoin(base, url)


def alternate_host_url(url: str, source_url: str) -> str:
    """把 url 的域名替换为书源域名（保留协议、路径、查询）。

    有些书源在规则里写死了旧域名/镜像域名，源站已经换域名；连接失败时
    用书源自身的域名再试一次，比直接报错更实用。
    """
    try:
        target = urllib.parse.urlsplit(url)
        source = urllib.parse.urlsplit(source_url)
    except ValueError:
        return ""
    if not target.netloc or not source.netloc:
        return ""
    if target.netloc.lower() == source.netloc.lower():
        return ""
    if source.scheme not in ("http", "https"):
        return ""
    return urllib.parse.urlunsplit(
        (source.scheme, source.netloc, target.path, target.query, target.fragment)
    )


def is_connection_refused(error: BaseException) -> bool:
    text = f"{error} {getattr(error, 'cause', '')}".lower()
    return (
        "10061" in text
        or "10054" in text
        or "connection refused" in text
        or "connection reset" in text
        or "积极拒绝" in text
        or "拒绝" in text
        or "重置" in text
        or "reset" in text
        or "closed" in text
        or "关闭" in text
    )
