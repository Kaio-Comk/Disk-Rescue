"""
tests/test_gui_lifecycle.py — Testes automatizados do ciclo de vida e renderização da GUI do DiskRescue.
Valida que a interface inicializa corretamente, troca de visualizações, recebe atualizações assíncronas
da fila e renderiza métricas de hardware, S.M.A.R.T., clonagem e superfície sem lançar exceções.
"""
import tkinter as tk
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from core.hardware.scanner import DiskInfo
from core.hardware.smart import SmartReport, SmartRiskLevel
from core.hardware.surface_scan import SurfaceBlockResult, SurfaceScanSummary
from core.recovery.resilient_clone import ResilientCloneStats
from gui.app import DiskRescueApp


@pytest.fixture(scope="module")
def mock_gui():
    try:
        root = tk.Tk()
        root.withdraw()  # Não exibe janela física durante testes
    except Exception as e:
        pytest.skip(f"Ambiente headless sem suporte a Tkinter/Tcl: {e}")

    app = DiskRescueApp(root)
    root.update()
    yield app, root
    try:
        root.update()
        root.destroy()
    except Exception:
        pass


def test_gui_instantiation_and_views_presence(mock_gui):
    app, root = mock_gui
    expected_views = ["dashboard", "diagnostic", "smart", "clone", "carver", "surface", "logs"]
    for view_key in expected_views:
        assert view_key in app.views
        assert view_key in app.nav_buttons

    for view_key in expected_views:
        app._switch_view(view_key)
        assert app.active_view == view_key


def test_gui_render_disks_list(mock_gui):
    app, root = mock_gui
    fake_disks = [
        DiskInfo(
            number=0,
            device_path=r"\\.\PhysicalDrive0",
            friendly_name="Samsung SSD 870 EVO 1TB",
            serial_number="S5Y2NJ0N123456",
            size_bytes=1_000_204_886_016,
            bus_type="SATA",
            partition_style="GPT",
            media_type="SSD",
            health_status="Healthy",
            operational_status="OK",
            temperature=34,
            read_errors=0,
            write_errors=0,
            wear=None,
            is_read_only=False,
            is_boot=True,
            is_system=True,
            can_open_raw=True
        ),
        DiskInfo(
            number=1,
            device_path=r"\\.\PhysicalDrive1",
            friendly_name="Seagate ST2000DM008 2TB",
            serial_number="W4601234",
            size_bytes=2_000_398_934_016,
            bus_type="SATA",
            partition_style="MBR",
            media_type="HDD",
            health_status="Warning",
            operational_status="Degraded",
            temperature=42,
            read_errors=5,
            write_errors=0,
            wear=None,
            is_read_only=False,
            is_boot=False,
            is_system=False,
            can_open_raw=True
        )
    ]

    app._render_disks_list(fake_disks)
    assert len(app.disks) == 2
    assert app.selected_disk is not None
    assert app.selected_disk.number == 0
    assert "Samsung" in app.lbl_mini_model.cget("text")


def test_gui_render_smart_metrics_honest_nd(mock_gui):
    app, root = mock_gui
    # Relatório com valores None (disco USB sem telemetria pass-through)
    report = SmartReport(
        device_id="1",
        device_path=r"\\.\PhysicalDrive1",
        model="Generic USB Drive",
        serial=None,
        risk_level=SmartRiskLevel.UNKNOWN,
        temperature_c=None,
        reallocated_sectors=None,
        pending_sectors=None,
        uncorrectable_sectors=None,
        crc_errors=None,
        spin_retries=None,
        power_on_hours=None,
        attributes=[],
        windows_reliability={},
        summary_messages=["Telemetria ATA indisponível através desta controladora USB."],
        action_recommendation="Conecte diretamente em uma porta SATA nativa para diagnóstico preditivo.",
        evidencia_observada=[],
        hipotese_provavel="Controladora USB sem pass-through ATA",
        limitacoes="Sem acesso a registradores SMART brutos",
        acao_sugerida="Conexão SATA direta"
    )

    app._render_smart_results(report)
    assert app.val_temp.cget("text") == "--"
    assert app.val_crc_err.cget("text") == "N/D"
    assert app.val_reallocated.cget("text") == "N/D"
    assert app.val_pending.cget("text") == "N/D"
    assert app.val_spin.cget("text") == "N/D"


def test_gui_render_clone_stats(mock_gui):
    app, root = mock_gui
    stats = ResilientCloneStats(
        current_sector=50_000,
        total_sectors=100_000,
        rescued_sectors=49_900,
        bad_sectors=100,
        skipped_sectors=0,
        speed_mb_s=125.4,
        phase="Fase 1: Cópia Sequencial Rápida"
    )
    app._render_clone_stats(stats)
    assert app.clone_progressbar["value"] == 50.0
    assert "Fase 1" in app.lbl_clone_phase.cget("text")
    assert "49,900" in app.lbl_clone_rescued.cget("text")
    assert "100" in app.lbl_clone_bad.cget("text")
    assert "125.4 MB/s" in app.lbl_clone_speed.cget("text")


def test_gui_render_surface_scan_block(mock_gui):
    app, root = mock_gui
    res = SurfaceBlockResult(lba=1024, count_sectors=256, latency_ms=12.5, status="fast")
    summary = SurfaceScanSummary(
        total_tested_blocks=1,
        fast_count=1,
        good_count=0,
        slow_count=0,
        very_slow_count=0,
        critical_delay_count=0,
        bad_count=0
    )
    app.block_canvas_idx = 0
    app._draw_surface_block(res, summary)
    assert "< 20ms: 1" in app.leg_fast.cget("text")
    assert app.block_canvas_idx == 1


