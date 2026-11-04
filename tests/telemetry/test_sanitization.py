"""Event details are sanitized as any other mapping is, then bounded."""

from microcosm_emitter.telemetry.service.sanitization import (
    sanitize_details,
    sanitize_json,
)


def test_details_within_the_size_limit_are_sanitized_as_any_mapping_is():
    details = {
        "done": 2,
        "nested": {"HF_TOKEN": "hf_abcdefghijk", "values": [1, 2.5, float("nan")]},
        "message": "Bearer hf_abcdefghijk",
        7: "a key that is not text",
    }
    sanitized = sanitize_details(details)
    assert sanitized == sanitize_json(details)
    assert sanitized == {
        "done": 2,
        "nested": {"HF_TOKEN": "[redacted]", "values": [1, 2.5, None]},
        "message": "[redacted]",
        "7": "a key that is not text",
    }
