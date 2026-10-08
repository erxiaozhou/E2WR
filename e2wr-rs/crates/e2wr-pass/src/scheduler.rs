//! Top-level scheduling (M12): fixed-rotation multi-pass orchestration + statistics to disk.
//!
//! Mirrors Python:
//! - `TopReducer/Reducer.py`: `FrameworkReducerDirSystem` (work_dir/logs/
//!   tmp/tmp_phase_output.wasm directory conventions) and `FrameworkReducer.check_before_run`
//!   (input snapshot + output parent directory).
//! - `TopReducer/MultiPassReducerBase.py`: per-pass timeout =
//!   `max(int(elapsed/10), PASS_TIMEOUT)` then min against the **total limit** (note: the total limit,
//!   not the remaining time; copied verbatim); a pass artifact replaces the official output only when it
//!   passes the oracle with real reduction; `tried_since_last_update` tracks passes tried since the last effective update.
//! - `TopReducer/MultiPassReducer.py`: `RandomReducer` (the name misleads; it is a fixed
//!   rotation) — STATE_TRANSITIONS cycles NodeShrink→UnusedDefReducer→
//!   FinalPolishPass→NodeShrink; a pass that succeeds without being cut off by timeout is marked saturated
//!   (rerunning brings no gain) until another pass produces new reduction.
//! - `TopReducer/ReduceLimit.py`: the total-limit check (exceeded → stop).
//! - `TopReducer/PassStatistics.py`: per-pass statistics (JSON + summary text written to disk;
//!   filename prefix = the Python class name RandomReducer).
//! - `ReducerPassUtil/get_reduce_result_util.py`: the three-state pass result
//!   (no reduction → IGNORE_BY_SIZE, otherwise ask the oracle).
//!
//! Known deviations (recorded in the M12 migration notes):
//! - Python pass-internal logging goes through logging into `logs/<PassName>.log`; Rust passes print to
//!   stdout, and the scheduler only creates same-named empty files to keep the directory layout.
//! - reducer.log lines lack the filename/line-number fields of Python logging.
//! - stats JSON indents with 2 spaces (Python json.dump uses 4); fields and order identical.

use std::collections::{BTreeMap, BTreeSet};
use std::io::Write;
use std::path::{Path, PathBuf};
use std::process::{Command, Stdio};
use std::time::Instant;

use anyhow::{bail, Context, Result};

use e2wr_dd::factory::DdFactory;
use e2wr_dd::oracle::Oracle;

use crate::common::ExecResult;
use crate::final_polish::{FinalPolishConfig, FinalPolishPass};
use crate::instseq::v9::OnlyOneInstTask;
use crate::nodeshrink_pass::NodeShrinkPass;
use crate::uur::UurPass;

/// Python `ReducerCommonConfig.PASS_TIMEOUT`.
pub const PASS_TIMEOUT: i64 = 9000;

/// Pass name (= the value of Python `ZReducerType`; names take part in the rotation table and statistics keys).
pub const NAME_NODE_SHRINK: &str = "NodeShrink";
pub const NAME_UNUSED_DEF: &str = "UnusedDefReducer";
pub const NAME_FINAL_POLISH: &str = "FinalPolishPass";

/// Python `MultiPassReducer.STATE_TRANSITIONS`.
fn next_pass_name(name: &str) -> Option<&'static str> {
    match name {
        NAME_NODE_SHRINK => Some(NAME_UNUSED_DEF),
        NAME_UNUSED_DEF => Some(NAME_FINAL_POLISH),
        NAME_FINAL_POLISH => Some(NAME_NODE_SHRINK),
        _ => None,
    }
}

/// Python `RERUN_NO_FURTHER_GAIN_PASSES` (contains all three passes; the saturation test always passes).
fn is_rerun_no_gain(name: &str) -> bool {
    matches!(name, NAME_NODE_SHRINK | NAME_UNUSED_DEF | NAME_FINAL_POLISH)
}

// ---------------------------------------------------------------------------
// The three-state result (Python `ReducerPassUtil/ReduceResult.py` / get_reduce_result_util)
// ---------------------------------------------------------------------------

/// Python `ReduceProcessStatus`.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum ReduceProcessStatus {
    FailOracle,
    PassOracle,
    IgnoreBySize,
}

/// Python `ReduceResult` (the three-state wrapper over `ExecResult`).
#[derive(Debug, Clone)]
pub struct ReduceResult {
    pub exec_result: ExecResult,
    pub reduce_process_status: ReduceProcessStatus,
    pub total_time: f64,
}

impl ReduceResult {
    /// Python `can_accept`: effect_status == ACCEPT ⟺ PASS_ORACLE.
    pub fn can_accept(&self) -> bool {
        self.reduce_process_status == ReduceProcessStatus::PassOracle
    }

    /// Python `pass_oracle`.
    pub fn pass_oracle(&self) -> bool {
        self.reduce_process_status == ReduceProcessStatus::PassOracle
    }

    /// Python `actual_reduced_size`: 0 when rejected; the reduction amount when accepted
    /// (Python asserts non-None — acceptance implies reduction > 0; 0 as a fallback here).
    pub fn actual_reduced_size(&self) -> i64 {
        if !self.can_accept() {
            return 0;
        }
        self.exec_result.reduced_size_num.unwrap_or(0).max(0)
    }
}

/// Python `get_reduce_result` (the always-require_smaller_size=True call form):
/// no reduction → IGNORE_BY_SIZE (oracle not asked); otherwise the oracle decides PASS/FAIL.
/// Oracle execution errors are treated as not passing (consistent with in-pass verdicts).
fn get_reduce_result(
    output_path: &Path,
    exec_result: ExecResult,
    oracle: &Oracle,
    total_time: f64,
) -> ReduceResult {
    let status = if !exec_result.is_size_effective() {
        ReduceProcessStatus::IgnoreBySize
    } else if oracle.check(output_path).unwrap_or(false) {
        ReduceProcessStatus::PassOracle
    } else {
        ReduceProcessStatus::FailOracle
    };
    ReduceResult {
        exec_result,
        reduce_process_status: status,
        total_time,
    }
}

