"""
tests/test_resilient_clone.py — Testes unitários aprofundados para o motor de clonagem resiliente.
Valida verificação LBA exata setor por setor, sem lacunas silenciosas, e retomada atômica (resume).
"""
import struct
from pathlib import Path
import pytest

from core.recovery.resilient_clone import CloneMap, ResilientCloner, SECTOR_SIZE


def test_resilient_clone_clean_disk(tmp_path: Path):
    total_sectors = 500
    one_sector = (b"SECTOR_DATA_" * 50)[:SECTOR_SIZE]
    src_data = one_sector * total_sectors

    def mock_reader(lba: int, count: int) -> bytes:
        start = lba * SECTOR_SIZE
        end = start + count * SECTOR_SIZE
        return src_data[start:end]

    dst_img = tmp_path / "cloned.img"
    cloner = ResilientCloner(
        device_path="mock://drive_clean",
        output_image_path=dst_img,
        total_sectors=total_sectors,
        block_sectors=64,
        skip_step_sectors=128
    )

    stats_list = []
    res = cloner.run(progress_cb=stats_list.append, reader_fn=mock_reader)

    assert res.success is True
    assert res.fully_recovered is True
    assert res.bad_sectors == 0
    assert res.rescued_sectors == total_sectors
    assert dst_img.exists()
    assert dst_img.stat().st_size == total_sectors * SECTOR_SIZE
    assert dst_img.read_bytes() == src_data
    assert cloner.map_path.exists()


def test_resilient_clone_synthetic_lba_exact_verification(tmp_path: Path):
    """
    P0 Teste Crítico: Cria imagem sintética na qual cada setor contém seu próprio LBA.
    Simula falhas no início, meio e blocos múltiplos, comprovando que:
    1. Todos os setores legíveis foram copiados para os offsets corretos;
    2. Todos os setores irrecuperáveis foram identificados e preenchidos com zeros;
    3. Nenhum setor foi omitido silenciosamente;
    4. A imagem final NÃO é declarada fully_recovered quando há falhas.
    """
    total_sectors = 1200
    # Setores defeituosos simulados (incluindo LBA 0 e 1, meio de bloco, e saltos)
    bad_lbas = {0, 1, 150, 151, 152, 500, 501, 800, 801, 802, 803}

    # Gera payload sintético identificável por LBA
    def generate_sector_bytes(lba: int) -> bytes:
        header = struct.pack(">Q", lba) + b"_LBA_SIG_"
        payload = bytes([lba % 256]) * (SECTOR_SIZE - len(header))
        return header + payload

    def mock_faulty_reader(lba: int, count: int) -> bytes:
        requested = set(range(lba, lba + count))
        # Se qualquer setor requisitado for bad, falha a leitura do bloco
        if requested & bad_lbas:
            raise OSError(f"Hardware I/O Error: Sector read timeout at LBA {lba}")
        return b"".join(generate_sector_bytes(s) for s in range(lba, lba + count))

    dst_img = tmp_path / "synthetic_rescued.img"
    cloner = ResilientCloner(
        device_path="mock://synthetic_faulty_drive",
        output_image_path=dst_img,
        total_sectors=total_sectors,
        block_sectors=64,
        skip_step_sectors=128
    )

    res = cloner.run(reader_fn=mock_faulty_reader)

    # 1. Status geral
    assert res.success is True
    assert res.fully_recovered is False, "Não pode ser declarado fully_recovered se houve bad sectors!"
    assert res.bad_sectors == len(bad_lbas)
    assert res.rescued_sectors == total_sectors - len(bad_lbas)
    assert res.skipped_sectors == 0, "Nenhum setor pode permanecer no estado skipped (*) ao final da Fase 2!"
    assert res.rescued_sectors + res.bad_sectors == total_sectors, "Todos os setores devem estar contabilizados sem lacunas!"

    # 2. Verificação setor por setor da imagem final
    assert dst_img.stat().st_size == total_sectors * SECTOR_SIZE
    with open(dst_img, "rb") as f:
        for s in range(total_sectors):
            data = f.read(SECTOR_SIZE)
            assert len(data) == SECTOR_SIZE
            if s in bad_lbas:
                # Setor irrecuperável deve conter zeros para manter alinhamento
                assert data == b"\x00" * SECTOR_SIZE, f"Bad sector LBA {s} não foi preenchido com zeros!"
            else:
                # Setor legível deve conter o payload exato daquele LBA
                expected_data = generate_sector_bytes(s)
                assert data == expected_data, f"Setor legível LBA {s} possui dados incorretos ou desalinhados!"


