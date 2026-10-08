//! e2wr CLI entry point (M4 latter half; M12 adds the full-pipeline entry).
//!
//! Subcommands (matching the behavioral contract of the Python baseline `script_run_nodeshrink.py --use_uur`:
//! verify the input against the oracle first, re-verify the output once, then print the outcome and elapsed time):
//! - `uur`: standalone UnusedDefReducer run;
//! - `final-polish`: the five definition-level FinalPolish steps (full orchestration added in M11);
//! - `func-level`: standalone round-0 function-level preprocessing of NodeShrinkPass (M8/M9;
//!   delta-debugging factory config matches script_run_nodeshrink.py: online p0 inference + 0.1).
//! - `instseq`: standalone single-stage/full-stage NodeShrink run (M10).
//! - `full`: the fixed three-stage rotation (M12, matching the effective parameter
//!   surface of `script_run_reduce_v2.py`; hidden switches get no fake flags, P-1).

use std::path::PathBuf;
use std::process::ExitCode;

use anyhow::{bail, Context, Result};
use e2wr_dd::factory::DdFactory;
use e2wr_dd::oracle::Oracle;
use e2wr_pass::common::ExecStatus;
use e2wr_pass::final_polish::{FinalPolishConfig, FinalPolishPass};
use e2wr_pass::func_level::{FuncLevelConfig, FuncLevelCtx, FuncLevelPass};
use e2wr_pass::instseq::v9::OnlyOneInstTask;
use e2wr_pass::nodeshrink_pass::NodeShrinkPass;
use e2wr_pass::scheduler::{Scheduler, SchedulerConfig};
use e2wr_pass::uur::UurPass;

struct Args {
    input: PathBuf,
    output: PathBuf,
    oracle: PathBuf,
    result_dir: PathBuf,
    timeout: f64,
    debug: bool,
    // func-level / instseq only.
    to_test_func_name: Option<String>,
    disable_callsite: bool,
    callsite_as_unreachable: bool,
    // instseq only. --stage full = OnlyOneInstTask::Disable (all stages).
    stage: OnlyOneInstTask,
    // final-polish only (negations of flags like --disable-polish-return).
    disable_polish_return: bool,
    disable_inline: bool,
    disable_size_polish: bool,
}

fn parse_args(args: &[String], default_timeout: f64) -> Result<Args> {
    let mut a = Args {
        input: PathBuf::new(),
        output: PathBuf::new(),
        oracle: PathBuf::new(),
        result_dir: PathBuf::new(),
        timeout: default_timeout,
        debug: false,
        to_test_func_name: None,
        disable_callsite: false,
        callsite_as_unreachable: false,
        stage: OnlyOneInstTask::Disable,
        disable_polish_return: false,
        disable_inline: false,
        disable_size_polish: false,
    };
    let mut i = 0;
    while i < args.len() {
        let arg = args[i].as_str();
        let take = |i: &mut usize, arg: &str| -> Result<String> {
            *i += 1;
            Ok(args
                .get(*i)
                .with_context(|| format!("missing value for {arg}"))?
                .clone())
        };
        match arg {
            "--input" => a.input = PathBuf::from(take(&mut i, arg)?),
            "--output" => a.output = PathBuf::from(take(&mut i, arg)?),
            "--oracle" => a.oracle = PathBuf::from(take(&mut i, arg)?),
            "--result-dir" => a.result_dir = PathBuf::from(take(&mut i, arg)?),
            "--timeout" => {
                i += 1;
                a.timeout = args
                    .get(i)
                    .with_context(|| "missing value for --timeout")?
                    .parse()
                    .context("invalid --timeout")?;
            }
            "--debug" => a.debug = true,
            "--to-test-func-name" => {
                a.to_test_func_name = Some(take(&mut i, arg)?);
            }
            "--disable-callsite" => a.disable_callsite = true,
            "--callsite-as-unreachable" => a.callsite_as_unreachable = true,
            "--disable-polish-return" => a.disable_polish_return = true,
            "--disable-inline" => a.disable_inline = true,
            "--disable-size-polish" => a.disable_size_polish = true,
            "--stage" => {
                let v = take(&mut i, arg)?;
                a.stage = match v.as_str() {
                    "full" => OnlyOneInstTask::Disable,
                    "p3" => OnlyOneInstTask::P3,
                    "core" => OnlyOneInstTask::Core,
                    "rev" => OnlyOneInstTask::Rev,
                    other => bail!("invalid --stage {other} (full|p3|core|rev)"),
                };
            }
            other => bail!("unknown argument {other}"),
        }
        i += 1;
    }
    if a.input.as_os_str().is_empty() {
        bail!("--input is required");
    }
    if a.output.as_os_str().is_empty() {
        bail!("--output is required");
    }
    if a.oracle.as_os_str().is_empty() {
        bail!("--oracle is required");
    }
    if a.result_dir.as_os_str().is_empty() {
        bail!("--result-dir is required");
    }
    Ok(a)
}

