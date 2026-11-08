"""Shared desktop theme and layout; audio/session behavior stays in HostWindow."""
from __future__ import annotations

from collections import Counter
import os
import tkinter as tk
from tkinter import ttk


# The semantic palette follows the Android client. Spacing uses a four-pixel grid.
COLORS = {
    "background": "#101815", "surface": "#1B2922", "control": "#24392E",
    "hover": "#304C3D", "border": "#365144", "text": "#EBF5F0",
    "muted": "#A4B9AE", "accent": "#80E8BA", "accent_hover": "#A0F3D0",
    "accent_pressed": "#63CFA2", "accent_text": "#102019",
    "disabled": "#23342B", "disabled_text": "#83988B",
}
FONT = "Segoe UI"


def device_choices(devices: list[dict[str, str]], *, capture: bool = False) -> dict[str, str]:
    """Readable labels retain exact IDs; duplicate names never overwrite a device."""
    names = [d["name"] + (" · системный звук" if capture and d.get("loopback") == "True" else "")
             for d in devices]
    counts = Counter(names)
    result: dict[str, str] = {}
    for device, name in zip(devices, names):
        label = name if counts[name] == 1 else f"{name} · {device['id']}"
        # Also handle an actual device name matching a generated disambiguator.
        while label in result:
            label += " · устройство"
        result[label] = device["id"]
    return result


def apply_theme(window: tk.Tk) -> float:
    scale = max(1.0, float(window.tk.call("tk", "scaling")) / (96 / 72))
    px = lambda value: round(value * scale)
    c = COLORS
    window.configure(background=c["background"])
    style = ttk.Style(window)
    style.theme_use("clam")
    style.configure(".", background=c["background"], foreground=c["text"],
                    font=(FONT, 10), bordercolor=c["border"], lightcolor=c["border"],
                    darkcolor=c["border"], focuscolor=c["accent"])
    style.configure("TFrame", background=c["background"])
    style.configure("Card.TFrame", background=c["surface"], relief="solid", borderwidth=1)
    style.configure("CardBody.TFrame", background=c["surface"])
    for name, bg, fg, font in [
        ("TLabel", c["background"], c["text"], (FONT, 10)),
        ("Title.TLabel", c["background"], c["text"], (FONT, 28, "bold")),
        ("Eyebrow.TLabel", c["background"], c["accent"], (FONT, 9, "bold")),
        ("Muted.TLabel", c["background"], c["muted"], (FONT, 10)),
        ("Card.TLabel", c["surface"], c["text"], (FONT, 11)),
        ("CardTitle.TLabel", c["surface"], c["text"], (FONT, 13, "bold")),
        ("CardMuted.TLabel", c["surface"], c["muted"], (FONT, 10)),
        ("Status.TLabel", c["surface"], c["text"], (FONT, 14, "bold")),
        ("Live.Status.TLabel", c["surface"], c["accent"], (FONT, 14, "bold")),
        ("Live.CardMuted.TLabel", c["surface"], c["accent"], (FONT, 10)),
        ("Code.TLabel", c["surface"], c["accent"], (FONT, 18, "bold")),
    ]:
        style.configure(name, background=bg, foreground=fg, font=font)
    for name, bg, fg in [("TButton", c["control"], c["text"]),
                          ("Primary.TButton", c["accent"], c["accent_text"])]:
        primary = name.startswith("Primary")
        style.configure(name, background=bg, foreground=fg, borderwidth=0,
                        font=(FONT, 10, "bold"), padding=(px(16), px(11)),
                        focusthickness=2, focuscolor=c["accent_text"] if primary else c["accent"])
        style.map(name, background=[("disabled", c["disabled"]),
                                   ("pressed", c["accent_pressed"] if primary else c["border"]),
                                   ("active", c["accent_hover"] if primary else c["hover"])],
                  foreground=[("disabled", c["disabled_text"])])
    style.configure("TCombobox", fieldbackground=c["background"], background=c["control"],
                    foreground=c["text"], arrowcolor=c["accent"], padding=px(8),
                    arrowsize=px(16), borderwidth=1, insertcolor=c["text"], font=(FONT, 10))
    style.map("TCombobox", fieldbackground=[("disabled", c["disabled"]),
                                           ("readonly", c["background"])],
              foreground=[("disabled", c["disabled_text"]), ("readonly", c["text"])],
              selectbackground=[("readonly", c["control"])],
              selectforeground=[("readonly", c["text"])],
              bordercolor=[("focus", c["accent"])])
    style.configure("TCheckbutton", background=c["surface"], foreground=c["text"],
                    padding=(0, px(6)), indicatorbackground=c["background"],
                    indicatorforeground=c["accent_text"], indicatorsize=px(16), font=(FONT, 10))
    style.map("TCheckbutton", background=[("active", c["surface"])],
              foreground=[("disabled", c["disabled_text"])],
              indicatorbackground=[("disabled", c["disabled"]), ("selected", c["accent"])],
              indicatorforeground=[("disabled", c["disabled_text"])])
    style.configure("TNotebook", background=c["background"], borderwidth=0, tabmargins=0)
    style.configure("TNotebook.Tab", background=c["background"], foreground=c["muted"],
                    padding=(px(18), px(10)), font=(FONT, 11, "bold"),
                    bordercolor=c["border"], lightcolor=c["border"], darkcolor=c["border"])
    style.map("TNotebook.Tab", background=[("selected", c["surface"]), ("active", c["control"])],
              foreground=[("selected", c["accent"]), ("active", c["text"])],
              expand=[("selected", (0, 0, 0, 0))],
              padding=[("selected", (px(18), px(10))), ("!selected", (px(18), px(10)))])
    style.configure("Vertical.TScrollbar", background=c["control"], troughcolor=c["background"],
                    borderwidth=0, arrowsize=px(12))
    window.option_add("*TCombobox*Listbox.background", c["surface"])
    window.option_add("*TCombobox*Listbox.foreground", c["text"])
    window.option_add("*TCombobox*Listbox.selectBackground", c["accent"])
    window.option_add("*TCombobox*Listbox.selectForeground", c["accent_text"])
    window.option_add("*TCombobox*Listbox.font", (FONT, 10))
    window.option_add("*TCombobox.font", (FONT, 10))
    if os.name == "nt":
        window.after_idle(lambda: _dark_titlebar(window))
    return scale


