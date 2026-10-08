//! U4: the probe-encode-oracle chain (single node list, D-11).
//!
//! Mirrors Python (the post-round-4-cleanup baseline):
//! - `ReduceUtil/MutationInstsUtil.py`: `get_mutated_insts_sequence` /
//!   `get_inst_mutation` / `OneReduceUnitInfo` (`OneNodeListMutation`/
//!   `FIMutationsInOneNodeList`, two pure-data shells, folded into this module's parameter shapes);
//! - `ReduceUtil/ElemGuidedNodeListReducerMultiNode.py` +
//!   `RewritingUtil/OnlyInstSnapshotRewriter.py`'s live methods merged into
//!   [`NodeListTrialApplier`];
//! - `RewritingUtil/NodeReplacement.py`: this path degenerates to "whole-segment replacement inside the parent list"
//!   (`replace_split_with_new_sub_nodes`).
//!
//! State model (aligned with actual Python behavior): **within a stage**, probes start from the stage-entry baseline
//! snapshot, rebuilding whole functions per the accumulated mutation dict; only success advances `final_snapshot` and
//! the AST tree (`update_ast_nodes` rebuilds children via `insts2AST` on the mutated full instruction sequence);
//! **between stages**, the baseline advances with `finalize` (Python's
//! `finalize_replace_insts_range_v2` synchronizes the shared parser and sets
//! `ast_state.snapshot = final_snapshot`; the U8 chained comparison confirmed its necessity).
//! Python's byte-layer snapshot lazily decodes the final bytes; the parser synchronization is equivalently
//! implemented by baseline advancement in Rust.
//!
//! Mechanism replacement (equivalent judgment surface): Python's `tmp_file_is_valid` shells out to wasm-validate;
//! Rust fully validates with the wasmparser `Validator` (same spec implementation, no subprocess).
//! The DEBUG-only wat dumping and prints are not ported.

use std::collections::BTreeMap;
use std::path::PathBuf;

use anyhow::{bail, Context as _, Result};

use e2wr_dd::oracle::Oracle;
use e2wr_ir::ast::{Ast, NodeId};
use e2wr_ir::decode::decode_bytes;
use e2wr_ir::mutation::{
    apply_mutation_and_encode, func_def, DefEdit, Mutation,
};
use e2wr_ir::snapshot::{SectionKind, Snapshot};
use e2wr_ir::types::FTy;
use e2wr_ir::Inst;

use crate::instseq::elem::OneElem;

/// The judgment-surface-equivalent replacement of Python `validate_wasm` (a wasm-validate subprocess).
pub fn validate_wasm_bytes(bytes: &[u8]) -> bool {
    wasmparser::Validator::new().validate_all(bytes).is_ok()
}

/// Seconds budget → deadline (a non-positive/invalid budget → an immediately-due deadline; Python's negative
/// rest_time yields a past deadline that fires on the first timeout check; same semantics).
pub fn deadline_after(secs: f64) -> std::time::SystemTime {
    let d = if secs.is_finite() && secs > 0.0 {
        std::time::Duration::from_secs_f64(secs)
    } else {
        std::time::Duration::ZERO
    };
    std::time::SystemTime::now() + d
}

/// Whether the deadline has passed (None = no budget, always false). R-28: the same three-line check
/// was previously repeated in 7 places (free-function versions in core_stage/rev_stage/shrink_block,
/// method versions in cf_elem/p3/v9, an inline version in cfn_stage); merged into this function.
pub fn deadline_passed(deadline: Option<std::time::SystemTime>) -> bool {
    deadline
        .map(|t| std::time::SystemTime::now() > t)
        .unwrap_or(false)
}

/// Python `OneReduceUnitInfo` (D-11 single list: one applier has exactly one unit).
pub struct ReduceUnit {
    pub ori_node_list: NodeId,
    pub func_idx: u32,
    /// The list's starting instruction ordinal (baseline coordinates; unchanged during probing).
    pub inst_idx: u32,
    pub node_type: FTy,
    /// Python `_last_nodes`: the list's children after the last successful rebuild.
    last_nodes: Vec<NodeId>,
}

impl ReduceUnit {
    pub fn new(ast: &Ast, ori_node_list: NodeId) -> Result<ReduceUnit> {
        let node = ast.node(ori_node_list);
        Ok(ReduceUnit {
            ori_node_list,
            func_idx: node.loc.func_idx,
            inst_idx: node.loc.inst_idx,
            node_type: ast.get_node_list_type_in_ast_practical(ori_node_list)?,
            last_nodes: ast.sub_nodes(ori_node_list),
        })
    }

    pub fn actual_nodes(&self) -> &[NodeId] {
        &self.last_nodes
    }
}

