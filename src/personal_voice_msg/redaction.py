from __future__ import annotations

import logging
import re
import traceback

REDACTED = "[REDACTED]"
GITHUB_TOKEN = re.compile(
    r"\b(?:gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,})\b"
)
E164_PHONE = re.compile(r"(?<!\w)\+[1-9][0-9]{7,14}(?![0-9])")

class SensitiveValue[SensitiveType]:
    """Require an explicit method call to access sensitive data."""

    __slots__ = ("_value",)

    def __init__(self, value: SensitiveType) -> None:
        self._value = value

    def reveal(self) -> SensitiveType:
        return self._value

    def __str__(self) -> str:
        return REDACTED

    def __repr__(self) -> str:
        return REDACTED


class Redactor:
    """Remove registered values and recognizable credentials from text."""

    def __init__(self, sensitive_values: tuple[str, ...]) -> None:
        self._sensitive_values = tuple(
            sorted(
                {value for value in sensitive_values if value},
                key=len,
                reverse=True,
            )
        )

    def redact(self, text: str) -> str:
        redacted = text
        for value in self._sensitive_values:
            redacted = redacted.replace(value, REDACTED)
        redacted = GITHUB_TOKEN.sub(REDACTED, redacted)
        return E164_PHONE.sub(REDACTED, redacted)


class RedactingFilter(logging.Filter):
    """Redact a fully formatted log message before handlers emit it."""

    def __init__(self, redactor: Redactor) -> None:
        super().__init__()
        self._redactor = redactor

    def filter(self, record: logging.LogRecord) -> bool:
        record.msg = self._redactor.redact(record.getMessage())
        record.args = ()
        if record.exc_info:
            exception_text = "".join(traceback.format_exception(*record.exc_info))
            record.exc_text = self._redactor.redact(exception_text)
            record.exc_info = None
        elif record.exc_text:
            record.exc_text = self._redactor.redact(record.exc_text)
        if record.stack_info:
            record.stack_info = self._redactor.redact(record.stack_info)
        return True


def install_redacting_filter(
    logger: logging.Logger,
    redactor: Redactor,
) -> None:
    """Install one redacting filter on every existing logger handler."""

    for handler in logger.handlers:
        if not any(
            isinstance(installed_filter, RedactingFilter)
            for installed_filter in handler.filters
        ):
            handler.addFilter(RedactingFilter(redactor))
