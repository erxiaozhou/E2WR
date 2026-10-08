//! ProbDD probabilistic delta deletion (M1.2/M1.3).
//!
//! Mirrors Python `ProbDDUtil/abstract_probdd.py` + `prob_dd.py`. The algorithm skeleton
//! originates from the open-source project Picire (BSD-3).
//!
//! Known items deliberately not ported from Python:
//! - `OneCallLogInfo` and `cal_actual_ig` statistics (logging only, no effect on the algorithm);
//! - `ConfigCache` (a dedup tree that is only written, never read; real dedup goes through `memory`);
//! - the FAIL branch of `ProbDD._process` (the main loop calls `_process` only on PASS; dead code).

pub mod inner_info;
pub mod infer_p0;
pub mod update_p0;

use std::collections::HashMap;
use std::hash::Hash;
use std::time::SystemTime;

use indexmap::IndexMap;

use inner_info::{BetaEmulator, Emulator, InnerInfo};
use infer_p0::InferP0History;
use update_p0::{should_update_p0_bak3, update_global_probability_v2};

/// Outcome of the test function. `Pass`/`Fail` map to Python `ProbDD.PASS/FAIL`.
/// `Stop` maps to the M8/M9 callers raising `_DDEarlyStop` inside the test function:
/// delta debugging aborts immediately and the current config is the final result (Python does this via exception propagation).
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum TestOutcome {
    Pass,
    Fail,
    Stop,
}

/// Return shape of `reduce_ext`: normal convergence yields the minimal keep set; when the test function emits `Stop`
/// it aborts and returns `Stopped` (the caller takes the final result from its own state, matching the Python
/// caller catching `_DDEarlyStop` and returning `e.replaced` directly).
#[derive(Debug, Clone)]
pub enum ReduceOutcome<T> {
    Config(Vec<T>),
    Stopped,
}

/// Convergence threshold (Python `AbstractProbDD.threshold`).
pub const THRESHOLD: f64 = 0.8;

/// Upper bound on p0 updates (Python `max_update_p0_times`; effectively unreachable).
const MAX_UPDATE_P0_TIMES: usize = 20_000_000_000;

/// ProbDD deleter. `T` identifies deletable items (Python uses arbitrary hashable objects,
/// in practice definition descriptors/indices).
///
/// Sampling is fully deterministic (no RNG): same test function and input always give the same result.
pub struct ProbDD<T> {
    /// Initial "undeletable probability", clamped to [0.001, 0.799] at construction.
    pub initial_p: f64,
    /// Whether to infer p0 online (Python `update_p0`; enabled by default in real runs).
    pub update_p0: bool,
    /// Per-element initial probability overrides (Python `given_inip`).
    pub given_inip: Option<HashMap<T, f64>>,
    threshold: f64,
    /// element → undeletable probability. Keeps insertion order (Python `OrderedDict`);
    /// sampling sorts stably, so order affects the internal ordering of candidate sets.
    p: IndexMap<T, f64>,
    /// Dedup table config (keep-set list) → test result (true = PASS) (Python `memory`).
    memory: HashMap<Vec<T>, bool>,
    /// History of failed deletion candidates (Python `testHistory`).
    test_history: Vec<Vec<T>>,
    /// Current keep set (Python `passconfig`).
    passconfig: Vec<T>,
    p0: f64,
    beta_emulator: BetaEmulator,
}

impl<T: Clone + Eq + Hash + Ord> ProbDD<T> {
    pub fn new(initial_p: f64, update_p0: bool, given_inip: Option<HashMap<T, f64>>) -> Self {
        let threshold = THRESHOLD;
        let initial_p = (threshold - 0.001).min(initial_p).max(0.001);
        Self {
            initial_p,
            update_p0,
            given_inip,
            threshold,
            p: IndexMap::new(),
            memory: HashMap::new(),
            test_history: Vec::new(),
            passconfig: Vec::new(),
            p0: initial_p,
            beta_emulator: BetaEmulator::new(),
        }
    }

