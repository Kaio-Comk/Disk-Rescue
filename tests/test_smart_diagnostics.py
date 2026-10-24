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
