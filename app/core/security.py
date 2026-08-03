"""Security helpers: Meta webhook signature verification."""

import hashlib
import hmac


def verify_meta_signature(
    app_secret: str,
    payload: bytes,
    signature_header: str | None,
    *,
    allow_unsigned: bool = False,
) -> bool:
    """Verify the ``X-Hub-Signature-256`` header sent by Meta.

    Meta signs every delivery with HMAC-SHA256 over the RAW request body,
    using the app secret. Returns True only when that signature is present
    and correct.

    ``allow_unsigned`` exists for local development, where no app secret is
    configured and deliveries are replayed by hand. It defaults to False, and
    that default is the entire point of this signature.

    An unset secret used to mean "skip the check", which reads as harmless
    until you notice the code cannot tell a developer's laptop from a
    production stack whose ``secrets/whatsapp_app_secret`` file happens to be
    empty -- a file ``init-secrets.sh`` creates and nothing validates. In that
    state webhook authentication is off and nothing says so: no error, no
    warning, no failed health check. Anyone who learned the URL could inject
    customer messages, and every injected message costs an OpenAI completion
    and sends a real WhatsApp reply to a real number.

    So callers must now opt in deliberately, and app/routers/webhook.py only
    does so outside production. The failure mode is inverted: a missing
    secret now rejects traffic loudly instead of accepting it quietly.
    """
    if not app_secret:
        return allow_unsigned
    if not signature_header or not signature_header.startswith("sha256="):
        return False
    expected = hmac.new(app_secret.encode(), payload, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature_header.removeprefix("sha256="))


def verify_token_matches(configured: str, provided: str | None) -> bool:
    """Constant-time comparison for the webhook verification token.

    ``==`` on a secret short-circuits at the first differing byte, so how long
    it takes reveals how much of a matching prefix the caller supplied. This
    endpoint is only useful to somebody configuring the Meta app and the token
    is long, so the hole is small -- but ``compare_digest`` costs nothing and
    removes the need to keep judging how small.

    An unset token never matches. Under ``==`` an empty configured token
    compared equal to an empty query parameter, so a misconfigured deployment
    would hand the challenge to anyone who asked.
    """
    if not configured or not provided:
        return False
    return hmac.compare_digest(configured, provided)
