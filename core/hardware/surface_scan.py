"""
core/hardware/surface_scan.py — Varredura de superfície magnética com medição de tempo de resposta por bloco (estilo Victoria / MHDD).
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Callable, Dict, Optional

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
    fast_count: int = 0         # < 20ms
    good_count: int = 0         # 20ms .. 100ms
    slow_count: int = 0         # 100ms .. 250ms
    very_slow_count: int = 0    # 250ms .. 1000ms
    critical_delay_count: int = 0 # >= 1000ms (lido com atraso severo)
    bad_count: int = 0          # Erro irrecuperável de I/O ou truncamento (bad sector confirmado)


def scan_surface_latency(
    device_path: str,
    start_lba: int = 0,
    max_sectors: int = 100_000,
    block_sectors: int = 2048,  # 1MB
    progress_cb: Optional[Callable[[SurfaceBlockResult, SurfaceScanSummary], None]] = None,
    cancel_check: Optional[Callable[[], bool]] = None
) -> SurfaceScanSummary:
    """Varre a superfície medindo a latência precisa de resposta de cada bloco."""
    if start_lba < 0:
        raise ValueError("start_lba deve ser maior ou igual a zero.")
    if max_sectors <= 0:
        raise ValueError("max_sectors deve ser estritamente positivo.")
    if block_sectors <= 0:
        raise ValueError("block_sectors deve ser estritamente positivo.")

    summary = SurfaceScanSummary()
    current_lba = start_lba
    end_lba = start_lba + max_sectors
    block_bytes = block_sectors * SECTOR_SIZE

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
                    latency = (t1 - t0) * 1000.0 # ms

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

                res = SurfaceBlockResult(
                    lba=current_lba,
                    count_sectors=to_read_sectors,
                    latency_ms=latency,
                    status=status
                )

                if progress_cb:
                    progress_cb(res, summary)

                current_lba += to_read_sectors

    except Exception as exc:
        log.error("Erro durante Surface Scan: %s", exc)

    return summary
