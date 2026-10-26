"""Confidence intervals for error rates (DESIGN.md §8.2). Stdlib only."""

from __future__ import annotations

import math
import random
from collections.abc import Sequence


def _betacf(a: float, b: float, x: float) -> float:
    """Continued fraction for the incomplete beta function (modified Lentz)."""
    tiny = 1e-300
    qab, qap, qam = a + b, a + 1.0, a - 1.0
    c = 1.0
    d = 1.0 - qab * x / qap
    d = 1.0 / (d if abs(d) > tiny else tiny)
    h = d
    for m in range(1, 100_000):
        m2 = 2 * m
        aa = m * (b - m) * x / ((qam + m2) * (a + m2))
        d = 1.0 + aa * d
        d = 1.0 / (d if abs(d) > tiny else tiny)
        c = 1.0 + aa / c
        c = c if abs(c) > tiny else tiny
        h *= d * c
        aa = -(a + m) * (qab + m) * x / ((a + m2) * (qap + m2))
        d = 1.0 + aa * d
        d = 1.0 / (d if abs(d) > tiny else tiny)
        c = 1.0 + aa / c
        c = c if abs(c) > tiny else tiny
        delta = d * c
        h *= delta
        if abs(delta - 1.0) < 1e-15:
            return h
    raise ArithmeticError(f"incomplete beta did not converge for a={a}, b={b}, x={x}")


def betainc(a: float, b: float, x: float) -> float:
    """Regularized incomplete beta I_x(a, b)."""
    if x <= 0.0:
        return 0.0
    if x >= 1.0:
        return 1.0
    log_front = math.lgamma(a + b) - math.lgamma(a) - math.lgamma(b) + a * math.log(x) + b * math.log1p(-x)
    front = math.exp(log_front)
    if x < (a + 1.0) / (a + b + 2.0):
        return front * _betacf(a, b, x) / a
    return 1.0 - front * _betacf(b, a, 1.0 - x) / b


def beta_ppf(q: float, a: float, b: float) -> float:
    """Quantile of Beta(a, b) by bisection; I_x is monotone in x."""
    lo, hi = 0.0, 1.0
    for _ in range(200):
        mid = (lo + hi) / 2.0
        if betainc(a, b, mid) < q:
            lo = mid
        else:
            hi = mid
    return (lo + hi) / 2.0


def clopper_pearson(k: int, n: int, confidence: float = 0.95) -> tuple[float, float]:
    """Exact two-sided interval for a binomial proportion k/n."""
    if n <= 0:
        raise ValueError("n must be positive")
    if not 0 <= k <= n:
        raise ValueError("k must be in [0, n]")
    alpha = 1.0 - confidence
    lower = 0.0 if k == 0 else beta_ppf(alpha / 2.0, k, n - k + 1)
    upper = 1.0 if k == n else beta_ppf(1.0 - alpha / 2.0, k + 1, n - k)
    return lower, upper


def upper_bound(k: int, n: int, confidence: float = 0.95) -> float:
    """Exact one-sided upper bound: the number behind a claim like "FPR < 0.1%"."""
    if n <= 0:
        raise ValueError("n must be positive")
    if not 0 <= k <= n:
        raise ValueError("k must be in [0, n]")
    return 1.0 if k == n else beta_ppf(confidence, k + 1, n - k)


def cluster_bootstrap(
    clusters: Sequence[tuple[int, int]],
    *,
    confidence: float = 0.95,
    n_boot: int = 2000,
    seed: int = 0,
) -> tuple[float, float] | None:
    """Percentile CI for sum(numerators) / sum(denominators), resampling whole clusters.

    Actions from one prompt or session are correlated, so the cluster is the resampling unit.
    Returns None when there is nothing to estimate.
    """
    clusters = [c for c in clusters if c[1] > 0]
    if not clusters:
        return None
    rng = random.Random(seed)
    m = len(clusters)
    estimates = []
    for _ in range(n_boot):
        num = den = 0
        for _ in range(m):
            c_num, c_den = clusters[rng.randrange(m)]
            num += c_num
            den += c_den
        estimates.append(num / den)
    estimates.sort()
    alpha = 1.0 - confidence
    lo_idx = int(math.floor(alpha / 2.0 * n_boot))
    hi_idx = min(n_boot - 1, int(math.ceil((1.0 - alpha / 2.0) * n_boot)) - 1)
    return estimates[lo_idx], estimates[hi_idx]
