"""
core/recovery/resilient_clone.py — Motor de clonagem resiliente em duas fases com mapa de resgate (estilo ddrescue).
Permite resgatar discos com bad sectors sem travar o sistema, pulando áreas danificadas e raspando dados cirurgicamente.
Garante contabilização estrita de 100% dos LBAs sem lacunas silenciosas, persistência atômica,
durabilidade com fsync e validação rigorosa de integridade e parâmetros na retomada (resume).
"""
from __future__ import annotations

import errno
import hashlib
import json
import logging
import os
import tempfile
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

from core.hardware.device_resolver import (
    assert_safe_source_and_destination,
    get_device_sector_size,
    is_physical_device_path,
)

log = logging.getLogger("diskrescue.clone")

SECTOR_SIZE = 512
SUPPORTED_SECTOR_SIZES = (512, 4096)


class CloneError(OSError):
    """Exceção base para falhas estruturais, de dispositivo ou de destino durante a clonagem."""


class LocalizedSectorReadError(OSError):
    """Falha localizada de leitura de setor/bloco (ex: bad sector, CRC ou leitura parcial do setor)."""


class TransientSectorReadError(LocalizedSectorReadError):
    """Falha transitória de leitura de setor que pode ter sucesso em nova tentativa."""


class DeviceDisconnectedError(CloneError):
    """O dispositivo de origem foi desconectado, ficou indisponível ou parou de responder."""


class SourceAccessError(CloneError):
    """Permissão negada ou bloqueio de acesso ao dispositivo/arquivo de origem."""


class DestinationWriteError(CloneError):
    """Falha ao gravar dados na imagem de destino."""


class DestinationPersistenceError(CloneError):
    """Falha ao sincronizar (fsync), validar tamanho, verificar amostra ou persistir o mapa no destino."""


# Códigos WinError que indicam perda do dispositivo físico / mídia removida / unidade inexistente
_DISCONNECTED_WINERRORS = {
    2,     # ERROR_FILE_NOT_FOUND
    3,     # ERROR_PATH_NOT_FOUND
    15,    # ERROR_INVALID_DRIVE
    21,    # ERROR_NOT_READY
    55,    # ERROR_DEV_NOT_EXIST
    433,   # ERROR_NO_SUCH_DEVICE
    1167,  # ERROR_DEVICE_NOT_CONNECTED
}

# Códigos WinError que indicam erro de permissão / violação de compartilhamento na origem
_ACCESS_WINERRORS = {
    5,     # ERROR_ACCESS_DENIED
    32,    # ERROR_SHARING_VIOLATION
    33,    # ERROR_LOCK_VIOLATION
}


def _classify_source_read_error(exc: BaseException) -> BaseException:
    """
    Classifica falhas ocorridas durante a leitura da origem sem inspecionar mensagens de texto arbitrárias:
    - Exceções já tipadas de falha fatal ou localizada são preservadas.
    - Exceções que não herdam de OSError (ex: RuntimeError, ValueError, TypeError) nunca são tratadas como bad sector.
    - PermissionError ou winerror de acesso -> SourceAccessError (fatal).
    - winerror/errno de desconexão ou dispositivo ausente -> DeviceDisconnectedError (fatal).
    - Demais OSError de leitura de bloco/setor -> LocalizedSectorReadError.
    """
    if isinstance(
        exc,
        (
            DeviceDisconnectedError,
            SourceAccessError,
            DestinationWriteError,
            DestinationPersistenceError,
            LocalizedSectorReadError,
        ),
    ):
        return exc

    if not isinstance(exc, OSError):
        return exc

    if isinstance(exc, PermissionError):
        return SourceAccessError(f"Acesso negado ao ler dispositivo de origem: {exc}")

    winerr = getattr(exc, "winerror", None)
    if winerr in _ACCESS_WINERRORS:
        return SourceAccessError(f"Acesso negado ou bloqueado na origem (winerror={winerr}): {exc}")
    if winerr in _DISCONNECTED_WINERRORS:
        return DeviceDisconnectedError(f"Dispositivo de origem desconectado ou indisponível (winerror={winerr}): {exc}")

    err_no = getattr(exc, "errno", None)
    if err_no in {errno.EACCES, errno.EPERM}:
        return SourceAccessError(f"Permissão negada na origem (errno={err_no}): {exc}")
    if err_no in {errno.ENODEV, errno.ENXIO, errno.ENOENT}:
        return DeviceDisconnectedError(f"Dispositivo de origem ausente ou desconectado (errno={err_no}): {exc}")

    return LocalizedSectorReadError(str(exc))


@dataclass
class CloneBlock:
    start_lba: int
    sector_count: int
    status: str  # "?" = Não lido, "+" = Resgatado, "*" = Pulado (aguardando raspagem), "-" = Bad Sector confirmado


