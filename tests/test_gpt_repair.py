"""
tests/test_gpt_repair.py — Testes unitários para o parser e motor de reparo GPT.
"""
import io
import struct
import zlib
from pathlib import Path

import pytest

from core.filesystem.ntfs import parse_ntfs_boot_sector
from core.partition.gpt import (
    DEFAULT_SECTOR_SIZE,
    GPTPartitionEntry,
    GPTTable,
    parse_gpt,
    rebuild_gpt,
)


def _criar_gpt_sintetico() -> bytearray:
    """Cria uma imagem binária em memória com LBA 0 (MBR protector), LBA 1 (GPT Header) e LBA 2..33 (Partições)."""
    disk = bytearray(64 * 512) # 64 setores (32 KB)
    
    # LBA 1: Header GPT
    header = bytearray(512)
    header[0:8] = b"EFI PART"
    rev = 0x00010000
    h_size = 92
    cur_lba = 1
    backup_lba = 100000
    first_usable = 34
    last_usable = 99966
    disk_guid = b"\x01" * 16
    part_lba = 2
    num_entries = 128
    entry_size = 128
    
    # Cria duas partições: MSR e Basic Data
    p1 = GPTPartitionEntry(
        index=1,
        type_guid=b"\x16\xe3\xc9\xe3" + b"\x00" * 12,
        unique_guid=b"\x02" * 16,
        first_lba=2048,
        last_lba=34815,
        flags=0x8000000000000000,
        name="Microsoft reserved partition"
    )
    p2 = GPTPartitionEntry(
        index=2,
        type_guid=b"\xa2\xa0\xd0\xeb" + b"\x00" * 12,
        unique_guid=b"\x03" * 16,
        first_lba=34816, # Erro sintético (deslocado)
        last_lba=99000,
        flags=0x8000000000000000,
        name="Basic data partition"
    )
    
    entries = bytearray(num_entries * entry_size)
    entries[0:128] = p1.to_bytes(entry_size)
    entries[128:256] = p2.to_bytes(entry_size)
    
    part_crc = zlib.crc32(entries) & 0xFFFFFFFF
    
    struct.pack_into(
        "<8sIIIIQQQQ16sQIII",
        header, 0,
        b"EFI PART", rev, h_size, 0, 0,
        cur_lba, backup_lba, first_usable, last_usable,
        disk_guid, part_lba, num_entries, entry_size, part_crc
    )
    
    h_crc = zlib.crc32(header[:h_size]) & 0xFFFFFFFF
    struct.pack_into("<I", header, 16, h_crc)
    
    disk[512:1024] = header
    disk[1024 : 1024 + len(entries)] = entries
    return disk


def test_parse_gpt_valida_crc(tmp_path):
    img = tmp_path / "mock_disk.bin"
    img.write_bytes(_criar_gpt_sintetico())
    
    with open(img, "rb") as f:
        gpt = parse_gpt(f)
        
    assert gpt is not None
    assert gpt.is_header_crc_valid is True
    assert gpt.is_entries_crc_valid is True
    assert len(gpt.partitions) == 2
    assert gpt.partitions[0].first_lba == 2048
    assert gpt.partitions[1].first_lba == 34816


def test_rebuild_gpt_corrige_offset_e_recalcula_crc(tmp_path):
    img = tmp_path / "mock_disk.bin"
    img.write_bytes(_criar_gpt_sintetico())
    
    with open(img, "r+b") as f:
        gpt = parse_gpt(f)
        assert gpt is not None
        
        # Realinha partição 2 para LBA 567296
        gpt.partitions[1].first_lba = 567296
        gpt.partitions[1].last_lba = 945291255
        gpt.partitions[1].flags = 0x0
        
        new_header, new_entries = rebuild_gpt(gpt, gpt.partitions)
        f.seek(512)
        f.write(new_header)
        f.seek(1024)
        f.write(new_entries)
        f.flush()
        
    # Reabre e valida
    with open(img, "rb") as f:
        gpt_corrigido = parse_gpt(f)
        
    assert gpt_corrigido is not None
    assert gpt_corrigido.is_header_crc_valid is True
    assert gpt_corrigido.is_entries_crc_valid is True
    assert gpt_corrigido.partitions[1].first_lba == 567296
    assert gpt_corrigido.partitions[1].last_lba == 945291255
    assert gpt_corrigido.partitions[1].no_drive_letter is False


