# Changelog

## 0.20.1 (2026-10-22)

- Strict privacy now also turns off slow query plans (`explain_slow_queries`): no EXPLAIN is run and no
  plan is sent, since a plan carries the SQL. Before, a plan could still be sent, with its values masked.
- The README says what strict privacy does in its opening lines.
- The source is public at https://github.com/Luke-Popwell/forge-ops-tracker-python, and the package links to it.

## 0.20.0 (2026-10-20)

- Strict privacy: a raw request path now keeps only its first segment, and the rest is sent as
  `:path` (`/patients/jane-doe/12` becomes `/patients/:path`; a first segment with a digit or over
  40 characters becomes `:id`). Before, only segments holding a digit were replaced, so a path like
  `/patients/janedoe` went out whole. A route template (`/patients/:id/notes`, `{id}`, `<int:id>`,
  `[slug]`, `*path`) is kept as it is. This applies everywhere strict privacy names a request or a
  call by its path: span names and transaction names.

## 0.19.0 (2026-10-20)

- New `strict_privacy` option (off by default, or `FORGE_OPS_STRICT_PRIVACY=true`) for apps that
  can't let personal or health data leave their servers. With it on, an error event carries no
  message (a `message_fingerprint` computed in your process is sent instead, so errors still
  group), no breadcrumbs, no `endpoint`, no SQL statement and no source context, and the user only
  by `id`. Outbound HTTP spans are named by method and host only, a request that matched no route
  is named by its path with ids taken out, and span data keeps only `db.statement` and
  `db.system`. The same unmatched-route name has its ids taken out in error events and performance
  samples too.
- New `privacy_allowed_fields` option (or `FORGE_OPS_PRIVACY_ALLOWED_FIELDS=error_code,feature`):
  the context and tag keys strict privacy still sends, when their value is a string, number or
  boolean. Empty by default.

## 0.18.0 (2026-10-13)

- Fixed: a batch the server refused was re-sent at every flush for as long as the process lived,
  with everything recorded since added to it. Performance samples, custom and infrastructure
  metrics and session counts are now dropped when the server answers with a 4xx other than 408 or
  429 (a 400, 401, 403, 413 or 422, for example), since sending the same batch again can only fail
  the same way. Once a metric buffer had filled up behind such a batch, every new metric was
  dropped too; that no longer happens.
- A batch that could not be delivered for now (a network error, a timeout, a 5xx, a 408 or a 429)
  is still kept for the next flush, but only for 5 flushes in a row. After the fifth it is dropped
  and the buffer starts clean, so a long outage no longer holds the same data in memory forever.
- Each drop is logged once at debug on the `forge_ops_tracker` logger with what was dropped and
  why, and a 4xx or 5xx answer is logged with its status code.
- Internal: `Client.deliver` and its sibling `deliver_*` methods now return a delivery status
  (`delivered`, `rejected` or `failed`, see `forge_ops_tracker.delivery_status`) instead of
  `True`/`False`. Only `delivered` is truthy, so `if client.deliver(payload):` means what it did
  before. Nothing in the documented API changed.

## 0.17.0 (2026-10-02)

- Changed: the environment now defaults to `production` when `FORGE_OPS_ENVIRONMENT` isn't set
  (it was `development`). Only `production` and `staging` send by default, so before this a host
  that set just a DSN, as the setup instructions show, sent nothing at all. Now a DSN alone is
  enough. To keep the old behavior on a development machine or in CI, set
  `FORGE_OPS_ENVIRONMENT=development` (or pass `environment="development"` to `init()`).
