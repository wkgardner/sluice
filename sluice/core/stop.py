"""The cooperative shutdown flag shared by the daemon and long-running gates.

One module-level ``threading.Event``, nothing else. The daemon's signal
handlers set it (decision A: a signal carries zero control semantics — this
flag asks work to stop *submitting*, never to abandon what is in flight), and
long-running gate internals poll it at their submission boundaries. It lives
in core so gates can import it without touching ``core.daemon`` (which imports
the gates registry — a direct import would be circular).

``run_forever`` clears the flag at startup, so a flag left set by a previous
shutdown can never make a fresh daemon refuse work.
"""

import threading

SHUTDOWN = threading.Event()
