"""
tests/test_raw_image.py — Testes unitários para create_raw_disk_image com verificações rigorosas de segurança P0 e integridade P1.
"""
from pathlib import Path
import pytest
from core.recovery.repair import RawImageResult, RawImageStatus, create_raw_disk_image


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
    res = create_raw_disk_image(str(src), dst, sector_size=512, max_sectors=10)
    assert isinstance(res, RawImageResult)
    assert bool(res) is False
    assert res.status == RawImageStatus.PARTIAL
    assert res.sectors_written == 2


def test_create_raw_disk_image_success_with_progress_and_cancel(tmp_path):
    """Valida fluxo de criação íntegra de imagem, objeto RawImageResult e suporte a cancelamento."""
    src = tmp_path / "valid_src.bin"
    src.write_bytes(b"\x12\x34" * 2048)  # 4096 bytes = 8 setores
    dst = tmp_path / "valid_out.img"

    progress_records = []
    def on_progress(cur, total, speed):
        progress_records.append((cur, total))

    # Cópia completa
    res = create_raw_disk_image(str(src), dst, sector_size=512, max_sectors=8, chunk_sectors=2, progress_cb=on_progress)
    assert isinstance(res, RawImageResult)
    assert bool(res) is True
    assert res.status == RawImageStatus.COMPLETED
    assert res.sectors_written == 8
    assert res.bytes_written == 4096
    assert dst.read_bytes() == src.read_bytes()
    assert len(progress_records) > 0

    # Cancelamento imediato
    dst_canceled = tmp_path / "canceled.img"
    res_cancel = create_raw_disk_image(
        str(src),
        dst_canceled,
        sector_size=512,
        max_sectors=8,
        chunk_sectors=2,
        cancel_check=lambda: True
    )
    assert isinstance(res_cancel, RawImageResult)
    assert bool(res_cancel) is False
    assert res_cancel.status == RawImageStatus.CANCELLED
    assert not dst_canceled.exists()


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

    res = create_raw_disk_image(str(src), dst, allow_overwrite=True)
    assert isinstance(res, RawImageResult)
    assert bool(res) is True
    assert res.status == RawImageStatus.COMPLETED
    assert dst.read_bytes() == src.read_bytes()


def test_create_raw_disk_image_rejects_unaligned_sector_reads(tmp_path):
    """P1: Falha e remove arquivo temporário se a leitura for desalinhada em relação ao setor."""
    src = tmp_path / "unaligned_src.bin"
    # 700 bytes não é múltiplo de 512
    src.write_bytes(b"\x33" * 700)
    dst = tmp_path / "unaligned_out.img"

    res = create_raw_disk_image(str(src), dst, sector_size=512)
    assert isinstance(res, RawImageResult)
    assert bool(res) is False
    assert res.status == RawImageStatus.READ_ERROR
    assert not dst.exists()
    # Verifica que não ficou nenhum .tmp no diretório
    tmps = list(tmp_path.glob("*.tmp"))
    assert len(tmps) == 0


def test_create_raw_disk_image_handles_fsync_failure(tmp_path, monkeypatch):
    """P1: Trata adequadamente falhas de persistência física (fsync) retornando PERSISTENCE_ERROR."""
    src = tmp_path / "fsync_src.bin"
    src.write_bytes(b"\x44" * 1024)
    dst = tmp_path / "fsync_dst.img"

    def mock_fsync(fd):
        raise OSError("Falha simulada de hardware no fsync")

    monkeypatch.setattr("os.fsync", mock_fsync)

    res = create_raw_disk_image(str(src), dst, sector_size=512)
    assert isinstance(res, RawImageResult)
    assert bool(res) is False
    assert res.status == RawImageStatus.PERSISTENCE_ERROR
    assert not dst.exists()
    assert len(list(tmp_path.glob("*.tmp"))) == 0


def test_create_raw_disk_image_rejects_excessive_chunk_memory_and_non_512_multiple(tmp_path):
    """P0/3.3: Valida alinhamento de setor (múltiplo de 512) e limite máximo de memória por bloco (64 MiB)."""
    src = tmp_path / "mem_src.bin"
    src.write_bytes(b"\x11" * 1024)
    dst = tmp_path / "mem_dst.img"

    with pytest.raises(ValueError, match="múltiplo de 512"):
        create_raw_disk_image(str(src), dst, sector_size=500)

    with pytest.raises(ValueError, match="excede o limite"):
        create_raw_disk_image(str(src), dst, sector_size=512, chunk_sectors=200_000)


def test_create_raw_disk_image_empty_source_returns_partial(tmp_path):
    """P0/3.3: Origem vazia (0 bytes lidos) nunca pode retornar COMPLETED."""
    src = tmp_path / "empty_src.bin"
    src.write_bytes(b"")
    dst = tmp_path / "empty_dst.img"

    res = create_raw_disk_image(str(src), dst, sector_size=512)
    assert res.status == RawImageStatus.PARTIAL
    assert bool(res) is False
    assert res.sectors_written == 0
    assert not dst.exists()


