//! Type system (M5): the stack-requirement type `FTy` with compose/match/candidate-set/stack-state projections.
//!
//! Mirrors the new Python baseline (commit 8801071): `extract_block_mutator/typeSys2.py`
//! is the body, with `funcType/typeReq/funcTypeFactory` as compatibility shells. The model is a five-tuple
//! `(params, results, params_poly, results_poly, terminal)`, written
//! `[t1*?] -> [t2*?]`: a poly bit means the stack bottom may hold unknown content (incomplete knowledge / stack polymorphism),
//! terminal means the type comes from a terminating instruction (br/return/br_table, mapping the old
//! determined_return_ty). Lists run from stack bottom to stack top.
//!
//! `compose` rules (settled against the dual Python implementations, B-3):
//! 1. `a.terminal` → resolution is skipped and composition always succeeds: params = a.params, results = b.results
//!    (semantics: "b executes on an unreachable stack");
//! 2. otherwise the overlap of a.results and b.params (the trailing min(|R1|,|P2|) of each)
//!    must match element-wise; mismatch → None (candidate eliminated);
//! 3. the remaining prefix of b.params: swallowed when `a.results_poly`, otherwise it joins the composite params;
//! 4. the remaining prefix of a.results: swallowed when `b.terminal`, otherwise it joins the composite results;
//! 5. poly bits propagate by or; terminal propagates to the tail element (like the old det propagation).
//!
//! Candidate sets are explicitly ordered by value (Python `sorted`; ty0 = smallest candidate).

/// Value types (Python `WasmInfoCfg.val_type_strs`, 7 kinds).
///
/// Declaration order = the Python type-name string order (externref < f32 < f64 < funcref < i32
/// < i64 < v128), so that derive(Ord)'s candidate ordering matches Python `sorted(FTy)`
/// (ty0 semantics aligned). Extended value types outside the corpus are unsupported (fail fast).
#[derive(Clone, Copy, Debug, PartialEq, Eq, Hash, PartialOrd, Ord)]
pub enum ValTy {
    Externref,
    F32,
    F64,
    Funcref,
    I32,
    I64,
    V128,
}

impl ValTy {
    pub fn name(self) -> &'static str {
        match self {
            ValTy::Externref => "externref",
            ValTy::F32 => "f32",
            ValTy::F64 => "f64",
            ValTy::Funcref => "funcref",
            ValTy::I32 => "i32",
            ValTy::I64 => "i64",
            ValTy::V128 => "v128",
        }
    }

    #[cfg(test)]
    pub fn parse(s: &str) -> Option<ValTy> {
        Some(match s {
            "externref" => ValTy::Externref,
            "f32" => ValTy::F32,
            "f64" => ValTy::F64,
            "funcref" => ValTy::Funcref,
            "i32" => ValTy::I32,
            "i64" => ValTy::I64,
            "v128" => ValTy::V128,
            _ => return None,
        })
    }

    pub fn from_wasmparser(t: wasmparser::ValType) -> Option<ValTy> {
        use wasmparser::ValType as V;
        Some(match t {
            V::I32 => ValTy::I32,
            V::I64 => ValTy::I64,
            V::F32 => ValTy::F32,
            V::F64 => ValTy::F64,
            V::V128 => ValTy::V128,
            V::Ref(r) if r == wasmparser::RefType::FUNCREF => ValTy::Funcref,
            V::Ref(r) if r == wasmparser::RefType::EXTERNREF => ValTy::Externref,
            // Extended reference types (non-nullable / abstract types) are out of scope
            _ => return None,
        })
    }
}

/// The `[t1*?] -> [t2*?]` stack-requirement type.
#[derive(Clone, Debug, PartialEq, Eq, Hash, PartialOrd, Ord)]
pub struct FTy {
    pub params: Vec<ValTy>,
    pub results: Vec<ValTy>,
    pub params_poly: bool,
    pub results_poly: bool,
    pub terminal: bool,
}

impl FTy {
    /// A concrete type (all false), matching the old 'eq' requirement and function types.
    pub fn of(params: &[ValTy], results: &[ValTy]) -> FTy {
        FTy {
            params: params.to_vec(),
            results: results.to_vec(),
            params_poly: false,
            results_poly: false,
            terminal: false,
        }
    }

    /// The type of unreachable, `[t*?] -> [t*?]` (terminal=true; its judgment surface equals the old
    /// 'unreachable' branch, B-3).
    pub fn unreachable() -> FTy {
        FTy {
            params: vec![],
            results: vec![],
            params_poly: true,
            results_poly: true,
            terminal: true,
        }
    }

    #[cfg(test)]
    pub fn is_unreachable_shape(&self) -> bool {
        self.params.is_empty()
            && self.results.is_empty()
            && self.params_poly
            && self.results_poly
    }
}

