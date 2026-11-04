"""Response headers and log formatting for the gateway."""

import logging
import traceback

# Every served page loads same-origin scripts, styles and images only; camera
# snapshots are shown through blob: URLs. WebRTC and MediaStream playback are
# not governed by these directives. No HSTS: households reach the gateway
# through a self-signed certificate exception that HSTS would forbid.
CONTENT_SECURITY_POLICY = ("default-src 'self'; img-src 'self' blob:; object-src 'none'; base-uri 'none'; "
                           "frame-ancestors 'none'; form-action 'self'")


class SecurityHeaders:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        response = self.get_response(request)
        response.setdefault("Content-Security-Policy", CONTENT_SECURITY_POLICY)
        return response


class PrivateRecord(logging.Filter):
    """Drop request objects that Django attaches to its request log records."""

    def filter(self, record):
        for name in ("request", "scope"):
            if hasattr(record, name):
                setattr(record, name, None)
        return True


class PrivateFormatter(logging.Formatter):
    """Tracebacks keep their frames and exception classes but never exception
    messages, which may carry request, upstream or credential text."""

    def formatException(self, info):
        kind, value, trace = info
        lines = ["Traceback (most recent call last):"]
        lines += [f'  File "{frame.filename}", line {frame.lineno}, in {frame.name}' for frame in traceback.extract_tb(trace)]
        lines.append(kind.__module__ + "." + kind.__qualname__ if kind else "Exception")
        return "\n".join(lines)
