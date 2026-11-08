use rootcause::Report;
use serde::Serialize;

// Only fixed messages cross IPC. Dependency reports may contain account or device data.
#[derive(Debug, Clone, Serialize)]
pub struct AppError {
    pub code: &'static str,
    pub message: &'static str,
}

impl AppError {
    pub const fn new(code: &'static str, message: &'static str) -> Self {
        Self { code, message }
    }

    pub fn busy() -> Self {
        Self::new("busy", "Дождитесь завершения текущей операции.")
    }

    pub fn storage() -> Self {
        Self::new(
            "protected_storage",
            "Защищённое хранилище Windows недоступно. Операция остановлена.",
        )
    }
}

impl From<Report> for AppError {
    fn from(report: Report) -> Self {
        // Inspect error categories internally; never export the raw report.
        for cause in report.iter_reports() {
            if cause.downcast_current_context::<keyring::Error>().is_some() {
                return Self::storage();
            }
            if let Some(error) = cause.downcast_current_context::<isideload::SideloadError>() {
                use isideload::SideloadError;
                return match error {
                    SideloadError::AuthWithMessage(-20209, _) => Self::new(
                        "account_locked",
                        "Apple заблокировала вход. Проверьте учётную запись на сайте Apple.",
                    ),
                    SideloadError::AuthWithMessage(_, _) => Self::new(
                        "apple_auth",
                        "Apple отклонила вход. Проверьте учётную запись и подтверждение 2FA.",
                    ),
                    SideloadError::DeveloperError(_, _) => Self::new(
                        "apple_developer",
                        "Apple отклонила операцию с профилем или сертификатом. Проверьте лимиты команды.",
                    ),
                    SideloadError::IdeviceError(_) => Self::new(
                        "device",
                        "Операция с iPhone не выполнена. Проверьте USB, разблокировку и доверие компьютеру.",
                    ),
                    _ => Self::new(
                        "signing",
                        "Подпись не выполнена. Проверьте сервис Anisette, сеть и лимиты Apple.",
                    ),
                };
            }
        }
        Self::new(
            "operation",
            "Операция не выполнена. Проверьте подключение, сервис Anisette и состояние Apple Account.",
        )
    }
}