def test_create_raw_disk_image_preserves_existing_destination_on_failed_overwrite(tmp_path, monkeypatch):
    """P0/3.2: Quando allow_overwrite=True, falha durante a criação da nova imagem preserva a imagem anterior válida."""
    src = tmp_path / "bad_src.bin"
    src.write_bytes(b"\x22" * 700)  # Tamanho desalinhado -> falha de leitura
    dst = tmp_path / "precious_existing.img"
    dst.write_bytes(b"VALID_PREVIOUS_IMAGE_BYTES" * 64)
    original_bytes = dst.read_bytes()

    res = create_raw_disk_image(str(src), dst, sector_size=512, allow_overwrite=True)
    assert res.status == RawImageStatus.READ_ERROR
    assert dst.exists()
    assert dst.read_bytes() == original_bytes
    assert len(list(tmp_path.glob("*.tmp"))) == 0


def test_create_raw_disk_image_concurrent_processes_no_overwrite_race(tmp_path):
    """
    P0/3.1: Testa dois processos concorrentes tentando finalizar imagens no mesmo destino
    com allow_overwrite=False sincronizados para passar na verificação inicial ao mesmo tempo.
    Apenas UM processo pode vencer a finalização atômica; o outro deve falhar sem sobrescrever.
    """
    import subprocess
    import sys

    src_a = tmp_path / "src_proc_a.bin"
    src_b = tmp_path / "src_proc_b.bin"
    payload_a = b"PROCESS_A_IMAGE_" * 128  # 2048 bytes (4 setores)
    payload_b = b"PROCESS_B_IMAGE_" * 128  # 2048 bytes (4 setores)
    src_a.write_bytes(payload_a)
    src_b.write_bytes(payload_b)

    shared_dst = tmp_path / "shared_target.img"
    ready_a = tmp_path / "ready_a.flag"
    ready_b = tmp_path / "ready_b.flag"

    worker_code = """
import sys, time
from pathlib import Path
from core.recovery.repair import create_raw_disk_image

src = Path(sys.argv[1])
dst = Path(sys.argv[2])
my_flag = Path(sys.argv[3])
other_flag = Path(sys.argv[4])

def sync_progress(cur, total, speed):
    my_flag.write_text("ready")
    deadline = time.time() + 5.0
    while not other_flag.exists() and time.time() < deadline:
        time.sleep(0.01)

try:
    res = create_raw_disk_image(str(src), dst, sector_size=512, chunk_sectors=2, allow_overwrite=False, progress_cb=sync_progress)
    print(f"STATUS:{res.status.value}")
except FileExistsError:
    print("STATUS:replace_error")
"""
    proc_a = subprocess.Popen(
        [sys.executable, "-c", worker_code, str(src_a), str(shared_dst), str(ready_a), str(ready_b)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    proc_b = subprocess.Popen(
        [sys.executable, "-c", worker_code, str(src_b), str(shared_dst), str(ready_b), str(ready_a)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )

    out_a, err_a = proc_a.communicate(timeout=15)
    out_b, err_b = proc_b.communicate(timeout=15)

    statuses = [out_a.strip().upper(), out_b.strip().upper()]
    assert statuses.count("STATUS:COMPLETED") == 1, f"Esperado exatamente 1 vencedor, obtido: {statuses} ({err_a} / {err_b})"
    assert statuses.count("STATUS:REPLACE_ERROR") == 1, f"Esperado exatamente 1 bloqueio atômico, obtido: {statuses}"

    assert shared_dst.exists()
    final_bytes = shared_dst.read_bytes()
    if out_a.strip().upper() == "STATUS:COMPLETED":
        assert final_bytes == payload_a
    else:
        assert final_bytes == payload_b

    # Nenhum arquivo .tmp pode restar
    assert len(list(tmp_path.glob("*.tmp"))) == 0


def test_raw_image_result_explicit_api_contract(tmp_path):
    """
    Seção 12 (P1): Valida propriedade explícita .success, __bool__, .status, .sectors_written, .bytes_written e .message
    em todos os estados de RawImageResult.
    """
    src = tmp_path / "contract_src.bin"
    src.write_bytes(b"\x5A" * 2048)  # 4 setores de 512 B
    dst = tmp_path / "contract_dst.img"

    res_ok = create_raw_disk_image(str(src), dst, sector_size=512, max_sectors=4)
    assert res_ok.status == RawImageStatus.COMPLETED
    assert res_ok.success is True
    assert bool(res_ok) is True
    assert res_ok.sectors_written == 4
    assert res_ok.bytes_written == 2048
    assert res_ok.message != ""

    res_partial = create_raw_disk_image(str(src), tmp_path / "partial_dst.img", sector_size=512, max_sectors=10)
    assert res_partial.status == RawImageStatus.PARTIAL
    assert res_partial.success is False
    assert bool(res_partial) is False
    assert res_partial.sectors_written == 4
    assert res_partial.bytes_written == 2048