@dataclass
class CloneMap:
    device_path: str
    output_image: str
    total_sectors: int
    sector_size: int = SECTOR_SIZE
    rescued_sectors: int = 0
    bad_sectors: int = 0
    skipped_sectors: int = 0
    blocks: List[CloneBlock] = field(default_factory=list)
    anchor_lba: Optional[int] = None
    anchor_sha256: Optional[str] = None

    def __post_init__(self):
        if self.sector_size not in SUPPORTED_SECTOR_SIZES:
            raise ValueError(
                f"Tamanho de setor lógico não suportado no mapa ({self.sector_size}). "
                f"Valores suportados: {SUPPORTED_SECTOR_SIZES}."
            )
        if not self.blocks and self.total_sectors > 0:
            self.blocks = [CloneBlock(0, self.total_sectors, "?")]
        self.recalculate_counts()

    def recalculate_counts(self):
        """Atualiza a contagem consolidada de setores por estado."""
        self.rescued_sectors = sum(b.sector_count for b in self.blocks if b.status == "+")
        self.bad_sectors = sum(b.sector_count for b in self.blocks if b.status == "-")
        self.skipped_sectors = sum(b.sector_count for b in self.blocks if b.status == "*")

    def mark_range(self, mark_start: int, count: int, new_status: str):
        """
        Atualiza o estado de um intervalo de LBAs com precisão matemática,
        dividindo e fundindo blocos adjacentes para garantir zero lacunas e contiguidade estrita.
        """
        if count <= 0:
            return
        mark_end = mark_start + count

        retained: List[CloneBlock] = []
        for b in self.blocks:
            b_end = b.start_lba + b.sector_count
            if b_end <= mark_start or b.start_lba >= mark_end:
                retained.append(b)
                continue
            if b.start_lba < mark_start:
                retained.append(CloneBlock(b.start_lba, mark_start - b.start_lba, b.status))
            if b_end > mark_end:
                retained.append(CloneBlock(mark_end, b_end - mark_end, b.status))

        retained.append(CloneBlock(mark_start, count, new_status))
        retained.sort(key=lambda x: x.start_lba)

        # Fusão de blocos contíguos com mesmo status
        coalesced: List[CloneBlock] = []
        for b in retained:
            if not coalesced:
                coalesced.append(b)
            else:
                prev = coalesced[-1]
                if prev.start_lba + prev.sector_count == b.start_lba and prev.status == b.status:
                    prev.sector_count += b.sector_count
                else:
                    coalesced.append(b)

        self.blocks = coalesced
        self.recalculate_counts()

    def get_unread_ranges(self) -> List[Tuple[int, int]]:
        """Retorna lista de tuplas (start_lba, count) dos blocos ainda não lidos ('?')."""
        return [(b.start_lba, b.sector_count) for b in self.blocks if b.status == "?"]

    def get_skipped_ranges(self) -> List[Tuple[int, int]]:
        """Retorna lista de tuplas (start_lba, count) dos blocos pulados ('*') aguardando raspagem."""
        return [(b.start_lba, b.sector_count) for b in self.blocks if b.status == "*"]

    def _compute_image_anchor(self) -> Tuple[Optional[int], Optional[str]]:
        """Calcula hash SHA-256 do primeiro setor resgatado ('+') na imagem física para detectar substituição externa."""
        if self.rescued_sectors <= 0:
            return None, None
        img_path = Path(self.output_image)
        if not img_path.exists():
            return self.anchor_lba, self.anchor_sha256
        for b in self.blocks:
            if b.status == "+" and b.sector_count > 0:
                lba = b.start_lba
                offset = lba * self.sector_size
                try:
                    if img_path.stat().st_size >= offset + self.sector_size:
                        with open(img_path, "rb") as f:
                            f.seek(offset)
                            sec_bytes = f.read(self.sector_size)
                        if len(sec_bytes) == self.sector_size:
                            return lba, hashlib.sha256(sec_bytes).hexdigest()
                except OSError:
                    pass
                break
        return self.anchor_lba, self.anchor_sha256

    def save_map(self, map_path: Path):
        """
        Persiste o mapa atomicamente (gravação temporária + fsync + os.replace)
        garantindo durabilidade física contra quedas de energia.
        """
        self.recalculate_counts()
        anchor_lba, anchor_sha256 = self._compute_image_anchor()
        self.anchor_lba = anchor_lba
        self.anchor_sha256 = anchor_sha256

        data = {
            "version": "2.1",
            "device_path": self.device_path,
            "output_image": self.output_image,
            "total_sectors": self.total_sectors,
            "sector_size": self.sector_size,
            "rescued_sectors": self.rescued_sectors,
            "bad_sectors": self.bad_sectors,
            "skipped_sectors": self.skipped_sectors,
            "updated_at": time.time(),
            "blocks": [asdict(b) for b in self.blocks],
        }
        if self.anchor_lba is not None and self.anchor_sha256:
            data["anchor_lba"] = self.anchor_lba
            data["anchor_sha256"] = self.anchor_sha256

        map_path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_str = tempfile.mkstemp(
            dir=str(map_path.parent),
            prefix=f".{map_path.name}.",
            suffix=".tmp"
        )
        tmp_path = Path(tmp_str)
        fd_closed = False
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                fd_closed = True
                f.write(json.dumps(data, indent=2))
                f.flush()
                try:
                    if hasattr(f, "fileno"):
                        os.fsync(f.fileno())
                except OSError as err:
                    log.error("Falha ao sincronizar mapa no disco (fsync): %s", err)
                    raise DestinationPersistenceError(f"Falha de persistência física no mapa: {err}") from err
            os.replace(tmp_path, map_path)
        except Exception:
            if not fd_closed:
                try:
                    os.close(fd)
                except OSError:
                    pass
            if tmp_path.exists():
                try:
                    tmp_path.unlink()
                except OSError:
                    pass
            raise

    @classmethod
    def load_map(
        cls,
        map_path: Path,
        expected_device: str,
        expected_output: Path,
        expected_sectors: int,
        expected_sector_size: int = SECTOR_SIZE,
    ) -> CloneMap | None:
        """
        Carrega e valida rigorosamente se o mapa pertence à mesma origem, destino e geometria,
        rejeitando mapas com metadados ausentes, blocos malformados ou incompatibilidade
        com a imagem física em disco.
        """
        try:
            if not map_path.exists():
                return None
            if expected_sector_size not in SUPPORTED_SECTOR_SIZES:
                log.warning("Mapa rejeitado: expected_sector_size não suportado (%s).", expected_sector_size)
                return None

            data = json.loads(map_path.read_text(encoding="utf-8"))

            if not isinstance(data, dict):
                log.warning("Mapa rejeitado: estrutura raiz JSON inválida.")
                return None

            if "version" in data and str(data.get("version")) not in {"2.0", "2.1"}:
                log.warning("Mapa rejeitado: versão incompatível (%s).", data.get("version"))
                return None

            if data.get("total_sectors") != expected_sectors:
                log.warning("Mapa rejeitado: total_sectors incompatível (%s != %s)", data.get("total_sectors"), expected_sectors)
                return None

            # Metadados de dispositivo e imagem são obrigatórios e não podem ser vazios
            map_dev = data.get("device_path")
            map_out = data.get("output_image")
            if not map_dev or not map_out:
                log.warning("Mapa rejeitado: metadados 'device_path' ou 'output_image' ausentes.")
                return None

            clean_exp_dev = expected_device.replace("\\\\.\\", "").replace("\\", "/").strip().lower()
            clean_map_dev = str(map_dev).replace("\\\\.\\", "").replace("\\", "/").strip().lower()
            if clean_exp_dev != clean_map_dev:
                log.warning("Mapa rejeitado: dispositivo de origem diferente (%s != %s)", clean_map_dev, clean_exp_dev)
                return None

            clean_exp_out = str(expected_output.resolve()).replace("\\", "/").lower()
            clean_map_out = str(Path(str(map_out)).resolve()).replace("\\", "/").lower()
            if clean_exp_out != clean_map_out:
                log.warning("Mapa rejeitado: arquivo de destino diferente (%s != %s)", clean_map_out, clean_exp_out)
                return None

            blocks_raw = data.get("blocks", [])
            if not isinstance(blocks_raw, list) or not blocks_raw:
                log.warning("Mapa rejeitado: lista de blocos vazia ou malformada.")
                return None

            # Validação rigorosa dos blocos (cobertura total contígua, sem lacunas, contagens > 0)
            blocks = []
            expected_start = 0
            for b in blocks_raw:
                if not isinstance(b, dict):
                    return None
                start = b.get("start_lba", -1)
                count = b.get("sector_count", 0)
                status = b.get("status", "")
                if not isinstance(start, int) or not isinstance(count, int):
                    return None
                if count <= 0 or start != expected_start or status not in {"?", "+", "*", "-"}:
                    log.warning("Mapa rejeitado: bloco inconsistente ou com lacuna (%s)", b)
                    return None
                blocks.append(CloneBlock(start, count, status))
                expected_start += count

            if expected_start != expected_sectors:
                log.warning("Mapa rejeitado: cobertura total de setores (%d) difere de total_sectors (%d)", expected_start, expected_sectors)
                return None

            # Validação estrita do tamanho do setor
            map_sector_size = data.get("sector_size", SECTOR_SIZE)
            if not isinstance(map_sector_size, int) or map_sector_size not in SUPPORTED_SECTOR_SIZES:
                log.warning("Mapa rejeitado: sector_size inválido ou não suportado (%s).", map_sector_size)
                return None
            if map_sector_size != expected_sector_size:
                log.warning("Mapa rejeitado: sector_size incompatível (%s != %s)", map_sector_size, expected_sector_size)
                return None

            # Verificação de consistência interna dos contadores consolidados (quando presentes no JSON)
            rescued_in_map = sum(b.sector_count for b in blocks if b.status == "+")
            bad_in_map = sum(b.sector_count for b in blocks if b.status == "-")
            skipped_in_map = sum(b.sector_count for b in blocks if b.status == "*")

            if "rescued_sectors" in data and data["rescued_sectors"] != rescued_in_map:
                log.warning("Mapa rejeitado: contador 'rescued_sectors' (%s) diverge dos blocos (%d).", data["rescued_sectors"], rescued_in_map)
                return None
            if "bad_sectors" in data and data["bad_sectors"] != bad_in_map:
                log.warning("Mapa rejeitado: contador 'bad_sectors' (%s) diverge dos blocos (%d).", data["bad_sectors"], bad_in_map)
                return None
            if "skipped_sectors" in data and data["skipped_sectors"] != skipped_in_map:
                log.warning("Mapa rejeitado: contador 'skipped_sectors' (%s) diverge dos blocos (%d).", data["skipped_sectors"], skipped_in_map)
                return None

            # Verificação de coerência com o arquivo físico de imagem
            expected_size = expected_sectors * expected_sector_size
            has_progress = (rescued_in_map > 0 or bad_in_map > 0 or skipped_in_map > 0)
            if has_progress and not expected_output.exists():
                log.warning(
                    "Mapa rejeitado: o mapa indica progresso anterior, mas o arquivo de imagem '%s' não existe.",
                    expected_output
                )
                return None

            if expected_output.exists():
                actual_size = expected_output.stat().st_size
                if actual_size != expected_size:
                    log.warning(
                        "Mapa rejeitado: discrepância no tamanho da imagem física (%d bytes != %d bytes esperados para %d setores de %d bytes).",
                        actual_size, expected_size, expected_sectors, expected_sector_size
                    )
                    return None

                # Validação de assinatura de ancoragem (detecta substituição da imagem por outro arquivo de mesmo tamanho)
                anchor_lba = data.get("anchor_lba")
                anchor_sha256 = data.get("anchor_sha256")
                if anchor_lba is not None and anchor_sha256:
                    if not isinstance(anchor_lba, int) or anchor_lba < 0 or anchor_lba >= expected_sectors:
                        log.warning("Mapa rejeitado: anchor_lba inválido (%s).", anchor_lba)
                        return None
                    with open(expected_output, "rb") as img_f:
                        img_f.seek(anchor_lba * expected_sector_size)
                        sec_bytes = img_f.read(expected_sector_size)
                    actual_hash = hashlib.sha256(sec_bytes).hexdigest()
                    if actual_hash != str(anchor_sha256):
                        log.warning(
                            "Mapa rejeitado: o conteúdo da imagem no LBA âncora %d diverge do mapa (arquivo substituído ou modificado).",
                            anchor_lba,
                        )
                        return None

            cmap = cls(
                device_path=str(map_dev),
                output_image=str(expected_output),
                total_sectors=expected_sectors,
                sector_size=map_sector_size,
                blocks=blocks,
                anchor_lba=data.get("anchor_lba"),
                anchor_sha256=data.get("anchor_sha256"),
            )
            cmap.recalculate_counts()
            return cmap
        except Exception as exc:
            log.warning("Falha ao carregar mapa de clonagem existente: %s", exc)
            return None


