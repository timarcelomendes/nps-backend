# Backup e restauração do banco

## Como funciona
- Todo dia às 03:15 (Brasília) o GitHub Actions roda `.github/workflows/backup.yml`.
- Ele faz um `pg_dump` completo do banco de produção, criptografa com a senha `BACKUP_SENHA` (AES-256) e guarda o arquivo por **30 dias** na aba *Actions* do repositório (e, se configurado, também num bucket S3/R2/B2).
- Para rodar na hora: *Actions > Backup do banco > Run workflow*.

## Configurar (uma vez)
No GitHub do `nps-backend`: *Settings > Secrets and variables > Actions > New repository secret*
1. `BACKUP_DATABASE_URL`: a **External Database URL** do banco no Render, com `?sslmode=require` no final.
2. `BACKUP_SENHA`: uma senha longa. **Guarde num gerenciador de senhas**: sem ela o backup não abre.

> O plano pago do banco no Render também faz backups próprios. Este backup é uma segunda cópia, fora do Render, contra erro humano ou problema na conta.

## Restaurar
1. Baixe o arquivo `rakiti-AAAA-MM-DD.dump.gpg` em *Actions > (execução do dia) > Artifacts*.
2. Descriptografe:
   ```bash
   gpg --decrypt -o rakiti.dump rakiti-AAAA-MM-DD.dump.gpg   # pede a BACKUP_SENHA
   ```
3. Restaure num banco **novo/vazio** (nunca por cima do de produção sem ter certeza):
   ```bash
   pg_restore --no-owner --no-privileges -d "postgresql://usuario:senha@host/banco_novo?sslmode=require" rakiti.dump
   ```
4. Confira os dados e só então aponte a `DATABASE_URL` do serviço para o banco restaurado.

## Teste de restauração
Faça pelo menos uma vez por trimestre: restaure o último backup num banco de teste e abra o sistema apontando para ele. Backup que nunca foi restaurado não é garantia.
