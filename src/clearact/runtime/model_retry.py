from __future__ import annotations

import httpx


class ModelRequestError(RuntimeError):
    """A model request failed with a user-actionable configuration error."""


class ModelServiceUnavailableError(ModelRequestError):
    """A model request still failed after transient-error retries."""


_RETRYABLE_STATUS_CODES = {408, 425, 429, 500, 502, 503, 504}
_RETRYABLE_TRANSPORT_ERRORS = (
    httpx.NetworkError,
    httpx.ProxyError,
    httpx.RemoteProtocolError,
    httpx.TimeoutException,
)


def is_transient_model_error(exc: Exception) -> bool:
    if isinstance(exc, _RETRYABLE_TRANSPORT_ERRORS):
        return True
    return isinstance(exc, httpx.HTTPStatusError) and exc.response.status_code in _RETRYABLE_STATUS_CODES


def describe_model_error(exc: Exception, language: str = "zh") -> str:
    if isinstance(exc, httpx.HTTPStatusError):
        status = exc.response.status_code
        if status == 400:
            return (
                "模型服务无法识别当前请求，请检查接口类型、模型名称和 Base URL"
                if language == "zh"
                else "The model service rejected the request; check the provider type, model, and Base URL"
            )
        if status == 401:
            return (
                "模型服务认证失败（HTTP 401），请检查接口类型和 API Key"
                if language == "zh"
                else "Model authentication failed (HTTP 401); check the provider type and API key"
            )
        if status == 403:
            return (
                "模型服务拒绝访问（HTTP 403），请检查 API Key 权限"
                if language == "zh"
                else "The model service denied access (HTTP 403); check the API key permissions"
            )
        if status == 404:
            return (
                "找不到模型接口（HTTP 404），请检查接口类型、模型名称和 Base URL"
                if language == "zh"
                else "The model endpoint was not found (HTTP 404); check the provider type, model, and Base URL"
            )
        if status == 429:
            return "模型服务当前请求过多" if language == "zh" else "The model service is rate-limited"
        if status in _RETRYABLE_STATUS_CODES:
            return (
                f"模型服务暂时不可用（HTTP {status}）"
                if language == "zh"
                else f"The model service is temporarily unavailable (HTTP {status})"
            )
    if isinstance(exc, httpx.TimeoutException):
        return "模型服务响应超时" if language == "zh" else "The model service timed out"
    if isinstance(exc, httpx.RemoteProtocolError):
        return (
            "模型服务在返回结果前断开了连接"
            if language == "zh"
            else "The model service disconnected before returning a response"
        )
    if isinstance(exc, httpx.NetworkError | httpx.ProxyError):
        return "无法稳定连接到模型服务" if language == "zh" else "The model service connection is unstable"
    return str(exc) or type(exc).__name__
