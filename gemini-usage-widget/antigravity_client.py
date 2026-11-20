"""Local Connect-RPC client, endpoint caching, and quota query orchestration for Antigravity."""

from __future__ import annotations

import json
import ssl
import threading
import time
import urllib.error
import urllib.request
from typing import Any

from antigravity_discovery import (
    _TCP_TABLE_LOCAL,
    _candidate_ports_for_process,
    _discover_language_server_processes,
    _get_all_listening_ports_win32,
)
from antigravity_parser import (
    AuthRequiredError,
    UsageError,
    UsageSnapshot,
    _has_weekly_limits,
    parse_user_quota,
)


_SERVICE_PREFIX = "/exa.language_server_pb.LanguageServerService"
_GET_USER_STATUS_PATH = f"{_SERVICE_PREFIX}/GetUserStatus"
_RETRIEVE_QUOTA_SUMMARY_PATH = f"{_SERVICE_PREFIX}/RetrieveUserQuotaSummary"

_EMPTY_BODY = b"{}"
_METADATA_BODY = json.dumps(
    {
        "metadata": {
            "ideName": "antigravity",
            "extensionName": "antigravity",
            "ideVersion": "unknown",
            "locale": "en",
        }
    },
    separators=(",", ":"),
).encode("utf-8")

_CACHE_LOCK = threading.Lock()
_ENDPOINT_CACHE: dict[str, Any] = {}
_PARTIAL_CACHE_TTL_SECONDS = 300.0
_HTTPS_SSL_CONTEXT: ssl.SSLContext | None = None


def _get_https_ssl_context() -> ssl.SSLContext:
    global _HTTPS_SSL_CONTEXT
    if _HTTPS_SSL_CONTEXT is None:
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        _HTTPS_SSL_CONTEXT = ctx
    return _HTTPS_SSL_CONTEXT


def _call_language_server_rpc(
    scheme: str,
    port: int,
    path: str,
    csrf_token: str,
    timeout: float,
) -> dict[str, Any]:
    bodies = (
        (_EMPTY_BODY, _METADATA_BODY)
        if path == _RETRIEVE_QUOTA_SUMMARY_PATH
        else (_METADATA_BODY, _EMPTY_BODY)
    )

    handlers: list[urllib.request.BaseHandler] = [urllib.request.ProxyHandler({})]
    if scheme == "https":
        handlers.append(urllib.request.HTTPSHandler(context=_get_https_ssl_context()))
    opener = urllib.request.build_opener(*handlers)

    last_exc: Exception | None = None
    for host in ("127.0.0.1", "[::1]"):
        url = f"{scheme}://{host}:{port}{path}"
        host_unreachable = False
        for body in bodies:
            request = urllib.request.Request(
                url,
                data=body,
                headers={
                    "Content-Type": "application/json",
                    "Connect-Protocol-Version": "1",
                    "X-Codeium-Csrf-Token": csrf_token,
                },
                method="POST",
            )
            try:
                with opener.open(request, timeout=timeout) as response:
                    parsed = json.loads(response.read().decode("utf-8"))
                if not isinstance(parsed, dict):
                    raise UsageError("Antigravity 상태 응답 형식이 올바르지 않습니다.")
                return parsed
            except urllib.error.HTTPError as exc:
                exc.close()
                last_exc = exc
                if exc.code == 400 and body is not bodies[-1]:
                    continue
                raise
            except (urllib.error.URLError, OSError) as exc:
                last_exc = exc
                host_unreachable = True
                break
        if not host_unreachable:
            break

    if last_exc is not None:
        raise last_exc
    raise UsageError("Antigravity 로컬 서버에 연결하지 못했습니다.")


def _fetch_endpoint_payloads(
    scheme: str,
    port: int,
    csrf_token: str,
    timeout: float,
) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    status_payload: dict[str, Any] | None = None
    summary_payload: dict[str, Any] | None = None
    auth_exc: urllib.error.HTTPError | None = None
    last_exc: Exception | None = None
    deadline = time.monotonic() + timeout

    for path, is_summary in (
        (_RETRIEVE_QUOTA_SUMMARY_PATH, True),
        (_GET_USER_STATUS_PATH, False),
    ):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        try:
            result = _call_language_server_rpc(
                scheme=scheme,
                port=port,
                path=path,
                csrf_token=csrf_token,
                timeout=remaining,
            )
            if is_summary:
                summary_payload = result
            else:
                status_payload = result
        except urllib.error.HTTPError as exc:
            exc.close()
            if exc.code in (401, 403) and auth_exc is None:
                auth_exc = exc
            last_exc = exc
        except (urllib.error.URLError, OSError, TimeoutError) as exc:
            last_exc = exc
            # Transport/SSL/timeout failure on the first RPC means this port+scheme is unreachable.
            if summary_payload is None:
                raise
        except Exception as exc:
            last_exc = exc
            if not is_summary and summary_payload is None:
                raise

    if status_payload is None and summary_payload is None:
        if auth_exc is not None:
            raise auth_exc
        if last_exc is not None:
            raise last_exc
        raise UsageError("Antigravity 로컬 서버 응답이 없습니다.")

    return status_payload, summary_payload


def _build_snapshot_from_payloads(
    status_payload: dict[str, Any] | None,
    summary_payload: dict[str, Any] | None,
    fallback_status_payload: dict[str, Any] | None = None,
) -> UsageSnapshot:
    effective_status = status_payload if status_payload is not None else fallback_status_payload
    return parse_user_quota(
        effective_status or summary_payload or {},
        summary_result=summary_payload if effective_status is not None else None,
    )