/// The composite of executing b after a; returns None on concrete-tail mismatch without polymorphic cover.
fn compose(a: &FTy, b: &FTy) -> Option<FTy> {
    if a.terminal {
        return Some(FTy {
            params: a.params.clone(),
            results: b.results.clone(),
            params_poly: a.params_poly || b.params_poly,
            results_poly: a.results_poly || b.results_poly,
            terminal: b.terminal,
        });
    }
    let r1 = &a.results;
    let p2 = &b.params;
    let m = r1.len().min(p2.len());
    for k in 0..m {
        if r1[r1.len() - m + k] != p2[p2.len() - m + k] {
            return None;
        }
    }
    let rest_p: &[ValTy] = if p2.len() > m && !a.results_poly {
        &p2[..p2.len() - m]
    } else {
        &[]
    };
    let rest_r: &[ValTy] = if r1.len() > m && !b.terminal {
        &r1[..r1.len() - m]
    } else {
        &[]
    };
    let mut params = rest_p.to_vec();
    params.extend_from_slice(&a.params);
    let mut results = rest_r.to_vec();
    results.extend_from_slice(&b.results);
    Some(FTy {
        params,
        results,
        params_poly: a.params_poly || b.params_poly,
        results_poly: a.results_poly || b.results_poly,
        terminal: b.terminal,
    })
}

/// Whether `fty` (actual/candidate type) satisfies `req` (requirement); req's poly bit = bottom-truncation
/// semantics. Matches the old check_ftype_match_req: eg_param_f = param truncation + exact results;
/// eg_param_and_result = truncation on both; the unreachable shape (both poly, empty) matches everything.
/// (Zero production callers, used only by this file's unit tests — R-3.)
#[cfg(test)]
pub fn satisfies(fty: &FTy, req: &FTy) -> bool {
    if req.params_poly {
        if fty.params.len() < req.params.len() {
            return false;
        }
        if fty.params[fty.params.len() - req.params.len()..] != req.params[..] {
            return false;
        }
    } else if fty.params != req.params {
        return false;
    }
    if req.results_poly {
        if fty.results.len() < req.results.len() {
            return false;
        }
        if fty.results[fty.results.len() - req.results.len()..] != req.results[..] {
            return false;
        }
    } else if fty.results != req.results {
        return false;
    }
    true
}

/// Candidate set (the old typeReq; the req_type dimension merged into candidate poly bits).
/// Sorted, deduplicated, explicitly ordered (ty0 = smallest candidate).
#[derive(Clone, Debug, PartialEq, Eq, Hash)]
pub struct TR {
    cands: Vec<FTy>,
}

impl TR {
    pub fn new(ftys: impl IntoIterator<Item = FTy>) -> TR {
        let mut v: Vec<FTy> = ftys.into_iter().collect();
        v.sort();
        v.dedup();
        TR { cands: v }
    }

    pub fn impossible(&self) -> bool {
        self.cands.is_empty()
    }

    pub fn ty0(&self) -> &FTy {
        &self.cands[0]
    }

    pub fn cands(&self) -> &[FTy] {
        &self.cands
    }
}

/// Cartesian product of candidates composed pairwise, failures eliminated (the old merge_req).
pub fn merge(tr1: &TR, tr2: &TR) -> TR {
    let mut out = Vec::with_capacity(tr1.cands.len() * tr2.cands.len());
    for x in &tr1.cands {
        for y in &tr2.cands {
            if let Some(c) = compose(x, y) {
                out.push(c);
            }
        }
    }
    TR::new(out)
}

