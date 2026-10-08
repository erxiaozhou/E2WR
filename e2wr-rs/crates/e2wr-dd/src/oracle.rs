//! Oracle runner with timeout control (M1.1).
//!
//! Mirrors Python: `run_with_timeout` from `timeout_process.py` and
//! `pass_script_oracle`/`Oracle` from `reduction_analysis/ReductionDescUtil/Oracle.py`.
//!
//! Behavior notes (aligned with actual Python behavior):
//! - Runs `<oracle script> <target wasm path>` via a shell (`/bin/sh -c "..."`);
//! - Timeout defaults to 30 s (`ORACLE_TIMEOUT`); on expiry sends SIGTERM to the whole process group,
//!   then SIGKILL after a 2 s grace period if still alive;
//! - Verdict: exit code 0 = interesting; timeout or nonzero = not interesting.

use std::io::Read;
use std::path::Path;
use std::process::{Child, Command, Stdio};
use std::time::{Duration, Instant};

/// Python `ReducerCommonConfig.ORACLE_TIMEOUT`, in seconds.
pub const ORACLE_TIMEOUT: u64 = 30;

const SIGTERM: i32 = 15;
const SIGKILL: i32 = 9;

/// Return value of `run_with_timeout` (stdout/stderr are collected but unused).
pub struct RunOutcome {
    pub stdout: Vec<u8>,
    pub stderr: Vec<u8>,
    pub returncode: Option<i32>,
    pub timeout_occurred: bool,
    pub execution_time: f64,
}

fn kill_process_group(child: &Child, sig: i32) {
    #[cfg(unix)]
    unsafe {
        // process_group(0) was set on spawn, so the child pid is the process group id.
        libc::killpg(child.id() as i32, sig);
    }
    #[cfg(not(unix))]
    {
        let _ = (child, sig);
    }
}

fn spawn_reader<R: Read + Send + 'static>(mut pipe: R) -> std::thread::JoinHandle<Vec<u8>> {
    std::thread::spawn(move || {
        let mut buf = Vec::new();
        let _ = pipe.read_to_end(&mut buf);
        buf
    })
}

/// Runs a command via a shell with a timeout (Python `run_with_timeout`; always process-group mode).
pub fn run_with_timeout(cmd: &str, timeout: u64) -> anyhow::Result<RunOutcome> {
    let start = Instant::now();
    let mut child = shell_command(cmd)
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .spawn()?;
    let stdout_handle = spawn_reader(child.stdout.take().expect("stdout piped"));
    let stderr_handle = spawn_reader(child.stderr.take().expect("stderr piped"));

    let deadline = start + Duration::from_secs(timeout);
    let mut timeout_occurred = false;
    let mut grace_deadline: Option<Instant> = None;
    loop {
        if child.try_wait()?.is_some() {
            break;
        }
        let now = Instant::now();
        if !timeout_occurred {
            if now >= deadline {
                timeout_occurred = true;
                kill_process_group(&child, SIGTERM);
                grace_deadline = Some(now + Duration::from_secs(2));
            }
        } else if let Some(g) = grace_deadline {
            if now >= g {
                kill_process_group(&child, SIGKILL);
                break;
            }
        }
        std::thread::sleep(Duration::from_millis(10));
    }
    let status = child.wait()?;
    let stdout = stdout_handle.join().map_err(|_| anyhow::anyhow!("stdout reader panicked"))?;
    let stderr = stderr_handle.join().map_err(|_| anyhow::anyhow!("stderr reader panicked"))?;
    Ok(RunOutcome {
        stdout,
        stderr,
        returncode: status.code(),
        timeout_occurred,
        execution_time: start.elapsed().as_secs_f64(),
    })
}

#[cfg(unix)]
fn shell_command(cmd: &str) -> Command {
    use std::os::unix::process::CommandExt;
    let mut c = Command::new("/bin/sh");
    c.arg("-c").arg(cmd);
    // Equivalent of Python's os.setsid (preexec_fn): the whole group is killed on timeout.
    c.process_group(0);
    c
}

#[cfg(not(unix))]
fn shell_command(cmd: &str) -> Command {
    let mut c = Command::new("sh");
    c.arg("-c").arg(cmd);
    c
}

/// Mirrors Python `pass_script_oracle(cmd_or_oracle, target_path, timeout)`.
pub fn pass_script_oracle(cmd_or_oracle: &str, target_path: &Path, timeout: u64) -> anyhow::Result<bool> {
    assert!(
        target_path.exists(),
        "The target_path {} does not exist",
        target_path.display()
    );
    let cmd = format!("{} {}", cmd_or_oracle, target_path.display());
    let result = run_with_timeout(&cmd, timeout)?;
    if result.timeout_occurred {
        return Ok(false);
    }
    Ok(result.returncode == Some(0))
}

/// Oracle-script runner (Python class `Oracle`).
#[derive(Clone)]
pub struct Oracle {
    oracle_path: String,
    default_timeout: u64,
}

impl Oracle {
    pub fn new(oracle_path: &str) -> Self {
        Self {
            oracle_path: oracle_path.to_string(),
            default_timeout: ORACLE_TIMEOUT,
        }
    }

    pub fn with_timeout(oracle_path: &str, timeout: u64) -> Self {
        Self {
            oracle_path: oracle_path.to_string(),
            default_timeout: timeout,
        }
    }

    /// Checks whether the target wasm still keeps the property of interest (`Oracle.__call__`).
    pub fn check(&self, target_path: &Path) -> anyhow::Result<bool> {
        pass_script_oracle(&self.oracle_path, target_path, self.default_timeout)
    }
}
