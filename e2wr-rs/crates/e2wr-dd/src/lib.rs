//! e2wr-dd: delta debugging infrastructure (M1).
//!
//! Components:
//! - [`oracle`]: oracle-script runner (runs `<oracle> <wasm>` via a shell, with timeout control);
//! - [`probdd`]: ProbDD probabilistic delta deletion (with online inference of the initial probability p0);
//! - [`factory`]: ProbDD factory configuration surface (Python `ProbDDFactory`).

pub mod factory;
pub mod oracle;
pub mod probdd;

pub use oracle::{Oracle, ORACLE_TIMEOUT};
