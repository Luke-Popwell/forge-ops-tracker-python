"""Test-only stand-in for `django.contrib.auth.middleware.AuthenticationMiddleware`: sets
`request.user` from a test header, so `test_integrations_django.py` can control what
`ForgeOpsTrackerUserContextMiddleware` sees without needing `django.contrib.auth` installed (and
the migrations that would need to run for it). Duck-typed the same shape a real Django user model
has (`pk`, `email`, `get_username()`, `is_authenticated`), not django.contrib.auth.models.User
itself: this middleware's own job is to prove the *duck-typing contract*, which is exactly what a
custom (non-contrib.auth) user model in a real app would also need to satisfy.
"""


class FakeUser:
    def __init__(self, pk, email, username):
        self.pk = pk
        self.email = email
        self._username = username
        self.is_authenticated = True

    def get_username(self):
        return self._username


class AnonymousUser:
    is_authenticated = False


class FakeAuthMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        user_id = request.META.get("HTTP_X_TEST_USER_ID")
        if user_id:
            request.user = FakeUser(
                user_id,
                request.META.get("HTTP_X_TEST_USER_EMAIL", ""),
                request.META.get("HTTP_X_TEST_USER_USERNAME", ""),
            )
        else:
            request.user = AnonymousUser()
        return self.get_response(request)
