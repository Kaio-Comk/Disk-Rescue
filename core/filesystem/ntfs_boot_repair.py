"""
core/filesystem/ntfs_boot_repair.py — Detecção de corrupção no Boot Sector NTFS e restauração cirúrgica via Backup Boot Sector.
"""
from __future__ import annotations

import logging
import os
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

from core.filesystem.ntfs import NTFSBootSector, parse_ntfs_boot_sector, verify_ntfs_mft

log = logging.getLogger("diskrescue.ntfs_boot")


@dataclass
class BootSectorDiagnostic:
    primary_valid: bool
    backup_valid: bool
    can_repair: bool
    primary_lba: int
    backup_lba: int
    primary_boot: NTFSBootSector | None = None
    backup_boot: NTFSBootSector | None = None
    details: str = ""


@dataclass
class BootRepairResult:
    success: bool
    message: str
    backup_file: Path | None = None
    restored_lba: int = 0


def diagnose_ntfs_boot_sectors(
    device_path: str,
    partition_start_lba: int,
    partition_total_sectors: int,
    sector_size: int = 512
) -> BootSectorDiagnostic:
    """Inspeciona o setor de boot primário e o setor de boot reserva (backup) no final do volume."""
    primary_lba = partition_start_lba
    backup_lba = partition_start_lba + partition_total_sectors - 1

    primary_boot = None
    backup_boot = None
    primary_valid = False
    backup_valid = False
    details = []

    try:
        with open(device_path, "rb") as f:
            # 1. Avalia o setor primário (LBA inicial)
            f.seek(primary_lba * sector_size)
            prim_data = f.read(sector_size)
            primary_boot = parse_ntfs_boot_sector(prim_data, primary_lba)
            if primary_boot:
                primary_valid = verify_ntfs_mft(f, primary_boot)
                if primary_valid:
                    details.append(f"Setor primário (LBA {primary_lba:,}) é válido e $MFT foi confirmada.")
                else:
                    details.append(f"Setor primário (LBA {primary_lba:,}) tem cabeçalho NTFS mas $MFT não confere.")
            else:
                details.append(f"Setor primário (LBA {primary_lba:,}) está corrompido, zerado ou com assinatura inválida.")

            # 2. Avalia o setor de backup (último LBA do volume)
            f.seek(backup_lba * sector_size)
            bkp_data = f.read(sector_size)
            backup_boot = parse_ntfs_boot_sector(bkp_data, primary_lba) # interpreta usando o offset primário
            if backup_boot:
                backup_valid = verify_ntfs_mft(f, backup_boot)
                if backup_valid:
                    details.append(f"Backup Boot Sector (LBA {backup_lba:,}) está íntegro e aponta para $MFT válida.")
                else:
                    details.append(f"Backup Boot Sector (LBA {backup_lba:,}) encontrado mas $MFT falhou.")
            else:
                details.append(f"Backup Boot Sector não encontrado no LBA {backup_lba:,}.")

    except Exception as exc:
        details.append(f"Erro durante acesso ao disco: {exc}")

    can_repair = (not primary_valid) and backup_valid

    return BootSectorDiagnostic(
        primary_valid=primary_valid,
        backup_valid=backup_valid,
        can_repair=can_repair,
        primary_lba=primary_lba,
        backup_lba=backup_lba,
        primary_boot=primary_boot,
        backup_boot=backup_boot,
        details=" \n".join(details)
    )


