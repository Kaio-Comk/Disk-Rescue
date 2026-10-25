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
