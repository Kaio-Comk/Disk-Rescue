"""
core/hardware/scanner.py — Descoberta de discos físicos e propriedades de hardware.
"""
from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger("diskrescue.hardware")


@dataclass
class DiskInfo:
    number: int
    device_path: str
    friendly_name: str
    serial_number: str
    size_bytes: int
    bus_type: str
    partition_style: str
    is_read_only: bool = False
    can_open_raw: bool = False

    @property
    def size_gb(self) -> float:
        return self.size_bytes / (1024 ** 3)

    def __str__(self) -> str:
        return f"Disco {self.number}: {self.friendly_name} ({self.size_gb:.1f} GB) [{self.bus_type}] - {self.partition_style}"


def check_raw_access(device_path: str) -> bool:
    """Verifica se é possível abrir o disco em modo somente-leitura."""
    try:
        with open(device_path, "rb") as f:
            header = f.read(512)
            return len(header) == 512
    except (PermissionError, OSError) as exc:
        log.debug("Acesso raw falhou em %s: %s", device_path, exc)
        return False


def scan_disks() -> list[DiskInfo]:
    """Descobre todos os discos físicos conectados."""
    if os.name == "nt":
        return _scan_disks_windows()
    return _scan_disks_linux()


def _scan_disks_windows() -> list[DiskInfo]:
    """Enumeração via PowerShell Get-Disk no Windows."""
    cmd = [
        "powershell",
        "-NoProfile",
        "-Command",
        "Get-Disk | Select-Object Number, FriendlyName, SerialNumber, Size, BusType, PartitionStyle, IsReadOnly | ConvertTo-Json -Depth 2"
    ]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
        if r.returncode != 0 or not r.stdout.strip():
            log.warning("Get-Disk falhou ou retornou vazio.")
            return []
        
        raw_data = json.loads(r.stdout)
        if isinstance(raw_data, dict):
            raw_data = [raw_data]
            
        disks = []
        for d in raw_data:
            num = int(d.get("Number", 0))
            dev_path = rf"\\.\PhysicalDrive{num}"
            info = DiskInfo(
                number=num,
                device_path=dev_path,
                friendly_name=str(d.get("FriendlyName", f"Disco {num}")).strip(),
                serial_number=str(d.get("SerialNumber", "")).strip(),
                size_bytes=int(d.get("Size", 0)),
                bus_type=str(d.get("BusType", "Unknown")).strip(),
                partition_style=str(d.get("PartitionStyle", "Unknown")).strip(),
                is_read_only=bool(d.get("IsReadOnly", False)),
                can_open_raw=check_raw_access(dev_path),
            )
            disks.append(info)
        return sorted(disks, key=lambda x: x.number)
    except Exception as exc:
        log.error("Erro ao enumerar discos no Windows: %s", exc)
        return []


def _scan_disks_linux() -> list[DiskInfo]:
    """Enumeração via lsblk no Linux."""
    cmd = ["lsblk", "-J", "-b", "-d", "-o", "NAME,MODEL,SERIAL,SIZE,TRAN,PTTYPE,RO"]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
        data = json.loads(r.stdout).get("blockdevices", [])
        disks = []
        for idx, d in enumerate(data):
            name = d.get("name", "")
            dev_path = f"/dev/{name}"
            info = DiskInfo(
                number=idx,
                device_path=dev_path,
                friendly_name=str(d.get("model") or name).strip(),
                serial_number=str(d.get("serial", "")).strip(),
                size_bytes=int(d.get("size", 0)),
                bus_type=str(d.get("tran", "SATA")).upper(),
                partition_style=str(d.get("pttype", "Unknown")).upper(),
                is_read_only=bool(d.get("ro", False)),
                can_open_raw=check_raw_access(dev_path),
            )
            disks.append(info)
        return disks
    except Exception as exc:
        log.error("Erro ao enumerar discos no Linux: %s", exc)
        return []