def restore_ntfs_boot_from_backup(
    device_path: str,
    partition_start_lba: int,
    partition_total_sectors: int,
    backup_dir: Path | None = None,
    sector_size: int = 512
) -> BootRepairResult:
    """Restaura cirurgicamente o setor primário corrompido usando o Backup Boot Sector."""
    diag = diagnose_ntfs_boot_sectors(device_path, partition_start_lba, partition_total_sectors, sector_size)

    if diag.primary_valid:
        return BootRepairResult(
            success=False,
            message="O setor de boot primário já está íntegro e validado. Nenhuma restauração necessária."
        )

    if not diag.backup_valid:
        return BootRepairResult(
            success=False,
            message=f"Não foi possível restaurar: o setor de backup no LBA {diag.backup_lba:,} também é inválido."
        )

    backup_dir = backup_dir or Path("backups")

    # Proteção P0: Validar segurança antes de criar qualquer diretório ou arquivo
    try:
        from core.hardware.device_resolver import assert_safe_source_and_destination
        assert_safe_source_and_destination(device_path, backup_dir, is_directory=True)
    except Exception as exc:
        log.error("Bloqueio de segurança no diretório de backup: %s", exc)
        return BootRepairResult(success=False, message=f"Diretório de backup inseguro: {exc}")

    backup_dir.mkdir(parents=True, exist_ok=True)

    clean_name = device_path.replace("\\", "_").replace("/", "_").replace(":", "").replace(".", "").strip("_")[-30:]
    timestamp = time.strftime("%Y%m%d_%H%M%S")
    snapshot_path = backup_dir / f"corrupted_vbr_{clean_name}_{timestamp}.bin"

    try:
        # 1. Snapshot de segurança do setor atual corrompido
        with open(device_path, "r+b") as f:
            f.seek(diag.primary_lba * sector_size)
            corrupted_data = f.read(sector_size)
            if len(corrupted_data) != sector_size:
                return BootRepairResult(success=False, message="Leitura incompleta do setor primário para snapshot.")
            snapshot_path.write_bytes(corrupted_data)

            # Verificação pós-criação do snapshot antes de qualquer escrita
            if snapshot_path.read_bytes() != corrupted_data:
                return BootRepairResult(success=False, message="Verificação de integridade do snapshot de boot falhou.")

            # 2. Ler o setor de backup
            f.seek(diag.backup_lba * sector_size)
            valid_vbr = f.read(sector_size)

            if len(valid_vbr) != sector_size or valid_vbr[3:7] != b"NTFS":
                return BootRepairResult(success=False, message="Setor de backup inconsistente na leitura.")

            def do_rollback():
                log.warning("Iniciando rollback do setor de boot primário...")
                try:
                    f.seek(diag.primary_lba * sector_size)
                    f.write(corrupted_data)
                    f.flush()
                    try:
                        os.fsync(f.fileno())
                    except OSError:
                        pass
                    f.seek(diag.primary_lba * sector_size)
                    rb_data = f.read(sector_size)
                    return rb_data == corrupted_data
                except Exception as rb_exc:
                    log.error("Exceção durante rollback do setor de boot: %s", rb_exc)
                    return False

            # 3. Gravar o setor válido no setor primário com verificação e rollback
            try:
                f.seek(diag.primary_lba * sector_size)
                f.write(valid_vbr)
                f.flush()
                try:
                    os.fsync(f.fileno())
                except OSError as err:
                    log.error("Falha ao sincronizar Boot Sector NTFS gravado (fsync): %s", err)
                    raise OSError(f"Falha de sincronização física: {err}") from err

                # Verificação pós-escrita imediata
                f.seek(diag.primary_lba * sector_size)
                verified = f.read(sector_size)
                if verified != valid_vbr:
                    raise OSError("Verificação pós-escrita falhou: o Boot Sector gravado diverge do backup.")
            except Exception as write_err:
                log.error("Falha na gravação ou verificação do Boot Sector: %s", write_err)
                rb_ok = do_rollback()
                if rb_ok:
                    return BootRepairResult(
                        success=False,
                        message=f"Falha na gravação/verificação ({write_err}). Rollback executado com sucesso e verificado.",
                        backup_file=snapshot_path
                    )
                else:
                    return BootRepairResult(
                        success=False,
                        message=f"FALHA CRÍTICA: Gravação falhou ({write_err}) e rollback falhou na verificação. Restaure manualmente a partir de {snapshot_path}.",
                        backup_file=snapshot_path
                    )

        log.info("Boot sector primário NTFS restaurado com sucesso a partir do backup LBA %d", diag.backup_lba)

        # 4. Notificar SO
        if os.name == "nt":
            subprocess.run(["powershell", "-NoProfile", "-Command", "Update-HostStorageCache"], capture_output=True, timeout=10)

        return BootRepairResult(
            success=True,
            message=f"Boot Sector NTFS restaurado com sucesso no LBA {diag.primary_lba:,} a partir do backup no LBA {diag.backup_lba:,}.",
            backup_file=snapshot_path,
            restored_lba=diag.primary_lba
        )

    except PermissionError:
        return BootRepairResult(
            success=False,
            message="Acesso negado. Execute o programa como Administrador para gravar no disco."
        )
    except Exception as exc:
        log.error("Erro na restauração de VBR NTFS: %s", exc)
        return BootRepairResult(success=False, message=f"Erro durante a gravação: {exc}")
