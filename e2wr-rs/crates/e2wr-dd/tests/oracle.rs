use std::fs;
use std::path::PathBuf;
use std::time::{Duration, Instant};

use e2wr_dd::oracle::{run_with_timeout, Oracle, ORACLE_TIMEOUT};

fn test_dir() -> PathBuf {
    let dir = std::env::temp_dir().join(format!("e2wr-dd-oracle-test-{}", std::process::id()));
    fs::create_dir_all(&dir).unwrap();
    dir
}

/// Writes a fake oracle script (0o755) and a placeholder target file, returning (script path, target path).
fn write_script(name: &str, body: &str) -> (PathBuf, PathBuf) {
    let dir = test_dir();
    let script = dir.join(name);
    fs::write(&script, body).unwrap();
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        fs::set_permissions(&script, fs::Permissions::from_mode(0o755)).unwrap();
    }
    let target = dir.join("target.wasm");
    fs::write(&target, b"\0asm").unwrap();
    (script, target)
}

fn pid_alive(pid: i32) -> bool {
    #[cfg(unix)]
    {
        unsafe { libc::kill(pid, 0) == 0 }
    }
    #[cfg(not(unix))]
    {
        let _ = pid;
        true
    }
}

#[test]
fn oracle_exit_zero_is_valid() {
    let (script, target) = write_script("exit0.sh", "#!/bin/sh\nexit 0\n");
    let oracle = Oracle::new(script.to_str().unwrap());
    assert!(oracle.check(&target).unwrap());
}

#[test]
fn oracle_exit_nonzero_is_invalid() {
    let (script, target) = write_script("exit1.sh", "#!/bin/sh\nexit 3\n");
    let oracle = Oracle::new(script.to_str().unwrap());
    assert!(!oracle.check(&target).unwrap());
}

#[test]
fn oracle_missing_target_asserts() {
    let (script, _) = write_script("exit0b.sh", "#!/bin/sh\nexit 0\n");
    let oracle = Oracle::new(script.to_str().unwrap());
    let missing = test_dir().join("no_such_target.wasm");
    let result = std::panic::catch_unwind(std::panic::AssertUnwindSafe(|| {
        let _ = oracle.check(&missing);
    }));
    assert!(result.is_err(), "missing target should assert like Python");
}

#[test]
fn oracle_timeout_is_invalid_and_process_group_killed() {
    let dir = test_dir();
    let pid_file = dir.join("timeout_pid.txt");
    let _ = fs::remove_file(&pid_file);
    let body = format!("#!/bin/sh\necho $$ > {}\nsleep 300 &\nsleep 300\n", pid_file.display());
    let (script, target) = write_script("sleep.sh", &body);
    let oracle = Oracle::with_timeout(script.to_str().unwrap(), 1);
    let start = Instant::now();
    let valid = oracle.check(&target).unwrap();
    let elapsed = start.elapsed();
    assert!(!valid, "timeout must count as invalid");
    assert!(
        elapsed < Duration::from_secs(5),
        "should return shortly after timeout, took {:?}",
        elapsed
    );
    // The process group should be reaped by SIGTERM/SIGKILL: the script's main process must be gone.
    let pid_txt = fs::read_to_string(&pid_file).unwrap();
    let pid: i32 = pid_txt.trim().parse().unwrap();
    let deadline = Instant::now() + Duration::from_secs(5);
    while pid_alive(pid) && Instant::now() < deadline {
        std::thread::sleep(Duration::from_millis(50));
    }
    assert!(!pid_alive(pid), "process group should be reaped");
}

#[test]
fn default_timeout_is_30s() {
    assert_eq!(ORACLE_TIMEOUT, 30);
}

#[test]
fn run_with_timeout_reports_zero_returncode() {
    let out = run_with_timeout("exit 0", 10).unwrap();
    assert!(!out.timeout_occurred);
    assert_eq!(out.returncode, Some(0));
}

#[test]
fn run_with_timeout_reports_nonzero_returncode() {
    let out = run_with_timeout("exit 7", 10).unwrap();
    assert!(!out.timeout_occurred);
    assert_eq!(out.returncode, Some(7));
}
