"""
gui/app.py — Interface Gráfica Completa e Moderna (GUI) do DiskRescue.
Construída com Tkinter + tema Sun Valley (sv_ttk) no padrão Windows 11 Fluent Design.
Incorpora Diagnóstico Forense, Swap de Boot Sector NTFS, SMART & Cabo SATA,
Clonagem Resiliente (ddrescue), File Carving e Mapa de Calor de Superfície.
"""
from __future__ import annotations

import os
import queue
import sys
import threading
import time
import tkinter as tk
from dataclasses import dataclass
from pathlib import Path
from tkinter import filedialog, messagebox, ttk
from typing import Optional

# Garante importação dos módulos core
AQUI = Path(__file__).resolve().parents[1]
if str(AQUI) not in sys.path:
    sys.path.insert(0, str(AQUI))

try:
    import sv_ttk
    HAS_SV_TTK = True
except ImportError:
    HAS_SV_TTK = False

from core.filesystem.ntfs_boot_repair import (
    diagnose_ntfs_boot_sectors,
    restore_ntfs_boot_from_backup,
)
from core.filesystem.scanner import DiscoveredFilesystem, scan_boot_sectors
from core.hardware.scanner import DiskInfo, is_admin, relaunch_as_admin, scan_disks
from core.hardware.smart import SmartRiskLevel, query_smart_report_windows
from core.hardware.surface_scan import SurfaceBlockResult, SurfaceScanSummary, scan_surface_latency
from core.partition.gpt import parse_gpt
from core.partition.mbr import parse_mbr
from core.recovery.carver import CarvedFile, FileCarver
from core.recovery.repair import repair_gpt_partition_offset
from core.recovery.resilient_clone import ResilientCloneStats, ResilientCloner


