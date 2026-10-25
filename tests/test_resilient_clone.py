"""
tests/test_resilient_clone.py — Testes unitários aprofundados para o motor de clonagem resiliente.
Valida verificação LBA exata setor por setor, sem lacunas silenciosas, e retomada atômica (resume).
"""
import json
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

    # Caso 5: Imagem existe, mas está truncada (menor que total_sectors * SECTOR_SIZE)
    img_file.write_bytes(b"\x00" * (50 * SECTOR_SIZE))  # Apenas 50 setores em vez de 100
    assert CloneMap.load_map(map_file, "mock://dev", img_file, 100) is None, "Deve rejeitar mapa quando imagem estiver truncada!"

    # Caso 6: Imagem com tamanho exato (100 setores) -> Aceito!
    img_file.write_bytes(b"\x00" * (100 * SECTOR_SIZE))
    assert CloneMap.load_map(map_file, "mock://dev", img_file, 100) is not None


def test_cloner_rejects_map_path_identical_to_output_image(tmp_path: Path):
    """P0 Teste: Impede categoricamente que map_path coincida com output_image_path (destruição de imagem)."""
    img_path = tmp_path / "cloned.img"
    with pytest.raises(ValueError, match="não pode ser idêntico à imagem de destino"):
        ResilientCloner(
            device_path="mock://drive",
            output_image_path=img_path,
            total_sectors=100,
            map_path=img_path  # COLISÃO PROIBIDA!
        )


def test_cloner_rejects_map_path_on_same_physical_disk_as_source(monkeypatch, tmp_path: Path):
    """P0 Teste: Impede que o arquivo de mapa ou seu temporário seja salvo no disco físico de origem."""
    def mock_drive_resolver(path):
        p = str(path).lower()
        if "physicaldrive0" in p or "c:" in p:
            return 0
        if "d:" in p:
            return 1
        return None

    monkeypatch.setattr("core.hardware.device_resolver.get_physical_drive_number", mock_drive_resolver)

    # Imagem de destino está em D: (disco 1 - seguro), mas mapa em C: (disco 0 - MESMO DISCO DE ORIGEM -> BLOQUEADO!)
    with pytest.raises(ValueError, match="mesmo disco físico"):
        ResilientCloner(
            device_path=r"\\.\PhysicalDrive0",
            output_image_path=r"D:\Imagens\disco.img",
            total_sectors=100,
            map_path=r"C:\Resgate\disco.map"
        )


def test_cloner_rejects_map_temporary_collision_with_output_image(tmp_path: Path):
    """
    P0 Teste Crítico: Garante que um arquivo temporário de mapa (ex: disco.map.tmp)
    não colida com a imagem de saída especificada pelo usuário, impedindo que save_map
    trunque a imagem recuperada ao persistir o mapa.
    """
    # Imagem nomeada propositalmente como o temporário do mapa
    img_colliding = tmp_path / ".disco.map.tmp"
    map_target = tmp_path / "disco.map"

    with pytest.raises(ValueError, match="colidir com seu temporário"):
        ResilientCloner(
            device_path="mock://drive",
            output_image_path=img_colliding,
            total_sectors=100,
            map_path=map_target
        )


def test_clone_map_rejects_mismatched_sector_size(tmp_path: Path):
    """P1 Teste: Garante que um mapa com sector_size incompatível (ex: 4096) seja rejeitado."""
    map_file = tmp_path / "test_4k.map"
    img_file = tmp_path / "test_4k.img"
    img_file.write_bytes(b"\x00" * (10 * 512))

    data = {
        "version": "2.0",
        "device_path": "mock://dev",
        "output_image": str(img_file),
        "total_sectors": 10,
        "sector_size": 4096,  # Setor incompatível
        "blocks": [{"start_lba": 0, "sector_count": 10, "status": "+"}]
    }
    map_file.write_text(json.dumps(data), encoding="utf-8")

    loaded = CloneMap.load_map(
        map_path=map_file,
        expected_device="mock://dev",
        expected_output=img_file,
        expected_sectors=10
    )
    assert loaded is None


