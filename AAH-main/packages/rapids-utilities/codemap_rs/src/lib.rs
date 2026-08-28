//! CodeMap Rust Core: high-performance file walking, hashing, and skeleton parsing.
//!
//! This module provides the hot-path operations that benefit most from Rust:
//! - Parallel file walking with .gitignore respect (via `ignore` crate)
//! - Content hashing with xxhash (zero-copy, SIMD-accelerated)
//! - Tree-sitter parsing (tree-sitter IS Rust — no FFI overhead)
//! - Skeleton symbol extraction (top-level names + kinds + locations)
//!
//! All results are returned to Python as dicts via PyO3. The Python side
//! handles SQLite writes, tier management, vector indexing, and agent tools.
//!
//! Performance target: 500K files in <3 minutes on 8 cores.

use pyo3::prelude::*;
use pyo3::types::{PyDict, PyList};
use rayon::prelude::*;
use std::path::{Path, PathBuf};
use std::sync::atomic::{AtomicUsize, Ordering};

mod walker;
mod hasher;
mod extractor;

/// A parsed file result returned to Python.
#[derive(Debug, Clone)]
struct FileResult {
    path: String,           // relative path
    language: String,
    content_hash: String,
    size_bytes: u64,
    line_count: u32,
    symbols: Vec<SymbolResult>,
}

/// A skeleton symbol result.
#[derive(Debug, Clone)]
struct SymbolResult {
    fqn: String,
    name: String,
    kind: String,       // "function", "class", "method", "interface", etc.
    start_line: u32,
    end_line: u32,
    is_exported: bool,
}

/// Detect language from file extension.
fn detect_language(path: &Path) -> Option<&'static str> {
    match path.extension()?.to_str()? {
        "py" | "pyi" => Some("python"),
        "ts" => Some("typescript"),
        "tsx" => Some("tsx"),
        "js" | "mjs" | "cjs" => Some("javascript"),
        "jsx" => Some("jsx"),
        "java" => Some("java"),
        "go" => Some("go"),
        "rs" => Some("rust"),
        "c" | "h" => Some("c"),
        "cpp" | "hpp" | "cc" => Some("cpp"),
        "cs" => Some("csharp"),
        "rb" => Some("ruby"),
        "php" => Some("php"),
        "swift" => Some("swift"),
        "kt" | "kts" => Some("kotlin"),
        "scala" => Some("scala"),
        _ => None,
    }
}

/// Build FQN from file path and symbol name.
fn build_fqn(relative_path: &str, name: &str) -> String {
    let module = relative_path
        .replace('/', ".")
        .replace('\\', ".");
    // Strip extension
    let module = if let Some(pos) = module.rfind('.') {
        let ext = &module[pos..];
        if [".py", ".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs",
            ".java", ".go", ".rs", ".c", ".h", ".cpp", ".cs",
            ".rb", ".php", ".swift", ".kt", ".scala"]
            .contains(&ext)
        {
            &module[..pos]
        } else {
            &module
        }
    } else {
        &module
    };
    format!("{}.{}", module, name)
}

// =========================================================================
// PyO3 Python Module
// =========================================================================

/// Tier 0: Parallel inventory scan. Returns list of file metadata dicts.
///
/// Args:
///     root: Absolute path to codebase root
///     max_file_size_kb: Skip files larger than this (default 500)
///
/// Returns:
///     List of dicts: [{path, language, content_hash, size_bytes, line_count}]
#[pyfunction]
#[pyo3(signature = (root, max_file_size_kb=500))]
fn scan_inventory(py: Python<'_>, root: &str, max_file_size_kb: u64) -> PyResult<Vec<PyObject>> {
    let root_path = PathBuf::from(root);
    let max_bytes = max_file_size_kb * 1024;

    // Collect all source file paths using the `ignore` crate
    // (respects .gitignore automatically)
    let files: Vec<PathBuf> = walker::walk_source_files(&root_path);

    let counter = AtomicUsize::new(0);

    // Parallel hash + metadata extraction
    let results: Vec<FileResult> = files
        .par_iter()
        .filter_map(|path| {
            let language = detect_language(path)?;
            let metadata = std::fs::metadata(path).ok()?;
            let size = metadata.len();

            if size > max_bytes {
                return None;
            }

            let content = std::fs::read(path).ok()?;
            let hash = hasher::xxhash64(&content);
            let line_count = bytecount::count(&content, b'\n') as u32 + 1;
            let relative = path.strip_prefix(&root_path).ok()?;

            counter.fetch_add(1, Ordering::Relaxed);

            Some(FileResult {
                path: relative.to_string_lossy().to_string(),
                language: language.to_string(),
                content_hash: hash,
                size_bytes: size,
                line_count,
                symbols: vec![],
            })
        })
        .collect();

    // Convert to Python dicts
    let py_results: Vec<PyObject> = results
        .iter()
        .map(|r| {
            let dict = PyDict::new_bound(py);
            dict.set_item("path", &r.path).unwrap();
            dict.set_item("language", &r.language).unwrap();
            dict.set_item("content_hash", &r.content_hash).unwrap();
            dict.set_item("size_bytes", r.size_bytes).unwrap();
            dict.set_item("line_count", r.line_count).unwrap();
            dict.set_item("tier", 0u32).unwrap();
            dict.unbind().into_any()
        })
        .collect();

    Ok(py_results)
}

