"""
core/filesystem/fat_exfat.py — Parsers e validadores forenses para FAT32 e exFAT.
"""
from __future__ import annotations

import logging
import struct
from dataclasses import dataclass

log = logging.getLogger("diskrescue.fat")


@dataclass
class FAT32BootSector:
    start_lba: int
    bytes_per_sector: int
    sectors_per_cluster: int
    reserved_sectors: int
    num_fats: int
    total_sectors: int
    sectors_per_fat: int
    root_cluster: int
    fs_version: int
    is_valid: bool = False

    @property
    def size_gb(self) -> float:
        return (self.total_sectors * self.bytes_per_sector) / (1024 ** 3)

    @property
    def end_lba(self) -> int:
        return self.start_lba + self.total_sectors - 1


@dataclass
class EXFATBootSector:
    start_lba: int
    bytes_per_sector: int
    sectors_per_cluster: int
    partition_offset: int
    total_sectors: int
    fat_offset: int
    cluster_heap_offset: int
    root_cluster: int
    is_valid: bool = False

    @property
    def size_gb(self) -> float:
        return (self.total_sectors * self.bytes_per_sector) / (1024 ** 3)

    @property
    def end_lba(self) -> int:
        return self.start_lba + self.total_sectors - 1


def parse_fat32_boot_sector(data: bytes, start_lba: int) -> FAT32BootSector | None:
    """Valida e decodifica 512 bytes de um VBR FAT32."""
    if len(data) < 512 or data[510:512] != b"\x55\xaa":
        return None

    # Verifica assinatura FAT32 no offset 82
    if data[82:87] != b"FAT32":
        return None

    try:
        bytes_per_sec = struct.unpack("<H", data[11:13])[0]
        sec_per_cluster = data[13]
        reserved_sec = struct.unpack("<H", data[14:16])[0]
        num_fats = data[16]
        tot_sec_16 = struct.unpack("<H", data[19:21])[0]
        tot_sec_32 = struct.unpack("<I", data[32:36])[0]
        tot_sectors = tot_sec_32 if tot_sec_32 > 0 else tot_sec_16
        sec_per_fat = struct.unpack("<I", data[36:40])[0]
        root_cluster = struct.unpack("<I", data[44:48])[0]

        if bytes_per_sec not in (512, 1024, 2048, 4096) or sec_per_cluster == 0 or tot_sectors == 0:
            return None

        return FAT32BootSector(
            start_lba=start_lba,
            bytes_per_sector=bytes_per_sec,
            sectors_per_cluster=sec_per_cluster,
            reserved_sectors=reserved_sec,
            num_fats=num_fats,
            total_sectors=tot_sectors,
            sectors_per_fat=sec_per_fat,
            root_cluster=root_cluster,
            fs_version=struct.unpack("<H", data[42:44])[0],
            is_valid=True
        )
    except Exception as exc:
        log.debug("Erro ao decodificar FAT32 no LBA %d: %s", start_lba, exc)
        return None


def parse_exfat_boot_sector(data: bytes, start_lba: int) -> EXFATBootSector | None:
    """Valida e decodifica 512 bytes de um VBR exFAT."""
    if len(data) < 512 or data[510:512] != b"\x55\xaa":
        return None

    if data[3:11] != b"EXFAT   ":
        return None

    try:
        part_offset = struct.unpack("<Q", data[64:72])[0]
        vol_length = struct.unpack("<Q", data[72:80])[0]
        fat_offset = struct.unpack("<I", data[80:84])[0]
        cluster_heap_offset = struct.unpack("<I", data[88:92])[0]
        root_cluster = struct.unpack("<I", data[96:100])[0]
        bytes_per_sec_shift = data[108]
        sec_per_clus_shift = data[109]

        if bytes_per_sec_shift < 9 or bytes_per_sec_shift > 12: # 512 .. 4096
            return None

        bytes_per_sec = 1 << bytes_per_sec_shift
        sec_per_cluster = 1 << sec_per_clus_shift

        return EXFATBootSector(
            start_lba=start_lba,
            bytes_per_sector=bytes_per_sec,
            sectors_per_cluster=sec_per_cluster,
            partition_offset=part_offset,
            total_sectors=vol_length,
            fat_offset=fat_offset,
            cluster_heap_offset=cluster_heap_offset,
            root_cluster=root_cluster,
            is_valid=True
        )
    except Exception as exc:
        log.debug("Erro ao decodificar exFAT no LBA %d: %s", start_lba, exc)
        return None
