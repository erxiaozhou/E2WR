import math
import numpy as np
from typing import Any
from .prob_util import compute_group_test_likelihood


def betaln(a, b):
    return math.lgamma(a) + math.lgamma(b) - math.lgamma(a + b)


def should_update_p0_bak3(test_history, current_p0: float, new_p0: float):
    if not test_history:
        return False

    log_L0 = compute_group_test_likelihood(current_p0, test_history)
    L0 = math.exp(log_L0) if log_L0 > -700 else 0

    log_L1 = compute_group_test_likelihood(new_p0, test_history)
    L1 = math.exp(log_L1) if log_L1 > -700 else 0
    if L1 <= 1e-300:
        return True

    if L0 <= 1e-300:
        return True

    BF01 = L0 / L1

    should_update = BF01 < 0.1

    # print(f"L0 = {L0:.2e} ;; L1 = {L1:.2e} ;; BF01 = {BF01:.2e}")
    # print(f"{'Update p0' if should_update else 'Keep p0'}")

    return should_update


def update_global_probability_v2(test_history, alpha, beta):

    assert len(test_history) > 0, 'test_history is empty'
    result = _map_estimate_grid_v2(test_history, alpha, beta)

    print(f"MAP estimate result: p0 = {result:.6f}")
    return result


def _log_posterior_v2(p, test_history, alpha, beta):
    if p <= 0 or p >= 1:
        return -np.inf

    # ln(Beta(p; α, β)) ∝ (α-1)ln(p) + (β-1)ln(1-p)
    log_prior = (alpha - 1) * math.log(p) + (beta - 1) * math.log(1 - p)

    log_likelihood = 0.0
    for S, R in test_history:
        k = len(S)
        if k == 0:
            continue

        if R == 0:  # P(R=0|p) = (1-p)^k
            if p > 1 - 1e-10:  # p≈1
                return -np.inf
            log_likelihood += k * math.log(1 - p)

        else:  # P(R=1|p) = 1-(1-p)^k
            if p < 1e-10:  # p≈0
                return -np.inf
            log_likelihood += math.log(1 - (1 - p) ** k)

    return log_prior + log_likelihood


def _map_estimate_grid_v2(test_history, alpha, beta, num_points=1000):
    p_grid = np.linspace(0.000001, 0.999, num_points)

    log_probs = []
    best_log_prob = -np.inf
    best_p = 0.5

    for p in p_grid:
        log_prob = _log_posterior_v2(p, test_history, alpha, beta)
        log_probs.append(log_prob)

        if log_prob > best_log_prob:
            best_log_prob = log_prob
            best_p = p
    return best_p
