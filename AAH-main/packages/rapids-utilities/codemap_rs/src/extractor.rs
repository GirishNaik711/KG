//! Skeleton symbol extractor using tree-sitter.
//!
//! Extracts top-level symbols (functions, classes, interfaces, enums)
//! from source code using tree-sitter grammars. This is the Tier 1
//! extraction — fast, cross-language, names + kinds + locations only.
//!
//! Tree-sitter is native Rust, so there's zero FFI overhead here.
//! Each language grammar is compiled into the binary at build time.

use tree_sitter::{Language, Parser, Node};
use super::{SymbolResult, build_fqn};

/// Get tree-sitter Language for a language name.
fn get_language(lang: &str) -> Option<Language> {
    match lang {
        "python" => Some(tree_sitter_python::LANGUAGE.into()),
        "typescript" => Some(tree_sitter_typescript::LANGUAGE_TYPESCRIPT.into()),
        "tsx" => Some(tree_sitter_typescript::LANGUAGE_TSX.into()),
        "javascript" | "jsx" => Some(tree_sitter_javascript::LANGUAGE.into()),
        "java" => Some(tree_sitter_java::LANGUAGE.into()),
        "go" => Some(tree_sitter_go::LANGUAGE.into()),
        "rust" => Some(tree_sitter_rust::LANGUAGE.into()),
        _ => None,
    }
}

/// Extract skeleton symbols from source code.
/// Returns a Vec of SymbolResult with names, kinds, and locations.
pub fn extract_skeleton(lang: &str, source: &[u8], file_path: &str) -> Vec<SymbolResult> {
    let ts_lang = match get_language(lang) {
        Some(l) => l,
        None => return vec![],
    };

    let mut parser = Parser::new();
    parser.set_language(&ts_lang).ok();

    let tree = match parser.parse(source, None) {
        Some(t) => t,
        None => return vec![],
    };

    let mut symbols = Vec::new();
    extract_from_node(tree.root_node(), source, file_path, lang, &mut symbols, 0);
    symbols
}

/// Recursively extract symbols from a tree-sitter node.
/// Depth-limited to avoid deep recursion into function bodies.
fn extract_from_node(
    node: Node,
    source: &[u8],
    file_path: &str,
    lang: &str,
    symbols: &mut Vec<SymbolResult>,
    depth: u32,
) {
    if depth > 3 {
        return;
    }

    let mut cursor = node.walk();
    for child in node.children(&mut cursor) {
        let mut actual = child;
        let mut is_exported = false;

        // Unwrap export/decorator wrappers
        match child.kind() {
            "export_statement" | "decorated_definition" => {
                is_exported = child.kind() == "export_statement";
                let mut inner_cursor = child.walk();
                let found = child.children(&mut inner_cursor).find(|c| {
                    is_function_like(c.kind()) || is_class_like(c.kind())
                });
                match found {
                    Some(inner) => actual = inner,
                    None => {
                        extract_from_node(child, source, file_path, lang, symbols, depth);
                        continue;
                    }
                }
            }
            _ => {}
        }

        if is_function_like(actual.kind()) {
            if let Some(name) = get_name_field(&actual, source) {
                let kind = if depth > 0 { "method" } else { "function" };
                symbols.push(SymbolResult {
                    fqn: build_fqn(file_path, &name),
                    name,
                    kind: kind.to_string(),
                    start_line: actual.start_position().row as u32 + 1,
                    end_line: actual.end_position().row as u32 + 1,
                    is_exported,
                });
            }
        } else if is_class_like(actual.kind()) {
            if let Some(name) = get_name_field(&actual, source) {
                let kind = match actual.kind() {
                    "interface_declaration" => "interface",
                    "enum_declaration" => "enum",
                    "type_alias_declaration" => "type_alias",
                    "struct_item" => "class",  // Rust struct
                    "trait_item" => "interface", // Rust trait
                    "impl_item" => "class",     // Rust impl
                    _ => "class",
                };
                symbols.push(SymbolResult {
                    fqn: build_fqn(file_path, &name),
                    name,
                    kind: kind.to_string(),
                    start_line: actual.start_position().row as u32 + 1,
                    end_line: actual.end_position().row as u32 + 1,
                    is_exported,
                });

                // Recurse into class body for methods
                if let Some(body) = actual.child_by_field_name("body") {
                    extract_from_node(body, source, file_path, lang, symbols, depth + 1);
                }
            }
        } else if matches!(actual.kind(), "block" | "program" | "module" | "source_file") {
            extract_from_node(actual, source, file_path, lang, symbols, depth);
        }
    }
}

fn is_function_like(kind: &str) -> bool {
    matches!(kind,
        "function_definition" | "function_declaration" | "method_definition" |
        "method_declaration" | "function_item" | "generator_function_declaration"
    )
}

fn is_class_like(kind: &str) -> bool {
    matches!(kind,
        "class_definition" | "class_declaration" | "struct_item" |
        "interface_declaration" | "enum_declaration" | "type_alias_declaration" |
        "trait_item" | "impl_item"
    )
}

/// Extract the "name" field from a tree-sitter node.
fn get_name_field(node: &Node, source: &[u8]) -> Option<String> {
    let name_node = node.child_by_field_name("name")?;
    let name = &source[name_node.start_byte()..name_node.end_byte()];
    Some(String::from_utf8_lossy(name).to_string())
}
