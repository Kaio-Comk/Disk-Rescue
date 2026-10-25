"""
core/recovery/carver.py — Motor forense de File Carving (recuperação de arquivos por assinaturas de bytes).
Extrai imagens, documentos e mídias diretamente de setores brutos com suporte a arquivos que atravessam blocos
(cross-block streaming carving), validação estrutural pós-extração e isolamento estrito de arquivos inválidos.
"""
from __future__ import annotations

import io
import logging
import os
import struct
import time
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO, Callable, List, Optional, Tuple, Union

from core.hardware.device_resolver import assert_safe_source_and_destination

log = logging.getLogger("diskrescue.carver")

SECTOR_SIZE = 512


@dataclass
class CarvedFile:
    file_id: int
    file_type: str
    extension: str
    start_lba: int
    size_bytes: int
    saved_path: Path
    is_structurally_valid: bool = True


class FileCarver:
    # Assinaturas de início e fim de arquivo (Header, Footer, Max Size)
    SIGNATURES = [
        ("JPEG", ".jpg", b"\xff\xd8\xff", b"\xff\xd9", 30 * 1024 * 1024),            # max 30MB
        ("PNG", ".png", b"\x89PNG\r\n\x1a\n", b"IEND\xaeB`\x82", 50 * 1024 * 1024),   # max 50MB
        ("PDF", ".pdf", b"%PDF-", b"%%EOF", 100 * 1024 * 1024),                      # max 100MB
        ("ZIP", ".zip", b"PK\x03\x04", b"PK\x05\x06", 500 * 1024 * 1024),           # max 500MB
    ]

    def __init__(
        self,
        device_path: Union[str, Path, BinaryIO],
        output_dir: Path,
        discard_invalid: bool = False,
        source_identifier: Optional[str] = None
    ):
        self.device = device_path
        self.output_dir = Path(output_dir)
        self.discard_invalid = discard_invalid
        self.recovered_files: List[CarvedFile] = []
        self.corrupted_files: List[CarvedFile] = []
        self.cancel_requested = False

        # Validação de segurança P0: Identificar a origem e impedir salvar no próprio disco sob análise
        resolved_source_id = None
        if isinstance(device_path, (str, Path)):
            resolved_source_id = str(device_path)
        elif source_identifier:
            resolved_source_id = str(source_identifier)
        elif hasattr(device_path, "name") and isinstance(device_path.name, (str, bytes)):
            resolved_source_id = str(device_path.name)
        elif hasattr(device_path, "_underlying") and hasattr(device_path._underlying, "name") and isinstance(device_path._underlying.name, (str, bytes)):
            resolved_source_id = str(device_path._underlying.name)

        if resolved_source_id:
            assert_safe_source_and_destination(resolved_source_id, self.output_dir, is_directory=True)
        else:
            # Apenas fluxos binários comprovadamente sintéticos em memória são isentos de identificador
            is_synthetic_bytes = isinstance(device_path, io.BytesIO)
            if not is_synthetic_bytes and hasattr(device_path, "_underlying"):
                underlying = getattr(device_path, "_underlying")
                if isinstance(underlying, (io.BytesIO, bytes, bytearray)):
                    is_synthetic_bytes = True

            if not is_synthetic_bytes:
                raise ValueError(
                    "BLOQUEIO FAIL-CLOSED: O FileCarver recebeu um fluxo binário sem identificador de origem comprovável "
                    "(source_identifier). Para prevenir gravação acidental no disco de origem, informe o caminho do dispositivo."
                )

        self.output_dir.mkdir(parents=True, exist_ok=True)

    @property
    def valid_files(self) -> List[CarvedFile]:
        """Retorna exclusivamente os arquivos que passaram na validação estrutural."""
        return [f for f in self.recovered_files if f.is_structurally_valid]

    @staticmethod
    def verify_file_integrity(file_path: Path, kind: str) -> bool:
        """Validação profunda e estrutural pós-extração (chunks/CRCs para PNG, segmentos para JPEG, tabelas para PDF, CRC para ZIP)."""
        try:
            if not file_path.exists():
                return False
            size = file_path.stat().st_size
            if size == 0:
                return False

            if kind == "ZIP":
                import zipfile
                with zipfile.ZipFile(file_path, "r") as zf:
                    return zf.testzip() is None

            elif kind == "PNG":
                if size < 33:
                    return False
                import zlib
                with open(file_path, "rb") as f:
                    header = f.read(8)
                    if header != b"\x89PNG\r\n\x1a\n":
                        return False
                    has_ihdr = False
                    has_iend = False
                    while True:
                        len_b = f.read(4)
                        if not len_b or len(len_b) < 4:
                            break
                        c_len = struct.unpack(">I", len_b)[0]
                        c_type = f.read(4)
                        if len(c_type) < 4:
                            return False
                        if not has_ihdr:
                            if c_type != b"IHDR" or c_len != 13:
                                return False
                            has_ihdr = True
                        c_data = f.read(c_len)
                        if len(c_data) < c_len:
                            return False
                        crc_b = f.read(4)
                        if len(crc_b) < 4:
                            return False
                        exp_crc = struct.unpack(">I", crc_b)[0]
                        calc_crc = zlib.crc32(c_type + c_data) & 0xffffffff
                        if calc_crc != exp_crc:
                            return False
                        if c_type == b"IEND":
                            has_iend = True
                            break
                    return has_ihdr and has_iend

            elif kind == "JPEG":
                if size < 4:
                    return False
                with open(file_path, "rb") as f:
                    header = f.read(2)
                    if header != b"\xff\xd8":
                        return False
                    has_valid_marker = False
                    while True:
                        b = f.read(1)
                        if not b:
                            break
                        if b != b"\xff":
                            continue
                        while b == b"\xff":
                            b = f.read(1)
                        if not b:
                            break
                        m = b[0]
                        if m == 0xd9:  # EOI
                            break
                        if m in (0xd8, 0x01) or (0xd0 <= m <= 0xd7):
                            continue
                        len_b = f.read(2)
                        if len(len_b) < 2:
                            return False
                        seg_l = struct.unpack(">H", len_b)[0]
                        if seg_l < 2:
                            return False
                        has_valid_marker = True
                        if m in (0xc0, 0xc2):
                            sof_d = f.read(seg_l - 2)
                            if len(sof_d) >= 5:
                                h, w = struct.unpack(">HH", sof_d[1:5])
                                if h == 0 or w == 0:
                                    return False
                            continue
                        if m == 0xda:  # SOS
                            break
                        f.seek(seg_l - 2, os.SEEK_CUR)

                    f.seek(-2, os.SEEK_END)
                    return has_valid_marker and f.read(2) == b"\xff\xd9"

            elif kind == "PDF":
                if size < 32:
                    return False
                with open(file_path, "rb") as f:
                    header = f.read(8)
                    if not header.startswith(b"%PDF-"):
                        return False
                    tail_len = min(4096, size)
                    f.seek(size - tail_len)
                    tail = f.read(tail_len)
                    if b"%%EOF" not in tail:
                        return False
                    if b"startxref" not in tail:
                        return b"xref" in tail or b"trailer" in tail
                    idx = tail.rfind(b"startxref")
                    after_sx = tail[idx + 9:].strip()
                    tokens = after_sx.split()
                    if not tokens:
                        return False
                    try:
                        x_off = int(tokens[0])
                    except ValueError:
                        return False
                    if x_off < 0 or x_off >= size:
                        return False
                    f.seek(x_off)
                    lead = f.read(32)
                    return b"xref" in lead or b"/XRef" in lead or b"obj" in lead or b"trailer" in lead

            return True
        except Exception:
            return False

    def cancel(self):
        self.cancel_requested = True

    def _extract_cross_block_file(
        self,
        stream: BinaryIO,
        start_offset: int,
        kind: str,
        ext: str,
        header_sig: bytes,
        footer_sig: bytes,
        max_size: int,
        file_counter: int
    ) -> Optional[CarvedFile]:
        """
        Localiza o final do arquivo e o extrai através de múltiplos blocos e fronteiras,
        escrevendo chunks diretamente em disco (streaming spooling com consumo O(1) de memória),
        mantendo um buffer deslizante de até 128 KB para detecção precisa do footer delimitador.
        """
        temp_name = f"recovered_{file_counter:04d}{ext}"
        candidate_path = self.output_dir / temp_name
        collision_counter = 1
        while candidate_path.exists():
            candidate_path = self.output_dir / f"recovered_{file_counter:04d}_{collision_counter}{ext}"
            collision_counter += 1

        stream.seek(start_offset)
        chunk_size = 64 * 1024  # 64 KB por lote
        total_written = 0
        rolling_tail = bytearray()
        footer_pos_in_file = -1
        found_footer = False

        write_failed_exc: Optional[Exception] = None
        try:
            with open(candidate_path, "wb") as out_f:
                while total_written < max_size and not self.cancel_requested:
                    to_read = min(chunk_size, max_size - total_written)
                    chunk = stream.read(to_read)
                    if not chunk:
                        break

                    try:
                        out_f.write(chunk)
                    except OSError as w_err:
                        write_failed_exc = w_err
                        raise
                    prev_total = total_written
                    total_written += len(chunk)

                    combined = rolling_tail + chunk
                    combined_base = prev_total - len(rolling_tail)

                    # Busca footer no buffer deslizante
                    idx = combined.find(footer_sig)
                    if idx != -1:
                        if kind == "PDF":
                            # PDFs podem ter revisões incrementais; procura a última ocorrência no buffer
                            last_idx = combined.rfind(footer_sig)
                            footer_pos_in_file = combined_base + last_idx + len(footer_sig)
                            found_footer = True
                            break
                        elif kind == "ZIP":
                            # O registro EOCD (PK\x05\x06) possui no mínimo 22 bytes
                            if len(combined) >= idx + 22:
                                comment_len = struct.unpack_from("<H", combined, idx + 20)[0]
                                target_end = combined_base + idx + 22 + comment_len
                                if total_written < target_end and total_written < max_size:
                                    needed = min(target_end - total_written, max_size - total_written)
                                    extra = stream.read(needed)
                                    if extra:
                                        try:
                                            out_f.write(extra)
                                        except OSError as w_err:
                                            write_failed_exc = w_err
                                            raise
                                        total_written += len(extra)
                                footer_pos_in_file = min(total_written, target_end)
                                found_footer = True
                                break
                            else:
                                # Registro cortado na fronteira, avança para próximo chunk
                                rolling_tail = combined[-128 * 1024:] if len(combined) > 128 * 1024 else combined
                                continue
                        else:
                            footer_pos_in_file = combined_base + idx + len(footer_sig)
                            found_footer = True
                            break

                    rolling_tail = combined[-128 * 1024:] if len(combined) > 128 * 1024 else combined

                if self.cancel_requested:
                    found_footer = False
                elif found_footer and footer_pos_in_file >= len(header_sig) + 4:
                    try:
                        out_f.flush()
                        out_f.truncate(footer_pos_in_file)
                    except OSError as w_err:
                        write_failed_exc = w_err
                        raise
                else:
                    found_footer = False

        except OSError as os_exc:
            if write_failed_exc is not None or not candidate_path.exists():
                try:
                    candidate_path.unlink(missing_ok=True)
                except Exception:
                    pass
                log.error("Erro de gravação no destino durante extração de %s: %s", temp_name, os_exc)
                raise
            log.warning("Erro de leitura durante extração de candidato %s: %s", temp_name, os_exc)
            found_footer = False
        except Exception as exc:
            log.warning("Erro durante extração em disco de candidato %s: %s", temp_name, exc)
            found_footer = False

        if not found_footer or footer_pos_in_file < len(header_sig) + 4:
            try:
                candidate_path.unlink(missing_ok=True)
            except Exception:
                pass
            return None

        file_len = footer_pos_in_file
        file_lba = start_offset // SECTOR_SIZE

        is_valid = self.verify_file_integrity(candidate_path, kind)

        if not is_valid:
            if self.discard_invalid:
                try:
                    candidate_path.unlink(missing_ok=True)
                except Exception:
                    pass
                log.info("Descartado arquivo inválido/corrompido no LBA %d (%s, %d bytes)", file_lba, kind, file_len)
                return None
            else:
                # Renomeia com sufixo _corrupted para não enganar o usuário sem sobrescrever existentes
                corrupted_name = f"recovered_{file_counter:04d}_corrupted{ext}"
                corrupted_path = self.output_dir / corrupted_name
                collision_counter = 1
                while corrupted_path.exists():
                    corrupted_path = self.output_dir / f"recovered_{file_counter:04d}_{collision_counter}_corrupted{ext}"
                    collision_counter += 1
                candidate_path.replace(corrupted_path)
                candidate_path = corrupted_path

        carved = CarvedFile(
            file_id=file_counter,
            file_type=kind,
            extension=ext,
            start_lba=file_lba,
            size_bytes=file_len,
            saved_path=candidate_path,
            is_structurally_valid=is_valid
        )

        if is_valid:
            self.recovered_files.append(carved)
            log.info("Arquivo íntegro resgatado: %s (%s, %d bytes) no LBA %d", candidate_path.name, kind, file_len, file_lba)
        else:
            self.corrupted_files.append(carved)
            log.warning("Arquivo recuperado com corrupção estrutural: %s (%s, %d bytes)", candidate_path.name, kind, file_len)

        return carved

    def scan(
        self,
        max_sectors: int | None = None,
        chunk_sectors: int = 2048,  # 1MB por lote de busca inicial
        progress_cb: Optional[Callable[[int, int, int], None]] = None  # (cur_lba, total_lba, valid_count)
    ) -> List[CarvedFile]:
        """Varre o dispositivo físico setor a setor extraindo arquivos íntegros com suporte a multi-bloco."""
        if chunk_sectors <= 0 or chunk_sectors > 65536:
            raise ValueError(
                f"chunk_sectors deve ser estritamente positivo e razoável (1..65536 setores). Recebido: {chunk_sectors}"
            )
        chunk_bytes = chunk_sectors * SECTOR_SIZE
        current_lba = 0
        file_counter = 0

        # Gerenciamento de abertura de stream de leitura
        should_close = False
        if isinstance(self.device, (str, Path)):
            stream = open(str(self.device), "rb")
            should_close = True
        else:
            stream = self.device

        try:
            # Determina tamanho total se não fornecido
            if not max_sectors:
                stream.seek(0, os.SEEK_END)
                max_sectors = stream.tell() // SECTOR_SIZE
                stream.seek(0)

            overlap = b""
            MAX_HEADER_OVERLAP = 16
            last_extracted_offset = -1

            while current_lba < max_sectors and not self.cancel_requested:
                chunk_start_byte = current_lba * SECTOR_SIZE
                stream.seek(chunk_start_byte)
                new_bytes = stream.read(chunk_bytes)
                if not new_bytes:
                    break

                overlap_len = len(overlap)
                raw_chunk = overlap + new_bytes
                chunk_len = len(raw_chunk)
                pos = 0

                while pos < chunk_len and not self.cancel_requested:
                    earliest_sig = None
                    earliest_idx = -1

                    # Localiza o cabeçalho mais próximo no buffer atual (incluindo sobreposição com o bloco anterior)
                    for kind, ext, header_sig, footer_sig, max_size in self.SIGNATURES:
                        idx = raw_chunk.find(header_sig, pos)
                        if idx != -1:
                            if earliest_idx == -1 or idx < earliest_idx:
                                earliest_idx = idx
                                earliest_sig = (kind, ext, header_sig, footer_sig, max_size)

                    if earliest_idx == -1 or earliest_sig is None:
                        # Nenhum cabeçalho restante neste chunk
                        break

                    kind, ext, header_sig, footer_sig, max_size = earliest_sig
                    file_start_byte = (chunk_start_byte - overlap_len) + earliest_idx

                    # Previne re-extração se o início estiver na zona de sobreposição e já tiver sido processado
                    if file_start_byte <= last_extracted_offset:
                        pos = earliest_idx + len(header_sig)
                        continue

                    file_counter += 1

                    # Executa extração com acumulação contínua além da fronteira do bloco
                    carved = self._extract_cross_block_file(
                        stream=stream,
                        start_offset=file_start_byte,
                        kind=kind,
                        ext=ext,
                        header_sig=header_sig,
                        footer_sig=footer_sig,
                        max_size=max_size,
                        file_counter=file_counter
                    )

                    if carved:
                        last_extracted_offset = file_start_byte
                        # Avança o cursor além do arquivo extraído para otimizar
                        pos = earliest_idx + max(len(header_sig), carved.size_bytes)
                        # Re-sincroniza stream para onde estávamos
                        stream.seek(chunk_start_byte + min(max(0, pos - overlap_len), len(new_bytes)))
                    else:
                        pos = earliest_idx + len(header_sig)

                # Mantém buffer de sobreposição dos últimos bytes para não perder assinaturas divididas na fronteira
                overlap = raw_chunk[-MAX_HEADER_OVERLAP:] if len(raw_chunk) >= MAX_HEADER_OVERLAP else raw_chunk
                current_lba += (len(new_bytes) // SECTOR_SIZE)
                if progress_cb:
                    progress_cb(current_lba, max_sectors, len(self.valid_files))

        except Exception as exc:
            log.error("Erro durante File Carving: %s", exc)
            raise
        finally:
            if should_close:
                try:
                    stream.close()
                except Exception:
                    pass

        return self.valid_files if self.discard_invalid else self.recovered_files