/// The live fields of Python `FuncInstMutation` (the replacement of one instruction interval within a function).
pub struct InstRangeMutation {
    pub start_offset: u32,
    pub end_offset: u32,
    pub new_insts: Vec<Inst>,
}

/// Python `get_inst_mutation`: element index → (baseline) instruction interval and new instructions.
/// The interval start accumulates from `unit.inst_idx` by element lengths (Python builds two dicts; equivalent).
pub fn get_inst_mutations(
    unit: &ReduceUnit,
    ast: &Ast,
    raw_elems: &[OneElem],
    mutation_elem_idx2new_elems: &BTreeMap<usize, Vec<OneElem>>,
) -> Result<Vec<InstRangeMutation>> {
    let mut out = Vec::new();
    let mut cur_inst_idx = unit.inst_idx;
    for (elem_idx, elem) in raw_elems.iter().enumerate() {
        let start = cur_inst_idx;
        cur_inst_idx += elem.get_length(ast);
        if let Some(new_elems) = mutation_elem_idx2new_elems.get(&elem_idx) {
            let mut new_insts = Vec::new();
            for e in new_elems {
                new_insts.extend(e.as_insts(ast));
            }
            out.push(InstRangeMutation {
                start_offset: start,
                end_offset: cur_inst_idx,
                new_insts,
            });
        }
    }
    Ok(out)
}

/// Python `get_mutated_insts_sequence`: the full pre + replaced segment + post sequence.
pub fn get_mutated_insts_sequence(
    ast: &Ast,
    raw_elems: &[OneElem],
    mutation_elem_idx2new_elems: &BTreeMap<usize, Vec<OneElem>>,
) -> Result<Vec<Inst>> {
    let Some(&start_idx) = mutation_elem_idx2new_elems.keys().next() else {
        // Python takes mutation_idxs[0] on an empty dict and gets IndexError (the upstream v2 has
        // a non-empty assertion).
        bail!("empty mutation dict");
    };
    let end_idx = *mutation_elem_idx2new_elems.keys().next_back().unwrap() + 1;

    let mut out: Vec<Inst> = Vec::new();
    for elem in &raw_elems[..start_idx] {
        out.extend(elem.as_insts(ast));
    }
    for (elem_idx, elem) in raw_elems.iter().enumerate().take(end_idx.min(raw_elems.len())).skip(start_idx) {
        if let Some(new_elems) = mutation_elem_idx2new_elems.get(&elem_idx) {
            for e in new_elems {
                out.extend(e.as_insts(ast));
            }
        } else {
            out.extend(elem.as_insts(ast));
        }
    }
    for elem in &raw_elems[end_idx.min(raw_elems.len())..] {
        out.extend(elem.as_insts(ast));
    }
    Ok(out)
}

/// The stage ↔ probe-chain protocol (Python `OneNodeListReducerApplier`, D-11 single list).
/// Stages from U5 on depend only on this trait; the real implementation is [`NodeListTrialApplier`], the test stub
/// implements it separately (the frozen surface of design-notes section 9).
pub trait StageApplier {
    /// Python `gen_replacement_by_elems_and_test_by_mutation`.
    fn try_mutation(
        &mut self,
        raw_elems: &[OneElem],
        mutation_elem_idx2new_elems: &BTreeMap<usize, Vec<OneElem>>,
        check_invalid: Option<bool>,
    ) -> Result<bool>;
    /// Python `finalize(ori_node_list, raw_elems_length, new_elems)`.
    fn finalize(&mut self, raw_elems_length: u32, new_elems: &[OneElem]) -> Result<()>;
    /// Python `cal_elems_length`.
    fn cal_elems_length(&self, elems: &[OneElem]) -> u32;
    /// Python `actual_nodes`.
    fn actual_nodes(&self) -> Vec<NodeId>;
}

/// Python `ElemGuidedNodeListReducerMultiNode` + `OnlyInstSnapshotRewriter`
/// Live methods merged (D-11 single-listing).
pub struct NodeListTrialApplier {
    debug: bool,
    unit: ReduceUnit,
    /// The stage baseline snapshot (used immutably; probes always rebuild from it).
    base_snapshot: Snapshot,
    /// The last successful probe's snapshot (equal to the baseline if none succeeded).
    final_snapshot: Snapshot,
    ast: Ast,
    oracle: Oracle,
    tmp_used_path: PathBuf,
    /// Python NodeRewriter.best_path.
    best_path: Option<PathBuf>,
    /// Observation counts (behavior-neutral): total probes / invalid artifacts intercepted.
    /// Design invariant (D-12): mutations generated by the stages are always valid; invalid_count should stay 0.
    pub trial_count: u64,
    pub invalid_count: u64,
}

