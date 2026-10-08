//! Value-operand graph: the live subset of Python `ElemOperandMappingV2.py` (post round-4 cleanup).
//!
//! Key equivalence decisions:
//! - VopWT compares by object identity (`__eq__`/`__hash__` commented out) → the arena index
//!   [`VopId`] (D-section ruling); OpWithTypeFactory.cur_idx's monotonically increasing **value** has
//!   no behavioral consumer after the cleanup; a per-graph counter suffices.
//! - `ExistingUnknownInstIdx`'s global counter → per-graph [`InstKey::Outside`].
//! - `force_by_taken_ops` always True (`is_debug_env()` always False): on a stack-type/taken-type
//!   mismatch it **forces** the stack operand type instead of erroring — actual behavior copied.
//! - `init_is_given_any_type` always False; not ported.
//! - Python's `find_subgraph_ncf_inst_sequences` iterates a `set(VopWT)`, the order unstable with
//!   object addresses (it is itself order-indeterminate); Rust takes VopId ascending (= creation order);
//!   the partition results agree, the sequence-order difference is an allowed difference (recorded in the migration notes).

use std::collections::HashMap;

use super::elem::OTy;
use e2wr_ir::types::ValTy;

/// Element type kinds (Python `OneElemTypeInfoKind`; OUTSIDE is expressed by InstKey::Outside).
#[derive(Clone, Copy, Debug, PartialEq, Eq, Hash, PartialOrd, Ord)]
pub enum ElemKind {
    Common,
    Ncnd,
    Unreachable,
    ReturnLike,
    BrIf,
}

/// Instruction identity (the Python `InstIdx` family; `VInstIdx`/`VInstIdxFactory` have zero callers,
/// not ported).
#[derive(Clone, Copy, Debug, PartialEq, Eq, Hash, PartialOrd, Ord)]
pub enum InstKey {
    /// `CInstIdx(idx, kind)`: an element in the concrete instruction stream; all is_inside_concrete.
    Concrete { idx: usize, kind: ElemKind },
    /// `ExistingUnknownInstIdx`: an outside-graph producer/consumer placeholder, unique identity per instance.
    Outside(u32),
}

impl InstKey {
    pub fn is_inside_concrete_inst(&self) -> bool {
        matches!(self, InstKey::Concrete { .. })
    }

    pub fn is_not_sure_type_inst(&self) -> bool {
        match self {
            InstKey::Concrete { kind, .. } => *kind != ElemKind::Common,
            InstKey::Outside(_) => true,
        }
    }
}

#[derive(Clone, Copy, Debug, PartialEq, Eq, Hash)]
pub enum EdgeType {
    Produce,
    Consume,
    CfProduce,
    CfConsume,
}

impl EdgeType {
    fn is_produce_side(&self) -> bool {
        matches!(self, EdgeType::Produce | EdgeType::CfProduce)
    }
}

pub type VopId = u32;

/// Python `VopWT` (an arena element; identity = index).
/// R-29: Python's depth_ref field was deleted — no readers repo-wide (even in Python only
/// `__repr__` printing used it); both graph-building chains maintained ref_depth only to write it;
/// VopFactory was removed too (R-30): push_vop's debug_assert already proved its
/// count always equals vops.len(); one fact is no longer tracked twice.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Vop {
    pub id: VopId,
    pub ty: OTy,
}

/// Python `OperandInstGraphV2` (all vop-side maps indexed by VopId;
/// instruction-side maps by InstKey).
#[derive(Default)]
pub struct OperandInstGraph {
    pub vops: Vec<Vop>,
    pub producer: Vec<Option<InstKey>>,
    pub consumer: Vec<Option<InstKey>>,
    pub producer_relation: Vec<Option<EdgeType>>,
    pub consumer_relation: Vec<Option<EdgeType>>,
    pub inst_idx_to_produced_ops: HashMap<InstKey, Vec<VopId>>,
    pub inst_idx_to_consumed_ops: HashMap<InstKey, Vec<VopId>>,
    pub numidx2_instidx: HashMap<usize, InstKey>,
    pub inst2stack_before_it: HashMap<InstKey, Vec<VopId>>,
    pub end_stack_ops: Vec<VopId>,
    outside_counter: u32,
    /// Operand count at graph-build completion; later allocations are detached operands (the equivalence
    /// subgraph remap's shared operands — Python generates them via a separate factory, never entering the graph).
    pub attached_vop_num: usize,
}

