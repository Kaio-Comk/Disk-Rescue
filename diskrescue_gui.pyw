"""
diskrescue_gui.pyw — Launcher para a aplicação desktop do DiskRescue v0.3.2.
Ponto de entrada para execução direta e empacotamento PyInstaller.
Suporta flag '--smoke-test' para validação automatizada em CI e post-build.
"""
import sys

if __name__ == "__main__":
    if "--smoke-test" in sys.argv:
        print("[SMOKE TEST] Iniciando verificação de integridade do DiskRescue v0.3.2...")
        import tkinter as tk
        from pathlib import Path
        from gui.app import DiskRescueApp, AQUI
        import core.filesystem.ntfs_boot_repair  # noqa: F401
        import core.filesystem.scanner           # noqa: F401
        import core.hardware.device_resolver     # noqa: F401
        import core.hardware.scanner             # noqa: F401
        import core.hardware.smart               # noqa: F401
        import core.hardware.surface_scan        # noqa: F401
        import core.partition.gpt                # noqa: F401
        import core.partition.mbr                # noqa: F401
        import core.recovery.carver              # noqa: F401
        import core.recovery.repair              # noqa: F401
        import core.recovery.resilient_clone     # noqa: F401

        for asset_name in ("diskrescue.ico", "diskrescue.png"):
            asset_path = Path(AQUI) / "gui" / "assets" / asset_name
            assert asset_path.is_file(), f"Asset obrigatório ausente: {asset_path}"

        root = tk.Tk()
        root.withdraw()
        app = DiskRescueApp(root, auto_refresh=False)
        root.update()

        expected_views = ["dashboard", "diagnostic", "smart", "clone", "carver", "surface", "logs"]
        for view_name in expected_views:
            assert view_name in app.views, f"Visão obrigatória ausente: {view_name}"
            assert view_name in app.nav_buttons, f"Botão de navegação ausente: {view_name}"

        app._on_close_window()
        print(f"[SMOKE TEST] PASS: Interface gráfica, assets e todos os {len(expected_views)} módulos carregados sem acessar discos físicos.")
        sys.exit(0)
    else:
        from gui.app import run_gui
        run_gui()