    /// Delta-deletes from the initial config, returning the minimal keep set (Python `AbstractProbDD.__call__`).
    ///
    /// - `test`: test function; receives a candidate keep set, returns true when the config is interesting (PASS).
    /// - `weights`: secondary key for sampling order (on equal probability, larger weight wins).
    /// - `expected_end_time`: time budget; stop after it expires (returning the current keep set).
    pub fn reduce<F>(
        &mut self,
        config: &[T],
        weights: Option<&HashMap<T, f64>>,
        expected_end_time: Option<SystemTime>,
        test: &mut F,
    ) -> Vec<T>
    where
        F: FnMut(&[T]) -> bool,
    {
        match self.reduce_ext(config, weights, expected_end_time, &mut |cfg| {
            if test(cfg) {
                TestOutcome::Pass
            } else {
                TestOutcome::Fail
            }
        }) {
            ReduceOutcome::Config(c) => c,
            // A boolean test function can never emit Stop.
            ReduceOutcome::Stopped => unreachable!("bool test cannot raise early stop"),
        }
    }

    /// Early-stop extension of `reduce`: the test function may return `TestOutcome::Stop` to abort immediately
    /// (Python `_DDEarlyStop` exception semantics: the memory-table write is skipped on abort).
    pub fn reduce_ext<F>(
        &mut self,
        config: &[T],
        weights: Option<&HashMap<T, f64>>,
        expected_end_time: Option<SystemTime>,
        test: &mut F,
    ) -> ReduceOutcome<T>
    where
        F: FnMut(&[T]) -> TestOutcome,
    {
        for c in config {
            let init = match &self.given_inip {
                Some(g) if g.contains_key(c) => g[c],
                _ => self.initial_p,
            };
            self.p.insert(c.clone(), init);
        }
        let mut update_p0_times = 0usize;
        // Actual Python behavior: large_enough is unconditionally True (the len(config)>=10 check is overridden).
        let large_enough = true;
        let mut inner_info = InnerInfo::new(&self.p);
        let mut infer_p0_history = InferP0History::new();
        // Same-named Python local: the assignment in the p0-update branch is never read in Python either
        // (only the HAS SAMPLE ALL branch reads it after rebuilding); kept for structural parity.
        #[allow(unused_assignments)]
        let mut emulator;
        let mut new_p0 = self.p0;
        self.passconfig = config.to_vec();
        while !self.test_done() {
            if let Some(t) = expected_end_time {
                if SystemTime::now() >= t {
                    break;
                }
            }
            let deleteconfig = self.sample(weights);
            if self.update_p0
                && large_enough
                && deleteconfig.len() + inner_info.cur_epoch_visited_elem_num
                    >= inner_info.epoch_start_elem_num
            {
                // HAS SAMPLE ALL: all undetermined elements covered within one round; reset round stats and the prior.
                emulator = Emulator::from_direct_num(
                    inner_info.rest_num(),
                    inner_info.epoch_start_elem_num as f64 * new_p0
                        - inner_info.last_epoch_detected_cannot_remove as f64,
                );
                infer_p0_history.reset_new_update_p0_history();
                self.beta_emulator
                    .reset_with_num(emulator.exp_cannot_remove, inner_info.rest_num() as f64);
                inner_info.reset_last_epoch_info(&self.p);
            }
            let config2test = minus(&self.passconfig, &deleteconfig);
            let outcome = match self.memory.get(&config2test) {
                Some(o) => *o,
                None => {
                    match test(&config2test) {
                        TestOutcome::Stop => {
                            // Python: the _DDEarlyStop exception propagates straight through __call__,
                            // skipping probability updates and the memory-table write.
                            return ReduceOutcome::Stopped;
                        }
                        TestOutcome::Pass => true,
                        TestOutcome::Fail => false,
                    }
                }
            };
            if !outcome {
                // FAIL: deletion failed; raise the probabilities of elements in the delete set.
                self.update_probabilities_on_fail(&deleteconfig, &config2test);
                self.test_history.push(deleteconfig.clone());
                if deleteconfig.len() == 1 {
                    self.p.insert(deleteconfig[0].clone(), 1.0);
                }
            } else {
                // PASS: deletion succeeded; shrink the keep set and zero out probabilities of dropped elements.
                for key in self.p.keys().cloned().collect::<Vec<_>>() {
                    if !config2test.contains(&key) {
                        self.p[&key] = 0.0;
                    }
                }
                let deleted = minus(&self.passconfig, &config2test);
                self.process_pass(&deleted);
                self.passconfig = config2test.clone();
            }
            debug_assert!(self.p0 < self.threshold);
            if self.update_p0
                && large_enough
                && self.get_all_non_determined_elem_num() > 10
                && !self.test_done()
            {
                inner_info.update_one_step_info(&self.p, &deleteconfig);
                let test_result: i32 = if outcome { 0 } else { 1 };
                infer_p0_history.add_one_log(&deleteconfig, test_result);
                new_p0 = update_global_probability_v2(
                    infer_p0_history.new_update_p0_history(),
                    self.beta_emulator.alpha,
                    self.beta_emulator.beta,
                );
                new_p0 = (self.threshold - 0.0001).min(new_p0).max(0.001);
                if new_p0 != self.p0 {
                    let need_update_p0 = should_update_p0_bak3(
                        infer_p0_history.new_update_p0_history(),
                        self.p0,
                        new_p0,
                    );
                    if need_update_p0 && update_p0_times < MAX_UPDATE_P0_TIMES {
                        self.beta_emulator.reset_with_p0(new_p0);
                        // The same-named Python local is never read here either; kept for structural parity.
                        #[allow(unused_assignments)]
                        {
                            emulator = Emulator::new(
                                inner_info.epoch_start_elem_num,
                                new_p0,
                                inner_info.last_epoch_detected_cannot_remove as f64,
                            );
                        }
                        self.p0 = new_p0;
                        infer_p0_history.update_p0_using_history(self.p0, &mut self.p);
                        update_p0_times += 1;
                    }
                }
            }
            self.memory.insert(config2test, outcome);
        }
        ReduceOutcome::Config(self.passconfig.clone())
    }

