# forge-ops-tracker

Python error reporting client for [ForgeOps](https://getforgeops.net).
Requires Python 3.9+. It captures unhandled and explicitly reported exceptions, builds a backtrace,
scrubs likely PII, and delivers events to ForgeOps over HTTP without blocking the request or
process that raised them.

> For health or personal data, turn on [strict privacy](#strict-privacy): the SDK then sends only structured facts about each error (its type, stack trace, release and route), never the message, a raw path, breadcrumbs, or context you haven't allowed.

## Installation

```bash
pip install forge-ops-tracker
```

For Django or Flask integration, install the matching extra:

```bash
pip install "forge-ops-tracker[django]"
pip install "forge-ops-tracker[flask]"
```

For outbound HTTP span capture (see Distributed tracing below), install the `requests` extra too:

```bash
pip install "forge-ops-tracker[requests]"
```

For Redis span capture, the `redis` extra:

```bash
pip install "forge-ops-tracker[redis]"
```

## Configuration

Set a DSN (from a project's settings page in ForgeOps), either via the `FORGE_OPS_DSN` environment
variable or explicitly:

```python
import forge_ops_tracker

forge_ops_tracker.init(
    dsn="https://<api_key>@getforgeops.net/api/v1/events",  # or leave unset to read FORGE_OPS_DSN
    release="...",
    environment="production",
)
```

Call `init()` once at startup: Django's `settings.py`, or right after creating a Flask app. Any
`Configuration` attribute can be overridden by keyword.

The environment comes from `environment=` or `FORGE_OPS_ENVIRONMENT`, and is `production` when
neither is set. Only `production` and `staging` send by default (`enabled_environments`), so set
`FORGE_OPS_ENVIRONMENT=development` on a development machine or in CI to keep it from reporting
there. When a DSN is set but the environment isn't enabled, `init()` logs one warning saying so.

### Django

```python
# settings.py
import forge_ops_tracker

forge_ops_tracker.init(dsn="https://<api_key>@getforgeops.net/api/v1/events")

MIDDLEWARE = [
    ...,
    "django.contrib.auth.middleware.AuthenticationMiddleware",  # if not already there
    "forge_ops_tracker.integrations.django.ForgeOpsTrackerBreadcrumbContextMiddleware",
    "forge_ops_tracker.integrations.django.ForgeOpsTrackerTracingMiddleware",
    "forge_ops_tracker.integrations.django.ForgeOpsTrackerMiddleware",
    "forge_ops_tracker.integrations.django.ForgeOpsTrackerSessionTrackingMiddleware",
    "forge_ops_tracker.integrations.django.ForgeOpsTrackerPerformanceTrackingMiddleware",
    "forge_ops_tracker.integrations.django.ForgeOpsTrackerUserContextMiddleware",
]
```

### Flask

```python
from flask import Flask
import forge_ops_tracker
from forge_ops_tracker.integrations.flask import init_flask

forge_ops_tracker.init(dsn="https://<api_key>@getforgeops.net/api/v1/events")

app = Flask(__name__)
init_flask(app)
```

## What gets reported automatically, and what doesn't

**An exception that crashes a request needs no further wiring at all.** The Django middleware's
`process_exception` hook and Flask's `got_request_exception` signal both fire for anything that
propagates uncaught out of a view, then let the framework handle it exactly as if this client
weren't installed.

**An exception your own code catches and handles is different: neither integration ever sees
it**, since it never propagates far enough to reach either hook:

```python
try:
    charge_card(order)
except CardError as e:
    logger.warning("card declined: %s", e)
    # ForgeOps never sees this: caught locally, never reaches the
    # middleware/signal at all.
```

There's no application-wide hook that reports an exception while still letting your own `except`
block handle it: report it explicitly instead, right at the catch site:

```python
except CardError as e:
    forge_ops_tracker.capture_exception(e, context={"order_id": order.id})
    logger.warning("card declined: %s", e)
```

Called with no arguments, `capture_exception()` picks up whichever exception is currently being
handled (same as a bare `raise` inside an `except:` block), so it usually reads as just
`forge_ops_tracker.capture_exception()` from inside the block that already caught it.

### Outside a web request (scripts, management commands, workers)

`init()` also installs a `sys.excepthook` wrapper by default (`Configuration.install_excepthook`,
`True` unless set otherwise), which reports anything that crashes the whole interpreter: a plain
script, a Django management command, a worker's own top-level loop: with no wiring needed, the
same "unhandled needs no wiring" case the Django/Flask integrations cover for web requests. It
still calls whatever `sys.excepthook` was already installed afterward, so it never changes program
behavior. This does **not** catch a web request's unhandled exception under a real WSGI server
(Gunicorn/uWSGI catch that themselves per-request, long before it would ever reach the interpreter
level): that's what the Django/Flask integrations are for.

Delivery happens on a background thread with a bounded queue and a short per-request HTTP timeout
(`Configuration.timeout`, 2s default). Every failure mode: network errors, timeouts, a full queue,
a malformed DSN: is caught and dropped rather than raised, so a broken or unreachable tracker can
never take down the host app. The worker thread starts lazily, on first push, not at import time:
Gunicorn (prefork) and uWSGI commonly fork worker processes *after* the application has already
loaded, which would leave an eagerly-started thread dead in every forked child; starting fresh on
first push means each forked worker gets its own live thread regardless of when it was forked
relative to import.

Anything still queued when the process exits normally (the script ends, `sys.exit()`, or an
uncaught exception the hook above has just reported) is sent before the interpreter shuts down, so
a short-lived script or cron job doesn't lose the errors it reported in its last moments. That
exit wait is capped at 5 seconds in total, and a process that never reported anything doesn't
wait at all. It can't run if the process ends some other way (`os._exit()`, a kill signal).

## Where an error happened

An error reported during a request the Django/Flask integrations are handling (unhandled, or your
own `capture_exception()` from inside the view) carries three extra fields, set as soon as the
framework has matched the request to a route:

- `transaction_name`: the method and the URL pattern, the same name performance monitoring and
  traces use: `"GET orders/<int:order_id>/"` on Django, `"GET /orders/<int:order_id>"` on Flask.
- `endpoint`: the method and the route as declared in your URL patterns, `"GET
  /orders/<int:order_id>/"`. Never the literal path, so an id or a token in the URL never ends up
  here.
- `trace_id`: the request's W3C trace id, which ForgeOps uses to link this error to errors that
  other services reported for the same trace (see "Following a request across services" below).

They're left out entirely outside a request (a Celery task, a script), and they're never
PII-scrubbed, like `environment` and `release`: they're structured fields, not free text. Django
needs `ForgeOpsTrackerMiddleware` (or `ForgeOpsTrackerTracingMiddleware`) in `MIDDLEWARE`; Flask
needs `init_flask(app)`.

## Identifying users

```python
forge_ops_tracker.capture_exception(error, user={"id": user.id, "email": user.email})
```

Or `set_user(id=None, email=None, username=None)` to attach it for the rest of the current thread
(a WSGI request, a background worker, a console session) rather than passing it to every
`capture_exception()` call by hand:

```python
forge_ops_tracker.set_user(id=request.user.id, email=request.user.email)
```

Thread-local, not global: a WSGI server (Django/Flask's own deployment model) runs one request per
thread, so this is the right isolation boundary; not correct under an ASGI/asyncio deployment,
where several requests can share one OS thread, but this SDK has no async integration today for
that gap to matter yet. `id`/`email`/`username` are all independently optional; call `set_user()`
with none of them to clear whatever was set, e.g. once a request finishes. Shows up on an issue's
own detail page, and as its own affected-users count alongside the regular event count.

**Automatic detection, if you're using either integration above:**

- **Django**: `ForgeOpsTrackerUserContextMiddleware` (see the Django snippet above; must be listed
  after `django.contrib.auth.middleware.AuthenticationMiddleware`, or whatever else sets
  `request.user`) reads `request.user` when it's present and `is_authenticated`, and calls
  `set_user()` for you: `pk` for `id` (present on every model instance, regardless of your own
  `AUTH_USER_MODEL`), `email` if the attribute exists, and `get_username()` (correct even with a
  custom `USERNAME_FIELD`) if it does. A no-op for an app with no such middleware installed at all.
- **Flask**: `init_flask()` detects [Flask-Login](https://flask-login.readthedocs.io/) if it's
  installed and configured (a genuinely optional dependency this package never requires): `id`
  comes from `current_user.get_id()` (the only thing Flask-Login's own `UserMixin` actually
  guarantees); `email`/`username` are read as plain optional attributes, since Flask-Login itself
  guarantees neither, the common convention most apps' own `User` model follows regardless. A
  no-op if Flask-Login isn't installed, isn't configured (no `LoginManager` attached to this app),
  or nobody's logged in.

Both compose with the manual API above rather than replacing it: call `set_user()` yourself
afterward (e.g. for a custom auth setup neither integration can detect, or to override what was
auto-detected) and it wins for the rest of that request.

## Breadcrumbs

A trail of what happened right before an error, on by default, no setup needed beyond the
Django/Flask integration above: every SQL query (Django's own ORM automatically; any tracked
SQLAlchemy engine too) and request/controller lifecycle is recorded automatically, and shows up
alongside the error on an issue's own detail page.

```python
forge_ops_tracker.init(
    dsn="...",
    track_breadcrumbs=False,  # opt out of the automatic sources entirely
    max_breadcrumbs=30,       # oldest entry dropped once this many have accumulated in one trail
)
```

Add your own by hand, regardless of whether the automatic sources are on:

```python
forge_ops_tracker.add_breadcrumb("charged card", category="billing", data={"order_id": order.id})
```

`category` defaults to `"custom"`, `level` to `"info"` (`"debug"`/`"info"`/`"warning"`/`"error"`
are the four levels the automatic sources themselves use too), and `data` to `{}`. Works outside a
request entirely too (a background task, a console session): the trail it adds to is created
lazily on whatever context calls it, the same "works standalone, no specific setup required" shape
`set_user()` already has, rather than silently doing nothing with no active request/task around it.

Each request or Celery task gets its own fresh, bounded trail (a ring buffer capped at
`max_breadcrumbs`, oldest entry dropped once full), scoped with a `contextvars.ContextVar` rather
than the thread-local `set_user()` above uses: a plain WSGI worker thread behaves identically
either way, but this also stays correctly isolated under a Celery worker's own thread pool (which,
unlike its prefork processes, does reuse one OS thread across many unrelated task runs) and any
future asyncio/ASGI integration, where several requests could otherwise interleave on a single OS
thread as separate asyncio Tasks. Unlike the affected user above, a breadcrumb's `message`/`data`
**is** scrubbed for likely PII: console-style/query/request trail entries are exactly the kind of
free text (a bind parameter showing up in a message, a URL with a token in it) the scrubber exists
to catch, not a deliberately-structured field the way `user` is.

Celery tasks (with `init_celery()` from the section above) get their own automatic `"job"` category
breadcrumb too, recorded at the very start of the task, before its own body even runs: this is
deliberate, not an oversight, since Celery fires `task_failure` (which is what actually reports a
task's exception) *before* `task_postrun`, so a breadcrumb only added once the task finishes would
never make it into that same task's own failure report.

## `in_app` backtrace frames

Python runs interpreted directly from real `.py` files on disk, so file-path matching against
`Configuration.app_root` is a straightforward prefix comparison against those on-disk paths.
Defaults to the current working directory; set it explicitly if that doesn't match your app's
actual layout (a WSGI server started from a different directory than your app's root, for
instance). Standard-library and installed-package (`site-packages`/`dist-packages`) frames are
never marked `in_app`, regardless of `app_root`.

## PII scrubbing

By default, the message, backtrace, and any context/tags you attach are scanned
for likely personal data (email addresses, formatted SSNs/credit cards, known API key/token
formats, and anything under a suspiciously-named key like `password`, `api_key`, or `ssn`)
and redacted before the payload ever leaves this process. ForgeOps itself scrubs again on arrival
regardless, so this is a second, earlier layer, not the only one. The user attached via `user=`/
`set_user()` above is a deliberate exception: it's never scrubbed, since redacting it would defeat
the whole point of identifying users in the first place.

To disable it:

```python
forge_ops_tracker.init(dsn="...", scrub_pii=False)
```

## Strict privacy

For apps that can't let personal or health data (PII, PHI) leave their own servers. With strict
privacy on, the client sends only structured facts about an error (its class, its stack frames'
file, line and function, the release, environment, server, the request's `transaction_name` and
trace id, and the database tables or procedures involved), never free text that could hold a
name, an email or a diagnosis:

- No error message. A `message_fingerprint` is sent instead: a one-way hash of the message with
  its numbers, hex ids and UUIDs taken out, computed in your process, so errors still group
  without the message ever leaving it.
- No breadcrumbs (they aren't recorded at all), no `endpoint`, no SQL statement even with
  `capture_sql_statement=True`, and no source context (the source file is never read).
- The user is sent by `id` only, never `email` or `username`.
- Context and tags only keep the top-level keys you list in `privacy_allowed_fields`, and only
  when the value is a string, number or boolean. Nothing else from them is sent.
- Spans: an outbound HTTP call is named by method and host only (`GET api.example.com`), a request
  that matched no route is named by the first segment of its path only (`GET /patients/jane-doe/12`
  is sent as `GET /patients/:path`; a first segment with a digit becomes `:id`), and span data
  keeps only the masked `db.statement` and `db.system`.
- The same goes for that request's name in error events and performance samples. A route
  pattern such as `/patients/<int:id>` is kept as it is.

```python
forge_ops_tracker.init(
    dsn="...",
    strict_privacy=True,                               # default False
    privacy_allowed_fields=["error_code", "feature"],  # default []
)
```

Both can also come from the environment: `FORGE_OPS_STRICT_PRIVACY=true` and
`FORGE_OPS_PRIVACY_ALLOWED_FIELDS=error_code,feature` (comma separated). A value passed to `init()`
wins over the environment.

ForgeOps applies the same rules again on arrival when the project's own Strict privacy setting is
on, so an app with an older client, or one with this option off, still can't get that text stored.

## Source context

By default, each in-app backtrace frame (never a standard-library or installed-package frame) is
captured along with the 5 lines of source on either side of the culprit line, read straight off
disk at raise-time, so an issue's detail page can show the actual code that broke, not just a
`file:line:method` reference. This never applies to a frame outside your configured `app_root`, and
it fails silently (no context, not an exception) for any file that can't be read for whatever
reason.

This is a real, deliberate exception to "off by default is safer": literal source code is being
transmitted, not just a reference to it, and the real protection here is not this flag. Every
project on ForgeOps has its own setting (on by default, off durably and immediately once an org
owner turns it off, regardless of what any individual app's own `capture_source_context` is still
set to) that governs whether the server will ever actually store what a client sends. Set this to
`False` if you'd rather this client never even attempt the disk read in the first place:

```python
forge_ops_tracker.init(dsn="...", capture_source_context=False)
```

## Database errors

When an error comes from a database call (SQLAlchemy's `StatementError`/`DBAPIError`, or your own
exception raised from one), the event carries the names of the stored procedure or function and the
tables or views its SQL touched, so the issue tells you where to start looking. This is on by default
and sends identifiers only, never values. A view and a table are written the same way in SQL, so
both show as tables/views; the database's own error message usually settles which it was.

To also send the SQL statement itself, opt in. Every string and number is replaced by `?` before it
leaves your process (`WHERE email = 'a@b.co' AND id = 42` is sent as `WHERE email = ? AND id = ?`),
and ForgeOps masks it again on arrival:

```python
forge_ops_tracker.init(dsn="...", capture_sql_statement=True)  # default False
# capture_sql_objects=False (default True) stops even the names
```

Each ForgeOps project also has its own "Capture the SQL behind database errors" setting. Turn it off
there and the statement is never stored for that project, whatever this flag says; the names are
still kept.


## Session tracking (release health)

By default, every request through the Django/Flask integrations is counted as a session:
crash-free unless an unhandled exception (or a 5xx response, for Django, where the exception has
already been converted to a response by the time this client ever sees the request) actually
affects it, giving ForgeOps a crash-free rate per release to show alongside the errors themselves,
not just the errors on their own. Counted in-process and flushed as a small periodic aggregate on
a background thread (never one network call per request), the same delivery philosophy as
everything else in this client: a broken or unreachable tracker never affects the host app either
way.

```python
forge_ops_tracker.init(
    dsn="...",
    track_sessions=False,       # opt out entirely
    session_flush_interval=30,  # seconds; default 60
)
```

Requires a ForgeOps plan that includes release health; on a plan that doesn't, the periodic
flushes are accepted but not recorded, and the response says why.

## Performance monitoring

By default, the Django/Flask integrations also time every request, so a dashboard widget on
ForgeOps can show which parts of your app are actually slow, not just which ones raise. Bucketed
by transaction (`"GET /users/<int:id>"`, the matched URL pattern rather than the literal path, so
a distinct user id doesn't explode into its own separate transaction) and flushed as a small
periodic aggregate per transaction on the same kind of background thread session tracking above
uses.

Each aggregate also carries a small latency histogram (a count per fixed latency bucket: 50, 100,
250, 500, 1000, 2500, 5000 and 10000ms, plus an overflow bucket), so ForgeOps can show an
approximate p50/p95/p99 per transaction, not just an average. Percentiles are accurate to the width
of whichever bucket a duration falls into; the SDK never stores the individual durations.

```python
forge_ops_tracker.init(
    dsn="...",
    track_performance=False,        # opt out entirely
    performance_flush_interval=30,  # seconds; default 60
)
```

Requires a ForgeOps plan that includes performance monitoring; on a plan that doesn't, the
periodic flushes are accepted but not recorded, and the response says why.

### Database queries and Celery tasks

The same automatic instrumentation, on the same `track_performance` flag, also covers:

- **Database queries**, for Django automatically (no extra step beyond the middleware above) and
  for any SQLAlchemy engine (Flask-SQLAlchemy included) with one extra call:

  ```python
  from forge_ops_tracker.integrations.sqlalchemy import track_sqlalchemy_queries

  db = SQLAlchemy(app)
  track_sqlalchemy_queries(db.engine)
  ```

  Bucketed by `"<VERB> <table>"` (`"SELECT auth_user"`, `"INSERT INTO orders"`), not the raw SQL
  text: a low-cardinality name in the same spirit as the request transaction name above, and never
  a literal value even where a query's own parameters aren't already placeholder-bound.

- **Celery tasks**, via a separate integration (Celery is an optional dependency, same as
  Django/Flask):

  ```python
  from forge_ops_tracker.integrations.celery import init_celery

  init_celery()
  ```

  Bucketed by the task's own registered name (`"myapp.tasks.send_email"`). This is also this
  SDK's only error-reporting integration for Celery: a task that raises is reported the same way
  an unhandled Django/Flask request exception already is, no separate wiring needed.

Each shows up as its own `kind` (`"controller"`, `"job"`, `"query"`) on the same `performance`
dashboard dataset, so "slowest queries" and "slowest tasks" are just a filtered version of the
same widget builder "slowest transactions" already uses.

## Distributed tracing

For one slow or errored request, the Django/Flask integrations (via `ForgeOpsTrackerTracingMiddleware` and
`init_flask()` respectively, both installed above) capture its full nested call tree: the
controller/view span, plus every database query, outbound HTTP call, Redis command, and
manually-wrapped span nested under it, so ForgeOps can render a waterfall for that one request.

This is the whole point of the feature, so it's worth being explicit about: a request's own trace
is only ever built, let alone sent, once its own root span's duration crosses a threshold, decided
entirely client-side before a single byte goes over the wire. A normal, fast request costs nothing
extra. A request that errored (an unhandled exception, or any `capture_exception()` during it) is
the one exception: its trace is always sent, however fast it was, since the spans leading up to an
error are exactly what you want next to it.

```python
forge_ops_tracker.init(
    dsn="...",
    track_tracing=False,             # opt out entirely
    trace_capture_threshold_ms=500,  # milliseconds; default 1000
)
```

Database queries nest in automatically, the same way they do for performance monitoring above
(Django's ORM with no extra step; any engine passed to `track_sqlalchemy_queries`). Outbound HTTP
calls made via [requests](https://requests.readthedocs.io/) nest in too, with one extra call:

```python
from forge_ops_tracker.integrations.requests import init_requests

init_requests()
```

Redis commands made through [redis-py](https://redis.readthedocs.io/) nest in the same way, with
one call of their own:

```python
import redis
from forge_ops_tracker.integrations.redis import init_redis

init_redis()

cache = redis.Redis()
cache.get("cart:42")        # a "Redis GET" span

with cache.pipeline() as pipe:
    pipe.incr("visits")
    pipe.expire("visits", 3600)
    pipe.execute()          # one "Redis MULTI" span for the whole batch
```

Every command a `redis.Redis` client sends becomes its own span, with `kind` `"redis"` and the
name `"Redis <COMMAND>"` (the command alone, upcased). A pipeline is one span for the whole
round trip: `"Redis MULTI"` when it runs as a transaction (redis-py's default) and
`"Redis PIPELINE"` with `transaction=False`. Keys and values are never recorded, since a key can
carry a customer's own id. Each command also adds a `"redis"` breadcrumb (level `"error"` if it
raised). A pipeline that is WATCHing keys sends `WATCH` and any reads before `multi()` straight
away, and each of those is its own span too.

The same `init_redis()` call covers `redis.asyncio` and `RedisCluster` (both `redis.cluster` and
`redis.asyncio.cluster`), with the same span names:

```python
import redis.asyncio

cache = redis.asyncio.Redis()

async def load_cart(cart_id):
    with forge_ops_tracker.span("CartService.load"):
        return await cache.hgetall(f"cart:{cart_id}")  # a "Redis HGETALL" span under CartService.load
```

An awaited command is recorded in the trace of the task that awaited it, including tasks started
from inside the request with `asyncio.gather` or `asyncio.create_task`, and requests running at the
same time on one event loop never see each other's commands.

Not covered yet: `RedisCluster` pipelines (`cluster.pipeline()`), sync or async, aren't recorded.

Every span name (`"GET api.stripe.com"`, `"SELECT orders"`, `"Redis GET"`) is low-cardinality by
design, the same as every transaction name elsewhere in this SDK: never a raw URL path, query
string, SQL literal, or Redis key, since any of those can carry a customer's own id or a secret.

There's no way to auto-detect "this is a logically distinct service layer" the way a SQL query or
an outbound HTTP call already has a real hook to extend, so wrap your own service-layer code by
hand to have it show up as its own span:

```python
with forge_ops_tracker.span("PaymentService.charge"):
    charge_card(order)
```

Also works as a decorator (`@forge_ops_tracker.span("PaymentService.charge")`, on an `async def`
too, where the span covers the awaited body) and as `async with`. A span is open only in the task
or thread that opened it: tasks started inside it (`asyncio.gather`, `asyncio.create_task`) and
threads run with a copy of its context (`asyncio.to_thread`) nest under it, while two spans running
at the same time in sibling tasks each stay the parent of only their own queries, calls and
commands. `kind=` accepts
`"controller"`, `"service"` (the default), `"database"`, `"redis"`, `"http"`, `"job"`, or
`"other"`. A no-op outside of a request currently being traced (a plain script, a fast request
that's already finished) or with `track_tracing` off: it just runs the wrapped code and records nothing, never
raising.

Requires a ForgeOps plan that includes distributed tracing; on a plan that doesn't, a captured
trace is accepted but not recorded, and the response says why.

### SQL on database spans

Every database span the Django and SQLAlchemy integrations record carries the query's SQL as
`db.statement`, with every string and number replaced by `?`, plus `db.system` (`postgresql`,
`mysql`, `sqlite`, and so on, from the connection). Bind values are never sent. So a slow request's
waterfall shows which query was slow, not only which table:

```
SELECT * FROM orders WHERE customer_id = ? AND status = ? ORDER BY created_at DESC
```

A query you time yourself (a driver this SDK doesn't instrument) can carry its SQL too. Pass
`statement=` (and optionally `db_system=`) to a `kind="database"` span; it's masked the same way:

```python
import sqlite3

import forge_ops_tracker

db = sqlite3.connect("app.db")
sql = "SELECT id, total FROM orders WHERE customer_id = ? AND status = 'paid'"

with forge_ops_tracker.span("Load orders", kind="database", statement=sql, db_system="sqlite"):
    rows = db.execute(sql, (42,)).fetchall()
# The span's data: {"db.statement": "SELECT id, total FROM orders WHERE customer_id = ? AND status = ?",
#                   "db.system": "sqlite"}
```

### Query plans for slow PostgreSQL queries (opt-in)

With `explain_slow_queries=True`, a slow read on PostgreSQL also gets its query plan, so ForgeOps
can show why it was slow (a sequential scan, a missing index) next to the query:

```python
forge_ops_tracker.init(
    dsn="...",
    explain_slow_queries=True,   # default False
    explain_threshold_ms=500,    # milliseconds; default 500
)
```

What it does:

- Only for a query that took at least `explain_threshold_ms`, on a PostgreSQL connection (Django's
  `postgresql` backend, or a SQLAlchemy engine whose dialect is `postgresql`).
- Only for a single plain `SELECT`: never a write, a statement with a second statement after it,
  a `SELECT ... FOR UPDATE`/`FOR SHARE`, or a `WITH` query that modifies data.
- Runs `EXPLAIN (FORMAT JSON)`, never `EXPLAIN ANALYZE`, so the query itself is not run again. It
  runs on a background thread, after your query has finished, on a separate connection (a new one
  for Django; one checked out of the engine's pool for SQLAlchemy), inside a `READ ONLY`
  transaction with a 2 second `statement_timeout`, which is always rolled back. Your own
  connection and transaction are never touched.
- At most once per distinct query every 10 minutes, and at most 10 per minute per process.
- Sends the masked statement and the plan with every string in it masked the same way (a plan's
  `Filter` repeats the query's values). Your bind values are used for the `EXPLAIN` and then
  dropped; they are never sent. A plan over 64 KB is dropped.

What it doesn't do: it doesn't run for MySQL, SQLite, or any other database, for `executemany`, or
for SQLAlchemy's async engines. If the `EXPLAIN` fails for any reason (a timeout, a permission
error, no free connection in the pool), it's logged at debug level and skipped; it never raises
into your app. The database user needs no extra privileges beyond being able to run the query.
Plans are stored only for projects with performance monitoring; otherwise ForgeOps quietly
declines them.

### Following a request across services

Traces use the [W3C Trace Context](https://www.w3.org/TR/trace-context/) standard, so a request can
be followed from one service into the next, whichever language or tracing library the other
service uses:

- **Incoming**: a request that arrives with a valid `traceparent` header continues that trace (same
  trace id), and its root span records the caller's span as its parent. A missing or malformed
  header just starts a new trace.
- **Outgoing**: every outbound call made through `requests` during a request (with
  `init_requests()` called) gets a `traceparent` header whose parent id is that call's own span, so
  the next service's spans nest under it. A `traceparent` you set yourself is never replaced, and
  this client's own deliveries to ForgeOps never get one.

A trace id exists for every request even with `track_tracing` off, and the header is still sent,
since the trace id is also what links an error here to an error in the service you called. Seeing
the two errors connected in ForgeOps needs both services' projects linked there.

```python
import re

import forge_ops_tracker
import requests
from flask import Flask
from forge_ops_tracker.integrations.flask import init_flask
from forge_ops_tracker.integrations.requests import init_requests

forge_ops_tracker.init(
    dsn="https://<api_key>@getforgeops.net/api/v1/events",
    propagate_traces=True,  # default True; False never sends traceparent
    # Default None: every host. A string matches that host and its subdomains on a dot boundary
    # ("internal.example" matches "orders.internal.example", not "notinternal.example"); a compiled
    # regular expression is searched against the host.
    trace_propagation_targets=["internal.example", re.compile(r"^10\.0\.")],
)
init_requests()

app = Flask(__name__)
init_flask(app)


@app.post("/checkout")
def checkout():
    # Carries traceparent: 00-<this request's trace id>-<this call's span id>-01
    requests.post("https://orders.internal.example/orders", json={"sku": "A1"}, timeout=5)
    return {"ok": True}
```

## Custom metrics and infrastructure monitoring

Two explicit calls (nothing is automatic, so there is no `track_*` flag): a business event you name
yourself, and a reading from one of your own hosts.

```python
forge_ops_tracker.capture_metric("signup")           # value defaults to 1.0: a bare counter
forge_ops_tracker.capture_metric("payment", 49.0)    # a real magnitude; it may be negative (a refund)

forge_ops_tracker.capture_infrastructure_metric("cpu", 0.42)                      # hostname defaults to server_name
forge_ops_tracker.capture_infrastructure_metric("disk", 0.81, hostname="db-1")
forge_ops_tracker.flush_metrics()                    # optional: send right now
```

Each capture is buffered and flushed as one batch every `metric_flush_interval` /
`infrastructure_metric_flush_interval` seconds (60 by default) on a background thread, and once more
when the process exits normally, which is what a short-lived cron script relies on; call
`flush_metrics()` yourself if it might exit another way (`os._exit`, a kill). Every entry is stored
as it was captured (a signup is a row, not a running total), so a count or sum you compute later is
exact. Both are a no-op when the client isn't enabled for the environment.

A delivery that fails for now (a network error, a timeout, a 5xx, a 408 or a 429) keeps every entry
for the next flush, and an entry captured while a delivery is in flight is kept too. A batch the
server rejects (any other 4xx, such as the 403 a plan without the feature answers) is dropped rather
than sent again, and so is one that has failed 5 flushes in a row. The buffer holds at most 1000
entries per kind and drops further ones until a flush lets go of them. A NaN or infinite value is
dropped at capture: it is not valid JSON and would make the server reject the whole batch behind it.
Requires a ForgeOps plan that includes custom metrics / infrastructure monitoring.

## What changed

ForgeOps can show what changed in your system next to the errors and slowdowns that followed it.
Two ways in:

**Record a change yourself** when something changes that no deploy captures, like a feature flag
flipped, a config value edited, or a migration run by hand:

```python
import forge_ops_tracker

forge_ops_tracker.record_change(
    "feature_flag",  # feature_flag, config, migration, dependency, infrastructure, or other
    "Enabled new_checkout for 10% of users",
    details={"flag": "new_checkout", "rollout_percent": 10},
    actor="ops@example.com",
    url="https://flags.example.com/new_checkout",
)
```

`kind` and `title` are required; `details`, `environment` (defaults to the configured one),
`service`, `actor`, `url`, `id` (an idempotency key, so sending the same change twice records it
once), and `occurred_at` (a `datetime` or ISO 8601 string, defaulting to now) are optional. An
unknown `kind` is sent as `other`. It's queued and sent from a background thread, so it never slows
down the caller, never raises, and is a no-op when the client isn't enabled for the environment.

**Changes between deploys are detected for you.** Once per process, `init()` starts a background
thread that sends ForgeOps a snapshot of what the process is running: the Python version and every
installed package version (from `importlib.metadata`). ForgeOps compares it with the previous
boot's and records whatever changed, such as a package upgrade. Startup never waits on it.

```python
forge_ops_tracker.init(
    dsn="https://<api_key>@getforgeops.net/api/v1/events",
    detect_changes=True,        # default; False sends no startup snapshot
    track_env_var_names=False,  # default; True also sends environment variable names
)
```

With `track_env_var_names` on, the snapshot lists the names of your environment variables (never
their values), so an added or removed variable shows up as a change. Names that differ from host to
host, like `HOSTNAME`, `PATH`, `PORT`, `LC_*`, and Kubernetes service variables, are left out, as
are the SDK's own `FORGE_OPS_*` settings.

Requires a ForgeOps plan that includes change tracking; on a plan that doesn't, both are accepted but not recorded,
and the response says why.

## Running the tests

```bash
cd sdks/python
python3 -m venv .venv
./.venv/bin/pip install -e ".[test]"
./.venv/bin/python -m pytest
./.venv/bin/ruff check src tests
```
