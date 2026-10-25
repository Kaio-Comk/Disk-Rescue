"""
core/recovery/repair.py — Motores de reparo não destrutivo e restauração in-place de partições.
"""
from __future__ import annotations

import logging
import os
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

from core.filesystem.scanner import DiscoveredFilesystem
from core.partition.gpt import (
    DEFAULT_SECTOR_SIZE,
    GPTPartitionEntry,
    GPTTable,
    create_lba_snapshot,
    parse_gpt,
    rebuild_gpt,
)

log = logging.getLogger("diskrescue.repair")


@dataclass
class RepairResult:
    success: bool
    message: str
    backup_file: Path | None = None
    old_start_lba: int = 0
    new_start_lba: int = 0
    new_end_lba: int = 0


def repair_gpt_partition_offset(
    device_path: str,
    target_partition_index: int,
    discovered_fs: DiscoveredFilesystem,
    backup_dir: Path | None = None,
    sector_size: int = DEFAULT_SECTOR_SIZE
) -> RepairResult:
    """Corrige o offset de início e fim de uma partição na tabela GPT com backup prévio de segurança."""
    backup_dir = backup_dir or Path("backups")
    backup_dir.mkdir(parents=True, exist_ok=True)

    # Proteção P0: Não gravar backup de segurança no próprio disco de origem
    try:
        from core.hardware.device_resolver import assert_safe_source_and_destination
        assert_safe_source_and_destination(device_path, backup_dir, is_directory=True)
    except Exception as exc:
        log.error("Bloqueio de segurança no diretório de backup: %s", exc)
        return RepairResult(success=False, message=f"Diretório de backup inseguro: {exc}")

    timestamp = time.strftime("%Y%m%d_%H%M%S")
    clean_name = device_path.replace("\\", "_").replace("/", "_").replace(":", "").replace(".", "").strip("_")[-30:]
    backup_path = backup_dir / f"backup_gpt_{clean_name}_{timestamp}.bin"

    # 1. Snapshot de segurança (LBA 0 a 33)
    if not create_lba_snapshot(device_path, backup_path, sector_count=34, sector_size=sector_size):
        return RepairResult(success=False, message="Falha ao criar snapshot de segurança antes do reparo.")

    try:
        # 2. Ler tabela GPT atual
        with open(device_path, "r+b") as f:
            gpt = parse_gpt(f, sector_size=sector_size)
            if not gpt:
                return RepairResult(success=False, message="Não foi possível ler a tabela GPT atual.", backup_file=backup_path)

            # Localiza a partição alvo
            part_to_update = None
            for p in gpt.partitions:
                if p.index == target_partition_index:
                    part_to_update = p
                    break

            if not part_to_update:
                return RepairResult(
                    success=False,
                    message=f"Partição de índice {target_partition_index} não encontrada na tabela GPT.",
                    backup_file=backup_path
                )

            old_start = part_to_update.first_lba
            new_start = discovered_fs.start_lba
            new_end = discovered_fs.end_lba

            # Valida limites utilizáveis da GPT
            if new_start < gpt.first_usable_lba or new_end > gpt.last_usable_lba or new_start > new_end:
                return RepairResult(
                    success=False,
                    message=f"Limites inválidos: LBA {new_start}..{new_end} ultrapassa área utilizável ({gpt.first_usable_lba}..{gpt.last_usable_lba}).",
                    backup_file=backup_path
                )

            # Valida sobreposição com outras partições ativas
            for p in gpt.partitions:
                if p.index != target_partition_index and not p.is_empty:
                    if max(new_start, p.first_lba) <= min(new_end, p.last_lba):
                        return RepairResult(
                            success=False,
                            message=f"Conflito de partição: o novo intervalo LBA {new_start}..{new_end} sobrepõe a partição {p.index} (LBA {p.first_lba}..{p.last_lba}).",
                            backup_file=backup_path
                        )

            log.info("Ajustando partição %d: LBA %d -> LBA %d..%d", target_partition_index, old_start, new_start, new_end)

            # Atualiza o objeto da partição preservando flags existentes (remove apenas flag de ocultação se presente)
            part_to_update.first_lba = new_start
            part_to_update.last_lba = new_end
            part_to_update.flags = part_to_update.flags & ~0x8000000000000000

            # 3. Recalcular cabeçalho primário, entradas e cabeçalho de backup (LBA final)
            new_header_bytes, new_entries_bytes = rebuild_gpt(gpt, gpt.partitions, sector_size=sector_size)
            from core.partition.gpt import rebuild_backup_gpt
            bkp_header_bytes, bkp_entries_bytes, bkp_entries_lba = rebuild_backup_gpt(gpt, gpt.partitions, sector_size=sector_size)

            # 4. Gravação nos LBAs primários (LBA 1 e LBA 2..33)
            f.seek(1 * sector_size)
            f.write(new_header_bytes)

            f.seek(gpt.partition_entries_lba * sector_size)
            f.write(new_entries_bytes)

            # 5. Gravação da cópia de backup no fim do disco
            if gpt.backup_lba > 0 and bkp_entries_lba > 0:
                f.seek(bkp_entries_lba * sector_size)
                f.write(bkp_entries_bytes)
                f.seek(gpt.backup_lba * sector_size)
                f.write(bkp_header_bytes)

            f.flush()
            try:
                os.fsync(f.fileno())
            except OSError:
                pass

            # Verificação pós-escrita imediata
            f.seek(1 * sector_size)
            read_hdr = f.read(len(new_header_bytes))
            if read_hdr != new_header_bytes:
                raise OSError("Verificação pós-escrita falhou no cabeçalho GPT primário.")

        log.info("Tabela GPT primária corrigida e gravada com sucesso.")

        # 5. Notificar o sistema operacional para atualizar o cache de disco
        if os.name == "nt":
            subprocess.run(["powershell", "-NoProfile", "-Command", "Update-HostStorageCache"], capture_output=True, timeout=10)

        return RepairResult(
            success=True,
            message=f"Partição {target_partition_index} realinhada com sucesso para LBA {new_start:,}..{new_end:,} ({discovered_fs.size_gb:.1f} GB).",
            backup_file=backup_path,
            old_start_lba=old_start,
            new_start_lba=new_start,
            new_end_lba=new_end,
        )

    except PermissionError:
        return RepairResult(
            success=False,
            message="Permissão negada ao gravar no disco. Execute o terminal como Administrador/root.",
            backup_file=backup_path
        )
    except Exception as exc:
        log.error("Exceção ao reparar tabela GPT: %s", exc)
        return RepairResult(success=False, message=f"Erro durante a gravação: {exc}", backup_file=backup_path)


