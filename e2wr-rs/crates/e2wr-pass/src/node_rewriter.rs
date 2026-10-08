//! U9: the live subset of NodeRewriter + InstsReplacement (shared by M10 block shrinking and M11
//! FinalPolish; the interface was fixed first per the frozen-interface principle).
//!
//! Mirrors the live subset of Python `ReduceUtil/RewritingUtil/NodeRewriter.py`
//! (try_replace_nodes / try_replace_insts_in_one_func /
//! _try_apply_replacement / get_insts_in_node_lists /
//! get_ori_node_list_scope / set_best_path) and
//! `RewritingUtil/InstsReplacement.py` (block type renormalization + type-section appends).
//!
//! Semantic notes:
//! - probes start from the **current snapshot** (NodeRewriter has no "phase baseline" — its difference
//!   from NodeListTrialApplier); the snapshot and syntax tree advance only on success;
//! - `InstsReplacement._get_processed_insts_and_new_types`: block instructions with type indices in the
//!   concatenation are normalized — empty type → the empty shorthand; existing same-shape → the original index;
//!   no-param one-result → the value-type shorthand; the rest appended per update_cf_insts (deduplicated in section);
//! - a validation failure chooses between "return false (treated as rejected)" and "error out", decided jointly by
//!   `check_invalid_and_return_false` (defaulting to `!debug` when None) and
//!   `force_return_false_on_invalid_case` (copied verbatim);
//! - the DEBUG-only wat dumping and prints, and the `_tmp_check_ast_update` debug assertion,
//!   are not ported (Rust replaces them with internal assertions).

use std::collections::BTreeMap;
use std::path::PathBuf;

use anyhow::{bail, Context as _, Result};
use wasmparser::ValType;

use e2wr_dd::oracle::Oracle;
use e2wr_ir::ast::{Ast, NodeId};
use e2wr_ir::module::FuncType;
use e2wr_ir::mutation::{
    apply_mutation_and_encode, func_def, type_def, DefEdit, Mutation,
};
use e2wr_ir::snapshot::{SectionKind, Snapshot};
use e2wr_ir::types::FTy;
use e2wr_ir::Inst;

/// Python `NodeRewriter` (live subset). Python's `debug` member is not ported:
/// its only consumption (the `check_invalid_and_return_false = not debug` default)
/// is resolved at the constructing caller (R-5).
pub struct NodeRewriter {
    force_return_false_on_invalid_case: bool,
    /// The current snapshot (advancing after each successful probe).
    snapshot: Snapshot,
    ast: Ast,
    oracle: Oracle,
    tmp_used_path: PathBuf,
    /// Python NodeRewriter.best_path.
    best_path: Option<PathBuf>,
    /// Observation counts (behavior-neutral): total probes / invalid artifacts intercepted.
    pub trial_count: u64,
    pub invalid_count: u64,
}

impl NodeRewriter {
    pub fn new(
        snapshot: Snapshot,
        oracle: Oracle,
        tmp_used_path: PathBuf,
        force_return_false_on_invalid_case: bool,
    ) -> Result<NodeRewriter> {
        let ast = Ast::from_module(snapshot.try_module()?)?;
        Ok(Self::new_with_state(
            snapshot,
            ast,
            oracle,
            tmp_used_path,
            force_return_false_on_invalid_case,
        ))
    }

    /// U10: injects an existing syntax tree (a clone of the pass tree), keeping the task's NodeId coordinate system
    /// (same rationale as NodeListTrialApplier::new_with_ast).
    pub fn new_with_state(
        snapshot: Snapshot,
        ast: Ast,
        oracle: Oracle,
        tmp_used_path: PathBuf,
        force_return_false_on_invalid_case: bool,
    ) -> NodeRewriter {
        NodeRewriter {
            force_return_false_on_invalid_case,
            snapshot,
            ast,
            oracle,
            tmp_used_path,
            best_path: None,
            trial_count: 0,
            invalid_count: 0,
        }
    }

    pub fn ast(&self) -> &Ast {
        &self.ast
    }

    pub fn snapshot(&self) -> &Snapshot {
        &self.snapshot
    }

    pub fn set_best_path(&mut self, path: PathBuf) {
        self.best_path = Some(path);
    }

    /// Takes back the final state.
    pub fn into_state(self) -> (Snapshot, Ast) {
        (self.snapshot, self.ast)
    }

