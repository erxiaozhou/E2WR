//! History replay for p0 updates (Python `ProbDDUtil/InferP0History.py`).
//!
//! Implementation-detail note: when rebuilding history Python derives element order via `list(set)`;
//! CPython iterates sets of small non-negative integers in ascending value order, so collecting into a Rust `BTreeSet`
//! is semantically equivalent (the order only affects the multiplication order during replay).

use std::collections::{BTreeSet, HashSet};
use std::hash::Hash;

use indexmap::IndexMap;

const NEW_UPDATE_P0_HISTORY_MAXLEN: usize = 10;

/// p0 inference history (Python `InferP0History`).
/// `new_history` is a FIFO queue of capacity 10 (Python `deque(maxlen=10)`).
pub struct InferP0History<T: Hash + Eq + Clone + Ord> {
    all_history: Vec<(Vec<T>, i32)>,
    new_history: Vec<(Vec<T>, i32)>,
}

impl<T: Hash + Eq + Clone + Ord> Default for InferP0History<T> {
    fn default() -> Self {
        Self::new()
    }
}

impl<T: Hash + Eq + Clone + Ord> InferP0History<T> {
    pub fn new() -> Self {
        Self {
            all_history: Vec::new(),
            new_history: Vec::new(),
        }
    }

    pub fn add_one_log(&mut self, deleteconfig: &[T], test_result: i32) {
        if self.new_history.len() == NEW_UPDATE_P0_HISTORY_MAXLEN {
            self.new_history.remove(0);
        }
        self.new_history.push((deleteconfig.to_vec(), test_result));
        self.all_history.push((deleteconfig.to_vec(), test_result));
    }

    pub fn reset_new_update_p0_history(&mut self) {
        self.new_history.clear();
    }

    pub fn new_update_p0_history(&self) -> &[(Vec<T>, i32)] {
        &self.new_history
    }

    /// Updates p by replaying the whole history (Python `update_p0_using_history`).
    pub fn update_p0_using_history(&self, p0: f64, p: &mut IndexMap<T, f64>) {
        let reconstructed = reconstruct_history(&self.all_history, p);
        for v in p.values_mut() {
            // Determined probabilities (≈0 or ≈1) stay untouched; the rest reset to p0.
            if *v <= 1e-6 || *v >= 1.0 - 1e-6 {
                continue;
            }
            *v = p0;
        }
        for (dc, _) in &reconstructed {
            update_p_using_one_log(dc, p);
        }
    }
}

/// Mirrors Python `_get_reconstruct_p0_history`: keep only histories with R==1 and no must-keep elements,
/// drop already-determined deletable elements, and deduplicate by set semantics.
fn reconstruct_history<T: Hash + Eq + Clone + Ord>(
    all_history: &[(Vec<T>, i32)],
    p: &IndexMap<T, f64>,
) -> Vec<(Vec<T>, i32)> {
    let eq1_elems: HashSet<&T> = p
        .iter()
        .filter(|(_, v)| **v >= 1.0)
        .map(|(k, _)| k)
        .collect();
    let eq0_elems: HashSet<&T> = p
        .iter()
        .filter(|(_, v)| **v <= 1e-6)
        .map(|(k, _)| k)
        .collect();
    let mut result = Vec::new();
    let mut visited: HashSet<BTreeSet<&T>> = HashSet::new();
    for (elems, r) in all_history {
        if *r == 0 {
            continue;
        }
        if elems.iter().any(|e| eq1_elems.contains(e)) {
            continue;
        }
        let cur: BTreeSet<&T> = elems.iter().filter(|e| !eq0_elems.contains(*e)).collect();
        if !visited.insert(cur.clone()) {
            continue;
        }
        result.push((cur.into_iter().cloned().collect(), 1));
    }
    result
}

/// Mirrors Python `_update_p_using_one_log`: ratios are recomputed per element inside the loop (p changes),
/// replicating the actual behavior.
fn update_p_using_one_log<T: Hash + Eq + Clone + Ord>(
    deleteconfig: &[T],
    p: &mut IndexMap<T, f64>,
) {
    for elem in deleteconfig {
        let cur = p[elem];
        if cur > 0.8 {
            continue;
        }
        let res = compute_ratio(deleteconfig, p);
        p.insert(elem.clone(), (cur * res).min(0.79));
    }
}

/// Mirrors Python `_computRatio`.
fn compute_ratio<T: Hash + Eq + Clone + Ord>(
    deleteconfig: &[T],
    p: &IndexMap<T, f64>,
) -> f64 {
    let mut tmplog = 1.0;
    for delc in deleteconfig {
        let pv = p[delc];
        if pv > 0.0 && pv < 1.0 {
            tmplog *= 1.0 - pv;
        }
    }
    1.0 / (1.0 - tmplog)
}
