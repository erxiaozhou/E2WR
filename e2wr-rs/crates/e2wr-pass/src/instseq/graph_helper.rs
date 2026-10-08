//! Graph helper layer: the live subset of Python `V6V1GraphHelper.py` (post round-4 cleanup).
//!
//! Parity notes:
//! - `SGBuilder._count` is a Python **class-level** global counter → Rust hangs an
//!   `Rc<Cell<u32>>` off [`GraphHelper`], shared by all builders (identical numbering semantics:
//!   monotonically increasing without duplicates within one GraphHelper lifetime).
//! - Snapshot cache: Python lazily caches raw/non_cf; Rust precomputes once after graph building
//!   (the relations are immutable after building; content-equivalent).
//! - Python iterates `set(VopWT)`/`set(int)` in several places (order unstable with object addresses);
//!   Rust always iterates ascending; the partition/membership results agree, order differences are allowed
//!   (affecting only ProbDD candidate order, itself indeterminate in Python).
//! - B-7-deleted items (op2gen_origin, get_elem_symbol, the StackSnapshot type-view
//!   property families, etc.) are not ported.

use std::cell::Cell;
use std::collections::{BTreeMap, BTreeSet, HashMap};
use std::rc::Rc;

use e2wr_ir::types::{Context, ValTy};
use e2wr_ir::Inst;

use super::elem::{ElemRef, OneElem, OTy};
use super::vop::{
    build_operand_graph, EdgeType, ElemTyInput, InstKey, OperandInstGraph, VopId,
};

pub const MAX_EQ_LINK: usize = 10;

/// Python `V5_util.sort_and_get_continuous_groups`.
pub fn sort_and_get_continuous_groups(nums: BTreeSet<usize>) -> Vec<Vec<usize>> {
    let mut groups: Vec<Vec<usize>> = Vec::new();
    let mut cur: Vec<usize> = Vec::new();
    let mut last: Option<usize> = None;
    for num in nums {
        match last {
            None => cur.push(num),
            Some(l) if num == l + 1 => cur.push(num),
            Some(_) => {
                groups.push(std::mem::take(&mut cur));
                cur.push(num);
            }
        }
        last = Some(num);
    }
    if !cur.is_empty() {
        groups.push(cur);
    }
    groups
}

/// Python `get_seq_common_prefix_len` (operands compared by identity).
fn common_prefix_len_ops(a: &[VopId], b: &[VopId]) -> usize {
    a.iter().zip(b).take_while(|(x, y)| x == y).count()
}

/// Python `SGComponentWOpInfo` (after the B-7 simplification).
#[derive(Clone, Debug)]
pub struct SGComponent {
    pub elem_idxs: Vec<usize>,
    /// The following two fields are unread by production code; only the m10_graph_parity comparison test uses them
    /// (R-29 note, same as R-15's parity-test-only marking).
    pub drop_types: Vec<OTy>,
    pub comp_gen_types: Vec<OTy>,
    pub taken_op_list: Vec<VopId>,
    pub gen_op_list: Vec<VopId>,
    pub sgc_taken_ops: BTreeSet<VopId>,
    pub sgc_gen_ops: BTreeSet<VopId>,
}

/// Python `SubGraph`.
#[derive(Clone, Debug)]
pub struct SubGraph {
    pub idx: u32,
    pub components: Vec<SGComponent>,
    pub enable_internal_cancel: bool,
    pub raw_taken_ops: BTreeSet<VopId>,
    pub raw_gen_ops: BTreeSet<VopId>,
    pub ops_from_outside: BTreeSet<VopId>,
    pub ops_to_outside_non_cf: BTreeSet<VopId>,
    pub sg_elem_idxs: Vec<usize>,
    pub has_one_elem: bool,
    pub raw_external_input_ops: Vec<VopId>,
    pub final_output_ops: Vec<VopId>,
}

