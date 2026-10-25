"""
core/recovery/resilient_clone.py — Motor de clonagem resiliente em duas fases com mapa de resgate (estilo ddrescue).
Permite resgatar discos com bad sectors sem travar o sistema, pulando áreas danificadas e raspando dados cirurgicamente.
Garante contabilização estrita de 100% dos LBAs sem lacunas silenciosas, persistência atômica,
durabilidade com fsync e validação rigorosa de integridade e parâmetros na retomada (resume).
"""
from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Callable, List, Optional, Tuple

from core.hardware.device_resolver import assert_safe_source_and_destination

log = logging.getLogger("diskrescue.clone")

SECTOR_SIZE = 512


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

    def __post_init__(self):
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

    def save_map(self, map_path: Path):
        """
        Persiste o mapa atomicamente (gravação temporária + fsync + os.replace)
        garantindo durabilidade física contra quedas de energia.
        """
        self.recalculate_counts()
        data = {
            "version": "2.0",
            "device_path": self.device_path,
            "output_image": self.output_image,
            "total_sectors": self.total_sectors,
            "sector_size": self.sector_size,
            "rescued_sectors": self.rescued_sectors,
            "bad_sectors": self.bad_sectors,
            "skipped_sectors": self.skipped_sectors,
            "updated_at": time.time(),
            "blocks": [asdict(b) for b in self.blocks]
        }
        map_path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = map_path.with_suffix(".map.tmp")
        with open(tmp_path, "w", encoding="utf-8") as f:
            f.write(json.dumps(data, indent=2))
            f.flush()
            try:
                os.fsync(f.fileno())
            except OSError:
                pass
        os.replace(tmp_path, map_path)

    @classmethod
    def load_map(
        cls,
        map_path: Path,
        expected_device: str,
        expected_output: Path,
        expected_sectors: int
    ) -> CloneMap | None:
        """
        Carrega e valida rigorosamente se o mapa pertence à mesma origem e destino,
        rejeitando mapas com metadados ausentes, blocos malformados ou incompatibilidade
        com a imagem física em disco.
        """
        try:
            if not map_path.exists():
                return None
            data = json.loads(map_path.read_text(encoding="utf-8"))

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
            clean_map_out = str(map_out).replace("\\", "/").lower()
            if clean_exp_out != clean_map_out:
                log.warning("Mapa rejeitado: arquivo de destino diferente (%s != %s)", clean_map_out, clean_exp_out)
                return None

            blocks_raw = data.get("blocks", [])
            if not blocks_raw:
                log.warning("Mapa rejeitado: lista de blocos vazia.")
                return None

            # Validação rigorosa dos blocos (cobertura total contígua, sem lacunas, contagens > 0)
            blocks = []
            expected_start = 0
            for b in blocks_raw:
                start = b.get("start_lba", -1)
                count = b.get("sector_count", 0)
                status = b.get("status", "")
                if count <= 0 or start != expected_start or status not in {"?", "+", "*", "-"}:
                    log.warning("Mapa rejeitado: bloco inconsistente ou com lacuna (%s)", b)
                    return None
                blocks.append(CloneBlock(start, count, status))
                expected_start += count

            if expected_start != expected_sectors:
                log.warning("Mapa rejeitado: cobertura total de setores (%d) difere de total_sectors (%d)", expected_start, expected_sectors)
                return None

            # Verificação de coerência com o arquivo físico de imagem
            rescued_in_map = sum(b.sector_count for b in blocks if b.status == "+")
            if rescued_in_map > 0:
                if not expected_output.exists() or expected_output.stat().st_size == 0:
                    log.warning(
                        "Mapa rejeitado: o mapa indica %d setores recuperados, mas o arquivo de imagem '%s' não existe ou está vazio.",
                        rescued_in_map, expected_output
                    )
                    return None

            cmap = cls(
                device_path=str(map_dev),
                output_image=str(expected_output),
                total_sectors=expected_sectors,
                sector_size=data.get("sector_size", SECTOR_SIZE),
                blocks=blocks
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
    """Resultado detalhado da clonagem distinguindo conclusão operacional de integridade 100%."""
    success: bool
    fully_recovered: bool
    total_sectors: int
    rescued_sectors: int
    bad_sectors: int
    skipped_sectors: int
    map_path: Path
    message: str = ""

    def __bool__(self) -> bool:
        return self.success


class ResilientCloner:
    def __init__(
        self,
        device_path: str,
        output_image_path: Path,
        total_sectors: int,
        block_sectors: int = 128,  # 64 KB por leitura na fase rápida
        skip_step_sectors: int = 2048,  # 1 MB de salto após erro
        map_path: Path | None = None
    ):
        # Validação estrita de parâmetros de execução (impedir loops infinitos ou parâmetros não positivos)
        if total_sectors <= 0 or block_sectors <= 0 or skip_step_sectors <= 0:
            raise ValueError("Os parâmetros total_sectors, block_sectors e skip_step_sectors devem ser estritamente positivos (> 0).")

        self.device_path = device_path
        self.output_image_path = Path(output_image_path)
        self.total_sectors = total_sectors
        self.block_sectors = block_sectors
        self.skip_step_sectors = skip_step_sectors
        self.map_path = map_path or self.output_image_path.with_suffix(".map")
        self.cancel_requested = False

        # Validação de segurança P0 Fail-Closed: Impedir gravação no mesmo disco físico
        assert_safe_source_and_destination(self.device_path, self.output_image_path)

        # Verifica espaço em disco disponível no destino
        try:
            import shutil
            target_parent = self.output_image_path.parent
            target_parent.mkdir(parents=True, exist_ok=True)
            free_bytes = shutil.disk_usage(target_parent).free
            needed_bytes = total_sectors * SECTOR_SIZE
            if free_bytes < needed_bytes:
                log.warning(
                    "Espaço livre no destino (%s MB) é menor que o tamanho estimado da imagem (%s MB).",
                    free_bytes // (1024 * 1024),
                    needed_bytes // (1024 * 1024)
                )
        except Exception as exc:
            log.debug("Não foi possível verificar espaço livre no destino: %s", exc)

        # Tenta carregar mapa prévio para retomada verídica (resume)
        loaded = CloneMap.load_map(
            self.map_path,
            expected_device=self.device_path,
            expected_output=self.output_image_path,
            expected_sectors=self.total_sectors
        )
        if loaded:
            self.clone_map = loaded
            log.info("Retomando clonagem a partir do mapa verificado: %s", self.map_path)
        else:
            self.clone_map = CloneMap(
                device_path=device_path,
                output_image=str(self.output_image_path),
                total_sectors=total_sectors,
                blocks=[]
            )

    def cancel(self):
        self.cancel_requested = True

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

        # Cria ou pré-aloca o arquivo de saída
        with open(self.output_image_path, "a+b") as dst:
            dst.seek(0, os.SEEK_END)
            cur_size = dst.tell()
            target_size = self.total_sectors * SECTOR_SIZE
            if cur_size < target_size:
                dst.seek(target_size - 1)
                dst.write(b"\x00")
                dst.flush()
                try:
                    os.fsync(dst.fileno())
                except OSError:
                    pass

        # Leitor customizado ou leitura nativa direta do dispositivo (abertura estrita somente leitura)
        def default_reader(lba: int, count: int) -> bytes:
            with open(self.device_path, "rb") as dev:
                dev.seek(lba * SECTOR_SIZE)
                return dev.read(count * SECTOR_SIZE)

        read_block = reader_fn or default_reader

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
                    try:
                        data = read_block(cur_lba, count)
                        if len(data) != count * SECTOR_SIZE:
                            raise OSError("Leitura incompleta do setor.")

                        # Grava no destino no LBA exato
                        dst_file.seek(cur_lba * SECTOR_SIZE)
                        dst_file.write(data)

                        self.clone_map.mark_range(cur_lba, count, "+")
                        bytes_rescued_in_run += len(data)
                        cur_lba += count

                    except Exception as exc:
                        log.warning("Erro de leitura no LBA %d..%d: %s. Pulando...", cur_lba, cur_lba + count, exc)
                        # Salta para afastar cabeças magnéticas da área degradada
                        jump = min(self.skip_step_sectors, range_end - cur_lba)
                        # REGRA CRÍTICA P0: Todos os setores saltados são marcados como '*' (pulados)
                        # para serem raspados na Fase 2, garantindo zero omissão de dados.
                        self.clone_map.mark_range(cur_lba, jump, "*")
                        cur_lba += jump

                    iteration_count += 1
                    now = time.time()
                    if now - last_save_time >= 2.0:
                        dst_file.flush()
                        try:
                            os.fsync(dst_file.fileno())
                        except OSError:
                            pass
                        self.clone_map.save_map(self.map_path)
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

            dst_file.flush()
            try:
                os.fsync(dst_file.fileno())
            except OSError:
                pass
            self.clone_map.save_map(self.map_path)

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
                        try:
                            data = read_block(single_lba, 1)
                            if len(data) != SECTOR_SIZE:
                                raise OSError("Leitura incompleta de setor.")

                            dst_file.seek(single_lba * SECTOR_SIZE)
                            dst_file.write(data)
                            self.clone_map.mark_range(single_lba, 1, "+")
                            bytes_rescued_in_run += SECTOR_SIZE
                        except Exception:
                            # Setor irrecuperável: escreve zeros no offset exato e registra como bad '-'
                            dst_file.seek(single_lba * SECTOR_SIZE)
                            dst_file.write(b"\x00" * SECTOR_SIZE)
                            self.clone_map.mark_range(single_lba, 1, "-")

                        iteration_count += 1
                        now = time.time()
                        if now - last_save_time >= 2.0:
                            dst_file.flush()
                            try:
                                os.fsync(dst_file.fileno())
                            except OSError:
                                pass
                            self.clone_map.save_map(self.map_path)
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

            dst_file.flush()
            try:
                os.fsync(dst_file.fileno())
            except OSError:
                pass

        self.clone_map.save_map(self.map_path)
        self.clone_map.recalculate_counts()

        success = not self.cancel_requested
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
            message=msg
        )