/// The per-pass timeout formula of Python `MultiPassReducerBase.run_one_pass`:
/// `int(min(max(int(elapsed seconds/10), PASS_TIMEOUT), total limit))`.
pub fn compute_pass_timeout(cur_testing_time: f64, limit: Option<u64>) -> i64 {
    let max_timeout = ((cur_testing_time / 10.0) as i64).max(PASS_TIMEOUT);
    match limit {
        Some(t) => max_timeout.min(t as i64),
        None => max_timeout,
    }
}

// ---------------------------------------------------------------------------
// The fixed-rotation state machine (Python `RandomReducer`'s selection/saturation logic, decoupled from execution)
// ---------------------------------------------------------------------------

/// Rotation state (directly drivable by unit tests).
pub struct RotationState {
    pass_names: Vec<&'static str>,
    /// Python `last_selected_pass_name`: a **name state**, not an index. Always initialized to the
    /// virtual seed 'FinalPolishPass' — independent of whether the pass list actually contains
    /// FinalPolishPass (with FinalPolish disabled via `--full_wo_final_polish`, Python still uses it
    /// as the seed, so the first transition FinalPolishPass→NodeShrink selects NodeShrink; when the rotation chain
    /// reaches a name not in the list it is skipped naturally). Fixed 2026-09-30: an index was stored before,
    /// panicking when the list lacked FinalPolishPass (`e2wr full --full_wo_final_polish`
    /// crashed outright), inconsistent with Python.
    last_selected: &'static str,
    saturated: BTreeSet<usize>,
    tried_since_update: BTreeSet<usize>,
    select_times: u64,
}

impl RotationState {
    pub fn new(pass_names: Vec<&'static str>) -> RotationState {
        RotationState {
            pass_names,
            // Python: self.last_selected_pass_name = 'FinalPolishPass' (a virtual
            // seed; the name need not exist in the pass list).
            last_selected: NAME_FINAL_POLISH,
            saturated: BTreeSet::new(),
            tried_since_update: BTreeSet::new(),
            select_times: 0,
        }
    }

    pub fn select_times(&self) -> u64 {
        self.select_times
    }

    // R-31: value getters pass_names()/tried_since_update() with no callers
    // (module internals read the fields directly) were removed.

    fn idx_of(&self, name: &str) -> Option<usize> {
        self.pass_names.iter().position(|n| *n == name)
    }

    /// Python `RandomReducer.select_pass`: saturated passes join the tried set →
    /// candidates minus tried → walk the transition chain from the last selection. Returns the chosen index.
    pub fn select_pass(&mut self, candi: &[usize]) -> Result<usize> {
        let candi_set: BTreeSet<usize> = candi.iter().copied().collect();
        for skip in self.saturated.iter() {
            if candi_set.contains(skip) {
                self.tried_since_update.insert(*skip);
            }
        }
        let effective: Vec<usize> = candi
            .iter()
            .copied()
            .filter(|i| !self.tried_since_update.contains(i))
            .collect();
        // Python asserts this (the run loop breaks when untried empties, guaranteeing reachability).
        if effective.is_empty() {
            bail!("no runnable pass: every candidate is already tried");
        }
        let selected = self.find_next_pass(&effective)?;
        self.last_selected = self.pass_names[selected];
        self.select_times += 1;
        Ok(selected)
    }

    /// Python `_find_next_pass`: walk STATE_TRANSITIONS from the last selection (a name state, possibly a virtual
    /// name not in the list); return the first pass still among the candidates;
    /// error when the whole circle misses.
    fn find_next_pass(&self, effective: &[usize]) -> Result<usize> {
        let effective_set: BTreeSet<usize> = effective.iter().copied().collect();
        let mut visited: BTreeSet<&str> = BTreeSet::new();
        let mut current: &str = self.last_selected;
        while !visited.contains(current) {
            visited.insert(current);
            let Some(next) = next_pass_name(current) else {
                break;
            };
            if let Some(idx) = self.idx_of(next) {
                if effective_set.contains(&idx) {
                    return Ok(idx);
                }
            }
            current = next;
        }
        bail!("Cannot find a valid pass");
    }

    /// Python `_update_saturation`: an effective update clears the saturated set; the updating pass itself,
    /// having completed a full round (not cut by timeout) and being saturable, is marked saturated.
    pub fn update_saturation(
        &mut self,
        pass_idx: usize,
        cause_update: bool,
        is_partial_by_timeout: bool,
    ) {
        if !cause_update {
            return;
        }
        self.saturated.clear();
        let name = self.pass_names[pass_idx];
        if is_rerun_no_gain(name) && !is_partial_by_timeout {
            self.saturated.insert(pass_idx);
        }
    }

    /// run_one_pass's tried-set upkeep: cleared on effective update, otherwise recorded.
    pub fn note_tried(&mut self, pass_idx: usize, cause_update: bool) {
        if cause_update {
            self.tried_since_update.clear();
        } else {
            self.tried_since_update.insert(pass_idx);
        }
    }

    /// The outer loop's untried set (Python's run() recomputes it in place).
    pub fn untried(&self) -> Vec<usize> {
        (0..self.pass_names.len())
            .filter(|i| !self.tried_since_update.contains(i))
            .collect()
    }
}

// ---------------------------------------------------------------------------
// Statistics (Python `PassStatistics.py`)
// ---------------------------------------------------------------------------