def test_gpt_parser_rejects_malformed_structures(tmp_path):
    """P1 Teste: Garante que o parser GPT rejeite tamanhos incoerentes e evite alocação abusiva de RAM."""
    raw = _criar_gpt_sintetico()
    
    # 1. header_size menor que 92 bytes
    corrupt_hsize = bytearray(raw)
    struct.pack_into("<I", corrupt_hsize, 512 + 12, 50)  # h_size = 50
    f1 = io.BytesIO(corrupt_hsize)
    assert parse_gpt(f1) is None

    # 2. num_entries astronômico (ex: 50.000 entradas)
    corrupt_entries = bytearray(raw)
    struct.pack_into("<I", corrupt_entries, 512 + 80, 50000)
    f2 = io.BytesIO(corrupt_entries)
    assert parse_gpt(f2) is None

    # 3. entry_size inválido (ex: 10 bytes)
    corrupt_esize = bytearray(raw)
    struct.pack_into("<I", corrupt_esize, 512 + 84, 10)
    f3 = io.BytesIO(corrupt_esize)
    assert parse_gpt(f3) is None


def test_repair_gpt_partition_offset_syncs_backup_and_rejects_overlaps(tmp_path):
    """
    P1 Teste: Valida que repair_gpt_partition_offset sincroniza a cópia de backup no fim do disco
    e rejeita sobreposições e limites inválidos.
    """
    from core.filesystem.scanner import DiscoveredFilesystem
    from core.recovery.repair import repair_gpt_partition_offset

    # Cria disco sintético com espaço suficiente para as partições
    total_sectors = 100_000
    disk_bytes = bytearray(64 * 512)
    base_gpt = _criar_gpt_sintetico()
    disk_bytes[:len(base_gpt)] = base_gpt
    disk_bytes.extend(b"\x00" * ((total_sectors - 64) * 512))

    # Atualiza cabeçalho primário: backup no LBA 99999 e last_usable no LBA 90000
    struct.pack_into("<Q", disk_bytes, 512 + 32, 99999)  # backup_lba = 99999
    struct.pack_into("<Q", disk_bytes, 512 + 48, 90000)  # last_usable = 90000
    # Recalcula CRC do cabeçalho primário
    disk_bytes[512 + 16 : 512 + 20] = b"\x00\x00\x00\x00"
    h_crc = zlib.crc32(disk_bytes[512 : 512 + 92]) & 0xFFFFFFFF
    struct.pack_into("<I", disk_bytes, 512 + 16, h_crc)

    img = tmp_path / "mock_disk_gpt_full.bin"
    img.write_bytes(disk_bytes)
    backup_dir = tmp_path / "backups_gpt"

    # 1. Tenta reparar para fora da área utilizável (LBA 91000..92000 > last_usable 90000)
    fs_out_of_bounds = DiscoveredFilesystem("NTFS", start_lba=91000, total_sectors=1000, size_bytes=1000 * 512)
    res_err = repair_gpt_partition_offset(str(img), target_partition_index=2, discovered_fs=fs_out_of_bounds, backup_dir=backup_dir)
    assert res_err.success is False
    assert "ultrapassa área utilizável" in res_err.message

    # 2. Tenta reparar para sobrepor partição 1 (partição 1 está em 2048..34815; tenta 2000..3000)
    fs_overlap = DiscoveredFilesystem("NTFS", start_lba=2000, total_sectors=1001, size_bytes=1001 * 512)
    res_overlap = repair_gpt_partition_offset(str(img), target_partition_index=2, discovered_fs=fs_overlap, backup_dir=backup_dir)
    assert res_overlap.success is False
    assert "sobrepõe a partição 1" in res_overlap.message

    # 3. Reparo válido em LBA 40000..50000 (dentro dos limites e sem sobreposição)
    fs_valid = DiscoveredFilesystem("NTFS", start_lba=40000, total_sectors=10001, size_bytes=10001 * 512)
    res_ok = repair_gpt_partition_offset(str(img), target_partition_index=2, discovered_fs=fs_valid, backup_dir=backup_dir)
    assert res_ok.success is True

    # Verifica se os bytes da GPT primária foram atualizados
    with open(img, "rb") as f:
        reopened = parse_gpt(f)
    assert reopened is not None
    assert reopened.partitions[1].first_lba == 40000
    assert reopened.partitions[1].last_lba == 50000
    # Verifica que flags originais foram preservadas
    assert reopened.partitions[1].flags == 0x0


