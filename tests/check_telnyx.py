"""Check Telnyx SDK create_token signature."""
import inspect
from telnyx.resources.telephony_credentials import AsyncTelephonyCredentialsResource

src = inspect.getsource(AsyncTelephonyCredentialsResource.create_token)
print(src[:2000])