#[derive(Debug, Clone)]
struct PassStatEntry {
    called_count: u64,
    success_count: u64,
    failed_count: u64,
    fail_oracle_count: u64,
    fail_size_count: u64,
    fail_exec_count: u64,
    total_reduce_size: i64,
    total_time: f64,
    avg_reduce_size: f64,
    avg_time: f64,
    max_reduce_size: i64,
    /// Python's min_reduce_size starts at inf; an integer after the first success.
    min_reduce_size: Option<i64>,
}

impl PassStatEntry {
    fn new() -> PassStatEntry {
        PassStatEntry {
            called_count: 0,
            success_count: 0,
            failed_count: 0,
            fail_oracle_count: 0,
            fail_size_count: 0,
            fail_exec_count: 0,
            total_reduce_size: 0,
            total_time: 0.0,
            avg_reduce_size: 0.0,
            avg_time: 0.0,
            max_reduce_size: 0,
            min_reduce_size: None,
        }
    }
}

/// Per-pass statistics (Python `PassStatistics`; construction registers all passes).
pub struct PassStatistics {
    names: Vec<&'static str>,
    per_pass: Vec<PassStatEntry>,
    pass_count_since_last_update: BTreeMap<usize, u64>,
    max_min_try_time: i64,
}

/// The JSON-on-disk shape (fields and order = Python `PassStatistics.add_pass`'s dict order).
#[derive(serde::Serialize)]
struct PassStatsJson {
    called_count: u64,
    success_count: u64,
    failed_count: u64,
    fail_oracle_count: u64,
    fail_size_count: u64,
    fail_exec_count: u64,
    total_reduce_size: i64,
    total_time: f64,
    avg_reduce_size: f64,
    avg_time: f64,
    max_reduce_size: i64,
    min_reduce_size: i64,
}

impl PassStatistics {
    pub fn new(names: Vec<&'static str>) -> PassStatistics {
        PassStatistics {
            per_pass: names.iter().map(|_| PassStatEntry::new()).collect(),
            names,
            pass_count_since_last_update: BTreeMap::new(),
            max_min_try_time: -1,
        }
    }

    /// Python `update_stats`.
    pub fn update_stats(&mut self, pass_idx: usize, result: &ReduceResult, cause_update: bool) {
        let e = &mut self.per_pass[pass_idx];
        e.called_count += 1;
        e.total_time += result.total_time;
        *self.pass_count_since_last_update.entry(pass_idx).or_insert(0) += 1;

        let reduced_size = result.actual_reduced_size();
        if reduced_size > 0 {
            e.success_count += 1;
            e.total_reduce_size += reduced_size;
            e.max_reduce_size = e.max_reduce_size.max(reduced_size);
            e.min_reduce_size = Some(
                e.min_reduce_size
                    .map_or(reduced_size, |m| m.min(reduced_size)),
            );
        } else {
            e.failed_count += 1;
            if !result.pass_oracle() {
                e.fail_oracle_count += 1;
            }
            if !result.exec_result.is_successful_exec() {
                e.fail_exec_count += 1;
            }
            if let Some(n) = result.exec_result.reduced_size_num {
                if n <= 0 {
                    e.fail_size_count += 1;
                }
            }
        }

        if cause_update {
            let min_value = self
                .pass_count_since_last_update
                .values()
                .copied()
                .min()
                .unwrap_or(0);
            if min_value as i64 > self.max_min_try_time {
                self.max_min_try_time = min_value as i64;
            }
            self.pass_count_since_last_update.clear();
        }
    }

    /// Python `calculate_averages` (folding min's inf → 0 happens at serialization).
    fn calculate_averages(&mut self) {
        for e in self.per_pass.iter_mut() {
            if e.success_count > 0 {
                e.avg_reduce_size = e.total_reduce_size as f64 / e.success_count as f64;
            }
            if e.called_count > 0 {
                e.avg_time = e.total_time / e.called_count as f64;
            }
        }
    }

    fn json_entries(&mut self) -> Vec<(&'static str, PassStatsJson)> {
        self.calculate_averages();
        self.names
            .iter()
            .copied()
            .zip(self.per_pass.iter().map(|e| PassStatsJson {
                called_count: e.called_count,
                success_count: e.success_count,
                failed_count: e.failed_count,
                fail_oracle_count: e.fail_oracle_count,
                fail_size_count: e.fail_size_count,
                fail_exec_count: e.fail_exec_count,
                total_reduce_size: e.total_reduce_size,
                total_time: e.total_time,
                avg_reduce_size: e.avg_reduce_size,
                avg_time: e.avg_time,
                max_reduce_size: e.max_reduce_size,
                min_reduce_size: e.min_reduce_size.unwrap_or(0),
            }))
            .collect()
    }

    /// Python `get_summary` (a text table; descending by total_reduce_size, stable sort).
    pub fn summary(&mut self) -> String {
        self.calculate_averages();
        let mut s = String::new();
        s.push_str("Pass statistics summary:\n");
        s.push_str(&"-".repeat(120));
        s.push('\n');
        s.push_str(&format!(
            "{:<40} | {:^10} | {:^8} | {:^8} | {:^10} | {:^10} | {:^10} | {:^10} | {:^8} | {:^8} | {:^8}\n",
            "Pass name",
            "called_count",
            "success_count",
            "failed_count",
            "total_reduce_size",
            "avg_reduce_size",
            "total_time",
            "avg_time",
            "fail_oracle_count",
            "fail_size_count",
            "fail_exec_count",
        ));
        s.push_str(&"-".repeat(120));
        s.push('\n');

        let mut order: Vec<usize> = (0..self.names.len()).collect();
        order.sort_by_key(|&i| std::cmp::Reverse(self.per_pass[i].total_reduce_size));
        for i in order {
            let e = &self.per_pass[i];
            s.push_str(&format!(
                "{:<30} | {:^10} | {:^8} | {:^8} | {:^10} | {:^10.2} | {:^10.2} | {:^10.2} | {:^8} | {:^8} | {:^8}\n",
                self.names[i],
                e.called_count,
                e.success_count,
                e.failed_count,
                e.total_reduce_size,
                e.avg_reduce_size,
                e.total_time,
                e.avg_time,
                e.fail_oracle_count,
                e.fail_size_count,
                e.fail_exec_count,
            ));
        }
        s.push_str(&"-".repeat(120));
        s.push('\n');
        s.push_str(&format!("max_min_try_time: {}\n", self.max_min_try_time));
        s
    }

