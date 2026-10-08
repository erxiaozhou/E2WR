//! Shared pass types: execution results and directory conventions.
//!
//! Mirrors ExecResult/ExecStatus of `ReducerPassUtil/ReduceResult.py` and
//! `ReductionDescUtil/OneReducerDirSystem.py` (the minimal subset needed for reduction; P-8:
//! tmp_dir becomes a required field, eliminating the None branch).

use std::collections::HashMap;
use std::hash::Hash;
use std::path::{Path, PathBuf};
use std::time::SystemTime;

use anyhow::{Context as _, Result};
use e2wr_dd::probdd::ProbDD;

/// Execution status (Python `ExecStatus`).
/// Python's `TIMEOUT` member is not ported: the E2WR Python snapshot constructs none either
/// (only the upstream CommandReducePass does, outside the porting scope; R-6
/// pre-check 2026-09-28).
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum ExecStatus {
    Success,
    ExecFailed,
}

/// One pass's execution result (Python `ExecResult`).
/// reduced_size_num's meaning varies per pass: UnusedDefReducer = the number of definitions deleted
/// (P-7); FinalPolishPass / NodeShrinkPass = the byte-size delta (the callsite replacement's stack-padding
/// sequence can grow the file, making it negative, e.g. +831 bytes measured for p10_0 function-level preprocessing).
#[derive(Debug, Clone)]
pub struct ExecResult {
    pub exec_status: ExecStatus,
    pub exec_taken_time: f64,
    pub reduced_size_num: Option<i64>,
    pub reduced_inst_num: Option<u64>,
    pub is_partial_by_timeout: bool,
}

impl ExecResult {
    /// Python `ExecResult.is_size_effective`: either reduction amount > 0.
    pub fn is_size_effective(&self) -> bool {
        self.reduced_size_num.is_some_and(|v| v > 0)
            || self.reduced_inst_num.is_some_and(|v| v > 0)
    }

    /// Python `ExecResult.is_successful_exec`.
    pub fn is_successful_exec(&self) -> bool {
        self.exec_status == ExecStatus::Success
    }
}

/// Reduction working-directory conventions (the minimal subset of OneReducerDirSystem).
#[derive(Debug, Clone)]
pub struct DirSystem {
    pub tmp_dir: PathBuf,
    pub tmp_used_path: PathBuf,
}

impl DirSystem {
    /// Mirrors `OneReducerDirSystem(tmp_dir=result_dir, tmp_used_path=result_dir/'tmp_used.wasm')`
    /// (the construction form of script_run_nodeshrink.py --use_uur).
    pub fn new(result_dir: &Path) -> Self {
        std::fs::create_dir_all(result_dir).expect("create result dir");
        DirSystem {
            tmp_used_path: result_dir.join("tmp_used.wasm"),
            tmp_dir: result_dir.to_path_buf(),
        }
    }
}

/// R-19: the shared ProbDD error-capturing helper (previously seven identical copies across cf_elem / core_stage / p3 / uur /
/// func_level / final_polish: "the test closure records an error and returns false →
/// dd.reduce → re-raise afterwards"). The test closure now returns `Result<bool>`:
/// - Err is recorded at the adapter layer and treated as false, then re-raised as-is after reduce (overwrite semantics,
///   the last error wins — same as the original boilerplate);
/// - sites with first-error short-circuit/timeout pre-checks keep them inside the closure body (returning `Ok(false)` means
///   "treat as not passed, record no error"; the first error is preserved by the short-circuit flag before later closure calls);
/// - sites whose re-raise wording differs append context to the Err at the call site (verbatim-equivalent).
///
/// func_level's reduce_ext/TestOutcome three-value form (early stop) has a different shape and is out of scope.
pub(crate) fn run_probdd_capturing<T>(
    dd: &mut ProbDD<T>,
    config: &[T],
    weights: Option<&HashMap<T, f64>>,
    expected_end_time: Option<SystemTime>,
    mut try_test: impl FnMut(&[T]) -> Result<bool>,
) -> Result<Vec<T>>
where
    T: Clone + Eq + Hash + Ord,
{
    let mut test_err: Option<anyhow::Error> = None;
    let keep = dd.reduce(
        config,
        weights,
        expected_end_time,
        &mut |to_save: &[T]| -> bool {
            match try_test(to_save) {
                Ok(v) => v,
                Err(e) => {
                    test_err = Some(e);
                    false
                }
            }
        },
    );
    match test_err {
        Some(e) => Err(e),
        None => Ok(keep),
    }
}

