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
    reallocated_sectors: Optional[int] = None
    pending_sectors: Optional[int] = None
    uncorrectable_sectors: Optional[int] = None
    crc_errors: Optional[int] = None
    spin_retries: Optional[int] = None
    power_on_hours: Optional[int] = None
    attributes: Dict[int, SmartAttribute] = field(default_factory=dict)
    windows_reliability: Optional[Dict[str, int]] = None
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
    normalized_attributes: Optional[Dict[int, tuple[int, int, int]]] = None,
    windows_reliability: Optional[Dict[str, int]] = None,
    is_usb: bool = False
) -> SmartReport:
    """
    Interpreta valores brutos SMART e gera diagnóstico técnico com regras forenses.
    Não preenche valores normalizados fictícios (current/worst/threshold) a menos que
    fornecidos explicitamente pelo hardware.
    Nunca declara estado Verde sem monitoramento de setores remapeados (05) e pendentes (C5).
    """
    # Se não houver atributos SMART de hardware disponíveis, não declarar verde falsamente
    if not raw_attributes:
        evidencias = ["Nenhum atributo S.M.A.R.T. de hardware do fabricante retornado."]
        if windows_reliability:
            evidencias.append(
                f"Métricas do Subsistema Windows (StorageReliabilityCounter): "
                f"ReadErrors={windows_reliability.get('ReadErrorsTotal', 0)}, "
                f"WriteErrors={windows_reliability.get('WriteErrorsTotal', 0)}, "
                f"Wear={windows_reliability.get('Wear', 0)}"
            )

        if is_usb:
            limitacoes = [
                "A telemetria S.M.A.R.T. do fabricante não pôde ser consultada diretamente através do barramento USB (sem ATA pass-through).",
                "Métricas do SO refletem apenas retentativas no nível de software e não substituem o diagnóstico de hardware."
            ]
            hipoteses = ["Dispositivo conectado via interface USB que não repassa comandos ATA brutos (sem pass-through)."]
            summary = ["Telemetria S.M.A.R.T. indisponível através da interface USB."]
        else:
            limitacoes = [
                "A telemetria S.M.A.R.T. do fabricante não foi exposta pelo driver ou controladora de armazenamento.",
                "Métricas do SO refletem apenas retentativas no nível de software e não substituem o diagnóstico de hardware."
            ]
            hipoteses = ["Controladora de disco ou driver não expôs registros ATA S.M.A.R.T. diretos para consulta."]
            summary = ["Telemetria S.M.A.R.T. indisponível ou insuficiente para este dispositivo."]

        if is_usb:
            rec_text = (
                "Telemetria S.M.A.R.T. indisponível através da interface USB. "
                "Conecte o disco diretamente a uma porta SATA nativa para telemetria de hardware "
                "ou realize clonagem preventiva setor a setor."
            )
        else:
            rec_text = (
                "O hardware ou driver da controladora de disco não retornou registros S.M.A.R.T. válidos. "
                "Em caso de suspeita de instabilidade física, realize clonagem preventiva setor a setor."
            )

        return SmartReport(
            device_id=device_id,
            device_path=device_path,
            model=model,
            serial=serial,
            risk_level=SmartRiskLevel.UNKNOWN,
            temperature_c=temperature_c,
            reallocated_sectors=None,
            pending_sectors=None,
            uncorrectable_sectors=None,
            crc_errors=None,
            spin_retries=None,
            power_on_hours=None,
            attributes={},
            windows_reliability=windows_reliability,
            summary_messages=summary,
            action_recommendation=rec_text,
            evidencia_observada=evidencias,
            hipotese_provavel=hipoteses,
            limitacoes=limitacoes,
            acao_sugerida=rec_text
        )

    reallocated = raw_attributes.get(5, None)
    pending = raw_attributes.get(197, None)
    uncorrectable = raw_attributes.get(198, None)
    crc_err = raw_attributes.get(199, None)
    spin_retry = raw_attributes.get(10, None)
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
    crc_val = crc_err or 0
    realloc_val = reallocated or 0
    pend_val = pending or 0
    uncorr_val = uncorrectable or 0
    spin_val = spin_retry or 0

    is_red = False
    is_yellow = False

    # Regra 3: Fadiga Mecânica no Motor
    if spin_val > 0:
        is_red = True
        evidencias.append(f"Tentativas de rotação do motor (ID 10): {spin_val} eventos registrados.")
        hipoteses.append("Fadiga mecânica no rolamento do motor de rotação ou fornecimento elétrico instável.")
        messages.append(
            f"⚙️ FADIGA MECÂNICA: {spin_val} falhas de inicialização do motor (ID 10/0A). "
            "O motor de rotação ou o rolamento está tendo dificuldade para atingir a rotação nominal."
        )

    # Regra 2: Degradação de Superfície / Bad Sectors
    if pend_val > 0 or realloc_val > 0 or uncorr_val > 0:
        evidencias.append(
            f"Setores pendentes (197): {pend_val} | Setores remapeados (5): {realloc_val} | "
            f"Irrecuperáveis (198): {uncorr_val}"
        )
        if pend_val > 20 or realloc_val > 100 or uncorr_val > 0:
            is_red = True
            hipoteses.append("Degradação física severa da superfície magnética ou esgotamento de blocos de reserva.")
            messages.append(
                f"🚨 FALHA CRÍTICA DE MÍDIA: {pend_val} setores pendentes (C5), {realloc_val} setores remapeados (05) "
                f"e {uncorr_val} irrecuperáveis (C6). A superfície magnética está degradando rapidamente."
            )
        else:
            is_yellow = True
            hipoteses.append("Presença de setores defeituosos ou instáveis em processo de realocação pelo firmware.")
            messages.append(
                f"⚠️ ATENÇÃO: Detectados {pend_val} setores pendentes e {realloc_val} setores remapeados. "
                "Existem bad sectors sendo monitorados pelo firmware."
            )

    # Regra 1: Mau contato de Cabo SATA / USB
    if crc_val > 0 and realloc_val == 0 and pend_val == 0 and uncorr_val == 0:
        is_yellow = True
        evidencias.append(f"Contador UltraDMA CRC (ID 199/0xC7): {crc_val} erros registrados na interface.")
        evidencias.append("Zero setores pendentes (C5), realocados (05) ou offline uncorrectable (C6) observados.")
        hipoteses.append(
            "Indício de degradação na comunicação física (cabo de dados SATA, conector oxidado ou porta na placa-mãe). "
            "A mídia física não apresenta setores defeituosos registrados até o momento."
        )
        messages.append(
            f"🔍 INDÍCIO DE PROBLEMA DE CABO/INTERFACE: Detectados {crc_val} erros de CRC UltraDMA (ID 199/C7). "
            "Não foram observados setores pendentes ou realocados até o momento. Alta probabilidade de falha "
            "elétrica no cabo de dados SATA ou mau contato na porta. Recomenda-se substituir o cabo SATA e monitorar a integridade."
        )

    # Determinação rigorosa do nível de risco:
    # Para ser VERDE, a telemetria DEVE ter monitorado e confirmado ausência de defeito
    # tanto em setores remapeados (ID 5) quanto em setores pendentes (ID 197).
    has_sufficient_health_evidence = (
        (5 in raw_attributes and raw_attributes[5] == 0) and
        (197 in raw_attributes and raw_attributes[197] == 0)
    )

    if is_red:
        risk = SmartRiskLevel.RED
        recommendation = "RISCO IMINENTE: Crie imediatamente uma Imagem Forense (.img) via Clonagem Resiliente. NÃO execute chkdsk, desfragmentação ou formatação."
    elif is_yellow:
        risk = SmartRiskLevel.YELLOW
        if crc_val > 0 and realloc_val == 0 and pend_val == 0 and uncorr_val == 0:
            recommendation = (
                "Substitua o cabo SATA ou conecte em outra porta da placa-mãe. "
                "Gere uma imagem forense antes de submeter o disco a esforços intensos e execute teste de superfície."
            )
        else:
            recommendation = "Faça backup dos arquivos prioritários ou gere uma imagem clone antes de reparos."
    elif has_sufficient_health_evidence:
        risk = SmartRiskLevel.GREEN
        evidencias.append("Setores remapeados (ID 5) = 0 e setores pendentes (ID 197) = 0 confirmados.")
        evidencias.append("Zero erros de leitura bruta, remapeamento ou CRC nos atributos consultados.")
        hipoteses.append("Mídia operando dentro dos parâmetros nominais para os atributos monitorados.")
        messages.append("Nenhum erro de leitura, remapeamento ou comunicação física detectado.")
        recommendation = (
            "O disco apresenta parâmetros monitorados saudáveis e estáveis. "
            "Recomenda-se realizar snapshot de segurança antes de qualquer intervenção estrutural ou particionamento."
        )
    else:
        # Atributos insuficientes para comprovar saúde física (ex: apenas IDs desconhecidos, apenas horas de uso ou apenas ID 1)
        risk = SmartRiskLevel.UNKNOWN
        evidencias.append("Nenhum atributo crítico de saúde física verificado: telemetria não contém simultaneamente os contadores essenciais de superfície (IDs 5 e 197).")
        hipoteses.append("Dados S.M.A.R.T. insuficientes para comprovar a ausência de setores defeituosos.")
        limitacoes.append("A ausência de contadores críticos de superfície impede atestar se a mídia está sadia.")
        messages.append("Dados S.M.A.R.T. insuficientes para determinar o estado de saúde do disco.")
        recommendation = "Estado de integridade indeterminado. Execute varredura de superfície para avaliar a resposta dos blocos."

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


