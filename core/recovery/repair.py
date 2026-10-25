"""
core/recovery/repair.py — Motores de reparo não destrutivo e restauração in-place de partições.
"""
from __future__ import annotations

import logging
import os
import subprocess
import tempfile
import time
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

from core.filesystem.scanner import DiscoveredFilesystem
from core.partition.gpt import (
    DEFAULT_SECTOR_SIZE,
    GPTPartitionEntry,
    GPTTable,
    create_lba_snapshot,
    parse_gpt,
    rebuild_gpt,
    rebuild_backup_gpt,
)

log = logging.getLogger("diskrescue.repair")


class RawImageStatus(Enum):
    COMPLETED = "COMPLETED"
    PARTIAL = "PARTIAL"
    CANCELLED = "CANCELLED"
    READ_ERROR = "READ_ERROR"
    WRITE_ERROR = "WRITE_ERROR"
    PERSISTENCE_ERROR = "PERSISTENCE_ERROR"
    REPLACE_ERROR = "REPLACE_ERROR"


@dataclass
class RawImageResult:
    status: RawImageStatus
    sectors_written: int = 0
    bytes_written: int = 0
    total_sectors: int | None = None
    target_path: Path | None = None
    message: str = ""

    def __bool__(self) -> bool:
        return self.status == RawImageStatus.COMPLETED


@dataclass
class RepairResult:
    success: bool
    message: str
    backup_file: Path | None = None
    old_start_lba: int = 0
    new_start_lba: int = 0
    new_end_lba: int = 0


