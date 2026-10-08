//! M10.1 instruction-sequence reduction: the element model and the value-operand graph.
//!
//! Mirrors Python (the post-round-4-cleanup baseline):
//! - element model: the live subset of `ReduceInsts_V5_util.py`
//!   (OneElem/StackChange/ElemTypeInfo; the group model degenerates to an "element list");
//! - value-operand graph: `ElemOperandMappingV2.py`
//!   (VopWT compares by object identity → Rust does the equivalent with arena indices, D-section ruling;
//!   `force_by_taken_ops` always True — stack type mismatches are forced rather than errored, actual behavior copied;
//!   `init_is_given_any_type` always False; that branch is not ported).

pub mod cf_elem;
pub mod cfn_stage;
pub mod core_stage;
pub mod elem;
pub mod eq_subgraph;
pub mod graph_helper;
pub mod mutation;
pub mod p3;
pub mod reduce_ctx;
pub mod rev_stage;
pub mod shrink_block;
pub mod simple_cycles;
pub mod trial;
pub mod ur;
pub mod v9;
pub mod vop;

// The re-export surface = names actually used externally (crates and tests outside instseq; narrowed
// per R-15; everything else goes through full `instseq::<submodule>::` paths).
pub use reduce_ctx::{
    build_one_node_list_reduction_ctx, get_raw_elems_from_env,
};
pub use eq_subgraph::get_eq_sgs;
pub use trial::{snapshot_insts_of, validate_wasm_bytes, NodeListTrialApplier};
