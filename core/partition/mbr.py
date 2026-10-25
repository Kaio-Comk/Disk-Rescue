"""
core/partition/mbr.py — Parser, validação e recuperação de tabelas de partição MBR clássicas (LBA 0).
"""
from __future__ import annotations

import logging
import os
import struct
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO, List, Optional

log = logging.getLogger("diskrescue.mbr")

MBR_SIGNATURE = b"\x55\xaa"
PARTITION_ENTRY_SIZE = 16
PARTITION_TABLE_OFFSET = 446


@dataclass
class MBRPartitionEntry:
    index: int
    is_active: bool
    type_id: int
    type_name: str
    start_lba: int
    sector_count: int
    sector_size: int = 512

    @property
    def end_lba(self) -> int:
        return self.start_lba + self.sector_count - 1 if self.sector_count > 0 else self.start_lba

    @property
    def size_gb(self) -> float:
        return (self.sector_count * self.sector_size) / (1024 ** 3)

    @property
    def is_empty(self) -> bool:
        return self.type_id == 0 or self.sector_count == 0

    @property
    def size_sectors(self) -> int:
        return self.sector_count

    @property
    def type_code(self) -> int:
        return self.type_id

    @property
    def is_bootable(self) -> bool:
        return self.is_active


@dataclass
class MBRTable:
    has_valid_signature: bool
    is_protective_gpt: bool
    bootstrap_code: bytes
    partitions: List[MBRPartitionEntry]
    raw_data: bytes


MBR_TYPES = {
    0x00: "Vazia",
    0x01: "FAT12",
    0x04: "FAT16 (<32MB)",
    0x05: "Extended",
    0x06: "FAT16 (>32MB)",
    0x07: "NTFS / exFAT",
    0x0B: "FAT32 (CHS)",
    0x0C: "FAT32 (LBA)",
    0x0E: "FAT16 (LBA)",
    0x0F: "Extended (LBA)",
    0x82: "Linux Swap",
    0x83: "Linux Native (ext2/3/4)",
    0xEE: "GPT Protective MBR",
    0xEF: "EFI System Partition",
}


def parse_mbr(source: bytes | BinaryIO) -> MBRTable | None:
    """Decodifica o setor LBA 0 como Master Boot Record."""
    if hasattr(source, "read"):
        data = source.read(512)
    else:
        data = source[:512]

    if len(data) < 512:
        return None

    has_sig = data[510:512] == MBR_SIGNATURE
    bootstrap = data[:PARTITION_TABLE_OFFSET]

    partitions = []
    is_protective = False

    for idx in range(4):
        offset = PARTITION_TABLE_OFFSET + idx * PARTITION_ENTRY_SIZE
        entry_raw = data[offset : offset + PARTITION_ENTRY_SIZE]

        boot_indicator = entry_raw[0]
        type_id = entry_raw[4]
        start_lba, sec_count = struct.unpack("<II", entry_raw[8:16])

        if type_id == 0xEE:
            is_protective = True

        type_desc = MBR_TYPES.get(type_id, f"Desconhecido (0x{type_id:02X})")
        partitions.append(
            MBRPartitionEntry(
                index=idx + 1,
                is_active=(boot_indicator == 0x80),
                type_id=type_id,
                type_name=type_desc,
                start_lba=start_lba,
                sector_count=sec_count
            )
        )

    return MBRTable(
        has_valid_signature=has_sig,
        is_protective_gpt=is_protective,
        bootstrap_code=bootstrap,
        partitions=partitions,
        raw_data=data
    )


def rebuild_mbr_bytes(
    partitions: List[MBRPartitionEntry],
    bootstrap_code: bytes | None = None
) -> bytes:
    """Reconstrói os 512 bytes de um setor MBR com as partições atualizadas e assinatura 0x55AA."""
    mbr = bytearray(512)
    if bootstrap_code and len(bootstrap_code) <= PARTITION_TABLE_OFFSET:
        mbr[: len(bootstrap_code)] = bootstrap_code

    for p in partitions:
        if 1 <= p.index <= 4:
            offset = PARTITION_TABLE_OFFSET + (p.index - 1) * PARTITION_ENTRY_SIZE
            mbr[offset] = 0x80 if p.is_active else 0x00
            # CHS dummy (LBA mode default)
            mbr[offset + 1 : offset + 4] = b"\xfe\xff\xff"
            mbr[offset + 4] = p.type_id
            mbr[offset + 5 : offset + 8] = b"\xfe\xff\xff"
            struct.pack_into("<II", mbr, offset + 8, p.start_lba, p.sector_count)

    mbr[510:512] = MBR_SIGNATURE
    return bytes(mbr)