    /// Python `save_to_file` (json + summary files).
    ///
    /// Hand-rolled JSON serialization: field order = Python's dict insertion order, 4-space indent
    /// (serde_json sorts keys by default and cannot keep insertion order).
    pub fn save_to_file(&mut self, json_path: &Path, summary_path: &Path) -> Result<()> {
        let entries = self.json_entries();
        let mut json = String::from("{\n");
        for (i, (name, e)) in entries.iter().enumerate() {
            let fields: [(&str, String); 12] = [
                ("called_count", e.called_count.to_string()),
                ("success_count", e.success_count.to_string()),
                ("failed_count", e.failed_count.to_string()),
                ("fail_oracle_count", e.fail_oracle_count.to_string()),
                ("fail_size_count", e.fail_size_count.to_string()),
                ("fail_exec_count", e.fail_exec_count.to_string()),
                ("total_reduce_size", e.total_reduce_size.to_string()),
                ("total_time", fmt_py_float(e.total_time)),
                ("avg_reduce_size", fmt_py_float(e.avg_reduce_size)),
                ("avg_time", fmt_py_float(e.avg_time)),
                ("max_reduce_size", e.max_reduce_size.to_string()),
                ("min_reduce_size", e.min_reduce_size.to_string()),
            ];
            json.push_str(&format!("    \"{name}\": {{\n"));
            for (j, (k, v)) in fields.iter().enumerate() {
                json.push_str(&format!("        \"{k}\": {v}"));
                json.push_str(if j + 1 < fields.len() { ",\n" } else { "\n" });
            }
            json.push_str(if i + 1 < entries.len() { "    },\n" } else { "    }\n" });
        }
        json.push('}');
        std::fs::write(json_path, json + "\n").with_context(|| {
            format!("write stats json {}", json_path.display())
        })?;
        std::fs::write(summary_path, self.summary()).with_context(|| {
            format!("write stats summary {}", summary_path.display())
        })?;
        Ok(())
    }
}

/// Python `json.dump`'s float format (short repr style; integral floats keep `.0`).
fn fmt_py_float(v: f64) -> String {
    if v == v.trunc() && v.is_finite() && v.abs() < 1e16 {
        format!("{v:.1}")
    } else {
        format!("{v}")
    }
}

// ---------------------------------------------------------------------------
// Pass loading and scheduling (Python `DefaultZReducerFactory` + `RandomReducer.run`)
// ---------------------------------------------------------------------------

/// The loading forms of the three pass kinds (Python `get_default_z_reducer` builds by reducer_type;
/// directories = OneReducerDirSystem.from_framework_dir(tmp/<Name>)).
/// NodeShrink variant boxed (clippy large-difference warning; construction/call semantics unchanged).
pub enum PipelinePass<'a> {
    NodeShrink(Box<NodeShrinkPass<'a>>),
    UnusedDef(UurPass<'a>),
    FinalPolish(FinalPolishPass<'a>),
}

impl PipelinePass<'_> {
    pub fn name(&self) -> &'static str {
        match self {
            PipelinePass::NodeShrink(_) => NAME_NODE_SHRINK,
            PipelinePass::UnusedDef(_) => NAME_UNUSED_DEF,
            PipelinePass::FinalPolish(_) => NAME_FINAL_POLISH,
        }
    }

    fn reduce(&mut self, input: &Path, output: &Path, timeout_s: i64) -> Result<ExecResult> {
        match self {
            PipelinePass::NodeShrink(p) => p.reduce(input, output, timeout_s as f64),
            PipelinePass::UnusedDef(p) => p.reduce(input, output, Some(timeout_s as f64)),
            PipelinePass::FinalPolish(p) => p.reduce(input, output, timeout_s as f64),
        }
    }
}

/// The full-pipeline entry config (the effective parameter surface of Python `script_run_reduce_v2.py`; defaults
/// = Python's actual defaults). Hidden switches (P-1) get no fake CLI flags.
#[derive(Debug, Clone)]
pub struct SchedulerConfig {
    /// --to-test-func-name (required in Python; used by function-level preprocessing/NodeShrink).
    pub to_test_func_name: Option<String>,
    /// Negation of --full_remove_uur (default True).
    pub use_uur: bool,
    /// Negation of --full_wo_final_polish (default True).
    pub use_final_polish: bool,
    /// Negation of --full_wo_update_probdd_p0 (online p0 inference; default True).
    pub dd_p0_pred: bool,
    /// --init_p0 (default 0.1).
    pub dd_initial_p: f64,
    /// The three FinalPolish switches (negations of --disable_polish_return/--disable_inline/
    /// --disable_size_polish).
    pub final_polish: FinalPolishConfig,
    /// --force_inst_mutation (default disable = all stages; since R-11 this uses
    /// [`OnlyOneInstTask`] directly, `Disable` matching Python's disable).
    pub force_inst_mutation: OnlyOneInstTask,
    /// --debug passed through to the passes (D-14: turns on debug-mode artifact validity assertions,
    /// matching the DEBUG parameters of Python pass construction; Python's additional
    /// FRAMEWORK_IN_DEBUG graph-building branch and cProfile are not ported.
    /// D-14).
    pub debug: bool,
    /// Negation of --disable_whole_dd (ONE_V6_CFG.enable_whole_dd; default True).
    pub v6_whole_dd: bool,
}

