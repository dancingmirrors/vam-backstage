from __future__ import annotations

import http.client
import socket
import ssl
import urllib.error
import urllib.request
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any, Protocol

USER_AGENT = "VaMBackstage/1.0"
DEFAULT_TIMEOUT = 30.0


class TransportError(Exception):

    def __init__(self, message: str, transient: bool = True):
        super().__init__(message)
        self.transient = transient


@dataclass
class HttpResponse:
    status: int
    headers: dict[str, str]
    url: str
    _raw: Any = None
    _body: bytes | None = None

    @property
    def ok(self) -> bool:
        return 200 <= self.status < 300

    def header(self, name: str, default: str | None = None) -> str | None:
        return self.headers.get(name.lower(), default)

    def read(self) -> bytes:
        if self._body is None:
            try:
                self._body = self._raw.read() if self._raw is not None else b""
            except (http.client.IncompleteRead, OSError) as err:
                raise TransportError(f"read failed: {err}") from err
            finally:
                self.close()
        return self._body

    def iter_content(self, chunk_size: int = 256 * 1024) -> Iterator[bytes]:
        if self._body is not None:
            yield self._body
            return
        if self._raw is None:
            return
        try:
            while True:
                chunk = self._raw.read(chunk_size)
                if not chunk:
                    break
                yield chunk
        except (http.client.IncompleteRead, OSError, ssl.SSLError) as err:
            raise TransportError(f"read failed: {err}") from err
        finally:
            self.close()

    def json(self) -> Any:
        import json

        return json.loads(self.read().decode("utf-8"))

    def close(self) -> None:
        raw, self._raw = self._raw, None
        if raw is not None:
            try:
                raw.close()
            except Exception:
                pass


class HttpTransport(Protocol):
    def request(
        self,
        method: str,
        url: str,
        *,
        headers: dict[str, str] | None = None,
        data: bytes | None = None,
        timeout: float = DEFAULT_TIMEOUT,
    ) -> HttpResponse: ...


def _is_transient(err: BaseException) -> bool:
    for e in (err, getattr(err, "reason", None)):
        if isinstance(e, (TimeoutError, ConnectionRefusedError, socket.gaierror)):
            return False
    return True


class UrllibTransport:

    def __init__(self, user_agent: str = USER_AGENT):
        self.user_agent = user_agent
        self._opener = urllib.request.build_opener()

    def request(
        self,
        method: str,
        url: str,
        *,
        headers: dict[str, str] | None = None,
        data: bytes | None = None,
        timeout: float = DEFAULT_TIMEOUT,
    ) -> HttpResponse:
        hdrs = {"User-Agent": self.user_agent}
        if headers:
            hdrs.update(headers)
        req = urllib.request.Request(url, data=data, method=method, headers=hdrs)
        try:
            raw = self._opener.open(req, timeout=timeout)
        except urllib.error.HTTPError as err:
            raw = err
        except (
            urllib.error.URLError,
            TimeoutError,
            ConnectionError,
            ssl.SSLError,
            http.client.HTTPException,
            OSError,
        ) as err:
            raise TransportError(str(err), transient=_is_transient(err)) from err
        status = getattr(raw, "status", None) or getattr(raw, "code", 0)
        headers_out = {k.lower(): v for k, v in raw.headers.items()}
        return HttpResponse(int(status), headers_out, getattr(raw, "url", url) or url, raw)