impl NodeListTrialApplier {
    pub fn new(
        ori_node_list: NodeId,
        snapshot: Snapshot,
        oracle: Oracle,
        tmp_used_path: PathBuf,
        debug: bool,
    ) -> Result<NodeListTrialApplier> {
        let ast = Ast::from_module(snapshot.try_module()?)?;
        Self::new_with_ast(ori_node_list, snapshot, ast, oracle, tmp_used_path, debug)
    }

    /// U10: injects an existing syntax tree (a clone of the pass tree) — the task node's NodeId is given
    /// in that tree's coordinate system; the id semantics match Python's shared tree (cloning preserves arena indices,
    /// later evolution is stable for existing ids).
    pub fn new_with_ast(
        ori_node_list: NodeId,
        snapshot: Snapshot,
        ast: Ast,
        oracle: Oracle,
        tmp_used_path: PathBuf,
        debug: bool,
    ) -> Result<NodeListTrialApplier> {
        let unit = ReduceUnit::new(&ast, ori_node_list)?;
        Ok(NodeListTrialApplier {
            debug,
            unit,
            base_snapshot: snapshot.clone(),
            final_snapshot: snapshot,
            ast,
            oracle,
            tmp_used_path,
            best_path: None,
            trial_count: 0,
            invalid_count: 0,
        })
    }

    // R-30: intrinsic getters ast()/snapshot() with no callers (the retrieval need is covered
    // by into_state) and the one-line forwarding intrinsic actual_nodes to ReduceUnit::actual_nodes
    // (whose only caller was this type's own trait impl) were all deleted.

    pub fn set_best_path(&mut self, path: PathBuf) {
        self.best_path = Some(path);
    }

    /// Python `cal_elems_length`.
    pub fn cal_elems_length(&self, elems: &[OneElem]) -> u32 {
        elems.iter().map(|e| e.get_length(&self.ast)).sum()
    }

    /// Takes back the final state (after finalize): the snapshot + the synchronized syntax tree.
    pub fn into_state(self) -> (Snapshot, Ast) {
        (self.final_snapshot, self.ast)
    }

    /// Python `gen_replacement_by_elems_and_test_by_mutation` (converged to a single list via v2).
    /// into a single list).
    ///
    /// `check_invalid`: Python `check_invalid_and_return_false` — defaults to `!DEBUG` when None;
    /// an invalid artifact chooses between "return false (treated as rejected)" and
    /// "error out".
    pub fn try_mutation(
        &mut self,
        raw_elems: &[OneElem],
        mutation_elem_idx2new_elems: &BTreeMap<usize, Vec<OneElem>>,
        check_invalid: Option<bool>,
    ) -> Result<bool> {
        if mutation_elem_idx2new_elems.is_empty() {
            // Python v2 asserts non-empty on every mutation dict.
            bail!("empty mutation dict");
        }
        self.trial_count += 1;
        let ranges = get_inst_mutations(&self.unit, &self.ast, raw_elems, mutation_elem_idx2new_elems)?;

        // Baseline function body + descending interval replacement (the stability note of Python merge_to_snapshot).
        let mut sorted = ranges;
        sorted.sort_by(|a, b| b.start_offset.cmp(&a.start_offset));
        let func = &self.base_snapshot.module().defined_funcs
            [self.unit.func_idx as usize];
        let mut new_func = func.clone();
        for r in &sorted {
            new_func.insts.splice(
                r.start_offset as usize..r.end_offset as usize,
                r.new_insts.iter().cloned(),
            );
        }
        let repl = func_def(&new_func)?;
        let batch = [Mutation::definitions(
            SectionKind::Code,
            vec![DefEdit::replace_one(self.unit.func_idx, repl)],
        )];
        let new_snap = match apply_mutation_and_encode(
            &self.base_snapshot,
            &batch,
            &self.tmp_used_path,
        ) {
            Ok(s) => s,
            Err(e) => return self.handle_invalid(check_invalid, e),
        };
        let bytes = std::fs::read(&self.tmp_used_path)
            .context("read back trial wasm")?;
        if !validate_wasm_bytes(&bytes) {
            return self.handle_invalid(
                check_invalid,
                anyhow::anyhow!("wasm validation failed"),
            );
        }

        if !self.oracle.check(&self.tmp_used_path)? {
            return Ok(false);
        }

        self.final_snapshot = new_snap;
        if let Some(bp) = self.best_path.clone() {
            std::fs::copy(&self.tmp_used_path, bp)
                .context("commit to best path")?;
        }
        let insts =
            get_mutated_insts_sequence(&self.ast, raw_elems, mutation_elem_idx2new_elems)?;
        self.update_ast_nodes(insts)?;
        Ok(true)
    }

