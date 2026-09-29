import os
from sqlalchemy import text
from database import get_engine

def get_openai_token():
    """
    Recupera a API Key do banco de dados. 
    (Sem cache: cada conta tem a sua própria chave.)
    Prioridade: variável de ambiente OPENAI_API_KEY; depois a chave salva nas Configurações.
    """
    chave_env = os.getenv("OPENAI_API_KEY", "").strip()
    if chave_env:
        return chave_env
    try:
        engine = get_engine()
        with engine.connect() as conn:
            sql = text("SELECT valor FROM dbo.nps_configuracoes WHERE chave = 'openai_api_key'")
            token = conn.execute(sql).scalar()
            
            if not token:
                print("⚠️ Alerta: openai_api_key não encontrada no banco.")
                return None
                
            return token.strip()
    except Exception as e:
        print(f"❌ Erro ao buscar token no SQL: {e}")
        return None

def clear_config_cache():
    """Limpa o cache caso você altere a chave via Dashboard e precise que ela atualize na hora"""
    return None  # mantido por compatibilidade