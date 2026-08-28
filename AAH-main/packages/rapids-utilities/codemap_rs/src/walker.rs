//! Parallel file walker using the `ignore` crate.
//!
//! The `ignore` crate is from the same author as ripgrep. It natively
//! respects .gitignore, .ignore, and global gitignore — no manual
//! pattern matching needed.

use ignore::WalkBuilder;
use std::path::{Path, PathBuf};

/// Walk all source files under root, respecting .gitignore.
/// Returns absolute paths.
pub fn walk_source_files(root: &Path) -> Vec<PathBuf> {
    let walker = WalkBuilder::new(root)
        .hidden(true)           // skip hidden files/dirs
        .git_ignore(true)       // respect .gitignore
        .git_global(true)       // respect global gitignore
        .git_exclude(true)      // respect .git/info/exclude
        .require_git(false)     // work even outside git repos
        .threads(num_cpus::get()) // parallel directory walking
        .build();

    walker
        .filter_map(|entry| {
            let entry = entry.ok()?;
            if !entry.file_type()?.is_file() {
                return None;
            }
            let path = entry.into_path();
            // Only return source files (has recognized language extension)
            if super::detect_language(&path).is_some() {
                Some(path)
            } else {
                None
            }
        })
        .collect()
}
