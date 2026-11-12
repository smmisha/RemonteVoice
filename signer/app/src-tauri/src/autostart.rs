//! Optional per-user Windows autostart entry. The app starts minimized when it
//! was launched by this entry so the background renewal loop can run without
//! taking over the desktop.
use crate::error::AppError;
use std::path::Path;

const RUN_KEY: &str = r"Software\Microsoft\Windows\CurrentVersion\Run";
const VALUE_NAME: &str = "iReSign";
const LEGACY_VALUE_NAMES: [&str; 2] = ["LocalSign", "Remonte"];

fn autostart_error() -> AppError {
    AppError::new(
        "autostart",
        "Не удалось изменить автозапуск Windows. Проверьте права текущего пользователя.",
    )
}

pub fn command_for(executable: &Path) -> String {
    format!("\"{}\" --autostart", executable.display())
}

pub fn launched_by_autostart(args: &[String]) -> bool {
    args.iter().any(|value| value == "--autostart")
}

#[cfg(windows)]
pub fn enabled() -> Result<bool, AppError> {
    use std::io::ErrorKind;
    use winreg::{RegKey, enums::HKEY_CURRENT_USER};
    let run = match RegKey::predef(HKEY_CURRENT_USER).open_subkey(RUN_KEY) {
        Ok(key) => key,
        Err(error) if error.kind() == ErrorKind::NotFound => return Ok(false),
        Err(_) => return Err(autostart_error()),
    };
    match run.get_value::<String, _>(VALUE_NAME) {
        Ok(_) => Ok(true),
        Err(error) if error.kind() == ErrorKind::NotFound => Ok(false),
        Err(_) => Err(autostart_error()),
    }
}

#[cfg(windows)]
pub fn set(enabled: bool) -> Result<(), AppError> {
    use std::io::ErrorKind;
    use winreg::{
        RegKey,
        enums::{HKEY_CURRENT_USER, KEY_READ, KEY_WRITE},
    };
    let run = RegKey::predef(HKEY_CURRENT_USER)
        .open_subkey_with_flags(RUN_KEY, KEY_READ | KEY_WRITE)
        .map_err(|_| autostart_error())?;
    if enabled {
        let executable = std::env::current_exe().map_err(|_| autostart_error())?;
        run.set_value(VALUE_NAME, &command_for(&executable))
            .map_err(|_| autostart_error())?;
    } else {
        match run.delete_value(VALUE_NAME) {
            Ok(()) => {}
            Err(error) if error.kind() == ErrorKind::NotFound => {}
            Err(_) => return Err(autostart_error()),
        }
    }
    // Remove entries written by previous working names so they cannot keep
    // launching an old executable after the rename.
    for legacy in LEGACY_VALUE_NAMES {
        match run.delete_value(legacy) {
            Ok(()) => {}
            Err(error) if error.kind() == ErrorKind::NotFound => {}
            Err(_) => return Err(autostart_error()),
        }
    }
    Ok(())
}

#[cfg(not(windows))]
pub fn enabled() -> Result<bool, AppError> {
    Ok(false)
}

#[cfg(not(windows))]
pub fn set(_enabled: bool) -> Result<(), AppError> {
    Err(autostart_error())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn command_quotes_executable_and_marks_background_launch() {
        assert_eq!(
            command_for(Path::new(r"C:\Program Files\iReSign\iReSign.exe")),
            r#""C:\Program Files\iReSign\iReSign.exe" --autostart"#
        );
        assert!(launched_by_autostart(&[
            "iReSign.exe".into(),
            "--autostart".into()
        ]));
        assert!(!launched_by_autostart(&["iReSign.exe".into()]));
    }
}