fn run(cmd: &str, args: &[String]) -> Result<ExitCode> {
    if cmd == "full" {
        return run_full(args);
    }
    let a = match cmd {
        "uur" => parse_args(args, 10800.0)?,
        "final-polish" => parse_args(args, 9000.0)?,
        "func-level" => parse_args(args, 10800.0)?,
        "instseq" => parse_args(args, 10800.0)?,
        other => {
            bail!(
                "unknown subcommand {other}\nusage: e2wr <uur|final-polish|func-level|instseq|full> --input I --output O --oracle P --result-dir D [--timeout S] [--debug]\n  final-polish extra args: [--disable-polish-return] [--disable-inline] [--disable-size-polish]\n  func-level extra args: --to-test-func-name N (required) [--disable-callsite] [--callsite-as-unreachable]\n  instseq extra args: --to-test-func-name N (required) [--stage full|p3|core|rev]\n  full extra args (matching script_run_reduce_v2.py): --to-test-func-name N (required) [-i/-o/--oracle/-w/-t/--init_p0/--seed/--debug plus ablation switches]"
            )
        }
    };
    for (name, p) in [("input", &a.input), ("oracle", &a.oracle)] {
        if !p.exists() {
            bail!("{name} does not exist: {}", p.display());
        }
    }
    let to_test_func_name = match (cmd, &a.to_test_func_name) {
        ("func-level", None) => bail!("--to-test-func-name is required for func-level"),
        ("instseq", None) => bail!("--to-test-func-name is required for instseq"),
        (_, Some(n)) => n.clone(),
        _ => String::new(),
    };

    let oracle = Oracle::new(a.oracle.to_string_lossy().as_ref());
    if !oracle.check(&a.input).context("oracle failed on input")? {
        bail!("oracle rejected input: {}", a.input.display());
    }
    if a.result_dir.exists() {
        std::fs::remove_dir_all(&a.result_dir).with_context(|| format!("clean {}", a.result_dir.display()))?;
    }
    if let Some(parent) = a.output.parent() {
        std::fs::create_dir_all(parent)?;
    }

    let start = std::time::Instant::now();
    let result = match cmd {
        "uur" => UurPass::new(&oracle, &a.result_dir, a.debug)
            .reduce(&a.input, &a.output, Some(a.timeout)),
        "final-polish" => {
            // Factory config matches script_run_reduce_v2.py: online p0 inference + 0.1.
            let mut factory = DdFactory::default();
            factory.enable_p0_pred();
            factory.set_default_initial_p(0.1);
            let cfg = FinalPolishConfig {
                polish_return_type: !a.disable_polish_return,
                enable_inline: !a.disable_inline,
                enable_size_polish: !a.disable_size_polish,
            };
            let pass = FinalPolishPass::new(&oracle, factory, &a.result_dir, cfg, a.debug);
            pass.reduce(&a.input, &a.output, a.timeout)
        }
        "func-level" => {
            // Matches script_run_nodeshrink.py: online p0 inference + initial probability 0.1.
            let mut factory = DdFactory::default();
            factory.enable_p0_pred();
            factory.set_default_initial_p(0.1);
            let cfg = FuncLevelConfig {
                disable_callsite: a.disable_callsite,
                callsite_as_unreachable: a.callsite_as_unreachable,
                ..Default::default()
            };
            let mut ctx = FuncLevelCtx::new(
                &oracle,
                &a.result_dir,
                &to_test_func_name,
                factory,
                cfg,
                a.debug,
            );
            FuncLevelPass::reduce(&mut ctx, &a.input, &a.output, Some(a.timeout))
        }
        "instseq" => {
            // Full-stage NodeShrink (--stage single-stage semantics maps to --force_inst_mutation;
            // factory config matches script_run_reduce_v2.py: online p0 inference + 0.1).
            let mut factory = DdFactory::default();
            factory.enable_p0_pred();
            factory.set_default_initial_p(0.1);
            let mut pass = NodeShrinkPass::with_stage(
                &oracle,
                &a.result_dir,
                Some(&to_test_func_name),
                factory,
                true,
                a.stage,
                a.debug,
            );
            pass.reduce(&a.input, &a.output, a.timeout)
        }
        _ => unreachable!(),
    }?;
    let taken = start.elapsed().as_secs_f64();

    if !oracle.check(&a.output).context("oracle failed on output")? {
        bail!("oracle rejected output: {}", a.output.display());
    }

    println!("input_wasm_path: {}", a.input.display());
    println!("output_wasm_path: {}", a.output.display());
    println!("oracle_path: {}", a.oracle.display());
    if cmd == "func-level" {
        println!("to_test_func_name: {to_test_func_name}");
        println!(
            "func_level_cfg: disable_callsite={} callsite_as_unreachable={}",
            a.disable_callsite, a.callsite_as_unreachable
        );
    }
    if cmd == "instseq" {
        println!("to_test_func_name: {to_test_func_name}");
        println!("stage: {:?}", a.stage);
    }
    println!("result_dir: {}", a.result_dir.display());
    println!("timeout: {}", a.timeout);
    println!(
        "exec_result: status={:?} taken={:.3}s reduced_size_num={:?} reduced_inst_num={:?} is_partial_by_timeout={}",
        result.exec_status, result.exec_taken_time, result.reduced_size_num, result.reduced_inst_num,
        result.is_partial_by_timeout,
    );
    println!("time_cost: {taken:.3}s");
    Ok(ExitCode::from(if result.exec_status == ExecStatus::Success { 0 } else { 1 }))
}