- `init()` now logs one warning per process (on the `forge_ops_tracker` logger, which prints to
  stderr unless you've configured logging) when a DSN is set but the environment isn't in
  `enabled_environments`, naming the environment and how to send from it. Nothing is logged when
  no DSN is set.

## 0.16.1 (2026-10-01)

- Fixed: an error reported just before the process exits is no longer lost. A short-lived script,
  cron job or management command that called `capture_exception()` and then finished, or that
  crashed with an uncaught exception, could end before the background thread sent the event, so it
  never reached ForgeOps. Anything still waiting to be sent now goes out when the interpreter exits
  normally, including the crash itself. The exit waits at most 5 seconds in total, so an
  unreachable ForgeOps never holds a process open for long, and a process that never reported
  anything doesn't wait at all.

## 0.16.0 (2026-09-29)

- `init_redis()` now also covers `redis.asyncio`. Every awaited command on a `redis.asyncio.Redis`
  client is a `Redis <COMMAND>` span and a `redis` breadcrumb, and an async pipeline is one
  `Redis MULTI` or `Redis PIPELINE` span, the same as the synchronous client. A command is
  recorded in the trace of the task that awaited it, so it nests under the `span()` block around
  it, and requests running at the same time on one event loop each keep their own commands.
- `RedisCluster` commands, from `redis.cluster` and `redis.asyncio.cluster`, are now spans too, one
  per command, named the same way. Cluster pipelines aren't instrumented yet.
- Commands a pipeline sends while it is WATCHing keys (the `watch()` call itself and any reads
  before `multi()`) are now recorded as their own spans. A WATCHing pipeline's `execute()` with
  nothing queued is now recorded as `Redis MULTI` too, since it still makes a round trip.
- Each of these is skipped on a redis-py release that doesn't have it. Keys and arguments are
  still never recorded.
- Fixed: `span()` blocks running at the same time in sibling asyncio tasks of one request (each
  branch of an `asyncio.gather`, say) could be recorded under each other, along with the queries,
  HTTP calls and Redis commands inside them. The open span is now tracked per task (and per thread
  run with a copy of the context, like `asyncio.to_thread`), so each span is the parent of only
  its own work, and a task started inside a span still nests under it.
- `span()` as a decorator on an `async def` now times the awaited body; before, it closed as soon
  as the coroutine was created. `span()` also works with `async with`.
- SQL masking now matches ForgeOps's own server-side rules exactly. Also masked: strings with a
  backslash-escaped quote (`'o\'brien'`, `E'o\'brien'`), a string's `E`/`X`/`N`/`B`/`U&` prefix
  along with it (`X'DEADBEEF'` is one `?`), a string right after a word (`LIKE'%x%'`), and hex,
  binary, exponent and leading-dot numbers (`0x1F`, `0b101`, `1.5E-3`, `.5`). Digits inside an
  identifier (`$1`, `orders2`) are still left alone. On MySQL and MariaDB, `"double quoted"`
  strings are masked too: the query spans from Django and SQLAlchemy and a `span()` given
  `db_system="mysql"` pass the database through.
- A string cut off right after a backslash (`'secret\` as the very end of a statement) is now
  masked too; before, it didn't match at all and stayed visible. Same change as the server.
- The query plan delivery test no longer fails when the clock reads 42 minutes or seconds past;
  it checks the masked statement and plan instead of the whole payload.

## 0.15.0 (2026-09-28)

- Redis commands now show up in traces. Call `init_redis()` from
  `forge_ops_tracker.integrations.redis` once at startup (install the new `redis` extra), and every
  command a `redis.Redis` client sends during a traced request becomes a `kind="redis"` span named
  `Redis <COMMAND>` (`Redis GET`, `Redis HSET`, ...). A pipeline is one span for the whole batch,
  `Redis MULTI` or `Redis PIPELINE`. Keys, values and other arguments are never recorded. Each
  command also adds a `redis` breadcrumb, at level `error` when the command raised. redis-py stays
  optional: nothing imports it unless you import this module.
- `forge_ops_tracker.span()` documents `"redis"` as a `kind` for a Redis call you time yourself.

## 0.14.0 (2026-09-25)

- Database spans now carry their SQL. Every span the Django and SQLAlchemy integrations record for
  a query has `db.statement` in its data, with every string and number replaced by `?` (capped at
  4000 characters), and `db.system` (`postgresql`, `mysql`, `sqlite`, ...) from the connection.
  Bind values are never sent.
- `forge_ops_tracker.span()` takes optional `statement=` and `db_system=` for a `kind="database"`
  span you time yourself; the statement is masked the same way.
- New opt-in `explain_slow_queries` (default `False`) and `explain_threshold_ms` (default `500`).
  When on, a single plain `SELECT` on PostgreSQL that took at least the threshold gets an
  `EXPLAIN (FORMAT JSON)` (never `EXPLAIN ANALYZE`), run on a background thread over a separate
  connection inside a `READ ONLY` transaction with a 2 second `statement_timeout`, then rolled
  back. The masked statement and the plan, with every string in it masked, are sent to ForgeOps.
  At most once per distinct statement every 10 minutes and 10 per minute per process. Any failure
  is logged at debug level and skipped.

## 0.13.0 (2026-09-25)

- New `forge_ops_tracker.record_change(kind, title, details=None, environment=None, service=None,
  actor=None, url=None, id=None, occurred_at=None)` records something that changed in your system
  (a feature flag, a config value, a hand-run migration) so ForgeOps can show it next to the errors
  that followed. `kind` is one of `feature_flag`, `config`, `migration`, `dependency`,
  `infrastructure`, or `other`; anything else is sent as `other`. Sent from a background thread,
  never raises, and a no-op when the client isn't enabled.
- Changes between deploys are now detected automatically. Once per process, `init()` starts a
  background thread that sends a snapshot of the Python version and installed package versions
  (from `importlib.metadata`), and ForgeOps records whatever changed since the previous boot. New
  `detect_changes` setting (default `True`) turns this off.
- New `track_env_var_names` setting (default `False`) adds environment variable names, never
  values, to that snapshot. Host-specific names (`HOSTNAME`, `PATH`, `PORT`, `LC_*`, Kubernetes
  service variables, and others) and the SDK's own `FORGE_OPS_*` settings are always left out.
- `ruff check` passes again on current ruff: the `# noqa` comments that carried an explanation
  after a colon now use a format ruff accepts.

## 0.12.0

- Every error reported during a Django or Flask request now says where it happened:
  `transaction_name` (the same `GET /orders/<int:order_id>` name performance samples and traces
  already use), `endpoint` (the method plus the route as declared in your URL patterns, never the
  literal path, so no ids or tokens), and `trace_id` (the request's W3C trace id). All three are
  set as soon as the framework has matched the route, so an error you report yourself from inside
  the view with `capture_exception()` already has them. Left out entirely outside a request (a
  Celery task, a script). Structured fields, so they're never PII-scrubbed.
- Distributed tracing across services, using the W3C Trace Context standard. A request that
  arrives with a valid `traceparent` header continues that trace instead of starting its own, and
  its root span points at the caller's span. Every outbound `requests` call made during a request
  (with `init_requests()`) now sends a `traceparent` header whose parent id is that call's own
  span, so the next service (any service that understands the standard, not only ones reporting to
  ForgeOps) can continue the trace. A `traceparent` you set yourself is never overwritten, and the
  client's own deliveries to ForgeOps never get one. New `propagate_traces` (default True) and
  `trace_propagation_targets` (default None, meaning every host; or a list of host strings, each
  matching that host and its subdomains, and/or compiled regular expressions) control where the
  header goes. A trace id is created for every request even with `track_tracing` off, since it is
  also what links an error to errors in other services, once both projects are linked in ForgeOps.
- Trace and span ids are now guaranteed valid W3C ids (32 and 16 lowercase hex characters, never
  all zeros).
- An errored request's trace is always sent, however fast it was: an unhandled exception, or any
  `capture_exception()` while the request was running. Previously only requests slower than
  `trace_capture_threshold_ms` were sent. Fast, successful requests are unchanged.

## 0.11.0

- Database errors now say where to look. When an error comes from a database call (SQLAlchemy's
  `StatementError`/`DBAPIError`, or an exception of yours raised from one), the event carries the
  names of the stored procedure or function and the tables or views its SQL touched, so ForgeOps can
  show them on the issue. On by default (`capture_sql_objects`); names are identifiers, never
  values. New opt-in `capture_sql_statement` (default False) also sends the statement itself, with
  every string and number replaced by `?`. Each project has its own server-side setting that can
  stop the statement being stored regardless of this flag; the names are still kept.

## 0.10.1

- Documentation only: the README and package description now describe ForgeOps as a hosted service, link to getforgeops.net, and show the real host in the connection string example instead of a placeholder. No code changes.

## 0.10.0

- Performance percentiles: every performance sample now carries a small latency histogram alongside
  its count/sum/max (fixed buckets of 50, 100, 250, 500, 1000, 2500, 5000 and 10000ms, plus an
  overflow bucket), so ForgeOps can show an approximate p50/p95/p99 per transaction instead of
  only an average. No new config; this rides the existing performance tracking flag and flush
  interval. Also fixes a flush race: a request recorded while a flush was still delivering could be discarded
  when that delivery succeeded; now only what was actually delivered is removed.

## 0.9.0

- Custom metrics and infrastructure monitoring: `capture_metric(name, value=1.0)` records a named
  business event (a signup, a payment), and `capture_infrastructure_metric(name, value, hostname=None)`
  records a CPU/memory/disk reading from one of your own hosts. Both are buffered, flushed as a batch
  every `metric_flush_interval`/`infrastructure_metric_flush_interval` seconds and at exit, or right
  away with `flush_metrics()`. Nothing is automatic, so there is no `track_*` flag.

## 0.8.0

- Distributed tracing: for one slow request, captures its full nested call tree (a controller/
  view span, plus every database query, outbound HTTP call, and manually-wrapped span nested
  under it) and delivers it as a single trace, ready for ForgeOps to render as a waterfall. Only
  ever built, let alone sent, once a request's own root span duration crosses
  `trace_capture_threshold_ms` (default 1000ms, client-side, before a single byte goes over the
  wire): a normal, fast request costs nothing extra. `ForgeOpsTrackerTracingMiddleware` (Django)
  and `init_flask()` capture the controller/view span and every database query automatically; the
  new `forge_ops_tracker.integrations.requests` module (`init_requests()`) does the same for
  outbound HTTP calls made via `requests`, this SDK's first integration with it. A logically
  distinct chunk of your own code (typically a service-layer call) has no automatic hook to nest
  under, so wrap it by hand with the new `forge_ops_tracker.span(name, kind="service")`, usable
  as a context manager or a decorator; a no-op with no trace currently being captured. On by
  default (`track_tracing`), same posture as every other automatic instrumentation this client
  does. No Redis integration yet; see the README's own note on that gap.

## 0.7.0

- Breadcrumbs: a trail of SQL queries, the request/controller lifecycle, and Celery task runs
  leading up to an error, recorded automatically (on by default, same as every other automatic
  instrumentation this client does; opt out with `track_breadcrumbs=False`, or tune
  `max_breadcrumbs`, default 30). `forge_ops_tracker.add_breadcrumb(message, category=, level=,
  data=)` adds your own, regardless of whether the automatic sources are on, and works outside a
  request entirely too (a background task, a console session). Unlike the affected user, a
  breadcrumb's message/data is scrubbed for likely PII. Scoped with `contextvars.ContextVar`
  rather than the thread-local `set_user()` already uses, so it stays correctly isolated under
  Celery's own thread-pool workers (not just its prefork processes) and any future asyncio/ASGI
  integration, not just a plain WSGI server.

## 0.6.0

- Automatic user detection: `ForgeOpsTrackerUserContextMiddleware` (Django, reads `request.user`
  when authenticated) and `init_flask()`'s own detection of a logged-in
  [Flask-Login](https://flask-login.readthedocs.io/) user (a genuinely optional dependency).
  Both call `set_user()` for you; a manual `set_user()` call still overrides either.

## 0.5.0

- `capture_exception()` gains a `user=` keyword, and a new `set_user(id=None, email=None,
  username=None)` attaches one for the rest of the current thread instead. Never scrubbed. Shows
  up on an issue's own detail page and as its own affected-users count.

## 0.4.0

- Performance monitoring now also covers database queries (Django automatically; any SQLAlchemy
  engine, Flask-SQLAlchemy included, via the new `forge_ops_tracker.integrations.sqlalchemy`
  module), bucketed by `"<VERB> <table>"`, never raw SQL text. New Celery integration
  (`forge_ops_tracker.integrations.celery`) covering both task timing and error reporting, this
  SDK's first Celery support at all. All on the same `track_performance` flag as before.

## 0.2.0

- Every reported event now carries `sdk_name` ("python"), so a project's language on the ForgeOps
  dashboard auto-detects from whichever SDK is actually reporting into it, rather than staying
  stuck on whatever it was created with. Never overrides a language picked by hand in project
  settings.

## 0.1.0

- Initial release: Django and Flask integrations report every unhandled request exception
  automatically. PII scrubbing (email addresses, formatted SSNs/credit cards, known API
  key/token formats, and any field whose name suggests a secret) runs before a payload ever
  leaves the host app's process. Delivery runs on a background thread with a bounded queue and
  short HTTP timeouts; every failure mode is caught and dropped rather than raised, so a broken
  or unreachable tracker can never take down the host app.