def test_clone_map_rejects_oversized_image(tmp_path: Path):
    """P1 Teste: Garante que um arquivo de imagem físico com tamanho divergente (maior que o mapa) seja rejeitado."""
    map_file = tmp_path / "test_over.map"
    img_file = tmp_path / "test_over.img"
    # Grava 20 setores quando o mapa diz ter apenas 10
    img_file.write_bytes(b"\x00" * (20 * 512))

    data = {
        "version": "2.0",
        "device_path": "mock://dev",
        "output_image": str(img_file),
        "total_sectors": 10,
        "sector_size": 512,
        "blocks": [{"start_lba": 0, "sector_count": 10, "status": "+"}]
    }
    map_file.write_text(json.dumps(data), encoding="utf-8")

    loaded = CloneMap.load_map(
        map_path=map_file,
        expected_device="mock://dev",
        expected_output=img_file,
        expected_sectors=10
    )
    assert loaded is None


def test_cloner_fsync_failure_raises_persistence_error(tmp_path: Path, monkeypatch):
    """P1 Teste: Garante que falha física de fsync não seja ignorada silenciosamente."""
    import os
    img_file = tmp_path / "fsync_test.img"
    map_file = tmp_path / "fsync_test.map"

    cloner = ResilientCloner(
        device_path="mock://fsync_dev",
        output_image_path=img_file,
        total_sectors=10,
        map_path=map_file
    )

    def mock_fsync_fail(fd):
        raise OSError(5, "Input/output error")

    monkeypatch.setattr(os, "fsync", mock_fsync_fail)

    with pytest.raises(OSError, match="Falha de sincronização na imagem de destino|Falha de persistência física"):
        cloner.run(reader_fn=lambda lba, count: b"\x00" * (count * 512))

    # Garante que o clone_map em memória não permaneceu marcado como '+' após falha de fsync
    assert cloner.clone_map.rescued_sectors == 0


def test_clone_map_rejects_inconsistent_header_counters_and_invalid_version(tmp_path: Path):
    """Valida que load_map rejeita mapas cujos contadores no cabeçalho divergem da lista de blocos ou têm versão inválida."""
    map_file = tmp_path / "inconsistent_counters.map"
    img_file = tmp_path / "inconsistent_counters.img"
    img_file.write_bytes(b"\x00" * (10 * SECTOR_SIZE))

    # 1. rescued_sectors no cabeçalho (5) diverge da soma dos blocos '+' (10)
    bad_counters = {
        "version": "2.0",
        "device_path": "mock://dev",
        "output_image": str(img_file),
        "total_sectors": 10,
        "sector_size": 512,
        "rescued_sectors": 5,
        "bad_sectors": 0,
        "skipped_sectors": 5,
        "blocks": [{"start_lba": 0, "sector_count": 10, "status": "+"}],
    }
    map_file.write_text(json.dumps(bad_counters), encoding="utf-8")
    assert CloneMap.load_map(map_file, "mock://dev", img_file, 10) is None

    # 2. Versão desconhecida no cabeçalho
    bad_version = dict(bad_counters, version="99.9", rescued_sectors=10, skipped_sectors=0)
    map_file.write_text(json.dumps(bad_version), encoding="utf-8")
    assert CloneMap.load_map(map_file, "mock://dev", img_file, 10) is None


