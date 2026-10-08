//! M10.1 synthetic unit tests: the value-operand graph and element model (vs the post-cleanup baseline of Python's
//! `ElemOperandMappingV2.py` / `ReduceInsts_V5_util.py`).

use e2wr_pass::instseq::elem::{get_type_info, OTy, StackChange};
use e2wr_pass::instseq::vop::{
    build_operand_graph, EdgeType, ElemKind, ElemTyInput, GraphQuery, InstKey,
};
use e2wr_ir::types::{FTy, TR, ValTy};

fn ty(t: ValTy) -> OTy {
    OTy::Ty(t)
}

fn det(taken: &[OTy], gen: &[OTy]) -> ElemTyInput {
    ElemTyInput::Determined { taken: taken.to_vec(), gen: gen.to_vec() }
}

#[test]
fn chain_i32_add_drop() {
    let elems = [
        det(&[], &[ty(ValTy::I32)]),
        det(&[], &[ty(ValTy::I32)]),
        det(&[ty(ValTy::I32), ty(ValTy::I32)], &[ty(ValTy::I32)]),
        det(&[OTy::Any], &[]), // drop
    ];
    let g = build_operand_graph(&elems, &[], None);
    assert_eq!(g.vops.len(), 3);
    let e = |i: usize| InstKey::Concrete { idx: i, kind: ElemKind::Common };
    assert_eq!(g.producer[0], Some(e(0)));
    assert_eq!(g.producer[1], Some(e(1)));
    assert_eq!(g.producer[2], Some(e(2)));
    assert_eq!(g.consumer[0], Some(e(2)));
    assert_eq!(g.consumer[1], Some(e(2)));
    assert_eq!(g.consumer[2], Some(e(3)));
    assert_eq!(g.producer_relation[0], Some(EdgeType::Produce));
    assert_eq!(g.consumer_relation[2], Some(EdgeType::Consume));
    assert_eq!(g.end_stack_ops, Vec::<e2wr_pass::instseq::vop::VopId>::new());
    // Stack before add = two constant operands.
    assert_eq!(g.inst2stack_before_it[&e(2)], vec![0, 1]);
    let q = GraphQuery::new(&g);
    let mut seqs = q.find_subgraph_ncf_inst_sequences();
    for s in &mut seqs {
        s.sort_unstable();
    }
    assert_eq!(seqs, vec![vec![0, 1, 2, 3]]);
}

#[test]
fn unreachable_resets_and_cf_consumes() {
    let elems = [
        det(&[], &[ty(ValTy::I32)]),
        ElemTyInput::Unreachable,
        det(&[], &[ty(ValTy::I32)]),
        det(&[OTy::Any], &[]), // drop
    ];
    let g = build_operand_graph(&elems, &[], None);
    assert_eq!(g.vops.len(), 2);
    let e = |i: usize, k: ElemKind| InstKey::Concrete { idx: i, kind: k };
    // v0 consumed by unreachable in the CF manner.
    assert_eq!(g.consumer[0], Some(e(1, ElemKind::Unreachable)));
    assert_eq!(g.consumer_relation[0], Some(EdgeType::CfConsume));
    assert!(g.op_is_taken_by_any(0));
    // New values after unreachable produce/consume normally.
    assert_eq!(g.producer[1], Some(e(2, ElemKind::Common)));
    assert_eq!(g.consumer[1], Some(e(3, ElemKind::Common)));
    assert_eq!(g.end_stack_ops, Vec::<e2wr_pass::instseq::vop::VopId>::new());
    let q = GraphQuery::new(&g);
    let mut seqs = q.find_subgraph_ncf_inst_sequences();
    for s in &mut seqs {
        s.sort_unstable();
    }
    assert_eq!(seqs, vec![vec![2, 3]]);
    // count_dependency_on_cf: unreachable consumes 1 CF operand.
    assert_eq!(q.count_dependency_on_cf(1), (1, vec![]));
}

