"""
core/filesystem/scanner.py — Varredura de setores em busca de assinaturas de sistemas de arquivos.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Callable

from core.filesystem.fat_exfat import (
    parse_exfat_boot_sector,
    parse_fat32_boot_sector,
)
from core.filesystem.ntfs import NTFSBootSector, parse_ntfs_boot_sector, verify_ntfs_mft

log = logging.getLogger("diskrescue.scanner")


@dataclass
class DiscoveredFilesystem:
    fs_type: str                   # NTFS, FAT32, exFAT, EXT4
    start_lba: int
    total_sectors: int
    size_bytes: int
    label: str = ""
    is_verified: bool = False
    details: dict = None

    @property
    def size_gb(self) -> float:
        return self.size_bytes / (1024 ** 3)

    @property
    def end_lba(self) -> int:
        return self.start_lba + self.total_sectors - 1


def scan_boot_sectors(
    device_path: str,
    max_lba: int = 2_000_000,
    chunk_sectors: int = 2048,
    progress_cb: Callable[[int, int], None] | None = None
) -> list[DiscoveredFilesystem]:
    """Varre os primeiros setores do disco procurando assinaturas de Boot Sector válidas."""
    discovered: list[DiscoveredFilesystem] = []
    chunk_bytes = chunk_sectors * 512

    try:
        with open(device_path, "rb") as f:
            for lba in range(0, max_lba, chunk_sectors):
                if progress_cb:
                    progress_cb(lba, max_lba)
                f.seek(lba * 512)
                chunk = f.read(chunk_bytes)
                if not chunk:
                    break

                for sec_idx in range(0, len(chunk), 512):
                    sec_data = chunk[sec_idx : sec_idx + 512]
                    if len(sec_data) < 512 or sec_data[-2:] != b"\x55\xaa":
                        continue

                    cur_lba = lba + (sec_idx // 512)

                    # 1. Verifica NTFS
                    if sec_data[3:7] == b"NTFS":
                        boot = parse_ntfs_boot_sector(sec_data, cur_lba)
                        if boot and boot.is_valid:
                            verified = verify_ntfs_mft(f, boot)
                            # Checa backup boot sector no final da partição
                            try:
                                f.seek(boot.end_lba * 512)
                                b_sec = f.read(512)
                                has_backup = (len(b_sec) == 512 and b_sec[3:7] == b"NTFS")
                            except Exception:
                                has_backup = False

                            discovered.append(DiscoveredFilesystem(
                                fs_type="NTFS",
                                start_lba=cur_lba,
                                total_sectors=boot.total_sectors,
                                size_bytes=boot.total_sectors * boot.bytes_per_sector,
                                is_verified=verified,
                                details={
                                    "mft_lcn": boot.mft_lcn,
                                    "mft_verified": verified,
                                    "has_backup_vbr": has_backup,
                                    "serial": f"{boot.serial_number:016X}"
                                }
                            ))

                    # 2. Verifica FAT32
                    elif sec_data[82:87] == b"FAT32":
                        f32 = parse_fat32_boot_sector(sec_data, cur_lba)
                        if f32 and f32.is_valid:
                            label = sec_data[71:82].decode("latin1", errors="ignore").strip()
                            discovered.append(DiscoveredFilesystem(
                                fs_type="FAT32",
                                start_lba=cur_lba,
                                total_sectors=f32.total_sectors,
                                size_bytes=f32.total_sectors * f32.bytes_per_sector,
                                label=label,
                                is_verified=True,
                                details={
                                    "root_cluster": f32.root_cluster,
                                    "sectors_per_fat": f32.sectors_per_fat,
                                    "oem_id": sec_data[3:11].decode("latin1", errors="ignore").strip()
                                }
                            ))

                    # 3. Verifica exFAT
                    elif sec_data[3:11] == b"EXFAT   ":
                        exf = parse_exfat_boot_sector(sec_data, cur_lba)
                        if exf and exf.is_valid:
                            discovered.append(DiscoveredFilesystem(
                                fs_type="exFAT",
                                start_lba=cur_lba,
                                total_sectors=exf.total_sectors,
                                size_bytes=exf.total_sectors * exf.bytes_per_sector,
                                is_verified=True,
                                details={
                                    "root_cluster": exf.root_cluster,
                                    "fat_offset": exf.fat_offset,
                                    "cluster_heap_offset": exf.cluster_heap_offset
                                }
                            ))

    except Exception as exc:
        log.error("Erro durante varredura de boot sectors em %s: %s", device_path, exc)

    return discovered
