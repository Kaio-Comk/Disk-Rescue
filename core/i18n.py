"""
core/i18n.py — Sistema de Internacionalização (i18n) Multilíngue do DiskRescue.
Suporta detecção automática do idioma do sistema e troca dinâmica em tempo de execução.
Idiomas suportados:
  - pt_BR (Português do Brasil)
  - en_US (English)
  - es_ES (Español)
  - fr_FR (Français)
  - de_DE (Deutsch)
  - it_IT (Italiano)
  - ru_RU (Русский)
  - zh_CN (简体中文)
  - ja_JP (日本語)
"""

import locale
import os
import ctypes
from typing import Any, Optional

SUPPORTED_LANGUAGES = {
    "pt_BR": "Português (BR)",
    "en_US": "English (US)",
    "es_ES": "Español",
    "fr_FR": "Français",
    "de_DE": "Deutsch",
    "it_IT": "Italiano",
    "ru_RU": "Русский",
    "zh_CN": "简体中文",
    "ja_JP": "日本語",
}

TRANSLATIONS: dict[str, dict[str, str]] = {
    "pt_BR": {
        "app_title": "DiskRescue",
        "app_subtitle": "Diagnóstico Profundo em 1-Clique • S.M.A.R.T. ATA/NVMe • Clonagem Resiliente • Reparo GPT/NTFS",
        "version_badge": " v0.3.3 SUITE FORENSE ",
        "uac_admin": "MODO ADMINISTRADOR (ACESSO RAW ATIVO)",
        "uac_user": "MODO LEITURA (SEM UAC)",
        "btn_elevate": "Elevar UAC",
        "btn_refresh": "Atualizar Discos",
        "btn_theme_dark": "Tema: Escuro",
        "btn_theme_light": "Tema: Claro",
        "target_drive": "UNIDADE FÍSICA ALVO:",
        "btn_diag_all": "DIAGNOSTICAR TUDO (1-CLIQUE)",
        "raw_status": "Raw: {status}",
        "raw_accessible": "Acesso Físico Permitido",
        "raw_denied": "Acesso Bloqueado",
        "no_disk_selected": "Nenhum dispositivo selecionado. Aguardando enumeração...",
        "language_label": "Idioma:",
        
        # Sidebar
        "nav_section": "PAINEL DE NAVEGAÇÃO",
        "nav_dashboard": "Central & Diagnóstico 1-Clique",
        "nav_diagnostic": "Partições, GPT & Boot NTFS",
        "nav_smart": "Sensores S.M.A.R.T. & Cabos",
        "nav_clone": "Clonagem Resiliente (.img)",
        "nav_carver": "File Carving Forense",
        "nav_surface": "Mapa de Calor & Latência",
        "nav_logs": "Console de Auditoria",
        "mini_drive_title": "DISCO EM FOCO",
        
        # Dashboard
        "dash_hero_title": "DIAGNÓSTICO PROFUNDO & SAÚDE DO DISCO (1-CLIQUE)",
        "dash_hero_badge": "ANÁLISE INTEGRADA",
        "dash_waiting": "Aguardando Análise...",
        "dash_select_prompt": "Selecione um disco para carregar o resumo de confiabilidade.",
        "dash_recommendation_default": "Clique em 'EXECUTAR DIAGNÓSTICO COMPLETO (1-CLIQUE)' para testar sensores S.M.A.R.T., cabos SATA, partições GPT/MBR, Boot Sector NTFS e varrer de 0% a 100% da superfície física do HD.",
        "dash_depth": "Profundidade:",
        "mode_deep": "Profundo: Varrer Extensão Total 0% a 100% (512 Zonas Distribuídas)",
        "mode_full": "Completo: Ler 100% de Todos os Setores do HD (Modo Victoria)",
        "mode_quick": "Rápido: S.M.A.R.T. + Partições + Boot/MFT + Início (128 MB)",
        "dash_telemetry_waiting": "Posição Física: Aguardando início (0.0% concluído)",
        "btn_hero_scan": "EXECUTAR DIAGNÓSTICO COMPLETO (1-CLIQUE)",
        "btn_stop_diag": "⏹ Parar / Concluir Laudo",
        "matrix_title": "MATRIZ DE DETECÇÃO DE FALHAS (6 CAMADAS DE HARDWARE & LÓGICA)",
        "matrix_badge": "TEMPO REAL",
        
        "tile_smart_title": "1. Setores S.M.A.R.T.",
        "tile_smart_desc": "Reallocated (0x05) & Pending (0xC5)",
        "tile_cable_title": "2. Cabo SATA & Bus",
        "tile_cable_desc": "Erros elétricos CRC UltraDMA (0xC7)",
        "tile_thermal_title": "3. Térmico & Motor",
        "tile_thermal_desc": "Temperatura (°C), Spin & Desgaste",
        "tile_partition_title": "4. Tabela GPT / MBR",
        "tile_partition_desc": "Assinatura 0x55AA, CRC32 & Alinhamento",
        "tile_boot_title": "5. Boot Sector & $MFT",
        "tile_boot_desc": "VBR Primário vs Backup & NTFS $MFT",
        "tile_surface_title": "6. Leitura de Superfície",
        "tile_surface_desc": "Teste físico de Bad Sectors & Latência",
        "status_pending": "PENDENTE",
        
        # Diagnostic & Partitions
        "card_registered_table": "1. TABELA REGISTRADA NO DISCO (GPT / MBR)",
        "card_raw_media": "2. MÍDIA FÍSICA REAL (BOOT SECTORS & MFTs)",
        "btn_run_geometry_scan": "Iniciar Varredura de Geometria & Boot Sectors",
        "card_surgical_repair": "3. AÇÕES CIRÚRGICAS DE RESOLUÇÃO COM ROLLBACK VERIFICADO",
        "btn_restore_boot": "Restaurar Boot Sector via Backup",
        "btn_realign_gpt": "Realinhar Offset de Partição GPT",
        
        # SMART
        "smart_title": "Telemetria Avançada S.M.A.R.T. & Sensores ATA/NVMe",
        "smart_subtitle": "Isolamento de problemas de cabo SATA, temperatura, setores pendentes e integridade mecânica.",
        "smart_card_sectors": "Setores Problemáticos",
        "smart_card_cable": "Ruído no Cabo SATA (CRC)",
        "smart_card_temp": "Temperatura Operacional",
        "smart_card_power": "Tempo de Vida & Ciclos",
        "smart_card_mech": "Motor & Mecânica",
        "smart_card_nvme": "Mídia & Desgaste NVMe",
        
        # Clone
        "clone_title": "Clonagem Resiliente Byte-a-Byte (Modo ddrescue)",
        "clone_btn_start": "Iniciar Clonagem Resiliente",
        "clone_btn_cancel": "Interromper Clonagem",
        "clone_lbl_dest": "Destino (.img):",
        "clone_btn_browse": "Selecionar...",
        
        # Carver
        "carver_title": "File Carving Forense (Resgate Direto por Assinatura)",
        "carver_btn_start": "Iniciar File Carving",
        "carver_btn_cancel": "Interromper Carving",
        "carver_col_id": "ID",
        "carver_col_type": "Tipo",
        "carver_col_lba": "LBA Inicial",
        "carver_col_size": "Tamanho",
        "carver_col_path": "Arquivo Salvo",
        
        # Surface
        "surface_title": "Varredura de Superfície & Heatmap de Latência de Setor",
        "surface_subtitle": "Mede o tempo de resposta físico bloco a bloco (estilo Victoria / MHDD) para identificar setores degradados.",
        "surface_btn_start": "Iniciar Teste de Superfície",
        "surface_btn_cancel": "⏹ Interromper",
        
        # Logs
        "logs_title": "Console de Auditoria Forense & Registro de Eventos",
        "logs_btn_clear": "Limpar Log",
        "logs_btn_export": "Exportar Relatório (.txt)",
        
        # Status Bar
        "status_ready": "Pronto.",
    },
    
    "en_US": {
        "app_title": "DiskRescue",
        "app_subtitle": "1-Click Deep Diagnostics • ATA/NVMe S.M.A.R.T. • Resilient Cloning • GPT/NTFS Repair",
        "version_badge": " v0.3.3 FORENSIC SUITE ",
        "uac_admin": "ADMINISTRATOR MODE (RAW ACCESS ACTIVE)",
        "uac_user": "READ-ONLY MODE (NO UAC)",
        "btn_elevate": "Elevate UAC",
        "btn_refresh": "Refresh Disks",
        "btn_theme_dark": "Theme: Dark",
        "btn_theme_light": "Theme: Light",
        "target_drive": "TARGET PHYSICAL DRIVE:",
        "btn_diag_all": "DIAGNOSE ALL (1-CLICK)",
        "raw_status": "Raw: {status}",
        "raw_accessible": "Physical Access Allowed",
        "raw_denied": "Access Denied",
        "no_disk_selected": "No device selected. Waiting for enumeration...",
        "language_label": "Language:",
        
        # Sidebar
        "nav_section": "NAVIGATION PANEL",
        "nav_dashboard": "Dashboard & 1-Click Diag",
        "nav_diagnostic": "Partitions, GPT & Boot NTFS",
        "nav_smart": "S.M.A.R.T. & Cable Sensors",
        "nav_clone": "Resilient Cloning (.img)",
        "nav_carver": "Forensic File Carving",
        "nav_surface": "Heatmap & Surface Latency",
        "nav_logs": "Audit Console",
        "mini_drive_title": "SELECTED DRIVE",
        
        # Dashboard
        "dash_hero_title": "DEEP DRIVE DIAGNOSTICS & RELIABILITY (1-CLICK)",
        "dash_hero_badge": "INTEGRATED AUDIT",
        "dash_waiting": "Awaiting Analysis...",
        "dash_select_prompt": "Select a drive to load reliability summary.",
        "dash_recommendation_default": "Click 'RUN COMPLETE DIAGNOSTIC (1-CLICK)' to test S.M.A.R.T. sensors, SATA cables, GPT/MBR tables, NTFS Boot Sector, and scan 0% to 100% of the drive surface.",
        "dash_depth": "Scan Depth:",
        "mode_deep": "Deep: Span Full 0% to 100% Drive Extent (512 Distributed Zones)",
        "mode_full": "Complete: Read 100% of All Disk Sectors (Victoria Mode)",
        "mode_quick": "Quick: S.M.A.R.T. + Partitions + Boot/MFT + Drive Start (128 MB)",
        "dash_telemetry_waiting": "Physical Position: Waiting to start (0.0% completed)",
        "btn_hero_scan": "RUN COMPLETE DIAGNOSTIC (1-CLICK)",
        "btn_stop_diag": "⏹ Stop / Finalize Report",
        "matrix_title": "FAULT DETECTION MATRIX (6 HARDWARE & LOGICAL LAYERS)",
        "matrix_badge": "REAL TIME",
        
        "tile_smart_title": "1. S.M.A.R.T. Sectors",
        "tile_smart_desc": "Reallocated (0x05) & Pending (0xC5)",
        "tile_cable_title": "2. SATA Cable & Bus",
        "tile_cable_desc": "UltraDMA CRC electrical errors (0xC7)",
        "tile_thermal_title": "3. Thermal & Motor",
        "tile_thermal_desc": "Temperature (°C), Spin & Wear",
        "tile_partition_title": "4. GPT / MBR Table",
        "tile_partition_desc": "0x55AA signature, CRC32 & Alignment",
        "tile_boot_title": "5. Boot Sector & $MFT",
        "tile_boot_desc": "Primary vs Backup VBR & NTFS $MFT",
        "tile_surface_title": "6. Surface Read I/O",
        "tile_surface_desc": "Physical Bad Sectors & Latency test",
        "status_pending": "PENDING",
        
        # Diagnostic & Partitions
        "card_registered_table": "1. REGISTERED DISK TABLE (GPT / MBR)",
        "card_raw_media": "2. ACTUAL PHYSICAL MEDIA (BOOT SECTORS & MFTs)",
        "btn_run_geometry_scan": "Run Geometry & Boot Sector Scan",
        "card_surgical_repair": "3. SURGICAL REPAIRS WITH VERIFIED ROLLBACK",
        "btn_restore_boot": "Restore Boot Sector from Backup",
        "btn_realign_gpt": "Realign GPT Partition Offset",
        
        # SMART
        "smart_title": "Advanced S.M.A.R.T. Telemetry & ATA/NVMe Sensors",
        "smart_subtitle": "Isolating SATA cable issues, temperature, pending sectors, and mechanical integrity.",
        "smart_card_sectors": "Problematic Sectors",
        "smart_card_cable": "SATA Cable Noise (CRC)",
        "smart_card_temp": "Operating Temperature",
        "smart_card_power": "Power-On & Cycle Count",
        "smart_card_mech": "Motor & Mechanics",
        "smart_card_nvme": "NVMe Media & Wear",
        
        # Clone
        "clone_title": "Resilient Sector-by-Sector Cloning (ddrescue style)",
        "clone_btn_start": "Start Resilient Clone",
        "clone_btn_cancel": "Abort Clone",
        "clone_lbl_dest": "Destination (.img):",
        "clone_btn_browse": "Browse...",
        
        # Carver
        "carver_title": "Forensic File Carving (Direct Signature Extraction)",
        "carver_btn_start": "Start File Carving",
        "carver_btn_cancel": "Abort Carving",
        "carver_col_id": "ID",
        "carver_col_type": "Type",
        "carver_col_lba": "Start LBA",
        "carver_col_size": "Size",
        "carver_col_path": "Saved File",
        
        # Surface
        "surface_title": "Surface Scan & Sector Latency Heatmap",
        "surface_subtitle": "Measures physical block-by-block response latency (Victoria / MHDD style) to detect degraded sectors.",
        "surface_btn_start": "Start Surface Scan",
        "surface_btn_cancel": "⏹ Stop",
        
        # Logs
        "logs_title": "Forensic Audit Console & Event Log",
        "logs_btn_clear": "Clear Log",
        "logs_btn_export": "Export Report (.txt)",
        
        # Status Bar
        "status_ready": "Ready.",
    },
    
    "es_ES": {
        "app_title": "DiskRescue",
        "app_subtitle": "Diagnóstico Profundo en 1-Clic • S.M.A.R.T. ATA/NVMe • Clonación Resiliente • Reparación GPT/NTFS",
        "version_badge": " v0.3.3 SUITE FORENSE ",
        "uac_admin": "MODO ADMINISTRADOR (ACCESO RAW ACTIVO)",
        "uac_user": "MODO SOLO LECTURA (SIN UAC)",
        "btn_elevate": "Elevar UAC",
        "btn_refresh": "Actualizar Discos",
        "btn_theme_dark": "Tema: Oscuro",
        "btn_theme_light": "Tema: Claro",
        "target_drive": "UNIDAD FÍSICA OBJETIVO:",
        "btn_diag_all": "DIAGNOSTICAR TODO (1-CLIC)",
        "raw_status": "Raw: {status}",
        "raw_accessible": "Acceso Físico Permitido",
        "raw_denied": "Acceso Bloqueado",
        "no_disk_selected": "Ningún dispositivo seleccionado. Esperando enumeración...",
        "language_label": "Idioma:",
        
        # Sidebar
        "nav_section": "PANEL DE NAVEGACIÓN",
        "nav_dashboard": "Panel & Diagnóstico 1-Clic",
        "nav_diagnostic": "Particiones, GPT & Boot NTFS",
        "nav_smart": "Sensores S.M.A.R.T. & Cables",
        "nav_clone": "Clonación Resiliente (.img)",
        "nav_carver": "File Carving Forense",
        "nav_surface": "Mapa de Calor & Latencia",
        "nav_logs": "Consola de Auditoría",
        "mini_drive_title": "DISCO SELECCIONADO",
        
        # Dashboard
        "dash_hero_title": "DIAGNÓSTICO PROFUNDO & SALUD DEL DISCO (1-CLIC)",
        "dash_hero_badge": "ANÁLISIS INTEGRADO",
        "dash_waiting": "Esperando Análisis...",
        "dash_select_prompt": "Seleccione un disco para cargar el resumen de confiabilidad.",
        "dash_recommendation_default": "Haga clic en 'EJECUTAR DIAGNÓSTICO COMPLETO (1-CLIC)' para probar sensores S.M.A.R.T., cables SATA, tablas GPT/MBR, Boot Sector NTFS y escanear del 0% al 100% de la superficie física.",
        "dash_depth": "Profundidad:",
        "mode_deep": "Profundo: Barrer Extensión Total 0% a 100% (512 Zonas Distribuidas)",
        "mode_full": "Completo: Leer el 100% de Todos los Sectores del Disco (Modo Victoria)",
        "mode_quick": "Rápido: S.M.A.R.T. + Particiones + Boot/MFT + Inicio (128 MB)",
        "dash_telemetry_waiting": "Posición Física: Esperando inicio (0.0% completado)",
        "btn_hero_scan": "EJECUTAR DIAGNÓSTICO COMPLETO (1-CLIC)",
        "btn_stop_diag": "⏹ Detener / Concluir Informe",
        "matrix_title": "MATRIZ DE DETECCIÓN DE FALLOS (6 CAPAS DE HARDWARE Y LÓGICA)",
        "matrix_badge": "TIEMPO REAL",
        
        "tile_smart_title": "1. Sectores S.M.A.R.T.",
        "tile_smart_desc": "Reallocated (0x05) & Pending (0xC5)",
        "tile_cable_title": "2. Cable SATA & Bus",
        "tile_cable_desc": "Errores eléctricos CRC UltraDMA (0xC7)",
        "tile_thermal_title": "3. Térmico & Motor",
        "tile_thermal_desc": "Temperatura (°C), Spin y Desgaste",
        "tile_partition_title": "4. Tabla GPT / MBR",
        "tile_partition_desc": "Firma 0x55AA, CRC32 y Alineación",
        "tile_boot_title": "5. Boot Sector & $MFT",
        "tile_boot_desc": "VBR Primario vs Backup & NTFS $MFT",
        "tile_surface_title": "6. Lectura de Superficie",
        "tile_surface_desc": "Prueba física de Bad Sectors y Latencia",
        "status_pending": "PENDIENTE",
        
        # Diagnostic & Partitions
        "card_registered_table": "1. TABLA REGISTRADA EN EL DISCO (GPT / MBR)",
        "card_raw_media": "2. MEDIO FÍSICO REAL (BOOT SECTORS & MFTs)",
        "btn_run_geometry_scan": "Iniciar Escaneo de Geometría y Boot Sectors",
        "card_surgical_repair": "3. ACCIONES QUIRÚRGICAS CON ROLLBACK VERIFICADO",
        "btn_restore_boot": "Restaurar Boot Sector mediante Backup",
        "btn_realign_gpt": "Realinear Offset de Partición GPT",
        
        # SMART
        "smart_title": "Telemetría Avanzada S.M.A.R.T. & Sensores ATA/NVMe",
        "smart_subtitle": "Aislamiento de problemas de cable SATA, temperatura, sectores pendientes e integridad mecánica.",
        "smart_card_sectors": "Sectores Problemáticos",
        "smart_card_cable": "Ruido en Cable SATA (CRC)",
        "smart_card_temp": "Temperatura Operativa",
        "smart_card_power": "Tiempo de Vida y Ciclos",
        "smart_card_mech": "Motor y Mecánica",
        "smart_card_nvme": "Medio y Desgaste NVMe",
        
        # Clone
        "clone_title": "Clonación Resiliente Byte a Byte (Modo ddrescue)",
        "clone_btn_start": "Iniciar Clonación Resiliente",
        "clone_btn_cancel": "Interrumpir Clonación",
        "clone_lbl_dest": "Destino (.img):",
        "clone_btn_browse": "Examinar...",
        
        # Carver
        "carver_title": "File Carving Forense (Rescate Directo por Firma)",
        "carver_btn_start": "Iniciar File Carving",
        "carver_btn_cancel": "Interrumpir Carving",
        "carver_col_id": "ID",
        "carver_col_type": "Tipo",
        "carver_col_lba": "LBA Inicial",
        "carver_col_size": "Tamaño",
        "carver_col_path": "Archivo Guardado",
        
        # Surface
        "surface_title": "Escaneo de Superficie & Mapa de Calor de Latencia",
        "surface_subtitle": "Mide el tiempo de respuesta físico bloque a bloque (estilo Victoria / MHDD) para detectar sectores degradados.",
        "surface_btn_start": "Iniciar Prueba de Superficie",
        "surface_btn_cancel": "⏹ Detener",
        
        # Logs
        "logs_title": "Consola de Auditoría Forense y Registro de Eventos",
        "logs_btn_clear": "Limpiar Registro",
        "logs_btn_export": "Exportar Informe (.txt)",
        
        # Status Bar
        "status_ready": "Listo.",
    },
    
    "fr_FR": {
        "app_title": "DiskRescue",
        "app_subtitle": "Diagnostic Approfondi en 1 Clic • S.M.A.R.T. ATA/NVMe • Clonage Résilient • Réparation GPT/NTFS",
        "version_badge": " v0.3.3 SUITE FORENSIQUE ",
        "uac_admin": "MODE ADMINISTRATEUR (ACCÈS RAW ACTIF)",
        "uac_user": "MODE LECTURE SEULE (SANS UAC)",
        "btn_elevate": "Élever UAC",
        "btn_refresh": "Actualiser les Disques",
        "btn_theme_dark": "Thème : Sombre",
        "btn_theme_light": "Thème : Clair",
        "target_drive": "LECTEUR PHYSIQUE CIBLE :",
        "btn_diag_all": "TOUT DIAGNOSTIQUER (1-CLIC)",
        "raw_status": "Raw : {status}",
        "raw_accessible": "Accès Physique Autorisé",
        "raw_denied": "Accès Refusé",
        "no_disk_selected": "Aucun périphérique sélectionné. En attente d'énumération...",
        "language_label": "Langue :",
        
        "nav_section": "PANNEAU DE NAVIGATION",
        "nav_dashboard": "Tableau de Bord & Diag 1-Clic",
        "nav_diagnostic": "Partitions, GPT & Boot NTFS",
        "nav_smart": "Capteurs S.M.A.R.T. & Câbles",
        "nav_clone": "Clonage Résilient (.img)",
        "nav_carver": "File Carving Forensique",
        "nav_surface": "Carte Thermique & Latence",
        "nav_logs": "Console d'Audit",
        "mini_drive_title": "DISQUE EN VUE",
        
        "dash_hero_title": "DIAGNOSTIC APPROFONDI & FIABILITÉ DU DISQUE (1-CLIC)",
        "dash_hero_badge": "ANALYSE INTÉGRÉE",
        "dash_waiting": "En attente d'analyse...",
        "dash_select_prompt": "Sélectionnez un disque pour charger le rapport de fiabilité.",
        "dash_recommendation_default": "Cliquez sur 'DIAGNOSTIC COMPLET (1-CLIC)' pour tester S.M.A.R.T., câbles SATA, tables GPT/MBR, Boot Sector NTFS et scanner de 0% à 100% de la surface physique.",
        "dash_depth": "Profondeur :",
        "mode_deep": "Profond : Balayer Toute l'Étendue 0% à 100% (512 Zones Réparties)",
        "mode_full": "Complet : Lire 100% des Secteurs du Disque (Mode Victoria)",
        "mode_quick": "Rapide : S.M.A.R.T. + Partitions + Boot/MFT + Début (128 Mo)",
        "dash_telemetry_waiting": "Position Physique : En attente (0.0% complété)",
        "btn_hero_scan": "LANCER LE DIAGNOSTIC COMPLET (1-CLIC)",
        "btn_stop_diag": "⏹ Arrêter / Conclure le Rapport",
        "matrix_title": "MATRICE DE DÉTECTION DES DÉFAUTS (6 COUCHES MATÉRIELLES ET LOGIQUES)",
        "matrix_badge": "TEMPS RÉEL",
        
        "tile_smart_title": "1. Secteurs S.M.A.R.T.",
        "tile_smart_desc": "Réalloués (0x05) & En attente (0xC5)",
        "tile_cable_title": "2. Câble SATA & Bus",
        "tile_cable_desc": "Erreurs électriques CRC UltraDMA (0xC7)",
        "tile_thermal_title": "3. Thermique & Moteur",
        "tile_thermal_desc": "Température (°C), Rotation & Usure",
        "tile_partition_title": "4. Table GPT / MBR",
        "tile_partition_desc": "Signature 0x55AA, CRC32 & Alignement",
        "tile_boot_title": "5. Boot Sector & $MFT",
        "tile_boot_desc": "VBR Primaire vs Sauvegarde & NTFS $MFT",
        "tile_surface_title": "6. Lecture de Surface",
        "tile_surface_desc": "Test physique de Bad Sectors & Latence",
        "status_pending": "EN ATTENTE",
        
        "card_registered_table": "1. TABLE ENREGISTRÉE SUR LE DISQUE (GPT / MBR)",
        "card_raw_media": "2. MÉDIA PHYSIQUE RÉEL (BOOT SECTORS & MFTs)",
        "btn_run_geometry_scan": "Lancer l'Analyse Géométrique & Boot Sectors",
        "card_surgical_repair": "3. ACTIONS CHIRURGICALES AVEC ROLLBACK VÉRIFIÉ",
        "btn_restore_boot": "Restaurer le Boot Sector via Sauvegarde",
        "btn_realign_gpt": "Réaligner l'Offset de Partition GPT",
        
        "smart_title": "Télémétrie Avancée S.M.A.R.T. & Capteurs ATA/NVMe",
        "smart_subtitle": "Isolation des problèmes de câble SATA, température, secteurs en attente et mécanique.",
        "smart_card_sectors": "Secteurs Problématiques",
        "smart_card_cable": "Bruit de Câble SATA (CRC)",
        "smart_card_temp": "Température de Fonctionnement",
        "smart_card_power": "Durée de Vie & Cycles",
        "smart_card_mech": "Moteur & Mécanique",
        "smart_card_nvme": "Support & Usure NVMe",
        
        "clone_title": "Clonage Résilient Secteur par Secteur (Mode ddrescue)",
        "clone_btn_start": "Démarrer le Clonage Résilient",
        "clone_btn_cancel": "Interrompre le Clonage",
        "clone_lbl_dest": "Destination (.img) :",
        "clone_btn_browse": "Parcourir...",
        
        "carver_title": "File Carving Forensique (Extraction Directe par Signature)",
        "carver_btn_start": "Démarrer le File Carving",
        "carver_btn_cancel": "Interrompre le Carving",
        "carver_col_id": "ID",
        "carver_col_type": "Type",
        "carver_col_lba": "LBA Début",
        "carver_col_size": "Taille",
        "carver_col_path": "Fichier Sauvegardé",
        
        "surface_title": "Balayage de Surface & Carte Thermique de Latence",
        "surface_subtitle": "Mesure la latence bloc par bloc (style Victoria / MHDD) pour détecter les secteurs dégradés.",
        "surface_btn_start": "Démarrer le Test de Surface",
        "surface_btn_cancel": "⏹ Arrêter",
        
        "logs_title": "Console d'Audit Forensique & Journal des Événements",
        "logs_btn_clear": "Effacer le Journal",
        "logs_btn_export": "Exporter le Rapport (.txt)",
        "status_ready": "Prêt.",
    },

    "de_DE": {
        "app_title": "DiskRescue",
        "app_subtitle": "1-Klick Tiefendiagnose • ATA/NVMe S.M.A.R.T. • Resiliente Klonung • GPT/NTFS Reparatur",
        "version_badge": " v0.3.3 FORENSIK SUITE ",
        "uac_admin": "ADMINISTRATOR-MODUS (RAW-ZUGRIFF AKTIV)",
        "uac_user": "NUR-LESEN-MODUS (KEIN UAC)",
        "btn_elevate": "UAC Erhöhen",
        "btn_refresh": "Festplatten Aktualisieren",
        "btn_theme_dark": "Design: Dunkel",
        "btn_theme_light": "Design: Hell",
        "target_drive": "ZIEL-FESTPLATTE:",
        "btn_diag_all": "ALLES DIAGNOSTIZIEREN (1-KLICK)",
        "raw_status": "Raw: {status}",
        "raw_accessible": "Physischer Zugriff Erlaubt",
        "raw_denied": "Zugriff Verweigert",
        "no_disk_selected": "Kein Gerät ausgewählt. Warte auf Aufzählung...",
        "language_label": "Sprache:",
        
        "nav_section": "NAVIGATIONSPANEL",
        "nav_dashboard": "Übersicht & 1-Klick-Diag",
        "nav_diagnostic": "Partitionen, GPT & Boot NTFS",
        "nav_smart": "S.M.A.R.T. & Kabelsensoren",
        "nav_clone": "Resilientes Klonen (.img)",
        "nav_carver": "Forensisches File Carving",
        "nav_surface": "Heatmap & Oberflächenlatenz",
        "nav_logs": "Audit-Konsole",
        "mini_drive_title": "GEWÄHLTE FESTPLATTE",
        
        "dash_hero_title": "FESTPLATTEN-TIEFENDIAGNOSE & GESUNDHEIT (1-KLICK)",
        "dash_hero_badge": "INTEGRIERTE ANALYSE",
        "dash_waiting": "Warte auf Analyse...",
        "dash_select_prompt": "Wählen Sie eine Festplatte aus, um die Zuverlässigkeit zu laden.",
        "dash_recommendation_default": "Klicken Sie auf 'VOLLSTÄNDIGE DIAGNOSE (1-KLICK)', um S.M.A.R.T., SATA-Kabel, GPT/MBR, Boot Sector NTFS und 0% bis 100% der Oberfläche zu prüfen.",
        "dash_depth": "Scan-Tiefe:",
        "mode_deep": "Tief: Gesamte Festplatte 0% bis 100% scannen (512 verteilte Zonen)",
        "mode_full": "Vollständig: 100% aller Sektoren lesen (Victoria-Modus)",
        "mode_quick": "Schnell: S.M.A.R.T. + Partitionen + Boot/MFT + Anfang (128 MB)",
        "dash_telemetry_waiting": "Physische Position: Warte auf Start (0.0% abgeschlossen)",
        "btn_hero_scan": "VOLLSTÄNDIGE DIAGNOSE STARTEN (1-KLICK)",
        "btn_stop_diag": "⏹ Stoppen / Bericht Abschließen",
        "matrix_title": "FEHLERERKENNUNGSMATRIX (6 HARDWARE- & LOGIKSCHICHTEN)",
        "matrix_badge": "ECHTZEIT",
        
        "tile_smart_title": "1. S.M.A.R.T.-Sektoren",
        "tile_smart_desc": "Neu zugewiesen (0x05) & Ausstehend (0xC5)",
        "tile_cable_title": "2. SATA-Kabel & Bus",
        "tile_cable_desc": "UltraDMA CRC-Übertragungsfehler (0xC7)",
        "tile_thermal_title": "3. Thermik & Motor",
        "tile_thermal_desc": "Temperatur (°C), Spin & Verschleiß",
        "tile_partition_title": "4. GPT / MBR-Tabelle",
        "tile_partition_desc": "0x55AA Signatur, CRC32 & Ausrichtung",
        "tile_boot_title": "5. Boot Sector & $MFT",
        "tile_boot_desc": "Primärer vs. Backup-VBR & NTFS $MFT",
        "tile_surface_title": "6. Oberflächenlesung",
        "tile_surface_desc": "Defekte Sektoren & Latenzprüfung",
        "status_pending": "AUSSTEHEND",
        
        "card_registered_table": "1. REGISTRIERTE TABELLE (GPT / MBR)",
        "card_raw_media": "2. PHYSISCHES MEDIUM (BOOT SECTORS & MFTs)",
        "btn_run_geometry_scan": "Geometrie- & Boot-Sektor-Scan Starten",
        "card_surgical_repair": "3. CHIRURGISCHE REPARATUREN MIT ROLLBACK",
        "btn_restore_boot": "Boot-Sektor aus Backup Wiederherstellen",
        "btn_realign_gpt": "GPT-Partitionsversatz Neu Ausrichten",
        
        "smart_title": "Erweiterte S.M.A.R.T.-Telemetrie & Sensoren",
        "smart_subtitle": "Isolierung von SATA-Kabelproblemen, Temperatur, ausstehenden Sektoren und Mechanik.",
        "smart_card_sectors": "Problematische Sektoren",
        "smart_card_cable": "SATA-Kabelrauschen (CRC)",
        "smart_card_temp": "Betriebstemperatur",
        "smart_card_power": "Betriebsstunden & Zyklen",
        "smart_card_mech": "Motor & Mechanik",
        "smart_card_nvme": "NVMe-Medium & Verschleiß",
        
        "clone_title": "Resilientes Sektor-für-Sektor Klonen (ddrescue-Stil)",
        "clone_btn_start": "Resilientes Klonen Starten",
        "clone_btn_cancel": "Klonen Abbrechen",
        "clone_lbl_dest": "Ziel (.img):",
        "clone_btn_browse": "Durchsuchen...",
        
        "carver_title": "Forensisches File Carving (Direkte Signatur-Wiederherstellung)",
        "carver_btn_start": "File Carving Starten",
        "carver_btn_cancel": "Carving Abbrechen",
        "carver_col_id": "ID",
        "carver_col_type": "Typ",
        "carver_col_lba": "Start-LBA",
        "carver_col_size": "Größe",
        "carver_col_path": "Gespeicherte Datei",
        
        "surface_title": "Oberflächen-Scan & Sektorlatenz-Heatmap",
        "surface_subtitle": "Misst physische Blocklatenzen (Victoria / MHDD Stil), um defekte Sektoren frühzeitig zu erkennen.",
        "surface_btn_start": "Oberflächen-Scan Starten",
        "surface_btn_cancel": "⏹ Stoppen",
        
        "logs_title": "Forensische Audit-Konsole & Ereignisprotokoll",
        "logs_btn_clear": "Protokoll Löschen",
        "logs_btn_export": "Bericht Exportieren (.txt)",
        "status_ready": "Bereit.",
    },

    "it_IT": {
        "app_title": "DiskRescue",
        "app_subtitle": "Diagnostica Approfondita in 1 Clic • S.M.A.R.T. ATA/NVMe • Clonazione Resiliente • Riparazione GPT/NTFS",
        "version_badge": " v0.3.3 SUITE FORENSE ",
        "uac_admin": "MODALITÀ AMMINISTRATORE (ACCESSO RAW ATTIVO)",
        "uac_user": "MODALITÀ SOLA LETTURA (SENZA UAC)",
        "btn_elevate": "Eleva UAC",
        "btn_refresh": "Aggiorna Dischi",
        "btn_theme_dark": "Tema: Scuro",
        "btn_theme_light": "Tema: Chiaro",
        "target_drive": "UNITÀ FISICA OBIETTIVO:",
        "btn_diag_all": "DIAGNOSTICA TUTTO (1-CLIC)",
        "raw_status": "Raw: {status}",
        "raw_accessible": "Accesso Fisico Consentito",
        "raw_denied": "Accesso Negato",
        "no_disk_selected": "Nessun dispositivo selezionato. In attesa di enumerazione...",
        "language_label": "Lingua:",
        
        "nav_section": "PANNELLO DI NAVIGAZIONE",
        "nav_dashboard": "Pannello & Diagnosi 1-Clic",
        "nav_diagnostic": "Partizioni, GPT & Boot NTFS",
        "nav_smart": "Sensori S.M.A.R.T. & Cavi",
        "nav_clone": "Clonazione Resiliente (.img)",
        "nav_carver": "File Carving Forense",
        "nav_surface": "Mappa Termica & Latenza",
        "nav_logs": "Console di Audit",
        "mini_drive_title": "DISCO SELEZIONATO",
        
        "dash_hero_title": "DIAGNOSI APPROFONDITA & SALUTE DEL DISCO (1-CLIC)",
        "dash_hero_badge": "ANALISI INTEGRATA",
        "dash_waiting": "In attesa di analisi...",
        "dash_select_prompt": "Seleziona un disco per caricare il riepilogo di affidabilità.",
        "dash_recommendation_default": "Fai clic su 'ESEGUI DIAGNOSI COMPLETA (1-CLIC)' per verificare sensori S.M.A.R.T., cavi SATA, tabelle GPT/MBR, Boot Sector NTFS e scansionare dallo 0% al 100% della superficie fisica.",
        "dash_depth": "Profondità:",
        "mode_deep": "Profonda: Scansiona Intera Estensione 0% a 100% (512 Zone Distribuite)",
        "mode_full": "Completa: Leggi 100% di Tutti i Settori (Modalità Victoria)",
        "mode_quick": "Rapida: S.M.A.R.T. + Partizioni + Boot/MFT + Inizio (128 MB)",
        "dash_telemetry_waiting": "Posizione Fisica: In attesa di avvio (0.0% completato)",
        "btn_hero_scan": "ESEGUI DIAGNOSI COMPLETA (1-CLIC)",
        "btn_stop_diag": "⏹ Ferma / Concludi Rapporto",
        "matrix_title": "MATRICE DI RILEVAMENTO GUASTI (6 LIVELLI HARDWARE E LOGICI)",
        "matrix_badge": "TEMPO REALE",
        
        "tile_smart_title": "1. Settori S.M.A.R.T.",
        "tile_smart_desc": "Riallocati (0x05) & Pendenti (0xC5)",
        "tile_cable_title": "2. Cavo SATA & Bus",
        "tile_cable_desc": "Errori elettrici CRC UltraDMA (0xC7)",
        "tile_thermal_title": "3. Termico & Motore",
        "tile_thermal_desc": "Temperatura (°C), Rotazione & Usura",
        "tile_partition_title": "4. Tabella GPT / MBR",
        "tile_partition_desc": "Firma 0x55AA, CRC32 & Allineamento",
        "tile_boot_title": "5. Boot Sector & $MFT",
        "tile_boot_desc": "VBR Primario vs Backup & NTFS $MFT",
        "tile_surface_title": "6. Lettura di Superficie",
        "tile_surface_desc": "Test fisico di Bad Sectors & Latenza",
        "status_pending": "IN ATTESA",
        
        "card_registered_table": "1. TABELLA REGISTRATA SUL DISCO (GPT / MBR)",
        "card_raw_media": "2. SUPPORTO FISICO REALE (BOOT SECTORS & MFTs)",
        "btn_run_geometry_scan": "Avvia Scansione Geometria & Boot Sectors",
        "card_surgical_repair": "3. AZIONI CHIRURGICHE CON ROLLBACK VERIFICATO",
        "btn_restore_boot": "Ripristina Boot Sector da Backup",
        "btn_realign_gpt": "Riallinea Offset Partizione GPT",
        
        "smart_title": "Telemetria Avanzata S.M.A.R.T. & Sensori ATA/NVMe",
        "smart_subtitle": "Isolamento problemi di cavo SATA, temperatura, settori pendenti e integrità meccanica.",
        "smart_card_sectors": "Settori Problematici",
        "smart_card_cable": "Rumore Cavo SATA (CRC)",
        "smart_card_temp": "Temperatura di Esercizio",
        "smart_card_power": "Tempo di Funzionamento & Cicli",
        "smart_card_mech": "Motore & Meccanica",
        "smart_card_nvme": "Supporto & Usura NVMe",
        
        "clone_title": "Clonazione Resiliente Settore per Settore (Stile ddrescue)",
        "clone_btn_start": "Avvia Clonazione Resiliente",
        "clone_btn_cancel": "Interrompi Clonazione",
        "clone_lbl_dest": "Destinazione (.img):",
        "clone_btn_browse": "Sfoglia...",
        
        "carver_title": "File Carving Forense (Recupero Diretto da Firma)",
        "carver_btn_start": "Avvia File Carving",
        "carver_btn_cancel": "Interrompi Carving",
        "carver_col_id": "ID",
        "carver_col_type": "Tipo",
        "carver_col_lba": "LBA Iniziale",
        "carver_col_size": "Dimensione",
        "carver_col_path": "File Salvato",
        
        "surface_title": "Scansione Superficie & Mappa Termica di Latenza",
        "surface_subtitle": "Misura la latenza blocco per blocco (stile Victoria / MHDD) per rilevare settori degradati.",
        "surface_btn_start": "Avvia Test di Superficie",
        "surface_btn_cancel": "⏹ Ferma",
        
        "logs_title": "Console di Audit Forense & Registro Eventi",
        "logs_btn_clear": "Cancella Registro",
        "logs_btn_export": "Esporta Rapporto (.txt)",
        "status_ready": "Pronto.",
    },

    "ru_RU": {
        "app_title": "DiskRescue",
        "app_subtitle": "Глубокая диагностика в 1 клик • S.M.A.R.T. ATA/NVMe • Надежное клонирование • Восстановление GPT/NTFS",
        "version_badge": " v0.3.3 ФОРЕНЗИК СЮИТ ",
        "uac_admin": "РЕЖИМ АДМИНИСТРАТОРА (RAW ДОСТУП АКТИВЕН)",
        "uac_user": "РЕЖИМ ЧТЕНИЯ (БЕЗ UAC)",
        "btn_elevate": "Повысить UAC",
        "btn_refresh": "Обновить диски",
        "btn_theme_dark": "Тема: Темная",
        "btn_theme_light": "Тема: Светлая",
        "target_drive": "ЦЕЛЕВОЙ ДИСК:",
        "btn_diag_all": "ДИАГНОСТИКА ВСЕГО (1 КЛИК)",
        "raw_status": "Raw: {status}",
        "raw_accessible": "Прямой доступ разрешен",
        "raw_denied": "Доступ заблокирован",
        "no_disk_selected": "Диск не выбран. Ожидание перечисления...",
        "language_label": "Язык:",
        
        "nav_section": "ПАНЕЛЬ НАВИГАЦИИ",
        "nav_dashboard": "Панель & Диагностика 1 клик",
        "nav_diagnostic": "Разделы, GPT & Boot NTFS",
        "nav_smart": "Сенсоры S.M.A.R.T. & Кабели",
        "nav_clone": "Надежное клонирование (.img)",
        "nav_carver": "Форензик Карвинг файлов",
        "nav_surface": "Тепловая карта & Задержка",
        "nav_logs": "Консоль аудита",
        "mini_drive_title": "ВЫБРАННЫЙ ДИСК",
        
        "dash_hero_title": "ГЛУБОКАЯ ДИАГНОСТИКА И НАДЕЖНОСТЬ ДИСКА (1 КЛИК)",
        "dash_hero_badge": "ИНТЕГРИРОВАННЫЙ АНАЛИЗ",
        "dash_waiting": "Ожидание анализа...",
        "dash_select_prompt": "Выберите диск для загрузки сводки надежности.",
        "dash_recommendation_default": "Нажмите 'ПОЛНАЯ ДИАГНОСТИКА (1 КЛИК)' для проверки S.M.A.R.T., кабелей SATA, GPT/MBR, загрузочного сектора NTFS и сканирования 0%-100% поверхности.",
        "dash_depth": "Глубина сканирования:",
        "mode_deep": "Глубокое: Сканировать 0% - 100% диска (512 распределенных зон)",
        "mode_full": "Полное: Чтение 100% секторов диска (Режим Victoria)",
        "mode_quick": "Быстрое: S.M.A.R.T. + Разделы + Boot/MFT + Начало (128 МБ)",
        "dash_telemetry_waiting": "Физическая позиция: Ожидание запуска (0.0% завершено)",
        "btn_hero_scan": "ЗАПУСТИТЬ ПОЛНУЮ ДИАГНОСТИКУ (1 КЛИК)",
        "btn_stop_diag": "⏹ Остановить / Завершить отчет",
        "matrix_title": "МАТРИЦА ОБНАРУЖЕНИЯ СБОЕВ (6 УРОВНЕЙ ОБОРУДОВАНИЯ И ЛОГИКИ)",
        "matrix_badge": "РЕАЛЬНОЕ ВРЕМЯ",
        
        "tile_smart_title": "1. Секторы S.M.A.R.T.",
        "tile_smart_desc": "Переназначенные (0x05) & Нестабильные (0xC5)",
        "tile_cable_title": "2. Кабель SATA & Шина",
        "tile_cable_desc": "Ошибки UltraDMA CRC (0xC7)",
        "tile_thermal_title": "3. Температура & Мотор",
        "tile_thermal_desc": "Температура (°C), Раскрутка & Износ",
        "tile_partition_title": "4. Таблица GPT / MBR",
        "tile_partition_desc": "Сигнатура 0x55AA, CRC32 & Выравнивание",
        "tile_boot_title": "5. Boot Sector & $MFT",
        "tile_boot_desc": "Основной vs Резервный VBR & NTFS $MFT",
        "tile_surface_title": "6. Чтение поверхности",
        "tile_surface_desc": "Тест физических бэд-секторов и задержки",
        "status_pending": "ОЖИДАНИЕ",
        
        "card_registered_table": "1. ЗАРЕГИСТРИРОВАННАЯ ТАБЛИЦА (GPT / MBR)",
        "card_raw_media": "2. РЕАЛЬНЫЙ ФИЗИЧЕСКИЙ НОСИТЕЛЬ (BOOT & MFT)",
        "btn_run_geometry_scan": "Сканировать геометрию и Boot Sectors",
        "card_surgical_repair": "3. ТОЧНЫЕ ИСПРАВЛЕНИЯ С ПРОВЕРЕННЫМ ОТКАТОМ",
        "btn_restore_boot": "Восстановить Boot Sector из резерва",
        "btn_realign_gpt": "Выровнять смещение раздела GPT",
        
        "smart_title": "Телеметрия S.M.A.R.T. & Сенсоры ATA/NVMe",
        "smart_subtitle": "Изоляция проблем с кабелем SATA, температурой, секторами и механикой.",
        "smart_card_sectors": "Проблемные секторы",
        "smart_card_cable": "Помехи кабеля SATA (CRC)",
        "smart_card_temp": "Рабочая температура",
        "smart_card_power": "Время работы & Циклы",
        "smart_card_mech": "Двигатель & Механика",
        "smart_card_nvme": "Носитель & Износ NVMe",
        
        "clone_title": "Надежное посекторное клонирование (стиль ddrescue)",
        "clone_btn_start": "Начать надежное клонирование",
        "clone_btn_cancel": "Прервать клонирование",
        "clone_lbl_dest": "Назначение (.img):",
        "clone_btn_browse": "Обзор...",
        
        "carver_title": "Форензик Карвинг файлов (Поиск по сигнатурам)",
        "carver_btn_start": "Начать File Carving",
        "carver_btn_cancel": "Прервать Carving",
        "carver_col_id": "ID",
        "carver_col_type": "Тип",
        "carver_col_lba": "Начальный LBA",
        "carver_col_size": "Размер",
        "carver_col_path": "Сохраненный файл",
        
        "surface_title": "Сканирование поверхности & Тепловая карта",
        "surface_subtitle": "Измерение физической задержки блоков (стиль Victoria / MHDD) для выявления поврежденных секторов.",
        "surface_btn_start": "Начать тест поверхности",
        "surface_btn_cancel": "⏹ Остановить",
        
        "logs_title": "Консоль аудита & Журнал событий",
        "logs_btn_clear": "Очистить журнал",
        "logs_btn_export": "Экспорт отчета (.txt)",
        "status_ready": "Готово.",
    },

    "zh_CN": {
        "app_title": "DiskRescue",
        "app_subtitle": "一键深度诊断 • ATA/NVMe S.M.A.R.T. • 弹性镜像克隆 • GPT/NTFS 修复",
        "version_badge": " v0.3.3 取证套件 ",
        "uac_admin": "管理员模式 (物理 RAW 访问已激活)",
        "uac_user": "只读模式 (无 UAC 权限)",
        "btn_elevate": "提升 UAC 权限",
        "btn_refresh": "刷新磁盘列表",
        "btn_theme_dark": "主题: 深色",
        "btn_theme_light": "主题: 浅色",
        "target_drive": "目标物理磁盘:",
        "btn_diag_all": "全面诊断 (一键启动)",
        "raw_status": "Raw 状态: {status}",
        "raw_accessible": "已允许物理访问",
        "raw_denied": "物理访问被拒绝",
        "no_disk_selected": "未选择任何磁盘。等待设备枚举...",
        "language_label": "界面语言:",
        
        "nav_section": "导航面板",
        "nav_dashboard": "主控制台 & 一键诊断",
        "nav_diagnostic": "分区表, GPT & NTFS 引导",
        "nav_smart": "S.M.A.R.T. 传感器 & 数据线",
        "nav_clone": "弹性扇区克隆 (.img)",
        "nav_carver": "底层文件特征雕刻 (Carving)",
        "nav_surface": "介质热力图 & 延迟分析",
        "nav_logs": "取证审计终端",
        "mini_drive_title": "当前选中磁盘",
        
        "dash_hero_title": "磁盘深度物理诊断与可靠性评估 (一键启动)",
        "dash_hero_badge": "集成综合分析",
        "dash_waiting": "等待诊断分析...",
        "dash_select_prompt": "请在上方选择磁盘以加载健康评估摘要。",
        "dash_recommendation_default": "点击“执行全面诊断 (一键启动)”将顺序测试 S.M.A.R.T. 传感器、SATA 数据线、GPT/MBR 分区表、NTFS 引导扇区，并扫描 0% 到 100% 的物理介质表面。",
        "dash_depth": "扫描深度:",
        "mode_deep": "深度: 扫描全盘 0% 到 100% 物理范围 (512 个分布式测试区)",
        "mode_full": "完整: 顺序读取全盘 100% 扇区 (Victoria 模式)",
        "mode_quick": "快速: S.M.A.R.T. + 分区结构 + 引导/MFT + 开头 128 MB",
        "dash_telemetry_waiting": "物理扫描位置: 等待启动 (已完成 0.0%)",
        "btn_hero_scan": "执行全面诊断 (一键启动)",
        "btn_stop_diag": "⏹ 停止 / 立即生成报告",
        "matrix_title": "多维故障检测矩阵 (6 层硬件与逻辑分析)",
        "matrix_badge": "实时监测",
        
        "tile_smart_title": "1. S.M.A.R.T. 扇区状态",
        "tile_smart_desc": "重映射扇区 (0x05) 与 待映射扇区 (0xC5)",
        "tile_cable_title": "2. SATA 数据线与总线",
        "tile_cable_desc": "UltraDMA CRC 传输电气校验错误 (0xC7)",
        "tile_thermal_title": "3. 温度与机械电机",
        "tile_thermal_desc": "工作温度 (°C)、主轴起旋与机械磨损",
        "tile_partition_title": "4. GPT / MBR 分区表",
        "tile_partition_desc": "0x55AA 签名、CRC32 校验与扇区对齐",
        "tile_boot_title": "5. 引导扇区与 $MFT",
        "tile_boot_desc": "主引导扇区 vs 备份 VBR 及 NTFS $MFT",
        "tile_surface_title": "6. 物理表面读取",
        "tile_surface_desc": "物理坏道与扇区响应延迟测试",
        "status_pending": "待检测",
        
        "card_registered_table": "1. 磁盘记录分区表 (GPT / MBR)",
        "card_raw_media": "2. 真实物理介质数据 (引导扇区与 MFT)",
        "btn_run_geometry_scan": "启动几何结构与引导扇区深度扫描",
        "card_surgical_repair": "3. 具备原子级回滚验证的外科级修复",
        "btn_restore_boot": "通过备份镜像还原 NTFS 引导扇区",
        "btn_realign_gpt": "重新对齐 GPT 分区偏移量",
        
        "smart_title": "高级 S.M.A.R.T. 遥测与 ATA/NVMe 传感器",
        "smart_subtitle": "精准隔离 SATA 数据线接触不良、温度异常、待处理坏道与机械隐患。",
        "smart_card_sectors": "受损扇区统计",
        "smart_card_cable": "SATA 数据线干扰 (CRC)",
        "smart_card_temp": "当前工作温度",
        "smart_card_power": "通电时长与开关周期",
        "smart_card_mech": "主轴电机与机械",
        "smart_card_nvme": "NVMe 介质健康度与寿命",
        
        "clone_title": "弹性底层逐扇区克隆 (GNU ddrescue 双阶段模式)",
        "clone_btn_start": "开始弹性克隆",
        "clone_btn_cancel": "中止克隆任务",
        "clone_lbl_dest": "目标镜像文件 (.img):",
        "clone_btn_browse": "浏览保存路径...",
        
        "carver_title": "底层文件雕刻取证 (基于文件头特征直接恢复)",
        "carver_btn_start": "启动文件雕刻",
        "carver_btn_cancel": "中止雕刻任务",
        "carver_col_id": "序号",
        "carver_col_type": "文件类型",
        "carver_col_lba": "起始 LBA",
        "carver_col_size": "文件大小",
        "carver_col_path": "恢复保存路径",
        
        "surface_title": "物理介质表面扫描与响应延迟热力图",
        "surface_subtitle": "逐块测量物理响应时间 (Victoria / MHDD 模式)，在数据彻底丢失前捕获老化扇区。",
        "surface_btn_start": "启动介质表面测试",
        "surface_btn_cancel": "⏹ 停止测试",
        
        "logs_title": "底层取证审计控制台与实时事件记录",
        "logs_btn_clear": "清空日志输出",
        "logs_btn_export": "导出审计报告 (.txt)",
        "status_ready": "就绪。",
    },

    "ja_JP": {
        "app_title": "DiskRescue",
        "app_subtitle": "ワンクリック深層診断 • ATA/NVMe S.M.A.R.T. • 復元クローン • GPT/NTFS 修復",
        "version_badge": " v0.3.3 フォレンジック版 ",
        "uac_admin": "管理者モード (RAWアクセス有効)",
        "uac_user": "読み取り専用モード (UACなし)",
        "btn_elevate": "UAC昇格",
        "btn_refresh": "ディスク更新",
        "btn_theme_dark": "テーマ: ダーク",
        "btn_theme_light": "テーマ: ライト",
        "target_drive": "対象の物理ドライブ:",
        "btn_diag_all": "すべて診断 (1クリック)",
        "raw_status": "Raw: {status}",
        "raw_accessible": "物理アクセス許可",
        "raw_denied": "アクセス拒否",
        "no_disk_selected": "デバイスが選択されていません。待機中...",
        "language_label": "言語:",
        
        "nav_section": "ナビゲーション",
        "nav_dashboard": "ダッシュボード & 1クリック診断",
        "nav_diagnostic": "パーティション, GPT & Boot NTFS",
        "nav_smart": "S.M.A.R.T. & ケーブル診断",
        "nav_clone": "復元クローン作成 (.img)",
        "nav_carver": "フォレンジック・ファイル復元",
        "nav_surface": "サーフェス・ヒートマップ",
        "nav_logs": "監査ログ・コンソール",
        "mini_drive_title": "選択中のディスク",
        
        "dash_hero_title": "ディスク深層診断 & 健全性評価 (1クリック)",
        "dash_hero_badge": "統合診断",
        "dash_waiting": "診断待機中...",
        "dash_select_prompt": "ドライブを選択して信頼性サマリーを読み込みます。",
        "dash_recommendation_default": "「完全診断を実行 (1クリック)」をクリックすると、S.M.A.R.T.、SATAケーブル、GPT/MBR、NTFS Boot Sector、ディスク表面の0%〜100%を網羅的に検査します。",
        "dash_depth": "スキャン深度:",
        "mode_deep": "詳細: ディスク全域 0%〜100% スキャン (512分散ゾーン)",
        "mode_full": "完全: 全セクタ 100% 順次読み取り (Victoriaモード)",
        "mode_quick": "高速: S.M.A.R.T. + パーティション + Boot/MFT + 先頭 128MB",
        "dash_telemetry_waiting": "スキャン位置: 開始待機中 (0.0% 完了)",
        "btn_hero_scan": "完全診断を実行 (1クリック)",
        "btn_stop_diag": "⏹ 停止 / 診断レポート生成",
        "matrix_title": "障害検知マトリクス (6層ハードウェア＆論理診断)",
        "matrix_badge": "リアルタイム",
        
        "tile_smart_title": "1. S.M.A.R.T. セクタ",
        "tile_smart_desc": "代替処理済 (0x05) & 保留中 (0xC5)",
        "tile_cable_title": "2. SATAケーブル & バス",
        "tile_cable_desc": "UltraDMA CRC 電気的エラー (0xC7)",
        "tile_thermal_title": "3. 温度 & モーター",
        "tile_thermal_desc": "温度 (°C)、スピン & 経年劣化",
        "tile_partition_title": "4. GPT / MBR テーブル",
        "tile_partition_desc": "0x55AA 署名、CRC32 & アライメント",
        "tile_boot_title": "5. Boot Sector & $MFT",
        "tile_boot_desc": "プライマリ vs バックアップ VBR & NTFS $MFT",
        "tile_surface_title": "6. サーフェス読み取り",
        "tile_surface_desc": "不良セクタ & 応答遅延検査",
        "status_pending": "保留中",
        
        "card_registered_table": "1. 記録されたパーティションテーブル (GPT / MBR)",
        "card_raw_media": "2. 実際の物理メディア (BOOT SECTORS & MFTs)",
        "btn_run_geometry_scan": "ジオメトリ & ブートセクタスキャン開始",
        "card_surgical_repair": "3. ロールバック保護付き修復アクション",
        "btn_restore_boot": "バックアップからブートセクタを復元",
        "btn_realign_gpt": "GPT パーティションオフセットを再調整",
        
        "smart_title": "高度な S.M.A.R.T. テレメトリ & ATA/NVMe センサー",
        "smart_subtitle": "SATAケーブル不良、温度異常、保留セクタ、機構トラブルを切り分けます。",
        "smart_card_sectors": "不良セクタ統計",
        "smart_card_cable": "SATAケーブルノイズ (CRC)",
        "smart_card_temp": "動作温度",
        "smart_card_power": "通電時間 & 電源サイクル",
        "smart_card_mech": "モーター & 駆動機構",
        "smart_card_nvme": "NVMe メディア寿命 & 摩耗",
        
        "clone_title": "高耐障害セクタークローン (ddrescue 方式)",
        "clone_btn_start": "クローン作成開始",
        "clone_btn_cancel": "クローンを中断",
        "clone_lbl_dest": "保存先 (.img):",
        "clone_btn_browse": "参照...",
        
        "carver_title": "フォレンジック・ファイルカービング (シグネチャ直接救出)",
        "carver_btn_start": "ファイルカービング開始",
        "carver_btn_cancel": "カービングを中断",
        "carver_col_id": "ID",
        "carver_col_type": "形式",
        "carver_col_lba": "開始 LBA",
        "carver_col_size": "サイズ",
        "carver_col_path": "保存先ファイル",
        
        "surface_title": "サーフェススキャン & セクタ応答遅延ヒートマップ",
        "surface_subtitle": "ブロック単位で応答速度を計測 (Victoria / MHDD 方式) し、完全故障前に劣化を特定します。",
        "surface_btn_start": "サーフェステスト開始",
        "surface_btn_cancel": "⏹ 停止",
        
        "logs_title": "フォレンジック監査コンソール & イベントログ",
        "logs_btn_clear": "ログをクリア",
        "logs_btn_export": "レポート出力 (.txt)",
        "status_ready": "準備完了。",
    },
}

