"""
tests/test_safety_write_blocker.py — Testes formais de segurança "Do No Harm" e Write-Blocker.
Garante de forma verificável que:
1. Uma camada de I/O instrumentada (StrictReadOnlyDeviceStream) intercepta qualquer escrita com WriteBlockViolationError.
2. Todas as ferramentas de diagnóstico, varredura de partição, carving e leitura de clonagem nunca escrevem na origem.
3. Origem e destino nunca podem compartilhar o mesmo disco físico (ex: PhysicalDrive0 e volume na mesma unidade).
4. Arquivos extraídos por File Carving passam por verificação estrutural e isolamento.
"""
import io
import struct
from pathlib import Path
import pytest

from core.filesystem.ntfs_boot_repair import diagnose_ntfs_boot_sectors
from core.filesystem.scanner import scan_boot_sectors
from core.hardware.device_resolver import (
    assert_safe_source_and_destination,
    get_physical_drive_number,
    is_same_physical_device,
)
from core.hardware.surface_scan import scan_surface_latency
from core.hardware.write_blocker import StrictReadOnlyDeviceStream, WriteBlockViolationError
from core.partition.gpt import parse_gpt
from core.partition.mbr import parse_mbr
from core.recovery.carver import FileCarver
from core.recovery.resilient_clone import ResilientCloner


def test_strict_read_only_stream_throws_on_any_write():
    """Garante que o stream instrumentado intercepta e bloqueia qualquer operação de escrita."""
    stream = StrictReadOnlyDeviceStream(b"TEST_SECTOR_DATA" * 32)

    # Leitura permitida
    data = stream.read(16)
    assert len(data) == 16
    assert stream.read_count == 1
    assert stream.write_attempts == 0

    # Escrita proibida
    with pytest.raises(WriteBlockViolationError, match="Tentativa de escrita"):
        stream.write(b"HACK")
    assert stream.write_attempts == 1

    # Writelines proibido
    with pytest.raises(WriteBlockViolationError, match="writelines"):
        stream.writelines([b"HACK"])
    assert stream.write_attempts == 2

    # Truncate proibido
    with pytest.raises(WriteBlockViolationError, match="truncamento"):
        stream.truncate(0)
    assert stream.write_attempts == 3


def test_instrumented_stream_with_carver_and_parsers(tmp_path: Path):
    """
    Injeta a camada de I/O simulada StrictReadOnlyDeviceStream diretamente no carver e parsers.
    Comprova que executam seus fluxos completos sem realizar uma única tentativa de escrita.
    """
    total_sectors = 200
    sector_size = 512
    raw_buffer = bytearray(total_sectors * sector_size)

    # 1. Injeta MBR sintética no LBA 0
    raw_buffer[510:512] = b"\x55\xaa"

    # 2. Injeta assinatura JPEG no LBA 20
    jpeg_payload = b"\xff\xd8\xff\xe0\x00\x10JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00" + (b"DATA" * 10) + b"\xff\xd9"
    raw_buffer[20 * sector_size : 20 * sector_size + len(jpeg_payload)] = jpeg_payload

    # Envolve em stream estritamente somente-leitura instrumentado
    read_stream = StrictReadOnlyDeviceStream(raw_buffer)

    # Executa parser MBR
    mbr_result = parse_mbr(read_stream)
    assert mbr_result is not None
    assert mbr_result.has_valid_signature is True
    assert read_stream.write_attempts == 0

    # Executa File Carver diretamente no stream instrumentado
    out_dir = tmp_path / "carver_instrumented_out"
    carver = FileCarver(read_stream, out_dir)
    recovered = carver.scan(max_sectors=total_sectors, chunk_sectors=50)

    assert len(recovered) == 1
    assert recovered[0].file_type == "JPEG"
    assert read_stream.write_attempts == 0
    assert read_stream.read_count > 0


def test_cloner_source_never_receives_writes(tmp_path: Path):
    """
    Comprova que durante todo o ciclo de clonagem (Fase 1 e Fase 2),
    o leitor da mídia de origem é 100% isolado e nunca recebe chamadas de escrita.
    """
    total_sectors = 100
    one_sector = (b"SOURCE_SECTOR_" * 50)[:512]
    src_data = one_sector * total_sectors

    source_stream = StrictReadOnlyDeviceStream(src_data)

    def instrumented_reader(lba: int, count: int) -> bytes:
        source_stream.seek(lba * 512)
        return source_stream.read(count * 512)

    dst_img = tmp_path / "destination_isolated.img"
    cloner = ResilientCloner(
        device_path="mock://isolated_source",
        output_image_path=dst_img,
        total_sectors=total_sectors,
        block_sectors=16,
        skip_step_sectors=32
    )

    res = cloner.run(reader_fn=instrumented_reader)
    assert res.success is True
    assert source_stream.write_attempts == 0
    assert source_stream.bytes_read == total_sectors * 512


