"""Tests for cer.runtime.logging."""

import io
import json
import logging

import pytest

from cer.runtime.logging import bind_context, configure_logging


@pytest.fixture(autouse=True)
def _reset_root_logger():
    """Ensure each test starts with a clean root logger, and clean up after."""
    root = logging.getLogger()
    original_handlers = list(root.handlers)
    original_level = root.level
    for h in original_handlers:
        root.removeHandler(h)
    yield
    for h in list(root.handlers):
        root.removeHandler(h)
    for h in original_handlers:
        root.addHandler(h)
    root.setLevel(original_level)


def _configure_and_get_stream(level="INFO"):
    stream = io.StringIO()
    configure_logging(level, stream=stream)
    return stream


def test_log_output_is_single_line_json_with_required_fields():
    stream = _configure_and_get_stream()
    logger = logging.getLogger("cer.test")

    logger.info("hello world")

    lines = [line for line in stream.getvalue().splitlines() if line]
    assert len(lines) == 1
    record = json.loads(lines[0])  # must parse as JSON

    assert record["msg"] == "hello world"
    assert record["level"] == "INFO"
    assert record["logger"] == "cer.test"
    assert "module" in record
    assert "line" in record
    assert isinstance(record["line"], int)


def test_ts_is_iso8601_utc():
    stream = _configure_and_get_stream()
    logger = logging.getLogger("cer.test.ts")

    logger.info("time check")

    record = json.loads(stream.getvalue().splitlines()[0])
    ts = record["ts"]
    assert ts.endswith("Z") or ts.endswith("+00:00")

    from datetime import datetime

    # Must be parseable back into a datetime.
    normalized = ts[:-1] + "+00:00" if ts.endswith("Z") else ts
    parsed = datetime.fromisoformat(normalized)
    assert parsed.tzinfo is not None
    assert parsed.utcoffset().total_seconds() == 0


def test_extra_fields_are_merged_at_top_level():
    stream = _configure_and_get_stream()
    logger = logging.getLogger("cer.test.extra")

    logger.info("evidence appended", extra={"request_id": "req-123", "producer": "HSA"})

    record = json.loads(stream.getvalue().splitlines()[0])
    assert record["request_id"] == "req-123"
    assert record["producer"] == "HSA"
    # not nested under an "extra" key
    assert "extra" not in record


def test_configure_logging_twice_does_not_duplicate_handlers():
    stream1 = io.StringIO()
    configure_logging("INFO", stream=stream1)
    configure_logging("INFO", stream=stream1)

    root = logging.getLogger()
    json_handlers = [h for h in root.handlers if getattr(h, "_cer_json_handler", False)]
    assert len(json_handlers) == 1

    logger = logging.getLogger("cer.test.dup")
    logger.info("only once")

    lines = [line for line in stream1.getvalue().splitlines() if line]
    assert len(lines) == 1


def test_exception_log_carries_traceback_field():
    stream = _configure_and_get_stream()
    logger = logging.getLogger("cer.test.exc")

    try:
        raise ValueError("boom")
    except ValueError:
        logger.exception("something failed")

    record = json.loads(stream.getvalue().splitlines()[0])
    assert "exception" in record
    assert "ValueError: boom" in record["exception"]
    assert "Traceback" in record["exception"]


def test_non_serializable_extra_value_does_not_crash_logging():
    stream = _configure_and_get_stream()
    logger = logging.getLogger("cer.test.weird")

    class Unserializable:
        def __str__(self):
            return "<unserializable-repr>"

    # Must not raise.
    logger.info("weird value", extra={"payload": Unserializable()})

    lines = [line for line in stream.getvalue().splitlines() if line]
    assert len(lines) == 1
    record = json.loads(lines[0])  # still valid JSON
    assert "unserializable-repr" in record["payload"]


def test_bind_context_stamps_persistent_fields_on_every_record():
    stream = _configure_and_get_stream()
    base_logger = logging.getLogger("cer.test.context")
    bound = bind_context(base_logger, request_id="req-abc", producer="APOLLO")

    bound.info("first")
    bound.info("second", extra={"extra_field": "x"})

    lines = [line for line in stream.getvalue().splitlines() if line]
    assert len(lines) == 2
    first = json.loads(lines[0])
    second = json.loads(lines[1])

    assert first["request_id"] == "req-abc"
    assert first["producer"] == "APOLLO"
    assert second["request_id"] == "req-abc"
    assert second["producer"] == "APOLLO"
    assert second["extra_field"] == "x"


def test_configure_logging_sets_level():
    stream = io.StringIO()
    configure_logging("WARNING", stream=stream)

    logger = logging.getLogger("cer.test.level")
    logger.info("should be suppressed")
    logger.warning("should appear")

    lines = [line for line in stream.getvalue().splitlines() if line]
    assert len(lines) == 1
    record = json.loads(lines[0])
    assert record["msg"] == "should appear"