// ---------------------------------------------------------------------------
// StackState projections (mirroring reduction_analysis/StackState.py, new baseline)
// ---------------------------------------------------------------------------

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum StackStatus {
    Normal,
    Any,
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct StackState {
    pub all_rest_types: Vec<Vec<ValTy>>,
    pub status: StackStatus,
}

/// Each candidate's result list; `[[]]` when all are terminal (the old tys[0].determined_return_ty).
pub fn rest_types_view(tr: &TR) -> Vec<Vec<ValTy>> {
    if !tr.cands.is_empty() && tr.cands.iter().all(|c| c.terminal) {
        return vec![vec![]];
    }
    tr.cands.iter().map(|c| c.results.clone()).collect()
}

/// The old mapping (F,F)/(T,F)→NORMAL, (T,T)/unreachable→ANY, i.e. decided by results_poly alone.
pub fn status_view(tr: &TR) -> StackStatus {
    if tr.cands.iter().any(|c| c.results_poly) {
        StackStatus::Any
    } else {
        StackStatus::Normal
    }
}

impl StackState {
    /// Mirrors Python get_stack_state_from_type_req.
    pub fn from_tr(tr: &TR) -> StackState {
        StackState {
            all_rest_types: rest_types_view(tr),
            status: status_view(tr),
        }
    }

    /// Mirrors Python StackState.as_type_req: candidate = `[] -> rest`,
    /// ANY → (T,T) (terminal=false), NORMAL → (F,F).
    pub fn as_type_req(&self) -> TR {
        let pp = self.status == StackStatus::Any;
        TR::new(self.all_rest_types.iter().map(|rest| FTy {
            params: vec![],
            results: rest.clone(),
            params_poly: pp,
            results_poly: pp,
            terminal: false,
        }))
    }
}

/// Whether one stack state supports another (literal port of Python sstate1_support_sstate2).
/// An ANY state's rest must be a suffix of the other's (possibly shorter); a NORMAL state requires candidate equality.
pub fn sstate1_support_sstate2(s1: &StackState, s2: &StackState) -> bool {
    if s1.status == StackStatus::Normal && s2.status == StackStatus::Any {
        return false;
    }
    s2.all_rest_types.iter().all(|r2| {
        s1.all_rest_types
            .iter()
            .any(|r1| {
                if s1.status == StackStatus::Any {
                    any_support(r1, r2)
                } else {
                    r1 == r2
                }
            })
    })
}

fn any_support(r1: &[ValTy], r2: &[ValTy]) -> bool {
    r1.len() <= r2.len() && &r2[r2.len() - r1.len()..] == r1
}

// ---------------------------------------------------------------------------
// Type-inference context (mirroring extract_block_mutator/Context.py +
// tool.py get_func_n_context_from_wasm_parser, type-related fields only)
// ---------------------------------------------------------------------------

/// Type-inference context. `from_module` equals Python
/// `get_func_n_context_from_wasm_parser`: local_types includes the parameter prefix,
/// func_type_idxs spans the whole space (imports + definitions), label_types defaults to one level (the function-body level
/// = the function's result types).
///
/// Known difference from Python (correct semantics):
/// global_val_types / table_val_types span the **whole index space** (Python stores defined items only,
/// so import indices misalign and out-of-range lookups return None).
#[derive(Debug, Clone)]
pub struct Context {
    pub local_types: Vec<ValTy>,
    pub func_type_idxs: Vec<u32>,
    pub types: Vec<(Vec<ValTy>, Vec<ValTy>)>,
    /// The params half (.0) has no readers repo-wide (only the Return branch reads .1 results);
    /// kept for structural parity with the Python Context (R-27).
    pub cur_func_ty: (Vec<ValTy>, Vec<ValTy>),
    pub global_val_types: Vec<ValTy>,
    pub table_val_types: Vec<ValTy>,
    /// Label type stack from innermost outward; None = no label context (br-family returns None).
    pub label_types: Option<Vec<Vec<ValTy>>>,
}

fn conv_ty(tys: &[wasmparser::ValType]) -> Option<Vec<ValTy>> {
    tys.iter().map(|t| ValTy::from_wasmparser(*t)).collect()
}

impl Context {
    /// Mirrors Python get_func_n_context_from_wasm_parser.
    /// None = the function or type section contains unsupported value types (beyond the 7).
    pub fn from_module(module: &crate::module::Module, func_idx: usize) -> Option<Context> {
        use crate::module::*;
        let func = module.defined_funcs.get(func_idx)?;
        let ty = module.types.get(func.ty_idx as usize)?;
        let params = conv_ty(&ty.params)?;
        let results = conv_ty(&ty.results)?;
        let locals = conv_ty(&func.locals)?;

        // Whole-space function type indices (imports first)
        let mut func_type_idxs = Vec::with_capacity(
            module.imports.len() + module.defined_func_ty_ids.len());
        for imp in &module.imports {
            if let ImportDesc::Func(t) = &imp.desc {
                func_type_idxs.push(*t);
            }
        }
        func_type_idxs.extend(module.defined_func_ty_ids.iter().copied());

        let mut types = Vec::with_capacity(module.types.len());
        for t in &module.types {
            types.push((conv_ty(&t.params)?, conv_ty(&t.results)?));
        }

        // Whole-space global/table value types (imports first)
        let mut global_val_types = Vec::new();
        let mut table_val_types = Vec::new();
        for imp in &module.imports {
            match &imp.desc {
                ImportDesc::Global(g) => {
                    global_val_types.push(ValTy::from_wasmparser(g.val_ty)?)
                }
                ImportDesc::Table(t) => {
                    table_val_types.push(ValTy::from_wasmparser(
                        wasmparser::ValType::Ref(t.elem_ty),
                    )?)
                }
                _ => {}
            }
        }
        for g in &module.defined_globals {
            global_val_types.push(ValTy::from_wasmparser(g.ty.val_ty)?);
        }
        for t in &module.defined_table_datas {
            table_val_types.push(ValTy::from_wasmparser(
                wasmparser::ValType::Ref(t.ty.elem_ty),
            )?);
        }

        let mut local_all = params.clone();
        local_all.extend(locals);

        Some(Context {
            local_types: local_all,
            cur_func_ty: (params, results.clone()),
            label_types: Some(vec![results]),
            func_type_idxs,
            types,
            global_val_types,
            table_val_types,
        })
    }

    /// Python `generate_context_by_out_layers_reuse_data`: prepend the outer block types
    /// to the label stack (innermost first). Python reuses the other fields through shared
    /// context_variables references; Rust clones (same values, only losing the shared alias).
    /// With label_types=None only the outer level is set (Python asserts; the real path always has a value).
    /// R-26: the only caller is crate-internal stack_infer; pub(crate).
    pub(crate) fn with_out_layers(&self, out_layers: Vec<Vec<ValTy>>) -> Context {
        let mut labels = out_layers;
        if let Some(existing) = &self.label_types {
            labels.extend(existing.iter().cloned());
        }
        Context {
            local_types: self.local_types.clone(),
            func_type_idxs: self.func_type_idxs.clone(),
            types: self.types.clone(),
            cur_func_ty: self.cur_func_ty.clone(),
            global_val_types: self.global_val_types.clone(),
            table_val_types: self.table_val_types.clone(),
            label_types: Some(labels),
        }
    }

    fn ty_of(&self, type_idx: u32) -> Option<(&[ValTy], &[ValTy])> {
        let (p, r) = self.types.get(type_idx as usize)?;
        Some((p, r))
    }
}

// ---------------------------------------------------------------------------
// Per-instruction stack requirements (mirroring OneInstReqUtil.get_inst_ty_req, new baseline)
// ---------------------------------------------------------------------------

use crate::inst_gen::Inst;

/// An instruction's stack requirement. Static table (concrete) → placeholder instructions (typed from context) →
/// hand-written control flow (br-family/call-family/return/unreachable/ref.null/typed select).
/// block/loop/if/else/end return None (handled at the AST layer, Python to_skip_ops).
/// The cur_params pre-check parameter of the Python signature is None at every live call site (removed per R-9;
/// the pre-check logic is kept as `cur_params_filter` for unit-test comparison).
pub fn get_inst_ty_req(inst: &Inst, ctx: Option<&Context>) -> Option<TR> {
    // ---- Hand-written control flow (Python _just_get_type_req_manual) ----
    match inst {
        Inst::Unreachable => return Some(TR::new([FTy::unreachable()])),
        Inst::RefNull { hty } => {
            use wasmparser::{AbstractHeapType, HeapType};
            let ty = match hty {
                HeapType::Abstract { shared: false, ty: AbstractHeapType::Func } =>
                    ValTy::Funcref,
                HeapType::Abstract { shared: false, ty: AbstractHeapType::Extern } =>
                    ValTy::Externref,
                _ => return None,
            };
            return Some(TR::new([FTy::of(&[], &[ty])]));
        }
        Inst::Call { function_index } => {
            let ctx = ctx?;
            let ty_idx = *ctx.func_type_idxs.get(*function_index as usize)?;
            let (p, r) = ctx.ty_of(ty_idx)?;
            return Some(TR::new([FTy::of(p, r)]));
        }
        Inst::CallIndirect { type_index, .. } => {
            let ctx = ctx?;
            let (p, r) = ctx.ty_of(*type_index)?;
            let mut params = p.to_vec();
            params.push(ValTy::I32);
            return Some(TR::new([FTy::of(&params, r)]));
        }
        Inst::Br { relative_depth } => {
            let ctx = ctx?;
            let labels = ctx.label_types.as_ref()?;
            let l = labels.get(*relative_depth as usize)?;
            return Some(TR::new([FTy {
                params: l.clone(),
                results: l.clone(),
                params_poly: true,
                results_poly: true,
                terminal: true,
            }]));
        }
        Inst::BrIf { relative_depth } => {
            let ctx = ctx?;
            let labels = ctx.label_types.as_ref()?;
            let l = labels.get(*relative_depth as usize)?;
            let mut params = l.clone();
            params.push(ValTy::I32);
            return Some(TR::new([FTy {
                params,
                results: l.clone(),
                params_poly: true,
                results_poly: false,
                terminal: false,
            }]));
        }
        Inst::Return => {
            let ctx = ctx?;
            let r = ctx.cur_func_ty.1.clone();
            return Some(TR::new([FTy {
                params: r.clone(),
                results: r,
                params_poly: true,
                results_poly: true,
                terminal: true,
            }]));
        }
        Inst::BrTable(br_table) => {
            let ctx = ctx?;
            let labels = ctx.label_types.as_ref()?;
            let all: Vec<&Vec<ValTy>> = br_table
                .targets
                .iter()
                .chain(std::iter::once(&br_table.default))
                .map(|d| labels.get(*d as usize))
                .collect::<Option<Vec<_>>>()?;
            // Python: take the longest; the rest must be its prefixes, otherwise None
            let longest: &Vec<ValTy> =
                all.iter().max_by_key(|l| l.len()).unwrap();
            if all.iter().any(|l| !longest.starts_with(l)) {
                return None;
            }
            let mut params = longest.clone();
            params.push(ValTy::I32);
            return Some(TR::new([FTy {
                params,
                results: longest.clone(),
                params_poly: true,
                results_poly: true,
                terminal: true,
            }]));
        }
        // block/loop/if/else/end: handled at the AST layer (Python to_skip_ops)
        Inst::Block { .. } | Inst::Loop { .. } | Inst::If { .. } | Inst::Else
        | Inst::End => return None,
        // typed select: [t t i32] -> [t], t comes from the immediate
        Inst::TypedSelect { ty } => {
            let t = ValTy::from_wasmparser(*ty)?;
            return Some(TR::new([FTy::of(&[t, t, ValTy::I32], &[t])]));
        }
        _ => {}
    }

    // ---- Placeholder instructions (types from context; out of range → None, aligned with Python) ----
    let placeholder: Option<TR> = match inst {
        Inst::LocalGet { local_index } => ctx?.local_types
            .get(*local_index as usize)
            .map(|t| TR::new([FTy::of(&[], &[*t])])),
        Inst::LocalSet { local_index } => ctx?.local_types
            .get(*local_index as usize)
            .map(|t| TR::new([FTy::of(&[*t], &[])])),
        Inst::LocalTee { local_index } => ctx?.local_types
            .get(*local_index as usize)
            .map(|t| TR::new([FTy::of(&[*t], &[*t])])),
        Inst::GlobalGet { global_index } => ctx?.global_val_types
            .get(*global_index as usize)
            .map(|t| TR::new([FTy::of(&[], &[*t])])),
        Inst::GlobalSet { global_index } => ctx?.global_val_types
            .get(*global_index as usize)
            .map(|t| TR::new([FTy::of(&[*t], &[])])),
        Inst::TableGet { table } => ctx?.table_val_types
            .get(*table as usize)
            .map(|t| TR::new([FTy::of(&[ValTy::I32], &[*t])])),
        Inst::TableSet { table } => ctx?.table_val_types
            .get(*table as usize)
            .map(|t| TR::new([FTy::of(&[ValTy::I32, *t], &[])])),
        Inst::TableGrow { table } => ctx?.table_val_types
            .get(*table as usize)
            .map(|t| TR::new([FTy::of(&[*t, ValTy::I32], &[ValTy::I32])])),
        Inst::TableFill { table } => ctx?.table_val_types
            .get(*table as usize)
            .map(|t| TR::new([FTy::of(&[ValTy::I32, *t, ValTy::I32], &[])])),
        _ => None,
    };
    if let Some(tr) = placeholder {
        return Some(tr);
    }

    // ---- Static table (concrete candidates, including the 5 immediate-less select candidates) ----
    crate::inst_types_gen::concrete_cands(inst)
}

/// Equivalent of Python's cur_params pre-check (compose(()->cur_params, cand)).
/// Live call chains always pass None (removed from the get_inst_ty_req signature per R-9); kept for unit-test
/// comparison of that branch's semantics.
#[cfg(test)]
fn cur_params_filter(tr: TR, cur_params: Option<&[ValTy]>) -> Option<TR> {
    match cur_params {
        None => Some(tr),
        Some(ps) => {
            let base = FTy::of(&[], ps);
            let kept: Vec<FTy> = tr
                .cands
                .into_iter()
                .filter(|c| compose(&base, c).is_some())
                .collect();
            if kept.is_empty() {
                None
            } else {
                Some(TR::new(kept))
            }
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn v(s: &[&str]) -> Vec<ValTy> {
        s.iter().map(|x| ValTy::parse(x).unwrap()).collect()
    }

    fn fty(p: &[&str], r: &[&str]) -> FTy {
        FTy::of(&v(p), &v(r))
    }

    // ---- S1 matrix (ported from upstream tests/test_funcType_add.py) ----

    #[test]
    fn s1_add_matrix() {
        let cases: Vec<(FTy, FTy, Option<FTy>)> = vec![
            (
                fty(&["i32", "i32"], &["i32"]),
                fty(&["i32", "i32"], &["i32"]),
                Some(fty(&["i32", "i32", "i32"], &["i32"])),
            ),
            (
                fty(&[], &["i32"]),
                fty(&[], &["i32"]),
                Some(fty(&[], &["i32", "i32"])),
            ),
            (
                fty(&["i32"], &["i32"]),
                fty(&["i32"], &["i32", "i32"]),
                Some(fty(&["i32"], &["i32", "i64"])),
            ),
        ];
        // The third expectation corrected per actual Python behavior: the result list is
        // val1.results prefix kept + val2.results (i64 pending; asserted separately below)
        let (a, b, _) = cases[2].clone();
        assert_eq!(compose(&a, &b).unwrap().results, v(&["i32", "i32"]));

        let (a, b, _) = cases[0].clone();
        let c = compose(&a, &b).unwrap();
        assert_eq!(c.params, v(&["i32", "i32", "i32"]));
        assert_eq!(c.results, v(&["i32"]));

        let (a, b, _) = cases[1].clone();
        let c = compose(&a, &b).unwrap();
        assert_eq!(c.params, v(&[]));
        assert_eq!(c.results, v(&["i32", "i32"]));

        // Resolution mismatch → None (upstream test_exception)
        assert_eq!(compose(&fty(&[], &["i32", "i32"]), &fty(&["i64", "i64", "i32"], &["i32"])), None);

        // Tail swap: val1's results consume val2's params (R1 longer)
        let c = compose(&fty(&[], &["i32", "i64"]), &fty(&[], &["f32", "f64"])).unwrap();
        assert_eq!(c.results, v(&["i32", "i64", "f32", "f64"]));

        // P2 remainder joins params (R1 empty)
        let c = compose(&fty(&["i32", "i64"], &[]), &fty(&["f32", "f64"], &[])).unwrap();
        assert_eq!(c.params, v(&["f32", "f64", "i32", "i64"]));
    }

    // ---- compose directionality (terminal / poly swallowing; rules settled by dual-implementation comparison) ----

    #[test]
    fn compose_terminal_skips_resolution() {
        // a terminal: resolution skipped, always succeeds, params=a, results=b (trial2707 scenario)
        let a = FTy {
            params: v(&["i32"]),
            results: v(&["i32"]),
            params_poly: true,
            results_poly: true,
            terminal: true,
        };
        let b = fty(&["f64", "f64"], &["f32"]);
        let c = compose(&a, &b).unwrap();
        assert_eq!(c.params, v(&["i32"]));
        assert_eq!(c.results, v(&["f32"]));
        // terminal propagates to the tail element: b non-terminal → false (rest expands per candidate)
        assert!(!c.terminal);
        // No resolution-failure path: even a mismatching overlap succeeds
        let b2 = fty(&["f64"], &[]);
        assert!(compose(&a, &b2).is_some());
    }

    #[test]
    fn compose_rp_swallows_p2_prefix() {
        // a.results_poly swallows the P2 remainder (an ANY-state subpath, trial3186 scenario)
        let a = FTy {
            params: v(&[]),
            results: v(&["i32"]),
            params_poly: true,
            results_poly: true,
            terminal: false,
        };
        let b = fty(&["i64", "i32"], &["i64"]);
        let c = compose(&a, &b).unwrap();
        assert_eq!(c.params, v(&[])); // P2 remainder [i64] swallowed
        assert_eq!(c.results, v(&["i64"]));
        // non-poly a keeps the P2 remainder
        let a2 = fty(&[], &["i32"]);
        let c2 = compose(&a2, &b).unwrap();
        assert_eq!(c2.params, v(&["i64"]));
    }

    #[test]
    fn compose_b_terminal_swallows_r1_prefix() {
        // b terminal swallows the R1 remainder; an ANY-state b (rp=true but terminal=false) does not (trial15)
        let a = fty(&["i32", "i64"], &["i32"]);
        let any_b = FTy {
            params: v(&[]),
            results: v(&["i32", "i64"]),
            params_poly: true,
            results_poly: true,
            terminal: false,
        };
        let c = compose(&a, &any_b).unwrap();
        assert_eq!(c.results, v(&["i32", "i32", "i64"])); // R1 prefix kept

        let term_b = FTy {
            params: v(&["i32"]),
            results: v(&["i32"]),
            params_poly: true,
            results_poly: true,
            terminal: true,
        };
        let c2 = compose(&a, &term_b).unwrap();
        assert_eq!(c2.results, v(&["i32"])); // R1 prefix swallowed
        assert!(c2.terminal);
    }

    #[test]
    fn compose_unreachable_constant() {
        let u = FTy::unreachable();
        // unreachable; i32.const → (any stack bottom)+[i32], terminal propagates to the tail element
        let c = compose(&u, &fty(&[], &["i32"])).unwrap();
        assert_eq!(c.params, v(&[]));
        assert_eq!(c.results, v(&["i32"]));
        assert!(c.params_poly && c.results_poly);
        assert!(!c.terminal);
        // i32.const; unreachable → back to the unreachable shape and terminal (trial335)
        let c2 = compose(&fty(&[], &["i32"]), &u).unwrap();
        assert!(c2.is_unreachable_shape() && c2.terminal);
        // unreachable after multi-candidate select: rest dedups to [[]] (set semantics on the judgment surface)
        let sel = TR::new((["f32", "i32", "i64", "f64", "v128"]).iter().map(|t| {
            FTy::of(&v(&[t, t, "i32"]), &v(&[t]))
        }));
        let m = merge(&sel, &TR::new([u]));
        assert_eq!(rest_types_view(&m), vec![vec![]]);
        assert_eq!(status_view(&m), StackStatus::Any);
    }

    // ---- satisfies (S2 matrix port) ----

    #[test]
    fn s2_match_matrix() {
        assert!(satisfies(
            &fty(&["i32", "i32", "i32"], &["i32"]),
            &fty(&["i32", "i32", "i32"], &["i32"])
        ));
        assert!(!satisfies(&fty(&["i32", "i32"], &["i32"]), &fty(&["i32"], &["i32"])));
        // eg_param_f: param truncation, exact results
        let req = FTy {
            params: v(&["i32", "i32"]),
            results: v(&["i32"]),
            params_poly: true,
            results_poly: false,
            terminal: false,
        };
        assert!(satisfies(&fty(&["f32", "i32", "i32"], &["i32"]), &req));
        let req2 = FTy {
            params: v(&["i32"]),
            results: v(&[]),
            params_poly: true,
            results_poly: false,
            terminal: false,
        };
        assert!(!satisfies(&fty(&["i32", "f32"], &["i32"]), &req2));
        // eg_param_and_result: truncation on both sides
        let req3 = FTy {
            params: v(&["i32"]),
            results: v(&[]),
            params_poly: true,
            results_poly: true,
            terminal: false,
        };
        assert!(!satisfies(&fty(&[], &["i32"]), &req3));
        // The unreachable shape matches everything (fixes the old undefined branch; upstream expects MATCH)
        assert!(satisfies(&fty(&["f64"], &["i32", "i32"]), &FTy::unreachable()));
    }

    #[test]
    fn tr_ordering_and_merge() {
        // Candidate ordering = Python sorted (string order): f32 < f64 < i32 < i64 < v128
        let tr = TR::new([
            FTy::of(&v(&["i32"]), &v(&["i32"])),
            FTy::of(&v(&["f32"]), &v(&["f32"])),
            FTy::of(&v(&["f32"]), &v(&["f32"])), // dedup
        ]);
        assert_eq!(tr.cands().len(), 2);
        assert_eq!(tr.ty0().params, v(&["f32"]));
        // merge failure eliminates → empty set
        let m = merge(&tr, &TR::new([fty(&["i64"], &[])]));
        assert!(m.impossible());
    }

    #[test]
    fn stack_state_roundtrip() {
        // ANY state round-trip as_type_req → ((), rest, T, T, terminal=false)
        let tr = TR::new([FTy::of(&[], &v(&["i32", "i64"]))]);
        let ss = StackState::from_tr(&tr);
        assert_eq!(ss.status, StackStatus::Normal);
        let back = ss.as_type_req();
        assert_eq!(back.cands().len(), 1);
        assert!(!back.ty0().params_poly);
        let ss2 = StackState::from_tr(&back);
        assert_eq!(ss2.all_rest_types, ss.all_rest_types);

        // support: ANY supports candidates with a longer tail
        let a = StackState {
            all_rest_types: vec![v(&["i32"])],
            status: StackStatus::Any,
        };
        let b = StackState {
            all_rest_types: vec![v(&["f32", "i32"])],
            status: StackStatus::Any,
        };
        assert!(sstate1_support_sstate2(&a, &b));
        assert!(!sstate1_support_sstate2(&b, &a));
        let n = StackState {
            all_rest_types: vec![v(&["i32"])],
            status: StackStatus::Normal,
        };
        assert!(!sstate1_support_sstate2(&n, &b));
    }

    // ---- get_inst_ty_req hand-written branches ----

    fn ctx_for_test() -> Context {
        Context {
            local_types: v(&["i32", "f64"]),
            func_type_idxs: vec![0],
            types: vec![(v(&["i32"]), v(&["i64"]))],
            cur_func_ty: (v(&["i32"]), v(&["i64"])),
            global_val_types: v(&["f32"]),
            table_val_types: v(&["funcref"]),
            label_types: Some(vec![v(&["i64"])]),
        }
    }

    #[test]
    fn req_unreachable_and_return() {
        assert!(get_inst_ty_req(&Inst::Unreachable, None)
            .unwrap()
            .ty0()
            .is_unreachable_shape());
        // return: (R, R, T, T, terminal)
        let tr = get_inst_ty_req(&Inst::Return, Some(&ctx_for_test())).unwrap();
        let c = tr.ty0();
        assert_eq!(c.params, v(&["i64"]));
        assert_eq!(c.results, v(&["i64"]));
        assert!(c.params_poly && c.results_poly && c.terminal);
    }

    #[test]
    fn req_br_family() {
        let ctx = ctx_for_test();
        let br = get_inst_ty_req(
            &Inst::Br { relative_depth: 0 }, Some(&ctx)).unwrap();
        let c = br.ty0();
        assert_eq!(c.params, v(&["i64"]));
        assert!(c.terminal && c.params_poly && c.results_poly);
        // br depth out of range → None (Python raises IndexError; semantics = no knowledge)
        assert!(get_inst_ty_req(&Inst::Br { relative_depth: 5 }, Some(&ctx)).is_none());
        // br_if: (L+[i32], L, T, F, false)
        let br_if = get_inst_ty_req(
            &Inst::BrIf { relative_depth: 0 }, Some(&ctx)).unwrap();
        let c = br_if.ty0();
        assert_eq!(c.params, v(&["i64", "i32"]));
        assert_eq!(c.results, v(&["i64"]));
        assert!(c.params_poly && !c.results_poly && !c.terminal);
        // No label context → None
        let mut ctx2 = ctx_for_test();
        ctx2.label_types = None;
        assert!(get_inst_ty_req(&Inst::Br { relative_depth: 0 }, Some(&ctx2)).is_none());
    }

    #[test]
    fn req_call_and_placeholder() {
        let ctx = ctx_for_test();
        let call = get_inst_ty_req(
            &Inst::Call { function_index: 0 }, Some(&ctx)).unwrap();
        assert_eq!(call.ty0().params, v(&["i32"]));
        assert_eq!(call.ty0().results, v(&["i64"]));
        assert!(get_inst_ty_req(
            &Inst::Call { function_index: 9 }, Some(&ctx)).is_none());
        // call_indirect: append i32 to the param tail
        let ci = get_inst_ty_req(
            &Inst::CallIndirect { type_index: 0, table_index: 0 },
            Some(&ctx)).unwrap();
        assert_eq!(ci.ty0().params, v(&["i32", "i32"]));
        // local.get: typed from context
        let lg = get_inst_ty_req(
            &Inst::LocalGet { local_index: 1 }, Some(&ctx)).unwrap();
        assert_eq!(lg.ty0().results, v(&["f64"]));
        assert!(get_inst_ty_req(
            &Inst::LocalGet { local_index: 9 }, Some(&ctx)).is_none());
        // table.get: [i32] -> [elem type]
        let tg = get_inst_ty_req(
            &Inst::TableGet { table: 0 }, Some(&ctx)).unwrap();
        assert_eq!(tg.ty0().params, v(&["i32"]));
        assert_eq!(tg.ty0().results, v(&["funcref"]));
        // ref.null funcref
        let rn = get_inst_ty_req(
            &Inst::RefNull { hty: wasmparser::HeapType::Abstract {
                shared: false, ty: wasmparser::AbstractHeapType::Func } },
            None).unwrap();
        assert_eq!(rn.ty0().results, v(&["funcref"]));
    }

    #[test]
    fn req_select_and_cur_params() {
        // Immediate-less select goes through the static table (5 candidates)
        let sel = get_inst_ty_req(&Inst::Select, None).unwrap();
        assert_eq!(sel.cands().len(), 5);
        assert_eq!(sel.ty0().results, v(&["f32"])); // ordering: f32 smallest
        // typed select: [t t i32]->[t]
        let ts = get_inst_ty_req(
            &Inst::TypedSelect { ty: wasmparser::ValType::I64 }, None)
            .unwrap();
        let c = ts.ty0();
        assert_eq!(c.params, v(&["i64", "i64", "i32"]));
        assert_eq!(c.results, v(&["i64"]));
        // cur_params pre-check (R-9 corrigendum: the original assertion spun empty through the TypedSelect
        // manual branch — Python's manual branch does no pre-check, and the always-None parameter was
        // removed from the signature. Here we compare directly against cur_params_filter: the stack
        // already holds [i32,f32] which mismatches the i64 typed select's params [i64,i64,i32]
        // → candidate eliminated, None; matching params are kept).
        let ts_all = get_inst_ty_req(
            &Inst::TypedSelect { ty: wasmparser::ValType::I64 }, None)
            .unwrap();
        assert!(cur_params_filter(ts_all, Some(&v(&["i32", "f32"]))).is_none());
        let ts2 = get_inst_ty_req(
            &Inst::TypedSelect { ty: wasmparser::ValType::I64 }, None)
            .unwrap();
        let filtered = cur_params_filter(ts2, Some(&v(&["i64", "i64", "i32"]))).unwrap();
        assert!(!filtered.impossible());
        // block-family returns None (handled at the AST layer)
        assert!(get_inst_ty_req(
            &Inst::Block { blockty: wasmparser::BlockType::Empty },
            None).is_none());
    }
}