def _dark_titlebar(window: tk.Tk) -> None:
    """Match the Windows caption when supported, keeping the native window controls."""
    import ctypes
    from ctypes import wintypes
    try:
        user = ctypes.WinDLL("user32", use_last_error=True)
        user.GetParent.argtypes = [wintypes.HWND]
        user.GetParent.restype = wintypes.HWND
        handle = user.GetParent(window.winfo_id()) or window.winfo_id()
        dwm = ctypes.WinDLL("dwmapi", use_last_error=True)
        dwm.DwmSetWindowAttribute.argtypes = [wintypes.HWND, wintypes.DWORD,
                                            ctypes.c_void_p, wintypes.DWORD]
        dwm.DwmSetWindowAttribute.restype = ctypes.c_long
        enabled = ctypes.c_int(1)
        dwm.DwmSetWindowAttribute(handle, 20, ctypes.byref(enabled), ctypes.sizeof(enabled))
    except (OSError, tk.TclError):
        pass  # Older Windows builds retain their normal system caption.


class ScrollPage(ttk.Frame):
    """Only the page scrolls; status and recovery controls stay accessible."""
    def __init__(self, parent, px):
        super().__init__(parent)
        self.canvas = tk.Canvas(self, background=COLORS["background"], highlightthickness=0,
                                borderwidth=0, yscrollincrement=px(24))
        self.scrollbar = ttk.Scrollbar(self, orient="vertical", command=self.canvas.yview)
        self.canvas.configure(yscrollcommand=self.scrollbar.set)
        self.canvas.grid(row=0, column=0, sticky="nsew")
        self.scrollbar.grid(row=0, column=1, sticky="ns")
        self.columnconfigure(0, weight=1)
        self.rowconfigure(0, weight=1)
        self.body = ttk.Frame(self.canvas, padding=(0, px(16), px(4), px(12)))
        self.item = self.canvas.create_window(0, 0, anchor="nw", window=self.body)
        self.body.bind("<Configure>", lambda e: self.canvas.configure(scrollregion=self.canvas.bbox("all")))
        self.canvas.bind("<Configure>", lambda e: self.canvas.itemconfigure(self.item, width=e.width))

    def scroll(self, event):
        if not self.winfo_ismapped() or isinstance(event.widget, ttk.Combobox):
            return
        widget = event.widget
        while widget is not None and widget is not self:
            widget = getattr(widget, "master", None)
        if widget is self:
            self.canvas.yview_scroll(-int(event.delta / 120), "units")


