"""
core/filesystem/ntfs_boot_repair.py — Detecção de corrupção no Boot Sector NTFS e restauração cirúrgica via Backup Boot Sector.
"""
from __future__ import annotations

import hashlib
import logging
import os
import re
import subprocess
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

from core.filesystem.ntfs import NTFSBootSector, parse_ntfs_boot_sector, verify_ntfs_mft

log = logging.getLogger("diskrescue.ntfs_boot")


def _sanitize_ntfs_device_token(device_path: str) -> str:
    """Sanitiza caminho do dispositivo para uso seguro em nomes de arquivo no Windows sem colisões."""
    raw = str(device_path).strip()
    digest = hashlib.sha1(raw.encode("utf-8", errors="replace")).hexdigest()[:8]
    # Remove caracteres proibidos no Windows (< > : " / \ | ? * e caracteres de controle)
    cleaned = re.sub(r'[^A-Za-z0-9_-]', '_', raw).strip("_")
    while "__" in cleaned:
        cleaned = cleaned.replace("__", "_")
    # Limita a 20 caracteres + hash de 8 caracteres para respeitar MAX_PATH no Windows
    tail = cleaned[-20:] if cleaned else "vol"
    return f"{tail}_{digest}"


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
    rollback_attempted: bool = False
    rollback_confirmed: bool = False
    final_state_uncertain: bool = False