    /// Samples a candidate delete set (Python `sample`): ascending by undeletable probability (with weights, on equal probability
    /// descending by weight; stable sort), picking the prefix maximizing (i-k+1)·Π(1-p_j) (collected from the high end).
    fn sample(&mut self, weights: Option<&HashMap<T, f64>>) -> Vec<T> {
        let mut entries: Vec<(T, f64)> = self.p.iter().map(|(k, v)| (k.clone(), *v)).collect();
        match weights {
            Some(w) => entries.sort_by(|a, b| {
                a.1.partial_cmp(&b.1)
                    .expect("probability is not NaN")
                    .then_with(|| {
                        let wa = -w.get(&a.0).copied().unwrap_or(0.0);
                        let wb = -w.get(&b.0).copied().unwrap_or(0.0);
                        wa.partial_cmp(&wb).expect("weight is not NaN")
                    })
            }),
            None => entries.sort_by(|a, b| a.1.partial_cmp(&b.1).expect("probability is not NaN")),
        }
        self.p = entries.into_iter().collect();

        let keylist: Vec<T> = self.p.keys().cloned().collect();
        let mut k = 0usize;
        let mut tmp = 1.0f64;
        let mut last = 0.0f64;
        let mut i = 0usize;
        while i < keylist.len() {
            let pi = self.p[&keylist[i]];
            if pi == 0.0 {
                k += 1;
                i += 1;
                continue;
            }
            if pi >= 1.0 {
                // Probability reached 1 (including overflowed inf): the candidate segment ends here. p never becomes NaN.
                break;
            }
            for key in keylist.iter().take(i + 1).skip(k) {
                tmp *= 1.0 - self.p[key];
            }
            tmp *= (i - k + 1) as f64;
            if tmp < last {
                break;
            }
            last = tmp;
            tmp = 1.0;
            i += 1;
        }
        let mut config2test = Vec::new();
        while i > k {
            i -= 1;
            config2test.push(keylist[i].clone());
        }
        config2test
    }

