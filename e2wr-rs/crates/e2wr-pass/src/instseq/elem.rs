//! Element model: the live subset of Python `ReduceInsts_V5_util.py` (post round-4 cleanup).
//!
//! Not ported (B-6 dead code): the group-level get_length/as_insts/is_one_const_or_drop/
//! is_determined_type/set_index families, MutElemGroup's stack_change calculator,
//! StackChange.merge_one/_drop_or_type_to_stack_change/empty,
//! OneElem.is_unreachable_inst, forwarding property families, the elem_idx field (not stored; construction-order
//! semantics guaranteed by callers). The type currency is unified to [`OTy`] (Python's type strings +
//! 'any').

use e2wr_ir::ast::{Ast, NodeId};
use e2wr_ir::types::{FTy, TR, ValTy};
use e2wr_ir::Inst;

use crate::remap::const_inst;

/// Python `OneNodeType`: a concrete value type or ANY ('any').
#[derive(Clone, Copy, Debug, PartialEq, Eq, Hash)]
pub enum OTy {
    Any,
    Ty(ValTy),
}

impl OTy {
    pub fn concrete(self) -> Option<ValTy> {
        match self {
            OTy::Any => None,
            OTy::Ty(t) => Some(t),
        }
    }
}

fn wp_valty(ty: ValTy) -> wasmparser::ValType {
    use wasmparser::ValType as V;
    match ty {
        ValTy::I32 => V::I32,
        ValTy::I64 => V::I64,
        ValTy::F32 => V::F32,
        ValTy::F64 => V::F64,
        ValTy::V128 => V::V128,
        ValTy::Funcref => V::FUNCREF,
        ValTy::Externref => V::EXTERNREF,
    }
}

/// Python `DropOrType`. R-30: was a struct of `is_drop: bool` + `Option<FTy>`
/// (mimicking an enum with only the Drop / Type shapes; ill-formed combinations were writable
/// but never constructed); now a real enum.
#[derive(Clone, Debug, PartialEq, Eq)]
pub enum DropOrType {
    Drop,
    Type(FTy),
}

/// Python `ElemTypeInfo` (including the ArbElemTypeInfo subclass shape).
/// `elem_type.is_none() && arb.is_none()` ⇒ not_sure.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct ElemTypeInfo {
    elem_type: Option<DropOrType>,
    /// Some((taken, gen)) ⇒ the ArbElemTypeInfo shape (select/ref.is_null).
    arb: Option<(Vec<OTy>, Vec<OTy>)>,
    taken_op_num: Option<usize>,
    gen_ops: Option<Vec<OTy>>,
    taken_ops: Option<Vec<OTy>>,
}

impl ElemTypeInfo {
    pub fn not_sure() -> ElemTypeInfo {
        ElemTypeInfo {
            elem_type: None,
            arb: None,
            taken_op_num: None,
            gen_ops: None,
            taken_ops: None,
        }
    }

    fn from_elem_type(elem_type: DropOrType) -> ElemTypeInfo {
        let (taken_op_num, gen_ops, taken_ops) = match &elem_type {
            DropOrType::Drop => (1usize, vec![], vec![OTy::Any]),
            DropOrType::Type(fty) => (
                fty.params.len(),
                fty.results.iter().map(|t| OTy::Ty(*t)).collect(),
                fty.params.iter().map(|t| OTy::Ty(*t)).collect(),
            ),
        };
        ElemTypeInfo {
            elem_type: Some(elem_type),
            arb: None,
            taken_op_num: Some(taken_op_num),
            gen_ops: Some(gen_ops),
            taken_ops: Some(taken_ops),
        }
    }

    pub fn determined(fty: &FTy) -> ElemTypeInfo {
        Self::from_elem_type(DropOrType::Type(fty.clone()))
    }

    pub fn drop_elem() -> ElemTypeInfo {
        Self::from_elem_type(DropOrType::Drop)
    }

    /// Python `ArbElemTypeInfo(gen_ops, taken_ops)`.
    pub fn arb(taken: Vec<OTy>, gen: Vec<OTy>) -> ElemTypeInfo {
        ElemTypeInfo {
            elem_type: None,
            arb: Some((taken.clone(), gen.clone())),
            taken_op_num: Some(taken.len()),
            gen_ops: Some(gen),
            taken_ops: Some(taken),
        }
    }

