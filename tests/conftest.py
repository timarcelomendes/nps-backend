"""
Testes com PostgreSQL de verdade (a separação entre contas depende do Row Level Security).

Cada execução cria do zero o banco `rakiti_teste`, com um usuário SEM superpoderes
(superusuário ignora o RLS e esconderia vazamentos entre contas).

Variável: TEST_ADMIN_DATABASE_URL (padrão postgresql://postgres:postgres@localhost:5432/postgres)
Rodar:    pytest -q
"""
import os
import sys
import pytest
import psycopg

ADMIN_URL = os.getenv("TEST_ADMIN_DATABASE_URL", "postgresql://postgres:postgres@localhost:5432/postgres")
HOST = ADMIN_URL.split("@", 1)[1].split("/", 1)[0]
DB, USUARIO, SENHA = "rakiti_teste", "rakiti_teste", "rakiti_teste"

ADMIN_EMAIL, ADMIN_SENHA = "admin@rakiti.test", "Senha@Forte123"


def _preparar_banco():
    with psycopg.connect(ADMIN_URL, autocommit=True) as c:
        c.execute(f"SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname = '{DB}'")
        c.execute(f"DROP DATABASE IF EXISTS {DB}")
        if not c.execute(f"SELECT 1 FROM pg_roles WHERE rolname = '{USUARIO}'").fetchone():
            c.execute(f"CREATE ROLE {USUARIO} LOGIN PASSWORD '{SENHA}' NOSUPERUSER NOBYPASSRLS")
        c.execute(f"CREATE DATABASE {DB} OWNER {USUARIO}")


_preparar_banco()
os.environ.update({
    "DATABASE_URL": f"postgresql://{USUARIO}:{SENHA}@{HOST}/{DB}",
    "AMBIENTE": "dev",
    "FRONTEND_URL": "http://app.teste",
    "JWT_SECRET_KEY": "segredo-de-teste",
    "ENCRYPTION_KEY": "c2VncmVkby1kZS10ZXN0ZS0zMi1ieXRlcy1hcXVpISE=",
    "ADMIN_EMAIL": ADMIN_EMAIL, "ADMIN_SENHA": ADMIN_SENHA, "ADMIN_NOME": "Admin Teste",
    "DOMINIOS_PERMITIDOS": "rakiti.test",
    "SUPERADMIN_EMAILS": ADMIN_EMAIL,
    "ZEPTOMAIL_TOKEN": "token-falso", "EMAIL_REMETENTE": "pesquisa@rakiti.test", "EMAIL_REMETENTE_NOME": "Rakiti",
    "DESABILITAR_AGENDADOR": "1",
    "ASAAS_API_KEY": "chave-falsa", "ASAAS_WEBHOOK_TOKEN": "webhook-teste",
})
os.environ.pop("RESEND_API_KEY", None)
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


class _Resp:
    def __init__(self, status=200, dados=None):
        self.status_code, self._dados = status, dados or {}
        self.text = str(self._dados)

    def json(self):
        return self._dados


@pytest.fixture(scope="session")
def app():
    import main
    main.limiter.enabled = False  # o limite de tentativas atrapalharia os testes
    return main.app


@pytest.fixture(scope="session")
def client(app):
    from fastapi.testclient import TestClient
    with TestClient(app) as c:  # roda o lifespan: cria tabelas, migrações e o admin
        yield c


CAIXA = []   # tudo que seria enviado por e-mail durante os testes


@pytest.fixture(scope="session", autouse=True)
def _sem_email_de_verdade():
    """Nenhum e-mail sai de verdade: o envio HTTP do provedor é trocado por esta caixa."""
    import services.mail_provider as mp
    original = mp.requests.post

    def falso_post(url, json=None, headers=None, timeout=None, **kw):
        CAIXA.append({"url": url, "json": json})
        return _Resp(201, {"message": "OK"})
    mp.requests.post = falso_post
    yield
    mp.requests.post = original


@pytest.fixture(scope="session")
def emails():
    return CAIXA


def login(client, email, senha):
    r = client.post("/api/login", json={"email": email, "password": senha})
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


@pytest.fixture(scope="session")
def admin(client):
    return login(client, ADMIN_EMAIL, ADMIN_SENHA)


@pytest.fixture(scope="session")
def outra_conta(client, admin):
    """Segunda empresa cliente, criada pela aba Plataforma (superadmin)."""
    r = client.post("/api/superadmin/contas", headers=admin, json={
        "nome": "Distribuidora Teste", "admin_nome": "Carla", "admin_email": "carla@outra.test",
        "admin_senha": "Senha@Forte123", "dominios": "outra.test"})
    assert r.status_code == 200, r.text
    return login(client, "carla@outra.test", "Senha@Forte123")


def sql(query, params=None, conta=None):
    """Consulta direta no banco (modo sistema = enxerga todas as contas)."""
    from sqlalchemy import text
    from database import get_engine, modo_sistema, usando_conta
    ctx = usando_conta(conta) if conta else modo_sistema()
    with ctx:
        with get_engine().begin() as conn:
            res = conn.execute(text(query), params or {})
            return res.mappings().all() if res.returns_rows else None
