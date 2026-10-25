"""
tests/test_smart_diagnostics.py — Testes unitários para interpretação preditiva de SMART e diagnóstico de cabo/mídia.
"""
from core.hardware.smart import SmartRiskLevel, analyze_smart_data


def test_smart_healthy():
    report = analyze_smart_data(
        device_id=0,
        device_path=r"\\.\PhysicalDrive0",
        model="Seagate Barracuda 2TB",
        serial="WXB123",
        raw_attributes={1: 0, 5: 0, 197: 0, 199: 0},
        temperature_c=35
    )
    assert report.risk_level == SmartRiskLevel.GREEN
    assert report.crc_errors == 0
    assert report.reallocated_sectors == 0
    assert "saudáveis" in report.action_recommendation.lower()


def test_smart_sata_cable_fault_detected():
    # Caso real muito comum: HD perfeito, mas cabo SATA frouxo/danificado gerando erros C7
    report = analyze_smart_data(
        device_id=1,
        device_path=r"\\.\PhysicalDrive1",
        model="Western Digital Blue 1TB",
        serial="WD12345",
        raw_attributes={5: 0, 197: 0, 198: 0, 199: 42},
        temperature_c=38
    )
    assert report.risk_level == SmartRiskLevel.YELLOW
    assert report.crc_errors == 42
    assert report.reallocated_sectors == 0
    assert any("cabo sata" in m.lower() for m in report.summary_messages)
    assert "substitua o cabo sata" in report.action_recommendation.lower()


def test_smart_surface_critical_degradation():
    # HD morrendo com muitos setores pendentes e remapeados
    report = analyze_smart_data(
        device_id=2,
        device_path=r"\\.\PhysicalDrive2",
        model="Samsung Spinpoint 500GB",
        serial="SAM999",
        raw_attributes={5: 140, 197: 55, 198: 12, 199: 0},
        temperature_c=48
    )
    assert report.risk_level == SmartRiskLevel.RED
    assert report.reallocated_sectors == 140
    assert report.pending_sectors == 55
    assert any("falha crítica de mídia" in m.lower() for m in report.summary_messages)
    assert "risco iminente" in report.action_recommendation.lower()


def test_smart_mechanical_spin_retry():
    # Falha mecânica de partida do motor
    report = analyze_smart_data(
        device_id=3,
        device_path=r"\\.\PhysicalDrive3",
        model="Hitachi Deskstar",
        serial="HIT777",
        raw_attributes={10: 8, 5: 0, 197: 0},
        temperature_c=40
    )
    assert report.risk_level == SmartRiskLevel.RED
    assert report.spin_retries == 8
    assert any("fadiga mecânica" in m.lower() for m in report.summary_messages)


def test_smart_no_telemetry_returns_unknown():
    # Sem atributos SMART nem contadores de telemetria, não declarar verde falsamente
    report = analyze_smart_data(
        device_id=4,
        device_path=r"\\.\PhysicalDrive4",
        model="Generic USB Flash Disk",
        serial="UNKNOWN",
        raw_attributes={},
        temperature_c=None
    )
    assert report.risk_level == SmartRiskLevel.UNKNOWN
    assert report.reallocated_sectors is None
    assert report.pending_sectors is None
    assert report.uncorrectable_sectors is None
    assert report.crc_errors is None
    assert report.spin_retries is None
    assert any("não disponível" in m.lower() or "insuficiente" in m.lower() for m in report.summary_messages)


def test_smart_read_errors_not_confused_with_pending_sectors():
    # Erros de leitura brutos (ID 1) não devem ser confundidos com bad sectors pendentes (ID 197)
    report = analyze_smart_data(
        device_id=5,
        device_path=r"\\.\PhysicalDrive5",
        model="Crucial SSD 500GB",
        serial="CRU1234",
        raw_attributes={1: 15, 5: 0, 197: 0, 198: 0, 199: 0},
        temperature_c=32
    )
    assert report.pending_sectors == 0
    assert report.reallocated_sectors == 0


def test_smart_windows_reliability_counters_remain_segregated_and_unknown():
    """P2 Teste: Métricas do Windows não poluem raw_attributes e ausência de SMART mantém UNKNOWN."""
    report = analyze_smart_data(
        device_id=6,
        device_path=r"\\.\PhysicalDrive6",
        model="NVMe SSD",
        serial="NVME001",
        raw_attributes={},  # Nenhum registrador SMART ATA do fabricante
        temperature_c=41,
        windows_reliability={
            "ReadErrorsTotal": 0,
            "WriteErrorsTotal": 0,
            "Wear": 5
        }
    )
    # Deve permanecer UNKNOWN porque não há registros SMART de hardware
    assert report.risk_level == SmartRiskLevel.UNKNOWN
    # raw_attributes deve permanecer vazio (sem criar atributos falsos como ID 1 ou ID 197)
    assert len(report.attributes) == 0
    assert report.windows_reliability is not None
    assert report.windows_reliability["Wear"] == 5
    assert any("subsistema windows" in e.lower() for e in report.evidencia_observada)