#[test]
fn br_if_takes_cond_and_reproduces() {
    let elems = [ElemTyInput::BrIf { required_return: vec![ty(ValTy::I32)] }];
    let g = build_operand_graph(&elems, &[ValTy::I32, ValTy::I32], None);
    let e0 = InstKey::Concrete { idx: 0, kind: ElemKind::BrIf };
    assert_eq!(g.producer[2], Some(e0));
    assert_eq!(g.consumer[0], Some(e0));
    assert_eq!(g.consumer[1], Some(e0));
    assert_eq!(g.end_stack_ops, vec![2]);
    assert_eq!(g.vops[2].ty, ty(ValTy::I32));
}

#[test]
fn select_refines_gen_from_stack() {
    // Two i32 on the stack + select (no immediate): after taken refinement the generated type derives from any to i32.
    let elems = [ElemTyInput::Ncnd {
        taken: vec![OTy::Any, OTy::Any, ty(ValTy::I32)],
        gen: vec![OTy::Any],
    }];
    let g = build_operand_graph(&elems, &[ValTy::I32, ValTy::I32], None);
    let e0 = InstKey::Concrete { idx: 0, kind: ElemKind::Ncnd };
    // v0/v1 popped from the initial stack (produced outside the graph, consumed by select); v2 = the padded any value;
    // v3 = the refined generated operand (i32).
    assert_eq!(g.consumer[0], Some(e0));
    assert_eq!(g.consumer[1], Some(e0));
    assert!(matches!(g.producer[0], Some(InstKey::Outside(_))));
    assert_eq!(g.vops[3].ty, ty(ValTy::I32));
    assert_eq!(g.producer[3], Some(e0));
    assert_eq!(g.end_stack_ops, vec![3]);
}

#[test]
fn end_stack_gap_filled_by_cf_produce() {
    let elems = [det(&[], &[ty(ValTy::I32)])];
    let g = build_operand_graph(&elems, &[], Some(&[ValTy::I32, ValTy::I32]));
    // v0 produced normally; v1 = the CF-produced + outside-consumed padding slot.
    // Python copies end_stack_ops **before** padding: padding slots stay out of the final stack (copied verbatim).
    assert_eq!(g.end_stack_ops, vec![0]);
    assert_eq!(g.producer_relation[1], Some(EdgeType::CfProduce));
    assert!(matches!(g.producer[1], Some(InstKey::Outside(_))));
    assert_eq!(g.vops[1].ty, ty(ValTy::I32));
    assert!(matches!(g.consumer[1], Some(InstKey::Outside(_))));
}

#[test]
fn end_stack_gap_not_filled_after_return_like() {
    let elems = [ElemTyInput::ReturnLike { required: vec![ty(ValTy::I32)] }];
    let g = build_operand_graph(&elems, &[ValTy::I32], Some(&[ValTy::I32, ValTy::I32]));
    assert_eq!(g.end_stack_ops, Vec::<e2wr_pass::instseq::vop::VopId>::new());
}

#[test]
fn stack_type_mismatch_force_refined() {
    // force_by_taken_ops always True: i32 on the stack, taken type i64 → the stack operand forced to i64.
    let elems = [det(&[ty(ValTy::I64)], &[])];
    let g = build_operand_graph(&elems, &[ValTy::I32], None);
    assert_eq!(g.vops[0].ty, ty(ValTy::I64));
}

// ---- Element model ----

#[test]
fn type_info_determined_drop_arb_not_sure() {
    let f = FTy::of(&[ValTy::I32, ValTy::I32], &[ValTy::I32]);
    let ti = get_type_info(Some(&TR::new([f.clone()])), None);
    assert!(!ti.is_not_sure_type());
    assert_eq!(ti.taken_op_num(), 2);
    assert_eq!(ti.gen_ops(), [OTy::Ty(ValTy::I32)]);

    let ti = get_type_info(None, None);
    assert!(ti.is_not_sure_type());

    let drop_tr = TR::new([FTy::of(&[ValTy::I32], &[])]);
    let ti = get_type_info(Some(&drop_tr), None);
    // Without inst context, drop is not recognized (the drop test depends on inst).
    assert!(!ti.is_not_sure_type());

    // Multiple concrete candidates + select → the Arb shape.
    let multi = TR::new([
        FTy::of(&[ValTy::I32, ValTy::I32, ValTy::I32], &[ValTy::I32]),
        FTy::of(&[ValTy::I64, ValTy::I64, ValTy::I32], &[ValTy::I64]),
    ]);
    let ti = get_type_info(Some(&multi), Some(&e2wr_ir::Inst::Select));
    assert!(!ti.is_not_sure_type());
    assert_eq!(ti.taken_ops(), [OTy::Any, OTy::Any, OTy::Ty(ValTy::I32)]);
    assert_eq!(ti.gen_ops(), [OTy::Any]);

    // Candidates containing terminal (the old determined_return_ty) → not sure.
    let term = TR::new([FTy::of(&[], &[]), FTy::unreachable()]);
    let ti = get_type_info(Some(&term), None);
    assert!(ti.is_not_sure_type());
}

