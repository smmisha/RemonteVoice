use crate::error::AppError;
use serde::Serialize;
use sha2::{Digest, Sha256};
use std::{
    collections::HashSet,
    fs::{File, OpenOptions},
    io::{BufReader, Read, Seek, SeekFrom, Write},
    path::{Path, PathBuf},
    sync::atomic::{AtomicU64, Ordering},
};

const MAX_IPA_BYTES: u64 = 2 * 1024 * 1024 * 1024;
static SNAPSHOT_SEQUENCE: AtomicU64 = AtomicU64::new(0);

#[derive(Clone, Serialize)]
#[serde(rename_all = "camelCase")]
pub struct IpaInfo {
    pub name: String,
    pub bundle_id: String,
    pub version: String,
    pub sha256: String,
    #[serde(skip)]
    pub path: PathBuf,
}

/// App-owned snapshot. The original user path is never used by the signer.
/// On Windows the live handle permits readers but denies writes and replacement.
/// This is not an ACL boundary against an administrator or a compromised user account.
pub struct StagedIpa {
    info: IpaInfo,
    handle: Option<File>,
}

impl StagedIpa {
    pub fn info(&self) -> &IpaInfo {
        &self.info
    }

    pub fn verify(&self) -> Result<(), AppError> {
        let mut handle = self
            .handle
            .as_ref()
            .ok_or_else(invalid)?
            .try_clone()
            .map_err(|_| invalid())?;
        if fingerprint_handle(&mut handle)? != self.info.sha256 {
            return Err(AppError::new(
                "ipa_changed",
                "Копия IPA изменилась. Выберите файл заново.",
            ));
        }
        Ok(())
    }
}

impl Drop for StagedIpa {
    fn drop(&mut self) {
        // Close the Windows handle before removing the path it protects.
        drop(self.handle.take());
        let _ = std::fs::remove_file(&self.info.path);
    }
}

pub fn stage(path: PathBuf, directory: &Path) -> Result<StagedIpa, AppError> {
    let path = validated_path(path)?;
    let mut source = File::open(path).map_err(|_| invalid())?;
    if source.metadata().map_err(|_| invalid())?.len() > MAX_IPA_BYTES {
        return Err(invalid());
    }
    std::fs::create_dir_all(directory).map_err(|_| invalid())?;
    if !directory.is_dir()
        || std::fs::symlink_metadata(directory)
            .map_err(|_| invalid())?
            .file_type()
            .is_symlink()
    {
        return Err(invalid());
    }
    let stamp = std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map_err(|_| invalid())?
        .as_nanos();
    let mut created = None;
    for _ in 0..64 {
        let sequence = SNAPSHOT_SEQUENCE.fetch_add(1, Ordering::Relaxed);
        let destination =
            directory.join(format!("ipa-{}-{stamp}-{sequence}.ipa", std::process::id()));
        let mut options = OpenOptions::new();
        options.read(true).write(true).create_new(true);
        #[cfg(windows)]
        {
            use std::os::windows::fs::OpenOptionsExt;
            options.share_mode(1); // FILE_SHARE_READ: library readers work; writes/deletes do not.
        }
        match options.open(&destination) {
            Ok(file) => {
                created = Some((destination, file));
                break;
            }
            Err(error) if error.kind() == std::io::ErrorKind::AlreadyExists => continue,
            Err(_) => return Err(invalid()),
        }
    }
    let (destination, mut file) = created.ok_or_else(invalid)?;
    let result = (|| {
        let copied = std::io::copy(
            &mut Read::by_ref(&mut source).take(MAX_IPA_BYTES + 1),
            &mut file,
        )
        .map_err(|_| invalid())?;
        if copied > MAX_IPA_BYTES {
            return Err(invalid());
        }
        file.flush().map_err(|_| invalid())?;
        file.sync_all().map_err(|_| invalid())?;
        let info = inspect_handle(destination.clone(), &mut file)?;
        Ok(info)
    })();
    match result {
        Ok(info) => Ok(StagedIpa {
            info,
            handle: Some(file),
        }),
        Err(error) => {
            drop(file);
            let _ = std::fs::remove_file(destination);
            Err(error)
        }
    }
}

fn invalid() -> AppError {
    AppError::new(
        "invalid_ipa",
        "IPA повреждён, превышает допустимый размер или содержит небезопасные пути.",
    )
}

pub fn fingerprint(path: &Path) -> Result<String, AppError> {
    let mut file = File::open(path).map_err(|_| invalid())?;
    fingerprint_handle(&mut file)
}