def repair_gpt_partition_offset(
    device_path: str,
    target_partition_index: int,
    discovered_fs: DiscoveredFilesystem,
    backup_dir: Path | None = None,
    sector_size: int = DEFAULT_SECTOR_SIZE
) -> RepairResult:
    """Corrige o offset de início e fim de uma partição na tabela GPT com snapshot completo (primário e backup) e rollback verificado."""
    backup_dir = backup_dir or Path("backups")

    # Proteção P0: Validar segurança antes de criar qualquer diretório ou arquivo
    try:
        from core.hardware.device_resolver import assert_safe_source_and_destination
        assert_safe_source_and_destination(device_path, backup_dir, is_directory=True)
    except Exception as exc:
        log.error("Bloqueio de segurança no diretório de backup: %s", exc)
        return RepairResult(success=False, message=f"Diretório de backup inseguro: {exc}")

    backup_dir.mkdir(parents=True, exist_ok=True)

    timestamp = time.strftime("%Y%m%d_%H%M%S")
    clean_name = device_path.replace("\\", "_").replace("/", "_").replace(":", "").replace(".", "").strip("_")[-30:]
    primary_backup_path = backup_dir / f"backup_gpt_primary_{clean_name}_{timestamp}.bin"
    secondary_backup_path = backup_dir / f"backup_gpt_secondary_{clean_name}_{timestamp}.bin"

    try:
        with open(device_path, "r+b") as f:
            # 1. Ler tabela GPT atual
            gpt = parse_gpt(f, sector_size=sector_size)
            if not gpt:
                return RepairResult(success=False, message="Não foi possível ler a tabela GPT atual.")

            # Localiza a partição alvo
            part_to_update = None
            for p in gpt.partitions:
                if p.index == target_partition_index:
                    part_to_update = p
                    break

            if not part_to_update:
                return RepairResult(
                    success=False,
                    message=f"Partição de índice {target_partition_index} não encontrada na tabela GPT."
                )

            old_start = part_to_update.first_lba
            new_start = discovered_fs.start_lba
            new_end = discovered_fs.end_lba

            # Valida limites utilizáveis da GPT
            if new_start < gpt.first_usable_lba or new_end > gpt.last_usable_lba or new_start > new_end:
                return RepairResult(
                    success=False,
                    message=f"Limites inválidos: LBA {new_start}..{new_end} ultrapassa área utilizável ({gpt.first_usable_lba}..{gpt.last_usable_lba})."
                )

            # Valida sobreposição com outras partições ativas
            for p in gpt.partitions:
                if p.index != target_partition_index and not p.is_empty:
                    if max(new_start, p.first_lba) <= min(new_end, p.last_lba):
                        return RepairResult(
                            success=False,
                            message=f"Conflito de partição: o novo intervalo LBA {new_start}..{new_end} sobrepõe a partição {p.index} (LBA {p.first_lba}..{p.last_lba})."
                        )

            # 2. Identificar e capturar snapshot completo de todas as regiões a serem modificadas
            entries_bytes_len = gpt.num_partition_entries * gpt.partition_entry_size
            entries_sectors = (entries_bytes_len + sector_size - 1) // sector_size
            primary_total_sectors = gpt.partition_entries_lba + entries_sectors

            bkp_header_bytes, bkp_entries_bytes, bkp_entries_lba = rebuild_backup_gpt(gpt, gpt.partitions, sector_size=sector_size)

            # Snapshot primário (LBA 0 até fim das entradas primárias)
            f.seek(0)
            orig_primary_bytes = f.read(primary_total_sectors * sector_size)
            if len(orig_primary_bytes) != primary_total_sectors * sector_size:
                return RepairResult(success=False, message="Leitura incompleta da região GPT primária para snapshot.")
            primary_backup_path.write_bytes(orig_primary_bytes)

            # Snapshot secundário (LBA de entradas de backup até cabeçalho de backup)
            orig_backup_bytes = b""
            backup_total_sectors = 0
            if gpt.backup_lba > 0 and bkp_entries_lba > 0:
                backup_total_sectors = (gpt.backup_lba - bkp_entries_lba) + 1
                f.seek(bkp_entries_lba * sector_size)
                orig_backup_bytes = f.read(backup_total_sectors * sector_size)
                if len(orig_backup_bytes) != backup_total_sectors * sector_size:
                    return RepairResult(success=False, message="Leitura incompleta da região GPT de backup para snapshot.")
                secondary_backup_path.write_bytes(orig_backup_bytes)

            # Verificação pós-criação dos snapshots antes de qualquer escrita
            if primary_backup_path.read_bytes() != orig_primary_bytes:
                return RepairResult(success=False, message="Verificação de integridade do snapshot primário falhou.")
            if orig_backup_bytes and secondary_backup_path.read_bytes() != orig_backup_bytes:
                return RepairResult(success=False, message="Verificação de integridade do snapshot de backup falhou.")

            log.info("Ajustando partição %d: LBA %d -> LBA %d..%d", target_partition_index, old_start, new_start, new_end)

            # Atualiza o objeto da partição
            part_to_update.first_lba = new_start
            part_to_update.last_lba = new_end
            part_to_update.flags = part_to_update.flags & ~0x8000000000000000

            # 3. Recalcular estruturas primária e de backup
            new_header_bytes, new_entries_bytes = rebuild_gpt(gpt, gpt.partitions, sector_size=sector_size)
            bkp_header_bytes, bkp_entries_bytes, bkp_entries_lba = rebuild_backup_gpt(gpt, gpt.partitions, sector_size=sector_size)

            def do_rollback():
                log.warning("Iniciando rollback seguro a partir dos snapshots...")
                try:
                    f.seek(0)
                    f.write(orig_primary_bytes)
                    if orig_backup_bytes and bkp_entries_lba > 0:
                        f.seek(bkp_entries_lba * sector_size)
                        f.write(orig_backup_bytes)
                    f.flush()
                    os.fsync(f.fileno())
                    f.seek(0)
                    rb_prim = f.read(len(orig_primary_bytes))
                    rb_bkp = b""
                    if orig_backup_bytes:
                        f.seek(bkp_entries_lba * sector_size)
                        rb_bkp = f.read(len(orig_backup_bytes))
                    return (rb_prim == orig_primary_bytes) and (not orig_backup_bytes or rb_bkp == orig_backup_bytes)
                except Exception as rb_exc:
                    log.error("Exceção durante rollback: %s", rb_exc)
                    return False

            # 4. Gravação nos LBAs primários (LBA 1 e LBA 2..33)
            try:
                f.seek(1 * sector_size)
                f.write(new_header_bytes)

                f.seek(gpt.partition_entries_lba * sector_size)
                f.write(new_entries_bytes)

                # 5. Gravação da cópia de backup no fim do disco
                if gpt.backup_lba > 0 and bkp_entries_lba > 0:
                    f.seek(bkp_entries_lba * sector_size)
                    f.write(bkp_entries_bytes)
                    f.seek(gpt.backup_lba * sector_size)
                    f.write(bkp_header_bytes)

                f.flush()
                os.fsync(f.fileno())

                # Verificação pós-escrita imediata no cabeçalho e entradas primárias
                f.seek(1 * sector_size)
                read_hdr = f.read(len(new_header_bytes))
                if read_hdr != new_header_bytes:
                    raise OSError("Verificação pós-escrita falhou no cabeçalho GPT primário.")

                f.seek(gpt.partition_entries_lba * sector_size)
                read_entries = f.read(len(new_entries_bytes))
                if read_entries != new_entries_bytes:
                    raise OSError("Verificação pós-escrita falhou nas entradas GPT primárias.")

                # Verificação pós-escrita imediata no cabeçalho e entradas de backup
                if gpt.backup_lba > 0 and bkp_entries_lba > 0:
                    f.seek(bkp_entries_lba * sector_size)
                    read_bkp_entries = f.read(len(bkp_entries_bytes))
                    if read_bkp_entries != bkp_entries_bytes:
                        raise OSError("Verificação pós-escrita falhou nas entradas GPT de backup.")

                    f.seek(gpt.backup_lba * sector_size)
                    read_bkp_hdr = f.read(len(bkp_header_bytes))
                    if read_bkp_hdr != bkp_header_bytes:
                        raise OSError("Verificação pós-escrita falhou no cabeçalho GPT de backup.")

            except Exception as write_err:
                log.error("Erro na gravação ou verificação GPT: %s", write_err)
                rb_ok = do_rollback()
                if rb_ok:
                    return RepairResult(
                        success=False,
                        message=f"Falha na gravação/verificação ({write_err}). Rollback executado com sucesso e verificado.",
                        backup_file=primary_backup_path
                    )
                else:
                    return RepairResult(
                        success=False,
                        message=f"FALHA CRÍTICA: Gravação falhou ({write_err}) e rollback falhou na verificação. Restaure manualmente a partir de {primary_backup_path}.",
                        backup_file=primary_backup_path
                    )

        log.info("Tabela GPT primária e de backup corrigidas e verificadas com sucesso.")

        # 6. Notificar o sistema operacional para atualizar o cache de disco
        if os.name == "nt":
            subprocess.run(["powershell", "-NoProfile", "-Command", "Update-HostStorageCache"], capture_output=True, timeout=10)

        return RepairResult(
            success=True,
            message=f"Partição {target_partition_index} realinhada com sucesso para LBA {new_start:,}..{new_end:,} ({discovered_fs.size_gb:.1f} GB).",
            backup_file=primary_backup_path,
            old_start_lba=old_start,
            new_start_lba=new_start,
            new_end_lba=new_end,
        )

    except PermissionError:
        return RepairResult(
            success=False,
            message="Permissão negada ao gravar no disco. Execute o terminal como Administrador/root.",
            backup_file=primary_backup_path
        )
    except Exception as exc:
        log.error("Exceção ao reparar tabela GPT: %s", exc)
        return RepairResult(success=False, message=f"Erro durante a operação: {exc}", backup_file=primary_backup_path)