def create_raw_disk_image(
    device_path: str,
    output_path: Path,
    sector_size: int = DEFAULT_SECTOR_SIZE,
    max_sectors: int | None = None,
    chunk_sectors: int = 2048,
    progress_cb=None,
    cancel_check=None
) -> bool:
    """Cria uma imagem binária setor a setor da mídia física para análise forense segura."""
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    chunk_bytes = chunk_sectors * sector_size
    current_sector = 0
    t_start = time.time()
    last_cb_time = t_start
    bytes_transferred = 0

    try:
        with open(device_path, "rb") as src, open(output_path, "wb") as dst:
            while True:
                if cancel_check and cancel_check():
                    log.info("Cópia de imagem cancelada pelo usuário.")
                    return False

                if max_sectors and current_sector >= max_sectors:
                    break

                to_read = chunk_bytes
                if max_sectors:
                    remaining_sectors = max_sectors - current_sector
                    if remaining_sectors < chunk_sectors:
                        to_read = remaining_sectors * sector_size

                buf = src.read(to_read)
                if not buf:
                    break

                dst.write(buf)
                sectors_read = len(buf) // sector_size
                current_sector += sectors_read
                bytes_transferred += len(buf)

                now = time.time()
                if progress_cb and (now - last_cb_time >= 0.25):
                    speed = (bytes_transferred / (1024 * 1024)) / max(0.1, now - t_start)
                    progress_cb(current_sector, max_sectors or current_sector, speed)
                    last_cb_time = now

        if progress_cb:
            now = time.time()
            speed = (bytes_transferred / (1024 * 1024)) / max(0.1, now - t_start)
            progress_cb(current_sector, current_sector, speed)
        return True
    except Exception as exc:
        log.error("Erro ao criar imagem forense: %s", exc)
        return False