fn fingerprint_handle(file: &mut File) -> Result<String, AppError> {
    if file.metadata().map_err(|_| invalid())?.len() > MAX_IPA_BYTES {
        return Err(invalid());
    }
    file.seek(SeekFrom::Start(0)).map_err(|_| invalid())?;
    let mut reader = BufReader::new(file);
    let mut digest = Sha256::new();
    let mut buffer = [0u8; 65536];
    let mut total = 0u64;
    loop {
        let len = reader.read(&mut buffer).map_err(|_| invalid())?;
        if len == 0 {
            break;
        }
        total += len as u64;
        if total > MAX_IPA_BYTES {
            return Err(invalid());
        }
        digest.update(&buffer[..len]);
    }
    Ok(format!("{:x}", digest.finalize()))
}

pub fn inspect(path: PathBuf) -> Result<IpaInfo, AppError> {
    let path = validated_path(path)?;
    let mut file = File::open(&path).map_err(|_| invalid())?;
    inspect_handle(path, &mut file)
}

fn validated_path(path: PathBuf) -> Result<PathBuf, AppError> {
    let path = path.canonicalize().map_err(|_| invalid())?;
    if !path.is_file()
        || path
            .extension()
            .and_then(|s| s.to_str())
            .is_none_or(|s| !s.eq_ignore_ascii_case("ipa"))
    {
        return Err(invalid());
    }
    Ok(path)
}