impl SubGraph {
    /// Python `SubGraph.__init__` (snapshots passed in as raw/non_cf slices).
    fn new(
        idx: u32,
        components: Vec<SGComponent>,
        enable_internal_cancel: bool,
        raw_init: &[VopId],
        non_cf_end: &[VopId],
        raw_end: &[VopId],
    ) -> SubGraph {
        let mut raw_taken_ops = BTreeSet::new();
        let mut raw_gen_ops = BTreeSet::new();
        for comp in &components {
            raw_taken_ops.extend(comp.sgc_taken_ops.iter().copied());
            raw_gen_ops.extend(comp.sgc_gen_ops.iter().copied());
        }
        let ops_from_outside: BTreeSet<VopId> =
            raw_taken_ops.difference(&raw_gen_ops).copied().collect();
        let ops_to_outside: BTreeSet<VopId> =
            raw_gen_ops.difference(&raw_taken_ops).copied().collect();
        let ops_to_outside_non_cf: BTreeSet<VopId> = ops_to_outside
            .intersection(&non_cf_end.iter().copied().collect())
            .copied()
            .collect();
        let mut sg_elem_idxs: Vec<usize> = Vec::new();
        for comp in &components {
            sg_elem_idxs.extend(comp.elem_idxs.iter().copied());
        }
        sg_elem_idxs.sort_unstable();
        let has_one_elem = sg_elem_idxs.len() == 1;
        let common_prefix = common_prefix_len_ops(raw_init, raw_end);
        let raw_external_input_ops: Vec<VopId> = raw_init[common_prefix..]
            .iter()
            .copied()
            .filter(|op| ops_from_outside.contains(op))
            .collect();
        let final_output_ops: Vec<VopId> = raw_end[common_prefix..]
            .iter()
            .copied()
            .filter(|op| ops_to_outside_non_cf.contains(op))
            .collect();
        SubGraph {
            idx,
            components,
            enable_internal_cancel,
            raw_taken_ops,
            raw_gen_ops,
            ops_from_outside,
            ops_to_outside_non_cf,
            sg_elem_idxs,
            has_one_elem,
            raw_external_input_ops,
            final_output_ops,
        }
    }
}

/// Python `SubGraphRepo` (items iteration = insertion order = idx ascending; the two orders coincide).
#[derive(Default)]
pub struct SubGraphRepo {
    sg_idx2sg: BTreeMap<u32, SubGraph>,
}

impl SubGraphRepo {
    pub fn new() -> SubGraphRepo {
        SubGraphRepo::default()
    }

    pub fn insert_sg(&mut self, sg: SubGraph) {
        self.sg_idx2sg.insert(sg.idx, sg);
    }

    pub fn get_sg_by_idx(&self, sg_idx: u32) -> &SubGraph {
        &self.sg_idx2sg[&sg_idx]
    }

    pub fn items(&self) -> impl Iterator<Item = (&u32, &SubGraph)> {
        self.sg_idx2sg.iter()
    }
}

/// Python `SGBuilder` (the counter shared with GraphHelper — the class-level global equivalently narrowed).
pub struct SgBuilder {
    counter: Rc<Cell<u32>>,
}

impl SgBuilder {
    pub fn new(helper: &GraphHelper) -> SgBuilder {
        SgBuilder { counter: Rc::clone(&helper.sg_counter) }
    }

    pub fn gen_sg_core(
        &self,
        helper: &GraphHelper,
        components: Vec<SGComponent>,
        enable_internal_cancel: bool,
    ) -> SubGraph {
        let mut comp_idxs: BTreeSet<usize> = BTreeSet::new();
        for c in &components {
            comp_idxs.extend(c.elem_idxs.iter().copied());
        }
        let first = *comp_idxs.iter().next().expect("non-empty components");
        let last = *comp_idxs.iter().next_back().expect("non-empty components");
        let raw_init = helper.snapshot_raw(first).to_vec();
        let raw_end = helper.snapshot_raw(last + 1).to_vec();
        let non_cf_end = helper.snapshot_non_cf(last + 1).to_vec();
        let idx = self.counter.get();
        self.counter.set(idx + 1);
        SubGraph::new(idx, components, enable_internal_cancel, &raw_init, &non_cf_end, &raw_end)
    }