    pub fn is_not_sure_type(&self) -> bool {
        self.elem_type.is_none() && self.arb.is_none()
    }

    pub fn gen_ops(&self) -> &[OTy] {
        self.gen_ops.as_deref().expect("gen_ops on not-sure info")
    }
    pub fn taken_ops(&self) -> &[OTy] {
        self.taken_ops.as_deref().expect("taken_ops on not-sure info")
    }
    pub fn taken_op_num(&self) -> usize {
        self.taken_op_num.expect("taken_op_num on not-sure info")
    }
}

/// Python `get_type_info(type_req, inst)`: TR → ElemTypeInfo.
/// Under the 'eq' view (typeReq._poly2req: the first candidate without params_poly): a single candidate ⟹
/// determined (**even when terminal** — Python's branch order checks the candidate count before determined_return_ty,
/// corrected at U3); multiple candidates with terminal ⟹ not_sure; select (no
/// immediate)/ref.is_null ⟹ the Arb shape; otherwise not_sure.
pub fn get_type_info(tr: Option<&TR>, inst: Option<&Inst>) -> ElemTypeInfo {
    let Some(tr) = tr else { return ElemTypeInfo::not_sure() };
    if matches!(inst, Some(Inst::Drop)) {
        return ElemTypeInfo::drop_elem();
    }
    let cands = tr.cands();
    let eq_view = cands.first().map(|c| !c.params_poly).unwrap_or(true);
    if cands.len() == 1 && eq_view {
        return ElemTypeInfo::determined(&cands[0]);
    }
    if eq_view {
        if cands.iter().any(|f| f.terminal) {
            return ElemTypeInfo::not_sure();
        }
        match inst {
            Some(Inst::Select) => {
                return ElemTypeInfo::arb(
                    vec![OTy::Any, OTy::Any, OTy::Ty(ValTy::I32)],
                    vec![OTy::Any],
                );
            }
            Some(Inst::RefIsNull) => {
                return ElemTypeInfo::arb(vec![OTy::Any], vec![OTy::Ty(ValTy::I32)]);
            }
            _ => {}
        }
    }
    ElemTypeInfo::not_sure()
}

/// Python `OneElem.elem: Union[Inst, ASTINode]`.
#[derive(Clone, Debug)]
pub enum ElemRef {
    Inst(Inst),
    Node(NodeId),
}

/// Python `OneElem` (no elem_idx field stored, B-6).
#[derive(Clone, Debug)]
pub struct OneElem {
    pub elem: ElemRef,
    pub type_info: ElemTypeInfo,
    pub raw_index: Option<u32>,
}

impl OneElem {
    pub fn inst(inst: Inst, type_info: ElemTypeInfo, raw_index: Option<u32>) -> OneElem {
        OneElem { elem: ElemRef::Inst(inst), type_info, raw_index }
    }

    pub fn node(node: NodeId, type_info: ElemTypeInfo, raw_index: Option<u32>) -> OneElem {
        OneElem { elem: ElemRef::Node(node), type_info, raw_index }
    }

    pub fn get_length(&self, ast: &Ast) -> u32 {
        match &self.elem {
            ElemRef::Inst(_) => 1,
            ElemRef::Node(id) => ast.get_length(*id),
        }
    }

    pub fn as_insts(&self, ast: &Ast) -> Vec<Inst> {
        match &self.elem {
            ElemRef::Inst(i) => vec![i.clone()],
            ElemRef::Node(id) => ast.get_insts(*id),
        }
    }

    /// A single instruction whose tail looks like unreachable (return/br/br_table/unreachable).
    pub fn tail_likes_unreachable(&self) -> bool {
        match &self.elem {
            ElemRef::Inst(i) => matches!(
                i,
                Inst::Return | Inst::Br { .. } | Inst::BrTable(_) | Inst::Unreachable
            ),
            ElemRef::Node(_) => false,
        }
    }