def test_repair_gpt_validates_backup_dir_before_mkdir(tmp_path, monkeypatch):
    """P0 Teste: Garante que diretório de backup inseguro seja rejeitado antes de mkdir."""
    from core.filesystem.scanner import DiscoveredFilesystem
    from core.recovery.repair import repair_gpt_partition_offset

    img = tmp_path / "mock_disk.bin"
    img.write_bytes(_criar_gpt_sintetico())
    unsafe_dir = tmp_path / "uncreated_unsafe_gpt_dir"

    def mock_assert_safe(source_str, target_path, is_directory=False):
        raise ValueError("Diretório de backup inseguro no mesmo disco.")

    monkeypatch.setattr("core.hardware.device_resolver.assert_safe_source_and_destination", mock_assert_safe)

    fs = DiscoveredFilesystem("NTFS", start_lba=40000, total_sectors=1000, size_bytes=1000 * 512)
    res = repair_gpt_partition_offset(str(img), target_partition_index=2, discovered_fs=fs, backup_dir=unsafe_dir)
    assert res.success is False
    assert not unsafe_dir.exists()


def test_repair_gpt_creates_both_primary_and_backup_snapshots(tmp_path):
    """P0 Teste: Garante que snapshots preventivos tanto da GPT Primária quanto da de Backup sejam gravados."""
    from core.filesystem.scanner import DiscoveredFilesystem
    from core.recovery.repair import repair_gpt_partition_offset

    total_sectors = 100_000
    disk_bytes = bytearray(64 * 512)
    base_gpt = _criar_gpt_sintetico()
    disk_bytes[:len(base_gpt)] = base_gpt
    disk_bytes.extend(b"\x00" * ((total_sectors - 64) * 512))

    struct.pack_into("<Q", disk_bytes, 512 + 32, 99999)  # backup_lba = 99999
    struct.pack_into("<Q", disk_bytes, 512 + 48, 90000)  # last_usable = 90000
    disk_bytes[512 + 16 : 512 + 20] = b"\x00\x00\x00\x00"
    h_crc = zlib.crc32(disk_bytes[512 : 512 + 92]) & 0xFFFFFFFF
    struct.pack_into("<I", disk_bytes, 512 + 16, h_crc)

    img = tmp_path / "dual_snap_disk.bin"
    img.write_bytes(disk_bytes)
    backup_dir = tmp_path / "dual_snapshots"

    fs_valid = DiscoveredFilesystem("NTFS", start_lba=40000, total_sectors=10001, size_bytes=10001 * 512)
    res = repair_gpt_partition_offset(str(img), target_partition_index=2, discovered_fs=fs_valid, backup_dir=backup_dir)
    assert res.success is True

    # Valida existência física e integridade de ambos os snapshots
    primary_snaps = list(backup_dir.glob("backup_gpt_primary_*.bin"))
    backup_snaps = list(backup_dir.glob("backup_gpt_secondary_*.bin"))

    assert len(primary_snaps) >= 1
    assert len(backup_snaps) >= 1
    assert primary_snaps[0].stat().st_size == 34 * 512
    assert backup_snaps[0].stat().st_size == 33 * 512


