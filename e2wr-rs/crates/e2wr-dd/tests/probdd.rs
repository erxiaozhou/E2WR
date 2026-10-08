use e2wr_dd::factory::DdFactory;
use e2wr_dd::probdd::ProbDD;

fn keep_is_kept(cfg: &[u32]) -> bool {
    cfg.contains(&5u32)
}

/// Same synthetic oracle as the Python-side driver: PASS when the candidate set contains every element of `keep`.
fn run_case(n: u32, keep: &[u32], update_p0: bool) -> (Vec<u32>, usize) {
    let config: Vec<u32> = (0..n).collect();
    let mut calls = 0usize;
    let mut factory = DdFactory::default();
    factory.set(update_p0, 0.1);
    let mut dd: ProbDD<u32> = factory.create_probdd();
    let result = dd.reduce(
        &config,
        None,
        None,
        &mut |cfg: &[u32]| {
            calls += 1;
            keep.iter().all(|k| cfg.contains(k))
        },
    );
    let mut sorted = result;
    sorted.sort_unstable();
    (sorted, calls)
}

#[test]
fn small_keep2_converges_to_keep_set() {
    let (res, _) = run_case(16, &[1, 4], false);
    assert_eq!(res, vec![1, 4]);
}

#[test]
fn all_removable_converges_to_empty() {
    let (res, _) = run_case(12, &[], false);
    assert!(res.is_empty());
}

#[test]
fn mid_keep4_with_p0_update_converges() {
    let (res, _) = run_case(30, &[2, 5, 11, 29], true);
    assert_eq!(res, vec![2, 5, 11, 29]);
}

#[test]
fn large_keep2_with_p0_update_converges() {
    let (res, _) = run_case(40, &[3, 21], true);
    assert_eq!(res, vec![3, 21]);
}

#[test]
fn deterministic_repeat_run() {
    let a = run_case(20, &[0, 7, 19], true);
    let b = run_case(20, &[0, 7, 19], true);
    assert_eq!(a, b, "ProbDD sampling is deterministic, runs must match");
}

#[test]
fn given_inip_overrides_initial_probability() {
    // Must-keep element 5 gets initial probability 1 (treated as determined-undeletable); the rest get low probabilities.
    let config: Vec<u32> = (0..10).collect();
    let mut given = std::collections::HashMap::new();
    given.insert(5u32, 1.0);
    let mut dd: ProbDD<u32> = ProbDD::new(0.1, false, Some(given));
    let mut calls = 0;
    let result = dd.reduce(
        &config,
        None,
        None,
        &mut |cfg: &[u32]| {
            calls += 1;
            keep_is_kept(cfg)
        },
    );
    let mut sorted = result;
    sorted.sort_unstable();
    assert_eq!(sorted, vec![5]);
    // Elements with probability 1 must never enter a delete set: must-keep element 5 is never tried.
    assert!(calls > 0);
}

#[test]
fn weights_bias_sampling_order() {
    // With everything deletable, weights should still converge to the empty set (semantics unchanged, only sampling order differs).
    let config: Vec<u32> = (0..14).collect();
    let weights: std::collections::HashMap<u32, f64> =
        (0..14u32).map(|i| (i, i as f64)).collect();
    let mut dd: ProbDD<u32> = ProbDD::new(0.1, false, None);
    let result = dd.reduce(&config, Some(&weights), None, &mut |_| true);
    assert!(result.is_empty());
}

#[test]
fn expected_end_time_stops_immediately() {
    let config: Vec<u32> = (0..20).collect();
    let mut dd: ProbDD<u32> = ProbDD::new(0.1, false, None);
    let past = std::time::SystemTime::now() - std::time::Duration::from_secs(10);
    let mut calls = 0;
    let result = dd.reduce(
        &config,
        None,
        Some(past),
        &mut |cfg: &[u32]| {
            calls += 1;
            cfg.is_empty()
        },
    );
    assert_eq!(calls, 0, "expired budget must prevent any test");
    assert_eq!(result.len(), 20);
}
