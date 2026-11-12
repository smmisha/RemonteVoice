//! App-owned IPA copies kept for automatic renewal.
//!
//! Renewal must re-sign the exact bytes the user approved. The staging snapshot is
//! temporary, so an enabled tracked app keeps a private copy under the app data
//! directory and its SHA-256 is verified again before every renewal attempt.
use crate::{error::AppError, ipa};
use std::{
    collections::HashSet,
    fs::{File, OpenOptions},
    io::{Read, Write},
    path::{Path, PathBuf},
    sync::atomic::{AtomicU64, Ordering},
};

const MAX_ARCHIVED_BYTES: u64 = 2 * 1024 * 1024 * 1024;
static ARCHIVE_SEQUENCE: AtomicU64 = AtomicU64::new(0);

fn archive_error() -> AppError {
    AppError::new(
        "renewal_ipa",
        "Копия IPA для автопродления отсутствует или повреждена. Выберите IPA заново.",
    )
}

fn valid_sha256(value: &str) -> bool {
    value.len() == 64 && value.bytes().all(|b| b.is_ascii_hexdigit())
}

pub fn directory(data_directory: &Path) -> PathBuf {
    data_directory.join("ipa-archive")
}

pub fn path_for(directory: &Path, sha256: &str) -> Result<PathBuf, AppError> {
    if !valid_sha256(sha256) {
        return Err(archive_error());
    }
    Ok(directory.join(format!("{sha256}.ipa")))
}

fn prepare_directory(directory: &Path) -> Result<(), AppError> {
    std::fs::create_dir_all(directory).map_err(|_| archive_error())?;
    let metadata = std::fs::symlink_metadata(directory).map_err(|_| archive_error())?;
    if !metadata.is_dir() || metadata.file_type().is_symlink() {
        return Err(archive_error());
    }
    Ok(())
}

fn verified(path: &Path, sha256: &str) -> Result<(), AppError> {
    let metadata = std::fs::symlink_metadata(path).map_err(|_| archive_error())?;
    if !metadata.is_file()
        || metadata.file_type().is_symlink()
        || metadata.len() > MAX_ARCHIVED_BYTES
    {
        return Err(archive_error());
    }
    if ipa::fingerprint(path).map_err(|_| archive_error())? != sha256 {
        return Err(archive_error());
    }
    Ok(())
}

/// Copy `source` into the archive unless a verified copy already exists.
pub fn ensure(directory: &Path, source: &Path, sha256: &str) -> Result<PathBuf, AppError> {
    let destination = path_for(directory, sha256)?;
    prepare_directory(directory)?;
    match std::fs::symlink_metadata(&destination) {
        Ok(_) => {
            verified(&destination, sha256)?;
            return Ok(destination);
        }
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => {}
        Err(_) => return Err(archive_error()),
    }
    let source_metadata = std::fs::symlink_metadata(source).map_err(|_| archive_error())?;
    if !source_metadata.is_file() || source_metadata.len() > MAX_ARCHIVED_BYTES {
        return Err(archive_error());
    }
    let mut input = File::open(source).map_err(|_| archive_error())?;
    let stamp = std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map_err(|_| archive_error())?
        .as_nanos();
    let temporary = directory.join(format!(
        ".tmp-{}-{stamp}-{}",
        std::process::id(),
        ARCHIVE_SEQUENCE.fetch_add(1, Ordering::Relaxed)
    ));
    let result = (|| {
        let mut output = OpenOptions::new()
            .write(true)
            .read(true)
            .create_new(true)
            .open(&temporary)
            .map_err(|_| archive_error())?;
        let copied = std::io::copy(
            &mut Read::by_ref(&mut input).take(MAX_ARCHIVED_BYTES + 1),
            &mut output,
        )
        .map_err(|_| archive_error())?;
        if copied > MAX_ARCHIVED_BYTES {
            return Err(archive_error());
        }
        output.flush().map_err(|_| archive_error())?;
        output.sync_all().map_err(|_| archive_error())?;
        drop(output);
        verified(&temporary, sha256)?;
        match std::fs::rename(&temporary, &destination) {
            Ok(()) => Ok(destination.clone()),
            Err(_) => {
                // Another copy may have appeared; accept it only if it matches.
                verified(&destination, sha256)?;
                Ok(destination.clone())
            }
        }
    })();
    if result.is_err() {
        let _ = std::fs::remove_file(&temporary);
    }
    result
}

/// Return the archived path only when it still matches the approved digest.
pub fn verify(directory: &Path, sha256: &str) -> Result<PathBuf, AppError> {
    let path = path_for(directory, sha256)?;
    verified(&path, sha256)?;
    Ok(path)
}