def test_repair_gpt_dynamic_entries_calculation_64_entries(tmp_path):
    """P1 Teste: Valida cálculo dinâmico de regiões GPT com 64 entradas (não fixo em 34 setores)."""
    from core.filesystem.scanner import DiscoveredFilesystem
    from core.recovery.repair import repair_gpt_partition_offset

    total_sectors = 100_000
    disk_bytes = bytearray(64 * 512)
    # Header GPT com 64 entradas (64 * 128 = 8192 bytes = 16 setores)
    header = bytearray(512)
    header[0:8] = b"EFI PART"
    rev = 0x00010000
    h_size = 92
    cur_lba = 1
    backup_lba = 99999
    first_usable = 18  # 1 (protective) + 1 (header) + 16 (entries) = 18
    last_usable = 99980
    disk_guid = b"\x05" * 16
    part_lba = 2
    num_entries = 64
    entry_size = 128

    p1 = GPTPartitionEntry(
        index=1,
        type_guid=b"\xa2\xa0\xd0\xeb" + b"\x00" * 12,
        unique_guid=b"\x06" * 16,
        first_lba=2048,
        last_lba=50000,
        flags=0x8000000000000000,
        name="Dynamic Data"
    )
    entries = bytearray(num_entries * entry_size)
    entries[0:128] = p1.to_bytes(entry_size)
    part_crc = zlib.crc32(entries) & 0xFFFFFFFF

    struct.pack_into(
        "<8sIIIIQQQQ16sQIII",
        header, 0,
        b"EFI PART", rev, h_size, 0, 0,
        cur_lba, backup_lba, first_usable, last_usable,
        disk_guid, part_lba, num_entries, entry_size, part_crc
    )
    h_crc = zlib.crc32(header[:h_size]) & 0xFFFFFFFF
    struct.pack_into("<I", header, 16, h_crc)

    disk_bytes[512:1024] = header
    disk_bytes[1024 : 1024 + len(entries)] = entries
    disk_bytes.extend(b"\x00" * ((total_sectors - 64) * 512))

    img = tmp_path / "dynamic_64_entries.bin"
    img.write_bytes(disk_bytes)
    backup_dir = tmp_path / "dynamic_snapshots"

    fs = DiscoveredFilesystem("NTFS", start_lba=4000, total_sectors=20000, size_bytes=20000 * 512)
    res = repair_gpt_partition_offset(str(img), target_partition_index=1, discovered_fs=fs, backup_dir=backup_dir)
    assert res.success is True

    # Primário: LBA 0 (1) + Header (1) + 16 setores de entradas = 18 setores
    primary_snaps = list(backup_dir.glob("backup_gpt_primary_*.bin"))
    # Backup: 16 setores de entradas + 1 de cabeçalho = 17 setores
    backup_snaps = list(backup_dir.glob("backup_gpt_secondary_*.bin"))

    assert len(primary_snaps) >= 1
    assert len(backup_snaps) >= 1
    assert primary_snaps[0].stat().st_size == 18 * 512
    assert backup_snaps[0].stat().st_size == 17 * 512


def test_repair_gpt_executes_verified_rollback_on_failure(tmp_path, monkeypatch):
    """P0 Teste: Garante que um erro na gravação aciona rollback verificado e restaura os dados originais."""
    from core.filesystem.scanner import DiscoveredFilesystem
    from core.recovery.repair import repair_gpt_partition_offset

    total_sectors = 100_000
    disk_bytes = bytearray(64 * 512)
    base_gpt = _criar_gpt_sintetico()
    disk_bytes[:len(base_gpt)] = base_gpt
    disk_bytes.extend(b"\x00" * ((total_sectors - 64) * 512))

    struct.pack_into("<Q", disk_bytes, 512 + 32, 99999)
    struct.pack_into("<Q", disk_bytes, 512 + 48, 90000)
    disk_bytes[512 + 16 : 512 + 20] = b"\x00\x00\x00\x00"
    h_crc = zlib.crc32(disk_bytes[512 : 512 + 92]) & 0xFFFFFFFF
    struct.pack_into("<I", disk_bytes, 512 + 16, h_crc)

    img = tmp_path / "rollback_test_disk.bin"
    img.write_bytes(disk_bytes)
    original_content = img.read_bytes()
    backup_dir = tmp_path / "rollback_snapshots"

    # Simula falha física na gravação durante a escrita de entradas
    import builtins
    real_open = builtins.open

    class FaultyFile:
        def __init__(self, target):
            self._target = target
            self._write_count = 0

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc_val, exc_tb):
            return self._target.__exit__(exc_type, exc_val, exc_tb)

        def write(self, data):
            self._write_count += 1
            if self._write_count == 2:  # Falha simulada após gravar cabeçalho primário
                raise OSError("Falha simulada de hardware durante escrita de setor GPT")
            return self._target.write(data)

        def __getattr__(self, name):
            return getattr(self._target, name)

    def mock_open(file, mode="r", *args, **kwargs):
        f = real_open(file, mode, *args, **kwargs)
        if "r+b" in mode:
            return FaultyFile(f)
        return f

    monkeypatch.setattr(builtins, "open", mock_open)

    fs = DiscoveredFilesystem("NTFS", start_lba=40000, total_sectors=10001, size_bytes=10001 * 512)
    res = repair_gpt_partition_offset(str(img), target_partition_index=2, discovered_fs=fs, backup_dir=backup_dir)

    assert res.success is False
    assert res.rollback_attempted is True
    assert res.rollback_confirmed is True
    assert res.final_state_uncertain is False
    assert "Rollback executado com sucesso e verificado" in res.message
    # Garante que os bytes do disco após o rollback voltaram exatamente ao estado original
    assert img.read_bytes() == original_content