    fn handle_invalid(&mut self, check_invalid: Option<bool>, e: anyhow::Error) -> Result<bool> {
        self.invalid_count += 1;
        let check = check_invalid.unwrap_or(!self.debug);
        if check {
            return Ok(false);
        }
        bail!("invalid trial wasm (check_invalid=false): {e:#}")
    }

    /// Python `OneReduceUnitInfo.update_ast_nodes` + `NodeReplacement.apply`:
    /// Rebuild children via insts2AST on the mutated full sequence, replace the whole segment in the parent list, recompute positions.
    fn update_ast_nodes(&mut self, insts: Vec<Inst>) -> Result<()> {
        let module = self.base_snapshot.module();
        let (_tmp_root, new_nodes) = self.ast.append_insts_tree(
            module,
            &insts,
            self.unit.node_type.clone(),
            self.unit.func_idx,
        )?;
        if new_nodes.is_empty() && self.unit.last_nodes.is_empty() {
            return Ok(());
        }
        if self.unit.last_nodes.is_empty() {
            // Python's sub_nodes.index(...) crashes on empty actual_nodes taking [0];
            // a dead path (task filtering guarantees non-empty lists); the explicit error aligns.
            bail!("update_ast_nodes with empty actual_nodes");
        }
        let parent = self.unit.ori_node_list;
        let children = match &self.ast.node(parent).kind {
            e2wr_ir::ast::NodeKind::List { children, .. } => children.clone(),
            _ => bail!("ori_node_list is not a list"),
        };
        let start = children
            .iter()
            .position(|c| *c == self.unit.last_nodes[0])
            .ok_or_else(|| anyhow::anyhow!("original nodes no longer in parent list"))?;
        self.ast.replace_split_with_new_sub_nodes(
            parent,
            start,
            start + self.unit.last_nodes.len(),
            new_nodes.clone(),
        );
        self.ast.update_loc_info(self.unit.func_idx as usize);
        self.unit.last_nodes = new_nodes;
        Ok(())
    }

    /// Python `finalize` (`finalize_replace_insts_range_v2`): cuts the shared
    /// parser's instruction sequence to the final content and sets
    /// `ast_state.snapshot = final_snapshot` — **the inter-stage rebuild baseline advances
    /// with finalize** (the next stage's probes start from the stage's final state). The byte-layer snapshot's
    /// equivalent = advancing the rebuild baseline to the final snapshot; parser synchronization needs no explicit step
    /// (on the Rust side the region coordinates are always `unit.inst_idx` + accumulated current element lengths,
    /// and after finalize the baseline body's region holds exactly the stage's final content, matching Python's
    /// region invariant).
    ///
    /// U8 chained verification: without this advancement, later stages' mutation intervals act on the pipeline-start
    /// original body (without the accepted reductions); the artifact is invalid (the D-12 invariant breaks).
    pub fn finalize(
        &mut self,
        _raw_elems_length: u32,
        _new_elems: &[OneElem],
    ) -> Result<()> {
        self.base_snapshot = self.final_snapshot.clone();
        Ok(())
    }
}

/// Convenience: whole-encode the final snapshot, re-decode, take one function's instruction sequence (for test comparison).
pub fn snapshot_insts_of(snapshot: &Snapshot, func_idx: u32) -> Result<Vec<Inst>> {
    let bytes = snapshot.encode_to_bytes()?;
    let module = decode_bytes(&bytes)?;
    Ok(module
        .defined_funcs
        .get(func_idx as usize)
        .context("func idx out of range")?
        .insts
        .clone())
}

impl StageApplier for NodeListTrialApplier {
    fn try_mutation(
        &mut self,
        raw_elems: &[OneElem],
        mutation_elem_idx2new_elems: &BTreeMap<usize, Vec<OneElem>>,
        check_invalid: Option<bool>,
    ) -> Result<bool> {
        NodeListTrialApplier::try_mutation(self, raw_elems, mutation_elem_idx2new_elems, check_invalid)
    }

    fn finalize(&mut self, raw_elems_length: u32, new_elems: &[OneElem]) -> Result<()> {
        NodeListTrialApplier::finalize(self, raw_elems_length, new_elems)
    }

    fn cal_elems_length(&self, elems: &[OneElem]) -> u32 {
        NodeListTrialApplier::cal_elems_length(self, elems)
    }

    fn actual_nodes(&self) -> Vec<NodeId> {
        // R-30: previously forwarded to the deleted intrinsic actual_nodes; read unit directly.
        self.unit.actual_nodes().to_vec()
    }
}