_current_lang = "pt_BR"


def detect_system_language() -> str:
    """Detecta o idioma do sistema operacional Windows ou usa fallback para pt_BR."""
    try:
        if os.name == "nt":
            lang_id = ctypes.windll.kernel32.GetUserDefaultUILanguage()
            primary = lang_id & 0x3FF
            # Mapeamento primário Win32
            mapping = {
                0x16: "pt_BR",  # Portuguese
                0x09: "en_US",  # English
                0x0A: "es_ES",  # Spanish
                0x0C: "fr_FR",  # French
                0x07: "de_DE",  # German
                0x10: "it_IT",  # Italian
                0x19: "ru_RU",  # Russian
                0x04: "zh_CN",  # Chinese
                0x11: "ja_JP",  # Japanese
            }
            if primary in mapping:
                return mapping[primary]
    except Exception:
        pass

    try:
        loc = locale.getdefaultlocale()[0]
        if loc:
            for k in SUPPORTED_LANGUAGES:
                if loc.startswith(k[:2]):
                    return k
    except Exception:
        pass

    return "pt_BR"


def set_language(lang_code: str) -> None:
    """Define o idioma global ativo."""
    global _current_lang
    if lang_code in TRANSLATIONS:
        _current_lang = lang_code
    elif lang_code[:2] in [k[:2] for k in TRANSLATIONS]:
        for k in TRANSLATIONS:
            if k.startswith(lang_code[:2]):
                _current_lang = k
                break
    else:
        _current_lang = "en_US"


def get_current_language() -> str:
    """Retorna o código do idioma ativo (ex: 'pt_BR')."""
    return _current_lang


def t(key: str, **kwargs: Any) -> str:
    """
    Traduz uma chave para o idioma ativo.
    Faz fallback para pt_BR e depois en_US caso a chave não exista no idioma selecionado.
    """
    lang_dict = TRANSLATIONS.get(_current_lang, TRANSLATIONS["pt_BR"])
    text = lang_dict.get(key)
    if text is None:
        text = TRANSLATIONS["pt_BR"].get(key)
    if text is None:
        text = TRANSLATIONS["en_US"].get(key, key)

    if kwargs:
        try:
            return text.format(**kwargs)
        except Exception:
            return text
    return text
