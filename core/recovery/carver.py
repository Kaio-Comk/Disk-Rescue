"""
core/recovery/carver.py — Motor forense de File Carving (recuperação de arquivos por assinaturas de bytes).
Extrai imagens, documentos e mídias diretamente de setores brutos mesmo com partições destruídas.
"""
from __future__ import annotations

import logging
import os
import struct
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, List, Optional

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
    # Assinaturas de início de arquivo
    SIGNATURES = [
        ("JPEG", ".jpg", b"\xff\xd8\xff", b"\xff\xd9", 30 * 1024 * 1024),          # max 30MB
        ("PNG", ".png", b"\x89PNG\r\n\x1a\n", b"IEND\xaeB`\x82", 50 * 1024 * 1024),# max 50MB
        ("PDF", ".pdf", b"%PDF-", b"%%EOF", 100 * 1024 * 1024),                    # max 100MB
        ("ZIP", ".zip", b"PK\x03\x04", b"PK\x05\x06", 500 * 1024 * 1024),         # max 500MB
    ]

    def __init__(self, device_path: str, output_dir: Path):
        self.device_path = device_path
        self.output_dir = Path(output_dir)
        self.recovered_files: List[CarvedFile] = []
        self.cancel_requested = False

        # Validação de segurança P0: Impedir salvar no próprio disco sob análise
        clean_dev = device_path.replace("\\\\.\\", "").replace("\\", "/").strip().lower()
        clean_out = str(self.output_dir.resolve()).replace("\\", "/").lower()
        if clean_dev and (clean_dev == clean_out or clean_out.startswith(clean_dev)):
            raise ValueError(f"A pasta de destino do carving não pode ser o dispositivo de origem: {device_path}")

        self.output_dir.mkdir(parents=True, exist_ok=True)

    @staticmethod
    def verify_file_integrity(file_path: Path, kind: str) -> bool:
        """Valida a integridade estrutural após a extração."""
        try:
            if kind == "ZIP":
                import zipfile
                with zipfile.ZipFile(file_path, "r") as zf:
                    return zf.testzip() is None
            elif kind == "PDF":
                data = file_path.read_bytes()
                return data.startswith(b"%PDF-") and (b"%%EOF" in data[-1024:] or b"startxref" in data[-2048:])
            elif kind == "PNG":
                data = file_path.read_bytes()
                return data.startswith(b"\x89PNG\r\n\x1a\n") and b"IEND" in data[-32:]
            elif kind == "JPEG":
                data = file_path.read_bytes()
                return data.startswith(b"\xff\xd8\xff") and data.endswith(b"\xff\xd9")
            return True
        except Exception:
            return False

    def cancel(self):
        self.cancel_requested = True

    def scan(
        self,
        max_sectors: int | None = None,
        chunk_sectors: int = 2048,  # 1MB por lote
        progress_cb: Optional[Callable[[int, int, int], None]] = None  # (cur_lba, total_lba, count_found)
    ) -> List[CarvedFile]:
        """Varre o dispositivo físico setor a setor extraindo arquivos íntegros."""
        chunk_bytes = chunk_sectors * SECTOR_SIZE
        current_lba = 0
        file_counter = 0

        try:
            with open(self.device_path, "rb") as f:
                # Determina tamanho total se não fornecido
                if not max_sectors:
                    f.seek(0, os.SEEK_END)
                    max_sectors = f.tell() // SECTOR_SIZE
                    f.seek(0)

                overlap_buffer = b""

                while current_lba < max_sectors and not self.cancel_requested:
                    f.seek(current_lba * SECTOR_SIZE)
                    raw_chunk = f.read(chunk_bytes)
                    if not raw_chunk:
                        break

                    data = overlap_buffer + raw_chunk
                    base_lba = current_lba - (len(overlap_buffer) // SECTOR_SIZE)

                    # Varre procurando cada assinatura mágica
                    for kind, ext, header_sig, footer_sig, max_size in self.SIGNATURES:
                        pos = 0
                        while True:
                            idx = data.find(header_sig, pos)
                            if idx == -1:
                                break

                            # Alinhamento típico de setor (setor de 512 bytes)
                            file_lba = base_lba + (idx // SECTOR_SIZE)
                            pos = idx + len(header_sig)

                            # Procura delimitador de final
                            footer_idx = data.find(footer_sig, pos)
                            if footer_idx != -1:
                                file_len = (footer_idx + len(footer_sig)) - idx
                                if 32 <= file_len <= max_size:
                                    file_counter += 1
                                    out_name = f"recovered_{file_counter:04d}{ext}"
                                    out_file = self.output_dir / out_name
                                    out_file.write_bytes(data[idx : idx + file_len])

                                    is_valid = self.verify_file_integrity(out_file, kind)
                                    carved = CarvedFile(
                                        file_id=file_counter,
                                        file_type=kind,
                                        extension=ext,
                                        start_lba=file_lba,
                                        size_bytes=file_len,
                                        saved_path=out_file,
                                        is_structurally_valid=is_valid
                                    )
                                    self.recovered_files.append(carved)
                                    log.info("Arquivo resgatado: %s (%s, %d bytes, íntegro=%s) no LBA %d", out_name, kind, file_len, is_valid, file_lba)
                                    pos = footer_idx + len(footer_sig)

                    # Guarda os últimos 4KB para não perder cabeçalhos partidos entre chunks
                    overlap_size = min(4096, len(raw_chunk))
                    overlap_buffer = raw_chunk[-overlap_size:]
                    current_lba += (len(raw_chunk) // SECTOR_SIZE)

                    if progress_cb:
                        progress_cb(current_lba, max_sectors, len(self.recovered_files))

        except Exception as exc:
            log.error("Erro durante File Carving: %s", exc)

        return self.recovered_files
