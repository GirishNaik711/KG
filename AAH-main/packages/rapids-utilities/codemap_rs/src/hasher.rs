//! Content hashing with xxhash64.
//!
//! xxhash is extremely fast (>10 GB/s on modern CPUs) and produces
//! good distribution for change detection. We don't need cryptographic
//! strength — just collision resistance for file deduplication.

use xxhash_rust::xxh64;

/// Hash file content and return hex string.
pub fn xxhash64(content: &[u8]) -> String {
    format!("{:016x}", xxh64::xxh64(content, 0))
}