def test_resilient_clone_resume_from_interrupted_map(tmp_path: Path):
    """
    P1 Teste: Valida a retomada verídica (resume) a partir do arquivo de mapa.
    Interrompe a clonagem na fase 1 e confirma que a retomada só lê os blocos pendentes.
    """
    total_sectors = 400
    bad_lbas = {50, 51}

    reads_counter = {"count": 0}

    def mock_reader(lba: int, count: int) -> bytes:
        reads_counter["count"] += count
        if set(range(lba, lba + count)) & bad_lbas:
            raise OSError("I/O error")
        return b"VALID_SEC_" * ((count * SECTOR_SIZE) // 10) + b"\x00" * ((count * SECTOR_SIZE) % 10)

    dst_img = tmp_path / "resume_test.img"
    map_file = tmp_path / "resume_test.map"

    # Sessão 1: Clona os primeiros blocos e cancela
    cloner1 = ResilientCloner(
        device_path="mock://resume_drive",
        output_image_path=dst_img,
        total_sectors=total_sectors,
        block_sectors=32,
        skip_step_sectors=64,
        map_path=map_file
    )

    def cancel_after_some(stats):
        if stats.rescued_sectors >= 96:
            cloner1.cancel()

    res1 = cloner1.run(progress_cb=cancel_after_some, reader_fn=mock_reader)
    assert res1.success is False
    assert map_file.exists()

    reads_in_session1 = reads_counter["count"]
    assert reads_in_session1 > 0
    rescued_in_session1 = cloner1.clone_map.rescued_sectors
    assert rescued_in_session1 >= 96

    # Sessão 2: Retoma a clonagem
    reads_counter["count"] = 0
    cloner2 = ResilientCloner(
        device_path="mock://resume_drive",
        output_image_path=dst_img,
        total_sectors=total_sectors,
        block_sectors=32,
        skip_step_sectors=64,
        map_path=map_file
    )

    # O mapa carregado deve ter mantido os setores resgatados da sessão anterior
    assert cloner2.clone_map.rescued_sectors == rescued_in_session1

    res2 = cloner2.run(reader_fn=mock_reader)
    assert res2.success is True
    assert res2.bad_sectors == len(bad_lbas)
    assert res2.rescued_sectors == total_sectors - len(bad_lbas)
    # Na sessão 2, o número de leituras de blocos da Fase 1 deve ser menor que total_sectors
    # comprovando que os blocos já marcados '+' não foram repetidos!
    assert reads_counter["count"] < total_sectors


def test_clone_map_rejects_mismatched_device(tmp_path: Path):
    """P1 Teste: Garante que um mapa de outro disco ou outro destino seja recusado."""
    map_file = tmp_path / "other_disk.map"
    cmap = CloneMap(
        device_path="mock://disk_alpha",
        output_image=str(tmp_path / "alpha.img"),
        total_sectors=500
    )
    cmap.save_map(map_file)

    # Tenta carregar com dispositivo diferente
    loaded = CloneMap.load_map(
        map_path=map_file,
        expected_device="mock://disk_beta",
        expected_output=tmp_path / "alpha.img",
        expected_sectors=500
    )
    assert loaded is None

    # Tenta carregar com total_sectors diferente
    loaded2 = CloneMap.load_map(
        map_path=map_file,
        expected_device="mock://disk_alpha",
        expected_output=tmp_path / "alpha.img",
        expected_sectors=999
    )
    assert loaded2 is None


def test_clone_parameters_strictly_positive(tmp_path: Path):
    """P1 Teste: Validação estrita de parâmetros de execução (devem ser > 0)."""
    dst = tmp_path / "dst.img"
    with pytest.raises(ValueError, match="estritamente positivos"):
        ResilientCloner("mock://dev", dst, total_sectors=0)

    with pytest.raises(ValueError, match="estritamente positivos"):
        ResilientCloner("mock://dev", dst, total_sectors=100, block_sectors=0)

    with pytest.raises(ValueError, match="estritamente positivos"):
        ResilientCloner("mock://dev", dst, total_sectors=100, block_sectors=16, skip_step_sectors=-5)


def test_clone_map_rejects_corrupted_or_gapped_intervals(tmp_path: Path):
    """P1 Teste: Validação de intervalos contíguos e ausência de imagem física."""
    import json
    map_file = tmp_path / "corrupt_map.map"
    img_file = tmp_path / "missing_img.img"

    # Caso 1: Mapa com lacuna (gap) entre blocos (0..50 e depois 60..100)
    gapped_data = {
        "version": "2.0",
        "device_path": "mock://dev",
        "output_image": str(img_file),
        "total_sectors": 100,
        "blocks": [
            {"start_lba": 0, "sector_count": 50, "status": "+"},
            {"start_lba": 60, "sector_count": 40, "status": "?"}
        ]
    }
    map_file.write_text(json.dumps(gapped_data), encoding="utf-8")
    assert CloneMap.load_map(map_file, "mock://dev", img_file, 100) is None

    # Caso 2: Mapa com status inválido
    invalid_status = {
        "version": "2.0",
        "device_path": "mock://dev",
        "output_image": str(img_file),
        "total_sectors": 100,
        "blocks": [
            {"start_lba": 0, "sector_count": 100, "status": "UNKNOWN_BAD_STATUS"}
        ]
    }
    map_file.write_text(json.dumps(invalid_status), encoding="utf-8")
    assert CloneMap.load_map(map_file, "mock://dev", img_file, 100) is None

    # Caso 3: Mapa com contagem <= 0
    zero_count = {
        "version": "2.0",
        "device_path": "mock://dev",
        "output_image": str(img_file),
        "total_sectors": 100,
        "blocks": [
            {"start_lba": 0, "sector_count": 0, "status": "?"},
            {"start_lba": 0, "sector_count": 100, "status": "?"}
        ]
    }
    map_file.write_text(json.dumps(zero_count), encoding="utf-8")
    assert CloneMap.load_map(map_file, "mock://dev", img_file, 100) is None

    # Caso 4: Mapa alega setores resgatados (+), mas o arquivo de imagem não existe em disco
    claimed_rescued_missing_img = {
        "version": "2.0",
        "device_path": "mock://dev",
        "output_image": str(img_file),
        "total_sectors": 100,
        "blocks": [
            {"start_lba": 0, "sector_count": 100, "status": "+"}
        ]
    }
    map_file.write_text(json.dumps(claimed_rescued_missing_img), encoding="utf-8")
    assert not img_file.exists()
    assert CloneMap.load_map(map_file, "mock://dev", img_file, 100) is None