def query_smart_report_windows(
    device_number: int,
    device_path: str,
    model: str,
    serial: str,
    bus_type: Optional[str] = None,
    is_usb: bool = False
) -> SmartReport:
    """Obtém os contadores de confiabilidade do Windows sem confundir métricas de SO com registros SMART de hardware."""
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
    win_reliability: Dict[str, int] = {}

    try:
        r = subprocess.run(cmd, capture_output=True, timeout=10, creationflags=creationflags)
        import json
        stdout = r.stdout.decode("utf-8", errors="replace").strip()
        if stdout:
            data = json.loads(stdout)
            temperature = data.get("Temperature")
            if data.get("ReadErrorsTotal") is not None:
                win_reliability["ReadErrorsTotal"] = int(data.get("ReadErrorsTotal"))
            if data.get("WriteErrorsTotal") is not None:
                win_reliability["WriteErrorsTotal"] = int(data.get("WriteErrorsTotal"))
            if data.get("Wear") is not None:
                win_reliability["Wear"] = int(data.get("Wear"))
    except Exception as exc:
        log.debug("Falha na consulta de StorageReliabilityCounter: %s", exc)

    is_usb_resolved = is_usb or bool(bus_type and "USB" in bus_type.upper())

    return analyze_smart_data(
        device_id=device_number,
        device_path=device_path,
        model=model,
        serial=serial,
        raw_attributes={},  # Sem atributos SMART de hardware falsamente mapeados
        temperature_c=temperature,
        windows_reliability=win_reliability if win_reliability else None,
        is_usb=is_usb_resolved
    )