#[test]
fn stack_change_not_determined_from_any_gen() {
    let sc = StackChange::new(vec![OTy::Any], vec![OTy::Any]);
    assert!(sc.is_not_determined_type);
    let sc = StackChange::new(vec![OTy::Any], vec![OTy::Ty(ValTy::I32)]);
    assert!(!sc.is_not_determined_type);
    assert_eq!(sc.taken_op_num, 1);
}

// ---- M10.1 second half: GraphHelper / SubGraph / Splitter / equivalence subgraphs ----

use e2wr_pass::instseq::elem::OneElem;
use e2wr_pass::instseq::graph_helper::{get_init_subgraph_repo, GraphHelper, SubGraphSplitter};
use e2wr_pass::instseq::get_eq_sgs;

fn ftY(params: &[ValTy], results: &[ValTy]) -> e2wr_pass::instseq::elem::ElemTypeInfo {
    e2wr_pass::instseq::elem::ElemTypeInfo::determined(&FTy::of(params, results))
}

fn ielem(inst: e2wr_ir::Inst, params: &[ValTy], results: &[ValTy]) -> OneElem {
    OneElem::inst(inst, ftY(params, results), None)
}

#[test]
fn graph_helper_groups_and_splitter() {
    use e2wr_ir::Inst;
    // Scenario A (same input as Python's gh_driver): const, const, add, i64.const, drop.
    // Note: drop pops the i64.const's product off the stack top (forced to i32),
    // so the connected groups are {0,1,2} and {3,4} (as actually measured in Python).
    let i = ValTy::I32;
    let l = ValTy::I64;
    let elems = vec![
        ielem(Inst::I32Const { value: 0 }, &[], &[i]),
        ielem(Inst::I32Const { value: 0 }, &[], &[i]),
        ielem(Inst::I32Add, &[i, i], &[i]),
        ielem(Inst::I64Const { value: 0 }, &[], &[l]),
        ielem(Inst::Drop, &[i], &[]),
    ];
    let h = GraphHelper::new(&elems, None, &[], None);
    // Rust discovers components in VopId ascending order: {0,1,2} first (Python's set order is unstable — an allowed difference).
    let mut groups: Vec<Vec<Vec<usize>>> = h.sg_in_contigous_idxs.clone();
    groups.sort();
    assert_eq!(groups, vec![vec![vec![0, 1, 2]], vec![vec![3, 4]]]);

    // The shared counter increments per repo build (matching Python's global _count); hence a single repo instance.
    let mut repo2 = get_init_subgraph_repo(&h);
    let sets: Vec<(u32, Vec<usize>)> =
        repo2.items().map(|(idx, sg)| (*idx, sg.sg_elem_idxs.clone())).collect();
    assert_eq!(sets.len(), 2);
    let by_elems = |want: &[usize]| -> u32 {
        sets.iter()
            .find(|(_, e)| e == &want.to_vec())
            .map(|(i, _)| *i)
            .expect("sg with elems")
    };
    assert_eq!(by_elems(&[0, 1, 2]), 0);
    assert_eq!(by_elems(&[3, 4]), 1);

    // Splitting {3,4}: the edge between e3 and e4 points at last (not in the prefix) → two single-element subgraphs.
    let sp = SubGraphSplitter::new(&h);
    let kids = sp.replace_a_graph(&h, 1, &mut repo2);
    let kid_elems: Vec<Vec<usize>> = kids.iter().map(|s| s.sg_elem_idxs.clone()).collect();
    assert_eq!(kid_elems, vec![vec![3], vec![4]]);

    // Splitting {0,1,2}: add is last; the prefix's two constants are unconnected → three single-element subgraphs.
    let kids = sp.replace_a_graph(&h, 0, &mut repo2);
    let kid_elems: Vec<Vec<usize>> = kids.iter().map(|s| s.sg_elem_idxs.clone()).collect();
    assert_eq!(kid_elems, vec![vec![0], vec![1], vec![2]]);
}

