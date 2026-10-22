"""Holds a single ForgeOps DSN plus everything else the client needs to
build and deliver events. Mirrors gems/forge_ops_tracker's Configuration:
a single DSN string carries both the ingestion URL and the
project's api_key: "https://<api_key>@host/api/v1/events".
"""

import logging
import os
import re
import socket
from urllib.parse import unquote, urlsplit, urlunsplit


class Configuration:
    def __init__(self):
        self.dsn = os.environ.get("FORGE_OPS_DSN")
        # FORGE_OPS_ENVIRONMENT when set, otherwise "production". Python has no environment
        # variable every framework agrees on (Django and Flask each leave it to the app), so this
        # doesn't guess from one. Defaulting to "production" means a host that only sets a DSN
        # actually sends; a development machine or CI run opts out by setting
        # FORGE_OPS_ENVIRONMENT=development (or test), and init() prints one warning saying so.
        self.environment = os.environ.get("FORGE_OPS_ENVIRONMENT") or "production"
        self.release = os.environ.get("FORGE_OPS_RELEASE")
        self.server_name = _safe_hostname()

        # Used to decide whether a backtrace frame is "in_app": a frame's
        # file path is compared against this root. Unlike the .NET SDK
        # (where a compiled assembly's file path never matches its
        # original source location), Python runs interpreted directly
        # from real .py files on disk, so file-path matching is the
        # correct approach here too, same as the Ruby gem's Rails.root
        # comparison. Defaults to the current working directory; set
        # explicitly if that doesn't match your app's actual layout (a
        # WSGI server started from a different directory, for instance).
        self.app_root = os.getcwd()

        self.enabled_environments = {"production", "staging"}
        self.queue_size = 1000
        self.timeout = 2.0  # seconds
        self.scrub_pii = True

        # Whether EventBuilder reads a few lines of source off disk around each
        # in-app frame's culprit line (see EventBuilder._attach_source_context).
        # Defaults to True so a snippet shows up with no extra setup, but this
        # flag by itself isn't what actually keeps proprietary source code from
        # ending up somewhere it shouldn't: ForgeOps' own per-project setting is
        # the durable, server-enforced off switch, since it applies no matter
        # what this flag happens to be set to on any given deployment, and can't
        # quietly drift back on the way a local config value could. Set this to
        # False too if this host app should never even attempt that disk read
        # in the first place.
        self.capture_source_context = True

        # When an error comes from a database call (SQLAlchemy's StatementError/DBAPIError, and
        # anything that wraps one), send the names of the stored procedure, table and view its
        # SQL touched, so an issue says where to start looking. Names are identifiers, never
        # values, which is why this defaults on. capture_sql_statement is the separate, opt-in
        # step of also sending the statement itself, with every string and number replaced by
        # "?"; off by default because even a masked statement describes your schema, and
        # ForgeOps' own per-project setting is what durably governs whether the server stores
        # it. See sql_statement.py.
        self.capture_sql_objects = True
        self.capture_sql_statement = False

        self.logger = logging.getLogger("forge_ops_tracker")

        # Reports anything that crashes the whole interpreter (a plain
        # script, a management command) with zero extra wiring, the same
        # way an unhandled Rails/ASP.NET Core request is covered
        # automatically elsewhere: see _install_excepthook in __init__.py.
        # Doesn't change program behavior (the original hook still runs
        # afterward), so on by default is safe; set False to opt out.
        self.install_excepthook = True

        # Whether the Django/Flask integrations count every request as a session (crash-free
        # unless an unhandled exception actually escaped it) and periodically report an
        # aggregate crash-free rate: see session_flusher.py. On by default, the same "on
        # unless you turn it off" posture error tracking itself already has.
        self.track_sessions = True
        # Seconds between aggregate session reports; requests are counted in-process and
        # flushed as one small report on this interval, not one network call per request.
        self.session_flush_interval = 60

        # Whether the Django/Flask integrations time every request and periodically report an
        # aggregate per transaction: see performance_flusher.py. On by default, same posture as
        # track_sessions above.
        self.track_performance = True
        # Seconds between aggregate performance reports; same "counted in-process, flushed as
        # one small batch on this interval" reasoning as session_flush_interval above.
        self.performance_flush_interval = 60

        # Whether the Django/Flask/SQLAlchemy/Celery integrations automatically record a
        # breadcrumb for every SQL query, request/controller lifecycle, and job run, independent
        # of whether track_performance is also on (each call site checks its own flag
        # separately): see breadcrumb_buffer.py for the trail this feeds and
        # forge_ops_tracker.add_breadcrumb for adding one by hand regardless of this flag. On by
        # default, same posture as every other automatic instrumentation flag above.
        self.track_breadcrumbs = True
        # Oldest entry dropped once this many have accumulated in a single trail: the same
        # "bounded ring buffer, not an unbounded log" reasoning gems/forge_ops_tracker's own
        # Configuration#max_breadcrumbs already documents, so a request or task that runs a very
        # large number of queries doesn't grow the trail (and the payload it rides in) without
        # bound.
        self.max_breadcrumbs = 30

        # Seconds between flushes of the buffered capture_metric / capture_infrastructure_metric
        # entries (see metric_buffer.py). No track_metrics flag the way track_performance has one:
        # these are explicit calls the host app's own code makes, not automatic instrumentation, so
        # there is nothing to turn off that simply not calling them doesn't already do.
        self.metric_flush_interval = 60
        self.infrastructure_metric_flush_interval = 60

        # Whether the Django/Flask integrations capture one slow request's own full nested call
        # tree (a controller/view span, plus every database/outbound-HTTP/manually-wrapped span
        # nested under it: see span_buffer.py) and deliver it as a single trace once it crosses
        # trace_capture_threshold_ms below. On by default, same posture as every other automatic
        # instrumentation flag above.
        self.track_tracing = True
        # The whole point of this feature: a normal, fast request must never cost a single byte
        # over the wire. This is the threshold, in milliseconds, a request's own root span
        # duration has to cross before its whole captured trace is even built, let alone sent.
        # Matches gems/forge_ops_tracker's own default.
        self.trace_capture_threshold_ms = 1000
        # Adds a W3C `traceparent` header (https://www.w3.org/TR/trace-context/) to every outbound
        # call integrations/requests.py instruments during a request, so a service that reports to
        # ForgeOps too (or anything else that understands the standard) continues this request's
        # trace instead of starting its own. On by default, the same as the Ruby gem's
        # propagate_traces: the header also carries the trace id that links an error here to an
        # error there, which is useful with or without spans. Never overwrites a traceparent the
        # request already has.
        self.propagate_traces = True
        # Which hosts get that header. None (the default) means every host. Otherwise a list whose
        # entries are either a host string, matching that host and its subdomains on a dot
        # boundary ("example.com" matches "api.example.com", not "badexample.com"; a leading dot is
        # ignored), or a compiled regular expression searched against the host. Narrow it for a
        # third-party API that rejects headers it doesn't know, or that shouldn't learn this app's
        # trace ids at all.
        self.trace_propagation_targets = None

        # Opt-in EXPLAIN for slow PostgreSQL reads (see query_plans.py). When on, a SELECT timed by
        # the Django or SQLAlchemy integration that took at least explain_threshold_ms gets an
        # EXPLAIN (FORMAT JSON), never EXPLAIN ANALYZE, run on a background thread over a separate
        # connection in a READ ONLY transaction with a 2 second statement_timeout. The masked
        # statement and masked plan are sent; bind values never leave the process. Rate limited to
        # one EXPLAIN per distinct statement per 10 minutes and 10 per minute overall. Off by
        # default because it does run extra (cheap, read-only) statements against your database.
        self.explain_slow_queries = False
        self.explain_threshold_ms = 500

        # Sends one startup snapshot per process (the Python version and installed package
        # versions; see changes.py) which ForgeOps diffs against the previous boot's to record what
        # changed between deploys. On by default, same posture as every other automatic behavior.
        self.detect_changes = True
        # Whether that snapshot also lists the names of this process's environment variables, so
        # an added or removed variable shows up as a change. Off by default: names only, never
        # values, but even names can say more about an app than some teams want to share. See
        # changes.ENV_VAR_DENYLIST for the host-specific names that are always left out.
        self.track_env_var_names = False

        # Off by default. On, nothing this client sends carries free text that could hold personal
        # or health data: no error message (a message_fingerprint is sent instead, so errors still
        # group), no breadcrumbs, no endpoint, no SQL statement, no source context, no user email or
        # username, and outbound HTTP spans named by method and host only. See strict_privacy.py
        # for every rule. FORGE_OPS_STRICT_PRIVACY=true turns it on from the environment; init()
        # or setting it here wins over that.
        self.strict_privacy = _env_flag(os.environ.get("FORGE_OPS_STRICT_PRIVACY"))
        # Context and tag keys strict privacy still sends (only plain str, int, float or bool
        # values under exactly these top-level keys), e.g. ["error_code", "feature"]. Empty by
        # default, so nothing from context or tags goes out.
        # FORGE_OPS_PRIVACY_ALLOWED_FIELDS=error_code,feature sets it from the environment.
        self.privacy_allowed_fields = os.environ.get("FORGE_OPS_PRIVACY_ALLOWED_FIELDS") or []

    @property
    def privacy_allowed_fields(self):
        return self._privacy_allowed_fields

    @privacy_allowed_fields.setter
    def privacy_allowed_fields(self, fields):
        """Takes a list of names or one comma-separated string; always kept as stripped strings."""
        if isinstance(fields, str):
            fields = fields.split(",")
        self._privacy_allowed_fields = [str(field).strip() for field in fields or () if str(field).strip()]

    def propagates_trace_to(self, host):
        """Whether an outbound request to host should carry a traceparent header; see
        propagate_traces/trace_propagation_targets above. Case-insensitive, since hostnames are."""
        if not self.propagate_traces:
            return False
        if self.trace_propagation_targets is None:
            return True

        host = (host or "").lower()
        for target in self.trace_propagation_targets:
            if isinstance(target, re.Pattern):
                if target.search(host):
                    return True
                continue
            target = str(target).lower().lstrip(".")
            if target and (host == target or host.endswith(f".{target}")):
                return True
        return False

    def is_own_request(self, url):
        """Whether url is one of this client's own endpoints (same scheme, host, and port as the
        DSN), so the outbound-HTTP integration never adds a traceparent to a delivery to ForgeOps.
        The Client itself uses urllib, which that integration never instruments, so this is a
        second guard, not the only one."""
        parsed = self._parsed_dsn()
        if parsed is None or not parsed.hostname:
            return False
        try:
            target = urlsplit(url)
            return (
                target.scheme.lower() == parsed.scheme.lower()
                and (target.hostname or "") == parsed.hostname
                and _effective_port(target) == _effective_port(parsed)
            )
        except ValueError:
            return False

    @property
    def api_key(self):
        parsed = self._parsed_dsn()
        if parsed is None or not parsed.username:
            return None
        return unquote(parsed.username)

    def ingestion_uri(self):
        """The ingestion URL with credentials stripped out (they travel as
        the Authorization header instead, not embedded in the request
        URI)."""
        parsed = self._parsed_dsn()
        if parsed is None:
            return None

        netloc = parsed.hostname or ""
        if parsed.port:
            netloc = f"{netloc}:{parsed.port}"
        return urlunsplit((parsed.scheme, netloc, parsed.path, parsed.query, ""))

    def session_checkins_uri(self):
        """Same derivation as ingestion_uri, with the trailing /events swapped for
        /session_checkins: one DSN, two endpoints, matching the Ruby gem's own
        Configuration#session_checkins_uri."""
        uri = self.ingestion_uri()
        if not uri:
            return None
        if uri.endswith("/events"):
            return uri[: -len("/events")] + "/session_checkins"
        return uri

    def performance_samples_uri(self):
        """Same derivation again, swapping the trailing /events for /performance_samples."""
        uri = self.ingestion_uri()
        if not uri:
            return None
        if uri.endswith("/events"):
            return uri[: -len("/events")] + "/performance_samples"
        return uri

    def custom_metrics_uri(self):
        """Same derivation again, swapping the trailing /events for /custom_metrics."""
        uri = self.ingestion_uri()
        if not uri:
            return None
        if uri.endswith("/events"):
            return uri[: -len("/events")] + "/custom_metrics"
        return uri

    def infrastructure_metrics_uri(self):
        """Same derivation again, swapping the trailing /events for /infrastructure_metrics."""
        uri = self.ingestion_uri()
        if not uri:
            return None
        if uri.endswith("/events"):
            return uri[: -len("/events")] + "/infrastructure_metrics"
        return uri

    def spans_uri(self):
        """Same derivation again, swapping the trailing /events for /spans: one whole captured
        trace (a trace_id plus every span belonging to it) delivered per POST, matching the Ruby
        gem's own Configuration#spans_uri."""
        uri = self.ingestion_uri()
        if not uri:
            return None
        if uri.endswith("/events"):
            return uri[: -len("/events")] + "/spans"
        return uri

    def changes_uri(self):
        """Same derivation again, for record_change() (POST /api/v1/changes)."""
        return self._sibling_uri("/changes")

    def change_snapshots_uri(self):
        """Same derivation again, for the startup snapshot (POST /api/v1/change_snapshots)."""
        return self._sibling_uri("/change_snapshots")

    def query_plans_uri(self):
        """Same derivation again, for EXPLAIN plans of slow queries (POST /api/v1/query_plans)."""
        return self._sibling_uri("/query_plans")

    def _sibling_uri(self, path):
        uri = self.ingestion_uri()
        if not uri:
            return None
        if uri.endswith("/events"):
            return uri[: -len("/events")] + path
        return uri

    def is_enabled(self):
        return self.is_configured() and self.environment in self.enabled_environments

    def is_configured(self):
        """Whether a usable DSN is set, regardless of environment."""
        return bool(self.dsn) and bool(self.api_key)

    def disabled_environment_warning(self):
        """The one-line warning init() logs when a DSN is set but this environment isn't one that
        sends, or None when there's nothing to warn about (no DSN, or the environment is enabled)."""
        if not self.is_configured() or self.environment in self.enabled_environments:
            return None
        enabled = ", ".join(sorted(str(e) for e in self.enabled_environments)) or "none"
        return (
            f'[ForgeOps] Not sending: this environment is "{self.environment}", and only {enabled} '
            f"are enabled. Set FORGE_OPS_ENVIRONMENT=production (or add \"{self.environment}\" to "
            "enabled_environments) to send from here."
        )

    def _parsed_dsn(self):
        if not self.dsn:
            return None
        # urlsplit never raises on malformed input (verified directly,
        # not assumed): a bad DSN just parses to an empty netloc, which
        # api_key/ingestion_uri already handle by returning None.
        result = urlsplit(self.dsn)
        return result if result.scheme else None


def _env_flag(value):
    """"true", "1", "yes" or "on" (any case) is on; anything else, or unset, is off."""
    return (value or "").strip().lower() in ("true", "1", "yes", "on")


def _effective_port(parsed):
    if parsed.port:
        return parsed.port
    return {"http": 80, "https": 443}.get(parsed.scheme.lower())


def _safe_hostname():
    try:
        return socket.gethostname()
    except Exception:  # noqa: BLE001 (hostname lookup must never be able to crash the host app)
        return None