fn main() -> Result<ExitCode> {
    let args: Vec<String> = std::env::args().skip(1).collect();
    let Some(cmd) = args.first().cloned() else {
        bail!("usage: e2wr <uur|final-polish|func-level|instseq|full> --input I --output O --oracle P --result-dir D [--timeout S] [--debug]");
    };
    run(&cmd, &args[1..])
}

// ---------------------------------------------------------------------------
// The full subcommand (M12): matches the effective parameter surface of script_run_reduce_v2.py.
// ---------------------------------------------------------------------------

struct FullArgs {
    input: PathBuf,
    output: PathBuf,
    oracle: PathBuf,
    to_test_func_name: String,
    work_dir: PathBuf,
    time_limit: i64,
    init_p0: f64,
    seed: i64,
    debug: bool,
    full_remove_uur: bool,
    full_wo_update_probdd_p0: bool,
    full_wo_final_polish: bool,
    disable_whole_dd: bool,
    disable_polish_return: bool,
    disable_inline: bool,
    disable_size_polish: bool,
    force_inst_mutation: OnlyOneInstTask,
}

impl Default for FullArgs {
    fn default() -> FullArgs {
        FullArgs {
            input: PathBuf::new(),
            output: PathBuf::new(),
            oracle: PathBuf::new(),
            to_test_func_name: String::new(),
            work_dir: PathBuf::from("work_dir"),
            time_limit: 9000,
            init_p0: 0.1,
            seed: 42,
            debug: false,
            full_remove_uur: false,
            full_wo_update_probdd_p0: false,
            full_wo_final_polish: false,
            disable_whole_dd: false,
            disable_polish_return: false,
            disable_inline: false,
            disable_size_polish: false,
            force_inst_mutation: OnlyOneInstTask::Disable,
        }
    }
}

