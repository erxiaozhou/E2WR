//! M7 instrumentation probe mechanism (only the EXECUTED probe is replicated).
//!
//! Mirrors Python `reduction_analysis/Instrumentation/`:
//! `ValueProbeInstrument.py` (ValueProbeManager.instrument_multiple_places_
//! and_get_result / instrument_and_get_exec_freq), `CodeProbeUtil.py`
//! (locate_imported_fd_write / exec_and_get_trace), `ParseDumpData.py`
//! (dual parsers), `ProbeType.py` (event header codec), `ValueProbeUtil.py` and
//! `NonRefStackProbe.py` (helper function bodies and probe instruction sequences).
//!
//! Scope ruling (D-10, user-confirmed): the VP stack-value collection chain and
//! LOCAL/GLOBAL probes are not replicated; `ProbeDesc` and the event model shrank accordingly;
//! `CanControlData` degenerates to a known-probe-id set.
//!
//! Behavior notes (P-25):
//! - an unexported or parameter-taking to_test function → print a skip message and return an empty result (the upstream treats everything as
//!   executed);
//! - the dual parsers keep their distinct semantics: the general one (callsite replacement path) on a marker byte + a non-EXECUTED type with a
//!   known id **aborts and returns partial results**; the executed-only one (frequency/indirect-call paths) **skips and keeps scanning** on the same input;
//! - no 4-byte alignment or contiguity is assumed for events; a 0xF0 happening to appear in the program's own output is skipped via the
//!   valid-header + known-id check;
//! - the probe memory base reads memory 0's min pages; an imported-memory module errors out directly;
//! - the grow-by-one-page-if-memory-exists branch never ran in Python (need_memory always False,
//!   removed per B-4) and is not implemented.
//!   (removed per B-4); not implemented here.

use std::collections::{BTreeMap, BTreeSet, HashSet};
use std::path::{Path, PathBuf};

use anyhow::{bail, Context, Result};

use e2wr_ir::ast::NodeLoc;
use e2wr_ir::module::{
    Export, ExportDesc, FuncType, Global, GlobalType, Import, ImportDesc, Limits, MemType, Memory,
    Module,
};
use e2wr_ir::mutation::{self, DefEdit, Mutation, RawDef};
use e2wr_ir::{Inst, Snapshot};
use wasmparser::{BlockType, MemArg, ValType};

/// Instrumentation execution budget, in seconds. Two same-valued Python constants merged (R-12):
/// `ReducerCommonConfig.VP_INSTRUMENTATION_TIMEOUT` and
/// `CALLSITE_REDUCTION_INSTRUMENT_TIMEOUT` (both 45).
pub const INSTRUMENTATION_TIMEOUT: u64 = 45;

// ---------------------------------------------------------------------------
// Event header codec (the PackedProbeHeaderCodec of Python `ProbeType.py`).
// Layout (from the u32 low end): low 8 bits marker 0xF0 | 2-bit type | 22-bit id.
// ---------------------------------------------------------------------------

pub const MARKER_BYTE: u8 = 0xF0;
pub const MARKER_BITS: u32 = 8;
pub const TYPE_BITS: u32 = 2;
pub const IDX_BITS: u32 = 32 - MARKER_BITS - TYPE_BITS;
pub const IDX_MASK: u32 = (1 << IDX_BITS) - 1;
pub const TYPE_MASK: u32 = (1 << TYPE_BITS) - 1;

/// Probe types (the STACK value kept to replicate the general parser's abort semantics for non-EXECUTED headers;
/// LOCAL/GLOBAL were removed with B-4; decoding those values is invalid).
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum ProbeType {
    Stack,
    Executed,
}

pub fn encode_probe_type(ty: ProbeType) -> u32 {
    match ty {
        ProbeType::Stack => 0,
        ProbeType::Executed => 3,
    }
}

/// Mirrors Python `decode_probe_type`: an invalid value returns None (Python raises ValueError).
pub fn decode_probe_type(value: u32) -> Option<ProbeType> {
    match value {
        0 => Some(ProbeType::Stack),
        3 => Some(ProbeType::Executed),
        _ => None,
    }
}

pub fn pack_probe_header(probe_type_value: u32, probe_idx: u32) -> u32 {
    assert!(probe_type_value <= TYPE_MASK);
    assert!(probe_idx <= IDX_MASK);
    MARKER_BYTE as u32 | (probe_type_value << MARKER_BITS) | (probe_idx << (MARKER_BITS + TYPE_BITS))
}

/// A marker-byte mismatch returns None (Python raises ValueError).
pub fn unpack_probe_header(packed: u32) -> Option<(u32, u32)> {
    let packed_u32 = packed;
    if packed_u32 & 0xFF != MARKER_BYTE as u32 {
        return None;
    }
    let ty = (packed_u32 >> MARKER_BITS) & TYPE_MASK;
    let idx = (packed_u32 >> (MARKER_BITS + TYPE_BITS)) & IDX_MASK;
    Some((ty, idx))
}

/// A probe event (EXECUTED; Python `OneDumpData` shrunk to just the id).
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct ProbeEvent {
    pub probe_idx: u32,
}

// ---------------------------------------------------------------------------
// Trace parsing (Python `ParseDumpData.py`).
// ---------------------------------------------------------------------------

