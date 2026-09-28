import os
import re
import time
import random
import hashlib
import requests
import pandas as pd
from datetime import date
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from database import get_engine, exec_sql

PERFIS = ["Decisor", "Influenciador"]

def normalize(s: str) -> str:
    s = (s or "").strip().lower()
    return re.sub(r"\s+", " ", s)

def make_cliente_id(email: str, empresa: str) -> str:
    base = f"{normalize(empresa)}|{normalize(email)}"
    digest = hashlib.sha256(base.encode("utf-8")).hexdigest()
    return "C" + digest[:16]

def read_df(sql: str, params: dict = None) -> pd.DataFrame:
    engine = get_engine()
    with engine.connect() as conn:
        result = conn.execute(text(sql), params or {})
        rows = result.fetchall()
        cols = list(result.keys())
    return pd.DataFrame(rows, columns=cols)

def disparar_n8n_force(cliente_id: str) -> tuple[bool, str, dict]:
    base_url = (os.getenv("N8N_FORCE_URL") or "").strip()
    if not base_url:
        return False, "N8N_FORCE_URL não configurada.", {"stage": "config"}

    payload = {"cliente_id": cliente_id}
    urls_to_try = [base_url]
    if "/webhook-test/" in base_url:
        urls_to_try.append(base_url.replace("/webhook-test/", "/webhook/"))

    last_details = {}
    for url in urls_to_try:
        t0 = time.time()
        try:
            resp = requests.post(url, json=payload, timeout=40)
            ms = int((time.time() - t0) * 1000)

            if resp.status_code in (404, 410):
                last_details = {"stage": "webhook_not_listening", "http": resp.status_code, "ms": ms, "url": url}
                continue

            if not (200 <= resp.status_code < 300):
                return False, f"Falha webhook (HTTP {resp.status_code}).", {"stage": "http_error"}

            data = resp.json() if resp.text else {}
            if isinstance(data, dict) and data.get("ok") is True:
                return True, f"Fluxo iniciado no n8n ✅", {"stage": "started", "ms": ms, "data": data}

            return True, "Envio concluído ✅", {"stage": "ok_no_confirm", "ms": ms}

        except Exception as e:
            return False, "Erro ao conectar no n8n.", {"stage": "exception", "error": str(e)}

    return False, "Webhook do n8n não está disponível (404/410).", last_details

