"""
core/filesystem/ntfs.py — Validação, parsing de BPB e conferência de integridade de volumes NTFS.
"""
from __future__ import annotations

import logging
import struct
from dataclasses import dataclass

log = logging.getLogger("diskrescue.ntfs")


@dataclass
class NTFSBootSector:
    start_lba: int
    bytes_per_sector: int
    sectors_per_cluster: int
    total_sectors: int
    mft_lcn: int
    mftmirr_lcn: int
    clusters_per_mft_record: int
    serial_number: int
    is_valid: bool = False
    mft_verified: bool = False

    @property
    def bytes_per_cluster(self) -> int:
        return self.bytes_per_sector * self.sectors_per_cluster

    @property
    def size_gb(self) -> float:
        return (self.total_sectors * self.bytes_per_sector) / (1024 ** 3)

    @property
    def end_lba(self) -> int:
        return self.start_lba + self.total_sectors - 1

    @property
    def mft_offset_bytes(self) -> int:
        return (self.start_lba + self.mft_lcn * self.sectors_per_cluster) * self.bytes_per_sector


def parse_ntfs_boot_sector(data: bytes, start_lba: int) -> NTFSBootSector | None:
    """Decodifica os 512 bytes de um setor de boot NTFS."""
    if len(data) < 512 or data[3:7] != b"NTFS" or data[-2:] != b"\x55\xaa":
        return None

    try:
        bytes_per_sec, sec_per_cluster = struct.unpack("<HB", data[11:14])
        tot_sectors = struct.unpack("<Q", data[40:48])[0]
        mft_lcn = struct.unpack("<Q", data[48:56])[0]
        mftmirr_lcn = struct.unpack("<Q", data[56:64])[0]
        clusters_per_mft = struct.unpack("<b", data[64:65])[0]
        serial_num = struct.unpack("<Q", data[72:80])[0]

        if bytes_per_sec not in (512, 1024, 2048, 4096) or sec_per_cluster == 0 or tot_sectors == 0:
            return None

        return NTFSBootSector(
            start_lba=start_lba,
            bytes_per_sector=bytes_per_sec,
            sectors_per_cluster=sec_per_cluster,
            total_sectors=tot_sectors,
            mft_lcn=mft_lcn,
            mftmirr_lcn=mftmirr_lcn,
            clusters_per_mft_record=clusters_per_mft,
            serial_number=serial_num,
            is_valid=True,
        )
    except Exception as exc:
        log.debug("Erro ao decodificar BPB NTFS no LBA %d: %s", start_lba, exc)
        return None


def verify_ntfs_mft(f, boot: NTFSBootSector) -> bool:
    """Abre o disco e verifica se o MFT Record 0 ($MFT) existe e é válido."""
    try:
        f.seek(boot.mft_offset_bytes)
        rec = f.read(1024)
        if len(rec) >= 4 and rec[:4] == b"FILE":
            boot.mft_verified = True
            return True
        return False
    except Exception as exc:
        log.debug("Falha ao ler MFT no offset %d: %s", boot.mft_offset_bytes, exc)
        return False