def repair_mbr_partition(
    device_path: str,
    target_index: int,
    new_start_lba: int,
    new_sector_count: int,
    new_type_id: int = 0x07,
    backup_dir: Path | None = None
) -> bool:
    """Atualiza de forma cirúrgica uma entrada MBR corrompida com backup prévio de LBA 0."""
    if not (1 <= target_index <= 4):
        log.error("Índice de partição MBR inválido: %d (deve ser entre 1 e 4).", target_index)
        return False

    if new_start_lba < 1 or new_sector_count <= 0 or (new_start_lba + new_sector_count) > 0xFFFFFFFF:
        log.error("Limites de partição MBR inválidos: LBA %d, contagem %d.", new_start_lba, new_sector_count)
        return False

    backup_dir = backup_dir or Path("backups")

    # Proteção P0: Validar segurança antes de criar qualquer diretório ou arquivo
    try:
        from core.hardware.device_resolver import assert_safe_source_and_destination
        assert_safe_source_and_destination(device_path, backup_dir, is_directory=True)
    except Exception as exc:
        log.error("Bloqueio de segurança no diretório de backup: %s", exc)
        return False

    backup_dir.mkdir(parents=True, exist_ok=True)

    clean_name = device_path.replace("\\", "_").replace("/", "_").replace(":", "").replace(".", "").strip("_")
    timestamp = time.strftime("%Y%m%d_%H%M%S")
    backup_path = backup_dir / f"mbr_backup_{clean_name[-30:]}_{timestamp}.bin"

    try:
        with open(device_path, "r+b") as f:
            f.seek(0)
            cur_mbr_bytes = f.read(512)
            if len(cur_mbr_bytes) != 512:
                return False

            table = parse_mbr(cur_mbr_bytes)
            if not table:
                return False

            # Valida sobreposição com outras partições ativas
            new_end_lba = new_start_lba + new_sector_count - 1
            for p in table.partitions:
                if p.index != target_index and p.type_id != 0x00 and p.sector_count > 0:
                    other_start = p.start_lba
                    other_end = p.start_lba + p.sector_count - 1
                    if max(new_start_lba, other_start) <= min(new_end_lba, other_end):
                        log.error(
                            "Conflito de geometria MBR: a partição %d (LBA %d..%d) sobrepõe a partição %d (LBA %d..%d).",
                            target_index, new_start_lba, new_end_lba, p.index, other_start, other_end
                        )
                        return False

            # Atualiza partição alvo
            found = False
            for p in table.partitions:
                if p.index == target_index:
                    found = True
                    p.start_lba = new_start_lba
                    p.sector_count = new_sector_count
                    p.type_id = new_type_id
                    p.type_name = MBR_TYPES.get(new_type_id, "Partição Restaurada")
                    break

            if not found:
                log.error("Partição com índice %d não encontrada na tabela MBR.", target_index)
                return False

            # Salva snapshot de segurança somente após validar parâmetros e geometria
            backup_path.write_bytes(cur_mbr_bytes)

            new_mbr_bytes = rebuild_mbr_bytes(table.partitions, table.bootstrap_code)

            f.seek(0)
            f.write(new_mbr_bytes)
            f.flush()
            try:
                os.fsync(f.fileno())
            except OSError as err:
                log.error("Falha ao sincronizar gravação de LBA 0 (fsync): %s", err)
                raise OSError(f"Falha de sincronização física: {err}") from err

            # Verificação pós-escrita imediata
            f.seek(0)
            written = f.read(512)
            if written != new_mbr_bytes:
                raise OSError("Verificação pós-escrita falhou: LBA 0 diverge dos dados gravados.")

        log.info("Entrada MBR %d restaurada com sucesso.", target_index)

        if os.name == "nt":
            subprocess.run(["powershell", "-NoProfile", "-Command", "Update-HostStorageCache"], capture_output=True, timeout=10)
        return True
    except Exception as exc:
        log.error("Erro ao reparar MBR: %s", exc)
        return False