impl OperandInstGraph {
    pub fn new() -> OperandInstGraph {
        OperandInstGraph::default()
    }

    /// Allocates and registers an operand (id = vops.len(), identical to the old VopFactory count).
    fn push_vop(&mut self, ty: OTy) -> VopId {
        let id = self.vops.len() as VopId;
        self.vops.push(Vop { id, ty });
        self.producer.push(None);
        self.consumer.push(None);
        self.producer_relation.push(None);
        self.consumer_relation.push(None);
        id
    }

    /// A detached operand (Python's equivalence-subgraph remap `OpWithTypeFactory().gen_one_vopwt`,
    /// no edges; never participates in validate/connected components).
    pub fn alloc_detached(&mut self, ty: OTy) -> VopId {
        self.push_vop(ty)
    }

    /// Python `add_edge`: the produce side asserts a unique producer, the consume side a unique consumer.
    pub fn add_edge(&mut self, vop: VopId, inst: InstKey, edge_type: EdgeType) {
        let v = vop as usize;
        if edge_type.is_produce_side() {
            assert!(
                self.producer[v].is_none(),
                "vop {vop} already produced by {:?}, cannot be produced by {inst:?}",
                self.producer[v]
            );
            self.producer[v] = Some(inst);
            self.producer_relation[v] = Some(edge_type);
            self.inst_idx_to_produced_ops.entry(inst).or_default().push(vop);
        } else {
            assert!(
                self.consumer[v].is_none(),
                "vop {vop} already consumed by {:?}, cannot be consumed by {inst:?}",
                self.consumer[v]
            );
            self.consumer[v] = Some(inst);
            self.consumer_relation[v] = Some(edge_type);
            self.inst_idx_to_consumed_ops.entry(inst).or_default().push(vop);
        }
    }

    fn add_edge_produced_by_outside_graph(&mut self, vop: VopId) {
        assert!(self.producer[vop as usize].is_none());
        let inst = InstKey::Outside(self.outside_counter);
        self.outside_counter += 1;
        self.add_edge(vop, inst, EdgeType::Produce);
    }

    fn add_edge_consumed_by_outside_graph(&mut self, vop: VopId) {
        assert!(self.consumer[vop as usize].is_none());
        let inst = InstKey::Outside(self.outside_counter);
        self.outside_counter += 1;
        self.add_edge(vop, inst, EdgeType::Consume);
    }

    fn add_edge_for_control_flow_produce(&mut self, vop: VopId, inst: InstKey) {
        self.add_edge(vop, inst, EdgeType::CfProduce);
    }

    fn add_edge_for_control_flow_consume(&mut self, vop: VopId, inst: InstKey) {
        self.add_edge(vop, inst, EdgeType::CfConsume);
    }

    /// Python `_validate` (a debug assertion; checks only operands attached during graph building).
    pub fn validate(&self) {
        for v in 0..self.attached_vop_num {
            assert!(
                self.producer[v].is_some(),
                "vop {v} has no producer"
            );
            assert!(
                self.consumer[v].is_some(),
                "vop {v} has no consumer"
            );
        }
    }

    /// Python `op_is_taken_by_any`.
    pub fn op_is_taken_by_any(&self, vop: VopId) -> bool {
        self.consumer_relation[vop as usize] == Some(EdgeType::CfConsume)
    }

    /// Python `get_stack_type_before_elem` (type-string view).
    pub fn stack_types_before(&self, elem_idx: usize) -> Vec<OTy> {
        let inst = self.numidx2_instidx[&elem_idx];
        self.inst2stack_before_it[&inst].iter().map(|v| self.vops[*v as usize].ty).collect()
    }
}

/// Graph-build input (the `OneElemTypeInfo` family of Python `build_operand_inst_mapping_naive_v2_consider_special_elem`).
/// OneElemTypeInfo family).
pub enum ElemTyInput {
    Unreachable,
    /// return/br/br_table: the required param types.
    ReturnLike { required: Vec<OTy> },
    /// br_if: the required types besides the i32 condition.
    BrIf { required_return: Vec<OTy> },
    /// select / ref.is_null.
    Ncnd { taken: Vec<OTy>, gen: Vec<OTy> },
    /// Other determined-type elements.
    Determined { taken: Vec<OTy>, gen: Vec<OTy> },
}