fn find_marker(raw: &[u8], from: usize) -> Option<usize> {
    raw[from..].iter().position(|&b| b == MARKER_BYTE).map(|i| i + from)
}

/// The executed-only parser (Python `get_all_raw_bytes_and_then_parse_executed_only`):
/// an invalid header or unknown id skips that byte and keeps scanning.
pub fn parse_executed_only(raw: &[u8], known: &HashSet<u32>) -> Vec<ProbeEvent> {
    let n = raw.len();
    let mut out = Vec::new();
    let mut pos = 0usize;
    while let Some(p) = find_marker(raw, pos) {
        if p + 4 > n {
            break;
        }
        let packed = u32::from_le_bytes(raw[p..p + 4].try_into().unwrap());
        let ty = (packed >> MARKER_BITS) & TYPE_MASK;
        if ty != encode_probe_type(ProbeType::Executed) {
            pos = p + 1;
            continue;
        }
        let idx = (packed >> (MARKER_BITS + TYPE_BITS)) & IDX_MASK;
        if !known.contains(&idx) {
            pos = p + 1;
            continue;
        }
        out.push(ProbeEvent { probe_idx: idx });
        pos = p + 4;
    }
    out
}

/// The general parser (Python `get_all_raw_bytes_and_then_parse`, the callsite replacement path):
/// a type-bit decode failure or a known-id non-EXECUTED header aborts parsing and returns the parsed prefix
/// (Python returns partial results via except Exception, P-25).
pub fn parse_generic(raw: &[u8], known: &HashSet<u32>) -> Vec<ProbeEvent> {
    let n = raw.len();
    let mut out = Vec::new();
    let mut pos = 0usize;
    while let Some(p) = find_marker(raw, pos) {
        if p + 4 > n {
            break;
        }
        let packed = u32::from_le_bytes(raw[p..p + 4].try_into().unwrap());
        // extract_head_info: an undecodable type value → IllFormedDataException → partial results.
        let Some((ty, idx)) = unpack_probe_header(packed) else {
            return out;
        };
        if decode_probe_type(ty).is_none() {
            return out;
        }
        // Unknown id → treated as a false match; skip the byte and continue.
        if !known.contains(&idx) {
            pos = p + 1;
            continue;
        }
        // _get_types_by_probe_type supports EXECUTED only (a STACK header → abort by exception).
        if ty != encode_probe_type(ProbeType::Executed) {
            return out;
        }
        out.push(ProbeEvent { probe_idx: idx });
        pos = p + 4;
    }
    out
}

// ---------------------------------------------------------------------------
// Probe descriptions and buffer configuration.
// ---------------------------------------------------------------------------

/// Probe position description (Python `ProbeDesc` shrunk: EXECUTED only).
#[derive(Debug, Clone, Copy, PartialEq, Eq, PartialOrd, Ord)]
pub struct ProbeDesc {
    pub idx: u32,
    pub loc: NodeLoc,
}

/// Instrumentation buffer layout (Python `BufferConfig`; dead fields removed with B-4).
#[derive(Debug, Clone, Copy)]
pub struct BufferConfig {
    pub ori_global_num: u32,
    pub max_new_global_num: u32,
    pub probe_memory_start_idx: u32,
    pub save_memory_global_start_idx: u32,
    pub probe_output_counter_start_idx: u32,
}

impl BufferConfig {
    pub fn new(ori_global_num: u32, max_new_global_num: u32, memory_start_idx: u32, counter_mem_start_idx: u32) -> Self {
        BufferConfig {
            ori_global_num,
            max_new_global_num,
            probe_memory_start_idx: memory_start_idx + 12,
            save_memory_global_start_idx: ori_global_num + 4,
            probe_output_counter_start_idx: memory_start_idx + counter_mem_start_idx,
        }
    }
}

/// Helper function indices (the live fields of Python `ProbeUtilWasmFuncManager`).
#[derive(Debug, Clone, Copy)]
pub struct ProbeFuncs {
    pub print_core_probe_func_idx: u32,
    pub print_core_probe_type_idx: u32,
    pub noop_print_core_probe_func_idx: u32,
    pub store_i32_func_idx: u32,
}

// ---------------------------------------------------------------------------
// Helper function bodies (Python `ValueProbeUtil.py`).
// ---------------------------------------------------------------------------

fn mem_arg(align: u8) -> MemArg {
    MemArg { align, max_align: align, offset: 0, memory: 0 }
}

const fn i32c(v: i32) -> Inst {
    Inst::I32Const { value: v }
}

