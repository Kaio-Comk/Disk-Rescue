"""
core/hardware/smart.py — Decodificação e interpretação preditiva de telemetria SMART em HDDs e SSDs.
"""
from __future__ import annotations

import logging
import os
import subprocess
from dataclasses import dataclass, field
from enum import Enum
from typing import Dict, List, Optional

log = logging.getLogger("diskrescue.smart")


class SmartRiskLevel(Enum):
    GREEN = "Verde (Saudável)"
    YELLOW = "Amarelo (Atenção / Risco Moderado)"
    RED = "Vermelho (Crítico / Falha Iminente)"


@dataclass
class SmartAttribute:
    id: int
    name: str
    current: int
    worst: int
    threshold: int
    raw_value: int
    is_failing: bool = False
    description: str = ""


@dataclass
class SmartReport:
    device_id: int
    device_path: str
    model: str
    serial: str
    risk_level: SmartRiskLevel
    temperature_c: Optional[int]
    reallocated_sectors: int = 0
    pending_sectors: int = 0
    uncorrectable_sectors: int = 0
    crc_errors: int = 0
    spin_retries: int = 0
    power_on_hours: Optional[int] = None
    attributes: Dict[int, SmartAttribute] = field(default_factory=dict)
    summary_messages: List[str] = field(default_factory=list)
    action_recommendation: str = ""


SMART_DEFINITIONS = {
    1: ("Raw Read Error Rate", "Taxa de erros brutos de leitura na cabeça"),
    5: ("Reallocated Sectors Count", "Setores defeituosos aposentados e remapeados para reserva"),
    9: ("Power-On Hours", "Horas totais de funcionamento contínuo"),
    10: ("Spin Retry Count", "Tentativas repetidas de alcançar rotação do motor"),
    184: ("End-to-End Error", "Erros de paridade entre buffer de memória e controladora"),
    187: ("Reported Uncorrectable", "Erros não corrigidos por código ECC"),
    196: ("Reallocation Event Count", "Eventos em que o firmware realizou remapeamento"),
    197: ("Current Pending Sector", "Bad sectors instáveis aguardando reescrita para remapeamento"),
    198: ("Offline Uncorrectable", "Setores irrecuperáveis detectados em varredura offline"),
    199: ("UltraDMA CRC Error Count", "Erros de transmissão elétrica entre disco e placa-mãe (Cabo SATA / Conector)"),
}