def test_gui_render_scan_results_enables_boot_repair_when_corrupted_vbr_detected(mock_gui):
    """Item 4: Valida que a detecção de VBR NTFS corrompido habilita o botão de restauração."""
    from core.filesystem.ntfs_boot_repair import BootSectorDiagnostic

    app, root = mock_gui
    fake_disk = app.disks[0] if app.disks else None
    if not fake_disk:
        pytest.skip("Discos não carregados no mock")

    # Cenário 1: Boot primário corrompido, mas backup íntegro (can_repair=True)
    diag_repairable = BootSectorDiagnostic(
        primary_valid=False,
        backup_valid=True,
        can_repair=True,
        primary_lba=2048,
        backup_lba=10247
    )
    app._render_scan_results(None, None, [], fake_disk, ntfs_boot_diag=diag_repairable, ntfs_target_partition=(2048, 8200))
    assert str(app.btn_repair_boot.cget("state")) == "normal"
    assert "VBR CORROMPIDO" in app.lbl_repair_diagnosis.cget("text")

    # Cenário 2: Geometria consistente, sem corrupção (can_repair=False)
    diag_healthy = BootSectorDiagnostic(
        primary_valid=True,
        backup_valid=True,
        can_repair=False,
        primary_lba=2048,
        backup_lba=10247
    )
    app._render_scan_results(None, None, [], fake_disk, ntfs_boot_diag=diag_healthy, ntfs_target_partition=(2048, 8200))
    assert str(app.btn_repair_boot.cget("state")) == "disabled"


def test_gui_render_smart_metrics_usb_vs_internal_badge(mock_gui):
    """Item 7: Valida texto diferenciado de badge para USB vs conexão interna sem telemetria."""
    app, root = mock_gui

    # Relatório Unknown
    rep = SmartReport(
        device_id="0",
        device_path=r"\\.\PhysicalDrive0",
        model="Test Drive",
        serial=None,
        risk_level=SmartRiskLevel.UNKNOWN,
        temperature_c=None,
        reallocated_sectors=None,
        pending_sectors=None,
        uncorrectable_sectors=None,
        crc_errors=None,
        spin_retries=None,
        power_on_hours=None,
        attributes=[],
        windows_reliability={},
        summary_messages=[],
        action_recommendation="Conecte em porta nativa",
        evidencia_observada=[],
        hipotese_provavel="",
        limitacoes="",
        acao_sugerida=""
    )

    # Disco USB
    usb_disk = DiskInfo(
        number=2,
        device_path=r"\\.\PhysicalDrive2",
        friendly_name="USB Flash Drive",
        serial_number=None,
        size_bytes=16_000_000_000,
        bus_type="USB",
        partition_style="MBR",
        media_type="Removable",
        health_status="Unknown",
        operational_status="OK",
        temperature=None,
        read_errors=0,
        write_errors=0,
        wear=None,
        is_read_only=False,
        is_boot=False,
        is_system=False,
        can_open_raw=True
    )
    app._render_smart_results(rep, disk=usb_disk)
    assert "Interface USB" in app.dash_health_badge.cget("text")

    # Disco SATA interno
    sata_disk = DiskInfo(
        number=3,
        device_path=r"\\.\PhysicalDrive3",
        friendly_name="SATA Internal HDD",
        serial_number=None,
        size_bytes=500_000_000_000,
        bus_type="SATA",
        partition_style="GPT",
        media_type="HDD",
        health_status="Unknown",
        operational_status="OK",
        temperature=None,
        read_errors=0,
        write_errors=0,
        wear=None,
        is_read_only=False,
        is_boot=False,
        is_system=False,
        can_open_raw=True
    )
    app._render_smart_results(rep, disk=sata_disk)
    assert "S.M.A.R.T. Não Disponível / Indeterminada" in app.dash_health_badge.cget("text")


def test_gui_queue_poller_thread_safe_message_handlers(mock_gui):
    """Item 6: Valida que mensagens da fila restabelecem estados dos botões sem chamadas de thread secundária."""
    app, root = mock_gui

    # 1. Clone ended restabelece botão de clone
    app.btn_start_clone.config(state=tk.DISABLED)
    app.btn_cancel_clone.config(state=tk.NORMAL)
    app.msg_queue.put(("clone_ended", None))

    # 2. Carver ended restabelece botão de carver
    app.btn_start_carve.config(state=tk.DISABLED)
    app.msg_queue.put(("carver_ended", None))

    # 3. Surface ended restabelece botões de teste de superfície
    app.btn_start_surface.config(state=tk.DISABLED)
    app.btn_cancel_surface.config(state=tk.NORMAL)
    app.msg_queue.put(("surface_ended", None))

    # Processa mensagens enfileiradas através do poller da fila
    app._start_queue_poller()
    root.update()

    assert str(app.btn_start_clone.cget("state")) == "normal"
    assert str(app.btn_cancel_clone.cget("state")) == "disabled"
    assert str(app.btn_start_carve.cget("state")) == "normal"
    assert str(app.btn_start_surface.cget("state")) == "normal"
    assert str(app.btn_cancel_surface.cget("state")) == "disabled"
