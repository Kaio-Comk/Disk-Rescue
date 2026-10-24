# 🛠️ DiskRescue

**Sistema Unificado e Autônomo para Diagnóstico Profundo e Recuperação de Discos.**

O **DiskRescue** é uma suíte em Python para análise forense, diagnóstico de saúde físico-lógica e reparo automatizado não destrutivo de discos rígidos (HDDs), SSDs e mídias removíveis (USB/Cartões SD).

---

## 🎯 Por que o DiskRescue?

* **Diagnóstico Automatizado em Camadas:** Identifica em segundos a causa raiz exata do problema (hardware, SMART, partição deslocada, boot sector corrompido ou bad sectors).
* **Segurança "Do No Harm":** 100% somente-leitura por padrão. Se houver reparo in-place de tabela de partição, cria backup de cada LBA antes de gravar 1 byte.
* **Sem Varreduras Destrutivas:** Diferente de softwares comerciais que forçam o disco morrendo, o DiskRescue detecta falhas mecânicas e redireciona direto para clonagem de emergência (*ddrescue*).
* **Resolução Cirúrgica Instantânea:** Partições que o Windows enxerga como RAW por causa de ponteiros GPT deslocados ou boot sectors corrompidos são recuperadas em menos de 2 segundos.

---

## 📁 Estrutura do Projeto

```
DiskRescue/
├── core/
│   ├── hardware/       # Descoberta de dispositivos, barramento e SMART
│   ├── partition/      # Parsers e construtores de MBR e GPT (com validação CRC32)
│   ├── filesystem/     # Scanners de Boot Sectors (NTFS, FAT32, exFAT, EXT4) e MFT
│   └── recovery/       # Snapshots de LBA, reparo in-place e orquestrador ddrescue
├── cli/                # Interface interativa de linha de comando
├── tools/              # Ferramentas auxiliares de dump e inspeção
└── tests/              # Testes unitários com fixtures e discos sintéticos
```

---

## 🚀 Como Executar

```bash
# Executar a CLI interativa (requer privilégio de Administrador/root para acesso raw aos discos)
python -m cli.main
```