def analyze_smart_data(
    device_id: int,
    device_path: str,
    model: str,
    serial: str,
    raw_attributes: Dict[int, int],
    temperature_c: Optional[int] = None
) -> SmartReport:
    """Interpreta valores brutos SMART e gera diagnóstico técnico com regras forenses."""
    reallocated = raw_attributes.get(5, 0)
    pending = raw_attributes.get(197, 0)
    uncorrectable = raw_attributes.get(198, 0)
    crc_err = raw_attributes.get(199, 0)
    spin_retry = raw_attributes.get(10, 0)
    power_hours = raw_attributes.get(9, None)

    attributes: Dict[int, SmartAttribute] = {}
    for aid, raw_val in raw_attributes.items():
        name, desc = SMART_DEFINITIONS.get(aid, (f"Atributo {aid}", "Atributo de telemetria do fabricante"))
        attributes[aid] = SmartAttribute(
            id=aid,
            name=name,
            current=100,
            worst=100,
            threshold=10,
            raw_value=raw_val,
            description=desc
        )

    messages = []
    risk = SmartRiskLevel.GREEN
    recommendation = "O disco apresenta parâmetros saudáveis. Operações de reparo de partição são seguras."

    # Regra 1: Mau contato de Cabo SATA / USB
    if crc_err > 0 and reallocated == 0 and pending == 0 and uncorrectable == 0:
        risk = SmartRiskLevel.YELLOW
        messages.append(
            f"🔍 INDÍCIO DE PROBLEMA DE CABO/INTERFACE: Detectados {crc_err} erros de CRC UltraDMA (ID 199/C7). "
            "Não foram observados setores pendentes ou realocados até o momento. Alta probabilidade de falha "
            "elétrica no cabo de dados SATA ou mau contato na porta. Recomenda-se substituir o cabo SATA e monitorar a integridade."
        )
        recommendation = "Substitua o cabo SATA ou conecte em outra porta da placa-mãe. Não condene o disco sem antes testar a superfície."

    # Regra 2: Degradação de Superfície / Bad Sectors
    if pending > 0 or reallocated > 0 or uncorrectable > 0:
        if pending > 20 or reallocated > 100 or uncorrectable > 0:
            risk = SmartRiskLevel.RED
            messages.append(
                f"🚨 FALHA CRÍTICA DE MÍDIA: {pending} setores pendentes (C5), {reallocated} setores remapeados (05) "
                f"e {uncorrectable} irrecuperáveis (C6). A superfície magnética está degradando rapidamente."
            )
            recommendation = "RISCO IMINENTE: Crie imediatamente uma Imagem Forense (.img). NÃO execute chkdsk ou formatação."
        else:
            risk = SmartRiskLevel.YELLOW
            messages.append(
                f"⚠️ ATENÇÃO: Detectados {pending} setores pendentes e {reallocated} setores remapeados. "
                "Existem bad sectors sendo monitorados pelo firmware."
            )
            recommendation = "Faça backup dos arquivos prioritários ou gere uma imagem clone antes de reparos."

    # Regra 3: Fadiga Mecânica no Motor
    if spin_retry > 0:
        risk = SmartRiskLevel.RED
        messages.append(
            f"⚙️ FADIGA MECÂNICA: {spin_retry} falhas de inicialização do motor (ID 10/0A). "
            "O motor de rotação ou o rolamento está tendo dificuldade para atingir a rotação nominal."
        )
        recommendation = "Evite desligar e ligar o disco frequentemente. Faça cópia de emergência ininterrupta."

    if not messages:
        messages.append("Nenhum erro de leitura, remapeamento ou comunicação física detectado.")

    return SmartReport(
        device_id=device_id,
        device_path=device_path,
        model=model,
        serial=serial,
        risk_level=risk,
        temperature_c=temperature_c,
        reallocated_sectors=reallocated,
        pending_sectors=pending,
        uncorrectable_sectors=uncorrectable,
        crc_errors=crc_err,
        spin_retries=spin_retry,
        power_on_hours=power_hours,
        attributes=attributes,
        summary_messages=messages,
        action_recommendation=recommendation
    )


def query_smart_report_windows(device_number: int, device_path: str, model: str, serial: str) -> SmartReport:
    """Obtém os contadores de confiabilidade e atributos SMART no Windows."""
    script = f"""
    [Console]::OutputEncoding = [System.Text.Encoding]::UTF8
    $phys = Get-PhysicalDisk | Where-Object {{ $_.DeviceId -eq '{device_number}' }} | Select-Object -First 1
    $rel = if ($phys) {{ $phys | Get-StorageReliabilityCounter -ErrorAction SilentlyContinue }} else {{ $null }}

    [PSCustomObject]@{{
        Temperature = if ($null -ne $rel) {{ $rel.Temperature }} else {{ $null }}
        ReadErrorsTotal = if ($null -ne $rel) {{ $rel.ReadErrorsTotal }} else {{ 0 }}
        WriteErrorsTotal = if ($null -ne $rel) {{ $rel.WriteErrorsTotal }} else {{ 0 }}
        Wear = if ($null -ne $rel) {{ $rel.Wear }} else {{ 0 }}
    }} | ConvertTo-Json
    """
    cmd = ["powershell", "-NoProfile", "-Command", script]
    creationflags = 0x08000000 if os.name == "nt" else 0

    temperature = None
    raw_attrs = {}

    try:
        r = subprocess.run(cmd, capture_output=True, timeout=10, creationflags=creationflags)
        import json
        stdout = r.stdout.decode("utf-8", errors="replace").strip()
        if stdout:
            data = json.loads(stdout)
            temperature = data.get("Temperature")
            read_err = data.get("ReadErrorsTotal") or 0
            if read_err > 0:
                raw_attrs[1] = read_err
                raw_attrs[197] = read_err
    except Exception as exc:
        log.debug("Falha na consulta de StorageReliabilityCounter: %s", exc)

    return analyze_smart_data(
        device_id=device_number,
        device_path=device_path,
        model=model,
        serial=serial,
        raw_attributes=raw_attrs,
        temperature_c=temperature
    )
