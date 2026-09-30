# Automação do GitHub (Actions)

Estes arquivos precisam ficar em `.github/workflows/` para o GitHub rodar.
Na pasta do projeto, rode uma vez:

```bash
mkdir -p .github/workflows
git mv ci-github/testes.yml ci-github/backup.yml .github/workflows/
git rm .github/workflows/main_nps-backend.yml   # deploy antigo para o Azure da Stefanini
git commit -m "Ativa testes e backup no GitHub Actions"
```

- `testes.yml`: roda os testes a cada push.
- `backup.yml`: backup diário do banco (veja docs/BACKUP.md para configurar os segredos).
