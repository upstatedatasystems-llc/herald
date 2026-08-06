import hmac

from fastapi import Header, HTTPException, status

from packages.herald.config import settings


def verify_api_key(x_api_key: str | None = Header(None, alias="X-API-Key")):
    """
    Enforce X-API-Key header authentication using constant-time comparison.
    Rejects missing or invalid keys with HTTP 401/403.
    """
    configured_key = settings.HERALD_API_KEY

    # In testing environment with no key set, bypass or require test key
    if not configured_key and settings.HERALD_ENV.lower() != "production":
        return

    if not x_api_key:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing X-API-Key header",
        )

    if not configured_key or not hmac.compare_digest(
        x_api_key.encode("utf-8"), configured_key.encode("utf-8")
    ):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Invalid API authentication key",
        )