@dataclass
class ResilientCloneStats:
    current_sector: int
    total_sectors: int
    rescued_sectors: int
    bad_sectors: int
    skipped_sectors: int
    speed_mb_s: float
    phase: str  # "Fase 1: Passagem Rápida" ou "Fase 2: Raspagem Cirúrgica"


@dataclass
class CloningResult:
    """
    Resultado detalhado da clonagem distinguindo conclusão operacional de integridade 100%,
    bem como verificação incremental por amostragem (checkpoint) vs verificação completa pós-clonagem.
    """
    success: bool
    fully_recovered: bool
    total_sectors: int
    rescued_sectors: int
    bad_sectors: int
    skipped_sectors: int
    map_path: Path
    message: str = ""
    sector_size: int = SECTOR_SIZE
    incremental_sample_verified: bool = True
    full_image_verified: bool = False
    verified_sectors: int = 0
    bad_sector_evidence: Dict[int, str] = field(default_factory=dict)

    def __bool__(self) -> bool:
        return self.success


class ResilientCloner:
    def __init__(
        self,
        device_path: str,
        output_image_path: Path,
        total_sectors: int,
        block_sectors: int = 128,  # 64 KB por leitura na fase rápida (para setores de 512 B)
        skip_step_sectors: int = 2048,  # 1 MB de salto após erro (para setores de 512 B)
        map_path: Path | None = None,
        sector_size: Optional[int] = None,
        allow_overwrite: bool = False,
        fresh_start: bool = False,
        max_read_retries: int = 2,
        retry_delay_s: float = 0.0,
        verify_full_image_on_finish: bool = False,
    ):
        # Validação estrita de parâmetros de execução (impedir loops infinitos ou parâmetros não positivos)
        if total_sectors <= 0 or block_sectors <= 0 or skip_step_sectors <= 0:
            raise ValueError("Os parâmetros total_sectors, block_sectors e skip_step_sectors devem ser estritamente positivos (> 0).")
        if max_read_retries < 0:
            raise ValueError("O parâmetro max_read_retries não pode ser negativo.")

        self.device_path = device_path
        self.output_image_path = Path(output_image_path)
        self.total_sectors = total_sectors
        self.block_sectors = block_sectors
        self.skip_step_sectors = skip_step_sectors
        self.map_path = Path(map_path) if map_path else self.output_image_path.with_suffix(".map")
        self.allow_overwrite = bool(allow_overwrite or fresh_start)
        self.max_read_retries = int(max_read_retries)
        self.retry_delay_s = float(retry_delay_s)
        self.verify_full_image_on_finish = bool(verify_full_image_on_finish)
        self.cancel_requested = False
        self.bad_sector_evidence: Dict[int, str] = {}

        # Resolução e validação estrita da geometria (tamanho de setor lógico)
        hw_sector_size = get_device_sector_size(self.device_path)
        if sector_size is not None:
            if not isinstance(sector_size, int) or sector_size not in SUPPORTED_SECTOR_SIZES:
                raise ValueError(
                    f"Tamanho de setor lógico inválido ou não suportado ({sector_size}). "
                    f"Suportados: {SUPPORTED_SECTOR_SIZES}."
                )
            if hw_sector_size is not None and hw_sector_size != sector_size:
                raise ValueError(
                    f"Geometria conflitante: sector_size informado ({sector_size} B) difere do setor lógico "
                    f"reportado pelo hardware ({hw_sector_size} B)."
                )
            self.sector_size = sector_size
        else:
            if is_physical_device_path(self.device_path):
                if hw_sector_size is None:
                    raise ValueError(
                        f"Não foi possível estabelecer a geometria (tamanho de setor lógico) do dispositivo físico '{self.device_path}'. "
                        "Operação recusada antes da escrita para evitar desalinhamento de LBAs."
                    )
                if hw_sector_size not in SUPPORTED_SECTOR_SIZES:
                    raise ValueError(
                        f"O dispositivo '{self.device_path}' possui tamanho de setor lógico não suportado ({hw_sector_size} B). "
                        f"Suportados: {SUPPORTED_SECTOR_SIZES}."
                    )
                self.sector_size = hw_sector_size
            else:
                self.sector_size = hw_sector_size if hw_sector_size in SUPPORTED_SECTOR_SIZES else SECTOR_SIZE

        # Validação de segurança P0 Fail-Closed: Impedir gravação no mesmo disco físico
        assert_safe_source_and_destination(self.device_path, self.output_image_path)

        # Validação de segurança P0 do arquivo de mapa:
        # 1. Impedir que map_path ou qualquer de seus arquivos temporários coincida com output_image_path
        tmp_map_1 = self.map_path.parent / f".{self.map_path.name}.tmp"
        tmp_map_2 = self.map_path.with_suffix(".map.tmp")

        try:
            out_res = self.output_image_path.resolve()
            if (
                out_res == self.map_path.resolve()
                or out_res == tmp_map_1.resolve()
                or out_res == tmp_map_2.resolve()
            ):
                raise ValueError(
                    f"Conflito crítico: o arquivo de mapa (map_path) não pode ser idêntico à imagem de destino "
                    f"nem colidir com seu temporário ({self.map_path} vs {self.output_image_path}). Salvar o mapa sobre a imagem destruiria os dados resgatados."
                )
        except OSError:
            out_str = str(self.output_image_path).strip().lower()
            if out_str in (
                str(self.map_path).strip().lower(),
                str(tmp_map_1).strip().lower(),
                str(tmp_map_2).strip().lower(),
            ):
                raise ValueError("O arquivo de mapa (map_path) ou seu temporário não pode coincidir com a imagem de destino.")

        # 2. Impedir que o mapa ou seus temporários sejam gravados no mesmo disco físico de origem
        assert_safe_source_and_destination(self.device_path, self.map_path)
        assert_safe_source_and_destination(self.device_path, tmp_map_1)
        assert_safe_source_and_destination(self.device_path, tmp_map_2)

        # Verifica espaço em disco disponível no destino
        try:
            import shutil
            target_parent = self.output_image_path.parent
            target_parent.mkdir(parents=True, exist_ok=True)
            free_bytes = shutil.disk_usage(target_parent).free
            needed_bytes = total_sectors * self.sector_size
            if free_bytes < needed_bytes:
                log.warning(
                    "Espaço livre no destino (%s MB) é menor que o tamanho estimado da imagem (%s MB).",
                    free_bytes // (1024 * 1024),
                    needed_bytes // (1024 * 1024)
                )
        except Exception as exc:
            log.debug("Não foi possível verificar espaço livre no destino: %s", exc)

        # Política explícita de proteção de imagem e mapa existentes (Seções 3 e 4):
        # - Reinicialização explícita (allow_overwrite / fresh_start): inicia novo mapa em memória sem apagar o antigo até run()
        # - Mapa existente válido e compatível: retoma
        # - Mapa existente inválido/incompatível sem allow_overwrite: recusa operação
        # - Imagem existente sem mapa compatível e sem allow_overwrite: recusa operação
        map_exists = self.map_path.exists()
        img_exists = self.output_image_path.exists()
        target_size = self.total_sectors * self.sector_size

        if self.allow_overwrite:
            self.is_resuming = False
            self.clone_map = CloneMap(
                device_path=device_path,
                output_image=str(self.output_image_path),
                total_sectors=total_sectors,
                sector_size=self.sector_size,
                blocks=[],
            )
            log.info("Reinicialização explícita autorizada (allow_overwrite=True) para '%s'.", self.output_image_path)
        elif map_exists:
            loaded = CloneMap.load_map(
                self.map_path,
                expected_device=self.device_path,
                expected_output=self.output_image_path,
                expected_sectors=self.total_sectors,
                expected_sector_size=self.sector_size,
            )
            if loaded is None:
                raise ValueError(
                    f"O arquivo de mapa existente '{self.map_path}' é inválido, corrompido ou incompatível com a origem, "
                    f"destino ou geometria ({self.total_sectors} setores de {self.sector_size} B). "
                    "Operação bloqueada por segurança para impedir sobrescrita de dados."
                )
            if img_exists:
                actual_size = self.output_image_path.stat().st_size
                if actual_size != target_size:
                    raise ValueError(
                        f"Tamanho da imagem existente ({actual_size} bytes) é incompatível com a geometria esperada ({target_size} bytes)."
                    )
            self.clone_map = loaded
            self.is_resuming = True
            log.info("Retomando clonagem a partir do mapa verificado: %s", self.map_path)
        else:
            if img_exists:
                raise FileExistsError(
                    f"A imagem de destino '{self.output_image_path}' já existe, mas o arquivo de mapa '{self.map_path}' "
                    "está ausente. Operação recusada por padrão para evitar sobrescrita de dados existentes."
                )
            self.is_resuming = False
            self.clone_map = CloneMap(
                device_path=device_path,
                output_image=str(self.output_image_path),
                total_sectors=total_sectors,
                sector_size=self.sector_size,
                blocks=[],
            )

    def cancel(self):
        self.cancel_requested = True

    def estimate_full_verification_cost(self, assumed_speed_mb_s: float = 80.0) -> Dict[str, float | int]:
        """
        Calcula o custo de leitura (bytes e setores) e o tempo estimado para a verificação completa
        pós-clonagem de todos os setores resgatados ('+').
        """
        self.clone_map.recalculate_counts()
        rescued_sectors = self.clone_map.rescued_sectors
        bytes_to_read = rescued_sectors * self.sector_size
        speed_bps = max(1.0, assumed_speed_mb_s) * 1024 * 1024
        estimated_seconds = bytes_to_read / speed_bps
        return {
            "rescued_sectors": rescued_sectors,
            "bytes_to_read": bytes_to_read,
            "estimated_seconds": round(estimated_seconds, 3),
        }

    def verify_rescued_sectors(
        self,
        reader_fn: Optional[Callable[[int, int], bytes]] = None,
        progress_cb: Optional[Callable[[int, int], None]] = None,
    ) -> Tuple[bool, int, str]:
        """
        Executa verificação completa opcional pós-clonagem: relê todos os blocos marcados como '+'
        na imagem de destino (e compara com a origem quando reader_fn é fornecido, ou valida legibilidade/alinhamento),
        suportando cancelamento cooperativo.
        """
        target_size = self.total_sectors * self.sector_size
        if not self.output_image_path.exists():
            return False, 0, "Arquivo de imagem de destino não existe para verificação completa."
        actual_size = self.output_image_path.stat().st_size
        if actual_size != target_size:
            return False, 0, f"Tamanho da imagem ({actual_size} B) difere do esperado ({target_size} B)."

        verified_sectors = 0
        total_to_verify = sum(b.sector_count for b in self.clone_map.blocks if b.status == "+")

        with open(self.output_image_path, "rb") as img_f:
            for b in self.clone_map.blocks:
                if b.status != "+":
                    continue
                cur_lba = b.start_lba
                end_lba = b.start_lba + b.sector_count
                while cur_lba < end_lba:
                    if self.cancel_requested:
                        return False, verified_sectors, "Verificação completa pós-clonagem cancelada pelo operador."
                    chunk_sectors = min(self.block_sectors, end_lba - cur_lba)
                    chunk_bytes = chunk_sectors * self.sector_size
                    img_f.seek(cur_lba * self.sector_size)
                    img_data = img_f.read(chunk_bytes)
                    if len(img_data) != chunk_bytes:
                        return (
                            False,
                            verified_sectors,
                            f"Leitura truncada na imagem de destino durante verificação completa no LBA {cur_lba}.",
                        )
                    if reader_fn is not None:
                        src_data = reader_fn(cur_lba, chunk_sectors)
                        if src_data != img_data:
                            return (
                                False,
                                verified_sectors,
                                f"Divergência byte a byte detectada na verificação completa pós-clonagem (LBA {cur_lba}..{cur_lba + chunk_sectors}).",
                            )
                    verified_sectors += chunk_sectors
                    cur_lba += chunk_sectors
                    if progress_cb:
                        progress_cb(verified_sectors, total_to_verify)

        return True, verified_sectors, f"Verificação completa concluída: {verified_sectors:,} setores confirmados."

    def _validate_destination_before_write(self, target_size: int) -> str:
        """
        Valida a existência, tamanho e integridade do destino e do mapa ANTES de abrir o arquivo em modo de escrita.
        Retorna o modo de abertura seguro ('r+b' para retomada validada, 'w+b' para nova imagem ou sobrescrita autorizada).
        """
        img_exists = self.output_image_path.exists()
        map_exists = self.map_path.exists()

        if self.is_resuming:
            if not map_exists:
                raise ValueError(
                    f"O arquivo de mapa '{self.map_path}' desapareceu antes do início da escrita. Operação abortada."
                )
            reverified_map = CloneMap.load_map(
                self.map_path,
                expected_device=self.device_path,
                expected_output=self.output_image_path,
                expected_sectors=self.total_sectors,
                expected_sector_size=self.sector_size,
            )
            if reverified_map is None:
                raise ValueError(
                    "Falha na revalidação pré-escrita: o mapa ou a imagem de destino tornou-se inválido, "
                    "incompatível ou foi modificado externamente antes do início da gravação."
                )
            if img_exists:
                cur_size = self.output_image_path.stat().st_size
                if cur_size != target_size:
                    raise ValueError(
                        f"Tamanho da imagem existente ({cur_size} bytes) é incompatível com o tamanho esperado "
                        f"({target_size} bytes) antes da escrita. A imagem não será truncada nem expandida silenciosamente."
                    )
                return "r+b"
            else:
                # Mapa válido sem progresso prévio (todos os blocos '?') e imagem ainda não criada
                return "w+b"
        else:
            if img_exists and not self.allow_overwrite:
                raise FileExistsError(
                    f"A imagem de destino '{self.output_image_path}' existe sem autorização de sobrescrita. "
                    "Operação abortada antes de qualquer escrita."
                )
            if map_exists and not self.allow_overwrite:
                raise ValueError(
                    f"O arquivo de mapa '{self.map_path}' surgiu sem retomada válida nem autorização de sobrescrita."
                )
            return "w+b"

    def run(
        self,
        progress_cb: Optional[Callable[[ResilientCloneStats], None]] = None,
        reader_fn: Optional[Callable[[int, int], bytes]] = None
    ) -> CloningResult:
        """Executa a clonagem em 2 fases: varredura rápida com saltos e raspagem cirúrgica setor a setor."""
        self.output_image_path.parent.mkdir(parents=True, exist_ok=True)
        t_start = time.time()
        last_cb_time = 0.0
        last_save_time = t_start
        bytes_rescued_in_run = 0
        iteration_count = 0
        target_size = self.total_sectors * self.sector_size

        # Validação obrigatória pré-escrita (Seções 3 e 4): impede truncamento, expansão silenciosa ou sobrescrita
        open_mode = self._validate_destination_before_write(target_size)

        with open(self.output_image_path, open_mode) as dst:
            dst.seek(0, os.SEEK_END)
            cur_size = dst.tell()
            if open_mode == "w+b":
                dst.truncate(target_size)
                dst.flush()
                try:
                    if hasattr(dst, "fileno"):
                        os.fsync(dst.fileno())
                except OSError as err:
                    log.error("Falha ao sincronizar pré-alocação da imagem (fsync): %s", err)
                    raise DestinationPersistenceError(f"Falha de sincronização na imagem de destino: {err}") from err
            elif cur_size != target_size:
                raise ValueError(
                    f"Inconsistência de tamanho na imagem existente ao abrir para retomada ({cur_size} != {target_size} bytes)."
                )

        # Leitor customizado ou leitura nativa direta do dispositivo (abertura estrita somente leitura)
        def default_reader(lba: int, count: int) -> bytes:
            with open(self.device_path, "rb") as dev:
                dev.seek(lba * self.sector_size)
                return dev.read(count * self.sector_size)

        read_block = reader_fn or default_reader

        # Snapshot dos blocos já confirmados em disco para rollback do mapa em memória se ocorrer falha
        synced_blocks_snapshot = [CloneBlock(b.start_lba, b.sector_count, b.status) for b in self.clone_map.blocks]
        last_written_sample: Optional[Tuple[int, bytes]] = None

        def write_to_destination(dst_f, offset: int, payload: bytes) -> None:
            try:
                dst_f.seek(offset)
                written = dst_f.write(payload)
            except OSError as err:
                raise DestinationWriteError(
                    f"Erro de escrita na imagem de destino no offset {offset}: {err}"
                ) from err
            if written != len(payload):
                raise DestinationWriteError(
                    f"Escrita incompleta na imagem de destino ({written} != {len(payload)} bytes)."
                )

        def sync_and_checkpoint(dst_f):
            nonlocal synced_blocks_snapshot, last_written_sample
            try:
                dst_f.flush()
                if hasattr(dst_f, "fileno"):
                    os.fsync(dst_f.fileno())
            except OSError as err:
                log.error("Falha de persistência física na imagem (fsync): %s", err)
                raise DestinationPersistenceError(f"Falha de sincronização na imagem de destino: {err}") from err

            # Validação incremental por amostragem da última escrita do lote:
            # Confirma integridade do último bloco gravado + tamanho físico exato da imagem sem reler o disco inteiro a cada 2s
            if last_written_sample is not None:
                sample_offset, sample_bytes = last_written_sample
                try:
                    dst_f.seek(sample_offset)
                    verified_bytes = dst_f.read(len(sample_bytes))
                except OSError as err:
                    raise DestinationPersistenceError(
                        f"Erro ao reler amostra incremental na imagem de destino no offset {sample_offset}: {err}"
                    ) from err
                if verified_bytes != sample_bytes:
                    raise DestinationPersistenceError(
                        f"Falha de coerência incremental na imagem de destino no offset {sample_offset}: "
                        "dados lidos divergem dos gravados."
                    )
                last_written_sample = None

            dst_f.seek(0, os.SEEK_END)
            if dst_f.tell() != target_size:
                raise DestinationPersistenceError(
                    f"Inconsistência no tamanho físico da imagem durante clonagem ({dst_f.tell()} != {target_size} bytes)."
                )

            self.clone_map.save_map(self.map_path)
            synced_blocks_snapshot = [CloneBlock(b.start_lba, b.sector_count, b.status) for b in self.clone_map.blocks]

        try:
            with open(self.output_image_path, "r+b") as dst_file:
                # ── FASE 1: PASSAGEM RÁPIDA COM PULO DE BAD SECTORS ──────────────────
                unread_ranges = self.clone_map.get_unread_ranges()

                for range_start, range_count in unread_ranges:
                    if self.cancel_requested:
                        break

                    cur_lba = range_start
                    range_end = range_start + range_count

                    while cur_lba < range_end and not self.cancel_requested:
                        count = min(self.block_sectors, range_end - cur_lba)
                        expected_bytes = count * self.sector_size

                        # 1. Leitura isolada da origem com classificação estrita de erro
                        read_succeeded = False
                        data = b""
                        try:
                            data = read_block(cur_lba, count)
                            if not isinstance(data, (bytes, bytearray)):
                                raise TypeError(
                                    f"O leitor de setores retornou tipo inválido ({type(data).__name__}) no LBA {cur_lba}."
                                )
                            if len(data) != expected_bytes:
                                raise LocalizedSectorReadError(
                                    f"Leitura parcial/incompleta no LBA {cur_lba} ({len(data)} != {expected_bytes} bytes)."
                                )
                            read_succeeded = True
                        except BaseException as raw_exc:
                            classified = _classify_source_read_error(raw_exc)
                            if isinstance(classified, LocalizedSectorReadError):
                                log.warning(
                                    "Erro localizado de leitura no LBA %d..%d: %s. Saltando para raspagem na Fase 2...",
                                    cur_lba,
                                    cur_lba + count,
                                    classified,
                                )
                                jump = min(self.skip_step_sectors, range_end - cur_lba)
                                self.clone_map.mark_range(cur_lba, jump, "*")
                                cur_lba += jump
                            else:
                                raise classified from raw_exc

                        # 2. Escrita isolada no destino (erros de destino nunca são confundidos com bad sector!)
                        if read_succeeded:
                            offset = cur_lba * self.sector_size
                            write_to_destination(dst_file, offset, bytes(data))
                            last_written_sample = (offset, bytes(data))
                            self.clone_map.mark_range(cur_lba, count, "+")
                            bytes_rescued_in_run += len(data)
                            cur_lba += count

                        iteration_count += 1
                        now = time.time()
                        if now - last_save_time >= 2.0:
                            sync_and_checkpoint(dst_file)
                            last_save_time = now

                        if progress_cb and (now - last_cb_time >= 0.1 or iteration_count % 3 == 0):
                            speed = (bytes_rescued_in_run / (1024 * 1024)) / max(0.1, now - t_start)
                            progress_cb(ResilientCloneStats(
                                current_sector=cur_lba,
                                total_sectors=self.total_sectors,
                                rescued_sectors=self.clone_map.rescued_sectors,
                                bad_sectors=self.clone_map.bad_sectors,
                                skipped_sectors=self.clone_map.skipped_sectors,
                                speed_mb_s=speed,
                                phase="Fase 1: Passagem Rápida"
                            ))
                            last_cb_time = now

                sync_and_checkpoint(dst_file)

                # ── FASE 2: RASPAGEM CIRÚRGICA DE TODOS OS SETORES PULADOS ───────────
                skipped_ranges = self.clone_map.get_skipped_ranges()
                if skipped_ranges and not self.cancel_requested:
                    log.info("Iniciando Fase 2 (Raspagem cirúrgica setor a setor)...")
                    for range_start, range_count in skipped_ranges:
                        if self.cancel_requested:
                            break

                        for single_lba in range(range_start, range_start + range_count):
                            if self.cancel_requested:
                                break
                            offset = single_lba * self.sector_size

                            # Tenta ler o setor individual até (1 + max_read_retries) vezes antes de declarar '-'
                            sector_data: Optional[bytes] = None
                            last_sector_error: Optional[LocalizedSectorReadError] = None
                            total_attempts = 1 + self.max_read_retries

                            for attempt in range(1, total_attempts + 1):
                                try:
                                    raw_sector = read_block(single_lba, 1)
                                    if not isinstance(raw_sector, (bytes, bytearray)):
                                        raise TypeError(
                                            f"O leitor de setores retornou tipo inválido ({type(raw_sector).__name__}) no LBA {single_lba}."
                                        )
                                    if len(raw_sector) != self.sector_size:
                                        raise LocalizedSectorReadError(
                                            f"Leitura incompleta de setor no LBA {single_lba} ({len(raw_sector)} != {self.sector_size} bytes)."
                                        )
                                    sector_data = bytes(raw_sector)
                                    break
                                except BaseException as raw_exc:
                                    classified = _classify_source_read_error(raw_exc)
                                    if isinstance(classified, LocalizedSectorReadError):
                                        last_sector_error = classified
                                        if attempt < total_attempts and self.retry_delay_s > 0:
                                            time.sleep(self.retry_delay_s)
                                        continue
                                    # Falha fatal de dispositivo, permissão, destino ou programação: aborta imediatamente!
                                    raise classified from raw_exc

                            if sector_data is not None:
                                write_to_destination(dst_file, offset, sector_data)
                                last_written_sample = (offset, sector_data)
                                self.clone_map.mark_range(single_lba, 1, "+")
                                bytes_rescued_in_run += self.sector_size
                            else:
                                # Setor confirmado como irrecuperável após todas as tentativas de leitura localizada:
                                # escreve zeros no offset exato, registra evidência e marca como bad '-'
                                zero_sec = b"\x00" * self.sector_size
                                write_to_destination(dst_file, offset, zero_sec)
                                last_written_sample = (offset, zero_sec)
                                self.clone_map.mark_range(single_lba, 1, "-")
                                self.bad_sector_evidence[single_lba] = (
                                    f"Falha localizada após {total_attempts} tentativa(s): {last_sector_error}"
                                )

                            iteration_count += 1
                            now = time.time()
                            if now - last_save_time >= 2.0:
                                sync_and_checkpoint(dst_file)
                                last_save_time = now

                            if progress_cb and (now - last_cb_time >= 0.1 or iteration_count % 5 == 0):
                                speed = (bytes_rescued_in_run / (1024 * 1024)) / max(0.1, now - t_start)
                                progress_cb(ResilientCloneStats(
                                    current_sector=single_lba,
                                    total_sectors=self.total_sectors,
                                    rescued_sectors=self.clone_map.rescued_sectors,
                                    bad_sectors=self.clone_map.bad_sectors,
                                    skipped_sectors=self.clone_map.skipped_sectors,
                                    speed_mb_s=speed,
                                    phase="Fase 2: Raspagem Cirúrgica"
                                ))
                                last_cb_time = now

                sync_and_checkpoint(dst_file)
        except BaseException:
            # Reverte o mapa em memória para o último estado efetivamente sincronizado (fsync) em disco
            self.clone_map.blocks = synced_blocks_snapshot
            self.clone_map.recalculate_counts()
            raise

        self.clone_map.recalculate_counts()

        success = not self.cancel_requested
        full_image_verified = False
        verified_sectors = 0

        if success and self.verify_full_image_on_finish:
            ok_verify, verified_sectors, verify_msg = self.verify_rescued_sectors(reader_fn=reader_fn)
            if not ok_verify:
                if self.cancel_requested:
                    success = False
                else:
                    raise DestinationPersistenceError(verify_msg)
            else:
                full_image_verified = True

        fully_recovered = (
            success
            and self.clone_map.bad_sectors == 0
            and self.clone_map.skipped_sectors == 0
            and self.clone_map.rescued_sectors == self.total_sectors
        )

        if not success:
            msg = "Clonagem interrompida pelo operador."
        elif fully_recovered:
            msg = f"Clonagem concluída com sucesso total: 100% dos {self.total_sectors:,} setores resgatados."
        else:
            msg = (
                f"Clonagem concluída com resgate parcial: {self.clone_map.rescued_sectors:,} setores resgatados, "
                f"{self.clone_map.bad_sectors:,} setores irrecuperáveis preenchidos com zeros."
            )

        return CloningResult(
            success=success,
            fully_recovered=fully_recovered,
            total_sectors=self.total_sectors,
            rescued_sectors=self.clone_map.rescued_sectors,
            bad_sectors=self.clone_map.bad_sectors,
            skipped_sectors=self.clone_map.skipped_sectors,
            map_path=self.map_path,
            message=msg,
            sector_size=self.sector_size,
            incremental_sample_verified=True,
            full_image_verified=full_image_verified,
            verified_sectors=verified_sectors,
            bad_sector_evidence=dict(self.bad_sector_evidence),
        )
