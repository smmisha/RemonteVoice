"""Small, best-effort session records; never log peer reasons or exception text."""
from __future__ import annotations

import re
import time

_LOCAL_REASONS = {
    "keepalive ping timeout": "heartbeat_timeout",
    "Audio device error": "audio_worker_error",
    "Audio progress stopped": "audio_progress_stopped",
    "Host audio error": "host_error",
    "Host stopping": "host_stopping",
    "Audio readiness timeout": "audio_ready_timeout",
}
_STAGES = {"route_prepare", "audio_setup", "auth_reply", "audio_ready", "audio_start", "stream",
           "audio_progress", "audio_cleanup", "system_send", "system_capture", "phone_playback"}
_FAILURES = {"none", "route_prepare_timeout", "audio_start_timeout", "cancelled", "session_error"}


def _tag(value):
    return value if isinstance(value, str) and re.fullmatch(r"[0-9a-f]{32}", value) else "unavailable"


def session_id_for(audio, fallback):
    try:
        candidate = _tag(getattr(audio, "diagnostic_id", None))
        return _tag(fallback) if candidate == "unavailable" else candidate
    except Exception:
        return _tag(fallback)


def _code(frame):
    value = getattr(frame, "code", None)
    return int(value) if isinstance(value, int) and not isinstance(value, bool) and 1000 <= value <= 4999 else 0


def close_summary(websocket):
    protocol = getattr(websocket, "protocol", None)
    sent, received = getattr(protocol, "close_sent", None), getattr(protocol, "close_rcvd", None)
    order = getattr(protocol, "close_rcvd_then_sent", None)
    sent_code, received_code = _code(sent), _code(received)
    value = getattr(websocket, "close_code", None)
    close_code = int(value) if isinstance(value, int) and not isinstance(value, bool) and 1000 <= value <= 4999 else 0
    origin, cause = "unknown", "unclassified"
    if sent is not None and (received is None or order is False):
        origin = "local"
        reason = getattr(sent, "reason", None)
        reason = reason if isinstance(reason, str) and len(reason) <= 64 else None
        cause = _LOCAL_REASONS.get(reason, "local_internal_unclassified" if sent_code == 1011 else "local_close")
    elif received is not None and (sent is None or order is True):
        origin = "peer"
        cause = "peer_normal" if received_code in (1000, 1001) else "peer_error"
    elif sent is None and received is None and close_code == 1006:
        origin, cause = "transport", "transport_lost"
    return dict(origin=origin, cause=cause, sent_code=sent_code, received_code=received_code, close_code=close_code)


def log_session_end(logger, websocket, *, session_id, stage, failure):
    try:
        details = close_summary(websocket)
        logger.info("audio_session_end session=%s origin=%s cause=%s sent_code=%d received_code=%d "
                    "close_code=%d stage=%s failure=%s clock_ns=%d", _tag(session_id),
                    details["origin"], details["cause"], details["sent_code"], details["received_code"],
                    details["close_code"], stage if stage in _STAGES else "unknown",
                    failure if failure in _FAILURES else "unknown", time.monotonic_ns())
    except Exception:
        pass  # Diagnostics must not delay or prevent route restoration or native stop.


def _log_failure(method, event, *, session_id, stage, error):
    try:
        name = type(error).__name__
        name = name if re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{0,63}", name) else "OtherError"
        method(event + " stage=%s error_type=%s session=%s clock_ns=%d",
               stage if stage in _STAGES else "unknown", name, _tag(session_id), time.monotonic_ns())
    except Exception:
        pass


def log_session_error(logger, *, session_id, stage, error):
    _log_failure(logger.error, "audio_session_failure", session_id=session_id, stage=stage, error=error)


def log_worker_failure(logger, *, session_id, stage, error):
    _log_failure(logger.warning, "audio_worker_failure", session_id=session_id, stage=stage, error=error)