    /// Python `gen_sg_component`: takes the common identity prefix of the non_cf stacks before min and after max,
    /// with taken/gen after the prefix.
    pub fn gen_sg_component(
        &self,
        helper: &GraphHelper,
        elem_idxs: &[usize],
        operand_remap: Option<&HashMap<VopId, VopId>>,
    ) -> SGComponent {
        assert!(!elem_idxs.is_empty());
        let min_idx = *elem_idxs.iter().min().expect("non-empty");
        let max_idx = *elem_idxs.iter().max().expect("non-empty");
        let ori_stack = helper.snapshot_non_cf(min_idx);
        let cur_stack = helper.snapshot_non_cf(max_idx + 1);
        let mut offset = 0;
        for (ori, new) in ori_stack.iter().zip(cur_stack) {
            if ori != new {
                break;
            }
            offset += 1;
        }
        let map_op = |op: VopId| operand_remap.and_then(|m| m.get(&op).copied()).unwrap_or(op);
        let taken_ops: Vec<VopId> = ori_stack[offset..].iter().map(|o| map_op(*o)).collect();
        let gen_ops: Vec<VopId> = cur_stack[offset..].iter().map(|o| map_op(*o)).collect();
        let drop_types: Vec<OTy> =
            taken_ops.iter().map(|op| helper.graph.vops[*op as usize].ty).collect();
        let comp_gen_types: Vec<OTy> =
            gen_ops.iter().map(|op| helper.graph.vops[*op as usize].ty).collect();
        SGComponent {
            elem_idxs: elem_idxs.to_vec(),
            drop_types,
            comp_gen_types,
            sgc_taken_ops: taken_ops.iter().copied().collect(),
            sgc_gen_ops: gen_ops.iter().copied().collect(),
            taken_op_list: taken_ops,
            gen_op_list: gen_ops,
        }
    }
}

/// Python `GraphHelper`.
pub struct GraphHelper {
    pub graph: OperandInstGraph,
    pub all_elem_num: usize,
    pub sg_in_contigous_idxs: Vec<Vec<Vec<usize>>>,
    raw_snaps: Vec<Vec<VopId>>,
    non_cf_snaps: Vec<Vec<VopId>>,
    sg_counter: Rc<Cell<u32>>,
}

impl GraphHelper {
    /// Python `GraphHelper.__init__` (internally calls `build_graph_by_elems` →
    /// element classification in [`classify_elem`]).
    pub fn new(
        elems: &[OneElem],
        ctx: Option<&Context>,
        init_stack: &[ValTy],
        end_stack: Option<&[ValTy]>,
    ) -> GraphHelper {
        let inputs: Vec<ElemTyInput> = elems.iter().map(|e| classify_elem(e, ctx)).collect();
        let graph = build_operand_graph(&inputs, init_stack, end_stack);
        let all_elem_num = elems.len();
        let seqs: Vec<Vec<usize>> = super::vop::GraphQuery::new(&graph)
            .find_subgraph_ncf_inst_sequences();
        let mut sg_in_contigous_idxs: Vec<Vec<Vec<usize>>> = seqs
            .into_iter()
            .map(|seq| sort_and_get_continuous_groups(seq.into_iter().collect()))
            .collect();
        // Uncovered elements: non-control-flow ones each form their own group.
        let covered: BTreeSet<usize> = sg_in_contigous_idxs
            .iter()
            .flat_map(|groups: &Vec<Vec<usize>>| {
                groups.iter().flat_map(|g: &Vec<usize>| g.iter().copied())
            })
            .collect();
        for (idx, elem) in elems.iter().enumerate() {
            if !covered.contains(&idx) && !elem.is_cf_related_inst() {
                sg_in_contigous_idxs.push(vec![vec![idx]]);
            }
        }
        // Precomputed snapshots (0..=all_elem_num; Python get_raw_stack_snapshot_before_elem).
        let mut raw_snaps = Vec::with_capacity(all_elem_num + 1);
        let mut non_cf_snaps = Vec::with_capacity(all_elem_num + 1);
        for boundary in 0..=all_elem_num {
            let raw: Vec<VopId> = if boundary == all_elem_num {
                graph.end_stack_ops.clone()
            } else {
                let inst = graph.numidx2_instidx[&boundary];
                graph.inst2stack_before_it[&inst].clone()
            };
            let non_cf: Vec<VopId> = raw
                .iter()
                .copied()
                .filter(|op| !graph.op_is_taken_by_any(*op))
                .collect();
            raw_snaps.push(raw);
            non_cf_snaps.push(non_cf);
        }
        GraphHelper {
            graph,
            all_elem_num,
            sg_in_contigous_idxs,
            raw_snaps,
            non_cf_snaps,
            sg_counter: Rc::new(Cell::new(0)),
        }
    }