/// print_core: writes the parameter-given number of bytes from probe memory to stdout (via fd_write).
/// Invades memory [m-12, m+8), three i32 temporaries in globals g_base..g_base+2, restored after use.
fn build_print_core_func(fd_write_id: u32, g_base: u32, memory_start_idx: u32) -> Func {
    assert!(memory_start_idx >= 12);
    let result_offset = memory_start_idx - 12;
    let para_start_offset = memory_start_idx - 8;
    let para_len_offset = memory_start_idx - 4;
    use Inst::*;
    let insts = vec![
        i32c(result_offset as i32),
        I32Load { memarg: mem_arg(2) },
        GlobalSet { global_index: g_base },
        i32c(para_start_offset as i32),
        I32Load { memarg: mem_arg(2) },
        GlobalSet { global_index: g_base + 1 },
        i32c(para_len_offset as i32),
        I32Load { memarg: mem_arg(2) },
        GlobalSet { global_index: g_base + 2 },
        i32c(memory_start_idx as i32),
        LocalSet { local_index: 1 },
        i32c(para_start_offset as i32),
        LocalGet { local_index: 1 },
        I32Store { memarg: mem_arg(2) },
        i32c(para_len_offset as i32),
        LocalGet { local_index: 0 },
        I32Store { memarg: mem_arg(2) },
        i32c(1),
        i32c(para_start_offset as i32),
        i32c(1),
        i32c(result_offset as i32),
        Call { function_index: fd_write_id },
        Drop,
        i32c(result_offset as i32),
        GlobalGet { global_index: g_base },
        I32Store { memarg: mem_arg(2) },
        i32c(para_start_offset as i32),
        GlobalGet { global_index: g_base + 1 },
        I32Store { memarg: mem_arg(2) },
        i32c(para_len_offset as i32),
        GlobalGet { global_index: g_base + 2 },
        I32Store { memarg: mem_arg(2) },
        End,
    ];
    Func {
        ty_idx: u32::MAX,
        locals: vec![ValType::I32],
        insts,
    }
}

fn build_noop_print_core_func() -> Func {
    Func {
        ty_idx: u32::MAX,
        locals: vec![],
        insts: vec![Inst::End],
    }
}

/// store_i32(addr=local1, value=local0): the call site pushes value first, then the address.
fn build_store_i32_func() -> Func {
    use Inst::*;
    Func {
        ty_idx: u32::MAX,
        locals: vec![],
        insts: vec![
            LocalGet { local_index: 1 },
            LocalGet { local_index: 0 },
            I32Store { memarg: mem_arg(2) },
            End,
        ],
    }
}

fn build_zero_global(val_ty: ValType) -> Global {
    Global {
        ty: GlobalType { val_ty, mutable: true },
        init_expr: match val_ty {
            ValType::I32 => vec![i32c(0)],
            ValType::I64 => vec![Inst::I64Const { value: 0 }],
            _ => unreachable!("global type not supported"),
        },
    }
}

/// Intermediate state for building the Rust-side Func (ty_idx patched back by the orchestration layer).
struct Func {
    locals: Vec<ValType>,
    insts: Vec<Inst>,
    ty_idx: u32,
}

impl Func {
    fn to_module_func(&self) -> e2wr_ir::module::Func {
        e2wr_ir::module::Func {
            ty_idx: self.ty_idx,
            locals: self.locals.clone(),
            insts: self.insts.clone(),
        }
    }
}

// ---------------------------------------------------------------------------
// Probe instruction sequence (the empty-stack path of Python `NonRefStackProbe.dump_stack_non_ref_probe`).
// ---------------------------------------------------------------------------

fn executed_probe_insts(
    buffer: &BufferConfig,
    funcs: &ProbeFuncs,
    probe_idx: u32,
    max_output_time: Option<u32>,
) -> Vec<Inst> {
    use Inst::*;
    let m = buffer.probe_memory_start_idx;
    let g = buffer.save_memory_global_start_idx;
    let packed = pack_probe_header(encode_probe_type(ProbeType::Executed), probe_idx);
    let mut insts: Vec<Inst> = vec![i32c(packed as i32)];
    // Save the 8 bytes at the probe memory location into an i64 global.
    insts.push(i32c(m as i32));
    insts.push(I64Load { memarg: mem_arg(3) });
    insts.push(GlobalSet { global_index: g });
    // Write the event header word into probe memory.
    insts.push(i32c(m as i32));
    insts.push(Call { function_index: funcs.store_i32_func_idx });
    // Output (optionally capped by the 1-byte-per-probe counter).
    insts.extend(call_print_core_insts(funcs, probe_idx, max_output_time, buffer));
    // Restore the 8 bytes.
    insts.push(i32c(m as i32));
    insts.push(GlobalGet { global_index: g });
    insts.push(I64Store { memarg: mem_arg(3) });
    insts
}

fn counter_addr_insts(buffer: &BufferConfig, probe_idx: u32) -> Vec<Inst> {
    vec![i32c((buffer.probe_output_counter_start_idx + probe_idx) as i32)]
}

fn call_print_core_insts(
    funcs: &ProbeFuncs,
    probe_idx: u32,
    max_output_time: Option<u32>,
    buffer: &BufferConfig,
) -> Vec<Inst> {
    use Inst::*;
    let word_num_inst = i32c(4);
    let probe_call = Call { function_index: funcs.print_core_probe_func_idx };
    let noop_call = Call { function_index: funcs.noop_print_core_probe_func_idx };
    let Some(max_output_time) = max_output_time else {
        return vec![word_num_inst, probe_call];
    };
    if max_output_time == 0 {
        return vec![word_num_inst, noop_call];
    }
    let cond_if_type = BlockType::FuncType(funcs.print_core_probe_type_idx);
    let mut insts = vec![word_num_inst];
    insts.extend(counter_addr_insts(buffer, probe_idx));
    insts.push(I32Load8U { memarg: mem_arg(0) });
    insts.push(i32c(max_output_time as i32));
    insts.push(I32LtU);
    insts.push(If { blockty: cond_if_type });
    insts.extend(counter_addr_insts(buffer, probe_idx));
    insts.extend(counter_addr_insts(buffer, probe_idx));
    insts.push(I32Load8U { memarg: mem_arg(0) });
    insts.push(i32c(1));
    insts.push(I32Add);
    insts.push(I32Store8 { memarg: mem_arg(0) });
    insts.push(probe_call);
    insts.push(Else);
    insts.push(noop_call);
    insts.push(End);
    insts
}