#[test]
fn eq_subgraph_add_borrow_pattern() {
    use e2wr_ir::Inst;
    // Scenario B (same input as Python's gh_driver): const, const, add, drop.
    // add's equivalence subgraph = {0,2} (borrowing v0, keeping v1's match); {1,2} is a trivial consecutive run, skipped.
    let i = ValTy::I32;
    let elems = vec![
        ielem(Inst::I32Const { value: 0 }, &[], &[i]),
        ielem(Inst::I32Const { value: 0 }, &[], &[i]),
        ielem(Inst::I32Add, &[i, i], &[i]),
        ielem(Inst::Drop, &[i], &[]),
    ];
    let mut h = GraphHelper::new(&elems, None, &[], None);
    let eq = get_eq_sgs(&mut h);
    let sets: Vec<(Vec<usize>, bool)> =
        eq.items().map(|(_, sg)| (sg.sg_elem_idxs.clone(), sg.enable_internal_cancel)).collect();
    assert_eq!(sets, vec![(vec![0, 2], false)]);
}

#[test]
fn subsequence_positions_order_and_budget() {
    use e2wr_pass::instseq::eq_subgraph::find_subsequence_positions;
    let r = find_subsequence_positions::<i32>(&[1, 2], &[1, 1, 2, 2], None);
    assert_eq!(r, vec![vec![0, 2], vec![0, 3], vec![1, 2], vec![1, 3]]);
    let r = find_subsequence_positions::<i32>(&[1, 2], &[1, 1, 2, 2], Some(2));
    assert_eq!(r, vec![vec![0, 2], vec![0, 3]]);
    let r: Vec<Vec<usize>> = find_subsequence_positions::<i32>(&[], &[1, 2], None);
    assert_eq!(r, vec![vec![]]);
}

// ---- M10.2: mutation generation (get_elem_mutation_for_sg_ng / materialize_replacements) ----

use e2wr_pass::instseq::mutation::{get_elem_mutation_for_sg_ng, materialize_replacements};
use rand::SeedableRng;

fn opcode_of(e: &OneElem) -> &'static str {
    match &e.elem {
        e2wr_pass::instseq::elem::ElemRef::Inst(i) => match i {
            e2wr_ir::Inst::Drop => "drop",
            e2wr_ir::Inst::I32Const { .. } => "i32.const",
            e2wr_ir::Inst::I64Const { .. } => "i64.const",
            e2wr_ir::Inst::I32Add => "i32.add",
            _ => "?",
        },
        _ => "node",
    }
}

#[test]
fn sg_mutation_drop_for_external_param() {
    // S1 (the Python mut_driver2 baseline): param i32 + [const, add, drop].
    // sg {0,1,2}: the external input v0 → drop replacement; covered={0,1,2};
    // materialized = {0:[drop], 1:[], 2:[]}.
    use e2wr_ir::Inst;
    let i = ValTy::I32;
    let elems = vec![
        ielem(Inst::I32Const { value: 0 }, &[], &[i]),
        ielem(Inst::I32Add, &[i, i], &[i]),
        ielem(Inst::Drop, &[i], &[]),
    ];
    let h = GraphHelper::new(&elems, None, &[ValTy::I32], None);
    let repo = get_init_subgraph_repo(&h);
    let sg = repo.items().next().map(|(_, s)| s).unwrap();
    assert_eq!(sg.sg_elem_idxs, vec![0, 1, 2]);
    let mut rng = rand::rngs::StdRng::seed_from_u64(42);
    let m = get_elem_mutation_for_sg_ng(&h, sg, &mut rng).expect("mutation");
    assert_eq!(m.covered_elem_idxs, [0, 1, 2].into_iter().collect::<std::collections::BTreeSet<_>>());
    let mut desc = Vec::new();
    for (k, v) in &m.materialized_mutation {
        desc.push((*k, v.iter().map(opcode_of).collect::<Vec<_>>()));
    }
    assert_eq!(
        desc,
        vec![(0usize, vec!["drop"]), (1usize, vec![]), (2usize, vec![])]
    );
    assert_eq!(m.consumed_operands.len(), 1);
    assert!(m.produced_operands.is_empty());
    assert!(m.is_operand_aware);
}