    /// Python `try_replace_nodes`.
    ///
    /// `check_invalid_and_return_false`: defaults to `!debug` when None (copied verbatim).
    /// `update_cf_insts`: whether the concatenation's block type normalization may append to the type section.
    #[allow(clippy::too_many_arguments)]
    pub fn try_replace_nodes(
        &mut self,
        ori_nodes: &[NodeId],
        new_nodes: &[NodeId],
        onlyless_inst: bool,
        check_invalid_and_return_false: bool,
        update_cf_insts: bool,
    ) -> Result<bool> {
        // Python get_ori_node_list_scope: each node's scope must be contiguous and adjacent.
        let (func_idx, start, end) = self.ori_scope(ori_nodes)?;
        // Python get_insts_in_node_lists: the new nodes' instructions concatenated in order.
        let mut new_node_insts: Vec<Inst> = Vec::new();
        for n in new_nodes {
            new_node_insts.extend(self.ast.get_insts(*n));
        }
        let ori_len = (end - start) as usize;
        let new_len = new_node_insts.len();
        if onlyless_inst && new_len >= ori_len {
            return Ok(false);
        }

        // R-31: a same-named convenience version without ex (always binding the empty synthetic block table)
        // was deleted; the empty table is passed directly here to the ex version (the same semantics
        // as the Python same-named function's syn_block_types=None).
        let result = self.try_replace_insts_in_one_func_ex(
            func_idx,
            &[(start, end)],
            &[new_node_insts],
            check_invalid_and_return_false,
            update_cf_insts,
            &BTreeMap::new(),
        )?;

        if result {
            self.apply_node_replacement(ori_nodes, new_nodes)?;
            self.ast.update_loc_info(func_idx as usize);
        }
        Ok(result)
    }

    /// Python `try_replace_insts_in_one_func` (multi-segment replacement within one function,
    /// concatenated in descending order into one big interval through `_try_apply_replacement`).
    /// `synthetic`: the synthetic table of each segment's head/tail block types (Python's
    /// syn_block_types; the None semantics = passing an empty table; since R-31 the "always empty"
    /// no-ex convenience version is deleted — see the call-site comment above).
    #[allow(clippy::too_many_arguments)]
    pub fn try_replace_insts_in_one_func_ex(
        &mut self,
        func_idx: u32,
        insts_scopes_to_replace: &[(u32, u32)],
        new_insts: &[Vec<Inst>],
        check_invalid_and_return_false: bool,
        update_cf_insts: bool,
        synthetic: &BTreeMap<usize, (Vec<ValType>, Vec<ValType>)>,
    ) -> Result<bool> {
        if insts_scopes_to_replace.is_empty() {
            return Ok(false);
        }
        assert_eq!(
            insts_scopes_to_replace.len(),
            new_insts.len(),
            "scope/new-insts count mismatch"
        );

        let min_start_idx = insts_scopes_to_replace.iter().map(|s| s.0).min().unwrap();
        let max_end_idx = insts_scopes_to_replace.iter().map(|s| s.1).max().unwrap();

        let func = &self.snapshot.module().defined_funcs[func_idx as usize];
        let mut modified_is0: Vec<Inst> =
            func.insts[min_start_idx as usize..max_end_idx as usize].to_vec();

        let mut pairs: Vec<(&(u32, u32), &Vec<Inst>)> = insts_scopes_to_replace
            .iter()
            .zip(new_insts.iter())
            .collect();
        pairs.sort_by_key(|(scope, _)| std::cmp::Reverse(scope.0));

        for (scope, new_inst_list) in pairs {
            let relative_start = (scope.0 - min_start_idx) as usize;
            let relative_end = (scope.1 - min_start_idx) as usize;
            modified_is0.splice(
                relative_start..relative_end,
                new_inst_list.iter().cloned(),
            );
        }

        // Python InstsReplacement: block type normalization + type appends.
        let module = self.snapshot.module();
        let (processed_insts, new_types) =
            process_insts_blocktypes(module, &modified_is0, update_cf_insts, synthetic)?;

        self.trial_count += 1;
        let mut new_func = func.clone();
        new_func.insts.splice(
            min_start_idx as usize..max_end_idx as usize,
            processed_insts,
        );
        let repl = func_def(&new_func)?;
        let mut batch = vec![Mutation::definitions(
            SectionKind::Code,
            vec![DefEdit::replace_one(func_idx, repl)],
        )];
        if !new_types.is_empty() {
            let base = module.types.len() as u32;
            let mut defs = Vec::with_capacity(new_types.len());
            for ty in &new_types {
                defs.push(type_def(ty)?);
            }
            batch.push(Mutation::definitions(
                SectionKind::Type,
                vec![DefEdit { range: base..base, repl: defs }],
            ));
        }

        let new_snap = match apply_mutation_and_encode(
            &self.snapshot,
            &batch,
            &self.tmp_used_path,
        ) {
            Ok(s) => s,
            Err(e) => return self.handle_invalid(check_invalid_and_return_false, e),
        };
        let bytes = std::fs::read(&self.tmp_used_path).context("read back trial wasm")?;
        if !crate::instseq::trial::validate_wasm_bytes(&bytes) {
            return self.handle_invalid(
                check_invalid_and_return_false,
                anyhow::anyhow!("wasm validation failed"),
            );
        }
        if !self.oracle.check(&self.tmp_used_path)? {
            return Ok(false);
        }

        // Python replacement.apply() + MPMApplier commit: the snapshot advances to this encoding.
        self.snapshot = new_snap;
        if let Some(bp) = self.best_path.clone() {
            std::fs::copy(&self.tmp_used_path, bp).context("commit to best path")?;
        }
        Ok(true)
    }

