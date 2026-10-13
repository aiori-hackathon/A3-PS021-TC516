"""Vercel entry point: the Threadline JSON API as one Python function.

vercel.json rewrites every /api/* request here and serves the dashboard from
static/ through the CDN. Vercel functions have no persistent disk, so the
event log lives in the instance's temp directory: it survives while an
instance stays warm and starts empty after a cold start. Use
``python server.py`` for a durable log.
"""

import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from server import make_handler  # noqa: E402
from threadline.service import Threadline  # noqa: E402
from threadline.store import EventStore  # noqa: E402

DB = os.environ.get("THREADLINE_DB", os.path.join(tempfile.gettempdir(), "threadline.db"))

_Handler = make_handler(Threadline(EventStore(DB)))


class handler(_Handler):
    """Vercel serves a module-level BaseHTTPRequestHandler subclass named `handler`.

    It must be a class statement: Vercel's build only detects ``class handler``
    (or ``app = ...``), not a plain ``handler = ...`` assignment.
    """
