//! Round statistics and p0-inference emulators (Python `ProbDDUtil/InnerInfo.py`).

use std::collections::HashSet;
use std::hash::Hash;

use indexmap::IndexMap;

fn undetermined_elems<T: Hash + Eq + Clone>(p: &IndexMap<T, f64>) -> HashSet<T> {
    p.iter()
        .filter(|(_, v)| **v > 1e-6 && **v < 1.0)
        .map(|(k, _)| k.clone())
        .collect()
}

/// Round statistics (Python `InnerInfo`). Fields match the Python names.
pub struct InnerInfo<T: Hash + Eq + Clone> {
    pub ori_elem_num: usize,
    pub considered_elems: HashSet<T>,
    pub all_detected_cannot_remove: usize,
    pub all_detected_can_remove: usize,
    pub last_epoch_detected_cannot_remove: usize,
    pub last_epoch_detected_can_remove: usize,
    pub new_cannot_remove: usize,
    pub new_can_remove: usize,
    pub epoch_start_elem_num: usize,
    pub cur_epoch_visited_elem_num: usize,
}

impl<T: Hash + Eq + Clone> InnerInfo<T> {
    pub fn new(p: &IndexMap<T, f64>) -> Self {
        Self {
            ori_elem_num: p.len(),
            considered_elems: p.keys().cloned().collect(),
            all_detected_cannot_remove: 0,
            all_detected_can_remove: 0,
            last_epoch_detected_cannot_remove: 0,
            last_epoch_detected_can_remove: 0,
            new_cannot_remove: 0,
            new_can_remove: 0,
            epoch_start_elem_num: p.len(),
            cur_epoch_visited_elem_num: 0,
        }
    }

    pub fn reset_last_epoch_info(&mut self, p: &IndexMap<T, f64>) {
        self.last_epoch_detected_cannot_remove = 0;
        self.last_epoch_detected_can_remove = 0;
        self.cur_epoch_visited_elem_num = 0;
        self.considered_elems = undetermined_elems(p);
        self.epoch_start_elem_num = self.considered_elems.len();
    }

    pub fn update_one_step_info(&mut self, p: &IndexMap<T, f64>, deleteconfig: &[T]) {
        let cur_all_detected_cannot_remove = p.values().filter(|v| **v >= 1.0).count();
        let cur_all_detected_can_remove = p.values().filter(|v| **v == 0.0).count();
        self.new_cannot_remove = cur_all_detected_cannot_remove - self.all_detected_cannot_remove;
        self.new_can_remove = cur_all_detected_can_remove - self.all_detected_can_remove;
        self.all_detected_cannot_remove = cur_all_detected_cannot_remove;
        self.all_detected_can_remove = cur_all_detected_can_remove;
        self.last_epoch_detected_can_remove += self.new_can_remove;
        self.last_epoch_detected_cannot_remove += self.new_cannot_remove;
        let unde = undetermined_elems(p);
        self.considered_elems.retain(|e| unde.contains(e));
        for d in deleteconfig {
            self.considered_elems.remove(d);
        }
        self.cur_epoch_visited_elem_num += deleteconfig.len();
        assert!(
            self.cur_epoch_visited_elem_num <= self.epoch_start_elem_num,
            "cur_epoch_visited_elem_num {} exceeds epoch_start_elem_num {}",
            self.cur_epoch_visited_elem_num,
            self.epoch_start_elem_num
        );
    }

    pub fn rest_num(&self) -> usize {
        self.ori_elem_num - self.all_detected_can_remove - self.all_detected_cannot_remove
    }
}

/// Expectation emulator (Python `Emulator`).
pub struct Emulator {
    pub exp_cannot_remove: f64,
    pub total_num: f64,
    pub p0: f64,
}

impl Emulator {
    pub fn new(init_num: usize, p0: f64, visited_cannot_remove: f64) -> Self {
        Self {
            exp_cannot_remove: init_num as f64 * p0 - visited_cannot_remove,
            total_num: init_num as f64 - visited_cannot_remove,
            p0,
        }
    }

    pub fn from_direct_num(total_num: usize, cannot_remove_num: f64) -> Self {
        let cannot_remove_num = cannot_remove_num.max(1.0);
        let p0 = if total_num == 0 {
            0.0
        } else {
            cannot_remove_num / total_num as f64
        };
        Self::new(total_num, p0, 0.0)
    }
}

/// Beta-prior parameter emulator (Python `BetaEmulator`).
pub struct BetaEmulator {
    pub alpha: f64,
    pub beta: f64,
}

impl BetaEmulator {
    pub fn new() -> Self {
        Self { alpha: 1.0, beta: 1.0 }
    }

    pub fn reset_with_num(&mut self, considered_cannot_remove: f64, considered_total_num: f64) {
        assert!(considered_total_num > 0.0);
        self.alpha = considered_cannot_remove.max(1.0);
        self.beta = (considered_total_num - considered_cannot_remove).max(1.0);
    }

    pub fn reset_with_p0(&mut self, p0: f64) {
        self.alpha = Self::resize(p0);
        self.beta = Self::resize(1.0 - p0);
    }

    fn resize(num: f64) -> f64 {
        (2.0 * num).max(1.0)
    }
}

impl Default for BetaEmulator {
    fn default() -> Self {
        Self::new()
    }
}
