"""
gui/app.py — Interface Gráfica Moderna e Profissional (GUI) do DiskRescue v0.3.3.
Construída com Design System Fluent Obsidian, Motor Vetorial Anti-Aliased via Pillow (4x Supersampling),
Suporte High-DPI Per-Monitor v2, Barra de Título Escura Nativa DWM do Windows 11 e
Motor de Diagnóstico Completo em 1 Clique (SMART ATA/NVMe + Geometria GPT/MBR + Boot Sector/$MFT + Superfície).
"""
from __future__ import annotations

import ctypes
import math
import os
import queue
import subprocess
import sys
import threading
import time
import tkinter as tk
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from tkinter import filedialog, messagebox, ttk
from typing import Optional

# Garante acesso aos módulos core e pacotes irmãos
AQUI = Path(__file__).resolve().parents[1]
if str(AQUI) not in sys.path:
    sys.path.insert(0, str(AQUI))

try:
    import sv_ttk
    HAS_SV_TTK = True
except ImportError:
    HAS_SV_TTK = False

try:
    from PIL import Image, ImageDraw, ImageTk
    HAS_PIL = True
except ImportError:
    HAS_PIL = False

from core.filesystem.ntfs_boot_repair import (
    diagnose_ntfs_boot_sectors,
    restore_ntfs_boot_from_backup,
)
from core.filesystem.scanner import DiscoveredFilesystem, scan_boot_sectors
from core.hardware.device_resolver import (
    assert_safe_source_and_destination,
    get_device_sector_size,
    is_physical_device_path,
)
from core.hardware.scanner import DiskInfo, is_admin, relaunch_as_admin, scan_disks
from core.hardware.smart import SmartReport, SmartRiskLevel, query_smart_report_windows
from core.hardware.surface_scan import SurfaceBlockResult, SurfaceScanSummary, scan_surface_latency
from core.partition.gpt import GPTPartitionEntry, GPTTable, parse_gpt
from core.partition.mbr import parse_mbr
from core.recovery.carver import CarvedFile, FileCarver
from core.recovery.repair import repair_gpt_partition_offset
from core.recovery.resilient_clone import CloningResult, ResilientCloneStats, ResilientCloner


# ── PALETAS DE DESIGN FLUENT OBSIDIAN / SLATE ────────────────────────────────
PALETTES: dict[str, dict[str, str]] = {
    "dark": {
        "bg_root": "#070B14",
        "bg_header": "#0B1120",
        "bg_sidebar": "#090E1A",
        "bg_card": "#111827",
        "bg_card_alt": "#162033",
        "bg_hero": "#0F1E36",
        "bg_input": "#060911",
        "border": "#1E293B",
        "border_subtle": "#172033",
        "border_accent": "#0EA5E9",
        "accent": "#0EA5E9",
        "accent_hover": "#38BDF8",
        "accent_soft_bg": "#0C2D48",
        "accent_soft_fg": "#38BDF8",
        "text_primary": "#F8FAFC",
        "text_secondary": "#94A3B8",
        "text_muted": "#64748B",
        "success_bg": "#064E3B",
        "success_fg": "#34D399",
        "warning_bg": "#78350F",
        "warning_fg": "#FBBF24",
        "danger_bg": "#7F1D1D",
        "danger_fg": "#F87171",
        "neutral_bg": "#1E293B",
        "neutral_fg": "#CBD5E1",
        "tree_bg": "#0B1120",
        "tree_alt": "#111827",
        "tree_head_bg": "#162033",
    },
    "light": {
        "bg_root": "#F1F5F9",
        "bg_header": "#FFFFFF",
        "bg_sidebar": "#F8FAFC",
        "bg_card": "#FFFFFF",
        "bg_card_alt": "#F8FAFC",
        "bg_hero": "#E0F2FE",
        "bg_input": "#F8FAFC",
        "border": "#CBD5E1",
        "border_subtle": "#E2E8F0",
        "border_accent": "#0284C7",
        "accent": "#0284C7",
        "accent_hover": "#0369A1",
        "accent_soft_bg": "#E0F2FE",
        "accent_soft_fg": "#0369A1",
        "text_primary": "#0F172A",
        "text_secondary": "#475569",
        "text_muted": "#64748B",
        "success_bg": "#DCFCE7",
        "success_fg": "#15803D",
        "warning_bg": "#FEF3C7",
        "warning_fg": "#B45309",
        "danger_bg": "#FEE2E2",
        "danger_fg": "#B91C1C",
        "neutral_bg": "#E2E8F0",
        "neutral_fg": "#334155",
        "tree_bg": "#FFFFFF",
        "tree_alt": "#F8FAFC",
        "tree_head_bg": "#F1F5F9",
    },
}


def _enable_high_dpi() -> None:
    """Ativa High-DPI Per-Monitor v2 no Windows para fontes e controles ultra nítidos."""
    if os.name != "nt":
        return
    try:
        ctypes.windll.user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))
        return
    except Exception:
        pass
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
        return
    except Exception:
        pass
    try:
        ctypes.windll.user32.SetProcessDPIAware()
    except Exception:
        pass


def _apply_windows_dwm_theme(root: tk.Tk, dark: bool = True) -> None:
    """Aplica barra de título escura nativa do Windows 11/10 (Immersive Dark Mode + Caption Color)."""
    if os.name != "nt":
        return
    try:
        hwnd = ctypes.windll.user32.GetParent(root.winfo_id())
        if not hwnd:
            hwnd = root.winfo_id()
        dwmapi = ctypes.windll.dwmapi

        val = ctypes.c_int(1 if dark else 0)
        res = dwmapi.DwmSetWindowAttribute(hwnd, 20, ctypes.byref(val), ctypes.sizeof(val))
        if res != 0:
            dwmapi.DwmSetWindowAttribute(hwnd, 19, ctypes.byref(val), ctypes.sizeof(val))

        caption_color = ctypes.c_int(0x0020110B if dark else 0x00FFFFFF)
        dwmapi.DwmSetWindowAttribute(hwnd, 35, ctypes.byref(caption_color), ctypes.sizeof(caption_color))
    except Exception:
        pass


