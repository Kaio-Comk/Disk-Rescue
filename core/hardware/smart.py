"""
core/hardware/smart.py — Decodificação e classificação heurística fundamentada de telemetria SMART em HDDs e SSDs.
Separa evidências observadas, hipóteses prováveis, limitações de telemetria e ações sugeridas com transparência técnica.
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
    raw_value: Optional[int] = None
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
    # Seções estruturadas para relatório técnico transparente
    evidencia_observada: List[str] = field(default_factory=list)
    hipotese_provavel: List[str] = field(default_factory=list)
    limitacoes: List[str] = field(default_factory=list)
    acao_sugerida: str = ""


SMART_DEFINITIONS = {
    1: ("Raw Read Error Rate", "Taxa/contador bruto de erros de leitura na cabeça (codificação dependente do fabricante)"),
    5: ("Reallocated Sectors Count", "Setores defeituosos aposentados e remapeados para reserva"),
    9: ("Power-On Hours", "Horas totais de funcionamento acumuladas"),
    10: ("Spin Retry Count", "Tentativas repetidas de alcançar rotação nominal do motor"),
    181: ("Program Fail Count", "Falhas de programação de blocos flash"),
    182: ("Erase Fail Count", "Falhas de apagamento de blocos flash"),
    183: ("Runtime Bad Block", "Blocos defeituosos detectados em tempo de execução"),
    184: ("End-to-End Error", "Erros de paridade entre buffer de memória e controladora"),
    187: ("Reported Uncorrectable", "Erros não corrigidos por código ECC"),
    188: ("Command Timeout", "Comandos abortados por timeout na comunicação com a controladora"),
    191: ("G-Sense Error Rate", "Erros induzidos por impacto mecânico ou vibração externa"),
    196: ("Reallocation Event Count", "Eventos em que o firmware realizou operações de remapeamento"),
    197: ("Current Pending Sector", "Setores instáveis aguardando reescrita/verificação para remapeamento"),
    198: ("Offline Uncorrectable", "Setores irrecuperáveis detectados em varredura offline"),
    199: ("UltraDMA CRC Error Count", "Erros de transmissão elétrica entre disco e host (Cabo SATA / Conector / Ponte)"),
    200: ("Multi-Zone Error Rate", "Taxa de erros ao gravar em um setor/trilha"),
}

# Atributos cujo valor bruto > 0 indica falha ou degradação nos esquemas padrão ATA
AUXILIARY_ERROR_COUNTERS = {181, 182, 183, 191, 200}

# Atributos de integridade/erros conhecidos capazes de sustentar evidência complementar de saúde (ID 9 excluído pois mede apenas tempo ligado)
INTEGRITY_SUPPORTING_IDS = {1, 10, 184, 187, 188, 196, 198, 199}


def _is_valid_int(val: object) -> bool:
    """Retorna True somente se val for um int genuíno (excluindo bool)."""
    return isinstance(val, int) and not isinstance(val, bool)


def _is_known_zero(val: Optional[int]) -> bool:
    """Retorna True somente se o atributo foi efetivamente consultado e seu valor é exatamente zero."""
    return _is_valid_int(val) and val == 0


def _is_positive(val: Optional[int]) -> bool:
    """Retorna True somente se o atributo foi efetivamente consultado e seu valor é > 0."""
    return _is_valid_int(val) and val > 0


def _fmt_counter(val: Optional[int]) -> str:
    """Formata contador distinguindo zero consultado de valor ausente (N/D)."""
    return str(val) if _is_valid_int(val) else "N/D (não consultado)"


def analyze_smart_data(
    device_id: int,
    device_path: str,
    model: str,
    serial: str,
    raw_attributes: Dict[int, Optional[int] | dict],
    temperature_c: Optional[int] = None,
    normalized_attributes: Optional[Dict[int, tuple[Optional[int], Optional[int], Optional[int]]]] = None,
    windows_reliability: Optional[Dict[str, int]] = None,
    is_usb: bool = False
) -> SmartReport:
    """
    Interpreta valores SMART disponibilizados pelo dispositivo usando regras heurísticas transparentes.
    - Não preenche valores normalizados fictícios nem trata valores ausentes (None) ou inválidos como zero.
    - Mantém métricas do Windows separadas dos atributos SMART de hardware.
    - Nunca declara estado GREEN sem evidências de integridade suficientes (ID 9 não conta como evidência de integridade).
    """
    if not raw_attributes:
        evidencias = ["Nenhum atributo S.M.A.R.T. de hardware do fabricante retornado."]
        if windows_reliability:
            evidencias.append(
                f"Métricas do Subsistema Windows (StorageReliabilityCounter): "
                f"ReadErrors={windows_reliability.get('ReadErrorsTotal', 'N/D')}, "
                f"WriteErrors={windows_reliability.get('WriteErrorsTotal', 'N/D')}, "
                f"Wear={windows_reliability.get('Wear', 'N/D')}"
            )

        if is_usb:
            limitacoes = [
                "A telemetria S.M.A.R.T. do fabricante não pôde ser consultada diretamente através do barramento USB (sem ATA pass-through).",
                "Métricas do SO refletem apenas retentativas no nível de software e não substituem o diagnóstico de hardware.",
                "Classificação baseada em regras heurísticas; sem telemetria ATA não é possível aferir a saúde física."
            ]
            hipoteses = ["Dispositivo conectado via interface USB que não repassa comandos ATA brutos (sem pass-through)."]
            summary = ["Telemetria S.M.A.R.T. indisponível através da interface USB."]
            rec_text = (
                "Telemetria S.M.A.R.T. indisponível através da interface USB. "
                "Conecte o disco diretamente a uma porta SATA nativa para telemetria de hardware "
                "ou realize clonagem preventiva setor a setor."
            )
        else:
            limitacoes = [
                "A telemetria S.M.A.R.T. do fabricante não foi exposta pelo driver ou controladora de armazenamento.",
                "Métricas do SO refletem apenas retentativas no nível de software e não substituem o diagnóstico de hardware.",
                "Classificação baseada em regras heurísticas; sem telemetria ATA não é possível aferir a saúde física."
            ]
            hipoteses = ["Controladora de disco ou driver não expôs registros ATA S.M.A.R.T. diretos para consulta."]
            summary = ["Telemetria S.M.A.R.T. indisponível ou insuficiente para este dispositivo."]
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

    evidencias: List[str] = []
    hipoteses: List[str] = []
    limitacoes: List[str] = [
        "A classificação utiliza regras heurísticas sobre os contadores S.M.A.R.T. disponibilizados, não representando probabilidade estatística calibrada.",
        "Fabricantes diferentes (ex: Seagate, WD, Toshiba, SSDs) podem codificar campos brutos (especialmente IDs 1, 9 e 188) em formatos proprietários.",
        "A ausência de erros registrados nos contadores consultados não garante ausência total de defeitos físicos não rastreados pelo firmware."
    ]
    messages: List[str] = []
    has_malformed_telemetry = False

    raw_norm_input: Dict[int, object] = dict(normalized_attributes or {})
    norm_map: Dict[int, tuple[Optional[int], Optional[int], Optional[int]]] = {}
    raw_map: Dict[int, Optional[int]] = {}

    for aid, val in raw_attributes.items():
        if isinstance(val, dict):
            r_v = val.get("raw", val.get("raw_value", None))
            if r_v is None:
                raw_map[aid] = None
            elif _is_valid_int(r_v) and r_v >= 0:
                raw_map[aid] = r_v
            else:
                raw_map[aid] = None
                has_malformed_telemetry = True
                evidencias.append(f"Atributo ID {aid} possui valor bruto malformado ou negativo ({r_v!r}); tratado como N/D.")
                limitacoes.append(f"Valor bruto inválido no atributo ID {aid} ({r_v!r}) descartado para evitar falso positivo/negativo.")

            c_v = val.get("value", val.get("current", None))
            w_v = val.get("worst", None)
            t_v = val.get("thresh", val.get("threshold", None))
            if any(x is not None for x in (c_v, w_v, t_v)):
                raw_norm_input[aid] = (c_v, w_v, t_v)
        elif val is None:
            raw_map[aid] = None
        elif _is_valid_int(val) and val >= 0:
            raw_map[aid] = val
        else:
            raw_map[aid] = None
            has_malformed_telemetry = True
            evidencias.append(f"Atributo ID {aid} possui valor bruto inválido ({val!r}); tratado como N/D (não convertido para zero).")
            limitacoes.append(f"Valor bruto malformado no atributo ID {aid} ({val!r}) impede atestar integridade plena.")

    # Validação rigorosa das tuplas de atributos normalizados (current, worst, threshold)
    for aid, n_tuple in raw_norm_input.items():
        if not isinstance(n_tuple, (tuple, list)) or len(n_tuple) != 3:
            has_malformed_telemetry = True
            evidencias.append(f"Atributo normalizado ID {aid} malformado ({n_tuple!r}); ignorado.")
            limitacoes.append(f"Estrutura normalizada inválida para ID {aid}.")
            continue
        c_v, w_v, t_v = n_tuple
        tuple_valid = True
        if c_v is not None and (not _is_valid_int(c_v) or c_v <= 0 or c_v > 255):
            tuple_valid = False
        if w_v is not None and (not _is_valid_int(w_v) or w_v <= 0 or w_v > 255):
            tuple_valid = False
        if t_v is not None and (not _is_valid_int(t_v) or t_v < 0 or t_v > 255):
            tuple_valid = False
        if not tuple_valid:
            has_malformed_telemetry = True
            evidencias.append(
                f"Valores normalizados impossíveis ou fora da faixa ATA (1..255 / 0..255) no ID {aid}: "
                f"current={c_v!r}, worst={w_v!r}, threshold={t_v!r}."
            )
            limitacoes.append(
                f"Valores normalizados malformados no ID {aid} foram rejeitados e impedem classificação saudável automática."
            )
            norm_map[aid] = (None, None, None)
        else:
            norm_map[aid] = (c_v, w_v, t_v)

    raw_attributes = raw_map
    normalized_attributes = norm_map

    reallocated = raw_attributes.get(5, None)
    pending = raw_attributes.get(197, None)
    uncorrectable = raw_attributes.get(198, None)
    crc_err = raw_attributes.get(199, None)
    spin_retry = raw_attributes.get(10, None)
    power_hours = raw_attributes.get(9, None)
    end_to_end = raw_attributes.get(184, None)
    reported_uncorr = raw_attributes.get(187, None)
    command_timeout = raw_attributes.get(188, None)
    realloc_events = raw_attributes.get(196, None)
    raw_read_err = raw_attributes.get(1, None)

    is_red = False
    is_yellow = False

    attributes: Dict[int, SmartAttribute] = {}
    for aid, raw_val in raw_attributes.items():
        name, desc = SMART_DEFINITIONS.get(aid, (f"Atributo {aid}", "Atributo de telemetria específico do fabricante"))
        cur, worst, thresh = (None, None, None)
        failing = False
        if normalized_attributes and aid in normalized_attributes:
            cur, worst, thresh = normalized_attributes[aid]
            if cur is not None and thresh is not None and thresh > 0 and cur <= thresh:
                failing = True
                is_red = True
                evidencias.append(
                    f"Limiar S.M.A.R.T. violado no atributo ID {aid} ({name}): current={cur} <= threshold={thresh} (raw={raw_val})."
                )
                hipoteses.append(f"O próprio firmware do fabricante sinalizou falha de limiar no atributo {name} (ID {aid}).")
                messages.append(
                    f"🚨 LIMIAR S.M.A.R.T. VIOLADO: {name} (ID {aid}) atingiu ou caiu abaixo do limite crítico ({cur} <= {thresh})."
                )

        attributes[aid] = SmartAttribute(
            id=aid,
            name=name,
            current=cur,
            worst=worst,
            threshold=thresh,
            raw_value=raw_val,
            is_failing=failing,
            description=desc
        )

    # Avaliação do ID 9 (Power-On Hours) quando consultado
    if power_hours is not None:
        # Alguns fabricantes codificam minutos/segundos nos bits superiores de 48 bits; extrai 24 bits inferiores se absurdo
        effective_hours = power_hours & 0xFFFFFF if power_hours > 200_000 else power_hours
        evidencias.append(f"Horas de funcionamento registradas (ID 9): {effective_hours} h (bruto={power_hours}).")
        if effective_hours >= 50_000:
            limitacoes.append(
                f"Tempo de operação elevado ({effective_hours} horas no ID 9) aumenta o desgaste mecânico/elétrico acumulado."
            )

    # Avaliação de Temperatura quando disponível
    if temperature_c is not None:
        evidencias.append(f"Temperatura reportada: {temperature_c} °C.")
        if temperature_c >= 70:
            is_red = True
            hipoteses.append("Superaquecimento crítico da unidade em operação (>= 70 °C).")
            messages.append(f"🚨 TEMPERATURA CRÍTICA: {temperature_c} °C. Risco imediato de dano térmico e perda de dados.")
        elif temperature_c >= 58:
            is_yellow = True
            hipoteses.append("Temperatura de operação elevada (>= 58 °C), podendo acelerar degradação ou causar instabilidade.")
            messages.append(f"⚠️ TEMPERATURA ELEVADA: {temperature_c} °C. Melhore a ventilação antes de operações longas.")

    # Regra: Erros End-to-End na controladora/buffer interno (ID 184)
    if _is_positive(end_to_end):
        is_red = True
        evidencias.append(f"Erros de buffer/controladora End-to-End (ID 184): {end_to_end} erros registrados.")
        hipoteses.append("Corrupção no caminho de dados interno entre a memória cache do disco e a interface da controladora.")
        messages.append(
            f"🚨 FALHA DE BUFFER END-TO-END: {end_to_end} erros no ID 184. "
            "Risco iminente de corrupção silenciosa de dados em trânsito."
        )

    # Regra: Erros não corrigidos por ECC reportados pelo hardware (ID 187)
    if _is_positive(reported_uncorr):
        is_red = True
        evidencias.append(f"Setores não corrigidos por código ECC (ID 187): {reported_uncorr} setores reportados.")
        hipoteses.append("Setores com corrupção severa que excederam a capacidade de correção de erros do hardware (ECC).")
        messages.append(
            f"🚨 ERROS NÃO CORRIGIDOS POR ECC: {reported_uncorr} leituras falharam sem possibilidade de correção por hardware (ID 187)."
        )

    # Regra: Comandos abortados por timeout (ID 188)
    if _is_positive(command_timeout):
        is_yellow = True
        evidencias.append(f"Comandos abortados por tempo limite / Timeout (ID 188): {command_timeout} (valor bruto).")
        hipoteses.append("Instabilidade no fornecimento de energia elétrica, congelamento temporário do firmware ou travamento de comandos.")
        messages.append(
            f"⚠️ TIMEOUT DE COMANDOS (ID 188): valor bruto {command_timeout} indica operações abortadas por expiração de tempo limite."
        )

    # Regra: Eventos de realocação executados pelo firmware (ID 196)
    if _is_positive(realloc_events):
        evidencias.append(f"Eventos de realocação executados pelo firmware (ID 196): {realloc_events} ocorrências.")
        if realloc_events > 15:
            is_red = True
            hipoteses.append("Múltiplos ciclos sucessivos de realocação de bad sectors pelo firmware do disco.")
        else:
            is_yellow = True
            hipoteses.append("Tentativas prévias de remapeamento de blocos defeituosos registradas pelo firmware.")
        messages.append(
            f"⚠️ EVENTOS DE REALOCAÇÃO (ID 196): O firmware realizou {realloc_events} operações de remapeamento de setores."
        )
        # Combinação contraditória: eventos de realocação > 0 mas setores remapeados (ID 5) == 0
        if _is_known_zero(reallocated):
            evidencias.append(
                "Inconsistência / divergência observada entre ID 196 (> 0 eventos de realocação) e ID 5 (0 setores remapeados)."
            )
            hipoteses.append(
                "Tentativas de realocação registradas no ID 196 sem incremento correspondente no ID 5 "
                "(possível recuperação in-place ou semântica específica do firmware)."
            )

    # Regra: Taxa/contador bruto de erros de leitura (ID 1)
    if _is_positive(raw_read_err):
        evidencias.append(
            f"Contador bruto de erros de leitura (ID 1): {raw_read_err} "
            "(interpretação depende da codificação específica do fabricante)."
        )
        # Se houver valor normalizado saudável explícito (current >> threshold), registra limitação;
        # caso contrário, eleva para atenção (YELLOW) pois o contador bruto é não nulo.
        norm_1 = normalized_attributes.get(1) if normalized_attributes else None
        if norm_1 and norm_1[0] is not None and norm_1[2] is not None and norm_1[0] > norm_1[2] + 20:
            limitacoes.append(
                f"O ID 1 possui valor bruto não nulo ({raw_read_err}), mas o índice normalizado do fabricante ({norm_1[0]} > limiar {norm_1[2]}) permanece dentro da faixa nominal."
            )
        else:
            is_yellow = True
            hipoteses.append(
                "Valor bruto não nulo no ID 1 (Raw Read Error Rate) pode indicar retentativas de leitura na superfície ou codificação proprietária sem valores normalizados."
            )
            messages.append(
                f"⚠️ ATENÇÃO NO ID 1 (Raw Read Error Rate): valor bruto {raw_read_err} observado. "
                "Como a codificação bruta varia por fabricante e não há confirmação normalizada, recomenda-se cautela."
            )

    # Regra: Fadiga Mecânica no Motor (ID 10)
    if _is_positive(spin_retry):
        is_red = True
        evidencias.append(f"Tentativas de rotação do motor (ID 10): {spin_retry} eventos registrados.")
        hipoteses.append("Fadiga mecânica no rolamento do motor de rotação ou fornecimento elétrico instável.")
        messages.append(
            f"⚙️ FADIGA MECÂNICA: {spin_retry} falhas de inicialização do motor (ID 10/0A). "
            "O motor de rotação ou o rolamento está tendo dificuldade para atingir a rotação nominal."
        )

    # Regra: Degradação de Superfície / Bad Sectors (IDs 5, 197, 198)
    has_surface_errors = _is_positive(pending) or _is_positive(reallocated) or _is_positive(uncorrectable)
    if has_surface_errors:
        evidencias.append(
            f"Setores pendentes (197): {_fmt_counter(pending)} | "
            f"Setores remapeados (5): {_fmt_counter(reallocated)} | "
            f"Irrecuperáveis (198): {_fmt_counter(uncorrectable)}"
        )
        if (pending is not None and pending > 20) or (reallocated is not None and reallocated > 100) or _is_positive(uncorrectable):
            is_red = True
            hipoteses.append("Degradação física severa da superfície magnética/flash ou setores irrecuperáveis confirmados.")
            messages.append(
                f"🚨 FALHA CRÍTICA DE MÍDIA: pendentes (C5)={_fmt_counter(pending)}, "
                f"remapeados (05)={_fmt_counter(reallocated)}, irrecuperáveis (C6)={_fmt_counter(uncorrectable)}. "
                "A superfície está degradando."
            )
        else:
            is_yellow = True
            hipoteses.append("Presença de setores defeituosos ou instáveis em monitoramento/realocação pelo firmware.")
            messages.append(
                f"⚠️ ATENÇÃO: Detectados setores pendentes ({_fmt_counter(pending)}) e/ou remapeados ({_fmt_counter(reallocated)}). "
                "Existem bad sectors sendo monitorados pelo firmware."
            )

    # Regra: Erros de comunicação elétrica UltraDMA CRC (ID 199)
    if _is_positive(crc_err):
        is_yellow = True
        evidencias.append(f"Contador UltraDMA CRC (ID 199/0xC7): {crc_err} erros registrados na interface.")
        surface_confirmed_zero = _is_known_zero(reallocated) and _is_known_zero(pending) and (
            uncorrectable is None or _is_known_zero(uncorrectable)
        )
        if surface_confirmed_zero and not has_surface_errors:
            evidencias.append(
                f"Contadores de superfície consultados com zero erros: ID 5=0, ID 197=0"
                + (", ID 198=0." if _is_known_zero(uncorrectable) else " (ID 198 não consultado).")
            )
            hipoteses.append(
                "Indício de degradação na comunicação física (cabo de dados SATA, conector oxidado ou porta na placa-mãe). "
                "Os contadores de superfície consultados (IDs 5 e 197) estão zerados."
            )
            messages.append(
                f"🔍 INDÍCIO DE PROBLEMA DE CABO/INTERFACE: Detectados {crc_err} erros de CRC UltraDMA (ID 199/C7). "
                "Não foram observados setores pendentes ou realocados nos atributos consultados. Alta probabilidade de falha "
                "elétrica no cabo de dados SATA ou mau contato na porta. Recomenda-se substituir o cabo SATA e monitorar a integridade."
            )
        elif has_surface_errors:
            hipoteses.append(
                "Falha combinada: erros de comunicação na interface SATA/ponte (ID 199) ocorrendo simultaneamente com defeitos de superfície."
            )
            messages.append(
                f"⚠️ ERROS DE INTERFACE E MÍDIA COMBINADOS: Além da degradação de superfície, foram detectados {crc_err} erros de CRC UltraDMA (ID 199/C7). "
                "Verifique o cabo SATA/conexão antes de iniciar a clonagem resiliente."
            )
        else:
            limitacoes.append(
                "Erros de CRC (ID 199) foram detectados, porém os contadores essenciais de superfície (IDs 5 e 197) não estão ambos disponíveis para descartar danos na mídia."
            )
            hipoteses.append(
                "Erros de comunicação elétrica na interface (ID 199), com estado da superfície física indeterminado por ausência de IDs 5/197."
            )
            messages.append(
                f"🔍 ERROS DE INTERFACE (ID 199): {crc_err} erros de CRC detectados (contadores de superfície IDs 5/197 incompletos). "
                "Substitua o cabo SATA e realize verificação cautelosa."
            )

    # Avaliação de outros contadores auxiliares críticos (IDs 181, 182, 183, 191, 200)
    for aux_id in sorted(AUXILIARY_ERROR_COUNTERS):
        aux_val = raw_attributes.get(aux_id, None)
        if _is_positive(aux_val):
            aux_name = SMART_DEFINITIONS[aux_id][0]
            is_yellow = True
            evidencias.append(f"Indicador adicional não nulo — {aux_name} (ID {aux_id}): {aux_val}.")
            hipoteses.append(f"Anomalia reportada pelo contador {aux_name} (ID {aux_id} = {aux_val}).")
            messages.append(f"⚠️ ALERTA EM INDICADOR ADICIONAL: {aux_name} (ID {aux_id}) = {aux_val}.")

    # Avaliação segregada de métricas do Subsistema Windows (quando fornecidas em conjunto com SMART)
    if windows_reliability:
        win_r = windows_reliability.get("ReadErrorsTotal")
        win_w = windows_reliability.get("WriteErrorsTotal")
        win_wear = windows_reliability.get("Wear")
        evidencias.append(
            f"Métricas segregadas do Subsistema Windows (StorageReliabilityCounter): "
            f"ReadErrors={win_r if win_r is not None else 'N/D'}, "
            f"WriteErrors={win_w if win_w is not None else 'N/D'}, "
            f"Wear={win_wear if win_wear is not None else 'N/D'}"
        )
        if _is_positive(win_r) or _is_positive(win_w) or (win_wear is not None and win_wear >= 90):
            is_yellow = True
            hipoteses.append(
                "O subsistema de armazenamento do Windows registrou erros de I/O ou desgaste elevado, mesmo que alguns contadores SMART estáticos pareçam zerados."
            )
            messages.append(
                f"⚠️ ALERTA DO SUBSISTEMA WINDOWS: O subsistema Windows reportou ReadErrors={win_r or 0}, WriteErrors={win_w or 0}, Wear={win_wear or 0}%."
            )

    # Regra para atributos desconhecidos com valores não nulos sem cobertura completa
    unknown_ids = [aid for aid in raw_attributes if aid not in SMART_DEFINITIONS]
    if unknown_ids:
        limitacoes.append(
            f"Atributos específicos do fabricante não padronizados presentes ({', '.join(str(x) for x in sorted(unknown_ids))}) não possuem decodificação universal."
        )

    unknown_positive = [aid for aid in unknown_ids if _is_positive(raw_attributes.get(aid))]
    if unknown_positive and _is_known_zero(reallocated) and _is_known_zero(pending):
        for up_id in sorted(unknown_positive):
            evidencias.append(f"Indicador não padronizado com valor positivo: ID {up_id}={raw_attributes[up_id]}.")
            messages.append(
                f"⚠️ ATRIBUTO ESPECÍFICO NÃO NULO: ID {up_id} = {raw_attributes[up_id]} observado sem decodificação padronizada."
            )

    # Determinação rigorosa do nível de risco (Seções 10 e 11):
    # O estado GREEN só pode ser atribuído quando:
    # 1. ID 5 e ID 197 foram efetivamente consultados e ambos valem 0;
    # 2. Se ID 198 foi consultado, vale 0;
    # 3. Pelo menos mais um indicador de INTEGRIDADE (INTEGRITY_SUPPORTING_IDS = {1, 10, 184, 187, 188, 196, 198, 199})
    #    foi efetivamente consultado com valor válido — ID 9 (Power-On Hours) NÃO conta como evidência de integridade;
    # 4. Nenhum atributo consultado contém valor nulo/malformado ou fora da faixa normalizada;
    # 5. Nenhum contador de erro crítico ou auxiliar é > 0 e nenhum alerta (RED/YELLOW) foi acionado.
    critical_counters = [1, 5, 10, 181, 182, 183, 184, 187, 188, 191, 196, 197, 198, 199, 200]
    has_any_critical_errors = any(_is_positive(raw_attributes.get(k)) for k in critical_counters)

    if unknown_positive and set(raw_attributes.keys()) == ({5, 197} | set(unknown_positive)):
        evidencias.append(
            f"Indicadores não interpretados com valor positivo: {', '.join(f'ID {k}={raw_attributes[k]}' for k in sorted(unknown_positive))}."
        )

    has_supporting_telemetry = any(
        _is_valid_int(raw_attributes.get(k)) for k in INTEGRITY_SUPPORTING_IDS
    )
    has_any_none_raw = any(v is None for v in raw_attributes.values())

    has_sufficient_health_evidence = (
        _is_known_zero(reallocated)
        and _is_known_zero(pending)
        and (uncorrectable is None or _is_known_zero(uncorrectable))
        and has_supporting_telemetry
        and not has_any_critical_errors
        and not unknown_positive
        and not has_malformed_telemetry
        and not has_any_none_raw
        and not is_red
        and not is_yellow
    )

    if is_red:
        risk = SmartRiskLevel.RED
        recommendation = "RISCO IMINENTE: Crie imediatamente uma Imagem Forense (.img) via Clonagem Resiliente. NÃO execute chkdsk, desfragmentação ou formatação."
    elif is_yellow:
        risk = SmartRiskLevel.YELLOW
        if _is_positive(crc_err) and not has_surface_errors and _is_known_zero(reallocated) and _is_known_zero(pending):
            recommendation = (
                "Substitua o cabo SATA ou conecte em outra porta da placa-mãe. "
                "Gere uma imagem forense antes de submeter o disco a esforços intensos e execute teste de superfície."
            )
        else:
            recommendation = "Faça backup dos arquivos prioritários ou gere uma imagem clone antes de reparos."
    elif has_sufficient_health_evidence:
        risk = SmartRiskLevel.GREEN
        queried_zeros = [f"ID {k}=0" for k in sorted(raw_attributes.keys()) if _is_known_zero(raw_attributes.get(k))]
        evidencias.append(
            f"Setores remapeados (ID 5) = 0 e setores pendentes (ID 197) = 0 confirmados ({', '.join(queried_zeros)})."
        )
        evidencias.append("Zero erros de leitura, remapeamento ou comunicação nos atributos efetivamente consultados.")
        hipoteses.append("Mídia operando dentro dos parâmetros monitorados avaliados pelas regras heurísticas (não constitui garantia absoluta contra falhas não rastreadas).")
        messages.append("Saúde monitorada dentro dos parâmetros avaliados: nenhum erro de leitura, remapeamento ou comunicação física nos contadores consultados.")
        recommendation = (
            "O disco apresenta parâmetros monitorados saudáveis segundo os atributos S.M.A.R.T. avaliados (não representa garantia absoluta de ausência de defeitos físicos). "
            "Recomenda-se realizar snapshot de segurança antes de qualquer intervenção estrutural ou particionamento."
        )
    else:
        risk = SmartRiskLevel.UNKNOWN
        if not (_is_known_zero(reallocated) and _is_known_zero(pending)):
            evidencias.append(
                "Nenhum atributo crítico de saúde física completo: telemetria não contém simultaneamente os contadores essenciais de superfície (IDs 5 e 197) com valores válidos."
            )
        elif not has_supporting_telemetry:
            evidencias.append(
                "Apenas IDs 5 e 197 isolados (ou acompanhados somente de ID 9 Power-On Hours) não constituem evidência de integridade suficiente para declarar estado saudável."
            )
        else:
            evidencias.append(
                "Presença de atributos não interpretados, ausentes (N/D) ou valores malformados impede atestar saúde integral."
            )
        hipoteses.append("Dados S.M.A.R.T. insuficientes para comprovar de forma conclusiva a integridade global do disco.")
        limitacoes.append("A ausência de cobertura suficiente de contadores de integridade impede declarar estado saudável (Verde).")
        messages.append("Dados S.M.A.R.T. insuficientes para determinar com segurança o estado saudável do disco.")
        recommendation = "Estado de integridade indeterminado pelas evidências S.M.A.R.T. Execute varredura de superfície ou clonagem preventiva."

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
        windows_reliability=windows_reliability,
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
    """
    Obtém telemetria S.M.A.R.T. real via WMI (MSStorageDriver_FailurePredictData / Thresholds)
    e contadores de confiabilidade do Windows (StorageReliabilityCounter), mantendo métricas do SO
    segregadas dos atributos ATA de hardware.
    """
    script = f"""
    [Console]::OutputEncoding = [System.Text.Encoding]::UTF8
    $phys = Get-PhysicalDisk | Where-Object {{ $_.DeviceId -eq '{device_number}' }} | Select-Object -First 1
    $rel = if ($phys) {{ $phys | Get-StorageReliabilityCounter -ErrorAction SilentlyContinue }} else {{ $null }}
    $dd = Get-CimInstance Win32_DiskDrive -ErrorAction SilentlyContinue | Where-Object {{ $_.Index -eq {device_number} }} | Select-Object -First 1
    $pnp = if ($dd) {{ $dd.PNPDeviceID }} else {{ $null }}

    $smartVendor = @()
    $smartThresh = @()
    if ($pnp) {{
        $pred = Get-CimInstance -Namespace root\\wmi -ClassName MSStorageDriver_FailurePredictData -ErrorAction SilentlyContinue |
            Where-Object {{ $_.InstanceName -like "$pnp*" }} | Select-Object -First 1
        if ($pred -and $pred.VendorSpecific) {{
            $smartVendor = $pred.VendorSpecific
        }}
        $th = Get-CimInstance -Namespace root\\wmi -ClassName MSStorageDriver_FailurePredictThresholds -ErrorAction SilentlyContinue |
            Where-Object {{ $_.InstanceName -like "$pnp*" }} | Select-Object -First 1
        if ($th -and $th.VendorSpecific) {{
            $smartThresh = $th.VendorSpecific
        }}
    }}

    [PSCustomObject]@{{
        Temperature = if ($rel) {{ $rel.Temperature }} else {{ $null }}
        ReadErrorsTotal = if ($rel) {{ $rel.ReadErrorsTotal }} else {{ $null }}
        WriteErrorsTotal = if ($rel) {{ $rel.WriteErrorsTotal }} else {{ $null }}
        Wear = if ($rel) {{ $rel.Wear }} else {{ $null }}
        PowerOnHours = if ($rel) {{ $rel.PowerOnHours }} else {{ $null }}
        VendorSpecific = $smartVendor
        Thresholds = $smartThresh
    }} | ConvertTo-Json -Compress
    """
    cmd = ["powershell", "-NoProfile", "-Command", script]
    creationflags = 0x08000000 if os.name == "nt" else 0

    temperature: Optional[int] = None
    win_reliability: Dict[str, int] = {}
    raw_attrs: Dict[int, int] = {}
    norm_attrs: Dict[int, tuple[Optional[int], Optional[int], Optional[int]]] = {}

    try:
        r = subprocess.run(cmd, capture_output=True, timeout=10, creationflags=creationflags)
        import json
        stdout = r.stdout.decode("utf-8", errors="replace").strip()
        if stdout:
            data = json.loads(stdout)
            t_val = data.get("Temperature")
            if isinstance(t_val, int) and t_val > 0:
                temperature = t_val
            if data.get("ReadErrorsTotal") is not None:
                win_reliability["ReadErrorsTotal"] = int(data.get("ReadErrorsTotal"))
            if data.get("WriteErrorsTotal") is not None:
                win_reliability["WriteErrorsTotal"] = int(data.get("WriteErrorsTotal"))
            if data.get("Wear") is not None:
                win_reliability["Wear"] = int(data.get("Wear"))

            vs = data.get("VendorSpecific") or []
            th = data.get("Thresholds") or []
            tmap: Dict[int, int] = {}
            if isinstance(th, list) and len(th) >= 14:
                for i in range(2, min(len(th), 362), 12):
                    if i + 1 < len(th):
                        aid = int(th[i])
                        if aid != 0:
                            tmap[aid] = int(th[i + 1])

            if isinstance(vs, list) and len(vs) >= 14:
                for i in range(2, min(len(vs), 362), 12):
                    if i + 10 < len(vs):
                        aid = int(vs[i])
                        if aid == 0:
                            continue
                        cur = int(vs[i + 3])
                        worst = int(vs[i + 4])
                        raw_val = sum(int(vs[i + 5 + b]) << (8 * b) for b in range(6))
                        # Temperatura no ID 194 (0xC2) ou 190 (0xBE) se não reportada via StorageReliabilityCounter
                        if aid in (194, 190) and temperature is None:
                            temp_low = raw_val & 0xFF
                            if 0 < temp_low < 120:
                                temperature = temp_low
                        if aid in SMART_DEFINITIONS:
                            raw_attrs[aid] = raw_val
                            thresh_val = tmap.get(aid, 0)
                            if 1 <= cur <= 255 and 1 <= worst <= 255 and 0 <= thresh_val <= 255:
                                norm_attrs[aid] = (cur, worst, thresh_val)
    except Exception as exc:
        log.debug("Falha na consulta WMI S.M.A.R.T. / StorageReliabilityCounter: %s", exc)

    is_usb_resolved = is_usb or bool(bus_type and "USB" in bus_type.upper())

    return analyze_smart_data(
        device_id=device_number,
        device_path=device_path,
        model=model,
        serial=serial,
        raw_attributes=raw_attrs,
        temperature_c=temperature,
        normalized_attributes=norm_attrs if norm_attrs else None,
        windows_reliability=win_reliability if win_reliability else None,
        is_usb=is_usb_resolved
    )