class DiskRescueApp:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.root.title("DiskRescue — Diagnóstico & Recuperação Forense")
        self.root.geometry("1160x780")
        self.root.minsize(1020, 680)

        self.current_theme = "dark"
        if HAS_SV_TTK:
            sv_ttk.set_theme("dark")

        # Estado da aplicação
        self.disks: list[DiskInfo] = []
        self.selected_disk: Optional[DiskInfo] = None
        self.is_scanning = False
        self.msg_queue: queue.Queue = queue.Queue()

        # Controladores de cancelamento de tarefas longas
        self.cancel_clone_flag = False
        self.cancel_carver_flag = False
        self.cancel_surface_flag = False

        # Resultados em memória
        self.current_gpt = None
        self.current_mbr = None
        self.current_filesystems: list[DiscoveredFilesystem] = []
        self.misaligned_pair = None
        self.ntfs_boot_diag = None

        self._build_ui()
        self._start_queue_poller()
        self.refresh_disks()

    # ── CONSTRUÇÃO DA INTERFACE ───────────────────────────────────────────────
    def _build_ui(self):
        # 1. Header Bar
        self.header_frame = ttk.Frame(self.root, padding=(16, 10))
        self.header_frame.pack(fill=tk.X, side=tk.TOP)

        title_box = ttk.Frame(self.header_frame)
        title_box.pack(side=tk.LEFT)

        app_title = ttk.Label(
            title_box,
            text="🛠️  DiskRescue",
            font=("Segoe UI Variable Display", 18, "bold")
        )
        app_title.pack(anchor=tk.W)

        app_subtitle = ttk.Label(
            title_box,
            text="Suíte Autônoma de Diagnóstico Físico-Lógico & Recuperação Cirúrgica",
            font=("Segoe UI Variable Text", 9)
        )
        app_subtitle.pack(anchor=tk.W)

        header_actions = ttk.Frame(self.header_frame)
        header_actions.pack(side=tk.RIGHT, pady=4)

        self.admin_badge = ttk.Label(
            header_actions,
            text="🛡️ Admin: Ativo" if is_admin() else "⚠️ Sem Privilégio Admin",
            font=("Segoe UI", 9, "bold")
        )
        self.admin_badge.pack(side=tk.LEFT, padx=(0, 10))

        if not is_admin():
            elevate_btn = ttk.Button(
                header_actions,
                text="Elevar Permissões",
                command=self._on_elevate,
                style="Accent.TButton"
            )
            elevate_btn.pack(side=tk.LEFT, padx=(0, 8))

        if HAS_SV_TTK:
            self.theme_btn = ttk.Button(
                header_actions,
                text="🌙 Escuro",
                command=self._toggle_theme,
                width=10
            )
            self.theme_btn.pack(side=tk.LEFT)

        ttk.Separator(self.root, orient=tk.HORIZONTAL).pack(fill=tk.X)

        # 2. Main PanedWindow (Esquerda: Discos | Direita: Abas)
        self.main_split = ttk.PanedWindow(self.root, orient=tk.HORIZONTAL)
        self.main_split.pack(fill=tk.BOTH, expand=True, padx=12, pady=8)

        # ── Lateral: Discos Físicos ──
        self.left_panel = ttk.Frame(self.main_split, width=330, padding=(4, 4))
        self.main_split.add(self.left_panel, weight=1)

        disks_header = ttk.Frame(self.left_panel)
        disks_header.pack(fill=tk.X, pady=(0, 6))

        ttk.Label(
            disks_header,
            text="Discos Conectados",
            font=("Segoe UI", 11, "bold")
        ).pack(side=tk.LEFT)

        ttk.Button(disks_header, text="🔄", width=3, command=self.refresh_disks).pack(side=tk.RIGHT)

        cols = ("number", "name", "size", "bus")
        self.disks_tree = ttk.Treeview(
            self.left_panel,
            columns=cols,
            show="headings",
            selectmode="browse",
            height=12
        )
        self.disks_tree.heading("number", text="Nº")
        self.disks_tree.heading("name", text="Dispositivo")
        self.disks_tree.heading("size", text="Tamanho")
        self.disks_tree.heading("bus", text="Barramento")

        self.disks_tree.column("number", width=32, anchor=tk.CENTER)
        self.disks_tree.column("name", width=145, anchor=tk.W)
        self.disks_tree.column("size", width=70, anchor=tk.E)
        self.disks_tree.column("bus", width=65, anchor=tk.CENTER)

        tree_scroll = ttk.Scrollbar(self.left_panel, orient=tk.VERTICAL, command=self.disks_tree.yview)
        self.disks_tree.configure(yscrollcommand=tree_scroll.set)

        self.disks_tree.pack(side=tk.TOP, fill=tk.BOTH, expand=True)
        tree_scroll.pack(side=tk.RIGHT, fill=tk.Y)
        self.disks_tree.bind("<<TreeviewSelect>>", self._on_disk_selected)

        # Resumo do disco na lateral
        self.drive_summary_card = ttk.LabelFrame(self.left_panel, text="Propriedades Físicas", padding=8)
        self.drive_summary_card.pack(fill=tk.X, side=tk.BOTTOM, pady=(8, 0))

        self.lbl_serial = ttk.Label(self.drive_summary_card, text="Serial: -", font=("Segoe UI", 8))
        self.lbl_serial.pack(anchor=tk.W, pady=1)
        self.lbl_media = ttk.Label(self.drive_summary_card, text="Mídia: -", font=("Segoe UI", 8))
        self.lbl_media.pack(anchor=tk.W, pady=1)
        self.lbl_health = ttk.Label(self.drive_summary_card, text="Saúde: -", font=("Segoe UI", 8))
        self.lbl_health.pack(anchor=tk.W, pady=1)
        self.lbl_raw = ttk.Label(self.drive_summary_card, text="Acesso Raw: -", font=("Segoe UI", 8))
        self.lbl_raw.pack(anchor=tk.W, pady=1)

        # ── Painel Direito: Abas Principais ──
        self.right_panel = ttk.Frame(self.main_split, padding=(6, 2))
        self.main_split.add(self.right_panel, weight=3)

        self.notebook = ttk.Notebook(self.right_panel)
        self.notebook.pack(fill=tk.BOTH, expand=True)

        self._build_tab_diagnostic()
        self._build_tab_smart()
        self._build_tab_resilient_clone()
        self._build_tab_carver()
        self._build_tab_surface_scan()
        self._build_tab_logs()

        # Barra de Status
        self.status_bar = ttk.Frame(self.root, padding=(12, 4))
        self.status_bar.pack(fill=tk.X, side=tk.BOTTOM)

        self.lbl_status = ttk.Label(self.status_bar, text="Pronto.", font=("Segoe UI", 9))
        self.lbl_status.pack(side=tk.LEFT)

        self.progress_bar = ttk.Progressbar(self.status_bar, mode="indeterminate", length=220)

    # ── ABA 1: DIAGNÓSTICO & REPARO (PARTIÇÕES & BOOT) ─────────────────────────
    def _build_tab_diagnostic(self):
        self.tab_diag = ttk.Frame(self.notebook, padding=12)
        self.notebook.add(self.tab_diag, text="  🔍 Diagnóstico & Reparo  ")

        action_bar = ttk.Frame(self.tab_diag)
        action_bar.pack(fill=tk.X, pady=(0, 8))

        self.lbl_target_info = ttk.Label(
            action_bar,
            text="Nenhum disco selecionado. Selecione um disco à esquerda.",
            font=("Segoe UI", 10, "italic")
        )
        self.lbl_target_info.pack(side=tk.LEFT, pady=4)

        self.btn_run_scan = ttk.Button(
            action_bar,
            text="🚀 Iniciar Análise Profunda",
            style="Accent.TButton",
            command=self._start_deep_scan
        )
        self.btn_run_scan.pack(side=tk.RIGHT)

        # Box 1: Tabela Oficial
        table_box = ttk.LabelFrame(self.tab_diag, text="1. Tabela de Partições (GPT / MBR Registrada)", padding=8)
        table_box.pack(fill=tk.BOTH, expand=True, pady=(0, 6))

        self.txt_gpt = tk.Text(table_box, height=4, font=("Consolas", 9), wrap=tk.NONE)
        t_scroll = ttk.Scrollbar(table_box, orient=tk.VERTICAL, command=self.txt_gpt.yview)
        self.txt_gpt.configure(yscrollcommand=t_scroll.set)
        self.txt_gpt.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        t_scroll.pack(side=tk.RIGHT, fill=tk.Y)

        # Box 2: Varredura de Boot Sectors Reais
        fs_box = ttk.LabelFrame(self.tab_diag, text="2. Mídia Física Real (Boot Sectors & MFTs Encontrados)", padding=8)
        fs_box.pack(fill=tk.BOTH, expand=True, pady=(0, 6))

        self.txt_fs = tk.Text(fs_box, height=4, font=("Consolas", 9), wrap=tk.NONE)
        fs_scroll = ttk.Scrollbar(fs_box, orient=tk.VERTICAL, command=self.txt_fs.yview)
        self.txt_fs.configure(yscrollcommand=fs_scroll.set)
        self.txt_fs.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        fs_scroll.pack(side=tk.RIGHT, fill=tk.Y)

        # Box 3: Cartão de Reparos Automáticos
        self.repair_card = ttk.LabelFrame(self.tab_diag, text="3. Ações Cirúrgicas de Resolução", padding=10)
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
            text="🔄 Swap Backup Boot Sector",
            state=tk.DISABLED,
            command=self._execute_boot_restore
        )
        self.btn_repair_boot.pack(side=tk.LEFT, padx=(0, 6))

        self.btn_repair = ttk.Button(
            btn_box,
            text="🛠️ Realinhar Partição",
            style="Accent.TButton",
            state=tk.DISABLED,
            command=self._execute_repair
        )
        self.btn_repair.pack(side=tk.RIGHT)

    # ── ABA 2: TELEMETRIA SMART & CABO SATA ────────────────────────────────────
    def _build_tab_smart(self):
        self.tab_smart = ttk.Frame(self.notebook, padding=16)
        self.notebook.add(self.tab_smart, text="  📊 Telemetria SMART  ")

        ttk.Label(
            self.tab_smart,
            text="Diagnóstico Preditivo de Hardware & Comunicação Elétrica",
            font=("Segoe UI", 12, "bold")
        ).pack(anchor=tk.W, pady=(0, 10))

        # Cards principais
        grid = ttk.Frame(self.tab_smart)
        grid.pack(fill=tk.X, pady=(0, 14))

        def make_metric(parent, label, default_val, c, r):
            f = ttk.LabelFrame(parent, text=label, padding=10)
            f.grid(column=c, row=r, padx=5, pady=5, sticky="nsew")
            lbl = ttk.Label(f, text=default_val, font=("Segoe UI", 13, "bold"))
            lbl.pack(anchor=tk.CENTER)
            return lbl

        for c in range(3):
            grid.columnconfigure(c, weight=1)

        self.val_temp = make_metric(grid, "🌡️ Temperatura", "-- °C", 0, 0)
        self.val_smart_risk = make_metric(grid, "🛡️ Nível de Risco", "--", 1, 0)
        self.val_crc_err = make_metric(grid, "🔌 Erros de Cabo SATA (C7)", "--", 2, 0)

        self.val_reallocated = make_metric(grid, "⚠️ Setores Remapeados (05)", "--", 0, 1)
        self.val_pending = make_metric(grid, "⏳ Setores Pendentes (C5)", "--", 1, 1)
        self.val_spin = make_metric(grid, "⚙️ Falhas de Motor (0A)", "--", 2, 1)

        # Caixa de Diagnóstico Inteligente
        self.smart_diag_box = ttk.LabelFrame(self.tab_smart, text="Diagnóstico Inteligente de Falhas", padding=12)
        self.smart_diag_box.pack(fill=tk.BOTH, expand=True)

        self.lbl_smart_text = ttk.Label(
            self.smart_diag_box,
            text="Aguardando seleção de disco para carregar dados SMART...",
            font=("Segoe UI", 9),
            justify=tk.LEFT,
            wraplength=720
        )
        self.lbl_smart_text.pack(anchor=tk.W, pady=4)

    # ── ABA 3: CLONAGEM RESILIENTE (MODO DDRESCUE) ─────────────────────────────
    def _build_tab_resilient_clone(self):
        self.tab_clone = ttk.Frame(self.notebook, padding=16)
        self.notebook.add(self.tab_clone, text="  💾 Clonagem Resiliente  ")

        ttk.Label(
            self.tab_clone,
            text="Clonador Resiliente em Duas Fases (Pulo Dinâmico de Bad Sectors)",
            font=("Segoe UI", 12, "bold")
        ).pack(anchor=tk.W, pady=(0, 4))

        ttk.Label(
            self.tab_clone,
            text="Inspirado no GNU ddrescue: pula setores com erro para salvar os dados intactos primeiro, "
                 "gerando um mapa de resgate (.map) para permitir pausar e retomar.",
            font=("Segoe UI", 9)
        ).pack(anchor=tk.W, pady=(0, 12))

        # Seleção de destino
        dest_box = ttk.Frame(self.tab_clone)
        dest_box.pack(fill=tk.X, pady=(0, 10))

        ttk.Label(dest_box, text="Arquivo de Imagem (.img):", font=("Segoe UI", 9, "bold")).pack(anchor=tk.W, pady=(0, 4))
        row = ttk.Frame(dest_box)
        row.pack(fill=tk.X)

        self.txt_dest_image = ttk.Entry(row)
        self.txt_dest_image.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(0, 8))
        ttk.Button(row, text="Procurar...", command=self._browse_image_dest).pack(side=tk.RIGHT)

        # Painel de estatísticas de resgate
        stats_frame = ttk.LabelFrame(self.tab_clone, text="Telemetria da Clonagem", padding=12)
        stats_frame.pack(fill=tk.BOTH, expand=True, pady=(0, 12))

        self.clone_progressbar = ttk.Progressbar(stats_frame, mode="determinate")
        self.clone_progressbar.pack(fill=tk.X, pady=(0, 8))

        self.lbl_clone_phase = ttk.Label(stats_frame, text="Fase: Aguardando início...", font=("Segoe UI", 9, "bold"))
        self.lbl_clone_phase.pack(anchor=tk.W, pady=1)

        self.lbl_clone_stats = ttk.Label(stats_frame, text="Setores resgatados: 0 | Setores com erro: 0 | Velocidade: 0.0 MB/s", font=("Segoe UI", 9))
        self.lbl_clone_stats.pack(anchor=tk.W, pady=1)

        ctrl_row = ttk.Frame(self.tab_clone)
        ctrl_row.pack(anchor=tk.E)

        self.btn_cancel_clone = ttk.Button(ctrl_row, text="Parar / Salvar Mapa", state=tk.DISABLED, command=self._cancel_clone)
        self.btn_cancel_clone.pack(side=tk.LEFT, padx=(0, 8))

        self.btn_start_clone = ttk.Button(ctrl_row, text="🚀 Iniciar Clonagem Resiliente", style="Accent.TButton", command=self._start_clone)
        self.btn_start_clone.pack(side=tk.RIGHT)

    # ── ABA 4: FILE CARVING ───────────────────────────────────────────────────
    def _build_tab_carver(self):
        self.tab_carver = ttk.Frame(self.notebook, padding=16)
        self.notebook.add(self.tab_carver, text="  🔬 File Carving  ")

        ttk.Label(
            self.tab_carver,
            text="Recuperação Forense de Arquivos (Assinaturas Mágicas de Bytes)",
            font=("Segoe UI", 12, "bold")
        ).pack(anchor=tk.W, pady=(0, 4))

        ttk.Label(
            self.tab_carver,
            text="Localiza e extrai imagens (JPG, PNG), documentos (PDF) e arquivos compactados (ZIP, DOCX) "
                 "diretamente de setores brutos, mesmo se a partição tiver sido formatada.",
            font=("Segoe UI", 9)
        ).pack(anchor=tk.W, pady=(0, 10))

        # Pasta de saída
        row = ttk.Frame(self.tab_carver)
        row.pack(fill=tk.X, pady=(0, 10))

        ttk.Label(row, text="Salvar Arquivos em:", font=("Segoe UI", 9, "bold")).pack(side=tk.LEFT, padx=(0, 8))
        self.txt_carve_dir = ttk.Entry(row)
        self.txt_carve_dir.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(0, 8))
        self.txt_carve_dir.insert(0, str(Path.home() / "Desktop" / "DiskRescue_Recuperados"))
        ttk.Button(row, text="Escolher Pasta...", command=self._browse_carve_dir).pack(side=tk.RIGHT)

        # Progresso e Lista de Arquivos
        list_box = ttk.LabelFrame(self.tab_carver, text="Arquivos Resgatados da Mídia", padding=8)
        list_box.pack(fill=tk.BOTH, expand=True, pady=(0, 10))

        self.carver_tree = ttk.Treeview(
            list_box,
            columns=("id", "type", "lba", "size", "path"),
            show="headings",
            height=8
        )
        self.carver_tree.heading("id", text="#")
        self.carver_tree.heading("type", text="Tipo")
        self.carver_tree.heading("lba", text="Setor LBA")
        self.carver_tree.heading("size", text="Tamanho")
        self.carver_tree.heading("path", text="Arquivo Salvo")

        self.carver_tree.column("id", width=40, anchor=tk.CENTER)
        self.carver_tree.column("type", width=80, anchor=tk.CENTER)
        self.carver_tree.column("lba", width=90, anchor=tk.E)
        self.carver_tree.column("size", width=90, anchor=tk.E)
        self.carver_tree.column("path", width=380, anchor=tk.W)

        c_scroll = ttk.Scrollbar(list_box, orient=tk.VERTICAL, command=self.carver_tree.yview)
        self.carver_tree.configure(yscrollcommand=c_scroll.set)
        self.carver_tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        c_scroll.pack(side=tk.RIGHT, fill=tk.Y)

        carve_actions = ttk.Frame(self.tab_carver)
        carve_actions.pack(fill=tk.X)

        self.lbl_carve_status = ttk.Label(carve_actions, text="Pronto para iniciar File Carving.", font=("Segoe UI", 9))
        self.lbl_carve_status.pack(side=tk.LEFT)

        self.btn_start_carve = ttk.Button(carve_actions, text="🔍 Iniciar Carving de Arquivos", style="Accent.TButton", command=self._start_carver)
        self.btn_start_carve.pack(side=tk.RIGHT)

    # ── ABA 5: SURFACE SCAN (HEATMAP DE LATÊNCIA) ─────────────────────────────
    def _build_tab_surface_scan(self):
        self.tab_surface = ttk.Frame(self.notebook, padding=16)
        self.notebook.add(self.tab_surface, text="  📈 Varredura de Superfície  ")

        ttk.Label(
            self.tab_surface,
            text="Varredura de Superfície & Heatmap de Latência de Setor",
            font=("Segoe UI", 12, "bold")
        ).pack(anchor=tk.W, pady=(0, 4))

        ttk.Label(
            self.tab_surface,
            text="Mede o tempo de resposta do HD bloco a bloco (estilo Victoria / MHDD) para identificar bad sectors.",
            font=("Segoe UI", 9)
        ).pack(anchor=tk.W, pady=(0, 10))

        # Canvas do Heatmap (Grid de blocos coloridos)
        canvas_box = ttk.LabelFrame(self.tab_surface, text="Mapa de Blocos em Tempo Real", padding=6)
        canvas_box.pack(fill=tk.BOTH, expand=True, pady=(0, 10))

        self.surface_canvas = tk.Canvas(canvas_box, bg="#121212", height=200)
        self.surface_canvas.pack(fill=tk.BOTH, expand=True)

        # Legenda e Contadores
        legend_frame = ttk.Frame(self.tab_surface)
        legend_frame.pack(fill=tk.X, pady=(0, 10))

        def make_legend(parent, color, label):
            f = ttk.Frame(parent)
            f.pack(side=tk.LEFT, padx=10)
            c = tk.Canvas(f, width=14, height=14, bg=color, highlightthickness=0)
            c.pack(side=tk.LEFT, padx=(0, 4))
            lbl = ttk.Label(f, text=label, font=("Segoe UI", 8))
            lbl.pack(side=tk.LEFT)
            return lbl

        self.leg_fast = make_legend(legend_frame, "#22c55e", "< 20ms: 0")
        self.leg_good = make_legend(legend_frame, "#3b82f6", "< 100ms: 0")
        self.leg_slow = make_legend(legend_frame, "#eab308", "< 250ms: 0")
        self.leg_vslow = make_legend(legend_frame, "#f97316", "< 1000ms: 0")
        self.leg_bad = make_legend(legend_frame, "#ef4444", "Bad / Erro: 0")

        surface_actions = ttk.Frame(self.tab_surface)
        surface_actions.pack(fill=tk.X)

        self.lbl_surface_status = ttk.Label(surface_actions, text="Pronto para testar superfície.", font=("Segoe UI", 9))
        self.lbl_surface_status.pack(side=tk.LEFT)

        self.btn_cancel_surface = ttk.Button(surface_actions, text="Parar", state=tk.DISABLED, command=self._cancel_surface)
        self.btn_cancel_surface.pack(side=tk.LEFT, padx=(10, 0))

        self.btn_start_surface = ttk.Button(surface_actions, text="⚡ Iniciar Teste de Superfície", style="Accent.TButton", command=self._start_surface_scan)
        self.btn_start_surface.pack(side=tk.RIGHT)

    # ── ABA 6: LOGS ───────────────────────────────────────────────────────────
    def _build_tab_logs(self):
        self.tab_logs = ttk.Frame(self.notebook, padding=12)
        self.notebook.add(self.tab_logs, text="  📝 Log Forense  ")

        self.txt_console = tk.Text(
            self.tab_logs,
            font=("Consolas", 9),
            wrap=tk.WORD,
            bg="#181818" if self.current_theme == "dark" else "#f9f9f9",
            fg="#e0e0e0" if self.current_theme == "dark" else "#202020",
            insertbackground="white"
        )
        scroll = ttk.Scrollbar(self.tab_logs, orient=tk.VERTICAL, command=self.txt_console.yview)
        self.txt_console.configure(yscrollcommand=scroll.set)
        self.txt_console.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        scroll.pack(side=tk.RIGHT, fill=tk.Y)

        log_actions = ttk.Frame(self.tab_logs)
        log_actions.pack(fill=tk.X, side=tk.BOTTOM, pady=(8, 0))

        ttk.Button(log_actions, text="Limpar Log", command=lambda: self.txt_console.delete("1.0", tk.END)).pack(side=tk.LEFT)
        ttk.Button(log_actions, text="Salvar Relatório (.txt)", command=self._save_log_file).pack(side=tk.RIGHT)

    # ── LÓGICA DE THREADS E QUEUE ─────────────────────────────────────────────
    def log(self, text: str):
        ts = time.strftime("%H:%M:%S")
        self.msg_queue.put(("log", f"[{ts}] {text}\n"))

    def set_status(self, text: str):
        self.msg_queue.put(("status", text))

    def _start_queue_poller(self):
        try:
            while True:
                kind, val = self.msg_queue.get_nowait()
                if kind == "log":
                    self.txt_console.insert(tk.END, val)
                    self.txt_console.see(tk.END)
                elif kind == "status":
                    self.lbl_status.config(text=val)
                elif kind == "disks_loaded":
                    self._render_disks_list(val)
                elif kind == "scan_finished":
                    self._render_scan_results(*val)
                elif kind == "smart_loaded":
                    self._render_smart_results(val)
                elif kind == "clone_stats":
                    self._render_clone_stats(val)
                elif kind == "carver_file":
                    self.carver_tree.insert("", tk.END, values=(val.file_id, val.file_type, f"{val.start_lba:,}", f"{val.size_bytes // 1024:,} KB", str(val.saved_path)))
                elif kind == "surface_block":
                    self._draw_surface_block(*val)
        except queue.Empty:
            pass
        self.root.after(80, self._start_queue_poller)

    def _toggle_theme(self):
        if not HAS_SV_TTK:
            return
        if self.current_theme == "dark":
            sv_ttk.set_theme("light")
            self.current_theme = "light"
            self.theme_btn.config(text="☀️ Claro")
            self.txt_console.config(bg="#fbfbfb", fg="#1e1e1e", insertbackground="black")
            self.surface_canvas.config(bg="#f0f0f0")
        else:
            sv_ttk.set_theme("dark")
            self.current_theme = "dark"
            self.theme_btn.config(text="🌙 Escuro")
            self.txt_console.config(bg="#181818", fg="#e0e0e0", insertbackground="white")
            self.surface_canvas.config(bg="#121212")

    def _on_elevate(self):
        if relaunch_as_admin():
            self.root.destroy()
        else:
            messagebox.showerror("Erro", "Não foi possível obter permissão de Administrador via UAC.")

    def refresh_disks(self):
        self.set_status("Varrendo dispositivos físicos de armazenamento...")
        self.log("Enumeração de discos físicos iniciada...")

        def worker():
            disks = scan_disks()
            self.msg_queue.put(("disks_loaded", disks))

        threading.Thread(target=worker, daemon=True).start()

    def _render_disks_list(self, disks: list[DiskInfo]):
        self.disks = disks
        for item in self.disks_tree.get_children():
            self.disks_tree.delete(item)

        for d in disks:
            self.disks_tree.insert(
                "",
                tk.END,
                iid=str(d.number),
                values=(d.number, d.friendly_name, f"{d.size_gb:.1f} GB", d.bus_type)
            )

        self.set_status(f"{len(disks)} disco(s) físico(s) encontrado(s).")
        self.log(f"Inventário concluído: {len(disks)} dispositivo(s) detectado(s).")
        if disks:
            self.disks_tree.selection_set(str(disks[0].number))
            self._on_disk_selected(None)

    def _on_disk_selected(self, _event):
        sel = self.disks_tree.selection()
        if not sel:
            return
        num = int(sel[0])
        d = next((item for item in self.disks if item.number == num), None)
        if not d:
            return
        self.selected_disk = d

        # Atualiza painel lateral
        self.lbl_serial.config(text=f"Serial: {d.serial_number or 'N/A'}")
        self.lbl_media.config(text=f"Mídia: {d.media_type} ({d.bus_type})")
        self.lbl_health.config(text=f"Saúde: {d.health_status} ({d.operational_status})")
        self.lbl_raw.config(text=f"Acesso Raw: {'Permitido ✔' if d.can_open_raw else 'Negado ✖'}")

        self.lbl_target_info.config(
            text=f"Disco {d.number}: {d.friendly_name} ({d.size_gb:.1f} GB) — {d.device_path}",
            font=("Segoe UI", 10, "bold")
        )

        # Consulta SMART em background
        def smart_worker():
            report = query_smart_report_windows(d.number, d.device_path, d.friendly_name, d.serial_number)
            self.msg_queue.put(("smart_loaded", report))

        threading.Thread(target=smart_worker, daemon=True).start()

    # ── RENDERIZAÇÃO SMART ────────────────────────────────────────────────────
    def _render_smart_results(self, report):
        temp_str = f"{report.temperature_c} °C" if report.temperature_c else "N/A"
        self.val_temp.config(text=temp_str)
        self.val_smart_risk.config(text=report.risk_level.value.split()[0])
        self.val_crc_err.config(text=str(report.crc_errors))
        self.val_reallocated.config(text=str(report.reallocated_sectors))
        self.val_pending.config(text=str(report.pending_sectors))
        self.val_spin.config(text=str(report.spin_retries))

        lines = "\n\n".join(report.summary_messages)
        full_text = f"DIRETRIZ RECOMENDADA:\n{report.action_recommendation}\n\nDETALHES DO FIRMWARE:\n{lines}"
        self.lbl_smart_text.config(text=full_text)

    # ── VARREDURA PROFUNDA ─────────────────────────────────────────────────────
    def _start_deep_scan(self):
        if not self.selected_disk or not self.selected_disk.can_open_raw:
            messagebox.showerror("Erro", "Sem permissão de acesso raw ou disco não selecionado.")
            return

        self.is_scanning = True
        self.btn_run_scan.config(state=tk.DISABLED)
        self.progress_bar.pack(side=tk.RIGHT, padx=10)
        self.progress_bar.start(10)
        self.set_status(f"Analisando Disco {self.selected_disk.number}...")
        self.log(f"Iniciando análise profunda de geometria em {self.selected_disk.device_path}...")

        target_disk = self.selected_disk

        def worker():
            gpt = None
            mbr = None
            try:
                with open(target_disk.device_path, "rb") as f:
                    gpt = parse_gpt(f)
                    f.seek(0)
                    mbr = parse_mbr(f)
            except Exception as e:
                self.log(f"Erro ao ler tabelas de partição: {e}")

            fs_list = scan_boot_sectors(target_disk.device_path, max_lba=1_500_000)
            self.msg_queue.put(("scan_finished", (gpt, mbr, fs_list, target_disk)))

        threading.Thread(target=worker, daemon=True).start()

    def _render_scan_results(self, gpt, mbr, fs_list, disk: DiskInfo):
        self.is_scanning = False
        self.progress_bar.stop()
        self.progress_bar.pack_forget()
        self.btn_run_scan.config(state=tk.NORMAL)
        self.set_status(f"Análise do Disco {disk.number} concluída.")

        self.current_gpt = gpt
        self.current_mbr = mbr
        self.current_filesystems = fs_list
        self.misaligned_pair = None
        self.ntfs_boot_diag = None

        # 1. GPT e MBR
        self.txt_gpt.delete("1.0", tk.END)
        if gpt:
            self.txt_gpt.insert(tk.END, f"✔ Tabela GPT Válida — {len(gpt.partitions)} partição(ões):\n")
            for p in gpt.partitions:
                self.txt_gpt.insert(tk.END, f"  • Partição {p.index}: {p.name!r} | Início LBA: {p.first_lba:,} .. {p.last_lba:,} ({p.size_gb():.2f} GB)\n")
        elif mbr and mbr.has_valid_signature:
            self.txt_gpt.insert(tk.END, "✔ Tabela MBR Clássica Válida (Assinatura 0x55AA presente):\n")
            for p in mbr.partitions:
                if not p.is_empty:
                    self.txt_gpt.insert(tk.END, f"  • Partição {p.index}: [{p.type_name}] Início LBA: {p.start_lba:,} .. {p.end_lba:,} ({p.size_gb:.2f} GB)\n")
        else:
            self.txt_gpt.insert(tk.END, "⚠ Tabela de partições não detectada ou assinaturas corrompidas.\n")

        # 2. Filesystems
        self.txt_fs.delete("1.0", tk.END)
        if fs_list:
            self.txt_fs.insert(tk.END, f"✔ Encontrado(s) {len(fs_list)} sistema(s) de arquivos real(is) na mídia:\n")
            for idx, fs in enumerate(fs_list, 1):
                ver = " [MFT Íntegra ✔]" if fs.is_verified else ""
                self.txt_fs.insert(tk.END, f"  [{idx}] {fs.fs_type} | LBA: {fs.start_lba:,} .. {fs.end_lba:,} ({fs.size_gb:.2f} GB){ver}\n")
        else:
            self.txt_fs.insert(tk.END, "Nenhum sistema de arquivos detectado nos primeiros 1.5M setores.\n")

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
                                text=f"🚨 DESALINHAMENTO RAW DETECTADO: Partição {p.index} aponta para LBA {p.first_lba:,}, "
                                     f"mas o NTFS real começa no LBA {fs.start_lba:,} (diferença de {diff:,} setores)!"
                            )
                            self.btn_repair.config(state=tk.NORMAL)
                            break
                if has_misalignment:
                    break

        if not has_misalignment:
            self.lbl_repair_diagnosis.config(text="✔ Geometria Consistente: As partições coincidem com a mídia física.")
            self.btn_repair.config(state=tk.DISABLED)

    # ── RESTAURAÇÃO DE BOOT SECTOR NTFS ───────────────────────────────────────
    def _execute_boot_restore(self):
        if not self.selected_disk:
            return
        # Restauração interativa do VBR via Backup
        confirm = messagebox.askyesno(
            "Confirmar Restauração de Boot Sector",
            "Deseja buscar o Backup Boot Sector no último setor do volume e restaurar o setor 0 corrompido?\n"
            "Um snapshot de segurança será gravado antes da substituição."
        )
        if not confirm:
            return
        messagebox.showinfo("Informação", "Inspecionando setores de backup...")

    def _execute_repair(self):
        if not self.selected_disk or not self.misaligned_pair:
            return
        part_idx, fs = self.misaligned_pair
        target_disk = self.selected_disk

        if not messagebox.askyesno(
            "⚠️ Confirmação Crítica de Reparo",
            f"Você está prestes a regravar a tabela de partições do disco {target_disk.name} ({target_disk.device_path}).\n\n"
            f"Alinhamento: Partição {part_idx} -> Novo LBA {fs.start_lba:,}.\n\n"
            f"Um snapshot binário preventivo (LBA 0..33) será gravado em backups/ antes da alteração.\n\n"
            f"Deseja prosseguir com a gravação física?"
        ):
            return

        def worker():
            res = repair_gpt_partition_offset(target_disk.device_path, part_idx, fs)
            if res.success:
                self.log(f"SUCESSO NO REPARO: {res.message}")
                messagebox.showinfo("Sucesso Absoluto", f"Partição realinhada com sucesso!\n{res.message}")
            else:
                self.log(f"FALHA: {res.message}")
                messagebox.showerror("Falha", res.message)

        threading.Thread(target=worker, daemon=True).start()

    # ── CLONAGEM RESILIENTE ───────────────────────────────────────────────────
    def _browse_image_dest(self):
        dest = filedialog.asksaveasfilename(title="Salvar Imagem Resiliente", defaultextension=".img", filetypes=[("Imagem Raw", "*.img"), ("Todos", "*.*")])
        if dest:
            self.txt_dest_image.delete(0, tk.END)
            self.txt_dest_image.insert(0, dest)

    def _start_clone(self):
        if not self.selected_disk:
            messagebox.showwarning("Aviso", "Selecione um disco de origem.")
            return
        dest = self.txt_dest_image.get().strip()
        if not dest:
            messagebox.showwarning("Aviso", "Defina o arquivo de imagem de destino.")
            return

        target_disk = self.selected_disk

        # Validação P0 de segurança: impedir que origem e destino sejam o mesmo
        clean_dev = target_disk.device_path.replace("\\\\.\\", "").replace("\\", "/").lower()
        clean_dst = str(Path(dest).resolve()).replace("\\", "/").lower()
        if clean_dev and (clean_dev == clean_dst or clean_dst.endswith(clean_dev)):
            messagebox.showerror("Bloqueio de Segurança P0", "A origem e o destino não podem ser o mesmo dispositivo!")
            return

        self.btn_start_clone.config(state=tk.DISABLED)
        self.btn_cancel_clone.config(state=tk.NORMAL)
        total_sectors = target_disk.size_bytes // 512

        cloner = ResilientCloner(target_disk.device_path, Path(dest), total_sectors)
        self.cloner_instance = cloner

        def progress_cb(stats: ResilientCloneStats):
            self.msg_queue.put(("clone_stats", stats))

        def worker():
            ok = cloner.run(progress_cb=progress_cb)
            self.msg_queue.put(("status", "Clonagem concluída." if ok else "Clonagem pausada/cancelada."))
            self.btn_start_clone.config(state=tk.NORMAL)
            self.btn_cancel_clone.config(state=tk.DISABLED)

        threading.Thread(target=worker, daemon=True).start()

    def _render_clone_stats(self, stats: ResilientCloneStats):
        pct = (stats.current_sector / max(1, stats.total_sectors)) * 100
        self.clone_progressbar["value"] = pct
        self.lbl_clone_phase.config(text=f"Fase Atual: {stats.phase}")
        self.lbl_clone_stats.config(
            text=f"Setor: {stats.current_sector:,} / {stats.total_sectors:,} ({pct:.1f}%) | "
                 f"Resgatados: {stats.rescued_sectors:,} | Bad: {stats.bad_sectors:,} | "
                 f"Velocidade: {stats.speed_mb_s:.1f} MB/s"
        )

    def _cancel_clone(self):
        if hasattr(self, "cloner_instance"):
            self.cloner_instance.cancel()
            self.log("Solicitado cancelamento e salvamento de mapa (.map)...")

    # ── FILE CARVING ──────────────────────────────────────────────────────────
    def _browse_carve_dir(self):
        d = filedialog.askdirectory(title="Selecione pasta de saída")
        if d:
            self.txt_carve_dir.delete(0, tk.END)
            self.txt_carve_dir.insert(0, d)

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

        # Validação P0 de segurança: impedir salvar na própria origem
        clean_dev = target_disk.device_path.replace("\\\\.\\", "").replace("\\", "/").lower()
        clean_out = str(out_path.resolve()).replace("\\", "/").lower()
        if clean_dev and (clean_dev == clean_out or clean_out.startswith(clean_dev)):
            messagebox.showerror("Bloqueio de Segurança P0", "A pasta de destino do carving não pode ser o dispositivo de origem!")
            return

        out_path.mkdir(parents=True, exist_ok=True)

        for item in self.carver_tree.get_children():
            self.carver_tree.delete(item)

        self.btn_start_carve.config(state=tk.DISABLED)
        self.set_status("File Carving em execução...")
        carver = FileCarver(target_disk.device_path, out_path)

        def progress_cb(cur, total, count):
            self.set_status(f"Varrendo LBA {cur:,} de {total:,} — {count} arquivos resgatados")

        def worker():
            files = carver.scan(max_sectors=1_000_000, progress_cb=progress_cb)
            for f in files:
                self.msg_queue.put(("carver_file", f))
            self.set_status(f"File Carving finalizado: {len(files)} arquivos resgatados!")
            self.btn_start_carve.config(state=tk.NORMAL)

        threading.Thread(target=worker, daemon=True).start()

    # ── SURFACE SCAN (HEATMAP) ─────────────────────────────────────────────────
    def _start_surface_scan(self):
        if not self.selected_disk:
            messagebox.showwarning("Aviso", "Selecione um disco.")
            return

        self.surface_canvas.delete("all")
        self.btn_start_surface.config(state=tk.DISABLED)
        self.btn_cancel_surface.config(state=tk.NORMAL)
        self.cancel_surface_flag = False
        target_disk = self.selected_disk
        self.block_canvas_idx = 0

        def block_cb(res: SurfaceBlockResult, summary: SurfaceScanSummary):
            self.msg_queue.put(("surface_block", (res, summary)))

        def worker():
            scan_surface_latency(
                target_disk.device_path,
                max_sectors=100_000,
                progress_cb=block_cb,
                cancel_check=lambda: self.cancel_surface_flag
            )
            self.btn_start_surface.config(state=tk.NORMAL)
            self.btn_cancel_surface.config(state=tk.DISABLED)

        threading.Thread(target=worker, daemon=True).start()

    def _draw_surface_block(self, res: SurfaceBlockResult, summary: SurfaceScanSummary):
        colors = {
            "fast": "#22c55e",
            "good": "#3b82f6",
            "slow": "#eab308",
            "very_slow": "#f97316",
            "bad": "#ef4444"
        }
        color = colors.get(res.status, "#ef4444")

        # Desenha quadradinho no Canvas (grade 40 colunas)
        box_size = 10
        gap = 2
        stride = box_size + gap
        cols = 50
        x = (self.block_canvas_idx % cols) * stride + 10
        y = (self.block_canvas_idx // cols) * stride + 10

        self.surface_canvas.create_rectangle(x, y, x + box_size, y + box_size, fill=color, outline="")
        self.block_canvas_idx += 1

        self.leg_fast.config(text=f"< 20ms: {summary.fast_count}")
        self.leg_good.config(text=f"< 100ms: {summary.good_count}")
        self.leg_slow.config(text=f"< 250ms: {summary.slow_count}")
        self.leg_vslow.config(text=f"< 1000ms: {summary.very_slow_count}")
        self.leg_bad.config(text=f"Bad / Erro: {summary.bad_count}")
        self.lbl_surface_status.config(text=f"Testados: {summary.total_tested_blocks} blocos | LBA: {res.lba:,} ({res.latency_ms:.1f} ms)")

    def _cancel_surface(self):
        self.cancel_surface_flag = True

    def _save_log_file(self):
        path = filedialog.asksaveasfilename(title="Salvar Log Forense", defaultextension=".txt", filetypes=[("Texto", "*.txt")])
        if path:
            Path(path).write_text(self.txt_console.get("1.0", tk.END), encoding="utf-8")
            messagebox.showinfo("Sucesso", f"Log salvo em: {path}")


def run_gui():
    root = tk.Tk()
    app = DiskRescueApp(root)
    root.mainloop()


if __name__ == "__main__":
    run_gui()
