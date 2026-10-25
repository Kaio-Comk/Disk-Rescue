"""
gui/app.py — Interface Gráfica Moderna e Profissional (GUI) do DiskRescue v0.3.3.
Construída com Tkinter + Sun Valley (sv_ttk) + Design System Fluent Obsidian/Slate,
com suporte nativo a High-DPI Per-Monitor v2 e Barra de Título Escura DWM do Windows 11.
Oferece Navegação por Sidebar, Dashboard de Saúde, Diagnóstico & Reparo de Partições,
Telemetria SMART ATA, Clonagem Resiliente em Duas Fases (estilo GNU ddrescue),
File Carving por Assinaturas Mágicas, Heatmap de Latência de Superfície e Log Forense.
"""
from __future__ import annotations

import ctypes
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
    from PIL import Image, ImageTk
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
        "bg_root": "#090D16",
        "bg_header": "#0D1322",
        "bg_sidebar": "#0B101D",
        "bg_card": "#111827",
        "bg_card_alt": "#162033",
        "bg_input": "#070B12",
        "border": "#1E293B",
        "border_subtle": "#172033",
        "border_accent": "#0284C7",
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
        "tree_bg": "#0D1322",
        "tree_alt": "#111A2E",
        "tree_head_bg": "#162033",
    },
    "light": {
        "bg_root": "#F1F5F9",
        "bg_header": "#FFFFFF",
        "bg_sidebar": "#F8FAFC",
        "bg_card": "#FFFFFF",
        "bg_card_alt": "#F8FAFC",
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
    """Ativa High-DPI Per-Monitor v2 no Windows para fontes e bordas ultra nítidas."""
    if os.name != "nt":
        return
    try:
        # DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2 = -4
        ctypes.windll.user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))
        return
    except Exception:
        pass
    try:
        # PROCESS_PER_MONITOR_DPI_AWARE = 2
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

        # DWMWA_USE_IMMERSIVE_DARK_MODE = 20 (fallback 19 em builds antigas do Win10)
        val = ctypes.c_int(1 if dark else 0)
        res = dwmapi.DwmSetWindowAttribute(hwnd, 20, ctypes.byref(val), ctypes.sizeof(val))
        if res != 0:
            dwmapi.DwmSetWindowAttribute(hwnd, 19, ctypes.byref(val), ctypes.sizeof(val))

        # No Windows 11 (Build 22000+), DWMWA_CAPTION_COLOR = 35 e DWMWA_TEXT_COLOR = 36 (formato COLORREF 0x00BBGGRR)
        caption_color = ctypes.c_int(0x0022130D if dark else 0x00FFFFFF)
        dwmapi.DwmSetWindowAttribute(hwnd, 35, ctypes.byref(caption_color), ctypes.sizeof(caption_color))
    except Exception:
        pass


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
        self.root.title("DiskRescue v0.3.3 — Diagnóstico, Clonagem Resiliente & Recuperação Forense")
        self.root.geometry("1280x850")
        self.root.minsize(1100, 740)

        # Listas para sincronização instantânea de tema em todos os containers customizados
        self._themed_cards: list[tk.Widget] = []
        self._themed_card_alts: list[tk.Widget] = []
        self._themed_headers: list[tk.Widget] = []
        self._themed_sidebars: list[tk.Widget] = []
        self._themed_roots: list[tk.Widget] = []
        self._themed_dividers: list[tk.Frame] = []
        self._themed_text_widgets: list[tk.Text] = []

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
        self.nav_buttons: dict[str, ttk.Button] = {}
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
                header_img = img.resize((36, 36), resample)
                self.header_logo_photo = ImageTk.PhotoImage(header_img)
        except Exception:
            pass

    # ── SISTEMA DE ESTILOS TTK CUSTOMIZADO ────────────────────────────────────
    def _configure_custom_styles(self):
        p = self.palette
        style = ttk.Style(self.root)

        # Frames principais
        style.configure("Root.TFrame", background=p["bg_root"])
        style.configure("Header.TFrame", background=p["bg_header"])
        style.configure("Sidebar.TFrame", background=p["bg_sidebar"])
        style.configure("Card.TFrame", background=p["bg_card"])
        style.configure("CardAlt.TFrame", background=p["bg_card_alt"])

        # Labels contextuais
        style.configure("HeaderTitle.TLabel", background=p["bg_header"], foreground=p["text_primary"], font=("Segoe UI Semibold", 15, "bold"))
        style.configure("HeaderSub.TLabel", background=p["bg_header"], foreground=p["text_secondary"], font=("Segoe UI", 8))
        style.configure("SidebarSection.TLabel", background=p["bg_sidebar"], foreground=p["text_muted"], font=("Segoe UI Semibold", 8, "bold"))
        style.configure("CardTitle.TLabel", background=p["bg_card"], foreground=p["text_primary"], font=("Segoe UI Semibold", 10, "bold"))
        style.configure("CardSub.TLabel", background=p["bg_card"], foreground=p["text_secondary"], font=("Segoe UI", 8))
        style.configure("CardBody.TLabel", background=p["bg_card"], foreground=p["text_primary"], font=("Segoe UI", 9))
        style.configure("CardMuted.TLabel", background=p["bg_card"], foreground=p["text_secondary"], font=("Segoe UI", 8))
        style.configure("AltTitle.TLabel", background=p["bg_card_alt"], foreground=p["text_primary"], font=("Segoe UI Semibold", 9, "bold"))
        style.configure("AltMuted.TLabel", background=p["bg_card_alt"], foreground=p["text_secondary"], font=("Segoe UI", 8))
        style.configure("AltMetric.TLabel", background=p["bg_card_alt"], foreground=p["text_primary"], font=("Segoe UI Semibold", 14, "bold"))
        style.configure("ViewTitle.TLabel", background=p["bg_root"], foreground=p["text_primary"], font=("Segoe UI Semibold", 13, "bold"))
        style.configure("ViewSub.TLabel", background=p["bg_root"], foreground=p["text_secondary"], font=("Segoe UI", 9))
        style.configure("Status.TLabel", background=p["bg_header"], foreground=p["text_secondary"], font=("Segoe UI", 9))

        # Botões da Sidebar (alinhados à esquerda com respiro moderno)
        style.configure(
            "Nav.TButton",
            font=("Segoe UI Semibold", 9),
            anchor="w",
            padding=(12, 9),
        )
        style.configure(
            "NavActive.TButton",
            font=("Segoe UI Semibold", 9, "bold"),
            anchor="w",
            padding=(12, 9),
        )

        # Tabelas Treeview modernas (linhas mais altas, cabeçalho limpo)
        style.configure(
            "Treeview",
            background=p["tree_bg"],
            fieldbackground=p["tree_bg"],
            foreground=p["text_primary"],
            rowheight=30,
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

    # ── COMPONENTES VISUAIS REUTILIZÁVEIS (CARDS ELEVADOS & TERMINAIS) ────────
    def _make_card(
        self,
        parent: tk.Widget,
        title: Optional[str] = None,
        subtitle: Optional[str] = None,
        badge_text: Optional[str] = None,
        padding: tuple[int, int] = (14, 12),
        alt: bool = False,
        accent_top: Optional[str] = None,
    ) -> tuple[tk.Frame, ttk.Frame]:
        """Cria um Card moderno com borda sutil de 1px, substituindo o antigo ttk.LabelFrame dos anos 90."""
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
            hdr.pack(fill=tk.X, pady=(0, 8))

            lbl_t = ttk.Label(hdr, text=title, style=title_style)
            lbl_t.pack(side=tk.LEFT, anchor=tk.W)

            if badge_text:
                lbl_b = ttk.Label(
                    hdr,
                    text=f" {badge_text} ",
                    font=("Segoe UI Semibold", 8, "bold"),
                    background=p["accent_soft_bg"],
                    foreground=p["accent_soft_fg"],
                    padding=(6, 2),
                )
                lbl_b.pack(side=tk.RIGHT, anchor=tk.E)

            if subtitle:
                lbl_s = ttk.Label(inner, text=subtitle, style=sub_style, wraplength=900)
                lbl_s.pack(anchor=tk.W, pady=(0, 8))

        body = ttk.Frame(inner, style=style_name)
        body.pack(fill=tk.BOTH, expand=True)
        return outer, body

    def _make_styled_text(self, parent: tk.Widget, wrap: str = tk.NONE) -> tuple[tk.Frame, tk.Text]:
        """Cria um painel de texto/código escuro moderno com borda sutil e tags coloridas."""
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

        # 1. Header Superior (Barra de Título Moderna)
        self._build_top_header()

        # Linha divisória sutil de 1px
        div_top = tk.Frame(self.root, bg=p["border"], height=1, bd=0, highlightthickness=0)
        div_top.pack(fill=tk.X)
        self._themed_dividers.append(div_top)

        # 2. Container Central Dividido (Sidebar de Navegação + Conteúdo Dinâmico)
        self.body_container = ttk.Frame(self.root, style="Root.TFrame")
        self.body_container.pack(fill=tk.BOTH, expand=True)

        # Sidebar à Esquerda
        self._build_sidebar()

        # Divisor vertical de 1px
        div_vert = tk.Frame(self.body_container, bg=p["border"], width=1, bd=0, highlightthickness=0)
        div_vert.pack(side=tk.LEFT, fill=tk.Y)
        self._themed_dividers.append(div_vert)

        # Área de Conteúdo à Direita
        self.content_area = ttk.Frame(self.body_container, style="Root.TFrame", padding=(18, 14))
        self.content_area.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

        # Card de Seleção e Resumo do Disco Ativo (fixo no topo do conteúdo)
        self._build_drive_selector_banner()

        # Container onde as telas/views alternam
        self.view_container = ttk.Frame(self.content_area, style="Root.TFrame")
        self.view_container.pack(fill=tk.BOTH, expand=True, pady=(10, 0))

        # Construir todas as telas no container
        self._build_views()

        # Divisor inferior de 1px
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
        self.header_frame = ttk.Frame(self.root, style="Header.TFrame", padding=(20, 12))
        self.header_frame.pack(fill=tk.X, side=tk.TOP)

        # Bloco da Marca / Título
        brand_box = ttk.Frame(self.header_frame, style="Header.TFrame")
        brand_box.pack(side=tk.LEFT, anchor=tk.CENTER)

        if self.header_logo_photo is not None:
            logo_lbl = ttk.Label(brand_box, image=self.header_logo_photo, style="HeaderTitle.TLabel")
            logo_lbl.pack(side=tk.LEFT, padx=(0, 12))

        text_stack = ttk.Frame(brand_box, style="Header.TFrame")
        text_stack.pack(side=tk.LEFT, anchor=tk.CENTER)

        title_row = ttk.Frame(text_stack, style="Header.TFrame")
        title_row.pack(anchor=tk.W)

        app_title = ttk.Label(
            title_row,
            text="DiskRescue",
            style="HeaderTitle.TLabel",
        )
        app_title.pack(side=tk.LEFT, anchor=tk.CENTER)

        self.lbl_version_badge = ttk.Label(
            title_row,
            text=" v0.3.3 PRO ",
            font=("Segoe UI Semibold", 8, "bold"),
            background=p["accent_soft_bg"],
            foreground=p["accent_soft_fg"],
            padding=(6, 2),
        )
        self.lbl_version_badge.pack(side=tk.LEFT, padx=(10, 0), anchor=tk.CENTER)

        app_subtitle = ttk.Label(
            text_stack,
            text="Diagnóstico de Hardware, Clonagem Resiliente & Recuperação Forense de Partições",
            style="HeaderSub.TLabel",
        )
        app_subtitle.pack(anchor=tk.W, pady=(2, 0))

        # Bloco de Ações e Status do Sistema (à Direita)
        actions_box = ttk.Frame(self.header_frame, style="Header.TFrame")
        actions_box.pack(side=tk.RIGHT, anchor=tk.CENTER)

        # Badge UAC
        elevated = is_admin()
        uac_bg = p["success_bg"] if elevated else p["warning_bg"]
        uac_fg = p["success_fg"] if elevated else p["warning_fg"]
        uac_text = "🛡️ Administrador Ativo (Acesso Raw)" if elevated else "⚠️ Modo Leitura (Sem UAC)"

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
                text="⚡ Elevar UAC",
                command=self._on_elevate,
                style="Accent.TButton",
                width=12,
            )
            self.btn_elevate.pack(side=tk.LEFT, padx=(0, 8))

        # Botão Atualizar Discos
        self.btn_refresh = ttk.Button(
            actions_box,
            text="🔄 Atualizar Discos",
            command=self.refresh_disks,
            width=16,
        )
        self.btn_refresh.pack(side=tk.LEFT, padx=(0, 8))

        # Alternador de Tema
        if HAS_SV_TTK:
            self.btn_theme = ttk.Button(
                actions_box,
                text="🌙 Escuro",
                command=self._toggle_theme,
                width=10,
            )
            self.btn_theme.pack(side=tk.LEFT)

    # ── SIDEBAR DE NAVEGAÇÃO ──────────────────────────────────────────────────
    def _build_sidebar(self):
        p = self.palette
        self.sidebar_frame = ttk.Frame(self.body_container, style="Sidebar.TFrame", width=240, padding=(12, 16))
        self.sidebar_frame.pack(side=tk.LEFT, fill=tk.Y)
        self.sidebar_frame.pack_propagate(False)

        ttk.Label(
            self.sidebar_frame,
            text="MÓDULOS DE OPERAÇÃO",
            style="SidebarSection.TLabel",
        ).pack(anchor=tk.W, pady=(0, 10), padx=6)

        nav_items = [
            ("dashboard", "📊   Visão Geral"),
            ("diagnostic", "🔍   Diagnóstico & Reparo"),
            ("smart", "📈   Telemetria S.M.A.R.T."),
            ("clone", "💾   Clonagem Resiliente"),
            ("carver", "🔬   File Carving Forense"),
            ("surface", "⚡   Varredura Superfície"),
            ("logs", "📝   Console de Auditoria"),
        ]

        for key, label in nav_items:
            btn = ttk.Button(
                self.sidebar_frame,
                text=label,
                command=lambda k=key: self._switch_view(k),
                style="Nav.TButton",
            )
            btn.pack(fill=tk.X, pady=3)
            self.nav_buttons[key] = btn

        # Card de Resumo do Disco no Rodapé da Sidebar
        self.mini_drive_outer, self.mini_drive_box = self._make_card(
            self.sidebar_frame,
            title="DISPOSITIVO ATIVO",
            padding=(12, 10),
            alt=True,
            accent_top=p["accent"],
        )
        self.mini_drive_outer.pack(side=tk.BOTTOM, fill=tk.X, pady=(14, 0))

        self.lbl_mini_model = ttk.Label(
            self.mini_drive_box,
            text="Nenhum selecionado",
            style="AltTitle.TLabel",
            wraplength=195,
        )
        self.lbl_mini_model.pack(anchor=tk.W, pady=(0, 3))

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

        # Esconde todas as telas
        for v in self.views.values():
            v.pack_forget()

        # Exibe a tela selecionada
        self.views[view_key].pack(fill=tk.BOTH, expand=True)

        # Atualiza destaques nos botões da sidebar
        for k, btn in self.nav_buttons.items():
            if k == view_key:
                btn.configure(style="Accent.TButton")
            else:
                btn.configure(style="Nav.TButton")

    # ── BANNER FIXO DO DISCO SELECIONADO ──────────────────────────────────────
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
            text="💿  Dispositivo Físico Alvo:",
            style="CardTitle.TLabel",
        ).pack(side=tk.LEFT, padx=(0, 10))

        self.combo_disks = ttk.Combobox(top_row, state="readonly", width=48, font=("Segoe UI", 9))
        self.combo_disks.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(0, 12))
        self.combo_disks.bind("<<ComboboxSelected>>", self._on_combo_disk_selected)

        self.lbl_drive_raw_badge = ttk.Label(
            top_row,
            text="Raw: Indefinido",
            font=("Segoe UI Semibold", 8, "bold"),
            background=p["neutral_bg"],
            foreground=p["neutral_fg"],
            padding=(10, 4),
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
        # 1. Dashboard
        self.view_dashboard = ttk.Frame(self.view_container, style="Root.TFrame")
        self.views["dashboard"] = self.view_dashboard
        self._setup_view_dashboard()

        # 2. Diagnóstico & Reparo
        self.view_diagnostic = ttk.Frame(self.view_container, style="Root.TFrame")
        self.views["diagnostic"] = self.view_diagnostic
        self._setup_view_diagnostic()

        # 3. SMART
        self.view_smart = ttk.Frame(self.view_container, style="Root.TFrame")
        self.views["smart"] = self.view_smart
        self._setup_view_smart()

        # 4. Clonagem Resiliente
        self.view_clone = ttk.Frame(self.view_container, style="Root.TFrame")
        self.views["clone"] = self.view_clone
        self._setup_view_clone()

        # 5. File Carving
        self.view_carver = ttk.Frame(self.view_container, style="Root.TFrame")
        self.views["carver"] = self.view_carver
        self._setup_view_carver()

        # 6. Varredura de Superfície
        self.view_surface = ttk.Frame(self.view_container, style="Root.TFrame")
        self.views["surface"] = self.view_surface
        self._setup_view_surface()

        # 7. Console de Logs
        self.view_logs = ttk.Frame(self.view_container, style="Root.TFrame")
        self.views["logs"] = self.view_logs
        self._setup_view_logs()

    # ══════════════════════════════════════════════════════════════════════════
    # TELA 1: VISÃO GERAL (DASHBOARD)
    # ══════════════════════════════════════════════════════════════════════════
    def _setup_view_dashboard(self):
        f = self.view_dashboard
        p = self.palette

        # Cartão de Diagnóstico de Saúde e Recomendação Técnica
        health_outer, health_box = self._make_card(
            f,
            title="DIAGNÓSTICO DE SAÚDE & CONDUTA RECOMENDADA",
            badge_text="TEMPO REAL",
            padding=(16, 12),
        )
        health_outer.pack(fill=tk.X, pady=(0, 10))

        h_top = ttk.Frame(health_box, style="Card.TFrame")
        h_top.pack(fill=tk.X)

        self.dash_health_badge = ttk.Label(
            h_top,
            text="⚪ Aguardando Análise...",
            font=("Segoe UI Semibold", 11, "bold"),
            background=p["neutral_bg"],
            foreground=p["text_primary"],
            padding=(12, 5),
        )
        self.dash_health_badge.pack(side=tk.LEFT)

        self.dash_health_title = ttk.Label(
            h_top,
            text="Selecione um disco para carregar o resumo de confiabilidade.",
            font=("Segoe UI Semibold", 11, "bold"),
            style="CardBody.TLabel",
        )
        self.dash_health_title.pack(side=tk.LEFT, padx=(14, 0))

        self.dash_recommendation = ttk.Label(
            health_box,
            text="Diretriz: O sistema analisará dados de firmware, setores e boot sectors para orientar a conduta recomendada.",
            style="CardMuted.TLabel",
            wraplength=960,
            justify=tk.LEFT,
        )
        self.dash_recommendation.pack(anchor=tk.W, pady=(8, 0))

        # Grade de Ações Rápidas (4 Cards Modernos com barra superior de destaque)
        actions_grid = ttk.Frame(f, style="Root.TFrame")
        actions_grid.pack(fill=tk.X, pady=(0, 10))

        for col in range(4):
            actions_grid.columnconfigure(col, weight=1)

        def make_action_card(parent, title, desc, icon, c, target_view, accent_hex):
            outer, card = self._make_card(
                parent,
                padding=(14, 12),
                alt=True,
                accent_top=accent_hex,
            )
            pad_l = 0 if c == 0 else 5
            pad_r = 0 if c == 3 else 5
            outer.grid(row=0, column=c, padx=(pad_l, pad_r), sticky="nsew")

            ttk.Label(
                card,
                text=f"{icon}  {title}",
                style="AltTitle.TLabel",
            ).pack(anchor=tk.W)

            ttk.Label(
                card,
                text=desc,
                style="AltMuted.TLabel",
                wraplength=210,
            ).pack(anchor=tk.W, pady=(4, 10))

            btn = ttk.Button(
                card,
                text="Abrir Módulo →",
                command=lambda: self._switch_view(target_view),
            )
            btn.pack(anchor=tk.W)

        make_action_card(
            actions_grid,
            "Clonagem Segura",
            "Crie imagem .img saltando setores defeituosos sem estressar a mídia.",
            "💾", 0, "clone", "#0EA5E9"
        )
        make_action_card(
            actions_grid,
            "Análise Partições",
            "Investigue GPT, MBR e recupere boot sectors NTFS desalinhados.",
            "🔍", 1, "diagnostic", "#8B5CF6"
        )
        make_action_card(
            actions_grid,
            "Resgate de Arquivos",
            "Extraia fotos, PDFs e ZIPs diretamente dos setores brutos.",
            "🔬", 2, "carver", "#10B981"
        )
        make_action_card(
            actions_grid,
            "Teste Latência",
            "Mapeie blocos lentos com heatmap estilo MHDD/Victoria.",
            "⚡", 3, "surface", "#F59E0B"
        )

        # Tabela Resumo das Partições do Disco
        part_outer, part_box = self._make_card(
            f,
            title="PARTIÇÕES E VOLUMES DETECTADOS NA MÍDIA",
            badge_text="GPT / MBR",
            padding=(12, 10),
        )
        part_outer.pack(fill=tk.BOTH, expand=True)

        cols = ("idx", "name", "start_lba", "end_lba", "size", "fs", "status")
        self.dash_tree = ttk.Treeview(part_box, columns=cols, show="headings", height=6)
        self.dash_tree.heading("idx", text="#")
        self.dash_tree.heading("name", text="Nome / Rótulo")
        self.dash_tree.heading("start_lba", text="LBA Inicial")
        self.dash_tree.heading("end_lba", text="LBA Final")
        self.dash_tree.heading("size", text="Capacidade")
        self.dash_tree.heading("fs", text="Tabela")
        self.dash_tree.heading("status", text="Condição")

        self.dash_tree.column("idx", width=40, anchor=tk.CENTER)
        self.dash_tree.column("name", width=180, anchor=tk.W)
        self.dash_tree.column("start_lba", width=120, anchor=tk.E)
        self.dash_tree.column("end_lba", width=120, anchor=tk.E)
        self.dash_tree.column("size", width=110, anchor=tk.E)
        self.dash_tree.column("fs", width=80, anchor=tk.CENTER)
        self.dash_tree.column("status", width=180, anchor=tk.W)

        dt_scroll = ttk.Scrollbar(part_box, orient=tk.VERTICAL, command=self.dash_tree.yview)
        self.dash_tree.configure(yscrollcommand=dt_scroll.set)
        self.dash_tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        dt_scroll.pack(side=tk.RIGHT, fill=tk.Y)

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
            text="🚀 Iniciar Varredura de Geometria",
            style="Accent.TButton",
            command=self._start_deep_scan,
        )
        self.btn_run_scan.pack(side=tk.RIGHT)

        # Container dos 2 cards comparativos
        split_cards = ttk.Frame(f, style="Root.TFrame")
        split_cards.pack(fill=tk.BOTH, expand=True, pady=(0, 10))
        split_cards.columnconfigure(0, weight=1)
        split_cards.columnconfigure(1, weight=1)
        split_cards.rowconfigure(0, weight=1)

        # Card 1: Tabela Oficial (GPT / MBR)
        card_table_outer, card_table = self._make_card(
            split_cards,
            title="1. TABELA REGISTRADA NO DISCO (GPT / MBR)",
            badge_text="LBA 0..33",
            padding=(12, 10),
        )
        card_table_outer.grid(row=0, column=0, padx=(0, 5), sticky="nsew")

        gpt_wrap, self.txt_gpt = self._make_styled_text(card_table, wrap=tk.NONE)
        gpt_wrap.pack(fill=tk.BOTH, expand=True)

        # Card 2: Assinaturas Reais na Mídia Física
        card_media_outer, card_media = self._make_card(
            split_cards,
            title="2. MÍDIA FÍSICA REAL (BOOT SECTORS & MFTs)",
            badge_text="RAW SCAN",
            padding=(12, 10),
        )
        card_media_outer.grid(row=0, column=1, padx=(5, 0), sticky="nsew")

        fs_wrap, self.txt_fs = self._make_styled_text(card_media, wrap=tk.NONE)
        fs_wrap.pack(fill=tk.BOTH, expand=True)

        # Card 3: Ações de Resolução Cirúrgica
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
            text="🔄 Restaurar Boot Sector via Backup",
            state=tk.DISABLED,
            command=self._execute_boot_restore,
        )
        self.btn_repair_boot.pack(side=tk.LEFT, padx=(0, 8))

        self.btn_repair = ttk.Button(
            btn_box,
            text="🛠️ Realinhar Partição GPT",
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

        # Grade de Métricas Principais (2 Linhas x 3 Colunas)
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

        self.val_temp = make_smart_metric(metrics_grid, "🌡️ Temperatura Operacional", "-- °C", 0, 0, "#0EA5E9")
        self.val_smart_risk = make_smart_metric(metrics_grid, "🛡️ Classificação de Risco", "--", 1, 0, "#10B981")
        self.val_crc_err = make_smart_metric(metrics_grid, "🔌 Erros de Cabo SATA (CRC 0xC7)", "--", 2, 0, "#8B5CF6")

        self.val_reallocated = make_smart_metric(metrics_grid, "⚠️ Setores Remapeados (0x05)", "--", 0, 1, "#F59E0B")
        self.val_pending = make_smart_metric(metrics_grid, "⏳ Setores Pendentes (0xC5)", "--", 1, 1, "#EF4444")
        self.val_spin = make_smart_metric(metrics_grid, "⚙️ Falhas de Partida Motor (0x0A)", "--", 2, 1, "#EC4899")

        # Aviso sobre adaptadores USB e transparência técnica
        advisory_outer, advisory_box = self._make_card(f, padding=(12, 8))
        advisory_outer.pack(fill=tk.X, pady=(0, 10))

        ttk.Label(
            advisory_box,
            text="ℹ️ Transparência Técnica: Pontes USB genéricas frequentemente bloqueiam registros ATA brutos. "
                 "Quando os atributos numéricos não estão disponíveis, o sistema classifica honestamente como "
                 "'Cinza (Indeterminado)' e exibe 'N/D', sem inventar saúde 'Verde'.",
            style="CardMuted.TLabel",
            wraplength=980,
        ).pack(anchor=tk.W)

        # Card de Diagnóstico do Firmware
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

        # Configuração de Destino
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

        ttk.Button(row, text="📂 Procurar...", command=self._browse_image_dest).pack(side=tk.RIGHT)

        ttk.Label(
            dest_card,
            text="🛡️ Barreira P0: Bloqueia automaticamente destinos no mesmo disco físico de origem e impede sobrescrita silenciosa de imagens sem mapa válido.",
            style="CardMuted.TLabel",
        ).pack(anchor=tk.W, pady=(8, 0))

        # Painel de Telemetria e Progresso
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

        # Grade de estatísticas detalhadas
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

        # Botões de Ação
        ctrl_row = ttk.Frame(f, style="Root.TFrame")
        ctrl_row.pack(fill=tk.X, side=tk.BOTTOM)

        self.btn_cancel_clone = ttk.Button(
            ctrl_row,
            text="⏸️ Interromper / Salvar Mapa (.map)",
            state=tk.DISABLED,
            command=self._cancel_clone,
        )
        self.btn_cancel_clone.pack(side=tk.LEFT)

        self.btn_start_clone = ttk.Button(
            ctrl_row,
            text="🚀 Iniciar Clonagem Resiliente",
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

        # Opções e Destino
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
        ttk.Button(row1, text="📂 Escolher Pasta...", command=self._browse_carve_dir).pack(side=tk.RIGHT)

        # Filtros por Tipo de Arquivo
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

        # Lista de Arquivos Resgatados
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

        # Rodapé de Ações
        carve_actions = ttk.Frame(f, style="Root.TFrame")
        carve_actions.pack(fill=tk.X, side=tk.BOTTOM)

        self.lbl_carve_status = ttk.Label(carve_actions, text="Pronto para iniciar File Carving.", style="ViewSub.TLabel")
        self.lbl_carve_status.pack(side=tk.LEFT)

        ttk.Button(carve_actions, text="📂 Abrir Pasta dos Recuperados", command=self._open_carve_folder).pack(side=tk.LEFT, padx=(14, 0))

        self.btn_start_carve = ttk.Button(
            carve_actions,
            text="🔍 Iniciar Carving Forense",
            style="Accent.TButton",
            command=self._start_carver,
        )
        self.btn_start_carve.pack(side=tk.RIGHT)

        self.btn_cancel_carve = ttk.Button(
            carve_actions,
            text="⏸️ Interromper Carving",
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

        # Canvas do Heatmap (Grid de blocos coloridos)
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

        # Legenda e Contadores em Card
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

        # Barra de Ações
        surface_actions = ttk.Frame(f, style="Root.TFrame")
        surface_actions.pack(fill=tk.X, side=tk.BOTTOM)

        self.lbl_surface_status = ttk.Label(surface_actions, text="Pronto para testar superfície.", style="ViewSub.TLabel")
        self.lbl_surface_status.pack(side=tk.LEFT)

        self.btn_cancel_surface = ttk.Button(
            surface_actions,
            text="⏹️ Interromper",
            state=tk.DISABLED,
            command=self._cancel_surface,
        )
        self.btn_cancel_surface.pack(side=tk.LEFT, padx=(14, 0))

        self.btn_start_surface = ttk.Button(
            surface_actions,
            text="⚡ Iniciar Teste de Superfície",
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

        ttk.Button(log_actions, text="🧹 Limpar Log", command=lambda: self.txt_console.delete("1.0", tk.END)).pack(side=tk.LEFT)
        ttk.Button(log_actions, text="💾 Exportar Relatório (.txt)", style="Accent.TButton", command=self._save_log_file).pack(side=tk.RIGHT)

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
        """Verifica se há qualquer worker ou operação de I/O ativa na aplicação."""
        return bool(
            self.is_scanning
            or self.is_surface_scanning
            or self.is_repairing
            or (self.cloner_instance is not None)
            or (self.carver_instance is not None)
        )

    def _on_close_window(self, confirm_if_active: bool = False):
        """
        Encerramento limpo e cooperativo das threads e operações ativas.
        Não destrói a janela abruptamente enquanto workers ainda estão gravando arquivos ou salvando mapas.
        """
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
        """Verifica periodicamente pelo event loop se todos os workers encerraram antes de destruir a janela."""
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
        """Transiciona o OperationState ao final de um worker preservando estados terminais já definidos."""
        if isinstance(explicit_val, OperationState):
            self.op_state = explicit_val
        elif self.op_state == OperationState.CANCELLING:
            self.op_state = OperationState.CANCELLED
        elif self.op_state == OperationState.RUNNING:
            self.op_state = OperationState.IDLE

    def _process_pending_messages(self):
        """Processa todas as mensagens pendentes na fila na thread principal da GUI."""
        if not getattr(self, "is_running", True):
            # Drena eventos tardios recebidos após o encerramento sem tocar em widgets destruídos
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
                    self._resolve_ended_state(val)
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
            self.btn_theme.config(text="☀️ Claro")
            _apply_windows_dwm_theme(self.root, dark=False)
        else:
            sv_ttk.set_theme("dark")
            self.current_theme = "dark"
            self.btn_theme.config(text="🌙 Escuro")
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
            combo_values.append(f"[Disco {d.number}] {d.friendly_name} ({d.size_gb:.1f} GB) — {d.bus_type}")

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

        # Atualiza Card Superior do Dispositivo
        raw_text = "✔ Acesso Raw Liberado" if d.can_open_raw else "✖ Acesso Raw Bloqueado"
        raw_bg = "#064E3B" if d.can_open_raw else "#7F1D1D"
        raw_fg = "#34D399" if d.can_open_raw else "#FCA5A5"

        self.lbl_drive_raw_badge.config(text=raw_text, background=raw_bg, foreground=raw_fg)
        self.lbl_drive_details.config(
            text=f"Caminho: {d.device_path}  •  Capacidade: {d.size_gb:.1f} GB ({d.size_bytes:,} bytes)  •  "
                 f"Mídia: {d.media_type}  •  Barramento: {d.bus_type}  •  Serial: {d.serial_number or 'N/A'}"
        )

        # Atualiza Mini Card da Sidebar
        self.lbl_mini_model.config(text=f"Disco {d.number}: {d.friendly_name}")
        self.lbl_mini_size.config(text=f"Tamanho: {d.size_gb:.1f} GB ({d.media_type})")
        self.lbl_mini_path.config(text=f"{d.device_path} ({d.bus_type})")

        # Atualiza Header da Aba de Diagnóstico
        self.lbl_target_info.config(
            text=f"Disco Alvo: [Disco {d.number}] {d.friendly_name} ({d.size_gb:.1f} GB)",
        )

        # Atualiza Dashboard Inicial
        self._update_dashboard_with_disk(d)

        # Consulta Telemetria SMART em background
        def smart_worker():
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

        threading.Thread(target=smart_worker, daemon=True).start()

    def _update_dashboard_with_disk(self, d: DiskInfo):
        # Limpa árvore de partições do dashboard
        for item in self.dash_tree.get_children():
            self.dash_tree.delete(item)

        self.dash_health_badge.config(
            text=f"● Status: {d.health_status}",
            background="#1e293b",
            foreground="#38bdf8"
        )
        self.dash_health_title.config(
            text=f"{d.friendly_name} ({d.size_gb:.1f} GB — {d.media_type})"
        )
        self.dash_recommendation.config(
            text="Recomendação: Execute a 'Varredura de Geometria' para inspecionar partições e tabelas GPT/MBR."
        )

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

        # Determina honestamente se o dispositivo é USB para diferenciar telemetria indisponível
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

        # Atualiza badge de saúde no Dashboard
        if risk_val == "Verde":
            self.dash_health_badge.config(text="● Confiável / Saudável", background="#14532d", foreground="#4ade80")
        elif risk_val == "Amarelo":
            self.dash_health_badge.config(text="● Atenção / Análise Requerida", background="#78350f", foreground="#fcd34d")
        elif risk_val == "Vermelho":
            self.dash_health_badge.config(text="● Crítico / Risco de Falha Iminente", background="#7f1d1d", foreground="#fca5a5")
        else:
            if is_usb:
                self.dash_health_badge.config(text="● Telemetria Não Disponível (Interface USB)", background="#334155", foreground="#94a3b8")
            else:
                self.dash_health_badge.config(text="● Telemetria S.M.A.R.T. Não Disponível / Indeterminada", background="#334155", foreground="#94a3b8")

        self.dash_recommendation.config(text=f"Diretriz Recomendada: {report.action_recommendation}")

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

                # Diagnóstico de integridade do Boot Sector NTFS nas partições candidatas
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

        # Limpa árvore de partições do Dashboard
        for item in self.dash_tree.get_children():
            self.dash_tree.delete(item)

        # 1. GPT e MBR
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
                    values=(p.index, p.name or "Sem Rótulo", f"{p.first_lba:,}", f"{p.last_lba:,}", f"{p.size_gb():.2f} GB", "GPT", "Registrada")
                )
        elif mbr and mbr.has_valid_signature:
            self.txt_gpt.insert(tk.END, "✔ Tabela MBR Clássica Válida (Assinatura 0x55AA presente):\n\n")
            for p in mbr.partitions:
                if not p.is_empty:
                    self.txt_gpt.insert(tk.END, f"  • Partição {p.index}: [{p.type_name}]\n")
                    self.txt_gpt.insert(tk.END, f"    LBA Inicial: {p.start_lba:,} .. LBA Final: {p.end_lba:,} ({p.size_gb:.2f} GB)\n")
                    self.txt_gpt.insert(tk.END, f"    Código de Tipo: 0x{p.type_code:02X}\n\n")

                    self.dash_tree.insert(
                        "",
                        tk.END,
                        values=(p.index, p.type_name, f"{p.start_lba:,}", f"{p.end_lba:,}", f"{p.size_gb:.2f} GB", "MBR", "Ativa" if p.is_bootable else "Primária")
                    )
        else:
            self.txt_gpt.insert(tk.END, "⚠️ Tabela de partições não detectada ou assinaturas corrompidas.\n")

        # 2. Filesystems
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

        # 3. Cruzamento e Diagnósticos de Falha
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

        # Habilita ou desabilita o botão de restauração de Boot Sector NTFS
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

        # Se ainda não diagnosticado, tenta identificar partição candidata
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

        # Validação P0 de segurança
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
            # Se for bloqueio de imagem existente sem mapa ou mapa inválido/incompatível, exige confirmação explícita
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

        # Validação P0 de segurança
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

        def block_cb(res: SurfaceBlockResult, summary: SurfaceScanSummary):
            self.msg_queue.put(("surface_block", (res, summary)))

        def worker():
            try:
                summary = scan_surface_latency(
                    target_disk.device_path,
                    max_sectors=100_000,
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

        # Desenha célula moderna no Canvas com geometria responsiva
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
        self.lbl_surface_status.config(
            text=f"Blocos Testados: {summary.total_tested_blocks:,} | LBA: {res.lba:,} | Latência: {res.latency_ms:.1f} ms"
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
