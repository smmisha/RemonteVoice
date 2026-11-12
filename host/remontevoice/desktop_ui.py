"""Shared desktop theme and layout; audio/session behavior stays in HostWindow."""
from __future__ import annotations

from collections import Counter
import os
import tkinter as tk
from tkinter import ttk


from . import design_tokens as tokens
from .desktop_widgets import RoundedButton, RoundedCard, RoundedCheck, RoundedSelect

# Palette, spacing grid, radii and type scale are shared with the Android client
# (REQ-039): see design_tokens.py. Colors were already validated against the
# phone reference screenshot; shapes and typography come from the same module.
COLORS = tokens.PALETTE
FONT = tokens.FONT_FAMILY


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
    scale = tokens.scaling(window)
    px = lambda value: round(value * scale)
    c = COLORS

    def type_font(key, weight="normal"):
        return tokens.font(window, key, weight=weight)

    window.configure(background=c["background"])
    style = ttk.Style(window)
    style.theme_use("clam")
    style.configure(".", background=c["background"], foreground=c["text"],
                    font=type_font("body"), bordercolor=c["border"], lightcolor=c["border"],
                    darkcolor=c["border"], focuscolor=c["accent"])
    style.configure("TFrame", background=c["background"])
    style.configure("Card.TFrame", background=c["surface"], relief="solid", borderwidth=1)
    style.configure("CardBody.TFrame", background=c["surface"])
    for name, bg, fg, font in [
        ("TLabel", c["background"], c["text"], type_font("body")),
        ("Title.TLabel", c["background"], c["text"], type_font("title", "bold")),
        ("Eyebrow.TLabel", c["background"], c["accent"], type_font("eyebrow", "bold")),
        ("Muted.TLabel", c["background"], c["muted"], type_font("body")),
        ("Card.TLabel", c["surface"], c["text"], type_font("body")),
        ("CardTitle.TLabel", c["surface"], c["text"], type_font("card_title", "bold")),
        ("CardMuted.TLabel", c["surface"], c["muted"], type_font("note")),
        ("Status.TLabel", c["surface"], c["text"], type_font("status", "bold")),
        ("Live.Status.TLabel", c["surface"], c["accent"], type_font("status", "bold")),
        ("Live.CardMuted.TLabel", c["surface"], c["accent"], type_font("note")),
        ("Code.TLabel", c["surface"], c["accent"], type_font("code", "bold")),
    ]:
        style.configure(name, background=bg, foreground=fg, font=font)
    style.configure("TCombobox", fieldbackground=c["background"], background=c["control"],
                    foreground=c["text"], arrowcolor=c["accent"], padding=px(8),
                    arrowsize=px(16), borderwidth=1, insertcolor=c["text"],
                    font=type_font("body"), relief="flat",
                    lightcolor=c["border"], darkcolor=c["border"], bordercolor=c["border"])
    style.map("TCombobox", fieldbackground=[("disabled", c["disabled"]),
                                           ("readonly", c["background"])],
              foreground=[("disabled", c["disabled_text"]), ("readonly", c["text"])],
              selectbackground=[("readonly", c["control"])],
              selectforeground=[("readonly", c["text"])],
              bordercolor=[("focus", c["accent"])])
    style.configure("TCheckbutton", background=c["surface"], foreground=c["text"],
                    padding=(0, px(6)), indicatorbackground=c["background"],
                    indicatorforeground=c["accent_text"], indicatorsize=px(16),
                    font=type_font("body"), lightcolor=c["border"], darkcolor=c["border"],
                    bordercolor=c["border"])
    style.map("TCheckbutton", background=[("active", c["surface"])],
              foreground=[("disabled", c["disabled_text"])],
              indicatorbackground=[("disabled", c["disabled"]), ("selected", c["accent"])],
              indicatorforeground=[("disabled", c["disabled_text"])])
    style.configure("TNotebook", background=c["background"], borderwidth=0, tabmargins=0,
                    lightcolor=c["background"], darkcolor=c["background"],
                    bordercolor=c["background"])
    style.configure("TNotebook.Tab", background=c["background"], foreground=c["muted"],
                    padding=(px(18), px(10)), font=type_font("body", "bold"),
                    bordercolor=c["border"], lightcolor=c["border"], darkcolor=c["border"],
                    focuscolor=c["border"])
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
    window.option_add("*TCombobox*Listbox.font", type_font("body"))
    window.option_add("*TCombobox.font", type_font("body"))
    if os.name == "nt":
        window.after(150, lambda: _dark_titlebar(window))
        window.bind("<Map>", lambda _event: _dark_titlebar(window), add="+")
    return scale


