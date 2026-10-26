"""
core/hardware/surface_scan.py — Varredura de superfície magnética com medição de tempo de resposta por bloco (estilo Victoria / MHDD).
Suporta varredura sequencial 100% setor a setor e varredura distribuída multi-zona (0% a 100% da capacidade física do disco),
com isolamento automático de LBAs defeituosos (bad blocks) e telemetria de velocidade (MB/s).
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Callable, List, Optional

log = logging.getLogger("diskrescue.surface")

SECTOR_SIZE = 512


@dataclass
class SurfaceBlockResult:
    lba: int
    count_sectors: int
    latency_ms: float
    status: str  # "fast", "good", "slow", "very_slow", "critical_delay", "bad"


@dataclass
class SurfaceScanSummary:
    total_tested_blocks: int = 0
    fast_count: int = 0           # < 20ms
    good_count: int = 0           # 20ms .. 100ms
    slow_count: int = 0           # 100ms .. 250ms
    very_slow_count: int = 0      # 250ms .. 1000ms
    critical_delay_count: int = 0 # >= 1000ms (lido com atraso severo)
    bad_count: int = 0            # Erro irrecuperável de I/O ou truncamento (bad sector confirmado)
    bytes_read: int = 0
    elapsed_seconds: float = 0.0
    speed_mb_s: float = 0.0
    avg_latency_ms: float = 0.0
    max_latency_ms: float = 0.0
    scanned_span_sectors: int = 0
    current_span_sector: int = 0
    bad_lbas: List[int] = field(default_factory=list)


def scan_surface_latency(
    device_path: str,
    start_lba: int = 0,
    max_sectors: int = 100_000,
    block_sectors: int = 2048,  # 1MB padrão
    progress_cb: Optional[Callable[[SurfaceBlockResult, SurfaceScanSummary], None]] = None,
    cancel_check: Optional[Callable[[], bool]] = None,
    stride_sectors: Optional[int] = None,
) -> SurfaceScanSummary:
    """
    Varre a superfície medindo a latência precisa de resposta de cada bloco.
    - Se stride_sectors for None, realiza leitura sequencial contínua de start_lba até start_lba + max_sectors.
    - Se stride_sectors > block_sectors, realiza varredura distribuída por toda a extensão (start_lba .. start_lba + max_sectors),
      testando trilhas externas, médias e internas do disco (0% a 100%).
    """
    if start_lba < 0:
        raise ValueError("start_lba deve ser maior ou igual a zero.")
    if max_sectors <= 0:
        raise ValueError("max_sectors deve ser estritamente positivo.")
    if block_sectors <= 0:
        raise ValueError("block_sectors deve ser estritamente positivo.")

    summary = SurfaceScanSummary(scanned_span_sectors=max_sectors)
    current_lba = start_lba
    end_lba = start_lba + max_sectors
    total_latency_ms = 0.0

    try:
        with open(device_path, "rb") as f:
            while current_lba < end_lba:
                if cancel_check and cancel_check():
                    break

                to_read_sectors = min(block_sectors, end_lba - current_lba)
                to_read_bytes = to_read_sectors * SECTOR_SIZE

                f.seek(current_lba * SECTOR_SIZE)
                t0 = time.perf_counter()
                status = "fast"
                latency = 0.0

                try:
                    data = f.read(to_read_bytes)
                    t1 = time.perf_counter()
                    latency = (t1 - t0) * 1000.0  # ms

                    if len(data) != to_read_bytes:
                        status = "bad"
                    elif latency < 20.0:
                        status = "fast"
                    elif latency < 100.0:
                        status = "good"
                    elif latency < 250.0:
                        status = "slow"
                    elif latency < 1000.0:
                        status = "very_slow"
                    else:
                        status = "critical_delay"
                except Exception:
                    latency = 3000.0
                    status = "bad"

                summary.total_tested_blocks += 1
                if status != "bad":
                    summary.bytes_read += to_read_bytes
                else:
                    if len(summary.bad_lbas) < 100:
                        summary.bad_lbas.append(current_lba)

                total_latency_ms += max(0.0, latency)
                summary.avg_latency_ms = total_latency_ms / summary.total_tested_blocks
                if latency > summary.max_latency_ms:
                    summary.max_latency_ms = latency

                elapsed = max(0.001, total_latency_ms / 1000.0)
                summary.elapsed_seconds = elapsed
                summary.speed_mb_s = (summary.bytes_read / (1024.0 * 1024.0)) / elapsed

                if status == "fast":
                    summary.fast_count += 1
                elif status == "good":
                    summary.good_count += 1
                elif status == "slow":
                    summary.slow_count += 1
                elif status == "very_slow":
                    summary.very_slow_count += 1
                elif status == "critical_delay":
                    summary.critical_delay_count += 1
                elif status == "bad":
                    summary.bad_count += 1

                if stride_sectors and stride_sectors > to_read_sectors:
                    next_lba = current_lba + stride_sectors
                else:
                    next_lba = current_lba + to_read_sectors

                summary.current_span_sector = min(max_sectors, next_lba - start_lba)

                res = SurfaceBlockResult(
                    lba=current_lba,
                    count_sectors=to_read_sectors,
                    latency_ms=latency,
                    status=status,
                )

                if progress_cb:
                    progress_cb(res, summary)

                current_lba = next_lba

    except Exception as exc:
        log.error("Erro durante Surface Scan: %s", exc)

    return summary
