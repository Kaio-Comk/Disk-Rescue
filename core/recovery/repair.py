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
    timestamp = time.strftime("%Y%m%d_%H%M%S")
    clean_name = device_path.replace("\\", "_").replace(".", "").replace("/", "_").strip("_")
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

            log.info("Ajustando partição %d: LBA %d -> LBA %d..%d", target_partition_index, old_start, new_start, new_end)

            # Atualiza o objeto da partição
            part_to_update.first_lba = new_start
            part_to_update.last_lba = new_end
            part_to_update.flags = 0x0000000000000000 # Habilita montagem e letra automática

            # 3. Recalcular e reconstruir cabeçalho e tabela com novos CRCs
            new_header_bytes, new_entries_bytes = rebuild_gpt(gpt, gpt.partitions, sector_size=sector_size)

            # 4. Gravação segura nos LBAs primários (LBA 1 e LBA 2..33)
            f.seek(1 * sector_size)
            f.write(new_header_bytes)

            f.seek(gpt.partition_entries_lba * sector_size)
            f.write(new_entries_bytes)
            f.flush()

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