#[test]
fn sg_mutation_const_for_external_result() {
    // S2 (the Python mut_driver2 baseline): result i32 + [const].
    // sg {0}: the external output v0 → const replacement; materialized = {0:[i32.const]}.
    use e2wr_ir::Inst;
    let i = ValTy::I32;
    let elems = vec![ielem(Inst::I32Const { value: 0 }, &[], &[i])];
    let h = GraphHelper::new(&elems, None, &[], Some(&[ValTy::I32]));
    let repo = get_init_subgraph_repo(&h);
    let sg = repo.items().next().map(|(_, s)| s).unwrap();
    assert_eq!(sg.sg_elem_idxs, vec![0]);
    let mut rng = rand::rngs::StdRng::seed_from_u64(7);
    let m = get_elem_mutation_for_sg_ng(&h, sg, &mut rng).expect("mutation");
    assert_eq!(m.covered_elem_idxs, [0].into_iter().collect::<std::collections::BTreeSet<_>>());
    let only = &m.materialized_mutation[&0];
    assert_eq!(only.len(), 1);
    assert_eq!(opcode_of(&only[0]), "i32.const");
    assert!(m.consumed_operands.is_empty());
    assert_eq!(m.produced_operands.len(), 1);
}

#[test]
fn eq_sg_mutation_and_internal_operand_filtering() {
    // Python's mut3 baseline: equivalence subgraph {0,2} (cancel=false).
    // materialized = {0:[const], 2:[drop,drop,const]};
    // consumed={v0,v1}, produced={v0,v2} (v0's dual identity = an internalized operand);
    // after materialize blocked={v0} → merged = {0:[], 2:[drop,const]}.
    use e2wr_ir::Inst;
    let i = ValTy::I32;
    let elems = vec![
        ielem(Inst::I32Const { value: 0 }, &[], &[i]),
        ielem(Inst::I32Const { value: 0 }, &[], &[i]),
        ielem(Inst::I32Add, &[i, i], &[i]),
        ielem(Inst::Drop, &[i], &[]),
    ];
    let mut h = GraphHelper::new(&elems, None, &[], None);
    let eq = get_eq_sgs(&mut h);
    let mut rng = rand::rngs::StdRng::seed_from_u64(1);
    let sgs: Vec<_> = eq.items().map(|(_, s)| s.clone()).collect();
    assert_eq!(sgs.len(), 1);
    let m = get_elem_mutation_for_sg_ng(&h, &sgs[0], &mut rng).expect("mutation");
    assert_eq!(
        m.covered_elem_idxs,
        [0, 2].into_iter().collect::<std::collections::BTreeSet<_>>()
    );
    let mat = |mm: &std::collections::BTreeMap<usize, Vec<OneElem>>| {
        mm.iter()
            .map(|(k, v)| (*k, v.iter().map(opcode_of).collect::<Vec<_>>()))
            .collect::<Vec<_>>()
    };
    assert_eq!(
        mat(&m.materialized_mutation),
        vec![(0usize, vec!["i32.const"]), (2usize, vec!["drop", "drop", "i32.const"])]
    );
    // v0 (e0's product) is in both the consumed and produced sets.
    let internal: std::collections::BTreeSet<_> = m
        .consumed_operands
        .intersection(&m.produced_operands)
        .copied()
        .collect();
    assert_eq!(internal.len(), 1);
    let merged = materialize_replacements(&[&m]).expect("merge");
    assert_eq!(
        mat(&merged),
        vec![(0usize, vec![]), (2usize, vec!["drop", "i32.const"])]
    );
}