def diagnose_ntfs_boot_sectors(
    device_path: str,
    partition_start_lba: int,
    partition_total_sectors: int,
    sector_size: int = 512
) -> BootSectorDiagnostic:
    """Inspeciona o setor de boot primário e o setor de boot reserva (backup) no final do volume."""
    primary_lba = partition_start_lba
    backup_lba = partition_start_lba + partition_total_sectors - 1

    if sector_size not in (512, 1024, 2048, 4096) or partition_start_lba < 0 or partition_total_sectors <= 1:
        return BootSectorDiagnostic(
            primary_valid=False,
            backup_valid=False,
            can_repair=False,
            primary_lba=primary_lba,
            backup_lba=backup_lba,
            details="Parâmetros geométricos do volume ou tamanho de setor inválidos."
        )

    primary_boot = None
    backup_boot = None
    primary_valid = False
    backup_valid = False
    details = []

    try:
        with open(device_path, "rb") as f:
            f.seek(0, os.SEEK_END)
            dev_size = f.tell()
            if dev_size > 0 and (backup_lba + 1) * sector_size > dev_size:
                return BootSectorDiagnostic(
                    primary_valid=False,
                    backup_valid=False,
                    can_repair=False,
                    primary_lba=primary_lba,
                    backup_lba=backup_lba,
                    details=f"Limites do volume (LBA {backup_lba:,}) excedem o tamanho físico do dispositivo ({dev_size // sector_size:,} setores)."
                )

            # 1. Avalia o setor primário (LBA inicial)
            f.seek(primary_lba * sector_size)
            prim_data = f.read(sector_size)
            if len(prim_data) == sector_size:
                primary_boot = parse_ntfs_boot_sector(prim_data, primary_lba)
            if primary_boot and primary_boot.bytes_per_sector == sector_size and primary_boot.total_sectors <= partition_total_sectors:
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
            if len(bkp_data) == sector_size:
                backup_boot = parse_ntfs_boot_sector(bkp_data, primary_lba)  # interpreta usando o offset primário
            if backup_boot and backup_boot.bytes_per_sector == sector_size and backup_boot.total_sectors <= partition_total_sectors:
                backup_valid = verify_ntfs_mft(f, backup_boot)
                if backup_valid:
                    details.append(f"Backup Boot Sector (LBA {backup_lba:,}) está íntegro e aponta para $MFT válida.")
                else:
                    details.append(f"Backup Boot Sector (LBA {backup_lba:,}) encontrado mas $MFT falhou.")
            else:
                details.append(f"Backup Boot Sector não encontrado ou estruturalmente inválido no LBA {backup_lba:,}.")

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
    backup_dir = backup_dir or Path("backups")

    # Proteção P0: Validar segurança antes de criar qualquer diretório ou arquivo
    try:
        from core.hardware.device_resolver import assert_safe_source_and_destination
        assert_safe_source_and_destination(device_path, backup_dir, is_directory=True)
    except Exception as exc:
        log.error("Bloqueio de segurança no diretório de backup: %s", exc)
        return BootRepairResult(success=False, message=f"Diretório de backup inseguro: {exc}")

    diag = diagnose_ntfs_boot_sectors(device_path, partition_start_lba, partition_total_sectors, sector_size)

    if diag.primary_valid:
        return BootRepairResult(
            success=False,
            message="O setor de boot primário já está íntegro e validado. Nenhuma restauração necessária."
        )

    if not diag.backup_valid:
        return BootRepairResult(
            success=False,
            message=f"Não foi possível restaurar: o setor de backup no LBA {diag.backup_lba:,} também é inválido. ({diag.details})"
        )

    backup_dir.mkdir(parents=True, exist_ok=True)

    clean_token = _sanitize_ntfs_device_token(device_path)
    timestamp = time.strftime("%Y%m%d_%H%M%S")
    snapshot_path: Path | None = None

    try:
        with open(device_path, "r+b") as f:
            # Verifica limites do volume em relação ao tamanho físico
            f.seek(0, os.SEEK_END)
            dev_size = f.tell()
            if dev_size > 0 and (diag.backup_lba + 1) * sector_size > dev_size:
                return BootRepairResult(
                    success=False,
                    message="Limites do volume excedem o tamanho físico do dispositivo."
                )

            # 1. Ler setor primário original completo para snapshot
            f.seek(diag.primary_lba * sector_size)
            corrupted_data = f.read(sector_size)
            if len(corrupted_data) != sector_size:
                return BootRepairResult(success=False, message="Leitura incompleta do setor primário para snapshot.")

            # 2. Ler e revalidar estruturalmente o setor de backup ANTES de criar snapshot/gravar
            f.seek(diag.backup_lba * sector_size)
            valid_vbr = f.read(sector_size)
            if len(valid_vbr) != sector_size:
                return BootRepairResult(success=False, message="Leitura incompleta do setor de backup.")

            parsed_bkp = parse_ntfs_boot_sector(valid_vbr, diag.primary_lba)
            if (
                not parsed_bkp
                or parsed_bkp.bytes_per_sector != sector_size
                or parsed_bkp.total_sectors > partition_total_sectors
                or not verify_ntfs_mft(f, parsed_bkp)
            ):
                return BootRepairResult(
                    success=False,
                    message="Setor de backup inconsistente ou estruturalmente inválido na releitura."
                )

            # 3. Criar snapshot exclusivo, sincronizar (fsync), reler e comparar todos os bytes
            fd, snap_str = tempfile.mkstemp(
                dir=str(backup_dir),
                prefix=f"corrupted_vbr_{clean_token}_{timestamp}_",
                suffix=".bin",
            )
            snapshot_path = Path(snap_str)
            fd_closed = False
            try:
                assert_safe_source_and_destination(device_path, snapshot_path, is_directory=False)
                if Path(device_path).exists() and Path(device_path).resolve() == snapshot_path.resolve():
                    raise ValueError("O arquivo de snapshot não pode coincidir com o dispositivo de origem.")

                with os.fdopen(fd, "wb") as f_snap:
                    fd_closed = True
                    w_snap = f_snap.write(corrupted_data)
                    if w_snap != sector_size:
                        raise OSError(f"Escrita incompleta no snapshot ({w_snap} != {sector_size} bytes).")
                    f_snap.flush()
                    os.fsync(f_snap.fileno())

                read_back_snap = snapshot_path.read_bytes()
                if len(read_back_snap) != sector_size or read_back_snap != corrupted_data:
                    raise OSError("Verificação de integridade do snapshot de boot falhou na releitura.")
            except Exception as snap_err:
                if not fd_closed:
                    try:
                        os.close(fd)
                    except OSError:
                        pass
                log.error("Falha ao criar/validar snapshot preventivo do VBR: %s", snap_err)
                return BootRepairResult(
                    success=False,
                    message=f"Verificação de integridade do snapshot de boot falhou antes da escrita: {snap_err}",
                    backup_file=snapshot_path,
                )

            snap_sha256 = hashlib.sha256(corrupted_data).digest()

            def do_rollback() -> tuple[bool, str]:
                log.warning("Iniciando rollback do setor de boot primário a partir do snapshot...")
                try:
                    # Lê do snapshot em disco e valida contra o hash original
                    if not snapshot_path or not snapshot_path.exists():
                        return False, "Arquivo de snapshot ausente durante tentativa de rollback."
                    snap_bytes = snapshot_path.read_bytes()
                    if len(snap_bytes) != sector_size or hashlib.sha256(snap_bytes).digest() != snap_sha256:
                        return False, "Snapshot em disco está corrompido ou diverge do hash original."

                    f.seek(diag.primary_lba * sector_size)
                    written_rb = f.write(snap_bytes)
                    if written_rb != sector_size:
                        return False, f"Escrita parcial durante rollback ({written_rb} de {sector_size} bytes)."
                    f.flush()
                    # NÃO ignorar erros de fsync no rollback
                    os.fsync(f.fileno())

                    f.seek(diag.primary_lba * sector_size)
                    rb_data = f.read(sector_size)
                    if rb_data != snap_bytes or rb_data != corrupted_data:
                        return False, "Bytes relidos após rollback divergem do setor original."
                    return True, "Rollback confirmado e sincronizado."
                except Exception as rb_exc:
                    log.error("Exceção durante rollback do setor de boot: %s", rb_exc)
                    return False, f"Falha durante rollback ({rb_exc})."

            # 4. Gravar o setor válido no setor primário com verificação e rollback
            try:
                f.seek(diag.primary_lba * sector_size)
                w_vbr = f.write(valid_vbr)
                if w_vbr != sector_size:
                    raise OSError(f"Escrita incompleta no Boot Sector ({w_vbr} de {sector_size} bytes).")
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
                rb_ok, rb_detail = do_rollback()
                if rb_ok:
                    return BootRepairResult(
                        success=False,
                        message=(
                            f"Falha na gravação/verificação ({write_err}). "
                            f"Rollback tentado e confirmado: {rb_detail} (Rollback executado com sucesso e verificado)."
                        ),
                        backup_file=snapshot_path,
                        rollback_attempted=True,
                        rollback_confirmed=True,
                        final_state_uncertain=False,
                    )
                else:
                    return BootRepairResult(
                        success=False,
                        message=(
                            f"FALHA CRÍTICA: Gravação falhou ({write_err}) e rollback falhou na verificação ({rb_detail}) — "
                            f"rollback tentado, mas o estado final do disco é incerto. "
                            f"Snapshot preservado em {snapshot_path}."
                        ),
                        backup_file=snapshot_path,
                        rollback_attempted=True,
                        rollback_confirmed=False,
                        final_state_uncertain=True,
                    )

        log.info("Boot sector primário NTFS restaurado com sucesso a partir do backup LBA %d", diag.backup_lba)

        # 5. Notificar SO
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
            message="Acesso negado. Execute o programa como Administrador para gravar no disco.",
            backup_file=snapshot_path,
        )
    except Exception as exc:
        log.error("Erro na restauração de VBR NTFS: %s", exc)
        return BootRepairResult(
            success=False,
            message=f"Erro durante a gravação: {exc}",
            backup_file=snapshot_path,
        )