/// Python `build_operand_inst_mapping_naive_v2_consider_special_elem`
/// (`init_is_given_any_type=False` fixed).
pub fn build_operand_graph(
    given: &[ElemTyInput],
    init_stack: &[ValTy],
    end_stack: Option<&[ValTy]>,
) -> OperandInstGraph {
    let mut graph = OperandInstGraph::new();
    let mut stack: Vec<VopId> = Vec::new();

    for ty in init_stack {
        let id = graph.push_vop(OTy::Ty(*ty));
        graph.add_edge_produced_by_outside_graph(id);
        stack.push(id);
    }
    let mut unreachable_source: Option<InstKey> = None;

    for (elem_idx, info) in given.iter().enumerate() {
        let kind = match info {
            ElemTyInput::Unreachable => ElemKind::Unreachable,
            ElemTyInput::ReturnLike { .. } => ElemKind::ReturnLike,
            ElemTyInput::BrIf { .. } => ElemKind::BrIf,
            ElemTyInput::Ncnd { .. } => ElemKind::Ncnd,
            ElemTyInput::Determined { .. } => ElemKind::Common,
        };
        let cinst = InstKey::Concrete { idx: elem_idx, kind };
        graph.inst2stack_before_it.insert(cinst, stack.clone());
        graph.numidx2_instidx.insert(elem_idx, cinst);
        match info {
            ElemTyInput::Unreachable => {
                consume_stack_by_control_flow(&mut graph, &mut stack, cinst);
                unreachable_source = Some(cinst);
            }
            ElemTyInput::ReturnLike { required } => {
                process_concrete_taken(
                    unreachable_source,
                    &mut graph,
                    &mut stack,
                    cinst,
                    &mut required.clone(),
                );
                consume_stack_by_control_flow(&mut graph, &mut stack, cinst);
                unreachable_source = Some(cinst);
            }
            ElemTyInput::BrIf { required_return } => {
                let mut taken = required_return.clone();
                taken.push(OTy::Ty(ValTy::I32));
                process_concrete_taken(
                    unreachable_source,
                    &mut graph,
                    &mut stack,
                    cinst,
                    &mut taken,
                );
                build_concrete_produce(&mut graph, &mut stack, cinst, required_return.to_vec());
            }
            ElemTyInput::Ncnd { taken, gen } => {
                let mut taken = taken.clone();
                let mut gen_list: Vec<OTy> = gen.clone();
                process_concrete_taken(
                    unreachable_source,
                    &mut graph,
                    &mut stack,
                    cinst,
                    &mut taken,
                );
                // select shape refinement: result type = the value operand type (reads the refined taken).
                if gen_list.len() == 1
                    && gen_list[0] == OTy::Any
                    && taken.len() == 3
                    && taken[2] == OTy::Ty(ValTy::I32)
                {
                    if let Some(value_ty) = taken[..2].iter().copied().find(|t| *t != OTy::Any) {
                        gen_list = vec![value_ty];
                    }
                }
                build_concrete_produce(&mut graph, &mut stack, cinst, gen_list);
            }
            ElemTyInput::Determined { taken, gen } => {
                // Python _process_one_determined_elem: copies taken first (preventing refinement leakage),
                // force_by_taken_ops always True.
                process_concrete_taken(
                    unreachable_source,
                    &mut graph,
                    &mut stack,
                    cinst,
                    &mut taken.clone(),
                );
                build_concrete_produce(&mut graph, &mut stack, cinst, gen.to_vec());
            }
        }
    }

    for vop in &stack {
        graph.add_edge_consumed_by_outside_graph(*vop);
    }
    graph.end_stack_ops = stack.clone();

    let last_is_return_like =
        !given.is_empty() && matches!(given[given.len() - 1], ElemTyInput::ReturnLike { .. });

    // Final-state stack gap: filled with CF production + outside-graph consumption (except the return ending).
    if let Some(end_stack) = end_stack {
        if stack.len() < end_stack.len() && !last_is_return_like {
            let missing = &end_stack[stack.len()..];
            let producer = unreachable_source.unwrap_or_else(|| {
                let k = InstKey::Outside(graph.outside_counter);
                graph.outside_counter += 1;
                k
            });
            for ty in missing {
                let id = graph.push_vop(OTy::Ty(*ty));
                graph.add_edge_for_control_flow_produce(id, producer);
                graph.add_edge_consumed_by_outside_graph(id);
                stack.push(id);
            }
        }
    }

    graph.attached_vop_num = graph.vops.len();
    graph.validate();
    graph
}