# ── MOTOR VETORIAL ANTI-ALIASED (4X SUPERSAMPLING VIA PILLOW) ────────────────
class VectorFactory:
    """Gera ícones vetoriais coloridos anti-aliased e medidores circulares sem depender de emojis do Windows."""

    @staticmethod
    def create_nav_icon(kind: str, bg_hex: str, fg_hex: str = "#FFFFFF", size: int = 24) -> Optional[ImageTk.PhotoImage]:
        if not HAS_PIL:
            return None
        scale = 4
        s = size * scale
        img = Image.new("RGBA", (s, s), (0, 0, 0, 0))
        d = ImageDraw.Draw(img)

        # Squircle de fundo colorido
        d.rounded_rectangle((4, 4, s - 4, s - 4), radius=int(s * 0.28), fill=bg_hex)
        lw = max(6, int(s * 0.08))
        m = int(s * 0.26)

        if kind == "dashboard":
            # 4 quadrantes modernos
            mid = s // 2
            gap = int(s * 0.05)
            d.rounded_rectangle((m, m, mid - gap, mid - gap), radius=6, fill=fg_hex)
            d.rounded_rectangle((mid + gap, m, s - m, mid - gap), radius=6, fill=fg_hex)
            d.rounded_rectangle((m, mid + gap, mid - gap, s - m), radius=6, fill=fg_hex)
            d.rounded_rectangle((mid + gap, mid + gap, s - m, s - m), radius=6, fill=fg_hex)
        elif kind == "diagnostic":
            # Lupa de inspeção
            r = int(s * 0.16)
            cx, cy = int(s * 0.44), int(s * 0.44)
            d.ellipse((cx - r, cy - r, cx + r, cy + r), outline=fg_hex, width=lw)
            d.line((cx + int(r * 0.7), cy + int(r * 0.7), s - m, s - m), fill=fg_hex, width=lw + 2)
        elif kind == "smart":
            # Linha de pulso / ECG
            pts = [
                (m, s // 2),
                (int(s * 0.38), s // 2),
                (int(s * 0.45), int(s * 0.28)),
                (int(s * 0.55), int(s * 0.72)),
                (int(s * 0.62), s // 2),
                (s - m, s // 2),
            ]
            d.line(pts, fill=fg_hex, width=lw, joint="curve")
        elif kind == "clone":
            # Dois discos sobrepostos
            d.rounded_rectangle((m, m, int(s * 0.62), int(s * 0.62)), radius=8, outline=fg_hex, width=lw)
            d.rounded_rectangle((int(s * 0.38), int(s * 0.38), s - m, s - m), radius=8, fill=fg_hex)
        elif kind == "carver":
            # Camadas forenses
            mid_x = s // 2
            d.polygon([(mid_x, m), (s - m, int(s * 0.44)), (mid_x, int(s * 0.58)), (m, int(s * 0.44))], fill=fg_hex)
            d.line([(m, int(s * 0.62)), (mid_x, int(s * 0.76)), (s - m, int(s * 0.62))], fill=fg_hex, width=lw)
        elif kind == "surface":
            # Grade de blocos de calor
            cell = (s - 2 * m - 8) // 2
            d.rectangle((m, m, m + cell, m + cell), fill=fg_hex)
            d.rectangle((m + cell + 8, m, s - m, m + cell), fill="#FBBF24")
            d.rectangle((m, m + cell + 8, m + cell, s - m), fill="#34D399")
            d.rectangle((m + cell + 8, m + cell + 8, s - m, s - m), fill="#F87171")
        elif kind == "logs":
            # Prompt de terminal >_
            d.line([(m, int(s * 0.36)), (int(s * 0.46), s // 2), (m, int(s * 0.64))], fill=fg_hex, width=lw)
            d.line([(int(s * 0.52), int(s * 0.64)), (s - m, int(s * 0.64))], fill=fg_hex, width=lw)
        elif kind == "bolt":
            # Raio de ação 1-clique
            pts = [
                (int(s * 0.56), int(s * 0.18)),
                (int(s * 0.28), int(s * 0.54)),
                (int(s * 0.48), int(s * 0.54)),
                (int(s * 0.42), int(s * 0.82)),
                (int(s * 0.72), int(s * 0.46)),
                (int(s * 0.52), int(s * 0.46)),
            ]
            d.polygon(pts, fill=fg_hex)

        resample = getattr(Image, "Resampling", Image).LANCZOS
        out = img.resize((size, size), resample)
        return ImageTk.PhotoImage(out)

    @staticmethod
    def create_gauge_image(
        score: Optional[int],
        ring_color: str,
        bg_card_hex: str,
        track_hex: str = "#1E293B",
        size: int = 128,
    ) -> Optional[ImageTk.PhotoImage]:
        """Desenha um anel medidor de saúde (Health Gauge) ultra nítido com 4x supersampling."""
        if not HAS_PIL:
            return None
        scale = 4
        s = size * scale
        img = Image.new("RGBA", (s, s), bg_card_hex)
        d = ImageDraw.Draw(img)

        pad = int(s * 0.08)
        width = int(s * 0.10)
        bbox = (pad, pad, s - pad, s - pad)

        # Trilho de fundo (270 graus, de 135 a 405)
        d.arc(bbox, start=135, end=405, fill=track_hex, width=width)

        # Arco de pontuação
        if score is not None and score > 0:
            sweep = int(270 * (max(0, min(100, score)) / 100.0))
            d.arc(bbox, start=135, end=135 + sweep, fill=ring_color, width=width)
        elif score is None:
            # Arco tracejado de aguardo/indeterminado
            d.arc(bbox, start=135, end=270, fill=ring_color, width=width)

        resample = getattr(Image, "Resampling", Image).LANCZOS
        out = img.resize((size, size), resample)
        return ImageTk.PhotoImage(out)


class OperationState(Enum):
    IDLE = "idle"
    RUNNING = "running"
    CANCELLING = "cancelling"
    COMPLETED = "completed"
    PARTIAL = "partial"
    FAILED = "failed"
    CANCELLED = "cancelled"


class DiskRescueApp:
    def __init__(self, root: tk.Tk, auto_refresh: bool = True):
        self.root = root
        self.root.title("DiskRescue v0.3.3 — Diagnóstico Completo 1-Clique, Clonagem Resiliente & Recuperação Forense")
        self.root.geometry("1320x860")
        self.root.minsize(1120, 760)

        # Coleções para atualização de tema
        self._themed_cards: list[tk.Widget] = []
        self._themed_card_alts: list[tk.Widget] = []
        self._themed_dividers: list[tk.Widget] = []
        self._themed_text_widgets: list[tk.Text] = []
        self._icon_refs: dict[str, ImageTk.PhotoImage] = {}
        self._gauge_photo: Optional[ImageTk.PhotoImage] = None

        # Tema e Estilos
        self.current_theme = "dark"
        if HAS_SV_TTK:
            sv_ttk.set_theme("dark")

        # Configura ícone da janela e barra DWM
        self._setup_window_icons()
        self._configure_custom_styles()
        self.root.configure(bg=self.palette["bg_root"])
        _apply_windows_dwm_theme(self.root, dark=True)

        # Estado da Aplicação
        self.op_state: OperationState = OperationState.IDLE
        self.disks: list[DiskInfo] = []
        self.selected_disk: Optional[DiskInfo] = None
        self.is_scanning = False
        self.is_surface_scanning = False
        self.is_repairing = False
        self.is_running = True
        self._closing_requested = False
        self.closing_requested = False
        self.msg_queue: queue.Queue = queue.Queue()

        # Controladores de cancelamento
        self.cancel_scan_flag = False
        self.cancel_clone_flag = False
        self.cancel_carver_flag = False
        self.cancel_surface_flag = False
        self.cloner_instance: Optional[ResilientCloner] = None
        self.carver_instance: Optional[FileCarver] = None

        # Dados em Memória da Análise Atual
        self.current_gpt = None
        self.current_mbr = None
        self.current_filesystems: list[DiscoveredFilesystem] = []
        self.current_smart_report: Optional[SmartReport] = None
        self.misaligned_pair = None
        self.ntfs_boot_diag = None
        self.ntfs_target_partition: Optional[tuple[int, int]] = None

        # Protocolo de Encerramento Gracioso da Janela
        self.root.protocol("WM_DELETE_WINDOW", self._on_close_window)

        # Navegação
        self.views: dict[str, tk.Frame] = {}
        self.nav_buttons: dict[str, tk.Widget] = {}
        self.active_view = "dashboard"

        # Construção da Interface
        self._build_ui()
        self._start_queue_poller()
        if auto_refresh:
            self.refresh_disks()

    @property
    def palette(self) -> dict[str, str]:
        return PALETTES[self.current_theme]

    # ── ÍCONES & ATIVOS ────────────────────────────────────────────────────────
    def _setup_window_icons(self):
        if getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS"):
            base_dir = Path(sys._MEIPASS)
        else:
            base_dir = Path(__file__).resolve().parent

        assets_dir = base_dir / "assets"
        if not assets_dir.exists():
            assets_dir = base_dir / "gui" / "assets"

        ico_path = assets_dir / "diskrescue.ico"
        png_path = assets_dir / "diskrescue.png"

        self.header_logo_photo = None
        try:
            if ico_path.exists():
                self.root.iconbitmap(str(ico_path))
        except Exception:
            pass

        try:
            if png_path.exists() and HAS_PIL:
                img = Image.open(png_path)
                self.app_icon_photo = ImageTk.PhotoImage(img)
                self.root.iconphoto(True, self.app_icon_photo)
                resample = getattr(Image, "Resampling", Image).LANCZOS
                header_img = img.resize((38, 38), resample)
                self.header_logo_photo = ImageTk.PhotoImage(header_img)
        except Exception:
            pass

        # Pré-gera ícones vetoriais coloridos anti-aliased
        nav_specs = [
            ("dashboard", "#0EA5E9"),
            ("diagnostic", "#8B5CF6"),
            ("smart", "#10B981"),
            ("clone", "#3B82F6"),
            ("carver", "#F59E0B"),
            ("surface", "#EC4899"),
            ("logs", "#64748B"),
            ("bolt", "#0284C7"),
        ]
        for key, color in nav_specs:
            ic = VectorFactory.create_nav_icon(key, color, "#FFFFFF", size=22)
            if ic is not None:
                self._icon_refs[key] = ic

    # ── SISTEMA DE ESTILOS TTK CUSTOMIZADO ────────────────────────────────────
    def _configure_custom_styles(self):
        p = self.palette
        style = ttk.Style(self.root)

        style.configure("Root.TFrame", background=p["bg_root"])
        style.configure("Header.TFrame", background=p["bg_header"])
        style.configure("Sidebar.TFrame", background=p["bg_sidebar"])
        style.configure("Card.TFrame", background=p["bg_card"])
        style.configure("CardAlt.TFrame", background=p["bg_card_alt"])
        style.configure("Hero.TFrame", background=p["bg_hero"])

        style.configure("HeaderTitle.TLabel", background=p["bg_header"], foreground=p["text_primary"], font=("Segoe UI Semibold", 15, "bold"))
        style.configure("HeaderSub.TLabel", background=p["bg_header"], foreground=p["text_secondary"], font=("Segoe UI", 8))
        style.configure("SidebarSection.TLabel", background=p["bg_sidebar"], foreground=p["text_muted"], font=("Segoe UI Semibold", 8, "bold"))
        style.configure("CardTitle.TLabel", background=p["bg_card"], foreground=p["text_primary"], font=("Segoe UI Semibold", 9, "bold"))
        style.configure("CardSub.TLabel", background=p["bg_card"], foreground=p["text_secondary"], font=("Segoe UI", 8))
        style.configure("CardBody.TLabel", background=p["bg_card"], foreground=p["text_primary"], font=("Segoe UI", 9))
        style.configure("CardMuted.TLabel", background=p["bg_card"], foreground=p["text_secondary"], font=("Segoe UI", 8))
        style.configure("AltTitle.TLabel", background=p["bg_card_alt"], foreground=p["text_primary"], font=("Segoe UI Semibold", 9, "bold"))
        style.configure("AltMuted.TLabel", background=p["bg_card_alt"], foreground=p["text_secondary"], font=("Segoe UI", 8))
        style.configure("AltMetric.TLabel", background=p["bg_card_alt"], foreground=p["text_primary"], font=("Segoe UI Semibold", 13, "bold"))
        style.configure("ViewTitle.TLabel", background=p["bg_root"], foreground=p["text_primary"], font=("Segoe UI Semibold", 13, "bold"))
        style.configure("ViewSub.TLabel", background=p["bg_root"], foreground=p["text_secondary"], font=("Segoe UI", 9))
        style.configure("Status.TLabel", background=p["bg_header"], foreground=p["text_secondary"], font=("Segoe UI", 9))

        style.configure(
            "Nav.TButton",
            font=("Segoe UI Semibold", 9),
            anchor="w",
            padding=(10, 8),
        )
        style.configure(
            "Accent.TButton",
            font=("Segoe UI Semibold", 9, "bold"),
            padding=(12, 8),
        )

        style.configure(
            "Treeview",
            background=p["tree_bg"],
            fieldbackground=p["tree_bg"],
            foreground=p["text_primary"],
            rowheight=28,
            font=("Segoe UI", 9),
            borderwidth=0,
        )
        style.configure(
            "Treeview.Heading",
            background=p["tree_head_bg"],
            foreground=p["text_secondary"],
            font=("Segoe UI Semibold", 9, "bold"),
            padding=(8, 6),
            borderwidth=0,
        )
        style.map(
            "Treeview",
            background=[("selected", p["accent_soft_bg"])],
            foreground=[("selected", p["accent_soft_fg"])],
        )

    # ── COMPONENTES VISUAIS REUTILIZÁVEIS ─────────────────────────────────────
    def _make_card(
        self,
        parent: tk.Widget,
        title: Optional[str] = None,
        subtitle: Optional[str] = None,
        badge_text: Optional[str] = None,
        padding: tuple[int, int] = (14, 10),
        alt: bool = False,
        accent_top: Optional[str] = None,
    ) -> tuple[tk.Frame, ttk.Frame]:
        p = self.palette
        bg_color = p["bg_card_alt"] if alt else p["bg_card"]
        style_name = "CardAlt.TFrame" if alt else "Card.TFrame"
        title_style = "AltTitle.TLabel" if alt else "CardTitle.TLabel"
        sub_style = "AltMuted.TLabel" if alt else "CardSub.TLabel"

        outer = tk.Frame(
            parent,
            bg=bg_color,
            highlightbackground=p["border"],
            highlightcolor=p["border_accent"],
            highlightthickness=1,
            bd=0,
        )
        if alt:
            self._themed_card_alts.append(outer)
        else:
            self._themed_cards.append(outer)

        if accent_top:
            stripe = tk.Frame(outer, bg=accent_top, height=3, bd=0, highlightthickness=0)
            stripe.pack(fill=tk.X, side=tk.TOP)

        inner = ttk.Frame(outer, style=style_name, padding=padding)
        inner.pack(fill=tk.BOTH, expand=True)

        if title:
            hdr = ttk.Frame(inner, style=style_name)
            hdr.pack(fill=tk.X, pady=(0, 6))

            lbl_t = ttk.Label(hdr, text=title, style=title_style)
            lbl_t.pack(side=tk.LEFT, anchor=tk.W)

            if badge_text:
                lbl_b = ttk.Label(
                    hdr,
                    text=f" {badge_text} ",
                    font=("Segoe UI Semibold", 8, "bold"),
                    background=p["accent_soft_bg"],
                    foreground=p["accent_soft_fg"],
                    padding=(6, 1),
                )
                lbl_b.pack(side=tk.RIGHT, anchor=tk.E)

            if subtitle:
                lbl_s = ttk.Label(inner, text=subtitle, style=sub_style, wraplength=900)
                lbl_s.pack(anchor=tk.W, pady=(0, 6))

        body = ttk.Frame(inner, style=style_name)
        body.pack(fill=tk.BOTH, expand=True)
        return outer, body

    def _make_styled_text(self, parent: tk.Widget, wrap: str = tk.NONE) -> tuple[tk.Frame, tk.Text]:
        p = self.palette
        wrapper = tk.Frame(
            parent,
            bg=p["bg_input"],
            highlightbackground=p["border"],
            highlightcolor=p["border_accent"],
            highlightthickness=1,
            bd=0,
        )
        self._themed_dividers.append(wrapper)

        txt = tk.Text(
            wrapper,
            font=("Consolas", 9),
            wrap=wrap,
            bg=p["bg_input"],
            fg=p["text_primary"],
            insertbackground=p["accent"],
            selectbackground=p["accent_soft_bg"],
            selectforeground=p["text_primary"],
            relief="flat",
            bd=0,
            highlightthickness=0,
            padx=12,
            pady=10,
        )
        scroll = ttk.Scrollbar(wrapper, orient=tk.VERTICAL, command=txt.yview)
        txt.configure(yscrollcommand=scroll.set)
        txt.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        scroll.pack(side=tk.RIGHT, fill=tk.Y)

        self._themed_text_widgets.append(txt)
        return wrapper, txt

    # ── CONSTRUÇÃO DA INTERFACE PRINCIPAL ──────────────────────────────────────
    def _build_ui(self):
        p = self.palette

        # 1. Header Superior
        self._build_top_header()

        div_top = tk.Frame(self.root, bg=p["border"], height=1, bd=0, highlightthickness=0)
        div_top.pack(fill=tk.X)
        self._themed_dividers.append(div_top)

        # 2. Container Central Dividido
        self.body_container = ttk.Frame(self.root, style="Root.TFrame")
        self.body_container.pack(fill=tk.BOTH, expand=True)

        # Sidebar à Esquerda
        self._build_sidebar()

        div_vert = tk.Frame(self.body_container, bg=p["border"], width=1, bd=0, highlightthickness=0)
        div_vert.pack(side=tk.LEFT, fill=tk.Y)
        self._themed_dividers.append(div_vert)

        # Área de Conteúdo à Direita
        self.content_area = ttk.Frame(self.body_container, style="Root.TFrame", padding=(16, 12))
        self.content_area.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

        # Banner de Seleção de Disco + Botão 1-Clique Global
        self._build_drive_selector_banner()

        # Container onde as telas/views alternam
        self.view_container = ttk.Frame(self.content_area, style="Root.TFrame")
        self.view_container.pack(fill=tk.BOTH, expand=True, pady=(8, 0))

        # Construir todas as telas
        self._build_views()

        div_bot = tk.Frame(self.root, bg=p["border"], height=1, bd=0, highlightthickness=0)
        div_bot.pack(fill=tk.X, side=tk.BOTTOM)
        self._themed_dividers.append(div_bot)

        # 3. Barra de Status Inferior
        self._build_status_bar()

        # Exibir a tela inicial padrão (Dashboard)
        self._switch_view("dashboard")

    # ── HEADER SUPERIOR ───────────────────────────────────────────────────────
    def _build_top_header(self):
        p = self.palette
        self.header_frame = ttk.Frame(self.root, style="Header.TFrame", padding=(18, 10))
        self.header_frame.pack(fill=tk.X, side=tk.TOP)

        brand_box = ttk.Frame(self.header_frame, style="Header.TFrame")
        brand_box.pack(side=tk.LEFT, anchor=tk.CENTER)

        if self.header_logo_photo is not None:
            logo_lbl = ttk.Label(brand_box, image=self.header_logo_photo, style="HeaderTitle.TLabel")
            logo_lbl.pack(side=tk.LEFT, padx=(0, 12))

        text_stack = ttk.Frame(brand_box, style="Header.TFrame")
        text_stack.pack(side=tk.LEFT, anchor=tk.CENTER)

        title_row = ttk.Frame(text_stack, style="Header.TFrame")
        title_row.pack(anchor=tk.W)

        app_title = ttk.Label(title_row, text="DiskRescue", style="HeaderTitle.TLabel")
        app_title.pack(side=tk.LEFT, anchor=tk.CENTER)

        self.lbl_version_badge = ttk.Label(
            title_row,
            text=" v0.3.3 SUITE FORENSE ",
            font=("Segoe UI Semibold", 8, "bold"),
            background=p["accent_soft_bg"],
            foreground=p["accent_soft_fg"],
            padding=(6, 2),
        )
        self.lbl_version_badge.pack(side=tk.LEFT, padx=(10, 0), anchor=tk.CENTER)

        app_subtitle = ttk.Label(
            text_stack,
            text="Diagnóstico Profundo em 1-Clique • S.M.A.R.T. ATA/NVMe • Clonagem Resiliente • Reparo GPT/NTFS",
            style="HeaderSub.TLabel",
        )
        app_subtitle.pack(anchor=tk.W, pady=(1, 0))

        actions_box = ttk.Frame(self.header_frame, style="Header.TFrame")
        actions_box.pack(side=tk.RIGHT, anchor=tk.CENTER)

        elevated = is_admin()
        uac_bg = p["success_bg"] if elevated else p["warning_bg"]
        uac_fg = p["success_fg"] if elevated else p["warning_fg"]
        uac_text = "MODO ADMINISTRADOR (ACESSO RAW ATIVO)" if elevated else "MODO LEITURA (SEM UAC)"

        self.lbl_uac = ttk.Label(
            actions_box,
            text=uac_text,
            font=("Segoe UI Semibold", 8, "bold"),
            background=uac_bg,
            foreground=uac_fg,
            padding=(10, 5),
        )
        self.lbl_uac.pack(side=tk.LEFT, padx=(0, 10))

        if not elevated:
            self.btn_elevate = ttk.Button(
                actions_box,
                text="Elevar UAC",
                command=self._on_elevate,
                style="Accent.TButton",
                width=11,
            )
            self.btn_elevate.pack(side=tk.LEFT, padx=(0, 8))

        self.btn_refresh = ttk.Button(
            actions_box,
            text="Atualizar Discos",
            command=self.refresh_disks,
            width=15,
        )
        self.btn_refresh.pack(side=tk.LEFT, padx=(0, 8))

        if HAS_SV_TTK:
            self.btn_theme = ttk.Button(
                actions_box,
                text="Tema: Escuro",
                command=self._toggle_theme,
                width=12,
            )
            self.btn_theme.pack(side=tk.LEFT)

    # ── SIDEBAR DE NAVEGAÇÃO COM ÍCONES VETORIAIS COLORIDOS ───────────────────
    def _build_sidebar(self):
        p = self.palette
        self.sidebar_frame = ttk.Frame(self.body_container, style="Sidebar.TFrame", width=235, padding=(12, 14))
        self.sidebar_frame.pack(side=tk.LEFT, fill=tk.Y)
        self.sidebar_frame.pack_propagate(False)

        ttk.Label(
            self.sidebar_frame,
            text="PAINEL DE NAVEGAÇÃO",
            style="SidebarSection.TLabel",
        ).pack(anchor=tk.W, pady=(0, 8), padx=4)

        nav_items = [
            ("dashboard", "Central & Diagnóstico 1-Clique"),
            ("diagnostic", "Partições, GPT & Boot NTFS"),
            ("smart", "Sensores S.M.A.R.T. & Cabos"),
            ("clone", "Clonagem Resiliente (.img)"),
            ("carver", "File Carving Forense"),
            ("surface", "Mapa de Calor & Latência"),
            ("logs", "Console de Auditoria"),
        ]

        for key, label in nav_items:
            ic = self._icon_refs.get(key)
            btn_kwargs = {
                "text": f"  {label}" if ic else label,
                "command": lambda k=key: self._switch_view(k),
                "style": "Nav.TButton",
            }
            if ic is not None:
                btn_kwargs["image"] = ic
                btn_kwargs["compound"] = tk.LEFT
            btn = ttk.Button(self.sidebar_frame, **btn_kwargs)
            btn.pack(fill=tk.X, pady=3)
            self.nav_buttons[key] = btn

        # Card de Resumo do Disco no Rodapé da Sidebar
        self.mini_drive_outer, self.mini_drive_box = self._make_card(
            self.sidebar_frame,
            title="DISCO SELECIONADO",
            padding=(12, 10),
            alt=True,
            accent_top=p["accent"],
        )
        self.mini_drive_outer.pack(side=tk.BOTTOM, fill=tk.X, pady=(12, 0))

        self.lbl_mini_model = ttk.Label(
            self.mini_drive_box,
            text="Nenhum selecionado",
            style="AltTitle.TLabel",
            wraplength=190,
        )
        self.lbl_mini_model.pack(anchor=tk.W, pady=(0, 2))

        self.lbl_mini_size = ttk.Label(
            self.mini_drive_box,
            text="Tamanho: --",
            style="AltMuted.TLabel",
        )
        self.lbl_mini_size.pack(anchor=tk.W)

        self.lbl_mini_path = ttk.Label(
            self.mini_drive_box,
            text="Caminho: --",
            style="AltMuted.TLabel",
        )
        self.lbl_mini_path.pack(anchor=tk.W, pady=(2, 0))

    def _switch_view(self, view_key: str):
        if view_key not in self.views:
            return

        self.active_view = view_key

        for v in self.views.values():
            v.pack_forget()

        self.views[view_key].pack(fill=tk.BOTH, expand=True)

        for k, btn in self.nav_buttons.items():
            if k == view_key:
                btn.configure(style="Accent.TButton")
            else:
                btn.configure(style="Nav.TButton")

    # ── BANNER FIXO DO DISCO SELECIONADO + BOTÃO DE DIAGNÓSTICO 1-CLIQUE ──────
    def _build_drive_selector_banner(self):
        p = self.palette
        self.drive_banner_outer, self.drive_banner = self._make_card(
            self.content_area,
            padding=(14, 10),
            accent_top=p["accent"],
        )
        self.drive_banner_outer.pack(fill=tk.X, side=tk.TOP, pady=(0, 4))

        top_row = ttk.Frame(self.drive_banner, style="Card.TFrame")
        top_row.pack(fill=tk.X)

        ttk.Label(
            top_row,
            text="UNIDADE FÍSICA ALVO:",
            style="CardTitle.TLabel",
        ).pack(side=tk.LEFT, padx=(0, 10))

        self.combo_disks = ttk.Combobox(top_row, state="readonly", width=44, font=("Segoe UI Semibold", 9))
        self.combo_disks.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(0, 12))
        self.combo_disks.bind("<<ComboboxSelected>>", self._on_combo_disk_selected)

        # Botão Hero Global de Diagnóstico Completo em 1 Clique
        bolt_ic = self._icon_refs.get("bolt")
        diag_btn_kw = {
            "text": "  DIAGNOSTICAR TUDO (1-CLIQUE)",
            "style": "Accent.TButton",
            "command": self._start_full_diagnostic,
        }
        if bolt_ic is not None:
            diag_btn_kw["image"] = bolt_ic
            diag_btn_kw["compound"] = tk.LEFT
        self.btn_full_diagnostic = ttk.Button(top_row, **diag_btn_kw)
        self.btn_full_diagnostic.pack(side=tk.RIGHT, padx=(8, 0))

        self.lbl_drive_raw_badge = ttk.Label(
            top_row,
            text="Raw: Indefinido",
            font=("Segoe UI Semibold", 8, "bold"),
            background=p["neutral_bg"],
            foreground=p["neutral_fg"],
            padding=(10, 5),
        )
        self.lbl_drive_raw_badge.pack(side=tk.RIGHT)

        # Linha inferior de detalhes do hardware
        self.drive_info_row = ttk.Frame(self.drive_banner, style="Card.TFrame")
        self.drive_info_row.pack(fill=tk.X, pady=(6, 0))

        self.lbl_drive_details = ttk.Label(
            self.drive_info_row,
            text="Nenhum dispositivo selecionado. Aguardando enumeração...",
            style="CardMuted.TLabel",
        )
        self.lbl_drive_details.pack(side=tk.LEFT)

    # ── INSTANCIAÇÃO DAS VIEWS ────────────────────────────────────────────────
    def _build_views(self):
        self.view_dashboard = ttk.Frame(self.view_container, style="Root.TFrame")
        self.views["dashboard"] = self.view_dashboard
        self._setup_view_dashboard()

        self.view_diagnostic = ttk.Frame(self.view_container, style="Root.TFrame")
        self.views["diagnostic"] = self.view_diagnostic
        self._setup_view_diagnostic()

        self.view_smart = ttk.Frame(self.view_container, style="Root.TFrame")
        self.views["smart"] = self.view_smart
        self._setup_view_smart()

        self.view_clone = ttk.Frame(self.view_container, style="Root.TFrame")
        self.views["clone"] = self.view_clone
        self._setup_view_clone()

        self.view_carver = ttk.Frame(self.view_container, style="Root.TFrame")
        self.views["carver"] = self.view_carver
        self._setup_view_carver()

        self.view_surface = ttk.Frame(self.view_container, style="Root.TFrame")
        self.views["surface"] = self.view_surface
        self._setup_view_surface()

        self.view_logs = ttk.Frame(self.view_container, style="Root.TFrame")
        self.views["logs"] = self.view_logs
        self._setup_view_logs()

    # ══════════════════════════════════════════════════════════════════════════
    # TELA 1: CENTRAL DE DIAGNÓSTICO COMPLETO EM 1 CLIQUE (DASHBOARD)
    # ══════════════════════════════════════════════════════════════════════════
    def _setup_view_dashboard(self):
        f = self.view_dashboard
        p = self.palette

        # ── SEÇÃO SUPERIOR: MEDIDOR CIRCULAR DE SAÚDE + CHECKLIST DE 6 CAMADAS ──
        top_split = ttk.Frame(f, style="Root.TFrame")
        top_split.pack(fill=tk.X, pady=(0, 10))
        top_split.columnconfigure(0, weight=2)
        top_split.columnconfigure(1, weight=3)

        # Card Esquerdo: Medidor de Saúde + Resumo Executivo + Botão 1-Clique
        hero_outer, hero_box = self._make_card(
            top_split,
            title="LAUDO EXECUTIVO DE INTEGRIDADE DO DISCO",
            badge_text="SCANNER 360°",
            padding=(14, 12),
            accent_top="#10B981",
        )
        hero_outer.grid(row=0, column=0, padx=(0, 6), sticky="nsew")

        hero_body = ttk.Frame(hero_box, style="Card.TFrame")
        hero_body.pack(fill=tk.BOTH, expand=True)

        # Medidor Circular Anti-Aliased à esquerda do Card
        gauge_col = ttk.Frame(hero_body, style="Card.TFrame")
        gauge_col.pack(side=tk.LEFT, padx=(0, 14), anchor=tk.CENTER)

        self.lbl_gauge_ring = ttk.Label(gauge_col, style="CardTitle.TLabel")
        self.lbl_gauge_ring.pack(anchor=tk.CENTER)
        self.lbl_gauge_score = ttk.Label(
            gauge_col,
            text="PRONTO",
            font=("Segoe UI Semibold", 10, "bold"),
            style="CardTitle.TLabel",
        )
        self.lbl_gauge_score.pack(anchor=tk.CENTER, pady=(2, 0))
        self._update_health_gauge(None, "#38BDF8", "PRONTO")

        # Coluna Direita do Card Hero (Status, Modelo, Diretriz e Botão de Varredura)
        info_col = ttk.Frame(hero_body, style="Card.TFrame")
        info_col.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

        self.dash_health_badge = ttk.Label(
            info_col,
            text="Aguardando Análise...",
            font=("Segoe UI Semibold", 10, "bold"),
            background=p["neutral_bg"],
            foreground=p["text_primary"],
            padding=(10, 4),
        )
        self.dash_health_badge.pack(anchor=tk.W)

        self.dash_health_title = ttk.Label(
            info_col,
            text="Selecione um disco para carregar o resumo de confiabilidade.",
            font=("Segoe UI Semibold", 11, "bold"),
            style="CardBody.TLabel",
            wraplength=360,
        )
        self.dash_health_title.pack(anchor=tk.W, pady=(6, 4))

        self.dash_recommendation = ttk.Label(
            info_col,
            text="Clique em 'EXECUTAR DIAGNÓSTICO COMPLETO (1-CLIQUE)' para testar sensores S.M.A.R.T., cabos SATA, partições GPT/MBR, Boot Sector NTFS e varrer de 0% a 100% da superfície física do HD.",
            style="CardMuted.TLabel",
            wraplength=360,
            justify=tk.LEFT,
        )
        self.dash_recommendation.pack(anchor=tk.W, pady=(0, 6))

        mode_row = ttk.Frame(info_col, style="Card.TFrame")
        mode_row.pack(fill=tk.X, pady=(0, 6))
        ttk.Label(mode_row, text="Profundidade:", style="CardMuted.TLabel").pack(side=tk.LEFT, padx=(0, 6))

        self.scan_modes = [
            "Profundo: Varrer Extensão Total 0% a 100% (512 Zonas Distribuídas)",
            "Completo: Ler 100% de Todos os Setores do HD (Modo Victoria)",
            "Rápido: S.M.A.R.T. + Partições + Boot/MFT + Início (128 MB)",
        ]
        self.var_scan_mode = tk.StringVar(value=self.scan_modes[0])
        self.combo_diag_mode = ttk.Combobox(
            mode_row,
            textvariable=self.var_scan_mode,
            values=self.scan_modes,
            state="readonly",
            width=42,
        )
        self.combo_diag_mode.pack(side=tk.LEFT, fill=tk.X, expand=True)

        self.diag_progressbar = ttk.Progressbar(
            info_col,
            mode="determinate",
            maximum=100.0,
            value=0.0,
        )
        self.diag_progressbar.pack(fill=tk.X, pady=(0, 4))

        self.lbl_diag_live_telemetry = ttk.Label(
            info_col,
            text="Posição Física: Aguardando início (0.0% concluído)",
            style="CardMuted.TLabel",
            font=("Consolas", 8),
        )
        self.lbl_diag_live_telemetry.pack(anchor=tk.W, pady=(0, 8))

        hero_btn_row = ttk.Frame(info_col, style="Card.TFrame")
        hero_btn_row.pack(fill=tk.X)

        self.btn_hero_scan = ttk.Button(
            hero_btn_row,
            text="EXECUTAR DIAGNÓSTICO COMPLETO (1-CLIQUE)",
            style="Accent.TButton",
            command=self._start_full_diagnostic,
        )
        self.btn_hero_scan.pack(side=tk.LEFT)

        self.btn_stop_diag = ttk.Button(
            hero_btn_row,
            text="⏹ Parar / Concluir Laudo",
            state=tk.DISABLED,
            command=self._stop_full_diagnostic,
        )
        self.btn_stop_diag.pack(side=tk.LEFT, padx=(8, 0))

        # Card Direito: Matriz de Verificação de Falhas em 6 Camadas (Checklist 1-Clique)
        matrix_outer, matrix_box = self._make_card(
            top_split,
            title="MATRIZ DE DETECÇÃO DE FALHAS (6 CAMADAS DE HARDWARE & LÓGICA)",
            badge_text="TEMPO REAL",
            padding=(12, 10),
            accent_top="#0EA5E9",
        )
        matrix_outer.grid(row=0, column=1, padx=(6, 0), sticky="nsew")

        matrix_grid = ttk.Frame(matrix_box, style="Card.TFrame")
        matrix_grid.pack(fill=tk.BOTH, expand=True)
        for c in range(3):
            matrix_grid.columnconfigure(c, weight=1)
        for r in range(2):
            matrix_grid.rowconfigure(r, weight=1)

        self.chk_cards: dict[str, dict[str, ttk.Label]] = {}

        def make_check_tile(key: str, title: str, default_status: str, default_detail: str, r: int, c: int, accent_color: str):
            outer, tile = self._make_card(
                matrix_grid,
                padding=(10, 8),
                alt=True,
                accent_top=accent_color,
            )
            outer.grid(row=r, column=c, padx=4, pady=4, sticky="nsew")

            top_r = ttk.Frame(tile, style="CardAlt.TFrame")
            top_r.pack(fill=tk.X)

            lbl_t = ttk.Label(top_r, text=title, style="AltTitle.TLabel")
            lbl_t.pack(side=tk.LEFT)

            lbl_badge = ttk.Label(
                top_r,
                text=default_status,
                font=("Segoe UI Semibold", 7, "bold"),
                background=p["neutral_bg"],
                foreground=p["neutral_fg"],
                padding=(5, 1),
            )
            lbl_badge.pack(side=tk.RIGHT)

            lbl_det = ttk.Label(
                tile,
                text=default_detail,
                style="AltMuted.TLabel",
                wraplength=185,
            )
            lbl_det.pack(anchor=tk.W, pady=(4, 0))
            self.chk_cards[key] = {"badge": lbl_badge, "detail": lbl_det}

        make_check_tile("smart_surface", "1. Setores S.M.A.R.T.", "PENDENTE", "Reallocated (0x05) & Pending (0xC5)", 0, 0, "#10B981")
        make_check_tile("cable_crc", "2. Cabo SATA & Bus", "PENDENTE", "Erros elétricos CRC UltraDMA (0xC7)", 0, 1, "#3B82F6")
        make_check_tile("thermal_mech", "3. Térmico & Motor", "PENDENTE", "Temperatura (°C), Spin & Desgaste", 0, 2, "#F59E0B")
        make_check_tile("partition_tbl", "4. Tabela GPT / MBR", "PENDENTE", "Assinatura 0x55AA, CRC32 & Alinhamento", 1, 0, "#8B5CF6")
        make_check_tile("boot_mft", "5. Boot Sector & $MFT", "PENDENTE", "VBR Primário vs Backup & NTFS $MFT", 1, 1, "#EC4899")
        make_check_tile("surface_io", "6. Leitura de Superfície", "PENDENTE", "Teste físico de Bad Sectors & Latência", 1, 2, "#0EA5E9")

        # ── GRADE DE MÓDULOS DE AÇÃO RÁPIDA (4 CARDS COMPACTOS) ─────────────────
        actions_grid = ttk.Frame(f, style="Root.TFrame")
        actions_grid.pack(fill=tk.X, pady=(0, 10))
        for col in range(4):
            actions_grid.columnconfigure(col, weight=1)

        def make_action_card(parent, title, desc, icon_key, c, target_view, accent_hex):
            outer, card = self._make_card(
                parent,
                padding=(12, 9),
                alt=True,
                accent_top=accent_hex,
            )
            pad_l = 0 if c == 0 else 4
            pad_r = 0 if c == 3 else 4
            outer.grid(row=0, column=c, padx=(pad_l, pad_r), sticky="nsew")

            hdr_row = ttk.Frame(card, style="CardAlt.TFrame")
            hdr_row.pack(fill=tk.X)

            ic = self._icon_refs.get(icon_key)
            if ic is not None:
                ttk.Label(hdr_row, image=ic, style="AltTitle.TLabel").pack(side=tk.LEFT, padx=(0, 8))

            ttk.Label(hdr_row, text=title, style="AltTitle.TLabel").pack(side=tk.LEFT)

            ttk.Button(
                hdr_row,
                text="Abrir →",
                command=lambda: self._switch_view(target_view),
            ).pack(side=tk.RIGHT)

            ttk.Label(
                card,
                text=desc,
                style="AltMuted.TLabel",
                wraplength=260,
            ).pack(anchor=tk.W, pady=(5, 0))

        make_action_card(
            actions_grid,
            "Clonagem Segura (.img)",
            "Copie o disco pulando bad sectors com mapa .map atômico.",
            "clone", 0, "clone", "#0EA5E9"
        )
        make_action_card(
            actions_grid,
            "Reparo GPT & Boot NTFS",
            "Realinhe partições e restaure VBR corrompido via backup.",
            "diagnostic", 1, "diagnostic", "#8B5CF6"
        )
        make_action_card(
            actions_grid,
            "Resgate Forense (Carving)",
            "Extraia fotos, PDFs e ZIPs diretamente dos setores brutos.",
            "carver", 2, "carver", "#F59E0B"
        )
        make_action_card(
            actions_grid,
            "Heatmap de Superfície",
            "Mapeie blocos lentos e setores ilegíveis em tempo real.",
            "surface", 3, "surface", "#EC4899"
        )

        # ── TABELA DE PARTIÇÕES E VOLUMES DETECTADOS NA MÍDIA ───────────────────
        part_outer, part_box = self._make_card(
            f,
            title="PARTIÇÕES E VOLUMES DETECTADOS AUTOMATICAMENTE NA MÍDIA",
            badge_text="LEITURA LBA DIRETA",
            padding=(12, 8),
        )
        part_outer.pack(fill=tk.BOTH, expand=True)

        cols = ("idx", "name", "start_lba", "end_lba", "size", "fs", "status")
        self.dash_tree = ttk.Treeview(part_box, columns=cols, show="headings", height=5)
        self.dash_tree.heading("idx", text="#")
        self.dash_tree.heading("name", text="Nome / Rótulo da Partição")
        self.dash_tree.heading("start_lba", text="LBA Inicial")
        self.dash_tree.heading("end_lba", text="LBA Final")
        self.dash_tree.heading("size", text="Capacidade")
        self.dash_tree.heading("fs", text=" Esquema ")
        self.dash_tree.heading("status", text="Diagnóstico Estrutural")

        self.dash_tree.column("idx", width=40, anchor=tk.CENTER)
        self.dash_tree.column("name", width=210, anchor=tk.W)
        self.dash_tree.column("start_lba", width=120, anchor=tk.E)
        self.dash_tree.column("end_lba", width=120, anchor=tk.E)
        self.dash_tree.column("size", width=110, anchor=tk.E)
        self.dash_tree.column("fs", width=90, anchor=tk.CENTER)
        self.dash_tree.column("status", width=220, anchor=tk.W)

        dt_scroll = ttk.Scrollbar(part_box, orient=tk.VERTICAL, command=self.dash_tree.yview)
        self.dash_tree.configure(yscrollcommand=dt_scroll.set)
        self.dash_tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        dt_scroll.pack(side=tk.RIGHT, fill=tk.Y)

    def _update_health_gauge(self, score: Optional[int], color_hex: str, label_text: str):
        p = self.palette
        photo = VectorFactory.create_gauge_image(score, color_hex, p["bg_card"], p["border"], size=92)
        if photo is not None:
            self._gauge_photo = photo
            self.lbl_gauge_ring.configure(image=photo)
        self.lbl_gauge_score.configure(text=label_text, foreground=color_hex)

    def _set_check_tile(self, key: str, status_text: str, detail_text: str, level: str = "neutral"):
        if key not in self.chk_cards:
            return
        p = self.palette
        colors = {
            "ok": (p["success_bg"], p["success_fg"]),
            "warn": (p["warning_bg"], p["warning_fg"]),
            "fail": (p["danger_bg"], p["danger_fg"]),
            "info": (p["accent_soft_bg"], p["accent_soft_fg"]),
            "neutral": (p["neutral_bg"], p["neutral_fg"]),
        }
        bg, fg = colors.get(level, colors["neutral"])
        self.chk_cards[key]["badge"].configure(text=f" {status_text} ", background=bg, foreground=fg)
        self.chk_cards[key]["detail"].configure(text=detail_text)

    # ══════════════════════════════════════════════════════════════════════════
    # TELA 2: DIAGNÓSTICO & REPARO (PARTIÇÕES & BOOT)
    # ══════════════════════════════════════════════════════════════════════════
    def _setup_view_diagnostic(self):
        f = self.view_diagnostic
        p = self.palette

        action_bar = ttk.Frame(f, style="Root.TFrame")
        action_bar.pack(fill=tk.X, pady=(0, 10))

        self.lbl_target_info = ttk.Label(
            action_bar,
            text="Nenhum disco selecionado.",
            style="ViewTitle.TLabel",
        )
        self.lbl_target_info.pack(side=tk.LEFT, pady=2)

        self.btn_run_scan = ttk.Button(
            action_bar,
            text="Iniciar Varredura de Geometria & Boot Sectors",
            style="Accent.TButton",
            command=self._start_deep_scan,
        )
        self.btn_run_scan.pack(side=tk.RIGHT)

        split_cards = ttk.Frame(f, style="Root.TFrame")
        split_cards.pack(fill=tk.BOTH, expand=True, pady=(0, 10))
        split_cards.columnconfigure(0, weight=1)
        split_cards.columnconfigure(1, weight=1)
        split_cards.rowconfigure(0, weight=1)

        card_table_outer, card_table = self._make_card(
            split_cards,
            title="1. TABELA REGISTRADA NO DISCO (GPT / MBR)",
            badge_text="LBA 0..33",
            padding=(12, 10),
        )
        card_table_outer.grid(row=0, column=0, padx=(0, 5), sticky="nsew")

        gpt_wrap, self.txt_gpt = self._make_styled_text(card_table, wrap=tk.NONE)
        gpt_wrap.pack(fill=tk.BOTH, expand=True)

        card_media_outer, card_media = self._make_card(
            split_cards,
            title="2. MÍDIA FÍSICA REAL (BOOT SECTORS & MFTs)",
            badge_text="RAW SCAN",
            padding=(12, 10),
        )
        card_media_outer.grid(row=0, column=1, padx=(5, 0), sticky="nsew")

        fs_wrap, self.txt_fs = self._make_styled_text(card_media, wrap=tk.NONE)
        fs_wrap.pack(fill=tk.BOTH, expand=True)

        self.repair_card_outer, self.repair_card = self._make_card(
            f,
            title="3. AÇÕES CIRÚRGICAS DE RESOLUÇÃO COM ROLLBACK VERIFICADO",
            badge_text="TRANSAÇÃO SEGURA",
            padding=(14, 12),
            accent_top=p["accent"],
        )
        self.repair_card_outer.pack(fill=tk.X, side=tk.BOTTOM)

        self.lbl_repair_diagnosis = ttk.Label(
            self.repair_card,
            text="Selecione um disco e execute a análise para verificar desalinhamentos ou boot sector corrompido.",
            style="CardBody.TLabel",
            wraplength=560,
        )
        self.lbl_repair_diagnosis.pack(side=tk.LEFT, fill=tk.X, expand=True)

        btn_box = ttk.Frame(self.repair_card, style="Card.TFrame")
        btn_box.pack(side=tk.RIGHT)

        self.btn_repair_boot = ttk.Button(
            btn_box,
            text="Restaurar Boot Sector via Backup",
            state=tk.DISABLED,
            command=self._execute_boot_restore,
        )
        self.btn_repair_boot.pack(side=tk.LEFT, padx=(0, 8))

        self.btn_repair = ttk.Button(
            btn_box,
            text="Realinhar Partição GPT",
            style="Accent.TButton",
            state=tk.DISABLED,
            command=self._execute_repair,
        )
        self.btn_repair.pack(side=tk.RIGHT)

    # ══════════════════════════════════════════════════════════════════════════
    # TELA 3: TELEMETRIA S.M.A.R.T. & CABOS SATA
    # ══════════════════════════════════════════════════════════════════════════
    def _setup_view_smart(self):
        f = self.view_smart
        p = self.palette

        header = ttk.Frame(f, style="Root.TFrame")
        header.pack(fill=tk.X, pady=(0, 10))

        ttk.Label(
            header,
            text="Telemetria S.M.A.R.T. de Hardware & Comunicação Elétrica",
            style="ViewTitle.TLabel",
        ).pack(side=tk.LEFT)

        self.lbl_smart_source = ttk.Label(
            header,
            text="Interface: --",
            font=("Segoe UI Semibold", 8, "bold"),
            background=p["neutral_bg"],
            foreground=p["neutral_fg"],
            padding=(10, 4),
        )
        self.lbl_smart_source.pack(side=tk.RIGHT)

        metrics_grid = ttk.Frame(f, style="Root.TFrame")
        metrics_grid.pack(fill=tk.X, pady=(0, 10))

        for c in range(3):
            metrics_grid.columnconfigure(c, weight=1)

        def make_smart_metric(parent, label, default_val, c, r, accent_hex):
            outer, card = self._make_card(
                parent,
                padding=(14, 10),
                alt=True,
                accent_top=accent_hex,
            )
            pad_l = 0 if c == 0 else 5
            pad_r = 0 if c == 2 else 5
            outer.grid(column=c, row=r, padx=(pad_l, pad_r), pady=4, sticky="nsew")

            ttk.Label(card, text=label, style="AltMuted.TLabel").pack(anchor=tk.W)
            lbl = ttk.Label(card, text=default_val, style="AltMetric.TLabel")
            lbl.pack(anchor=tk.W, pady=(4, 0))
            return lbl

        self.val_temp = make_smart_metric(metrics_grid, "Temperatura Operacional", "-- °C", 0, 0, "#0EA5E9")
        self.val_smart_risk = make_smart_metric(metrics_grid, "Classificação de Risco", "--", 1, 0, "#10B981")
        self.val_crc_err = make_smart_metric(metrics_grid, "Erros de Cabo SATA (CRC 0xC7)", "--", 2, 0, "#8B5CF6")

        self.val_reallocated = make_smart_metric(metrics_grid, "Setores Remapeados (0x05)", "--", 0, 1, "#F59E0B")
        self.val_pending = make_smart_metric(metrics_grid, "Setores Pendentes (0xC5)", "--", 1, 1, "#EF4444")
        self.val_spin = make_smart_metric(metrics_grid, "Falhas de Partida Motor (0x0A)", "--", 2, 1, "#EC4899")

        advisory_outer, advisory_box = self._make_card(f, padding=(12, 8))
        advisory_outer.pack(fill=tk.X, pady=(0, 10))

        ttk.Label(
            advisory_box,
            text="Transparência Técnica: Pontes USB e controladoras NVMe que não expõem tabelas ATA S.M.A.R.T. tradicionais têm seus atributos ATA "
                 "apresentados como 'N/D', utilizando-se o Diagnóstico Completo (1-Clique) para testar leitura física e geometria.",
            style="CardMuted.TLabel",
            wraplength=980,
        ).pack(anchor=tk.W)

        self.smart_diag_outer, self.smart_diag_box = self._make_card(
            f,
            title="LAUDO DE TELEMETRIA & DIRETRIZ OPERACIONAL",
            badge_text="ATA / WMI",
            padding=(16, 14),
        )
        self.smart_diag_outer.pack(fill=tk.BOTH, expand=True)

        self.lbl_smart_text = ttk.Label(
            self.smart_diag_box,
            text="Aguardando seleção do dispositivo para consultar telemetria...",
            style="CardBody.TLabel",
            justify=tk.LEFT,
            wraplength=980,
        )
        self.lbl_smart_text.pack(anchor=tk.NW, fill=tk.BOTH, expand=True)

    # ══════════════════════════════════════════════════════════════════════════
    # TELA 4: CLONAGEM RESILIENTE (MODO DDRESCUE)
    # ══════════════════════════════════════════════════════════════════════════
    def _setup_view_clone(self):
        f = self.view_clone
        p = self.palette

        ttk.Label(
            f,
            text="Clonagem Resiliente Não-Destrutiva em Duas Fases",
            style="ViewTitle.TLabel",
        ).pack(anchor=tk.W, pady=(0, 2))

        ttk.Label(
            f,
            text="Inspirado no GNU ddrescue: na Fase 1, faz cópia rápida saltando blocos com erro de I/O para resgatar "
                 "imediatamente os dados saudáveis. Na Fase 2, realiza leitura fina setor a setor com checkpoint atômico (.map).",
            style="ViewSub.TLabel",
            wraplength=980,
        ).pack(anchor=tk.W, pady=(0, 10))

        dest_outer, dest_card = self._make_card(
            f,
            title="ARQUIVO DE IMAGEM DE DESTINO (.IMG)",
            badge_text="PROTEÇÃO P0 ATIVA",
            padding=(14, 12),
        )
        dest_outer.pack(fill=tk.X, pady=(0, 10))

        row = ttk.Frame(dest_card, style="Card.TFrame")
        row.pack(fill=tk.X)

        self.txt_dest_image = ttk.Entry(row, font=("Segoe UI", 9))
        self.txt_dest_image.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(0, 10))

        ttk.Button(row, text="Procurar...", command=self._browse_image_dest).pack(side=tk.RIGHT)

        ttk.Label(
            dest_card,
            text="Barreira P0: Bloqueia automaticamente destinos no mesmo disco físico de origem e impede sobrescrita silenciosa de imagens sem mapa válido.",
            style="CardMuted.TLabel",
        ).pack(anchor=tk.W, pady=(8, 0))

        stats_outer, stats_frame = self._make_card(
            f,
            title="TELEMETRIA DE RESGATE & CHECKPOINT ATÔMICO",
            badge_text="DDRESCUE ENGINE",
            padding=(16, 14),
            accent_top=p["accent"],
        )
        stats_outer.pack(fill=tk.BOTH, expand=True, pady=(0, 10))

        self.lbl_clone_phase = ttk.Label(
            stats_frame,
            text="Fase: Aguardando início...",
            style="CardTitle.TLabel",
        )
        self.lbl_clone_phase.pack(anchor=tk.W, pady=(0, 6))

        self.clone_progressbar = ttk.Progressbar(stats_frame, mode="determinate")
        self.clone_progressbar.pack(fill=tk.X, pady=(0, 14))

        grid_stats = ttk.Frame(stats_frame, style="Card.TFrame")
        grid_stats.pack(fill=tk.X, pady=(4, 0))
        for col in range(4):
            grid_stats.columnconfigure(col, weight=1)

        def make_stat_box(parent, title, val, c, accent_hex):
            outer, box = self._make_card(
                parent,
                padding=(12, 10),
                alt=True,
                accent_top=accent_hex,
            )
            pad_l = 0 if c == 0 else 5
            pad_r = 0 if c == 3 else 5
            outer.grid(row=0, column=c, padx=(pad_l, pad_r), sticky="nsew")

            ttk.Label(box, text=title, style="AltMuted.TLabel").pack(anchor=tk.W)
            l = ttk.Label(box, text=val, style="AltMetric.TLabel")
            l.pack(anchor=tk.W, pady=(4, 0))
            return l

        self.lbl_clone_pos = make_stat_box(grid_stats, "Setor Atual / Total", "0 / 0", 0, "#0EA5E9")
        self.lbl_clone_rescued = make_stat_box(grid_stats, "Setores Resgatados", "0", 1, "#10B981")
        self.lbl_clone_bad = make_stat_box(grid_stats, "Bad Sectors Identificados", "0", 2, "#EF4444")
        self.lbl_clone_speed = make_stat_box(grid_stats, "Taxa de Transferência", "0.0 MB/s", 3, "#8B5CF6")

        ctrl_row = ttk.Frame(f, style="Root.TFrame")
        ctrl_row.pack(fill=tk.X, side=tk.BOTTOM)

        self.btn_cancel_clone = ttk.Button(
            ctrl_row,
            text="Interromper / Salvar Mapa (.map)",
            state=tk.DISABLED,
            command=self._cancel_clone,
        )
        self.btn_cancel_clone.pack(side=tk.LEFT)

        self.btn_start_clone = ttk.Button(
            ctrl_row,
            text="Iniciar Clonagem Resiliente",
            style="Accent.TButton",
            command=self._start_clone,
        )
        self.btn_start_clone.pack(side=tk.RIGHT)

    # ══════════════════════════════════════════════════════════════════════════
    # TELA 5: FILE CARVING FORENSE
    # ══════════════════════════════════════════════════════════════════════════
    def _setup_view_carver(self):
        f = self.view_carver

        ttk.Label(
            f,
            text="Recuperação Forense de Arquivos (Magic Bytes / Assinaturas Brutas)",
            style="ViewTitle.TLabel",
        ).pack(anchor=tk.W, pady=(0, 2))

        ttk.Label(
            f,
            text="Localiza e extrai imagens (JPG, PNG), documentos (PDF) e arquivos compactados (ZIP, DOCX, XLSX) "
                 "diretamente de setores brutos, mesmo quando o sistema de arquivos foi formatado ou corrompido.",
            style="ViewSub.TLabel",
            wraplength=980,
        ).pack(anchor=tk.W, pady=(0, 10))

        cfg_outer, cfg_box = self._make_card(
            f,
            title="CONFIGURAÇÕES DE EXTRAÇÃO & DIRETÓRIO DE SAÍDA",
            badge_text="RAW STREAM",
            padding=(14, 12),
        )
        cfg_outer.pack(fill=tk.X, pady=(0, 10))

        row1 = ttk.Frame(cfg_box, style="Card.TFrame")
        row1.pack(fill=tk.X, pady=(0, 8))

        ttk.Label(row1, text="Salvar Arquivos em:", style="CardBody.TLabel").pack(side=tk.LEFT, padx=(0, 10))
        self.txt_carve_dir = ttk.Entry(row1, font=("Segoe UI", 9))
        self.txt_carve_dir.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(0, 10))
        default_dir = Path.home() / "Desktop" / "DiskRescue_Recuperados"
        self.txt_carve_dir.insert(0, str(default_dir))
        ttk.Button(row1, text="Escolher Pasta...", command=self._browse_carve_dir).pack(side=tk.RIGHT)

        filter_row = ttk.Frame(cfg_box, style="Card.TFrame")
        filter_row.pack(fill=tk.X)

        ttk.Label(filter_row, text="Assinaturas Ativas:", style="CardMuted.TLabel").pack(side=tk.LEFT, padx=(0, 12))

        self.chk_jpg_var = tk.BooleanVar(value=True)
        self.chk_png_var = tk.BooleanVar(value=True)
        self.chk_pdf_var = tk.BooleanVar(value=True)
        self.chk_zip_var = tk.BooleanVar(value=True)

        ttk.Checkbutton(filter_row, text="Imagens JPEG (.jpg)", variable=self.chk_jpg_var).pack(side=tk.LEFT, padx=6)
        ttk.Checkbutton(filter_row, text="Imagens PNG (.png)", variable=self.chk_png_var).pack(side=tk.LEFT, padx=6)
        ttk.Checkbutton(filter_row, text="Documentos PDF (.pdf)", variable=self.chk_pdf_var).pack(side=tk.LEFT, padx=6)
        ttk.Checkbutton(filter_row, text="Compactados ZIP/Office (.zip)", variable=self.chk_zip_var).pack(side=tk.LEFT, padx=6)

        list_outer, list_box = self._make_card(
            f,
            title="ARQUIVOS RESGATADOS EM TEMPO REAL",
            padding=(12, 10),
        )
        list_outer.pack(fill=tk.BOTH, expand=True, pady=(0, 10))

        self.carver_tree = ttk.Treeview(
            list_box,
            columns=("id", "type", "lba", "size", "path"),
            show="headings",
            height=8,
        )
        self.carver_tree.heading("id", text="#")
        self.carver_tree.heading("type", text="Formato")
        self.carver_tree.heading("lba", text="Setor LBA")
        self.carver_tree.heading("size", text="Tamanho")
        self.carver_tree.heading("path", text="Caminho do Arquivo Salvo")

        self.carver_tree.column("id", width=46, anchor=tk.CENTER)
        self.carver_tree.column("type", width=90, anchor=tk.CENTER)
        self.carver_tree.column("lba", width=120, anchor=tk.E)
        self.carver_tree.column("size", width=110, anchor=tk.E)
        self.carver_tree.column("path", width=500, anchor=tk.W)

        c_scroll = ttk.Scrollbar(list_box, orient=tk.VERTICAL, command=self.carver_tree.yview)
        self.carver_tree.configure(yscrollcommand=c_scroll.set)
        self.carver_tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        c_scroll.pack(side=tk.RIGHT, fill=tk.Y)

        carve_actions = ttk.Frame(f, style="Root.TFrame")
        carve_actions.pack(fill=tk.X, side=tk.BOTTOM)

        self.lbl_carve_status = ttk.Label(carve_actions, text="Pronto para iniciar File Carving.", style="ViewSub.TLabel")
        self.lbl_carve_status.pack(side=tk.LEFT)

        ttk.Button(carve_actions, text="Abrir Pasta dos Recuperados", command=self._open_carve_folder).pack(side=tk.LEFT, padx=(14, 0))

        self.btn_start_carve = ttk.Button(
            carve_actions,
            text="Iniciar Carving Forense",
            style="Accent.TButton",
            command=self._start_carver,
        )
        self.btn_start_carve.pack(side=tk.RIGHT)

        self.btn_cancel_carve = ttk.Button(
            carve_actions,
            text="Interromper Carving",
            state=tk.DISABLED,
            command=self._cancel_carver,
        )
        self.btn_cancel_carve.pack(side=tk.RIGHT, padx=(0, 8))

    # ══════════════════════════════════════════════════════════════════════════
    # TELA 6: VARREDURA DE SUPERFÍCIE (HEATMAP DE LATÊNCIA)
    # ══════════════════════════════════════════════════════════════════════════
    def _setup_view_surface(self):
        f = self.view_surface
        p = self.palette

        ttk.Label(
            f,
            text="Varredura de Superfície & Heatmap de Latência de Setor",
            style="ViewTitle.TLabel",
        ).pack(anchor=tk.W, pady=(0, 2))

        ttk.Label(
            f,
            text="Mede o tempo de resposta físico bloco a bloco (estilo Victoria / MHDD) para identificar setores degradados "
                 "antes que ocorra perda definitiva de dados.",
            style="ViewSub.TLabel",
        ).pack(anchor=tk.W, pady=(0, 10))

        canvas_outer, canvas_box = self._make_card(
            f,
            title="MAPA DE CALOR DE LATÊNCIA EM TEMPO REAL",
            badge_text="I/O PROFILER",
            padding=(12, 10),
        )
        canvas_outer.pack(fill=tk.BOTH, expand=True, pady=(0, 10))

        self.surface_canvas = tk.Canvas(
            canvas_box,
            bg=p["bg_input"],
            height=220,
            highlightthickness=1,
            highlightbackground=p["border"],
            bd=0,
        )
        self.surface_canvas.pack(fill=tk.BOTH, expand=True)

        leg_outer, legend_frame = self._make_card(f, padding=(12, 8), alt=True)
        leg_outer.pack(fill=tk.X, pady=(0, 10))

        def make_legend(parent, color, label):
            f_item = ttk.Frame(parent, style="CardAlt.TFrame")
            f_item.pack(side=tk.LEFT, padx=10)
            c = tk.Canvas(f_item, width=13, height=13, bg=color, highlightthickness=0, bd=0)
            c.pack(side=tk.LEFT, padx=(0, 6))
            lbl = ttk.Label(f_item, text=label, style="AltTitle.TLabel")
            lbl.pack(side=tk.LEFT)
            return lbl

        self.leg_fast = make_legend(legend_frame, "#22c55e", "< 20ms: 0")
        self.leg_good = make_legend(legend_frame, "#3b82f6", "< 100ms: 0")
        self.leg_slow = make_legend(legend_frame, "#eab308", "< 250ms: 0")
        self.leg_vslow = make_legend(legend_frame, "#f97316", "< 1000ms: 0")
        self.leg_crit = make_legend(legend_frame, "#dc2626", "Atraso Crítico: 0")
        self.leg_bad = make_legend(legend_frame, "#7f1d1d", "Bad Sector: 0")

        surface_actions = ttk.Frame(f, style="Root.TFrame")
        surface_actions.pack(fill=tk.X, side=tk.BOTTOM)

        self.lbl_surface_status = ttk.Label(surface_actions, text="Pronto para testar superfície.", style="ViewSub.TLabel")
        self.lbl_surface_status.pack(side=tk.LEFT)

        self.combo_surface_mode = ttk.Combobox(
            surface_actions,
            textvariable=self.var_scan_mode,
            values=self.scan_modes,
            state="readonly",
            width=44,
        )
        self.combo_surface_mode.pack(side=tk.LEFT, padx=(14, 6))

        self.btn_cancel_surface = ttk.Button(
            surface_actions,
            text="⏹ Interromper",
            state=tk.DISABLED,
            command=self._cancel_surface,
        )
        self.btn_cancel_surface.pack(side=tk.LEFT, padx=(6, 0))

        self.btn_start_surface = ttk.Button(
            surface_actions,
            text="Iniciar Teste de Superfície",
            style="Accent.TButton",
            command=self._start_surface_scan,
        )
        self.btn_start_surface.pack(side=tk.RIGHT)

    # ══════════════════════════════════════════════════════════════════════════
    # TELA 7: CONSOLE DE LOGS
    # ══════════════════════════════════════════════════════════════════════════
    def _setup_view_logs(self):
        f = self.view_logs

        ttk.Label(
            f,
            text="Console de Auditoria Forense & Registro de Eventos",
            style="ViewTitle.TLabel",
        ).pack(anchor=tk.W, pady=(0, 8))

        log_outer, log_box = self._make_card(
            f,
            title="HISTÓRICO DE OPERAÇÕES E DIAGNÓSTICOS DE BAIXO NÍVEL",
            badge_text="AUDIT LOG",
            padding=(12, 10),
        )
        log_outer.pack(fill=tk.BOTH, expand=True, pady=(0, 10))

        console_wrap, self.txt_console = self._make_styled_text(log_box, wrap=tk.WORD)
        console_wrap.pack(fill=tk.BOTH, expand=True)

        log_actions = ttk.Frame(f, style="Root.TFrame")
        log_actions.pack(fill=tk.X, side=tk.BOTTOM)

        ttk.Button(log_actions, text="Limpar Log", command=lambda: self.txt_console.delete("1.0", tk.END)).pack(side=tk.LEFT)
        ttk.Button(log_actions, text="Exportar Relatório (.txt)", style="Accent.TButton", command=self._save_log_file).pack(side=tk.RIGHT)

    # ── BARRA DE STATUS INFERIOR ──────────────────────────────────────────────
    def _build_status_bar(self):
        self.status_bar = ttk.Frame(self.root, style="Header.TFrame", padding=(18, 7))
        self.status_bar.pack(fill=tk.X, side=tk.BOTTOM)

        self.lbl_status = ttk.Label(self.status_bar, text="Pronto.", style="Status.TLabel")
        self.lbl_status.pack(side=tk.LEFT)

        self.progress_bar = ttk.Progressbar(self.status_bar, mode="indeterminate", length=220)

    # ══════════════════════════════════════════════════════════════════════════
    # LÓGICA DE THREADS, FILAS E POLLING
    # ══════════════════════════════════════════════════════════════════════════
    def log(self, text: str):
        ts = time.strftime("%H:%M:%S")
        self.msg_queue.put(("log", f"[{ts}] {text}\n"))

    def set_status(self, text: str):
        self.msg_queue.put(("status", text))

    def _has_active_operations(self) -> bool:
        return bool(
            self.is_scanning
            or self.is_surface_scanning
            or self.is_repairing
            or (self.cloner_instance is not None)
            or (self.carver_instance is not None)
        )

    def _on_close_window(self, confirm_if_active: bool = False):
        if not self._has_active_operations():
            self.is_running = False
            try:
                self.root.destroy()
            except Exception:
                pass
            return

        if self._closing_requested:
            self.set_status(
                "Aguardando finalização cooperativa das operações em andamento (preservando mapa/snapshots)..."
            )
            return

        if confirm_if_active:
            if not messagebox.askyesno(
                "Confirmar Saída",
                "Existem tarefas ativas em execução (clonagem, carving, varredura ou reparo).\n\n"
                "Deseja solicitar o cancelamento cooperativo, aguardar a preservação segura do estado (mapa/snapshots) e fechar?"
            ):
                return

        self._closing_requested = True
        self.closing_requested = True
        self.op_state = OperationState.CANCELLING

        if self.cloner_instance:
            self._cancel_clone()

        if self.carver_instance:
            self._cancel_carver()

        if self.is_surface_scanning:
            self._cancel_surface()

        if self.is_scanning:
            self.cancel_scan_flag = True

        self.set_status("Encerrando com segurança: aguardando término cooperativo dos workers...")
        self.log("Encerramento solicitado: aguardando finalização segura dos workers ativos...\n")
        self._check_close_completion()

    def _check_close_completion(self):
        if not getattr(self, "is_running", True):
            return
        self._process_pending_messages()
        if not self._has_active_operations():
            self.is_running = False
            self.op_state = OperationState.CANCELLED
            try:
                self.root.destroy()
            except Exception:
                pass
        else:
            try:
                self.root.after(50, self._check_close_completion)
            except Exception:
                pass

    def _resolve_ended_state(self, explicit_val):
        if isinstance(explicit_val, OperationState):
            self.op_state = explicit_val
        elif self.op_state == OperationState.CANCELLING:
            self.op_state = OperationState.CANCELLED
        elif self.op_state == OperationState.RUNNING:
            self.op_state = OperationState.IDLE

    def _process_pending_messages(self):
        if not getattr(self, "is_running", True):
            try:
                while True:
                    self.msg_queue.get_nowait()
            except queue.Empty:
                pass
            return

        try:
            while True:
                kind, val = self.msg_queue.get_nowait()
                if not getattr(self, "is_running", True):
                    continue
                if kind == "log":
                    self.txt_console.insert(tk.END, val)
                    self.txt_console.see(tk.END)
                elif kind == "status":
                    self.lbl_status.config(text=val)
                elif kind == "disks_loaded":
                    self._render_disks_list(val)
                elif kind == "disks_error":
                    self.lbl_status.config(text=f"Erro ao enumerar discos: {val}")
                    self.log(f"ERRO NA ENUMERAÇÃO DE DISCOS: {val}\n")
                elif kind == "quick_partitions_loaded":
                    gpt, mbr, disk = val
                    if self.selected_disk and self.selected_disk.number == disk.number and not self.is_scanning:
                        self._populate_partition_views(gpt, mbr)
                elif kind == "scan_finished":
                    self._render_scan_results(*val)
                    if self.op_state not in (OperationState.FAILED, OperationState.CANCELLED):
                        self.op_state = OperationState.CANCELLED if self.cancel_scan_flag else OperationState.COMPLETED
                elif kind == "scan_error":
                    self.op_state = OperationState.FAILED
                    self.lbl_status.config(text=f"Erro na varredura: {val}")
                    self.log(f"ERRO NA VARREDURA: {val}\n")
                    if not self._closing_requested:
                        messagebox.showerror("Erro na Varredura", f"Falha durante a varredura:\n{val}")
                elif kind == "scan_ended":
                    self.is_scanning = False
                    self.progress_bar.stop()
                    self.progress_bar.pack_forget()
                    self.btn_run_scan.config(state=tk.NORMAL)
                    self.btn_full_diagnostic.config(state=tk.NORMAL)
                    self.btn_hero_scan.config(state=tk.NORMAL)
                    if hasattr(self, "btn_stop_diag"):
                        self.btn_stop_diag.config(state=tk.DISABLED)
                    self._resolve_ended_state(val)
                elif kind == "diag_progress":
                    pct, telemetry_str = val
                    if hasattr(self, "diag_progressbar"):
                        self.diag_progressbar["value"] = pct
                    if hasattr(self, "lbl_diag_live_telemetry"):
                        self.lbl_diag_live_telemetry.config(text=telemetry_str)
                elif kind == "full_diag_finished":
                    self._render_full_diagnostic_verdict(*val)
                elif kind == "smart_loaded":
                    if isinstance(val, tuple):
                        self._render_smart_results(*val)
                    else:
                        self._render_smart_results(val)
                elif kind == "clone_stats":
                    self._render_clone_stats(val)
                elif kind == "clone_finished":
                    res, status_text = val
                    self.lbl_status.config(text=status_text)
                    self.log(status_text)
                    if not res.success:
                        self.op_state = OperationState.CANCELLED
                    elif res.fully_recovered:
                        self.op_state = OperationState.COMPLETED
                    else:
                        self.op_state = OperationState.PARTIAL
                elif kind == "clone_error":
                    self.op_state = OperationState.FAILED
                    self.lbl_status.config(text=f"Erro na clonagem: {val}")
                    self.log(f"ERRO NA CLONAGEM: {val}\n")
                    if not self._closing_requested:
                        messagebox.showerror("Erro de Clonagem", f"Falha na clonagem resiliente:\n{val}")
                elif kind == "clone_ended":
                    self.btn_start_clone.config(state=tk.NORMAL)
                    self.btn_cancel_clone.config(state=tk.DISABLED)
                    self.cloner_instance = None
                    self._resolve_ended_state(val)
                elif kind == "carver_file":
                    self.carver_tree.insert(
                        "",
                        tk.END,
                        values=(val.file_id, val.file_type, f"{val.start_lba:,}", f"{val.size_bytes // 1024:,} KB", str(val.saved_path))
                    )
                elif kind == "carver_finished":
                    was_cancelled, has_corrupted = val
                    if was_cancelled:
                        self.op_state = OperationState.CANCELLED
                    elif has_corrupted:
                        self.op_state = OperationState.PARTIAL
                    else:
                        self.op_state = OperationState.COMPLETED
                elif kind == "carver_error":
                    self.op_state = OperationState.FAILED
                    self.lbl_status.config(text=f"Erro no carving: {val}")
                    self.log(f"ERRO NO FILE CARVING: {val}\n")
                    if not self._closing_requested:
                        messagebox.showerror("Erro no File Carving", f"Falha durante o carving:\n{val}")
                elif kind == "carver_ended":
                    self.btn_start_carve.config(state=tk.NORMAL)
                    self.btn_cancel_carve.config(state=tk.DISABLED)
                    self.carver_instance = None
                    self._resolve_ended_state(val)
                elif kind == "surface_block":
                    self._draw_surface_block(*val)
                elif kind == "surface_finished":
                    summary = val
                    self.lbl_status.config(text=f"Teste de superfície concluído: {summary.total_tested_blocks:,} blocos testados.")
                    self.log(f"Teste de superfície concluído: {summary.total_tested_blocks:,} blocos, {summary.bad_count} bad blocks.\n")
                    if self.cancel_surface_flag:
                        self.op_state = OperationState.CANCELLED
                    elif summary.bad_count > 0:
                        self.op_state = OperationState.PARTIAL
                    else:
                        self.op_state = OperationState.COMPLETED
                elif kind == "surface_error":
                    self.op_state = OperationState.FAILED
                    self.lbl_status.config(text=f"Erro no teste de superfície: {val}")
                    self.log(f"ERRO NO TESTE DE SUPERFÍCIE: {val}\n")
                    if not self._closing_requested:
                        messagebox.showerror("Erro de Superfície", f"Falha no teste de superfície:\n{val}")
                elif kind == "surface_ended":
                    self.is_surface_scanning = False
                    self.btn_start_surface.config(state=tk.NORMAL)
                    self.btn_cancel_surface.config(state=tk.DISABLED)
                    self._resolve_ended_state(val)
                elif kind == "repair_result":
                    self.is_repairing = False
                    res = val
                    if res.success:
                        self.op_state = OperationState.COMPLETED
                        self.log(f"SUCESSO NO REPARO: {res.message}\n")
                        if not self._closing_requested:
                            messagebox.showinfo("Sucesso", f"Partição realinhada com sucesso!\n{res.message}")
                    else:
                        self.op_state = OperationState.FAILED
                        self.log(f"FALHA NO REPARO: {res.message}\n")
                        if not self._closing_requested:
                            messagebox.showerror("Falha no Reparo", res.message)
                    self.btn_repair.config(state=tk.NORMAL)
                elif kind == "repair_error":
                    self.is_repairing = False
                    self.op_state = OperationState.FAILED
                    self.log(f"ERRO CRÍTICO NO REPARO: {val}\n")
                    if not self._closing_requested:
                        messagebox.showerror("Erro Crítico", f"Exceção durante reparo:\n{val}")
                    self.btn_repair.config(state=tk.NORMAL)
                elif kind == "boot_restore_result":
                    self.is_repairing = False
                    res = val
                    if res.success:
                        self.op_state = OperationState.COMPLETED
                        self.log(f"SUCESSO NA RESTAURAÇÃO: {res.message}\n")
                        if res.backup_file:
                            self.log(f"Snapshot preventivo salvo em: {res.backup_file}\n")
                        if not self._closing_requested:
                            messagebox.showinfo("Sucesso", f"Boot Sector NTFS restaurado com sucesso!\n\n{res.message}")
                        self.btn_repair_boot.config(state=tk.DISABLED)
                        self.lbl_status.config(text="Boot Sector NTFS restaurado com sucesso.")
                    else:
                        self.op_state = OperationState.FAILED
                        self.log(f"FALHA NA RESTAURAÇÃO: {res.message}\n")
                        if not self._closing_requested:
                            messagebox.showerror("Falha na Restauração", res.message)
                        self.btn_repair_boot.config(state=tk.NORMAL)
                        self.lbl_status.config(text="Falha na restauração do Boot Sector NTFS.")
                elif kind == "boot_restore_error":
                    self.is_repairing = False
                    self.op_state = OperationState.FAILED
                    self.log(f"ERRO NA RESTAURAÇÃO: {val}\n")
                    if not self._closing_requested:
                        messagebox.showerror("Erro Crítico", f"Falha inesperada durante restauração de Boot Sector:\n{val}")
                    self.btn_repair_boot.config(state=tk.NORMAL)
        except queue.Empty:
            pass

    def _start_queue_poller(self):
        self._process_pending_messages()
        if getattr(self, "is_running", True):
            try:
                self.root.after(80, self._start_queue_poller)
            except Exception:
                pass

    # ── TEMA E ELEVAÇÃO ───────────────────────────────────────────────────────
    def _toggle_theme(self):
        if not HAS_SV_TTK:
            return
        if self.current_theme == "dark":
            sv_ttk.set_theme("light")
            self.current_theme = "light"
            self.btn_theme.config(text="Tema: Claro")
            _apply_windows_dwm_theme(self.root, dark=False)
        else:
            sv_ttk.set_theme("dark")
            self.current_theme = "dark"
            self.btn_theme.config(text="Tema: Escuro")
            _apply_windows_dwm_theme(self.root, dark=True)

        p = self.palette
        self._configure_custom_styles()
        self.root.configure(bg=p["bg_root"])

        for w in self._themed_cards:
            w.configure(bg=p["bg_card"], highlightbackground=p["border"])
        for w in self._themed_card_alts:
            w.configure(bg=p["bg_card_alt"], highlightbackground=p["border"])
        for d in self._themed_dividers:
            d.configure(bg=p["border"], highlightbackground=p["border"])
        for t in self._themed_text_widgets:
            t.configure(
                bg=p["bg_input"],
                fg=p["text_primary"],
                insertbackground=p["accent"],
                selectbackground=p["accent_soft_bg"],
                selectforeground=p["text_primary"],
            )
        self.surface_canvas.configure(bg=p["bg_input"], highlightbackground=p["border"])
        self._update_health_gauge(None, p["accent"], self.lbl_gauge_score.cget("text"))
        self._switch_view(self.active_view)

    def _on_elevate(self):
        if relaunch_as_admin():
            self.root.destroy()
        else:
            messagebox.showerror("Erro UAC", "Não foi possível obter privilégio de Administrador via Controle de Conta de Usuário (UAC).")

    # ── ENUMERAÇÃO E SELEÇÃO DE DISCOS ────────────────────────────────────────
    def refresh_disks(self):
        self.set_status("Varrendo dispositivos físicos de armazenamento...")
        self.log("Inventário de discos físicos iniciado...")

        def worker():
            try:
                disks = scan_disks()
                self.msg_queue.put(("disks_loaded", disks))
            except Exception as exc:
                self.msg_queue.put(("disks_error", str(exc)))

        threading.Thread(target=worker, daemon=True).start()

    def _render_disks_list(self, disks: list[DiskInfo]):
        self.disks = disks
        combo_values = []
        for d in disks:
            combo_values.append(f"[Disco {d.number}] {d.friendly_name} ({d.size_gb:.1f} GB) — {d.media_type} {d.bus_type}")

        self.combo_disks["values"] = combo_values

        self.set_status(f"{len(disks)} dispositivo(s) físico(s) detectado(s).")
        self.log(f"Inventário concluído: {len(disks)} dispositivo(s) detectado(s).")

        if disks:
            self.combo_disks.current(0)
            self._select_disk_by_index(0)

    def _on_combo_disk_selected(self, _event):
        idx = self.combo_disks.current()
        if idx >= 0 and idx < len(self.disks):
            self._select_disk_by_index(idx)

    def _select_disk_by_index(self, idx: int):
        d = self.disks[idx]
        self.selected_disk = d

        raw_text = "✔ Acesso Raw Liberado" if d.can_open_raw else "✖ Acesso Raw Bloqueado"
        raw_bg = "#064E3B" if d.can_open_raw else "#7F1D1D"
        raw_fg = "#34D399" if d.can_open_raw else "#FCA5A5"

        self.lbl_drive_raw_badge.config(text=raw_text, background=raw_bg, foreground=raw_fg)
        self.lbl_drive_details.config(
            text=f"Caminho: {d.device_path}  •  Capacidade: {d.size_gb:.1f} GB ({d.size_bytes:,} bytes)  •  "
                 f"Mídia: {d.media_type}  •  Barramento: {d.bus_type}  •  Serial: {d.serial_number or 'N/A'}"
        )

        self.lbl_mini_model.config(text=f"Disco {d.number}: {d.friendly_name}")
        self.lbl_mini_size.config(text=f"Tamanho: {d.size_gb:.1f} GB ({d.media_type})")
        self.lbl_mini_path.config(text=f"{d.device_path} ({d.bus_type})")

        self.lbl_target_info.config(
            text=f"Disco Alvo: [Disco {d.number}] {d.friendly_name} ({d.size_gb:.1f} GB)",
        )

        self._update_dashboard_with_disk(d)

        # Leitura instantânea de tabelas GPT/MBR (LBA 0..34) + Telemetria SMART em background
        def init_probe_worker():
            if d.can_open_raw:
                try:
                    with open(d.device_path, "rb") as f_raw:
                        gpt = parse_gpt(f_raw)
                        f_raw.seek(0)
                        mbr = parse_mbr(f_raw)
                    self.msg_queue.put(("quick_partitions_loaded", (gpt, mbr, d)))
                except Exception:
                    pass
            try:
                report = query_smart_report_windows(
                    d.number,
                    d.device_path,
                    d.friendly_name,
                    d.serial_number,
                    is_usb=(d.bus_type.upper() == "USB"),
                    bus_type=d.bus_type
                )
                self.msg_queue.put(("smart_loaded", (report, d)))
            except Exception as exc:
                self.msg_queue.put(("log", f"Erro ao consultar telemetria S.M.A.R.T.: {exc}\n"))

        threading.Thread(target=init_probe_worker, daemon=True).start()

    def _populate_partition_views(self, gpt, mbr):
        """Popula a tabela do Dashboard e a caixa GPT/MBR instantaneamente ao selecionar o disco."""
        for item in self.dash_tree.get_children():
            self.dash_tree.delete(item)
        self.txt_gpt.delete("1.0", tk.END)

        if gpt:
            self.txt_gpt.insert(tk.END, f"✔ Tabela GPT Válida (CRC32 Header: {gpt.header_crc32:#x}) — {len(gpt.partitions)} partição(ões):\n\n")
            for p in gpt.partitions:
                self.txt_gpt.insert(tk.END, f"  • Partição {p.index}: {p.name!r}\n")
                self.txt_gpt.insert(tk.END, f"    LBA Inicial: {p.first_lba:,} .. LBA Final: {p.last_lba:,} ({p.size_gb():.2f} GB)\n")
                self.txt_gpt.insert(tk.END, f"    Tipo GUID: {p.type_guid}\n\n")
                self.dash_tree.insert(
                    "",
                    tk.END,
                    values=(p.index, p.name or "Sem Rótulo", f"{p.first_lba:,}", f"{p.last_lba:,}", f"{p.size_gb():.2f} GB", "GPT", "✔ CRC32 Íntegro")
                )
            self._set_check_tile("partition_tbl", "✔ GPT OK", f"{len(gpt.partitions)} partição(ões) GPT com CRC32 válido", "ok")
        elif mbr and mbr.has_valid_signature:
            self.txt_gpt.insert(tk.END, "✔ Tabela MBR Clássica Válida (Assinatura 0x55AA presente):\n\n")
            non_empty = [p for p in mbr.partitions if not p.is_empty]
            for p in non_empty:
                self.txt_gpt.insert(tk.END, f"  • Partição {p.index}: [{p.type_name}]\n")
                self.txt_gpt.insert(tk.END, f"    LBA Inicial: {p.start_lba:,} .. LBA Final: {p.end_lba:,} ({p.size_gb:.2f} GB)\n")
                self.txt_gpt.insert(tk.END, f"    Código de Tipo: 0x{p.type_code:02X}\n\n")
                self.dash_tree.insert(
                    "",
                    tk.END,
                    values=(p.index, p.type_name, f"{p.start_lba:,}", f"{p.end_lba:,}", f"{p.size_gb:.2f} GB", "MBR", "Ativa" if p.is_bootable else "Primária")
                )
            self._set_check_tile("partition_tbl", "✔ MBR OK", f"{len(non_empty)} partição(ões) MBR (0x55AA válido)", "ok")
        else:
            self.txt_gpt.insert(tk.END, "⚠️ Tabela de partições não detectada ou assinaturas corrompidas.\n")
            self._set_check_tile("partition_tbl", "⚠ ALERTA", "Nenhuma tabela GPT/MBR válida no LBA 0..1", "warn")

    def _update_dashboard_with_disk(self, d: DiskInfo):
        for item in self.dash_tree.get_children():
            self.dash_tree.delete(item)

        self.dash_health_badge.config(
            text=f"● Status Windows: {d.health_status}",
            background="#1e293b",
            foreground="#38bdf8"
        )
        self.dash_health_title.config(
            text=f"{d.friendly_name} ({d.size_gb:.1f} GB — {d.media_type} {d.bus_type})"
        )
        self.dash_recommendation.config(
            text="Clique em 'EXECUTAR DIAGNÓSTICO COMPLETO (1-CLIQUE)' para inspecionar todos os setores, cabos, S.M.A.R.T., partições e boot sectors."
        )
        self._update_health_gauge(None, "#38BDF8", "ANALISANDO")
        for k in self.chk_cards:
            self._set_check_tile(k, "AGUARDANDO", "Clique em Diagnosticar Tudo (1-Clique)", "neutral")

    # ── RENDERIZAÇÃO DA TELEMETRIA S.M.A.R.T. ─────────────────────────────────
    def _render_smart_results(self, report: SmartReport, disk: DiskInfo | None = None):
        self.current_smart_report = report

        temp_str = f"{report.temperature_c} °C" if report.temperature_c else "--"
        self.val_temp.config(text=temp_str)

        risk_val = report.risk_level.value.split()[0]
        color_map = {
            "Verde": "#10b981",
            "Amarelo": "#f59e0b",
            "Vermelho": "#ef4444",
            "Cinza": "#94a3b8",
        }
        self.val_smart_risk.config(text=risk_val, foreground=color_map.get(risk_val, "#ffffff"))

        def fmt_val(val):
            return str(val) if val is not None else "N/D"

        self.val_crc_err.config(text=fmt_val(report.crc_errors))
        self.val_reallocated.config(text=fmt_val(report.reallocated_sectors))
        self.val_pending.config(text=fmt_val(report.pending_sectors))
        self.val_spin.config(text=fmt_val(report.spin_retries))

        lines = "\n\n".join(report.summary_messages)
        full_text = f"DIRETRIZ RECOMENDADA:\n{report.action_recommendation}\n\nDETALHES DO FIRMWARE:\n{lines}"
        self.lbl_smart_text.config(text=full_text)

        target_disk = disk or self.selected_disk
        hip_str = (
            " ".join(report.hipotese_provavel).lower()
            if isinstance(report.hipotese_provavel, list)
            else str(report.hipotese_provavel or "").lower()
        )
        is_usb = (
            (target_disk and target_disk.bus_type.upper() == "USB") or
            ("usb" in hip_str) or
            ("usb" in (report.action_recommendation or "").lower())
        )

        if target_disk:
            self.lbl_smart_source.config(text=f"Interface: {target_disk.bus_type}")

        # Atualiza os 3 primeiros cards da Matriz de 6 Camadas no Dashboard
        if report.reallocated_sectors is not None or report.pending_sectors is not None:
            r_s = report.reallocated_sectors or 0
            p_s = report.pending_sectors or 0
            if r_s == 0 and p_s == 0:
                self._set_check_tile("smart_surface", "✔ 0 ERROS", f"Remapeados: {r_s} | Pendentes: {p_s}", "ok")
            else:
                self._set_check_tile("smart_surface", "✖ FALHA", f"Remapeados: {r_s} | Pendentes: {p_s}", "fail")
        else:
            win_rel = report.windows_reliability or {}
            r_err = win_rel.get("ReadErrorsTotal")
            if r_err == 0:
                self._set_check_tile("smart_surface", "ℹ NVMe/SO", "0 erros de leitura no contador do Windows", "info")
            else:
                self._set_check_tile("smart_surface", "N/D ATA", "Execute o Diagnóstico 1-Clique para testar leitura", "neutral")

        if report.crc_errors is not None:
            if report.crc_errors == 0:
                self._set_check_tile("cable_crc", "✔ CABO OK", "0 erros elétricos de CRC UltraDMA (0xC7)", "ok")
            else:
                self._set_check_tile("cable_crc", "⚠ RUÍDO CRC", f"{report.crc_errors} erros de cabo/porta SATA!", "warn")
        else:
            bus_lbl = target_disk.bus_type if target_disk else "Barramento"
            self._set_check_tile("cable_crc", f"ℹ {bus_lbl}", f"Comunicação {bus_lbl} ativa", "info")

        hrs_txt = f" | {report.power_on_hours:,}h uso" if report.power_on_hours else ""
        if report.temperature_c is not None:
            if report.temperature_c >= 65:
                self._set_check_tile("thermal_mech", f"✖ {report.temperature_c} °C", f"Superaquecimento crítico{hrs_txt}", "fail")
            elif report.temperature_c >= 55:
                self._set_check_tile("thermal_mech", f"⚠ {report.temperature_c} °C", f"Temperatura elevada{hrs_txt}", "warn")
            else:
                self._set_check_tile("thermal_mech", f"✔ {report.temperature_c} °C", f"Temperatura normal{hrs_txt}", "ok")
        else:
            self._set_check_tile("thermal_mech", "N/D", "Sensor térmico não reportado", "neutral")

        # Atualiza badge de saúde e Medidor Circular no Dashboard
        if risk_val == "Verde":
            self.dash_health_badge.config(text="● Confiável / Saudável", background="#14532d", foreground="#4ade80")
            self._update_health_gauge(100, "#10B981", "100% OK")
        elif risk_val == "Amarelo":
            self.dash_health_badge.config(text="● Atenção / Análise Requerida", background="#78350f", foreground="#fcd34d")
            self._update_health_gauge(65, "#F59E0B", "ATENÇÃO")
        elif risk_val == "Vermelho":
            self.dash_health_badge.config(text="● Crítico / Risco de Falha Iminente", background="#7f1d1d", foreground="#fca5a5")
            self._update_health_gauge(20, "#EF4444", "CRÍTICO")
        else:
            if is_usb:
                self.dash_health_badge.config(text="● Telemetria Não Disponível (Interface USB)", background="#334155", foreground="#94a3b8")
            else:
                self.dash_health_badge.config(text="● Telemetria S.M.A.R.T. Não Disponível / Indeterminada", background="#334155", foreground="#94a3b8")
            self._update_health_gauge(None, "#38BDF8", "1-CLIQUE")

        self.dash_recommendation.config(text=f"Diretriz Recomendada: {report.action_recommendation}")

    # ══════════════════════════════════════════════════════════════════════════
    # MOTOR DE DIAGNÓSTICO COMPLETO EM 1 CLIQUE (ENCONTRAR QUALQUER FALHA)
    # ══════════════════════════════════════════════════════════════════════════
    def _compute_surface_scan_params(self, disk_total_sectors: int) -> tuple[int, int, Optional[int], str]:
        """Calcula max_sectors, block_sectors e stride_sectors conforme o modo escolhido."""
        if disk_total_sectors <= 65_536:
            surf_max = max(1, disk_total_sectors)
            surf_block = min(2048, max(1, surf_max // 16))
            return surf_max, surf_block, None, "Sequencial Integral"

        mode_str = self.var_scan_mode.get() if hasattr(self, "var_scan_mode") else ""
        if "Completo" in mode_str:
            surf_max = disk_total_sectors
            surf_block = 16_384  # Blocos de 8 MB sequenciais em 100% do disco
            return surf_max, surf_block, None, "100% Setor a Setor (Extensão Total)"
        elif "Rápido" in mode_str:
            surf_max = min(262_144, disk_total_sectors)  # 128 MB iniciais
            surf_block = 2048  # Blocos de 1 MB
            return surf_max, surf_block, None, "Triagem Rápida (128 MB Iniciais)"
        else:
            surf_max = disk_total_sectors
            surf_block = 4096  # 2 MB por zona física (512 zonas = 1 GB distribuído do início ao fim do HD)
            surf_stride = max(surf_block, disk_total_sectors // 512)
            return surf_max, surf_block, surf_stride, "Extensão Total 0% a 100% (512 Zonas)"

    def _stop_full_diagnostic(self):
        """Interrompe cooperativamente a varredura de superfície e gera o laudo imediato com os dados coletados."""
        self.cancel_surface_flag = True
        self.cancel_scan_flag = True
        if hasattr(self, "btn_stop_diag"):
            self.btn_stop_diag.config(state=tk.DISABLED)
        self.set_status("Finalizando leitura atual e consolidando laudo diagnóstico...")
        self.log("Interrupção solicitada pelo usuário: gerando laudo com os blocos físicos já inspecionados.\n")

    def _start_full_diagnostic(self):
        """
        Executa em 1 único clique todos os diagnósticos possíveis no HD/SSD selecionado:
        1. Telemetria S.M.A.R.T. de Hardware (ATA WMI + Contadores NVMe/Windows)
        2. Inspeção de Tabelas de Partição (MBR 0x55AA + GPT Primária/Backup CRC32)
        3. Varredura de Sistemas de Arquivos, Boot Sectors (VBR Primário vs Backup) e $MFT
        4. Teste Físico de Superfície, Latência de Setores e Detecção de Bad Sectors (0% a 100%)
        """
        if not self.selected_disk:
            messagebox.showwarning("Aviso", "Selecione um disco para diagnosticar.")
            return
        if not self.selected_disk.can_open_raw:
            messagebox.showerror(
                "Erro de Permissão",
                "Sem permissão de acesso físico direto (Raw). Clique em 'Elevar UAC' ou execute como Administrador."
            )
            return
        if self.is_scanning or self.is_surface_scanning:
            return

        self._switch_view("dashboard")
        self.is_scanning = True
        self.is_surface_scanning = True
        self.cancel_scan_flag = False
        self.cancel_surface_flag = False
        self.op_state = OperationState.RUNNING

        self.btn_run_scan.config(state=tk.DISABLED)
        self.btn_full_diagnostic.config(state=tk.DISABLED)
        self.btn_hero_scan.config(state=tk.DISABLED)
        self.btn_start_surface.config(state=tk.DISABLED)
        if hasattr(self, "btn_stop_diag"):
            self.btn_stop_diag.config(state=tk.NORMAL)
        if hasattr(self, "diag_progressbar"):
            self.diag_progressbar["value"] = 0.0

        self.surface_canvas.delete("all")
        self.block_canvas_idx = 0

        self.progress_bar.pack(side=tk.RIGHT, padx=10)
        self.progress_bar.start(10)

        target_disk = self.selected_disk
        self._update_health_gauge(None, "#0EA5E9", "0%")
        self.set_status(f"[1/4] Consultando sensores S.M.A.R.T. e barramento do Disco {target_disk.number}...")
        self.log(f"=== INICIANDO DIAGNÓSTICO COMPLETO 1-CLIQUE EM {target_disk.friendly_name} ({target_disk.device_path}) ===")

        def worker():
            gpt = None
            mbr = None
            fs_list = []
            ntfs_boot_diag = None
            ntfs_target_partition = None
            smart_rep = None
            surf_summary = None

            try:
                # ETAPA 1: S.M.A.R.T. & Sensores
                self.msg_queue.put(("diag_progress", (2.0, "Etapa 1/4: Lendo registradores S.M.A.R.T. e barramento físico...")))
                try:
                    smart_rep = query_smart_report_windows(
                        target_disk.number,
                        target_disk.device_path,
                        target_disk.friendly_name,
                        target_disk.serial_number,
                        is_usb=(target_disk.bus_type.upper() == "USB"),
                        bus_type=target_disk.bus_type,
                    )
                    self.msg_queue.put(("smart_loaded", (smart_rep, target_disk)))
                    self.msg_queue.put(("log", f"[Etapa 1/4] S.M.A.R.T. consultado: {smart_rep.risk_level.value}\n"))
                except Exception as e:
                    self.msg_queue.put(("log", f"[Etapa 1/4] Aviso S.M.A.R.T.: {e}\n"))

                # ETAPA 2: Tabelas de Partição GPT / MBR
                self.msg_queue.put(("diag_progress", (5.0, "Etapa 2/4: Validando assinatura MBR (0x55AA) e CRC32 das tabelas GPT...")))
                self.msg_queue.put(("status", f"[2/4] Verificando tabelas GPT/MBR e CRC32 do Disco {target_disk.number}..."))
                try:
                    with open(target_disk.device_path, "rb") as f_raw:
                        gpt = parse_gpt(f_raw)
                        f_raw.seek(0)
                        mbr = parse_mbr(f_raw)
                except Exception as e:
                    self.msg_queue.put(("log", f"[Etapa 2/4] Erro ao ler tabelas GPT/MBR: {e}\n"))

                # ETAPA 3: Sistemas de Arquivos, Boot Sectors (VBR) & $MFT
                self.msg_queue.put(("diag_progress", (8.0, "Etapa 3/4: Inspecionando Boot Sectors Primário/Backup e registros $MFT...")))
                self.msg_queue.put(("status", f"[3/4] Inspecionando Boot Sectors NTFS/FAT/exFAT e registros $MFT..."))
                fs_list = scan_boot_sectors(
                    target_disk.device_path,
                    max_lba=300_000,
                    cancel_check=lambda: self.cancel_scan_flag,
                )

                if not self.cancel_scan_flag:
                    try:
                        if gpt and gpt.partitions:
                            for p in gpt.partitions:
                                total_p_sectors = p.last_lba - p.first_lba + 1
                                if total_p_sectors > 2048:
                                    diag = diagnose_ntfs_boot_sectors(target_disk.device_path, p.first_lba, total_p_sectors)
                                    if diag.can_repair:
                                        ntfs_boot_diag = diag
                                        ntfs_target_partition = (p.first_lba, total_p_sectors)
                                        break
                                    elif diag.primary_valid and not ntfs_boot_diag:
                                        ntfs_boot_diag = diag
                                        ntfs_target_partition = (p.first_lba, total_p_sectors)
                        elif mbr and mbr.has_valid_signature:
                            for p in mbr.partitions:
                                if not p.is_empty and p.size_sectors > 2048:
                                    diag = diagnose_ntfs_boot_sectors(target_disk.device_path, p.start_lba, p.size_sectors)
                                    if diag.can_repair:
                                        ntfs_boot_diag = diag
                                        ntfs_target_partition = (p.start_lba, p.size_sectors)
                                        break
                                    elif diag.primary_valid and not ntfs_boot_diag:
                                        ntfs_boot_diag = diag
                                        ntfs_target_partition = (p.start_lba, p.size_sectors)
                    except Exception as e:
                        self.msg_queue.put(("log", f"[Etapa 3/4] Aviso na verificação VBR NTFS: {e}\n"))

                self.msg_queue.put(("scan_finished", (gpt, mbr, fs_list, target_disk, ntfs_boot_diag, ntfs_target_partition)))

                # ETAPA 4: Varredura Física de Superfície & Latência (Detecção de Bad Sectors)
                disk_total_sectors = max(1, target_disk.size_bytes // 512)
                surf_max, surf_block, surf_stride, mode_desc = self._compute_surface_scan_params(disk_total_sectors)
                total_gb = (surf_max * 512) / (1024 ** 3)
                self.msg_queue.put((
                    "log",
                    f"[Etapa 4/4] Iniciando teste físico de superfície ({mode_desc}) cobrindo {total_gb:.1f} GB ({surf_max:,} setores)...\n",
                ))
                self.msg_queue.put(("status", f"[4/4] Varrendo superfície física ({mode_desc}) — 0.0% de {total_gb:.1f} GB..."))

                def block_cb(res: SurfaceBlockResult, summary: SurfaceScanSummary):
                    self.msg_queue.put(("surface_block", (res, summary)))

                surf_summary = scan_surface_latency(
                    target_disk.device_path,
                    max_sectors=surf_max,
                    block_sectors=surf_block,
                    stride_sectors=surf_stride,
                    progress_cb=block_cb,
                    cancel_check=lambda: self.cancel_surface_flag or self.cancel_scan_flag,
                )
                self.msg_queue.put(("surface_finished", surf_summary))
                self.msg_queue.put((
                    "full_diag_finished",
                    (target_disk, smart_rep, gpt, mbr, fs_list, ntfs_boot_diag, surf_summary),
                ))
            except Exception as exc:
                self.msg_queue.put(("scan_error", str(exc)))
            finally:
                self.msg_queue.put(("surface_ended", None))
                self.msg_queue.put(("scan_ended", None))

        threading.Thread(target=worker, daemon=True).start()

    def _render_full_diagnostic_verdict(
        self,
        disk: DiskInfo,
        smart_rep: Optional[SmartReport],
        gpt,
        mbr,
        fs_list: list[DiscoveredFilesystem],
        ntfs_boot_diag,
        surf_summary: Optional[SurfaceScanSummary],
    ):
        """Consolida os resultados das 4 etapas do Diagnóstico Completo 1-Clique no Dashboard."""
        faults: list[str] = []
        warnings: list[str] = []

        # Avaliação Camada 5: Boot Sector & $MFT
        if self.misaligned_pair:
            p_idx, fs_obj = self.misaligned_pair
            faults.append(f"Partição {p_idx} desalinhada (aponta para LBA diferente do início real NTFS em {fs_obj.start_lba:,})")
            self._set_check_tile("boot_mft", "✖ DESALINHADO", f"Partição {p_idx} requer realinhamento GPT", "fail")
        elif ntfs_boot_diag and ntfs_boot_diag.can_repair:
            faults.append(f"Boot Sector NTFS primário corrompido no LBA {ntfs_boot_diag.primary_lba:,} (Backup íntegro disponível)")
            self._set_check_tile("boot_mft", "✖ VBR CORROMPIDO", "Restaurável via Backup NTFS", "fail")
        elif ntfs_boot_diag and ntfs_boot_diag.primary_valid:
            self._set_check_tile("boot_mft", "✔ NTFS ÍNTEGRO", f"VBR Primário, Backup e $MFT validados", "ok")
        elif fs_list:
            self._set_check_tile("boot_mft", "✔ VÁLIDO", f"{len(fs_list)} volume(s) com assinatura íntegra", "ok")
        else:
            self._set_check_tile("boot_mft", "✔ VERIFICADO", "Nenhuma corrupção de VBR detectada", "ok")

        # Avaliação Camada 6: Superfície & Bad Sectors
        if surf_summary:
            crit_c = getattr(surf_summary, "critical_delay_count", 0)
            read_mb = getattr(surf_summary, "bytes_read", 0) / (1024 * 1024)
            span_gb = (getattr(surf_summary, "current_span_sector", 0) * 512) / (1024 ** 3)
            tot_span_gb = (getattr(surf_summary, "scanned_span_sectors", 0) * 512) / (1024 ** 3)
            avg_ms = getattr(surf_summary, "avg_latency_ms", 0.0)
            max_ms = getattr(surf_summary, "max_latency_ms", 0.0)
            spd_mbs = getattr(surf_summary, "speed_mb_s", 0.0)

            if hasattr(self, "diag_progressbar"):
                pct_done = (getattr(surf_summary, "current_span_sector", 0) / max(1, getattr(surf_summary, "scanned_span_sectors", 1))) * 100.0
                self.diag_progressbar["value"] = min(100.0, pct_done)
            if hasattr(self, "lbl_diag_live_telemetry"):
                self.lbl_diag_live_telemetry.config(
                    text=(
                        f"Concluído: {span_gb:.1f}/{tot_span_gb:.1f} GB varridos • "
                        f"Lidos: {read_mb:.0f} MB ({surf_summary.total_tested_blocks} blocos) • "
                        f"Vel: {spd_mbs:.1f} MB/s • Lat. Méd/Máx: {avg_ms:.1f}/{max_ms:.1f} ms"
                    )
                )

            if surf_summary.bad_count > 0:
                bad_list = getattr(surf_summary, "bad_lbas", [])
                first_bad = f" (1º em LBA {bad_list[0]:,})" if bad_list else ""
                faults.append(f"{surf_summary.bad_count} Bad Sector(s) físico(s) ilegível(is){first_bad}")
                self._set_check_tile(
                    "surface_io",
                    f"✖ {surf_summary.bad_count} BAD",
                    f"Ilegíveis{first_bad}! Clone imediatamente.",
                    "fail",
                )
            elif crit_c > 0 or surf_summary.very_slow_count > 0:
                warnings.append(f"Setores lentos detectados (Atraso crítico: {crit_c}, Muito lentos: {surf_summary.very_slow_count})")
                self._set_check_tile(
                    "surface_io",
                    "⚠ LENTIDÃO",
                    f"Críticos: {crit_c} | Lentos: {surf_summary.very_slow_count} (Máx {max_ms:.0f}ms)",
                    "warn",
                )
            else:
                self._set_check_tile(
                    "surface_io",
                    "✔ 0 BAD SECTORS",
                    f"{surf_summary.total_tested_blocks} zonas ({span_gb:.1f} GB) • Méd {avg_ms:.1f}ms / {spd_mbs:.0f} MB/s",
                    "ok",
                )

        # Avaliação S.M.A.R.T.
        if smart_rep:
            if smart_rep.risk_level == SmartRiskLevel.RED:
                faults.append("S.M.A.R.T. reportou risco crítico de falha física na mídia")
            elif smart_rep.risk_level == SmartRiskLevel.YELLOW:
                warnings.append("S.M.A.R.T. reportou alerta de atenção (verifique cabo SATA / setores)")

        # Veredito Geral e Pontuação no Medidor Circular
        if faults:
            self._update_health_gauge(25, "#EF4444", "FALHA")
            self.dash_health_badge.config(
                text=f"✖ {len(faults)} FALHA(S) ENCONTRADA(S) NO DISCO",
                background="#7F1D1D",
                foreground="#FCA5A5",
            )
            self.dash_recommendation.config(
                text="FALHAS DETECTADAS: " + " | ".join(faults) + ". Recomendação: Use Clonagem Resiliente ou Reparo Cirúrgico."
            )
            self.set_status(f"Diagnóstico 1-Clique concluído: {len(faults)} falha(s) encontrada(s) no Disco {disk.number}.")
        elif warnings:
            self._update_health_gauge(70, "#F59E0B", "ALERTA")
            self.dash_health_badge.config(
                text=f"⚠ ATENÇÃO: {len(warnings)} ALERTA(S) PREVENTIVO(S)",
                background="#78350F",
                foreground="#FCD34D",
            )
            self.dash_recommendation.config(
                text="ALERTAS: " + " | ".join(warnings) + "."
            )
            self.set_status(f"Diagnóstico 1-Clique concluído: {len(warnings)} alerta(s) encontrado(s) no Disco {disk.number}.")
        else:
            self._update_health_gauge(100, "#10B981", "100% OK")
            self.dash_health_badge.config(
                text="✔ DIAGNÓSTICO COMPLETO: NENHUMA FALHA ENCONTRADA",
                background="#064E3B",
                foreground="#34D399",
            )
            self.dash_recommendation.config(
                text="Laudo 1-Clique: Todas as camadas inspecionadas (S.M.A.R.T., Cabo/Barramento, Temperatura, Tabela GPT/MBR, Boot Sector/$MFT e Leitura Física de Superfície) estão íntegras e sem falhas."
            )
            self.set_status(f"Diagnóstico Completo 1-Clique concluído no Disco {disk.number}: 0 falhas encontradas.")

        self.log(f"=== DIAGNÓSTICO 1-CLIQUE FINALIZADO: {len(faults)} falha(s), {len(warnings)} alerta(s) ===\n")

    # ── VARREDURA PROFUNDA DE GEOMETRIA & PARTIÇÕES ───────────────────────────
    def _start_deep_scan(self):
        if not self.selected_disk or not self.selected_disk.can_open_raw:
            messagebox.showerror("Erro de Permissão", "Sem permissão de acesso físico direto (Raw) ou disco não selecionado. Execute como Administrador.")
            return

        self.is_scanning = True
        self.cancel_scan_flag = False
        self.op_state = OperationState.RUNNING
        self.btn_run_scan.config(state=tk.DISABLED)
        self.progress_bar.pack(side=tk.RIGHT, padx=10)
        self.progress_bar.start(10)
        self.set_status(f"Analisando geometria do Disco {self.selected_disk.number}...")
        self.log(f"Iniciando varredura profunda de boot sectors e partições em {self.selected_disk.device_path}...")

        target_disk = self.selected_disk

        def worker():
            gpt = None
            mbr = None
            fs_list = []
            ntfs_boot_diag = None
            ntfs_target_partition = None
            try:
                try:
                    with open(target_disk.device_path, "rb") as f:
                        gpt = parse_gpt(f)
                        f.seek(0)
                        mbr = parse_mbr(f)
                except Exception as e:
                    self.msg_queue.put(("log", f"Erro ao ler tabelas de partição: {e}\n"))

                fs_list = scan_boot_sectors(
                    target_disk.device_path,
                    max_lba=1_500_000,
                    cancel_check=lambda: self.cancel_scan_flag,
                )

                if not self.cancel_scan_flag:
                    try:
                        if gpt and gpt.partitions:
                            for p in gpt.partitions:
                                total_p_sectors = p.last_lba - p.first_lba + 1
                                if total_p_sectors > 2048:
                                    diag = diagnose_ntfs_boot_sectors(target_disk.device_path, p.first_lba, total_p_sectors)
                                    if diag.can_repair:
                                        ntfs_boot_diag = diag
                                        ntfs_target_partition = (p.first_lba, total_p_sectors)
                                        break
                                    elif diag.primary_valid and not ntfs_boot_diag:
                                        ntfs_boot_diag = diag
                                        ntfs_target_partition = (p.first_lba, total_p_sectors)
                        elif mbr and mbr.has_valid_signature:
                            for p in mbr.partitions:
                                if not p.is_empty and p.size_sectors > 2048:
                                    diag = diagnose_ntfs_boot_sectors(target_disk.device_path, p.start_lba, p.size_sectors)
                                    if diag.can_repair:
                                        ntfs_boot_diag = diag
                                        ntfs_target_partition = (p.start_lba, p.size_sectors)
                                        break
                                    elif diag.primary_valid and not ntfs_boot_diag:
                                        ntfs_boot_diag = diag
                                        ntfs_target_partition = (p.start_lba, p.size_sectors)
                        elif fs_list:
                            for fs in fs_list:
                                if fs.fs_type == "NTFS":
                                    diag = diagnose_ntfs_boot_sectors(target_disk.device_path, fs.start_lba, fs.total_sectors)
                                    if diag.can_repair:
                                        ntfs_boot_diag = diag
                                        ntfs_target_partition = (fs.start_lba, fs.total_sectors)
                                        break
                                    elif not ntfs_boot_diag:
                                        ntfs_boot_diag = diag
                                        ntfs_target_partition = (fs.start_lba, fs.total_sectors)
                    except Exception as e:
                        self.msg_queue.put(("log", f"Aviso durante diagnóstico de boot sectors: {e}\n"))

                self.msg_queue.put(("scan_finished", (gpt, mbr, fs_list, target_disk, ntfs_boot_diag, ntfs_target_partition)))
            except Exception as exc:
                self.msg_queue.put(("scan_error", str(exc)))
            finally:
                self.msg_queue.put(("scan_ended", None))

        threading.Thread(target=worker, daemon=True).start()

    def _render_scan_results(self, gpt, mbr, fs_list, disk: DiskInfo, ntfs_boot_diag=None, ntfs_target_partition=None):
        self.is_scanning = False
        self.progress_bar.stop()
        self.progress_bar.pack_forget()
        self.btn_run_scan.config(state=tk.NORMAL)
        if self.cancel_scan_flag:
            self.set_status(f"Análise de geometria do Disco {disk.number} cancelada.")
        else:
            self.set_status(f"Análise de geometria do Disco {disk.number} concluída.")

        self.current_gpt = gpt
        self.current_mbr = mbr
        self.current_filesystems = fs_list
        self.misaligned_pair = None
        self.ntfs_boot_diag = ntfs_boot_diag
        self.ntfs_target_partition = ntfs_target_partition

        self._populate_partition_views(gpt, mbr)

        self.txt_fs.delete("1.0", tk.END)
        if fs_list:
            self.txt_fs.insert(tk.END, f"✔ Encontrado(s) {len(fs_list)} sistema(s) de arquivos real(is) na mídia física:\n\n")
            for idx, fs in enumerate(fs_list, 1):
                ver = " [MFT Íntegra ✔]" if fs.is_verified else ""
                self.txt_fs.insert(tk.END, f"  [{idx}] {fs.fs_type}{ver}\n")
                self.txt_fs.insert(tk.END, f"      Início no LBA: {fs.start_lba:,} .. Fim: {fs.end_lba:,} ({fs.size_gb:.2f} GB)\n")
                self.txt_fs.insert(tk.END, f"      Tamanho de Setor: {getattr(fs, 'sector_size', 512)} bytes\n\n")
        else:
            self.txt_fs.insert(tk.END, "Nenhum sistema de arquivos detectado nos setores investigados.\n")

        has_misalignment = False
        if gpt and fs_list:
            for p in gpt.partitions:
                for fs in fs_list:
                    if fs.fs_type == "NTFS" and fs.is_verified:
                        if p.first_lba != fs.start_lba and p.size_gb() > 10:
                            has_misalignment = True
                            self.misaligned_pair = (p.index, fs)
                            diff = abs(fs.start_lba - p.first_lba)
                            self.lbl_repair_diagnosis.config(
                                text=f"🚨 DESALINHAMENTO DETECTADO: Partição {p.index} aponta para LBA {p.first_lba:,}, "
                                     f"mas o sistema de arquivos real começa no LBA {fs.start_lba:,} (diferença de {diff:,} setores)!"
                            )
                            self.btn_repair.config(state=tk.NORMAL)
                            break
                if has_misalignment:
                    break

        if not has_misalignment:
            if ntfs_boot_diag and ntfs_boot_diag.can_repair:
                self.lbl_repair_diagnosis.config(
                    text=f"🚨 VBR CORROMPIDO: Boot Sector primário NTFS no LBA {ntfs_boot_diag.primary_lba:,} inválido, "
                         f"mas Backup íntegro no LBA {ntfs_boot_diag.backup_lba:,}! Restauração recomendada."
                )
            else:
                self.lbl_repair_diagnosis.config(text="✔ Geometria Consistente: A tabela de partições coincide com os sistemas de arquivos reais.")
            self.btn_repair.config(state=tk.DISABLED)

        if ntfs_boot_diag and ntfs_boot_diag.can_repair:
            self.btn_repair_boot.config(state=tk.NORMAL)
        else:
            self.btn_repair_boot.config(state=tk.DISABLED)

    # ── REPAROS CIRÚRGICOS ────────────────────────────────────────────────────
    def _execute_boot_restore(self):
        if not self.selected_disk:
            messagebox.showwarning("Aviso", "Selecione um disco de origem.")
            return

        target_disk = self.selected_disk
        diag = self.ntfs_boot_diag
        part_info = self.ntfs_target_partition

        if not diag or not part_info:
            if self.current_gpt and self.current_gpt.partitions:
                p = self.current_gpt.partitions[0]
                part_info = (p.first_lba, p.last_lba - p.first_lba + 1)
                diag = diagnose_ntfs_boot_sectors(target_disk.device_path, part_info[0], part_info[1])
            elif self.current_mbr and self.current_mbr.partitions:
                p_cand = [p for p in self.current_mbr.partitions if not p.is_empty]
                if p_cand:
                    p = p_cand[0]
                    part_info = (p.start_lba, p.size_sectors)
                    diag = diagnose_ntfs_boot_sectors(target_disk.device_path, part_info[0], part_info[1])
            elif self.current_filesystems:
                for fs in self.current_filesystems:
                    if fs.fs_type == "NTFS":
                        part_info = (fs.start_lba, fs.total_sectors)
                        diag = diagnose_ntfs_boot_sectors(target_disk.device_path, part_info[0], part_info[1])
                        break

        if not diag:
            messagebox.showinfo(
                "Diagnóstico Requerido",
                "Nenhuma partição candidata a NTFS identificada. Execute a 'Varredura de Geometria' primeiro."
            )
            return

        if diag.primary_valid:
            messagebox.showinfo(
                "Setor Íntegro",
                f"O setor de boot primário NTFS no LBA {diag.primary_lba:,} já é válido e validado com $MFT.\n\n"
                "Nenhuma restauração necessária."
            )
            return

        if not diag.backup_valid:
            messagebox.showerror(
                "Backup Inconsistente",
                f"Não foi possível restaurar: o setor de backup no LBA {diag.backup_lba:,} também está inválido ou ausente."
            )
            return

        if not diag.can_repair:
            messagebox.showwarning(
                "Não Reparável",
                "As condições de integridade para restauração segura não foram atendidas.\n\n" + diag.details
            )
            return

        confirm = messagebox.askyesno(
            "⚠️ Confirmar Restauração de Boot Sector NTFS",
            f"Disco: {target_disk.friendly_name} ({target_disk.device_path})\n\n"
            f"• Setor Primário Corrompido: LBA {diag.primary_lba:,}\n"
            f"• Setor Backup Íntegro: LBA {diag.backup_lba:,}\n\n"
            f"Um snapshot preventivo do setor primário será gravado na pasta 'backups/' antes de copiar o backup.\n\n"
            f"Deseja regravar o setor primário a partir do backup agora?"
        )
        if not confirm:
            return

        self.is_repairing = True
        self.op_state = OperationState.RUNNING
        self.btn_repair_boot.config(state=tk.DISABLED)
        self.set_status("Restaurando Boot Sector NTFS via Backup...")
        self.log(f"Iniciando restauração de Boot Sector NTFS no LBA {diag.primary_lba:,} a partir do backup {diag.backup_lba:,}...")

        start_lba, total_sec = part_info

        def worker():
            try:
                res = restore_ntfs_boot_from_backup(
                    target_disk.device_path,
                    start_lba,
                    total_sec
                )
                self.msg_queue.put(("boot_restore_result", res))
            except Exception as exc:
                self.msg_queue.put(("boot_restore_error", str(exc)))

        threading.Thread(target=worker, daemon=True).start()

    def _execute_repair(self):
        if not self.selected_disk or not self.misaligned_pair:
            return
        part_idx, fs = self.misaligned_pair
        target_disk = self.selected_disk

        if not messagebox.askyesno(
            "⚠️ Confirmação Crítica de Reparo de Partição",
            f"Você está prestes a regravar as tabelas GPT Primária e Backup do disco {target_disk.friendly_name} ({target_disk.device_path}).\n\n"
            f"Realinhamento: Partição {part_idx} -> Novo LBA {fs.start_lba:,}.\n\n"
            f"Snapshots binários preventivos das tabelas GPT Primária e Backup serão salvos na pasta backups/ antes de qualquer alteração física.\n\n"
            f"Deseja prosseguir com a gravação?"
        ):
            return

        self.is_repairing = True
        self.op_state = OperationState.RUNNING
        self.btn_repair.config(state=tk.DISABLED)
        self.set_status("Realinhando partição GPT...")
        self.log(f"Iniciando realinhamento da partição {part_idx} para LBA {fs.start_lba:,} em {target_disk.device_path}...")

        def worker():
            try:
                res = repair_gpt_partition_offset(target_disk.device_path, part_idx, fs)
                self.msg_queue.put(("repair_result", res))
            except Exception as exc:
                self.msg_queue.put(("repair_error", str(exc)))

        threading.Thread(target=worker, daemon=True).start()

    # ── CLONAGEM RESILIENTE (MODO DDRESCUE) ────────────────────────────────────
    def _browse_image_dest(self):
        dest = filedialog.asksaveasfilename(
            title="Salvar Imagem Bruta Resiliente",
            defaultextension=".img",
            filetypes=[("Imagem Bruta (.img)", "*.img"), ("Todos os Arquivos", "*.*")]
        )
        if dest:
            self.txt_dest_image.delete(0, tk.END)
            self.txt_dest_image.insert(0, dest)

    def _start_clone(self):
        if not self.selected_disk:
            messagebox.showwarning("Aviso", "Selecione um disco de origem.")
            return
        dest = self.txt_dest_image.get().strip()
        if not dest:
            messagebox.showwarning("Aviso", "Defina o caminho do arquivo de imagem de destino.")
            return

        target_disk = self.selected_disk

        try:
            assert_safe_source_and_destination(target_disk.device_path, Path(dest))
        except ValueError as err:
            messagebox.showerror("Bloqueio de Segurança P0", str(err))
            return

        self.btn_start_clone.config(state=tk.DISABLED)
        self.btn_cancel_clone.config(state=tk.NORMAL)

        dest_path = Path(dest)
        hw_sector_size = get_device_sector_size(target_disk.device_path)
        if is_physical_device_path(target_disk.device_path) and hw_sector_size is None:
            messagebox.showerror(
                "Erro de Geometria",
                f"Não foi possível consultar o tamanho de setor lógico do dispositivo físico '{target_disk.device_path}'.\n"
                "Operação recusada antes da escrita para evitar desalinhamento de LBAs."
            )
            self.btn_start_clone.config(state=tk.NORMAL)
            self.btn_cancel_clone.config(state=tk.DISABLED)
            return

        sector_size = hw_sector_size if hw_sector_size is not None else 512
        total_sectors = target_disk.size_bytes // sector_size

        try:
            cloner = ResilientCloner(
                target_disk.device_path,
                dest_path,
                total_sectors,
                sector_size=sector_size,
                allow_overwrite=False,
            )
        except (FileExistsError, ValueError) as err:
            err_msg = str(err)
            is_overwrite_block = isinstance(err, FileExistsError) or (
                "Operação bloqueada por segurança" in err_msg
                or "Tamanho da imagem existente" in err_msg
                or "Operação recusada por padrão" in err_msg
            )
            if is_overwrite_block:
                confirm_restart = messagebox.askyesno(
                    "⚠️ Confirmar Reinicialização e Sobrescrita de Imagem",
                    f"{err_msg}\n\n"
                    f"ATENÇÃO: Os dados existentes na imagem de destino '{dest_path}' PODEM SER SOBRESCRITOS "
                    "caso você reinicialize uma nova clonagem do zero.\n\n"
                    "Deseja autorizar explicitamente a reinicialização e sobrescrever a imagem existente?"
                )
                if not confirm_restart:
                    self.log("Clonagem cancelada pelo operador: imagem e mapa existentes preservados intactos.\n")
                    self.btn_start_clone.config(state=tk.NORMAL)
                    self.btn_cancel_clone.config(state=tk.DISABLED)
                    return
                try:
                    cloner = ResilientCloner(
                        target_disk.device_path,
                        dest_path,
                        total_sectors,
                        sector_size=sector_size,
                        allow_overwrite=True,
                    )
                except Exception as retry_err:
                    messagebox.showerror("Erro de Inicialização", str(retry_err))
                    self.btn_start_clone.config(state=tk.NORMAL)
                    self.btn_cancel_clone.config(state=tk.DISABLED)
                    return
            else:
                messagebox.showerror("Erro de Inicialização", err_msg)
                self.btn_start_clone.config(state=tk.NORMAL)
                self.btn_cancel_clone.config(state=tk.DISABLED)
                return
        except Exception as err:
            messagebox.showerror("Erro de Inicialização", str(err))
            self.btn_start_clone.config(state=tk.NORMAL)
            self.btn_cancel_clone.config(state=tk.DISABLED)
            return

        self.cloner_instance = cloner
        self.op_state = OperationState.RUNNING

        def progress_cb(stats: ResilientCloneStats):
            self.msg_queue.put(("clone_stats", stats))

        def worker():
            try:
                res = cloner.run(progress_cb=progress_cb)
                if not res.success:
                    status_text = "Clonagem interrompida pelo operador. Mapa salvo para retomada."
                elif res.fully_recovered:
                    status_text = f"Clonagem 100% concluída: {res.rescued_sectors:,} setores resgatados com sucesso."
                else:
                    status_text = (
                        f"Clonagem concluída com resgate parcial: {res.rescued_sectors:,} setores OK, "
                        f"{res.bad_sectors:,} bad sectors identificados."
                    )
                self.msg_queue.put(("clone_finished", (res, status_text)))
            except Exception as exc:
                self.msg_queue.put(("clone_error", str(exc)))
            finally:
                self.msg_queue.put(("clone_ended", None))

        threading.Thread(target=worker, daemon=True).start()

    def _render_clone_stats(self, stats: ResilientCloneStats):
        pct = (stats.current_sector / max(1, stats.total_sectors)) * 100
        self.clone_progressbar["value"] = pct
        self.lbl_clone_phase.config(text=f"Fase Atual: {stats.phase}")

        self.lbl_clone_pos.config(text=f"{stats.current_sector:,} / {stats.total_sectors:,} ({pct:.1f}%)")
        self.lbl_clone_rescued.config(text=f"{stats.rescued_sectors:,}")
        self.lbl_clone_bad.config(text=f"{stats.bad_sectors:,}")
        self.lbl_clone_speed.config(text=f"{stats.speed_mb_s:.1f} MB/s")

    def _cancel_clone(self):
        if self.cloner_instance:
            self.op_state = OperationState.CANCELLING
            self.cloner_instance.cancel()
            self.btn_cancel_clone.config(state=tk.DISABLED)
            self.log("Solicitado cancelamento cooperativo e salvamento de mapa (.map)...")

    # ── FILE CARVING FORENSE ──────────────────────────────────────────────────
    def _browse_carve_dir(self):
        d = filedialog.askdirectory(title="Selecione pasta de destino dos arquivos recuperados")
        if d:
            self.txt_carve_dir.delete(0, tk.END)
            self.txt_carve_dir.insert(0, d)

    def _open_carve_folder(self):
        out_str = self.txt_carve_dir.get().strip()
        if out_str and Path(out_str).exists():
            os.startfile(out_str)
        else:
            messagebox.showinfo("Informação", "Pasta de destino ainda não foi criada.")

    def _start_carver(self):
        if not self.selected_disk:
            messagebox.showwarning("Aviso", "Selecione um disco de origem.")
            return
        out_str = self.txt_carve_dir.get().strip()
        if not out_str:
            messagebox.showwarning("Aviso", "Defina a pasta de destino do carving.")
            return
        out_path = Path(out_str)
        target_disk = self.selected_disk

        try:
            assert_safe_source_and_destination(target_disk.device_path, out_path, is_directory=True)
        except ValueError as err:
            messagebox.showerror("Bloqueio de Segurança P0", str(err))
            return

        out_path.mkdir(parents=True, exist_ok=True)

        for item in self.carver_tree.get_children():
            self.carver_tree.delete(item)

        self.btn_start_carve.config(state=tk.DISABLED)
        self.btn_cancel_carve.config(state=tk.NORMAL)
        self.op_state = OperationState.RUNNING
        self.set_status("File Carving em execução nos setores brutos...")
        carver = FileCarver(target_disk.device_path, out_path)
        self.carver_instance = carver

        def progress_cb(cur, total, count):
            self.msg_queue.put(("status", f"Varrendo LBA {cur:,} de {total:,} — {count} arquivos resgatados"))

        def worker():
            try:
                files = carver.scan(max_sectors=1_000_000, progress_cb=progress_cb)
                for f in files:
                    self.msg_queue.put(("carver_file", f))
                if carver.cancel_requested:
                    self.msg_queue.put(("status", f"File Carving cancelado: {len(files)} arquivos resgatados até a interrupção."))
                else:
                    self.msg_queue.put(("status", f"File Carving finalizado: {len(files)} arquivos resgatados com sucesso!"))
                self.msg_queue.put(("log", f"File Carving finalizado: {len(files)} arquivos gravados em {out_path}\n"))
                self.msg_queue.put(("carver_finished", (carver.cancel_requested, len(carver.corrupted_files) > 0)))
            except Exception as exc:
                self.msg_queue.put(("carver_error", str(exc)))
            finally:
                self.msg_queue.put(("carver_ended", None))

        threading.Thread(target=worker, daemon=True).start()

    def _cancel_carver(self):
        if getattr(self, "carver_instance", None) and hasattr(self.carver_instance, "cancel"):
            self.op_state = OperationState.CANCELLING
            self.carver_instance.cancel()
            self.btn_cancel_carve.config(state=tk.DISABLED)
            self.set_status("Interrupção do carver solicitada. Aguardando término cooperativo...")
            self.log("Interrupção do File Carver solicitada pelo usuário.\n")

    # ── VARREDURA DE SUPERFÍCIE (HEATMAP) ─────────────────────────────────────
    def _start_surface_scan(self):
        if not self.selected_disk:
            messagebox.showwarning("Aviso", "Selecione um disco.")
            return

        self.surface_canvas.delete("all")
        self.btn_start_surface.config(state=tk.DISABLED)
        self.btn_cancel_surface.config(state=tk.NORMAL)
        self.cancel_surface_flag = False
        self.is_surface_scanning = True
        self.op_state = OperationState.RUNNING
        target_disk = self.selected_disk
        self.block_canvas_idx = 0

        disk_total_sectors = max(1, target_disk.size_bytes // 512)
        surf_max, surf_block, surf_stride, mode_desc = self._compute_surface_scan_params(disk_total_sectors)

        def block_cb(res: SurfaceBlockResult, summary: SurfaceScanSummary):
            self.msg_queue.put(("surface_block", (res, summary)))

        def worker():
            try:
                summary = scan_surface_latency(
                    target_disk.device_path,
                    max_sectors=surf_max,
                    block_sectors=surf_block,
                    stride_sectors=surf_stride,
                    progress_cb=block_cb,
                    cancel_check=lambda: self.cancel_surface_flag
                )
                self.msg_queue.put(("surface_finished", summary))
            except Exception as exc:
                self.msg_queue.put(("surface_error", str(exc)))
            finally:
                self.msg_queue.put(("surface_ended", None))

        threading.Thread(target=worker, daemon=True).start()

    def _draw_surface_block(self, res: SurfaceBlockResult, summary: SurfaceScanSummary):
        colors = {
            "fast": "#22c55e",
            "good": "#3b82f6",
            "slow": "#eab308",
            "very_slow": "#f97316",
            "critical_delay": "#dc2626",
            "bad": "#7f1d1d"
        }
        color = colors.get(res.status, "#ef4444")

        box_size = 12
        gap = 3
        stride = box_size + gap
        canvas_w = max(self.surface_canvas.winfo_width(), 820)
        cols = max(20, (canvas_w - 24) // stride)
        x = (self.block_canvas_idx % cols) * stride + 14
        y = (self.block_canvas_idx // cols) * stride + 14

        self.surface_canvas.create_rectangle(x, y, x + box_size, y + box_size, fill=color, outline="")
        self.block_canvas_idx += 1

        self.leg_fast.config(text=f"< 20ms: {summary.fast_count}")
        self.leg_good.config(text=f"< 100ms: {summary.good_count}")
        self.leg_slow.config(text=f"< 250ms: {summary.slow_count}")
        self.leg_vslow.config(text=f"< 1000ms: {summary.very_slow_count}")
        crit_count = getattr(summary, "critical_delay_count", 0)
        self.leg_crit.config(text=f"Atraso Crítico: {crit_count}")
        self.leg_bad.config(text=f"Bad Sector: {summary.bad_count}")

        cur_span = getattr(summary, "current_span_sector", res.lba)
        tot_span = max(1, getattr(summary, "scanned_span_sectors", max(1, cur_span)))
        pct = min(100.0, (cur_span / tot_span) * 100.0)
        cur_gb = (cur_span * 512) / (1024 ** 3)
        tot_gb = (tot_span * 512) / (1024 ** 3)
        spd = getattr(summary, "speed_mb_s", 0.0)

        live_str = (
            f"Posição: {cur_gb:.1f}/{tot_gb:.1f} GB ({pct:.1f}%) • "
            f"LBA: {res.lba:,} • Vel: {spd:.1f} MB/s • "
            f"Latência: {res.latency_ms:.1f} ms • Blocos: {summary.total_tested_blocks:,} (Bad: {summary.bad_count})"
        )
        self.lbl_surface_status.config(text=live_str)

        if hasattr(self, "diag_progressbar"):
            self.diag_progressbar["value"] = pct
        if hasattr(self, "lbl_diag_live_telemetry"):
            self.lbl_diag_live_telemetry.config(text=live_str)
        if self.is_scanning and hasattr(self, "lbl_gauge_score"):
            self.lbl_gauge_score.config(text=f"{pct:.0f}%")
        if self.is_scanning:
            self.lbl_status.config(text=f"[4/4] Varrendo superfície física: {live_str}")
            if summary.bad_count > 0:
                self._set_check_tile(
                    "surface_io",
                    f"✖ {summary.bad_count} BAD",
                    f"Lendo {cur_gb:.1f}/{tot_gb:.1f} GB ({pct:.0f}%) • {spd:.0f} MB/s",
                    "fail",
                )
            else:
                self._set_check_tile(
                    "surface_io",
                    f"LENDO {pct:.0f}%",
                    f"{cur_gb:.1f}/{tot_gb:.1f} GB • {res.latency_ms:.1f}ms • {spd:.0f} MB/s",
                    "info",
                )

    def _cancel_surface(self):
        self.cancel_surface_flag = True
        if self.is_surface_scanning:
            self.op_state = OperationState.CANCELLING
            self.btn_cancel_surface.config(state=tk.DISABLED)

    # ── LOGS E EXPORTAÇÃO ─────────────────────────────────────────────────────
    def _save_log_file(self):
        path = filedialog.asksaveasfilename(
            title="Salvar Log Forense",
            defaultextension=".txt",
            filetypes=[("Arquivo de Texto (.txt)", "*.txt"), ("Todos os Arquivos", "*.*")]
        )
        if path:
            Path(path).write_text(self.txt_console.get("1.0", tk.END), encoding="utf-8")
            messagebox.showinfo("Sucesso", f"Relatório de log salvo em:\n{path}")


def run_gui():
    _enable_high_dpi()
    root = tk.Tk()
    app = DiskRescueApp(root)
    root.mainloop()


if __name__ == "__main__":
    run_gui()
