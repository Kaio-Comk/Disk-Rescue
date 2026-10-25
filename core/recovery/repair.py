"""
core/recovery/repair.py — Motores de reparo não destrutivo e restauração in-place de partições.
"""
from __future__ import annotations

import ctypes
import hashlib
import logging
import os
import re
import subprocess
import struct
import tempfile
import time
import zlib
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path

from core.filesystem.scanner import DiscoveredFilesystem
from core.partition.gpt import (
    DEFAULT_SECTOR_SIZE,
    GPT_HEADER_SIGNATURE,
    GPTPartitionEntry,
    GPTTable,
    create_lba_snapshot,
    parse_gpt,
    rebuild_gpt,
    rebuild_backup_gpt,
)

log = logging.getLogger("diskrescue.repair")

MAX_RAW_IMAGE_CHUNK_BYTES = 64 * 1024 * 1024  # Limite seguro de 64 MiB por bloco em RAM


def _sanitize_device_token(device_path: str) -> str:
    """Gera identificador seguro para nomes de arquivos no Windows sem colisões entre dispositivos distintos."""
    raw = str(device_path).strip()
    digest = hashlib.sha1(raw.encode("utf-8", errors="replace")).hexdigest()[:8]
    cleaned = re.sub(r"[^A-Za-z0-9_-]", "_", raw).strip("_")
    while "__" in cleaned:
        cleaned = cleaned.replace("__", "_")
    tail = cleaned[-20:] if cleaned else "dev"
    return f"{tail}_{digest}"


def _write_exclusive_synced_snapshot(
    device_path: str,
    backup_dir: Path,
    prefix: str,
    payload: bytes,
) -> Path:
    """
    Cria arquivo de snapshot de forma estritamente exclusiva (sem sobrescrever anteriores),
    grava os bytes, executa flush + fsync, relê do disco e valida bytes e hash SHA-256.
    """
    from core.hardware.device_resolver import assert_safe_source_and_destination

    fd, snap_str = tempfile.mkstemp(dir=str(backup_dir), prefix=prefix, suffix=".bin")
    snap_path = Path(snap_str)
    fd_closed = False
    try:
        assert_safe_source_and_destination(device_path, snap_path, is_directory=False)
        try:
            if Path(device_path).exists() and Path(device_path).resolve() == snap_path.resolve():
                raise ValueError("O arquivo de snapshot não pode coincidir com o dispositivo de origem.")
        except OSError:
            pass

        with os.fdopen(fd, "wb") as f_snap:
            fd_closed = True
            written = f_snap.write(payload)
            if written != len(payload):
                raise OSError(f"Escrita incompleta no snapshot {snap_path}: {written} != {len(payload)} bytes.")
            f_snap.flush()
            os.fsync(f_snap.fileno())

        read_back = snap_path.read_bytes()
        if len(read_back) != len(payload) or read_back != payload:
            raise OSError(f"Verificação pós-escrita falhou para o snapshot {snap_path}.")
        if hashlib.sha256(read_back).digest() != hashlib.sha256(payload).digest():
            raise OSError(f"Divergência de hash SHA-256 no snapshot {snap_path}.")

        return snap_path
    except Exception:
        if not fd_closed:
            try:
                os.close(fd)
            except OSError:
                pass
        try:
            if snap_path.exists():
                snap_path.unlink()
        except OSError:
            pass
        raise


def _atomic_move_file(src_path: Path, dst_path: Path, allow_overwrite: bool) -> None:
    """
    Move `src_path` para `dst_path` de forma atômica.
    Quando `allow_overwrite=False`, garante no nível de sistema operacional que um destino
    criado concorrentemente NÃO seja sobrescrito (MoveFileExW com flags=0 no Windows).
    """
    if allow_overwrite:
        os.replace(src_path, dst_path)
        return

    if os.name == "nt":
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        from ctypes import wintypes
        k32.MoveFileExW.argtypes = [wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD]
        k32.MoveFileExW.restype = wintypes.BOOL
        # dwFlags = 0 impede MOVEFILE_REPLACE_EXISTING: falha atomicamente se dst_path existir
        ok = k32.MoveFileExW(str(src_path), str(dst_path), 0)
        if not ok:
            err_code = ctypes.get_last_error()
            raise FileExistsError(
                f"Falha ao finalizar imagem sem sobrescrita para '{dst_path}' (WinError {err_code}): "
                "o arquivo de destino já existe ou foi criado concorrentemente."
            )
    else:
        os.link(src_path, dst_path)
        os.unlink(src_path)


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

    @property
    def success(self) -> bool:
        """Propriedade explícita de compatibilidade indicando conclusão integral sem erros."""
        return self.status == RawImageStatus.COMPLETED

    def __bool__(self) -> bool:
        return self.status == RawImageStatus.COMPLETED


