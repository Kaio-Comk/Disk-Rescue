"""
core/hardware/smart.py — Decodificação e interpretação preditiva de telemetria SMART em HDDs e SSDs.
Separa evidências observadas, hipóteses prováveis, limitações de telemetria e ações sugeridas com rigor forense.
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
    UNKNOWN = "Cinza (Telemetria Não Disponível / Insuficiente)"


@dataclass
class SmartAttribute:
    id: int
    name: str
    current: Optional[int] = None
    worst: Optional[int] = None
    threshold: Optional[int] = None
    raw_value: int = 0
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
    # Seções estruturadas para relatório técnico rigoroso
    evidencia_observada: List[str] = field(default_factory=list)
    hipotese_provavel: List[str] = field(default_factory=list)
    limitacoes: List[str] = field(default_factory=list)
    acao_sugerida: str = ""


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
    temperature_c: Optional[int] = None,
    normalized_attributes: Optional[Dict[int, tuple[int, int, int]]] = None
) -> SmartReport:
    """
    Interpreta valores brutos SMART e gera diagnóstico técnico com regras forenses.
    Não preenche valores normalizados fictícios (current/worst/threshold) a menos que
    fornecidos explicitamente pelo hardware.
    """
    # Se não houver atributos SMART nem contadores de telemetria disponíveis, não declarar verde falsamente
    if not raw_attributes:
        return SmartReport(
            device_id=device_id,
            device_path=device_path,
            model=model,
            serial=serial,
            risk_level=SmartRiskLevel.UNKNOWN,
            temperature_c=temperature_c,
            reallocated_sectors=0,
            pending_sectors=0,
            uncorrectable_sectors=0,
            crc_errors=0,
            spin_retries=0,
            power_on_hours=None,
            attributes={},
            summary_messages=["Telemetria S.M.A.R.T. não disponível ou insuficiente para este dispositivo."],
            action_recommendation=(
                "O hardware ou driver não retornou contadores S.M.A.R.T. válidos. "
                "Em caso de suspeita de instabilidade física, realize clonagem preventiva setor a setor."
            ),
            evidencia_observada=["Nenhum atributo S.M.A.R.T. ou contador de confiabilidade retornado pelo firmware."],
            hipotese_provavel=["Dispositivo USB sem ATA pass-through, unidade virtual/simulada ou telemetria inacessível."],
            limitacoes=["Sem acesso aos registradores de hardware, o status de confiabilidade não pode ser afirmado como saudável nem crítico."],
            acao_sugerida="Proceder com clonagem resiliente para resguardar os dados caso haja lentidão ou sintomas de falha."
        )

    reallocated = raw_attributes.get(5, 0)
    pending = raw_attributes.get(197, 0)
    uncorrectable = raw_attributes.get(198, 0)
    crc_err = raw_attributes.get(199, 0)
    spin_retry = raw_attributes.get(10, 0)
    power_hours = raw_attributes.get(9, None)

    attributes: Dict[int, SmartAttribute] = {}
    for aid, raw_val in raw_attributes.items():
        name, desc = SMART_DEFINITIONS.get(aid, (f"Atributo {aid}", "Atributo de telemetria do fabricante"))
        cur, worst, thresh = (None, None, None)
        if normalized_attributes and aid in normalized_attributes:
            cur, worst, thresh = normalized_attributes[aid]

        attributes[aid] = SmartAttribute(
            id=aid,
            name=name,
            current=cur,
            worst=worst,
            threshold=thresh,
            raw_value=raw_val,
            description=desc
        )

    evidencias: List[str] = []
    hipoteses: List[str] = []
    limitacoes: List[str] = [
        "A telemetria S.M.A.R.T. reflete apenas os contadores relatados pelo firmware do fabricante.",
        "A ausência de erros registrados não comprova que a mídia magnética ou células flash estejam 100% íntegras."
    ]

    messages = []
    risk = SmartRiskLevel.GREEN
    recommendation = (
        "O disco apresenta parâmetros monitorados saudáveis e estáveis. "
        "Recomenda-se realizar snapshot de segurança antes de qualquer intervenção estrutural ou particionamento."
    )

    # Regra 1: Mau contato de Cabo SATA / USB
    if crc_err > 0 and reallocated == 0 and pending == 0 and uncorrectable == 0:
        risk = SmartRiskLevel.YELLOW
        evidencias.append(f"Contador UltraDMA CRC (ID 199/0xC7): {crc_err} erros registrados na interface.")
        evidencias.append("Zero setores pendentes (C5), realocados (05) ou offline uncorrectable (C6) observados.")
        hipoteses.append(
            "Indício de degradação na comunicação física (cabo de dados SATA, conector oxidado ou porta na placa-mãe). "
            "A mídia física não apresenta setores defeituosos registrados até o momento."
        )
        limitacoes.append(
            "Erros de CRC são um indicativo de problema de transmissão, mas não excluem a possibilidade de "
            "danos não detectados na controladora do próprio disco."
        )
        msg_cabo = (
            f"🔍 INDÍCIO DE PROBLEMA DE CABO/INTERFACE: Detectados {crc_err} erros de CRC UltraDMA (ID 199/C7). "
            "Não foram observados setores pendentes ou realocados até o momento. Alta probabilidade de falha "
            "elétrica no cabo de dados SATA ou mau contato na porta. Recomenda-se substituir o cabo SATA e monitorar a integridade."
        )
        messages.append(msg_cabo)
        recommendation = (
            "Substitua o cabo SATA ou conecte em outra porta da placa-mãe. "
            "Gere uma imagem forense antes de submeter o disco a esforços intensos e execute teste de superfície."
        )

    # Regra 2: Degradação de Superfície / Bad Sectors
    if pending > 0 or reallocated > 0 or uncorrectable > 0:
        evidencias.append(
            f"Setores pendentes (197): {pending} | Setores remapeados (5): {reallocated} | "
            f"Irrecuperáveis (198): {uncorrectable}"
        )
        if pending > 20 or reallocated > 100 or uncorrectable > 0:
            risk = SmartRiskLevel.RED
            hipoteses.append("Degradação física severa da superfície magnética ou esgotamento de blocos de reserva.")
            messages.append(
                f"🚨 FALHA CRÍTICA DE MÍDIA: {pending} setores pendentes (C5), {reallocated} setores remapeados (05) "
                f"e {uncorrectable} irrecuperáveis (C6). A superfície magnética está degradando rapidamente."
            )
            recommendation = (
                "RISCO IMINENTE: Crie imediatamente uma Imagem Forense (.img) via Clonagem Resiliente. "
                "NÃO execute chkdsk, desfragmentação ou formatação."
            )
        else:
            risk = SmartRiskLevel.YELLOW
            hipoteses.append("Presença de setores instáveis sendo monitorados ou remapeados pelo firmware.")
            messages.append(
                f"⚠️ ATENÇÃO: Detectados {pending} setores pendentes e {reallocated} setores remapeados. "
                "Existem bad sectors sendo monitorados pelo firmware."
            )
            recommendation = "Faça backup dos arquivos prioritários ou gere uma imagem clone antes de reparos."

    # Regra 3: Fadiga Mecânica no Motor
    if spin_retry > 0:
        risk = SmartRiskLevel.RED
        evidencias.append(f"Tentativas de rotação do motor (ID 10): {spin_retry} eventos registrados.")
        hipoteses.append("Fadiga mecânica no rolamento do motor de rotação ou fornecimento elétrico instável.")
        messages.append(
            f"⚙️ FADIGA MECÂNICA: {spin_retry} falhas de inicialização do motor (ID 10/0A). "
            "O motor de rotação ou o rolamento está tendo dificuldade para atingir a rotação nominal."
        )
        recommendation = "Evite desligar e ligar o disco frequentemente. Faça cópia de emergência ininterrupta."

    if not messages:
        evidencias.append("Nenhum erro de leitura bruta, remapeamento ou CRC registrado nos atributos consultados.")
        hipoteses.append("Mídia operando dentro dos parâmetros nominais para os atributos monitorados.")
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
        action_recommendation=recommendation,
        evidencia_observada=evidencias,
        hipotese_provavel=hipoteses,
        limitacoes=limitacoes,
        acao_sugerida=recommendation
    )


def query_smart_report_windows(device_number: int, device_path: str, model: str, serial: str) -> SmartReport:
    """Obtém os contadores de confiabilidade e atributos SMART no Windows sem confundir métricas de SO com bad sectors."""
    script = f"""
    [Console]::OutputEncoding = [System.Text.Encoding]::UTF8
    $phys = Get-PhysicalDisk | Where-Object {{ $_.DeviceId -eq '{device_number}' }} | Select-Object -First 1
    $rel = if ($phys) {{ $phys | Get-StorageReliabilityCounter -ErrorAction SilentlyContinue }} else {{ $null }}

    if ($null -eq $rel) {{
        ""
    }} else {{
        [PSCustomObject]@{{
            Temperature = $rel.Temperature
            ReadErrorsTotal = $rel.ReadErrorsTotal
            WriteErrorsTotal = $rel.WriteErrorsTotal
            Wear = $rel.Wear
        }} | ConvertTo-Json
    }}
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
            read_err = data.get("ReadErrorsTotal")
            wear = data.get("Wear")
            # ReadErrorsTotal do Windows StorageReliabilityCounter reflete contadores de leitura do SO,
            # NÃO deve ser atribuído ao SMART ID 197 (Current Pending Sector).
            # Mapeamos para SMART ID 1 (Raw Read Error Rate) se presente.
            if read_err is not None:
                raw_attrs[1] = int(read_err)
            if wear is not None and wear > 0:
                raw_attrs[233] = int(wear)
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
