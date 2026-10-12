"""Production entry point:  gunicorn wsgi:app

``app.py`` run directly is one person on localhost, with no sign-in. This is
what a host runs, reachable from the internet - so it refuses to start without
the key that sessions are signed with, because without it there are no
accounts and every visitor would see the same bills.

    python -c "import secrets; print(secrets.token_urlsafe(48))"
"""

import os

from werkzeug.middleware.proxy_fix import ProxyFix


if len(os.environ.get("ELEC_SECRET_KEY", "")) < 32:
    raise SystemExit(
        "refusing to start: set ELEC_SECRET_KEY to a long random value (32+ characters).\n"
        "It signs the session cookies that keep each user's bills their own.")

# Cloud Run sets K_SERVICE. Its disk is memory: it empties on every restart and
# isn't shared between instances, so without a database every account and
# bill would quietly vanish. Better not to start.
if os.environ.get("K_SERVICE") and not os.environ.get("ELEC_DATABASE_URL"):
    raise SystemExit(
        "refusing to start: on Cloud Run, set ELEC_DATABASE_URL to your Supabase "
        "connection string. There is no disk here to keep anything on.")

from metermatters.web.app import app  # noqa: E402 - only once we know it is safe to

# A host puts a proxy in front that terminates TLS. Trust exactly that many
# hops for the client's address and scheme - trusting more lets a visitor
# forge their own address, and with it the sign-in rate limit.
hops = int(os.environ.get("ELEC_TRUSTED_PROXIES", "1"))
if hops:
    app.wsgi_app = ProxyFix(app.wsgi_app, x_for=hops, x_proto=hops, x_host=hops)

# The scheduler is started per worker, after the fork - see gunicorn.conf.py.
