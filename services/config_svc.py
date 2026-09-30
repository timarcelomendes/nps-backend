"""
Configuração da Inteligência Artificial.

A chave da OpenAI é da PLATAFORMA (variável OPENAI_API_KEY no servidor), não do cliente:
o custo fica embutido no preço do plano. Cada conta tem um limite mensal de análises
(variável IA_LIMITE_MENSAL, padrão 500) para o custo não sair do controle.
"""
import os
from datetime import datetime
from sqlalchemy import text
from database import get_engine

MSG_IA_INDISPONIVEL = "A inteligência artificial está temporariamente indisponível. Tente novamente mais tarde."
MSG_IA_LIMITE = "O limite mensal de análises de IA do seu plano foi atingido. Fale com o suporte para ampliar."


def get_openai_token():
    """Chave da plataforma. (Compatibilidade: se não houver, usa a salva na conta.)"""
    chave_env = os.getenv("OPENAI_API_KEY", "").strip()
    if chave_env:
        return chave_env
    try:
        with get_engine().connect() as conn:
            token = conn.execute(text("SELECT valor FROM dbo.nps_configuracoes WHERE chave = 'openai_api_key'")).scalar()
            return token.strip() if token and token.strip() else None
    except Exception as e:
        print(f"❌ Erro ao buscar chave de IA: {e}")
        return None


def _chave_uso() -> str:
    return f"ia_uso_{datetime.utcnow():%Y%m}"


def limite_mensal_ia() -> int:
    """Limite do plano da conta; contas cortesia usam IA_LIMITE_MENSAL (padrão 500)."""
    try:
        from services.planos_svc import limite_ia
        do_plano = limite_ia()
        if do_plano is not None:
            return do_plano
    except Exception:
        pass
    try:
        return int(os.getenv("IA_LIMITE_MENSAL", "500"))
    except ValueError:
        return 500


def uso_mensal_ia() -> int:
    try:
        with get_engine().connect() as conn:
            valor = conn.execute(text("SELECT valor FROM dbo.nps_configuracoes WHERE chave = :c"), {"c": _chave_uso()}).scalar()
        return int(valor or 0)
    except Exception:
        return 0


def ia_disponivel():
    """Retorna (chave, mensagem_de_erro). Se a chave vier None, mostre a mensagem ao usuário."""
    chave = get_openai_token()
    if not chave:
        return None, MSG_IA_INDISPONIVEL
    if uso_mensal_ia() >= limite_mensal_ia():
        return None, MSG_IA_LIMITE
    return chave, None


def registrar_uso_ia(quantidade: int = 1):
    try:
        with get_engine().begin() as conn:
            conn.execute(text("""
                INSERT INTO dbo.nps_configuracoes (chave, valor, descricao, updated_at)
                VALUES (:c, :q, 'Análises de IA usadas no mês', CURRENT_TIMESTAMP)
                ON CONFLICT (conta_id, chave) DO UPDATE
                SET valor = (COALESCE(NULLIF(dbo.nps_configuracoes.valor, ''), '0')::int + :qi)::text,
                    updated_at = CURRENT_TIMESTAMP
            """), {"c": _chave_uso(), "q": str(quantidade), "qi": quantidade})
    except Exception as e:
        print(f"⚠️ Não foi possível registrar o uso de IA: {e}")


def clear_config_cache():
    return None  # mantido por compatibilidade
