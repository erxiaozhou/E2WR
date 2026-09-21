import math


def create_adaptive_grid(n: int):
    linear_part = []
    boundary_points = 50
    for i in range(boundary_points):
        x = 0.001 + (0.1 - 0.001) * i / (boundary_points - 1)
        linear_part.append(x)

    middle_points = n - 2 * boundary_points
    for i in range(middle_points):
        x = 0.1 + (0.9 - 0.1) * i / (middle_points - 1)
        linear_part.append(x)

    for i in range(boundary_points):
        x = 0.9 + (0.999 - 0.9) * i / (boundary_points - 1)
        linear_part.append(x)

    return sorted(linear_part)


def compute_group_test_likelihood(p, test_history):
    log_lik = 0.0

    for S, R in test_history:
        k = len(S)

        if R == 0:  # PASS
            # log[(1-p)^k] = k*log(1-p)
            if p >= 1:
                return -math.inf  # (1-p)^k = 0
            log_lik += k * math.log(1 - p)

        else:  # FAIL 
            # log[1 - (1-p)^k]
            if p <= 0:
                return -math.inf  # 1-(1-p)^k = 0

            #  log[1 - (1-p)^k]
            neg_prob = (1 - p) ** k  # (1-p)^k
            log_lik += math.log(1 - neg_prob)

    return log_lik


def compute_beta_log_prior(p, alpha, beta):
    
    if p <= 0 or p >= 1:
        return -math.inf

    # log B(α,β) = log Γ(α) + log Γ(β) - log Γ(α+β)
    log_beta = math.lgamma(alpha) + math.lgamma(beta) - \
        math.lgamma(alpha + beta)

    # log P(p|α,β) = (α-1)log(p) + (β-1)log(1-p) - log B(α,β)
    return (alpha - 1) * math.log(p) + (beta - 1) * math.log(1 - p) - log_beta