def _dark_titlebar(window: tk.Tk) -> None:
    """Match the Windows caption when supported, keeping the native window controls."""
    import ctypes
    from ctypes import wintypes
    try:
        user = ctypes.WinDLL("user32", use_last_error=True)
        user.GetParent.argtypes = [wintypes.HWND]
        user.GetParent.restype = wintypes.HWND
        user.SetWindowPos.argtypes = [wintypes.HWND, wintypes.HWND, ctypes.c_int,
                                      ctypes.c_int, ctypes.c_int, ctypes.c_int,
                                      wintypes.UINT]
        user.SetWindowPos.restype = wintypes.BOOL
        handle = user.GetParent(window.winfo_id()) or window.winfo_id()
        dwm = ctypes.WinDLL("dwmapi", use_last_error=True)
        dwm.DwmSetWindowAttribute.argtypes = [wintypes.HWND, wintypes.DWORD,
                                            ctypes.c_void_p, wintypes.DWORD]
        dwm.DwmSetWindowAttribute.restype = ctypes.c_long
        enabled = ctypes.c_int(1)
        # 20 = DWMWA_USE_IMMERSIVE_DARK_MODE (1903+), 19 = its pre-1903 value.
        for attribute in (20, 19):
            if dwm.DwmSetWindowAttribute(handle, attribute, ctypes.byref(enabled),
                                         ctypes.sizeof(enabled)) == 0:
                break
        # Windows 11: paint the caption and frame with the app background so the
        # native chrome reads as part of the interface (COLORREF is 0x00BBGGRR).
        background = tokens.PALETTE["background"]
        color_ref = (int(background[1:3], 16) |
                     int(background[3:5], 16) << 8 |
                     int(background[5:7], 16) << 16)
        for attribute in (35, 34):  # DWMWA_CAPTION_COLOR, DWMWA_BORDER_COLOR
            value = ctypes.c_int(color_ref)
            dwm.DwmSetWindowAttribute(handle, attribute, ctypes.byref(value),
                                      ctypes.sizeof(value))
        # Repaint the non-client area so the caption picks the attribute up.
        user.SetWindowPos(handle, None, 0, 0, 0, 0, 0x0020 | 0x0002 | 0x0001)
    except (OSError, tk.TclError, AttributeError):
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
    window.title("RemonteVoice — звук телефона и ноутбука")
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
        frame = RoundedCard(parent)
        frame.grid(row=row, column=0, sticky="new", pady=(0, px(tokens.SPACING["l"])))
        frame.body.columnconfigure(0, weight=1)
        label(frame.body, title, style="CardTitle.TLabel")
        return frame.body

    header = ttk.Frame(root)
    header.grid(row=0, column=0, sticky="ew", pady=(0, px(20)))
    header.columnconfigure(0, weight=1)
    label(header, "ТЕЛЕФОН И НОУТБУК · ОДНО ПОДКЛЮЧЕНИЕ", style="Eyebrow.TLabel")
    label(header, "RemonteVoice", style="Title.TLabel", row=1, pady=(px(4), 0))
    label(header, "Говорите с телефона. Слушайте ноутбук в наушниках.",
          style="Muted.TLabel", row=2, pady=(px(4), 0))

    def ui_font(key, weight="normal"):
        return tokens.font(window, key, weight=weight)

    status = RoundedCard(root)
    status.grid(row=1, column=0, sticky="ew", pady=(0, px(16)))
    status.body.columnconfigure(0, weight=1)
    caption = tk.Frame(status.body, bg=COLORS["surface"])
    caption.grid(row=0, column=0, sticky="w")
    window.status_dot = tk.Label(caption, text="●", bg=COLORS["surface"], fg=COLORS["muted"],
                                 font=ui_font("eyebrow"), bd=0)
    window.status_dot.pack(side="left", padx=(0, px(6)))
    window.status_caption = tk.Label(caption, text="СЕАНС НЕ АКТИВЕН", bg=COLORS["surface"],
                                     fg=COLORS["muted"], font=ui_font("eyebrow", "bold"), bd=0)
    window.status_caption.pack(side="left")
    window.status_label = label(status.body, variable=window.status_var, style="Status.TLabel",
                                row=1, pady=(px(6), 0))
    window.status_detail = tk.Label(status.body, text="Ноутбук · " + window.ip_var.get(),
                                    bg=COLORS["surface"], fg=COLORS["muted"], anchor="w", bd=0,
                                    font=ui_font("note"))
    window.status_detail.grid(row=2, column=0, sticky="w", pady=(px(6), 0))
    window.ip_var.trace_add("write", lambda *_:
                            window.status_detail.configure(text="Ноутбук · " + window.ip_var.get()))

    def update_status_style(*_):
        active = (getattr(window, "_output_route_active", False)
                  and getattr(window, "server_running", False) and not getattr(window, "_stopping", False))
        window.status_label.configure(style="Live.Status.TLabel" if active else "Status.TLabel")
        color = COLORS["accent"] if active else COLORS["muted"]
        window.status_dot.configure(fg=color)
        window.status_caption.configure(text="СЕАНС АКТИВЕН" if active else "СЕАНС НЕ АКТИВЕН",
                                       fg=color)
    window.status_var.trace_add("write", update_status_style)
    update_status_style()

    content = ttk.Frame(root)
    content.grid(row=2, column=0, sticky="nsew")
    content.columnconfigure(0, weight=1)
    content.rowconfigure(1, weight=1)

    chip_bar = tk.Frame(content, bg=COLORS["background"])
    chip_bar.grid(row=0, column=0, sticky="w", pady=(0, px(12)))
    pages = ttk.Frame(content)
    pages.grid(row=1, column=0, sticky="nsew")
    pages.columnconfigure(0, weight=1)
    pages.rowconfigure(0, weight=1)
    connection = ScrollPage(pages, px)
    audio = ScrollPage(pages, px)
    for page in (connection, audio):
        page.grid(row=0, column=0, sticky="nsew")
    window._desktop_pages = (connection, audio)

    def select_page(index):
        for position, page in enumerate(window._desktop_pages):
            if position == index:
                page.grid()
            else:
                page.grid_remove()
        for position, chip in enumerate(window._desktop_chips):
            chip.configure(active=position == index)

    window._select_desktop_page = select_page
    chip_connection = RoundedButton(chip_bar, "Подключение", lambda: select_page(0),
                                    variant="chip", active=True)
    chip_connection.pack(side="left", padx=(0, px(8)))
    chip_audio = RoundedButton(chip_bar, "Аудиоустройства", lambda: select_page(1), variant="chip")
    chip_audio.pack(side="left")
    window._desktop_chips = (chip_connection, chip_audio)
    select_page(0)
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
    label(pairing, "Откройте RemonteVoice на телефоне и отсканируйте QR-код.",
          row=1, pady=(px(8), px(16)))
    pair_content = ttk.Frame(pairing, style="CardBody.TFrame")
    pair_content.grid(row=2, column=0, sticky="ew", pady=(0, px(12)))
    pair_content.columnconfigure(1, weight=1)
    qr_holder = RoundedCard(pair_content, radius=12, padding=8)
    qr_holder.grid(row=0, column=0, sticky="nw", padx=(0, px(16)))
    qr_holder.body.grid_propagate(False)
    qr_holder.body.configure(width=px(208), height=px(208))
    qr_holder.body.columnconfigure(0, weight=1)
    qr_holder.body.rowconfigure(0, weight=1)
    window.qr_label = ttk.Label(qr_holder.body, text="Нажмите «Начать подключение», чтобы показать QR-код",
                                style="CardMuted.TLabel", anchor="center", justify="center",
                                wraplength=px(170), padding=px(8))
    window.qr_label.grid(row=0, column=0, sticky="nsew")
    code = ttk.Frame(pair_content, style="CardBody.TFrame")
    code.grid(row=0, column=1, sticky="nw", pady=px(8))
    code.columnconfigure(0, weight=1)
    label(code, "КОД СОПРЯЖЕНИЯ", row=0)
    ttk.Label(code, textvariable=window.pairing_code_var, style="Code.TLabel").grid(
        row=1, column=0, sticky="w", pady=(px(4), px(12)))
    window.new_pair_button = RoundedButton(code, "Новое сопряжение", window._begin_pairing)
    window.new_pair_button.grid(row=2, column=0, sticky="w")
    label(pairing, "Микрофон включается отдельно на телефоне.", row=3, pady=(px(4), 0))

    network = card(right, "Локальная сеть")
    label(network, "Телефон и ноутбук должны быть в одной локальной сети.", row=1,
          pady=(px(8), px(12)))
    label(network, "Адрес ноутбука", style="Card.TLabel", row=2, pady=(0, px(8)))
    window.ip_combo = RoundedSelect(network, textvariable=window.ip_var, state="readonly")
    window.ip_combo.grid(row=3, column=0, sticky="ew")
    label(network, "Другая сеть? Включите на ноутбуке мобильный хотспот и подключите телефон "
                    "к нему — адрес хотспота появится в списке.", row=4, pady=(px(10), 0))

    screen = card(right, "Экран ноутбука", row=1)
    window.screen_check = RoundedCheck(screen, text="Разрешить просмотр экрана",
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

    def routing_summary(parent, title, rows, *, row=0):
        """Compact read-only routing summary; the device lists live behind a disclosure."""
        card_body = card(parent, title, row=row)
        for offset, (row_title, variable, name) in enumerate(rows):
            tk.Label(card_body, text=row_title, bg=COLORS["surface"], fg=COLORS["muted"],
                     anchor="w", font=ui_font("note")).grid(
                row=1 + offset * 2, column=0, sticky="w", pady=(px(8), 0))
            value = tk.Label(card_body, text=variable.get() or "—", bg=COLORS["surface"],
                             fg=COLORS["text"], anchor="w", justify="left", wraplength=px(560),
                             font=ui_font("body"))
            value.grid(row=2 + offset * 2, column=0, sticky="w")
            variable.trace_add("write", lambda *_args, label=value, var=variable:
                               label.configure(text=var.get() or "—"))
            setattr(window, name, value)
        return card_body

    routing = routing_summary(audio.body, "Звук между устройствами", [
        ("Звук ноутбука → телефон", window.capture_var, "capture_summary"),
        ("Голос телефона → ноутбук", window.playback_var, "playback_summary"),
        ("Системный вывод Windows → телефон", window.system_output_var, "system_output_summary"),
    ])
    label(routing, "Приложение выбирает эти устройства само — обычно менять их не нужно.",
          row=7, pady=(px(12), 0))
    routing_summary(audio.body, "Возврат на ноутбук", [
        ("Динамики или наушники ноутбука", window.fallback_output_var, "fallback_output_summary"),
        ("Микрофон ноутбука", window.local_microphone_var, "local_microphone_summary"),
    ], row=1)

    voicemeeter = card(audio.body, "Voicemeeter Banana", row=2)
    label(voicemeeter, "Нужен только как аудиодвижок: окно держать открытым не обязательно.",
          row=1, pady=(px(8), px(10)))
    window.voicemeeter_status = label(voicemeeter, variable=window.voicemeeter_status_var,
                                      row=2, pady=(0, px(10)))
    window.keep_voicemeeter_hidden_check = RoundedCheck(
        voicemeeter, text="Скрывать окно Voicemeeter",
        variable=window.keep_voicemeeter_hidden_var,
        command=window._toggle_voicemeeter_window)
    window.keep_voicemeeter_hidden_check.grid(row=3, column=0, sticky="w", pady=(0, px(10)))
    window.show_voicemeeter_button = RoundedButton(voicemeeter, "Показать Voicemeeter",
                                                   window._show_voicemeeter)
    window.show_voicemeeter_button.grid(row=4, column=0, sticky="w")

    detail = ttk.Frame(audio.body)
    detail.grid(row=3, column=0, sticky="ew")
    detail.columnconfigure(0, weight=1)
    detail.grid_remove()

    def toggle_routing_advanced():
        advanced = not bool(getattr(window, "_routing_advanced", False))
        window._routing_advanced = advanced
        if advanced:
            detail.grid()
            window.routing_advanced_button.configure(text="Скрыть настройки")
        else:
            detail.grid_remove()
            window.routing_advanced_button.configure(text="Настроить вручную")

    window._toggle_routing_advanced = toggle_routing_advanced
    window.routing_advanced_button = RoundedButton(routing, "Настроить вручную",
                                                   toggle_routing_advanced)
    window.routing_advanced_button.grid(row=8, column=0, sticky="w", pady=(px(16), 0))

    setup = card(detail, "Маршрутизация: выбор устройств")
    label(setup, "Выберите два разных виртуальных канала: звук ноутбука и голос телефона.",
          row=1, pady=(px(8), px(16)))
    for row, title, variable, name in [
        (2, "Звук ноутбука → телефон · захват", window.capture_var, "capture_combo"),
        (4, "Голос телефона → ноутбук · вывод", window.playback_var, "playback_combo"),
        (6, "Системный вывод Windows → телефон", window.system_output_var, "system_output_combo"),
    ]:
        label(setup, title, style="Card.TLabel", row=row, pady=(0, px(6)))
        combo = RoundedSelect(setup, textvariable=variable, state="readonly")
        combo.grid(row=row + 1, column=0, sticky="ew", pady=(0, px(16)))
        setattr(window, name, combo)
    window.capture_combo.bind("<<ComboboxSelected>>", window._suggest_system_output)
    label(setup, variable=window.endpoint_hint_var, row=8)

    fallback = card(detail, "Возврат на ноутбук: выбор устройств", row=1)
    label(fallback, "Эти устройства будут восстановлены после завершения сеанса.",
          row=1, pady=(px(8), px(16)))
    for row, title, variable, name in [
        (2, "Динамики или наушники ноутбука", window.fallback_output_var, "fallback_output_combo"),
        (4, "Микрофон ноутбука", window.local_microphone_var, "local_microphone_combo"),
    ]:
        label(fallback, title, style="Card.TLabel", row=row, pady=(0, px(6)))
        combo = RoundedSelect(fallback, textvariable=variable, state="readonly")
        combo.grid(row=row + 1, column=0, sticky="ew", pady=(0, px(12)))
        setattr(window, name, combo)
    window.mute_local_microphone_check = RoundedCheck(fallback,
        text="Выключать микрофон ноутбука при подключении",
        variable=window.mute_local_microphone_var, command=window._update_microphone_controls)
    window.mute_local_microphone_check.grid(row=6, column=0, sticky="w")
    window._update_microphone_controls()

    footer = ttk.Frame(root)
    footer.grid(row=3, column=0, sticky="ew", pady=(px(16), 0))
    footer.columnconfigure(0, weight=1)
    window.return_button = RoundedButton(footer, "Вернуть звук и микрофон", window._return_to_laptop)
    window.return_button.grid(row=0, column=0, sticky="w", padx=(0, px(12)))
    window.stop_button = RoundedButton(footer, "Остановить", window._stop_host, state="disabled")
    window.stop_button.grid(row=0, column=1, padx=(0, px(8)))
    window.start_button = RoundedButton(footer, "Начать подключение", window._start_host, primary=True)
    window.start_button.grid(row=0, column=2)
