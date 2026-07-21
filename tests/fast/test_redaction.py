import io
import logging
from dataclasses import asdict

import pytest

from personal_voice_msg import redaction
from personal_voice_msg.redaction import (
    REDACTED,
    RedactingFilter,
    Redactor,
    SensitiveValue,
)


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


@pytest.mark.fast
def test_sensitive_value_does_not_leak_through_dataclass_conversion() -> None:
    secret = "dataclass-conversion-secret"

    with pytest.raises(TypeError):
        asdict(SensitiveValue(secret))


@pytest.mark.fast
def test_install_redacting_filter_covers_handlers_once_and_exception_logs() -> None:
    secret = "central-filter-secret"
    phone = "+14155550123"
    token = "waha-central-filter-token"
    redactor = Redactor((secret, phone, token))
    outputs = (io.StringIO(), io.StringIO())
    handlers = tuple(logging.StreamHandler(output) for output in outputs)
    logger = logging.getLogger(f"test.redaction.central.{id(outputs)}")
    logger.handlers.clear()
    logger.propagate = False
    logger.setLevel(logging.INFO)
    for handler in handlers:
        logger.addHandler(handler)

    install_redacting_filter = getattr(redaction, "install_redacting_filter")
    try:
        install_redacting_filter(logger, redactor)
        install_redacting_filter(logger, redactor)
        logger.info("recipient=%s token=%s", phone, token)
        try:
            raise RuntimeError(f"operation failed for {secret}")
        except RuntimeError:
            logger.exception("request failed token=%s", token)
    finally:
        for handler in handlers:
            logger.removeHandler(handler)
            handler.close()

    for handler, output in zip(handlers, outputs, strict=True):
        filters = [
            installed_filter
            for installed_filter in handler.filters
            if isinstance(installed_filter, RedactingFilter)
        ]
        assert len(filters) == 1
        rendered = output.getvalue()
        assert REDACTED in rendered
        for plaintext in (secret, phone, token):
            assert plaintext not in rendered


@pytest.mark.fast
def test_install_redacting_filter_covers_propagated_parent_handler() -> None:
    secret = "propagated-secret"
    output = io.StringIO()
    handler = logging.StreamHandler(output)
    parent = logging.getLogger(f"test.redaction.parent.{id(output)}")
    child = parent.getChild("child")
    parent.handlers.clear()
    child.handlers.clear()
    parent.propagate = False
    child.propagate = True
    parent.setLevel(logging.INFO)
    child.setLevel(logging.INFO)
    parent.addHandler(handler)
    try:
        redaction.install_redacting_filter(child, Redactor((secret,)))
        child.info("secret=%s", secret)
    finally:
        parent.removeHandler(handler)
        handler.close()

    assert output.getvalue().strip() == "secret=[REDACTED]"


@pytest.mark.fast
def test_install_redacting_filter_replaces_obsolete_redactor() -> None:
    old_secret = "old-secret"
    new_secret = "new-secret"
    output = io.StringIO()
    handler = logging.StreamHandler(output)
    logger = logging.getLogger(f"test.redaction.refresh.{id(output)}")
    logger.handlers.clear()
    logger.propagate = False
    logger.setLevel(logging.INFO)
    logger.addHandler(handler)
    try:
        redaction.install_redacting_filter(logger, Redactor((old_secret,)))
        redaction.install_redacting_filter(logger, Redactor((new_secret,)))
        logger.info("secret=%s", new_secret)
    finally:
        logger.removeHandler(handler)
        handler.close()

    filters = [item for item in handler.filters if isinstance(item, RedactingFilter)]
    assert len(filters) == 1
    assert output.getvalue().strip() == "secret=[REDACTED]"