/// Tier 1: Parallel skeleton parse. Returns file metadata + top-level symbols.
///
/// Args:
///     root: Absolute path to codebase root
///     file_paths: Optional list of specific files to parse (relative paths).
///                 If None, parses all source files.
///     max_file_size_kb: Skip files larger than this
///
/// Returns:
///     List of dicts with 'symbols' key containing skeleton symbol data.
#[pyfunction]
#[pyo3(signature = (root, file_paths=None, max_file_size_kb=500))]
fn parse_skeleton(
    py: Python<'_>,
    root: &str,
    file_paths: Option<Vec<String>>,
    max_file_size_kb: u64,
) -> PyResult<Vec<PyObject>> {
    let root_path = PathBuf::from(root);
    let max_bytes = max_file_size_kb * 1024;

    let files: Vec<PathBuf> = match file_paths {
        Some(paths) => paths.iter().map(|p| root_path.join(p)).collect(),
        None => walker::walk_source_files(&root_path),
    };

    let results: Vec<FileResult> = files
        .par_iter()
        .filter_map(|path| {
            let language = detect_language(path)?;
            let content = std::fs::read(path).ok()?;

            if content.len() as u64 > max_bytes {
                return None;
            }

            let hash = hasher::xxhash64(&content);
            let line_count = bytecount::count(&content, b'\n') as u32 + 1;
            let relative = path.strip_prefix(&root_path).ok()?;
            let rel_str = relative.to_string_lossy().to_string();

            // Parse with tree-sitter and extract skeleton
            let symbols = extractor::extract_skeleton(language, &content, &rel_str);

            Some(FileResult {
                path: rel_str,
                language: language.to_string(),
                content_hash: hash,
                size_bytes: content.len() as u64,
                line_count,
                symbols,
            })
        })
        .collect();

    // Convert to Python dicts
    let py_results: Vec<PyObject> = results
        .iter()
        .map(|r| {
            let dict = PyDict::new_bound(py);
            dict.set_item("path", &r.path).unwrap();
            dict.set_item("language", &r.language).unwrap();
            dict.set_item("content_hash", &r.content_hash).unwrap();
            dict.set_item("size_bytes", r.size_bytes).unwrap();
            dict.set_item("line_count", r.line_count).unwrap();
            dict.set_item("tier", 1u32).unwrap();

            // Symbols as list of dicts
            let symbols: Vec<Py<PyAny>> = r.symbols.iter().map(|s| {
                let sdict = PyDict::new_bound(py);
                sdict.set_item("fqn", &s.fqn).unwrap();
                sdict.set_item("name", &s.name).unwrap();
                sdict.set_item("kind", &s.kind).unwrap();
                sdict.set_item("language", &r.language).unwrap();
                sdict.set_item("file_path", &r.path).unwrap();
                sdict.set_item("start_line", s.start_line).unwrap();
                sdict.set_item("end_line", s.end_line).unwrap();
                sdict.set_item("is_exported", s.is_exported).unwrap();
                sdict.set_item("tier", 1u32).unwrap();
                sdict.unbind().into_any()
            }).collect();

            let sym_list = PyList::new_bound(py, &symbols);
            dict.set_item("symbols", sym_list).unwrap();
            dict.unbind().into_any()
        })
        .collect();

    Ok(py_results)
}

/// Detect changes: compare current file hashes against stored hashes.
///
/// Args:
///     root: Codebase root path
///     stored_hashes: Dict of {relative_path: content_hash} from database
///
/// Returns:
///     Dict with keys: added, modified, deleted, unchanged (lists of paths)
#[pyfunction]
fn detect_changes(
    py: Python<'_>,
    root: &str,
    stored_hashes: std::collections::HashMap<String, String>,
) -> PyResult<PyObject> {
    let root_path = PathBuf::from(root);
    let files = walker::walk_source_files(&root_path);

    let mut current: std::collections::HashMap<String, String> = std::collections::HashMap::new();
    let mut added = Vec::new();
    let mut modified = Vec::new();
    let mut unchanged = Vec::new();

    // Parallel hash computation
    let hashed: Vec<(String, String)> = files
        .par_iter()
        .filter_map(|path| {
            let content = std::fs::read(path).ok()?;
            let relative = path.strip_prefix(&root_path).ok()?;
            let rel_str = relative.to_string_lossy().to_string();
            let hash = hasher::xxhash64(&content);
            Some((rel_str, hash))
        })
        .collect();

    for (rel, hash) in hashed {
        current.insert(rel.clone(), hash.clone());
        match stored_hashes.get(&rel) {
            None => added.push(rel),
            Some(old_hash) if old_hash != &hash => modified.push(rel),
            _ => unchanged.push(rel),
        }
    }

    let deleted: Vec<String> = stored_hashes
        .keys()
        .filter(|k| !current.contains_key(*k))
        .cloned()
        .collect();

    let dict = PyDict::new_bound(py);
    dict.set_item("added", added)?;
    dict.set_item("modified", modified)?;
    dict.set_item("deleted", deleted)?;
    dict.set_item("unchanged_count", unchanged.len())?;

    Ok(dict.unbind().into_any())
}

/// Python module definition.
#[pymodule]
fn codemap_rs(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_function(wrap_pyfunction!(scan_inventory, m)?)?;
    m.add_function(wrap_pyfunction!(parse_skeleton, m)?)?;
    m.add_function(wrap_pyfunction!(detect_changes, m)?)?;
    Ok(())
}