def create_raw_disk_image(
    device_path: str,
    output_path: Path,
    sector_size: int = DEFAULT_SECTOR_SIZE,
    max_sectors: int | None = None,
    chunk_sectors: int = 2048,
    allow_overwrite: bool = False,
    progress_cb=None,
    cancel_check=None
) -> RawImageResult:
    """Cria uma imagem binária setor a setor da mídia física para análise forense segura."""
    if sector_size <= 0:
        raise ValueError("sector_size deve ser estritamente positivo.")
    if chunk_sectors <= 0:
        raise ValueError("chunk_sectors deve ser estritamente positivo.")
    if max_sectors is not None and max_sectors <= 0:
        raise ValueError("max_sectors deve ser estritamente positivo quando fornecido.")

    output_path = Path(output_path)

    # 1. Proteção P0: Validar independência de disco físico ANTES de criar diretório ou arquivo
    try:
        from core.hardware.device_resolver import assert_safe_source_and_destination
        assert_safe_source_and_destination(device_path, output_path, is_directory=False)
    except Exception as exc:
        log.error("Bloqueio de segurança na criação de imagem forense: %s", exc)
        raise ValueError(f"Origem e destino inseguros: {exc}") from exc

    # Bloqueio caso a origem seja um arquivo e coincida com o destino
    try:
        if Path(device_path).resolve() == output_path.resolve():
            raise ValueError("O arquivo de destino não pode coincidir com o arquivo de origem.")
    except (ValueError, OSError) as err:
        if "não pode coincidir" in str(err):
            raise

    # 2. Política de não sobrescrever destino existente sem autorização explícita
    if output_path.exists() and not allow_overwrite:
        log.warning("Arquivo de destino já existe e allow_overwrite=False: %s", output_path)
        raise FileExistsError(f"O arquivo de destino já existe ({output_path}). Para substituir, defina allow_overwrite=True.")

    # 3. Criação do diretório pai somente após validação segura
    output_path.parent.mkdir(parents=True, exist_ok=True)

    # 4. Arquivo temporário isolado e exclusivo via tempfile.mkstemp
    fd, temp_str = tempfile.mkstemp(
        dir=str(output_path.parent),
        prefix=f".{output_path.stem}_",
        suffix=".tmp"
    )
    temp_output_path = Path(temp_str)
    try:
        assert_safe_source_and_destination(device_path, temp_output_path, is_directory=False)
    except Exception as exc:
        try:
            os.close(fd)
        except OSError:
            pass
        if temp_output_path.exists():
            try:
                temp_output_path.unlink()
            except OSError:
                pass
        log.error("Bloqueio de segurança no arquivo temporário: %s", exc)
        raise ValueError(f"Destino temporário inseguro: {exc}") from exc

    chunk_bytes = chunk_sectors * sector_size
    current_sector = 0
    t_start = time.time()
    last_cb_time = t_start
    bytes_transferred = 0
    result: RawImageResult | None = None
    fd_closed = False

    def cleanup_temp():
        nonlocal fd_closed
        if not fd_closed:
            try:
                os.close(fd)
                fd_closed = True
            except OSError:
                pass
        try:
            if temp_output_path.exists():
                temp_output_path.unlink()
        except Exception as e:
            log.warning("Falha ao remover arquivo temporário %s: %s", temp_output_path, e)

    try:
        with open(device_path, "rb") as src, os.fdopen(fd, "wb") as dst:
            fd_closed = True
            while True:
                if cancel_check and cancel_check():
                    log.info("Cópia de imagem cancelada pelo usuário.")
                    result = RawImageResult(
                        status=RawImageStatus.CANCELLED,
                        sectors_written=current_sector,
                        bytes_written=bytes_transferred,
                        total_sectors=max_sectors,
                        target_path=output_path,
                        message="Cópia de imagem cancelada pelo usuário."
                    )
                    return result

                if max_sectors and current_sector >= max_sectors:
                    break

                to_read = chunk_bytes
                if max_sectors:
                    remaining_sectors = max_sectors - current_sector
                    if remaining_sectors < chunk_sectors:
                        to_read = remaining_sectors * sector_size

                try:
                    buf = src.read(to_read)
                except Exception as read_err:
                    log.error("Erro ao ler da mídia de origem: %s", read_err)
                    result = RawImageResult(
                        status=RawImageStatus.READ_ERROR,
                        sectors_written=current_sector,
                        bytes_written=bytes_transferred,
                        total_sectors=max_sectors,
                        target_path=output_path,
                        message=f"Erro de leitura da mídia de origem: {read_err}"
                    )
                    return result

                if not buf:
                    # Se max_sectors era esperado mas EOF ocorreu antes, a imagem ficou incompleta
                    if max_sectors and current_sector < max_sectors:
                        log.error("Fim prematuro da origem: lidos %d setores de %d esperados.", current_sector, max_sectors)
                        result = RawImageResult(
                            status=RawImageStatus.PARTIAL,
                            sectors_written=current_sector,
                            bytes_written=bytes_transferred,
                            total_sectors=max_sectors,
                            target_path=output_path,
                            message=f"Fim prematuro da mídia de origem: transferidos {current_sector} de {max_sectors} setores."
                        )
                        return result
                    break

                # Verificação estrita de alinhamento de setor na leitura parcial
                if len(buf) % sector_size != 0:
                    log.error("Leitura parcial desalinhada: lidos %d bytes (não múltiplo de %d).", len(buf), sector_size)
                    result = RawImageResult(
                        status=RawImageStatus.READ_ERROR,
                        sectors_written=current_sector,
                        bytes_written=bytes_transferred,
                        total_sectors=max_sectors,
                        target_path=output_path,
                        message=f"Leitura desalinhada: lidos {len(buf)} bytes, não múltiplo do setor ({sector_size})."
                    )
                    return result

                try:
                    dst.write(buf)
                except Exception as write_err:
                    log.error("Erro ao gravar imagem temporária: %s", write_err)
                    result = RawImageResult(
                        status=RawImageStatus.WRITE_ERROR,
                        sectors_written=current_sector,
                        bytes_written=bytes_transferred,
                        total_sectors=max_sectors,
                        target_path=output_path,
                        message=f"Erro de gravação no arquivo temporário: {write_err}"
                    )
                    return result

                sectors_read = len(buf) // sector_size
                current_sector += sectors_read
                bytes_transferred += len(buf)

                now = time.time()
                if progress_cb and (now - last_cb_time >= 0.25):
                    speed = (bytes_transferred / (1024 * 1024)) / max(0.1, now - t_start)
                    progress_cb(current_sector, max_sectors or current_sector, speed)
                    last_cb_time = now

            # Flush e fsync de durabilidade física
            dst.flush()
            try:
                os.fsync(dst.fileno())
            except OSError as err:
                log.error("Falha ao sincronizar durabilidade da imagem (fsync): %s", err)
                result = RawImageResult(
                    status=RawImageStatus.PERSISTENCE_ERROR,
                    sectors_written=current_sector,
                    bytes_written=bytes_transferred,
                    total_sectors=max_sectors,
                    target_path=output_path,
                    message=f"Falha de sincronização física (fsync): {err}"
                )
                return result

        if max_sectors and current_sector < max_sectors:
            log.error("Cópia incompleta: apenas %d setores transferidos de %d.", current_sector, max_sectors)
            result = RawImageResult(
                status=RawImageStatus.PARTIAL,
                sectors_written=current_sector,
                bytes_written=bytes_transferred,
                total_sectors=max_sectors,
                target_path=output_path,
                message=f"Cópia incompleta: transferidos {current_sector} de {max_sectors} setores."
            )
            return result

        # Substituição atômica somente após fsync comprovado e validação total
        try:
            os.replace(temp_output_path, output_path)
        except Exception as rep_err:
            log.error("Falha ao mover arquivo temporário para destino final: %s", rep_err)
            result = RawImageResult(
                status=RawImageStatus.REPLACE_ERROR,
                sectors_written=current_sector,
                bytes_written=bytes_transferred,
                total_sectors=max_sectors,
                target_path=output_path,
                message=f"Falha ao mover arquivo temporário para destino final: {rep_err}"
            )
            return result

        if progress_cb:
            now = time.time()
            speed = (bytes_transferred / (1024 * 1024)) / max(0.1, now - t_start)
            progress_cb(current_sector, max_sectors or current_sector, speed)

        result = RawImageResult(
            status=RawImageStatus.COMPLETED,
            sectors_written=current_sector,
            bytes_written=bytes_transferred,
            total_sectors=max_sectors,
            target_path=output_path,
            message="Imagem forense criada com sucesso."
        )
        return result
    except Exception as exc:
        log.error("Erro inesperado ao criar imagem forense: %s", exc)
        result = RawImageResult(
            status=RawImageStatus.WRITE_ERROR,
            sectors_written=current_sector,
            bytes_written=bytes_transferred,
            total_sectors=max_sectors,
            target_path=output_path,
            message=f"Erro inesperado durante a operação: {exc}"
        )
        return result
    finally:
        if result is None or result.status != RawImageStatus.COMPLETED:
            cleanup_temp()

