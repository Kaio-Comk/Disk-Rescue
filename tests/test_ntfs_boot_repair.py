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
    assert res.rollback_attempted is True
    assert res.rollback_confirmed is True
    assert res.final_state_uncertain is False
    assert "Rollback executado com sucesso e verificado" in res.message
    # Verifica que o setor primário permanece com os dados originais
    with real_open(disk_img, "rb") as f:
        f.seek(start_lba * sector_size)
        data = f.read(sector_size)
        assert b"ORIGINAL_CORRUPTED_SECTOR" in data


def test_restore_ntfs_never_overwrites_existing_snapshots_and_handles_complex_paths(tmp_path: Path):
    """P1/5.1 & 5.4: Execuções repetidas ou caminhos de dispositivo colidentes geram snapshots exclusivos sem sobrescrever."""
    from core.filesystem.ntfs_boot_repair import _sanitize_ntfs_device_token

    # Caminhos diferentes com caracteres especiais do Windows geram tokens distintos graças ao hash embutido
    t1 = _sanitize_ntfs_device_token(r"\\.\PhysicalDrive1")
    t2 = _sanitize_ntfs_device_token(r"\\?\PhysicalDrive1")
    t3 = _sanitize_ntfs_device_token("C:\\caminho\\muito\\longo\\" + ("subpasta_extensa_" * 20) + "\\imagem:stream?.img")
    assert t1 != t2
    assert len(t3) <= 50

    disk_img = tmp_path / "repeat_snap.img"
    sector_size = 512
    start_lba = 2048
    vol_sectors = 10000
    backup_lba = start_lba + vol_sectors - 1
    mft_lcn = 100

    with open(disk_img, "wb") as f:
        f.seek(12000 * sector_size - 1)
        f.write(b"\x00")

    valid_vbr = _build_synthetic_ntfs_vbr(vol_sectors, mft_lcn)
    backup_dir = tmp_path / "exclusive_snaps"

    # Execução 1
    with open(disk_img, "r+b") as f:
        f.seek((start_lba + mft_lcn * 8) * sector_size)
        f.write(b"FILE" + b"\x00" * 1020)
        f.seek(start_lba * sector_size)
        f.write(b"FIRST_CORRUPTION_STATE_" * 20)
        f.seek(backup_lba * sector_size)
        f.write(valid_vbr)

    res1 = restore_ntfs_boot_from_backup(str(disk_img), start_lba, vol_sectors, backup_dir=backup_dir)
    assert res1.success is True
    assert res1.backup_file is not None and res1.backup_file.exists()

    # Execução 2 (no mesmo segundo, com outra corrupção)
    with open(disk_img, "r+b") as f:
        f.seek(start_lba * sector_size)
        f.write(b"SECOND_CORRUPTION_STATE" * 20)

    res2 = restore_ntfs_boot_from_backup(str(disk_img), start_lba, vol_sectors, backup_dir=backup_dir)
    assert res2.success is True
    assert res2.backup_file is not None and res2.backup_file.exists()
    assert res1.backup_file != res2.backup_file
    assert b"FIRST_CORRUPTION_STATE_" in res1.backup_file.read_bytes()
    assert b"SECOND_CORRUPTION_STATE" in res2.backup_file.read_bytes()


def test_restore_ntfs_aborts_before_write_if_snapshot_verification_fails(tmp_path: Path, monkeypatch):
    """P1/5.2 & 5.4: Se a verificação byte-a-byte do snapshot falhar, o setor original não é tocado."""
    import builtins
    disk_img = tmp_path / "snap_fail.img"
    sector_size = 512
    start_lba = 2048
    vol_sectors = 10000
    backup_lba = start_lba + vol_sectors - 1
    mft_lcn = 100

    with open(disk_img, "wb") as f:
        f.seek(12000 * sector_size - 1)
        f.write(b"\x00")
    with open(disk_img, "r+b") as f:
        f.seek((start_lba + mft_lcn * 8) * sector_size)
        f.write(b"FILE" + b"\x00" * 1020)
        f.seek(start_lba * sector_size)
        f.write((b"UNTOUCHED_PRIMARY_BYTES_" * 25)[:512])
        f.seek(backup_lba * sector_size)
        f.write(_build_synthetic_ntfs_vbr(vol_sectors, mft_lcn))

    real_open = builtins.open
    monkeypatch.setattr(
        Path,
        "read_bytes",
        lambda self: b"BAD_SNAP_DATA" if "corrupted_vbr_" in self.name else real_open(self, "rb").read(),
    )

    res = restore_ntfs_boot_from_backup(str(disk_img), start_lba, vol_sectors, backup_dir=tmp_path / "b_corrupt")
    assert res.success is False
    assert "snapshot" in res.message.lower()

    with real_open(disk_img, "rb") as f:
        f.seek(start_lba * sector_size)
        assert f.read(512) == (b"UNTOUCHED_PRIMARY_BYTES_" * 25)[:512]


