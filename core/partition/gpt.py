"""
core/partition/gpt.py — Parser, validador e reparador de tabelas GPT (GUID Partition Table).
"""
from __future__ import annotations

import logging
import struct
import zlib
from dataclasses import dataclass, field
from pathlib import Path

log = logging.getLogger("diskrescue.gpt")

GPT_HEADER_SIGNATURE = b"EFI PART"
GPT_HEADER_SIZE = 92
DEFAULT_SECTOR_SIZE = 512


@dataclass
class GPTPartitionEntry:
    index: int
    type_guid: bytes
    unique_guid: bytes
    first_lba: int
    last_lba: int
    flags: int
    name: str

    @property
    def total_sectors(self) -> int:
        if self.last_lba < self.first_lba:
            return 0
        return self.last_lba - self.first_lba + 1

    def size_gb(self, sector_size: int = DEFAULT_SECTOR_SIZE) -> float:
        return (self.total_sectors * sector_size) / (1024 ** 3)

    @property
    def is_empty(self) -> bool:
        return self.type_guid == b"\x00" * 16

    @property
    def no_drive_letter(self) -> bool:
        return bool(self.flags & 0x8000000000000000)

    def to_bytes(self, entry_size: int = 128) -> bytes:
        name_bytes = self.name.encode("utf-16le")[:72]
        name_padded = name_bytes.ljust(72, b"\x00")
        raw = bytearray(entry_size)
        raw[0:16] = self.type_guid
        raw[16:32] = self.unique_guid
        struct.pack_into("<QQQ", raw, 32, self.first_lba, self.last_lba, self.flags)
        raw[56:128] = name_padded
        return bytes(raw)


@dataclass
class GPTTable:
    revision: int
    header_size: int
    header_crc: int
    current_lba: int
    backup_lba: int
    first_usable_lba: int
    last_usable_lba: int
    disk_guid: bytes
    partition_entries_lba: int
    num_partition_entries: int
    partition_entry_size: int
    partition_entries_crc: int
    partitions: list[GPTPartitionEntry] = field(default_factory=list)
    raw_header: bytes = b""
    raw_entries: bytes = b""
    is_header_crc_valid: bool = False
    is_entries_crc_valid: bool = False


def parse_gpt(f, sector_size: int = DEFAULT_SECTOR_SIZE) -> GPTTable | None:
    """Lê e valida o cabeçalho e tabela GPT a partir de um descritor de arquivo aberto."""
    f.seek(1 * sector_size)
    header_data = f.read(sector_size)
    if len(header_data) < sector_size or header_data[:8] != GPT_HEADER_SIGNATURE:
        log.debug("Assinatura EFI PART não encontrada no LBA 1.")
        return None

    (
        rev, h_size, h_crc, reserved,
        cur_lba, backup_lba, first_usable, last_usable,
        disk_guid, part_lba, num_entries, entry_size, part_crc
    ) = struct.unpack("<IIIIQQQQ16sQIII", header_data[8:92])

    # Validação estrita dos limites de entrada para evitar estouro de memória em discos corrompidos
    if not (92 <= h_size <= sector_size):
        log.debug("Cabeçalho GPT inválido: header_size=%d fora do limite (92..%d).", h_size, sector_size)
        return None

    if not (128 <= entry_size <= 4096) or (entry_size % 8 != 0):
        log.debug("Cabeçalho GPT inválido: entry_size=%d fora do limite aceitável.", entry_size)
        return None

    if not (1 <= num_entries <= 1024):
        log.debug("Cabeçalho GPT inválido: num_entries=%d fora do limite (1..1024).", num_entries)
        return None

    if first_usable >= last_usable or part_lba < 2:
        log.debug("Cabeçalho GPT inválido: first_usable=%d >= last_usable=%d ou part_lba=%d.", first_usable, last_usable, part_lba)
        return None

    # Validação do CRC do cabeçalho (com o campo de CRC zerado durante o cálculo)
    test_hdr = bytearray(header_data[:h_size])
    test_hdr[16:20] = b"\x00\x00\x00\x00"
    calc_h_crc = zlib.crc32(test_hdr) & 0xFFFFFFFF
    header_crc_valid = (calc_h_crc == h_crc)

    # Leitura dos registros de partição
    f.seek(part_lba * sector_size)
    entries_len = num_entries * entry_size
    entries_data = f.read(entries_len)
    calc_part_crc = zlib.crc32(entries_data) & 0xFFFFFFFF
    entries_crc_valid = (calc_part_crc == part_crc)

    partitions: list[GPTPartitionEntry] = []
    for i in range(num_entries):
        entry_raw = entries_data[i * entry_size : (i + 1) * entry_size]
        t_guid = entry_raw[:16]
        if t_guid == b"\x00" * 16:
            continue
        u_guid = entry_raw[16:32]
        f_lba, l_lba, flg = struct.unpack("<QQQ", entry_raw[32:56])
        name = entry_raw[56:128].decode("utf-16le", errors="ignore").rstrip("\x00")
        partitions.append(GPTPartitionEntry(
            index=i + 1,
            type_guid=t_guid,
            unique_guid=u_guid,
            first_lba=f_lba,
            last_lba=l_lba,
            flags=flg,
            name=name
        ))

    return GPTTable(
        revision=rev,
        header_size=h_size,
        header_crc=h_crc,
        current_lba=cur_lba,
        backup_lba=backup_lba,
        first_usable_lba=first_usable,
        last_usable_lba=last_usable,
        disk_guid=disk_guid,
        partition_entries_lba=part_lba,
        num_partition_entries=num_entries,
        partition_entry_size=entry_size,
        partition_entries_crc=part_crc,
        partitions=partitions,
        raw_header=header_data,
        raw_entries=entries_data,
        is_header_crc_valid=header_crc_valid,
        is_entries_crc_valid=entries_crc_valid,
    )


