"""
Prepara o banco automaticamente na inicialização da API.

1. Se as tabelas ainda não existem, executa db/schema_postgres.sql.
2. Se não existe nenhum usuário, cria o Admin inicial a partir das variáveis:
     ADMIN_EMAIL, ADMIN_SENHA, ADMIN_NOME (opcional)
   e grava DOMINIOS_PERMITIDOS (opcional; padrão = domínio do ADMIN_EMAIL).

É seguro rodar a cada inicialização: nada é recriado se já existir.
"""
import os
from pathlib import Path
from sqlalchemy import text

from database import get_engine

SCHEMA_FILE = Path(__file__).parent / "db" / "schema_postgres.sql"


def _tabelas_existem(conn) -> bool:
    return conn.execute(text("SELECT to_regclass('dbo.nps_usuarios') IS NOT NULL")).scalar()


def _criar_tabelas(engine):
    sql = SCHEMA_FILE.read_text(encoding="utf-8")
    raw = engine.raw_connection()
    try:
        cur = raw.cursor()
        cur.execute(sql)  # script inteiro (sem parâmetros) em uma única transação
        raw.commit()
    except Exception:
        raw.rollback()
        raise
    finally:
        raw.close()


def _criar_admin_inicial(conn):
    email = os.getenv("ADMIN_EMAIL", "").strip()
    senha = os.getenv("ADMIN_SENHA", "")
    if not email or not senha:
        print("ℹ️ Nenhum usuário no banco. Defina ADMIN_EMAIL e ADMIN_SENHA para criar o Admin inicial.")
        return
    from services.auth_svc import hash_password
    nome = os.getenv("ADMIN_NOME", "Administrador").strip() or "Administrador"
    conn.execute(text("""
        INSERT INTO dbo.nps_usuarios (nome, email, senha_hash, tipo, cargo, ativo, email_verificado)
        VALUES (:nome, :email, :hash, 'Admin', 'Administrador', 1, 1)
    """), {"nome": nome, "email": email, "hash": hash_password(senha)})

    dominios = os.getenv("DOMINIOS_PERMITIDOS", "").strip() or email.split("@")[-1].lower()
    conn.execute(text("""
        INSERT INTO dbo.nps_configuracoes (chave, valor, descricao)
        VALUES ('dominios_permitidos', :v, 'Domínios de e-mail que podem acessar (separados por vírgula)')
        ON CONFLICT (chave) DO UPDATE SET valor = EXCLUDED.valor, updated_at = CURRENT_TIMESTAMP
    """), {"v": dominios})
    print(f"👤 Admin inicial criado: {email} (domínios permitidos: {dominios})")


def preparar_banco():
    try:
        engine = get_engine()
        with engine.connect() as conn:
            existe = _tabelas_existem(conn)
        if not existe:
            print("🧱 Banco vazio: criando tabelas a partir de db/schema_postgres.sql ...")
            _criar_tabelas(engine)
            print("✅ Tabelas criadas.")
        with engine.begin() as conn:
            total = conn.execute(text("SELECT COUNT(*) FROM dbo.nps_usuarios")).scalar()
            if total == 0:
                _criar_admin_inicial(conn)
    except Exception as e:
        print(f"❌ Erro ao preparar o banco: {e}")
