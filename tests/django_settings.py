"""Minimal Django settings so pytest-django can boot Django for the
integration test: not a real app, just enough to exercise the
middleware.
"""

SECRET_KEY = "test"
DEBUG = False
ALLOWED_HOSTS = ["testserver"]
ROOT_URLCONF = "tests.django_urls"
MIDDLEWARE = [
    "tests.django_fake_auth.FakeAuthMiddleware",
    "forge_ops_tracker.integrations.django.ForgeOpsTrackerBreadcrumbContextMiddleware",
    "forge_ops_tracker.integrations.django.ForgeOpsTrackerTracingMiddleware",
    "forge_ops_tracker.integrations.django.ForgeOpsTrackerMiddleware",
    "forge_ops_tracker.integrations.django.ForgeOpsTrackerSessionTrackingMiddleware",
    "forge_ops_tracker.integrations.django.ForgeOpsTrackerPerformanceTrackingMiddleware",
    "forge_ops_tracker.integrations.django.ForgeOpsTrackerUserContextMiddleware",
]
DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.sqlite3",
        "NAME": ":memory:",
    }
}
USE_TZ = True
