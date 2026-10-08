//! Handwritten part of the instruction representation (generated enums and conversions live in `inst_gen.rs`).
//!
//! Instruction-level semantic accessors (index-space references etc., used by M4 reference detection)
//! are added in `refs.rs` as needed; reducer code never touches raw bytes.

/// Owned br_table target list (wasmparser's `BrTable<'a>` borrows the byte stream;
/// collected into a Vec here).
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct BrTableData {
    pub targets: Vec<u32>,
    pub default: u32,
}

pub use crate::inst_gen::Inst;
