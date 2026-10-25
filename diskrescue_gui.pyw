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
        from gui.app import DiskRescueApp

        root = tk.Tk()
        root.withdraw()
        app = DiskRescueApp(root)
        root.update()

        expected_views = ["dashboard", "diagnostic", "smart", "clone", "carver", "surface", "logs"]
        for view_name in expected_views:
            assert view_name in app.views, f"Visão obrigatória ausente: {view_name}"
            assert view_name in app.nav_buttons, f"Botão de navegação ausente: {view_name}"

        root.update()
        root.destroy()
        print(f"[SMOKE TEST] PASS: Interface gráfica e todos os {len(expected_views)} módulos carregados com sucesso.")
        sys.exit(0)
    else:
        from gui.app import run_gui
        run_gui()
