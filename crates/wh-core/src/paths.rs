//! Platform-aware data paths and sandbox validation.
//!
//! Worktree path derivation and escape prevention are implemented by GitHub #25.

use std::path::{Path, PathBuf};

/// Resolve the platform default user data directory.
///
/// - Windows: `%APPDATA%` (fallback: `%USERPROFILE%\AppData\Roaming`)
/// - macOS: `~/Library/Application Support`
/// - Unix/Linux: `$XDG_DATA_HOME` or `~/.local/share`
#[must_use]
pub fn user_data_dir() -> PathBuf {
    #[cfg(windows)]
    {
        if let Some(appdata) = std::env::var_os("APPDATA") {
            return PathBuf::from(appdata);
        }
        if let Some(profile) = std::env::var_os("USERPROFILE") {
            return PathBuf::from(profile).join("AppData").join("Roaming");
        }
        return std::env::temp_dir();
    }

    #[cfg(target_os = "macos")]
    {
        if let Some(home) = std::env::var_os("HOME") {
            return PathBuf::from(home)
                .join("Library")
                .join("Application Support");
        }
        return std::env::temp_dir();
    }

    #[cfg(not(any(windows, target_os = "macos")))]
    {
        if let Some(xdg) = std::env::var_os("XDG_DATA_HOME") {
            return PathBuf::from(xdg);
        }
        if let Some(home) = std::env::var_os("HOME") {
            return PathBuf::from(home).join(".local").join("share");
        }
        std::env::temp_dir()
    }
}

/// Named root for worktrees-hives durable state under the user data directory.
///
/// Default layout: `{user_data_dir}/worktrees-hives/`.
#[derive(Debug, Clone, Eq, PartialEq)]
pub struct StateRoot {
    path: PathBuf,
}

impl StateRoot {
    /// Default state root under the platform user-data directory.
    #[must_use]
    pub fn default_root() -> Self {
        Self {
            path: user_data_dir().join("worktrees-hives"),
        }
    }

    /// Construct a state root from an explicit directory.
    #[must_use]
    pub fn from_path(path: impl Into<PathBuf>) -> Self {
        Self { path: path.into() }
    }

    /// Absolute path to this state root.
    #[must_use]
    pub fn as_path(&self) -> &Path {
        &self.path
    }

    /// Path to the watched-jobs store (`watched.json`) under this root.
    #[must_use]
    pub fn watched_json(&self) -> PathBuf {
        self.path.join("watched.json")
    }
}

/// Resolve the watched-jobs state path from an optional `WH_STATE_PATH` override.
///
/// When `wh_state_path` is `Some`, that value is used (same as setting the env var).
/// Otherwise defaults to [`StateRoot::default_root()`]'s `watched.json`.
#[must_use]
pub fn resolve_state_path(wh_state_path: Option<&str>) -> PathBuf {
    if let Some(custom) = wh_state_path {
        return PathBuf::from(custom);
    }
    StateRoot::default_root().watched_json()
}

/// Resolve the path to the watched-jobs state file.
///
/// Honours `WH_STATE_PATH` if set; otherwise defaults to
/// [`StateRoot::default_root()`]'s `watched.json`.
#[must_use]
pub fn state_path() -> PathBuf {
    resolve_state_path(std::env::var("WH_STATE_PATH").ok().as_deref())
}

#[cfg(test)]
mod tests {
    use super::{StateRoot, resolve_state_path, user_data_dir};

    #[test]
    fn user_data_dir_is_non_empty() {
        let dir = user_data_dir();
        assert!(!dir.as_os_str().is_empty());
    }

    #[test]
    fn state_root_watched_json_joins_filename() {
        let root = StateRoot::from_path("/tmp/wh-state");
        assert_eq!(
            root.watched_json(),
            std::path::PathBuf::from("/tmp/wh-state/watched.json")
        );
    }

    #[test]
    fn resolve_state_path_honours_wh_state_path_override() {
        let path = resolve_state_path(Some("/tmp/acme/watched.json"));
        assert_eq!(path, std::path::PathBuf::from("/tmp/acme/watched.json"));
    }

    #[test]
    fn resolve_state_path_default_uses_state_root() {
        let path = resolve_state_path(None);
        assert!(path.ends_with("worktrees-hives/watched.json") || path.ends_with("worktrees-hives\\watched.json"));
    }
}