// ---------------------------------------------------------------------------
// D-14: the artifact-validity assertion in debug mode (user ruling 2026-09-29: "port it")
// ---------------------------------------------------------------------------

/// The debug validation strategy, mirroring Python's `if DEBUG: assert validate_wasm(...)`
/// blocks and their two error-exemption families. Python validates via a wasm-validate subprocess (wabt);
/// Rust validates in place with the wasmparser `Validator` (equivalent judgment surface, D-12);
/// error texts differ in wording, so exemption matching is done against the wasmparser
/// wording:
/// - Python `'is not declared in any elem sections'`
///   (the exemptions at UnusedDefReducer.py:928 and FinalPolishPass._mutate_and_save:441)
///   ↔ wasmparser `"undeclared function reference"` (0.240's
///   validator/operators.rs:3061);
/// - Python `'type mismatch in initializer expression'`
///   (the exemption at FuncNodeRemover.py:165) ↔ wasmparser has no initializer-context
///   marker, degrading to "text contains type mismatch and the error offset falls inside the Global/Elem section
///   byte ranges" (those two sections' constant expressions are the Python "initializer
///   expression"; the exemption surface matches Python).
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub(crate) enum DebugValidate {
    /// Non-debug: no validation (production path, behavior unchanged).
    Off,
    /// Debug: validate; invalid errors out (most assertion sites have no exemption).
    Strict,
    /// Debug: error out after exempting the elem undeclared-reference class.
    ExemptElem,
    /// Debug: error out after exempting initializer type mismatches.
    ExemptInitExpr,
}

impl DebugValidate {
    /// debug bool → Strict/Off.
    pub(crate) fn strict(debug: bool) -> Self {
        if debug { DebugValidate::Strict } else { DebugValidate::Off }
    }

    /// debug bool → ExemptElem/Off.
    pub(crate) fn exempt_elem(debug: bool) -> Self {
        if debug { DebugValidate::ExemptElem } else { DebugValidate::Off }
    }

    /// debug bool → ExemptInitExpr/Off.
    pub(crate) fn exempt_init_expr(debug: bool) -> Self {
        if debug { DebugValidate::ExemptInitExpr } else { DebugValidate::Off }
    }

    /// Whether in debug mode (all three validation variants imply debug=true; Off implies
    /// debug=false). Lets log switches derive from this strategy.
    pub(crate) fn is_debug(self) -> bool {
        !matches!(self, DebugValidate::Off)
    }
}

/// In debug mode, validate the written artifact and either exempt or error per the strategy (mirroring Python's
/// `debug_validate_tmp` / inline DEBUG assertion blocks). Non-debug returns Ok directly.
pub(crate) fn debug_validate_wasm_file(mode: DebugValidate, path: &Path) -> Result<()> {
    if mode == DebugValidate::Off {
        return Ok(());
    }
    let bytes =
        std::fs::read(path).with_context(|| format!("read {}", path.display()))?;
    let err = match wasmparser::Validator::new().validate_all(&bytes) {
        Ok(_) => return Ok(()),
        Err(e) => e,
    };
    let text = err.to_string();
    let exempted = match mode {
        DebugValidate::ExemptElem => text.contains("undeclared function reference"),
        DebugValidate::ExemptInitExpr => {
            text.contains("type mismatch") && offset_in_init_section(err.offset(), &bytes)
        }
        _ => false,
    };
    if exempted {
        return Ok(());
    }
    anyhow::bail!("{} is invalid wasm: {}", path.display(), text);
}

/// Whether the error offset falls inside the Global (section id 6) or Elem (section id 9) byte range.
/// Hand-rolled section-header scan (section id + LEB128 size, the same scan style as e2wr-ir's
/// scan_section_headers, without exposing its internals).
fn offset_in_init_section(offset: usize, bytes: &[u8]) -> bool {
    if bytes.len() < 8 {
        return false;
    }
    let mut pos = 8usize; // magic + version
    while pos < bytes.len() {
        let section_id = bytes[pos];
        pos += 1;
        if section_id == 0 {
            // A custom section's name is also LEB length-prefixed; skipped the same way (by section size).
        }
        let Some((size, next)) = read_leb_u32_max5(bytes, pos) else {
            return false;
        };
        let body = pos + (next - pos);
        let end = body + size as usize;
        if (section_id == 6 || section_id == 9) && offset >= body && offset < end {
            return true;
        }
        if end <= pos {
            return false; // defensive: zero/looping size
        }
        pos = end;
    }
    false
}