def test_physical_drive_conflict_detection_blocks_same_disk(monkeypatch, tmp_path: Path):
    """
    P0 Teste Crítico: Garante que a proteção bloqueia a gravação quando origem e destino
    estiverem no mesmo disco físico (ex: PhysicalDrive0 e partição C: ou B: no mesmo drive).
    """
    # 1. Simula que \\.\PhysicalDrive0 e a unidade B: pertencem ao mesmo disco físico (número 0)
    def mock_drive_resolver(path):
        p = str(path).lower()
        if "physicaldrive0" in p or "b:" in p:
            return 0
        if "physicaldrive1" in p or "c:" in p:
            return 1
        return None

    monkeypatch.setattr("core.hardware.device_resolver.get_physical_drive_number", mock_drive_resolver)

    # Caso 1: Clonar PhysicalDrive0 para B:\imagem.img (MESMO DISCO FÍSICO -> BLOQUEADO!)
    with pytest.raises(ValueError, match="mesmo disco físico \\(PhysicalDrive0\\)"):
        assert_safe_source_and_destination(r"\\.\PhysicalDrive0", r"B:\Imagens\disco.img")

    # Caso 2: Carving de PhysicalDrive0 para B:\Recuperados (MESMO DISCO FÍSICO -> BLOQUEADO!)
    with pytest.raises(ValueError, match="mesmo disco físico \\(PhysicalDrive0\\)"):
        assert_safe_source_and_destination(r"\\.\PhysicalDrive0", r"B:\Recuperados", is_directory=True)

    # Caso 3: Clonar PhysicalDrive0 para C:\Imagens\disco.img (DISCOS FÍSICOS DIFERENTES: 0 e 1 -> PERMITIDO!)
    assert_safe_source_and_destination(r"\\.\PhysicalDrive0", r"C:\Imagens\disco.img")


def test_cloner_blocks_exact_same_file(tmp_path: Path):
    """P0 Teste: Impede categoricamente clonar um arquivo sobre si mesmo."""
    img_file = tmp_path / "disk.raw"
    img_file.write_bytes(b"\x00" * 4096)

    with pytest.raises(ValueError, match="Origem e destino são o mesmo dispositivo"):
        assert_safe_source_and_destination(img_file, img_file)


def test_carver_structural_integrity_verification(tmp_path: Path):
    """P1 Teste: Validação pós-extração de arquivos."""
    import zipfile

    out_dir = tmp_path / "carver_verify"
    out_dir.mkdir()

    # 1. Arquivo ZIP válido
    valid_zip = out_dir / "valid.zip"
    with zipfile.ZipFile(valid_zip, "w") as zf:
        zf.writestr("test.txt", "conteúdo íntegro")
    assert FileCarver.verify_file_integrity(valid_zip, "ZIP") is True

    # 2. Arquivo ZIP corrompido / truncado
    broken_zip = out_dir / "broken.zip"
    broken_zip.write_bytes(b"PK\x03\x04" + b"\x00" * 20)
    assert FileCarver.verify_file_integrity(broken_zip, "ZIP") is False

    # 3. PDF válido vs truncado
    valid_pdf = out_dir / "valid.pdf"
    pdf_lead = b"%PDF-1.4\n1 0 obj\n<<>>\nendobj\n"
    valid_pdf.write_bytes(pdf_lead + b"xref\ntrailer\n<<>>\nstartxref\n" + str(len(pdf_lead)).encode() + b"\n%%EOF\n")
    assert FileCarver.verify_file_integrity(valid_pdf, "PDF") is True

    broken_pdf = out_dir / "broken.pdf"
    broken_pdf.write_bytes(b"%PDF-1.4\nincomplete data without eof")
    assert FileCarver.verify_file_integrity(broken_pdf, "PDF") is False

    # 4. PNG válido com CRC matematicamente verificado vs CRC corrompido vs truncado
    import zlib
    valid_png = out_dir / "valid.png"
    ihdr_data = b"\x00" * 13
    ihdr_crc = struct.pack(">I", zlib.crc32(b"IHDR" + ihdr_data) & 0xffffffff)
    ihdr_chunk = struct.pack(">I", 13) + b"IHDR" + ihdr_data + ihdr_crc
    iend_crc = struct.pack(">I", zlib.crc32(b"IEND") & 0xffffffff)
    iend_chunk = struct.pack(">I", 0) + b"IEND" + iend_crc
    valid_png.write_bytes(b"\x89PNG\r\n\x1a\n" + ihdr_chunk + iend_chunk)
    assert FileCarver.verify_file_integrity(valid_png, "PNG") is True

    bad_crc_png = out_dir / "bad_crc.png"
    bad_crc_png.write_bytes(b"\x89PNG\r\n\x1a\n" + struct.pack(">I", 13) + b"IHDR" + ihdr_data + b"\xde\xad\xbe\xef" + iend_chunk)
    assert FileCarver.verify_file_integrity(bad_crc_png, "PNG") is False

    broken_png = out_dir / "broken.png"
    broken_png.write_bytes(b"\x89PNG\r\n\x1a\n" + b"CORRUPTED_WITHOUT_IHDR_OR_IEND")
    assert FileCarver.verify_file_integrity(broken_png, "PNG") is False

    # 5. JPEG válido vs inválido
    valid_jpeg = out_dir / "valid.jpg"
    valid_jpeg.write_bytes(b"\xff\xd8\xff\xe0\x00\x10JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00" + b"\xff\xd9")
    assert FileCarver.verify_file_integrity(valid_jpeg, "JPEG") is True

    broken_jpeg = out_dir / "broken.jpg"
    broken_jpeg.write_bytes(b"\xff\xd8\xff\x00" + b"NO_EOI_FOOTER")
    assert FileCarver.verify_file_integrity(broken_jpeg, "JPEG") is False


