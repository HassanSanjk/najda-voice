"""
Telnyx WebRTC token endpoint.

Generates a short-lived JWT that allows the browser caller (telnyx_caller.html)
to authenticate with Telnyx's WebRTC platform and place a call.

Requires a Telnyx telephony credential ID. Set TELNYX_TELEPHONY_CREDENTIAL_ID
in .env after creating it in Telnyx Mission Control:
    API Keys -> Telephony Credentials -> Create credential -> copy the ID
"""

import logging

from fastapi import APIRouter
from telnyx import AsyncTelnyx

from config import settings

logger = logging.getLogger(__name__)

router = APIRouter()

_telnyx_client: AsyncTelnyx | None = None


def _get_client() -> AsyncTelnyx:
    global _telnyx_client
    if _telnyx_client is None:
        _telnyx_client = AsyncTelnyx(api_key=settings.telnyx_api_key)
    return _telnyx_client


@router.get("/telnyx-token")
async def get_webrtc_token():
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
