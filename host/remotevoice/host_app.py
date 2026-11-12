"""Tkinter desktop UI for pairing and configuring the Windows host."""

from __future__ import annotations

import asyncio
import argparse
import base64
import io
import json
import logging
from logging.handlers import RotatingFileHandler
import os
import sys
import threading
import time
from functools import partial
import tkinter as tk
from pathlib import Path
from tkinter import messagebox, ttk
from urllib.parse import urlencode

import qrcode
import soundcard as sc

from .audio import is_virtual_audio_device, list_audio_devices, virtual_device_family
from .audio_backend import AudioBackendError, close_audio_backend, prepare_audio_backend
from .certificates import load_or_create_identity
from .deployment import write_receipt
from .driver_route_recovery import (
    DriverRecoveryError, DriverRouteRecovery, load_driver_recovery, save_driver_recovery,
)
from .desktop_ui import build_ui, device_choices
from .host_lease import HostLease, HostLeaseError
from .microphone_guard import MicrophoneGuardError, MicrophoneMuteGuard
from .input_routing import InputRoute, InputRoutingError
from .network_addresses import private_ipv4_addresses
from .output_routing import OutputRoute, OutputRoutingError
from .pairing import PairingManager
from .server import RemoteVoiceServer
from . import voicemeeter_window
from .safety_guard import SafetyGuard, SafetyGuardError

LOGGER = logging.getLogger("remotevoice.ui")
PORT = 8765
_CLOSE_GRACE_SECONDS = 5.0


def app_data_dir() -> Path:
    base = os.environ.get("APPDATA")
    return Path(base) / "RemoteVoice" if base else Path.home() / ".remotevoice"


def matching_system_output(capture_name: str, speakers: list[dict[str, str]]) -> str | None:
    """Suggest only an unambiguous playback partner for a known virtual capture."""
    capture_base = capture_name.split("(", 1)[0].strip().casefold()
    voicemeeter_outputs = {
        "voicemeeter out b1": "voicemeeter input",
        "voicemeeter output": "voicemeeter input",
        "voicemeeter out b2": "voicemeeter aux input",
        "voicemeeter aux output": "voicemeeter aux input",
    }
    expected = voicemeeter_outputs.get(capture_base)
    if expected is not None:
        candidates = [
            device["id"]
            for device in speakers
            if device["name"].split("(", 1)[0].strip().casefold() == expected
        ]
    elif "cable" in capture_base:
        candidates = [
            device["id"]
            for device in speakers
            if virtual_device_family(device["name"]) == virtual_device_family(capture_name)
        ]
    else:
        return None
    return candidates[0] if len(candidates) == 1 else None


def matching_virtual_microphone(playback_id: str, playback_name: str, captures: list[dict[str, str]]) -> str | None:
    candidates = [device["id"] for device in captures
                  if device.get("loopback") != "True" and matching_system_output(
                      device["name"], [{"id": playback_id, "name": playback_name}]) == playback_id]
    return candidates[0] if len(candidates) == 1 else None


def window_icon_path() -> Path | None:
    """Prefer the icon bundled next to the code, then the source tree copy."""
    bundle = getattr(sys, "_MEIPASS", None)
    candidates = [Path(bundle) / "RemoteVoice.ico"] if bundle else []
    candidates.append(Path(__file__).resolve().parents[1] / "RemoteVoice.ico")
    return next((candidate for candidate in candidates if candidate.is_file()), None)