// ---------------------------------------------------------------------------
// Execution trace collection (Python `CodeProbeUtil.py`).
// ---------------------------------------------------------------------------

/// Locates the imported fd_write, returning its **imported function index** (None if absent).
pub fn locate_imported_fd_write(module: &Module) -> Option<u32> {
    let mut import_func_idx = 0u32;
    for imp in &module.imports {
        if let ImportDesc::Func(_) = imp.desc {
            if imp.module == "wasi_unstable" && imp.name == "fd_write" {
                return Some(import_func_idx);
            }
            import_func_idx += 1;
        }
    }
    None
}

/// Runs `timeout {t} wasmtime run <path>` and returns the raw stdout bytes.
pub fn exec_and_get_trace(path: &Path, allocated_time: u64, debug: bool) -> Result<Vec<u8>> {
    let cmd = format!("timeout {allocated_time} wasmtime run {}", path.display());
    let outcome = e2wr_dd::oracle::run_with_timeout(&cmd, allocated_time)
        .with_context(|| format!("run trace collector: {cmd}"))?;
    if debug && outcome.stdout.is_empty() {
        // The Python DEBUG branch: validates the artifact and warns on memory allocation failure (here a full decode
        // replaces the shell wasm-tools validate; equivalent judgment surface).
        let bytes = std::fs::read(path)?;
        if Snapshot::from_bytes(bytes).is_err() {
            bail!("The instrumented program is invalid : {}", path.display());
        }
        if outcome.returncode != Some(0) && outcome.stderr.windows(20).any(|w| w == b"Cannot allocate memory") {
            eprintln!(
                "Warning: There may be something wrong: StdErr: {:?}, ReturnCode: {:?}",
                String::from_utf8_lossy(&outcome.stderr),
                outcome.returncode
            );
        }
    }
    Ok(outcome.stdout)
}

// ---------------------------------------------------------------------------
// The instrumentation manager (Python `ValueProbeManager`).
// ---------------------------------------------------------------------------

/// The to_test function fails instrumentation preconditions (Python's two exception kinds, uniformly swallowed into an empty result).
#[derive(Debug)]
enum SkipProbe {
    NotInBinary(String),
    RequiresParams(String),
}

impl std::fmt::Display for SkipProbe {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            SkipProbe::NotInBinary(m) | SkipProbe::RequiresParams(m) => write!(f, "{m}"),
        }
    }
}

impl std::error::Error for SkipProbe {}

pub struct InstrumentManager {
    pub instrumented_path: PathBuf,
    pub to_test_func_name: String,
    pub debug: bool,
}

/// Instrumentation parameters (the keyword arguments of Python `instrument_multiple_places_and_get_result`,
/// same defaults: allocated_time=45, max_output_time=255).
/// R-12 narrowing: `max_global_num` (always 200, including the `instrument_and_get_exec_freq`
/// parameter — Python's only call site does not pass it) and `counter_mem_start_idx` (always 0x1000;
/// the Python comment says `all_memory_start_idx is never provided externally`)
/// degenerated to constants and no longer take fields.
#[derive(Debug, Clone, Copy)]
pub struct InstrumentParams {
    pub allocated_time: u64,
    pub only_executed_probe: bool,
    pub max_output_time: Option<u32>,
}

/// Cap on appended globals (Python keyword default 200; never overridden at any live call site).
const MAX_GLOBAL_NUM: u32 = 200;
/// Counter memory start index (Python keyword default 0x1000; no override site).
const COUNTER_MEM_START_IDX: u32 = 0x1000;

impl Default for InstrumentParams {
    fn default() -> Self {
        InstrumentParams {
            allocated_time: INSTRUMENTATION_TIMEOUT,
            only_executed_probe: false,
            max_output_time: Some(255),
        }
    }
}

impl InstrumentManager {
    pub fn new(instrumented_path: &Path, to_test_func_name: &str, debug: bool) -> Self {
        InstrumentManager {
            instrumented_path: instrumented_path.to_path_buf(),
            to_test_func_name: to_test_func_name.to_string(),
            debug,
        }
    }

    /// Inserts EXECUTED probes at a set of positions and counts execution frequencies (the common base of Python's
    /// `instrument_and_get_exec_freq` + `IdUnexecFuncUtil.get_un_exec_func_idxs_by_freq`
    /// Returns an empty map when there are no events at all (Python behavior: the upstream treats it as no unexecuted functions).
    /// Python's max_global_num parameter is always used at its default 200 (R-12 inlined as a
    /// constant).
    pub fn instrument_and_get_exec_freq(
        &self,
        snapshot: &Snapshot,
        locs: &BTreeSet<NodeLoc>,
    ) -> Result<BTreeMap<NodeLoc, u32>> {
        let probe_descs: Vec<ProbeDesc> = locs
            .iter()
            .enumerate()
            .map(|(idx, loc)| ProbeDesc { idx: idx as u32, loc: *loc })
            .collect();
        let params = InstrumentParams {
            only_executed_probe: true,
            ..Default::default()
        };
        let dumped = self.instrument_multiple_places_and_get_result(snapshot, &probe_descs, &params)?;
        if dumped.is_empty() {
            return Ok(BTreeMap::new());
        }
        let idx2loc: BTreeMap<u32, NodeLoc> =
            probe_descs.iter().map(|d| (d.idx, d.loc)).collect();
        let mut loc2freq: BTreeMap<NodeLoc, u32> = locs.iter().map(|l| (*l, 0)).collect();
        for ev in dumped {
            let loc = idx2loc[&ev.probe_idx];
            *loc2freq.entry(loc).or_insert(0) += 1;
        }
        Ok(loc2freq)
    }