    fn handle_invalid(
        &mut self,
        check_invalid_and_return_false: bool,
        e: anyhow::Error,
    ) -> Result<bool> {
        self.invalid_count += 1;
        // Python: not check_invalid and not force_return_false → raise.
        if check_invalid_and_return_false || self.force_return_false_on_invalid_case {
            return Ok(false);
        }
        bail!("invalid trial wasm: {e:#}")
    }

    /// Python `_get_new_node_list_from_insts_and_replace_old_node_list`
    /// (the node-level replacement entry of ShrinkBlock/FinalPolish).
    ///
    /// Structural equivalence with Python: Python first builds the tree via insts2AST then
    /// try_replace_nodes (fetching the same instruction sequence from the new tree's nodes for interval replacement);
    /// Rust goes straight to interval replacement on the instruction sequence and builds the tree on the current module
    /// (type appends included) **only after success** for the node replacement — the tree input is the same instruction sequence as Python's;
    /// on failure no dead tree nodes are created (a smaller arena; behaviorally equivalent).
    /// `synthetic`: synthetic block types (sequence position → params/results), corresponding to Python's
    /// unresolved `Blocktype(funcType object)` form.
    #[allow(clippy::too_many_arguments)]
    pub fn try_replace_block_with_insts(
        &mut self,
        target: NodeId,
        insts: &[Inst],
        expected_ty: FTy,
        onlyless_inst: bool,
        check_invalid_and_return_false: bool,
        update_cf_insts: bool,
        synthetic: &BTreeMap<usize, (Vec<ValType>, Vec<ValType>)>,
    ) -> Result<(bool, Vec<NodeId>)> {
        let func_idx = self.ast.node(target).loc.func_idx;
        let (_, start, end) = self.ori_scope(std::slice::from_ref(&target))?;
        if onlyless_inst && insts.len() >= (end - start) as usize {
            return Ok((false, Vec::new()));
        }
        let ok = self.try_replace_insts_in_one_func_ex(
            func_idx,
            &[(start, end)],
            &[insts.to_vec()],
            check_invalid_and_return_false,
            update_cf_insts,
            synthetic,
        )?;
        if !ok {
            return Ok((false, Vec::new()));
        }
        let new_nodes = {
            let NodeRewriter { ast, snapshot, .. } = self;
            let module = snapshot.module();
            let (_root, new_nodes) =
                ast.append_insts_tree(module, insts, expected_ty, func_idx)?;
            new_nodes
        };
        self.apply_node_replacement(std::slice::from_ref(&target), &new_nodes)?;
        self.ast.update_loc_info(func_idx as usize);
        Ok((true, new_nodes))
    }

    /// Python `get_ori_node_list_scope`: the adjacency assertion + the big interval.
    fn ori_scope(&self, ori_nodes: &[NodeId]) -> Result<(u32, u32, u32)> {
        let mut scopes: Vec<(u32, u32, u32)> = Vec::with_capacity(ori_nodes.len());
        for n in ori_nodes {
            let node = self.ast.node(*n);
            let start = node.loc.inst_idx;
            let end = start + self.ast.get_length(*n);
            scopes.push((node.loc.func_idx, start, end));
        }
        for w in scopes.windows(2) {
            // Python asserts front-back adjacency (end == next start).
            assert_eq!(w[0].2, w[1].1, "ori nodes are not consecutive");
        }
        Ok((
            scopes[0].0,
            scopes[0].1,
            scopes[scopes.len() - 1].2,
        ))
    }

