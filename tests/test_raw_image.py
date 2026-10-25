"""
tests/test_raw_image.py — Testes unitários para create_raw_disk_image com verificações rigorosas de segurança P0 e integridade P1.
"""
from pathlib import Path
import pytest
from core.recovery.repair import create_raw_disk_image


def test_create_raw_disk_image_validates_positive_parameters(tmp_path):
    """Valida que parâmetros menores ou iguais a zero são rejeitados com ValueError."""
    src = tmp_path / "src.bin"
    src.write_bytes(b"\xAA" * 1024)
    dst = tmp_path / "dst.img"

    with pytest.raises(ValueError, match="sector_size deve ser estritamente positivo"):
        create_raw_disk_image(str(src), dst, sector_size=0)

    with pytest.raises(ValueError, match="chunk_sectors deve ser estritamente positivo"):
        create_raw_disk_image(str(src), dst, chunk_sectors=0)

    with pytest.raises(ValueError, match="max_sectors deve ser estritamente positivo"):
        create_raw_disk_image(str(src), dst, max_sectors=0)


def test_create_raw_disk_image_blocks_identical_source_and_destination(tmp_path):
    """P0: Impede que a imagem sobrescreva o próprio arquivo de origem."""
    src = tmp_path / "source.img"
    src.write_bytes(b"\xBB" * 2048)

    with pytest.raises(ValueError, match="mesmo dispositivo ou caminho|não pode coincidir"):
        create_raw_disk_image(str(src), src)


def test_create_raw_disk_image_blocks_same_physical_drive(tmp_path, monkeypatch):
    """P0: Impede gravação se assert_safe_source_and_destination indicar colisão de disco físico."""
    src = r"\\.\PhysicalDrive1"
    dst = tmp_path / "sub_nonexistent" / "image.img"

    # Simula conflito de mesmo disco físico
    def mock_assert_safe(source_str, target_path, is_directory=False):
        raise ValueError("Destino reside no mesmo disco físico da origem (PhysicalDrive1).")

    monkeypatch.setattr("core.hardware.device_resolver.assert_safe_source_and_destination", mock_assert_safe)

    with pytest.raises(ValueError, match="Destino reside no mesmo disco físico da origem"):
        create_raw_disk_image(src, dst)

    # Garante que o diretório NÃO foi criado antes do bloqueio
    assert not dst.parent.exists()


def test_create_raw_disk_image_detects_incomplete_read(tmp_path):
    """P1: Rejeita marcar imagem como sucesso se a origem tiver menos setores que o esperado."""
    src = tmp_path / "small_src.bin"
    src.write_bytes(b"\x55" * 1024)  # Apenas 2 setores de 512 bytes
    dst = tmp_path / "out.img"

    # Solicita 10 setores, mas o arquivo só possui 2
    ok = create_raw_disk_image(str(src), dst, sector_size=512, max_sectors=10)
    assert ok is False


def test_create_raw_disk_image_success_with_progress_and_cancel(tmp_path):
    """Valida fluxo de criação íntegra de imagem e suporte a cancelamento."""
    src = tmp_path / "valid_src.bin"
    src.write_bytes(b"\x12\x34" * 2048)  # 4096 bytes = 8 setores
    dst = tmp_path / "valid_out.img"

    progress_records = []
    def on_progress(cur, total, speed):
        progress_records.append((cur, total))

    # Cópia completa
    ok = create_raw_disk_image(str(src), dst, sector_size=512, max_sectors=8, chunk_sectors=2, progress_cb=on_progress)
    assert ok is True
    assert dst.read_bytes() == src.read_bytes()
    assert len(progress_records) > 0

    # Cancelamento imediato
    dst_canceled = tmp_path / "canceled.img"
    ok_cancel = create_raw_disk_image(
        str(src),
        dst_canceled,
        sector_size=512,
        max_sectors=8,
        chunk_sectors=2,
        cancel_check=lambda: True
    )
    assert ok_cancel is False


def test_create_raw_disk_image_refuses_overwrite_by_default(tmp_path):
    """P0: Impede que destino existente seja sobrescrito quando allow_overwrite=False (padrão)."""
    src = tmp_path / "src_data.bin"
    src.write_bytes(b"\x99" * 1024)
    dst = tmp_path / "already_exists.img"
    dst.write_bytes(b"ORIGINAL_IMPORTANT_DATA")

    with pytest.raises(FileExistsError, match=r"(?i)arquivo de destino já existe"):
        create_raw_disk_image(str(src), dst, allow_overwrite=False)

    # Conteúdo original permanece intocado
    assert dst.read_bytes() == b"ORIGINAL_IMPORTANT_DATA"


def test_create_raw_disk_image_allows_overwrite_when_flag_is_true(tmp_path):
    """P1: Permite sobrescrita controlada quando allow_overwrite=True."""
    src = tmp_path / "src_data_ovw.bin"
    src.write_bytes(b"\x77" * 2048)
    dst = tmp_path / "target_ovw.img"
    dst.write_bytes(b"OLD_CONTENT")

    ok = create_raw_disk_image(str(src), dst, allow_overwrite=True)
    assert ok is True
    assert dst.read_bytes() == src.read_bytes()


def test_create_raw_disk_image_rejects_unaligned_sector_reads(tmp_path):
    """P1: Falha e remove arquivo temporário se a leitura for desalinhada em relação ao setor."""
    src = tmp_path / "unaligned_src.bin"
    # 700 bytes não é múltiplo de 512
    src.write_bytes(b"\x33" * 700)
    dst = tmp_path / "unaligned_out.img"

    ok = create_raw_disk_image(str(src), dst, sector_size=512)
    assert ok is False
    assert not dst.exists()
    # Verifica que não ficou nenhum .tmp no diretório
    tmps = list(tmp_path.glob("*.tmp"))
    assert len(tmps) == 0