def load_clientes(q: str, ativo: str, perfil: str, topn: int) -> pd.DataFrame:
    where = []
    params = {}

    if (q or "").strip():
        where.append("""
        (LOWER(c.nome) LIKE :like OR 
         LOWER(c.email) LIKE :like OR 
         LOWER(c.empresa) LIKE :like OR 
         CAST(c.cliente_id AS VARCHAR(100)) LIKE :like_id)
        """)
        params["like"] = f"%{q.strip().lower()}%"
        params["like_id"] = f"%{q.strip()}%"

    where_sql = ("WHERE " + " AND ".join(where)) if where else ""

    # Busca o ID da empresa através do JOIN e cruza com a Fila de Disparos
    sql = f"""
    -- 👇 CTE para obter apenas o último disparo de cada cliente
    WITH LatestDisparo AS (
        SELECT 
            cliente_id,
            data_envio_inicial,
            data_ultimo_lembrete,
            status,
            lembretes_enviados,
            ROW_NUMBER() OVER(PARTITION BY cliente_id ORDER BY COALESCE(data_envio_inicial, created_at) DESC) as rn
        FROM dbo.nps_disparos
    )
    SELECT
        c.cliente_id, 
        c.nome, 
        c.email, 
        c.telefone,          
        
        c.cargo_id, cg.nome as cargo,             
        c.empresa_id, e.nome as empresa, e.gestor,            
        c.perfil_id, p.nome as perfil_decisor, 
        c.segmento_id, s.nome as segmento,
        
        COALESCE(d.data_ultimo_lembrete, d.data_envio_inicial, c.ultimo_envio) as ultimo_envio,
        DATEADD('day', COALESCE((SELECT dbo.try_int(valor) FROM dbo.nps_configuracoes WHERE chave = 'recorrencia_dias' LIMIT 1), 90), COALESCE(d.data_ultimo_lembrete, d.data_envio_inicial, c.ultimo_envio)) AS proximo_envio,

        -- 🎯 MAGIA AQUI: Lógica à prova de falhas para o Estado
        CASE 
            -- 1. Se o ciclo de carência terminou, volta para a fila
            WHEN GETDATE() >= DATEADD('day', COALESCE((SELECT dbo.try_int(valor) FROM dbo.nps_configuracoes WHERE chave = 'recorrencia_dias' LIMIT 1), 90), COALESCE(d.data_ultimo_lembrete, d.data_envio_inicial, c.ultimo_envio)) THEN 'Pendente'
            
            -- 2. Se nunca foi disparado
            WHEN COALESCE(d.data_ultimo_lembrete, d.data_envio_inicial, c.ultimo_envio) IS NULL THEN 'Pendente'
            
            -- 3. A PROVA DE BALA: Vai à tabela de respostas confirmar se há algo mais recente que o disparo
            WHEN (SELECT COUNT(1) FROM dbo.nps_respostas r2 WHERE r2.cliente_id = c.cliente_id AND COALESCE(r2.data_resposta, r2.created_at) >= COALESCE(d.data_envio_inicial, c.ultimo_envio, '2000-01-01')) > 0 THEN 'Respondido'
            
            -- 4. Força a prioridade máxima para a palavra "Respondido" se estiver presa numa das tabelas
            WHEN c.status_envio = 'Respondido' OR d.status = 'Respondido' THEN 'Respondido'
            
            -- 5. Fallback padrão
            ELSE COALESCE(d.status, c.status_envio, 'Não Iniciado')
        END AS status_envio,
        
        d.data_envio_inicial as data_envio_inicial,
        COALESCE(d.lembretes_enviados, 0) as lembretes_enviados,
        c.ativo, 
        c.updated_at,
        
        (SELECT COUNT(1) FROM dbo.nps_respostas r WHERE r.cliente_id = c.cliente_id) AS respostas_cliente,
        (SELECT COUNT(1) FROM dbo.nps_respostas r2 WHERE r2.empresa_id = c.empresa_id) AS respostas_empresa,
        
        CAST(CASE WHEN EXISTS (SELECT 1 FROM dbo.nps_acoes a WHERE a.empresa_id = e.id AND a.status != 'Concluído') THEN 1 ELSE 0 END AS BOOLEAN) AS tem_acao_pendente
        
    FROM dbo.nps_clientes c
    LEFT JOIN dbo.nps_empresas e ON c.empresa_id = e.id
    LEFT JOIN dbo.nps_perfis p ON c.perfil_id = p.id
    LEFT JOIN dbo.nps_segmentos s ON c.segmento_id = s.id
    LEFT JOIN dbo.nps_cargos cg ON c.cargo_id = cg.id
    LEFT JOIN LatestDisparo d ON c.cliente_id = d.cliente_id AND d.rn = 1
    {where_sql}
    ORDER BY c.updated_at DESC
    LIMIT {int(topn)};
    """

    return read_df(sql, params)

def insert_cliente(nome: str, email: str, telefone: str, empresa_id: int, perfil_id: int, segmento_id: int, cargo_id: int, ultimo_envio: str = None, gestor: str = None):
    cliente_id = str(random.randint(100000000, 999999999))

    sql = """
    INSERT INTO dbo.nps_clientes
      (cliente_id, nome, email, telefone, empresa_id, perfil_id, segmento_id, cargo_id, gestor,
       ativo, status_envio, ultimo_envio, proximo_envio, ultimo_erro,
       created_at, updated_at)
    VALUES
      (:cliente_id, :nome, :email, :telefone, :empresa_id, :perfil_id, :segmento_id, :cargo_id, :gestor,
       1, 'Pendente', CAST(:ultimo_envio AS TIMESTAMP), CAST(GETDATE() AS DATE), NULL,
       SYSUTCDATETIME(), SYSUTCDATETIME());
    """

    engine = get_engine()
    with engine.begin() as conn:
        conn.execute(text(sql), {
            "cliente_id": cliente_id,
            "nome": (nome or "").strip(),
            "email": (email or "").strip(),
            "telefone": (telefone or "").strip() or None,
            "empresa_id": empresa_id,
            "perfil_id": perfil_id,
            "segmento_id": segmento_id,
            "cargo_id": cargo_id,
            "ultimo_envio": ultimo_envio,
            "gestor": (gestor or "").strip() or None
        })

    return cliente_id

