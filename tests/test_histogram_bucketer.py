from forge_ops_tracker.histogram_bucketer import BOUNDARIES_MS, bucket_for


def test_returns_the_smallest_boundary_a_duration_fits_under_as_a_string():
    assert bucket_for(10) == "50"
    assert bucket_for(50) == "50"
    assert bucket_for(51) == "100"
    assert bucket_for(4999) == "5000"


def test_returns_inf_for_anything_larger_than_the_largest_boundary():
    assert bucket_for(10_001) == "inf"
    assert bucket_for(1_000_000) == "inf"


def test_puts_a_duration_exactly_on_a_boundary_into_that_boundarys_own_bucket():
    for boundary in BOUNDARIES_MS:
        assert bucket_for(boundary) == str(boundary)


def test_boundaries_match_the_servers_histogram_percentile():
    # app/services/histogram_percentile.rb and every other SDK must agree on this exact list.
    assert BOUNDARIES_MS == (50, 100, 250, 500, 1000, 2500, 5000, 10_000)
