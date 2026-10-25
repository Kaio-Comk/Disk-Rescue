"""
tests/test_ntfs_boot_repair.py — Testes unitários para o diagnóstico e restauração de Boot Sector NTFS via Backup.
"""
import struct
from pathlib import Path

from core.filesystem.ntfs_boot_repair import (
    diagnose_ntfs_boot_sectors,
    restore_ntfs_boot_from_backup,
)


def _build_synthetic_ntfs_vbr(total_sectors: int, mft_lcn: int) -> bytes:
    """Monta um setor de boot NTFS sintético de 512 bytes válido."""
    vbr = bytearray(512)
    vbr[0:3] = b"\xeb\x52\x90"
    vbr[3:11] = b"NTFS    "
    struct.pack_into("<H", vbr, 11, 512)   # 512 bytes por setor
    struct.pack_into("<B", vbr, 13, 8)     # 8 setores por cluster (4KB)
    struct.pack_into("<Q", vbr, 40, total_sectors)
    struct.pack_into("<Q", vbr, 48, mft_lcn)
    struct.pack_into("<Q", vbr, 56, mft_lcn + 1)
    struct.pack_into("<b", vbr, 64, -10)   # 1024 bytes por registro MFT
    struct.pack_into("<Q", vbr, 72, 0x1234567890ABCDEF)
    vbr[510:512] = b"\x55\xaa"
    return bytes(vbr)


def test_diagnose_and_restore_ntfs_boot_sector(tmp_path: Path):
    disk_img = tmp_path / "mock_disk.img"
    sector_size = 512
    start_lba = 2048
    vol_sectors = 10000
    backup_lba = start_lba + vol_sectors - 1
    mft_lcn = 100

    # 1. Cria disco sintético de 12.000 setores
    total_disk_bytes = 12000 * sector_size
    with open(disk_img, "wb") as f:
        f.seek(total_disk_bytes - 1)
        f.write(b"\x00")

    # 2. Escreve MFT válida no cluster correspondente
    mft_offset = (start_lba + mft_lcn * 8) * sector_size
    with open(disk_img, "r+b") as f:
        f.seek(mft_offset)
        f.write(b"FILE" + b"\x00" * 1020)

        # Setor primário corrompido com lixo
        f.seek(start_lba * sector_size)
        f.write(b"CORRUPTED_GARBAGE_DATA" * 10)

        # Setor de backup no final do volume gravado com VBR íntegro
        valid_vbr = _build_synthetic_ntfs_vbr(vol_sectors, mft_lcn)
        f.seek(backup_lba * sector_size)
        f.write(valid_vbr)

    # 3. Executa Diagnóstico
    diag = diagnose_ntfs_boot_sectors(str(disk_img), start_lba, vol_sectors, sector_size)
    assert diag.primary_valid is False
    assert diag.backup_valid is True
    assert diag.can_repair is True
    assert diag.primary_lba == start_lba
    assert diag.backup_lba == backup_lba

    # 4. Executa Restauração Cirúrgica
    backup_dir = tmp_path / "backups"
    res = restore_ntfs_boot_from_backup(str(disk_img), start_lba, vol_sectors, backup_dir=backup_dir, sector_size=sector_size)
    assert res.success is True
    assert res.restored_lba == start_lba
    assert res.backup_file.exists()
    assert b"CORRUPTED_GARBAGE_DATA" in res.backup_file.read_bytes()

    # 5. Valida que agora o setor primário está 100% íntegro e validado com MFT
    diag_after = diagnose_ntfs_boot_sectors(str(disk_img), start_lba, vol_sectors, sector_size)
    assert diag_after.primary_valid is True
    assert diag_after.can_repair is False


def test_restore_ntfs_validates_backup_dir_before_mkdir(tmp_path: Path, monkeypatch):
    """P0 Teste: Garante que diretório de backup inseguro seja rejeitado antes de mkdir."""
    disk_img = tmp_path / "mock_disk.img"
    disk_img.write_bytes(b"\x00" * 4096)
    unsafe_dir = tmp_path / "uncreated_unsafe_ntfs_dir"

    def mock_assert_safe(source_str, target_path, is_directory=False):
        raise ValueError("Diretório de backup inseguro no mesmo disco.")

    monkeypatch.setattr("core.hardware.device_resolver.assert_safe_source_and_destination", mock_assert_safe)

    res = restore_ntfs_boot_from_backup(str(disk_img), partition_start_lba=2048, partition_total_sectors=1000, backup_dir=unsafe_dir)
    assert res.success is False
    assert not unsafe_dir.exists()


def test_restore_ntfs_executes_verified_rollback_on_write_failure(tmp_path: Path, monkeypatch):
    """P0 Teste: Garante rollback do setor primário se a verificação pós-escrita falhar."""
    disk_img = tmp_path / "mock_disk_rollback.img"
    sector_size = 512
    start_lba = 2048
    vol_sectors = 10000
    backup_lba = start_lba + vol_sectors - 1
    mft_lcn = 100

    total_disk_bytes = 12000 * sector_size
    with open(disk_img, "wb") as f:
        f.seek(total_disk_bytes - 1)
        f.write(b"\x00")

    mft_offset = (start_lba + mft_lcn * 8) * sector_size
    with open(disk_img, "r+b") as f:
        f.seek(mft_offset)
        f.write(b"FILE" + b"\x00" * 1020)
        f.seek(start_lba * sector_size)
        f.write(b"ORIGINAL_CORRUPTED_SECTOR" * 15)
        valid_vbr = _build_synthetic_ntfs_vbr(vol_sectors, mft_lcn)
        f.seek(backup_lba * sector_size)
        f.write(valid_vbr)

    # Simula falha na gravação do novo VBR
    import builtins
    real_open = builtins.open

    class FaultyVBRFile:
        def __init__(self, target):
            self._target = target
            self._write_count = 0

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc_val, exc_tb):
            return self._target.__exit__(exc_type, exc_val, exc_tb)

        def write(self, data):
            self._write_count += 1
            if self._write_count == 1:
                # Falha física simulada na escrita no setor primário
                raise OSError("Falha física simulada ao regravar setor de boot NTFS")
            return self._target.write(data)

        def __getattr__(self, name):
            return getattr(self._target, name)

    def mock_open(file, mode="r", *args, **kwargs):
        f = real_open(file, mode, *args, **kwargs)
        if "r+b" in mode:
            return FaultyVBRFile(f)
        return f

    monkeypatch.setattr(builtins, "open", mock_open)

    backup_dir = tmp_path / "rollback_backups"
    res = restore_ntfs_boot_from_backup(str(disk_img), start_lba, vol_sectors, backup_dir=backup_dir, sector_size=sector_size)

    assert res.success is False
    assert "Rollback executado com sucesso e verificado" in res.message
    # Verifica que o setor primário permanece com os dados originais
    with real_open(disk_img, "rb") as f:
        f.seek(start_lba * sector_size)
        data = f.read(sector_size)
        assert b"ORIGINAL_CORRUPTED_SECTOR" in data

