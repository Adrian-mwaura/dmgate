import math

import pytest

from dmgate.stats import betainc, clopper_pearson, cluster_bootstrap, upper_bound


def binom_cdf(k: int, n: int, p: float) -> float:
    return sum(math.comb(n, i) * p**i * (1 - p) ** (n - i) for i in range(k + 1))


def test_betainc_symmetry_and_edges():
    assert betainc(3, 3, 0.5) == pytest.approx(0.5, abs=1e-12)
    assert betainc(2, 5, 0.0) == 0.0
    assert betainc(2, 5, 1.0) == 1.0
    # I_x(a, b) = 1 - I_{1-x}(b, a)
    assert betainc(2.5, 7.0, 0.3) == pytest.approx(1 - betainc(7.0, 2.5, 0.7), abs=1e-12)


@pytest.mark.parametrize("k,n", [(1, 10), (5, 10), (3, 40), (17, 60), (59, 60)])
def test_clopper_pearson_satisfies_defining_equations(k, n):
    lo, hi = clopper_pearson(k, n)
    # Upper bound: P(X <= k | hi) = alpha/2. Lower bound: P(X >= k | lo) = alpha/2.
    assert binom_cdf(k, n, hi) == pytest.approx(0.025, abs=1e-9)
    assert 1 - binom_cdf(k - 1, n, lo) == pytest.approx(0.025, abs=1e-9)
    assert lo < k / n < hi


def test_clopper_pearson_known_value():
    lo, hi = clopper_pearson(5, 10)
    assert lo == pytest.approx(0.1871, abs=1e-4)
    assert hi == pytest.approx(0.8129, abs=1e-4)


def test_zero_errors_closed_forms():
    n = 3000
    # Two-sided upper limit with k = 0 is 1 - (alpha/2)^(1/n).
    assert clopper_pearson(0, n) == (0.0, pytest.approx(1 - 0.025 ** (1 / n), rel=1e-9))
    # One-sided bound is the "rule of three": ~3/n.
    assert upper_bound(0, n) == pytest.approx(1 - 0.05 ** (1 / n), rel=1e-9)
    assert upper_bound(0, n) == pytest.approx(3 / n, rel=0.01)


def test_large_n_is_fast_and_sane():
    lo, hi = clopper_pearson(10, 100_000)
    assert lo < 1e-4 < hi < 2e-4


def test_invalid_inputs():
    with pytest.raises(ValueError):
        clopper_pearson(0, 0)
    with pytest.raises(ValueError):
        upper_bound(5, 4)


def test_cluster_bootstrap_is_deterministic_and_brackets_estimate():
    clusters = [(1, 10), (0, 10), (2, 10), (0, 10), (1, 10)] * 4
    a = cluster_bootstrap(clusters, n_boot=500, seed=7)
    b = cluster_bootstrap(clusters, n_boot=500, seed=7)
    assert a == b
    assert a[0] <= 4 / 50 <= a[1]


def test_cluster_bootstrap_empty():
    assert cluster_bootstrap([]) is None
    assert cluster_bootstrap([(0, 0)]) is None
