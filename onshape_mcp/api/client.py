"""Onshape API client for REST API communication."""

import asyncio
import base64
import httpx
from typing import Any, Dict, Optional
from pydantic import BaseModel
from loguru import logger


class OnshapeCredentials(BaseModel):
    """Onshape API credentials."""

    access_key: str
    secret_key: str
    base_url: str = "https://cad.onshape.com"


class OnshapeClient:
    """Client for interacting with Onshape REST API.

    Use as an async context manager to ensure proper cleanup:
        async with OnshapeClient(credentials) as client:
            result = await client.get("/api/v9/documents")
    """

    def __init__(self, credentials: OnshapeCredentials):
        """Initialize the Onshape client.

        Args:
            credentials: Onshape API credentials (access key and secret key)
        """
        self.credentials = credentials
        self.base_url = credentials.base_url
        self._client: Optional[httpx.AsyncClient] = None
        self._own_client = False

    async def __aenter__(self):
        """Async context manager entry."""
        self._client = httpx.AsyncClient(timeout=30.0)
        self._own_client = True
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        """Async context manager exit - ensures cleanup."""
        await self.close()
        return False

    def _ensure_client(self):
        """Ensure HTTP client is initialized."""
        if self._client is None:
            # Create client if not using context manager (backwards compatibility)
            self._client = httpx.AsyncClient(timeout=30.0)
            self._own_client = True

    RETRY_STATUS = (429, 502, 503, 504)
    MAX_RETRIES = 5

    async def _send(self, method: str, url: str, **kwargs) -> httpx.Response:
        """Send a request, waiting and retrying when Onshape rate-limits (429) or is busy."""
        delay = 2.0
        for attempt in range(self.MAX_RETRIES + 1):
            response = await getattr(self._client, method)(url, **kwargs)
            status = getattr(response, "status_code", 200)
            if not isinstance(status, int) or status not in self.RETRY_STATUS:
                return response
            if attempt == self.MAX_RETRIES:
                return response
            retry_after = (
                response.headers.get("Retry-After") if hasattr(response, "headers") else None
            )
            try:
                wait = float(retry_after) if retry_after else delay
            except (TypeError, ValueError):
                wait = delay
            wait = min(max(wait, 0.5), 60.0)
            logger.warning(f"{method.upper()} {url}: HTTP {status}, retrying in {wait:.1f}s")
            await asyncio.sleep(wait)
            delay = min(delay * 2, 30.0)
        return response

    def _get_auth_header(self) -> str:
        """Generate Basic Auth header from credentials.

        Returns:
            Authorization header value
        """
        auth_string = f"{self.credentials.access_key}:{self.credentials.secret_key}"
        encoded = base64.b64encode(auth_string.encode()).decode()
        return f"Basic {encoded}"

    def _sanitize_for_logging(self, data: Any, max_length: int = 200) -> str:
        """Sanitize sensitive data for logging.

        Args:
            data: Data to sanitize
            max_length: Maximum length of output string

        Returns:
            Sanitized string safe for logging
        """
        if isinstance(data, dict):
            sanitized = {}
            for k, v in data.items():
                if k.lower() in {
                    "authorization",
                    "api_key",
                    "secret",
                    "password",
                    "token",
                    "access_key",
                    "secret_key",
                }:
                    sanitized[k] = "***REDACTED***"
                else:
                    sanitized[k] = v
            return str(sanitized)[:max_length]

        result = str(data)
        if len(result) > max_length:
            return result[:max_length] + "... (truncated)"
        return result

    async def get(self, path: str, params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """Make a GET request to Onshape API.

        Args:
            path: API endpoint path (e.g., "/api/v9/documents")
            params: Query parameters

        Returns:
            JSON response data
        """
        url = f"{self.base_url}{path}"
        headers = {
            "Authorization": self._get_auth_header(),
            "Accept": "application/json;charset=UTF-8; qs=0.09",
        }

        self._ensure_client()
        logger.debug(f"GET {url} with params: {self._sanitize_for_logging(params)}")
        response = await self._send("get", url, params=params, headers=headers)
        response.raise_for_status()
        result = response.json()
        logger.debug(f"GET {url} response: {self._sanitize_for_logging(result, max_length=500)}")
        return result

    async def post(
        self,
        path: str,
        data: Optional[Dict[str, Any]] = None,
        params: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """Make a POST request to Onshape API.

        Args:
            path: API endpoint path
            data: JSON body data
            params: Query parameters

        Returns:
            JSON response data
        """
        url = f"{self.base_url}{path}"
        headers = {
            "Authorization": self._get_auth_header(),
            "Accept": "application/json;charset=UTF-8; qs=0.09",
            "Content-Type": "application/json;charset=UTF-8; qs=0.09",
        }

        self._ensure_client()
        logger.debug(f"POST {url} with params: {self._sanitize_for_logging(params)}")
        logger.debug(f"POST {url} data: {self._sanitize_for_logging(data, max_length=1000)}")
        response = await self._send("post", url, json=data, params=params, headers=headers)

        # Log error details if request failed
        if response.status_code >= 400:
            try:
                error_body = response.json()
                logger.error(
                    f"POST {url} failed with status {response.status_code}: {self._sanitize_for_logging(error_body)}"
                )
            except Exception:
                logger.error(
                    f"POST {url} failed with status {response.status_code}: {response.text[:500]}"
                )

        response.raise_for_status()
        if not response.content:
            logger.debug(f"POST {url} returned empty body (status {response.status_code})")
            return {}
        result = response.json()
        logger.debug(f"POST {url} response: {self._sanitize_for_logging(result, max_length=500)}")
        return result

    async def delete(self, path: str, params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """Make a DELETE request to Onshape API.

        Args:
            path: API endpoint path
            params: Query parameters

        Returns:
            JSON response data
        """
        url = f"{self.base_url}{path}"
        headers = {
            "Authorization": self._get_auth_header(),
            "Accept": "application/json;charset=UTF-8; qs=0.09",
        }

        self._ensure_client()
        response = await self._send("delete", url, params=params, headers=headers)
        response.raise_for_status()
        if not response.content:
            return {}
        return response.json()

    async def get_bytes(self, path: str, params: Optional[Dict[str, Any]] = None) -> bytes:
        """GET a binary resource (e.g. exported files / blob data)."""
        url = f"{self.base_url}{path}"
        headers = {
            "Authorization": self._get_auth_header(),
            "Accept": "application/octet-stream, */*",
        }
        self._ensure_client()
        logger.debug(f"GET (bytes) {url}")
        response = await self._send(
            "get", url, params=params, headers=headers, follow_redirects=False, timeout=300.0
        )
        # Some export endpoints (e.g. STL) redirect to another Onshape host; httpx would drop
        # the Authorization header on such redirects, so follow them manually - but only
        # re-send credentials to onshape.com hosts.
        for _ in range(5):
            if response.status_code not in (301, 302, 303, 307, 308):
                break
            location = httpx.URL(url).join(response.headers["location"])
            host = location.host or ""
            hop_headers = dict(headers)
            if not (host == "onshape.com" or host.endswith(".onshape.com")):
                hop_headers.pop("Authorization", None)
            url = str(location)
            response = await self._send("get", url, headers=hop_headers, timeout=300.0)
        response.raise_for_status()
        return response.content

    async def post_multipart(
        self,
        path: str,
        files: Dict[str, Any],
        data: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """POST a multipart/form-data request (file uploads)."""
        url = f"{self.base_url}{path}"
        headers = {
            "Authorization": self._get_auth_header(),
            "Accept": "application/json;charset=UTF-8; qs=0.09",
        }
        self._ensure_client()
        logger.debug(f"POST (multipart) {url} fields: {self._sanitize_for_logging(data)}")
        response = await self._send(
            "post", url, files=files, data=data or {}, headers=headers, timeout=300.0
        )
        if response.status_code >= 400:
            logger.error(
                f"POST {url} failed with status {response.status_code}: {response.text[:500]}"
            )
        response.raise_for_status()
        return response.json() if response.content else {}

    async def close(self):
        """Close the HTTP client and clean up resources."""
        if self._client and self._own_client:
            await self._client.aclose()
            self._client = None