fn parse_full_args(args: &[String]) -> Result<FullArgs> {
    let mut a = FullArgs::default();
    let mut i = 0;
    while i < args.len() {
        let arg = args[i].as_str();
        let take = |i: &mut usize, arg: &str| -> Result<String> {
            *i += 1;
            Ok(args
                .get(*i)
                .with_context(|| format!("missing value for {arg}"))?
                .clone())
        };
        match arg {
            "--input" | "-i" => a.input = PathBuf::from(take(&mut i, arg)?),
            "--output" | "-o" => a.output = PathBuf::from(take(&mut i, arg)?),
            "--oracle" => a.oracle = PathBuf::from(take(&mut i, arg)?),
            "--to-test-func-name" | "-tn" => {
                a.to_test_func_name = take(&mut i, arg)?;
            }
            "--work-dir" | "-w" => a.work_dir = PathBuf::from(take(&mut i, arg)?),
            "--time-limit" | "-t" => {
                a.time_limit = take(&mut i, arg)?.parse().context("invalid --time-limit")?;
            }
            "--init_p0" => {
                a.init_p0 = take(&mut i, arg)?.parse().context("invalid --init_p0")?;
            }
            "--seed" => a.seed = take(&mut i, arg)?.parse().context("invalid --seed")?,
            "--debug" | "-d" => a.debug = true,
            "--full_remove_uur" => a.full_remove_uur = true,
            "--full_wo_update_probdd_p0" => a.full_wo_update_probdd_p0 = true,
            "--full_wo_final_polish" => a.full_wo_final_polish = true,
            "--disable_whole_dd" => a.disable_whole_dd = true,
            "--disable_polish_return" => a.disable_polish_return = true,
            "--disable_inline" => a.disable_inline = true,
            "--disable_size_polish" => a.disable_size_polish = true,
            "--force_inst_mutation" => {
                let v = take(&mut i, arg)?;
                a.force_inst_mutation = match v.to_lowercase().as_str() {
                    "disable" => OnlyOneInstTask::Disable,
                    "p3" => OnlyOneInstTask::P3,
                    "core" => OnlyOneInstTask::Core,
                    "rev" => OnlyOneInstTask::Rev,
                    other => bail!("invalid --force_inst_mutation {other} (disable|p3|core|rev)"),
                };
            }
            other => bail!("unknown argument {other}"),
        }
        i += 1;
    }
    if a.input.as_os_str().is_empty() {
        bail!("--input is required");
    }
    if a.output.as_os_str().is_empty() {
        bail!("--output is required");
    }
    if a.oracle.as_os_str().is_empty() {
        bail!("--oracle is required");
    }
    if a.to_test_func_name.is_empty() {
        bail!("--to-test-func-name is required");
    }
    Ok(a)
}

/// Full-pipeline orchestration of script_run_reduce_v2.py main() (Python does no input-oracle
/// pre-check; --debug is not passed to the pass — Python's FrameWorkReducerCfg.default_cfg
/// hardcodes debug=False).
fn run_full(args: &[String]) -> Result<ExitCode> {
    let a = parse_full_args(args)?;
    println!("Random seed set: {}", a.seed);
    for (name, p) in [("input", &a.input), ("oracle script", &a.oracle)] {
        if !p.exists() {
            println!("Error: {name} file {} does not exist", p.display());
            return Ok(ExitCode::from(1));
        }
    }
    println!("V6 cfg applied: enable_whole_dd={}", !a.disable_whole_dd);

    let oracle = Oracle::new(a.oracle.to_string_lossy().as_ref());
    let cfg = SchedulerConfig {
        to_test_func_name: Some(a.to_test_func_name.clone()),
        use_uur: !a.full_remove_uur,
        use_final_polish: !a.full_wo_final_polish,
        dd_p0_pred: !a.full_wo_update_probdd_p0,
        dd_initial_p: a.init_p0,
        final_polish: FinalPolishConfig {
            polish_return_type: !a.disable_polish_return,
            enable_inline: !a.disable_inline,
            enable_size_polish: !a.disable_size_polish,
        },
        force_inst_mutation: a.force_inst_mutation,
        v6_whole_dd: !a.disable_whole_dd,
        debug: a.debug,
    };
    let pass_names: Vec<&str> = [
        Some("NodeShrink"),
        cfg.use_uur.then_some("UnusedDefReducer"),
        cfg.use_final_polish.then_some("FinalPolishPass"),
    ]
    .into_iter()
    .flatten()
    .collect();
    println!("Loaded {} passes in total", pass_names.len());
    println!("\n==== Wasm Reducer Configuration ====");
    println!("Input file: {}", a.input.display());
    println!("Output file: {}", a.output.display());
    println!("Oracle script: {}", a.oracle.display());
    println!("Working directory: {}", a.work_dir.display());
    println!("Time limit: {} seconds", a.time_limit);
    println!("Random seed: {}", a.seed);
    println!("passes: {pass_names:?}");
    println!("==========================\n");

    println!("\nStarting Wasm file reduction ...");
    let mut scheduler = Scheduler::new(
        &oracle,
        &a.input,
        &a.output,
        &a.work_dir,
        Some(a.time_limit.max(0) as u64),
        &cfg,
    )?;
    scheduler.run()?;
    println!("\n==== Reduction Completed ====");
    Ok(ExitCode::SUCCESS)
}