    /// The raw stack before the boundary `elem_idx` (0..=all_elem_num) (Python
    /// `get_raw_stack_snapshot_before_elem`).
    pub fn snapshot_raw(&self, elem_idx: usize) -> &[VopId] {
        &self.raw_snaps[elem_idx]
    }

    /// The non-control-flow consuming stack before the boundary (Python `StackSnapshot.non_cf_ops`).
    pub fn snapshot_non_cf(&self, elem_idx: usize) -> &[VopId] {
        &self.non_cf_snaps[elem_idx]
    }

    /// Python `get_stack_ops_before_elem`. R-30 note: the same-named Python function
    /// is itself a forwarding shell to StackSnapshot.raw (V6V1GraphHelper.py); Rust
    /// keeps the name for comparison, with only the snapshot_raw implementation.
    pub fn stack_ops_before(&self, elem_idx: usize) -> &[VopId] {
        self.snapshot_raw(elem_idx)
    }

    pub fn get_ops_taken_by_elem(&self, elem_idx: usize) -> &[VopId] {
        let inst = self.graph.numidx2_instidx[&elem_idx];
        self.graph
            .inst_idx_to_consumed_ops
            .get(&inst)
            .map(Vec::as_slice)
            .unwrap_or(&[])
    }

    pub fn get_ops_generated_by_elem(&self, elem_idx: usize) -> &[VopId] {
        let inst = self.graph.numidx2_instidx[&elem_idx];
        self.graph
            .inst_idx_to_produced_ops
            .get(&inst)
            .map(Vec::as_slice)
            .unwrap_or(&[])
    }

    pub fn vop_ty(&self, op: VopId) -> OTy {
        self.graph.vops[op as usize].ty
    }

    /// Python `count_dependency_on_cf` (delegates to GraphQuery). R-30 note:
    /// the same-named Python function likewise forwards to GraphQuery logic (V6V1GraphHelper.py);
    /// Rust has only the GraphQuery implementation; the name is kept for comparison.
    pub fn count_dependency_on_cf(&self, elem_idx: usize) -> (usize, Vec<OTy>) {
        super::vop::GraphQuery::new(&self.graph).count_dependency_on_cf(elem_idx)
    }

    /// Python `op_is_v_produce`.
    pub fn op_is_v_produce(&self, op: VopId) -> bool {
        self.graph.producer_relation[op as usize] == Some(EdgeType::CfProduce)
    }
}

/// The element classification of Python `build_graph_by_elems` (OneElem → ElemTyInput).
pub fn classify_elem(elem: &OneElem, ctx: Option<&Context>) -> ElemTyInput {
    let i32y = || OTy::Ty(ValTy::I32);
    match &elem.elem {
        ElemRef::Inst(inst) => match inst {
            Inst::Unreachable => ElemTyInput::Unreachable,
            Inst::Return | Inst::Br { .. } | Inst::BrTable(_) => {
                let tr = e2wr_ir::types::get_inst_ty_req(inst, ctx)
                    .expect("type req for return-like inst");
                ElemTyInput::ReturnLike {
                    required: tr.ty0().params.iter().map(|t| OTy::Ty(*t)).collect(),
                }
            }
            Inst::BrIf { .. } => {
                let tr = e2wr_ir::types::get_inst_ty_req(inst, ctx)
                    .expect("type req for br_if");
                let params = &tr.ty0().params;
                ElemTyInput::BrIf {
                    required_return: params[..params.len() - 1]
                        .iter()
                        .map(|t| OTy::Ty(*t))
                        .collect(),
                }
            }
            Inst::Select => ElemTyInput::Ncnd {
                taken: vec![OTy::Any, OTy::Any, i32y()],
                gen: vec![OTy::Any],
            },
            Inst::RefIsNull => ElemTyInput::Ncnd {
                taken: vec![OTy::Any],
                gen: vec![i32y()],
            },
            _ => {
                assert!(
                    elem.is_determined_type(),
                    "elem must have determined type info"
                );
                ElemTyInput::Determined {
                    taken: elem.type_info.taken_ops().to_vec(),
                    gen: elem.type_info.gen_ops().to_vec(),
                }
            }
        },
        ElemRef::Node(_) => {
            assert!(
                elem.is_determined_type(),
                "block elem must have determined type info"
            );
            ElemTyInput::Determined {
                taken: elem.type_info.taken_ops().to_vec(),
                gen: elem.type_info.gen_ops().to_vec(),
            }
        }
    }
}

