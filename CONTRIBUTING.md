# Diretrizes de Contribuição — DiskRescue

Agradecemos o interesse em contribuir com o **DiskRescue**! Este projeto lida diretamente com recuperação forense de discos, portanto todas as contribuições devem aderir rigorosamente aos nossos princípios de segurança e integridade de dados.

---

## 🛡️ A Regra de Ouro: "Do No Harm"

1. **Nunca adicione código que realize escritas destrutivas ou não autorizadas.**
2. Todas as rotinas de diagnóstico, escaneamento ou file carving devem operar em **modo somente leitura (Write-Blocker)**.
3. Reparos in-place devem **sempre** gerar snapshot binário preventivo dos setores antes de qualquer alteração física.
4. Mantenha 100% dos testes unitários passando (`pytest tests/`).

---

## 🚀 Como Submeter Mudanças

1. Faça um Fork do repositório.
2. Crie uma branch descritiva (`git checkout -b feat/sua-feature` ou `fix/seu-ajuste`).
3. Escreva testes unitários para a nova funcionalidade em `tests/`.
4. Garanta que o pre-commit ou testes passem:
   ```powershell
   pytest tests/ -v
   ```
5. Abra um Pull Request detalhando o problema resolvido e os testes executados.