def update_cliente(cliente_id: str, nome: str, email: str, telefone: str, empresa_id: int, perfil_id: int, segmento_id: int, cargo_id: int, ativo: bool = True):
    ativo_sql = 1 if ativo else 0
    
    # 1. Salva a edição do cliente atual
    sql = """
    UPDATE dbo.nps_clientes 
    SET 
        nome = :nome, 
        email = :email, 
        telefone = :telefone, 
        empresa_id = :empresa_id, 
        perfil_id = :perfil_id, 
        segmento_id = :segmento_id, 
        cargo_id = :cargo_id,
        ativo = :ativo,
        updated_at = SYSUTCDATETIME()
    WHERE cliente_id = :cliente_id;
    """
    
    exec_sql(sql, {
        "cliente_id": cliente_id,
        "nome": (nome or "").strip() or None,
        "email": (email or "").strip().lower(),
        "telefone": (telefone or "").strip() or None,
        "empresa_id": empresa_id,
        "perfil_id": perfil_id,
        "segmento_id": segmento_id,
        "cargo_id": cargo_id,
        "ativo": ativo_sql
    })

    # 2. 🔥 A MÁGICA DA CASCATA: Se tem empresa e segmento, padroniza todos os "irmãos"!
    if empresa_id and segmento_id:
        sql_cascata = """
        UPDATE dbo.nps_clientes 
        SET segmento_id = :segmento_id 
        WHERE empresa_id = :empresa_id;
        """
        exec_sql(sql_cascata, {
            "segmento_id": segmento_id,
            "empresa_id": empresa_id
        })

def set_ativo(cliente_id: str, ativo: int):
    sql = "UPDATE dbo.nps_clientes SET ativo = :ativo, updated_at = SYSUTCDATETIME() WHERE cliente_id = :cliente_id;"
    exec_sql(sql, {"cliente_id": cliente_id, "ativo": int(ativo)})

def delete_cliente(cliente_id: str, delete_respostas: bool = False) -> tuple[bool, str]:
    from sqlalchemy import text as _text
    engine = get_engine()
    with engine.begin() as conn:  # transação única: tudo ou nada
        params = {"cliente_id": cliente_id}
        conn.execute(_text("DELETE FROM dbo.nps_disparos WHERE cliente_id = :cliente_id"), params)
        if delete_respostas:
            conn.execute(_text("DELETE FROM dbo.nps_acoes WHERE resposta_id IN (SELECT resposta_id FROM dbo.nps_respostas WHERE cliente_id = :cliente_id)"), params)
            conn.execute(_text("DELETE FROM dbo.nps_respostas WHERE cliente_id = :cliente_id"), params)
        else:
            # mantém o histórico de respostas, apenas desvincula do cliente
            conn.execute(_text("UPDATE dbo.nps_respostas SET cliente_id = NULL WHERE cliente_id = :cliente_id"), params)
        res = conn.execute(_text("DELETE FROM dbo.nps_clientes WHERE cliente_id = :cliente_id"), params)
        if res.rowcount != 1:
            raise Exception("Cliente não encontrado.")
    return True, "Exclusão concluída ✅"

def forcar_envio_db(cliente_id: str):
    sql = """
    UPDATE dbo.nps_clientes
    SET
      ativo = 1,
      status_envio = 'Pendente',
      ultimo_envio = NULL, 
      proximo_envio = CAST(GETDATE() AS DATE),
      ultimo_erro = NULL,
      updated_at = SYSUTCDATETIME()
    WHERE cliente_id = :cliente_id;
    """
    exec_sql(sql, {"cliente_id": cliente_id})