def _build_full_gpt_disk(
    total_sectors: int = 2000,
    num_entries: int = 128,
    entry_size: int = 128,
    part_lba: int = 2,
    first_usable: int = 34,
    last_usable: int = 1900,
    backup_lba: int = 1999,
    backup_entries_lba: int | None = None,
) -> bytearray:
    """Cria imagem sintética completa com MBR protetor, GPT primária e GPT de backup válida."""
    disk = bytearray(total_sectors * 512)
    # MBR protetor em LBA 0
    disk[0:8] = b"PROT_MBR"
    disk[510:512] = b"\x55\xaa"

    entries_bytes_len = num_entries * entry_size
    entries_sectors = (entries_bytes_len + 511) // 512
    if backup_entries_lba is None:
        backup_entries_lba = backup_lba - entries_sectors

    p1 = GPTPartitionEntry(
        index=1,
        type_guid=b"\xa2\xa0\xd0\xeb" + b"\x00" * 12,
        unique_guid=b"\x11" * 16,
        first_lba=100,
        last_lba=500,
        flags=0x0,
        name="Primary Data",
    )
    entries = bytearray(entries_sectors * 512)
    entries[0:entry_size] = p1.to_bytes(entry_size)
    part_crc = zlib.crc32(entries[:entries_bytes_len]) & 0xFFFFFFFF
    disk_guid = b"\xAA" * 16

    # Cabeçalho primário em LBA 1
    primary_hdr = bytearray(512)
    struct.pack_into(
        "<8sIIIIQQQQ16sQIII",
        primary_hdr, 0,
        b"EFI PART", 0x00010000, 92, 0, 0,
        1, backup_lba, first_usable, last_usable,
        disk_guid, part_lba, num_entries, entry_size, part_crc,
    )
    p_crc = zlib.crc32(primary_hdr[:92]) & 0xFFFFFFFF
    struct.pack_into("<I", primary_hdr, 16, p_crc)

    # Cabeçalho de backup em backup_lba
    backup_hdr = bytearray(512)
    struct.pack_into(
        "<8sIIIIQQQQ16sQIII",
        backup_hdr, 0,
        b"EFI PART", 0x00010000, 92, 0, 0,
        backup_lba, 1, first_usable, last_usable,
        disk_guid, backup_entries_lba, num_entries, entry_size, part_crc,
    )
    b_crc = zlib.crc32(backup_hdr[:92]) & 0xFFFFFFFF
    struct.pack_into("<I", backup_hdr, 16, b_crc)

    disk[512:1024] = primary_hdr
    disk[part_lba * 512 : (part_lba + entries_sectors) * 512] = entries
    disk[backup_entries_lba * 512 : (backup_entries_lba + entries_sectors) * 512] = entries
    disk[backup_lba * 512 : (backup_lba + 1) * 512] = backup_hdr
    return disk


def test_repair_gpt_custom_entry_size_256_and_nonstandard_table_lbas(tmp_path):
    """
    P1/4.1 & 4.4: Testa diferentes quantidades e tamanhos de entradas (32 entradas de 256 bytes = 16 setores)
    e tabelas primárias (LBA 4) e de backup (LBA 1920, diferente de backup_lba - entries_sectors) válidas.
    """
    from core.filesystem.scanner import DiscoveredFilesystem
    from core.recovery.repair import repair_gpt_partition_offset

    # backup_lba = 1999, entries_sectors = 16, mas backup_entries_lba = 1920 (após last_usable=1900)
    disk_bytes = _build_full_gpt_disk(
        total_sectors=2000,
        num_entries=32,
        entry_size=256,
        part_lba=4,
        first_usable=25,
        last_usable=1900,
        backup_lba=1999,
        backup_entries_lba=1920,
    )
    img = tmp_path / "custom_gpt_256.bin"
    img.write_bytes(disk_bytes)
    backup_dir = tmp_path / "snaps_256"

    fs = DiscoveredFilesystem("NTFS", start_lba=200, total_sectors=600, size_bytes=600 * 512)
    res = repair_gpt_partition_offset(str(img), target_partition_index=1, discovered_fs=fs, backup_dir=backup_dir)
    assert res.success is True
    assert len(res.region_snapshots) == 5

    # Confirma que a tabela de backup foi gravada no LBA 1920 (e não em 1999 - 16 = 1983)
    repaired_bytes = img.read_bytes()
    b_hdr = repaired_bytes[1999 * 512 : 2000 * 512]
    b_part_lba = struct.unpack_from("<Q", b_hdr, 72)[0]
    assert b_part_lba == 1920
    first_lba_in_backup = struct.unpack_from("<Q", repaired_bytes, 1920 * 512 + 32)[0]
    assert first_lba_in_backup == 200