    /// Main entry: instrument → encode to disk → run under wasmtime → parse the trace.
    /// An unexported/parameter-taking to_test prints a skip message and returns an empty result (Python behavior).
    pub fn instrument_multiple_places_and_get_result(
        &self,
        snapshot: &Snapshot,
        probe_descs: &[ProbeDesc],
        params: &InstrumentParams,
    ) -> Result<Vec<ProbeEvent>> {
        if probe_descs.is_empty() {
            return Ok(vec![]);
        }
        if let Some(max_output_time) = params.max_output_time {
            if max_output_time > 255 {
                bail!("max_output_time must be <= 255 when byte-counter is used, got {max_output_time}");
            }
        }
        let module = snapshot.module();
        let raw_defined_func_num = module.defined_funcs.len() as u32;
        for d in probe_descs {
            if d.loc.func_idx >= raw_defined_func_num {
                bail!("probe loc out of range: func_idx {} >= {}", d.loc.func_idx, raw_defined_func_num);
            }
        }
        let memory_start_idx = calc_probe_memory_start_idx(module)?;
        let buffer = BufferConfig::new(
            global_num(module),
            MAX_GLOBAL_NUM,
            memory_start_idx,
            COUNTER_MEM_START_IDX,
        );
        if params.max_output_time.is_some() {
            let max_probe_idx = probe_descs.iter().map(|d| d.idx).max().unwrap();
            if COUNTER_MEM_START_IDX + max_probe_idx >= 65536 {
                bail!(
                    "probe idx too large for one-page counter window: max_probe_idx={max_probe_idx}, need <= {}",
                    65536 - COUNTER_MEM_START_IDX - 1
                );
            }
        }
        let known: HashSet<u32> = probe_descs.iter().map(|d| d.idx).collect();

        let build_result = (|| -> Result<(Snapshot,)> {
            ensure_snapshot_exports_to_test_func(module, &self.to_test_func_name)?;
            let batch =
                build_instrumentation_mutations(module, probe_descs, &buffer, params, &self.to_test_func_name)?;
            let instrumented_snapshot = snapshot.full_copy();
            let new_snap = mutation::apply_mutations(&instrumented_snapshot, &batch)?;
            let bytes = new_snap.encode_to_bytes()?;
            if self.debug {
                // D-14: the debug assertion at Python ValueProbeInstrument:294-295
                // (validate_wasm spec-level validation, no exemption). Was previously
                // Snapshot::from_bytes parse-level self-checking; upgraded to the wasmparser
                // spec validation when D-14 landed (judgment surface aligned with wasm-validate).
                if !crate::instseq::trial::validate_wasm_bytes(&bytes) {
                    anyhow::bail!("instrumented output failed validation");
                }
            }
            std::fs::write(&self.instrumented_path, bytes)
                .with_context(|| format!("write {}", self.instrumented_path.display()))?;
            Ok((new_snap,))
        })();

        match build_result {
            Ok(_) => {
                let raw_trace = exec_and_get_trace(&self.instrumented_path, params.allocated_time, self.debug)?;
                let parsed = if params.only_executed_probe {
                    parse_executed_only(&raw_trace, &known)
                } else {
                    parse_generic(&raw_trace, &known)
                };
                Ok(parsed)
            }
            Err(e) => {
                if let Some(skip) = e.downcast_ref::<SkipProbe>() {
                    eprintln!("Trigger skip probe instrumentation: {skip}");
                    return Ok(vec![]);
                }
                Err(e)
            }
        }
    }
}

fn global_num(module: &Module) -> u32 {
    (module.defined_globals.len() + module.import_global_num()) as u32
}

/// The to_test function must be exported and zero-arity (Python `ensure_snapshot_exports_to_test_func`,
/// always called with require_zero_arity=True).
fn ensure_snapshot_exports_to_test_func(module: &Module, to_test_func_name: &str) -> Result<()> {
    for export in &module.exports {
        if let ExportDesc::Func(func_idx) = export.desc {
            if export.name == to_test_func_name {
                let func_type_idx = module.func_type_idxs()[func_idx as usize];
                let func_type = &module.types[func_type_idx as usize];
                if !func_type.params.is_empty() {
                    return Err(SkipProbe::RequiresParams(format!(
                        "to_test_func_name \"{to_test_func_name}\" requires params: {:?}. \
                         Current instrumentation redirects `_start` to that export, so probing is skipped.",
                        func_type.params
                    ))
                    .into());
                }
                return Ok(());
            }
        }
    }
    let exported_func_names: Vec<&str> = module
        .exports
        .iter()
        .filter(|e| matches!(e.desc, ExportDesc::Func(_)))
        .map(|e| e.name.as_str())
        .collect();
    Err(SkipProbe::NotInBinary(format!(
        "to_test_func_name \"{to_test_func_name}\" not found in exports. Available exported functions: {exported_func_names:?}"
    ))
    .into())
}