/// u32 LEB128 reading with a 5-byte cap (encoder output is always ≤5 bytes; mirrors
/// e2wr-ir snapshot.rs read_leb_u32, reimplemented locally to avoid widening the public surface).
fn read_leb_u32_max5(bytes: &[u8], pos: usize) -> Option<(u32, usize)> {
    let mut result: u32 = 0;
    let mut shift = 0u32;
    for i in 0..5 {
        let b = *bytes.get(pos + i)?;
        result |= ((b & 0x7f) as u32) << shift;
        if b & 0x80 == 0 {
            return Some((result, pos + i + 1));
        }
        shift += 7;
    }
    None
}

#[cfg(test)]
mod debug_validate_tests {
    use super::*;

    fn write_tmp(name: &str, bytes: &[u8]) -> std::path::PathBuf {
        let p = std::env::temp_dir().join(format!("e2wr_dv_test_{name}.wasm"));
        std::fs::write(&p, bytes).unwrap();
        p
    }

    /// The minimal valid module (magic + version; an empty section-10 empty vec would also do;
    /// here a "module" of only magic+version, which wasmparser accepts as a valid empty module).
    #[test]
    fn valid_and_invalid_file() {
        let good = write_tmp("good", b"\0asm\x01\0\0\0");
        assert!(debug_validate_wasm_file(DebugValidate::Strict, &good).is_ok());
        // Invalid: corrupted version.
        let bad = write_tmp("bad", b"\0asm\x02\0\0\0");
        assert!(debug_validate_wasm_file(DebugValidate::Strict, &bad).is_err());
        // Non-debug performs no validation (no error even if the file is missing — it is never read).
        assert!(debug_validate_wasm_file(
            DebugValidate::Off,
            std::path::Path::new("/nonexistent.wasm")
        )
        .is_ok());
    }

    /// The elem undeclared-reference exemption: function 1's body has `ref.func 0`, and function 0 is not
    /// declared by any element segment — wasmparser reports "undeclared function reference"
    /// (the same spec error as the Python wabt text 'is not declared in any elem
    /// sections'); ExemptElem lets it pass, Strict errors.
    #[test]
    fn exempt_elem_undeclared_reference() {
        let mut m = b"\0asm\x01\0\0\0".to_vec();
        // type (()->()); func ×2; code: f0=end, f1=ref.func 0; drop; end.
        m.extend_from_slice(&[0x01, 0x04, 0x01, 0x60, 0x00, 0x00]);
        m.extend_from_slice(&[0x03, 0x03, 0x02, 0x00, 0x00]);
        m.extend_from_slice(&[
            0x0a, 0x0a, 0x02, 0x02, 0x00, 0x0b, 0x05, 0x00, 0xd2, 0x00, 0x1a,
            0x0b,
        ]);
        let p = write_tmp("elem", &m);
        assert!(debug_validate_wasm_file(DebugValidate::Strict, &p).is_err());
        assert!(debug_validate_wasm_file(DebugValidate::ExemptElem, &p).is_ok());
    }

    /// The initializer type-mismatch exemption: a global declares i32 but initializes with i64.const —
    /// the error offset falls inside the Global section; ExemptInitExpr lets it pass; Strict errors.
    #[test]
    fn exempt_init_expr_type_mismatch() {
        let mut m = b"\0asm\x01\0\0\0".to_vec();
        let body = [0x01u8, 0x7f, 0x00, 0x42, 0x00, 0x0b]; // i32 global, i64.const 0, end
        m.push(6);
        m.push(body.len() as u8);
        m.extend_from_slice(&body);
        let p = write_tmp("init", &m);
        assert!(debug_validate_wasm_file(DebugValidate::Strict, &p).is_err());
        assert!(debug_validate_wasm_file(DebugValidate::ExemptInitExpr, &p).is_ok());
    }
}