    /// Python `NodeReplacement.apply` (dispatched by the ori head node's shape).
    fn apply_node_replacement(&mut self, ori_nodes: &[NodeId], new_nodes: &[NodeId]) -> Result<()> {
        let first_ori = ori_nodes[0];
        use e2wr_ir::ast::NodeKind;
        let ori_role = if let NodeKind::List { role, .. } = &self.ast.node(first_ori).kind {
            Some(*role)
        } else {
            None
        };
        let Some(role) = ori_role else {
            // Non-list node: whole-segment replacement inside the parent list (Python
            // replace_split_with_new_sub_nodes).
            let parent = self
                .ast
                .node(first_ori)
                .parent
                .context("block/insts node has no parent")?;
            let children = self.ast.sub_nodes(parent);
            let start = children
                .iter()
                .position(|c| *c == first_ori)
                .context("ori node no longer in parent list")?;
            self.ast
                .replace_split_with_new_sub_nodes(parent, start, start + ori_nodes.len(), new_nodes.to_vec());
            return Ok(());
        };

        // Python: a NodeList-shaped ori requires a unique same-shape new node, set by the parent's shape.
        if new_nodes.len() != 1 {
            bail!("NodeList replacement expects exactly one new node");
        }
        let new_node = new_nodes[0];
        // Same-shaping (Python rebuilds with the original node class; Rust adjusts the list role).
        let new_is_list = matches!(self.ast.node(new_node).kind, NodeKind::List { .. });
        if !new_is_list {
            bail!("NodeList replacement expects a list node");
        }
        let new_role = if let NodeKind::List { role, .. } = &self.ast.node(new_node).kind {
            *role
        } else {
            unreachable!()
        };
        if new_role != role {
            if let NodeKind::List { role: r, .. } = &mut self.ast.node_mut(new_node).kind {
                *r = role;
            }
        }
        let parent = self
            .ast
            .node(first_ori)
            .parent
            .context("list node has no parent")?;
        let parent_kind = match &self.ast.node(parent).kind {
            NodeKind::Block { .. } | NodeKind::Loop { .. } => 0u8,
            NodeKind::If { .. } => 1u8,
            _ => bail!("unsupported parent for NodeList replacement"),
        };
        match parent_kind {
            0 => self.ast.set_block_body(parent, new_node),
            _ => {
                if role == e2wr_ir::ast::ListRole::Else {
                    self.ast.set_if_else(parent, new_node);
                } else {
                    self.ast.set_if_then(parent, new_node);
                }
            }
        }
        Ok(())
    }
}

/// Python `InstsReplacement._get_processed_insts_and_new_types`:
/// Block type normalization of the concatenation. Only the type-index form participates in resolution (the empty and one-result
/// shorthands are kept verbatim — Python passes straight through when init_data is bool/str).
///
/// `synthetic`: the synthetic block types recorded while building the candidate sequence (position → params/results,
/// corresponding to Python's `Blocktype(funcType object)` entering the same resolution chain via
/// `concrete_type`). Resolution priority copied verbatim: empty type → empty shorthand; existing same-shape → original index;
/// no-param one-result → value-type shorthand; the rest appended per update_cf_insts with in-section dedup.
pub fn process_insts_blocktypes(
    module: &e2wr_ir::module::Module,
    insts: &[Inst],
    update_cf_insts: bool,
    synthetic: &BTreeMap<usize, (Vec<ValType>, Vec<ValType>)>,
) -> Result<(Vec<Inst>, Vec<FuncType>)> {
    let mut new_insts: Vec<Inst> = Vec::with_capacity(insts.len());
    let mut new_types: Vec<FuncType> = Vec::new();
    let type_num = module.types.len() as u32;

    let resolve = |params: &[ValType],
                   results: &[ValType],
                   new_types: &mut Vec<FuncType>|
     -> Result<wasmparser::BlockType> {
        if params.is_empty() && results.is_empty() {
            return Ok(wasmparser::BlockType::Empty);
        }
        if let Some(j) = module
            .types
            .iter()
            .position(|t| t.params == params && t.results == results)
        {
            return Ok(wasmparser::BlockType::FuncType(j as u32));
        }
        if params.is_empty() && results.len() == 1 {
            return Ok(wasmparser::BlockType::Type(results[0]));
        }
        if !update_cf_insts {
            bail!("Cannot handle blocktype when not updating cf insts");
        }
        if let Some(k) = new_types
            .iter()
            .position(|t| t.params == params && t.results == results)
        {
            return Ok(wasmparser::BlockType::FuncType(type_num + k as u32));
        }
        new_types.push(FuncType {
            params: params.to_vec(),
            results: results.to_vec(),
        });
        Ok(wasmparser::BlockType::FuncType(
            type_num + new_types.len() as u32 - 1,
        ))
    };

    for (pos, inst) in insts.iter().enumerate() {
        match inst {
            Inst::Block { blockty } => {
                let bt = resolve_at(pos, *blockty, module, synthetic, &resolve, &mut new_types)?;
                new_insts.push(Inst::Block { blockty: bt });
            }
            Inst::Loop { blockty } => {
                let bt = resolve_at(pos, *blockty, module, synthetic, &resolve, &mut new_types)?;
                new_insts.push(Inst::Loop { blockty: bt });
            }
            Inst::If { blockty } => {
                let bt = resolve_at(pos, *blockty, module, synthetic, &resolve, &mut new_types)?;
                new_insts.push(Inst::If { blockty: bt });
            }
            other => new_insts.push(other.clone()),
        }
    }
    Ok((new_insts, new_types))
}