fn inspect_handle(path: PathBuf, file: &mut File) -> Result<IpaInfo, AppError> {
    let sha256 = fingerprint_handle(file)?;
    file.seek(SeekFrom::Start(0)).map_err(|_| invalid())?;
    let mut archive = zip::ZipArchive::new(file).map_err(|_| invalid())?;
    if archive.len() > 100_000 {
        return Err(invalid());
    }
    let mut total: u64 = 0;
    let mut names = HashSet::new();
    let mut main_plist = None;
    for index in 0..archive.len() {
        let entry = archive.by_index(index).map_err(|_| invalid())?;
        let name = entry.name();
        if entry.enclosed_name().is_none()
            || name.contains('\\')
            || name.contains(':')
            || name.split('/').any(|p| p == "..")
            || entry.unix_mode().is_some_and(|m| m & 0o170000 == 0o120000)
        {
            return Err(invalid());
        }
        if !names.insert(name.trim_end_matches('/').to_lowercase()) {
            return Err(invalid());
        }
        for part in name.split('/').filter(|s| !s.is_empty()) {
            let base = part
                .split('.')
                .next()
                .unwrap_or_default()
                .to_ascii_uppercase();
            if part == "."
                || part.ends_with(['.', ' '])
                || matches!(base.as_str(), "CON" | "PRN" | "AUX" | "NUL")
                || ((base.starts_with("COM") || base.starts_with("LPT"))
                    && base.len() == 4
                    && matches!(base.as_bytes()[3], b'1'..=b'9'))
            {
                return Err(invalid());
            }
        }
        total = total.checked_add(entry.size()).ok_or_else(invalid)?;
        if total > 10 * 1024 * 1024 * 1024 || entry.size() > 2 * 1024 * 1024 * 1024 {
            return Err(invalid());
        }
        let parts: Vec<_> = name.split('/').collect();
        if parts.len() == 3
            && parts[0] == "Payload"
            && parts[1].ends_with(".app")
            && parts[2] == "Info.plist"
        {
            if main_plist.is_some() || entry.size() > 2 * 1024 * 1024 {
                return Err(invalid());
            }
            main_plist = Some(index);
        }
    }
    let mut entry = archive
        .by_index(main_plist.ok_or_else(invalid)?)
        .map_err(|_| invalid())?;
    let mut bytes = Vec::new();
    entry
        .by_ref()
        .take(2 * 1024 * 1024 + 1)
        .read_to_end(&mut bytes)
        .map_err(|_| invalid())?;
    if bytes.len() > 2 * 1024 * 1024 {
        return Err(invalid());
    }
    let value = plist::Value::from_reader(std::io::Cursor::new(bytes)).map_err(|_| invalid())?;
    let dict = value.as_dictionary().ok_or_else(invalid)?;
    let text = |key: &str| {
        dict.get(key)
            .and_then(plist::Value::as_string)
            .map(str::to_string)
    };
    let bundle_id = text("CFBundleIdentifier")
        .filter(|s| !s.is_empty())
        .ok_or_else(invalid)?;
    Ok(IpaInfo {
        name: text("CFBundleDisplayName")
            .or_else(|| text("CFBundleName"))
            .unwrap_or_else(|| bundle_id.clone()),
        version: text("CFBundleShortVersionString")
            .or_else(|| text("CFBundleVersion"))
            .unwrap_or_default(),
        bundle_id,
        sha256,
        path,
    })
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::io::Write;
    struct Fixture(PathBuf);
    impl Drop for Fixture {
        fn drop(&mut self) {
            let _ = std::fs::remove_file(&self.0);
        }
    }
    fn fixture(entries: &[(&str, &str)]) -> Fixture {
        let path = std::env::temp_dir().join(format!(
            "iresign-test-{}-{}.ipa",
            std::process::id(),
            std::time::SystemTime::now()
                .duration_since(std::time::UNIX_EPOCH)
                .unwrap()
                .as_nanos()
        ));
        let mut writer = zip::ZipWriter::new(File::create(&path).unwrap());
        for (name, content) in entries {
            writer
                .start_file(*name, zip::write::SimpleFileOptions::default())
                .unwrap();
            writer.write_all(content.as_bytes()).unwrap();
        }
        writer.finish().unwrap();
        Fixture(path)
    }
    const INFO: &str = "<?xml version=\"1.0\"?><plist version=\"1.0\"><dict><key>CFBundleIdentifier</key><string>dev.iresign.fixture</string><key>CFBundleName</key><string>Fixture</string><key>CFBundleVersion</key><string>1</string></dict></plist>";
    #[test]
    fn inspects_valid_ipa() {
        let file = fixture(&[("Payload/Fixture.app/Info.plist", INFO)]);
        let result = inspect(file.0.clone()).unwrap();
        assert_eq!(result.bundle_id, "dev.iresign.fixture");
        assert_eq!(result.name, "Fixture");
        assert_eq!(result.sha256.len(), 64);
    }
    #[test]
    fn rejects_unsafe_paths() {
        for path in [
            "../escape",
            "/absolute",
            "Payload/../escape",
            "C:/escape",
            "Payload\\escape",
            "Payload/CON",
            "Payload/LPT1.txt",
            "Payload/name.",
        ] {
            let file = fixture(&[("Payload/Fixture.app/Info.plist", INFO), (path, "invalid")]);
            assert!(inspect(file.0.clone()).is_err(), "{path}");
        }
    }
    #[test]
    fn rejects_missing_or_multiple_main_apps() {
        for entries in [
            vec![("other.plist", INFO)],
            vec![
                ("Payload/One.app/Info.plist", INFO),
                ("Payload/Two.app/Info.plist", INFO),
            ],
        ] {
            let file = fixture(&entries);
            assert!(inspect(file.0.clone()).is_err());
        }
    }
    #[test]
    fn detects_file_change() {
        let file = fixture(&[("Payload/Fixture.app/Info.plist", INFO)]);
        let first = fingerprint(&file.0).unwrap();
        std::fs::write(&file.0, b"changed").unwrap();
        assert_ne!(first, fingerprint(&file.0).unwrap());
        assert!(inspect(file.0.clone()).is_err());
    }

    fn staging_directory() -> PathBuf {
        let path = std::env::temp_dir().join(format!(
            "iresign-stage-test-{}-{}-{}",
            std::process::id(),
            std::time::SystemTime::now()
                .duration_since(std::time::UNIX_EPOCH)
                .unwrap()
                .as_nanos(),
            SNAPSHOT_SEQUENCE.fetch_add(1, Ordering::Relaxed)
        ));
        std::fs::create_dir(&path).unwrap();
        path
    }

    #[test]
    fn original_path_change_does_not_change_approved_snapshot_bytes() {
        let source = fixture(&[("Payload/Fixture.app/Info.plist", INFO)]);
        let directory = staging_directory();
        let staged = stage(source.0.clone(), &directory).unwrap();
        let snapshot = staged.info().path.clone();
        let approved = staged.info().sha256.clone();
        assert_ne!(snapshot, source.0);
        std::fs::write(&source.0, b"different bytes after user approval").unwrap();
        assert_eq!(fingerprint(&snapshot).unwrap(), approved);
        assert_eq!(staged.info().bundle_id, "dev.iresign.fixture");
        staged.verify().unwrap();
        drop(staged);
        assert!(!snapshot.exists());
        assert!(source.0.exists());
        std::fs::remove_dir(directory).unwrap();
    }

    #[test]
    fn invalid_snapshot_is_removed_without_touching_source() {
        let source = fixture(&[("not-an-app", "invalid")]);
        let directory = staging_directory();
        assert!(stage(source.0.clone(), &directory).is_err());
        assert_eq!(std::fs::read_dir(&directory).unwrap().count(), 0);
        assert!(source.0.exists());
        std::fs::remove_dir(directory).unwrap();
    }

    #[cfg(windows)]
    #[test]
    fn live_windows_snapshot_allows_readers_but_denies_write_and_replacement() {
        let source = fixture(&[("Payload/Fixture.app/Info.plist", INFO)]);
        let directory = staging_directory();
        let staged = stage(source.0.clone(), &directory).unwrap();
        let snapshot = staged.info().path.clone();
        assert!(File::open(&snapshot).is_ok());
        assert!(std::fs::write(&snapshot, b"replacement").is_err());
        assert!(std::fs::remove_file(&snapshot).is_err());
        staged.verify().unwrap();
        drop(staged);
        assert!(!snapshot.exists());
        std::fs::remove_dir(directory).unwrap();
    }
}
