import math

import httpx

DEFAULT_REQUEST_TIMEOUT_SECONDS = 600.0


def validate_request_timeout(value: object) -> float:
    if (isinstance(value, bool) or not isinstance(value, int | float)
            or not 1 <= value <= 7200 or not math.isfinite(value)):
        raise ValueError("requestTimeoutSeconds must be a finite number between 1 and 7200 seconds.")
    return float(value)


def model_timeout(seconds: float) -> httpx.Timeout:
    """Allow long non-streaming generation without delaying connection failures."""
    return httpx.Timeout(connect=10, read=validate_request_timeout(seconds), write=30, pool=10)