/// Remove archived IPAs and leftover temporary copies that no tracked app uses.
pub fn cleanup(directory: &Path, keep: &HashSet<String>) {
    let Ok(entries) = std::fs::read_dir(directory) else {
        return;
    };
    for entry in entries.flatten() {
        let path = entry.path();
        let Ok(metadata) = std::fs::symlink_metadata(&path) else {
            continue;
        };
        if !metadata.is_file() || metadata.file_type().is_symlink() {
            continue;
        }
        let name = path
            .file_name()
            .and_then(|value| value.to_str())
            .unwrap_or("");
        if name.starts_with(".tmp-") {
            let _ = std::fs::remove_file(path);
            continue;
        }
        let Some(stem) = path.file_stem().and_then(|value| value.to_str()) else {
            continue;
        };
        if path.extension().is_some_and(|value| value == "ipa") && !keep.contains(stem) {
            let _ = std::fs::remove_file(path);
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    struct Fixture(PathBuf);
    impl Fixture {
        fn new() -> Self {
            let path = std::env::temp_dir().join(format!(
                "iresign-archive-test-{}-{}-{}",
                std::process::id(),
                std::time::SystemTime::now()
                    .duration_since(std::time::UNIX_EPOCH)
                    .unwrap()
                    .as_nanos(),
                ARCHIVE_SEQUENCE.fetch_add(1, Ordering::Relaxed)
            ));
            std::fs::create_dir(&path).unwrap();
            Self(path)
        }
    }
    impl Drop for Fixture {
        fn drop(&mut self) {
            assert!(self.0.starts_with(std::env::temp_dir()));
            let _ = std::fs::remove_dir_all(&self.0);
        }
    }

    fn digest(bytes: &[u8]) -> String {
        use sha2::{Digest, Sha256};
        format!("{:x}", Sha256::digest(bytes))
    }

    #[test]
    fn copies_and_verifies_exact_bytes() {
        let fixture = Fixture::new();
        let source = fixture.0.join("source.ipa");
        let bytes = b"fixture ipa bytes";
        std::fs::write(&source, bytes).unwrap();
        let sha = digest(bytes);
        let archived = ensure(&fixture.0.join("archive"), &source, &sha).unwrap();
        assert_eq!(
            archived,
            fixture.0.join("archive").join(format!("{sha}.ipa"))
        );
        assert_eq!(std::fs::read(&archived).unwrap(), bytes);
        assert_eq!(verify(&fixture.0.join("archive"), &sha).unwrap(), archived);
        std::fs::write(&source, b"changed after approval").unwrap();
        assert_eq!(verify(&fixture.0.join("archive"), &sha).unwrap(), archived);
    }

    #[test]
    fn rejects_tampered_or_missing_archive() {
        let fixture = Fixture::new();
        let archive = fixture.0.join("archive");
        std::fs::create_dir(&archive).unwrap();
        let bytes = b"expected";
        let sha = digest(bytes);
        let path = path_for(&archive, &sha).unwrap();
        std::fs::write(&path, b"tampered").unwrap();
        assert_eq!(verify(&archive, &sha).unwrap_err().code, "renewal_ipa");
        std::fs::remove_file(&path).unwrap();
        assert_eq!(verify(&archive, &sha).unwrap_err().code, "renewal_ipa");
    }

    #[test]
    fn cleanup_keeps_referenced_archives_and_removes_stale_ones() {
        let fixture = Fixture::new();
        let archive = fixture.0.join("archive");
        std::fs::create_dir(&archive).unwrap();
        let kept = digest(b"kept");
        let stale = digest(b"stale");
        std::fs::write(path_for(&archive, &kept).unwrap(), b"kept").unwrap();
        std::fs::write(path_for(&archive, &stale).unwrap(), b"stale").unwrap();
        std::fs::write(archive.join(".tmp-leftover"), b"partial").unwrap();
        cleanup(&archive, &HashSet::from([kept.clone()]));
        assert!(path_for(&archive, &kept).unwrap().exists());
        assert!(!path_for(&archive, &stale).unwrap().exists());
        assert!(!archive.join(".tmp-leftover").exists());
    }

    #[test]
    fn rejects_invalid_digest_without_touching_files() {
        let fixture = Fixture::new();
        assert_eq!(
            ensure(&fixture.0, &fixture.0.join("missing.ipa"), "../escape")
                .unwrap_err()
                .code,
            "renewal_ipa"
        );
        assert_eq!(
            verify(&fixture.0, "not-a-digest").unwrap_err().code,
            "renewal_ipa"
        );
    }
}