def build_ui(window) -> None:
    scale = apply_theme(window)
    px = lambda value: round(value * scale)
    window.title("RemoteVoice — звук телефона и ноутбука")
    # Use available work area conservatively; page scrolling covers high DPI/small screens.
    width = min(px(1080), window.winfo_screenwidth() - px(48))
    height = min(px(800), window.winfo_screenheight() - px(100))
    window.geometry(f"{width}x{height}")
    window.minsize(min(px(800), width), min(px(600), height))
    window.resizable(True, True)
    window.columnconfigure(0, weight=1)
    window.rowconfigure(0, weight=1)
    root = ttk.Frame(window, padding=px(24))
    root.grid(row=0, column=0, sticky="nsew")
    root.columnconfigure(0, weight=1)
    root.rowconfigure(2, weight=1)

    def label(parent, text=None, *, variable=None, style="CardMuted.TLabel", row=0,
              column=0, pady=0):
        widget = ttk.Label(parent, text=text, textvariable=variable, style=style, wraplength=px(400))
        widget.grid(row=row, column=column, sticky="ew", pady=pady)
        def wrap(event):
            widget.configure(wraplength=max(px(120), parent.winfo_width() - px(48)))
        parent.bind("<Configure>", wrap, add="+")
        return widget

    def card(parent, title, *, row=0):
        frame = ttk.Frame(parent, style="Card.TFrame", padding=px(20))
        frame.grid(row=row, column=0, sticky="new", pady=(0, px(16)))
        frame.columnconfigure(0, weight=1)
        label(frame, title, style="CardTitle.TLabel")
        return frame

    header = ttk.Frame(root)
    header.grid(row=0, column=0, sticky="ew", pady=(0, px(20)))
    header.columnconfigure(0, weight=1)
    label(header, "ТЕЛЕФОН И НОУТБУК · ОДНО ПОДКЛЮЧЕНИЕ", style="Eyebrow.TLabel")
    label(header, "RemoteVoice", style="Title.TLabel", row=1, pady=(px(4), 0))
    label(header, "Говорите с телефона. Слушайте ноутбук в наушниках.",
          style="Muted.TLabel", row=2, pady=(px(4), 0))

    status = ttk.Frame(root, style="Card.TFrame", padding=(px(20), px(14)))
    status.grid(row=1, column=0, sticky="ew", pady=(0, px(16)))
    status.columnconfigure(0, weight=1)
    label(status, "СОСТОЯНИЕ СЕАНСА", style="CardMuted.TLabel")
    window.status_label = label(status, variable=window.status_var, style="Status.TLabel", row=1,
                                pady=(px(4), 0))
    def update_status_style(*_):
        active = (getattr(window, "_output_route_active", False)
                  and getattr(window, "server_running", False) and not getattr(window, "_stopping", False))
        window.status_label.configure(style="Live.Status.TLabel" if active else "Status.TLabel")
    window.status_var.trace_add("write", update_status_style)

    window.notebook = ttk.Notebook(root, takefocus=True)
    window.notebook.enable_traversal()
    window.notebook.grid(row=2, column=0, sticky="nsew")
    connection = ScrollPage(window.notebook, px)
    audio = ScrollPage(window.notebook, px)
    window.notebook.add(connection, text="Подключение")
    window.notebook.add(audio, text="Аудиоустройства")
    window._desktop_pages = (connection, audio)
    # Toplevel bindings disappear with this window; no global bind_all handlers.
    window.bind("<MouseWheel>", connection.scroll, add="+")
    window.bind("<MouseWheel>", audio.scroll, add="+")
    connection.body.columnconfigure(0, weight=3)
    connection.body.columnconfigure(1, weight=2)
    left = ttk.Frame(connection.body)
    right = ttk.Frame(connection.body)
    left.grid(row=0, column=0, sticky="new", padx=(0, px(16)))
    right.grid(row=0, column=1, sticky="new")
    left.columnconfigure(0, weight=1)
    right.columnconfigure(0, weight=1)

    pairing = card(left, "Подключить телефон")
    label(pairing, "Откройте RemoteVoice на телефоне и отсканируйте QR-код.",
          row=1, pady=(px(8), px(16)))
    pair_content = ttk.Frame(pairing, style="CardBody.TFrame")
    pair_content.grid(row=2, column=0, sticky="ew", pady=(0, px(12)))
    pair_content.columnconfigure(1, weight=1)
    window.qr_label = ttk.Label(pair_content, text="Нажмите «Начать подключение», чтобы показать QR-код",
                                style="CardMuted.TLabel", anchor="center", justify="center",
                                wraplength=px(190), padding=px(8))
    window.qr_label.grid(row=0, column=0, sticky="nw", padx=(0, px(16)))
    code = ttk.Frame(pair_content, style="CardBody.TFrame")
    code.grid(row=0, column=1, sticky="nw", pady=px(8))
    code.columnconfigure(0, weight=1)
    label(code, "КОД СОПРЯЖЕНИЯ", row=0)
    ttk.Label(code, textvariable=window.pairing_code_var, style="Code.TLabel").grid(
        row=1, column=0, sticky="w", pady=(px(4), px(12)))
    window.new_pair_button = ttk.Button(code, text="Новое сопряжение", command=window._begin_pairing)
    window.new_pair_button.grid(row=2, column=0, sticky="w")
    label(pairing, "Микрофон включается отдельно на телефоне.", row=3, pady=(px(4), 0))

    network = card(right, "Локальная сеть")
    label(network, "Телефон и ноутбук должны быть в одной локальной сети.", row=1,
          pady=(px(8), px(12)))
    label(network, "Адрес ноутбука", style="Card.TLabel", row=2, pady=(0, px(8)))
    window.ip_combo = ttk.Combobox(network, textvariable=window.ip_var, state="readonly", width=1)
    window.ip_combo.grid(row=3, column=0, sticky="ew")

    screen = card(right, "Экран ноутбука", row=1)
    window.screen_check = ttk.Checkbutton(screen, text="Разрешить просмотр экрана",
        variable=window.screen_allowed_var, command=window._update_screen_permission)
    window.screen_check.grid(row=1, column=0, sticky="w", pady=(px(8), 0))
    screen_indicator = label(screen, variable=window.screen_status_var, row=2, pady=(px(4), 0))
    def update_preview_style(*_):
        screen_indicator.configure(style="Live.CardMuted.TLabel"
                                   if window.screen_status_var.get().startswith("●") else "CardMuted.TLabel")
    window.screen_status_var.trace_add("write", update_preview_style)
    label(screen, "Просмотр запускается с телефона. Разрешение действует только в этом окне.",
          row=3, pady=(px(12), 0))

    def reflow(event):
        narrow = event.width < px(880)
        left.grid_configure(padx=0 if narrow else (0, px(16)))
        right.grid_configure(row=1 if narrow else 0, column=0 if narrow else 1)
        connection.body.columnconfigure(0, uniform="" if narrow else "connection")
        connection.body.columnconfigure(1, uniform="" if narrow else "connection")
        connection.body.columnconfigure(1, weight=0 if narrow else 2)
    connection.body.bind("<Configure>", reflow, add="+")

    audio.body.columnconfigure(0, weight=1)
    setup = card(audio.body, "Звук между устройствами")
    label(setup, "Выберите два разных виртуальных канала: звук ноутбука и голос телефона.",
          row=1, pady=(px(8), px(16)))
    for row, title, variable, name in [
        (2, "Звук ноутбука → телефон · захват", window.capture_var, "capture_combo"),
        (4, "Голос телефона → ноутбук · вывод", window.playback_var, "playback_combo"),
        (6, "Системный вывод Windows → телефон", window.system_output_var, "system_output_combo"),
    ]:
        label(setup, title, style="Card.TLabel", row=row, pady=(0, px(6)))
        combo = ttk.Combobox(setup, textvariable=variable, state="readonly", width=1)
        combo.grid(row=row + 1, column=0, sticky="ew", pady=(0, px(16)))
        setattr(window, name, combo)
    window.capture_combo.bind("<<ComboboxSelected>>", window._suggest_system_output)
    label(setup, variable=window.endpoint_hint_var, row=8)
    fallback = card(audio.body, "Возврат на ноутбук", row=1)
    label(fallback, "Эти устройства будут восстановлены после завершения сеанса.",
          row=1, pady=(px(8), px(16)))
    for row, title, variable, name in [
        (2, "Динамики или наушники ноутбука", window.fallback_output_var, "fallback_output_combo"),
        (4, "Микрофон ноутбука", window.local_microphone_var, "local_microphone_combo"),
    ]:
        label(fallback, title, style="Card.TLabel", row=row, pady=(0, px(6)))
        combo = ttk.Combobox(fallback, textvariable=variable, state="readonly", width=1)
        combo.grid(row=row + 1, column=0, sticky="ew", pady=(0, px(12)))
        setattr(window, name, combo)
    window.mute_local_microphone_check = ttk.Checkbutton(fallback,
        text="Выключать микрофон ноутбука при подключении",
        variable=window.mute_local_microphone_var, command=window._update_microphone_controls)
    window.mute_local_microphone_check.grid(row=6, column=0, sticky="w")
    window._update_microphone_controls()

    footer = ttk.Frame(root)
    footer.grid(row=3, column=0, sticky="ew", pady=(px(16), 0))
    footer.columnconfigure(0, weight=1)
    window.return_button = ttk.Button(footer, text="Вернуть звук и микрофон",
                                      command=window._return_to_laptop)
    window.return_button.grid(row=0, column=0, sticky="w", padx=(0, px(12)))
    window.stop_button = ttk.Button(footer, text="Остановить", command=window._stop_host, state="disabled")
    window.stop_button.grid(row=0, column=1, padx=(0, px(8)))
    window.start_button = ttk.Button(footer, text="Начать подключение", style="Primary.TButton",
                                     command=window._start_host)
    window.start_button.grid(row=0, column=2)
