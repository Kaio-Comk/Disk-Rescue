"""
core/recovery/carver.py — Motor forense de File Carving (recuperação de arquivos por assinaturas de bytes).
Extrai imagens, documentos e mídias diretamente de setores brutos com suporte a arquivos que atravessam blocos
(cross-block streaming carving), validação estrutural pós-extração e isolamento estrito de arquivos inválidos.
"""
from __future__ import annotations

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
        discard_invalid: bool = False
    ):
        self.device = device_path
        self.output_dir = Path(output_dir)
        self.discard_invalid = discard_invalid
        self.recovered_files: List[CarvedFile] = []
        self.corrupted_files: List[CarvedFile] = []
        self.cancel_requested = False

        # Validação de segurança P0: Impedir salvar no próprio disco ou partição sob análise
        if isinstance(device_path, (str, Path)):
            assert_safe_source_and_destination(str(device_path), self.output_dir, is_directory=True)

        self.output_dir.mkdir(parents=True, exist_ok=True)

    @property
    def valid_files(self) -> List[CarvedFile]:
        """Retorna exclusivamente os arquivos que passaram na validação estrutural."""
        return [f for f in self.recovered_files if f.is_structurally_valid]

    @staticmethod
    def verify_file_integrity(file_path: Path, kind: str) -> bool:
        """Valida a integridade estrutural após a extração."""
        try:
            if not file_path.exists() or file_path.stat().st_size == 0:
                return False

            if kind == "ZIP":
                import zipfile
                with zipfile.ZipFile(file_path, "r") as zf:
                    return zf.testzip() is None
            elif kind == "PDF":
                data = file_path.read_bytes()
                if not data.startswith(b"%PDF-"):
                    return False
                # Procura %%EOF nos últimos 4KB ou startxref
                tail = data[-4096:] if len(data) >= 4096 else data
                return b"%%EOF" in tail or b"startxref" in tail
            elif kind == "PNG":
                data = file_path.read_bytes()
                if not data.startswith(b"\x89PNG\r\n\x1a\n"):
                    return False
                tail = data[-64:] if len(data) >= 64 else data
                return b"IEND" in tail
            elif kind == "JPEG":
                data = file_path.read_bytes()
                return data.startswith(b"\xff\xd8\xff") and data.endswith(b"\xff\xd9")
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
        acumulando buffers até encontrar o delimitador de encerramento ou limite máximo.
        """
        stream.seek(start_offset)
        initial_chunk_size = 64 * 1024  # 64 KB por lote de leitura de arquivo
        accumulated = bytearray()
        footer_found_pos = -1

        while len(accumulated) < max_size and not self.cancel_requested:
            chunk = stream.read(min(initial_chunk_size, max_size - len(accumulated)))
            if not chunk:
                break
            accumulated.extend(chunk)

            # Procura footer
            idx = accumulated.find(footer_sig)
            if idx != -1:
                if kind == "PDF":
                    # PDFs podem ter múltiplos %%EOF devido a revisões; busca a última ocorrência no chunk
                    last_idx = accumulated.rfind(footer_sig)
                    # Verifica se há mais dados imediatamente após
                    footer_found_pos = last_idx + len(footer_sig)
                elif kind == "ZIP":
                    # O registro EOCD (PK\x05\x06) possui no mínimo 22 bytes
                    if len(accumulated) >= idx + 22:
                        comment_len = struct.unpack_from("<H", accumulated, idx + 20)[0]
                        footer_found_pos = min(len(accumulated), idx + 22 + comment_len)
                    else:
                        continue
                else:
                    footer_found_pos = idx + len(footer_sig)
                break

        if footer_found_pos == -1 or footer_found_pos < len(header_sig) + 4:
            # Assinatura falsa ou arquivo truncado sem footer válido
            return None

        file_bytes = bytes(accumulated[:footer_found_pos])
        file_len = len(file_bytes)
        file_lba = start_offset // SECTOR_SIZE

        # Salva em arquivo temporário para validação estrutural
        temp_name = f"recovered_{file_counter:04d}{ext}"
        candidate_path = self.output_dir / temp_name
        candidate_path.write_bytes(file_bytes)

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
                # Renomeia com sufixo _corrupted para não enganar o usuário
                corrupted_name = f"recovered_{file_counter:04d}_corrupted{ext}"
                corrupted_path = self.output_dir / corrupted_name
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

            while current_lba < max_sectors and not self.cancel_requested:
                chunk_start_byte = current_lba * SECTOR_SIZE
                stream.seek(chunk_start_byte)
                raw_chunk = stream.read(chunk_bytes)
                if not raw_chunk:
                    break

                chunk_len = len(raw_chunk)
                pos = 0

                while pos < chunk_len and not self.cancel_requested:
                    earliest_sig = None
                    earliest_idx = -1

                    # Localiza o cabeçalho mais próximo no buffer atual
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
                    file_start_byte = chunk_start_byte + earliest_idx
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
                        # Avança o cursor além do arquivo extraído para otimizar
                        pos = earliest_idx + max(len(header_sig), carved.size_bytes)
                        # Re-sincroniza stream para onde estávamos
                        stream.seek(chunk_start_byte + min(pos, chunk_len))
                    else:
                        pos = earliest_idx + len(header_sig)

                current_lba += (chunk_len // SECTOR_SIZE)
                if progress_cb:
                    progress_cb(current_lba, max_sectors, len(self.valid_files))

        except Exception as exc:
            log.error("Erro durante File Carving: %s", exc)
        finally:
            if should_close:
                try:
                    stream.close()
                except Exception:
                    pass

        return self.valid_files if self.discard_invalid else self.recovered_files
