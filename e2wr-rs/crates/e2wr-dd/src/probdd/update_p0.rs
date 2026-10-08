//! Numeric part of online p0 inference (Python `ProbDDUtil/update_p0_util.py`, `prob_util.py`).
//!
//! The grid-search formula matches numpy `linspace(0.000001, 0.999, 1000)`;
//! exponentiation uses `powf` (CPython's C `pow`) so results match on the same machine.

/// Mirrors Python `prob_util.compute_group_test_likelihood`.
pub fn compute_group_test_likelihood<T>(p: f64, history: &[(Vec<T>, i32)]) -> f64 {
    let mut log_lik = 0.0;
    for (s, r) in history {
        let k = s.len();
        if *r == 0 {
            // P(R=0|p) = (1-p)^k
            if p >= 1.0 {
                return f64::NEG_INFINITY;
            }
            log_lik += k as f64 * (1.0 - p).ln();
        } else {
            // P(R=1|p) = 1-(1-p)^k
            if p <= 0.0 {
                return f64::NEG_INFINITY;
            }
            let neg_prob = (1.0 - p).powf(k as f64);
            log_lik += (1.0 - neg_prob).ln();
        }
    }
    log_lik
}

fn exp_if_log_finite(log_x: f64) -> f64 {
    if log_x > -700.0 {
        log_x.exp()
    } else {
        0.0
    }
}

/// Mirrors Python `should_update_p0_bak3`: p0 may update only when Bayes factor L0/L1 < 0.1.
pub fn should_update_p0_bak3<T>(
    history: &[(Vec<T>, i32)],
    current_p0: f64,
    new_p0: f64,
) -> bool {
    if history.is_empty() {
        return false;
    }
    let log_l0 = compute_group_test_likelihood(current_p0, history);
    let l0 = exp_if_log_finite(log_l0);
    let log_l1 = compute_group_test_likelihood(new_p0, history);
    let l1 = exp_if_log_finite(log_l1);
    if l1 <= 1e-300 {
        return true;
    }
    if l0 <= 1e-300 {
        return true;
    }
    let bf01 = l0 / l1;
    bf01 < 0.1
}

/// Mirrors Python `update_global_probability_v2`: grid-search MAP estimate under a Beta prior.
pub fn update_global_probability_v2<T>(
    history: &[(Vec<T>, i32)],
    alpha: f64,
    beta: f64,
) -> f64 {
    assert!(!history.is_empty(), "test_history is empty");
    map_estimate_grid_v2(history, alpha, beta)
}

/// Mirrors Python `_log_posterior_v2`.
fn log_posterior_v2<T>(p: f64, history: &[(Vec<T>, i32)], alpha: f64, beta: f64) -> f64 {
    if p <= 0.0 || p >= 1.0 {
        return f64::NEG_INFINITY;
    }
    let log_prior = (alpha - 1.0) * p.ln() + (beta - 1.0) * (1.0 - p).ln();
    let mut log_likelihood = 0.0;
    for (s, r) in history {
        let k = s.len();
        if k == 0 {
            continue;
        }
        if *r == 0 {
            if p > 1.0 - 1e-10 {
                return f64::NEG_INFINITY;
            }
            log_likelihood += k as f64 * (1.0 - p).ln();
        } else {
            if p < 1e-10 {
                return f64::NEG_INFINITY;
            }
            log_likelihood += (1.0 - (1.0 - p).powf(k as f64)).ln();
        }
    }
    log_prior + log_likelihood
}

/// Mirrors Python `_map_estimate_grid_v2` (1000 grid points, take the max posterior).
fn map_estimate_grid_v2<T>(history: &[(Vec<T>, i32)], alpha: f64, beta: f64) -> f64 {
    let num_points = 1000u32;
    let start = 0.000001f64;
    let stop = 0.999f64;
    let step = (stop - start) / (num_points - 1) as f64;
    let mut best_log_prob = f64::NEG_INFINITY;
    let mut best_p = 0.5;
    for i in 0..num_points {
        let p = start + i as f64 * step;
        let log_prob = log_posterior_v2(p, history, alpha, beta);
        if log_prob > best_log_prob {
            best_log_prob = log_prob;
            best_p = p;
        }
    }
    best_p
}