@dataclass
class RepairResult:
    success: bool
    message: str
    backup_file: Path | None = None
    secondary_backup_file: Path | None = None
    old_start_lba: int = 0
    old_end_lba: int = 0
    new_start_lba: int = 0
    new_end_lba: int = 0
    write_completed: bool = False
    post_write_verified: bool = False
    rollback_attempted: bool = False
    rollback_confirmed: bool = False
    final_state_uncertain: bool = False
    cache_refresh_attempted: bool = False
    cache_refresh_succeeded: bool = True
    cache_refresh_warning: str = ""
    structural_diff: list[str] = field(default_factory=list)
    region_snapshots: dict[str, Path] = field(default_factory=dict)


def repair_gpt_partition_offset(
    device_path: str,
    target_partition_index: int,
    discovered_fs: DiscoveredFilesystem,
    backup_dir: Path | None = None,
    sector_size: int = DEFAULT_SECTOR_SIZE,
    clear_no_drive_letter_flag: bool = False,
) -> RepairResult:
    """Corrige o offset de início e fim de uma partição na tabela GPT com snapshot completo (primário e backup) e rollback verificado."""
    if sector_size <= 0 or sector_size % 512 != 0 or sector_size > 65536:
        return RepairResult(success=False, message=f"Tamanho de setor inválido: {sector_size}.")

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
    clean_name = _sanitize_device_token(device_path)
    primary_backup_path: Path | None = None
    secondary_backup_path: Path | None = None
    region_snapshots: dict[str, Path] = {}
    opened_for_write = False
    write_completed = False
    post_write_verified = False
    rollback_attempted = False
    rollback_confirmed = False
    structural_diff: list[str] = []

    try:
        # Fase 1: Leitura estritamente em modo somente-leitura ("rb") e criação/validação de todos os snapshots
        with open(device_path, "rb") as f_ro:
            gpt = parse_gpt(f_ro, sector_size=sector_size)
            if not gpt:
                return RepairResult(success=False, message="Não foi possível ler a tabela GPT atual ou metadados inválidos.")

            if not gpt.is_header_crc_valid:
                return RepairResult(success=False, message="Cabeçalho GPT primário com CRC32 inválido. Reparo recusado por segurança.")

            if not gpt.is_entries_crc_valid:
                return RepairResult(success=False, message="Tabela de partições GPT primária com CRC32 inválido. Reparo recusado por segurança.")

            entries_bytes_len = gpt.num_partition_entries * gpt.partition_entry_size
            entries_sectors = (entries_bytes_len + sector_size - 1) // sector_size

            # Verifica limites físicos do dispositivo/imagem
            f_ro.seek(0, os.SEEK_END)
            device_total_bytes = f_ro.tell()
            if device_total_bytes > 0:
                total_device_sectors = device_total_bytes // sector_size
                if gpt.backup_lba >= total_device_sectors:
                    return RepairResult(
                        success=False,
                        message=f"Metadados GPT inconsistentes: backup_lba ({gpt.backup_lba}) excede os setores físicos ({total_device_sectors})."
                    )

            # Inspeciona e valida cabeçalho e tabela GPT de backup
            if gpt.backup_lba <= gpt.last_usable_lba:
                return RepairResult(
                    success=False,
                    message=f"Metadados GPT inconsistentes: backup_lba ({gpt.backup_lba}) <= last_usable_lba ({gpt.last_usable_lba})."
                )

            f_ro.seek(gpt.backup_lba * sector_size)
            orig_bkp_header_bytes = f_ro.read(sector_size)
            if len(orig_bkp_header_bytes) != sector_size:
                return RepairResult(success=False, message="Leitura incompleta do cabeçalho GPT de backup.")

            bkp_entries_lba = gpt.backup_lba - entries_sectors
            raw_bkp_hdr_for_rebuild: bytes | None = None

            # Se o setor do cabeçalho de backup estiver preenchido, valida-o integralmente e usa seu partition_entries_lba
            if orig_bkp_header_bytes != (b"\x00" * sector_size):
                if orig_bkp_header_bytes[:8] != GPT_HEADER_SIGNATURE:
                    return RepairResult(
                        success=False,
                        message="Cabeçalho GPT de backup corrompido ou com assinatura inválida. Reparo recusado."
                    )
                (
                    b_rev, b_hsize, b_hcrc, b_res,
                    b_cur_lba, b_bkp_lba, b_first_u, b_last_u,
                    b_guid, b_part_lba, b_num_ent, b_ent_size, b_part_crc
                ) = struct.unpack("<IIIIQQQQ16sQIII", orig_bkp_header_bytes[8:92])

                if not (92 <= b_hsize <= sector_size) or b_hsize != gpt.header_size:
                    return RepairResult(success=False, message="Cabeçalho GPT de backup com header_size inconsistente.")

                test_bhdr = bytearray(orig_bkp_header_bytes[:b_hsize])
                test_bhdr[16:20] = b"\x00\x00\x00\x00"
                calc_bhcrc = zlib.crc32(test_bhdr) & 0xFFFFFFFF
                if calc_bhcrc != b_hcrc:
                    return RepairResult(success=False, message="Cabeçalho GPT de backup com CRC32 inválido. Reparo recusado.")

                if (
                    b_cur_lba != gpt.backup_lba
                    or b_bkp_lba != gpt.current_lba
                    or b_first_u != gpt.first_usable_lba
                    or b_last_u != gpt.last_usable_lba
                    or b_guid != gpt.disk_guid
                    or b_num_ent != gpt.num_partition_entries
                    or b_ent_size != gpt.partition_entry_size
                ):
                    return RepairResult(
                        success=False,
                        message="Inconsistência estrutural: cabeçalho GPT de backup diverge do cabeçalho primário."
                    )

                if b_part_lba <= gpt.last_usable_lba or (b_part_lba + entries_sectors) > gpt.backup_lba:
                    return RepairResult(
                        success=False,
                        message=f"Cabeçalho GPT de backup possui partition_entries_lba ({b_part_lba}) fora dos limites seguros."
                    )

                f_ro.seek(b_part_lba * sector_size)
                bkp_entries_check = f_ro.read(entries_bytes_len)
                if len(bkp_entries_check) != entries_bytes_len:
                    return RepairResult(success=False, message="Leitura incompleta da tabela de partições GPT de backup.")

                calc_bpart_crc = zlib.crc32(bkp_entries_check) & 0xFFFFFFFF
                if calc_bpart_crc != b_part_crc or calc_bpart_crc != gpt.partition_entries_crc:
                    return RepairResult(
                        success=False,
                        message="Tabela de partições GPT de backup com CRC32 inválido ou divergente da tabela primária."
                    )

                bkp_entries_lba = b_part_lba
                raw_bkp_hdr_for_rebuild = orig_bkp_header_bytes
            else:
                if bkp_entries_lba <= gpt.last_usable_lba or (bkp_entries_lba + entries_sectors) > gpt.backup_lba:
                    return RepairResult(
                        success=False,
                        message=f"Geometria GPT inválida: região de backup ({bkp_entries_lba}..{gpt.backup_lba}) invade área utilizável."
                    )

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
            old_end = part_to_update.last_lba
            old_flags = part_to_update.flags
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

            # 2. Capturar snapshots exatos de todas as regiões envolvidas (MBR protetor, Cabeçalho Primário,
            # Tabela Primária, Tabela de Backup e Cabeçalho Secundário)
            f_ro.seek(0)
            orig_mbr_bytes = f_ro.read(sector_size)
            if len(orig_mbr_bytes) != sector_size:
                return RepairResult(success=False, message="Leitura incompleta do MBR protetor (LBA 0) para snapshot.")

            f_ro.seek(1 * sector_size)
            orig_prim_hdr_bytes = f_ro.read(sector_size)
            if len(orig_prim_hdr_bytes) != sector_size:
                return RepairResult(success=False, message="Leitura incompleta do cabeçalho GPT primário para snapshot.")

            f_ro.seek(gpt.partition_entries_lba * sector_size)
            orig_prim_entries_bytes = f_ro.read(entries_sectors * sector_size)
            if len(orig_prim_entries_bytes) != entries_sectors * sector_size:
                return RepairResult(success=False, message="Leitura incompleta da tabela GPT primária para snapshot.")

            f_ro.seek(bkp_entries_lba * sector_size)
            orig_bkp_entries_bytes = f_ro.read(entries_sectors * sector_size)
            if len(orig_bkp_entries_bytes) != entries_sectors * sector_size:
                return RepairResult(success=False, message="Leitura incompleta da tabela GPT de backup para snapshot.")

            orig_primary_bytes = orig_mbr_bytes + orig_prim_hdr_bytes + orig_prim_entries_bytes
            orig_backup_bytes = orig_bkp_entries_bytes + orig_bkp_header_bytes

        # Grava e valida todos os snapshots ANTES de abrir o disco para escrita ("r+b")
        try:
            region_snapshots["protective_mbr"] = _write_exclusive_synced_snapshot(
                device_path=device_path,
                backup_dir=backup_dir,
                prefix=f"snap_mbr_protective_{clean_name}_{timestamp}_",
                payload=orig_mbr_bytes,
            )
            region_snapshots["primary_header"] = _write_exclusive_synced_snapshot(
                device_path=device_path,
                backup_dir=backup_dir,
                prefix=f"snap_gpt_prim_hdr_{clean_name}_{timestamp}_",
                payload=orig_prim_hdr_bytes,
            )
            region_snapshots["primary_entries"] = _write_exclusive_synced_snapshot(
                device_path=device_path,
                backup_dir=backup_dir,
                prefix=f"snap_gpt_prim_entries_{clean_name}_{timestamp}_",
                payload=orig_prim_entries_bytes,
            )
            region_snapshots["backup_entries"] = _write_exclusive_synced_snapshot(
                device_path=device_path,
                backup_dir=backup_dir,
                prefix=f"snap_gpt_bkp_entries_{clean_name}_{timestamp}_",
                payload=orig_bkp_entries_bytes,
            )
            region_snapshots["backup_header"] = _write_exclusive_synced_snapshot(
                device_path=device_path,
                backup_dir=backup_dir,
                prefix=f"snap_gpt_bkp_hdr_{clean_name}_{timestamp}_",
                payload=orig_bkp_header_bytes,
            )
            primary_backup_path = _write_exclusive_synced_snapshot(
                device_path=device_path,
                backup_dir=backup_dir,
                prefix=f"backup_gpt_primary_{clean_name}_{timestamp}_",
                payload=orig_primary_bytes,
            )
            secondary_backup_path = _write_exclusive_synced_snapshot(
                device_path=device_path,
                backup_dir=backup_dir,
                prefix=f"backup_gpt_secondary_{clean_name}_{timestamp}_",
                payload=orig_backup_bytes,
            )
        except Exception as snap_err:
            log.error("Falha ao criar/validar snapshots GPT antes da escrita: %s", snap_err)
            return RepairResult(
                success=False,
                message=f"Falha ao gravar ou verificar integridade dos snapshots preventivos: {snap_err}",
                backup_file=primary_backup_path,
                secondary_backup_file=secondary_backup_path,
                region_snapshots=region_snapshots,
            )

        prim_snap_hash = hashlib.sha256(orig_primary_bytes).digest()
        bkp_snap_hash = hashlib.sha256(orig_backup_bytes).digest()

        log.info("Ajustando partição %d: LBA %d..%d -> LBA %d..%d", target_partition_index, old_start, old_end, new_start, new_end)

        # Atualiza estritamente first_lba e last_lba da partição alvo.
        # Preserva integralmente os atributos (flags) por padrão (Seção 8), a menos que clear_no_drive_letter_flag=True seja solicitado.
        part_to_update.first_lba = new_start
        part_to_update.last_lba = new_end
        if clear_no_drive_letter_flag:
            part_to_update.flags = old_flags & ~0x8000000000000000
        else:
            part_to_update.flags = old_flags

        # 3. Recalcular estruturas primária e de backup
        new_header_bytes, new_entries_bytes = rebuild_gpt(gpt, gpt.partitions, sector_size=sector_size)
        bkp_header_bytes, bkp_entries_bytes, bkp_entries_lba = rebuild_backup_gpt(
            gpt,
            gpt.partitions,
            sector_size=sector_size,
            backup_entries_lba=bkp_entries_lba,
            raw_backup_header=raw_bkp_hdr_for_rebuild,
        )

        # Preenche até múltiplo de setor se necessário
        if len(new_entries_bytes) < entries_sectors * sector_size:
            new_entries_bytes = new_entries_bytes + orig_prim_entries_bytes[len(new_entries_bytes):]
        if len(bkp_entries_bytes) < entries_sectors * sector_size:
            bkp_entries_bytes = bkp_entries_bytes + orig_bkp_entries_bytes[len(bkp_entries_bytes):]

        # Validação estrita de preservação byte a byte de todas as outras entradas e dos campos não-offset da entrada alvo
        ent_size = gpt.partition_entry_size
        for idx in range(gpt.num_partition_entries):
            entry_num = idx + 1
            e_off = idx * ent_size
            orig_ent = orig_prim_entries_bytes[e_off : e_off + ent_size]
            new_ent = new_entries_bytes[e_off : e_off + ent_size]
            bkp_ent = bkp_entries_bytes[e_off : e_off + ent_size]
            if new_ent != bkp_ent:
                return RepairResult(
                    success=False,
                    message=f"Inconsistência interna: entrada {entry_num} diverge entre tabela primária e de backup geradas.",
                    backup_file=primary_backup_path,
                    secondary_backup_file=secondary_backup_path,
                    region_snapshots=region_snapshots,
                )
            if entry_num != target_partition_index:
                if new_ent != orig_ent:
                    return RepairResult(
                        success=False,
                        message=f"Violação de preservação: entrada GPT não-alvo (índice {entry_num}) foi alterada indevidamente.",
                        backup_file=primary_backup_path,
                        secondary_backup_file=secondary_backup_path,
                        region_snapshots=region_snapshots,
                    )
            else:
                # Verifica que partition_type_guid (0..16), unique_partition_guid (16..32), e nome/resto (56..ent_size) estão idênticos
                if new_ent[:32] != orig_ent[:32] or new_ent[56:] != orig_ent[56:]:
                    return RepairResult(
                        success=False,
                        message="Violação de preservação: GUIDs ou nome da partição alvo foram alterados durante rebuild.",
                        backup_file=primary_backup_path,
                        secondary_backup_file=secondary_backup_path,
                        region_snapshots=region_snapshots,
                    )
                if not clear_no_drive_letter_flag and new_ent[48:56] != orig_ent[48:56]:
                    return RepairResult(
                        success=False,
                        message="Violação de preservação: atributos (flags) da partição alvo foram alterados sem autorização.",
                        backup_file=primary_backup_path,
                        secondary_backup_file=secondary_backup_path,
                        region_snapshots=region_snapshots,
                    )

        new_prim_hcrc = struct.unpack("<I", new_header_bytes[16:20])[0]
        new_part_crc = struct.unpack("<I", new_header_bytes[88:92])[0]
        new_bkp_hcrc = struct.unpack("<I", bkp_header_bytes[16:20])[0]
        structural_diff = [
            f"partition[{target_partition_index}].first_lba: {old_start} -> {new_start}",
            f"partition[{target_partition_index}].last_lba: {old_end} -> {new_end}",
            (
                f"partition[{target_partition_index}].flags: 0x{old_flags:016X} -> 0x{part_to_update.flags:016X} "
                f"({'alterado por solicitação explícita' if clear_no_drive_letter_flag else 'preservado'})"
            ),
            f"gpt.partition_entries_crc32: 0x{gpt.partition_entries_crc:08X} -> 0x{new_part_crc:08X}",
            f"gpt.primary_header_crc32: 0x{gpt.header_crc:08X} -> 0x{new_prim_hcrc:08X}",
            f"gpt.backup_header_crc32: -> 0x{new_bkp_hcrc:08X}",
        ]

        # Fase 2: Somente após validação integral de todos os 7 snapshots, abre o dispositivo para escrita ("r+b")
        opened_for_write = True
        with open(device_path, "r+b") as f:

            def do_rollback() -> bool:
                log.warning("Iniciando rollback seguro a partir dos snapshots verificados...")
                try:
                    # Confirma integridade dos snapshots no disco; se adulterados externamente, usa cópia íntegra autenticada por SHA-256
                    prim_source = orig_primary_bytes
                    if primary_backup_path and primary_backup_path.exists():
                        disk_prim = primary_backup_path.read_bytes()
                        if hashlib.sha256(disk_prim).digest() == prim_snap_hash:
                            prim_source = disk_prim
                        else:
                            log.error("Snapshot primário em disco foi corrompido externamente! Usando buffer autenticado.")

                    bkp_source = orig_backup_bytes
                    if secondary_backup_path and secondary_backup_path.exists():
                        disk_bkp = secondary_backup_path.read_bytes()
                        if hashlib.sha256(disk_bkp).digest() == bkp_snap_hash:
                            bkp_source = disk_bkp
                        else:
                            log.error("Snapshot secundário em disco foi corrompido externamente! Usando buffer autenticado.")

                    rb_mbr = prim_source[:sector_size]
                    rb_prim_hdr = prim_source[sector_size : 2 * sector_size]
                    rb_prim_entries = prim_source[2 * sector_size :]

                    rb_bkp_entries = bkp_source[: entries_sectors * sector_size]
                    rb_bkp_hdr = bkp_source[entries_sectors * sector_size :]

                    f.seek(0)
                    f.write(rb_mbr)
                    f.seek(1 * sector_size)
                    f.write(rb_prim_hdr)
                    f.seek(gpt.partition_entries_lba * sector_size)
                    f.write(rb_prim_entries)
                    f.seek(bkp_entries_lba * sector_size)
                    f.write(rb_bkp_entries)
                    f.seek(gpt.backup_lba * sector_size)
                    f.write(rb_bkp_hdr)

                    f.flush()
                    os.fsync(f.fileno())

                    # Releitura e verificação integral de cada região restaurada
                    f.seek(0)
                    chk_mbr = f.read(sector_size)
                    f.seek(1 * sector_size)
                    chk_prim_hdr = f.read(sector_size)
                    f.seek(gpt.partition_entries_lba * sector_size)
                    chk_prim_entries = f.read(entries_sectors * sector_size)
                    f.seek(bkp_entries_lba * sector_size)
                    chk_bkp_entries = f.read(entries_sectors * sector_size)
                    f.seek(gpt.backup_lba * sector_size)
                    chk_bkp_hdr = f.read(sector_size)

                    return (
                        chk_mbr == orig_mbr_bytes
                        and chk_prim_hdr == orig_prim_hdr_bytes
                        and chk_prim_entries == orig_prim_entries_bytes
                        and chk_bkp_entries == orig_bkp_entries_bytes
                        and chk_bkp_hdr == orig_bkp_header_bytes
                    )
                except Exception as rb_exc:
                    log.error("Exceção durante rollback GPT: %s", rb_exc)
                    return False

            # 4. Gravação nas regiões primárias e de backup
            try:
                f.seek(1 * sector_size)
                w1 = f.write(new_header_bytes)
                if w1 != len(new_header_bytes):
                    raise OSError("Escrita incompleta no cabeçalho GPT primário.")

                f.seek(gpt.partition_entries_lba * sector_size)
                w2 = f.write(new_entries_bytes)
                if w2 != len(new_entries_bytes):
                    raise OSError("Escrita incompleta nas entradas GPT primárias.")

                # 5. Gravação da cópia de backup no fim do disco
                f.seek(bkp_entries_lba * sector_size)
                w3 = f.write(bkp_entries_bytes)
                if w3 != len(bkp_entries_bytes):
                    raise OSError("Escrita incompleta nas entradas GPT de backup.")

                f.seek(gpt.backup_lba * sector_size)
                w4 = f.write(bkp_header_bytes)
                if w4 != len(bkp_header_bytes):
                    raise OSError("Escrita incompleta no cabeçalho GPT de backup.")

                f.flush()
                os.fsync(f.fileno())
                write_completed = True

                # Verificação pós-escrita imediata em todas as regiões (incluindo preservação do MBR protetor)
                f.seek(0)
                read_mbr = f.read(sector_size)
                if read_mbr != orig_mbr_bytes:
                    raise OSError("Verificação pós-escrita falhou no MBR protetor (LBA 0).")

                f.seek(1 * sector_size)
                read_hdr = f.read(len(new_header_bytes))
                if read_hdr != new_header_bytes:
                    raise OSError("Verificação pós-escrita falhou no cabeçalho GPT primário.")

                f.seek(gpt.partition_entries_lba * sector_size)
                read_entries = f.read(len(new_entries_bytes))
                if read_entries != new_entries_bytes:
                    raise OSError("Verificação pós-escrita falhou nas entradas GPT primárias.")

                f.seek(bkp_entries_lba * sector_size)
                read_bkp_entries = f.read(len(bkp_entries_bytes))
                if read_bkp_entries != bkp_entries_bytes:
                    raise OSError("Verificação pós-escrita falhou nas entradas GPT de backup.")

                f.seek(gpt.backup_lba * sector_size)
                read_bkp_hdr = f.read(len(bkp_header_bytes))
                if read_bkp_hdr != bkp_header_bytes:
                    raise OSError("Verificação pós-escrita falhou no cabeçalho GPT de backup.")

                post_write_verified = True

            except Exception as write_err:
                log.error("Erro na gravação ou verificação GPT: %s", write_err)
                rollback_attempted = True
                rollback_confirmed = do_rollback()
                if rollback_confirmed:
                    return RepairResult(
                        success=False,
                        message=f"Falha na gravação/verificação ({write_err}). Rollback executado com sucesso e verificado.",
                        backup_file=primary_backup_path,
                        secondary_backup_file=secondary_backup_path,
                        old_start_lba=old_start,
                        old_end_lba=old_end,
                        new_start_lba=new_start,
                        new_end_lba=new_end,
                        write_completed=write_completed,
                        post_write_verified=False,
                        rollback_attempted=True,
                        rollback_confirmed=True,
                        final_state_uncertain=False,
                        structural_diff=structural_diff,
                        region_snapshots=region_snapshots,
                    )
                else:
                    return RepairResult(
                        success=False,
                        message=(
                            f"FALHA CRÍTICA: Gravação falhou ({write_err}) e rollback falhou na verificação/sincronização — "
                            f"estado final do disco é INCERTO. Restaure manualmente a partir de {primary_backup_path}."
                        ),
                        backup_file=primary_backup_path,
                        secondary_backup_file=secondary_backup_path,
                        old_start_lba=old_start,
                        old_end_lba=old_end,
                        new_start_lba=new_start,
                        new_end_lba=new_end,
                        write_completed=write_completed,
                        post_write_verified=False,
                        rollback_attempted=True,
                        rollback_confirmed=False,
                        final_state_uncertain=True,
                        structural_diff=structural_diff,
                        region_snapshots=region_snapshots,
                    )

        log.info("Tabela GPT primária e de backup corrigidas e verificadas com sucesso.")

        # 6. Etapa secundária isolada (Seção 9): Notificar o Windows para atualizar o cache de armazenamento.
        # Falha ou timeout aqui NUNCA invalida a gravação já verificada nem aciona rollback.
        cache_refresh_attempted = False
        cache_refresh_succeeded = True
        cache_refresh_warning = ""
        if os.name == "nt":
            cache_refresh_attempted = True
            try:
                proc = subprocess.run(
                    ["powershell", "-NoProfile", "-Command", "Update-HostStorageCache"],
                    capture_output=True,
                    text=True,
                    timeout=10,
                )
                if proc.returncode != 0:
                    cache_refresh_succeeded = False
                    stderr_txt = (proc.stderr or "").strip()
                    cache_refresh_warning = (
                        f"Gravação GPT concluída e verificada, mas Update-HostStorageCache retornou código {proc.returncode}"
                        + (f" ({stderr_txt})." if stderr_txt else ".")
                    )
                    log.warning(cache_refresh_warning)
            except Exception as cache_exc:
                cache_refresh_succeeded = False
                cache_refresh_warning = (
                    f"Gravação GPT concluída e verificada no disco, mas não foi possível atualizar o cache de "
                    f"armazenamento do Windows (Update-HostStorageCache: {cache_exc})."
                )
                log.warning(cache_refresh_warning)

        msg = f"Partição {target_partition_index} realinhada com sucesso para LBA {new_start:,}..{new_end:,} ({discovered_fs.size_gb:.1f} GB)."
        if cache_refresh_warning:
            msg = f"{msg} Aviso pós-gravação: {cache_refresh_warning}"

        return RepairResult(
            success=True,
            message=msg,
            backup_file=primary_backup_path,
            secondary_backup_file=secondary_backup_path,
            old_start_lba=old_start,
            old_end_lba=old_end,
            new_start_lba=new_start,
            new_end_lba=new_end,
            write_completed=write_completed,
            post_write_verified=post_write_verified,
            rollback_attempted=False,
            rollback_confirmed=False,
            final_state_uncertain=False,
            cache_refresh_attempted=cache_refresh_attempted,
            cache_refresh_succeeded=cache_refresh_succeeded,
            cache_refresh_warning=cache_refresh_warning,
            structural_diff=structural_diff,
            region_snapshots=region_snapshots,
        )

    except PermissionError:
        uncertain = opened_for_write and not post_write_verified and not rollback_confirmed
        return RepairResult(
            success=False,
            message="Permissão negada ao gravar no disco. Execute o terminal como Administrador/root.",
            backup_file=primary_backup_path,
            secondary_backup_file=secondary_backup_path,
            write_completed=write_completed,
            post_write_verified=post_write_verified,
            rollback_attempted=rollback_attempted,
            rollback_confirmed=rollback_confirmed,
            final_state_uncertain=uncertain,
            structural_diff=structural_diff,
            region_snapshots=region_snapshots,
        )
    except Exception as exc:
        log.error("Exceção ao reparar tabela GPT: %s", exc)
        uncertain = opened_for_write and not post_write_verified and not rollback_confirmed
        return RepairResult(
            success=False,
            message=f"Erro durante a operação: {exc}",
            backup_file=primary_backup_path,
            secondary_backup_file=secondary_backup_path,
            write_completed=write_completed,
            post_write_verified=post_write_verified,
            rollback_attempted=rollback_attempted,
            rollback_confirmed=rollback_confirmed,
            final_state_uncertain=uncertain,
            structural_diff=structural_diff,
            region_snapshots=region_snapshots,
        )


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
    if sector_size % 512 != 0 or sector_size > 65536:
        raise ValueError("sector_size deve ser múltiplo de 512 bytes (512..65536).")
    if chunk_sectors <= 0:
        raise ValueError("chunk_sectors deve ser estritamente positivo.")
    if max_sectors is not None and max_sectors <= 0:
        raise ValueError("max_sectors deve ser estritamente positivo quando fornecido.")

    chunk_bytes = chunk_sectors * sector_size
    if chunk_bytes > MAX_RAW_IMAGE_CHUNK_BYTES:
        raise ValueError(
            f"Tamanho do bloco ({chunk_bytes} bytes) excede o limite de memória permitido ({MAX_RAW_IMAGE_CHUNK_BYTES} bytes)."
        )

    output_path = Path(output_path)
    if output_path.exists() and output_path.is_dir():
        raise IsADirectoryError(f"O destino informado é um diretório ({output_path}), não um arquivo de imagem.")

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
        if output_path.exists() and temp_output_path.resolve() == output_path.resolve():
            raise ValueError("Colisão entre arquivo temporário de staging e destino final.")
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

                if max_sectors is not None and current_sector >= max_sectors:
                    break

                to_read = chunk_bytes
                if max_sectors is not None:
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
                    if max_sectors is not None and current_sector < max_sectors:
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
                    written = dst.write(buf)
                    if written != len(buf):
                        raise OSError(f"Escrita parcial no arquivo temporário ({written} de {len(buf)} bytes).")
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
                bytes_transferred += written

                now = time.time()
                if progress_cb and (now - last_cb_time >= 0.25):
                    speed = (bytes_transferred / (1024 * 1024)) / max(0.1, now - t_start)
                    progress_cb(current_sector, max_sectors or current_sector, speed)
                    last_cb_time = now

            if current_sector == 0 or bytes_transferred == 0:
                log.error("Nenhum setor lido da origem (%s).", device_path)
                result = RawImageResult(
                    status=RawImageStatus.PARTIAL,
                    sectors_written=0,
                    bytes_written=0,
                    total_sectors=max_sectors,
                    target_path=output_path,
                    message="Mídia de origem vazia: nenhum setor pôde ser lido."
                )
                return result

            # Flush e fsync de durabilidade física
            try:
                dst.flush()
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

        if (max_sectors is not None and current_sector < max_sectors) or (current_sector * sector_size != bytes_transferred):
            log.error("Cópia incompleta: apenas %d setores transferidos de %s.", current_sector, max_sectors)
            result = RawImageResult(
                status=RawImageStatus.PARTIAL,
                sectors_written=current_sector,
                bytes_written=bytes_transferred,
                total_sectors=max_sectors,
                target_path=output_path,
                message=f"Cópia incompleta: transferidos {current_sector} de {max_sectors} setores."
            )
            return result

        # Confirma tamanho real do arquivo temporário em disco antes de finalizar
        if temp_output_path.stat().st_size != bytes_transferred:
            result = RawImageResult(
                status=RawImageStatus.PERSISTENCE_ERROR,
                sectors_written=current_sector,
                bytes_written=bytes_transferred,
                total_sectors=max_sectors,
                target_path=output_path,
                message="Discrepância entre bytes transferidos e tamanho físico do arquivo de staging."
            )
            return result

        # Finalização atômica (sem sobrescrita se allow_overwrite=False; substituição explícita se allow_overwrite=True)
        try:
            _atomic_move_file(temp_output_path, output_path, allow_overwrite=allow_overwrite)
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

