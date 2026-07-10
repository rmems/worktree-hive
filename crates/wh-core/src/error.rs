//! Shared error definitions.

use std::fmt::{Display, Formatter};
use std::io;
use std::path::PathBuf;

/// Result alias for core operations.
pub type Result<T> = std::result::Result<T, Error>;

/// Errors returned by core primitives.
#[derive(Debug)]
pub enum Error {
    /// A path segment was invalid for sandbox derivation.
    InvalidSegment {
        field: &'static str,
        value: String,
    },
    /// A candidate path escaped or violated sandbox rules.
    SandboxViolation {
        base: PathBuf,
        candidate: PathBuf,
        reason: &'static str,
    },
    /// A filesystem operation failed.
    Io {
        context: &'static str,
        source: io::Error,
    },
    /// A git subprocess command failed.
    GitCommand {
        args: Vec<String>,
        stderr: String,
    },
}

impl Display for Error {
    fn fmt(&self, f: &mut Formatter<'_>) -> std::fmt::Result {
        match self {
            Self::InvalidSegment { field, value } => {
                write!(f, "invalid {field} segment: `{value}`")
            }
            Self::SandboxViolation {
                base,
                candidate,
                reason,
            } => write!(
                f,
                "sandbox violation for `{}` under `{}`: {reason}",
                candidate.display(),
                base.display()
            ),
            Self::Io { context, source } => write!(f, "{context}: {source}"),
            Self::GitCommand { args, stderr } => {
                write!(
                    f,
                    "git command failed (`git {}`): {}",
                    args.join(" "),
                    stderr.trim()
                )
            }
        }
    }
}

impl std::error::Error for Error {
    fn source(&self) -> Option<&(dyn std::error::Error + 'static)> {
        match self {
            Self::Io { source, .. } => Some(source),
            _ => None,
        }
    }
}