def test_cloner_detects_corrupted_destination_readback_and_rolls_back_memory_map(tmp_path: Path, monkeypatch):
    """Valida que divergência na releitura incremental da imagem de destino aborta o clone e reverte o mapa em memória."""
    import builtins
    img_file = tmp_path / "corrupt_readback.img"
    map_file = tmp_path / "corrupt_readback.map"

    cloner = ResilientCloner(
        device_path="mock://readback_dev",
        output_image_path=img_file,
        total_sectors=32,
        block_sectors=16,
        skip_step_sectors=16,
        map_path=map_file,
    )

    real_open = builtins.open

    class CorruptingDestFile:
        def __init__(self, target):
            self._target = target

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return self._target.__exit__(*args)

        def read(self, size=-1):
            data = self._target.read(size)
            # Corrompe os bytes lidos na verificação pós-escrita
            return b"\xFF" * len(data)

        def __getattr__(self, name):
            return getattr(self._target, name)

    def mock_open(file, mode="r", *args, **kwargs):
        f = real_open(file, mode, *args, **kwargs)
        if str(file) == str(img_file) and "r+b" in mode:
            return CorruptingDestFile(f)
        return f

    monkeypatch.setattr(builtins, "open", mock_open)

    with pytest.raises(OSError, match="Falha de coerência incremental"):
        cloner.run(reader_fn=lambda lba, count: b"\xAA" * (count * SECTOR_SIZE))

    # O mapa em memória deve ter sido revertido ao último checkpoint confirmado (0 setores resgatados)
    assert cloner.clone_map.rescued_sectors == 0


