# backend/routers/chat.py
import json
import openai
import asyncio
from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse
from sqlalchemy import text
from typing import List, Optional
from pydantic import BaseModel

# 🎯 IMPORTAÇÕES SEGURAS
from database import get_engine
from services.auth_svc import get_current_user
from services.config_svc import ia_disponivel, registrar_uso_ia

class ChatRequest(BaseModel):
    mensagem: str
    historico: Optional[List[dict]] = []

router = APIRouter(prefix="/chat", tags=["Intelligence"])

SYSTEM_PROMPT = """
Você é o ANALISTA SÊNIOR DE ESTRATÉGIA da Rakiti. Responda SEMPRE em português do Brasil, de forma simples e direta, para donos e gestores de pequenas e médias empresas.
Seu único conhecimento vem das ferramentas SQL disponibilizadas.
Use a palavra "Carteira" para o conjunto de clientes (nunca "Chapter" ou "Portfólio").

⚠️ MAPEAMENTO DE FERRAMENTA:
- Pedidos de "Visão Geral", "Carteira", "Portfólio" ou "Geral" -> use nome_empresa='Geral' e comparar=False.
- Pedidos de "Comparativo", "Evolução", "Tendência" ou "Deltas" -> use comparar=True.

🎨 REGRAS DE LAYOUT VERTICAL (OBRIGATÓRIO):

# [Nome do Cliente ou Carteira]

## 📊 Scorecard de Performance
(Apresente os dados em lista vertical para máxima clareza)

CASO A) Se for um "Snapshot 30 Dias":
**Métrica:** NPS
**Resultado:** {nps}
**Tendência:** {seta} (Se disponível)
**Volume:** {volume} respostas
**Distribuição:** {promotores} Promotores / {detratores} Detratores

CASO B) Se for um "Comparativo Trimestral":
**📈 Evolução Trimestral (QoQ):** {evolucao_nps}

---
**Trimestre Atual (Últimos 90 dias):**
- **NPS:** {nps atual}
- **Volume:** {volume atual} respostas
- **Mix:** {prom_atual} Prom. / {detr_atual} Detr.

**Trimestre Anterior (91-180 dias):**
- **NPS:** {nps anterior}
- **Volume:** {volume anterior} respostas
- **Mix:** {prom_ant} Prom. / {detr_ant} Detr.
---

## 🔍 Análise de Sentimento
- (Bullet points curtos com a síntese do qualitativo)

## 💡 Insights Estratégicos
> (Use blockquotes para recomendações de alta prioridade. Ex: "A queda no volume de respostas do cliente X sugere necessidade de reforço no engajamento da pesquisa.")

⚠️ EXCEÇÕES E DADOS PARCIAIS:
1. Se clicares em "Ver elogio/detrator" e a ferramenta retornar o texto na íntegra, imprime o comentário completo formatado em *itálico* usando um blockquote (>).
2. Se o "Comparativo Mensal" indicar "Início de tracking", explica rapidamente ao usuário que este é o primeiro mês com volume de respostas do cliente. NÃO digas "sem registros".

⚠️ REGRAS DE OURO:
1. PROIBIDO dizer "não tenho acesso". Se não houver dados, informa: "Sem registros para [Nome] nos últimos 30 dias".
2. PROIBIDO inventar dados. Se a ferramenta SQL falhar, reporta o erro técnico.
3. SENIOREITY: Mantém um tom executivo, focado em resultados e ações.
"""

