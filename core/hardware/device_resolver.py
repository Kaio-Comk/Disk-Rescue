"""
core/hardware/device_resolver.py — Resolução de identidade física de dispositivos de armazenamento.
Garante proteção P0 contra gravação no mesmo disco físico (incluindo volumes lógicos no mesmo drive).
"""
from __future__ import annotations

import ctypes
import logging
import os
import re
from pathlib import Path
from typing import Optional, Tuple

log = logging.getLogger("diskrescue.device_resolver")

# Windows API constants
IOCTL_STORAGE_GET_DEVICE_NUMBER = 0x002D1080
FILE_SHARE_READ = 0x00000001
FILE_SHARE_WRITE = 0x00000002
OPEN_EXISTING = 3
INVALID_HANDLE_VALUE = -1


if os.name == "nt":
    from ctypes import wintypes

    class STORAGE_DEVICE_NUMBER(ctypes.Structure):
        _fields_ = [
            ("DeviceType", wintypes.DWORD),
            ("DeviceNumber", wintypes.DWORD),
            ("PartitionNumber", wintypes.DWORD),
        ]


def get_physical_drive_number(target: str | Path) -> Optional[int]:
    """
    Identifica o número do disco físico (PhysicalDriveX) associado a um dispositivo, volume ou arquivo.
    Exemplos:
      - r"\\\\.\\PhysicalDrive0" -> 0
      - "C:\\" ou "C:\\Resgate\\disco.img" -> número do disco físico onde C: reside
    Retorna None se não for possível correlacionar.
    """
    target_str = str(target).strip()

    # 1. Checagem direta por regex PhysicalDriveN
    match = re.search(r"PhysicalDrive(\d+)", target_str, re.IGNORECASE)
    if match:
        return int(match.group(1))

    if os.name != "nt":
        return None

    # 2. Resolução no Windows via IOCTL_STORAGE_GET_DEVICE_NUMBER
    # Determina o caminho de volume/dispositivo para consulta
    volume_to_query = None

    # Se for letra de unidade com ou sem dois pontos (ex: "C:", "C:\...", "c:/...")
    drive_match = re.match(r"^([a-zA-Z]):", target_str)
    if drive_match:
        drive_letter = drive_match.group(1).upper()
        volume_to_query = rf"\\.\{drive_letter}:"
    elif target_str.startswith(r"\\.\Volume{") or target_str.startswith(r"\\?\Volume{"):
        # Volume GUID
        vol = target_str.rstrip("\\")
        volume_to_query = vol
    else:
        # Tenta resolver o caminho para encontrar a letra do drive
        try:
            resolved = Path(target_str).resolve()
            drive = resolved.drive
            if drive:
                clean_drive = drive.rstrip("\\")
                volume_to_query = rf"\\.\{clean_drive}"
        except Exception:
            pass

    if not volume_to_query:
        return None

    try:
        kernel32 = ctypes.windll.kernel32
        handle = kernel32.CreateFileW(
            volume_to_query,
            0,  # dwDesiredAccess = 0 (apenas metadados/atributos, não exige leitura ou escrita)
            FILE_SHARE_READ | FILE_SHARE_WRITE,
            None,
            OPEN_EXISTING,
            0,
            None,
        )

        if handle == INVALID_HANDLE_VALUE or handle == 0:
            return None

        try:
            sdn = STORAGE_DEVICE_NUMBER()
            bytes_returned = wintypes.DWORD(0)
            ok = kernel32.DeviceIoControl(
                handle,
                IOCTL_STORAGE_GET_DEVICE_NUMBER,
                None,
                0,
                ctypes.byref(sdn),
                ctypes.sizeof(sdn),
                ctypes.byref(bytes_returned),
                None,
            )
            if ok:
                return int(sdn.DeviceNumber)
        finally:
            kernel32.CloseHandle(handle)
    except Exception as exc:
        log.debug("Erro ao consultar IOCTL_STORAGE_GET_DEVICE_NUMBER para %s: %s", volume_to_query, exc)

    return None


def is_same_physical_device(
    source: str | Path, destination: str | Path, is_dest_dir: bool = False
) -> Tuple[bool, str]:
    """
    Verifica categoricamente se a origem e o destino compartilham o mesmo disco físico ou caminho de arquivo.
    Retorna (is_conflict, message_explanation).
    """
    src_str = str(source).strip()
    dst_str = str(destination).strip()

    # 1. Comparação direta normalizada de strings
    clean_src = src_str.replace("\\\\.\\", "").replace("\\", "/").rstrip("/").lower()
    clean_dst = dst_str.replace("\\\\.\\", "").replace("\\", "/").rstrip("/").lower()

    if clean_src and clean_src == clean_dst:
        return True, f"Origem e destino são o mesmo dispositivo ou caminho ({src_str})."

    # 2. Resolução de caminhos no sistema de arquivos (links, aliases, caminhos relativos)
    try:
        src_path = Path(src_str)
        dst_path = Path(dst_str)
        if src_path.exists():
            resolved_src = src_path.resolve()
            resolved_dst = dst_path.resolve()

            if resolved_src == resolved_dst:
                return True, f"Origem e destino resolvem para o mesmo arquivo físico ({resolved_src})."

            # Se o destino for um diretório e contiver a origem
            if is_dest_dir:
                try:
                    if resolved_src == resolved_dst or resolved_src.is_relative_to(resolved_dst):
                        return True, f"O arquivo de origem está contido dentro da pasta de destino ({resolved_dst})."
                except Exception:
                    pass
    except Exception:
        pass

    # 3. Resolução profunda de Discos Físicos (PhysicalDrive)
    # Aplica-se quando a origem ou destino for uma mídia física de baixo nível (ex: \\.\PhysicalDrive0, \\.\C:)
    src_is_physical = bool(re.search(r"PhysicalDrive\d+", src_str, re.IGNORECASE)) or src_str.startswith(r"\\.")
    dst_is_physical = bool(re.search(r"PhysicalDrive\d+", dst_str, re.IGNORECASE)) or dst_str.startswith(r"\\.")

    if src_is_physical or dst_is_physical:
        src_drive_num = get_physical_drive_number(src_str)
        dst_drive_num = get_physical_drive_number(dst_str)

        if src_drive_num is not None and dst_drive_num is not None:
            if src_drive_num == dst_drive_num:
                return (
                    True,
                    f"Origem e destino pertencem ao mesmo disco físico (PhysicalDrive{src_drive_num}). "
                    f"Gravar dados no mesmo disco em recuperação causará sobrescrita ou colapso do dispositivo.",
                )

    return False, ""


def assert_safe_source_and_destination(
    source: str | Path, destination: str | Path, is_directory: bool = False
) -> None:
    """
    Lança ValueError imediato se a origem e destino colidirem no mesmo disco físico ou arquivo.
    """
    conflict, reason = is_same_physical_device(source, destination, is_dest_dir=is_directory)
    if conflict:
        raise ValueError(
            f"BLOQUEIO DE SEGURANÇA P0: {reason}\n"
            f"Para resgatar dados com segurança, selecione uma unidade física externa ou outro disco independente."
        )


def recommend_safe_target(source: str | Path) -> str:
    """Retorna recomendação contextual para o operador."""
    drive_num = get_physical_drive_number(source)
    if drive_num is not None:
        return (
            f"O disco de origem foi identificado como PhysicalDrive{drive_num}. "
            f"Recomenda-se gravar a imagem em outro disco físico (ex: HD Externo USB ou SSD secundário)."
        )
    return "Recomenda-se gravar a imagem em um dispositivo de armazenamento secundário ou unidade externa."
