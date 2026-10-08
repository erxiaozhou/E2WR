import math




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