/// Python `init_subgraph_repo` / `get_init_subgraph_repo`.
pub fn get_init_subgraph_repo(helper: &GraphHelper) -> SubGraphRepo {
    let builder = SgBuilder::new(helper);
    let mut repo = SubGraphRepo::new();
    for continuous_elem_idxs in &helper.sg_in_contigous_idxs {
        let mut comps = Vec::new();
        for one in continuous_elem_idxs {
            comps.push(builder.gen_sg_component(helper, one, None));
        }
        let sg = builder.gen_sg_core(helper, comps, true);
        repo.insert_sg(sg);
    }
    repo
}

/// Python `_gen_subgraph_from_elem_idxs`.
pub fn gen_subgraph_from_elem_idxs(
    builder: &SgBuilder,
    helper: &GraphHelper,
    elem_idxs: &[usize],
    operand_remap: Option<&HashMap<VopId, VopId>>,
    enable_internal_cancel: bool,
) -> SubGraph {
    assert!(!elem_idxs.is_empty());
    let set: BTreeSet<usize> = elem_idxs.iter().copied().collect();
    let components: Vec<SGComponent> = sort_and_get_continuous_groups(set)
        .into_iter()
        .map(|seq| builder.gen_sg_component(helper, &seq, operand_remap))
        .collect();
    builder.gen_sg_core(helper, components, enable_internal_cancel)
}

/// Python `sg_is_cf_only`.
pub fn sg_is_cf_only(sg: &SubGraph, elems: &[OneElem]) -> bool {
    let idxs = &sg.sg_elem_idxs;
    if idxs.len() != 1 {
        return false;
    }
    elems[idxs[0]].is_cf_related_inst()
}

/// Python `SubGraphSplitter` (keeping only the `_split_sg_by_last_inst_and_local_dd`
/// split path; the approach-1 dead branch deleted).
pub struct SubGraphSplitter {
    sg_builder: SgBuilder,
}

impl SubGraphSplitter {
    pub fn new(helper: &GraphHelper) -> SubGraphSplitter {
        SubGraphSplitter { sg_builder: SgBuilder::new(helper) }
    }

