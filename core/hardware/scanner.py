"""
core/hardware/scanner.py — Descoberta de discos físicos, SMART e propriedades de hardware.
"""
from __future__ import annotations

import ctypes
import json
import logging
import os
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

log = logging.getLogger("diskrescue.hardware")


def is_admin() -> bool:
    """Verifica se o processo possui privilégios de Administrador/root."""
    try:
        if os.name == "nt":
            return bool(ctypes.windll.shell32.IsUserAnAdmin())
        return os.getuid() == 0  # type: ignore[attr-defined]
    except Exception:
        return False


def relaunch_as_admin() -> bool:
    """Solicita elevação UAC no Windows para reiniciar o programa como Administrador."""
    if os.name == "nt":
        try:
            params = " ".join([f'"{arg}"' for arg in sys.argv])
            res = ctypes.windll.shell32.ShellExecuteW(
                None, "runas", sys.executable, params, None, 1
            )
            return res > 32
        except Exception as exc:
            log.error("Erro ao solicitar elevação UAC: %s", exc)
            return False
    return False


@dataclass
class DiskInfo:
    number: int
    device_path: str
    friendly_name: str
    serial_number: str
    size_bytes: int
    bus_type: str
    partition_style: str
    media_type: str = "Unknown"
    health_status: str = "Healthy"
    operational_status: str = "OK"
    temperature: Optional[int] = None
    read_errors: Optional[int] = None
    write_errors: Optional[int] = None
    wear: Optional[int] = None
    is_read_only: bool = False
    is_boot: bool = False
    is_system: bool = False
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
    """Enumeração via PowerShell Get-Disk e StorageReliabilityCounter no Windows."""
    script = """
    [Console]::OutputEncoding = [System.Text.Encoding]::UTF8
    $disks = Get-Disk -ErrorAction SilentlyContinue
    $phys = Get-PhysicalDisk -ErrorAction SilentlyContinue
    $rel = if ($phys) { $phys | Get-StorageReliabilityCounter -ErrorAction SilentlyContinue } else { @() }

    $results = @()
    if ($disks) {
        foreach ($d in $disks) {
            $p = if ($phys) { $phys | Where-Object { $_.DeviceId -eq [string]$d.Number } | Select-Object -First 1 } else { $null }
            $r = if ($rel) { $rel | Where-Object { $_.DeviceId -eq [string]$d.Number } | Select-Object -First 1 } else { $null }
            $results += [PSCustomObject]@{
                Number = $d.Number
                FriendlyName = $d.FriendlyName
                SerialNumber = $d.SerialNumber
                Size = $d.Size
                BusType = $d.BusType
                PartitionStyle = $d.PartitionStyle
                IsReadOnly = $d.IsReadOnly
                IsBoot = $d.IsBoot
                IsSystem = $d.IsSystem
                MediaType = if ($null -ne $p) { $p.MediaType } else { 'Unknown' }
                HealthStatus = if ($null -ne $p) { $p.HealthStatus } else { 'Healthy' }
                OperationalStatus = if ($null -ne $p) { $p.OperationalStatus } else { 'OK' }
                Temperature = if ($null -ne $r) { $r.Temperature } else { $null }
                ReadErrorsTotal = if ($null -ne $r) { $r.ReadErrorsTotal } else { $null }
                WriteErrorsTotal = if ($null -ne $r) { $r.WriteErrorsTotal } else { $null }
                Wear = if ($null -ne $r) { $r.Wear } else { $null }
            }
        }
    }
    $results | ConvertTo-Json -Depth 3
    """
    cmd = ["powershell", "-NoProfile", "-Command", script]
    try:
        creationflags = 0x08000000 if os.name == "nt" else 0  # CREATE_NO_WINDOW
        r = subprocess.run(cmd, capture_output=True, timeout=15, creationflags=creationflags)
        stdout = r.stdout.decode("utf-8", errors="replace").strip()
        if not stdout:
            log.warning("Get-Disk falhou ou retornou vazio.")
            return []

        raw_data = json.loads(stdout)
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
                media_type=str(d.get("MediaType", "Unknown")).strip(),
                health_status=str(d.get("HealthStatus", "Healthy")).strip(),
                operational_status=str(d.get("OperationalStatus", "OK")).strip(),
                temperature=d.get("Temperature"),
                read_errors=d.get("ReadErrorsTotal"),
                write_errors=d.get("WriteErrorsTotal"),
                wear=d.get("Wear"),
                is_read_only=bool(d.get("IsReadOnly", False)),
                is_boot=bool(d.get("IsBoot", False)),
                is_system=bool(d.get("IsSystem", False)),
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
            dev_name = d.get("name", f"sdX{idx}")
            dev_path = f"/dev/{dev_name}"
            info = DiskInfo(
                number=idx,
                device_path=dev_path,
                friendly_name=str(d.get("model", dev_name)).strip(),
                serial_number=str(d.get("serial", "")).strip(),
                size_bytes=int(d.get("size", 0)),
                bus_type=str(d.get("tran", "unknown")).upper(),
                partition_style=str(d.get("pttype", "unknown")).upper(),
                is_read_only=bool(d.get("ro", False)),
                can_open_raw=check_raw_access(dev_path),
            )
            disks.append(info)
        return disks
    except Exception as exc:
        log.error("Erro ao enumerar discos no Linux: %s", exc)
        return []
