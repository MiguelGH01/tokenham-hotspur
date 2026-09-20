"""Drop websockets handshake noise from empty TCP probes (Cursor port preview, etc.)."""

import logging

from websockets.exceptions import InvalidMessage


class DropEmptyWebsocketHandshake(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        exc = record.exc_info[1] if record.exc_info else None
        if isinstance(exc, (EOFError, InvalidMessage)):
            return False
        if isinstance(getattr(exc, "__cause__", None), EOFError):
            return False
        msg = record.getMessage()
        if "did not receive a valid HTTP request" in msg:
            return False
        return True


def quiet_empty_websocket_probes() -> None:
    filt = DropEmptyWebsocketHandshake()
    logging.getLogger("websockets").addFilter(filt)
    logging.getLogger("websockets.server").addFilter(filt)
