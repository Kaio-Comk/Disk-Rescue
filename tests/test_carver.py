"""
tests/test_carver.py — Testes unitários para o motor forense de File Carving.
Comprova extração de arquivos maiores que 1 MiB, travessia de fronteiras de blocos,
e descarte/identificação de assinaturas falsas ou arquivos truncados.
"""
import struct
import zipfile
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
    
    # 2. Monta um PDF sintético no LBA 150 com startxref apontando exatamente para xref
    pdf_body = b"%PDF-1.7\n1 0 obj\n<< /Type /Catalog >>\nendobj\n"
    xref_offset = len(pdf_body)
    pdf_payload = pdf_body + b"xref\ntrailer\n<< /Root 1 0 R >>\nstartxref\n" + str(xref_offset).encode() + b"\n%%EOF"

    with open(disk_file, "r+b") as f:
        f.seek(50 * SECTOR_SIZE)
        f.write(jpeg_payload)

        f.seek(150 * SECTOR_SIZE)
        f.write(pdf_payload)

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


def test_carver_cross_block_large_file_over_1mb(tmp_path: Path):
    """
    P1 Teste: Arquivo com mais de 1 MiB (1.5 MiB) cujo início e fim atravessam múltiplos blocos.
    O bloco de busca padrão (chunk_sectors=1000 = ~500 KB) é ultrapassado pelo tamanho do arquivo.
    """
    disk_file = tmp_path / "disk_large_file.img"
    total_sectors = 5000  # ~2.5 MB

    # Constrói um PDF válido com 1.5 MiB de tamanho e startxref exato
    header = b"%PDF-1.4\n1 0 obj\n<< /Type /Catalog >>\nendobj\n"
    padding_needed = (1500 * 1024) - len(header) - 80
    body = header + (b"CONTENT_STREAM_CHUNK\n" * (padding_needed // 21))
    xref_offset = len(body) + 1
    footer = b"\nxref\ntrailer\n<< /Root 1 0 R >>\nstartxref\n" + str(xref_offset).encode() + b"\n%%EOF"
    pdf_payload = body + footer

    assert len(pdf_payload) >= 1.4 * 1024 * 1024  # > 1.4 MB

    with open(disk_file, "wb") as f:
        f.seek(total_sectors * SECTOR_SIZE - 1)
        f.write(b"\x00")

    # Grava o PDF começando no LBA 200
    with open(disk_file, "r+b") as f:
        f.seek(200 * SECTOR_SIZE)
        f.write(pdf_payload)

    out_dir = tmp_path / "carved_large_out"
    carver = FileCarver(str(disk_file), out_dir)
    # chunk_sectors=1000 (~500 KB) força o arquivo a atravessar 3 blocos inteiros
    recovered = carver.scan(max_sectors=total_sectors, chunk_sectors=1000)

    assert len(recovered) == 1
    carved_pdf = recovered[0]
    assert carved_pdf.file_type == "PDF"
    assert carved_pdf.start_lba == 200
    assert carved_pdf.size_bytes == len(pdf_payload)
    assert carved_pdf.is_structurally_valid is True
    assert carved_pdf.saved_path.read_bytes() == pdf_payload


def test_carver_file_crossing_boundary_over_4kb(tmp_path: Path):
    """
    P1 Teste: Arquivo JPEG com início no final de um bloco e fim no bloco seguinte,
    com tamanho superior a 4 KiB (superando o antigo buffer de sobreposição fixo de 4KB).
    """
    disk_file = tmp_path / "disk_crossing.img"
    total_sectors = 1000
    chunk_sectors = 100  # 50 KB por chunk

    # Arquivo JPEG de ~17 KiB
    jpeg_header = b"\xff\xd8\xff\xe0\x00\x10JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00"
    jpeg_footer = b"\xff\xd9"
    filler = b"PIXEL_DATA_RGB_" * 1200  # ~18 KB
    jpeg_payload = jpeg_header + filler + jpeg_footer
    assert len(jpeg_payload) > 15 * 1024  # > 15 KB (muito maior que 4KB)

    with open(disk_file, "wb") as f:
        f.seek(total_sectors * SECTOR_SIZE - 1)
        f.write(b"\x00")

    # Grava no LBA 95. Com chunk_sectors=100, o chunk 0 vai de 0..99 (termina no LBA 99).
    # O arquivo se estende do LBA 95 até além do LBA 125, atravessando a fronteira do bloco!
    with open(disk_file, "r+b") as f:
        f.seek(95 * SECTOR_SIZE)
        f.write(jpeg_payload)

    out_dir = tmp_path / "carved_crossing_out"
    carver = FileCarver(str(disk_file), out_dir)
    recovered = carver.scan(max_sectors=total_sectors, chunk_sectors=chunk_sectors)

    assert len(recovered) == 1
    carved_jpeg = recovered[0]
    assert carved_jpeg.file_type == "JPEG"
    assert carved_jpeg.start_lba == 95
    assert carved_jpeg.size_bytes == len(jpeg_payload)
    assert carved_jpeg.is_structurally_valid is True
    assert carved_jpeg.saved_path.read_bytes() == jpeg_payload


def test_carver_handles_false_signatures_and_truncated_files(tmp_path: Path):
    """
    P1 Teste: Mídia com assinaturas falsas, arquivos truncados e arquivo válido.
    Garante que assinaturas órfãs/truncadas não sejam declaradas como arquivos válidos resgatados.
    """
    disk_file = tmp_path / "disk_corrupt_signatures.img"
    total_sectors = 1000

    with open(disk_file, "wb") as f:
        f.seek(total_sectors * SECTOR_SIZE - 1)
        f.write(b"\x00")

    # 1. Assinatura falsa no LBA 50 (cabeçalho JPEG órfão sem footer, seguido de lixo aleatório)
    fake_jpeg_header = b"\xff\xd8\xff\x00" + b"RANDOM_GARBAGE_WITHOUT_FOOTER_" * 50

    # 2. Arquivo truncado no LBA 150 (cabeçalho PDF sem %%EOF e sem estrutura)
    truncated_pdf = b"%PDF-1.4\n1 0 obj\n<< incomplete truncated data without trailer"

    # 3. Arquivo ZIP íntegro no LBA 300
    valid_zip_data = bytearray()
    zip_buffer_path = tmp_path / "sample.zip"
    with zipfile.ZipFile(zip_buffer_path, "w") as zf:
        zf.writestr("relatorio.txt", "Dados vitais resgatados com sucesso.")
    valid_zip_payload = zip_buffer_path.read_bytes()

    with open(disk_file, "r+b") as f:
        f.seek(50 * SECTOR_SIZE)
        f.write(fake_jpeg_header)

        f.seek(150 * SECTOR_SIZE)
        f.write(truncated_pdf)

        f.seek(300 * SECTOR_SIZE)
        f.write(valid_zip_payload)

    # Execução 1: Com discard_invalid=True (somente arquivos válidos são mantidos no resultado)
    out_dir_clean = tmp_path / "carved_clean_out"
    carver_clean = FileCarver(str(disk_file), out_dir_clean, discard_invalid=True)
    clean_recovered = carver_clean.scan(max_sectors=total_sectors, chunk_sectors=100)

    # Apenas o ZIP válido deve ser retornado
    assert len(clean_recovered) == 1
    assert clean_recovered[0].file_type == "ZIP"
    assert clean_recovered[0].is_structurally_valid is True

    # Execução 2: Com discard_invalid=False (arquivos corrompidos são isolados com _corrupted)
    out_dir_all = tmp_path / "carved_all_out"
    carver_all = FileCarver(str(disk_file), out_dir_all, discard_invalid=False)
    all_recovered = carver_all.scan(max_sectors=total_sectors, chunk_sectors=100)

    # Arquivos válidos devem continuar contendo apenas o ZIP
    assert len(carver_all.valid_files) == 1
    assert carver_all.valid_files[0].file_type == "ZIP"


def test_carver_extracts_png_with_ihdr_and_iend(tmp_path: Path):
    """P1 Teste: Extração e validação estrutural profunda de arquivo PNG com chunks reais e CRCs calculados."""
    import zlib
    disk_file = tmp_path / "disk_with_png.img"
    total_sectors = 200

    with open(disk_file, "wb") as f:
        f.seek(total_sectors * SECTOR_SIZE - 1)
        f.write(b"\x00")

    # Monta um PNG válido no LBA 25 com CRCs matematicamente autênticos
    header = b"\x89PNG\r\n\x1a\n"
    ihdr_data = b"\x00\x00\x00\x20\x00\x00\x00\x20\x08\x02\x00\x00\x00"  # 32x32 pixels, 8-bit truecolor
    ihdr_crc = struct.pack(">I", zlib.crc32(b"IHDR" + ihdr_data) & 0xffffffff)
    ihdr_chunk = struct.pack(">I", 13) + b"IHDR" + ihdr_data + ihdr_crc

    idat_data = (b"COMPRESSED_IMAGE_PIXELS_" * 2)[:64]
    idat_crc = struct.pack(">I", zlib.crc32(b"IDAT" + idat_data) & 0xffffffff)
    idat_chunk = struct.pack(">I", len(idat_data)) + b"IDAT" + idat_data + idat_crc

    iend_crc = struct.pack(">I", zlib.crc32(b"IEND") & 0xffffffff)
    iend_chunk = struct.pack(">I", 0) + b"IEND" + iend_crc
    png_payload = header + ihdr_chunk + idat_chunk + iend_chunk

    with open(disk_file, "r+b") as f:
        f.seek(25 * SECTOR_SIZE)
        f.write(png_payload)

    out_dir = tmp_path / "carver_png_out"
    carver = FileCarver(str(disk_file), out_dir)
    recovered = carver.scan(max_sectors=total_sectors, chunk_sectors=50)

    assert len(recovered) == 1
    png_carved = recovered[0]
    assert png_carved.file_type == "PNG"
    assert png_carved.start_lba == 25
    assert png_carved.size_bytes == len(png_payload)
    assert png_carved.is_structurally_valid is True
    assert png_carved.saved_path.read_bytes() == png_payload


def test_carver_rejects_negative_or_zero_chunk_sectors(tmp_path: Path):
    """P1 Teste: Garante rejeição estrita de chunk_sectors <= 0 ou excessivo."""
    import pytest
    disk_file = tmp_path / "dummy.img"
    disk_file.write_bytes(b"\x00" * 2048)
    carver = FileCarver(str(disk_file), tmp_path / "out")

    with pytest.raises(ValueError, match="estritamente positivo"):
        carver.scan(chunk_sectors=0)

    with pytest.raises(ValueError, match="estritamente positivo"):
        carver.scan(chunk_sectors=-10)

    with pytest.raises(ValueError, match="razoável"):
        carver.scan(chunk_sectors=100000)


def test_carver_does_not_overwrite_existing_recovered_files(tmp_path: Path):
    """P1 Teste: Garante que múltiplas varreduras na mesma pasta nunca sobrescrevam arquivos anteriores."""
    disk_file = tmp_path / "disk.img"
    total_sectors = 100

    jpeg_payload = b"\xff\xd8\xff\xe0\x00\x10JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00" + (b"IMG" * 10) + b"\xff\xd9"
    with open(disk_file, "wb") as f:
        f.seek(total_sectors * SECTOR_SIZE - 1)
        f.write(b"\x00")
    with open(disk_file, "r+b") as f:
        f.seek(10 * SECTOR_SIZE)
        f.write(jpeg_payload)

    out_dir = tmp_path / "safe_output"
    # Pré-cria um arquivo já existente recovered_0001.jpg
    out_dir.mkdir(parents=True, exist_ok=True)
    existing_file = out_dir / "recovered_0001.jpg"
    existing_file.write_bytes(b"PREVIOUSLY_RECOVERED_IMPORTANT_DATA")

    carver = FileCarver(str(disk_file), out_dir)
    recovered = carver.scan(max_sectors=total_sectors, chunk_sectors=50)

    assert len(recovered) == 1
    # O arquivo pré-existente NÃO deve ter sido sobrescrito!
    assert existing_file.read_bytes() == b"PREVIOUSLY_RECOVERED_IMPORTANT_DATA"
    # O novo arquivo deve ter sido salvo com sufixo numérico sem colisão!
    assert recovered[0].saved_path.name != "recovered_0001.jpg"
    assert recovered[0].saved_path.exists()
    assert recovered[0].saved_path.read_bytes() == jpeg_payload


def test_carver_rejects_anonymous_unidentified_stream(tmp_path: Path):
    """P0 Teste: Fluxo binário anônimo sem source_identifier comprovável é bloqueado por Fail-Closed."""
    import pytest
    raw_disk = tmp_path / "raw_source.img"
    raw_disk.write_bytes(b"\x00" * 4096)

    # Abre sem expor o .name (usando wrapper opaco)
    class OpaqueStream:
        def __init__(self, f):
            self._f = f
        def seek(self, *a, **k): return self._f.seek(*a, **k)
        def read(self, *a, **k): return self._f.read(*a, **k)
        def tell(self): return self._f.tell()

    with open(raw_disk, "rb") as real_f:
        opaque = OpaqueStream(real_f)
        with pytest.raises(ValueError, match="BLOQUEIO FAIL-CLOSED"):
            FileCarver(opaque, tmp_path / "out_dir")


def test_carver_detects_header_split_across_block_boundary(tmp_path: Path):
    """
    P1 Teste Crítico: Garante que um cabeçalho mágico dividido na fronteira exata entre dois blocos
    de busca (ex: primeiros 4 bytes no final do bloco N e últimos 4 bytes no início do bloco N+1)
    seja detectado com sucesso através do buffer de sobreposição.
    """
    import zlib
    disk_file = tmp_path / "disk_split_header.img"
    total_sectors = 10
    sector_size = 512

    # Constrói PNG mínimo válido
    ihdr_data = struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0)
    ihdr_crc = zlib.crc32(b"IHDR" + ihdr_data) & 0xFFFFFFFF
    ihdr_chunk = struct.pack(">I", len(ihdr_data)) + b"IHDR" + ihdr_data + struct.pack(">I", ihdr_crc)

    raw_data = zlib.compress(b"\x00\x00\x00\x00")
    idat_crc = zlib.crc32(b"IDAT" + raw_data) & 0xFFFFFFFF
    idat_chunk = struct.pack(">I", len(raw_data)) + b"IDAT" + raw_data + struct.pack(">I", idat_crc)

    iend_crc = zlib.crc32(b"IEND") & 0xFFFFFFFF
    iend_chunk = struct.pack(">I", 0) + b"IEND" + struct.pack(">I", iend_crc)

    png_body = ihdr_chunk + idat_chunk + iend_chunk
    png_full = b"\x89PNG\r\n\x1a\n" + png_body

    # Aloca disco sintético
    disk_bytes = bytearray(total_sectors * sector_size)

    # Injeta a assinatura PNG dividida:
    # chunk_sectors será 1 (512 bytes).
    # Primeiros 4 bytes (b"\x89PNG") colocados no offset 508..511 (fim do bloco 0)
    # Restante colocado no offset 512.. (início do bloco 1)
    split_offset = 508
    disk_bytes[split_offset : split_offset + len(png_full)] = png_full
    disk_file.write_bytes(disk_bytes)

    out_dir = tmp_path / "carved_split_out"
    carver = FileCarver(str(disk_file), out_dir, discard_invalid=True)
    # Executa com chunk de exatamente 1 setor (512 bytes), forçando a quebra do cabeçalho
    recovered = carver.scan(max_sectors=total_sectors, chunk_sectors=1)

    assert len(recovered) == 1
    png_res = recovered[0]
    assert png_res.file_type == "PNG"
    assert png_res.is_structurally_valid is True
    assert png_res.saved_path.read_bytes() == png_full