def test_restore_ntfs_rejects_invalid_backup_vbr_and_out_of_bounds_geometry(tmp_path: Path):
    """P1/5.3 & 5.4: Recusa restauração se o VBR de backup for inválido ou exceder os limites da partição."""
    disk_img = tmp_path / "invalid_backup.img"
    sector_size = 512
    start_lba = 2048
    vol_sectors = 10000
    backup_lba = start_lba + vol_sectors - 1
    mft_lcn = 100

    with open(disk_img, "wb") as f:
        f.seek(12000 * sector_size - 1)
        f.write(b"\x00")

    # VBR de backup alegando total_sectors = 50000 (> vol_sectors = 10000)
    oversized_vbr = _build_synthetic_ntfs_vbr(50000, mft_lcn)
    with open(disk_img, "r+b") as f:
        f.seek((start_lba + mft_lcn * 8) * sector_size)
        f.write(b"FILE" + b"\x00" * 1020)
        f.seek(backup_lba * sector_size)
        f.write(oversized_vbr)

    diag = diagnose_ntfs_boot_sectors(str(disk_img), start_lba, vol_sectors, sector_size)
    assert diag.backup_valid is False
    assert diag.can_repair is False

    res = restore_ntfs_boot_from_backup(str(disk_img), start_lba, vol_sectors, backup_dir=tmp_path / "b_inv")
    assert res.success is False


def test_restore_ntfs_fsync_failure_in_write_and_in_rollback(tmp_path: Path, monkeypatch):
    """
    P1/5.3 & 5.4:
    1) Falha de fsync na escrita principal aciona rollback confirmado;
    2) Falha de fsync no rollback NÃO é ignorada e produz rollback_attempted=True,
       rollback_confirmed=False, final_state_uncertain=True, mantendo o snapshot intacto.
    """
    import os
    disk_img = tmp_path / "ntfs_fsync.img"
    sector_size = 512
    start_lba = 2048
    vol_sectors = 10000
    backup_lba = start_lba + vol_sectors - 1
    mft_lcn = 100

    with open(disk_img, "wb") as f:
        f.seek(12000 * sector_size - 1)
        f.write(b"\x00")

    orig_primary = (b"ORIG_VBR_BEFORE_FSYNC_FAIL_" * 25)[:512]
    with open(disk_img, "r+b") as f:
        f.seek((start_lba + mft_lcn * 8) * sector_size)
        f.write(b"FILE" + b"\x00" * 1020)
        f.seek(start_lba * sector_size)
        f.write(orig_primary)
        f.seek(backup_lba * sector_size)
        f.write(_build_synthetic_ntfs_vbr(vol_sectors, mft_lcn))

    real_fsync = os.fsync
    state = {"calls": 0, "mode": "fail_main_write_only"}

    def mock_fsync(fd):
        state["calls"] += 1
        # Call 1 é o snapshot preventivo; Call 2 é a escrita principal; Call 3 é o rollback
        if state["mode"] == "fail_main_write_only" and state["calls"] == 2:
            raise OSError("Erro de fsync ao gravar VBR primário")
        if state["mode"] == "fail_both_write_and_rollback" and state["calls"] >= 2:
            raise OSError("Erro persistente de fsync também no rollback")
        return real_fsync(fd)

    monkeypatch.setattr(os, "fsync", mock_fsync)

    # Caso 1: Falha no fsync da escrita principal -> rollback confirmado
    res1 = restore_ntfs_boot_from_backup(str(disk_img), start_lba, vol_sectors, backup_dir=tmp_path / "fsync_1")
    assert res1.success is False
    assert res1.rollback_attempted is True
    assert res1.rollback_confirmed is True
    assert res1.final_state_uncertain is False

    # Caso 2: Falha no fsync da escrita principal E no fsync do rollback -> estado incerto!
    state["calls"] = 0
    state["mode"] = "fail_both_write_and_rollback"
    res2 = restore_ntfs_boot_from_backup(str(disk_img), start_lba, vol_sectors, backup_dir=tmp_path / "fsync_2")
    assert res2.success is False
    assert res2.rollback_attempted is True
    assert res2.rollback_confirmed is False
    assert res2.final_state_uncertain is True
    assert "incerto" in res2.message.lower()
    assert res2.backup_file is not None and res2.backup_file.exists()
    assert res2.backup_file.read_bytes() == orig_primary