impl Default for SchedulerConfig {
    fn default() -> SchedulerConfig {
        SchedulerConfig {
            to_test_func_name: None,
            use_uur: true,
            use_final_polish: true,
            dd_p0_pred: true,
            dd_initial_p: 0.1,
            final_polish: FinalPolishConfig::default(),
            force_inst_mutation: OnlyOneInstTask::Disable,
            v6_whole_dd: true,
            debug: false,
        }
    }
}

/// The equivalent of Python `RandomReducer` + `FrameworkReducerDirSystem`.
pub struct Scheduler<'a> {
    oracle: &'a Oracle,
    input: PathBuf,
    output: PathBuf,
    work_dir: PathBuf,
    tmp_output_path: PathBuf,
    reducer_log: PathBuf,
    passes: Vec<PipelinePass<'a>>,
    state: RotationState,
    stats: PassStatistics,
    /// Python `ReduceLimit.timeout` (seconds; None = unlimited).
    limit: Option<u64>,
    /// Python `set_start_time` (None before run → the cur_testing_time branch is unreachable).
    start_time: Option<Instant>,
}

impl<'a> Scheduler<'a> {
    pub fn new(
        oracle: &'a Oracle,
        input: &Path,
        output: &Path,
        work_dir: &Path,
        limit: Option<u64>,
        cfg: &SchedulerConfig,
    ) -> Result<Scheduler<'a>> {
        let log_dir = work_dir.join("logs");
        let tmp_dir = work_dir.join("tmp");
        std::fs::create_dir_all(&log_dir).context("create logs dir")?;
        std::fs::create_dir_all(&tmp_dir).context("create tmp dir")?;

        // Python setup_passes: NodeShrink + [UnusedDefReducer] + [FinalPolishPass].
        let mut dd_factory = DdFactory::default();
        dd_factory.set(cfg.dd_p0_pred, cfg.dd_initial_p);

        let mut passes: Vec<PipelinePass<'a>> = Vec::new();
        passes.push(PipelinePass::NodeShrink(Box::new(
            NodeShrinkPass::with_stage(
                oracle,
                &tmp_dir.join(NAME_NODE_SHRINK),
                cfg.to_test_func_name.as_deref(),
                dd_factory.clone(),
                true,
                cfg.force_inst_mutation,
                cfg.debug,
            )
            .with_v6_whole_dd(cfg.v6_whole_dd),
        )));
        if cfg.use_uur {
            passes.push(PipelinePass::UnusedDef(UurPass::with_dd_factory(
                oracle,
                &tmp_dir.join(NAME_UNUSED_DEF),
                dd_factory.clone(),
                cfg.debug,
            )));
        }
        if cfg.use_final_polish {
            passes.push(PipelinePass::FinalPolish(
                FinalPolishPass::new(
                    oracle,
                    dd_factory,
                    &tmp_dir.join(NAME_FINAL_POLISH),
                    cfg.final_polish,
                    cfg.debug,
                )
                .with_v6(cfg.force_inst_mutation, cfg.v6_whole_dd),
            ));
        }
        // Python asserts: pass names unique (naturally so for the three kinds).
        if passes.is_empty() {
            bail!("no passes available for reduce");
        }

        // Python: get_logger at each pass's construction creates logs/<Name>.log (Rust pass
        // logs go to stdout; only the same-named file is created to keep the directory layout).
        for p in &passes {
            let _ = std::fs::File::create(log_dir.join(format!("{}.log", p.name())));
        }