/// Probe memory base address (bytes): page_base*65536 + 0x1000,
/// min≤1 page → page_base=0, else 1 (Python `_calc_probe_memory_start_idx`).
fn calc_probe_memory_start_idx(module: &Module) -> Result<u32> {
    const PAGE_SIZE: u32 = 65536;
    if module.import_memory_num() > 0 {
        bail!("Imported memory is not supported by this instrumentation path");
    }
    let old_min_pages = module.defined_memory_datas.first().map(|m| m.ty.limits.min).unwrap_or(0) as u32;
    let page_base = if old_min_pages <= 1 { 0 } else { 1 };
    Ok(page_base * PAGE_SIZE + 0x1000)
}

/// Type dedup-and-reuse (Python `_get_or_append_typeidx`): first search the existing type section,
/// then the pending append mutations of this round; append only if absent in both.
struct TypeAppender<'a> {
    module: &'a Module,
    base: u32,
    pending: Vec<FuncType>,
}

impl<'a> TypeAppender<'a> {
    fn new(module: &'a Module) -> Self {
        TypeAppender { module, base: module.types.len() as u32, pending: vec![] }
    }

    fn get_or_append(&mut self, ty: &FuncType) -> u32 {
        if let Some(i) = self.module.types.iter().position(|t| t == ty) {
            return i as u32;
        }
        if let Some(i) = self.pending.iter().position(|t| t == ty) {
            return self.base + i as u32;
        }
        self.pending.push(ty.clone());
        self.base + self.pending.len() as u32 - 1
    }

    fn into_edit(self) -> Result<Option<DefEdit>> {
        if self.pending.is_empty() {
            return Ok(None);
        }
        let mut defs = Vec::with_capacity(self.pending.len());
        for ty in &self.pending {
            defs.push(mutation::type_def(ty)?);
        }
        Ok(Some(DefEdit { range: self.base..self.base, repl: defs }))
    }
}

/// Shifts an element segment's function indices (≥ insertion_point get +delta; None when unchanged).
fn shift_one_elem_seg(seg: &e2wr_ir::module::ElemSeg, insertion_point: u32, delta: u32) -> Option<e2wr_ir::module::ElemSeg> {
    let mut seg2 = seg.clone();
    let changed = match &mut seg2.payload {
        e2wr_ir::module::ElemPayload::FuncIdxs(idxs) => {
            let mut changed = false;
            for x in idxs.iter_mut() {
                if *x >= insertion_point {
                    *x += delta;
                    changed = true;
                }
            }
            changed
        }
        e2wr_ir::module::ElemPayload::Exprs { exprs, .. } => {
            let mut changed = false;
            for expr in exprs.iter_mut() {
                for inst in expr.iter_mut() {
                    if let Inst::RefFunc { function_index } = inst {
                        if *function_index >= insertion_point {
                            *function_index += delta;
                            changed = true;
                        }
                    }
                }
            }
            changed
        }
    };
    if changed { Some(seg2) } else { None }
}