/// Python `_process_concrete_taken` (the force branch always forcing). The taken list is refined
/// in place (Any → the actual stack type): the Ncnd (select) path consumes the refined result to derive the generated
/// type; other paths' refinements have no downstream consumer.
/// R-29: the ref_depth/factory parameters were deleted (depth_ref has no readers; the maintenance chain removed with it,
/// and the empty-stack branch's `assert!(ref_depth <= 0)` vanished — constant true on the production path).
fn process_concrete_taken(
    unreachable_source: Option<InstKey>,
    graph: &mut OperandInstGraph,
    stack: &mut Vec<VopId>,
    cinst: InstKey,
    taken_ops: &mut [OTy],
) {
    for i in (0..taken_ops.len()).rev() {
        let taken_op = taken_ops[i];
        let op = if stack.is_empty() {
            let id = graph.push_vop(taken_op);
            match unreachable_source {
                None => graph.add_edge_produced_by_outside_graph(id),
                Some(src) => graph.add_edge_for_control_flow_produce(id, src),
            }
            id
        } else {
            let id = stack.pop().unwrap();
            let cur = graph.vops[id as usize].ty;
            if taken_op != OTy::Any && cur != OTy::Any {
                // Mismatch: force the stack operand type (force_by_taken_ops always True).
                if cur != taken_op {
                    graph.vops[id as usize].ty = taken_op;
                }
            } else if cur == OTy::Any && taken_op != OTy::Any {
                graph.vops[id as usize].ty = taken_op;
            } else if taken_op == OTy::Any && cur != OTy::Any {
                // Reverse refinement: the taken element records the actual stack type (for select's generated-type derivation).
                taken_ops[i] = cur;
            }
            id
        };
        graph.add_edge(op, cinst, EdgeType::Consume);
    }
}

/// Python `_build_concrete_produce` (R-29: the ref_depth/factory parameters deleted).
fn build_concrete_produce(
    graph: &mut OperandInstGraph,
    stack: &mut Vec<VopId>,
    cinst: InstKey,
    gen_types: Vec<OTy>,
) {
    for gen_type in gen_types {
        let id = graph.push_vop(gen_type);
        graph.add_edge(id, cinst, EdgeType::Produce);
        stack.push(id);
    }
}

/// Python `_consume_stack_by_control_flow`.
fn consume_stack_by_control_flow(
    graph: &mut OperandInstGraph,
    stack: &mut Vec<VopId>,
    cinst: InstKey,
) {
    for op in stack.iter() {
        graph.add_edge_for_control_flow_consume(*op, cinst);
    }
    stack.clear();
}

/// The live subset of Python `GraphQuery`.
pub struct GraphQuery<'a> {
    pub graph: &'a OperandInstGraph,
}

