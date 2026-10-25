"""
core/hardware/write_blocker.py — Camada de abstração de I/O e Write-Blocker formal.
Garante que ferramentas de diagnóstico, varredura e triagem nunca gravem um único byte na mídia de origem.
"""
from __future__ import annotations

import io
import logging
import os
from pathlib import Path
from typing import BinaryIO, Union

log = logging.getLogger("diskrescue.write_blocker")


class WriteBlockViolationError(PermissionError):
    """Exceção levantada quando qualquer tentativa de escrita é detectada no dispositivo de origem."""
    pass


class StrictReadOnlyDeviceStream(io.RawIOBase):
    """
    Dispositivo simulado de leitura que atua como bloqueador físico estrito (Hardware Write-Blocker).
    Registra todas as leituras e intercepta/bloqueia sumariamente qualquer tentativa de escrita.
    """

    def __init__(self, target: Union[bytes, bytearray, io.RawIOBase, io.BufferedIOBase, BinaryIO]):
        super().__init__()
        if isinstance(target, (bytes, bytearray)):
            self._underlying = io.BytesIO(target)
        else:
            self._underlying = target

        self.read_count = 0
        self.bytes_read = 0
        self.write_attempts = 0

    def readable(self) -> bool:
        return True

    def writable(self) -> bool:
        return False

    def seekable(self) -> bool:
        return hasattr(self._underlying, "seekable") and self._underlying.seekable()

    def seek(self, offset: int, whence: int = os.SEEK_SET) -> int:
        return self._underlying.seek(offset, whence)

    def tell(self) -> int:
        return self._underlying.tell()

    def read(self, size: int = -1) -> bytes:
        data = self._underlying.read(size)
        self.read_count += 1
        self.bytes_read += len(data) if data else 0
        return data

    def readinto(self, b) -> int:
        if hasattr(self._underlying, "readinto"):
            n = self._underlying.readinto(b)
            self.read_count += 1
            self.bytes_read += n if n else 0
            return n or 0
        data = self.read(len(b))
        n = len(data)
        b[:n] = data
        return n

    def write(self, b) -> int:
        self.write_attempts += 1
        raise WriteBlockViolationError(
            "BLOQUEIO WRITE-BLOCKER: Tentativa de escrita interceptada na mídia de leitura!"
        )

    def writelines(self, lines) -> None:
        self.write_attempts += 1
        raise WriteBlockViolationError(
            "BLOQUEIO WRITE-BLOCKER: Tentativa de escrita (writelines) interceptada na mídia de leitura!"
        )

    def truncate(self, size: int | None = None) -> int:
        self.write_attempts += 1
        raise WriteBlockViolationError(
            "BLOQUEIO WRITE-BLOCKER: Tentativa de truncamento de dispositivo de leitura!"
        )

    def flush(self) -> None:
        # Flush de leitura é no-op seguro
        pass

    def close(self) -> None:
        if hasattr(self._underlying, "close"):
            self._underlying.close()
        super().close()


def open_device_strictly_read_only(device: Union[str, Path, BinaryIO]) -> BinaryIO:
    """
    Garante a abertura ou encapsulamento de um dispositivo exclusivamente para leitura.
    Rejeita modos de escrita ou abre com flags de leitura explícitas.
    """
    if isinstance(device, (str, Path)):
        # Sempre abre com modo "rb" binário de leitura
        f = open(str(device), "rb")
        return StrictReadOnlyDeviceStream(f)
    if isinstance(device, StrictReadOnlyDeviceStream):
        return device
    return StrictReadOnlyDeviceStream(device)