# ==========================================
# 🛠️ FERRAMENTAS DO BANCO (SQL ENGINE)
# ==========================================
def db_obter_metricas_empresa(nome_empresa: str, comparar: bool = False):
    try:
        engine = get_engine()
        termos_globais = ['geral', 'todos', 'portfólio', 'portfolio', 'visão geral']
        is_geral = nome_empresa.lower() in termos_globais
        
        with engine.connect() as conn:
            filtro_empresa = "WHERE r.excluido = 0" if is_geral else "WHERE (UPPER(r.empresa) LIKE UPPER(:nome)) AND r.excluido = 0"
            params = {} if is_geral else {"nome": f"%{nome_empresa.strip()}%"}

            # 🎯 SQL TRIMESTRAL: Janelas de 0-90 dias vs 91-180 dias
            sql = text(f"""
                SELECT 
                    SUM(CASE WHEN DATEDIFF('day', created_at, GETDATE()) <= 90 THEN 1 ELSE 0 END) as total_atual,
                    SUM(CASE WHEN DATEDIFF('day', created_at, GETDATE()) <= 90 AND nota >= 9 THEN 1 ELSE 0 END) as prom_atual,
                    SUM(CASE WHEN DATEDIFF('day', created_at, GETDATE()) <= 90 AND nota <= 6 THEN 1 ELSE 0 END) as detr_atual,
                    
                    SUM(CASE WHEN DATEDIFF('day', created_at, GETDATE()) BETWEEN 91 AND 180 THEN 1 ELSE 0 END) as total_ant,
                    SUM(CASE WHEN DATEDIFF('day', created_at, GETDATE()) BETWEEN 91 AND 180 AND nota >= 9 THEN 1 ELSE 0 END) as prom_ant,
                    SUM(CASE WHEN DATEDIFF('day', created_at, GETDATE()) BETWEEN 91 AND 180 AND nota <= 6 THEN 1 ELSE 0 END) as detr_ant
                FROM dbo.nps_respostas r 
                {filtro_empresa}
            """)
            
            res = conn.execute(sql, params).mappings().first()
            
            t_atual = res['total_atual'] or 0
            t_ant = res['total_ant'] or 0

            if t_atual == 0 and t_ant == 0:
                return "Não foram encontrados dados nos últimos 180 dias."

            p_atual = res['prom_atual'] or 0
            d_atual = res['detr_atual'] or 0
            nps_atual = round(((p_atual - d_atual) / t_atual) * 100) if t_atual > 0 else 0

            if comparar:
                p_ant = res['prom_ant'] or 0
                d_ant = res['detr_ant'] or 0
                nps_ant = round(((p_ant - d_ant) / t_ant) * 100) if t_ant > 0 else 0
                
                delta = nps_atual - nps_ant
                seta = "🟢 Subida" if delta > 0 else "🔴 Queda" if delta < 0 else "🟡 Estável"
                evolucao = f"{seta} de {delta} pontos vs Trimestre Anterior"

                return json.dumps({
                    "tipo": "Comparativo Trimestral",
                    "evolucao_nps": evolucao,
                    "periodo_atual": {"nps": nps_atual, "volume": t_atual, "promotores": p_atual, "detratores": d_atual},
                    "periodo_anterior": {"nps": nps_ant, "volume": t_ant, "promotores": p_ant, "detratores": d_ant}
                })

            return json.dumps({
                "tipo": "Snapshot Trimestral",
                "nps": nps_atual,
                "volume": t_atual,
                "promotores": p_atual,
                "detratores": d_atual
            })
    except Exception as e:
        return f"Erro técnico: {str(e)}"

def db_listar_comentarios_recentes(nome_empresa: str, limite: int = 5):
    """Busca os últimos motivos qualitativos no banco"""
    try:
        engine = get_engine()
        with engine.connect() as conn:
            sql = text("""
                SELECT nota, motivo, created_at
                FROM dbo.nps_respostas 
                WHERE (empresa = :nome OR empresa_id IN (SELECT id FROM dbo.nps_empresas WHERE nome = :nome))
                  AND motivo IS NOT NULL AND motivo <> '' AND excluido = 0
                ORDER BY created_at DESC LIMIT :limite
            """)
            res = conn.execute(sql, {"nome": nome_empresa, "limite": limite}).mappings().all()
            return json.dumps([{"nota": r.nota, "comentario": r.motivo} for r in res])
    except Exception as e:
        return f"Erro ao buscar comentários: {str(e)}"
    
