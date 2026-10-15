#!/usr/bin/env python3
# SPDX-License-Identifier: MPL-2.0
"""Graphical interface: inventory (quantities, adding, deleting) and Kliff's skill points.

Changes are kept in memory and written all at once with "Save"
(the original is copied to ~/crimson_save_backups before each write).

Shortcuts: Ctrl+S save · Ctrl+N add · Ctrl+F search · Ctrl+Z revert all
           +/- quantity · Del delete
"""
from __future__ import annotations

import linux_only

linux_only.refuse_windows(gui=True)  # before the save modules and their dependencies

import json
import struct
import subprocess
import time
import tkinter as tk
from pathlib import Path
from tkinter import font as tkfont
from tkinter import messagebox, ttk

import cd_inventory
import cd_items
import cd_skills

try:
    import sv_ttk
except ImportError:  # the interface stays usable with the default Tk theme
    sv_ttk = None

MAX_STACK = cd_items.MAX_STACK
MAX_UNSAVED_COPIES = 10   # copies of one non-stackable item added before saving (mass additions broke saves)
MAX_SKILL_POINTS = 999      # cap offered in the interface (the format accepts up to 65535)
ABYSS_ARTIFACT = 75001      # "Abyss Artifact": the regular skill currency
TEXT_CLASSES = ("Entry", "TEntry", "TSpinbox", "Spinbox", "TCombobox", "Text")
INFINITE = 10**15  # beyond this, the max stack is considered unlimited
PREFS = Path.home() / ".config" / "crimson_save_editor.json"

COLORS = {
    "dark": {"modified": "#4a3f12", "added": "#173d24", "stripe": "#262626", "muted": "#9a9a9a",
             "danger": "#ff6b6b", "ok": "#5fd38d", "banner": "#5c1d1d", "sel": "#2b5f86", "sel_fg": "#ffffff"},
    "light": {"modified": "#fff1c2", "added": "#d6f5de", "stripe": "#f4f4f4", "muted": "#6b6b6b",
              "danger": "#c62828", "ok": "#1b7f3b", "banner": "#fde2e2", "sel": "#bcdcf5", "sel_fg": "#000000"},
}


_SCALE = 1.0


def px(n: float) -> int:
    """Pixel size adapted to the screen (HiDPI: fonts are enlarged, not geometry)."""
    return int(n * _SCALE)


def init_scale(root: tk.Misc):
    global _SCALE
    linespace = tkfont.nametofont("TkDefaultFont").metrics("linespace")
    _SCALE = max(1.0, linespace / 15)


def game_running() -> bool:
    try:
        return subprocess.run(["pgrep", "-f", "CrimsonDesert.exe"], capture_output=True).returncode == 0
    except OSError:  # pgrep not installed: no warning rather than no editor
        return False


def inventory_label(key) -> str:
    return cd_inventory.INVENTORY_NAMES.get(key, f"Inventory {key}")


def fmt(n) -> str:
    return f"{n:,}".replace(",", " ") if isinstance(n, int) else str(n)


def load_prefs() -> dict:
    try:
        return json.loads(PREFS.read_text())
    except (OSError, ValueError):
        return {}


def save_prefs(prefs: dict):
    try:
        PREFS.parent.mkdir(parents=True, exist_ok=True)
        PREFS.write_text(json.dumps(prefs))
    except OSError:
        pass


def configure_sel_tag(tree: ttk.Treeview, colors: dict):
    """Tag colours hide the Treeview selection: redraw it with a tag
    configured last (hence taking priority)."""
    tree.tag_configure("sel", background=colors["sel"], foreground=colors["sel_fg"])


def sync_sel_tag(tree: ttk.Treeview):
    selected = set(tree.selection())
    for iid in tree.tag_has("sel"):
        if iid not in selected:
            tree.item(iid, tags=[t for t in tree.item(iid, "tags") if t != "sel"])
    for iid in selected:
        tags = list(tree.item(iid, "tags"))
        if "sel" not in tags:
            tree.item(iid, tags=tags + ["sel"])


def responsive_columns(tree: ttk.Treeview, weights: dict):
    """Splits the available width between columns by weight (none overflows)."""
    def on_resize(event):
        total = sum(weights.values())
        avail = max(event.width - px(4), 100)
        for col, w in weights.items():
            tree.column(col, width=int(avail * w / total), stretch=False)
    tree.bind("<Configure>", on_resize, add="+")