def rebuild_gpt(
    gpt: GPTTable,
    updated_partitions: list[GPTPartitionEntry],
    sector_size: int = DEFAULT_SECTOR_SIZE
) -> tuple[bytes, bytes]:
    """Reconstrói os bytes da tabela de partições e do cabeçalho LBA 1 com novos CRCs calculados."""
    entries_data = bytearray(gpt.num_partition_entries * gpt.partition_entry_size)
    for p in updated_partitions:
        idx = p.index - 1
        if 0 <= idx < gpt.num_partition_entries:
            start = idx * gpt.partition_entry_size
            entries_data[start : start + gpt.partition_entry_size] = p.to_bytes(gpt.partition_entry_size)

    # Novo CRC das partições
    new_part_crc = zlib.crc32(entries_data) & 0xFFFFFFFF

    # Atualiza cabeçalho
    new_header = bytearray(gpt.raw_header)
    struct.pack_into("<I", new_header, 88, new_part_crc)
    new_header[16:20] = b"\x00\x00\x00\x00"
    new_header_crc = zlib.crc32(new_header[:gpt.header_size]) & 0xFFFFFFFF
    struct.pack_into("<I", new_header, 16, new_header_crc)

    return bytes(new_header), bytes(entries_data)


def rebuild_backup_gpt(
    gpt: GPTTable,
    updated_partitions: list[GPTPartitionEntry],
    sector_size: int = DEFAULT_SECTOR_SIZE
) -> tuple[bytes, bytes, int]:
    """
    Reconstrói o cabeçalho e as entradas da GPT de Backup (localizada no fim do disco).
    Retorna (backup_header_bytes, entries_data, backup_entries_lba).
    """
    entries_data = bytearray(gpt.num_partition_entries * gpt.partition_entry_size)
    for p in updated_partitions:
        idx = p.index - 1
        if 0 <= idx < gpt.num_partition_entries:
            start = idx * gpt.partition_entry_size
            entries_data[start : start + gpt.partition_entry_size] = p.to_bytes(gpt.partition_entry_size)

    new_part_crc = zlib.crc32(entries_data) & 0xFFFFFFFF
    entries_sectors = (len(entries_data) + sector_size - 1) // sector_size
    backup_entries_lba = gpt.backup_lba - entries_sectors

    backup_header = bytearray(gpt.raw_header)
    struct.pack_into("<QQ", backup_header, 24, gpt.backup_lba, gpt.current_lba)
    struct.pack_into("<Q", backup_header, 72, backup_entries_lba)
    struct.pack_into("<I", backup_header, 88, new_part_crc)
    backup_header[16:20] = b"\x00\x00\x00\x00"
    new_header_crc = zlib.crc32(backup_header[:gpt.header_size]) & 0xFFFFFFFF
    struct.pack_into("<I", backup_header, 16, new_header_crc)

    return bytes(backup_header), bytes(entries_data), backup_entries_lba


def create_lba_snapshot(device_path: str, output_path: Path, sector_count: int = 34, sector_size: int = DEFAULT_SECTOR_SIZE) -> bool:
    """Salva um backup binário exato dos primeiros setores do disco antes de qualquer alteração."""
    try:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        with open(device_path, "rb") as f:
            f.seek(0)
            data = f.read(sector_count * sector_size)
            output_path.write_bytes(data)
            log.info("Snapshot de LBA 0..%d salvo em %s (%d bytes)", sector_count - 1, output_path, len(data))
            return True
    except Exception as exc:
        log.error("Falha ao salvar snapshot de LBA em %s: %s", output_path, exc)
        return False
