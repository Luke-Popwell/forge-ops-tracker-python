from django.db import connection
from django.http import HttpResponse
from django.urls import path

import forge_ops_tracker


def throw(request):
    raise ValueError("boom")


def ok(request):
    return HttpResponse("ok")


def user_detail(request, user_id):
    return HttpResponse(f"user {user_id}")


def run_a_query(request):
    # A raw SELECT 1, not a real model query: no migrated table is needed for this, just a real
    # round trip through connection.execute_wrapper (see
    # ForgeOpsTrackerPerformanceTrackingMiddleware's own comment on why that's scoped per-request
    # rather than installed once globally). The table-name extraction itself (_query_transaction_name)
    # is covered directly, against realistic Django-shaped SQL, in test_query_naming.py.
    with connection.cursor() as cursor:
        cursor.execute("SELECT 1")
    return HttpResponse("ok")


def throw_with_breadcrumb(request):
    # Exercises the manual add_breadcrumb() API from inside a real view, alongside the automatic
    # query breadcrumb below, both of which should show up on the reported error.
    with connection.cursor() as cursor:
        cursor.execute("SELECT 1")
    forge_ops_tracker.add_breadcrumb("about to explode", category="custom")
    raise ValueError("boom")


def traced_request(request):
    # Exercises the tracing middleware end to end: a query span plus a manually-wrapped service
    # span, both expected to nest under this request's own root span in the delivered trace.
    with forge_ops_tracker.span("PaymentService.charge", kind="service"), connection.cursor() as cursor:
        cursor.execute("SELECT 1")
    return HttpResponse("ok")


def handled_order_error(request, order_id):
    # An error the view catches and reports itself, partway through the request: it should already
    # carry the matched route and trace id, not only errors that escape the view.
    try:
        raise ValueError(f"order {order_id} failed")
    except ValueError:
        forge_ops_tracker.capture_exception()
    return HttpResponse("handled")


def call_downstream(request):
    # An outbound call made during the request, through the requests integration (the test itself
    # stubs the transport underneath Session.send, so nothing actually goes over the network).
    import requests

    requests.get("https://orders.internal.example/api/orders/7")
    return HttpResponse("ok")


urlpatterns = [
    path("throw", throw),
    path("ok", ok),
    path("users/<int:user_id>/", user_detail),
    path("run-a-query", run_a_query),
    path("throw-with-breadcrumb", throw_with_breadcrumb),
    path("traced-request", traced_request),
    path("orders/<int:order_id>/handled", handled_order_error),
    path("call-downstream", call_downstream),
]