        let names: Vec<&'static str> = passes.iter().map(|p| p.name()).collect();
        Ok(Scheduler {
            oracle,
            input: input.to_path_buf(),
            output: output.to_path_buf(),
            work_dir: work_dir.to_path_buf(),
            tmp_output_path: work_dir.join("tmp_phase_output.wasm"),
            reducer_log: log_dir.join("reducer.log"),
            passes,
            state: RotationState::new(names.clone()),
            stats: PassStatistics::new(names),
            limit,
            start_time: None,
        })
    }

    fn log_line(&self, msg: &str) {
        let Ok(mut f) = std::fs::OpenOptions::new()
            .create(true)
            .append(true)
            .open(&self.reducer_log)
        else {
            return;
        };
        let ts = std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .map(|d| d.as_secs_f64())
            .unwrap_or(0.0);
        let _ = writeln!(f, "{ts:.3} - INFO - {msg}");
    }

    /// Python `ReduceLimit.limit_is_reached`.
    fn limit_is_reached(&self, start: Instant) -> bool {
        self.limit
            .is_some_and(|t| start.elapsed().as_secs_f64() > t as f64)
    }

    /// Python `check_before_run`: input snapshot + output parent directory.
    fn check_before_run(&self) -> Result<()> {
        std::fs::copy(&self.input, self.work_dir.join("input.wasm"))
            .context("save input snapshot")?;
        if let Some(parent) = self.output.parent() {
            if !parent.as_os_str().is_empty() {
                std::fs::create_dir_all(parent)?;
            }
        }
        Ok(())
    }

    /// Python `post_process` (base class collects the artifact + subclass marks saturation).
    /// Returns whether this was an effective update (oracle pass and acceptance → the temp artifact replaces the official output).
    fn post_process(&mut self, pass_idx: usize, result: &ReduceResult) -> Result<bool> {
        println!("pass_reduce_result: {result:?}");
        self.state.update_saturation(
            pass_idx,
            result.can_accept(),
            result.exec_result.is_partial_by_timeout,
        );
        if !result.exec_result.is_successful_exec() {
            return Ok(false);
        }
        if !self.tmp_output_path.exists() {
            bail!(
                "may_reduced_case {} does not exist",
                self.tmp_output_path.display()
            );
        }
        if result.can_accept() {
            let last_optimal = std::fs::metadata(&self.output)?.len();
            std::fs::copy(&self.tmp_output_path, &self.output).with_context(|| {
                format!(
                    "copy {} -> {}",
                    self.tmp_output_path.display(),
                    self.output.display()
                )
            })?;
            let cur = std::fs::metadata(&self.output)?.len();
            self.log_line(&format!(
                "Last optimal  case size is {last_optimal}, Current case size is {cur}, reduce size is {}",
                last_optimal - cur
            ));
            Ok(true)
        } else {
            Ok(false)
        }
    }

    /// Python `run_one_pass` (select → timeout → reduce_and_check → post →
    /// tried set → statistics → logging).
    fn run_one_pass(&mut self, candi: &[usize]) -> Result<ReduceResult> {
        let selected = self.state.select_pass(candi)?;
        let name = self.passes[selected].name();
        let cur_testing_time = self
            .start_time
            .map(|t| t.elapsed().as_secs_f64())
            .unwrap_or(0.0);
        let cur_pass_timeout = compute_pass_timeout(cur_testing_time, self.limit);
        println!(
            "Run pass: {name}, cur pass timeout: {cur_pass_timeout:.2}, cur_testing_time: {cur_testing_time:.2} selection times: {}",
            self.state.select_times()
        );

        // reduce_and_check: reduce timed (oracle re-check excluded), then the three-state decision.
        let t0 = Instant::now();
        let exec_result = self.passes[selected].reduce(&self.output, &self.tmp_output_path, cur_pass_timeout)?;
        let total_time = t0.elapsed().as_secs_f64();
        let result = get_reduce_result(&self.tmp_output_path, exec_result, self.oracle, total_time);
        println!("input_path: {}", self.output.display());
        println!("output_path: {}", self.tmp_output_path.display());
        println!(
            "ZPASS {name} can accept: {} pass_oracle: {}",
            result.can_accept(),
            result.pass_oracle()
        );

        let cause_update = self.post_process(selected, &result)?;
        self.state.note_tried(selected, cause_update);
        self.stats.update_stats(selected, &result, cause_update);
        self.log_line(&format!(
            "Run pass: {name}, result: {result:?}, cur_testing_time: {cur_testing_time:.2}"
        ));
        Ok(result)
    }

    /// Python `RandomReducer.run`: fixed rotation until every pass since the last effective update
    /// has been tried (untried empty) or the total limit hits; statistics to disk; wasm→wat export.
    pub fn run(&mut self) -> Result<()> {
        self.check_before_run()?;
        let start_time = Instant::now();
        self.start_time = Some(start_time);
        std::fs::copy(&self.input, &self.output).context("copy input to output")?;
        println!("Log is at  {}", self.reducer_log.display());

        let mut candi: Vec<usize> = (0..self.passes.len()).collect();
        while !self.limit_is_reached(start_time) {
            self.run_one_pass(&candi)?;
            let untried = self.state.untried();
            println!("Untried passes since last update: {} passes ", untried.len());
            self.log_line(&format!(
                "Untried passes since last update: {} passes ",
                untried.len()
            ));
            if untried.is_empty() {
                break;
            }
            candi = untried;
        }

        // Statistics to disk: RandomReducer_stats.json / _summary.txt under the output parent directory.
        let stats_dir = self
            .output
            .parent()
            .filter(|p| !p.as_os_str().is_empty())
            .unwrap_or_else(|| Path::new("."));
        std::fs::create_dir_all(stats_dir)?;
        let json_path = stats_dir.join("RandomReducer_stats.json");
        let summary_path = stats_dir.join("RandomReducer_stats_summary.txt");
        self.stats.save_to_file(&json_path, &summary_path)?;
        self.log_line(&format!("stats data saved to: {}", json_path.display()));
        self.log_line(&format!(
            "stats summary saved to: {}",
            summary_path.display()
        ));
        println!("stats data saved to: {}", json_path.display());

        // The wasm2wat --no-check at the end of Python run() (5 s timeout, failures ignored).
        // The wat path = the output with the last extension replaced by .wat (with_suffix('.wat')).
        let wat_path = self.output.with_extension("wat");
        wasm2wat_no_check(&self.output, &wat_path);
        println!("wasm saved to: {}", self.output.display());
        println!("wat saved to: {}", wat_path.display());
        println!("Log is at  {}", self.reducer_log.display());
        Ok(())
    }
}

