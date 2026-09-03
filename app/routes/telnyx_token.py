"""
Telnyx WebRTC token endpoint.

Generates a short-lived JWT that allows the browser caller (docs/app.js)
to authenticate with Telnyx's WebRTC platform and place a call.

Requires a Telnyx telephony credential ID. Set TELNYX_TELEPHONY_CREDENTIAL_ID
in .env after creating it in Telnyx Mission Control:
    API Keys -> Telephony Credentials -> Create credential -> copy the ID

SECURITY: this endpoint mints real, billable call tokens and was reachable
on the public internet with zero auth for a period after deployment — any
direct request (curl, not just a browser) could mint a token; CORS only
restricts browser-originated cross-origin calls, not this. Two layers now
guard it, and neither is sufficient by itself:

  1. A shared secret in the X-Najda-Demo-Token header (DEMO_TOKEN_SECRET).
     Weak on its own — docs/app.js is a public static file (GitHub Pages),
     so the value configured there is readable by anyone who views the
     page source. This stops blind bots/scanners probing for open
     endpoints, not someone who actually looks.
  2. A per-IP rate limit (TELNYX_TOKEN_RATE_LIMIT_PER_MINUTE), which caps
     blast radius even if the secret leaks or is guessed. Client IP comes
     from request.client.host, which uvicorn's own ProxyHeadersMiddleware
     (active by default, trusted_hosts="127.0.0.1") has already rewritten
     from X-Forwarded-For whenever the connecting peer is trusted. We must
     NOT read the raw X-Forwarded-For header ourselves — that bypasses the
     trust check and is trivially spoofable. If the real peer address
     isn't 127.0.0.1 the rewrite simply isn't applied and every request
     shares one bucket (rate limiting becomes ineffective, but never
     bypassable) — fixable by setting FORWARDED_ALLOW_IPS to whatever the
     actual peer is.

The backstop that actually matters most, independent of both layers above:
restrict the Telnyx Outbound Voice Profile's destination allowlist to only
this project's own DID, and set a spend limit on the API key, so a leaked
or force-minted token still can't place arbitrary billed calls.
"""

import logging
import secrets
import time
from collections import defaultdict

from fastapi import APIRouter, Header, HTTPException, Request
from telnyx import AsyncTelnyx

from config import settings

logger = logging.getLogger(__name__)

router = APIRouter()

_telnyx_client: AsyncTelnyx | None = None

# Simple bounded in-memory per-IP request log — same pattern as the rest
# of this project's module-level state (_tts_health, _active_streams).
# Not multi-process safe; fine for this project's single-instance deploy.
_request_times: dict[str, list[float]] = defaultdict(list)
RATE_LIMIT_WINDOW_S = 60.0
MAX_TRACKED_IPS = 1000  # opportunistic cleanup trigger, not a hard cap


def _client_ip(request: Request) -> str:
    # uvicorn's ProxyHeadersMiddleware is already active by default
    # (proxy_headers=True, trusting 127.0.0.1) and has rewritten
    # request.client.host from X-Forwarded-For — but ONLY when the
    # connecting peer is in its trusted set. READING THE RAW HEADER
    # HERE BYPASSES THAT TRUST CHECK and lets a caller spoof a client IP
    # to evade the rate limit. Use request.client.host, which is either
    # the real client IP (peer trusted) or the proxy's address (peer not
    # trusted -> everyone shares one bucket, merely ineffective, not
    # exploitable).
    return request.client.host if request.client else "unknown"


def _rate_limited(ip: str) -> bool:
    now = time.monotonic()
    window_start = now - RATE_LIMIT_WINDOW_S
    recent = [t for t in _request_times[ip] if t > window_start]
    recent.append(now)
    _request_times[ip] = recent

    if len(_request_times) > MAX_TRACKED_IPS:
        for key in list(_request_times):
            if not any(t > window_start for t in _request_times[key]):
                _request_times.pop(key, None)

    return len(recent) > settings.telnyx_token_rate_limit_per_minute


def _get_client() -> AsyncTelnyx:
    global _telnyx_client
    if _telnyx_client is None:
        _telnyx_client = AsyncTelnyx(api_key=settings.telnyx_api_key)
    return _telnyx_client


@router.get("/telnyx-token")
async def get_webrtc_token(
    request: Request,
    x_najda_demo_token: str | None = Header(default=None),
):
    if not settings.demo_token_secret:
        # Fail CLOSED, never open: an unset secret must never silently
        # mean "anyone may call this" — that was the exact bug this fixes.
        logger.error(
            "DEMO_TOKEN_SECRET is not set — refusing all /telnyx-token "
            "requests until it's configured in .env"
        )
        raise HTTPException(status_code=503, detail="token endpoint not configured")

    if not x_najda_demo_token or not secrets.compare_digest(
        x_najda_demo_token, settings.demo_token_secret
    ):
        raise HTTPException(status_code=401, detail="invalid or missing token")

    ip = _client_ip(request)
    if _rate_limited(ip):
        logger.warning(f"/telnyx-token rate-limited for {ip}")
        raise HTTPException(status_code=429, detail="too many requests, try again shortly")

    credential_id = settings.telnyx_telephony_credential_id
    if not credential_id:
        return {"error": "TELNYX_TELEPHONY_CREDENTIAL_ID not set in .env"}

    client = _get_client()
    try:
        token = await client.telephony_credentials.create_token(credential_id)
        return {"token": token}
    except Exception as e:
        logger.exception("failed to generate Telnyx WebRTC token")
        return {"error": str(e)}