impl<'a> GraphQuery<'a> {
    pub fn new(graph: &'a OperandInstGraph) -> GraphQuery<'a> {
        GraphQuery { graph }
    }

    /// Python `count_dependency_on_cf(elem_idx)`: returns (times consumed by CF, CF-produced types).
    pub fn count_dependency_on_cf(&self, elem_idx: usize) -> (usize, Vec<OTy>) {
        let Some(inst_sym) = self.graph.numidx2_instidx.get(&elem_idx) else {
            return (0, vec![]);
        };
        let taken_ops = self.graph.inst_idx_to_consumed_ops.get(inst_sym).map(Vec::as_slice).unwrap_or(&[]);
        let gen_ops = self.graph.inst_idx_to_produced_ops.get(inst_sym).map(Vec::as_slice).unwrap_or(&[]);
        let taken_num = taken_ops
            .iter()
            .filter(|op| self.graph.consumer_relation[**op as usize] == Some(EdgeType::CfConsume))
            .count();
        let gen_strs: Vec<OTy> = gen_ops
            .iter()
            .filter(|op| self.graph.producer_relation[**op as usize] == Some(EdgeType::CfProduce))
            .map(|op| self.graph.vops[*op as usize].ty)
            .collect();
        (taken_num, gen_strs)
    }

    /// Python `find_subgraph_ncf_inst_sequences`: the concrete element sets of non-CF connected components.
    /// Iteration starts in VopId ascending order (see the module comment's order note).
    pub fn find_subgraph_ncf_inst_sequences(&self) -> Vec<Vec<usize>> {
        let n = self.graph.attached_vop_num;
        if n == 0 {
            return vec![];
        }
        let mut processed_ops = vec![false; n];
        let mut all_sequences: Vec<Vec<usize>> = Vec::new();

        for start_op in 0..n as VopId {
            if processed_ops[start_op as usize] {
                continue;
            }
            let mut connected_ops: Vec<bool> = vec![false; n];
            let mut connected_insts: Vec<InstKey> = Vec::new();
            self.build_connected_subgraph(start_op, &mut connected_ops, &mut connected_insts);
            for (i, seen) in connected_ops.iter().enumerate() {
                if *seen {
                    processed_ops[i] = true;
                }
            }
            let concrete_idxs: Vec<usize> = connected_insts
                .into_iter()
                .filter_map(|k| match k {
                    InstKey::Concrete { idx, .. } => Some(idx),
                    _ => None,
                })
                .collect();
            if !concrete_idxs.is_empty() {
                all_sequences.push(concrete_idxs);
            }
        }
        all_sequences
    }

    /// Python `_build_connected_subgraph`: checks the producer and consumer kinds first,
    /// stopping if either is non-Common/Ncnd (including Outside); spreads along a side only if that side's relation is non-CF.
    /// An explicit stack avoids deep recursion; visit order = semantically equivalent (set semantics).
    fn build_connected_subgraph(
        &self,
        start_op: VopId,
        connected_ops: &mut [bool],
        connected_insts: &mut Vec<InstKey>,
    ) {
        let kind_ok = |k: InstKey| {
            matches!(
                k,
                InstKey::Concrete { kind: ElemKind::Common | ElemKind::Ncnd, .. }
            )
        };
        let mut work: Vec<VopId> = vec![start_op];
        while let Some(op) = work.pop() {
            if connected_ops[op as usize] {
                continue;
            }
            connected_ops[op as usize] = true;
            let producer = self.graph.producer[op as usize].expect("producer set");
            let consumer = self.graph.consumer[op as usize].expect("consumer set");
            if !kind_ok(producer) || !kind_ok(consumer) {
                continue;
            }
            if self.graph.producer_relation[op as usize] != Some(EdgeType::CfProduce) {
                if !connected_insts.contains(&producer) {
                    connected_insts.push(producer);
                }
                let mut next: Vec<VopId> = self
                    .graph
                    .inst_idx_to_consumed_ops
                    .get(&producer)
                    .map(|v| v.as_slice())
                    .unwrap_or(&[])
                    .iter()
                    .chain(
                        self.graph
                            .inst_idx_to_produced_ops
                            .get(&producer)
                            .map(|v| v.as_slice())
                            .unwrap_or(&[])
                            .iter(),
                    )
                    .copied()
                    .filter(|o| !connected_ops[*o as usize])
                    .collect();
                work.append(&mut next);
            }
            if self.graph.consumer_relation[op as usize] != Some(EdgeType::CfConsume) {
                if !connected_insts.contains(&consumer) {
                    connected_insts.push(consumer);
                }
                let mut next: Vec<VopId> = self
                    .graph
                    .inst_idx_to_produced_ops
                    .get(&consumer)
                    .map(|v| v.as_slice())
                    .unwrap_or(&[])
                    .iter()
                    .chain(
                        self.graph
                            .inst_idx_to_consumed_ops
                            .get(&consumer)
                            .map(|v| v.as_slice())
                            .unwrap_or(&[])
                            .iter(),
                    )
                    .copied()
                    .filter(|o| !connected_ops[*o as usize])
                    .collect();
                work.append(&mut next);
            }
        }
    }
}