fn resolve_at(
    pos: usize,
    blockty: wasmparser::BlockType,
    module: &e2wr_ir::module::Module,
    synthetic: &BTreeMap<usize, (Vec<ValType>, Vec<ValType>)>,
    resolve: &impl Fn(&[ValType], &[ValType], &mut Vec<FuncType>) -> Result<wasmparser::BlockType>,
    new_types: &mut Vec<FuncType>,
) -> Result<wasmparser::BlockType> {
    if let Some((params, results)) = synthetic.get(&pos) {
        return resolve(params, results, new_types);
    }
    match blockty {
        wasmparser::BlockType::Empty => Ok(wasmparser::BlockType::Empty),
        wasmparser::BlockType::Type(_) => Ok(blockty),
        wasmparser::BlockType::FuncType(idx) => {
            // An existing index necessarily hits "existing same-shape" on the resolution chain (reflexivity of concrete types);
            // the result can only be the empty shorthand/original index/one-result shorthand, never an append (as in Python).
            let ty = module
                .types
                .get(idx as usize)
                .context("blocktype index out of range")?;
            let mut scratch: Vec<FuncType> = Vec::new();
            resolve(&ty.params, &ty.results, &mut scratch)
        }
    }
}

/// R-21: the ValTy → wasm ValType mapping (two verbatim-identical copies — final_polish.rs's `valty_to_wp`
/// and this file's `conv` closure — merged).
pub(crate) fn valty_to_wp(t: &e2wr_ir::types::ValTy) -> wasmparser::ValType {
    use wasmparser::ValType as V;
    match t {
        e2wr_ir::types::ValTy::I32 => V::I32,
        e2wr_ir::types::ValTy::I64 => V::I64,
        e2wr_ir::types::ValTy::F32 => V::F32,
        e2wr_ir::types::ValTy::F64 => V::F64,
        e2wr_ir::types::ValTy::V128 => V::V128,
        e2wr_ir::types::ValTy::Funcref => V::FUNCREF,
        e2wr_ir::types::ValTy::Externref => V::EXTERNREF,
    }
}

/// Python `get_insts_padding_pos_and_list_end`: the
/// `GenSpecificType(expected_type).get_insts_for_replace()` =
/// `padding_input_type_naive(params, results)` (extra params dropped, missing results
/// padded with constants; constant values random, same distribution without chasing the sequence, D-2; 'any' falls to an i32 constant per
/// the actual behavior of Python's `get_inst_by_require_ty_const_n`).
pub fn gen_specific_type_insts(
    params: &[e2wr_ir::types::ValTy],
    results: &[e2wr_ir::types::ValTy],
    rng: &mut impl rand::Rng,
) -> Result<Vec<Inst>> {
    let conv = valty_to_wp;
    let common = params.iter().zip(results).take_while(|(a, b)| a == b).count();
    let mut insts = vec![Inst::Drop; params.len() - common];
    for ty in &results[common..] {
        insts.push(crate::remap::const_inst(&conv(ty), rng)?);
    }
    Ok(insts)
}
