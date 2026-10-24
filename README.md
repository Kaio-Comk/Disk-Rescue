# 🛠️ DiskRescue

**Sistema Unificado e Autônomo para Diagnóstico Profundo e Recuperação Forense de Discos.**

O **DiskRescue** é uma suíte forense para análise, diagnóstico de saúde físico-lógica e reparo automatizado não destrutivo de discos rígidos (HDDs), SSDs e mídias removíveis (USB/Cartões SD), com interface gráfica moderna no padrão **Windows 11 Fluent Design** e binário executável portátil (`.exe`).

---

## 💻 Aplicativo Desktop Portátil (`DiskRescue.exe`)

O executável pré-compilado está pronto em:
```
dist/DiskRescue.exe
```

* **Sem necessidade de instalar Python** ou dependências.
* **Elevação UAC Automática:** Manifest embutido solicita privilégios de Administrador ao iniciar, garantindo acesso raw imediato aos setores físicos dos discos.
* **Visual Moderno e Limpo:** Interface com tema Sun Valley escuro/claro, cartões informativos, abas operacionais e terminal forense em tempo real.

---

## 🎯 Capacidades do Sistema

* **🔍 Diagnóstico de Geometria & Reparo In-Place:**
  * Lê e valida tabelas **GPT** primárias e secundárias com verificação de **CRC32**.
  * Varre setores físicos em busca de **Boot Sectors reais e Master File Table ($MFT)**.
  * Detecta partições reportadas como **RAW** pelo Windows por ponteiros desalinhados e aplica **reparo cirúrgico com snapshot de segurança** automático.

* **📊 Telemetria de Confiabilidade & SMART:**
  * Monitoramento direto de temperatura (°C), erros totais de leitura/gravação, desgaste e status de integridade física.
  * Diretrizes de segurança (*"Do No Harm"*): trava operações arriscadas se a integridade física estiver degradada.

* **💾 Imagem Forense Byte-a-Byte (.img):**
  * Cria uma cópia espelho completa da mídia com progresso em tempo real (setores, velocidade em MB/s e tempo estimado).
  * Permite trabalhar na imagem virtual sem estressar ou degradar o disco físico original.

* **📝 Terminal de Log Forense:**
  * Registro detalhado de cada LBA inspecionado, cálculo de checksums e eventos de montagem, com exportação para arquivo `.txt`.

---

## 📁 Estrutura do Projeto

```
DiskRescue/
├── core/
│   ├── hardware/       # Descoberta de dispositivos, barramento, SMART e elevação
│   ├── partition/      # Parsers e reconstrutores GPT com recálculo CRC32
│   ├── filesystem/     # Scanners de Boot Sectors (NTFS/MFT)
│   └── recovery/       # Snapshots de LBA, reparo in-place e imagem forense raw
├── gui/                # Interface Desktop moderna com tema Sun Valley
│   └── app.py          # Dashboard com abas de Diagnóstico, SMART, Clone e Logs
├── cli/                # Interface interativa de linha de comando
├── dist/
│   └── DiskRescue.exe  # Executável portátil para Windows
├── diskrescue_gui.pyw  # Ponto de entrada gráfico
└── tests/              # Testes unitários com discos sintéticos
```

---

## 🚀 Como Executar

### Opção 1: Executável Portátil (Recomendado)
Dê um duplo-clique em `dist/DiskRescue.exe` (confirme a permissão de Administrador do Windows).

### Opção 2: Via Python
```bash
# Executar a interface gráfica moderna
python diskrescue_gui.pyw

# Ou executar a interface via terminal
python -m cli.main
```
