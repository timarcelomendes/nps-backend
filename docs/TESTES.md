# Testes automatizados

Rodam com PostgreSQL de verdade, porque a separação entre empresas clientes depende do Row Level Security do banco.

```bash
pip install -r requirements-dev.txt
# precisa de um PostgreSQL local com um usuário administrador:
export TEST_ADMIN_DATABASE_URL=postgresql://postgres:postgres@localhost:5432/postgres
pytest -q
```

Cada execução recria o banco `rakiti_teste` com um usuário sem superpoderes (superusuário ignora o RLS). Nenhum e-mail sai de verdade e o Asaas é simulado.

| Arquivo | O que garante |
|---|---|
| `tests/test_isolamento.py` | Uma empresa não vê nem altera dados de outra; rotas exigem login |
| `tests/test_formularios.py` | Validação, lógica por nota, link público, resultados, arquivamento |
| `tests/test_lembretes.py` | Prazos dos lembretes, limite, quem respondeu não recebe, convite novo substitui o antigo |
| `tests/test_cobranca.py` | Cadastro com teste grátis, confirmação de e-mail, limite de clientes, teste expirado, assinatura e webhook do Asaas |
| `tests/test_dashboard.py` | Números da Visão geral consistentes |

No GitHub, `.github/workflows/testes.yml` roda tudo a cada push.