/// Builds the instrumentation mutation batch (Python `_build_snapshot_instrumentation_mutations`).
/// R-31: previously returned (Vec<Mutation>, ProbeFuncs); the second value was dropped at its only call site
/// (probe_funcs is used only inside this function); the return type narrowed to Vec<Mutation>.
fn build_instrumentation_mutations(
    module: &Module,
    probe_descs: &[ProbeDesc],
    buffer: &BufferConfig,
    params: &InstrumentParams,
    to_test_func_name: &str,
) -> Result<Vec<Mutation>> {
    let mut mutations: Vec<Mutation> = Vec::new();
    let mut exports_replace: Vec<DefEdit> = Vec::new();
    let mut elem_replace: Vec<DefEdit> = Vec::new();
    let mut edited_funcs: BTreeMap<u32, e2wr_ir::module::Func> = BTreeMap::new();
    let mut type_appender = TypeAppender::new(module);

    // ---- 1) Import fd_write if needed, and shift function index references ----
    let old_import_func_num = module.import_func_num() as u32;
    let mut fd_write_func_idx = locate_imported_fd_write(module);
    let need_insert_fd_write = fd_write_func_idx.is_none();
    let delta_import_func = if need_insert_fd_write { 1u32 } else { 0 };
    if need_insert_fd_write {
        let fd_write_type = FuncType {
            params: vec![ValType::I32; 4],
            results: vec![ValType::I32],
        };
        let fd_write_typeidx = type_appender.get_or_append(&fd_write_type);
        fd_write_func_idx = Some(old_import_func_num);
        mutations.push(Mutation::definitions(
            e2wr_ir::SectionKind::Import,
            vec![DefEdit {
                range: module.imports.len() as u32..module.imports.len() as u32,
                repl: vec![mutation::import_def(&Import {
                    module: "wasi_unstable".to_string(),
                    name: "fd_write".to_string(),
                    desc: ImportDesc::Func(fd_write_typeidx),
                })?],
            }],
        ));
    }
    let fd_write_func_idx = fd_write_func_idx.expect("fd_write located or inserted");

    if delta_import_func > 0 {
        // call/ref.func immediate shifts (whole-function re-encode path, merged with probe insertion into the same rebuild).
        for (func_idx, func) in module.defined_funcs.iter().enumerate() {
            let mut touched = false;
            let mut new_insts = func.insts.clone();
            for inst in new_insts.iter_mut() {
                match inst {
                    Inst::Call { function_index } | Inst::RefFunc { function_index } => {
                        if *function_index >= old_import_func_num {
                            *function_index += delta_import_func;
                            touched = true;
                        }
                    }
                    _ => {}
                }
            }
            if touched {
                let f = edited_funcs.entry(func_idx as u32).or_insert_with(|| func.clone());
                f.insts = new_insts;
            }
        }
        // Export shifts: _start is redirected later; skipped to avoid duplicate mutations on the same slot.
        for (i, e) in module.exports.iter().enumerate() {
            if let ExportDesc::Func(idx) = e.desc {
                if e.name == "_start" && to_test_func_name != "_start" {
                    continue;
                }
                if idx < old_import_func_num {
                    continue;
                }
                exports_replace.push(DefEdit::replace_one(
                    i as u32,
                    mutation::export_def(&Export {
                        name: e.name.clone(),
                        desc: ExportDesc::Func(idx + delta_import_func),
                    })?,
                ));
            }
        }
        // start section shift.
        if let Some(start) = module.start_sec_data {
            if start >= old_import_func_num {
                mutations.push(Mutation::definitions(
                    e2wr_ir::SectionKind::Start,
                    vec![DefEdit::replace_one(0, mutation::u32_def(start + delta_import_func))],
                ));
            }
        }
        // Element segment shifts.
        for (i, seg) in module.elem_sec_datas.iter().enumerate() {
            if let Some(seg2) = shift_one_elem_seg(seg, old_import_func_num, delta_import_func) {
                elem_replace.push(DefEdit::replace_one(i as u32, mutation::elem_def(&seg2)?));
            }
        }
    }

    // ---- 2) Memory guarantee: insert one page when there is no memory at all (the grow branch is a dead Python path, not replicated) ----
    if module.import_memory_num() == 0 && module.defined_memory_datas.is_empty() {
        mutations.push(Mutation::definitions(
            e2wr_ir::SectionKind::Memory,
            vec![DefEdit {
                range: 0..0,
                repl: vec![mutation::memory_def(&Memory {
                    ty: MemType { limits: Limits { min: 1, max: None } },
                })?],
            }],
        ));
    }

    // ---- 3) Export guarantees: `memory` and `_start`; redirect _start when necessary ----
    let mut exports_to_append: Vec<RawDef> = Vec::new();
    let has_exported_memory = module
        .exports
        .iter()
        .any(|e| matches!(e.desc, ExportDesc::Memory(_)) && e.name == "memory");
    if !has_exported_memory {
        exports_to_append.push(mutation::export_def(&Export {
            name: "memory".to_string(),
            desc: ExportDesc::Memory(0),
        })?);
    }
    let has_start_export = module
        .exports
        .iter()
        .any(|e| matches!(e.desc, ExportDesc::Func(_)) && e.name == "_start");
    let shift_idx = |idx: u32| if idx >= old_import_func_num { idx + delta_import_func } else { idx };
    if !has_start_export {
        if to_test_func_name != "_start" {
            let to_exec = get_exported_func_idx_by_name(module, to_test_func_name)
                .expect("to_test export ensured above");
            exports_to_append.push(mutation::export_def(&Export {
                name: "_start".to_string(),
                desc: ExportDesc::Func(shift_idx(to_exec)),
            })?);
        } else {
            bail!(
                "to_test_func_name is _start, but no _start export found; this instrumentation \
                 path requires a _start export to work. That indicates the `to_test_func_name` \
                 provided is incorrect."
            );
        }
    }
    if !exports_to_append.is_empty() {
        // In-section mutations dedup by range; the new export must merge into a single insertion (stated in a Python comment).
        exports_replace.push(DefEdit {
            range: module.exports.len() as u32..module.exports.len() as u32,
            repl: exports_to_append,
        });
    }
    if to_test_func_name != "_start" {
        let to_exec = get_exported_func_idx_by_name(module, to_test_func_name)
            .expect("to_test export ensured above");
        if let Some(i) = module.exports.iter().position(|e| {
            matches!(e.desc, ExportDesc::Func(_)) && e.name == "_start"
        }) {
            exports_replace.push(DefEdit::replace_one(
                i as u32,
                mutation::export_def(&Export {
                    name: "_start".to_string(),
                    desc: ExportDesc::Func(shift_idx(to_exec)),
                })?,
            ));
        }
    }

    // ---- 4) Append globals: 3 mutable i32 + (max_global_num-3) mutable i64 ----
    let mut new_global_defs = Vec::new();
    for _ in 0..3 {
        new_global_defs.push(mutation::global_def(&build_zero_global(ValType::I32))?);
    }
    for _ in 0..buffer.max_new_global_num.saturating_sub(3) {
        new_global_defs.push(mutation::global_def(&build_zero_global(ValType::I64))?);
    }
    mutations.push(Mutation::definitions(
        e2wr_ir::SectionKind::Global,
        vec![DefEdit {
            range: module.defined_globals.len() as u32..module.defined_globals.len() as u32,
            repl: new_global_defs,
        }],
    ));

    // ---- 5) Append helper functions: print_core / noop / store_i32 ----
    let print_core_type = FuncType { params: vec![ValType::I32], results: vec![] };
    let print_core_func = build_print_core_func(
        fd_write_func_idx,
        buffer.ori_global_num,
        buffer.probe_memory_start_idx,
    );
    let print_core_typeidx = type_appender.get_or_append(&print_core_type);
    let noop_func = build_noop_print_core_func();
    let store_i32_func = build_store_i32_func();
    let store_i32_typeidx = type_appender.get_or_append(&FuncType {
        params: vec![ValType::I32, ValType::I32],
        results: vec![],
    });

    let new_import_func_num = old_import_func_num + delta_import_func;
    let defined_func_num = module.defined_funcs.len() as u32;
    let probe_funcs = ProbeFuncs {
        print_core_probe_func_idx: new_import_func_num + defined_func_num,
        print_core_probe_type_idx: print_core_typeidx,
        noop_print_core_probe_func_idx: new_import_func_num + defined_func_num + 1,
        store_i32_func_idx: new_import_func_num + defined_func_num + 2,
    };

    let mut helper_typeidxs = Vec::new();
    let mut helper_func_defs = Vec::new();
    {
        let mut finalize = |f: &Func, ty_idx: u32| -> Result<()> {
            helper_typeidxs.push(ty_idx);
            let mut mf = f.to_module_func();
            mf.ty_idx = ty_idx;
            helper_func_defs.push(mutation::func_def(&mf)?);
            Ok(())
        };
        finalize(&print_core_func, print_core_typeidx)?;
        finalize(&noop_func, print_core_typeidx)?;
        finalize(&store_i32_func, store_i32_typeidx)?;
    }
    mutations.push(Mutation::definitions(
        e2wr_ir::SectionKind::Function,
        vec![DefEdit {
            range: module.defined_func_ty_ids.len() as u32..module.defined_func_ty_ids.len() as u32,
            repl: helper_typeidxs.into_iter().map(mutation::u32_def).collect(),
        }],
    ));
    mutations.push(Mutation::definitions(
        e2wr_ir::SectionKind::Code,
        vec![DefEdit {
            range: defined_func_num..defined_func_num,
            repl: helper_func_defs,
        }],
    ));

    // ---- 6) Probe instruction insertion (original coordinate space; merged with the shifts in the same whole-function rebuild) ----
    // Python applies FuncInstMutations via the applier in descending start_offset order (later positions inserted first,
    // so earlier positions are not shifted by later insertions). Fixed 2026-10-01: insertion previously followed the probe list order,
    // so the second and later probes in a function landed displaced by previously inserted instruction lengths (probe header
    // constants misaligned with the emission mechanism, events lost; located while investigating commanderkeen).
    // The equivalent implementation here: group by function, insert in descending inst_idx.
    {
        use std::collections::BTreeMap as Map;
        let mut by_func: Map<u32, Vec<(u32, Vec<Inst>)>> = Map::new();
        for d in probe_descs {
            let insts = executed_probe_insts(buffer, &probe_funcs, d.idx, params.max_output_time);
            by_func.entry(d.loc.func_idx).or_default().push((d.loc.inst_idx, insts));
        }
        for (func_idx, mut probes) in by_func {
            probes.sort_by(|a, b| b.0.cmp(&a.0)); // inst_idx descending
            let f = edited_funcs
                .entry(func_idx)
                .or_insert_with(|| module.defined_funcs[func_idx as usize].clone());
            for (inst_idx, insts) in probes {
                let insert_at = inst_idx as usize;
                assert!(
                    insert_at <= f.insts.len(),
                    "probe inst_idx {} out of range (len {})",
                    insert_at,
                    f.insts.len()
                );
                let tail: Vec<Inst> = f.insts.split_off(insert_at);
                f.insts.extend(insts);
                f.insts.extend(tail);
            }
        }
    }

    // ---- Assemble the mutation batch ----
    if let Some(edit) = type_appender.into_edit()? {
        mutations.push(Mutation::definitions(e2wr_ir::SectionKind::Type, vec![edit]));
    }
    if !exports_replace.is_empty() {
        mutations.push(Mutation::definitions(e2wr_ir::SectionKind::Export, exports_replace));
    }
    if !elem_replace.is_empty() {
        mutations.push(Mutation::definitions(e2wr_ir::SectionKind::Element, elem_replace));
    }
    let mut code_edits = Vec::new();
    for (func_idx, f) in &edited_funcs {
        code_edits.push(DefEdit::replace_one(*func_idx, mutation::func_def(f)?));
    }
    if !code_edits.is_empty() {
        mutations.push(Mutation::definitions(e2wr_ir::SectionKind::Code, code_edits));
    }

    Ok(mutations)
}

fn get_exported_func_idx_by_name(module: &Module, func_name: &str) -> Option<u32> {
    module
        .exports
        .iter()
        .find(|e| matches!(e.desc, ExportDesc::Func(_)) && e.name == func_name)
        .map(|e| match e.desc {
            ExportDesc::Func(i) => i,
            _ => unreachable!(),
        })
}