def test_repair_gpt_rejects_bad_crcs_and_inconsistent_backup_header(tmp_path):
    """P1/4.4: Rejeita reparo quando CRCs primárias/secundárias estão corrompidas ou cabeçalhos divergem."""
    from core.filesystem.scanner import DiscoveredFilesystem
    from core.recovery.repair import repair_gpt_partition_offset

    fs = DiscoveredFilesystem("NTFS", start_lba=200, total_sectors=600, size_bytes=600 * 512)

    # 1. CRC do cabeçalho primário inválida
    d1 = _build_full_gpt_disk()
    struct.pack_into("<I", d1, 512 + 16, 0xDEADBEEF)
    img1 = tmp_path / "bad_prim_hcrc.bin"
    img1.write_bytes(d1)
    res1 = repair_gpt_partition_offset(str(img1), 1, fs, backup_dir=tmp_path / "b1")
    assert res1.success is False
    assert "CRC32 inválido" in res1.message

    # 2. CRC do cabeçalho de backup inválida
    d2 = _build_full_gpt_disk()
    struct.pack_into("<I", d2, 1999 * 512 + 16, 0xCAFEBABE)
    img2 = tmp_path / "bad_back_hcrc.bin"
    img2.write_bytes(d2)
    res2 = repair_gpt_partition_offset(str(img2), 1, fs, backup_dir=tmp_path / "b2")
    assert res2.success is False
    assert "Cabeçalho GPT de backup com CRC32 inválido" in res2.message

    # 3. Cabeçalho de backup inconsistente (disk_guid divergente do primário, mesmo com CRC recalculada)
    d3 = _build_full_gpt_disk()
    d3[1999 * 512 + 56 : 1999 * 512 + 72] = b"\xFF" * 16
    struct.pack_into("<I", d3, 1999 * 512 + 16, 0)
    new_bcrc = zlib.crc32(d3[1999 * 512 : 1999 * 512 + 92]) & 0xFFFFFFFF
    struct.pack_into("<I", d3, 1999 * 512 + 16, new_bcrc)
    img3 = tmp_path / "inconsistent_back_hdr.bin"
    img3.write_bytes(d3)
    res3 = repair_gpt_partition_offset(str(img3), 1, fs, backup_dir=tmp_path / "b3")
    assert res3.success is False
    assert "diverge do cabeçalho primário" in res3.message


@pytest.mark.parametrize("fail_on_write_step", [1, 2, 3, 4])
def test_repair_gpt_rollback_on_each_write_step(tmp_path, monkeypatch, fail_on_write_step):
    """P1/4.4: Testa falha em cada uma das 4 etapas de escrita e comprova rollback integral verificado."""
    import builtins
    from core.filesystem.scanner import DiscoveredFilesystem
    from core.recovery.repair import repair_gpt_partition_offset

    disk_bytes = _build_full_gpt_disk()
    img = tmp_path / f"step_{fail_on_write_step}_disk.bin"
    img.write_bytes(disk_bytes)
    original = img.read_bytes()
    backup_dir = tmp_path / f"snaps_step_{fail_on_write_step}"

    real_open = builtins.open

    class StepFaultyFile:
        def __init__(self, target):
            self._target = target
            self._writes = 0

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return self._target.__exit__(*args)

        def write(self, data):
            self._writes += 1
            if self._writes == fail_on_write_step:
                raise OSError(f"Falha simulada na etapa de escrita #{fail_on_write_step}")
            return self._target.write(data)

        def __getattr__(self, name):
            return getattr(self._target, name)

    def mock_open(file, mode="r", *args, **kwargs):
        f = real_open(file, mode, *args, **kwargs)
        if "r+b" in mode:
            return StepFaultyFile(f)
        return f

    monkeypatch.setattr(builtins, "open", mock_open)

    fs = DiscoveredFilesystem("NTFS", start_lba=200, total_sectors=600, size_bytes=600 * 512)
    res = repair_gpt_partition_offset(str(img), 1, fs, backup_dir=backup_dir)
    assert res.success is False
    assert res.rollback_attempted is True
    assert res.rollback_confirmed is True
    assert res.final_state_uncertain is False
    assert img.read_bytes() == original


