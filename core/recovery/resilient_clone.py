"""
core/recovery/resilient_clone.py — Motor de clonagem resiliente em duas fases com mapa de resgate (estilo ddrescue).
Permite resgatar discos com bad sectors sem travar o sistema, pulando áreas danificadas e raspando dados cirurgicamente.
"""
from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Callable, Dict, List, Optional

log = logging.getLogger("diskrescue.clone")

SECTOR_SIZE = 512


@dataclass
class CloneBlock:
    start_lba: int
    sector_count: int
    status: str  # "?" = Não lido, "+" = Sucesso, "*" = Pulado, "-" = Bad Sector


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

    def save_map(self, map_path: Path):
        data = {
            "device_path": self.device_path,
            "output_image": self.output_image,
            "total_sectors": self.total_sectors,
            "sector_size": self.sector_size,
            "rescued_sectors": self.rescued_sectors,
            "bad_sectors": self.bad_sectors,
            "skipped_sectors": self.skipped_sectors,
            "blocks": [asdict(b) for b in self.blocks]
        }
        map_path.write_text(json.dumps(data, indent=2), encoding="utf-8")

    @classmethod
    def load_map(cls, map_path: Path) -> CloneMap | None:
        try:
            data = json.loads(map_path.read_text(encoding="utf-8"))
            blocks = [CloneBlock(**b) for b in data.get("blocks", [])]
            return cls(
                device_path=data["device_path"],
                output_image=data["output_image"],
                total_sectors=data["total_sectors"],
                sector_size=data.get("sector_size", SECTOR_SIZE),
                rescued_sectors=data.get("rescued_sectors", 0),
                bad_sectors=data.get("bad_sectors", 0),
                skipped_sectors=data.get("skipped_sectors", 0),
                blocks=blocks
            )
        except Exception as exc:
            log.debug("Falha ao carregar mapa de clonagem existente: %s", exc)
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
        self.device_path = device_path
        self.output_image_path = Path(output_image_path)
        self.total_sectors = total_sectors
        self.block_sectors = block_sectors
        self.skip_step_sectors = skip_step_sectors
        self.map_path = map_path or self.output_image_path.with_suffix(".map")
        self.cancel_requested = False

        # Tenta carregar mapa prévio para retomada (resume)
        loaded = CloneMap.load_map(self.map_path) if self.map_path.exists() else None
        if loaded and loaded.total_sectors == total_sectors:
            self.clone_map = loaded
            log.info("Retomando clonagem a partir do mapa existente: %s", self.map_path)
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
    ) -> bool:
        """Executa a clonagem em 2 fases: varredura rápida com saltos e raspagem pontual."""
        self.output_image_path.parent.mkdir(parents=True, exist_ok=True)
        t_start = time.time()
        last_cb_time = t_start
        bytes_rescued_in_run = 0

        # Cria ou pré-aloca o arquivo de saída
        with open(self.output_image_path, "a+b") as dst:
            dst.seek(0, os.SEEK_END)
            cur_size = dst.tell()
            target_size = self.total_sectors * SECTOR_SIZE
            if cur_size < target_size:
                dst.seek(target_size - 1)
                dst.write(b"\x00")
                dst.flush()

        # Leitor customizado ou leitura nativa direta do dispositivo
        def default_reader(lba: int, count: int) -> bytes:
            with open(self.device_path, "rb") as dev:
                dev.seek(lba * SECTOR_SIZE)
                return dev.read(count * SECTOR_SIZE)

        read_block = reader_fn or default_reader

        # ── FASE 1: PASSAGEM RÁPIDA COM PULO DE BAD SECTORS ──────────────────
        current_lba = 0
        skipped_regions: List[tuple[int, int]] = []

        while current_lba < self.total_sectors and not self.cancel_requested:
            count = min(self.block_sectors, self.total_sectors - current_lba)
            try:
                data = read_block(current_lba, count)
                if len(data) != count * SECTOR_SIZE:
                    raise OSError("Leitura incompleta do setor.")

                # Grava no destino no LBA exato
                with open(self.output_image_path, "r+b") as dst:
                    dst.seek(current_lba * SECTOR_SIZE)
                    dst.write(data)

                self.clone_map.rescued_sectors += count
                bytes_rescued_in_run += len(data)
                self.clone_map.blocks.append(CloneBlock(current_lba, count, "+"))
                current_lba += count

            except Exception as exc:
                log.warning("Erro de leitura no LBA %d..%d: %s. Pulando...", current_lba, current_lba + count, exc)
                # Registra o bloco com erro
                self.clone_map.bad_sectors += count
                self.clone_map.blocks.append(CloneBlock(current_lba, count, "*"))
                skipped_regions.append((current_lba, count))

                # Pula dinamicamente à frente para não travar a controladora
                jump = self.skip_step_sectors
                current_lba = min(self.total_sectors, current_lba + jump)
                self.clone_map.skipped_sectors += jump

            now = time.time()
            if progress_cb and (now - last_cb_time >= 0.2):
                speed = (bytes_rescued_in_run / (1024 * 1024)) / max(0.1, now - t_start)
                progress_cb(ResilientCloneStats(
                    current_sector=current_lba,
                    total_sectors=self.total_sectors,
                    rescued_sectors=self.clone_map.rescued_sectors,
                    bad_sectors=self.clone_map.bad_sectors,
                    skipped_sectors=self.clone_map.skipped_sectors,
                    speed_mb_s=speed,
                    phase="Fase 1: Passagem Rápida"
                ))
                last_cb_time = now

        # Salva mapa da fase 1
        self.clone_map.save_map(self.map_path)

        if self.cancel_requested:
            log.info("Clonagem interrompida na Fase 1.")
            return False

        # ── FASE 2: RASPAGEM CIRÚRGICA DE BLOCOS PULADOS ─────────────────────
        if skipped_regions and not self.cancel_requested:
            log.info("Iniciando Fase 2 (Raspagem cirúrgica setor a setor)...")
            for region_lba, region_count in skipped_regions:
                if self.cancel_requested:
                    break
                for s in range(region_count):
                    single_lba = region_lba + s
                    try:
                        data = read_block(single_lba, 1)
                        if len(data) == SECTOR_SIZE:
                            with open(self.output_image_path, "r+b") as dst:
                                dst.seek(single_lba * SECTOR_SIZE)
                                dst.write(data)
                            self.clone_map.rescued_sectors += 1
                            self.clone_map.bad_sectors = max(0, self.clone_map.bad_sectors - 1)
                            bytes_rescued_in_run += SECTOR_SIZE
                    except Exception:
                        # Setor realmente danificado / irrecuperável: grava zeros para manter offset
                        with open(self.output_image_path, "r+b") as dst:
                            dst.seek(single_lba * SECTOR_SIZE)
                            dst.write(b"\x00" * SECTOR_SIZE)

                    now = time.time()
                    if progress_cb and (now - last_cb_time >= 0.2):
                        speed = (bytes_rescued_in_run / (1024 * 1024)) / max(0.1, now - t_start)
                        progress_cb(ResilientCloneStats(
                            current_sector=single_lba,
                            total_sectors=self.total_sectors,
                            rescued_sectors=self.clone_map.rescued_sectors,
                            bad_sectors=self.clone_map.bad_sectors,
                            skipped_sectors=0,
                            speed_mb_s=speed,
                            phase="Fase 2: Raspagem Cirúrgica"
                        ))
                        last_cb_time = now

        self.clone_map.save_map(self.map_path)
        return not self.cancel_requested