def test_cloner_blocks_existing_image_without_map_or_with_invalid_map(tmp_path: Path):
    """
    Seção 3 (P0):
    - Imagem existente sem mapa: bloquear e preservar imagem byte a byte inalterada.
    - Imagem existente com mapa malformado: bloquear, preservar imagem e mapa intactos.
    - Imagem existente com metadados de outro dispositivo: bloquear, preservar imagem e mapa intactos.
    - Reinicialização explícita (allow_overwrite=True): permitir somente depois da autorização.
    """
    total_sectors = 16
    dst_img = tmp_path / "valuable_recovery.img"
    map_file = tmp_path / "valuable_recovery.map"

    precious_bytes = b"PRECIOUS_RECOVERED_DATA_" * ((total_sectors * SECTOR_SIZE) // 24)
    precious_bytes += b"\x99" * ((total_sectors * SECTOR_SIZE) - len(precious_bytes))
    dst_img.write_bytes(precious_bytes)

    # 1. Imagem existente sem mapa -> deve recusar com FileExistsError
    with pytest.raises(FileExistsError, match="já existe, mas o arquivo de mapa"):
        ResilientCloner(
            device_path="mock://source_dev",
            output_image_path=dst_img,
            total_sectors=total_sectors,
            map_path=map_file,
        )
    assert dst_img.read_bytes() == precious_bytes

    # 2. Imagem existente com mapa malformado -> bloquear e não apagar o mapa nem tocar na imagem
    malformed_map_content = "{ invalid json map content !!!"
    map_file.write_text(malformed_map_content, encoding="utf-8")
    with pytest.raises(ValueError, match="inválido, corrompido ou incompatível"):
        ResilientCloner(
            device_path="mock://source_dev",
            output_image_path=dst_img,
            total_sectors=total_sectors,
            map_path=map_file,
        )
    assert dst_img.read_bytes() == precious_bytes
    assert map_file.read_text(encoding="utf-8") == malformed_map_content

    # 3. Imagem existente com mapa de outro dispositivo -> bloquear e manter ambos intactos
    other_dev_map = {
        "version": "2.1",
        "device_path": "mock://other_different_drive",
        "output_image": str(dst_img),
        "total_sectors": total_sectors,
        "sector_size": SECTOR_SIZE,
        "blocks": [{"start_lba": 0, "sector_count": total_sectors, "status": "+"}],
    }
    map_file.write_text(json.dumps(other_dev_map), encoding="utf-8")
    with pytest.raises(ValueError, match="inválido, corrompido ou incompatível"):
        ResilientCloner(
            device_path="mock://source_dev",
            output_image_path=dst_img,
            total_sectors=total_sectors,
            map_path=map_file,
        )
    assert dst_img.read_bytes() == precious_bytes

    # 4. Reinicialização explícita autorizada (allow_overwrite=True)
    cloner_authorized = ResilientCloner(
        device_path="mock://source_dev",
        output_image_path=dst_img,
        total_sectors=total_sectors,
        block_sectors=8,
        skip_step_sectors=8,
        map_path=map_file,
        allow_overwrite=True,
    )
    # Antes de run(), a imagem e o mapa antigos continuam intocados
    assert dst_img.read_bytes() == precious_bytes
    res = cloner_authorized.run(reader_fn=lambda lba, count: b"\xCC" * (count * SECTOR_SIZE))
    assert res.success is True
    assert res.fully_recovered is True
    assert dst_img.read_bytes() == b"\xCC" * (total_sectors * SECTOR_SIZE)


def test_cloner_detects_replaced_image_file_on_resume(tmp_path: Path):
    """
    Seção 3/4 (P0): Detecta se a imagem existente foi substituída por outro arquivo de mesmo tamanho
    durante a retomada (via hash SHA-256 de ancoragem no mapa) e recusa sem sobrescrever.
    """
    total_sectors = 20
    dst_img = tmp_path / "anchor_test.img"
    map_file = tmp_path / "anchor_test.map"

    cloner1 = ResilientCloner(
        device_path="mock://anchor_dev",
        output_image_path=dst_img,
        total_sectors=total_sectors,
        block_sectors=5,
        skip_step_sectors=5,
        map_path=map_file,
    )

    def stop_early(stats):
        if stats.rescued_sectors >= 5:
            cloner1.cancel()

    res1 = cloner1.run(progress_cb=stop_early, reader_fn=lambda lba, count: b"\x11" * (count * SECTOR_SIZE))
    assert res1.success is False
    assert map_file.exists()

    # Substitui o arquivo de imagem por outro arquivo com exatamente o mesmo tamanho (20 * 512 bytes), mas conteúdo diferente
    foreign_file_bytes = b"\xEE" * (total_sectors * SECTOR_SIZE)
    dst_img.write_bytes(foreign_file_bytes)

    with pytest.raises(ValueError, match="inválido, corrompido ou incompatível"):
        ResilientCloner(
            device_path="mock://anchor_dev",
            output_image_path=dst_img,
            total_sectors=total_sectors,
            map_path=map_file,
        )
    # Garante que o arquivo substituído permanece byte a byte inalterado
    assert dst_img.read_bytes() == foreign_file_bytes


def test_cloner_rejects_wrong_image_sizes_before_any_write(tmp_path: Path):
    """
    Seção 4 (P0):
    - Imagem existente menor que o esperado sem retomada válida: rejeitar antes da escrita.
    - Imagem existente maior que o esperado: rejeitar antes da escrita (nunca truncar silenciosamente).
    - Imagem existente com tamanho incompatível na retomada: rejeitar antes da escrita.
    """
    total_sectors = 10
    dst_img = tmp_path / "sized.img"
    map_file = tmp_path / "sized.map"

    # 1. Imagem maior que o esperado (20 setores em vez de 10) sem mapa
    oversized_bytes = b"\x77" * (20 * SECTOR_SIZE)
    dst_img.write_bytes(oversized_bytes)
    with pytest.raises(FileExistsError):
        ResilientCloner("mock://size_dev", dst_img, total_sectors=total_sectors, map_path=map_file)
    assert dst_img.read_bytes() == oversized_bytes

    # 2. Mesmo se um mapa foi criado inicialmente quando a imagem não existia, e depois uma imagem maior surgiu antes de run()
    dst_img.unlink()
    cloner = ResilientCloner("mock://size_dev", dst_img, total_sectors=total_sectors, map_path=map_file)
    # Outro processo cria arquivo maior antes de run()
    dst_img.write_bytes(oversized_bytes)
    with pytest.raises(FileExistsError, match="existe sem autorização de sobrescrita"):
        cloner.run(reader_fn=lambda lba, count: b"\x22" * (count * SECTOR_SIZE))
    assert dst_img.read_bytes() == oversized_bytes

    # 3. Na retomada, se a imagem mudar de tamanho (ficar menor ou maior) entre __init__ e run(), aborta antes de escrever
    dst_img.unlink()
    cloner_init = ResilientCloner("mock://size_dev", dst_img, total_sectors=total_sectors, block_sectors=2, map_path=map_file)
    cloner_init.run(
        progress_cb=lambda s: cloner_init.cancel() if s.rescued_sectors >= 2 else None,
        reader_fn=lambda lba, count: b"\x33" * (count * SECTOR_SIZE),
    )
    cloner_resume = ResilientCloner("mock://size_dev", dst_img, total_sectors=total_sectors, block_sectors=2, map_path=map_file)
    assert cloner_resume.is_resuming is True

    # Expande a imagem externamente antes de run()
    expanded_bytes = dst_img.read_bytes() + (b"\xFF" * SECTOR_SIZE)
    dst_img.write_bytes(expanded_bytes)
    with pytest.raises(ValueError, match="Falha na revalidação pré-escrita|incompatível"):
        cloner_resume.run(reader_fn=lambda lba, count: b"\x33" * (count * SECTOR_SIZE))
    assert dst_img.read_bytes() == expanded_bytes


def test_cloner_distinguishes_localized_bad_sector_from_fatal_errors_and_retries(tmp_path: Path):
    """
    Seção 5 (P0/P1):
    1. Falha transitória de leitura de setor na Fase 2 que se recupera na 2ª tentativa -> marcado '+', sem zeros.
    2. Bad sector persistente localizado -> tenta (1 + max_read_retries) vezes, preenche zeros e marca '-', registrando evidência.
    3. Desconexão do dispositivo (winerror=1167 / DeviceDisconnectedError) -> aborta imediatamente sem zerar setores restantes.
    4. Erro de permissão na origem (PermissionError / winerror=5) -> aborta imediatamente sem zerar setores.
    5. Erro inesperado de callback/programação (RuntimeError) -> aborta imediatamente sem tratar como bad sector.
    """
    from core.recovery.resilient_clone import (
        DeviceDisconnectedError,
        DestinationWriteError,
        SourceAccessError,
    )

    total_sectors = 16
    dst_img = tmp_path / "phase2_retry.img"
    map_file = tmp_path / "phase2_retry.map"

    # Cenário 1 & 2 combinados:
    # LBA 2 falha na Fase 1 e na 1ª tentativa da Fase 2, mas passa na 2ª tentativa da Fase 2 (transitório!)
    # LBA 3 falha em todas as tentativas (bad sector real!)
    lba2_attempts = {"phase2": 0}
    lba3_attempts = {"total": 0}

    def smart_mock_reader(lba: int, count: int) -> bytes:
        req = set(range(lba, lba + count))
        if count > 1 and (2 in req or 3 in req):
            raise OSError(23, "Data error (cyclic redundancy check)")
        if count == 1 and lba == 2:
            lba2_attempts["phase2"] += 1
            if lba2_attempts["phase2"] == 1:
                raise OSError(23, "Transient CRC error on first single-sector read")
            return b"RECOVERED_ON_RETRY" + b"\xAA" * (SECTOR_SIZE - 18)
        if count == 1 and lba == 3:
            lba3_attempts["total"] += 1
            raise OSError(23, "Permanent bad sector at LBA 3")
        return b"\x55" * (count * SECTOR_SIZE)

    cloner = ResilientCloner(
        device_path="mock://retry_drive",
        output_image_path=dst_img,
        total_sectors=total_sectors,
        block_sectors=4,
        skip_step_sectors=4,
        map_path=map_file,
        max_read_retries=2,
    )
    res = cloner.run(reader_fn=smart_mock_reader)
    assert res.success is True
    assert res.fully_recovered is False
    assert res.bad_sectors == 1
    assert res.rescued_sectors == total_sectors - 1
    assert lba2_attempts["phase2"] == 2
    assert lba3_attempts["total"] == 3  # 1 tentativa inicial + 2 retries
    assert 3 in res.bad_sector_evidence
    assert 2 not in res.bad_sector_evidence

    img_bytes = dst_img.read_bytes()
    assert img_bytes[2 * SECTOR_SIZE : 3 * SECTOR_SIZE].startswith(b"RECOVERED_ON_RETRY")
    assert img_bytes[3 * SECTOR_SIZE : 4 * SECTOR_SIZE] == b"\x00" * SECTOR_SIZE

    # Cenário 3: Desconexão física do dispositivo (winerror=1167 ERROR_DEVICE_NOT_CONNECTED) no meio da operação
    dst_disc = tmp_path / "disconnected.img"
    map_disc = tmp_path / "disconnected.map"

    def disconnecting_reader(lba: int, count: int) -> bytes:
        if lba >= 8:
            err = OSError(22, "The device is not connected")
            err.winerror = 1167
            raise err
        return b"\x44" * (count * SECTOR_SIZE)

    cloner_disc = ResilientCloner(
        device_path="mock://disc_drive",
        output_image_path=dst_disc,
        total_sectors=total_sectors,
        block_sectors=4,
        skip_step_sectors=4,
        map_path=map_disc,
    )
    with pytest.raises(DeviceDisconnectedError):
        cloner_disc.run(reader_fn=disconnecting_reader)
    # Nenhum setor deve ter sido marcado como bad ('-') nem pulado ('*') por causa de desconexão!
    assert cloner_disc.clone_map.bad_sectors == 0
    assert cloner_disc.clone_map.skipped_sectors == 0

    # Cenário 4: Erro de permissão na origem (PermissionError)
    dst_perm = tmp_path / "perm.img"
    cloner_perm = ResilientCloner("mock://perm_drive", dst_perm, total_sectors=total_sectors, block_sectors=4)
    with pytest.raises(SourceAccessError):
        cloner_perm.run(reader_fn=lambda lba, count: (_ for _ in ()).throw(PermissionError("Access denied")))
    assert cloner_perm.clone_map.bad_sectors == 0

    # Cenário 5: Exceção de programação (RuntimeError) no callback/reader não é tratada como bad sector
    dst_bug = tmp_path / "bug.img"
    cloner_bug = ResilientCloner("mock://bug_drive", dst_bug, total_sectors=total_sectors, block_sectors=4)
    with pytest.raises(RuntimeError, match="Unexpected bug"):
        cloner_bug.run(reader_fn=lambda lba, count: (_ for _ in ()).throw(RuntimeError("Unexpected bug")))
    assert cloner_bug.clone_map.bad_sectors == 0
    assert cloner_bug.clone_map.skipped_sectors == 0


def test_cloner_4096_sector_size_and_geometry_validation(tmp_path: Path, monkeypatch):
    """
    Seção 6 (P1):
    - Suporte completo a setores lógicos de 4096 bytes (4Kn): clonagem, mapa, retomada e alinhamento de offsets.
    - Rejeição de sector_size divergente entre mapa e dispositivo.
    - Rejeição de sector_size não suportado ou dispositivo físico sem geometria estabelecida.
    """
    sector_4k = 4096
    total_sectors = 24
    dst_4k = tmp_path / "disk_4kn.img"
    map_4k = tmp_path / "disk_4kn.map"

    def generate_4k_sector(lba: int) -> bytes:
        hdr = struct.pack(">Q", lba) + b"_4KN_SECTOR_"
        return hdr + bytes([lba % 251]) * (sector_4k - len(hdr))

    bad_lbas = {5, 12}

    def reader_4k(lba: int, count: int) -> bytes:
        if set(range(lba, lba + count)) & bad_lbas:
            raise OSError("4Kn sector read failure")
        return b"".join(generate_4k_sector(s) for s in range(lba, lba + count))

    # Sessão 1 (4Kn): para na metade e salva mapa
    cloner1 = ResilientCloner(
        device_path="mock://drive_4kn",
        output_image_path=dst_4k,
        total_sectors=total_sectors,
        block_sectors=4,
        skip_step_sectors=4,
        map_path=map_4k,
        sector_size=sector_4k,
    )
    res1 = cloner1.run(
        progress_cb=lambda st: cloner1.cancel() if st.rescued_sectors >= 8 else None,
        reader_fn=reader_4k,
    )
    assert res1.success is False
    assert dst_4k.stat().st_size == total_sectors * sector_4k

    # Tentar retomar com sector_size=512 contra o mapa de 4096 deve falhar antes da escrita!
    with pytest.raises(ValueError, match="inválido, corrompido ou incompatível"):
        ResilientCloner(
            device_path="mock://drive_4kn",
            output_image_path=dst_4k,
            total_sectors=total_sectors,
            map_path=map_4k,
            sector_size=512,
        )

    # Sessão 2 (4Kn): retoma com sector_size=4096 e conclui
    cloner2 = ResilientCloner(
        device_path="mock://drive_4kn",
        output_image_path=dst_4k,
        total_sectors=total_sectors,
        block_sectors=4,
        skip_step_sectors=4,
        map_path=map_4k,
        sector_size=sector_4k,
        verify_full_image_on_finish=True,
    )
    assert cloner2.is_resuming is True
    res2 = cloner2.run(reader_fn=reader_4k)
    assert res2.success is True
    assert res2.sector_size == 4096
    assert res2.incremental_sample_verified is True
    assert res2.full_image_verified is True
    assert res2.verified_sectors == total_sectors - len(bad_lbas)
    assert res2.bad_sectors == len(bad_lbas)
    assert res2.rescued_sectors == total_sectors - len(bad_lbas)

    raw_4k = dst_4k.read_bytes()
    assert len(raw_4k) == total_sectors * sector_4k
    for s in range(total_sectors):
        chunk = raw_4k[s * sector_4k : (s + 1) * sector_4k]
        if s in bad_lbas:
            assert chunk == b"\x00" * sector_4k
        else:
            assert chunk == generate_4k_sector(s)

    # Tamanho de setor não suportado (ex: 1024) deve ser rejeitado
    with pytest.raises(ValueError, match="não suportado"):
        ResilientCloner("mock://drive_bad_sec", tmp_path / "bad_sec.img", total_sectors=10, sector_size=1024)

    # Dispositivo físico cuja geometria não pôde ser obtida deve ser recusado antes de qualquer escrita
    monkeypatch.setattr("core.recovery.resilient_clone.get_device_sector_size", lambda p: None)
    monkeypatch.setattr("core.recovery.resilient_clone.is_physical_device_path", lambda p: True)
    with pytest.raises(ValueError, match="Não foi possível estabelecer a geometria"):
        ResilientCloner(r"\\.\PhysicalDrive99", tmp_path / "phys_no_geom.img", total_sectors=10)


def test_cloner_full_verification_cost_and_cancellation(tmp_path: Path):
    """
    Seção 7 (P1): Valida estimativa de custo de leitura da verificação completa e suporte a cancelamento cooperativo.
    """
    total_sectors = 32
    dst_img = tmp_path / "verify_cancel.img"
    cloner = ResilientCloner(
        device_path="mock://verify_dev",
        output_image_path=dst_img,
        total_sectors=total_sectors,
        block_sectors=8,
        skip_step_sectors=8,
    )
    res = cloner.run(reader_fn=lambda lba, count: b"\xAB" * (count * SECTOR_SIZE))
    assert res.success is True
    assert res.incremental_sample_verified is True
    assert res.full_image_verified is False

    cost = cloner.estimate_full_verification_cost(assumed_speed_mb_s=50.0)
    assert cost["rescued_sectors"] == total_sectors
    assert cost["bytes_to_read"] == total_sectors * SECTOR_SIZE
    assert cost["estimated_seconds"] >= 0.0

    # Cancela cooperativamente no meio da verificação completa
    def cancel_on_progress(verified, total):
        if verified >= 8:
            cloner.cancel()

    ok, verified_count, msg = cloner.verify_rescued_sectors(
        reader_fn=lambda lba, count: b"\xAB" * (count * SECTOR_SIZE),
        progress_cb=cancel_on_progress,
    )
    assert ok is False
    assert verified_count == 8
    assert "cancelada" in msg.lower()