class HostWindow(tk.Tk):
    def __init__(self, *, driver_runtime_factory=None) -> None:
        if driver_runtime_factory is not None and not callable(driver_runtime_factory):
            raise TypeError('Trusted inert driver runtime factory required')
        self._driver_runtime_factory = driver_runtime_factory
        self._driver_runtime = None
        self._driver_startup_worker = self._driver_cleanup_worker = None
        self._driver_generation = 0
        self._driver_cancelled = self._driver_ready = False
        self._driver_result = None
        self._driver_cleanup_error = ''
        self._close_deadline = None
        super().__init__()
        self.data_dir = app_data_dir()
        self._host_lease = HostLease(self.data_dir / "host.lock")
        try:
            # Ownership precedes TLS/pairing, recovery and every device write.
            self._host_lease.acquire()
        except HostLeaseError as exc:
            messagebox.showerror("RemonteVoice уже открыт", str(exc), parent=self)
            self.destroy()
            raise
        try:
            self._initialize()
        except BaseException:
            self._host_lease.release()
            self.destroy()
            raise

    def _initialize(self) -> None:
        write_receipt(self.data_dir / "runtime.json")
        self._audio_backend = None
        self.title("RemonteVoice — Windows Host")
        icon = window_icon_path()
        if icon is not None:
            try:
                # On Windows this sets both the title bar and taskbar icons for
                # this window and for dialogs that do not pass their own icon.
                self.iconbitmap(default=str(icon))
            except tk.TclError:
                LOGGER.warning("Window icon could not be applied")

        self.config_path = self.data_dir / "config.json"
        self.config_data = self._read_config()
        self.pairing = PairingManager(self.data_dir / "paired-phone.json")
        try:
            _, self.fingerprint = load_or_create_identity(self.data_dir / "identity")
        except Exception as exc:
            self.fingerprint = ""
            messagebox.showerror("Ошибка TLS", f"Не удалось создать TLS-сертификат:\n{exc}")

        self.server: RemoteVoiceServer | None = None
        self.server_thread: threading.Thread | None = None
        self.server_running = False
        self.output_route: OutputRoute | None = None
        self.input_route: InputRoute | None = None
        self._input_route_error = ""
        self._input_route_active = False
        self.microphone_guard: MicrophoneMuteGuard | None = None
        self._microphone_guard_active = False
        self._microphone_guard_error = ""
        self._microphone_activation_error = ""
        self._session_restore_failure = ""
        self._session_activation_error = ""
        self._server_generation = 0
        self._safety_guard: SafetyGuard | None = None
        self._output_route_active = False
        self._output_route_error = ""
        self._route_activation_failed = False
        self._stopping = False
        self.status_var = tk.StringVar(value="Подключение остановлено")
        self.screen_allowed_var = tk.BooleanVar(value=False)
        self.screen_status_var = tk.StringVar(value="Просмотр экрана выключен")
        self.ip_var = tk.StringVar()
        self.capture_var = tk.StringVar()
        self.playback_var = tk.StringVar()
        self.system_output_var = tk.StringVar()
        self.fallback_output_var = tk.StringVar()
        self.local_microphone_var = tk.StringVar()
        self.mute_local_microphone_var = tk.BooleanVar(
            value=self.config_data.get("mute_local_microphone", True) is not False
        )
        self.pairing_code_var = tk.StringVar()
        self.endpoint_hint_var = tk.StringVar()
        self.keep_voicemeeter_hidden_var = tk.BooleanVar(value=True)
        self.voicemeeter_status_var = tk.StringVar(value="Voicemeeter: проверяю…")
        self.qr_image: tk.PhotoImage | None = None
        self.capture_choices: dict[str, str] = {}
        self.playback_choices: dict[str, str] = {}
        self.capture_names: dict[str, str] = {}
        self.playback_names: dict[str, str] = {}
        self.system_output_choices: dict[str, str] = {}
        self.fallback_output_choices: dict[str, str] = {}
        self.local_microphone_choices: dict[str, str] = {}
        self.virtual_microphones: list[dict[str, str]] = []

        self._build_ui()
        self._load_audio_devices()
        self._recover_waiting_routes()
        self._load_ip_addresses()
        self._refresh_pairing_ui()
        self._screen_events_lock = threading.Lock()
        self._pending_screen_event = None
        self._pending_audio_event = None
        self._screen_events_closed = False
        self.after(25, self._drain_screen_status)
        self.protocol("WM_DELETE_WINDOW", self._on_close)

    def _build_ui(self) -> None:
        build_ui(self)

    def _read_config(self) -> dict[str, str | bool]:
        try:
            value = json.loads(self.config_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        return value if isinstance(value, dict) else {}

    def _save_config(self) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        value = {
            "capture_device_id": self.capture_choices.get(self.capture_var.get(), ""),
            "playback_device_id": self.playback_choices.get(self.playback_var.get(), ""),
            "system_output_device_id": self.system_output_choices.get(self.system_output_var.get(), ""),
            "fallback_output_device_id": self.fallback_output_choices.get(self.fallback_output_var.get(), ""),
            "local_microphone_device_id": self.local_microphone_choices.get(self.local_microphone_var.get(), ""),
            "virtual_microphone_device_id": self.config_data.get("virtual_microphone_device_id", ""),
            "mute_local_microphone": bool(self.mute_local_microphone_var.get()),
            "host_ip": self.ip_var.get(),
        }
        temporary = self.config_path.with_suffix(".tmp")
        temporary.write_text(json.dumps(value, indent=2) + "\n", encoding="utf-8")
        temporary.replace(self.config_path)

    def _load_audio_devices(self) -> None:
        try:
            speakers, microphones = list_audio_devices()
            physical_speakers = [
                {"id": str(device.id), "name": str(device.name)}
                for device in sc.all_speakers()
                if not is_virtual_audio_device(str(device.name))
            ]
        except Exception as exc:
            self.endpoint_hint_var.set(f"Не удалось перечислить аудиоустройства: {exc}")
            return
        try:
            default_speaker = sc.default_speaker()
        except Exception:
            # Enumeration may succeed even when Windows has no default endpoint.
            default_speaker = None
        try:
            physical_microphones = [
                {"id": str(device.id), "name": str(device.name)}
                for device in sc.all_microphones(include_loopback=False)
                if not is_virtual_audio_device(str(device.name))
                and not bool(getattr(device, "isloopback", False))
            ]
        except Exception:
            # Keep output selection usable when the capture list is unavailable.
            physical_microphones = []

        self.virtual_microphones = [device for device in microphones if device.get("loopback") != "True"]
        self.capture_choices = device_choices(microphones, capture=True)
        self.capture_names = {device["id"]: device["name"] for device in microphones}
        self.playback_choices = device_choices(speakers)
        self.playback_names = {device["id"]: device["name"] for device in speakers}
        self.system_output_choices = dict(self.playback_choices)
        self.fallback_output_choices = device_choices(physical_speakers)
        self.local_microphone_choices = device_choices(physical_microphones)
        self.capture_combo["values"] = list(self.capture_choices)
        self.playback_combo["values"] = list(self.playback_choices)
        self.system_output_combo["values"] = list(self.system_output_choices)
        self.fallback_output_combo["values"] = list(self.fallback_output_choices)
        self.local_microphone_combo["values"] = list(self.local_microphone_choices)

        capture_id = self.config_data.get("capture_device_id", "")
        playback_id = self.config_data.get("playback_device_id", "")
        capture_label = next(
            (label for label, device_id in self.capture_choices.items() if device_id == capture_id),
            None,
        )
        playback_label = next(
            (label for label, device_id in self.playback_choices.items() if device_id == playback_id),
            None,
        )
        if capture_label:
            self.capture_var.set(capture_label)
        elif self.capture_choices:
            self.capture_var.set(next(iter(self.capture_choices)))
        if playback_label:
            self.playback_var.set(playback_label)
        else:
            virtual_speaker = next(
                (label for label in self.playback_choices if "cable" in label.lower()), None
            )
            if virtual_speaker:
                self.playback_var.set(virtual_speaker)
        system_output_id = self.config_data.get("system_output_device_id", "")
        system_output_label = next(
            (label for label, device_id in self.system_output_choices.items() if device_id == system_output_id),
            None,
        )
        if system_output_label:
            self.system_output_var.set(system_output_label)
        else:
            self._suggest_system_output()
        fallback_output_id = self.config_data.get("fallback_output_device_id", "")
        fallback_output_label = next(
            (label for label, device_id in self.fallback_output_choices.items() if device_id == fallback_output_id),
            None,
        )
        if fallback_output_label:
            self.fallback_output_var.set(fallback_output_label)
        else:
            default_id = str(default_speaker.id) if default_speaker is not None else ""
            default_label = next(
                (label for label, device_id in self.fallback_output_choices.items() if device_id == default_id),
                None,
            )
            if default_label:
                self.fallback_output_var.set(default_label)
            elif len(self.fallback_output_choices) == 1:
                self.fallback_output_var.set(next(iter(self.fallback_output_choices)))
        local_microphone_id = self.config_data.get("local_microphone_device_id", "")
        local_microphone_label = next(
            (label for label, device_id in self.local_microphone_choices.items() if device_id == local_microphone_id),
            None,
        )
        if local_microphone_label:
            self.local_microphone_var.set(local_microphone_label)
        elif len(self.local_microphone_choices) == 1:
            self.local_microphone_var.set(next(iter(self.local_microphone_choices)))
        else:
            self.local_microphone_var.set("")
        if not self.capture_choices or not self.playback_choices:
            self.endpoint_hint_var.set(
                "Совместимые виртуальные устройства не найдены. Для двух направлений "
                "нужны две отдельные виртуальные пары."
            )
        else:
            self.endpoint_hint_var.set(
                "Выберите запись первой виртуальной пары для звука Windows → телефон "
                "и вывод второй пары для голоса телефон → Windows. Микрофон ноутбука "
                "выбирается отдельно."
            )
        if not self.fallback_output_choices:
            self.endpoint_hint_var.set(
                "Динамики или наушники для автоматического возврата звука не найдены. "
                "Подключите устройство вывода и перезапустите RemonteVoice."
            )

    def _update_microphone_controls(self) -> None:
        enabled = not self.server_running
        self.local_microphone_combo.configure(state="readonly" if enabled else "disabled")

    def _recover_waiting_routes(self) -> bool:
        """Recover a leftover virtual default even before network/mic setup succeeds."""
        if not self._host_lease.held:
            return False
        if self.server_running:
            return True
        driver_recovery_failed = False
        try:
            # Restore-only data is usable even if broker/package startup failed.
            # It never certifies devices or selects the own-driver backend.
            previous = load_driver_recovery(self.data_dir)
            if previous is not None:
                for route_type, remote, fallback in (
                    (OutputRoute, previous.output_remote, previous.output_fallback),
                    (InputRoute, previous.input_remote, previous.input_fallback),
                ):
                    try:
                        route_type(remote, fallback).restore()
                    except Exception:
                        driver_recovery_failed = True
        except DriverRecoveryError:
            driver_recovery_failed = True
        remote = self.system_output_choices.get(self.system_output_var.get())
        fallback = self.fallback_output_choices.get(self.fallback_output_var.get())
        physical_mic = self.local_microphone_choices.get(self.local_microphone_var.get())
        playback = self.playback_choices.get(self.playback_var.get())
        if self.output_route is None and remote and fallback:
            self.output_route = OutputRoute(remote, fallback)
        if self.input_route is None and physical_mic and playback:
            virtual_mic = matching_virtual_microphone(
                playback, self.playback_names.get(playback, ""), self.virtual_microphones)
            if virtual_mic:
                self.input_route = InputRoute(virtual_mic, physical_mic)
        restored = self._restore_session_routes()
        try:
            # Retry persisted recovery even when an old in-memory guard failed;
            # its journal lease still prevents a conflicting second writer.
            MicrophoneMuteGuard.recover(self.data_dir / "microphone-restore.json")
            if not self._microphone_guard_error:
                self._microphone_guard_error = ""
        except MicrophoneGuardError as exc:
            if not self._microphone_guard_error:
                self._microphone_guard_error = f"Не удалось восстановить микрофон ноутбука: {exc}"
            restored = False
        recovery_error = 'Не удалось восстановить сохранённые устройства драйвера'
        if driver_recovery_failed:
            self._session_restore_failure = recovery_error
            restored = False
        elif self._session_restore_failure == recovery_error:
            self._session_restore_failure = ''
        if not restored:
            self._set_status_text("Не удалось восстановить аудиоустройства")
        return restored

    def _return_to_laptop(self) -> None:
        # Stop first so a pending authenticated callback cannot switch back to virtual.
        if self.server_running:
            self._stop_host()
        elif self._recover_waiting_routes():
            if self.output_route is not None and self.input_route is not None:
                self._set_status_text("Звук и микрофон возвращены на ноутбук")
            else:
                self._set_status_text("Выберите физические динамики и микрофон для возврата")

    def _suggest_system_output(self, _event: object = None) -> None:
        capture_id = self.capture_choices.get(self.capture_var.get(), "")
        speakers = [
            {"id": device_id, "name": name}
            for device_id, name in self.playback_names.items()
        ]
        inferred = matching_system_output(self.capture_names.get(capture_id, ""), speakers)
        label = next(
            (label for label, device_id in self.system_output_choices.items() if device_id == inferred),
            "",
        )
        self.system_output_var.set(label)

    def _load_ip_addresses(self) -> None:
        addresses = private_ipv4_addresses()
        self.ip_combo["values"] = addresses
        saved_ip = self.config_data.get("host_ip", "")
        self.ip_var.set(saved_ip if saved_ip in addresses else (addresses[0] if addresses else ""))

    def _pairing_uri(self) -> str | None:
        pin = self.pairing.pairing_pin
        host = self.ip_var.get()
        if not pin or not host or not self.fingerprint:
            return None
        query = urlencode(
            {
                "host": host,
                "port": PORT,
                "pin": pin,
                "fingerprint": self.fingerprint,
            }
        )
        return f"remotevoice://pair?{query}"

    def _refresh_pairing_ui(self) -> None:
        pin = self.pairing.pairing_pin
        self.pairing_code_var.set(pin or ("Сопряжено" if self.pairing.is_paired else "—"))
        pairing_uri = self._pairing_uri()
        if not self.server_running:
            self.qr_label.configure(image="", text="Нажмите «Начать подключение», чтобы показать QR-код")
            return
        if pairing_uri:
            qr = qrcode.QRCode(box_size=5, border=2)
            qr.add_data(pairing_uri)
            qr.make(fit=True)
            image = qr.make_image(fill_color="black", back_color="white").resize((190, 190))
            buffer = io.BytesIO()
            image.save(buffer, format="PNG")
            self.qr_image = tk.PhotoImage(data=base64.b64encode(buffer.getvalue()).decode("ascii"))
            self.qr_label.configure(image=self.qr_image, text="")
        else:
            self.qr_image = None
            self.qr_label.configure(
                image="", text="Телефон уже сопряжён. Подключитесь в приложении телефона или создайте новую пару ниже."
            )

    def _begin_pairing(self) -> None:
        if not self._host_lease.held:
            return
        self.pairing.begin_pairing()
        self._refresh_pairing_ui()

    def _driver_workers_busy(self) -> bool:
        return any(worker is not None and worker.is_alive()
                   for worker in (self._driver_startup_worker, self._driver_cleanup_worker))

    def _driver_cleanup_confirmed(self) -> bool:
        if self._driver_runtime is None:
            return True
        try:
            return self._driver_runtime.snapshot.cleanup_confirmed is True
        except Exception:
            return False

    def _driver_audio_factory(self):
        from .driver_audio_bridge import DriverAudioBridge
        owner = self._driver_runtime
        return partial(DriverAudioBridge, driver_backend=owner.backend, sink_factory=owner.acquire_sink)

    def _prepare_driver_runtime(self) -> bool:
        """Off-Tk startup, before any phone route activation; policy is injected."""
        if self._driver_workers_busy():
            self._set_status_text('Подготовка аудиокомпонента ещё выполняется')
            return False
        if self._driver_runtime is not None:
            if self._driver_ready and not self._driver_cancelled:
                if self._driver_runtime.backend.is_available():
                    return True
                self._set_status_text('Аудиокомпонент недоступен — остановите подключение')
                return False
            if not self._driver_cleanup_confirmed():
                self._set_status_text('Перезапустите RemonteVoice: остановка аудиокомпонента не подтверждена')
                return False
        try:
            self._driver_runtime = self._driver_runtime_factory()  # Construction must be inert.
        except Exception:
            self._set_status_text('Не удалось подготовить аудиокомпонент')
            return False
        owner = self._driver_runtime
        self._driver_generation += 1
        token = self._driver_generation
        self._driver_cancelled = self._driver_ready = False
        self._driver_cleanup_error = ''
        self._driver_result = None
        def prepare():
            error = ''
            try:
                owner.start()
                deadline = time.monotonic() + 2.0
                while not owner.backend.is_available():
                    if self._driver_cancelled or token != self._driver_generation or time.monotonic() >= deadline:
                        raise RuntimeError('Подготовка аудиокомпонента отменена или просрочена')
                    time.sleep(0.02)
                if self._driver_cancelled or token != self._driver_generation:
                    raise RuntimeError('Подготовка аудиокомпонента отменена')
            except BaseException:
                error = 'Не удалось подтвердить готовность аудиокомпонента'
                try:
                    owner.stop(timeout=1.0)
                except BaseException:
                    self._driver_cleanup_error = 'Остановка аудиокомпонента не подтверждена'
            self._driver_result = (token, owner, error)
        self._driver_startup_worker = threading.Thread(target=prepare, name='remotevoice-driver-start', daemon=True)
        self._driver_startup_worker.start()
        self.start_button.configure(state='disabled')
        self.stop_button.configure(state='normal')
        self._set_status_text('Готовим аудиокомпонент…')
        self.after(50, self._finish_driver_startup, token)
        return False

    def _finish_driver_startup(self, token: int) -> None:
        if token != self._driver_generation:
            return
        if self._driver_startup_worker is not None and self._driver_startup_worker.is_alive():
            self.after(50, self._finish_driver_startup, token)
            return
        result = self._driver_result
        if self._driver_cancelled or result is None or result[:2] != (token, self._driver_runtime):
            return
        if result[2]:
            self._driver_ready = False
            self._set_running_controls(False)
            self._set_status_text(result[2])
            return
        self._driver_ready = True
        self._start_host()

    def _begin_driver_cleanup(self) -> None:
        if self._driver_runtime is None or self._driver_workers_busy() or self._driver_cleanup_confirmed():
            return
        if self._driver_cleanup_error:
            return  # Retain the owner; require an explicit restart after failed cleanup.
        owner = self._driver_runtime
        def cleanup():
            try:
                owner.stop(timeout=1.0)
                if owner.snapshot.cleanup_confirmed is not True:
                    raise RuntimeError('cleanup unconfirmed')
            except BaseException:
                self._driver_cleanup_error = 'Остановка аудиокомпонента не подтверждена'
        self._driver_cleanup_worker = threading.Thread(target=cleanup, name='remotevoice-driver-stop', daemon=True)
        self._driver_cleanup_worker.start()

    @staticmethod
    def _voicemeeter_window_module():
        return voicemeeter_window

    def _refresh_voicemeeter_status(self) -> None:
        module = self._voicemeeter_window_module()
        try:
            running = module.running()
        except Exception:
            running = False
        if not running:
            text = "Не запущен — приложение запустит его при подключении"
        elif self.keep_voicemeeter_hidden_var.get():
            text = "Работает в фоне, окно скрыто"
        else:
            text = "Работает, окно можно открыть"
        self.voicemeeter_status_var.set(text)

    def _apply_voicemeeter_policy(self) -> None:
        if not self.server_running:
            return
        module = self._voicemeeter_window_module()
        try:
            if self.keep_voicemeeter_hidden_var.get():
                module.hide()
        except Exception:
            LOGGER.warning("Voicemeeter window supervision failed")
        self._refresh_voicemeeter_status()

    def _toggle_voicemeeter_window(self) -> None:
        module = self._voicemeeter_window_module()
        try:
            if self.keep_voicemeeter_hidden_var.get():
                module.hide()
            else:
                module.show()
        except Exception:
            LOGGER.warning("Voicemeeter window toggle failed")
        self._refresh_voicemeeter_status()

    def _show_voicemeeter(self) -> None:
        module = self._voicemeeter_window_module()
        try:
            module.show()
        except Exception:
            LOGGER.warning("Voicemeeter window reveal failed")
        self.keep_voicemeeter_hidden_var.set(False)
        self._refresh_voicemeeter_status()

    def _start_host(self) -> None:
        if self.server_running:
            return
        if self.server_thread is not None and self.server_thread.is_alive():
            self._set_status_text("Предыдущее подключение ещё завершается")
            return
        if not self._host_lease.held:
            self._set_status_text("Другое окно RemonteVoice управляет аудиоустройствами")
            return
        if self.server is not None and self.server.cleanup_failed is True:
            self._set_status_text("Перезапустите RemonteVoice: аудиопотоки не завершены")
            return
        restored = self._recover_waiting_routes()
        if not restored:
            messagebox.showerror("Не удалось восстановить аудиоустройства", self._route_errors())
            return
        driver_mode = self._driver_runtime_factory is not None
        if driver_mode:
            if (not self.ip_var.get() or not self.fallback_output_choices.get(self.fallback_output_var.get())
                    or not self.local_microphone_choices.get(self.local_microphone_var.get())):
                self._set_running_controls(False)
                messagebox.showerror('Нужны аудиоустройства',
                    'Выберите локальный адрес, физические динамики и микрофон для возврата.')
                return
            if not self._prepare_driver_runtime():
                return
            observed = self._driver_runtime.observe()
            capture_id = system_output_id = observed.capture_endpoint_id
            playback_id = virtual_microphone_id = observed.microphone_endpoint_id
        else:
            capture_id = self.capture_choices.get(self.capture_var.get())
            playback_id = self.playback_choices.get(self.playback_var.get())
            system_output_id = self.system_output_choices.get(self.system_output_var.get())
        fallback_output_id = self.fallback_output_choices.get(self.fallback_output_var.get())
        local_microphone_id = self.local_microphone_choices.get(self.local_microphone_var.get())
        mute_local_microphone = bool(self.mute_local_microphone_var.get())
        host_ip = self.ip_var.get()
        if not host_ip or not capture_id or not playback_id or not system_output_id or not fallback_output_id:
            messagebox.showerror(
                "Нужны аудиоустройства",
                "Выберите IP-адрес, виртуальные аудиоустройства и динамики или "
                "наушники для возврата звука.",
            )
            return
        if not local_microphone_id:
            messagebox.showerror(
                "Выберите микрофон ноутбука",
                "Выберите физический микрофон для возврата после отключения телефона.",
            )
            return
        capture_name = self.capture_names.get(capture_id, "")
        playback_name = self.playback_names.get(playback_id, "")
        if not driver_mode:
            virtual_microphone_id = matching_virtual_microphone(playback_id, playback_name, self.virtual_microphones)
        if not virtual_microphone_id:
            messagebox.showerror("Нужен виртуальный микрофон",
                                 "Не удалось однозначно найти сторону записи канала голоса телефона.")
            return
        if not driver_mode and virtual_device_family(capture_name) == virtual_device_family(playback_name):
            messagebox.showerror(
                "Нужны разные каналы",
                "Выберите две разные виртуальные аудиопары, чтобы голос телефона "
                "не возвращался обратно как системный звук.",
            )
            return
        if not driver_mode and system_output_id == playback_id:
            messagebox.showerror(
                "Нужны разные каналы",
                "Системный звук и голос телефона должны идти в разные виртуальные каналы.",
            )
            return

        try:
            output_route = OutputRoute(system_output_id, fallback_output_id)
            # Until a phone connects, a previous virtual default must not swallow audio.
            output_route.restore()
        except OutputRoutingError as exc:
            messagebox.showerror(
                "Не удалось настроить возврат звука",
                f"Подключение не запущено. Проверьте устройство динамиков или наушников.\n{exc}",
            )
            return
        self.output_route = output_route
        self.input_route = InputRoute(virtual_microphone_id, local_microphone_id)
        try:
            self.input_route.restore()
        except InputRoutingError as exc:
            self._input_route_error = str(exc)
            messagebox.showerror("Не удалось вернуть микрофон ноутбука", str(exc))
            return
        self._input_route_active = False
        self._input_route_error = ""
        try:
            self._audio_backend = (self._driver_runtime.backend if driver_mode
                                   else prepare_audio_backend(capture_name, playback_name))
            if driver_mode and not self._audio_backend.matches_endpoints(capture_id, playback_id):
                raise AudioBackendError('Подтверждённые устройства драйвера изменились')
        except AudioBackendError as exc:
            self._set_status_text(str(exc))
            messagebox.showerror("Не удалось разделить аудиоканалы", str(exc), parent=self)
            return
        self.microphone_guard = (
            MicrophoneMuteGuard(
                local_microphone_id,
                journal_path=self.data_dir / "microphone-restore.json",
            )
            if mute_local_microphone and local_microphone_id
            else None
        )
        self._microphone_guard_active = False
        self._microphone_guard_error = ""
        self._microphone_activation_error = ""
        self._session_restore_failure = ""
        self._session_activation_error = ""
        self._output_route_active = False
        self._output_route_error = ""
        self._route_activation_failed = False
        self._stopping = False

        saved_devices = {} if driver_mode else {
                "capture_device_id": capture_id, "playback_device_id": playback_id,
                "system_output_device_id": system_output_id, "virtual_microphone_device_id": virtual_microphone_id}
        self.config_data.update(
            {**saved_devices,
                "fallback_output_device_id": fallback_output_id,
                "local_microphone_device_id": local_microphone_id or "",
                "mute_local_microphone": mute_local_microphone,
                "host_ip": host_ip,
            }
        )
        self._save_config()
        if driver_mode:
            try:
                save_driver_recovery(self.data_dir, DriverRouteRecovery(
                    system_output_id, fallback_output_id, virtual_microphone_id, local_microphone_id))
            except DriverRecoveryError:
                self._set_running_controls(False)
                self._set_status_text('Не удалось сохранить план возврата аудиоустройств')
                return
        try:
            if self._safety_guard is not None:
                self._safety_guard.disarm()
                self._safety_guard = None
            self._safety_guard = SafetyGuard.start(
                self.data_dir, system_output_id, fallback_output_id,
                virtual_microphone_id, local_microphone_id,
            )
        except SafetyGuardError:
            self._set_status_text("Подключение недоступно: аварийное восстановление аудио недоступно")
            messagebox.showerror("Не удалось защитить возврат звука", self.status_var.get(), parent=self)
            return
        self._server_generation += 1
        generation = self._server_generation
        self.server = RemoteVoiceServer(
            bind_host=host_ip,
            port=PORT,
            capture_device_id=capture_id,
            playback_device_id=playback_id,
            identity_dir=self.data_dir / "identity",
            pairing=self.pairing,
            on_status=lambda status: self._post_for_generation(generation, status),
            on_screen_status=lambda status: self._post_for_generation(generation, status, screen=True),
            on_session_start=lambda: self._request_session_start(generation),
            on_audio_status=lambda status, session: self._post_audio_status(generation, session, status),
            **({'audio_factory': self._driver_audio_factory()} if driver_mode else {}),
        )
        self.server.set_screen_allowed(bool(self.screen_allowed_var.get()))
        self.server_thread = threading.Thread(
            target=self._run_server, args=(self.server, generation), name="remotevoice-server", daemon=True
        )
        self.server_running = True
        self.status_var.set("Готовим подключение…")
        self._set_running_controls(True)
        self._refresh_pairing_ui()
        self.server_thread.start()
        self.after(500, self._check_safety_guard, generation)
        for delay in (2000, 6000, 12000):
            self.after(delay, lambda: self._apply_voicemeeter_policy())

    def _check_safety_guard(self, generation: int) -> None:
        if generation != self._server_generation or not self.server_running:
            return
        if self._safety_guard is None or not self._safety_guard.armed:
            self._stop_host()
            self._session_activation_error = "Процесс аварийного восстановления завершился"
            self._set_status_text("Подключение остановлено")
            return
        backend = self._audio_backend
        try:
            available = (self._driver_runtime.guard_is_available()
                         if self._driver_runtime_factory is not None and self._driver_runtime is not None
                         else backend is None or backend.is_available())
        except Exception:
            LOGGER.warning("Audio component health check failed")
            available = False
        if not available:
            # A dead mixer still exposes virtual endpoints and silent PCM.
            # Restore laptop devices immediately instead of treating silence
            # as evidence that the audio session is healthy.
            self._stop_host()
            self._session_activation_error = backend.unavailable_message
            self._set_status_text("Звук и микрофон возвращаются на ноутбук")
            return
        self.after(500, self._check_safety_guard, generation)

    def _update_screen_permission(self) -> None:
        allowed = bool(self.screen_allowed_var.get())
        if self.server is not None:
            self.server.set_screen_allowed(allowed)
        self.screen_status_var.set(
            "Просмотр разрешён — включите его на телефоне" if allowed
            else "Просмотр экрана выключен"
        )

    def _post_screen_status(self, status: str) -> None:
        self._queue_screen_status(self._server_generation, status)

    def _queue_screen_status(self, generation: int, status: str) -> None:
        # Tk.after from another thread waits for Tcl to service that call.
        # The asyncio server must never wait for a busy desktop UI to paint an
        # observational screen indicator. Only the newest indicator is useful.
        with self._screen_events_lock:
            if not self._screen_events_closed and generation == self._server_generation:
                self._pending_screen_event = (generation, status)

    def _drain_screen_status(self) -> None:
        """Called only by Tk's main thread; bounded mailbox, no frame data."""
        with self._screen_events_lock:
            if self._screen_events_closed:
                return
            event = self._pending_screen_event
            self._pending_screen_event = None
            audio_event = self._pending_audio_event
            self._pending_audio_event = None
        if (audio_event is not None and audio_event[0] == self._server_generation
                and self.server is not None and self.server.is_current_audio_session(audio_event[1])):
            self._set_status(audio_event[2])
        if event is not None and event[0] == self._server_generation:
            self._set_screen_status(event[1])
        self.after(25, self._drain_screen_status)

    def _set_screen_status(self, status: str) -> None:
        if not self.screen_allowed_var.get():
            self.screen_status_var.set("Просмотр экрана выключен")
        elif not self.server_running or self._stopping:
            self.screen_status_var.set("Просмотр недоступен — подключение остановлено")
        elif self.server is not None and self.server.screen_streaming is True:
            # Notifications may wait in Tk while an older preview closes. Read
            # the current owner instead of repainting from a retired event.
            self.screen_status_var.set("● Основной экран передаётся на телефон")
        elif status == "screen_error":
            self.screen_status_var.set("Не удалось передать экран — звук продолжает работать")
        else:
            self.screen_status_var.set("Просмотр разрешён — включите его на телефоне")

    def _run_server(self, server: RemoteVoiceServer, generation: int) -> None:
        driver = self._driver_runtime
        try:
            asyncio.run(server.run())
        except Exception as exc:
            LOGGER.exception("Host server stopped")
            self._post_for_generation(generation, f"server_error: {exc}")
        finally:
            if driver is not None:
                if self._driver_runtime is driver:
                    self._driver_ready = False
                    self._driver_cancelled = True
                try:
                    driver.stop(timeout=1.0)
                    if driver.snapshot.cleanup_confirmed is not True:
                        raise RuntimeError('Driver cleanup unconfirmed')
                except BaseException:
                    self._driver_cleanup_error = 'Остановка аудиокомпонента не подтверждена'
                    self._post_for_generation(generation, 'audio_cleanup_failed: driver owner retained')

    def _post_for_generation(self, generation: int, status: str, *, screen: bool = False) -> None:
        if screen:
            self._queue_screen_status(generation, status)
            return
        def apply() -> None:
            if generation == self._server_generation:
                (self._set_screen_status if screen else self._set_status)(status)
        try:
            self.after(0, apply)
        except (RuntimeError, tk.TclError):
            pass

    def _post_audio_status(self, generation: int, session: object, status: str) -> None:
        # A recorder is already running when it publishes audio_active. Calling
        # Tk here can wait for a busy UI while native capture accumulates PCM.
        # Reuse the main-thread mailbox pump, retaining a terminal error over
        # a late active event and rechecking ownership again when consumed.
        with self._screen_events_lock:
            if (self._screen_events_closed or generation != self._server_generation
                    or self.server is None or not self.server.is_current_audio_session(session)):
                return
            previous = self._pending_audio_event
            if previous is not None and previous[:2] == (generation, session):
                if previous[2].startswith('audio_error:'):
                    return
                if previous[2] == 'iphone_connected_muted' and status == 'audio_active':
                    return  # The connect event also refreshes the retired pairing QR.
            self._pending_audio_event = (generation, session, status)

    def _request_session_start(self, generation: int) -> bool:
        """Network thread waits for a confirmed, cancellable UI route transaction."""
        finished = threading.Event()
        cancelled = threading.Event()
        result = [False]
        deadline = time.monotonic() + 4.0

        def prepare() -> None:
            try:
                if (cancelled.is_set() or time.monotonic() >= deadline
                        or generation != self._server_generation
                        or self._stopping or not self.server_running):
                    LOGGER.warning("Session preparation callback rejected: expired=%s stale=%s stopping=%s running=%s",
                                   cancelled.is_set() or time.monotonic() >= deadline,
                                   generation != self._server_generation, self._stopping, self.server_running)
                    return
                result[0] = self._activate_session_routes()
                if (cancelled.is_set() or time.monotonic() >= deadline or self._stopping
                        or generation != self._server_generation):
                    self._restore_session_routes()
                    result[0] = False
            finally:
                finished.set()
        try:
            self.after(0, prepare)
        except (RuntimeError, tk.TclError):
            return False
        # Shorter than the server's preparation deadline; a delayed callback is
        # cancelled so it cannot switch devices after the connection has failed.
        if not finished.wait(max(0.0, deadline - time.monotonic())):
            cancelled.set()
            LOGGER.warning("Session preparation did not finish before UI deadline")
            return False
        return result[0]

    def _activate_session_routes(self) -> bool:
        if (not self._host_lease.held or self._stopping or self._route_activation_failed
                or not self.server_running):
            LOGGER.warning("Session route activation rejected: lease=%s stopping=%s previous_failure=%s running=%s",
                           self._host_lease.held, self._stopping, self._route_activation_failed, self.server_running)
            return False
        if self._safety_guard is None or not self._safety_guard.armed:
            self._stop_host()
            self._session_activation_error = "Аварийное восстановление аудио недоступно"
            self._set_status_text("Подключение остановлено")
            return False
        backend = self._audio_backend
        try:
            available = backend is None or backend.is_available()
        except Exception:
            available = False
        if not available:
            # Recheck immediately before muting/rerouting, not only on the UI
            # health timer: the component may have died since the last poll.
            self._stop_host()
            self._session_activation_error = backend.unavailable_message
            self._set_status_text("Подключение остановлено")
            return False
        if (self._activate_microphone_guard() and self._activate_input_route()
                and self._activate_output_route()):
            return True
        primary_error = self._route_errors()
        self._stop_host()
        self._session_activation_error = primary_error or "Не удалось подготовить аудиоустройства"
        self._set_status_text("Подключение остановлено")
        return False

    def _activate_output_route(self) -> bool:
        if self.output_route is None or self._output_route_active:
            return True
        if self._route_activation_failed or self._stopping:
            return False
        try:
            self.output_route.activate()
        except OutputRoutingError as exc:
            LOGGER.warning("Could not switch Windows output to the phone: %s", exc)
            self._route_activation_failed = True
            self._output_route_error = f"Не удалось направить звук на телефон: {exc}"
            return False
        self._output_route_active = True
        self._output_route_error = ""
        return True

    def _activate_microphone_guard(self) -> bool:
        if self.microphone_guard is None or self._microphone_guard_active:
            return True
        if self._stopping or self._route_activation_failed:
            return False
        try:
            self.microphone_guard.activate()
        except MicrophoneGuardError as exc:
            LOGGER.warning("Could not mute the laptop microphone: %s", exc)
            self._microphone_activation_error = f"Не удалось выключить микрофон ноутбука: {exc}"
            return False
        self._microphone_guard_active = True
        self._microphone_activation_error = ""
        return True

    def _restore_microphone_guard(self) -> bool:
        self._microphone_guard_active = False
        if self.microphone_guard is None:
            return True
        try:
            self.microphone_guard.restore()
        except MicrophoneGuardError as exc:
            LOGGER.warning("Could not restore the laptop microphone: %s", exc)
            self._microphone_guard_error = (
                f"Не удалось вернуть прежнее состояние микрофона ноутбука: {exc}. "
                "Проверьте микрофон в настройках Windows."
            )
            return False
        self._microphone_guard_error = ""
        return True

    def _restore_session_routes(self) -> bool:
        if not self._host_lease.held:
            return False
        results = []
        for restore in (self._restore_output_route, self._restore_input_route,
                        self._restore_microphone_guard):
            try:
                results.append(restore())
            except Exception:
                # An unexpected adapter error must not skip the other devices.
                LOGGER.exception("Audio device recovery failed")
                self._session_restore_failure = "Восстановление одного из аудиоустройств не подтверждено"
                results.append(False)
        return all(results)

    def _activate_input_route(self) -> bool:
        if self.input_route is None or self._input_route_active:
            return True
        try:
            self.input_route.activate()
        except InputRoutingError as exc:
            self._input_route_error = f"Не удалось выбрать микрофон телефона: {exc}"
            return False
        self._input_route_active = True
        self._input_route_error = ""
        return True

    def _restore_input_route(self) -> bool:
        self._input_route_active = False
        if self.input_route is None:
            return True
        try:
            self.input_route.restore()
        except InputRoutingError as exc:
            self._input_route_error = f"Не удалось вернуть микрофон ноутбука: {exc}"
            return False
        self._input_route_error = ""
        return True

    def _route_errors(self, *, include_activation: bool = True) -> str:
        return " · ".join(
            error for error in (
                self._output_route_error,
                self._input_route_error,
                self._microphone_activation_error if include_activation else "",
                self._microphone_guard_error,
                self._session_activation_error if include_activation else "",
            ) if error
        )

    def _restore_output_route(self) -> bool:
        # A partial failure leaves the route unknown, never fully active.
        self._output_route_active = False
        if self.output_route is None:
            return True
        try:
            self.output_route.restore()
        except OutputRoutingError as exc:
            LOGGER.warning("Could not restore Windows output: %s", exc)
            self._output_route_error = (
                f"Не удалось вернуть звук на динамики или наушники: {exc}. "
                "Выберите их вручную в выводе звука Windows."
            )
            return False
        self._output_route_error = ""
        return True

    def _set_running_controls(self, running: bool) -> None:
        cleanup_failed = self.server is not None and self.server.cleanup_failed is True
        driver_busy = (self._driver_runtime_factory is not None and
            (self._driver_workers_busy() or not self._driver_ready and not self._driver_cleanup_confirmed()))
        self.start_button.configure(state="disabled" if running or cleanup_failed or driver_busy else "normal")
        driver_owned = (self._driver_runtime_factory is not None and
                        (self._driver_workers_busy() or not self._driver_cleanup_confirmed()))
        self.stop_button.configure(state="normal" if running or driver_owned else "disabled")
        for combo in (
            self.ip_combo,
            self.capture_combo,
            self.playback_combo,
            self.system_output_combo,
            self.fallback_output_combo,
        ):
            combo.configure(state="disabled" if running else "readonly")
        self.mute_local_microphone_check.configure(state="disabled" if running else "normal")
        self._update_microphone_controls()

    def _set_status_text(self, text: str) -> None:
        details = [text]
        if self._microphone_guard_active:
            details.append("микрофон ноутбука выключен")
        for error in (self._route_errors(), self._session_restore_failure):
            if error and error not in details:
                details.append(error)
        self.status_var.set(" · ".join(details))

    def _stop_host(self) -> None:
        self._stopping = True
        if self._driver_runtime_factory is not None:
            self._driver_cancelled = True
            self._driver_ready = False
            self._driver_generation += 1
        if self._driver_runtime_factory is not None and self._driver_runtime is not None:
            try:
                # Do not wait for the startup worker to finish before cancelling
                # its native component. Cleanup and joins remain on their workers.
                self._driver_runtime.request_stop()
            except Exception:
                LOGGER.warning('Driver cancellation signal failed; final cleanup is still required')
        self._restore_session_routes()
        if self.server is not None:
            self.server.stop()
        if self.server_thread is None or not self.server_thread.is_alive():
            self._begin_driver_cleanup()
        if (self.server_thread is not None and self.server_thread.is_alive()) or self._driver_workers_busy():
            self._set_status_text("Завершаем подключение…")
            self.start_button.configure(state="disabled")
            self.stop_button.configure(state="disabled")
            self.after(100, self._finish_stop_when_ready)
            return
        self.server_running = False
        self._set_running_controls(False)
        self._set_status_text(self._driver_cleanup_error or "Подключение остановлено")
        self._refresh_pairing_ui()

    def _finish_stop_when_ready(self) -> None:
        if (self.server_thread is not None and self.server_thread.is_alive()) or self._driver_workers_busy():
            self.after(100, self._finish_stop_when_ready)
            return
        if not self._driver_cleanup_confirmed() and not self._driver_cleanup_error:
            self._begin_driver_cleanup()
            self.after(100, self._finish_stop_when_ready)
            return
        self.server_running = False
        self._restore_session_routes()
        self._set_running_controls(False)
        self._set_status_text("Подключение остановлено")
        if self._driver_cleanup_error:
            self.start_button.configure(state='disabled')
            self._set_status_text(self._driver_cleanup_error)
        self._refresh_pairing_ui()

    def _post_status(self, status: str) -> None:
        try:
            self.after(0, self._set_status, status)
        except RuntimeError:
            # The window may be closing while the server thread shuts down.
            pass

    def _set_status(self, status: str) -> None:
        # A native recorder may finish opening after the session was restored.
        # Its queued notification must not paint a disconnected session as active.
        if status == "audio_active" and (
            not self.server_running or self._stopping or not self._output_route_active
        ):
            return
        if status in {"iphone_connected_muted", "iphone_microphone_on"}:
            if self.server_running and not self._stopping:
                if not self._activate_session_routes():
                    return
        elif status in {"server_ready", "server_stopped"} or status.startswith(
            ("audio_error:", "client_error:", "server_error:", "audio_cleanup_failed:")
        ):
            restored = self._restore_session_routes()
            if status == "server_ready" and not restored:
                # Unexpected adapter failures and lost recovery ownership may
                # have no typed route error. Keep that failure visible and keep
                # the independent guard armed until an explicit clean restart.
                self._session_restore_failure = (
                    self._route_errors(include_activation=False)
                    or self._session_restore_failure
                    or "Восстановление аудиоустройств не подтверждено"
                )
                self._route_activation_failed = True
                self._stopping = True
                if self.server is not None:
                    self.server.stop()
                self.start_button.configure(state="disabled")
                self.stop_button.configure(state="disabled")
                self._set_status_text("Подключение завершается после ошибки восстановления устройств")
                return
            # An error must not let a delayed audio callback reactivate the route.
            self._route_activation_failed = status != "server_ready"
        if status.startswith("audio_cleanup_failed:"):
            self._stopping = True
            self._route_activation_failed = True
            self._session_restore_failure = "Аудиопотоки не завершены. Перезапустите RemonteVoice"
            self.start_button.configure(state="disabled")
            self._set_status_text("Остановка аудио не подтверждена")
            return
        labels = {
            "server_starting": "Готовим подключение…",
            "server_ready": "Ожидает телефон · микрофон выключен",
            "iphone_connected_muted": "Телефон подключён · микрофон выключен",
            "iphone_microphone_on": "Телефон подключён · микрофон включён",
            "audio_active": "Телефон подключён · аудиомаршрут активен",
            "server_stopped": "Подключение остановлено",
        }
        if status.startswith("audio_error:") or status.startswith("server_error:"):
            self._set_status_text(status.split(":", 1)[1].strip())
        elif status.startswith("client_error:"):
            self._set_status_text("Ошибка клиентской сессии: " + status.split(":", 1)[1].strip())
        else:
            self._set_status_text(labels.get(status, status))
        if status in {"iphone_connected_muted", "iphone_microphone_on", "server_ready"}:
            self._refresh_pairing_ui()
        if status == "server_stopped" or status.startswith("server_error:"):
            self.server_running = False
            self._set_screen_status("screen_stopped")
            self._set_running_controls(False)
            self._refresh_pairing_ui()
            if self._driver_runtime_factory is not None:
                self.after(100, self._finish_stop_when_ready)

    def _on_close(self) -> None:
        if self._close_deadline is None:
            self._close_deadline = time.monotonic() + _CLOSE_GRACE_SECONDS
        self._stop_host()
        errors = self._route_errors(include_activation=False)
        if errors:
            messagebox.showerror(
                "Аудиоустройства не восстановлены", errors, parent=self
            )
        if (self.server_thread is not None and self.server_thread.is_alive()) or self._driver_workers_busy():
            self.after(100, self._close_when_stopped)
        else:
            self._close_when_stopped()

    def _close_when_stopped(self) -> None:
        if (self.server_thread is not None and self.server_thread.is_alive()) or self._driver_workers_busy():
            if self._close_deadline is None or time.monotonic() < self._close_deadline:
                self.after(100, self._close_when_stopped)
                return
            # Destroying Tk is insufficient: executor shutdown hooks can wait
            # forever for native calls. Exit this owner process without running
            # those hooks, disarming recovery or releasing its lease early.
            os._exit(70)
        if ((self.server is not None and self.server.cleanup_failed is True)
                or not self._driver_cleanup_confirmed()):
            # A retired worker is not proof that its native/executor work ended.
            os._exit(70)
        self._destroy_host()

    def _destroy_host(self) -> None:
        with self._screen_events_lock:
            self._screen_events_closed = True
            self._pending_screen_event = None
            self._pending_audio_event = None
        self._server_generation += 1
        # Incomplete cleanup retains ownership until process exit; another
        # instance must not open endpoints still held by the old workers.
        if (not (self.server is not None and self.server.cleanup_failed is True)
                and not (self.server_thread is not None and self.server_thread.is_alive())
                and self._driver_cleanup_confirmed() and not self._driver_workers_busy()
                and not self._route_errors(include_activation=False)
                and not self._session_restore_failure):
            try:
                if self._safety_guard is not None:
                    self._safety_guard.disarm()
                    self._safety_guard = None
                self._host_lease.release()
            except SafetyGuardError:
                LOGGER.warning("Recovery helper cleanup was not confirmed")
        try:
            close_audio_backend()
        except AudioBackendError:
            # Physical-route restoration and guard ownership were decided
            # above; a final SDK error must not rewrite restored devices.
            LOGGER.warning("Audio component SDK logout was not confirmed")
        self.destroy()


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="RemonteVoice Windows Host")
    parser.add_argument("--autostart", action="store_true", help="Start with saved audio configuration")
    parser.add_argument("--safety-guard", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--driver-backend-lab", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--broker-lab-probe", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--broker-lab-synthetic", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--broker-lab-report", type=Path, help=argparse.SUPPRESS)
    options = parser.parse_args(argv)
    if options.broker_lab_probe:
        report_path = options.broker_lab_report
        if (options.safety_guard or options.driver_backend_lab or options.autostart
                or report_path is None or not report_path.is_absolute()
                or not report_path.parent.is_dir() or report_path.exists()
                or (getattr(sys, 'frozen', False) and
                    report_path.resolve().is_relative_to(Path(sys.executable).resolve().parent))):
            parser.error('Probe requires a new absolute report path outside the installation and no UI flags')
        from .broker_lab_probe import run_probe
        result = run_probe(synthetic_pcm=options.broker_lab_synthetic)
        with report_path.open('x', encoding='utf-8') as report:
            json.dump(result, report, ensure_ascii=True, sort_keys=True)
            report.write('\n')
        if result.get('cleanup_confirmed') is not True:
            os._exit(70)  # Retained native workers/handles must end with this test process.
        raise SystemExit(0 if result.get('success') is True else 1)
    if options.broker_lab_synthetic or options.broker_lab_report is not None:
        parser.error('Probe options require --broker-lab-probe')
    if options.safety_guard:
        from .safety_guard import main as guard_main
        guard_main()
        return
    try:
        data_dir = app_data_dir()
        data_dir.mkdir(parents=True, exist_ok=True)
        handler = RotatingFileHandler(data_dir / 'host.log', maxBytes=256 * 1024,
                                      backupCount=2, encoding='utf-8')
        try:
            logging.basicConfig(level=logging.INFO, handlers=[handler])
        finally:
            # basicConfig leaves existing logging untouched (e.g. an embedding
            # application/test runner). Do not leak the unused file handle.
            if handler not in logging.getLogger().handlers:
                handler.close()
    except OSError:
        # A log destination must not prevent audio recovery or launching UI.
        logging.basicConfig(level=logging.INFO)
    try:
        if options.driver_backend_lab:
            from .installed_driver_host import InstalledDriverHost
            app = HostWindow(driver_runtime_factory=InstalledDriverHost)
        else:
            app = HostWindow()
    except HostLeaseError:
        return
    if options.autostart:
        app.after(100, app._start_host)
    app.mainloop()


if __name__ == "__main__":
    main()