    pub fn split_sg_by_last_inst_and_local_dd(
        &self,
        helper: &GraphHelper,
        idxs_in_sg: &[usize],
    ) -> Vec<SubGraph> {
        assert!(!idxs_in_sg.is_empty());
        let sorted_idxs: Vec<usize> = {
            let mut v = idxs_in_sg.to_vec();
            v.sort_unstable();
            v
        };
        if sorted_idxs.len() == 1 {
            return vec![gen_subgraph_from_elem_idxs(
                &self.sg_builder,
                helper,
                &sorted_idxs,
                None,
                true,
            )];
        }
        let last_elem_idx = sorted_idxs[sorted_idxs.len() - 1];
        let prefix_elem_idxs = &sorted_idxs[..sorted_idxs.len() - 1];
        let prefix_allowed: BTreeSet<usize> = prefix_elem_idxs.iter().copied().collect();

        let graph = &helper.graph;
        // Adjacency: element ↔ the producers of its consumed operands / the consumers of its produced operands (within the prefix).
        let mut adjacency: BTreeMap<usize, BTreeSet<usize>> = BTreeMap::new();
        for idx in prefix_allowed.iter().copied() {
            adjacency.entry(idx).or_default();
        }
        for &elem_idx in prefix_elem_idxs {
            let inst = graph.numidx2_instidx[&elem_idx];
            for op in graph.inst_idx_to_consumed_ops.get(&inst).map(Vec::as_slice).unwrap_or(&[]) {
                if let Some(InstKey::Concrete { idx: p, .. }) = graph.producer[*op as usize] {
                    if prefix_allowed.contains(&p) {
                        adjacency.get_mut(&elem_idx).unwrap().insert(p);
                        adjacency.get_mut(&p).unwrap().insert(elem_idx);
                    }
                }
            }
            for op in graph.inst_idx_to_produced_ops.get(&inst).map(Vec::as_slice).unwrap_or(&[]) {
                if let Some(InstKey::Concrete { idx: c, .. }) = graph.consumer[*op as usize] {
                    if prefix_allowed.contains(&c) {
                        adjacency.get_mut(&elem_idx).unwrap().insert(c);
                        adjacency.get_mut(&c).unwrap().insert(elem_idx);
                    }
                }
            }
        }

        let mut prefix_subgraphs: Vec<SubGraph> = Vec::new();
        let mut visited: BTreeSet<usize> = BTreeSet::new();
        let mut covered: BTreeSet<usize> = BTreeSet::new();
        // Python: start points ascending; stack expansion uses sorted(adj - visited, reverse=True).
        for &start_idx in prefix_elem_idxs {
            if visited.contains(&start_idx) {
                continue;
            }
            let mut component: BTreeSet<usize> = BTreeSet::new();
            let mut stack = vec![start_idx];
            while let Some(cur) = stack.pop() {
                if visited.contains(&cur) {
                    continue;
                }
                visited.insert(cur);
                component.insert(cur);
                let neighbors: Vec<usize> = adjacency[&cur]
                    .difference(&visited)
                    .copied()
                    .collect::<Vec<_>>()
                    .into_iter()
                    .rev()
                    .collect();
                stack.extend(neighbors);
            }
            if component.len() > 1 {
                let idxs: Vec<usize> = component.iter().copied().collect();
                prefix_subgraphs.push(gen_subgraph_from_elem_idxs(
                    &self.sg_builder,
                    helper,
                    &idxs,
                    None,
                    true,
                ));
                covered.extend(component);
            }
        }
        for idx in prefix_allowed.difference(&covered).copied().collect::<Vec<_>>() {
            prefix_subgraphs.push(gen_subgraph_from_elem_idxs(
                &self.sg_builder,
                helper,
                &[idx],
                None,
                true,
            ));
        }
        prefix_subgraphs.push(gen_subgraph_from_elem_idxs(
            &self.sg_builder,
            helper,
            &[last_elem_idx],
            None,
            true,
        ));
        prefix_subgraphs
    }

    /// Python `replace_a_graph`.
    pub fn replace_a_graph(
        &self,
        helper: &GraphHelper,
        raw_sg_idx: u32,
        repo: &mut SubGraphRepo,
    ) -> Vec<SubGraph> {
        let raw_sg = repo.get_sg_by_idx(raw_sg_idx);
        let raw_elem_idxs = raw_sg.sg_elem_idxs.clone();
        let new_sgs = self.gen_new_sgs_ori(helper, &raw_elem_idxs);
        for sg in &new_sgs {
            repo.insert_sg(sg.clone());
        }
        new_sgs
    }

    /// Python `gen_new_sgs_ori` (single elements rebuilt as-is; otherwise local-dependency splitting).
    pub fn gen_new_sgs_ori(&self, helper: &GraphHelper, raw_sg_elem_idxs: &[usize]) -> Vec<SubGraph> {
        let idxs_in_sg = {
            let mut v = raw_sg_elem_idxs.to_vec();
            v.sort_unstable();
            v
        };
        if idxs_in_sg.len() == 1 {
            let component =
                self.sg_builder.gen_sg_component(helper, &[idxs_in_sg[0]], None);
            let sg = self.sg_builder.gen_sg_core(helper, vec![component], true);
            return vec![sg];
        }
        self.split_sg_by_last_inst_and_local_dd(helper, &idxs_in_sg)
    }
}
