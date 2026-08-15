from __future__ import annotations

import pytest

from personal_voice_msg.sender import _find_matching_provider_id, _ReconcileQueryFailed

WINDOW_START = 1_700_000_000.0


def _voice_message(
    *, timestamp: object = WINDOW_START + 1, provider_id: object = "RAW-ID-123"
) -> dict[str, object]:
    """A well-formed outgoing voice message, with timestamp/id overridable
    to construct malformed variants for the escalation tests below."""

    message: dict[str, object] = {
        "fromMe": True,
        "hasMedia": True,
        "media": {"mimetype": "audio/ogg; codecs=opus"},
        "timestamp": timestamp,
    }
    if provider_id is not None:
        message["_data"] = {"key": {"id": provider_id}}
    else:
        message["_data"] = {"key": {}}
    return message


@pytest.mark.fast
def test_finds_a_well_formed_matching_message() -> None:
    messages = [_voice_message()]

    result = _find_matching_provider_id(messages, WINDOW_START)

    assert result == "RAW-ID-123"


@pytest.mark.fast
def test_returns_none_when_nothing_qualifies_as_a_candidate() -> None:
    """Messages that never pass fromMe/hasMedia/mimetype are genuinely not
    candidates -- these must still be skipped silently, not escalated."""

    messages = [
        {"fromMe": False, "hasMedia": True, "media": {"mimetype": "audio/ogg"}},
        {"fromMe": True, "hasMedia": False, "media": {"mimetype": "audio/ogg"}},
        {"fromMe": True, "hasMedia": True, "media": {"mimetype": "text/plain"}},
        {"fromMe": True, "hasMedia": True, "media": None},
        "not-even-a-dict",
    ]

    assert _find_matching_provider_id(messages, WINDOW_START) is None


@pytest.mark.fast
def test_ignores_a_matching_message_before_the_window_start() -> None:
    messages = [_voice_message(timestamp=WINDOW_START - 100)]

    assert _find_matching_provider_id(messages, WINDOW_START) is None


@pytest.mark.fast
def test_finds_the_first_qualifying_message_after_a_non_candidate() -> None:
    messages = [
        {"fromMe": False, "hasMedia": True, "media": {"mimetype": "audio/ogg"}},
        _voice_message(provider_id="SECOND-MESSAGE-ID"),
    ]

    assert _find_matching_provider_id(messages, WINDOW_START) == "SECOND-MESSAGE-ID"


@pytest.mark.fast
def test_non_list_payload_raises_reconcile_query_failed() -> None:
    with pytest.raises(_ReconcileQueryFailed):
        _find_matching_provider_id({"not": "a list"}, WINDOW_START)


@pytest.mark.fast
@pytest.mark.parametrize("bad_timestamp", [None, "not-a-number", [], {}])
def test_malformed_timestamp_on_an_otherwise_matching_message_is_escalated(
    bad_timestamp: object,
) -> None:
    """This is the finding from independent review: once fromMe/hasMedia/
    mimetype have all matched -- WAHA is clearly telling us this message
    IS an outgoing voice note -- a malformed timestamp on that exact
    message must raise _ReconcileQueryFailed (-> DELIVERY_UNKNOWN in
    reconcile_delivery), not be silently treated as "not a match." A
    silent skip here would let a malformed shape on every candidate
    resolve indistinguishably from "genuinely nothing sent," which can
    reach AUDIO_READY once RECONCILE_GRACE_SECONDS elapses -- a
    duplicate-send outcome.
    """

    messages = [_voice_message(timestamp=bad_timestamp)]

    with pytest.raises(_ReconcileQueryFailed):
        _find_matching_provider_id(messages, WINDOW_START)


@pytest.mark.fast
def test_missing_provider_id_on_an_otherwise_matching_message_is_escalated() -> None:
    """Same escalation, for a matching message whose _data.key.id is
    missing/malformed instead of its timestamp."""

    messages = [_voice_message(provider_id=None)]

    with pytest.raises(_ReconcileQueryFailed):
        _find_matching_provider_id(messages, WINDOW_START)


@pytest.mark.fast
def test_malformed_shape_on_a_non_candidate_message_is_not_escalated() -> None:
    """The escalation is scoped to messages that already passed the
    substantive fromMe/hasMedia/mimetype gates. A message that never
    qualified as a candidate at all (e.g. it's a text message) having a
    garbage timestamp must still just be skipped, not raise."""

    messages = [
        {
            "fromMe": True,
            "hasMedia": False,
            "media": None,
            "timestamp": "garbage",
        },
        _voice_message(provider_id="REAL-MATCH"),
    ]

    assert _find_matching_provider_id(messages, WINDOW_START) == "REAL-MATCH"