def db_obter_comentario_especifico(nome_empresa: str, trecho_comentario: str):
    try:
        engine = get_engine()
        # 1. Limpa reticências e aspas
        busca_limpa = trecho_comentario.replace("...", "").replace("'", "").replace('"', "").strip()
        # 2. 🎯 Pega APENAS os primeiros 20 caracteres. O suficiente para ser único, curto o suficiente para não quebrar.
        busca_segura = busca_limpa[:20] 
        
        with engine.connect() as conn:
            # 3. 🎯 Usamos UPPER e LIKE na empresa para ignorar espaços em branco
            sql = text("""
                SELECT motivo, nota, created_at 
                FROM dbo.nps_respostas 
                WHERE (UPPER(empresa) LIKE UPPER(:nome))
                  AND motivo ILIKE :busca 
                  AND excluido = 0
                ORDER BY created_at DESC LIMIT 1
            """)
            
            res = conn.execute(sql, {
                "nome": f"%{nome_empresa.strip()}%", 
                "busca": f"%{busca_segura}%"
            }).mappings().first()
            
            if res:
                return json.dumps({
                    "data_resposta": str(res['created_at'].date()),
                    "nota": res['nota'],
                    "comentario_na_integra": res['motivo']
                })
                
            return f"Não localizado. A IA deve informar que o comentário '{busca_segura}...' não foi encontrado."
    except Exception as e:
        return f"Erro na busca: {str(e)}"
    
def obter_sugestoes_dinamicas(nome_empresa: str = None):
    # 🎯 1. Limpeza do Fallback (Removido o termo 'Chapter')
    if not nome_empresa:
        return ["NPS geral da carteira", "Principais detratores", "Comparativo trimestral"]
    
    """Busca um exemplo de cada extremo para gerar tags clicáveis"""
    try:
        from database import get_engine
        from sqlalchemy import text
        
        engine = get_engine()
        with engine.connect() as conn:
            # Busca o detrator mais recente
            detrator = conn.execute(text("""
                SELECT motivo FROM dbo.nps_respostas 
                WHERE empresa = :nome AND nota <= 6 AND motivo IS NOT NULL 
                ORDER BY created_at DESC LIMIT 1
            """), {"nome": nome_empresa}).scalar()

            # Busca o promotor mais recente
            promotor = conn.execute(text("""
                SELECT motivo FROM dbo.nps_respostas 
                WHERE empresa = :nome AND nota >= 9 AND motivo IS NOT NULL 
                ORDER BY created_at DESC LIMIT 1
            """), {"nome": nome_empresa}).scalar()

            sugestoes = []
            if detrator:
                # Limita o texto para caber na tag
                sugestoes.append(f"Analisar detrator: '{detrator[:30]}...'")
            if promotor:
                sugestoes.append(f"Ver elogio: '{promotor[:30]}...'")
            
            # 🎯 2. Atualização para a Visão Trimestral (QoQ)
            sugestoes.append(f"Ver comparativo trimestral da {nome_empresa}")
            
            return sugestoes
    except:
        # Fallback em caso de erro no banco
        return ["Visão da carteira", "Alertas Críticos", "Evolução Trimestral"]
        

# ==========================================
# 🧠 ROTA PRINCIPAL: PERGUNTAR À IA
# ==========================================