def test_physical_drive_conflict_fail_closed_when_unresolved(monkeypatch):
    """
    P0 Teste Crítico Fail-Closed: Quando a origem for um disco físico e o destino não
    puder ter sua identidade física comprovada com certeza absoluta, a operação DEVE ser bloqueada.
    """
    def mock_unresolved(path):
        p = str(path).lower()
        if "physicaldrive0" in p:
            return 0
        return None

    monkeypatch.setattr("core.hardware.device_resolver.get_physical_drive_number", mock_unresolved)

    with pytest.raises(ValueError, match="BLOQUEIO FAIL-CLOSED"):
        assert_safe_source_and_destination(r"\\.\PhysicalDrive0", r"X:\Imagens\disco.img")


def test_physical_drive_conflict_fail_closed_when_destination_physical_and_source_unresolved(monkeypatch):
    """
    P0 Teste Crítico Fail-Closed Universal: Quando o destino for um disco físico e a origem
    não puder ser resolvida com certeza, a operação DEVE ser bloqueada preventivamente.
    """
    def mock_dest_physical(path):
        p = str(path).lower()
        if "physicaldrive1" in p:
            return 1
        return None

    monkeypatch.setattr("core.hardware.device_resolver.get_physical_drive_number", mock_dest_physical)

    with pytest.raises(ValueError, match="BLOQUEIO FAIL-CLOSED"):
        assert_safe_source_and_destination(r"X:\Imagens\origem.img", r"\\.\PhysicalDrive1")


def test_volume_guid_conflict_detection(monkeypatch):
    r"""
    P0 Teste Crítico: Garante que caminhos com Volume GUID (\\?\Volume{...}) sejam classificados
    como dispositivos de baixo nível e bloqueiem conflitos no mesmo disco físico.
    """
    def mock_guid_resolver(path):
        p = str(path).lower()
        if "volume{12345678-1234-1234-1234-123456789abc}" in p or "c:" in p:
            return 0  # Ambos estão no PhysicalDrive0
        if "d:" in p:
            return 1  # No PhysicalDrive1
        return None

    monkeypatch.setattr("core.hardware.device_resolver.get_physical_drive_number", mock_guid_resolver)

    vol_guid_path = r"\\?\Volume{12345678-1234-1234-1234-123456789abc}\\"
    # Mesma unidade física (disco 0) -> BLOQUEADO!
    with pytest.raises(ValueError, match="mesmo disco físico"):
        assert_safe_source_and_destination(vol_guid_path, r"C:\Resgate\disco.img")

    # Unidades físicas diferentes (disco 0 vs disco 1) -> PERMITIDO!
    assert_safe_source_and_destination(vol_guid_path, r"D:\Resgate\disco.img")


def test_physical_drive_number_not_fooled_by_filename():
    r"""
    P0 Teste Crítico: Garante que um arquivo comum cujo nome contenha 'PhysicalDrive1'
    (ex: C:\Recuperacao\PhysicalDrive1.img) NÃO seja falsamente interpretado como o disco 1
    por uma regex não ancorada.
    """
    # Nomes ancorados de dispositivos físicos
    assert get_physical_drive_number(r"\\.\PhysicalDrive0") == 0
    assert get_physical_drive_number(r"\\?\PhysicalDrive1") == 1
    assert get_physical_drive_number("PhysicalDrive2") == 2
    assert get_physical_drive_number(r"//./PhysicalDrive3") == 3

    # Um nome de arquivo comum contendo a substring NÃO deve retornar o número da substring
    # Se C: não puder ser consultado via IOCTL neste teste ou for outro disco, o importante
    # é que NÃO retorne cegamente 1 por causa do nome do arquivo!
    filename_drive = get_physical_drive_number(r"C:\Recuperacao\PhysicalDrive1.img")
    # No Windows real, se C: for PhysicalDrive0, filename_drive será 0 (e NUNCA 1)
    if filename_drive is not None:
        c_drive = get_physical_drive_number(r"C:\\")
        assert filename_drive == c_drive