    /// Mirrors Python `_update_probabilities_on_fail`: scale up by the ratio 1/(1-Π(1-p_j))
    /// the probabilities of undetermined elements in the delete set. Ratios are recomputed per element inside the loop (p changes), replicating actual behavior.
    fn update_probabilities_on_fail(&mut self, deleteconfig: &[T], config2test: &[T]) {
        for key in self.p.keys().cloned().collect::<Vec<_>>() {
            if config2test.contains(&key) {
                continue;
            }
            let pv = self.p[&key];
            if pv != 0.0 && pv != 1.0 {
                let ratio = self.compute_ratio(deleteconfig);
                let delta = (ratio - 1.0) * pv;
                self.p[&key] = pv + delta;
            }
        }
    }

    fn compute_ratio(&self, deleteconfig: &[T]) -> f64 {
        let mut tmplog = 1.0;
        for delc in deleteconfig {
            let pv = self.p[delc];
            if pv > 0.0 && pv < 1.0 {
                tmplog *= 1.0 - pv;
            }
        }
        1.0 / (1.0 - tmplog)
    }

    /// Post-PASS handling (the PASS branch of Python `prob_dd.ProbDD._process`):
    /// infers must-keep elements from the difference between historical failure sets and the current delete set.
    fn process_pass(&mut self, config: &[T]) {
        let mut tmp = Vec::new();
        let mut to_preserve: Vec<T> = Vec::new();
        for history in std::mem::take(&mut self.test_history) {
            if history.iter().any(|e| config.contains(e)) {
                let cha: Vec<T> = history.iter().filter(|e| !config.contains(e)).cloned().collect();
                if cha.len() == 1 {
                    let e0 = cha[0].clone();
                    if !to_preserve.contains(&e0) {
                        to_preserve.push(e0);
                    }
                } else {
                    tmp.push(cha);
                }
            } else {
                tmp.push(history);
            }
        }
        self.test_history = tmp;
        self.process_elements_to_preserve(&to_preserve);
    }

    /// Mirrors Python `_processElementToPreserve`. Actual Python behavior:
    /// histories intersecting the must-keep set are shrunk by the difference but **not** put back (the difference is dropped),
    /// i.e. intersecting histories are removed entirely — replicated as-is (P-12).
    fn process_elements_to_preserve(&mut self, to_preserve: &[T]) {
        let mut tmp = Vec::new();
        for history in std::mem::take(&mut self.test_history) {
            if history.iter().any(|e| to_preserve.contains(e)) {
                // Python: cha = history - toBePreserve is never appended back; dropped.
            } else {
                tmp.push(history);
            }
        }
        self.test_history = tmp;
        for elm in to_preserve {
            self.p.insert(elm.clone(), 1.0);
        }
    }

    /// Mirrors Python `_test_done`: all probabilities hit 0/1, or all ≥ threshold (converged).
    fn test_done(&self) -> bool {
        let all_decided = self.p.values().all(|v| *v == 0.0 || *v == 1.0);
        if all_decided {
            return true;
        }
        !self.p.values().any(|v| v.min(1.0) < self.threshold)
    }

    /// Mirrors Python `_get_all_non_determined_elem_num`.
    fn get_all_non_determined_elem_num(&self) -> usize {
        let determined = self
            .p
            .values()
            .filter(|v| **v >= self.threshold || **v < 1e-6)
            .count();
        self.p.len() - determined
    }
}

/// Mirrors Python `_minus`: keeps c1's order, dropping elements present in c2.
fn minus<T: Clone + Eq + Hash>(c1: &[T], c2: &[T]) -> Vec<T> {
    let set: std::collections::HashSet<&T> = c2.iter().collect();
    c1.iter().filter(|c| !set.contains(*c)).cloned().collect()
}
