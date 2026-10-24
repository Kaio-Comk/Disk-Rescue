"""
gui/app.py — Interface Gráfica Moderna (GUI) do DiskRescue.
Construída com Tkinter + tema Sun Valley (sv_ttk) no estilo Windows 11 Fluent Design.
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

from core.filesystem.scanner import DiscoveredFilesystem, scan_boot_sectors
from core.hardware.scanner import DiskInfo, is_admin, relaunch_as_admin, scan_disks
from core.partition.gpt import parse_gpt
from core.recovery.repair import create_raw_disk_image, repair_gpt_partition_offset


class DiskRescueApp:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.root.title("DiskRescue — Diagnóstico & Recuperação Forense")
        self.root.geometry("1100x720")
        self.root.minsize(980, 640)

        # Configura tema moderno Sun Valley (dark mode por padrão)
        self.current_theme = "dark"
        if HAS_SV_TTK:
            sv_ttk.set_theme("dark")

        # Estado da aplicação
        self.disks: list[DiskInfo] = []
        self.selected_disk: Optional[DiskInfo] = None
        self.is_scanning = False
        self.cancel_image_flag = False
        self.msg_queue: queue.Queue = queue.Queue()

        # Últimos resultados de análise
        self.current_gpt = None
        self.current_filesystems: list[DiscoveredFilesystem] = []
        self.misaligned_pair: Optional[tuple[int, DiscoveredFilesystem]] = None

        self._build_ui()
        self._start_queue_poller()

        # Carrega discos inicialmente em thread separada
        self.refresh_disks()

    # ── CONSTRUÇÃO DA INTERFACE ───────────────────────────────────────────────
    def _build_ui(self):
        # 1. Top Header Bar
        self.header_frame = ttk.Frame(self.root, padding=(16, 12))
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
            text="Sistema Autônomo de Diagnóstico Profundo & Recuperação Não Destrutiva",
            font=("Segoe UI Variable Text", 9)
        )
        app_subtitle.pack(anchor=tk.W)

        # Painel direito do Header (Admin status e troca de tema)
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

        # Linha separadora
        ttk.Separator(self.root, orient=tk.HORIZONTAL).pack(fill=tk.X)

        # 2. Main Content (Split em Coluna Esquerda e Painel Central)
        self.main_split = ttk.PanedWindow(self.root, orient=tk.HORIZONTAL)
        self.main_split.pack(fill=tk.BOTH, expand=True, padx=12, pady=10)

        # ── Coluna Esquerda: Lista de Discos ──
        self.left_panel = ttk.Frame(self.main_split, width=320, padding=(4, 4))
        self.main_split.add(self.left_panel, weight=1)

        disks_header = ttk.Frame(self.left_panel)
        disks_header.pack(fill=tk.X, pady=(0, 6))

        ttk.Label(
            disks_header,
            text="Discos Conectados",
            font=("Segoe UI", 11, "bold")
        ).pack(side=tk.LEFT)

        self.refresh_btn = ttk.Button(
            disks_header,
            text="🔄",
            width=3,
            command=self.refresh_disks
        )
        self.refresh_btn.pack(side=tk.RIGHT)

        # Treeview com os discos
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

        self.disks_tree.column("number", width=36, anchor=tk.CENTER)
        self.disks_tree.column("name", width=140, anchor=tk.W)
        self.disks_tree.column("size", width=70, anchor=tk.E)
        self.disks_tree.column("bus", width=70, anchor=tk.CENTER)

        tree_scroll = ttk.Scrollbar(self.left_panel, orient=tk.VERTICAL, command=self.disks_tree.yview)
        self.disks_tree.configure(yscrollcommand=tree_scroll.set)

        self.disks_tree.pack(side=tk.TOP, fill=tk.BOTH, expand=True)
        tree_scroll.pack(side=tk.RIGHT, fill=tk.Y)

        self.disks_tree.bind("<<TreeviewSelect>>", self._on_disk_selected)

        # Card de Resumo do Disco Selecionado na Lateral
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

        # ── Coluna Direita: Abas de Operação ──
        self.right_panel = ttk.Frame(self.main_split, padding=(6, 2))
        self.main_split.add(self.right_panel, weight=3)

        self.notebook = ttk.Notebook(self.right_panel)
        self.notebook.pack(fill=tk.BOTH, expand=True)

        self._build_tab_diagnostic()
        self._build_tab_health()
        self._build_tab_clone()
        self._build_tab_logs()

        # 3. Status Bar Inferior
        self.status_bar = ttk.Frame(self.root, padding=(12, 4))
        self.status_bar.pack(fill=tk.X, side=tk.BOTTOM)

        self.lbl_status = ttk.Label(self.status_bar, text="Pronto.", font=("Segoe UI", 9))
        self.lbl_status.pack(side=tk.LEFT)

        self.progress_bar = ttk.Progressbar(self.status_bar, mode="indeterminate", length=220)
        # exibido dinamicamente quando is_scanning = True

    # ── ABA 1: DIAGNÓSTICO & REPARO ───────────────────────────────────────────
    def _build_tab_diagnostic(self):
        self.tab_diag = ttk.Frame(self.notebook, padding=12)
        self.notebook.add(self.tab_diag, text="  🔍 Diagnóstico & Reparo  ")

        # Barra de Ação Superior da Aba
        action_bar = ttk.Frame(self.tab_diag)
        action_bar.pack(fill=tk.X, pady=(0, 10))

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

        # Card 1: Tabela GPT Oficial
        gpt_box = ttk.LabelFrame(self.tab_diag, text="1. Tabela de Partições (GPT / MBR Oficial)", padding=10)
        gpt_box.pack(fill=tk.BOTH, expand=True, pady=(0, 8))

        self.txt_gpt = tk.Text(gpt_box, height=5, font=("Consolas", 9), wrap=tk.NONE)
        gpt_scroll = ttk.Scrollbar(gpt_box, orient=tk.VERTICAL, command=self.txt_gpt.yview)
        self.txt_gpt.configure(yscrollcommand=gpt_scroll.set)
        self.txt_gpt.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        gpt_scroll.pack(side=tk.RIGHT, fill=tk.Y)

        # Card 2: Sistemas de Arquivos Reais Descobertos na Mídia
        fs_box = ttk.LabelFrame(self.tab_diag, text="2. Dados Físicos Reais (Varredura de Boot Sectors & MFT)", padding=10)
        fs_box.pack(fill=tk.BOTH, expand=True, pady=(0, 8))

        self.txt_fs = tk.Text(fs_box, height=5, font=("Consolas", 9), wrap=tk.NONE)
        fs_scroll = ttk.Scrollbar(fs_box, orient=tk.VERTICAL, command=self.txt_fs.yview)
        self.txt_fs.configure(yscrollcommand=fs_scroll.set)
        self.txt_fs.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        fs_scroll.pack(side=tk.RIGHT, fill=tk.Y)

        # Card 3: Banner de Diagnóstico e Ação de Reparo
        self.repair_card = ttk.LabelFrame(self.tab_diag, text="3. Diagnóstico Forense & Resolução", padding=12)
        self.repair_card.pack(fill=tk.X, side=tk.BOTTOM)

        self.lbl_repair_diagnosis = ttk.Label(
            self.repair_card,
            text="Execute a análise profunda para inspecionar inconsistências e integridade da mídia.",
            font=("Segoe UI", 9)
        )
        self.lbl_repair_diagnosis.pack(side=tk.LEFT, fill=tk.X, expand=True)

        self.btn_repair = ttk.Button(
            self.repair_card,
            text="🛠️ Reparar Alinhamento Agora",
            style="Accent.TButton",
            state=tk.DISABLED,
            command=self._execute_repair
        )
        self.btn_repair.pack(side=tk.RIGHT, padx=6)

    # ── ABA 2: SAÚDE FÍSICA & SMART ───────────────────────────────────────────
    def _build_tab_health(self):
        self.tab_health = ttk.Frame(self.notebook, padding=16)
        self.notebook.add(self.tab_health, text="  📊 Saúde & SMART  ")

        ttk.Label(
            self.tab_health,
            text="Telemetria de Confiabilidade de Hardware",
            font=("Segoe UI", 12, "bold")
        ).pack(anchor=tk.W, pady=(0, 12))

        # Grid de Indicadores
        metrics_grid = ttk.Frame(self.tab_health)
        metrics_grid.pack(fill=tk.X, pady=(0, 16))

        # Helper para criar cards de métrica
        def make_metric(parent, label, default_val, col, row):
            frame = ttk.LabelFrame(parent, text=label, padding=12)
            frame.grid(column=col, row=row, padx=6, pady=6, sticky="nsew")
            lbl = ttk.Label(frame, text=default_val, font=("Segoe UI", 14, "bold"))
            lbl.pack(anchor=tk.CENTER)
            return lbl

        for c in range(3):
            metrics_grid.columnconfigure(c, weight=1)

        self.val_temp = make_metric(metrics_grid, "🌡️ Temperatura", "-- °C", 0, 0)
        self.val_health = make_metric(metrics_grid, "🛡️ Status de Saúde", "--", 1, 0)
        self.val_op = make_metric(metrics_grid, "⚡ Estado Operacional", "--", 2, 0)

        self.val_read_err = make_metric(metrics_grid, "⚠️ Erros de Leitura (Total)", "--", 0, 1)
        self.val_write_err = make_metric(metrics_grid, "✍️ Erros de Gravação", "--", 1, 1)
        self.val_wear = make_metric(metrics_grid, "📉 Nível de Desgaste", "--", 2, 1)

        # Caixa explicativa de segurança
        rec_box = ttk.LabelFrame(self.tab_health, text="Diretrizes de Segurança ('Do No Harm')", padding=12)
        rec_box.pack(fill=tk.BOTH, expand=True)

        tips = (
            "• Verde (Saudável): O disco responde sem setores pendentes. Reparos lógicos em partições são 100% seguros.\n\n"
            "• Amarelo (Atenção): Erros de leitura ou setores pendentes detectados. Crie uma Imagem Forense na Aba 3 antes de qualquer tentativa de reparo.\n\n"
            "• Vermelho (Falha Crítica/Mecânica): Não force varreduras de leitura contínua ou desfragmentações. Proceda imediatamente para a clonagem resiliente com tolerância a falhas."
        )
        ttk.Label(rec_box, text=tips, font=("Segoe UI", 9), justify=tk.LEFT, wraplength=700).pack(anchor=tk.W)

    # ── ABA 3: CLONAGEM & IMAGEM FORENSE ───────────────────────────────────────
    def _build_tab_clone(self):
        self.tab_clone = ttk.Frame(self.notebook, padding=16)
        self.notebook.add(self.tab_clone, text="  💾 Imagem & Clone  ")

        ttk.Label(
            self.tab_clone,
            text="Criação de Imagem Forense Byte-a-Byte (.img)",
            font=("Segoe UI", 12, "bold")
        ).pack(anchor=tk.W, pady=(0, 6))

        ttk.Label(
            self.tab_clone,
            text="Gera uma cópia binária espelho do disco físico. Permite recuperar dados sem desgastar a mídia original.",
            font=("Segoe UI", 9)
        ).pack(anchor=tk.W, pady=(0, 14))

        dest_frame = ttk.Frame(self.tab_clone)
        dest_frame.pack(fill=tk.X, pady=(0, 12))

        ttk.Label(dest_frame, text="Arquivo de Destino:", font=("Segoe UI", 9, "bold")).pack(anchor=tk.W, pady=(0, 4))

        input_row = ttk.Frame(dest_frame)
        input_row.pack(fill=tk.X)

        self.txt_dest_image = ttk.Entry(input_row)
        self.txt_dest_image.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(0, 8))

        btn_browse = ttk.Button(input_row, text="Procurar...", command=self._browse_image_dest)
        btn_browse.pack(side=tk.RIGHT)

        # Progresso de cópia
        progress_box = ttk.LabelFrame(self.tab_clone, text="Progresso da Clonagem", padding=12)
        progress_box.pack(fill=tk.X, pady=(0, 14))

        self.clone_progressbar = ttk.Progressbar(progress_box, mode="determinate")
        self.clone_progressbar.pack(fill=tk.X, pady=(0, 8))

        self.lbl_clone_stats = ttk.Label(progress_box, text="Aguardando início...", font=("Segoe UI", 9))
        self.lbl_clone_stats.pack(anchor=tk.W)

        # Botões de controle
        btn_row = ttk.Frame(self.tab_clone)
        btn_row.pack(anchor=tk.E)

        self.btn_cancel_clone = ttk.Button(btn_row, text="Cancelar", state=tk.DISABLED, command=self._cancel_clone)
        self.btn_cancel_clone.pack(side=tk.LEFT, padx=(0, 8))

        self.btn_start_clone = ttk.Button(btn_row, text="🚀 Iniciar Imagem Forense", style="Accent.TButton", command=self._start_clone)
        self.btn_start_clone.pack(side=tk.RIGHT)

    # ── ABA 4: LOGS & FORENSE ─────────────────────────────────────────────────
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

    # ── LÓGICA DE EVENTOS & THREADING ─────────────────────────────────────────
    def log(self, text: str):
        """Envia mensagem para a fila para ser exibida no log console com timestamp."""
        ts = time.strftime("%H:%M:%S")
        self.msg_queue.put(("log", f"[{ts}] {text}\n"))

    def set_status(self, text: str):
        self.msg_queue.put(("status", text))

    def _start_queue_poller(self):
        """Processa mensagens enviadas por threads de background na UI thread."""
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
                elif kind == "clone_progress":
                    cur, total, speed = val
                    pct = (cur / max(1, total)) * 100
                    self.clone_progressbar["value"] = pct
                    self.lbl_clone_stats.config(
                        text=f"Setor {cur:,} de {total:,} ({pct:.1f}%) — Velocidade: {speed:.1f} MB/s"
                    )
                elif kind == "clone_done":
                    ok, msg = val
                    self.btn_start_clone.config(state=tk.NORMAL)
                    self.btn_cancel_clone.config(state=tk.DISABLED)
                    if ok:
                        messagebox.showinfo("Sucesso", f"Imagem forense criada com sucesso!\n{msg}")
                    else:
                        messagebox.showwarning("Aviso", f"Operação interrompida ou com falha: {msg}")
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
        else:
            sv_ttk.set_theme("dark")
            self.current_theme = "dark"
            self.theme_btn.config(text="🌙 Escuro")
            self.txt_console.config(bg="#181818", fg="#e0e0e0", insertbackground="white")

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

        # Seleciona o primeiro se disponível
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

        # Atualiza painel lateral de resumo
        self.lbl_serial.config(text=f"Serial: {d.serial_number or 'N/A'}")
        self.lbl_media.config(text=f"Mídia: {d.media_type} ({d.bus_type})")
        self.lbl_health.config(text=f"Saúde: {d.health_status} ({d.operational_status})")
        self.lbl_raw.config(text=f"Acesso Raw: {'Permitido ✔' if d.can_open_raw else 'Negado ✖'}")

        # Atualiza aba 1
        self.lbl_target_info.config(
            text=f"Disco {d.number}: {d.friendly_name} ({d.size_gb:.1f} GB) — {d.device_path}",
            font=("Segoe UI", 10, "bold")
        )

        # Atualiza aba 2 (Saúde/SMART)
        temp_str = f"{d.temperature} °C" if d.temperature else "N/A"
        self.val_temp.config(text=temp_str)
        self.val_health.config(text=d.health_status)
        self.val_op.config(text=d.operational_status)
        self.val_read_err.config(text=str(d.read_errors if d.read_errors is not None else 0))
        self.val_write_err.config(text=str(d.write_errors if d.write_errors is not None else 0))
        self.val_wear.config(text=f"{d.wear}%" if d.wear is not None else "0%")

        # Reseta resultados anteriores da tela
        self.txt_gpt.delete("1.0", tk.END)
        self.txt_fs.delete("1.0", tk.END)
        self.lbl_repair_diagnosis.config(
            text="Pronto para análise. Clique no botão acima para iniciar a varredura profunda."
        )
        self.btn_repair.config(state=tk.DISABLED)
        self.misaligned_pair = None

    # ── ANÁLISE PROFUNDA ───────────────────────────────────────────────────────
    def _start_deep_scan(self):
        if not self.selected_disk:
            messagebox.showwarning("Aviso", "Selecione um disco na lista antes de iniciar.")
            return

        if not self.selected_disk.can_open_raw:
            messagebox.showerror(
                "Acesso Negado",
                "Não foi possível abrir o dispositivo em modo Raw.\n"
                "Execute o DiskRescue com privilégios de Administrador."
            )
            return

        if self.is_scanning:
            return

        self.is_scanning = True
        self.btn_run_scan.config(state=tk.DISABLED)
        self.progress_bar.pack(side=tk.RIGHT, padx=10)
        self.progress_bar.start(10)
        self.set_status(f"Analisando Disco {self.selected_disk.number} em profundidade...")
        self.log(f"Iniciando análise profunda em {self.selected_disk.device_path} ({self.selected_disk.friendly_name})...")

        target_disk = self.selected_disk

        def worker():
            gpt = None
            try:
                with open(target_disk.device_path, "rb") as f:
                    gpt = parse_gpt(f)
            except Exception as e:
                self.log(f"Erro ao ler cabeçalho GPT: {e}")

            # Varre primeiros 1.5M setores (~750 MB)
            self.log("Varrendo setores à procura de assinaturas de sistemas de arquivos reais...")
            fs_list = scan_boot_sectors(target_disk.device_path, max_lba=1_500_000)

            self.msg_queue.put(("scan_finished", (gpt, fs_list, target_disk)))

        threading.Thread(target=worker, daemon=True).start()

    def _render_scan_results(self, gpt, fs_list, disk: DiskInfo):
        self.is_scanning = False
        self.progress_bar.stop()
        self.progress_bar.pack_forget()
        self.btn_run_scan.config(state=tk.NORMAL)
        self.set_status(f"Análise do Disco {disk.number} concluída.")

        self.current_gpt = gpt
        self.current_filesystems = fs_list
        self.misaligned_pair = None

        # 1. Renderiza GPT
        self.txt_gpt.delete("1.0", tk.END)
        if gpt:
            self.txt_gpt.insert(tk.END, f"✔ Tabela GPT Oficial Válida — {len(gpt.partitions)} partição(ões) registrada(s):\n")
            for p in gpt.partitions:
                self.txt_gpt.insert(
                    tk.END,
                    f"  • Partição {p.index}: {p.name!r} | Início LBA: {p.first_lba:,} | Fim LBA: {p.last_lba:,} "
                    f"({p.size_gb():.2f} GB) | Flags: 0x{p.flags:016x}\n"
                )
            self.log(f"Tabela GPT lida: {len(gpt.partitions)} partição(ões) encontradas.")
        else:
            self.txt_gpt.insert(tk.END, "⚠ Tabela GPT ausente ou corrompida (pode ser MBR clássico ou RAW).\n")
            self.log("Nenhuma tabela GPT válida detectada no LBA 1.")

        # 2. Renderiza Filesystems
        self.txt_fs.delete("1.0", tk.END)
        if fs_list:
            self.txt_fs.insert(tk.END, f"✔ Encontrado(s) {len(fs_list)} sistema(s) de arquivos com assinatura real:\n")
            for idx, fs in enumerate(fs_list, 1):
                ver = " [MFT Íntegra Verificada ✔]" if fs.is_verified else ""
                self.txt_fs.insert(
                    tk.END,
                    f"  [{idx}] {fs.fs_type} | Setor Inicial LBA: {fs.start_lba:,} .. {fs.end_lba:,} "
                    f"({fs.size_gb:.2f} GB){ver}\n"
                )
            self.log(f"Varredura real localizou {len(fs_list)} boot sector(s) válido(s).")
        else:
            self.txt_fs.insert(tk.END, "Nenhum setor de boot com MFT íntegra encontrado nos primeiros 1.5M setores.\n")
            self.log("Nenhum sistema de arquivos encontrado nos primeiros 1.5M setores.")

        # 3. Cruzamento e Diagnóstico de Falha
        if gpt and fs_list:
            found_mismatch = False
            for p in gpt.partitions:
                for fs in fs_list:
                    if fs.fs_type == "NTFS" and fs.is_verified:
                        if p.first_lba != fs.start_lba and p.size_gb() > 10:
                            found_mismatch = True
                            self.misaligned_pair = (p.index, fs)
                            diff = abs(fs.start_lba - p.first_lba)

                            msg = (
                                f"🚨 FALHA CRÍTICA DETECTADA: Partição {p.index} Desalinhada!\n"
                                f"A tabela GPT aponta para o LBA {p.first_lba:,}, mas a partição NTFS real começa no LBA {fs.start_lba:,} (diferença de {diff:,} setores).\n"
                                f"Isto faz o Windows reportar o disco como RAW. Clique em Reparar para corrigir os ponteiros com backup automático."
                            )
                            self.lbl_repair_diagnosis.config(text=msg)
                            self.btn_repair.config(state=tk.NORMAL)
                            self.log(f"ALERTA: Desalinhamento identificado na partição {p.index}. Pronto para reparo.")
                            break
                if found_mismatch:
                    break

            if not found_mismatch:
                self.lbl_repair_diagnosis.config(
                    text="✔ Geometria Consistente: As partições registradas coincidem com os dados físicos encontrados na mídia."
                )
                self.btn_repair.config(state=tk.DISABLED)
                self.log("Geometria de partições e sistemas de arquivos consistente.")
        else:
            self.lbl_repair_diagnosis.config(
                text="Análise concluída. Sem desalinhamentos automáticos identificados."
            )
            self.btn_repair.config(state=tk.DISABLED)

    # ── EXECUÇÃO DO REPARO ────────────────────────────────────────────────────
    def _execute_repair(self):
        if not self.selected_disk or not self.misaligned_pair:
            return

        part_idx, fs = self.misaligned_pair
        confirm = messagebox.askyesno(
            "Confirmar Reparo Cirúrgico",
            f"O DiskRescue irá realinhar a Partição {part_idx} para o LBA {fs.start_lba:,}.\n\n"
            f"• Um backup de segurança de todos os setores originais será salvo antes da gravação.\n"
            f"• Nenhum dado de arquivos será deletado.\n\n"
            "Deseja prosseguir com o reparo agora?"
        )
        if not confirm:
            return

        self.set_status("Aplicando reparo cirúrgico na tabela de partições...")
        self.log(f"Iniciando procedimento de reparo na partição {part_idx}...")

        target_disk = self.selected_disk

        def worker():
            res = repair_gpt_partition_offset(target_disk.device_path, part_idx, fs)
            if res.success:
                self.log(f"SUCESSO: {res.message}")
                self.log(f"Backup gravado em: {res.backup_file}")
                self.set_status("Reparo concluído com sucesso!")
                messagebox.showinfo(
                    "Sucesso Absoluto!",
                    f"A tabela GPT foi corrigida com sucesso!\n\n"
                    f"{res.message}\n\n"
                    f"Backup dos setores originais: {res.backup_file}\n\n"
                    "O Windows já deve ter reconhecido o volume automaticamente."
                )
            else:
                self.log(f"FALHA NO REPARO: {res.message}")
                self.set_status("Falha no reparo.")
                messagebox.showerror("Erro de Reparo", f"Não foi possível concluir o reparo:\n{res.message}")

        threading.Thread(target=worker, daemon=True).start()

    # ── ABA CLONE (IMAGEM FORENSE) ─────────────────────────────────────────────
    def _browse_image_dest(self):
        dest = filedialog.asksaveasfilename(
            title="Salvar Imagem Forense",
            defaultextension=".img",
            filetypes=[("Arquivos de Imagem Raw", "*.img"), ("Todos os Arquivos", "*.*")]
        )
        if dest:
            self.txt_dest_image.delete(0, tk.END)
            self.txt_dest_image.insert(0, dest)

    def _start_clone(self):
        if not self.selected_disk:
            messagebox.showwarning("Aviso", "Selecione um disco de origem à esquerda.")
            return

        dest = self.txt_dest_image.get().strip()
        if not dest:
            messagebox.showwarning("Aviso", "Escolha o arquivo de destino para salvar a imagem (.img).")
            return

        target_disk = self.selected_disk
        self.cancel_image_flag = False
        self.btn_start_clone.config(state=tk.DISABLED)
        self.btn_cancel_clone.config(state=tk.NORMAL)
        self.clone_progressbar["value"] = 0
        self.lbl_clone_stats.config(text="Iniciando cópia binária setor a setor...")
        self.log(f"Criando imagem forense de {target_disk.device_path} para {dest}...")

        # Calcula setores totais (512 bytes)
        total_sectors = target_disk.size_bytes // 512

        def progress_cb(current, total, speed):
            self.msg_queue.put(("clone_progress", (current, total, speed)))

        def worker():
            ok = create_raw_disk_image(
                target_disk.device_path,
                Path(dest),
                max_sectors=total_sectors,
                progress_cb=progress_cb,
                cancel_check=lambda: self.cancel_image_flag
            )
            self.msg_queue.put(("clone_done", (ok, dest)))

        threading.Thread(target=worker, daemon=True).start()

    def _cancel_clone(self):
        self.cancel_image_flag = True
        self.lbl_clone_stats.config(text="Cancelando transferência...")
        self.log("Cancelamento solicitado pelo usuário.")

    def _save_log_file(self):
        path = filedialog.asksaveasfilename(
            title="Salvar Log Forense",
            defaultextension=".txt",
            filetypes=[("Arquivo de Texto", "*.txt"), ("Markdown", "*.md")]
        )
        if path:
            try:
                content = self.txt_console.get("1.0", tk.END)
                Path(path).write_text(content, encoding="utf-8")
                messagebox.showinfo("Sucesso", f"Relatório salvo em:\n{path}")
            except Exception as e:
                messagebox.showerror("Erro", f"Falha ao salvar relatório: {e}")


def run_gui():
    root = tk.Tk()
    app = DiskRescueApp(root)
    root.mainloop()


if __name__ == "__main__":
    run_gui()
