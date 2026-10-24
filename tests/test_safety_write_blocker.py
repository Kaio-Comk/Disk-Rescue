"""
tests/test_safety_write_blocker.py — Testes formais de segurança "Do No Harm" e Write-Blocker.
Garante de forma verificável que:
1. Nenhuma operação de diagnóstico, varredura ou carving escreve na mídia de origem.
2. Origem e destino nunca podem ser o mesmo dispositivo.
3. Arquivos extraídos por File Carving passam por verificação estrutural.
"""
import io
from pathlib import Path
import pytest

from core.filesystem.ntfs_boot_repair import diagnose_ntfs_boot_sectors
from core.filesystem.scanner import scan_boot_sectors
from core.hardware.surface_scan import scan_surface_latency
from core.recovery.carver import FileCarver
from core.recovery.resilient_clone import ResilientCloner


class WriteBlockViolationError(Exception):
    """Lançada quando qualquer tentativa de escrita é detectada no dispositivo de origem."""
    pass


class ReadOnlyDeviceStream(io.BytesIO):
    """Dispositivo simulado de leitura que bloqueia estritamente qualquer chamada de escrita."""

    def __init__(self, initial_bytes: bytes):
        super().__init__(initial_bytes)
        self.write_attempts = 0
        self.read_count = 0

    def write(self, b):
        self.write_attempts += 1
        raise WriteBlockViolationError("BLOQUEIO DE SEGURANÇA: Tentativa de escrita em dispositivo de leitura!")

    def read(self, size=-1):
        self.read_count += 1
        return super().read(size)


def test_diagnostic_and_carver_scans_never_write_to_source(tmp_path: Path):
    """Garante que ferramentas de triagem e extração operam como Write-Blocker estrito."""
    # Cria disco sintético com Boot Sector NTFS
    sector_size = 512
    total_sectors = 100
    disk_data = bytearray(total_sectors * sector_size)

    # Injeta assinatura NTFS e parâmetros mínimos de BPB
    import struct
    disk_data[3:11] = b"NTFS    "
    struct.pack_into("<HB", disk_data, 11, 512, 8)
    struct.pack_into("<Q", disk_data, 40, total_sectors)
    disk_data[510:512] = b"\x55\xaa"

    disk_file = tmp_path / "mock_readonly_disk.bin"
    disk_file.write_bytes(disk_data)

    initial_mtime = disk_file.stat().st_mtime_ns

    # 1. Diagnóstico NTFS
    diag = diagnose_ntfs_boot_sectors(str(disk_file), partition_start_lba=0, partition_total_sectors=total_sectors)
    assert diag is not None

    # 2. Varredura de Boot Sectors
    discovered = scan_boot_sectors(str(disk_file), max_lba=total_sectors)
    assert len(discovered) >= 1

    # 3. Varredura de Superfície
    summary = scan_surface_latency(str(disk_file), max_sectors=total_sectors, block_sectors=10)
    assert summary.total_tested_blocks > 0

    # 4. File Carving
    carve_out = tmp_path / "carve_safe_out"
    carver = FileCarver(str(disk_file), carve_out)
    carved_files = carver.scan(max_sectors=total_sectors)

    # Verificação estrita de não-modificação da mídia de origem
    final_mtime = disk_file.stat().st_mtime_ns
    assert disk_file.stat().st_size == total_sectors * sector_size
    assert initial_mtime == final_mtime, "O arquivo de origem foi modificado durante operações de diagnóstico!"


def test_cloner_blocks_same_source_and_destination(tmp_path: Path):
    """P0: Impedir categoricamente que a imagem de saída seja gravada sobre o próprio disco de origem."""
    fake_disk = tmp_path / "failing_drive.raw"
    fake_disk.write_bytes(b"\x00" * 4096)

    # Tentativa de clonar o disco sobre ele mesmo
    with pytest.raises(ValueError, match="Origem e destino não podem ser o mesmo dispositivo"):
        ResilientCloner(
            device_path=str(fake_disk),
            output_image_path=fake_disk,
            total_sectors=8
        )


def test_carver_blocks_output_dir_on_source(tmp_path: Path):
    """P0: Impedir salvar arquivos extraídos no mesmo caminho do dispositivo."""
    fake_disk = tmp_path / "failing_drive.raw"
    fake_disk.write_bytes(b"\x00" * 4096)

    with pytest.raises(ValueError, match="não pode ser o dispositivo de origem"):
        FileCarver(
            device_path=str(fake_disk),
            output_dir=fake_disk
        )


def test_carver_structural_integrity_verification(tmp_path: Path):
    """P1: Validar verificação pós-extração de arquivos."""
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
    valid_pdf.write_bytes(b"%PDF-1.4\n1 0 obj\n<<>>\nendobj\ntrailer\n<<>>\n%%EOF\n")
    assert FileCarver.verify_file_integrity(valid_pdf, "PDF") is True

    broken_pdf = out_dir / "broken.pdf"
    broken_pdf.write_bytes(b"%PDF-1.4\nincomplete data without eof")
    assert FileCarver.verify_file_integrity(broken_pdf, "PDF") is False
