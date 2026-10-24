"""
tests/test_carver.py — Testes unitários para o motor forense de File Carving.
"""
from pathlib import Path

from core.recovery.carver import FileCarver, SECTOR_SIZE


def test_carver_detects_and_extracts_files(tmp_path: Path):
    disk_file = tmp_path / "raw_partitionless_disk.img"
    total_sectors = 500

    # Cria disco vazio
    with open(disk_file, "wb") as f:
        f.seek(total_sectors * SECTOR_SIZE - 1)
        f.write(b"\x00")

    # 1. Monta um JPEG sintético no LBA 50
    jpeg_payload = b"\xff\xd8\xff\xe0\x00\x10JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00" + (b"IMAGE_PIXELS_" * 20) + b"\xff\xd9"
    
    # 2. Monta um PDF sintético no LBA 150
    pdf_payload = b"%PDF-1.7\n1 0 obj\n<< /Type /Catalog >>\nendobj\nxref\ntrailer\n<< /Root 1 0 R >>\nstartxref\n99\n%%EOF"

    with open(disk_file, "r+b") as f:
        f.seek(50 * SECTOR_SIZE)
        f.write(jpeg_payload)

        f.seek(150 * SECTOR_SIZE)
        f.write(pdf_payload)

    # 3. Executa o File Carver
    out_dir = tmp_path / "carved_output"
    carver = FileCarver(str(disk_file), out_dir)
    recovered = carver.scan(max_sectors=total_sectors, chunk_sectors=100)

    assert len(recovered) == 2

    # Verifica o JPEG
    jpeg_carved = next(c for c in recovered if c.file_type == "JPEG")
    assert jpeg_carved.extension == ".jpg"
    assert jpeg_carved.start_lba == 50
    assert jpeg_carved.saved_path.exists()
    assert jpeg_carved.saved_path.read_bytes() == jpeg_payload

    # Verifica o PDF
    pdf_carved = next(c for c in recovered if c.file_type == "PDF")
    assert pdf_carved.extension == ".pdf"
    assert pdf_carved.start_lba == 150
    assert pdf_carved.saved_path.exists()
    assert pdf_carved.saved_path.read_bytes() == pdf_payload