class PlaceholderEntry(ttk.Entry):
    """Entry field with greyed-out placeholder text."""

    def __init__(self, master, placeholder: str, textvariable: tk.StringVar, **kw):
        super().__init__(master, **kw)
        self.var, self.placeholder, self.showing = textvariable, placeholder, False
        self.bind("<FocusIn>", self._focus_in)
        self.bind("<FocusOut>", self._focus_out)
        self._focus_out()

    def _focus_in(self, _=None):
        if self.showing:
            self.delete(0, "end")
            self.configure(foreground="")
            self.showing = False
        self.configure(textvariable=self.var)

    def _focus_out(self, _=None):
        if not self.var.get():
            self.configure(textvariable="")
            self.delete(0, "end")
            self.insert(0, self.placeholder)
            self.configure(foreground="gray")
            self.showing = True


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("Crimson Desert – Save Editor")
        self.prefs = load_prefs()
        self.theme = self.prefs.get("theme", "dark")
        self._apply_theme()
        w = min(px(1240), self.winfo_screenwidth() - 80)
        h = min(px(720), self.winfo_screenheight() - 120)
        self.geometry(f"{w}x{h}")
        self.minsize(min(px(960), w), min(px(520), h))

        self.db = cd_inventory.load_items_db()
        self.names = self.db              # single source for names and max stacks
        self.path: Path | None = None
        self.header = b""
        self.blob = bytearray()           # working blob (includes additions / deletions)
        self.items: dict[int, dict] = {}
        self.inv_of: dict[int, int] = {}  # element start -> sub-inventory key
        self.invs: dict = {}
        self.pending: dict[int, int] = {}  # id -> new quantity
        self.added: set[int] = set()      # itemNo of items added since loading
        self.orig: dict[int, int] = {}    # itemNo -> quantity on disk
        self.removed: list[str] = []      # names of deleted original items
        self.add_dialog = None
        self.knames = cd_skills.load_knowledge_names()
        self.skills = None                # SkillState of the working blob
        self.skills_orig = None           # SkillState on disk
        self.props: dict[int, dict] = {}       # itemNo -> {"enchant", "endurance"} in the working blob
        self.props_orig: dict[int, dict] = {}  # same, on disk

        self._build()
        self._bind_shortcuts()
        self._refresh_slots()

    # ------------------------------------------------------------ theme

    def _apply_theme(self):
        if sv_ttk:
            sv_ttk.set_theme(self.theme)
        init_scale(self)
        style = ttk.Style(self)
        base = tkfont.nametofont("TkDefaultFont")
        size = base.cget("size") or 10
        if not hasattr(self, "font_title"):  # created once: widgets keep following later changes
            self.font_title, self.font_big, self.font_small = tkfont.Font(), tkfont.Font(), tkfont.Font()
        family = base.cget("family")
        self.font_title.configure(family=family, size=size + 5, weight="bold")
        self.font_big.configure(family=family, size=size + 3, weight="bold")
        self.font_small.configure(family=family, size=max(size - 1, 8))
        style.configure("Treeview", rowheight=base.metrics("linespace") + px(8))
        style.configure("Side.TRadiobutton", padding=(px(4), px(3)))
        style.map("Treeview", background=[("selected", self.c("sel"))], foreground=[("selected", self.c("sel_fg"))])
        style.configure("Title.TLabel", font=self.font_title)
        style.configure("Big.TLabel", font=self.font_big)
        style.configure("Muted.TLabel", foreground=self.c("muted"), font=self.font_small)
        style.configure("Danger.TLabel", foreground=self.c("danger"))
        style.configure("Ok.TLabel", foreground=self.c("ok"))
        style.configure("Banner.TLabel", background=self.c("banner"), foreground=self.c("danger"),
                        padding=(12, 6))
        if hasattr(self, "tree"):
            self._configure_tags()
            self._fill()
            self._build_sidebar()
        if hasattr(self, "sk_tree"):
            self.sk_tree.tag_configure("stripe", background=self.c("stripe"))
            configure_sel_tag(self.sk_tree, COLORS[self.theme])

    def c(self, name: str) -> str:
        return COLORS[self.theme][name]

    def _toggle_theme(self):
        self.theme = "light" if self.theme == "dark" else "dark"
        self.prefs["theme"] = self.theme
        save_prefs(self.prefs)
        self._apply_theme()
        self.theme_btn["text"] = "☀ Light" if self.theme == "dark" else "☾ Dark"
        if self.add_dialog:
            self.add_dialog.restyle()

    def _configure_tags(self):
        self.tree.tag_configure("stripe", background=self.c("stripe"))
        self.tree.tag_configure("modified", background=self.c("modified"))
        self.tree.tag_configure("added", background=self.c("added"))
        configure_sel_tag(self.tree, COLORS[self.theme])

    # ------------------------------------------------------------ layout

    def _build(self):
        # top bar
        top = ttk.Frame(self, padding=(px(16), px(12), px(16), px(8)))
        top.pack(fill="x")
        ttk.Label(top, text="Crimson Desert", style="Title.TLabel").pack(side="left")
        ttk.Label(top, text="  Save Editor", style="Big.TLabel").pack(side="left")

        self.theme_btn = ttk.Button(top, text="☀ Light" if self.theme == "dark" else "☾ Dark",
                                    command=self._toggle_theme)
        self.theme_btn.pack(side="right")
        ttk.Button(top, text="↻", width=3, command=self._load).pack(side="right", padx=(0, 8))
        self.slot_var = tk.StringVar()
        self.slot_box = ttk.Combobox(top, textvariable=self.slot_var, state="readonly", width=30)
        self.slot_box.pack(side="right", padx=6)
        self.slot_box.bind("<<ComboboxSelected>>", lambda e: self._load())
        ttk.Label(top, text="Save").pack(side="right")

        self.banner = ttk.Label(self, style="Banner.TLabel",
                                text="⚠  Crimson Desert is running: close it before saving, "
                                     "otherwise it will overwrite your changes.")

        # body: sidebar | table | details
        self.nb = ttk.Notebook(self)
        self.nb.pack(fill="both", expand=True, padx=px(16), pady=(px(4), 0))
        self.body = self.nb
        body = ttk.Frame(self.nb, padding=(0, px(8), 0, 0))
        self.nb.add(body, text="   Inventory   ")
        skills_tab = ttk.Frame(self.nb, padding=(0, px(8), 0, 0))
        self.nb.add(skills_tab, text="   Skills   ")
        self._build_skills(skills_tab)

        self.sidebar = ttk.Frame(body, width=px(220))
        self.sidebar.pack(side="left", fill="y", padx=(0, px(12)))
        self.sidebar.pack_propagate(False)
        self.inv_filter = tk.StringVar(value="all")

        self.detail = ttk.Frame(body, width=px(290))
        self.detail.pack(side="right", fill="y", padx=(px(12), 0))
        self.detail.pack_propagate(False)
        self._build_detail()

        center = ttk.Frame(body)
        center.pack(side="left", fill="both", expand=True)
        bar = ttk.Frame(center)
        bar.pack(fill="x", pady=(0, 8))
        self.filter_var = tk.StringVar()
        self.filter_var.trace_add("write", lambda *a: self._fill())
        self.search_entry = PlaceholderEntry(bar, "Search for an item or an itemKey…  (Ctrl+F)",
                                             self.filter_var)
        self.search_entry.pack(side="left", fill="x", expand=True)
        ttk.Button(bar, text="+ Add item", style="Accent.TButton",
                   command=self._open_add).pack(side="left", padx=(8, 0))

        tf = ttk.Frame(center)
        tf.pack(fill="both", expand=True)
        cols = ("name", "cat", "inv", "slot", "qty", "max")
        self.tree = ttk.Treeview(tf, columns=cols, show="headings", selectmode="extended")
        self.headings = {"name": ("Item", 260), "cat": ("Category", 95), "inv": ("Inventory", 140),
                         "slot": ("Slot", 55), "qty": ("Quantity", 120), "max": ("Max stack", 85)}
        for col, (label, w) in self.headings.items():
            self.tree.heading(col, text=label, command=lambda col=col: self._sort(col))
            self.tree.column(col, width=px(w), minwidth=px(40),
                             anchor={"name": "w", "cat": "w", "inv": "w", "slot": "center"}.get(col, "e"))
        responsive_columns(self.tree, {"name": 3.0, "cat": 1.2, "inv": 1.9, "slot": 0.6, "qty": 1.5, "max": 1.0})
        sb = ttk.Scrollbar(tf, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=sb.set)
        self.tree.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")
        self._configure_tags()
        self.tree.bind("<<TreeviewSelect>>", lambda e: (sync_sel_tag(self.tree), self._on_select()))
        for key, d in (("<plus>", 1), ("<KP_Add>", 1), ("<minus>", -1), ("<KP_Subtract>", -1)):
            self.tree.bind(key, lambda e, d=d: self._delta(d))
        self.tree.bind("<Delete>", lambda e: self._remove())
        self.sort_col, self.sort_rev = "slot", False

        # footer
        foot = ttk.Frame(self, padding=(px(16), px(10)))
        foot.pack(fill="x")
        self.status = ttk.Label(foot, text="")
        self.status.pack(side="left")
        self.save_btn = ttk.Button(foot, text="Save  (Ctrl+S)", style="Accent.TButton",
                                   command=self._save, state="disabled")
        self.save_btn.pack(side="right")
        self.revert_btn = ttk.Button(foot, text="Revert all", command=self._revert, state="disabled")
        self.revert_btn.pack(side="right", padx=8)

        self.protocol("WM_DELETE_WINDOW", self._quit)
        self._tick_game_check()

    def _build_sidebar(self):
        for w in self.sidebar.winfo_children():
            w.destroy()
        ttk.Label(self.sidebar, text="INVENTORIES", style="Muted.TLabel").pack(anchor="w", pady=(4, 6))

        def entry(value, title, count, cap=None):
            f = ttk.Frame(self.sidebar)
            f.pack(fill="x", pady=2)
            rb = ttk.Radiobutton(f, text=f"{title}", value=value, variable=self.inv_filter,
                                 style="Side.TRadiobutton", command=self._fill)
            rb.pack(anchor="w")
            info = f"{count} item(s)" if cap is None else f"{count} / {cap} slots"
            ttk.Label(f, text=info, style="Muted.TLabel").pack(anchor="w", padx=px(30))
            if cap:
                pb = ttk.Progressbar(f, maximum=cap, value=count)
                pb.pack(fill="x", padx=(px(30), px(8)), pady=(2, 4))

        entry("all", "All", len(self.items))
        order = sorted(self.invs, key=lambda k: (k != cd_inventory.BAG, k != cd_inventory.RESOURCES, k))
        for k in order:
            inv = self.invs[k]
            if not inv.items:
                continue
            entry(str(k), inventory_label(k), len(inv.items), inv.capacity(self.db))

        ttk.Separator(self.sidebar).pack(fill="x", pady=12)
        ttk.Label(self.sidebar, text="LEGEND", style="Muted.TLabel").pack(anchor="w", pady=(0, 6))
        for tag, text in (("modified", "changed"), ("added", "item added")):
            row = ttk.Frame(self.sidebar)
            row.pack(fill="x", pady=1)
            tk.Label(row, width=2, background=self.c(tag)).pack(side="left", padx=(0, 6))
            ttk.Label(row, text=text, style="Muted.TLabel").pack(side="left")

    def _build_detail(self):
        d = self.detail
        ttk.Label(d, text="DETAILS", style="Muted.TLabel").pack(anchor="w", pady=(4, 6))
        self.d_name = ttk.Label(d, text="No item selected", style="Big.TLabel", wraplength=px(270))
        self.d_name.pack(anchor="w")
        self.d_meta = ttk.Label(d, text="Select a row in the table.", style="Muted.TLabel",
                                wraplength=px(270), justify="left")
        self.d_meta.pack(anchor="w", pady=(4, 12))

        self.d_qty_frame = ttk.LabelFrame(d, text=" Quantity ", padding=px(10))
        self.d_qty_frame.pack(fill="x")
        self.qty_var = tk.StringVar()
        qrow = ttk.Frame(self.d_qty_frame)
        qrow.pack(fill="x")
        self.qty_entry = ttk.Entry(qrow, textvariable=self.qty_var, justify="center", font=self.font_big, width=10)
        self.qty_entry.pack(side="left", fill="x", expand=True)
        self.qty_entry.bind("<Return>", lambda e: self._set_exact())
        ttk.Button(qrow, text="OK", width=4, command=self._set_exact).pack(side="left", padx=(6, 0))
        self.d_qty_info = ttk.Label(self.d_qty_frame, text="", style="Muted.TLabel")
        self.d_qty_info.pack(anchor="w", pady=(4, 6))
        grid = ttk.Frame(self.d_qty_frame)
        grid.pack(fill="x")
        for i, dlt in enumerate((-100, -10, -1, 1, 10, 100)):
            ttk.Button(grid, text=f"{dlt:+d}", command=lambda dlt=dlt: self._delta(dlt)).grid(
                row=i // 3, column=i % 3, sticky="ew", padx=2, pady=2)
        for col in range(3):
            grid.columnconfigure(col, weight=1)
        brow = ttk.Frame(self.d_qty_frame)
        brow.pack(fill="x", pady=(6, 0))
        ttk.Button(brow, text="Max stack", command=self._set_max).pack(side="left", fill="x", expand=True, padx=2)
        ttk.Button(brow, text="Reset", command=self._reset_selected).pack(side="left", fill="x", expand=True,
                                                                              padx=2)
        self.force_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(self.d_qty_frame, text="Ignore max stack", variable=self.force_var).pack(
            anchor="w", pady=(8, 0))

        eq = ttk.LabelFrame(d, text=" Enchantment & durability ", padding=px(10))
        eq.pack(fill="x", pady=(px(10), 0))
        erow = ttk.Frame(eq)
        erow.pack(fill="x")
        ttk.Label(erow, text="Enchant  +").pack(side="left")
        self.ench_var = tk.StringVar()
        self.ench_box = ttk.Spinbox(erow, from_=0, to=10, textvariable=self.ench_var, width=5, justify="right")
        self.ench_box.pack(side="left")
        self.ench_box.bind("<Return>", lambda e: self._set_enchant())
        self.ench_btn = ttk.Button(erow, text="Set", width=5, command=self._set_enchant)
        self.ench_btn.pack(side="left", padx=(6, 0))
        self.ench_info = ttk.Label(eq, text="", style="Muted.TLabel", wraplength=px(260), justify="left")
        self.ench_info.pack(anchor="w", pady=(4, 6))
        drow = ttk.Frame(eq)
        drow.pack(fill="x")
        self.dur_info = ttk.Label(drow, text="", style="Muted.TLabel")
        self.dur_info.pack(side="left")
        self.repair_btn = ttk.Button(drow, text="Repair", command=self._repair)
        self.repair_btn.pack(side="right")

        ttk.Button(d, text="Remove from inventory  (Del)", command=self._remove).pack(fill="x", pady=(16, 0))
        ttk.Label(d, text="+/- : quantity · Del : delete\nCtrl+N : add · Ctrl+S : save",
                  style="Muted.TLabel", justify="left").pack(side="bottom", anchor="w", pady=8)

    def _build_skills(self, parent):
        left = ttk.Frame(parent, width=px(380))
        left.pack(side="left", fill="y", padx=(0, px(16)))
        left.pack_propagate(False)

        card = ttk.LabelFrame(left, text=" Kliff — skill points ", padding=px(12))
        card.pack(fill="x")
        self.sk_points = ttk.Label(card, text="–", style="Title.TLabel")
        self.sk_points.pack(anchor="w")
        ttk.Label(card, text="points available to spend in the tree", style="Muted.TLabel").pack(anchor="w")
        self.sk_meta = ttk.Label(card, text="", style="Muted.TLabel", justify="left")
        self.sk_meta.pack(anchor="w", pady=(px(8), px(8)))
        row = ttk.Frame(card)
        row.pack(fill="x")
        self.sk_var = tk.StringVar()
        e = ttk.Entry(row, textvariable=self.sk_var, justify="center", font=self.font_big, width=8)
        e.pack(side="left", fill="x", expand=True)
        e.bind("<Return>", lambda ev: self._skill_define())
        set_btn = ttk.Button(row, text="Set", style="Accent.TButton", command=self._skill_define)
        set_btn.pack(side="left", padx=(6, 0))
        self.sk_controls = [e, set_btn]   # disabled when the save cannot be edited safely
        grid = ttk.Frame(card)
        grid.pack(fill="x", pady=(px(6), 0))
        for i, dlt in enumerate((-10, -1, 1, 5, 10, 50)):
            b = ttk.Button(grid, text=f"{dlt:+d}", command=lambda dlt=dlt: self._skill_delta(dlt))
            b.grid(row=0, column=i, sticky="ew", padx=1)
            self.sk_controls.append(b)
            grid.columnconfigure(i, weight=1)
        self.sk_warn = ttk.Label(card, text="", style="Danger.TLabel", wraplength=px(340), justify="left")
        self.sk_warn.pack(anchor="w", pady=(px(6), 0))

        art = ttk.LabelFrame(left, text=" Abyss Artifacts ", padding=px(12))
        art.pack(fill="x", pady=(px(12), 0))
        self.sk_art = ttk.Label(art, text="", justify="left", wraplength=px(340))
        self.sk_art.pack(anchor="w")
        ttk.Button(art, text=f"Fill a stack ({self._artifact_stack()})", command=self._fill_artifacts).pack(anchor="w", pady=(px(8), 0))
        ttk.Label(art, text="Absorb them in-game: 1 artifact = 1 skill point.",
                  style="Muted.TLabel", wraplength=px(340), justify="left").pack(anchor="w", pady=(px(6), 0))

        ttk.Label(left, text=("Points are spent in the game's Skills menu, which checks "
                              "prerequisites and costs itself. No skill is unlocked "
                              "directly in the save."),
                  style="Muted.TLabel", wraplength=px(360), justify="left").pack(anchor="w", pady=(px(14), 0))

        right = ttk.Frame(parent)
        right.pack(side="left", fill="both", expand=True)
        ttk.Label(right, text="SKILLS LEARNED BY KLIFF", style="Muted.TLabel").pack(anchor="w", pady=(0, px(6)))
        tf = ttk.Frame(right)
        tf.pack(fill="both", expand=True)
        cols = ("name", "key", "level", "spent")
        self.sk_tree = ttk.Treeview(tf, columns=cols, show="headings", selectmode="browse")
        for col, label in (("name", "Skill"), ("key", "Key"), ("level", "Level"), ("spent", "Points spent")):
            self.sk_tree.heading(col, text=label)
            self.sk_tree.column(col, width=px(100), anchor="w" if col == "name" else "center")
        responsive_columns(self.sk_tree, {"name": 3.0, "key": 1.0, "level": 0.8, "spent": 1.2})
        sb = ttk.Scrollbar(tf, orient="vertical", command=self.sk_tree.yview)
        self.sk_tree.configure(yscrollcommand=sb.set)
        self.sk_tree.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")
        self.sk_tree.tag_configure("stripe", background=self.c("stripe"))
        configure_sel_tag(self.sk_tree, COLORS[self.theme])
        self.sk_tree.bind("<<TreeviewSelect>>", lambda e: sync_sel_tag(self.sk_tree))

    def _refresh_skills(self):
        st = self.skills
        self.sk_tree.delete(*self.sk_tree.get_children())
        editable = st is not None and st.consistent
        for w in self.sk_controls:
            w.state(["!disabled"] if editable else ["disabled"])
        if st is None:
            self.sk_points["text"] = "–"
            self.sk_meta["text"] = ""
            self.sk_warn["text"] = "Skill data could not be read from this save."
            return
        self.sk_points["text"] = fmt(st.current)
        orig = self.skills_orig
        extra = f"\nOn disk: {fmt(orig.current)} available" if orig and orig.current != st.current else ""
        self.sk_meta["text"] = f"Spent: {fmt(st.spent)}    ·    Total earned: {fmt(st.elapsed)}{extra}"
        self.sk_var.set(str(st.current))
        self.sk_warn["text"] = ("" if st.consistent else
                                "Unusual save (earned ≠ spent + available): editing disabled.")
        for i, (key, name, level, spent) in enumerate(sorted(st.learned, key=lambda x: x[1].lower())):
            self.sk_tree.insert("", "end", values=(name, key, level, spent), tags=("stripe",) if i % 2 else ())
        have = sum(self._qty(it["id"]) for it in self.items.values() if it["key"] == ABYSS_ARTIFACT)
        used, cap = self.space(cd_inventory.BAG)
        self.sk_art["text"] = (f"Abyss Artifacts in bag: {fmt(have)}\n"
                               f"Bag: {used}/{cap} slots" if cap else f"Abyss Artifacts in bag: {fmt(have)}")

    def skills_changed(self) -> bool:
        return bool(self.skills and self.skills_orig and self.skills.current != self.skills_orig.current)

    def _set_points(self, n: int):
        if self.skills is None:
            return
        n = max(0, min(n, max(MAX_SKILL_POINTS, self.skills.current)))
        if n == self.skills.current:
            self._refresh_skills()
            return
        try:
            new_blob = cd_skills.set_skill_points(self._blob_with_pending(), n)
        except Exception as e:
            messagebox.showerror("Skill points", str(e) or type(e).__name__)
            return
        self._set_blob(new_blob)

    def _skill_delta(self, d: int):
        if self.skills:
            self._set_points(self.skills.current + d)

    def _skill_define(self):
        try:
            self._set_points(int(self.sk_var.get().replace(" ", "")))
        except ValueError:
            self._refresh_skills()

    def _artifact_stack(self) -> int:
        return self.db.get(ABYSS_ARTIFACT, {}).get("maxStack") or 10

    def _fill_artifacts(self):
        stack = next((it for it in self.items.values() if it["key"] == ABYSS_ARTIFACT
                      and self.inv_of.get(it["header"]) == cd_inventory.BAG), None)
        m = self._artifact_stack()
        if stack is not None:
            if self._qty(stack["id"]) < m:  # never lower a bigger stack
                self._apply(stack, m)
            self._after_edit()
            self._refresh_skills()
            return
        err = self.add_item(ABYSS_ARTIFACT, m)
        if err:
            messagebox.showerror("Abyss Artifacts", err)

    def _bind_shortcuts(self):
        self.bind_all("<Control-s>", lambda e: self._save())
        self.bind_all("<Control-n>", lambda e: self._open_add())
        # Ctrl+Z inside a text field is left alone (it used to trigger "Revert all" while typing)
        self.bind_all("<Control-z>", lambda e: None if e.widget.winfo_class() in TEXT_CLASSES else self._revert())
        self.bind("<Control-f>", lambda e: (self.search_entry.focus_set(), "break")[1])
        self.bind("<Escape>", lambda e: self.tree.focus_set())

    # ------------------------------------------------------------ data

    def _refresh_slots(self):
        saves = sorted(cd_items.find_saves(),
                       key=lambda p: p.stat().st_mtime, reverse=True)
        several_ids = len({p.parent.parent for p in saves}) > 1
        self.slot_paths = {}
        for p in saves:
            label = f"{p.parent.parent.name} / {p.parent.name}" if several_ids else p.parent.name
            label, n = f"{label}  ·  {_mtime(p)}", 1
            while label in self.slot_paths:   # never let two saves share a label
                n += 1
                label = f"{label.split(' (')[0]} ({n})"
            self.slot_paths[label] = p
        self.slot_box["values"] = list(self.slot_paths)
        if saves:
            self.slot_box.current(0)
            self._load()
        else:
            self.status["text"] = "No save found (set the folder with the CD_SAVE_DIR variable)"

    def dirty(self) -> bool:
        return bool(self.added or self.removed or self.changed() or self.skills_changed() or self.props_changed())

    def props_changed(self) -> set:
        """Original items whose enchant level or durability differs from the one on disk."""
        return {no for no, pr in self.props.items()
                if no in self.props_orig and no not in self.added and pr != self.props_orig[no]}

    def changed(self) -> list[dict]:
        """Original items whose quantity differs from the one on disk."""
        return [it for it in self.items.values()
                if it["itemNo"] in self.orig and it["itemNo"] not in self.added
                and self._qty(it["id"]) != self.orig[it["itemNo"]]]

    def _load(self, ask=True):
        if ask and self.dirty() and not messagebox.askyesno("Unsaved changes",
                                                            "Discard current changes?"):
            self._show_current_slot()
            return
        path = self.slot_paths.get(self.slot_var.get())
        if not path:
            return
        try:
            header, blob = cd_items.load_save(path)
            decoded = self._decode(blob)
        except Exception as e:  # unreadable save: keep the current one, never mix two slots
            messagebox.showerror("Read error", f"{path.parent.name}: {e or type(e).__name__}")
            self._show_current_slot()
            return
        self.path, self.header = path, header
        self.added.clear()
        self.removed.clear()
        self.orig = {i["itemNo"]: i["stack"] for i in decoded[0] if i["container"] == "InventorySaveData"}
        self.skills_orig = decoded[2]
        self.props_orig = self._props_of(blob, decoded[1])
        self._set_blob(blob, decoded)

    def _show_current_slot(self):
        """Puts the slot selector back on the save actually loaded."""
        label = next((k for k, v in self.slot_paths.items() if v == self.path), "")
        self.slot_var.set(label)

    def _decode(self, blob: bytearray):
        """Decodes what the window shows; raises if the items themselves cannot be read."""
        items = cd_items.find_items(blob)
        try:
            invs = cd_inventory.read_inventories(cd_inventory.Decoder(blob))
        except Exception:
            invs = {}  # adding / deleting disabled, editing still possible
        try:
            skills = cd_skills.read_skills(blob, self.knames)
        except Exception:
            skills = None
        return items, invs, skills

    @staticmethod
    def _props_of(blob: bytearray, invs: dict) -> dict[int, dict]:
        return {struct.unpack_from("<Q", blob, el.fields["_itemNo"][0])[0]: cd_inventory.item_props(blob, el)
                for inv in invs.values() for el in inv.items if "_itemNo" in el.fields}

    def _set_blob(self, blob: bytearray, decoded=None):
        """Installs a new working blob and recomputes the item list."""
        if decoded is None:
            try:
                decoded = self._decode(blob)
            except Exception as e:  # keep the previous working blob
                messagebox.showerror("Read error", str(e) or type(e).__name__)
                return
        items, self.invs, self.skills = decoded
        self.blob = blob
        self.pending.clear()
        self.items = {i["id"]: i for i in items if i["container"] == "InventorySaveData"}
        self.inv_of = {el.start: k for k, inv in self.invs.items() for el in inv.items}
        self.props = self._props_of(blob, self.invs)
        if self.inv_filter.get() != "all" and int(self.inv_filter.get()) not in self.invs:
            self.inv_filter.set("all")
        self._build_sidebar()
        self._fill()
        self._on_select()
        self._refresh_skills()
        if self.add_dialog:
            self.add_dialog.refresh()

    def _max_for(self, it) -> int:
        if self.force_var.get():
            return MAX_STACK
        m = self.names.get(it["key"], {}).get("maxStack")
        # the table is not always accurate: never cap below the current quantity
        return max(min(m, MAX_STACK), it["stack"]) if m else MAX_STACK

    def _qty(self, iid: int) -> int:
        return self.pending.get(iid, self.items[iid]["stack"])

    def _before(self, it) -> int:
        cur = self._qty(it["id"])
        return cur if it["itemNo"] in self.added else self.orig.get(it["itemNo"], cur)

    def _row(self, it):
        info = self.names.get(it["key"], {})
        cur, before = self._qty(it["id"]), self._before(it)
        qty = f"{fmt(before)} → {fmt(cur)}" if cur != before else fmt(cur)
        m = info.get("maxStack")
        inv = self.inv_of.get(it["header"])
        name = info.get("name", f"Unknown item ({it['key']})")
        ench = self.props.get(it["itemNo"], {}).get("enchant")
        if ench:
            name += f"  +{ench}"
        return (name, self.db.get(it["key"], {}).get("category") or "–",
                inventory_label(inv) if inv is not None else "?", it["slot"], qty,
                "∞" if m and m >= INFINITE else (fmt(m) if m else "?"))

    def _tags(self, it, index=0):
        if it["itemNo"] in self.added:
            return ("added",)
        if it["itemNo"] in self.orig and self._qty(it["id"]) != self.orig[it["itemNo"]]:
            return ("modified",)
        if it["itemNo"] in self.props_changed():
            return ("modified",)
        return ("stripe",) if index % 2 else ()

    def _fill(self):
        sel = set(self.tree.selection())
        self.tree.delete(*self.tree.get_children())
        flt = self.filter_var.get().strip().lower()
        inv_f = self.inv_filter.get()
        rows = []
        for it in self.items.values():
            if inv_f != "all" and self.inv_of.get(it["header"]) != int(inv_f):
                continue
            row = self._row(it)
            if flt and flt not in row[0].lower() and flt != str(it["key"]):
                continue
            rows.append((row, it))
        cols = ("name", "cat", "inv", "slot", "qty", "max")
        idx = cols.index(self.sort_col)
        def sort_key(r):
            if self.sort_col == "qty":
                v = self._qty(r[1]["id"])
            elif self.sort_col == "max":  # raw value, not the formatted "100 000"
                v = self.names.get(r[1]["key"], {}).get("maxStack") or 0
            else:
                v = r[0][idx]
            return (0, v) if isinstance(v, int) else (1, str(v).lower())
        for i, (row, it) in enumerate(sorted(rows, key=sort_key, reverse=self.sort_rev)):
            self.tree.insert("", "end", iid=str(it["id"]), values=row, tags=self._tags(it, i))
        for col, (label, _) in self.headings.items():
            arrow = (" ▼" if self.sort_rev else " ▲") if col == self.sort_col else ""
            self.tree.heading(col, text=label + arrow)
        keep = [s for s in sel if self.tree.exists(s)]
        if keep:
            self.tree.selection_set(keep)
        sync_sel_tag(self.tree)
        self._update_status()

    def _sort(self, col):
        self.sort_rev = not self.sort_rev if self.sort_col == col else False
        self.sort_col = col
        self._fill()

    def _selected(self) -> list[dict]:
        return [self.items[int(s)] for s in self.tree.selection() if int(s) in self.items]

    def _on_select(self):
        sel = self._selected()
        if not sel:
            self.d_name["text"] = "No item selected"
            self.d_meta["text"] = "Select one or more rows in the table."
            self.qty_var.set("")
            self.d_qty_info["text"] = ""
            self._show_equipment([])
        elif len(sel) == 1:
            it = sel[0]
            info = self.db.get(it["key"], {})
            inv = self.inv_of.get(it["header"])
            self.d_name["text"] = info.get("name", f"Unknown item ({it['key']})")
            self.d_meta["text"] = (f"{info.get('category') or 'Unknown category'}  ·  itemKey {it['key']}\n"
                                   f"{inventory_label(inv) if inv is not None else '?'}  ·  slot {it['slot']}")
            self.qty_var.set(str(self._qty(it["id"])))
            m = info.get("maxStack")
            mx = "unlimited" if m and m >= INFINITE else (fmt(m) if m else "?")
            before = self._before(it)
            extra = f"  ·  on disk: {fmt(before)}" if before != self._qty(it["id"]) else ""
            self.d_qty_info["text"] = f"Max stack: {mx}{extra}"
            self._show_equipment([it])
        else:
            self.d_name["text"] = f"{len(sel)} items selected"
            self.d_meta["text"] = "Buttons apply to the whole selection."
            self.qty_var.set("")
            self.d_qty_info["text"] = ""
            self._show_equipment(sel)

    # ------------------------------------------------------------ quantity editing

    def _apply(self, it, value: int):
        value = max(1, min(value, self._max_for(it)))
        if value == it["stack"]:
            self.pending.pop(it["id"], None)
        else:
            self.pending[it["id"]] = value
        iid = str(it["id"])
        if self.tree.exists(iid):
            tags = list(self._tags(it, self.tree.index(iid)))
            if iid in self.tree.selection():
                tags.append("sel")
            self.tree.item(iid, values=self._row(it), tags=tags)

    def _after_edit(self):
        self._on_select()
        self._update_status()

    def _delta(self, d: int):
        for it in self._selected():
            self._apply(it, self._qty(it["id"]) + d)
        self._after_edit()

    def _set_exact(self):
        try:
            v = int(self.qty_var.get().replace(" ", ""))
        except ValueError:
            return
        for it in self._selected():
            self._apply(it, v)
        self._after_edit()

    def _set_max(self):
        for it in self._selected():
            m = self.names.get(it["key"], {}).get("maxStack")
            if m and m < INFINITE:
                self._apply(it, max(m, it["stack"]) if not self.force_var.get() else m)
        self._after_edit()

    def _reset_selected(self):
        for it in self._selected():
            self._apply(it, self._before(it))
        self._after_edit()

    # ------------------------------------------------------------ enchantment / durability

    def _ench_cap(self, it) -> int:
        """Highest level offered: the item's max, never below its current level."""
        cur = self.props.get(it["itemNo"], {}).get("enchant", 0)
        m = cd_inventory.max_enchant(it["key"], self.db)
        return max(m, cur) if m else 0

    def _show_equipment(self, sel: list[dict]):
        enchantable = [it for it in sel if self._ench_cap(it)]
        damaged = [it for it in sel if self.props.get(it["itemNo"], {}).get("endurance")
                   not in (None, cd_inventory.FULL_ENDURANCE)]
        state = ["!disabled"] if enchantable else ["disabled"]
        self.ench_box.state(state)
        self.ench_btn.state(state)
        if len(sel) == 1 and enchantable:
            it = sel[0]
            self.ench_box["to"] = self._ench_cap(it)
            self.ench_var.set(str(self.props[it["itemNo"]]["enchant"]))
            before = self.props_orig.get(it["itemNo"], {}).get("enchant")
            extra = f"  ·  on disk: +{before}" if before is not None and before != self.props[it["itemNo"]]["enchant"] else ""
            self.ench_info["text"] = f"Max level: +{cd_inventory.max_enchant(it['key'], self.db)}{extra}"
        elif enchantable:
            self.ench_box["to"] = max(self._ench_cap(it) for it in enchantable)
            self.ench_info["text"] = f"{len(enchantable)} enchantable item(s), each capped at its own max."
        else:
            self.ench_var.set("")
            self.ench_info["text"] = "Not enchantable." if sel else ""
        if len(sel) == 1:
            dur = self.props.get(sel[0]["itemNo"], {}).get("endurance")
            self.dur_info["text"] = ("Durability: –" if dur is None else "Durability: full"
                                     if dur == cd_inventory.FULL_ENDURANCE else f"Durability: {fmt(dur)} (damaged)")
        else:
            self.dur_info["text"] = f"Damaged: {len(damaged)}" if sel else ""
        self.repair_btn.state(["!disabled"] if damaged else ["disabled"])

    def _edit_items(self, sel: list[dict], change) -> None:
        """Applies change(it) -> (field, value) | None to each item, one blob rewrite at a time."""
        blob = self._blob_with_pending()
        done = 0
        for it in sel:
            todo = change(it)
            if todo is None:
                continue
            try:
                invs = cd_inventory.read_inventories(cd_inventory.Decoder(blob))
                start = next(el.start for inv in invs.values() for el in inv.items
                             if "_itemNo" in el.fields
                             and struct.unpack_from("<Q", blob, el.fields["_itemNo"][0])[0] == it["itemNo"])
                blob = cd_inventory.set_item_field(blob, start, *todo)
            except Exception as e:
                name = self.names.get(it["key"], {}).get("name", str(it["key"]))
                messagebox.showerror("Cannot change item", f"{name}: {e or type(e).__name__}")
                break
            done += 1
        if done:
            self._set_blob(blob)

    def _set_enchant(self):
        try:
            level = int(self.ench_var.get())
        except ValueError:
            return
        def change(it):
            cap = self._ench_cap(it)
            v = max(0, min(level, cap))
            return ("_enchantLevel", v) if cap and v != self.props[it["itemNo"]]["enchant"] else None
        self._edit_items(self._selected(), change)

    def _repair(self):
        def change(it):
            dur = self.props.get(it["itemNo"], {}).get("endurance")
            return None if dur in (None, cd_inventory.FULL_ENDURANCE) else ("_endurance", cd_inventory.FULL_ENDURANCE)
        self._edit_items(self._selected(), change)

    def _blob_with_pending(self) -> bytearray:
        blob = bytearray(self.blob)
        for iid, v in self.pending.items():
            struct.pack_into("<Q", blob, self.items[iid]["stack_off"], v)
        return blob

    # ------------------------------------------------------------ adding items

    def _open_add(self):
        if not self.invs:
            messagebox.showerror("Cannot add",
                                 "The inventory structure could not be decoded for this save.")
            return
        if self.add_dialog:
            self.add_dialog.lift()
            self.add_dialog.focus_force()
            return
        self.add_dialog = AddDialog(self)

    def space(self, inv_key: int) -> tuple[int, int | None]:
        inv = self.invs.get(inv_key)
        if inv is None:
            return 0, None
        return len(inv.items), inv.capacity(self.db)

    def present_count(self, item_key: int) -> int:
        return sum(1 for it in self.items.values() if it["key"] == item_key)

    def add_item(self, item_key: int, count: int) -> str | None:
        """Adds the item to the working blob; returns an error message or None."""
        if (self.db.get(item_key, {}).get("maxStack") or 1) == 1:
            unsaved = sum(1 for it in self.items.values()
                          if it["key"] == item_key and it["itemNo"] in self.added)
            if unsaved >= MAX_UNSAVED_COPIES:
                return (f"Already {unsaved} unsaved copies of this item. Save, then load the game and let it "
                        "save once before adding more (mass additions have broken saves).")
        try:
            new_blob, info = cd_inventory.add_item(self._blob_with_pending(), item_key, count)
        except Exception as e:
            return str(e) or type(e).__name__
        self.added.add(info["itemNo"])
        self._set_blob(new_blob)
        new = next((it for it in self.items.values() if it["itemNo"] == info["itemNo"]), None)
        if new and self.tree.exists(str(new["id"])):
            self.tree.selection_set(str(new["id"]))
            self.tree.see(str(new["id"]))
        return None

    # ------------------------------------------------------------ deletion

    def _remove(self):
        sel = self._selected()
        if not sel or not self.invs:
            return
        elements = {el.start: el for inv in self.invs.values() for el in inv.items}
        lines, warn = [], False
        for it in sel:
            name = self.names.get(it["key"], {}).get("name", str(it["key"]))
            el = elements.get(it["header"])
            refs = cd_inventory.references(self.blob, it["itemNo"], el.start, el.end) if el else 0
            lines.append(f"• {name} ×{fmt(self._qty(it['id']))}" + ("  ⚠ referenced elsewhere" if refs else ""))
            warn |= bool(refs)
        msg = "Remove from inventory:\n\n" + "\n".join(lines[:15])
        if len(lines) > 15:
            msg += f"\n… and {len(lines) - 15} more"
        if warn:
            msg += ("\n\n⚠ Some items are referenced elsewhere in the save "
                    "(shortcut, reservation…). The game might not handle it well.")
        if not messagebox.askyesno("Delete", msg, icon="warning"):
            return

        blob = self._blob_with_pending()
        done = []
        for it in sel:
            try:
                invs = cd_inventory.read_inventories(cd_inventory.Decoder(blob))
                start = next(el.start for inv in invs.values() for el in inv.items
                             if "_itemNo" in el.fields
                             and struct.unpack_from("<Q", blob, el.fields["_itemNo"][0])[0] == it["itemNo"])
                blob, _ = cd_inventory.remove_item(blob, start)
            except Exception as e:
                name = self.names.get(it["key"], {}).get("name", str(it["key"]))
                messagebox.showerror("Cannot delete", f"{name}: {e or 'not found'}")
                break
            done.append(it)
        if not done:
            return
        for it in done:
            if it["itemNo"] in self.added:      # deleting an unsaved addition = undoing it
                self.added.discard(it["itemNo"])
            else:
                self.removed.append(self.names.get(it["key"], {}).get("name", str(it["key"])))
        self._set_blob(blob)

    # ------------------------------------------------------------ saving

    def _revert(self):
        if self.dirty() and messagebox.askyesno("Revert all", "Revert all unsaved changes?"):
            self._load(ask=False)

    def _update_status(self):
        parts = [f"{len(self.items)} items"]
        changes = []
        if self.added:
            changes.append(f"{len(self.added)} addition(s)")
        if self.removed:
            changes.append(f"{len(self.removed)} deletion(s)")
        n = len(self.changed())
        if n:
            changes.append(f"{n} quantity change(s)")
        if self.props_changed():
            changes.append(f"{len(self.props_changed())} enchant/durability change(s)")
        if self.skills_changed():
            changes.append(f"skill points {self.skills_orig.current} → {self.skills.current}")
        if changes:
            parts.append("●  " + ", ".join(changes) + " — unsaved")
            self.status.configure(style="Danger.TLabel")
        else:
            parts.append("✓ no pending changes")
            self.status.configure(style="TLabel")
        self.status["text"] = "    ".join(parts)
        state = "normal" if changes else "disabled"
        self.save_btn["state"] = state
        self.revert_btn["state"] = state
        self.title(("● " if changes else "") + "Crimson Desert – Save Editor")

    def _save(self):
        if not self.dirty() or not self.path:
            return
        if game_running() and not messagebox.askyesno(
                "Game running",
                "Crimson Desert is running: it may overwrite the save.\n"
                "Close the game before saving.\n\nSave anyway?", icon="warning"):
            return
        try:
            backup = cd_items.write_save(self.path, self.header, self._blob_with_pending())
        except (cd_items.SaveError, OSError) as e:
            messagebox.showerror("Write failed", str(e))
            return
        summary = (f"{len(self.added)} item(s) added, {len(self.removed)} deleted, "
                   f"{len(self.changed())} quantity change(s), "
                   f"{len(self.props_changed())} enchant/durability change(s).")
        if self.skills_changed():
            summary += (f"\nKliff's skill points: {self.skills_orig.current} → "
                        f"{self.skills.current} available.")
        self._load(ask=False)
        messagebox.showinfo("Saved", f"{summary}\n\nBackup of the original:\n{backup}")

    def _tick_game_check(self):
        if game_running():
            if not self.banner.winfo_ismapped():
                self.banner.pack(fill="x", padx=px(16), pady=(0, px(6)), before=self.body)
        elif self.banner.winfo_ismapped():
            self.banner.pack_forget()
        self.after(5000, self._tick_game_check)

    def _quit(self):
        if self.dirty() and not messagebox.askyesno("Quit", "There are unsaved changes. Quit anyway?"):
            return
        self.destroy()


class AddDialog(tk.Toplevel):
    """List of all game items, search, quantity, check of the remaining space."""

    MAX_ROWS = 600

    def __init__(self, app: App):
        super().__init__(app)
        self.app = app
        self.title("Add item")
        self.geometry(f"{min(px(880), app.winfo_screenwidth() - 80)}x{min(px(600), app.winfo_screenheight() - 120)}")
        self.minsize(px(680), px(430))
        self.transient(app)
        self.protocol("WM_DELETE_WINDOW", self._close)
        self.bind("<Escape>", lambda e: self._close())

        wrap = ttk.Frame(self, padding=px(16))
        wrap.pack(fill="both", expand=True)
        ttk.Label(wrap, text="Add item", style="Big.TLabel").pack(anchor="w")
        ttk.Label(wrap, text="Double-click or press Enter to add. The item is placed in the first free slot.",
                  style="Muted.TLabel").pack(anchor="w", pady=(2, 10))

        top = ttk.Frame(wrap)
        top.pack(fill="x")
        self.search = tk.StringVar()
        self.search.trace_add("write", lambda *a: self._schedule())
        self.search_entry = PlaceholderEntry(top, "Search by name or itemKey…", self.search)
        self.search_entry.pack(side="left", fill="x", expand=True)
        cats = sorted({v.get("category") or "?" for v in app.db.values()})
        self.cat = tk.StringVar(value="All categories")
        cb = ttk.Combobox(top, textvariable=self.cat, values=["All categories"] + cats,
                          state="readonly", width=18)
        cb.pack(side="left", padx=8)
        cb.bind("<<ComboboxSelected>>", lambda e: self._fill())
        self.hide_present = tk.BooleanVar(value=True)
        ttk.Checkbutton(top, text="Hide owned items", variable=self.hide_present,
                        command=self._fill).pack(side="left")

        mid = ttk.Frame(wrap)
        mid.pack(fill="both", expand=True, pady=10)
        cols = ("name", "key", "cat", "max", "dest", "have")
        self.tree = ttk.Treeview(mid, columns=cols, show="headings", selectmode="browse")
        for col, label, w in (("name", "Item", 280), ("key", "itemKey", 85), ("cat", "Category", 105),
                              ("max", "Max stack", 80), ("dest", "Destination", 145), ("have", "Owned", 75)):
            self.tree.heading(col, text=label)
            self.tree.column(col, width=px(w), anchor="w" if col in ("name", "cat", "dest") else "center")
        responsive_columns(self.tree, {"name": 3.0, "key": 1.1, "cat": 1.2, "max": 0.9, "dest": 1.8, "have": 0.8})
        sb = ttk.Scrollbar(mid, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=sb.set)
        self.tree.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")
        self.tree.bind("<<TreeviewSelect>>", lambda e: (sync_sel_tag(self.tree), self._on_select()))
        self.tree.bind("<Double-1>", lambda e: self._add())
        self.tree.bind("<Return>", lambda e: self._add())
        self.restyle()

        # bottom: remaining space + quantity + add
        bottom = ttk.Frame(wrap)
        bottom.pack(fill="x")
        left = ttk.Frame(bottom)
        left.pack(side="left", fill="x", expand=True)
        self.sel_title = ttk.Label(left, text="Select an item", style="Big.TLabel")
        self.sel_title.pack(anchor="w")
        self.info = ttk.Label(left, text="", style="Muted.TLabel", wraplength=px(460), justify="left")
        self.info.pack(anchor="w")
        self.space_bar = ttk.Progressbar(left, length=px(260))
        self.space_bar.pack(anchor="w", pady=(4, 0))

        right = ttk.Frame(bottom)
        right.pack(side="right")
        ttk.Label(right, text="Quantity").pack(side="left", padx=(0, 6))
        self.qty = tk.StringVar(value="1")
        self.qty_box = ttk.Spinbox(right, from_=1, to=1, textvariable=self.qty, width=9, justify="right")
        self.qty_box.pack(side="left")
        self.add_btn = ttk.Button(right, text="Add", style="Accent.TButton", command=self._add,
                                  state="disabled")
        self.add_btn.pack(side="left", padx=(8, 0))
        ttk.Button(right, text="Close", command=self._close).pack(side="left", padx=(8, 0))

        self.count_label = ttk.Label(wrap, text="", style="Muted.TLabel")
        self.count_label.pack(anchor="w", pady=(8, 0))

        self._after = None
        self._fill()
        self._on_select()
        self.search_entry.focus_set()

    def restyle(self):
        self.tree.tag_configure("stripe", background=self.app.c("stripe"))
        self.tree.tag_configure("owned", foreground=self.app.c("muted"))
        configure_sel_tag(self.tree, COLORS[self.app.theme])

    def _schedule(self):
        if self._after:
            self.after_cancel(self._after)
        self._after = self.after(200, self._fill)

    def refresh(self):
        self._fill()
        self._on_select()

    def _dest(self, key):
        return cd_inventory.destination(key, self.app.db)

    def _fill(self):
        self._after = None
        q = self.search.get().strip().lower()
        cat = self.cat.get()
        have = {}
        for it in self.app.items.values():
            have[it["key"]] = have.get(it["key"], 0) + 1
        rows = []
        for key, v in self.app.db.items():
            name = v.get("name") or "?"
            if q and q not in name.lower() and q != str(key):
                continue
            if cat != "All categories" and (v.get("category") or "?") != cat:
                continue
            if self.hide_present.get() and key in have:
                continue
            if cd_inventory.blocked(v) or self._dest(key) is None:
                continue
            rows.append((name.lower(), key, v))
        rows.sort()
        sel = self.tree.selection()
        self.tree.delete(*self.tree.get_children())
        for i, (_, key, v) in enumerate(rows[:self.MAX_ROWS]):
            m = v.get("maxStack") or 1
            tags = ("owned",) if key in have else (("stripe",) if i % 2 else ())
            self.tree.insert("", "end", iid=str(key), tags=tags, values=(
                v.get("name"), key, v.get("category") or "?", "∞" if m >= INFINITE else fmt(m),
                inventory_label(self._dest(key)), f"×{have[key]}" if key in have else ""))
        more = f" — showing only {self.MAX_ROWS}, refine your search" if len(rows) > self.MAX_ROWS else ""
        self.count_label["text"] = f"{fmt(len(rows))} item(s){more}"
        if sel and self.tree.exists(sel[0]):
            self.tree.selection_set(sel[0])
        sync_sel_tag(self.tree)

    def _selected_key(self):
        sel = self.tree.selection()
        return int(sel[0]) if sel else None

    def _on_select(self):
        key = self._selected_key()
        if key is None:
            self.add_btn["state"] = "disabled"
            self.sel_title["text"] = "Select an item"
            used, cap = self.app.space(cd_inventory.BAG)
            self.info["text"] = (f"{inventory_label(cd_inventory.BAG)}: {used}/{cap} slots used." if cap
                                 else f"{inventory_label(cd_inventory.BAG)}: unknown capacity.")
            self.info.configure(style="Muted.TLabel")
            self.space_bar.configure(maximum=cap or 1, value=used)
            return
        v = self.app.db[key]
        m = v.get("maxStack") or 1
        self.qty_box["to"] = min(m, 10**12)
        try:
            if int(self.qty.get()) > m:
                self.qty.set(str(m))
        except ValueError:
            self.qty.set("1")
        dest = self._dest(key)
        used, cap = self.app.space(dest)
        have = self.app.present_count(key)
        ok = True
        if cap is None:
            msg, ok = f"{inventory_label(dest)}: unknown capacity, cannot add.", False
        elif used >= cap:
            msg, ok = f"{inventory_label(dest)} full ({used}/{cap}).", False
        else:
            msg = f"{inventory_label(dest)}: {used}/{cap} slots used — {cap - used} free."
        if have and m > 1:
            msg += "\nAlready owned: change its quantity in the main window."
            ok = False
        elif have:
            msg += f"\nAlready owned ×{have} (not stackable: one more copy will be created)."
        self.sel_title["text"] = v.get("name")
        self.info["text"] = msg
        self.info.configure(style="Muted.TLabel" if ok else "Danger.TLabel")
        self.space_bar.configure(maximum=cap or 1, value=used if cap else 0)
        self.add_btn["state"] = "normal" if ok else "disabled"

    def _add(self):
        key = self._selected_key()
        if key is None or str(self.add_btn["state"]) == "disabled":
            return
        try:
            n = int(self.qty.get())
        except ValueError:
            messagebox.showerror("Quantity", "Invalid quantity.", parent=self)
            return
        err = self.app.add_item(key, n)
        if err:
            messagebox.showerror("Cannot add", err, parent=self)
        else:
            self.sel_title["text"] = f"✓ {self.app.db[key]['name']} ×{fmt(n)} added"
            self.info["text"] = "Remember to save in the main window (Ctrl+S)."
            self.info.configure(style="Ok.TLabel")

    def _close(self):
        self.app.add_dialog = None
        self.destroy()


def _mtime(p: Path) -> str:
    return time.strftime("%d/%m %H:%M", time.localtime(p.stat().st_mtime))


if __name__ == "__main__":
    App().mainloop()