def _fetch_from_endpoint(
    scheme: str,
    port: int,
    csrf_token: str,
    timeout: float,
    fallback_status_payload: dict[str, Any] | None = None,
    status_endpoint: dict[str, Any] | None = None,
) -> UsageSnapshot:
    deadline = time.monotonic() + timeout
    status_payload, summary_payload = _fetch_endpoint_payloads(
        scheme=scheme,
        port=port,
        csrf_token=csrf_token,
        timeout=timeout,
    )
    if status_payload is None and status_endpoint is not None:
        remaining = deadline - time.monotonic()
        if remaining > 0.2:
            try:
                status_payload = _call_language_server_rpc(
                    scheme=str(status_endpoint["scheme"]),
                    port=int(status_endpoint["port"]),
                    path=_GET_USER_STATUS_PATH,
                    csrf_token=str(status_endpoint["csrf_token"]),
                    timeout=remaining,
                )
            except urllib.error.HTTPError as exc:
                exc.close()
            except Exception:
                pass
    return _build_snapshot_from_payloads(
        status_payload=status_payload,
        summary_payload=summary_payload,
        fallback_status_payload=fallback_status_payload,
    )


def query_usage(timeout: float = 15.0) -> UsageSnapshot:
    """Fetch current Gemini & Claude/GPT quota from the local Antigravity Language Server."""
    deadline = time.monotonic() + timeout
    with _CACHE_LOCK:
        cached_endpoint = dict(_ENDPOINT_CACHE) if _ENDPOINT_CACHE else None

    if cached_endpoint:
        cache_has_weekly = cached_endpoint.get("has_weekly") is True
        cache_age = time.monotonic() - float(cached_endpoint.get("cached_at", 0.0))
        if cache_has_weekly or cache_age < _PARTIAL_CACHE_TTL_SECONDS:
            try:
                snapshot = _fetch_from_endpoint(
                    scheme=str(cached_endpoint["scheme"]),
                    port=int(cached_endpoint["port"]),
                    csrf_token=str(cached_endpoint["csrf_token"]),
                    timeout=min(timeout, 3.5),
                    status_endpoint=cached_endpoint.get("status_endpoint"),
                )
                if not cache_has_weekly or _has_weekly_limits(snapshot):
                    return snapshot
            except urllib.error.HTTPError as exc:
                exc.close()
            except Exception:
                pass
            with _CACHE_LOCK:
                _ENDPOINT_CACHE.clear()

    processes = _discover_language_server_processes()
    if not processes:
        raise UsageError(
            "실행 중인 Antigravity를 찾을 수 없습니다. Antigravity를 실행해 주세요."
        )

    per_attempt_timeout = max(1.5, min(timeout / 4.0, 3.5))
    saw_auth_error = False
    last_error: Exception | None = None
    fallback_status_payload: dict[str, Any] | None = None
    fallback_status_endpoint: dict[str, Any] | None = None
    best_partial: tuple[UsageSnapshot, dict[str, Any]] | None = None
    tried: set[tuple[str, int, str]] = set()

    _TCP_TABLE_LOCAL.snapshot = _get_all_listening_ports_win32()
    try:
        for proc in processes:
            if time.monotonic() >= deadline:
                break
            ports = _candidate_ports_for_process(proc)
            for port in ports:
                if time.monotonic() >= deadline:
                    break
                for scheme in ("https", "http"):
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        break
                    endpoint_key = (scheme, port, proc.csrf_token)
                    if endpoint_key in tried:
                        continue
                    tried.add(endpoint_key)
                    try:
                        status_payload, summary_payload = _fetch_endpoint_payloads(
                            scheme=scheme,
                            port=port,
                            csrf_token=proc.csrf_token,
                            timeout=min(per_attempt_timeout, remaining),
                        )
                        if status_payload is not None and fallback_status_payload is None:
                            fallback_status_payload = status_payload
                            fallback_status_endpoint = {
                                "scheme": scheme,
                                "port": port,
                                "csrf_token": proc.csrf_token,
                            }

                        snapshot = _build_snapshot_from_payloads(
                            status_payload=status_payload,
                            summary_payload=summary_payload,
                            fallback_status_payload=fallback_status_payload,
                        )
                        cache_entry = {
                            "pid": proc.pid,
                            "scheme": scheme,
                            "port": port,
                            "csrf_token": proc.csrf_token,
                            "has_weekly": _has_weekly_limits(snapshot),
                            "cached_at": time.monotonic(),
                            "status_endpoint": fallback_status_endpoint if status_payload is None else None,
                        }
                        if _has_weekly_limits(snapshot):
                            with _CACHE_LOCK:
                                _ENDPOINT_CACHE.clear()
                                _ENDPOINT_CACHE.update(cache_entry)
                            return snapshot

                        if best_partial is None:
                            best_partial = (snapshot, cache_entry)
                        # Stop trying http on the same port once https succeeded, and check next port/process.
                        break
                    except urllib.error.HTTPError as exc:
                        exc.close()
                        if exc.code in (401, 403):
                            saw_auth_error = True
                        last_error = exc
                    except AuthRequiredError:
                        raise
                    except Exception as exc:
                        last_error = exc
    finally:
        _TCP_TABLE_LOCAL.snapshot = None

    if best_partial is not None:
        snapshot, cache_entry = best_partial
        with _CACHE_LOCK:
            _ENDPOINT_CACHE.clear()
            _ENDPOINT_CACHE.update(cache_entry)
        return snapshot

    if saw_auth_error:
        raise AuthRequiredError(
            "Antigravity 인증에 실패했습니다. Antigravity에서 로그인 상태를 확인해 주세요."
        )
    if last_error is not None:
        raise UsageError(f"Antigravity 로컬 서버에 연결하지 못했습니다: {last_error}") from last_error
    raise UsageError("Antigravity 로컬 서버 포트를 찾지 못했습니다.")
