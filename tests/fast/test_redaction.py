import io
import logging

import pytest

from personal_voice_msg.redaction import REDACTED, RedactingFilter, Redactor


def render_log(
    redactor: Redactor,
    message: str,
    *args: object,
) -> str:
    output = io.StringIO()
    handler = logging.StreamHandler(output)
    handler.setFormatter(logging.Formatter("%(message)s"))
    handler.addFilter(RedactingFilter(redactor))

    logger = logging.getLogger(f"test.redaction.{id(output)}")
    logger.handlers.clear()
    logger.propagate = False
    logger.setLevel(logging.INFO)
    logger.addHandler(handler)
    try:
        logger.info(message, *args)
    finally:
        logger.removeHandler(handler)
        handler.close()

    return output.getvalue().rstrip("\n")


@pytest.mark.fast
def test_registered_sensitive_values_are_removed_from_formatted_log() -> None:
    token = "service-token-for-redaction-test"
    phone = "+14155550123"
    voice_path = "/run/secrets/voice/owner.embedding"
    session_path = "/var/lib/waha/sessions/production/session.json"
    redactor = Redactor((token, phone, voice_path, session_path))

    rendered = render_log(
        redactor,
        "blocked token=%s recipient=%s voice=%s session=%s reason=%s",
        token,
        phone,
        voice_path,
        session_path,
        "configuration rejected",
    )

    assert rendered == (
        "blocked token=[REDACTED] recipient=[REDACTED] voice=[REDACTED] "
        "session=[REDACTED] reason=configuration rejected"
    )
    for sensitive_value in (token, phone, voice_path, session_path):
        assert sensitive_value not in rendered


@pytest.mark.fast
def test_recognizable_token_and_phone_are_removed_without_registration() -> None:
    github_token = "gh" "p_1234567890abcdefghijklmnopqrstuvwxyz"
    phone = "+442079460123"

    rendered = render_log(
        Redactor(()),
        "provider token=%s destination=%s result=%s",
        github_token,
        phone,
        "rejected safely",
    )

    assert rendered == (
        "provider token=[REDACTED] destination=[REDACTED] "
        "result=rejected safely"
    )
    assert github_token not in rendered
    assert phone not in rendered


@pytest.mark.fast
def test_registered_value_is_removed_from_exception_traceback() -> None:
    secret = "exception-secret-for-redaction-test"
    output = io.StringIO()
    handler = logging.StreamHandler(output)
    handler.addFilter(RedactingFilter(Redactor((secret,))))
    logger = logging.getLogger(f"test.redaction.exception.{id(output)}")
    logger.handlers.clear()
    logger.propagate = False
    logger.setLevel(logging.ERROR)
    logger.addHandler(handler)
    try:
        try:
            raise RuntimeError(f"operation failed for {secret}")
        except RuntimeError:
            logger.exception("configuration failed")
    finally:
        logger.removeHandler(handler)
        handler.close()

    rendered = output.getvalue()
    assert REDACTED in rendered
    assert secret not in rendered