def test_repair_gpt_fsync_failure_and_rollback_uncertainty(tmp_path, monkeypatch):
    """
    P1/4.3 & 4.4:
    1) Falha de fsync durante o reparo aciona rollback verificado;
    2) Falha de fsync durante o próprio rollback reporta final_state_uncertain=True e rollback_confirmed=False.
    """
    import os
    from core.filesystem.scanner import DiscoveredFilesystem
    from core.recovery.repair import repair_gpt_partition_offset

    disk_bytes = _build_full_gpt_disk()
    img = tmp_path / "fsync_gpt_disk.bin"
    img.write_bytes(disk_bytes)
    original = img.read_bytes()

    real_fsync = os.fsync
    fsync_calls = {"count": 0, "fail_mode": "repair_only"}

    def controlled_fsync(fd):
        fsync_calls["count"] += 1
        # As primeiras 7 chamadas são os 7 snapshots preventivos (5 regiões + 2 agregados)
        if fsync_calls["fail_mode"] == "repair_only" and fsync_calls["count"] == 8:
            raise OSError("Falha de fsync ao persistir tabelas GPT no disco")
        if fsync_calls["fail_mode"] == "both_repair_and_rollback" and fsync_calls["count"] >= 8:
            raise OSError("Falha persistente de fsync também durante o rollback")
        return real_fsync(fd)

    monkeypatch.setattr(os, "fsync", controlled_fsync)
    fs = DiscoveredFilesystem("NTFS", start_lba=200, total_sectors=600, size_bytes=600 * 512)

    # Caso 1: Falha no fsync do reparo, mas fsync do rollback funciona -> rollback confirmado
    res1 = repair_gpt_partition_offset(str(img), 1, fs, backup_dir=tmp_path / "fsync_b1")
    assert res1.success is False
    assert res1.rollback_attempted is True
    assert res1.rollback_confirmed is True
    assert res1.final_state_uncertain is False
    assert img.read_bytes() == original

    # Caso 2: Falha no fsync do reparo E no fsync do rollback -> estado final incerto!
    fsync_calls["count"] = 0
    fsync_calls["fail_mode"] = "both_repair_and_rollback"
    res2 = repair_gpt_partition_offset(str(img), 1, fs, backup_dir=tmp_path / "fsync_b2")
    assert res2.success is False
    assert res2.rollback_attempted is True
    assert res2.rollback_confirmed is False
    assert res2.final_state_uncertain is True
    assert "INCERTO" in res2.message


def test_repair_gpt_aborts_before_any_disk_write_if_snapshot_corrupted(tmp_path, monkeypatch):
    """P1/4.2 & 4.4: Confirma que nenhuma gravação no disco ocorre se a validação de qualquer snapshot falhar."""
    import builtins
    from core.filesystem.scanner import DiscoveredFilesystem
    from core.recovery.repair import repair_gpt_partition_offset

    disk_bytes = _build_full_gpt_disk()
    img = tmp_path / "no_write_disk.bin"
    img.write_bytes(disk_bytes)
    original = img.read_bytes()

    disk_opened_rw = {"opened": False}
    real_open = builtins.open

    def tracking_open(file, mode="r", *args, **kwargs):
        if str(file) == str(img) and ("r+" in mode or "w" in mode or "a" in mode):
            disk_opened_rw["opened"] = True
        return real_open(file, mode, *args, **kwargs)

    monkeypatch.setattr(builtins, "open", tracking_open)

    # Corrompe a releitura do snapshot para falhar a verificação de integridade
    monkeypatch.setattr(Path, "read_bytes", lambda self: b"CORRUPTED_SNAPSHOT" if "backup_gpt_" in self.name else real_open(self, "rb").read())

    fs = DiscoveredFilesystem("NTFS", start_lba=200, total_sectors=600, size_bytes=600 * 512)
    res = repair_gpt_partition_offset(str(img), 1, fs, backup_dir=tmp_path / "corrupt_snap_dir")
    assert res.success is False
    assert disk_opened_rw["opened"] is False, "O disco jamais pode ser aberto para escrita se o snapshot falhar!"
    assert real_open(img, "rb").read() == original




