"""Rounded card and button primitives for the desktop UI (REQ-039).

``ttk`` on Windows cannot draw rounded corners, so the Android card/button shape
is painted as an anti-aliased image behind ordinary Tk children. Layout, theme
colors, keyboard focus, disabled state and DPI scaling keep working exactly like
the rest of the UI; only the shape and the control metrics come from
``design_tokens``.
"""

from __future__ import annotations

import tkinter as tk
from tkinter import font as tkfont

from PIL import Image, ImageDraw, ImageTk

from . import design_tokens as tokens

# Draw the shape oversized and downscale it: PIL's rounded_rectangle is aliased.
_SUPERSAMPLE = 4


def rounded_image(width: int, height: int, radius: int, fill: str | None,
                  outline: str | None = None, border: int = 0) -> Image.Image:
    """Anti-aliased rounded rectangle with transparent corners."""
    w = max(1, int(width))
    h = max(1, int(height))
    sw, sh = w * _SUPERSAMPLE, h * _SUPERSAMPLE
    image = Image.new("RGBA", (sw, sh), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    r = max(0, min(int(radius) * _SUPERSAMPLE, sw // 2, sh // 2))
    width_px = max(0, int(border) * _SUPERSAMPLE)
    if fill and outline and width_px > 0:
        draw.rounded_rectangle((0, 0, sw - 1, sh - 1), radius=r, fill=fill,
                               outline=outline, width=width_px)
    elif fill:
        draw.rounded_rectangle((0, 0, sw - 1, sh - 1), radius=r, fill=fill)
    elif outline:
        draw.rounded_rectangle((0, 0, sw - 1, sh - 1), radius=r, outline=outline,
                               width=max(1, width_px or _SUPERSAMPLE))
    return image.resize((w, h), Image.LANCZOS)


def focus_ring_image(width: int, height: int, radius: int, fill: str, ring: str,
                     ring_width: int, inset: int) -> Image.Image:
    """Rounded fill with an outer focus ring separated by a transparent gap."""
    w = max(1, int(width))
    h = max(1, int(height))
    sw, sh = w * _SUPERSAMPLE, h * _SUPERSAMPLE
    image = Image.new("RGBA", (sw, sh), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    draw.rounded_rectangle((0, 0, sw - 1, sh - 1), radius=radius * _SUPERSAMPLE,
                           outline=ring, width=max(1, ring_width * _SUPERSAMPLE))
    inner = (inset * _SUPERSAMPLE, inset * _SUPERSAMPLE,
             (w - inset) * _SUPERSAMPLE - 1, (h - inset) * _SUPERSAMPLE - 1)
    if inner[2] > inner[0] and inner[3] > inner[1]:
        draw.rounded_rectangle(inner, radius=max(0, (radius - inset) * _SUPERSAMPLE),
                               fill=fill)
    return image.resize((w, h), Image.LANCZOS)


def parent_background(widget) -> str:
    """Nearest ancestor background color, so transparent card corners blend."""
    node = widget
    while node is not None:
        try:
            color = node.cget("background")
        except tk.TclError:
            color = ""
        if color:
            return color
        node = getattr(node, "master", None)
    return tokens.PALETTE["background"]


class RoundedCard(tk.Frame):
    """A card surface with the Android radius; put children into ``self.body``.

    The frame itself is transparent (its background equals the parent's), the
    canvas paints the rounded surface and the body frame holds the content.
    """

    def __init__(self, master, **kwargs):
        self.fill = kwargs.pop("fill", tokens.PALETTE["surface"])
        self.border = kwargs.pop("border", tokens.PALETTE["border"])
        self.radius = kwargs.pop("radius", tokens.RADIUS["card"])
        padding = kwargs.pop("padding", tokens.CONTROL["card_padding"])
        background = kwargs.pop("background", None) or parent_background(master)
        super().__init__(master, bg=background, bd=0, highlightthickness=0, **kwargs)
        self._background = background
        self._canvas = tk.Canvas(self, bg=background, bd=0, highlightthickness=0,
                                 takefocus=0)
        self._canvas.place(x=0, y=0, relwidth=1, relheight=1)
        pad = tokens.dp(self, padding)
        self.body = tk.Frame(self, bg=self.fill)
        self.body.grid(row=0, column=0, sticky="nsew", padx=pad, pady=pad)
        self.rowconfigure(0, weight=1)
        self.columnconfigure(0, weight=1)
        self._image = None
        self._size = None
        self._canvas.bind("<Configure>", self._redraw)

    def _redraw(self, _event=None) -> None:
        width = self._canvas.winfo_width()
        height = self._canvas.winfo_height()
        if width < 2 or height < 2 or (width, height) == self._size:
            return
        self._size = (width, height)
        image = rounded_image(width, height, tokens.dp(self, self.radius), self.fill,
                              self.border,
                              max(1, tokens.dp(self, tokens.CONTROL["border_width"])))
        self._image = ImageTk.PhotoImage(image, master=self._canvas)
        self._canvas.delete("card")
        self._canvas.create_image(0, 0, anchor="nw", image=self._image, tags="card")


class RoundedButton(tk.Canvas):
    """Button with the Android shape and the full interactive state set.

    Supports ``configure(state=...)``/``configure(text=...)`` like the ttk
    buttons it replaces, plus hover, press, keyboard focus and activation.
    """

    def __init__(self, master, text: str = "", command=None, *, primary: bool = False,
                 state: str = "normal", variant: str | None = None, active: bool = False,
                 **kwargs):
        background = parent_background(master)
        kwargs.setdefault("height", tokens.dp(master, tokens.CONTROL["button_min_height"]))
        super().__init__(master, bd=0, highlightthickness=0, bg=background,
                         takefocus=1, **kwargs)
        self._text = str(text)
        self._command = command
        self._primary = primary
        self._variant = variant or ("primary" if primary else "secondary")
        self._active = bool(active)
        self._state = state
        self._hover = False
        self._pressed = False
        self._focused = False
        self._image = None
        self._font = None
        self.configure(width=self._preferred_width())
        self.bind("<Configure>", self._paint)
        self.bind("<Enter>", self._enter)
        self.bind("<Leave>", self._leave)
        self.bind("<ButtonPress-1>", self._press)
        self.bind("<ButtonRelease-1>", self._release)
        self.bind("<FocusIn>", self._focus_in)
        self.bind("<FocusOut>", self._focus_out)
        self.bind("<space>", self._keyboard)
        self.bind("<Return>", self._keyboard)

    # -- geometry ------------------------------------------------------
    def _button_font(self):
        if self._font is None:
            self._font = tkfont.Font(
                root=self, family=tokens.FONT_FAMILY,
                size=-tokens.font_px(self, "body" if self._variant == "chip" else "button"),
                weight="bold")
        return self._font

    def _preferred_width(self) -> int:
        horizontal = tokens.SPACING["m"] if self._variant == "chip" else tokens.SPACING["xl"]
        return self._button_font().measure(self._text) + 2 * tokens.dp(self, horizontal)

    # -- public API (ttk-compatible) -----------------------------------
    def configure(self, cnf=None, **kw):
        if "state" in kw:
            self._state = str(kw.pop("state"))
        if "text" in kw:
            self._text = str(kw.pop("text"))
            super().configure(width=self._preferred_width())
        if "command" in kw:
            self._command = kw.pop("command")
        if "primary" in kw:
            self._primary = bool(kw.pop("primary"))
        if "variant" in kw:
            self._variant = str(kw.pop("variant"))
            self._font = None
            super().configure(width=self._preferred_width())
        if "active" in kw:
            self._active = bool(kw.pop("active"))
        if kw:
            super().configure(cnf, **kw)
        if self._image is not None or self.winfo_ismapped():
            self._paint()

    config = configure

    def cget(self, key):
        if key == "state":
            return self._state
        if key == "text":
            return self._text
        return super().cget(key)

    def invoke(self) -> None:
        if self._state != "disabled" and self._command is not None:
            self._command()

    # -- interaction ---------------------------------------------------
    def _state_color(self) -> tuple[str, str]:
        colors = tokens.PALETTE
        if self._state == "disabled":
            return colors["disabled"], colors["disabled_text"]
        if self._variant == "chip":
            if self._active:
                return colors["surface"], colors["accent"]
            fill = colors["control"] if self._hover else colors["background"]
            return fill, colors["text"] if self._hover else colors["muted"]
        if self._variant == "primary":
            fill = (colors["accent_pressed"] if self._pressed
                    else colors["accent_hover"] if self._hover else colors["accent"])
            return fill, colors["accent_text"]
        fill = (colors["border"] if self._pressed
                else colors["hover"] if self._hover else colors["control"])
        return fill, colors["text"]

    def _enter(self, _event=None):
        self._hover = True
        self.configure(cursor="hand2" if self._state != "disabled" else "")
        self._paint()

    def _leave(self, _event=None):
        self._hover = False
        self._pressed = False
        self._paint()

    def _press(self, _event=None):
        if self._state == "disabled":
            return
        self._pressed = True
        self.focus_set()
        self._paint()

    def _release(self, _event=None):
        was_pressed = self._pressed
        self._pressed = False
        inside = 0 <= self.winfo_pointerx() - self.winfo_rootx() <= self.winfo_width()
        self._paint()
        if was_pressed and inside:
            self.invoke()

    def _focus_in(self, _event=None):
        self._focused = True
        self._paint()

    def _focus_out(self, _event=None):
        self._focused = False
        self._paint()

    def _keyboard(self, _event=None):
        self.invoke()
        return "break"

    # -- painting ------------------------------------------------------
    def _paint(self, _event=None) -> None:
        width = self.winfo_width()
        height = self.winfo_height()
        if width <= 1 or height <= 1:
            return
        fill, foreground = self._state_color()
        radius = tokens.dp(self, tokens.RADIUS["chip"] if self._variant == "chip"
                           else tokens.RADIUS["control"])
        if self._focused and self._state != "disabled":
            ring = (tokens.PALETTE["text"] if self._variant == "primary"
                    else tokens.PALETTE["accent"])
            ring_width = max(2, tokens.dp(self, tokens.CONTROL["focus_ring"]))
            gap = max(1, tokens.dp(self, tokens.CONTROL["border_width"]) + 1)
            image = focus_ring_image(width, height, radius, fill, ring, ring_width,
                                     ring_width + gap)
        else:
            outline, border = None, 0
            if self._variant == "chip" and not self._active:
                outline = tokens.PALETTE["border"]
                border = max(1, tokens.dp(self, tokens.CONTROL["border_width"]))
            image = rounded_image(width, height, radius, fill, outline, border)
        self._image = ImageTk.PhotoImage(image, master=self)
        self.delete("all")
        self.create_image(0, 0, anchor="nw", image=self._image)
        self.create_text(width // 2, height // 2, text=self._text, fill=foreground,
                         font=self._button_font())


class RoundedSelect(tk.Frame):
    """Read-only device picker with the Android field shape (ttk.Combobox replacement).

    Keeps the small API the host relies on: ``widget["values"] = [...]``,
    ``configure(state=...)``, ``bind("<<ComboboxSelected>>", ...)`` and the
    displayed text follows ``textvariable``.
    """

    def __init__(self, master, textvariable=None, state="readonly", width=1, **kwargs):
        background = parent_background(master)
        super().__init__(master, bg=background, bd=0, highlightthickness=0, **kwargs)
        self._variable = textvariable
        self._state = state
        self._values: list[str] = []
        self._selected_callback = None
        self._hover = False
        self._focused = False
        self._image = None
        self._canvas = tk.Canvas(self, bg=background, bd=0, highlightthickness=0,
                                 height=tokens.dp(self, tokens.CONTROL["field_height"]),
                                 takefocus=1)
        self._canvas.pack(fill="both", expand=True)
        self._canvas.bind("<Configure>", self._paint)
        self._canvas.bind("<Button-1>", self._open)
        self._canvas.bind("<Enter>", lambda _event: self._set_hover(True))
        self._canvas.bind("<Leave>", lambda _event: self._set_hover(False))
        self._canvas.bind("<FocusIn>", lambda _event: self._set_focus(True))
        self._canvas.bind("<FocusOut>", lambda _event: self._set_focus(False))
        self._canvas.bind("<space>", self._open)
        self._canvas.bind("<Return>", self._open)
        if textvariable is not None:
            textvariable.trace_add("write", lambda *_: self._paint())

    # -- state helpers -------------------------------------------------
    def _set_hover(self, value):
        self._hover = value
        self._paint()

    def _set_focus(self, value):
        self._focused = value
        self._paint()

    def _text(self) -> str:
        return self._variable.get() if self._variable is not None else ""

    def _body_font(self):
        return tkfont.Font(root=self, family=tokens.FONT_FAMILY,
                           size=-tokens.font_px(self, "body"))

    # -- painting ------------------------------------------------------
    def _paint(self, _event=None) -> None:
        width = self._canvas.winfo_width()
        height = self._canvas.winfo_height()
        if width <= 1 or height <= 1:
            return
        disabled = self._state == "disabled"
        fill = tokens.PALETTE["disabled"] if disabled else tokens.PALETTE["background"]
        outline = tokens.PALETTE["accent"] if self._focused else tokens.PALETTE["border"]
        if disabled:
            outline = tokens.PALETTE["disabled"]
        image = rounded_image(width, height, tokens.dp(self, tokens.RADIUS["chip"]), fill,
                              outline, max(1, tokens.dp(self, tokens.CONTROL["border_width"])))
        self._image = ImageTk.PhotoImage(image, master=self._canvas)
        self._canvas.delete("all")
        self._canvas.create_image(0, 0, anchor="nw", image=self._image)
        font = self._body_font()
        pad = tokens.dp(self, tokens.SPACING["m"])
        arrow = tokens.dp(self, tokens.SPACING["xl"])
        text = self._text()
        available = max(1, width - 2 * pad - arrow)
        while text and font.measure(text) > available:
            text = text[:-1]
        color = tokens.PALETTE["disabled_text"] if disabled else tokens.PALETTE["text"]
        self._canvas.create_text(pad, height // 2, text=text, anchor="w", fill=color, font=font)
        self._canvas.create_text(width - pad, height // 2, text="▾", anchor="e",
                                 fill=tokens.PALETTE["muted"], font=font)

    # -- popup ---------------------------------------------------------
    @staticmethod
    def _owner_available(owner) -> bool:
        """True only while the owning window is normal and visible."""
        try:
            return owner.state() == "normal" and bool(owner.winfo_viewable())
        except tk.TclError:
            return False

    @staticmethod
    def _close_popup(popup) -> None:
        try:
            popup.grab_release()
        except tk.TclError:
            pass
        try:
            popup.destroy()
        except tk.TclError:
            pass

    def _watch_owner(self, popup) -> None:
        """An overrideredirect popup outlives a minimized owner unless closed."""
        try:
            if not popup.winfo_exists():
                return
        except tk.TclError:
            return
        if not self._owner_available(self.winfo_toplevel()):
            self._close_popup(popup)
            return
        popup.after(100, lambda: self._watch_owner(popup))

    def _open(self, _event=None):
        if self._state == "disabled" or not self._values:
            return "break"
        popup = tk.Toplevel(self)
        popup.wm_overrideredirect(True)
        popup.configure(bg=tokens.PALETTE["background"])
        popup.attributes("-topmost", True)
        margin = tokens.dp(self, 6)
        radius = tokens.dp(self, tokens.RADIUS["chip"])
        row = tokens.dp(self, 26)
        width = self._canvas.winfo_width()
        height = min(max(row * len(self._values) + 2 * margin, row + 2 * margin),
                     tokens.dp(self, 320))
        popup.geometry(f"{width}x{height}+{self._canvas.winfo_rootx()}+"
                       f"{self._canvas.winfo_rooty() + self._canvas.winfo_height()}")
        card = tk.Canvas(popup, bg=tokens.PALETTE["background"], bd=0, highlightthickness=0)
        card.pack(fill="both", expand=True)
        listbox = tk.Listbox(card, bd=0, highlightthickness=0,
                             bg=tokens.PALETTE["surface"], fg=tokens.PALETTE["text"],
                             selectbackground=tokens.PALETTE["accent"],
                             selectforeground=tokens.PALETTE["accent_text"],
                             activestyle="none", font=self._body_font())
        for value in self._values:
            listbox.insert("end", value)
        current = self._text()
        if current in self._values:
            index = self._values.index(current)
            listbox.selection_set(index)
            listbox.see(index)
        popup.update_idletasks()
        card_width = width
        card_height = height
        image = rounded_image(card_width, card_height, radius, tokens.PALETTE["surface"],
                              tokens.PALETTE["border"],
                              max(1, tokens.dp(self, tokens.CONTROL["border_width"])))
        card._popup_image = ImageTk.PhotoImage(image, master=card)
        card.create_image(0, 0, anchor="nw", image=card._popup_image)
        listbox.place(x=margin, y=margin,
                      width=max(1, card_width - 2 * margin),
                      height=max(1, card_height - 2 * margin))

        def close(_event=None):
            self._close_popup(popup)

        def choose(_event=None):
            selection = listbox.curselection()
            if selection and self._variable is not None:
                self._variable.set(self._values[selection[0]])
                if self._selected_callback is not None:
                    self._selected_callback()
            close()

        listbox.bind("<ButtonRelease-1>", choose)
        listbox.bind("<Return>", choose)
        listbox.bind("<Escape>", close)
        popup.bind("<Button-1>", close)
        listbox.focus_set()
        popup.grab_set()
        popup.after(100, lambda: self._watch_owner(popup))
        return "break"

    # -- ttk.Combobox compatible surface -------------------------------
    def __setitem__(self, key, value):
        if key == "values":
            self._values = [str(item) for item in value]
            self._paint()
            return
        self.configure(**{key: value})

    def __getitem__(self, key):
        if key == "values":
            return tuple(self._values)
        return self.cget(key)

    def configure(self, cnf=None, **kw):
        if "values" in kw:
            self._values = [str(item) for item in kw.pop("values")]
        if "state" in kw:
            self._state = str(kw.pop("state"))
        if kw:
            super().configure(cnf, **kw)
        self._paint()

    config = configure

    def cget(self, key):
        if key == "values":
            return tuple(self._values)
        if key == "state":
            return self._state
        return super().cget(key)

    def bind(self, sequence=None, func=None, add=None):
        if sequence == "<<ComboboxSelected>>":
            self._selected_callback = func
            return ""
        return super().bind(sequence, func, add)

    def get(self) -> str:
        return self._text()

    def set(self, value) -> None:
        if self._variable is not None:
            self._variable.set(str(value))


class RoundedCheck(tk.Canvas):
    """Rounded checkbox matching the Android shapes (ttk.Checkbutton replacement)."""

    def __init__(self, master, text="", variable=None, command=None, state="normal", **kwargs):
        background = parent_background(master)
        super().__init__(master, bg=background, bd=0, highlightthickness=0,
                         height=tokens.dp(master, tokens.CONTROL["check_height"]),
                         takefocus=1, **kwargs)
        self._text = str(text)
        self._variable = variable if variable is not None else tk.BooleanVar(value=False)
        self._command = command
        self._state = state
        self._hover = False
        self._focused = False
        self._image = None
        self.configure(width=self._preferred_width())
        self.bind("<Configure>", self._paint)
        self.bind("<Button-1>", self._toggle)
        self.bind("<space>", self._toggle)
        self.bind("<Return>", self._toggle)
        self.bind("<Enter>", lambda _event: self._set_hover(True))
        self.bind("<Leave>", lambda _event: self._set_hover(False))
        self.bind("<FocusIn>", lambda _event: self._set_focus(True))
        self.bind("<FocusOut>", lambda _event: self._set_focus(False))
        self._variable.trace_add("write", lambda *_: self._paint())

    def _body_font(self):
        return tkfont.Font(root=self, family=tokens.FONT_FAMILY,
                           size=-tokens.font_px(self, "body"))

    def _preferred_width(self) -> int:
        gap = tokens.dp(self, tokens.SPACING["s"])
        return (tokens.dp(self, tokens.CONTROL["check_box"]) + gap +
                self._body_font().measure(self._text) + gap)

    def _set_hover(self, value):
        self._hover = value
        self._paint()

    def _set_focus(self, value):
        self._focused = value
        self._paint()

    def _toggle(self, _event=None):
        if self._state == "disabled":
            return "break"
        self._variable.set(not bool(self._variable.get()))
        if self._command is not None:
            self._command()
        return "break"

    def _paint(self, _event=None) -> None:
        width = self.winfo_width()
        height = self.winfo_height()
        if width <= 1 or height <= 1:
            return
        box = tokens.dp(self, tokens.CONTROL["check_box"])
        disabled = self._state == "disabled"
        checked = bool(self._variable.get())
        fill = tokens.PALETTE["accent"] if checked else tokens.PALETTE["background"]
        if disabled:
            fill = tokens.PALETTE["disabled"]
        outline = tokens.PALETTE["accent"] if self._focused else tokens.PALETTE["border"]
        if disabled:
            outline = tokens.PALETTE["disabled"]
        image = rounded_image(box, box, tokens.dp(self, 6), fill, outline,
                              max(1, tokens.dp(self, tokens.CONTROL["border_width"])))
        self._image = ImageTk.PhotoImage(image, master=self)
        self.delete("all")
        top = (height - box) // 2
        self.create_image(0, top, anchor="nw", image=self._image)
        font = self._body_font()
        if checked:
            self.create_text(box // 2, top + box // 2, text="✓",
                             fill=tokens.PALETTE["accent_text"], font=font)
        color = tokens.PALETTE["disabled_text"] if disabled else tokens.PALETTE["text"]
        self.create_text(box + tokens.dp(self, tokens.SPACING["s"]), height // 2,
                         text=self._text, anchor="w", fill=color, font=font)

    # -- ttk.Checkbutton compatible surface ----------------------------
    def configure(self, cnf=None, **kw):
        if "text" in kw:
            self._text = str(kw.pop("text"))
            super().configure(width=self._preferred_width())
        if "state" in kw:
            self._state = str(kw.pop("state"))
        if "variable" in kw:
            self._variable = kw.pop("variable")
        if "command" in kw:
            self._command = kw.pop("command")
        if kw:
            super().configure(cnf, **kw)
        self._paint()

    config = configure

    def cget(self, key):
        if key == "state":
            return self._state
        if key == "text":
            return self._text
        return super().cget(key)
