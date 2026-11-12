// Device/provider flow adapted from nab138/iloader (MIT); see THIRD_PARTY.md.
use crate::error::AppError;
use idevice::{
    IdeviceService,
    lockdown::LockdownClient,
    provider::UsbmuxdProvider,
    usbmuxd::{Connection, UsbmuxdAddr, UsbmuxdConnection},
};
use serde::Serialize;
use std::{future::Future, time::Duration};

const DISCOVERY_DEADLINE: Duration = Duration::from_secs(20);

async fn bounded<F, T>(deadline: Duration, work: F) -> Result<T, AppError>
where
    F: Future<Output = Result<T, AppError>>,
{
    tokio::time::timeout(deadline, work).await.map_err(|_| {
        AppError::new(
            "device_timeout",
            "iPhone не ответил. Проверьте USB и разблокировку устройства.",
        )
    })?
}

#[derive(Clone, Serialize)]
#[serde(rename_all = "camelCase")]
pub struct DeviceInfo {
    pub name: String,
    pub udid: String,
    pub version: String,
    pub connection_type: &'static str,
}

fn device_error() -> AppError {
    AppError::new(
        "device",
        "iPhone недоступен. Подключите USB, разблокируйте iPhone и подтвердите доверие компьютеру.",
    )
}

pub async fn list() -> Result<Vec<DeviceInfo>, AppError> {
    bounded(DISCOVERY_DEADLINE, list_inner()).await
}

async fn list_inner() -> Result<Vec<DeviceInfo>, AppError> {
    let mut mux = UsbmuxdConnection::default().await.map_err(|_| AppError::new(
        "apple_device_service", "Служба Apple Mobile Device недоступна. Проверьте установленный iTunes и службу Apple Mobile Device Service."))?;
    let devices = mux.get_devices().await.map_err(|_| device_error())?;
    let addr = UsbmuxdAddr::from_env_var().map_err(|_| device_error())?;
    let mut result = Vec::new();
    let mut first_error = None;
    for device in devices {
        if !matches!(device.connection_type, Connection::Usb) {
            continue;
        }
        let provider = device.to_provider(addr.clone(), "iReSign");
        let Ok(mut lockdown) = LockdownClient::connect(&provider).await else {
            first_error.get_or_insert_with(device_error);
            continue;
        };
        let Ok(name) = lockdown.get_value(Some("DeviceName"), None).await else {
            first_error.get_or_insert_with(device_error);
            continue;
        };
        let Ok(version) = lockdown.get_value(Some("ProductVersion"), None).await else {
            first_error.get_or_insert_with(device_error);
            continue;
        };
        let (Some(name), Some(version)) = (name.as_string(), version.as_string()) else {
            first_error.get_or_insert_with(device_error);
            continue;
        };
        result.push(DeviceInfo {
            name: name.to_string(),
            version: version.to_string(),
            udid: device.udid,
            connection_type: "USB",
        });
    }
    if result.is_empty()
        && let Some(error) = first_error
    {
        return Err(error);
    }
    Ok(result)
}

pub async fn provider(udid: &str) -> Result<UsbmuxdProvider, AppError> {
    bounded(DISCOVERY_DEADLINE, provider_inner(udid)).await
}

async fn provider_inner(udid: &str) -> Result<UsbmuxdProvider, AppError> {
    let mut mux = UsbmuxdConnection::default()
        .await
        .map_err(|_| device_error())?;
    let device = mux.get_device(udid).await.map_err(|_| device_error())?;
    if !matches!(device.connection_type, Connection::Usb) {
        return Err(device_error());
    }
    let addr = UsbmuxdAddr::from_env_var().map_err(|_| device_error())?;
    Ok(device.to_provider(addr, "iReSign"))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn stalled_discovery_is_bounded_without_device_access() {
        let runtime = tokio::runtime::Runtime::new().unwrap();
        let result = runtime.block_on(bounded(
            Duration::from_millis(5),
            std::future::pending::<Result<(), AppError>>(),
        ));
        assert_eq!(result.unwrap_err().code, "device_timeout");
    }
}
