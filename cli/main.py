"""
cli/main.py — Interface de Linha de Comando (CLI) interativa do DiskRescue.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

# Adiciona a raiz do projeto ao sys.path
AQUI = Path(__file__).resolve().parents[1]
if str(AQUI) not in sys.path:
    sys.path.insert(0, str(AQUI))

from core.filesystem.scanner import scan_boot_sectors
from core.hardware.scanner import DiskInfo, scan_disks
from core.partition.gpt import parse_gpt
from core.recovery.repair import repair_gpt_partition_offset

try:
    from rich import print as rprint
    from rich.console import Console
    from rich.panel import Panel
    from rich.prompt import Confirm, Prompt
    from rich.table import Table
    console = Console()
except ImportError:
    console = None
    rprint = print


def banner():
    txt = """
    ╔═══════════════════════════════════════════════════════════════╗
    ║                🛠️  D I S K R E S C U E  v0.1.0                ║
    ║        Sistema Autônomo de Diagnóstico e Recuperação          ║
    ╚═══════════════════════════════════════════════════════════════╝
    """
    if console:
        console.print(f"[bold cyan]{txt}[/bold cyan]")
    else:
        print(txt)


def listar_discos() -> list[DiskInfo]:
    disks = scan_disks()
    if not disks:
        rprint("[bold red]Nenhum disco físico encontrado ou privilégios insuficientes.[/bold red]")
        rprint("[yellow]Dica: Execute o PowerShell ou terminal como Administrador/root.[/yellow]")
        return []

    if console:
        t = Table(title="Discos Físicos Conectados", show_header=True, header_style="bold magenta")
        t.add_column("Nº", style="bold yellow", width=4)
        t.add_column("Dispositivo", style="cyan")
        t.add_column("Modelo / Nome", style="white")
        t.add_column("Tamanho", justify="right", style="green")
        t.add_column("Barramento", style="blue")
        t.add_column("Tabela", style="magenta")
        t.add_column("Acesso Raw", style="bold")

        for d in disks:
            raw_badge = "[green]OK[/green]" if d.can_open_raw else "[red]Negado[/red]"
            t.add_row(
                str(d.number),
                d.device_path,
                d.friendly_name,
                f"{d.size_gb:.1f} GB",
                d.bus_type,
                d.partition_style,
                raw_badge
            )
        console.print(t)
    else:
        for d in disks:
            print(f"[{d.number}] {d.device_path} - {d.friendly_name} ({d.size_gb:.1f} GB) [{d.bus_type}]")

    return disks


def analisar_disco(d: DiskInfo):
    rprint(f"\n[bold cyan]Iniciando análise profunda no Disco {d.number} ({d.friendly_name})...[/bold cyan]")
    
    if not d.can_open_raw:
        rprint("[bold red]Erro: Sem permissão de leitura raw no dispositivo. Execute como Administrador.[/bold red]")
        return

    # 1. Analisa GPT
    gpt = None
    try:
        with open(d.device_path, "rb") as f:
            gpt = parse_gpt(f)
    except Exception as exc:
        rprint(f"[red]Erro ao ler cabeçalho do disco: {exc}[/red]")
        return

    if gpt:
        rprint(f"[green]✔ Tabela GPT detectada:[/green] {len(gpt.partitions)} partição(ões) registrada(s).")
        for p in gpt.partitions:
            rprint(f"  • Partição {p.index}: {p.name!r} | LBA {p.first_lba:,} .. {p.last_lba:,} ({p.size_gb():.2f} GB) | Flags: 0x{p.flags:016x}")
    else:
        rprint("[yellow]⚠ Tabela GPT não detectada ou ausente (pode ser MBR ou RAW puro).[/yellow]")

    # 2. Varredura de Boot Sectors Reais
    rprint("\n[cyan]Varrendo primeiros setores em busca de assinaturas reais de sistemas de arquivos...[/cyan]")
    discovered = scan_boot_sectors(d.device_path, max_lba=1_500_000)
    
    if not discovered:
        rprint("[yellow]Nenhum setor de boot com MFT válida foi encontrado nos primeiros 1.5M setores.[/yellow]")
        return

    rprint(f"[bold green]✔ Encontrado(s) {len(discovered)} sistema(s) de arquivos real(is) na mídia:[/bold green]")
    for idx, fs in enumerate(discovered, 1):
        mft_info = " (MFT íntegra ✔)" if fs.is_verified else ""
        rprint(f"  [{idx}] [bold]{fs.fs_type}[/bold] no LBA [bold green]{fs.start_lba:,}[/bold green] até [bold green]{fs.end_lba:,}[/bold green] ({fs.size_gb:.2f} GB){mft_info}")

    # 3. Cruzamento e Diagnóstico de Inconsistência
    if gpt and discovered:
        # Checa se alguma partição GPT está deslocada em relação ao filesystem real
        for p in gpt.partitions:
            for fs in discovered:
                if fs.fs_type == "NTFS" and fs.is_verified:
                    if p.first_lba != fs.start_lba and p.size_gb() > 10:
                        rprint("\n" + "="*70)
                        rprint("[bold red]🚨 DIAGNÓSTICO DE FALHA ENCONTRADO:[/bold red]")
                        rprint(f"A Partição {p.index} da tabela GPT está gravada para começar no LBA [red]{p.first_lba:,}[/red].")
                        rprint(f"Porém, a partição NTFS real e íntegra começa no LBA [green]{fs.start_lba:,}[/green]!")
                        rprint("Esse desalinhamento faz o Windows enxergar o disco como vazio/RAW e recusar a montagem.")
                        rprint("="*70 + "\n")

                        confirm = False
                        if console:
                            confirm = Confirm.ask(
                                f"[bold yellow]Deseja que o DiskRescue repare a tabela GPT agora para o LBA {fs.start_lba:,} (com backup de segurança)?[/bold yellow]"
                            )
                        else:
                            resp = input(f"Reparar partição para LBA {fs.start_lba}? (s/n): ")
                            confirm = (resp.strip().lower() == 's')

                        if confirm:
                            rprint("[cyan]Criando snapshot de segurança e aplicando correção...[/cyan]")
                            res = repair_gpt_partition_offset(d.device_path, p.index, fs)
                            if res.success:
                                rprint(f"\n[bold green]✔ SUCESSO ABSOLUTO![/bold green] {res.message}")
                                rprint(f"[green]Backup dos setores originais salvo em:[/green] {res.backup_file}")
                                rprint("[bold white]O Windows já deve ter reconhecido o volume. Verifique no 'Meu Computador'![/bold white]")
                            else:
                                rprint(f"\n[bold red]✖ Falha no reparo:[/bold red] {res.message}")
                        return


def main():
    banner()
    discos = listar_discos()
    if not discos:
        return

    escolha = Prompt.ask("\nDigite o número do disco que deseja analisar (ou 'q' para sair)") if console else input("\nDigite o número do disco: ")
    if escolha.lower() == 'q':
        return

    try:
        num = int(escolha)
        alvo = next((d for d in discos if d.number == num), None)
        if not alvo:
            rprint("[red]Número de disco inválido.[/red]")
            return
        analisar_disco(alvo)
    except ValueError:
        rprint("[red]Entrada inválida.[/red]")


if __name__ == "__main__":
    main()
