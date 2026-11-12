use crate::error::AppError;
use isideload::util::{keyring_storage::KeyringStorage, storage::SideloadingStorage};

pub const SERVICE: &str = "dev.remotevoice.iresign.v1";

pub fn protected_storage() -> Result<Box<dyn SideloadingStorage>, AppError> {
    let entry =
        keyring::Entry::new(SERVICE, "availability-probe").map_err(|_| AppError::storage())?;
    let expected = b"iReSign storage check";
    entry
        .set_secret(expected)
        .map_err(|_| AppError::storage())?;
    let actual = entry.get_secret().map_err(|_| AppError::storage())?;
    entry.delete_credential().map_err(|_| AppError::storage())?;
    if actual != expected {
        return Err(AppError::storage());
    }
    // No FsStorage feature or plaintext fallback. No upstream namespace is read.
    Ok(Box::new(KeyringStorage::new(SERVICE.to_string())))
}
