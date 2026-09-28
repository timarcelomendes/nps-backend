import os
from sqlalchemy import create_engine, event, text
from dotenv import load_dotenv

load_dotenv()

# Instância única do engine (pool de conexões compartilhado)
_engine_instance = None


def _montar_url() -> str:
    """Lê a DATABASE_URL (padrão do Render) e ajusta para o driver psycopg 3."""
    url = os.getenv("DATABASE_URL", "").strip()
    if not url:
        raise RuntimeError("DATABASE_URL não configurada. Ex.: postgresql://usuario:senha@host:5432/banco")
    if url.startswith("postgres://"):
        url = "postgresql://" + url[len("postgres://"):]
    if url.startswith("postgresql://"):
        url = "postgresql+psycopg://" + url[len("postgresql://"):]
    return url


def _bool_para_int(valor):
    # As colunas de flag (ativo, excluido, revogado...) são SMALLINT 0/1, como no SQL Server.
    if isinstance(valor, bool):
        return int(valor)
    return valor


def get_engine():
    global _engine_instance

    if _engine_instance is not None:
        return _engine_instance

    print("🔌 Criando nova conexão com o PostgreSQL...")

    _engine_instance = create_engine(
        _montar_url(),
        pool_size=5,
        max_overflow=10,
        pool_pre_ping=True,
        pool_recycle=300,
        future=True,
        connect_args={
            "connect_timeout": 30,
            # schema "dbo" primeiro: as funções de compatibilidade (GETDATE, DATEADD...) moram nele
            "options": "-c search_path=dbo,public -c timezone=UTC",
        },
    )

    @event.listens_for(_engine_instance, "before_cursor_execute", retval=True)
    def _converter_parametros(conn, cursor, statement, parameters, context, executemany):
        if isinstance(parameters, dict):
            parameters = {k: _bool_para_int(v) for k, v in parameters.items()}
        elif isinstance(parameters, (list, tuple)) and parameters and isinstance(parameters[0], dict):
            parameters = [{k: _bool_para_int(v) for k, v in p.items()} for p in parameters]
        return statement, parameters

    return _engine_instance


def exec_sql(sql: str, params: dict | None = None):
    engine = get_engine()
    with engine.begin() as conn:
        return conn.execute(text(sql), params or {})
