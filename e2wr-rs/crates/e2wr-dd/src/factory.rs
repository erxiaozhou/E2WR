//! ProbDD factory configuration surface (Python `ProbDDUtil/ProbDDFactory.py`).
//!
//! Python keeps a global singleton; Rust passes it explicitly through the context (per the global-singleton handling rule).
//! `use_a_initp_temp`/`reset_initp` let FuncRemovalPass temporarily switch the initial probability to 0.0001;
//! the mechanism is kept (M1.4).

use std::collections::HashMap;
use std::hash::Hash;

use crate::probdd::ProbDD;

#[derive(Clone, Debug)]
pub struct DdFactory {
    default_initial_p: f64,
    use_p0_pred: bool,
    tmp_last_p: Option<f64>,
}

impl Default for DdFactory {
    fn default() -> Self {
        // Python class-attribute defaults: default_initialP = 0.1, _use_p0_pred = False.
        // The real entry point (script_run_reduce_v2.py) sets use_p0_pred=True.
        Self {
            default_initial_p: 0.1,
            use_p0_pred: false,
            tmp_last_p: None,
        }
    }
}

impl DdFactory {
    /// Mirrors `set_probdd_factory(use_p0_pred, initialP, logger)`.
    pub fn set(&mut self, use_p0_pred: bool, initial_p: f64) {
        self.use_p0_pred = use_p0_pred;
        self.default_initial_p = initial_p;
    }

    /// Mirrors `enable_p0_pred`.
    pub fn enable_p0_pred(&mut self) {
        self.use_p0_pred = true;
    }

    /// Mirrors `set_default_initialP`.
    pub fn set_default_initial_p(&mut self, initial_p: f64) {
        self.default_initial_p = initial_p;
    }

    /// Mirrors `use_a_initp_temp`: temporarily switch the default initial probability.
    pub fn use_a_initp_temp(&mut self, tmp_initial_p: f64) {
        self.tmp_last_p = Some(self.default_initial_p);
        self.default_initial_p = tmp_initial_p;
    }

    /// Mirrors `reset_initp`: restore the default initial probability switched away by `use_a_initp_temp`.
    pub fn reset_initp(&mut self) {
        if let Some(last) = self.tmp_last_p.take() {
            self.default_initial_p = last;
        }
    }

    /// Mirrors `get_default_probdd(test, ...)` (cache and logger not ported:
    /// ConfigCache is write-only, the logger only logs).
    pub fn create_probdd<T: Clone + Eq + Hash + Ord>(&self) -> ProbDD<T> {
        self.create_probdd_with_given(None)
    }

    pub fn create_probdd_with_given<T: Clone + Eq + Hash + Ord>(
        &self,
        given_inip: Option<HashMap<T, f64>>,
    ) -> ProbDD<T> {
        ProbDD::new(self.default_initial_p, self.use_p0_pred, given_inip)
    }
}
