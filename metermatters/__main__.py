"""The development server:  python -m metermatters

One person on localhost with no sign-in, the files in the checkout. A host
runs ``gunicorn wsgi:app`` instead (see wsgi.py).
"""

import os

from metermatters.web.app import app

port = int(os.environ.get("PORT", 5000))
print(f"\n  Meter Matters  ->  http://127.0.0.1:{port}\n")
# Development only, and only on localhost. The reloader picks up edits without
# a manual restart. It is not `debug=True`: that would also expose Werkzeug's
# interactive debugger, which has no business being on even a local-only port.
app.run(host="127.0.0.1", port=port,
        debug=bool(os.environ.get("DEBUG")),
        use_reloader=not os.environ.get("NO_RELOAD"))
