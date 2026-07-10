//! Safety-focused core primitives for `worktrees-hives`.
//!
//! The module boundaries are established in the R1 scaffold. Their behavior is
//! implemented by the linked foundation issues.

pub mod contract;
pub mod error;
pub mod git_safe;
pub mod paths;
pub mod state;
pub mod worktree;

/// Version shared by the core library and CLI workspace packages.
pub const VERSION: &str = env!("CARGO_PKG_VERSION");