/// Python `util/debug_util.wasm2wat`: `wasm2wat --no-check <in> -o <out>`,
/// 5-second timeout; failure/missing command silently ignored (Python runs it and never checks).
fn wasm2wat_no_check(wasm: &Path, wat: &Path) {
    let Ok(mut child) = Command::new("wasm2wat")
        .arg("--no-check")
        .arg(wasm)
        .arg("-o")
        .arg(wat)
        .stdout(Stdio::null())
        .stderr(Stdio::null())
        .spawn()
    else {
        return;
    };
    let start = Instant::now();
    loop {
        match child.try_wait() {
            Ok(Some(_)) => return,
            Ok(None) => {
                if start.elapsed() > std::time::Duration::from_secs(5) {
                    let _ = child.kill();
                    let _ = child.wait();
                    println!(
                        "Process timeout, execution time exceeds 5 seconds, terminating..."
                    );
                    return;
                }
                std::thread::sleep(std::time::Duration::from_millis(50));
            }
            Err(_) => return,
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::common::ExecStatus;

    fn exec(status: ExecStatus, size: Option<i64>, inst: Option<u64>) -> ExecResult {
        ExecResult {
            exec_status: status,
            exec_taken_time: 1.0,
            reduced_size_num: size,
            reduced_inst_num: inst,
            is_partial_by_timeout: false,
        }
    }

    #[test]
    fn pass_timeout_formula() {
        // max(int(t/10), 9000) then min against the total limit (the total limit, not the remaining).
        assert_eq!(compute_pass_timeout(0.0, Some(9000)), 9000);
        assert_eq!(compute_pass_timeout(15.0, Some(9000)), 9000);
        assert_eq!(compute_pass_timeout(100000.0, Some(9000)), 9000);
        assert_eq!(compute_pass_timeout(100000.0, None), 10000);
        assert_eq!(compute_pass_timeout(100000.0, Some(60)), 60);
        assert_eq!(compute_pass_timeout(0.0, Some(60)), 60);
    }

    #[test]
    fn rotation_first_pick_and_round_robin() {
        let names = vec![NAME_NODE_SHRINK, NAME_UNUSED_DEF, NAME_FINAL_POLISH];
        let mut st = RotationState::new(names);
        let all = vec![0, 1, 2];
        // Initial last='FinalPolishPass' → transition hits NodeShrink.
        assert_eq!(st.select_pass(&all).unwrap(), 0);
        // No update → rotation reaches UnusedDefReducer.
        st.note_tried(0, false);
        assert_eq!(st.select_pass(&all).unwrap(), 1);
        st.note_tried(1, false);
        assert_eq!(st.select_pass(&all).unwrap(), 2);
        // All tried → select errors (the outer loop already broke when untried emptied).
        st.note_tried(2, false);
        assert!(st.select_pass(&all).is_err());
        assert_eq!(st.select_times(), 3);
    }

    #[test]
    fn saturation_marks_and_unfreezes() {
        let names = vec![NAME_NODE_SHRINK, NAME_UNUSED_DEF, NAME_FINAL_POLISH];
        let mut st = RotationState::new(names);
        let all = vec![0, 1, 2];
        // NodeShrink succeeds and completes → saturated {NodeShrink}.
        assert_eq!(st.select_pass(&all).unwrap(), 0);
        st.update_saturation(0, true, false);
        st.note_tried(0, true); // an effective update clears the tried set
        // The next selection skips the saturated NodeShrink (folded into tried); rotation reaches UnusedDef.
        assert_eq!(st.select_pass(&all).unwrap(), 1);
        // UnusedDef succeeds → the saturated set clears then re-includes itself; NodeShrink unfreezes.
        st.update_saturation(1, true, false);
        st.note_tried(1, true);
        assert_eq!(st.select_pass(&all).unwrap(), 2);
        st.note_tried(2, false);
        // After FinalPolish updates nothing, rotation should return to NodeShrink (saturation was cleared).
        assert_eq!(st.select_pass(&all).unwrap(), 0);
    }

    #[test]
    fn saturation_skipped_when_partial_by_timeout() {
        let names = vec![NAME_NODE_SHRINK, NAME_UNUSED_DEF, NAME_FINAL_POLISH];
        let mut st = RotationState::new(names);
        st.update_saturation(0, true, true);
        // A success cut off by timeout does not mark saturation.
        assert!(st.tried_since_update.is_empty());
    }

    #[test]
    fn untried_tracks_updates() {
        let names = vec![NAME_NODE_SHRINK, NAME_UNUSED_DEF, NAME_FINAL_POLISH];
        let mut st = RotationState::new(names);
        st.note_tried(0, false);
        st.note_tried(1, false);
        assert_eq!(st.untried(), vec![2]);
        st.note_tried(2, true);
        assert_eq!(st.untried(), vec![0, 1, 2]);
    }

    #[test]
    fn rotation_without_final_polish_pass() {
        // `--full_wo_final_polish`: the list lacks FinalPolishPass (fixed 2026-09-30:
        // RotationState::new used to panic outright). Python's virtual seed
        // 'FinalPolishPass' need not be in the list; the rotation chain skips missing names.
        let names = vec![NAME_NODE_SHRINK, NAME_UNUSED_DEF];
        let mut st = RotationState::new(names);
        let all = vec![0, 1];
        // Initial last='FinalPolishPass' (virtual) → transition hits NodeShrink.
        assert_eq!(st.select_pass(&all).unwrap(), 0);
        st.note_tried(0, false);
        // NodeShrink→UnusedDefReducer.
        assert_eq!(st.select_pass(&all).unwrap(), 1);
        // After an effective update clears the tried set, select: UnusedDefReducer→FinalPolishPass
        // (absent, skipped) →NodeShrink.
        st.note_tried(1, true);
        assert_eq!(st.select_pass(&all).unwrap(), 0);
    }

    #[test]
    fn reduce_result_three_states() {
        // No reduction → IGNORE_BY_SIZE (oracle not asked).
        let r = get_reduce_result(
            Path::new("/nonexistent"),
            exec(ExecStatus::Success, Some(0), Some(0)),
            &Oracle::new("/bin/false"),
            1.0,
        );
        assert_eq!(r.reduce_process_status, ReduceProcessStatus::IgnoreBySize);
        assert!(!r.can_accept());
        assert_eq!(r.actual_reduced_size(), 0);

        // Reduction + oracle pass (/bin/true) → PASS_ORACLE.
        let tmp = std::env::temp_dir().join(format!("e2wr-sched-{}.wasm", std::process::id()));
        std::fs::write(&tmp, b"\0asm").unwrap();
        let r = get_reduce_result(
            &tmp,
            exec(ExecStatus::Success, Some(5), Some(0)),
            &Oracle::new("/bin/true"),
            1.0,
        );
        assert_eq!(r.reduce_process_status, ReduceProcessStatus::PassOracle);
        assert!(r.can_accept());
        assert_eq!(r.actual_reduced_size(), 5);

        // Reduction + oracle rejects (/bin/false) → FAIL_ORACLE.
        let r = get_reduce_result(
            &tmp,
            exec(ExecStatus::Success, Some(5), None),
            &Oracle::new("/bin/false"),
            1.0,
        );
        assert_eq!(r.reduce_process_status, ReduceProcessStatus::FailOracle);
        assert_eq!(r.actual_reduced_size(), 0);
        let _ = std::fs::remove_file(&tmp);
    }

    #[test]
    fn stats_update_and_json_shape() {
        let names = vec![NAME_NODE_SHRINK, NAME_UNUSED_DEF, NAME_FINAL_POLISH];
        let mut stats = PassStatistics::new(names);

        // NodeShrink: two successes (reduction 100/20), one without update.
        stats.update_stats(
            0,
            &ReduceResult {
                exec_result: exec(ExecStatus::Success, Some(100), Some(1)),
                reduce_process_status: ReduceProcessStatus::PassOracle,
                total_time: 2.0,
            },
            true,
        );
        stats.update_stats(
            0,
            &ReduceResult {
                exec_result: exec(ExecStatus::Success, Some(20), Some(1)),
                reduce_process_status: ReduceProcessStatus::PassOracle,
                total_time: 3.0,
            },
            false,
        );
        stats.update_stats(
            0,
            &ReduceResult {
                exec_result: exec(ExecStatus::Success, Some(0), Some(0)),
                reduce_process_status: ReduceProcessStatus::IgnoreBySize,
                total_time: 1.0,
            },
            false,
        );
        // UnusedDef: one failure (oracle rejects).
        stats.update_stats(
            1,
            &ReduceResult {
                exec_result: exec(ExecStatus::Success, Some(8), None),
                reduce_process_status: ReduceProcessStatus::FailOracle,
                total_time: 4.0,
            },
            false,
        );

        // At the first cause_update, min(pass counts)=1 → max_min_try_time=1.
        assert_eq!(stats.max_min_try_time, 1);

        let dir = std::env::temp_dir().join(format!("e2wr-sched-stats-{}", std::process::id()));
        let _ = std::fs::remove_dir_all(&dir);
        std::fs::create_dir_all(&dir).unwrap();
        let json_path = dir.join("RandomReducer_stats.json");
        let summary_path = dir.join("RandomReducer_stats_summary.txt");
        stats.save_to_file(&json_path, &summary_path).unwrap();

        let v: serde_json::Value =
            serde_json::from_str(&std::fs::read_to_string(&json_path).unwrap()).unwrap();
        let ns = &v[NAME_NODE_SHRINK];
        assert_eq!(ns["called_count"], 3);
        assert_eq!(ns["success_count"], 2);
        assert_eq!(ns["failed_count"], 1);
        assert_eq!(ns["fail_oracle_count"], 1);
        assert_eq!(ns["fail_size_count"], 1);
        assert_eq!(ns["fail_exec_count"], 0);
        assert_eq!(ns["total_reduce_size"], 120);
        assert_eq!(ns["max_reduce_size"], 100);
        assert_eq!(ns["min_reduce_size"], 20);
        assert_eq!(ns["avg_reduce_size"], 60.0);
        assert_eq!(ns["avg_time"], 2.0);
        // Field order = Python add_pass's insertion order.
        let text = std::fs::read_to_string(&json_path).unwrap();
        let keys: Vec<&str> = text
            .lines()
            .filter_map(|l| l.trim().strip_prefix('"'))
            .map(|l| l.split('"').next().unwrap())
            .collect();
        assert_eq!(
            keys,
            vec![
                "NodeShrink",
                "called_count",
                "success_count",
                "failed_count",
                "fail_oracle_count",
                "fail_size_count",
                "fail_exec_count",
                "total_reduce_size",
                "total_time",
                "avg_reduce_size",
                "avg_time",
                "max_reduce_size",
                "min_reduce_size",
                "UnusedDefReducer",
                "called_count",
                "success_count",
                "failed_count",
                "fail_oracle_count",
                "fail_size_count",
                "fail_exec_count",
                "total_reduce_size",
                "total_time",
                "avg_reduce_size",
                "avg_time",
                "max_reduce_size",
                "min_reduce_size",
                "FinalPolishPass",
                "called_count",
                "success_count",
                "failed_count",
                "fail_oracle_count",
                "fail_size_count",
                "fail_exec_count",
                "total_reduce_size",
                "total_time",
                "avg_reduce_size",
                "avg_time",
                "max_reduce_size",
                "min_reduce_size",
            ]
        );

        let summary = std::fs::read_to_string(&summary_path).unwrap();
        assert!(summary.contains("Pass statistics summary:"));
        assert!(summary.contains("max_min_try_time: 1"));
        // Descending: NodeShrink(120) before UnusedDefReducer(0).
        let ns_pos = summary.find(NAME_NODE_SHRINK).unwrap();
        let uur_pos = summary.find(NAME_UNUSED_DEF).unwrap();
        assert!(ns_pos < uur_pos);
        let _ = std::fs::remove_dir_all(&dir);
    }

    #[test]
    fn min_reduce_size_folds_to_zero_without_success() {
        let names = vec![NAME_UNUSED_DEF];
        let mut stats = PassStatistics::new(names);
        stats.update_stats(
            0,
            &ReduceResult {
                exec_result: exec(ExecStatus::ExecFailed, Some(0), Some(0)),
                reduce_process_status: ReduceProcessStatus::IgnoreBySize,
                total_time: 1.0,
            },
            false,
        );
        let dir = std::env::temp_dir().join(format!("e2wr-sched-stats2-{}", std::process::id()));
        let _ = std::fs::remove_dir_all(&dir);
        std::fs::create_dir_all(&dir).unwrap();
        stats
            .save_to_file(&dir.join("a.json"), &dir.join("a_summary.txt"))
            .unwrap();
        let text = std::fs::read_to_string(dir.join("a.json")).unwrap();
        assert!(text.contains("\"min_reduce_size\": 0"));
        // Failure + EXEC_FAILED + no reduction → each of the three failure counters +1 (fail_size per
        // reduced_size_num=Some(0)<=0).
        assert!(text.contains("\"fail_exec_count\": 1"));
        assert!(text.contains("\"fail_size_count\": 1"));
        assert!(text.contains("\"fail_oracle_count\": 1"));
        let _ = std::fs::remove_dir_all(&dir);
    }
}