    /// Control-flow single instructions (the four above + br_if).
    pub fn is_cf_related_inst(&self) -> bool {
        match &self.elem {
            ElemRef::Inst(i) => matches!(
                i,
                Inst::Return
                    | Inst::Br { .. }
                    | Inst::BrTable(_)
                    | Inst::Unreachable
                    | Inst::BrIf { .. }
            ),
            ElemRef::Node(_) => false,
        }
    }

    /// Python `is_target_one_inst(target_insts)`; P3's set is {'return','br_if'} (P-4).
    pub fn is_return_or_br_if(&self) -> bool {
        match &self.elem {
            ElemRef::Inst(i) => matches!(i, Inst::Return | Inst::BrIf { .. }),
            ElemRef::Node(_) => false,
        }
    }

    pub fn is_one_const_or_drop(&self) -> bool {
        match &self.elem {
            ElemRef::Inst(i) => matches!(
                i,
                Inst::I32Const { .. }
                    | Inst::I64Const { .. }
                    | Inst::F32Const { .. }
                    | Inst::F64Const { .. }
                    | Inst::V128Const { .. }
                    | Inst::Drop
                    | Inst::RefNull { .. }
            ),
            ElemRef::Node(_) => false,
        }
    }

    pub fn is_determined_type(&self) -> bool {
        !self.type_info.is_not_sure_type()
    }

    /// Python `OneElem.stack_change` property.
    pub fn stack_change(&self) -> StackChange {
        StackChange::new(self.type_info.taken_ops().to_vec(), self.type_info.gen_ops().to_vec())
    }
}

/// The live field subset of Python `StackChange`.
/// R-29: the taken_op_type_list field was deleted — no readers repo-wide (even in Python it served only
/// `__repr__` printing; cf_elem filled it with placeholder Any lists anyway),
/// the input parameter kept only to compute taken_op_num.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct StackChange {
    pub taken_op_num: usize,
    pub gen_types: Vec<OTy>,
    pub is_not_determined_type: bool,
}

impl StackChange {
    pub fn new(taken_op_type_list: Vec<OTy>, gen_types: Vec<OTy>) -> StackChange {
        let is_not_determined_type = gen_types.contains(&OTy::Any);
        StackChange {
            taken_op_num: taken_op_type_list.len(),
            gen_types,
            is_not_determined_type,
        }
    }
}

/// Python `gen_replacement_by_stack_change`: drop×taken + one constant per generated type.
/// Constant randomness same-distribution without chasing the sequence (D-2). Not used for replacement when a generated type includes ANY
/// (the Python side filters 'any' upstream); skipped defensively here.
/// (U5 correction: the constant element's type = `[] -> [ty]` (Python's
/// `generate_one_func_type_default([], [ty])`); previously mis-written as `[ty] -> []`.)
pub fn gen_replacement_by_stack_change(
    sc: &StackChange,
    rng: &mut impl rand::Rng,
) -> Vec<OneElem> {
    let mut new_elems = Vec::new();
    for _ in 0..sc.taken_op_num {
        new_elems.push(OneElem::inst(Inst::Drop, ElemTypeInfo::drop_elem(), None));
    }
    for ty in &sc.gen_types {
        if let OTy::Ty(ty) = ty {
            let inst = const_inst(&wp_valty(*ty), rng).expect("const inst");
            let fty = FTy::of(&[], &[*ty]);
            new_elems.push(OneElem::inst(inst, ElemTypeInfo::determined(&fty), None));
        }
    }
    new_elems
}

/// Python `cur_is_more_naive_v2`: shorter, or fewer non-trivial instructions.
pub fn cur_is_more_naive_v2(raw_elems: &[OneElem], cur_elems: &[OneElem], ast: &Ast) -> bool {
    let cur_len: u32 = cur_elems.iter().map(|e| e.get_length(ast)).sum();
    let raw_len: u32 = raw_elems.iter().map(|e| e.get_length(ast)).sum();
    if cur_len < raw_len {
        return true;
    }
    let cur_non_trivial = cur_elems.iter().filter(|e| !e.is_one_const_or_drop()).count();
    let raw_non_trivial = raw_elems.iter().filter(|e| !e.is_one_const_or_drop()).count();
    cur_non_trivial < raw_non_trivial
}
