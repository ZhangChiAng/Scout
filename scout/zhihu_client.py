"""Versioned, local HTTP boundary; no crawler imports or browser credentials."""

import json
import os
import re
import uuid
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

from .config import ConfigError


class CollectorError(RuntimeError):
    def __init__(self, kind, message):
        self.kind = kind
        super().__init__(message)


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class CollectorClient:
    def __init__(self, url: str, token: str, timeout: float = 10):
        parsed = urlsplit(url)
        if (
            parsed.scheme != "http"
            or parsed.hostname not in {"127.0.0.1", "::1", "localhost"}
            or parsed.username
            or parsed.password
            or parsed.path not in {"", "/"}
            or parsed.query
            or parsed.fragment
        ):
            raise ConfigError("知乎采集器必须使用本机 HTTP 地址")
        if not token or any(char.isspace() for char in token):
            raise ConfigError("缺少有效的 ZHIHU_COLLECTOR_TOKEN")
        self.url, self.token, self.timeout = url.rstrip("/"), token, timeout
        self.opener = build_opener(ProxyHandler({}), _NoRedirect())

    @classmethod
    def from_env(cls):
        return cls(
            os.environ.get("ZHIHU_COLLECTOR_URL", ""),
            os.environ.get("ZHIHU_COLLECTOR_TOKEN", ""),
        )

    def _request(self, path, payload=None, *, binary=False):
        if not re.fullmatch(r"/v1/[A-Za-z0-9_/-]+", path):
            raise ConfigError("无效的采集器资源路径")
        body = (
            None if payload is None else json.dumps(payload, allow_nan=False).encode()
        )
        request = Request(
            self.url + path,
            data=body,
            headers={
                "Authorization": "Bearer " + self.token,
                "Content-Type": "application/json",
                "Accept": "application/octet-stream" if binary else "application/json",
            },
        )
        try:
            with self.opener.open(request, timeout=self.timeout) as response:
                raw = response.read(32 * 1024 * 1024 + 1)
        except HTTPError as exc:
            kind = {
                401: "authentication",
                403: "authentication",
                404: "missing",
                409: "conflict",
            }.get(exc.code, "http_error")
            raise CollectorError(kind, f"采集器 HTTP {exc.code}") from None
        except URLError, TimeoutError, OSError:
            raise CollectorError("network", "无法连接本机采集器") from None
        if len(raw) > 32 * 1024 * 1024:
            raise CollectorError("protocol", "采集器响应超过大小上限")
        if binary:
            return raw
        try:
            result = json.loads(raw)
        except ValueError, UnicodeError:
            raise CollectorError("protocol", "采集器返回无效 JSON") from None
        if not isinstance(result, dict):
            raise CollectorError("protocol", "采集器返回必须是 JSON 对象")
        return result

    def health(self):
        value = self._request("/v1/health")
        if str(value.get("protocol_version")) not in {"1", "1.0"}:
            raise CollectorError("protocol", "不支持的采集器协议版本")
        return value

    def start(self, payload):
        return self._request("/v1/runs", payload)

    def run(self, run_id):
        return self._request("/v1/runs/" + str(uuid.UUID(run_id)))

    def records(self, run_id):
        return self._request("/v1/runs/" + str(uuid.UUID(run_id)) + "/records")

    def login_status(self):
        return self._request("/v1/login")

    def evidence(self, path):
        if not re.fullmatch(r"/v1/evidence/[A-Za-z0-9_-]+", path):
            raise ConfigError("证据必须来自采集器受控资源接口")
        return self._request(path, binary=True)