def test_smart_unknown_when_only_uninformative_attributes_present():
    """P2 Teste: Garante que atributos sem indicadores críticos de saúde retornem UNKNOWN (não verde)."""
    report = analyze_smart_data(
        device_id=7,
        device_path=r"\\.\PhysicalDrive7",
        model="Generic Flash Disk",
        serial="GEN001",
        raw_attributes={999: 42, 9: 500},  # Apenas horas de uso e ID desconhecido
        temperature_c=30
    )
    assert report.risk_level == SmartRiskLevel.UNKNOWN
    assert any("nenhum atributo crítico" in e.lower() for e in report.evidencia_observada)


def test_smart_missing_critical_5_or_197_prevents_green():
    """P2 Teste: Garante que a ausência de ID 5 ou ID 197 impeça GREEN mesmo com todos os outros zerados."""
    report = analyze_smart_data(
        device_id=8,
        device_path=r"\\.\PhysicalDrive8",
        model="SSD Incompleto",
        serial="INC001",
        raw_attributes={1: 0, 198: 0, 199: 0},  # Faltam ID 5 e ID 197
        temperature_c=28
    )
    assert report.risk_level == SmartRiskLevel.UNKNOWN
    assert any("nenhum atributo crítico" in e.lower() for e in report.evidencia_observada)


def test_smart_usb_vs_non_usb_unknown_differentiation():
    """P2 Teste: Diferenciação honesta entre limitação de ponte USB e ausência de telemetria geral."""
    # Cenário USB
    rep_usb = analyze_smart_data(
        device_id=9,
        device_path=r"\\.\PhysicalDrive9",
        model="USB Adapter Disk",
        serial="USB123",
        raw_attributes={},
        is_usb=True
    )
    assert rep_usb.risk_level == SmartRiskLevel.UNKNOWN
    assert "usb" in rep_usb.action_recommendation.lower()
    assert any("usb" in h.lower() for h in rep_usb.hipotese_provavel)

    # Cenário NÃO-USB (ex: SATA interno ou NVMe sem telemetria ATA direta)
    rep_sata = analyze_smart_data(
        device_id=10,
        device_path=r"\\.\PhysicalDrive10",
        model="Internal HDD",
        serial="INT456",
        raw_attributes={},
        is_usb=False
    )
    assert rep_sata.risk_level == SmartRiskLevel.UNKNOWN
    assert "controladora de disco" in rep_sata.action_recommendation.lower()
    assert "usb" not in rep_sata.action_recommendation.lower()


def test_smart_interprets_all_extended_attributes_correctly():
    """P1 Teste: Valida que IDs 184, 187, 188 e 196 são interpretados e elevam risco conforme o rigor técnico."""
    # 1. ID 184 (End-to-End Error) eleva para RED
    rep_184 = analyze_smart_data(
        device_id=11,
        device_path=r"\\.\PhysicalDrive11",
        model="Server Enterprise SSD",
        serial="SRV001",
        raw_attributes={5: 0, 197: 0, 184: 3},
        temperature_c=36
    )
    assert rep_184.risk_level == SmartRiskLevel.RED
    assert any("end-to-end" in m.lower() for m in rep_184.summary_messages)

    # 2. ID 187 (Reported Uncorrectable) eleva para RED
    rep_187 = analyze_smart_data(
        device_id=12,
        device_path=r"\\.\PhysicalDrive12",
        model="Seagate Enterprise",
        serial="SEA002",
        raw_attributes={5: 0, 197: 0, 187: 7},
        temperature_c=39
    )
    assert rep_187.risk_level == SmartRiskLevel.RED
    assert any("ecc" in m.lower() for m in rep_187.summary_messages)

    # 3. ID 188 (Command Timeout) eleva para YELLOW
    rep_188 = analyze_smart_data(
        device_id=13,
        device_path=r"\\.\PhysicalDrive13",
        model="WD Red NAS",
        serial="WDR003",
        raw_attributes={5: 0, 197: 0, 188: 4},
        temperature_c=34
    )
    assert rep_188.risk_level == SmartRiskLevel.YELLOW
    assert any("timeout" in m.lower() for m in rep_188.summary_messages)

    # 4. ID 196 (Reallocation Event Count) eleva risco
    rep_196 = analyze_smart_data(
        device_id=14,
        device_path=r"\\.\PhysicalDrive14",
        model="Toshiba DT01ACA",
        serial="TOS004",
        raw_attributes={5: 0, 197: 0, 196: 5},
        temperature_c=37
    )
    assert rep_196.risk_level == SmartRiskLevel.YELLOW
    assert any("realocação" in m.lower() for m in rep_196.summary_messages)


