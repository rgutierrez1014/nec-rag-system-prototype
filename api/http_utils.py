import time
from typing import Any

import httpx


def http_post_with_retry(
    url: str,
    *,
    retries: int = 3,
    backoff_base: float = 2.0,
    timeout: float = 300.0,
    **kwargs: Any,
) -> httpx.Response:
    """POST to url, retrying up to `retries` times with exponential backoff on failure."""
    delay = backoff_base
    for attempt in range(retries):
        try:
            response = httpx.post(url, timeout=timeout, **kwargs)
            response.raise_for_status()
            return response
        except (httpx.HTTPError, httpx.TimeoutException) as e:
            if attempt == retries - 1:
                raise
            print(f"  POST {url} attempt {attempt + 1} failed ({e}), retrying in {delay:.0f}s...")
            time.sleep(delay)
            delay *= backoff_base
    raise RuntimeError("unreachable")


def http_get_with_retry(
    url: str,
    *,
    retries: int = 3,
    backoff_base: float = 2.0,
    timeout: float = 30.0,
    **kwargs: Any,
) -> httpx.Response:
    """GET url, retrying up to `retries` times with exponential backoff on failure."""
    delay = backoff_base
    for attempt in range(retries):
        try:
            response = httpx.get(url, timeout=timeout, **kwargs)
            response.raise_for_status()
            return response
        except (httpx.HTTPError, httpx.TimeoutException) as e:
            if attempt == retries - 1:
                raise
            print(f"  GET {url} attempt {attempt + 1} failed ({e}), retrying in {delay:.0f}s...")
            time.sleep(delay)
            delay *= backoff_base
    raise RuntimeError("unreachable")
