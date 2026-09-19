import logging
import sys

from websockets.exceptions import InvalidMessage

from ws_probes import DropEmptyWebsocketHandshake


def _record(exc: BaseException | None) -> logging.LogRecord:
    record = logging.LogRecord(
        "websockets", logging.ERROR, __file__, 0, "opening handshake failed", (), None
    )
    if exc is None:
        return record
    try:
        raise exc
    except Exception:
        record.exc_info = sys.exc_info()
    return record


def test_drops_empty_handshake_and_keeps_other_errors():
    filt = DropEmptyWebsocketHandshake()
    assert filt.filter(_record(EOFError("stream ends after 0 bytes"))) is False
    assert filt.filter(_record(InvalidMessage("did not receive a valid HTTP request"))) is False
    assert filt.filter(_record(RuntimeError("origin not allowed"))) is True
    assert filt.filter(_record(None)) is True
    probe = logging.LogRecord(
        "websockets.server",
        logging.ERROR,
        __file__,
        0,
        "did not receive a valid HTTP request",
        (),
        None,
    )
    assert filt.filter(probe) is False