@router.post("/perguntar")
async def perguntar_inteligencia(requisicao: ChatRequest, usuario = Depends(get_current_user)):
    token, msg_ia = ia_disponivel()
    if not token:
        async def _aviso():
            yield f"data: {json.dumps({'texto': msg_ia})}\n\n"
        return StreamingResponse(_aviso(), media_type="text/event-stream")
    registrar_uso_ia()
    client = openai.OpenAI(api_key=token)

    mensagens = [{"role": "system", "content": SYSTEM_PROMPT}]
    
    if requisicao.historico:
        for msg in requisicao.historico:
            mensagens.append({"role": msg["role"], "content": msg["content"]})
    
    mensagens.append({"role": "user", "content": requisicao.mensagem})

    # 🎯 2. DEFINIR AS FERRAMENTAS (TOOLS)
    ferramentas = [
        {
            "type": "function",
            "function": {
                "name": "db_obter_metricas_empresa",
                "description": "Retorna o NPS, volume e distribuição.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        # 🎯 REGRA DE EXTRAÇÃO CEGA: PROIBE DATAS NO NOME
                        "nome_empresa": {
                            "type": "string", 
                            "description": "APENAS o nome do cliente (ex: 'Farmácia São João', 'Transportes Rápido'). NUNCA inclua expressões de tempo como 'nos últimos 30 dias'."
                        },
                        "comparar": {"type": "boolean"}
                    },
                    "required": ["nome_empresa"]
                }
            }
        },
        {
            "type": "function",
            "function": {
                "name": "db_obter_comentario_especifico",
                "description": "Busca o texto completo de um elogio ou crítica.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "nome_empresa": {
                            "type": "string",
                            "description": "Apenas o nome exato do cliente."
                        },
                        "trecho_comentario": {"type": "string"}
                    },
                    "required": ["nome_empresa", "trecho_comentario"]
                }
            }
        }
    ]

    async def gerar_fluxo():
        empresa_foco = None # 🎯 Inicializamos a variável aqui
        
        try:
            # ETAPA A: Verificar se a IA quer chamar o banco
            check_tools = client.chat.completions.create(
                model="gpt-4o",
                messages=mensagens,
                tools=ferramentas,
                tool_choice="auto"
            )
        
            msg_ia = check_tools.choices[0].message
            
            if msg_ia.tool_calls:
                mensagens.append(msg_ia.model_dump(exclude_none=True))
                
                for tool_call in msg_ia.tool_calls:
                    args = json.loads(tool_call.function.arguments)
                    nome_func = tool_call.function.name 
                    
                    if "nome_empresa" in args:
                        empresa_foco = args.get("nome_empresa")
                    
                    # 🎯 ROTEADOR DE FERRAMENTAS INTELIGENTE
                    if nome_func == "db_obter_metricas_empresa":
                        resultado = db_obter_metricas_empresa(
                            nome_empresa=args.get("nome_empresa", ""),
                            comparar=args.get("comparar", False)
                        )
                    elif nome_func == "db_obter_comentario_especifico":
                        resultado = db_obter_comentario_especifico(
                            nome_empresa=args.get("nome_empresa", ""),
                            trecho_comentario=args.get("trecho_comentario", "")
                        )
                    else:
                        resultado = f"Erro: Ferramenta {nome_func} desconhecida."
                    
                    # Devolve a resposta do banco para a IA ler
                    mensagens.append({
                        "tool_call_id": tool_call.id,
                        "role": "tool",
                        "name": nome_func,
                        "content": str(resultado)
                    })

            # ETAPA B: Stream da Resposta Final
            stream = client.chat.completions.create(
                model="gpt-4o",
                messages=mensagens,
                stream=True
            )

            for chunk in stream:
                if chunk.choices[0].delta.content:
                    yield f"data: {json.dumps({'texto': chunk.choices[0].delta.content})}\n\n"

            # Se a IA não detectou empresa (ex: pergunta genérica), usamos um padrão
            if empresa_foco:
                sugestoes = obter_sugestoes_dinamicas(empresa_foco)
                yield f"data: {json.dumps({'sugestoes': sugestoes})}\n\n"

        except Exception as e:
            yield f"data: {json.dumps({'erro': str(e)})}\n\n"

    return StreamingResponse(gerar_fluxo(), media_type="text/event-stream")