"""Security helpers: Meta webhook signature verification."""

import hashlib
import hmac


def verify_meta_signature(
    app_secret: str, payload: bytes, signature_header: str | None
) -> bool:
    """Verify the ``X-Hub-Signature-256`` header sent by Meta.

    Meta signs every webhook delivery with HMAC-SHA256 using the app secret.
    Returns True when the signature is valid. If no app secret is configured
    (local development), verification is skipped.
    """
    if not app_secret:
        return True
    if not signature_header or not signature_header.startswith("sha256="):
        return False
    expected = hmac.new(app_secret.encode(), payload, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature_header.removeprefix("sha256="))
