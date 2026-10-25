"""
gui/app.py — Interface Gráfica Moderna e Profissional (GUI) do DiskRescue v0.3.2.
Construída com Tkinter + Sun Valley (sv_ttk) no padrão Windows 11 Fluent Design.
Oferece Navegação por Sidebar, Dashboard de Saúde, Diagnóstico & Reparo de Partições,
Telemetria SMART ATA, Clonagem Resiliente em Duas Fases (estilo GNU ddrescue),
File Carving por Assinaturas Mágicas, Heatmap de Latência de Superfície e Log Forense.
"""
from __future__ import annotations

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
from core.hardware.device_resolver import assert_safe_source_and_destination
from core.hardware.scanner import DiskInfo, is_admin, relaunch_as_admin, scan_disks
from core.hardware.smart import SmartReport, SmartRiskLevel, query_smart_report_windows
from core.hardware.surface_scan import SurfaceBlockResult, SurfaceScanSummary, scan_surface_latency
from core.partition.gpt import GPTPartitionEntry, GPTTable, parse_gpt
from core.partition.mbr import parse_mbr
from core.recovery.carver import CarvedFile, FileCarver
from core.recovery.repair import repair_gpt_partition_offset
from core.recovery.resilient_clone import CloningResult, ResilientCloneStats, ResilientCloner


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
        self.root.title("DiskRescue v0.3.2 — Diagnóstico, Clonagem Resiliente & Recuperação Forense")
        self.root.geometry("1240x820")
        self.root.minsize(1080, 720)

        # Configura ícone da janela
        self._setup_window_icons()

        # Tema e Estilos
        self.current_theme = "dark"
        if HAS_SV_TTK:
            sv_ttk.set_theme("dark")

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
        self.views: dict[str, ttk.Frame] = {}
        self.nav_buttons: dict[str, ttk.Button] = {}
        self.active_view = "dashboard"

        # Construção da Interface
        self._build_ui()
        self._start_queue_poller()
        if auto_refresh:
            self.refresh_disks()

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
        except Exception:
            pass

    # ── CONSTRUÇÃO DA INTERFACE PRINCIPAL ──────────────────────────────────────
    def _build_ui(self):
        # 1. Header Superior (Barra de Título Moderna)
        self._build_top_header()

        # Separador sutil
        ttk.Separator(self.root, orient=tk.HORIZONTAL).pack(fill=tk.X)

        # 2. Container Central Dividido (Sidebar de Navegação + Conteúdo Dinâmico)
        self.body_container = ttk.Frame(self.root)
        self.body_container.pack(fill=tk.BOTH, expand=True)

        # Sidebar à Esquerda
        self._build_sidebar()

        # Divisor vertical
        ttk.Separator(self.body_container, orient=tk.VERTICAL).pack(side=tk.LEFT, fill=tk.Y)

        # Área de Conteúdo à Direita
        self.content_area = ttk.Frame(self.body_container, padding=(14, 10))
        self.content_area.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

        # Card de Seleção e Resumo do Disco Ativo (fixo no topo do conteúdo)
        self._build_drive_selector_banner()

        # Container onde as telas/views alternam
        self.view_container = ttk.Frame(self.content_area)
        self.view_container.pack(fill=tk.BOTH, expand=True, pady=(8, 0))

        # Construir todas as telas no container
        self._build_views()

        # 3. Barra de Status Inferior
        self._build_status_bar()

        # Exibir a tela inicial padrão (Dashboard)
        self._switch_view("dashboard")

    # ── HEADER SUPERIOR ───────────────────────────────────────────────────────
    def _build_top_header(self):
        self.header_frame = ttk.Frame(self.root, padding=(18, 10))
        self.header_frame.pack(fill=tk.X, side=tk.TOP)

        # Bloco da Marca / Título
        brand_box = ttk.Frame(self.header_frame)
        brand_box.pack(side=tk.LEFT, anchor=tk.CENTER)

        title_row = ttk.Frame(brand_box)
        title_row.pack(anchor=tk.W)

        app_title = ttk.Label(
            title_row,
            text="🛠️  DiskRescue",
            font=("Segoe UI Variable Display", 16, "bold")
        )
        app_title.pack(side=tk.LEFT, anchor=tk.CENTER)

        version_badge = ttk.Label(
            title_row,
            text=" v0.3.2 ",
            font=("Segoe UI", 9, "bold"),
            background="#1e293b",
            foreground="#38bdf8",
            padding=(4, 1)
        )
        version_badge.pack(side=tk.LEFT, padx=(8, 0), anchor=tk.CENTER)

        app_subtitle = ttk.Label(
            brand_box,
            text="Diagnóstico de Hardware, Clonagem Resiliente & Recuperação Lógica de Armazenamento",
            font=("Segoe UI", 8)
        )
        app_subtitle.pack(anchor=tk.W, pady=(2, 0))

        # Bloco de Ações e Status do Sistema (à Direita)
        actions_box = ttk.Frame(self.header_frame)
        actions_box.pack(side=tk.RIGHT, anchor=tk.CENTER)

        # Badge UAC
        elevated = is_admin()
        uac_bg = "#14532d" if elevated else "#7c2d12"
        uac_fg = "#4ade80" if elevated else "#fca5a5"
        uac_text = "🛡️ Administrador Ativo (Acesso Raw)" if elevated else "⚠️ Modo Leitura (Sem UAC)"

        self.lbl_uac = ttk.Label(
            actions_box,
            text=uac_text,
            font=("Segoe UI", 8, "bold"),
            background=uac_bg,
            foreground=uac_fg,
            padding=(8, 4)
        )
        self.lbl_uac.pack(side=tk.LEFT, padx=(0, 10))

        if not elevated:
            self.btn_elevate = ttk.Button(
                actions_box,
                text="Elevar UAC",
                command=self._on_elevate,
                style="Accent.TButton",
                width=11
            )
            self.btn_elevate.pack(side=tk.LEFT, padx=(0, 8))

        # Botão Atualizar Discos
        self.btn_refresh = ttk.Button(
            actions_box,
            text="🔄 Atualizar Discos",
            command=self.refresh_disks,
            width=16
        )
        self.btn_refresh.pack(side=tk.LEFT, padx=(0, 8))

        # Alternador de Tema
        if HAS_SV_TTK:
            self.btn_theme = ttk.Button(
                actions_box,
                text="🌙 Escuro",
                command=self._toggle_theme,
                width=10
            )
            self.btn_theme.pack(side=tk.LEFT)

    # ── SIDEBAR DE NAVEGAÇÃO ──────────────────────────────────────────────────
    def _build_sidebar(self):
        self.sidebar_frame = ttk.Frame(self.body_container, width=225, padding=(10, 12))
        self.sidebar_frame.pack(side=tk.LEFT, fill=tk.Y)
        self.sidebar_frame.pack_propagate(False)

        ttk.Label(
            self.sidebar_frame,
            text="MÓDULOS DO SISTEMA",
            font=("Segoe UI", 8, "bold"),
            foreground="#64748b"
        ).pack(anchor=tk.W, pady=(0, 8), padx=4)

        nav_items = [
            ("dashboard", "📊  Visão Geral"),
            ("diagnostic", "🔍  Diagnóstico & Reparo"),
            ("smart", "📈  Telemetria S.M.A.R.T."),
            ("clone", "💾  Clonagem Resiliente"),
            ("carver", "🔬  File Carving"),
            ("surface", "⚡  Varredura Superfície"),
            ("logs", "📝  Console de Logs"),
        ]

        for key, label in nav_items:
            btn = ttk.Button(
                self.sidebar_frame,
                text=label,
                command=lambda k=key: self._switch_view(k),
                style="TButton"
            )
            btn.pack(fill=tk.X, pady=3)
            self.nav_buttons[key] = btn

        # Card de Resumo do Disco no Rodapé da Sidebar
        self.mini_drive_box = ttk.LabelFrame(self.sidebar_frame, text="Disco Ativo", padding=8)
        self.mini_drive_box.pack(side=tk.BOTTOM, fill=tk.X, pady=(12, 0))

        self.lbl_mini_model = ttk.Label(self.mini_drive_box, text="Nenhum", font=("Segoe UI", 8, "bold"), wraplength=190)
        self.lbl_mini_model.pack(anchor=tk.W)

        self.lbl_mini_size = ttk.Label(self.mini_drive_box, text="Tamanho: --", font=("Segoe UI", 8))
        self.lbl_mini_size.pack(anchor=tk.W)

        self.lbl_mini_path = ttk.Label(self.mini_drive_box, text="Caminho: --", font=("Segoe UI", 8), foreground="#94a3b8")
        self.lbl_mini_path.pack(anchor=tk.W)

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
                btn.configure(style="TButton")

    # ── BANNER FIXO DO DISCO SELECIONADO ──────────────────────────────────────
    def _build_drive_selector_banner(self):
        self.drive_banner = ttk.LabelFrame(self.content_area, text="Dispositivo de Armazenamento Alvo", padding=(10, 8))
        self.drive_banner.pack(fill=tk.X, side=tk.TOP, pady=(0, 4))

        top_row = ttk.Frame(self.drive_banner)
        top_row.pack(fill=tk.X)

        ttk.Label(top_row, text="Disco Físico:", font=("Segoe UI", 9, "bold")).pack(side=tk.LEFT, padx=(0, 8))

        self.combo_disks = ttk.Combobox(top_row, state="readonly", width=46)
        self.combo_disks.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(0, 10))
        self.combo_disks.bind("<<ComboboxSelected>>", self._on_combo_disk_selected)

        self.lbl_drive_raw_badge = ttk.Label(
            top_row,
            text="Raw: Indefinido",
            font=("Segoe UI", 8, "bold"),
            background="#334155",
            foreground="#f8fafc",
            padding=(6, 2)
        )
        self.lbl_drive_raw_badge.pack(side=tk.RIGHT)

        # Linha inferior de detalhes do hardware
        self.drive_info_row = ttk.Frame(self.drive_banner)
        self.drive_info_row.pack(fill=tk.X, pady=(4, 0))

        self.lbl_drive_details = ttk.Label(
            self.drive_info_row,
            text="Nenhum dispositivo selecionado. Aguardando enumeração...",
            font=("Segoe UI", 8),
            foreground="#94a3b8"
        )
        self.lbl_drive_details.pack(side=tk.LEFT)

    # ── INSTANCIAÇÃO DAS VIEWS ────────────────────────────────────────────────
    def _build_views(self):
        # 1. Dashboard
        self.view_dashboard = ttk.Frame(self.view_container)
        self.views["dashboard"] = self.view_dashboard
        self._setup_view_dashboard()

        # 2. Diagnóstico & Reparo
        self.view_diagnostic = ttk.Frame(self.view_container)
        self.views["diagnostic"] = self.view_diagnostic
        self._setup_view_diagnostic()

        # 3. SMART
        self.view_smart = ttk.Frame(self.view_container)
        self.views["smart"] = self.view_smart
        self._setup_view_smart()

        # 4. Clonagem Resiliente
        self.view_clone = ttk.Frame(self.view_container)
        self.views["clone"] = self.view_clone
        self._setup_view_clone()

        # 5. File Carving
        self.view_carver = ttk.Frame(self.view_container)
        self.views["carver"] = self.view_carver
        self._setup_view_carver()

        # 6. Varredura de Superfície
        self.view_surface = ttk.Frame(self.view_container)
        self.views["surface"] = self.view_surface
        self._setup_view_surface()

        # 7. Console de Logs
        self.view_logs = ttk.Frame(self.view_container)
        self.views["logs"] = self.view_logs
        self._setup_view_logs()

    # ══════════════════════════════════════════════════════════════════════════
    # TELA 1: VISÃO GERAL (DASHBOARD)
    # ══════════════════════════════════════════════════════════════════════════
    def _setup_view_dashboard(self):
        f = self.view_dashboard

        # Cartão de Diagnóstico de Saúde e Recomendação Técnica
        health_box = ttk.LabelFrame(f, text="Diagnóstico Geral do Dispositivo", padding=12)
        health_box.pack(fill=tk.X, pady=(0, 10))

        h_top = ttk.Frame(health_box)
        h_top.pack(fill=tk.X)

        self.dash_health_badge = ttk.Label(
            h_top,
            text="⚪ Aguardando Análise...",
            font=("Segoe UI", 12, "bold"),
            background="#1e293b",
            foreground="#f8fafc",
            padding=(8, 4)
        )
        self.dash_health_badge.pack(side=tk.LEFT)

        self.dash_health_title = ttk.Label(
            h_top,
            text="Selecione um disco para carregar o resumo de confiabilidade.",
            font=("Segoe UI", 10, "bold")
        )
        self.dash_health_title.pack(side=tk.LEFT, padx=(12, 0))

        self.dash_recommendation = ttk.Label(
            health_box,
            text="Diretriz: O sistema analisará dados de firmware, setores e boot sectors para orientar a conduta recomendada.",
            font=("Segoe UI", 9),
            wraplength=950,
            justify=tk.LEFT
        )
        self.dash_recommendation.pack(anchor=tk.W, pady=(8, 0))

        # Grade de Ações Rápidas (4 Cards)
        actions_frame = ttk.LabelFrame(f, text="Ações Rápidas de Diagnóstico & Recuperação", padding=10)
        actions_frame.pack(fill=tk.X, pady=(0, 10))

        for col in range(4):
            actions_frame.columnconfigure(col, weight=1)

        def make_action_card(parent, title, desc, icon, c, target_view):
            card = ttk.Frame(parent, padding=8, relief="ridge")
            card.grid(row=0, column=c, padx=5, sticky="nsew")

            ttk.Label(card, text=f"{icon}  {title}", font=("Segoe UI", 10, "bold")).pack(anchor=tk.W)
            ttk.Label(card, text=desc, font=("Segoe UI", 8), wraplength=200, foreground="#94a3b8").pack(anchor=tk.W, pady=(3, 8))

            btn = ttk.Button(card, text="Abrir Módulo", command=lambda: self._switch_view(target_view))
            btn.pack(anchor=tk.W)

        make_action_card(
            actions_frame,
            "Clonagem Segura",
            "Crie imagem .img saltando setores defeituosos sem estressar a mídia.",
            "💾", 0, "clone"
        )
        make_action_card(
            actions_frame,
            "Análise Partições",
            "Investigue GPT, MBR e recupere boot sectors NTFS desalinhados.",
            "🔍", 1, "diagnostic"
        )
        make_action_card(
            actions_frame,
            "Resgate de Arquivos",
            "Extraia fotos, PDFs e ZIPs diretamente dos setores brutos.",
            "🔬", 2, "carver"
        )
        make_action_card(
            actions_frame,
            "Teste Latência",
            "Mapeie blocos lentos com heatmap estilo MHDD/Victoria.",
            "⚡", 3, "surface"
        )

        # Tabela Resumo das Partições do Disco
        part_box = ttk.LabelFrame(f, text="Partições e Volumes Detectados na Mídia", padding=8)
        part_box.pack(fill=tk.BOTH, expand=True)

        cols = ("idx", "name", "start_lba", "end_lba", "size", "fs", "status")
        self.dash_tree = ttk.Treeview(part_box, columns=cols, show="headings", height=6)
        self.dash_tree.heading("idx", text="#")
        self.dash_tree.heading("name", text="Nome / Rótulo")
        self.dash_tree.heading("start_lba", text="LBA Inicial")
        self.dash_tree.heading("end_lba", text="LBA Final")
        self.dash_tree.heading("size", text="Capacidade")
        self.dash_tree.heading("fs", text="Sistema")
        self.dash_tree.heading("status", text="Condição")

        self.dash_tree.column("idx", width=36, anchor=tk.CENTER)
        self.dash_tree.column("name", width=160, anchor=tk.W)
        self.dash_tree.column("start_lba", width=120, anchor=tk.E)
        self.dash_tree.column("end_lba", width=120, anchor=tk.E)
        self.dash_tree.column("size", width=100, anchor=tk.E)
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

        action_bar = ttk.Frame(f)
        action_bar.pack(fill=tk.X, pady=(0, 8))

        self.lbl_target_info = ttk.Label(
            action_bar,
            text="Nenhum disco selecionado.",
            font=("Segoe UI", 10, "italic")
        )
        self.lbl_target_info.pack(side=tk.LEFT, pady=4)

        self.btn_run_scan = ttk.Button(
            action_bar,
            text="🚀 Iniciar Varredura de Geometria",
            style="Accent.TButton",
            command=self._start_deep_scan
        )
        self.btn_run_scan.pack(side=tk.RIGHT)

        # Container dos 2 cards comparativos
        split_cards = ttk.Frame(f)
        split_cards.pack(fill=tk.BOTH, expand=True, pady=(0, 8))
        split_cards.columnconfigure(0, weight=1)
        split_cards.columnconfigure(1, weight=1)
        split_cards.rowconfigure(0, weight=1)

        # Card 1: Tabela Oficial (GPT / MBR)
        card_table = ttk.LabelFrame(split_cards, text="1. Tabela Registrada no Disco (GPT / MBR)", padding=8)
        card_table.grid(row=0, column=0, padx=(0, 4), sticky="nsew")

        self.txt_gpt = tk.Text(card_table, font=("Consolas", 9), wrap=tk.NONE)
        gpt_s = ttk.Scrollbar(card_table, orient=tk.VERTICAL, command=self.txt_gpt.yview)
        self.txt_gpt.configure(yscrollcommand=gpt_s.set)
        self.txt_gpt.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        gpt_s.pack(side=tk.RIGHT, fill=tk.Y)

        # Card 2: Assinaturas Reais na Mídia Física
        card_media = ttk.LabelFrame(split_cards, text="2. Mídia Física Real (Boot Sectors & MFTs)", padding=8)
        card_media.grid(row=0, column=1, padx=(4, 0), sticky="nsew")

        self.txt_fs = tk.Text(card_media, font=("Consolas", 9), wrap=tk.NONE)
        fs_s = ttk.Scrollbar(card_media, orient=tk.VERTICAL, command=self.txt_fs.yview)
        self.txt_fs.configure(yscrollcommand=fs_s.set)
        self.txt_fs.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        fs_s.pack(side=tk.RIGHT, fill=tk.Y)

        # Card 3: Ações de Resolução Cirúrgica
        self.repair_card = ttk.LabelFrame(f, text="3. Ações Cirúrgicas de Resolução", padding=10)
        self.repair_card.pack(fill=tk.X, side=tk.BOTTOM)

        self.lbl_repair_diagnosis = ttk.Label(
            self.repair_card,
            text="Selecione um disco e execute a análise para verificar desalinhamentos ou boot sector corrompido.",
            font=("Segoe UI", 9)
        )
        self.lbl_repair_diagnosis.pack(side=tk.LEFT, fill=tk.X, expand=True)

        btn_box = ttk.Frame(self.repair_card)
        btn_box.pack(side=tk.RIGHT)

        self.btn_repair_boot = ttk.Button(
            btn_box,
            text="🔄 Restaurar Boot Sector via Backup",
            state=tk.DISABLED,
            command=self._execute_boot_restore
        )
        self.btn_repair_boot.pack(side=tk.LEFT, padx=(0, 6))

        self.btn_repair = ttk.Button(
            btn_box,
            text="🛠️ Realinhar Partição GPT",
            style="Accent.TButton",
            state=tk.DISABLED,
            command=self._execute_repair
        )
        self.btn_repair.pack(side=tk.RIGHT)

    # ══════════════════════════════════════════════════════════════════════════
    # TELA 3: TELEMETRIA S.M.A.R.T. & CABOS SATA
    # ══════════════════════════════════════════════════════════════════════════
    def _setup_view_smart(self):
        f = self.view_smart

        header = ttk.Frame(f)
        header.pack(fill=tk.X, pady=(0, 8))

        ttk.Label(
            header,
            text="Telemetria S.M.A.R.T. de Hardware & Comunicação Elétrica",
            font=("Segoe UI", 12, "bold")
        ).pack(side=tk.LEFT)

        self.lbl_smart_source = ttk.Label(
            header,
            text="Interface: --",
            font=("Segoe UI", 8),
            foreground="#94a3b8"
        )
        self.lbl_smart_source.pack(side=tk.RIGHT)

        # Grade de Métricas Principais (2 Linhas x 3 Colunas)
        metrics_grid = ttk.Frame(f)
        metrics_grid.pack(fill=tk.X, pady=(0, 10))

        for c in range(3):
            metrics_grid.columnconfigure(c, weight=1)

        def make_smart_metric(parent, label, default_val, c, r):
            card = ttk.LabelFrame(parent, text=label, padding=10)
            card.grid(column=c, row=r, padx=5, pady=4, sticky="nsew")
            lbl = ttk.Label(card, text=default_val, font=("Segoe UI", 14, "bold"))
            lbl.pack(anchor=tk.CENTER)
            return lbl

        self.val_temp = make_smart_metric(metrics_grid, "🌡️ Temperatura", "-- °C", 0, 0)
        self.val_smart_risk = make_smart_metric(metrics_grid, "🛡️ Classificação de Risco", "--", 1, 0)
        self.val_crc_err = make_smart_metric(metrics_grid, "🔌 Erros de Cabo SATA (CRC 0xC7)", "--", 2, 0)

        self.val_reallocated = make_smart_metric(metrics_grid, "⚠️ Setores Remapeados (0x05)", "--", 0, 1)
        self.val_pending = make_smart_metric(metrics_grid, "⏳ Setores Pendentes (0xC5)", "--", 1, 1)
        self.val_spin = make_smart_metric(metrics_grid, "⚙️ Falhas de Partida Motor (0x0A)", "--", 2, 1)

        # Aviso sobre adaptadores USB e transparência técnica
        advisory_box = ttk.Frame(f, padding=(8, 4))
        advisory_box.pack(fill=tk.X, pady=(0, 6))

        ttk.Label(
            advisory_box,
            text="ℹ️ Nota Técnica: Controladoras USB genéricas frequentemente não repassam registros ATA brutos. "
                 "Nesses cenários, os valores são honestamente apresentados como 'N/D' (Não Disponível), "
                 "evitando falsos negativos de saúde.",
            font=("Segoe UI", 8),
            foreground="#94a3b8",
            wraplength=950
        ).pack(anchor=tk.W)

        # Card de Diagnóstico do Firmware
        self.smart_diag_box = ttk.LabelFrame(f, text="Análise Detalhada & Diretriz Recomendada", padding=12)
        self.smart_diag_box.pack(fill=tk.BOTH, expand=True)

        self.lbl_smart_text = ttk.Label(
            self.smart_diag_box,
            text="Aguardando seleção do dispositivo para consultar telemetria...",
            font=("Segoe UI", 9),
            justify=tk.LEFT,
            wraplength=950
        )
        self.lbl_smart_text.pack(anchor=tk.NW, fill=tk.BOTH, expand=True)

    # ══════════════════════════════════════════════════════════════════════════
    # TELA 4: CLONAGEM RESILIENTE (MODO DDRESCUE)
    # ══════════════════════════════════════════════════════════════════════════
    def _setup_view_clone(self):
        f = self.view_clone

        ttk.Label(
            f,
            text="Clonagem Resiliente Não-Destrutiva em Duas Fases",
            font=("Segoe UI", 12, "bold")
        ).pack(anchor=tk.W, pady=(0, 2))

        ttk.Label(
            f,
            text="Inspirado no GNU ddrescue: na Fase 1, faz cópia rápida saltando blocos com erro de I/O para resgatar "
                 "imediatamente os dados saudáveis. Na Fase 2, realiza leitura fina dos setores pendentes. "
                 "Gera arquivo de mapa (.map) persistente para permitir pausar e retomar.",
            font=("Segoe UI", 9),
            foreground="#94a3b8",
            wraplength=950
        ).pack(anchor=tk.W, pady=(0, 10))

        # Configuração de Destino
        dest_card = ttk.LabelFrame(f, text="Arquivo de Imagem de Destino (.img)", padding=10)
        dest_card.pack(fill=tk.X, pady=(0, 10))

        row = ttk.Frame(dest_card)
        row.pack(fill=tk.X)

        self.txt_dest_image = ttk.Entry(row)
        self.txt_dest_image.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(0, 8))

        ttk.Button(row, text="Procurar...", command=self._browse_image_dest).pack(side=tk.RIGHT)

        ttk.Label(
            dest_card,
            text="🛡️ Proteção de Segurança P0: O destino é rigorosamente verificado para garantir que não resida no mesmo disco físico de origem.",
            font=("Segoe UI", 8),
            foreground="#38bdf8"
        ).pack(anchor=tk.W, pady=(4, 0))

        # Painel de Telemetria e Progresso
        stats_frame = ttk.LabelFrame(f, text="Progresso da Clonagem & Resgate", padding=12)
        stats_frame.pack(fill=tk.BOTH, expand=True, pady=(0, 10))

        self.clone_progressbar = ttk.Progressbar(stats_frame, mode="determinate")
        self.clone_progressbar.pack(fill=tk.X, pady=(4, 10))

        self.lbl_clone_phase = ttk.Label(
            stats_frame,
            text="Fase: Aguardando início...",
            font=("Segoe UI", 10, "bold")
        )
        self.lbl_clone_phase.pack(anchor=tk.W, pady=2)

        # Grade de estatísticas detalhadas
        grid_stats = ttk.Frame(stats_frame)
        grid_stats.pack(fill=tk.X, pady=(8, 0))
        for col in range(4):
            grid_stats.columnconfigure(col, weight=1)

        def make_stat_box(parent, title, val, c):
            box = ttk.Frame(parent, padding=6, relief="ridge")
            box.grid(row=0, column=c, padx=4, sticky="nsew")
            ttk.Label(box, text=title, font=("Segoe UI", 8), foreground="#94a3b8").pack(anchor=tk.W)
            l = ttk.Label(box, text=val, font=("Segoe UI", 12, "bold"))
            l.pack(anchor=tk.W)
            return l

        self.lbl_clone_pos = make_stat_box(grid_stats, "Setor Atual / Total", "0 / 0", 0)
        self.lbl_clone_rescued = make_stat_box(grid_stats, "Setores Resgatados", "0", 1)
        self.lbl_clone_bad = make_stat_box(grid_stats, "Bad Sectors Identificados", "0", 2)
        self.lbl_clone_speed = make_stat_box(grid_stats, "Velocidade Média", "0.0 MB/s", 3)

        # Botões de Ação
        ctrl_row = ttk.Frame(f)
        ctrl_row.pack(fill=tk.X, side=tk.BOTTOM)

        self.btn_cancel_clone = ttk.Button(
            ctrl_row,
            text="⏸️ Interromper / Salvar Mapa",
            state=tk.DISABLED,
            command=self._cancel_clone
        )
        self.btn_cancel_clone.pack(side=tk.LEFT)

        self.btn_start_clone = ttk.Button(
            ctrl_row,
            text="🚀 Iniciar Clonagem Resiliente",
            style="Accent.TButton",
            command=self._start_clone
        )
        self.btn_start_clone.pack(side=tk.RIGHT)

    # ══════════════════════════════════════════════════════════════════════════
    # TELA 5: FILE CARVING FORENSE
    # ══════════════════════════════════════════════════════════════════════════
    def _setup_view_carver(self):
        f = self.view_carver

        ttk.Label(
            f,
            text="Recuperação Forense de Arquivos (Assinaturas Mágicas / Magic Bytes)",
            font=("Segoe UI", 12, "bold")
        ).pack(anchor=tk.W, pady=(0, 2))

        ttk.Label(
            f,
            text="Localiza e extrai imagens (JPG, PNG), documentos (PDF) e arquivos compactados (ZIP, DOCX, XLSX) "
                 "diretamente de setores brutos, mesmo se a partição tiver sido formatada ou perdida.",
            font=("Segoe UI", 9),
            foreground="#94a3b8",
            wraplength=950
        ).pack(anchor=tk.W, pady=(0, 10))

        # Opções e Destino
        cfg_box = ttk.LabelFrame(f, text="Configurações de Extração", padding=10)
        cfg_box.pack(fill=tk.X, pady=(0, 8))

        row1 = ttk.Frame(cfg_box)
        row1.pack(fill=tk.X, pady=(0, 6))

        ttk.Label(row1, text="Salvar Arquivos em:", font=("Segoe UI", 9, "bold")).pack(side=tk.LEFT, padx=(0, 8))
        self.txt_carve_dir = ttk.Entry(row1)
        self.txt_carve_dir.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(0, 8))
        default_dir = Path.home() / "Desktop" / "DiskRescue_Recuperados"
        self.txt_carve_dir.insert(0, str(default_dir))
        ttk.Button(row1, text="Escolher Pasta...", command=self._browse_carve_dir).pack(side=tk.RIGHT)

        # Filtros por Tipo de Arquivo
        filter_row = ttk.Frame(cfg_box)
        filter_row.pack(fill=tk.X)

        ttk.Label(filter_row, text="Tipos a Extrair:", font=("Segoe UI", 8, "bold"), foreground="#94a3b8").pack(side=tk.LEFT, padx=(0, 12))

        self.chk_jpg_var = tk.BooleanVar(value=True)
        self.chk_png_var = tk.BooleanVar(value=True)
        self.chk_pdf_var = tk.BooleanVar(value=True)
        self.chk_zip_var = tk.BooleanVar(value=True)

        ttk.Checkbutton(filter_row, text="Imagens JPEG (.jpg)", variable=self.chk_jpg_var).pack(side=tk.LEFT, padx=6)
        ttk.Checkbutton(filter_row, text="Imagens PNG (.png)", variable=self.chk_png_var).pack(side=tk.LEFT, padx=6)
        ttk.Checkbutton(filter_row, text="Documentos PDF (.pdf)", variable=self.chk_pdf_var).pack(side=tk.LEFT, padx=6)
        ttk.Checkbutton(filter_row, text="Compactados ZIP/Office (.zip)", variable=self.chk_zip_var).pack(side=tk.LEFT, padx=6)

        # Lista de Arquivos Resgatados
        list_box = ttk.LabelFrame(f, text="Arquivos Resgatados em Tempo Real", padding=8)
        list_box.pack(fill=tk.BOTH, expand=True, pady=(0, 8))

        self.carver_tree = ttk.Treeview(
            list_box,
            columns=("id", "type", "lba", "size", "path"),
            show="headings",
            height=9
        )
        self.carver_tree.heading("id", text="#")
        self.carver_tree.heading("type", text="Tipo")
        self.carver_tree.heading("lba", text="Setor LBA")
        self.carver_tree.heading("size", text="Tamanho")
        self.carver_tree.heading("path", text="Arquivo Salvo no Destino")

        self.carver_tree.column("id", width=42, anchor=tk.CENTER)
        self.carver_tree.column("type", width=80, anchor=tk.CENTER)
        self.carver_tree.column("lba", width=110, anchor=tk.E)
        self.carver_tree.column("size", width=100, anchor=tk.E)
        self.carver_tree.column("path", width=480, anchor=tk.W)

        c_scroll = ttk.Scrollbar(list_box, orient=tk.VERTICAL, command=self.carver_tree.yview)
        self.carver_tree.configure(yscrollcommand=c_scroll.set)
        self.carver_tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        c_scroll.pack(side=tk.RIGHT, fill=tk.Y)

        # Rodapé de Ações
        carve_actions = ttk.Frame(f)
        carve_actions.pack(fill=tk.X, side=tk.BOTTOM)

        self.lbl_carve_status = ttk.Label(carve_actions, text="Pronto para iniciar File Carving.", font=("Segoe UI", 9))
        self.lbl_carve_status.pack(side=tk.LEFT)

        ttk.Button(carve_actions, text="📂 Abrir Pasta dos Recuperados", command=self._open_carve_folder).pack(side=tk.LEFT, padx=(12, 0))

        self.btn_start_carve = ttk.Button(
            carve_actions,
            text="🔍 Iniciar Carving Forense",
            style="Accent.TButton",
            command=self._start_carver
        )
        self.btn_start_carve.pack(side=tk.RIGHT)

        self.btn_cancel_carve = ttk.Button(
            carve_actions,
            text="⏸️ Interromper Carving",
            state=tk.DISABLED,
            command=self._cancel_carver
        )
        self.btn_cancel_carve.pack(side=tk.RIGHT, padx=(0, 6))

    # ══════════════════════════════════════════════════════════════════════════
    # TELA 6: VARREDURA DE SUPERFÍCIE (HEATMAP DE LATÊNCIA)
    # ══════════════════════════════════════════════════════════════════════════
    def _setup_view_surface(self):
        f = self.view_surface

        ttk.Label(
            f,
            text="Varredura de Superfície & Heatmap de Latência de Setor",
            font=("Segoe UI", 12, "bold")
        ).pack(anchor=tk.W, pady=(0, 2))

        ttk.Label(
            f,
            text="Mede o tempo de resposta do disco bloco a bloco (estilo Victoria / MHDD) para identificar setores degradados "
                 "antes que ocorra perda definitiva de dados.",
            font=("Segoe UI", 9),
            foreground="#94a3b8"
        ).pack(anchor=tk.W, pady=(0, 8))

        # Canvas do Heatmap (Grid de blocos coloridos)
        canvas_box = ttk.LabelFrame(f, text="Grade de Blocos em Tempo Real", padding=6)
        canvas_box.pack(fill=tk.BOTH, expand=True, pady=(0, 8))

        canvas_bg = "#121212" if self.current_theme == "dark" else "#f3f4f6"
        self.surface_canvas = tk.Canvas(canvas_box, bg=canvas_bg, height=220, highlightthickness=0)
        self.surface_canvas.pack(fill=tk.BOTH, expand=True)

        # Legenda e Contadores em Linha
        legend_frame = ttk.Frame(f)
        legend_frame.pack(fill=tk.X, pady=(0, 8))

        def make_legend(parent, color, label):
            f_item = ttk.Frame(parent)
            f_item.pack(side=tk.LEFT, padx=8)
            c = tk.Canvas(f_item, width=12, height=12, bg=color, highlightthickness=0)
            c.pack(side=tk.LEFT, padx=(0, 4))
            lbl = ttk.Label(f_item, text=label, font=("Segoe UI", 8))
            lbl.pack(side=tk.LEFT)
            return lbl

        self.leg_fast = make_legend(legend_frame, "#22c55e", "< 20ms: 0")
        self.leg_good = make_legend(legend_frame, "#3b82f6", "< 100ms: 0")
        self.leg_slow = make_legend(legend_frame, "#eab308", "< 250ms: 0")
        self.leg_vslow = make_legend(legend_frame, "#f97316", "< 1000ms: 0")
        self.leg_crit = make_legend(legend_frame, "#dc2626", "Atraso Crítico: 0")
        self.leg_bad = make_legend(legend_frame, "#7f1d1d", "Bad Sector: 0")

        # Barra de Ações
        surface_actions = ttk.Frame(f)
        surface_actions.pack(fill=tk.X, side=tk.BOTTOM)

        self.lbl_surface_status = ttk.Label(surface_actions, text="Pronto para testar superfície.", font=("Segoe UI", 9))
        self.lbl_surface_status.pack(side=tk.LEFT)

        self.btn_cancel_surface = ttk.Button(
            surface_actions,
            text="⏹️ Interromper",
            state=tk.DISABLED,
            command=self._cancel_surface
        )
        self.btn_cancel_surface.pack(side=tk.LEFT, padx=(12, 0))

        self.btn_start_surface = ttk.Button(
            surface_actions,
            text="⚡ Iniciar Teste de Superfície",
            style="Accent.TButton",
            command=self._start_surface_scan
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
            font=("Segoe UI", 12, "bold")
        ).pack(anchor=tk.W, pady=(0, 6))

        c_bg = "#111827" if self.current_theme == "dark" else "#f9fafb"
        c_fg = "#f3f4f6" if self.current_theme == "dark" else "#111827"

        self.txt_console = tk.Text(
            f,
            font=("Consolas", 9),
            wrap=tk.WORD,
            bg=c_bg,
            fg=c_fg,
            insertbackground="white",
            relief="flat",
            padx=10,
            pady=8
        )
        scroll = ttk.Scrollbar(f, orient=tk.VERTICAL, command=self.txt_console.yview)
        self.txt_console.configure(yscrollcommand=scroll.set)
        self.txt_console.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        scroll.pack(side=tk.RIGHT, fill=tk.Y)

        log_actions = ttk.Frame(f)
        log_actions.pack(fill=tk.X, side=tk.BOTTOM, pady=(8, 0))

        ttk.Button(log_actions, text="🧹 Limpar Log", command=lambda: self.txt_console.delete("1.0", tk.END)).pack(side=tk.LEFT)
        ttk.Button(log_actions, text="💾 Exportar Relatório (.txt)", command=self._save_log_file).pack(side=tk.RIGHT)

    # ── BARRA DE STATUS INFERIOR ──────────────────────────────────────────────
    def _build_status_bar(self):
        self.status_bar = ttk.Frame(self.root, padding=(14, 6))
        self.status_bar.pack(fill=tk.X, side=tk.BOTTOM)

        self.lbl_status = ttk.Label(self.status_bar, text="Pronto.", font=("Segoe UI", 9))
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
            self.txt_console.config(bg="#f9fafb", fg="#111827", insertbackground="black")
            self.surface_canvas.config(bg="#f3f4f6")
        else:
            sv_ttk.set_theme("dark")
            self.current_theme = "dark"
            self.btn_theme.config(text="🌙 Escuro")
            self.txt_console.config(bg="#111827", fg="#f3f4f6", insertbackground="white")
            self.surface_canvas.config(bg="#121212")

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
        raw_bg = "#14532d" if d.can_open_raw else "#7f1d1d"
        raw_fg = "#4ade80" if d.can_open_raw else "#fca5a5"

        self.lbl_drive_raw_badge.config(text=raw_text, background=raw_bg, foreground=raw_fg)
        self.lbl_drive_details.config(
            text=f"Caminho: {d.device_path}  |  Capacidade: {d.size_gb:.1f} GB ({d.size_bytes:,} bytes)  |  "
                 f"Mídia: {d.media_type}  |  Barramento: {d.bus_type}  |  Serial: {d.serial_number or 'N/A'}"
        )

        # Atualiza Mini Card da Sidebar
        self.lbl_mini_model.config(text=f"Disco {d.number}: {d.friendly_name}")
        self.lbl_mini_size.config(text=f"Tamanho: {d.size_gb:.1f} GB ({d.media_type})")
        self.lbl_mini_path.config(text=f"{d.device_path} ({d.bus_type})")

        # Atualiza Header da Aba de Diagnóstico
        self.lbl_target_info.config(
            text=f"Disco Alvo: [Disco {d.number}] {d.friendly_name} ({d.size_gb:.1f} GB)",
            font=("Segoe UI", 10, "bold")
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
        total_sectors = target_disk.size_bytes // 512

        try:
            cloner = ResilientCloner(target_disk.device_path, Path(dest), total_sectors)
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

        # Desenha quadradinho no Canvas com geometria responsiva
        box_size = 11
        gap = 2
        stride = box_size + gap
        cols = 54
        x = (self.block_canvas_idx % cols) * stride + 12
        y = (self.block_canvas_idx // cols) * stride + 12

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
    root = tk.Tk()
    app = DiskRescueApp(root)
    root.mainloop()


if __name__ == "__main__":
    run_gui()
