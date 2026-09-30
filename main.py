import os
import io
import json
import traceback
import re
import uuid 
import secrets
import string
import random
from collections import Counter
from datetime import datetime, timedelta, timezone
from typing import Optional, List, Any
from contextlib import asynccontextmanager
import shutil
from fastapi.staticfiles import StaticFiles
from fastapi import FastAPI, Request
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.util import get_remote_address
from slowapi.errors import RateLimitExceeded
from slowapi.middleware import SlowAPIMiddleware

import pandas as pd
import bcrypt
import requests
import openai
from fastapi import FastAPI, HTTPException, File, UploadFile, Query, BackgroundTasks, Body, Depends, status, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse, RedirectResponse
from fastapi.security import OAuth2PasswordBearer
from pydantic import BaseModel, EmailStr
from passlib.context import CryptContext
from jose import jwt, JWTError, ExpiredSignatureError
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

# Importações do Agendador
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.interval import IntervalTrigger
from apscheduler.triggers.cron import CronTrigger

# Importações Locais
from database import get_engine, exec_sql
from services.mail_provider import post_email, usando_resend, provedor_email
from services.email_svc import enviar_email_recuperacao, processar_disparos_nps, validar_dominio_email, enviar_email_confirmacao, validar_senha_forte, enviar_email_senha_alterada
from services import clientes_svc, respostas_svc, dashboard_svc, importacao_svc
from services.teams_svc import enviar_resumo_matinal_gestores, enviar_alerta_tecnico_teams
from services.auth_svc import oauth2_scheme, SECRET_KEY, ALGORITHM, get_current_user, hash_password, verify_password, create_access_token, exigir_admin, exigir_manager, pwd_context
from routers import chat

# ==========================================
# ⏰ 2. LIFESPAN E SCHEDULERS
# ==========================================
scheduler = BackgroundScheduler()

def _contas_ativas():
    from database import modo_sistema as _ms
    with _ms():
        with get_engine().connect() as conn:
            return [r[0] for r in conn.execute(text("SELECT id FROM dbo.nps_contas WHERE ativo = 1 ORDER BY id"))]


def _para_cada_conta(func):
    from database import usando_conta
    for conta_id in _contas_ativas():
        with usando_conta(conta_id):
            try:
                func()
            except Exception as e:
                print(f"❌ Erro no job {getattr(func, '__name__', func)} (conta {conta_id}): {e}")


def _resumo_teams_no_horario():
    from zoneinfo import ZoneInfo
    from database import usando_conta
    agora = datetime.now(ZoneInfo("America/Sao_Paulo"))
    if agora.weekday() >= 5:
        return
    for conta_id in _contas_ativas():
        with usando_conta(conta_id):
            try:
                with get_engine().connect() as conn:
                    valor = conn.execute(text("SELECT valor FROM dbo.nps_configuracoes WHERE chave = 'teams_horario_resumo'")).scalar()
                hora = int(str(valor).split(":")[0]) if valor and ":" in str(valor) else 8
                if hora == agora.hour:
                    enviar_resumo_matinal_gestores()
            except Exception as e:
                print(f"❌ Erro no resumo do Teams (conta {conta_id}): {e}")


@asynccontextmanager
async def lifespan(app: FastAPI):
    # 1. Cria as tabelas e o Admin inicial se o banco estiver vazio
    from bootstrap_db import preparar_banco
    preparar_banco()

    # 2. Jobs rodam para cada conta (empresa cliente), com os dados isolados por conta
    scheduler.add_job(
        lambda: _para_cada_conta(processar_disparos_nps),
        IntervalTrigger(hours=6),
        id="disparo_nps_job",
        replace_existing=True
    )

    # Lembretes para quem não respondeu: uma vez por dia, em horário comercial
    from services.lembretes_svc import processar_lembretes
    scheduler.add_job(
        lambda: _para_cada_conta(processar_lembretes),
        CronTrigger(hour=10, minute=20, timezone="America/Sao_Paulo"),
        id="lembretes_job",
        replace_existing=True
    )

    # Resumo matinal do Teams: verifica de hora em hora o horário configurado em cada conta
    scheduler.add_job(
        _resumo_teams_no_horario,
        CronTrigger(minute=0),
        id="alerta_matinal_teams_job",
        replace_existing=True
    )

    if os.getenv("DESABILITAR_AGENDADOR", "").lower() in ("1", "true"):
        print("⏸️ Agendador desligado (DESABILITAR_AGENDADOR).")
        yield
        return
    scheduler.start()
    print("⏰ Agendador de tarefas (CRON) iniciado com sucesso! (NPS, lembretes e Teams)")
    yield
    scheduler.shutdown()

# ==========================================
# 🚀 3. INICIALIZAÇÃO DO APP E MIDDLEWARES
# ==========================================

# Cria a instância do Limiter baseada no IP do usuário
limiter = Limiter(key_func=get_remote_address)

app = FastAPI(
    title="Rakiti NPS API",
    description="API centralizada para gestão de NPS, Clientes e Respostas",
    version="1.0.0",
    lifespan=lifespan
)
# ==========================================
# 🚀 REGISTRO DE ROUTERS (Coloque Aqui)
# ==========================================
app.include_router(chat.router, prefix="/api")

# ==========================================
# 🔐 PROTEÇÃO GLOBAL DA API
# Toda rota /api/* exige token válido, exceto as públicas abaixo.
# Rotas administrativas sensíveis exigem perfil Admin.
# ==========================================
from fastapi.responses import JSONResponse as _JSONResponse

def _bg(func):
    """Tarefas em segundo plano rodam na mesma conta da requisição que as criou."""
    from database import conta_atual, usando_conta
    conta = conta_atual()
    def _executar(*args, **kwargs):
        with usando_conta(conta):
            return func(*args, **kwargs)
    _executar.__name__ = getattr(func, "__name__", "tarefa")
    return _executar


def _definir_conta_por_email(email: str):
    """Descobre a conta do usuário pelo e-mail (acesso de sistema) e a torna a conta ativa."""
    from database import modo_sistema as _ms, definir_conta as _dc
    if not email:
        return None
    with _ms():
        with get_engine().connect() as conn:
            conta = conn.execute(text("SELECT conta_id FROM dbo.nps_usuarios WHERE email = :e"), {"e": email.strip()}).scalar()
    if conta is not None:
        _dc(conta)
    return conta


def _definir_conta_por_dominio(email: str):
    """Para auto-cadastro: acha a ÚNICA conta que libera o domínio do e-mail."""
    from database import modo_sistema as _ms, definir_conta as _dc
    try:
        dominio = email.split("@")[1].strip().lower()
    except IndexError:
        raise HTTPException(status_code=400, detail="O formato do e-mail é inválido.")
    with _ms():
        with get_engine().connect() as conn:
            linhas = conn.execute(text("SELECT conta_id, valor FROM dbo.nps_configuracoes WHERE chave = 'dominios_permitidos'")).fetchall()
    contas = [c for c, v in linhas if dominio in [d.strip().lower() for d in str(v or "").split(",") if d.strip()]]
    if len(contas) != 1:
        raise HTTPException(status_code=403, detail="Não foi possível identificar a sua empresa pelo e-mail. Peça ao administrador da sua empresa para criar o seu acesso.")
    _dc(contas[0])
    return contas[0]


ROTAS_PUBLICAS = {
    "/api/login", "/api/register", "/api/reenviar-confirmacao", "/api/esqueci-senha",
    "/api/reset-password", "/api/auth/verificar-email", "/api/auth/sso-config",
    "/api/auth/microsoft", "/api/webhook/fillout", "/api/status", "/api/integracao/csat",
    "/api/cadastro-empresa", "/api/planos", "/api/webhook/asaas",
}
PREFIXOS_PUBLICOS = ("/api/pesquisa/",)  # formulário público de pesquisa (link com token)

def _rota_so_admin(metodo: str, caminho: str) -> bool:
    if caminho.startswith("/api/admin/") or caminho == "/api/cadastros/corrigir-historico":
        return True
    if caminho.endswith("/reset-manual"):
        return True
    if metodo == "POST" and caminho in ("/api/usuarios", "/api/configuracoes", "/api/config/email/autorizar"):
        return True
    if metodo == "PUT" and caminho == "/api/configuracoes/dominios":
        return True
    return False

@app.middleware("http")
async def exigir_autenticacao(request: Request, call_next):
    caminho = request.url.path.rstrip("/") or "/"
    if request.method == "OPTIONS" or not caminho.startswith("/api") or caminho in ROTAS_PUBLICAS \
            or caminho.startswith(PREFIXOS_PUBLICOS):
        return await call_next(request)
    auth = request.headers.get("authorization", "")
    if not auth.lower().startswith("bearer "):
        return _JSONResponse(status_code=401, content={"detail": "Não autenticado."})
    try:
        payload = jwt.decode(auth[7:], SECRET_KEY, algorithms=[ALGORITHM])
        if not payload.get("sub"):
            raise JWTError("sem sub")
    except (ExpiredSignatureError, JWTError):
        return _JSONResponse(status_code=401, content={"detail": "Sessão expirada."})
    if payload.get("conta_id") is None:
        return _JSONResponse(status_code=401, content={"detail": "Sessão expirada. Faça login novamente."})
    if caminho.startswith("/api/superadmin/") and not _eh_superadmin(payload.get("sub")):
        return _JSONResponse(status_code=403, content={"detail": "Acesso restrito à administração da plataforma."})
    if _rota_so_admin(request.method, caminho) and payload.get("tipo") != "Admin":
        return _JSONResponse(status_code=403, content={"detail": "Acesso negado. Apenas Administradores."})
    from database import definir_conta as _dc
    _dc(payload["conta_id"])
    return await call_next(request)


def _eh_superadmin(email) -> bool:
    lista = [e.strip().lower() for e in os.getenv("SUPERADMIN_EMAILS", "").split(",") if e.strip()]
    return bool(email) and email.strip().lower() in lista

app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)
app.add_middleware(SlowAPIMiddleware)

origem_oficial = os.getenv("FRONTEND_URL", "http://localhost:5173").rstrip("/")

origens_permitidas = [
    origem_oficial
]

if os.getenv("AMBIENTE") == "dev":
    origens_permitidas.extend([
        "http://localhost:5173",
        "http://127.0.0.1:5173"
    ])

# 3. Aplicação do Filtro na API (Rigoroso)
app.add_middleware(
    CORSMiddleware,
    allow_origins=origens_permitidas,
    allow_credentials=True,
    allow_methods=["GET", "POST", "PUT", "DELETE", "OPTIONS", "PATCH"],
    allow_headers=["*"],
)

# ==========================================
# 🥇 1. WEBHOOKS (PADRÃO OFICIAL)
# ==========================================

@app.post("/api/webhook/fillout")
async def receber_webhook_fillout(request: Request, background_tasks: BackgroundTasks, token: str = ""):
    """Recebe as respostas do Fillout. Exige o token secreto da conta (?token=...)."""
    token = (token or request.headers.get("x-webhook-token", "")).strip()
    conta_id = _conta_por_webhook_token(token)
    if conta_id is None:
        raise HTTPException(status_code=401, detail="Token do webhook ausente ou inválido.")
    try:
        payload = await request.json()
        from services.webhook_svc import processar_webhook_background
        background_tasks.add_task(processar_webhook_background, payload, conta_id)
        return {"status": "success", "message": "Recebido"}
        
    except Exception as e:
        print(f"❌ Erro ao receber webhook: {e}")
        return {"status": "error", "message": "Falha na leitura"}


def _conta_por_webhook_token(token: str):
    if not token or len(token) < 20:
        return None
    from database import modo_sistema as _ms
    with _ms():
        with get_engine().connect() as conn:
            return conn.execute(text("SELECT id FROM dbo.nps_contas WHERE webhook_token = :t AND ativo = 1"), {"t": token}).scalar()


@app.get("/api/webhook/fillout")
async def status_webhook_fillout(token: str = ""):
    """Healthcheck para o botão do Frontend (valida o token)"""
    if _conta_por_webhook_token(token) is None:
        raise HTTPException(status_code=401, detail="Token do webhook ausente ou inválido.")
    return {"status": "success", "message": "🟢 Webhook ativo e protegido por token."}

from fastapi import Depends, HTTPException, status
from jose import jwt, JWTError, ExpiredSignatureError

# ==========================================
# 📦 5. SCHEMAS (Pydantic Models)
# Validam os dados que chegam do Frontend
# ==========================================

class AcaoCriar(BaseModel):
    resposta_id: Optional[str] = None 
    empresa_id: Optional[int] = None  
    gestor_id: Optional[int] = None
    titulo: str
    descricao: Optional[str] = ""
    prioridade: Optional[str] = "Alta"
    prazo_limite: Optional[str] = None
    resolucao: Optional[str] = None

class AcaoAtualizar(BaseModel):
    status: Optional[str] = None
    prioridade: Optional[str] = None
    descricao: Optional[str] = None
    prazo_limite: Optional[str] = None
    gestor_id: Optional[int] = None
    empresa_id: Optional[int] = None
    resolucao: Optional[str] = None

class BasicoSchema(BaseModel):
    nome: str

class RespostaUpdate(BaseModel):
    nota: int
    categoria: Optional[str] = ""
    motivo: Optional[str] = ""
    canal: Optional[str] = ""
    expectativas: Optional[str] = ""
    o_que_faltava: Optional[str] = ""

class ConfigEmailRequest(BaseModel):
    tenant_id: str
    client_id: str
    client_secret: str
    email_remetente: EmailStr

class AutorizarEmailRequest(BaseModel):
    code: str
    redirect_uri: str

class ConfigEmailSchema(BaseModel):
    tenant_id: Optional[str] = ""
    client_id: Optional[str] = ""
    client_secret: Optional[str] = ""
    email_remetente: Optional[str] = ""
    base_url_frontend: Optional[str] = "http://localhost:5173"
    robo_ativo: bool = False
    envios_ativos: Optional[bool] = True
    sso_microsoft_ativo: Optional[bool] = False  # 👈 NOME CORRIGIDO AQUI

class RegistroRequest(BaseModel):
    nome: str
    email: str
    password: str
    url_plataforma: str

class ResetPasswordRequest(BaseModel):
    token: str
    nova_senha: str

class LoginRequest(BaseModel):
    email: str
    password: str
    remember: bool = False

class EsqueciSenhaRequest(BaseModel):
    email: str

class AlterarSenhaRequest(BaseModel):
    senha_atual: str
    nova_senha: str

class UsuarioCreate(BaseModel):
    nome: str
    email: str
    password: str
    cargo: str = "Analista"

class LoteEnvio(BaseModel):
    cliente_ids: List[str]

class SettingUpdate(BaseModel):
    valor: bool

class EmpresaSchema(BaseModel):
    nome: str
    segmento: Optional[str] = None
    valor_contrato: Optional[float] = 0.0
    gestor: Optional[str] = None
    gestor_id: Optional[int] = None 
    companhia_id: Optional[int] = None 

class ClienteCreate(BaseModel):
    nome: str
    email: str
    telefone: Optional[str] = ""
    empresa_id: Optional[int] = None 
    perfil_id: Optional[int] = None 
    segmento_id: Optional[int] = None 
    cargo_id: Optional[int] = None 
    gestor: Optional[str] = ""

class ClienteUpdate(BaseModel):
    nome: str
    email: str
    telefone: Optional[str] = ""
    empresa_id: Optional[int] = None 
    perfil_id: Optional[int] = None 
    segmento_id: Optional[int] = None 
    cargo_id: Optional[int] = None 
    gestor: Optional[str] = ""
    ativo: Optional[bool] = True

class StatusUpdate(BaseModel):
    ativo: bool

class ConfigItem(BaseModel):
    chave: str
    valor: str

class GestorSchema(BaseModel):
    nome: str
    papel: Optional[str] = ""
    email: Optional[str] = ""
    teams_webhook: Optional[str] = ""
    avatar: Optional[str] = None

class AlertaGestorRequest(BaseModel):
    empresa: str
    gestor: str
    nps: int

class ReportEmailPayload(BaseModel):
    emails: List[str]
    periodo: str
    resumo_ia: str
    foco: str
    prioridade: str

class EmpresaPayload(BaseModel):
    nome: str
    segmento: Optional[str] = None
    valor_contrato: Optional[float] = 0.0
    gestor: Optional[str] = None
    gestor_id: Optional[int] = None

class IntegracoesUpdate(BaseModel):
    webhook_global: Optional[str] = None
    webhook_tecnico: Optional[str] = None

class RegrasNegocioConfig(BaseModel):
    scheduler_hora_inicio: str = "09:00"
    scheduler_horas: int = 6
    sla_detrator_dias: int = 2
    sla_neutro_dias: int = 5
    sla_promotor_dias: int = 7
    recorrencia_dias: int = 90
    fillout_campos: str = "clienteid,email,nome,empresa,empresa_id"
    survey_url: Optional[str] = ""
    formulario_tipo: str = "proprio"
    pergunta_nps: Optional[str] = ""
    pergunta_csat: Optional[str] = ""
    email_template_html: Optional[str] = ""
    email_agradecimento_promotor: Optional[str] = ""
    email_agradecimento_neutro: Optional[str] = ""
    email_agradecimento_detrator: Optional[str] = ""
    email_template_lembrete_1: Optional[str] = ""
    email_template_lembrete_2: Optional[str] = ""
    email_template_lembrete_3: Optional[str] = ""
    teams_horario_resumo: str = "08:00"
    lembrete_qtd_maxima: int = 3
    lembrete_dias_1: int = 3
    lembrete_dias_2: int = 7
    lembrete_dias_3: int = 15
    robo_ativo: bool = False

class TesteTemplatePayload(BaseModel):
    email_destino: str
    html_content: str
    categoria: str # 'promotor', 'neutro', 'detrator'

class TesteWebhookPayload(BaseModel):
    webhook_url: str

class PermissaoUpdate(BaseModel):
    perfil: str
    chaves: List[str]

class RespostaManual(BaseModel):
    cliente_id: str
    nota: int
    motivo: Optional[str] = ""
    canal: str = "Manual"

class MicrosoftAuthPayload(BaseModel):
    access_token: str

class DominiosUpdate(BaseModel):
    dominios: str

class ReenviarEmailReq(BaseModel):
    email: str

class ReenviarEmailRequest(BaseModel):
    email: str

class SegurancaConfig(BaseModel):
    tempo_minutos: int

# ==========================================
# 🔗 ROTAS DE INTEGRAÇÕES (TEAMS / FILLOUT)
# ==========================================

@app.get("/api/configuracoes/integracoes")
def get_integracoes(usuario_email: str = Depends(get_current_user)):
    """Busca as configurações atuais de integração (Protegido)"""
    try:
        from database import get_engine
        from sqlalchemy import text
        
        engine = get_engine()
        with engine.connect() as conn:
            query = text("SELECT chave, valor FROM dbo.nps_configuracoes WHERE chave IN ('teams_webhook_url', 'teams_alerts_webhook')")
            rows = conn.execute(query).fetchall()
            
            config = {row.chave: row.valor for row in rows}
            
            return {
                "webhook_global": config.get("teams_webhook_url", ""),
                "webhook_tecnico": config.get("teams_alerts_webhook", "")
            }
    except Exception as e:
        import traceback
        print(traceback.format_exc())
        raise HTTPException(status_code=500, detail="Erro ao carregar integrações")

@app.put("/api/configuracoes/integracoes")
def update_integracoes(config: IntegracoesUpdate, usuario_email: str = Depends(get_current_user)):
    """Atualiza ou cria as chaves de integração no banco (Protegido)"""
    try:
        from database import get_engine
        from sqlalchemy import text
        
        engine = get_engine()
        with engine.begin() as conn:
            # Lógica de Upsert otimizada (incluindo o updated_at da sua rota antiga)
            sql_upsert = text("""
                INSERT INTO dbo.nps_configuracoes (chave, valor, updated_at)
                    VALUES (:chave, :valor, CURRENT_TIMESTAMP)
                    ON CONFLICT (conta_id, chave) DO UPDATE SET valor = EXCLUDED.valor, updated_at = CURRENT_TIMESTAMP
            """)
            
            # Grava o Webhook Global
            if config.webhook_global is not None:
                conn.execute(sql_upsert, {"chave": "teams_webhook_url", "valor": config.webhook_global})
            
            # Grava o Webhook Técnico
            if config.webhook_tecnico is not None:
                conn.execute(sql_upsert, {"chave": "teams_alerts_webhook", "valor": config.webhook_tecnico})
                
        return {"status": "success", "message": "Integrações atualizadas com sucesso!"}
    except Exception as e:
        import traceback
        print(traceback.format_exc())
        raise HTTPException(status_code=500, detail="Erro ao gravar integrações")
    
@app.get("/api/config/regras")
def obter_regras(usuario_email: str = Depends(get_current_user)):
    try:
        engine = get_engine()
        with engine.connect() as conn:
            sql = text("SELECT chave, valor FROM dbo.nps_configuracoes")
            result = conn.execute(sql).fetchall()
            
            configuracoes = {linha[0]: linha[1] for linha in result}
            
            if not configuracoes:
                return {"recorrencia_dias": 90}
                
            return configuracoes
            
    except Exception as e:
        print(f"Erro ao carregar regras: {e}")
        raise HTTPException(status_code=500, detail="Erro ao carregar configurações.")

@app.post("/api/config/regras")
def salvar_regras(payload: RegrasNegocioConfig, usuario_email: str = Depends(get_current_user)):
    payload.survey_url = (payload.survey_url or "").strip()
    payload.formulario_tipo = "externo" if payload.formulario_tipo == "externo" else "proprio"
    if payload.formulario_tipo == "externo" and not payload.survey_url.startswith("https://"):
        raise HTTPException(status_code=400, detail="Para usar um formulário externo, informe o link (começando com https://).")
    if payload.survey_url and not payload.survey_url.startswith("https://"):
        raise HTTPException(status_code=400, detail="O link do formulário de pesquisa precisa começar com https://")
    try:
        engine = get_engine()
        with engine.begin() as conn:
            # Busca o ID do usuário logado para o log
            uid = conn.execute(text("SELECT usuario_id FROM dbo.nps_usuarios WHERE email = :e"), {"e": usuario_email}).scalar()

            # --- AUDITORIA: VERIFICA SE O ROBÔ LIGOU OU DESLIGOU ---
            estado_robo_antigo = conn.execute(text("SELECT valor FROM dbo.nps_configuracoes WHERE chave = 'robo_ativo'")).scalar()
            novo_robo_str = 'true' if payload.robo_ativo else 'false'

            # Se o estado for diferente do que estava no banco, grava o log!
            if str(estado_robo_antigo).lower() != novo_robo_str:
                registrar_log(
                    acao="CONFIG_ROBO",
                    mensagem=f"O usuário {'ATIVOU' if payload.robo_ativo else 'DESATIVOU'} o Robô Automático (Background).",
                    nivel="WARN",
                    usuario_id=uid
                )

            # Salva todas as regras no banco (incluindo o robo_ativo)
            configuracoes = payload.dict()
            sql = text("""
                INSERT INTO dbo.nps_configuracoes (chave, valor, updated_at)
                    VALUES (:chave, :valor, CURRENT_TIMESTAMP)
                    ON CONFLICT (conta_id, chave) DO UPDATE SET valor = EXCLUDED.valor, updated_at = CURRENT_TIMESTAMP
            """)
            
            for chave, valor in configuracoes.items():
                if isinstance(valor, bool):
                    valor_string = 'true' if valor else 'false'
                else:
                    valor_string = str(valor) if valor is not None else ""
                
                conn.execute(sql, {"chave": chave, "valor": valor_string})
            
        return {"message": "Regras de negócio salvas com sucesso!"}
    except Exception as e:
        print(f"Erro ao salvar regras chave-valor: {e}")
        raise HTTPException(status_code=500, detail=f"Erro interno: {str(e)}")
    
@app.post("/api/config/testar-template")
def testar_template_html(payload: TesteTemplatePayload, usuario_email: str = Depends(get_current_user)):
    """Recebe um HTML do frontend e envia um e-mail de teste instantâneo"""
    try:
        from services.email_svc import get_valid_access_token
        import requests

        access_token = get_valid_access_token()
        if not access_token:
            raise HTTPException(status_code=400, detail="A conexão com o e-mail não está ativa. Autorize o Microsoft Graph primeiro.")

        if not payload.html_content:
            raise HTTPException(status_code=400, detail="A caixa de texto do HTML está vazia.")

        if payload.categoria == 'convite':
            from services.pesquisa_svc import botoes_nps_html
            assunto_teste = "[Rakiti Teste] Preview do Convite NPS"
            html_pronto = payload.html_content.replace("{nome}", "Maria (Teste)") \
                                              .replace("{empresa}", "Empresa Fictícia S/A") \
                                              .replace("{botoes_nota}", botoes_nps_html("https://rakiti.com/r/exemplo")) \
                                              .replace("{survey_url}", "https://rakiti.com/r/exemplo")
        else:
            assunto_teste = f"[Rakiti Teste] Preview do Layout — {payload.categoria.capitalize()}"
            nota_teste = "10" if payload.categoria == 'promotor' else "7" if payload.categoria == 'neutro' else "3"
            motivo_teste = "A equipe foi fantástica, mas acho que o portal poderia ser mais intuitivo."
            exp_teste = "Sim, o atendimento atendeu às expectativas."
            falta_teste = "Faltou apenas um manual de usuário mais detalhado."
            
            html_pronto = payload.html_content.replace("{nome}", "João (Teste)") \
                                              .replace("{empresa}", "Empresa Fictícia S/A") \
                                              .replace("{nota}", nota_teste) \
                                              .replace("{motivo}", motivo_teste) \
                                              .replace("{expectativas}", exp_teste) \
                                              .replace("{o_que_faltava}", falta_teste)

        msg_payload = {
            "message": {
                "subject": assunto_teste, 
                "body": {"contentType": "HTML", "content": html_pronto},
                "toRecipients": [{"emailAddress": {"address": payload.email_destino}}]
            },
            "saveToSentItems": False
        }

        headers = {
            "Authorization": f"Bearer {access_token}",
            "Content-Type": "application/json"
        }

        res = post_email("https://graph.microsoft.com/v1.0/me/sendMail", headers=headers, json=msg_payload)
        if res.status_code not in (200, 202):
            raise Exception(res.text)

        return {"status": "success", "message": "E-mail de teste despachado!"}

    except Exception as e:
        print(f"❌ Erro ao enviar e-mail de teste: {e}")
        raise HTTPException(status_code=500, detail=str(e))
    
# ==========================================
# 🤖 AUTENTICACAO (Login, Registros)
# ==========================================

@app.post("/api/login")
@limiter.limit("5/minute") # 🛡️ Limite de 5 tentativas de login por minuto
async def login(requisicao: LoginRequest, request: Request):
    try:
        if _definir_conta_por_email(requisicao.email) is None:
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Este e-mail não está cadastrado na plataforma.")
        engine = get_engine()
        with engine.connect() as conn:
            validar_dominio_email(requisicao.email, conn)
            
            query = text("""
                SELECT usuario_id, nome, email, senha_hash, cargo, tipo, ativo, avatar_url, email_verificado
                FROM dbo.nps_usuarios 
                WHERE email = :email
            """)
            resultado = conn.execute(query, {"email": requisicao.email}).mappings().first()

            if not resultado:
                raise HTTPException(
                    status_code=status.HTTP_401_UNAUTHORIZED, 
                    detail="Este e-mail não está cadastrado na plataforma."
                )

            if not resultado["email_verificado"]:
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN, 
                    detail="O seu e-mail ainda não foi verificado. Por favor, confirme a sua conta através do link enviado para o seu e-mail."
                )

            ativo_val = str(resultado["ativo"]).strip().lower()
            if ativo_val not in ['1', 'true']:
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN, 
                    detail="A sua conta está inativa ou aguarda aprovação do administrador."
                )

            try:
                senha_correta = bcrypt.checkpw(
                    requisicao.password.encode('utf-8'), 
                    resultado["senha_hash"].encode('utf-8')
                )
            except Exception as e:
                enviar_alerta_tecnico_teams(f"Erro Crítico no Bcrypt durante o Login: {str(e)}")
                print(f"Erro Bcrypt: {e}")
                raise HTTPException(
                    status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, 
                    detail="Erro na encriptação. Contacte o suporte técnico."
                )

            if not senha_correta:
                raise HTTPException(
                    status_code=status.HTTP_401_UNAUTHORIZED, 
                    detail="A senha digitada está incorreta."
                )

            user_agent = request.headers.get("user-agent", "Dispositivo Desconhecido")
            ip_address = request.client.host if request.client else "IP Desconhecido"
            
            tipo_disp = "Desktop/Browser"
            if any(x in user_agent for x in ["Mobile", "iPhone", "Android"]):
                tipo_disp = "Mobile"
            elif "Mac OS" in user_agent:
                tipo_disp = "Mac/Apple"
            elif "Windows" in user_agent:
                tipo_disp = "Windows/PC"
                
            dispositivo_amigavel = f"{tipo_disp} • {user_agent[:30]}..."

            check_sessao = conn.execute(text("""
                SELECT id FROM dbo.nps_sessoes_ativas 
                WHERE usuario_id = :uid AND ip_address = :ip AND dispositivo = :disp AND revogado = 0
            """), {
                "uid": resultado["usuario_id"],
                "ip": ip_address,
                "disp": dispositivo_amigavel
            }).fetchone()

            agora_utc = datetime.now(timezone.utc)

            if check_sessao:
                conn.execute(text("""
                    UPDATE dbo.nps_sessoes_ativas 
                    SET criado_em = :agora 
                    WHERE id = :sid
                """), {"agora": agora_utc, "sid": check_sessao.id})
            else:
                conn.execute(text("""
                    INSERT INTO dbo.nps_sessoes_ativas (usuario_id, dispositivo, ip_address, localizacao, criado_em, revogado)
                    VALUES (:uid, :disp, :ip, 'Detectado Automaticamente', :agora, 0)
                """), {
                    "uid": resultado["usuario_id"],
                    "disp": dispositivo_amigavel,
                    "ip": ip_address,
                    "agora": agora_utc
                })
            
            conn.execute(text("""
                UPDATE dbo.nps_usuarios 
                SET ultimo_acesso = :agora
                WHERE usuario_id = :uid
            """), {
                "agora": agora_utc,
                "uid": resultado["usuario_id"]
            })
            
            resultado_tempo = conn.execute(text(
                "SELECT valor FROM dbo.nps_configuracoes WHERE chave = 'sessao_expiracao_minutos'"
            )).scalar()
            
            tempo_minutos = int(resultado_tempo) if resultado_tempo and str(resultado_tempo).isdigit() else 60

            conn.commit() 

            # 🎯 1. Define o delta correto
            expires_delta = timedelta(days=30) if requisicao.remember else timedelta(minutes=tempo_minutos)

            # 🎯 2. Usa o delta no cálculo da expiração (em vez das 8 horas fixas)
            expire = datetime.utcnow() + expires_delta 

            from database import conta_atual as _ca
            to_encode = {
                "sub": resultado["email"],
                "exp": expire,
                "tipo": resultado["tipo"],
                "conta_id": _ca(),
                "superadmin": _eh_superadmin(resultado["email"])
            }

            access_token = jwt.encode(to_encode, SECRET_KEY, algorithm=ALGORITHM)

            avatar_final = ""
            if resultado.get("avatar_url"):
                base_url = str(request.base_url).rstrip("/")
                db_path = resultado["avatar_url"]
                avatar_final = db_path if db_path.startswith('http') else f"{base_url}{db_path}"

            sql_perm = text("SELECT chave FROM dbo.nps_permissoes WHERE perfil = :perfil")
            res_perm = conn.execute(sql_perm, {"perfil": resultado["tipo"]}).fetchall()
            
            lista_permissoes = [row.chave for row in res_perm]

            return {
                "access_token": access_token,
                "token_type": "bearer",
                "nome": resultado["nome"],
                "email": resultado["email"],
                "cargo": resultado["cargo"],
                "tipo": resultado["tipo"],
                "permissoes": lista_permissoes,
                "avatar_url": avatar_final,
                "superadmin": _eh_superadmin(resultado["email"])
            }

    except HTTPException:
        raise
    except Exception as e:
        enviar_alerta_tecnico_teams(f"Falha Crítica no Login (Banco Offline?): {str(e)}")
        print(traceback.format_exc())
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, 
            detail="Erro interno no servidor. A equipe técnica já foi notificada."
        )
    
@app.post("/api/reenviar-confirmacao")
@limiter.limit("3/minute") # 🛡️ Impede flood de e-mails
async def reenviar_confirmacao(
    req: ReenviarEmailRequest, 
    request: Request, 
    background_tasks: BackgroundTasks
):
    try:
        _definir_conta_por_email(req.email)
        engine = get_engine()
        with engine.connect() as conn:

            query = text("SELECT usuario_id, nome, email, ativo, email_verificado FROM dbo.nps_usuarios WHERE email = :email")
            user = conn.execute(query, {"email": req.email.strip()}).mappings().first()

            if not user:
                raise HTTPException(status_code=404, detail="E-mail não encontrado no sistema.")
            
            if user['ativo'] == True or user['ativo'] == 1:
                raise HTTPException(status_code=400, detail="Esta conta já está ativa e aprovada. Tente fazer login.")

            if user['email_verificado'] == True or user['email_verificado'] == 1: 
                raise HTTPException(status_code=400, detail="O seu e-mail já foi confirmado! Agora basta aguardar a aprovação de um Administrador no painel.")
            
            url_backend = f"{request.url.scheme}://{request.url.netloc}"
            from services.email_svc import enviar_email_confirmacao
            
            background_tasks.add_task(_bg(enviar_email_confirmacao), user['email'], SECRET_KEY, ALGORITHM, url_backend)
            
            return {"mensagem": "E-mail de confirmação reenviado com sucesso!"}
            
    except HTTPException:
        raise
    except Exception as e:
        print(f"Erro ao reenviar e-mail: {e}")
        raise HTTPException(status_code=500, detail="Erro interno ao tentar reenviar o e-mail.")
    
from fastapi.responses import RedirectResponse

@app.get("/api/auth/verificar-email")
def verificar_email(token: str):
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        email = payload.get("sub")
        url_origem = payload.get("origin")
        
        if not email or not url_origem:
            raise HTTPException(status_code=400, detail="Token inválido")
        _definir_conta_por_email(email)

        engine = get_engine()
        with engine.begin() as conn:
            conn.execute(
                text("UPDATE dbo.nps_usuarios SET email_verificado = 1, ativo = CASE WHEN tipo = 'Admin' THEN ativo ELSE 0 END WHERE email = :email"),
                {"email": email}
            )
            print(f"✅ Usuário {email} verificado com sucesso.")

        return RedirectResponse(url=f"{url_origem.rstrip('/')}/login?status=confirmado")

    except (ExpiredSignatureError, JWTError):
        return RedirectResponse(url="/login?status=erro")

@app.get("/api/auth/sso-config")
def get_sso_config():
    # (Mantido igual)
    try:
        from sqlalchemy import text
        engine = get_engine()
        with engine.connect() as conn:
            sso_check = conn.execute(text("SELECT valor FROM dbo.nps_configuracoes WHERE chave = 'sso_microsoft_ativo'")).scalar()
            email_cfg = conn.execute(text("SELECT tenant_id, client_id FROM dbo.nps_configuracoes_email LIMIT 1")).mappings().first()
            valor_banco = str(sso_check).strip().lower() if sso_check else 'false'
            is_ativo = valor_banco in ['true', '1', 't', 'y', 'sim']
            client_id = email_cfg.get("client_id") if email_cfg else None
            
            if not is_ativo: return {"sso_ativo": False, "motivo": "desligado_no_banco"}
            if not client_id or str(client_id).strip() == "": return {"sso_ativo": False, "motivo": "falta_client_id"}
                
            return {"sso_ativo": True, "tenant_id": str(email_cfg.get("tenant_id", "")).strip(), "client_id": str(client_id).strip()}
    except Exception as e:
        return {"sso_ativo": False, "erro": str(e)}

@app.post("/api/auth/microsoft")
@limiter.limit("5/minute") # 🛡️ Limite para tentativas de quebra de token
async def login_microsoft(payload: MicrosoftAuthPayload, request: Request):
    print("\n=============================================")
    print(" 🚨 ALERTA: A ROTA DA MICROSOFT FOI CHAMADA!")
    print("=============================================\n")
    try:
        engine = get_engine()
        with engine.begin() as conn: 
            sso_check = conn.execute(text("SELECT valor FROM dbo.nps_configuracoes WHERE chave = 'sso_microsoft_ativo'")).scalar()
            if not sso_check or str(sso_check).lower() != 'true':
                raise HTTPException(status_code=403, detail="O Login com Microsoft está desativado pelo administrador.")

            headers = {'Authorization': f'Bearer {payload.access_token}'}
            graph_response = requests.get('https://graph.microsoft.com/v1.0/me', headers=headers)
            
            if graph_response.status_code != 200:
                raise HTTPException(status_code=401, detail="Token da Microsoft inválido ou expirado.")
                
            microsoft_user = graph_response.json()
            user_email = (microsoft_user.get('mail') or microsoft_user.get('userPrincipalName') or "").lower()

            validar_dominio_email(user_email, conn)

            user_db = conn.execute(text("""
                SELECT usuario_id, nome, email, cargo, tipo, ativo 
                FROM dbo.nps_usuarios 
                WHERE email = :email
            """), {"email": user_email}).mappings().first()
            
            if not user_db:
                raise HTTPException(status_code=403, detail=f"O e-mail corporativo '{user_email}' não está cadastrado. Solicite a criação da sua conta ao administrador do sistema.")
                
            if not user_db["ativo"]:
                raise HTTPException(status_code=403, detail="A sua conta está temporariamente desativada.")

            agora_utc = datetime.now(timezone.utc)
            resultado_tempo = conn.execute(text("SELECT valor FROM dbo.nps_configuracoes WHERE chave = 'sessao_expiracao_minutos'")).scalar()
            tempo_minutos = int(resultado_tempo) if resultado_tempo and str(resultado_tempo).isdigit() else 60
            
            expire = agora_utc + timedelta(minutes=tempo_minutos)
            from database import conta_atual as _ca
            to_encode = {"sub": user_db["email"], "exp": expire, "tipo": user_db["tipo"], "conta_id": _ca()}
            access_token = jwt.encode(to_encode, SECRET_KEY, algorithm=ALGORITHM)

            ip_usuario = request.client.host
            user_agent = request.headers.get("user-agent", "Desconhecido")
            novo_token_id = str(uuid.uuid4()) 
            
            conn.execute(text("""
                INSERT INTO dbo.nps_sessoes_ativas 
                (usuario_id, token_id, dispositivo, ip_address, localizacao, criado_em, ultima_atividade, revogado) 
                VALUES (:uid, :tid, :disp, :ip, 'Detectado Automaticamente', :agora, :agora, 0)
            """), {"uid": user_db["usuario_id"], "tid": novo_token_id, "ip": ip_usuario, "disp": user_agent, "agora": agora_utc})

            sql_perm = text("SELECT chave FROM dbo.nps_permissoes WHERE perfil = :perfil")
            res_perm = conn.execute(sql_perm, {"perfil": user_db["tipo"]}).fetchall()
            lista_permissoes = [row.chave for row in res_perm]
            
            conn.execute(text("UPDATE dbo.nps_usuarios SET ultimo_acesso = :agora WHERE usuario_id = :uid"), {"agora": agora_utc, "uid": user_db["usuario_id"]})
            
            registrar_log(acao="LOGIN_SSO", mensagem=f"Acesso via Microsoft Entra ID (SSO) realizado com sucesso.", nivel="INFO", usuario_id=user_db["usuario_id"])

            return {
                "access_token": access_token, "token_type": "bearer", "nome": user_db["nome"],
                "email": user_db["email"], "cargo": user_db["cargo"], "tipo": user_db["tipo"],
                "permissoes": lista_permissoes, "avatar": user_db.get("avatar_url") or "",
            }

    except HTTPException: raise
    except Exception as e:
        print(f"Erro Auth Microsoft: {e}")
        raise HTTPException(status_code=500, detail="Erro interno no servidor de autenticação.")
    
@app.post("/api/register")
@limiter.limit("3/minute")
def registrar_usuario(
    requisicao: RegistroRequest, 
    background_tasks: BackgroundTasks,
    request: Request
):
    validar_senha_forte(requisicao.password)
    engine = get_engine()
    _definir_conta_por_dominio(requisicao.email)
    
    with engine.begin() as conn:
        validar_dominio_email(requisicao.email, conn)
        
        query_check = text("SELECT usuario_id FROM dbo.nps_usuarios WHERE email = :email")
        if conn.execute(query_check, {"email": requisicao.email}).fetchone():
            raise HTTPException(status_code=400, detail="Este e-mail já possui uma conta associada.")
        
        senha_hash = bcrypt.hashpw(requisicao.password.encode('utf-8'), bcrypt.gensalt()).decode('utf-8')
        
        query_insert = text("""
            INSERT INTO dbo.nps_usuarios (nome, email, senha_hash, cargo, ativo, tipo)
            VALUES (:nome, :email, :senha_hash, 'Analista', 0, 'Usuário')
        """)
        
        conn.execute(query_insert, {
            "nome": requisicao.nome,
            "email": requisicao.email,
            "senha_hash": senha_hash
        })
        
        url_frontend = requisicao.url_plataforma 
            
        url_backend = f"{request.url.scheme}://{request.url.netloc}" 

        background_tasks.add_task(_bg(enviar_email_confirmacao), 
            requisicao.email, 
            requisicao.nome,
            SECRET_KEY, 
            ALGORITHM, 
            url_frontend, 
            url_backend 
        )
            
        return {"mensagem": "Conta solicitada! Verifique seu e-mail para confirmar o endereço."}

@app.post("/api/reset-password")
@limiter.limit("3/minute") # 🛡️ Protege a rota final de reset
async def resetar_senha(
    req: ResetPasswordRequest, 
    request: Request, 
    background_tasks: BackgroundTasks
):
    from database import get_engine 
    engine = get_engine()
    
    try:
        try:
            # 1. Valida se o Token (o link do e-mail) é verdadeiro e está no prazo
            payload = jwt.decode(req.token, SECRET_KEY, algorithms=[ALGORITHM])
            email_usuario = payload.get("sub")
            tipo_token = payload.get("tipo")
            
            if email_usuario is None or tipo_token != "reset":
                raise HTTPException(status_code=400, detail="Token inválido.")
            _definir_conta_por_email(email_usuario)
        except JWTError:
            raise HTTPException(status_code=400, detail="O link de recuperação expirou ou é inválido.")

        validar_senha_forte(req.nova_senha)

        # 3. Se a senha for forte, continua para a encriptação
        senha_encriptada = hash_password(req.nova_senha)
        
        with engine.begin() as conn:
            query_update = text("""
                UPDATE dbo.nps_usuarios 
                SET senha_hash = :senha_hash
                WHERE email = :email
            """)
            resultado = conn.execute(query_update, {
                "senha_hash": senha_encriptada, 
                "email": email_usuario
            })
            
            if resultado.rowcount == 0:
                raise HTTPException(status_code=404, detail="Usuário não encontrado.")
            
            res_user = conn.execute(
                text("SELECT nome FROM dbo.nps_usuarios WHERE email = :email"),
                {"email": email_usuario}
            ).mappings().first()
            
            nome_usuario = res_user['nome'] if res_user else "Usuário"

        from services.email_svc import enviar_email_senha_alterada
        background_tasks.add_task(_bg(enviar_email_senha_alterada), email_usuario, nome_usuario)
            
        return {"status": "success", "message": "Senha alterada com sucesso!"}
            
    except HTTPException: raise
    except Exception as e:
        enviar_alerta_tecnico_teams(f"Falha ao atualizar a Hash de Senha no BD: {str(e)}")
        print(f"❌ Erro ao redefinir a senha no banco: {e}")
        raise HTTPException(status_code=500, detail="Erro interno ao salvar a nova senha.")

@app.post("/api/esqueci-senha")
@limiter.limit("3/minute")
async def solicitar_recuperacao(requisicao: EsqueciSenhaRequest, request: Request, background_tasks: BackgroundTasks):
    engine = get_engine()
    email_limpo = requisicao.email.strip().lower()
    _definir_conta_por_email(email_limpo)
    
    try:
        with engine.connect() as conn:
            # 🎯 CORREÇÃO: Adicionado 'nome' no SELECT
            query = text("""
                SELECT email, nome 
                FROM dbo.nps_usuarios 
                WHERE LOWER(LTRIM(RTRIM(email))) = :email
            """)
            
            resultado = conn.execute(query, {"email": email_limpo}).mappings().first()
            
            if not resultado:
                return {"mensagem": "Se o e-mail existir, você receberá um link em breve."}

            email_banco = resultado['email']
            nome_banco = resultado['nome']
            
            token = jwt.encode(
                {"sub": email_banco, "exp": datetime.utcnow() + timedelta(minutes=30), "tipo": "reset"}, 
                SECRET_KEY, algorithm=ALGORITHM
            )
            
            # 🎯 DISPARO COM NOME REAL
            background_tasks.add_task(_bg(enviar_email_recuperacao), email_banco, nome_banco, token)
                
        return {"mensagem": "E-mail de recuperação enviado."}
    except Exception as e:
        print(f"❌ Erro: {e}")
        raise HTTPException(status_code=500, detail="Erro interno.")

@app.post("/api/usuarios/alterar-senha")
async def alterar_minha_senha(requisicao: AlterarSenhaRequest, background_tasks: BackgroundTasks, usuario_email: str = Depends(get_current_user)):    
    validar_senha_forte(requisicao.nova_senha)

    engine = get_engine()
    with engine.begin() as conn:
        user = conn.execute(
            text("SELECT nome FROM dbo.nps_usuarios WHERE email = :email"),
            {"email": usuario_email}
        ).mappings().first()

        novo_hash = bcrypt.hashpw(requisicao.nova_senha.encode('utf-8'), bcrypt.gensalt()).decode('utf-8')
        conn.execute(text("UPDATE dbo.nps_usuarios SET senha_hash = :hash WHERE email = :email"), {"hash": novo_hash, "email": usuario_email})
        
        background_tasks.add_task(_bg(enviar_email_senha_alterada), usuario_email, user['nome'])
        
    return {"message": "Senha alterada com sucesso!"}

@app.post("/api/usuarios/{usuario_id}/reset-manual")
async def reset_manual_senha(usuario_id: str):
    engine = get_engine()
    caracteres = string.ascii_letters + string.digits
    senha_provisoria = ''.join(secrets.choice(caracteres) for i in range(10))
    senha_hash = bcrypt.hashpw(senha_provisoria.encode('utf-8'), bcrypt.gensalt()).decode('utf-8')
    
    try:
        with engine.connect() as conn:
            conn.execute(
                text("UPDATE dbo.nps_usuarios SET senha_hash = :hash WHERE usuario_id = :id"),
                {"hash": senha_hash, "id": usuario_id}
            )
            conn.commit()
        return {"senha_provisoria": senha_provisoria}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.delete("/api/usuarios/{usuario_id}")
def excluir_usuario(usuario_id: str, admin_email: str = Depends(exigir_admin)):
    """Exclui um usuário do sistema (Apenas Administradores)"""
    try:
        engine = get_engine()
        
        # Usamos engine.begin() para que ele faça o commit automaticamente no final
        with engine.begin() as conn:
            
            # 1. Pega o ID do Administrador logado usando o e-mail do token
            admin_id = conn.execute(
                text("SELECT usuario_id FROM dbo.nps_usuarios WHERE email = :email"),
                {"email": admin_email}
            ).scalar()

            # 2. Trava anti-suicídio (Não pode excluir a si mesmo)
            if str(usuario_id) == str(admin_id):
                raise HTTPException(status_code=400, detail="Operação bloqueada: Você não pode excluir a sua própria conta.")

            # 3. Verifica qual é o tipo de conta que estamos tentando excluir
            usuario_alvo = conn.execute(
                text("SELECT tipo FROM dbo.nps_usuarios WHERE usuario_id = :id"),
                {"id": usuario_id}
            ).mappings().first()

            if not usuario_alvo:
                raise HTTPException(status_code=404, detail="Usuário não encontrado.")

            # 4. Trava do Último Admin
            if str(usuario_alvo["tipo"]).lower() == 'admin':
                total_admins = conn.execute(
                    text("SELECT COUNT(usuario_id) FROM dbo.nps_usuarios WHERE LOWER(tipo) = 'admin' AND ativo = 1")
                ).scalar()

                if total_admins <= 1:
                    raise HTTPException(status_code=400, detail="Operação bloqueada: Este é o último administrador ativo do sistema.")

            # 5. Limpa as sessões ativas do usuário (para não dar erro de Chave Estrangeira - FK)
            conn.execute(text("DELETE FROM dbo.nps_sessoes_ativas WHERE usuario_id = :id"), {"id": usuario_id})
            
            # 6. Exclui o usuário definitivamente
            conn.execute(text("DELETE FROM dbo.nps_usuarios WHERE usuario_id = :id"), {"id": usuario_id})

            # 7. Registra a exclusão no nosso Log de Auditoria
            registrar_log(
                acao="EXCLUSAO_USUARIO",
                mensagem=f"O usuário ID {usuario_id} foi excluído definitivamente do sistema.",
                nivel="WARN",
                usuario_id=admin_id
            )

        return {"status": "success", "mensagem": "Usuário excluído com sucesso."}
        
    except HTTPException:
        # Repassa os erros 400 (como a trava de segurança) diretamente para o Vue.js
        raise
    except Exception as e:
        print(f"Erro ao excluir usuário: {e}")
        raise HTTPException(status_code=500, detail="Erro interno ao excluir o usuário no banco de dados.")
    
# ==========================================
# 🔐 GESTÃO DE PERMISSÕES (RBAC/PBAC)
# ==========================================

@app.get("/api/permissoes")
def listar_permissoes(usuario = Depends(exigir_admin)):
    try:
        engine = get_engine()
        with engine.begin() as conn:
            res = conn.execute(text("SELECT perfil, chave FROM dbo.nps_permissoes")).fetchall()
            
            # Inicializa a estrutura
            permissoes = {"Viewer": [], "Manager": []}
            
            for row in res:
                # O Admin não vem do banco porque tem acesso total '*' por defeito
                if row.perfil in permissoes and row.chave != '*':
                    permissoes[row.perfil].append(row.chave)
                    
        return {"status": "success", "permissoes": permissoes}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/api/permissoes")
def atualizar_permissoes(payload: List[PermissaoUpdate], usuario = Depends(exigir_admin)):
    try:
        engine = get_engine()
        with engine.begin() as conn:
            for item in payload:
                # Ignoramos o Admin, pois o Admin tem sempre acesso '*' nativamente no código
                if item.perfil == 'Admin':
                    continue
                    
                # 1. Apaga as permissões antigas do perfil
                conn.execute(text("DELETE FROM dbo.nps_permissoes WHERE perfil = :p"), {"p": item.perfil})
                
                # 2. Insere as novas opções selecionadas
                if item.chaves and len(item.chaves) > 0:
                    sql_insert = text("INSERT INTO dbo.nps_permissoes (perfil, chave) VALUES (:p, :c)")
                    for chave in item.chaves:
                        conn.execute(sql_insert, {"p": item.perfil, "c": chave})
                        
        return {"status": "success", "message": "Matriz de permissões atualizada com sucesso!"}
    except Exception as e:
        print(traceback.format_exc())
        raise HTTPException(status_code=500, detail=str(e))

# ==========================================
# ⚙️ ROTAS DE CONFIGURAÇÃO
# ==========================================
@app.get("/api/configuracoes")
async def get_configuracoes():
    try:
        engine = get_engine()
        with engine.connect() as conn:
            resultado = conn.execute(text("SELECT chave, valor FROM dbo.nps_configuracoes")).fetchall()
            configs = {row.chave: row.valor for row in resultado}
            return {"status": "success", "data": configs}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/api/configuracoes")
async def save_configuracoes(configs: List[ConfigItem]):
    try:
        engine = get_engine()
        with engine.begin() as conn:
            for item in configs:
                # O SEGREDO: Upsert em vez de Update simples
                conn.execute(text("""
                    INSERT INTO dbo.nps_configuracoes (chave, valor, updated_at)
                    VALUES (:chave, :valor, CURRENT_TIMESTAMP)
                    ON CONFLICT (conta_id, chave) DO UPDATE SET valor = EXCLUDED.valor, updated_at = CURRENT_TIMESTAMP
                """), {"valor": item.valor, "chave": item.chave})
        return {"status": "success", "detail": "Configurações salvas!"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

# ==========================================
# 🤖 MAGIC AI (LENDO CHAVE DO BANCO)
# ==========================================

@app.get("/api/dashboard/magic-ai")
async def get_magic_ai_insights():
    try:
        engine = get_engine()
        
        # 1. Puxa as configurações diretamente do Banco de Dados
        with engine.connect() as conn:
            from services.config_svc import ia_disponivel, registrar_uso_ia
            api_key, msg_ia = ia_disponivel()
            ai_model = conn.execute(text("SELECT valor FROM dbo.nps_configuracoes WHERE chave = 'openai_model'")).scalar() or "gpt-4o-mini"
            ai_temp = conn.execute(text("SELECT valor FROM dbo.nps_configuracoes WHERE chave = 'ai_temperature'")).scalar() or "0.4"
            
            if not api_key:
                return {
                    "status": "success", 
                    "insights": {
                        "arder": "Atenção necessária:",
                        "amar": "A análise por inteligência artificial não está disponível agora.",
                        "recomendacao": msg_ia
                    }
                }

            # 2. Busca os comentários reais
            sql = """
                SELECT nota, motivo, categoria 
                FROM dbo.nps_respostas 
                WHERE motivo IS NOT NULL AND motivo != '' AND excluido = 0
                ORDER BY created_at DESC LIMIT 100
            """
            df = pd.read_sql(text(sql), conn)

        if df.empty:
            return {"status": "success", "insights": {"arder": "Sem dados suficientes.", "amar": "Aguardando submissões.", "recomendacao": "Dispare uma nova pesquisa."}}

        lista_comentarios = [f"Nota: {row['nota']} - Categoria: {row['categoria']} - Comentário: {row['motivo']}" for _, row in df.iterrows()]
        texto_para_ia = "\n".join(lista_comentarios)

        # 3. Executa a IA com os parâmetros dinâmicos do Banco
        client = openai.OpenAI(api_key=api_key.strip())
        registrar_uso_ia()
        prompt_sistema = f"""
        Atue como um Consultor Executivo de CX. Responda em português do Brasil, com linguagem simples para pequenas e médias empresas. Analise estes feedbacks:
        {texto_para_ia}
        
        Forneça um resumo executivo com exatamente 3 pontos em formato JSON estrito:
        {{
            "arder": "1 frase resumindo o principal problema.",
            "amar": "1 frase resumindo os elogios.",
            "recomendacao": "1 frase com um plano de ação direto."
        }}
        """

        resposta_ia = client.chat.completions.create(
            model=ai_model,
            messages=[{"role": "user", "content": prompt_sistema}],
            response_format={ "type": "json_object" },
            temperature=float(ai_temp)
        )

        return {"status": "success", "insights": json.loads(resposta_ia.choices[0].message.content)}

    except Exception as e:
        print(f"❌ ERRO IA: {str(e)}")
        raise HTTPException(status_code=500, detail="Falha ao gerar insights. Verifique a API Key.")

def enviar_email_alerta_gestor(empresa: str, gestor_nome: str, gestor_email: str, nps: int):
    engine = get_engine()
    if usando_resend():
        cfg = {}
        access_token = "resend"
    else:
        cfg, access_token = _token_microsoft_alerta(engine)
    return _enviar_alerta_gestor(cfg, access_token, empresa, gestor_nome, gestor_email, nps)


def _token_microsoft_alerta(engine):
    with engine.connect() as conn:
        cfg = conn.execute(text("SELECT tenant_id, client_id, client_secret, email_remetente, refresh_token FROM dbo.nps_configuracoes_email LIMIT 1")).fetchone()
        if not cfg or not cfg.refresh_token:
            raise Exception("O sistema de e-mail não está autenticado. Vá às Configurações e conecte a conta Microsoft.")
        cfg = dict(cfg._mapping)

    # 1. Obter um Access Token NOVO usando o seu Refresh Token (o método que funciona)
    token_url = f"https://login.microsoftonline.com/{cfg['tenant_id']}/oauth2/v2.0/token"
    token_data = {
        'client_id': cfg['client_id'],
        'client_secret': cfg['client_secret'],
        'refresh_token': cfg['refresh_token'],
        'grant_type': 'refresh_token',
        'scope': 'offline_access mail.send'
    }
    
    r_token = requests.post(token_url, data=token_data)
    if r_token.status_code != 200:
        raise Exception(f"Falha ao renovar sessão Microsoft: {r_token.text}")
    
    token_json = r_token.json()
    access_token = token_json.get("access_token")
    
    # Opcional: Atualizar o refresh_token se a Microsoft enviou um novo
    if "refresh_token" in token_json:
        with engine.begin() as conn:
            conn.execute(text("UPDATE dbo.nps_configuracoes_email SET refresh_token = :rt, atualizado_em = GETDATE()"), {"rt": token_json["refresh_token"]})
    return cfg, access_token


def _enviar_alerta_gestor(cfg, access_token, empresa, gestor_nome, gestor_email, nps):
    # 2. Template do E-mail
    corpo_html = f"""
    <div style="font-family: sans-serif; max-width: 600px; border: 1px solid #eee; border-radius: 10px; overflow: hidden;">
        <div style="background: #f43f5e; color: white; padding: 20px; text-align: center;">
            <h2 style="margin: 0;">🚨 ALERTA DE RISCO</h2>
        </div>
        <div style="padding: 20px; color: #333;">
            <p>Olá <strong>{gestor_nome}</strong>,</p>
            <p>O cliente <strong>{empresa}</strong> cadastrou um NPS crítico de <strong>{nps} pts</strong>.</p>
            <p style="color: #be123c; font-weight: bold;">Ação de retenção aconselhada nas próximas 24h.</p>
        </div>
    </div>
    """

    # 3. Enviar o e-mail usando o token renovado
    send_url = f"https://graph.microsoft.com/v1.0/users/{cfg.get('email_remetente', '')}/sendMail"
    headers = {"Authorization": f"Bearer {access_token}", "Content-Type": "application/json"}
    mail_payload = {
        "message": {
            "subject": f"Ação Necessária: {empresa} (NPS {nps})",
            "body": {"contentType": "HTML", "content": corpo_html},
            "toRecipients": [{"emailAddress": {"address": gestor_email}}]
        }
    }
    
    r_send = post_email(send_url, headers=headers, json=mail_payload)
    if r_send.status_code not in [200, 202]:
        raise Exception(f"O envio de e-mail foi recusado: {r_send.text}")

    return True

@app.post("/api/dashboard/acionar-gestor")
def acionar_gestor_endpoint(req: AlertaGestorRequest):
    engine = get_engine()
    with engine.connect() as conn:
        gestor_db = conn.execute(text("SELECT email FROM dbo.nps_gestores WHERE nome = :nome"), {"nome": req.gestor}).fetchone()
        if not gestor_db or not gestor_db.email:
            raise HTTPException(status_code=400, detail="Gestor sem e-mail configurado.")
            
    try:
        enviar_email_alerta_gestor(req.empresa, req.gestor, gestor_db.email, req.nps)
        return {"status": "success", "message": f"Alerta enviado com sucesso para {gestor_db.email}!"}
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))

# ==========================================
# 👥 ROTAS: GESTÃO DE OPERADORES (USUÁRIOS)
# ==========================================

@app.post("/api/usuarios")
def criar_usuario(usuario: UsuarioCreate):
    engine = get_engine()
    with engine.connect() as conn:
        
        check_query = text("SELECT usuario_id FROM dbo.nps_usuarios WHERE email = :email")
        existe = conn.execute(check_query, {"email": usuario.email}).fetchone()
        
        if existe:
            raise HTTPException(status_code=400, detail="Este e-mail já está cadastrado no sistema.")
        
        bytes_senha = usuario.password.encode('utf-8')
        salt = bcrypt.gensalt()
        senha_hash = bcrypt.hashpw(bytes_senha, salt).decode('utf-8')
        
        insert_query = text("""
            INSERT INTO dbo.nps_usuarios (nome, email, senha_hash, cargo, ativo)
            VALUES (:nome, :email, :senha_hash, :cargo, 1)
        """)
        
        conn.execute(insert_query, {
            "nome": usuario.nome,
            "email": usuario.email,
            "senha_hash": senha_hash,
            "cargo": usuario.cargo,
        })
        conn.commit() 
        
        return {
            "status": "success", 
            "mensagem": f"Operador {usuario.nome} criado com sucesso!"
        }
    
@app.get("/api/usuarios")
async def listar_operadores():
    try:
        engine = get_engine()
        with engine.connect() as conn:
            query = text("""
                SELECT usuario_id, nome, email, cargo, tipo, ativo, ultimo_acesso, email_verificado
                FROM dbo.nps_usuarios 
                ORDER BY nome ASC
            """)
            result = conn.execute(query).mappings().all()
            
            lista_usuarios = []
            for r in result:
                usuario = dict(r)
                
                if usuario.get("ultimo_acesso"):
                    data_utc = usuario["ultimo_acesso"].replace(tzinfo=timezone.utc)
                    usuario["ultimo_acesso"] = data_utc.isoformat()
                else:
                    usuario["ultimo_acesso"] = None
                    
                lista_usuarios.append(usuario)
                
            return lista_usuarios
            
    except Exception as e:
        print(f"Erro ao listar usuários: {e}")
        raise HTTPException(status_code=500, detail="Erro ao carregar lista de usuários.")

# ==========================================
# 🏠 ROTAS: DASHBOARD (Home)
# ==========================================

@app.get("/api/dashboard/kpis")
def get_dashboard_kpis(
    empresa: Optional[str] = Query(None),
    companhia: Optional[str] = Query(None),
    data_inicio: Optional[str] = Query(None),
    data_fim: Optional[str] = Query(None),
    apenas_ativos: bool = Query(True)
):
    try:
        engine = get_engine()
        with engine.connect() as conn:
            tipo_join = "LEFT JOIN"
            
            filtros_sql = []
            parametros = {}
            
            # Filtro Ativos
            parametros["apenas_ativos"] = 1 if apenas_ativos else 0
            # respostas sem empresa (ex.: link público) também contam, igual ao gráfico de evolução
            filtros_sql.append("(:apenas_ativos = 0 OR e.ativo = 1 OR COALESCE(r.empresa_id, c.empresa_id) IS NULL)")
            
            if companhia and companhia not in ("Todas as Companhias", "Todos os grupos"):
                filtros_sql.append("e.companhia_id IN (SELECT id FROM dbo.nps_companhias WHERE nome = :companhia)")
                parametros["companhia"] = companhia
            
            # Filtro de Empresa
            if empresa:
                if empresa == "Não Identificado":
                    filtros_sql.append("COALESCE(r.empresa_id, c.empresa_id) IS NULL")
                else:
                    filtros_sql.append("e.nome = :empresa")
                    parametros["empresa"] = empresa
                    
            # Filtro de Datas
            if data_inicio and data_fim:
                filtros_sql.append("COALESCE(r.data_resposta, r.created_at) >= :data_inicio")
                filtros_sql.append("COALESCE(r.data_resposta, r.created_at) <= :data_fim")
                parametros["data_inicio"] = f"{data_inicio} 00:00:00"
                parametros["data_fim"] = f"{data_fim} 23:59:59"

            # CONSTRUÇÃO SEGURA DOS CONECTORES LOGICOS
            # respostas excluídas não entram em nenhum número
            filtros_efetivos = filtros_sql + ["(r.excluido = 0 OR r.excluido IS NULL)"]
            condicao_filtro = " WHERE " + " AND ".join(filtros_efetivos)
            condicao_filtro_and = " AND " + " AND ".join(filtros_efetivos)

            # --- 3. PROCESSAMENTO DE PALAVRAS MAIS USADAS ---
            sql_termos = text(f"""
                SELECT CAST(r.motivo AS TEXT) as comentario
                FROM dbo.nps_respostas r
                {tipo_join} dbo.nps_clientes c ON r.cliente_id = c.cliente_id
                LEFT JOIN dbo.nps_empresas e ON COALESCE(r.empresa_id, c.empresa_id) = e.id
                {condicao_filtro} 
                { "AND" if condicao_filtro else "WHERE" } r.motivo IS NOT NULL AND LENGTH(CAST(r.motivo AS TEXT)) > 3
            """)
            
            comentarios_raw = conn.execute(sql_termos, parametros).scalars().all()
            
            import re
            from collections import Counter
            stop_words = {
                'para', 'com', 'mais', 'esta', 'está', 'pela', 'pelo', 'como', 'muito', 'tudo', 
                'fazer', 'quando', 'você', 'pode', 'seria', 'estão', 'neste', 'esse', 'isso',
                'pela', 'pelo', 'uma', 'umas', 'uns', 'tem', 'têm', 'fui', 'foi', 'ser', 'bom', 'bem',
                'também', 'tambem', 'porque', 'ainda', 'sempre', 'nada', 'pouco', 'sobre', 'vocês', 'voces',
                'estou', 'estava', 'eles', 'elas', 'minha', 'meus', 'minhas', 'nossa', 'nosso', 'aqui', 'então',
                'entao', 'mesmo', 'depois', 'antes', 'agora', 'outro', 'outra', 'sendo', 'essa', 'este', 'isto',
                'algum', 'alguma', 'cada', 'qual', 'quais', 'onde', 'teve', 'tinha', 'fica', 'ficou', 'nota'
            }
            
            texto_unificado = " ".join([str(c).lower() for c in comentarios_raw if c])
            palavras = re.findall(r'\b[a-zà-ÿ]{4,}\b', texto_unificado)
            contagem = Counter([p for p in palavras if p not in stop_words])
            termos_frequentes = [{"palavra": p, "quantidade": q} for p, q in contagem.most_common(12)]

            # --- 4. QUERY DE KPIS PRINCIPAIS ---
            sql_kpis = text(f"""
                SELECT 
                    COUNT(r.resposta_id) as total_respostas,
                    SUM(CASE WHEN r.nota >= 9 THEN 1 ELSE 0 END) as promotores,
                    SUM(CASE WHEN r.nota BETWEEN 7 AND 8 THEN 1 ELSE 0 END) as neutros,
                    SUM(CASE WHEN r.nota <= 6 THEN 1 ELSE 0 END) as detratores,
                    
                    SUM(CASE WHEN LOWER(p.nome) LIKE '%decisor%' AND r.nota >= 9 THEN 1 ELSE 0 END) as decisor_promotores,
                    SUM(CASE WHEN LOWER(p.nome) LIKE '%decisor%' AND r.nota <= 6 THEN 1 ELSE 0 END) as decisor_detratores,
                    SUM(CASE WHEN LOWER(p.nome) LIKE '%decisor%' THEN 1 ELSE 0 END) as decisor_total
                FROM dbo.nps_respostas r
                {tipo_join} dbo.nps_clientes c ON r.cliente_id = c.cliente_id
                LEFT JOIN dbo.nps_empresas e ON COALESCE(r.empresa_id, c.empresa_id) = e.id
                LEFT JOIN dbo.nps_perfis p ON c.perfil_id = p.id
                {condicao_filtro};
            """)
                    
            resumo = conn.execute(sql_kpis, parametros).mappings().first()
            
            total = resumo['total_respostas'] or 0
            promotores = resumo['promotores'] or 0
            neutros = resumo['neutros'] or 0
            detratores = resumo['detratores'] or 0
            
            nps_score = 0
            if total > 0:
                nps_score = round(((promotores - detratores) / total) * 100)
                
            dec_total = resumo['decisor_total'] or 0
            nps_decisor = 0
            if dec_total > 0:
                nps_decisor = round(((resumo['decisor_promotores'] - resumo['decisor_detratores']) / dec_total) * 100)

            # --- 5. CÁLCULO REVENUE AT RISK ---
            sql_rev = text(f"""
                SELECT SUM(emp_out.valor_contrato) as risco
                FROM dbo.nps_empresas emp_out
                WHERE emp_out.id IN (
                    SELECT DISTINCT COALESCE(r.empresa_id, c.empresa_id)
                    FROM dbo.nps_respostas r
                    INNER JOIN dbo.nps_clientes c ON r.cliente_id = c.cliente_id
                    LEFT JOIN dbo.nps_empresas e ON COALESCE(r.empresa_id, c.empresa_id) = e.id
                    WHERE r.nota <= 6  
                    {condicao_filtro_and}
                )
            """)
            risco_real = conn.execute(sql_rev, parametros).scalar() or 0
                
            # --- 6. FEEDBACKS RECENTES ---
            sql_feedbacks = text(f"""
                SELECT 
                    r.nota, CAST(r.motivo AS TEXT) as comentario, 
                    r.created_at, r.jira_issue_url,
                    c.nome as cliente, e.nome as empresa, p.nome as perfil_decisor
                FROM dbo.nps_respostas r
                INNER JOIN dbo.nps_clientes c ON r.cliente_id = c.cliente_id
                LEFT JOIN dbo.nps_empresas e ON COALESCE(r.empresa_id, c.empresa_id) = e.id
                LEFT JOIN dbo.nps_perfis p ON c.perfil_id = p.id
                WHERE r.motivo IS NOT NULL AND LENGTH(CAST(r.motivo AS TEXT)) > 0
                {condicao_filtro_and} 
                ORDER BY r.created_at DESC LIMIT 10;
            """)
            
            feedbacks_raw = conn.execute(sql_feedbacks, parametros).mappings().all()
            
            # Temas pensados para PME (distribuição, transporte, varejo e serviços).
            # Casamento por palavra inteira/início de palavra, para "api" não casar com "rápido".
            regras_tags = {
                "Prazo e entrega": ["atras", "prazo", "demor", "entreg", "chegou", "chegada", "tarde", "espera"],
                "Produto e avarias": ["avari", "quebrad", "danific", "defeit", "faltand", "faltou", "errad", "trocad", "vencid", "qualidade"],
                "Atendimento": ["atendiment", "atendent", "suporte", "vendedor", "educad", "grosso", "grossa", "motorista", "entregador"],
                "Preço e condições": ["preço", "preco", "caro", "cara", "valor", "desconto", "frete", "pagamento", "boleto", "prazo de pagamento"],
                "Comunicação": ["retorno", "aviso", "avisar", "avisaram", "informação", "informacao", "contato", "responder", "respondeu", "whatsapp", "telefone"],
                "Sistema e pedidos": ["site", "sistema", "aplicativo", "app", "pedido", "nota fiscal", "cadastro", "erro", "bug"],
            }
            _padroes_tags = {tema: re.compile(r"\b(" + "|".join(re.escape(k) for k in chaves) + r")", re.IGNORECASE)
                             for tema, chaves in regras_tags.items()}

            feedbacks_processados = []
            for f_raw in feedbacks_raw:
                f = dict(f_raw)
                texto = str(f.get("comentario") or "").lower()
                f["tags"] = [tag for tag, padrao in _padroes_tags.items() if padrao.search(texto)]
                feedbacks_processados.append(f)

            # --- 7. CÁLCULOS RESGATES ---
            filtros_resgate = [f.replace("r.", "atual.") for f in filtros_sql]
            condicao_resgate_and = " AND " + " AND ".join(filtros_resgate) if filtros_resgate else ""

            # --- CÁLCULO DE DETRATORES RESGATADOS (Detrator -> Promotor) ---
            query_resgatados = text(f"""
                WITH Historico AS (
                    SELECT cliente_id, nota, data_resposta, created_at, empresa_id,
                           -- 🎯 CORREÇÃO: Substituir resposta_id por created_at para desempatar pela hora exata
                           ROW_NUMBER() OVER(PARTITION BY cliente_id ORDER BY COALESCE(data_resposta, created_at) DESC, created_at DESC) as rn
                    FROM dbo.nps_respostas
                    WHERE excluido = 0 AND cliente_id IS NOT NULL AND cliente_id <> ''
                )
                SELECT 
                    c.nome as cliente_nome,
                    COALESCE(e.nome, 'Sem Empresa') as empresa_nome,
                    anterior.nota as nota_anterior,
                    atual.nota as nota_atual
                FROM Historico atual
                JOIN Historico anterior ON atual.cliente_id = anterior.cliente_id AND anterior.rn = 2
                {tipo_join} dbo.nps_clientes c ON atual.cliente_id = c.cliente_id
                LEFT JOIN dbo.nps_empresas e ON COALESCE(atual.empresa_id, c.empresa_id) = e.id
                WHERE atual.rn = 1 
                  AND anterior.nota <= 6 
                  AND atual.nota >= 9     
                  {condicao_resgate_and}      
            """)
            
            res_resgatados_raw = conn.execute(query_resgatados, parametros).mappings().all()
            
            detratores_resgatados = len(res_resgatados_raw)
            lista_resgatados = [dict(r) for r in res_resgatados_raw]
            
            # --- VARIÁVEIS ANTIGAS ---
            from datetime import datetime, timedelta, timezone
            
            filtros_sql_ant = []
            params_ant = {}
            
            params_ant["apenas_ativos"] = 1 if apenas_ativos else 0
            filtros_sql_ant.append("(:apenas_ativos = 0 OR e.ativo = 1 OR COALESCE(r.empresa_id, c.empresa_id) IS NULL)")
            filtros_sql_ant.append("(r.excluido = 0 OR r.excluido IS NULL)")
            
            if companhia and companhia not in ("Todas as Companhias", "Todos os grupos"):
                filtros_sql_ant.append("e.companhia_id IN (SELECT id FROM dbo.nps_companhias WHERE nome = :companhia)")
                params_ant["companhia"] = companhia
            
            if empresa:
                if empresa == "Não Identificado":
                    filtros_sql_ant.append("COALESCE(r.empresa_id, c.empresa_id) IS NULL")
                else:
                    filtros_sql_ant.append("e.nome = :empresa")
                    params_ant["empresa"] = empresa
                    
            if data_inicio and data_fim:
                dt_ini = datetime.strptime(data_inicio, "%Y-%m-%d")
                dt_fim = datetime.strptime(data_fim, "%Y-%m-%d")
                dias = (dt_fim - dt_ini).days + 1
                ant_ini = dt_ini - timedelta(days=dias)
                ant_fim = dt_ini - timedelta(seconds=1)
                
                filtros_sql_ant.append("COALESCE(r.data_resposta, r.created_at) >= :ant_ini")
                filtros_sql_ant.append("COALESCE(r.data_resposta, r.created_at) <= :ant_fim")
                params_ant["ant_ini"] = ant_ini.strftime("%Y-%m-%d 00:00:00")
                params_ant["ant_fim"] = ant_fim.strftime("%Y-%m-%d 23:59:59")
            else:
                ant_fim = datetime.now(timezone.utc) - timedelta(days=30)
                filtros_sql_ant.append("COALESCE(r.data_resposta, r.created_at) <= :ant_fim")
                params_ant["ant_fim"] = ant_fim.strftime("%Y-%m-%d 23:59:59")

            condicao_ant = " WHERE " + " AND ".join(filtros_sql_ant) if filtros_sql_ant else ""
            
            sql_nps_ant = text(f"""
                SELECT 
                    COUNT(r.resposta_id) as total,
                    SUM(CASE WHEN r.nota >= 9 THEN 1 ELSE 0 END) as prom,
                    SUM(CASE WHEN r.nota <= 6 THEN 1 ELSE 0 END) as detr
                FROM dbo.nps_respostas r
                {tipo_join} dbo.nps_clientes c ON r.cliente_id = c.cliente_id
                LEFT JOIN dbo.nps_empresas e ON COALESCE(r.empresa_id, c.empresa_id) = e.id
                {condicao_ant};
            """)
            
            res_ant = conn.execute(sql_nps_ant, params_ant).mappings().first()
            # sem respostas no período anterior não há comparação (evita "variação" falsa)
            variacao_nps = None
            if res_ant and res_ant['total'] > 0 and total > 0:
                nps_anterior = round(((res_ant['prom'] - res_ant['detr']) / res_ant['total']) * 100)
                variacao_nps = nps_score - nps_anterior

            # 8. TÓPICOS CRÍTICOS ---
            sql_todos_comentarios = text(f"""
                SELECT r.nota, CAST(r.motivo AS TEXT) as comentario
                FROM dbo.nps_respostas r
                {tipo_join} dbo.nps_clientes c ON r.cliente_id = c.cliente_id
                LEFT JOIN dbo.nps_empresas e ON COALESCE(r.empresa_id, c.empresa_id) = e.id
                {condicao_filtro}
                { "AND" if condicao_filtro else "WHERE" } 
                    r.motivo IS NOT NULL 
                    AND LENGTH(CAST(r.motivo AS TEXT)) > 0 
            """)
            
            todos_comentarios = conn.execute(sql_todos_comentarios, parametros).mappings().all()
            
            topicos_agg = {k: {"mencoes": 0, "soma_notas": 0} for k in regras_tags.keys()}
            
            for row in todos_comentarios:
                texto = str(row["comentario"]).lower()
                nota = float(row["nota"])
                for tema, padrao in _padroes_tags.items():
                    if padrao.search(texto):
                        topicos_agg[tema]["mencoes"] += 1
                        topicos_agg[tema]["soma_notas"] += nota
                        
            topicos_criticos = []
            for tema, dados_tema in topicos_agg.items():
                if dados_tema["mencoes"] > 0:
                    topicos_criticos.append({
                        "tema": tema,
                        "mencoes": dados_tema["mencoes"],
                        "notaMedia": round(dados_tema["soma_notas"] / dados_tema["mencoes"], 1)
                    })
                    
            topicos_criticos = sorted(topicos_criticos, key=lambda x: (-x["mencoes"], x["notaMedia"]))[:5]

            # --- CÁLCULO DE PROMOTORES PERDIDOS / RISCO DE CHURN ---
            query_perdidos = text(f"""
                WITH Historico AS (
                    SELECT cliente_id, nota, data_resposta, created_at, empresa_id,
                           -- 🎯 CORREÇÃO: Substituir resposta_id por created_at para desempatar pela hora exata
                           ROW_NUMBER() OVER(PARTITION BY cliente_id ORDER BY COALESCE(data_resposta, created_at) DESC, created_at DESC) as rn
                    FROM dbo.nps_respostas
                    WHERE excluido = 0 AND cliente_id IS NOT NULL AND cliente_id <> ''
                )
                SELECT 
                    c.nome as cliente_nome,
                    COALESCE(e.nome, 'Sem Empresa') as empresa_nome,
                    anterior.nota as nota_anterior,
                    atual.nota as nota_atual,
                    CASE WHEN anterior.nota >= 9 AND atual.nota <= 6 THEN 1 ELSE 0 END as queda_drastica
                FROM Historico atual
                JOIN Historico anterior ON atual.cliente_id = anterior.cliente_id AND anterior.rn = 2
                {tipo_join} dbo.nps_clientes c ON atual.cliente_id = c.cliente_id
                LEFT JOIN dbo.nps_empresas e ON COALESCE(atual.empresa_id, c.empresa_id) = e.id
                WHERE atual.rn = 1 
                  AND anterior.nota >= 9 
                  AND atual.nota <= 8     
                  {condicao_resgate_and}      
            """)
            

            res_perdidos_raw = conn.execute(query_perdidos, parametros).mappings().all()
            
            clientes_em_risco = len(res_perdidos_raw)
            queda_drastica = sum(1 for r in res_perdidos_raw if r['queda_drastica'] == 1)
            lista_risco = [dict(r) for r in res_perdidos_raw]
            
        return {
            "status": "success",
            "kpis": {
                "score": nps_score,
                "total_respostas": total,
                "promotores": promotores,
                "neutros": neutros,
                "detratores": detratores,
                "nps_decisor": nps_decisor, 
                "clientes_resgatados": detratores_resgatados, 
                "lista_resgatados": lista_resgatados,
                "clientes_em_risco": clientes_em_risco, 
                "queda_drastica": queda_drastica,
                "lista_risco": lista_risco,    
                "variacao_nps": variacao_nps, 
                "revenue_at_risk": float(risco_real),
                "termos_frequentes": termos_frequentes,
                "total_decisores": dec_total,
                "topicos_criticos": topicos_criticos 
            },
            "feedbacks": feedbacks_processados
        }
    
    except Exception as e:
        import traceback
        print(traceback.format_exc())
        raise HTTPException(status_code=500, detail=str(e))
    

@app.get("/api/dashboard/detalhes")
def get_dashboard_detalhes(
    empresa: Optional[str] = Query(None),
    companhia: Optional[str] = Query(None), 
    data_inicio: Optional[str] = Query(None), 
    data_fim: Optional[str] = Query(None),
    apenas_ativos: bool = Query(True) 
):
    try:
        from sqlalchemy import text
        engine = get_engine()
        with engine.connect() as conn:
            filtros_sql_c = []
            filtros_sql_puro = []
            params = {}

            params["apenas_ativos"] = 1 if apenas_ativos else 0

            if companhia and companhia not in ("Todas as Companhias", "Todos os grupos"):
                filtros_sql_c.append("e.companhia_id IN (SELECT id FROM dbo.nps_companhias WHERE nome = :companhia)")
                filtros_sql_puro.append("empresa_id IN (SELECT id FROM dbo.nps_empresas WHERE companhia_id IN (SELECT id FROM dbo.nps_companhias WHERE nome = :companhia))")
                params["companhia"] = companhia

            if empresa:
                params["empresa"] = empresa
                if empresa == "Não Identificado":
                    filtros_sql_c.append("COALESCE(r.empresa_id, c.empresa_id) IS NULL")
                    filtros_sql_puro.append("empresa_id IS NULL")
                else:
                    filtros_sql_c.append("e.nome = :empresa")
                    filtros_sql_puro.append("empresa_id IN (SELECT id FROM dbo.nps_empresas WHERE nome = :empresa)")

            if data_inicio and data_fim:
                filtros_sql_c.append("COALESCE(r.data_resposta, r.created_at) >= :data_inicio")
                filtros_sql_c.append("COALESCE(r.data_resposta, r.created_at) <= :data_fim")
                params["data_inicio"] = f"{data_inicio} 00:00:00"
                params["data_fim"] = f"{data_fim} 23:59:59"

            # Se não há empresa selecionada, agrupa pelas empresas. Se há empresa, agrupa pelos segmentos.
            coluna_nome = "COALESCE(e.nome, 'Não Identificado')" if not empresa else "COALESCE(s.nome, 'Sem Segmento')"
            
            str_filtro_c = "WHERE 1=1"
            if len(filtros_sql_c) > 0:
                str_filtro_c += " AND " + " AND ".join(filtros_sql_c)
            str_filtro_c += " AND (r.excluido = 0 OR r.excluido IS NULL)"
            str_filtro_c += " AND (:apenas_ativos = 0 OR e.ativo = 1 OR COALESCE(r.empresa_id, c.empresa_id) IS NULL)"
                
            str_filtro_puro = "WHERE 1=1"
            if len(filtros_sql_puro) > 0:
                str_filtro_puro += " AND " + " AND ".join(filtros_sql_puro)
            
            sql_ranking = text(f"""
                SELECT 
                    {coluna_nome} as nome,
                    MAX(g.nome) as gestor, 
                    MAX(g.avatar) as gestor_avatar,
                    MAX(CAST(COALESCE(e.ativo, 1) AS INT)) as ativo, 
                    COUNT(r.resposta_id) as total,
                    MAX(COALESCE(r.data_resposta, r.created_at)) as data_ultima_resposta,
                    
                    (SELECT a.id FROM dbo.nps_acoes a WHERE a.empresa_id = MAX(e.id) ORDER BY a.created_at DESC LIMIT 1) as acao_id,
                    (SELECT COALESCE(a.status, 'Pendente') FROM dbo.nps_acoes a WHERE a.empresa_id = MAX(e.id) ORDER BY a.created_at DESC LIMIT 1) as acao_status,
                    -- 🎯 A LINHA ABAIXO FOI ADICIONADA PARA TRAZER A DATA PARA O RADAR:
                    (SELECT a.created_at FROM dbo.nps_acoes a WHERE a.empresa_id = MAX(e.id) ORDER BY a.created_at DESC LIMIT 1) as acao_criada_em,
                    (SELECT a.prazo_limite FROM dbo.nps_acoes a WHERE a.empresa_id = MAX(e.id) ORDER BY a.created_at DESC LIMIT 1) as acao_prazo,
                    (SELECT CAST(r2.motivo AS TEXT) FROM dbo.nps_respostas r2
                       LEFT JOIN dbo.nps_clientes c2 ON r2.cliente_id = c2.cliente_id
                      WHERE COALESCE(r2.empresa_id, c2.empresa_id) = MAX(e.id) AND r2.nota <= 6
                        AND r2.motivo IS NOT NULL AND LENGTH(CAST(r2.motivo AS TEXT)) > 0
                        AND (r2.excluido = 0 OR r2.excluido IS NULL)
                      ORDER BY r2.created_at DESC LIMIT 1) as ultimo_comentario,

                    ROUND(
                        (SUM(CASE WHEN r.nota >= 9 THEN 1.0 ELSE 0 END) / NULLIF(COUNT(r.resposta_id), 0) * 100) - 
                        (SUM(CASE WHEN r.nota <= 6 THEN 1.0 ELSE 0 END) / NULLIF(COUNT(r.resposta_id), 0) * 100), 0
                    ) as nps
                FROM dbo.nps_respostas r
                LEFT JOIN dbo.nps_clientes c ON r.cliente_id = c.cliente_id
                LEFT JOIN dbo.nps_empresas e ON COALESCE(r.empresa_id, c.empresa_id) = e.id 
                LEFT JOIN dbo.nps_segmentos s ON c.segmento_id = s.id 
                LEFT JOIN dbo.nps_gestores g ON e.gestor_id = g.id
                
                {str_filtro_c}
                
                GROUP BY {coluna_nome}
                ORDER BY nps DESC, data_ultima_resposta DESC;
            """)
            
            ranking_raw = conn.execute(sql_ranking, params).mappings().all()
            ranking = [dict(r) for r in ranking_raw]

            sql_taxa = text(f"""
                SELECT 
                    (SELECT COUNT(*) FROM dbo.nps_clientes {str_filtro_puro}) as total_convidados,
                    (SELECT COUNT(DISTINCT r.cliente_id) 
                     FROM dbo.nps_respostas r
                     LEFT JOIN dbo.nps_clientes c ON r.cliente_id = c.cliente_id
                     LEFT JOIN dbo.nps_empresas e ON COALESCE(r.empresa_id, c.empresa_id) = e.id
                     {str_filtro_c}) as total_responderam
            """)
            
            res_taxa = conn.execute(sql_taxa, params).mappings().first()
            
            taxa_pct = 0
            if res_taxa and res_taxa['total_convidados'] > 0:
                taxa_pct = round((res_taxa['total_responderam'] / res_taxa['total_convidados']) * 100)

            # Ações em aberto da conta (todas, inclusive as sem empresa: CSAT e link público)
            acoes = conn.execute(text("""
                SELECT COUNT(*) AS abertas,
                       SUM(CASE WHEN prazo_limite < CURRENT_TIMESTAMP THEN 1 ELSE 0 END) AS vencidas
                FROM dbo.nps_acoes WHERE COALESCE(status, 'Pendente') <> 'Concluído'
            """)).mappings().first()

        return {
            "ranking": ranking,
            "taxa_resposta": taxa_pct,
            "total_convidados": int(res_taxa['total_convidados'] or 0) if res_taxa else 0,
            "total_responderam": int(res_taxa['total_responderam'] or 0) if res_taxa else 0,
            "acoes_abertas": int(acoes["abertas"] or 0),
            "acoes_vencidas": int(acoes["vencidas"] or 0),
        }
    except Exception as e:
        import traceback
        print(traceback.format_exc())
        from fastapi import HTTPException
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/api/dashboard/trend")
def get_dashboard_trend(
    empresa: Optional[str] = Query(None),
    companhia: Optional[str] = Query(None),
    data_inicio: Optional[str] = Query(None),
    data_fim: Optional[str] = Query(None),
    apenas_ativos: bool = Query(True)
):
    try:
        engine = get_engine()
        with engine.connect() as conn:
            sql_set = text("SELECT valor FROM dbo.nps_configuracoes WHERE chave = 'mostrar_sem_cliente'")
            config_valor = conn.execute(sql_set).scalar()
            tipo_join = "LEFT JOIN"

            filtros_sql = ["COALESCE(r.data_resposta, r.created_at) IS NOT NULL", "(r.excluido = 0 OR r.excluido IS NULL)"]
            params = {}
            
            params["apenas_ativos"] = 1 if apenas_ativos else 0
            filtros_sql.append("(:apenas_ativos = 0 OR e.ativo = 1 OR COALESCE(r.empresa_id, c.empresa_id) IS NULL)")
            
            if companhia and companhia not in ("Todas as Companhias", "Todos os grupos"):
                filtros_sql.append("e.companhia_id IN (SELECT id FROM dbo.nps_companhias WHERE nome = :companhia)")
                params["companhia"] = companhia
                
            if empresa:
                if empresa == "Não Identificado":
                    filtros_sql.append("COALESCE(r.empresa_id, c.empresa_id) IS NULL")
                else:
                    filtros_sql.append("e.nome = :empresa")
                    params["empresa"] = empresa
                    
            if data_inicio and data_fim:
                filtros_sql.append("COALESCE(r.data_resposta, r.created_at) >= :data_inicio")
                filtros_sql.append("COALESCE(r.data_resposta, r.created_at) <= :data_fim")
                params["data_inicio"] = f"{data_inicio} 00:00:00"
                params["data_fim"] = f"{data_fim} 23:59:59"

            condicao = " WHERE " + " AND ".join(filtros_sql)

            sql_trend = text(f"""
                WITH UltimosMeses AS (
                    SELECT 
                        LEFT(CAST(COALESCE(r.data_resposta, r.created_at) AS VARCHAR(10)), 7) as mes,
                        COUNT(r.resposta_id) as total,
                        SUM(CASE WHEN r.nota >= 9 THEN 1 ELSE 0 END) as promotores,
                        SUM(CASE WHEN r.nota <= 6 THEN 1 ELSE 0 END) as detratores
                    FROM dbo.nps_respostas r
                    {tipo_join} dbo.nps_clientes c ON r.cliente_id = c.cliente_id
                    LEFT JOIN dbo.nps_empresas e ON COALESCE(r.empresa_id, c.empresa_id) = e.id
                    {condicao}
                    GROUP BY LEFT(CAST(COALESCE(r.data_resposta, r.created_at) AS VARCHAR(10)), 7)
                    ORDER BY LEFT(CAST(COALESCE(r.data_resposta, r.created_at) AS VARCHAR(10)), 7) DESC LIMIT {24 if (data_inicio and data_fim) else 6}
                )
                SELECT * FROM UltimosMeses ORDER BY mes ASC;
            """)

            result = conn.execute(sql_trend, params).mappings().all()
            
            labels = []
            scores = []
            totais = []
            
            meses_pt = {'01':'Jan', '02':'Fev', '03':'Mar', '04':'Abr', '05':'Mai', '06':'Jun', 
                        '07':'Jul', '08':'Ago', '09':'Set', '10':'Out', '11':'Nov', '12':'Dez'}

            for row in result:
                if not row['mes'] or '-' not in row['mes']: 
                    continue
                
                ano, mes_num = row['mes'].split('-')
                nome_mes = meses_pt.get(mes_num, mes_num) 
                mes_nome = f"{nome_mes}/{ano[2:]}" 
                
                total = row['total'] or 0
                prom = row['promotores'] or 0
                detr = row['detratores'] or 0
                
                nps = round(((prom / total) * 100) - ((detr / total) * 100)) if total > 0 else 0
                    
                labels.append(mes_nome)
                scores.append(nps)
                totais.append(total)

        return {"status": "success", "labels": labels, "scores": scores, "totais": totais}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    
@app.get("/api/dashboard/nuvem-palavras")
def get_nuvem_palavras(
    empresa: Optional[str] = Query(None),
    companhia: Optional[str] = Query(None), 
    data_inicio: Optional[str] = Query(None),
    data_fim: Optional[str] = Query(None),
    apenas_ativos: bool = Query(True)
):
    try:
        engine = get_engine()
        with engine.connect() as conn:
            filtros_sql = ["r.nota <= 6", "r.motivo IS NOT NULL", "LENGTH(CAST(r.motivo AS TEXT)) > 0", "(r.excluido = 0 OR r.excluido IS NULL)"]
            params = {}
            
            params["apenas_ativos"] = 1 if apenas_ativos else 0
            filtros_sql.append("(:apenas_ativos = 0 OR e.ativo = 1)")
            
            if companhia and companhia not in ("Todas as Companhias", "Todos os grupos"):
                filtros_sql.append("e.companhia_id IN (SELECT id FROM dbo.nps_companhias WHERE nome = :companhia)")
                params["companhia"] = companhia
                
            if empresa:
                if empresa == "Não Identificado":
                    filtros_sql.append("COALESCE(r.empresa_id, c.empresa_id) IS NULL")
                else:
                    filtros_sql.append("e.nome = :empresa")
                    params["empresa"] = empresa
                    
            if data_inicio and data_fim:
                filtros_sql.append("COALESCE(r.data_resposta, r.created_at) >= :data_inicio")
                filtros_sql.append("COALESCE(r.data_resposta, r.created_at) <= :data_fim")
                params["data_inicio"] = f"{data_inicio} 00:00:00"
                params["data_fim"] = f"{data_fim} 23:59:59"

            condicao = " WHERE " + " AND ".join(filtros_sql)

            sql = text(f"""
                SELECT CAST(r.motivo AS TEXT) as motivo
                FROM dbo.nps_respostas r
                LEFT JOIN dbo.nps_clientes c ON r.cliente_id = c.cliente_id
                LEFT JOIN dbo.nps_empresas e ON COALESCE(r.empresa_id, c.empresa_id) = e.id
                {condicao}
            """)
            
            result = conn.execute(sql, params).mappings().all()
            texto_completo = " ".join([r['motivo'].lower() for r in result if r['motivo']])
            palavras = re.findall(r'\b[a-zà-ú]{3,}\b', texto_completo)
            
            stop_words = {
                'que', 'não', 'para', 'com', 'uma', 'dos', 'das', 'aos', 'nas', 'nos', 
                'como', 'mais', 'mas', 'foi', 'por', 'sua', 'seu', 'tem', 'muito', 'isso', 
                'está', 'também', 'pelo', 'pela', 'até', 'quando', 'ou', 'só', 'ter', 'ser', 
                'fazer', 'estou', 'sobre', 'ainda', 'sem', 'porque', 'neste', 'nesta'
            }
            
            palavras_uteis = [p for p in palavras if p not in stop_words and len(p) > 3]
            contagem = Counter(palavras_uteis).most_common(15) 
            nuvem = [{"texto": p[0], "peso": p[1]} for p in contagem]
            
            return {"status": "success", "nuvem": nuvem}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/api/dashboard/exportar")
def exportar_dashboard(
    empresa: Optional[str] = Query(None),
    companhia: Optional[str] = Query(None), 
    data_inicio: Optional[str] = Query(None),
    data_fim: Optional[str] = Query(None),
    apenas_ativos: bool = Query(True)
):
    try:
        engine = get_engine()
        with engine.connect() as conn:
            filtros_sql = ["(r.excluido = 0 OR r.excluido IS NULL)"]
            parametros = {}
            
            parametros["apenas_ativos"] = 1 if apenas_ativos else 0
            filtros_sql.append("(:apenas_ativos = 0 OR e.ativo = 1)")
            
            if companhia and companhia not in ("Todas as Companhias", "Todos os grupos"):
                filtros_sql.append("e.companhia_id IN (SELECT id FROM dbo.nps_companhias WHERE nome = :companhia)")
                parametros["companhia"] = companhia
                
            if empresa:
                if empresa == "Não Identificado":
                    filtros_sql.append("COALESCE(r.empresa_id, c.empresa_id) IS NULL")
                else:
                    filtros_sql.append("e.nome = :empresa")
                    parametros["empresa"] = empresa
                    
            if data_inicio and data_fim:
                filtros_sql.append("COALESCE(r.data_resposta, r.created_at) >= :data_inicio")
                filtros_sql.append("COALESCE(r.data_resposta, r.created_at) <= :data_fim")
                parametros["data_inicio"] = f"{data_inicio} 00:00:00"
                parametros["data_fim"] = f"{data_fim} 23:59:59"

            condicao_filtro = " WHERE " + " AND ".join(filtros_sql)

            sql_relatorio = text(f"""
                SELECT 
                    c.nome as Cliente,
                    c.email as Email,
                    e.nome as Empresa, 
                    s.nome as Segmento,
                    p.nome as Perfil,
                    e.valor_contrato as Receita_ARR,
                    r.nota as Nota_NPS,
                    CASE 
                        WHEN r.nota >= 9 THEN 'Promotor'
                        WHEN r.nota >= 7 THEN 'Neutro'
                        ELSE 'Detrator'
                    END as Classificacao,
                    r.motivo as Comentario,
                    r.categoria as Categoria,
                    COALESCE(r.data_resposta, r.created_at) as Data_Resposta
                FROM dbo.nps_respostas r
                LEFT JOIN dbo.nps_clientes c ON r.cliente_id = c.cliente_id
                LEFT JOIN dbo.nps_empresas e ON COALESCE(r.empresa_id, c.empresa_id) = e.id
                LEFT JOIN dbo.nps_perfis p ON c.perfil_id = p.id
                LEFT JOIN dbo.nps_segmentos s ON c.segmento_id = s.id
                {condicao_filtro}
                ORDER BY Data_Resposta DESC
            """)

            df = pd.read_sql(sql_relatorio, conn, params=parametros)

        stream = io.StringIO()
        df.to_csv(stream, index=False, sep=';', encoding='utf-8-sig') 
        
        response = StreamingResponse(iter([stream.getvalue()]), media_type="text/csv")
        response.headers["Content-Disposition"] = "attachment; filename=NPS_CommandCenter_Export.csv"
        return response

    except Exception as e:
        print(f"Erro Exportação: {str(e)}")
        raise HTTPException(status_code=500, detail="Falha ao gerar o arquivo.")
        
@app.get("/api/dashboard/companhias")
def get_lista_companhias():
    try:
        engine = get_engine()
        with engine.connect() as conn:
            sql = text("SELECT nome FROM dbo.nps_companhias ORDER BY nome")
            resultados = conn.execute(sql).scalars().all()
            
            return ["Todos os grupos"] + list(resultados)
    except Exception as e:
        print(f"Erro ao buscar companhias: {e}")
        return ["Todos os grupos"]
    
# ==========================================
# 🏢 ROTAS: EMPRESAS, SEGMENTOS E PERFIS
# ==========================================

@app.get("/api/empresas")
def listar_empresas():
    try:
        engine = get_engine()
        with engine.connect() as conn:
            # Magia: Agrupa os clientes pela empresa e soma a receita (ARR) automaticamente!
            sql = text("""
                SELECT 
                    COALESCE(e.nome::text, c.empresa) as nome, 
                    COUNT(c.cliente_id) as total_contatos,
                    COALESCE(MAX(e.valor_contrato), 0) as arr_total
                FROM dbo.nps_clientes c
                LEFT JOIN dbo.nps_empresas e ON e.id = c.empresa_id
                WHERE COALESCE(e.nome::text, c.empresa) IS NOT NULL AND COALESCE(e.nome::text, c.empresa) <> ''
                GROUP BY COALESCE(e.nome::text, c.empresa)
                ORDER BY arr_total DESC
            """)
            res = conn.execute(sql).mappings().all()
            return [dict(r) for r in res]
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

def crud_factory(route_path, table_name, schema=BasicoSchema):
    @app.get(route_path)
    def listar():
        with get_engine().connect() as conn: 
            return [dict(r) for r in conn.execute(text(f"SELECT * FROM {table_name} ORDER BY nome")).mappings().all()]
            
    @app.post(route_path)
    def salvar(item: schema): # type: ignore  
        with get_engine().begin() as conn:
            if table_name == 'dbo.nps_gestores': 
                conn.execute(text(f"INSERT INTO {table_name} (nome, papel, email, teams_webhook, avatar) VALUES (:n, :p, :e, :t, :a)"), {
                    "n": item.nome, 
                    "p": getattr(item, 'papel', ''), 
                    "e": getattr(item, 'email', ''),
                    "t": getattr(item, 'teams_webhook', ''),
                    "a": getattr(item, 'avatar', None)
                })
            else: 
                conn.execute(text(f"INSERT INTO {table_name} (nome) VALUES (:n)"), {"n": item.nome})
        return {"status": "success"}
        
    @app.put(route_path + "/{item_id}")
    def atualizar(item_id: int, item: schema): # type: ignore  
        with get_engine().begin() as conn:
            nome_antigo = conn.execute(text(f"SELECT nome FROM {table_name} WHERE id=:id"), {"id": item_id}).scalar()
            
            if table_name == 'dbo.nps_gestores': 
                conn.execute(text(f"UPDATE {table_name} SET nome=:n, papel=:p, email=:e, teams_webhook=:t, avatar=:a WHERE id=:id"), {
                    "n": item.nome, 
                    "p": getattr(item, 'papel', ''), 
                    "e": getattr(item, 'email', ''), 
                    "t": getattr(item, 'teams_webhook', ''),
                    "a": getattr(item, 'avatar', None),
                    "id": item_id
                })
            else: 
                conn.execute(text(f"UPDATE {table_name} SET nome=:n WHERE id=:id"), {"n": item.nome, "id": item_id})
            
            if nome_antigo and str(nome_antigo) != str(item.nome):
                # 👇 Os clientes SUMIRAM daqui porque agora apontam para o ID!
                if table_name == 'dbo.nps_segmentos':
                    conn.execute(text("UPDATE dbo.nps_empresas SET segmento=:novo WHERE segmento=:antigo"), {"novo": item.nome, "antigo": nome_antigo})
                elif table_name == 'dbo.nps_gestores':
                    conn.execute(text("UPDATE dbo.nps_empresas SET gestor=:novo WHERE gestor=:antigo"), {"novo": item.nome, "antigo": nome_antigo})
                elif table_name == 'dbo.nps_companhias':
                    conn.execute(text("UPDATE dbo.nps_empresas SET companhia=:novo WHERE companhia=:antigo"), {"novo": item.nome, "antigo": nome_antigo})

        return {"status": "success"}
        
    @app.delete(route_path + "/{item_id}")
    def deletar(item_id: int):
        with get_engine().begin() as conn: conn.execute(text(f"DELETE FROM {table_name} WHERE id = :id"), {"id": item_id})
        return {"message": "Removido"}

# Estas 4 linhas substituem dezenas de rotas antigas e ativam todos os menus!
crud_factory("/api/cadastros/segmentos", "dbo.nps_segmentos")
crud_factory("/api/cadastros/perfis", "dbo.nps_perfis")
crud_factory("/api/cadastros/cargos", "dbo.nps_cargos")
crud_factory("/api/cadastros/gestores", "dbo.nps_gestores", GestorSchema)
crud_factory("/api/cadastros/companhias", "dbo.nps_companhias")
    
# --- ROTAS DE GESTORES DE CONTA ---
@app.get("/api/gestores")
async def get_lista_gestores():
    try:
        engine = get_engine()
        with engine.connect() as conn:
            sql = text("SELECT id, nome, email FROM dbo.nps_gestores ORDER BY nome")
            resultados = conn.execute(sql).mappings().all()
            
            gestores = [{"id": r['id'], "nome": r['nome'], "email": r['email']} for r in resultados if r['email']]
            return gestores
    except Exception as e:
        print(f"❌ Erro ao buscar gestores: {e}")
        return []
    
@app.post("/api/gestores/testar-webhook")
def testar_webhook_teams(payload: TesteWebhookPayload):
    if not payload.webhook_url:
        raise HTTPException(status_code=400, detail="URL do Webhook não fornecida.")

    # Um Cartão Adaptativo bonito só para confirmar que a ligação funciona
    adaptive_card = {
        "type": "message",
        "attachments": [{
            "contentType": "application/vnd.microsoft.card.adaptive",
            "content": {
                "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
                "type": "AdaptiveCard",
                "version": "1.4",
                "body": [
                    {
                        "type": "TextBlock",
                        "text": "🚀 Conexão Estabelecida!",
                        "size": "Large",
                        "weight": "Bolder",
                        "color": "Good"
                    },
                    {
                        "type": "TextBlock",
                        "text": "O Hub de NPS da Rakiti está agora conectado a este canal. Você receberá os resumos matinais de ações pendentes aqui.",
                        "wrap": True
                    }
                ]
            }
        }]
    }

    try:
        import requests
        resp = requests.post(payload.webhook_url, json=adaptive_card, headers={"Content-Type": "application/json"})
        resp.raise_for_status()
        return {"status": "success", "message": "Mensagem de teste enviada com sucesso!"}
    except Exception as e:
        print(f"Erro ao testar webhook: {e}")
        raise HTTPException(status_code=500, detail="Falha ao enviar mensagem. Verifique se a URL é válida.")

# ==========================================
# 🚀 SALVAR NOVOS CADASTROS
# ==========================================

@app.get("/api/cadastros/empresas")
async def listar_empresas():
    engine = get_engine()
    with engine.connect() as conn:
        sql = text("""
            SELECT 
                e.id, e.nome, e.segmento, e.valor_contrato as arr_total, 
                g.nome as gestor, e.gestor_id,
                comp.nome as companhia, e.companhia_id,
                e.ativo -- 👈 ADICIONADO AQUI!
            FROM dbo.nps_empresas e
            LEFT JOIN dbo.nps_gestores g ON e.gestor_id = g.id
            LEFT JOIN dbo.nps_companhias comp ON e.companhia_id = comp.id
            ORDER BY e.nome ASC
        """)
        return conn.execute(sql).mappings().all()

@app.post("/api/cadastros/empresas")
def save_empresa(emp: EmpresaSchema):
    try:
        engine = get_engine()
        with engine.begin() as conn:
            sql_insert = text("""
                INSERT INTO dbo.nps_empresas 
                (nome, segmento, valor_contrato, gestor, gestor_id, companhia_id) 
                VALUES (:n, :s, :v, :g, :gid, :cid)
            """)
            conn.execute(sql_insert, {
                "n": emp.nome, 
                "s": emp.segmento, 
                "v": emp.valor_contrato, 
                "g": emp.gestor,
                "gid": emp.gestor_id, # 👈 O ID agora é salvo!
                "cid": emp.companhia_id
            })
        return {"status": "success", "message": "Empresa cadastrada"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.put("/api/cadastros/empresas/{empresa_id}")
def update_empresa(empresa_id: int, emp: EmpresaSchema):
    engine = get_engine()
    with engine.begin() as conn: 
        sql_update = text("""
            UPDATE dbo.nps_empresas 
            SET nome=:n, segmento=:s, valor_contrato=:v, gestor=:g, gestor_id=:gid, companhia_id=:cid 
            WHERE id=:id
        """)
        
        conn.execute(sql_update, {
            "n": emp.nome, "s": emp.segmento, "v": emp.valor_contrato, 
            "g": emp.gestor, "gid": emp.gestor_id, "cid": emp.companhia_id, "id": empresa_id
        })
            
    return {"status": "success"}
        
# ==========================================
# 🗑️ EXCLUIR CADASTROS (DELETE)
# ==========================================

@app.delete("/api/cadastros/empresas/{empresa_id}")
def delete_empresa(empresa_id: int, admin_email: str = Depends(exigir_admin)):
    try:
        engine = get_engine()
        with engine.connect() as conn:
            admin_id = conn.execute(text("SELECT usuario_id FROM dbo.nps_usuarios WHERE email = :e"), {"e": admin_email}).scalar()

            conn.execute(text("DELETE FROM dbo.nps_empresas WHERE id = :id"), {"id": empresa_id})
            conn.commit()

            # --- AUDITORIA ---
            registrar_log(
                acao="EXCLUSAO_EMPRESA",
                mensagem=f"A empresa ID {empresa_id} foi excluída permanentemente.",
                nivel="ERROR",
                usuario_id=admin_id
            )

            return {"message": "Empresa removida com sucesso"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

# ==========================================
# 👤 ROTAS: CLIENTES
# ==========================================

def auto_cadastrar_referencias(cargo: str, empresa: str, perfil_decisor: str, gestor: str = None):
    engine = get_engine()
    with engine.begin() as conn:
        if cargo and cargo.strip():
            conn.execute(text("INSERT INTO dbo.nps_cargos (nome) SELECT CAST(:nome AS VARCHAR) WHERE NOT EXISTS (SELECT 1 FROM dbo.nps_cargos WHERE nome = :nome)"), {"nome": cargo.strip()})
        if empresa and empresa.strip():
            conn.execute(text("INSERT INTO dbo.nps_empresas (nome, segmento, valor_contrato) SELECT CAST(:nome AS VARCHAR), '', 0 WHERE NOT EXISTS (SELECT 1 FROM dbo.nps_empresas WHERE nome = :nome)"), {"nome": empresa.strip()})
        if perfil_decisor and perfil_decisor.strip():
            conn.execute(text("INSERT INTO dbo.nps_perfis (nome) SELECT CAST(:nome AS VARCHAR) WHERE NOT EXISTS (SELECT 1 FROM dbo.nps_perfis WHERE nome = :nome)"), {"nome": perfil_decisor.strip()})
        if gestor and gestor.strip():
            conn.execute(text("INSERT INTO dbo.nps_gestores (nome, papel, email) SELECT CAST(:nome AS VARCHAR), '', '' WHERE NOT EXISTS (SELECT 1 FROM dbo.nps_gestores WHERE nome = :nome)"), {"nome": gestor.strip()})

@app.get("/api/clientes")
def list_clientes(
    q: str = "", 
    ativo: str = "Ativos", 
    perfil: str = "Todos", 
    topn: int = 100000,
    _t: str = None,
    usuario_email: str = Depends(get_current_user)
):
    try:
        engine = get_engine()
        
        df = clientes_svc.load_clientes(q, ativo, perfil, topn)
        return df.fillna("").to_dict(orient="records")
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/api/clientes/{cliente_id}/status")
def change_cliente_status(cliente_id: str, payload: StatusUpdate):
    try:
        clientes_svc.set_ativo(cliente_id, payload.ativo)
        return {"status": "success"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/api/clientes/{cliente_id}/forcar-envio")
def forcar_envio_nps(cliente_id: str, request: Request, background_tasks: BackgroundTasks, usuario: str = Depends(get_current_user)):
    try:
        # 🎯 CAPTURA O DOMÍNIO AUTOMATICAMENTE DA REQUISIÇÃO
        dominio_atual = request.headers.get("origin") or str(request.base_url)
        
        from services.email_svc import disparar_convite_nps_especifico
        # Passamos o domínio como segundo argumento
        background_tasks.add_task(_bg(disparar_convite_nps_especifico), [cliente_id], dominio_atual)
        
        return {
            "status": "success", 
            "message": "Solicitação recebida! O e-mail está sendo despachado agora mesmo."
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail="Não conseguimos processar o envio manual.")

@app.post("/api/clientes/forcar-envio-lote")
def forcar_envio_lote(payload: LoteEnvio, request: Request, background_tasks: BackgroundTasks, usuario_email: str = Depends(get_current_user)):
    try:
        # 🎯 CAPTURA O DOMÍNIO AUTOMATICAMENTE DA REQUISIÇÃO
        dominio_atual = request.headers.get("origin") or str(request.base_url)

        engine = get_engine()
        with engine.connect() as conn:
            uid = conn.execute(text("SELECT usuario_id FROM dbo.nps_usuarios WHERE email = :e"), {"e": usuario_email}).scalar()

        from services.email_svc import disparar_convite_nps_especifico
        # Passamos o domínio como segundo argumento
        background_tasks.add_task(_bg(disparar_convite_nps_especifico), payload.cliente_ids, dominio_atual)
        
        registrar_log(
            acao="DISPARO_MANUAL",
            mensagem=f"Iniciado disparo manual forçado para um lote de {len(payload.cliente_ids)} clientes.",
            nivel="INFO",
            usuario_id=uid
        )

        return {
            "status": "success", 
            "message": f"O motor de disparos iniciou o processamento de {len(payload.cliente_ids)} e-mails com sucesso."
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail="Ocorreu um erro ao tentar processar o lote de envios.")

@app.delete("/api/clientes/{cliente_id}")
def delete_cliente_route(cliente_id: str, delete_respostas: bool = True, usuario_email: str = Depends(get_current_user)): 
    try:
        engine = get_engine()
        with engine.connect() as conn:
            uid = conn.execute(text("SELECT usuario_id FROM dbo.nps_usuarios WHERE email = :e"), {"e": usuario_email}).scalar()

        # O serviço apaga e devolve o status
        ok, msg = clientes_svc.delete_cliente(cliente_id, delete_respostas)
        
        # --- AUDITORIA ---
        registrar_log(
            acao="EXCLUSAO_CLIENTE",
            mensagem=f"O cliente ID {cliente_id} e os seus vínculos foram excluídos da base.",
            nivel="WARN",
            usuario_id=uid
        )

        return {"status": "success", "message": msg}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/api/clientes")
def create_cliente_route(payload: ClienteCreate):
    try:
        # A função auto_cadastrar_referencias foi removida pois os dropdowns agora enviam IDs rígidos
        novo_id = clientes_svc.insert_cliente(
            payload.nome, 
            payload.email, 
            payload.telefone, 
            payload.empresa_id,  # 👈 Passando o ID
            payload.perfil_id,   # 👈 Passando o ID
            payload.segmento_id, # 👈 Passando o ID
            payload.cargo_id,    # 👈 Passando o ID
            gestor=payload.gestor
        )
        return {"status": "success", "cliente_id": novo_id, "message": "Cliente cadastrado!"}
    except Exception as e:
        if "2627" in str(e) or "2601" in str(e) or "duplicate key" in str(e) or "UniqueViolation" in str(e):
            raise HTTPException(status_code=400, detail="Já existe cliente com este e-mail.")
        raise HTTPException(status_code=500, detail=str(e))

@app.put("/api/clientes/{cliente_id}")
def update_cliente_route(cliente_id: str, payload: ClienteUpdate):
    try:
        clientes_svc.update_cliente(
            cliente_id, 
            payload.nome, 
            payload.email, 
            payload.telefone, 
            payload.empresa_id,  # 👈 Passando o ID
            payload.perfil_id,   # 👈 Passando o ID
            payload.segmento_id, # 👈 Passando o ID
            payload.cargo_id,    # 👈 Passando o ID
            payload.ativo 
        )
        return {"status": "success", "message": "Cliente atualizado."}
    except IntegrityError as e:
        error_msg = str(e)
        if "UQ_nps_clientes_email" in error_msg or "duplicate key" in error_msg.lower():
            raise HTTPException(status_code=400, detail="Este e-mail já está cadastrado para outro cliente.")
        raise HTTPException(status_code=400, detail="Erro de restrição no banco de dados.")
    except Exception as e:
        import traceback
        traceback.print_exc()
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/api/chat/clientes-recentes")
async def obter_clientes_recentes(usuario = Depends(get_current_user)):
    """Busca as 3 últimas empresas que tiveram interações de NPS"""
    try:
        engine = get_engine()
        with engine.connect() as conn:
            # Query otimizada para performance
            sql = text("""
                SELECT empresa 
                FROM (
                    SELECT empresa, MAX(created_at) as ultima_interacao
                    FROM dbo.nps_respostas 
                    WHERE empresa IS NOT NULL 
                      AND empresa <> '' 
                      AND excluido = 0
                    GROUP BY empresa
                ) AS t
                ORDER BY ultima_interacao DESC LIMIT 3
            """)
            
            res = conn.execute(sql).mappings().all()
            return [r['empresa'] for r in res]
            
    except Exception as e:
        print(f"⚠️ Erro ao buscar atalhos no SQL: {e}")
        return []
    
# ==========================================
# 🛑 ROTAS PARA ATIVAR / INATIVAR PESSOAS E EMPRESAS
# ==========================================

@app.put("/api/clientes/{cliente_id}/status")
def alterar_status_cliente(cliente_id: str, payload: dict):
    try:
        # Pega o valor (True/False ou 1/0) e converte para Inteiro do SQL (1 ou 0)
        ativo = 1 if payload.get("ativo") else 0
        engine = get_engine()
        with engine.begin() as conn:
            conn.execute(text("""
                UPDATE dbo.nps_clientes 
                SET ativo = :a, updated_at = CURRENT_TIMESTAMP 
                WHERE cliente_id = :id
            """), {"a": ativo, "id": cliente_id})
        return {"status": "success", "message": "Status atualizado."}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.put("/api/empresas/{empresa_id}/status")
def alterar_status_empresa(empresa_id: int, payload: dict):
    try:
        ativo = 1 if payload.get("ativo") else 0
        engine = get_engine()
        with engine.begin() as conn:
            conn.execute(text("""
                UPDATE dbo.nps_empresas 
                SET ativo = :a 
                WHERE id = :id
            """), {"a": ativo, "id": empresa_id})
        return {"status": "success", "message": "Status atualizado."}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

# ==========================================
# 📋 LISTAR E ATUALIZAR FEEDBACKS (Respostas)
# ==========================================
@app.get("/api/respostas")
async def listar_respostas(
    q: str = "",
    companhia: str = "Todas",
    empresa: str = "",
    categoria: str = "Todas",
    perfil: str = "Todos",
    tipo_data: str = "data_resposta",
    data_inicio: str = None,
    data_fim: str = None,
    incluir_excluidas: bool = False,
    topn: int = 100000
):
    try:
        from services import respostas_svc
        
        df = respostas_svc.load_respostas(
            q=q, 
            companhia=companhia, 
            empresa=empresa, 
            categoria=categoria, 
            perfil=perfil, 
            incluir_excluidas=incluir_excluidas, 
            topn=topn,
            data_inicio=data_inicio,
            data_fim=data_fim,
            tipo_data=tipo_data
        )
        return df.fillna("").to_dict(orient="records")
    except Exception as e:
        import traceback
        traceback.print_exc()
        from fastapi import HTTPException
        raise HTTPException(status_code=500, detail=str(e))

@app.put("/api/respostas/{resposta_id}")
def update_resposta_route(resposta_id: str, payload: RespostaUpdate):
    try:
        engine = get_engine()
        
        # 1. TRATAMENTO INTELIGENTE DA CATEGORIA
        # (Para evitar conflitos com a CHECK constraint do SQL Server)
        cat_segura = None
        if payload.categoria:
            # Põe tudo em maiúsculas (geralmente as constraints exigem isso)
            cat_upper = payload.categoria.upper().strip()
            
            # Mapeamento para as categorias padrão mais prováveis do seu sistema
            if "UX" in cat_upper or "UI" in cat_upper:
                cat_segura = "UX/UI"
            elif "PERFORMANCE" in cat_upper or "LENTO" in cat_upper:
                cat_segura = "Performance"
            elif "ATENDIMENTO" in cat_upper or "SUPORTE" in cat_upper:
                cat_segura = "Atendimento"
            elif "BUG" in cat_upper or "ERRO" in cat_upper:
                cat_segura = "Bugs"
            elif "INTEGRA" in cat_upper:
                cat_segura = "Integração"
            else:
                # Se não for nada conhecido, usamos o valor com Primeira Letra Maiúscula
                cat_segura = payload.categoria.title()

        with engine.begin() as conn:
            # 2. ATUALIZAÇÃO NO BANCO DE DADOS
            sql = text("""
                UPDATE dbo.nps_respostas 
                SET nota = :nota, 
                    categoria = :categoria, 
                    motivo = :motivo, 
                    canal = :canal, 
                    expectativas = :expectativas, 
                    o_que_faltava = :o_que_faltava
                WHERE resposta_id = :id
            """)
            
            conn.execute(sql, {
                "nota": payload.nota,
                "categoria": cat_segura, # Usamos a categoria tratada
                "motivo": payload.motivo or "",
                "canal": payload.canal or "Manual",
                "expectativas": payload.expectativas or "",
                "o_que_faltava": payload.o_que_faltava or "",
                "id": resposta_id
            })
            
        return {"status": "success", "message": "Feedback enriquecido com sucesso!"}
        
    except Exception as e:
        print(f"❌ Erro ao enriquecer resposta: {str(e)}")
        # Retorna o detalhe exato para você saber que constraint falhou
        raise HTTPException(status_code=500, detail=f"Erro de Banco de Dados: {str(e)}")

# ==========================================
# 🗂️ ARQUIVAR / DESARQUIVAR FEEDBACKS
# ==========================================
@app.post("/api/respostas/{resposta_id}/soft-delete")
async def soft_delete_resposta_route(resposta_id: str):
    try:
        engine = get_engine()
        with engine.begin() as conn:
            conn.execute(text("UPDATE dbo.nps_respostas SET excluido = 1 WHERE resposta_id = :id"), {"id": resposta_id})
        return {"status": "success", "detail": "Arquivado com sucesso"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/api/respostas/{resposta_id}/restore")
async def restore_resposta_route(resposta_id: str):
    try:
        engine = get_engine()
        with engine.begin() as conn:
            conn.execute(text("UPDATE dbo.nps_respostas SET excluido = 0 WHERE resposta_id = :id"), {"id": resposta_id})
        return {"status": "success", "detail": "Restaurado com sucesso"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    
@app.post("/api/respostas/manual")
def inserir_resposta_manual(resp: RespostaManual, usuario_email: str = Depends(get_current_user)):
    try:
        engine = get_engine()
        import uuid # 🎯 Necessário para gerar o ID que a base de dados exige
        
        # 1. Gerar um ID único para identificar esta resposta manual
        novo_id_resposta = f"manual_{uuid.uuid4().hex[:16]}"

        with engine.begin() as conn:
            # 2. Obter o nome da empresa associada a este cliente
            sql_cliente = text("SELECT empresa FROM dbo.nps_clientes WHERE cliente_id = :cliente_id")
            resultado_cliente = conn.execute(sql_cliente, {"cliente_id": resp.cliente_id}).fetchone()
            
            if not resultado_cliente:
                raise HTTPException(status_code=404, detail="Cliente não encontrado.")
            
            empresa_nome = resultado_cliente.empresa

            # 3. Inserir a resposta (Agora com o resposta_id obrigatório e fuso horário corrigido)
            sql_insert = text("""
                INSERT INTO dbo.nps_respostas 
                (resposta_id, cliente_id, empresa, nota, categoria, motivo, canal, data_resposta, created_at, excluido) 
                VALUES (:res_id, :cliente_id, :empresa, :nota,
                        CASE WHEN :nota >= 9 THEN 'Promotor' WHEN :nota >= 7 THEN 'Neutro' ELSE 'Detrator' END,
                        :motivo, :canal, SYSUTCDATETIME(), SYSUTCDATETIME(), 0)
            """)
            conn.execute(sql_insert, {
                "res_id": novo_id_resposta,
                "cliente_id": resp.cliente_id,
                "empresa": empresa_nome,
                "nota": resp.nota,
                "motivo": resp.motivo,
                "canal": resp.canal
            })

            # 4. INTERROMPER A RÉGUA DE LEMBRETES (Status 'Respondido' para o robô não enviar mais)
            sql_update_disparo = text("""
                UPDATE dbo.nps_disparos 
                SET status = 'Respondido', updated_at = SYSUTCDATETIME()
                WHERE cliente_id = :cliente_id AND status <> 'Respondido'
            """)
            conn.execute(sql_update_disparo, {"cliente_id": resp.cliente_id})
            
            # Atualizar status no cadastro do cliente
            sql_update_cliente = text("""
                UPDATE dbo.nps_clientes 
                SET status_envio = 'Respondido', updated_at = SYSUTCDATETIME()
                WHERE cliente_id = :cliente_id
            """)
            conn.execute(sql_update_cliente, {"cliente_id": resp.cliente_id})

            # 5. CRIAR AÇÃO AUTOMÁTICA PARA DETRATORES
            if resp.nota <= 6:
                sql_empresa_id = text("SELECT id FROM dbo.nps_empresas WHERE nome = :nome")
                res_emp = conn.execute(sql_empresa_id, {"nome": empresa_nome}).fetchone()
                
                if res_emp:
                    sql_acao = text("""
                        INSERT INTO dbo.nps_acoes (empresa_id, resposta_id, descricao, prioridade, status, data_criacao)
                        VALUES (:emp_id, :res_id, :desc, 'Alta', 'Pendente', SYSUTCDATETIME())
                    """)
                    desc = f"Tratar Detrator (Nota {resp.nota}). Feedback inserido manualmente via {resp.canal}."
                    conn.execute(sql_acao, {
                        "emp_id": res_emp.id, 
                        "res_id": novo_id_resposta, # 🎯 Vincula a ação à resposta que acabámos de criar
                        "desc": desc
                    })

        return {"status": "success", "message": "Resposta inserida com sucesso!", "id": novo_id_resposta}
    
    except Exception as e:
        print(f"Erro ao inserir resposta manual: {e}")
        # Retorna o detalhe do erro para ajudar no debug do frontend
        raise HTTPException(status_code=500, detail=str(e))
    
# ==========================================
# 🗑️ EXCLUSÃO DEFINITIVA DE FEEDBACKS (ADMIN)
# ==========================================
@app.delete("/api/respostas/{resposta_id}")
def excluir_resposta_definitiva(resposta_id: str, admin_email: str = Depends(exigir_admin)):
    """Exclui permanentemente uma resposta do banco de dados (Apenas Admins)"""
    try:
        engine = get_engine()
        with engine.begin() as conn:
            admin_id = conn.execute(text("SELECT usuario_id FROM dbo.nps_usuarios WHERE email = :e"), {"e": admin_email}).scalar()
            
            check = conn.execute(text("SELECT resposta_id FROM dbo.nps_respostas WHERE resposta_id = :id"), {"id": resposta_id}).fetchone()
            if not check:
                raise HTTPException(status_code=404, detail="Resposta não encontrada.")
            
            conn.execute(text("DELETE FROM dbo.nps_acoes WHERE resposta_id = :id"), {"id": resposta_id})
            conn.execute(text("DELETE FROM dbo.nps_respostas WHERE resposta_id = :id"), {"id": resposta_id})
            
            # --- AUDITORIA ---
            registrar_log(
                acao="EXCLUSAO_RESPOSTA",
                mensagem=f"A resposta NPS ID {resposta_id} foi permanentemente excluída da base.",
                nivel="WARN",
                usuario_id=admin_id
            )

        return {"status": "success", "message": "Feedback e ações vinculadas foram excluídos permanentemente."}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail="Erro interno ao excluir a resposta.")

# ==========================================
# 📥 ROTAS: IMPORTAÇÃO
# ==========================================

@app.post("/api/importar/preview")
async def preview_importacao(file: UploadFile = File(...)):
    try:
        contents = await file.read()
        
        if file.filename.lower().endswith(('.xlsx', '.xls')):
            df = pd.read_excel(io.BytesIO(contents))
        else:
            try:
                conteudo_texto = contents.decode('utf-8-sig').strip()
            except UnicodeDecodeError:
                conteudo_texto = contents.decode('latin1').strip()
                
            if not conteudo_texto:
                raise ValueError("O arquivo está vazio ou só contém linhas em branco.")

            df = pd.read_csv(io.StringIO(conteudo_texto), sep=None, engine='python')
        
        df.columns = df.columns.str.strip().str.lower()
        
        df = df.fillna("")
        for col in df.select_dtypes(include=['datetime64', 'datetimetz']).columns:
            df[col] = df[col].astype(str)

        dados = df.to_dict(orient='records')
        return dados
        
    except Exception as e:
        print(f"🚨 ERRO REAL NO PYTHON: {str(e)}")
        raise HTTPException(status_code=400, detail=f"Erro ao ler arquivo: {str(e)}")


# A NOVA ROTA UNIFICADA E ROBUSTA QUE O FRONTEND ESTÁ CHAMANDO
@app.post("/api/importar/processar")
async def processar_importacao(payload: dict):
    tipo = payload.get("tipo")
    dados = payload.get("dados", [])
    chaves_cliente = payload.get("chaves_cliente", [])
    chaves_resposta = payload.get("chaves_resposta", [])
    
    configuracao = payload.get("configuracao", {})
    overwrite = configuracao.get("overwrite", True)
    companhia_id_selecionada = configuracao.get("companhia_id")

    if not dados:
        raise HTTPException(status_code=400, detail="Nenhum dado válido recebido.")
    if not chaves_cliente:
        raise HTTPException(status_code=400, detail="Defina pelo menos uma chave para identificar o cliente.")

    engine = get_engine()
    inserted_count = 0
    updated_count = 0
    ignored_count = 0
    detalhes_erros = [] 

    try:
        with engine.begin() as conn:
            
            # ==========================================
            # 🏢 1. GARANTIR A EXISTÊNCIA DOS DADOS MESTRE
            # ==========================================
            empresas_unicas = set()
            cargos_unicos = set()
            segmentos_unicos = set()
            perfis_unicos = set() # 👈 Adicionado para os Perfis!

            for row in dados:
                emp_nome = str(row.get("empresa", "")).strip()
                if emp_nome: empresas_unicas.add(emp_nome)
                
                if tipo == 'clientes':
                    cargo_nome = str(row.get("cargo", "")).strip()
                    if cargo_nome: cargos_unicos.add(cargo_nome)
                    
                    seg_nome = str(row.get("segmento", "")).strip()
                    if seg_nome: segmentos_unicos.add(seg_nome)
                    
                    perf_nome = str(row.get("perfil_decisor", row.get("perfil", ""))).strip()
                    if perf_nome: perfis_unicos.add(perf_nome)
            
            # Cria/Atualiza Empresas
            for emp_nome in empresas_unicas:
                check_emp = conn.execute(text("SELECT id FROM dbo.nps_empresas WHERE LOWER(nome) = LOWER(:nome)"), {"nome": emp_nome}).fetchone()
                if not check_emp:
                    conn.execute(text("INSERT INTO dbo.nps_empresas (nome, companhia_id, created_at) VALUES (:nome, :comp_id, CURRENT_TIMESTAMP)"), {"nome": emp_nome, "comp_id": companhia_id_selecionada})
                elif overwrite and companhia_id_selecionada:
                    conn.execute(text("UPDATE dbo.nps_empresas SET companhia_id = :comp_id WHERE id = :id"), {"comp_id": companhia_id_selecionada, "id": check_emp.id})

            # Cria Cargos novos
            for c_nome in cargos_unicos:
                if not conn.execute(text("SELECT id FROM dbo.nps_cargos WHERE LOWER(nome) = LOWER(:nome)"), {"nome": c_nome}).fetchone():
                    conn.execute(text("INSERT INTO dbo.nps_cargos (nome) VALUES (:nome)"), {"nome": c_nome})

            # Cria Segmentos novos
            for s_nome in segmentos_unicos:
                if not conn.execute(text("SELECT id FROM dbo.nps_segmentos WHERE LOWER(nome) = LOWER(:nome)"), {"nome": s_nome}).fetchone():
                    conn.execute(text("INSERT INTO dbo.nps_segmentos (nome) VALUES (:nome)"), {"nome": s_nome})
                    
            # Cria Perfis novos
            for p_nome in perfis_unicos:
                if not conn.execute(text("SELECT id FROM dbo.nps_perfis WHERE LOWER(nome) = LOWER(:nome)"), {"nome": p_nome}).fetchone():
                    conn.execute(text("INSERT INTO dbo.nps_perfis (nome) VALUES (:nome)"), {"nome": p_nome})

            # 💡 A GRANDE MAGIA: Mapeamento Dinâmico Texto -> ID
            mapa_empresas = {str(r.nome).strip().lower(): r.id for r in conn.execute(text("SELECT id, nome FROM dbo.nps_empresas")).fetchall()}
            mapa_cargos = {str(r.nome).strip().lower(): r.id for r in conn.execute(text("SELECT id, nome FROM dbo.nps_cargos")).fetchall()}
            mapa_segmentos = {str(r.nome).strip().lower(): r.id for r in conn.execute(text("SELECT id, nome FROM dbo.nps_segmentos")).fetchall()}
            mapa_perfis = {str(r.nome).strip().lower(): r.id for r in conn.execute(text("SELECT id, nome FROM dbo.nps_perfis")).fetchall()}
            
            # ==========================================
            # 🧑‍💼 2. IMPORTAÇÃO DE BASE DE CLIENTES
            # ==========================================
            if tipo == 'clientes':
                # O mapa_db agora aponta para as novas colunas *_id
                mapa_db = {
                    "e-mail": "email", "email_cliente": "email", "email": "email",
                    "cliente_id": "cliente_id", "id_cliente": "cliente_id",
                    "perfil": "perfil_id", "perfil_decisor": "perfil_id", 
                    "empresa": "empresa_id", "cargo": "cargo_id", "segmento": "segmento_id"
                }

                for c in dados:
                    where_clauses = []
                    params_busca = {}
                    has_null = False

                    # Tradução das chaves de cruzamento (ex: Se usuário escolheu cruzar por "Empresa")
                    for idx, col_arq in enumerate(chaves_cliente):
                        val = str(c.get(col_arq, "")).strip()
                        if not val:
                            has_null = True
                            break
                            
                        col_db = mapa_db.get(col_arq.lower(), col_arq.lower())
                        param_name = f"c_param_{idx}"
                        
                        # Se for uma coluna de ID, converte o texto para ID antes de cruzar!
                        if col_db == "empresa_id": val = mapa_empresas.get(val.lower())
                        elif col_db == "cargo_id": val = mapa_cargos.get(val.lower())
                        elif col_db == "segmento_id": val = mapa_segmentos.get(val.lower())
                        elif col_db == "perfil_id": val = mapa_perfis.get(val.lower())
                        
                        if val is None:
                            has_null = True
                            break

                        where_clauses.append(f"{col_db} = :{param_name}")
                        params_busca[param_name] = val

                    if has_null or not where_clauses:
                        ignored_count += 1
                        continue

                    where_sql = " AND ".join(where_clauses)
                    existente = conn.execute(text(f"SELECT cliente_id FROM dbo.nps_clientes WHERE {where_sql}"), params_busca).fetchone()

                    email = str(c.get("email", c.get("e-mail", c.get("email_cliente", "")))).strip().lower()
                    
                    raw_dt_envio = str(c.get("ultimo_envio", c.get("data_ultimo_envio", ""))).strip()
                    dt_envio = raw_dt_envio if raw_dt_envio and raw_dt_envio.lower() not in ['nan', 'nat', 'none', 'null', ''] else None
                    
                    raw_ativo = str(c.get("ativo", "True")).strip().lower()
                    status_ativo = 0 if raw_ativo in ['false', '0', 'falso', 'nao', 'não', 'f'] else 1

                    # Resolve os IDs baseados no que veio no Excel
                    perfil_raw = str(c.get("perfil_decisor", c.get("perfil", "Decisor"))).strip().lower()

                    params_save = {
                        "nome": str(c.get("nome", "")).strip() or None,
                        "email": email,
                        "empresa_id": mapa_empresas.get(str(c.get("empresa", "")).strip().lower()),
                        "cargo_id": mapa_cargos.get(str(c.get("cargo", "")).strip().lower()),
                        "perfil_id": mapa_perfis.get(perfil_raw),
                        "segmento_id": mapa_segmentos.get(str(c.get("segmento", "")).strip().lower()),
                        "ultimo_envio": dt_envio,
                        "ativo": status_ativo 
                    }

                    if existente:
                        if overwrite:
                            params_save["cid"] = existente.cliente_id
                            # A query de UPDATE agora grava apenas os IDs!
                            update_sql = text("""
                                UPDATE dbo.nps_clientes 
                                SET nome = COALESCE(:nome, nome), 
                                    email = COALESCE(NULLIF(:email, ''), email),
                                    empresa_id = COALESCE(:empresa_id, empresa_id), 
                                    cargo_id = COALESCE(:cargo_id, cargo_id),
                                    perfil_id = COALESCE(:perfil_id, perfil_id), 
                                    segmento_id = COALESCE(:segmento_id, segmento_id),
                                    ativo = :ativo,
                                    ultimo_envio = COALESCE(:ultimo_envio, ultimo_envio),
                                    updated_at = CURRENT_TIMESTAMP
                                WHERE cliente_id = :cid
                            """)
                            conn.execute(update_sql, params_save)
                            updated_count += 1
                        else:
                            ignored_count += 1
                    else:
                        params_save["cliente_id"] = str(random.randint(100000000, 999999999))
                        # A query de INSERT agora grava apenas os IDs!
                        insert_sql = text("""
                            INSERT INTO dbo.nps_clientes (
                                cliente_id, nome, email, empresa_id, cargo_id, 
                                perfil_id, segmento_id, ativo, ultimo_envio, status_envio,
                                created_at, updated_at
                            )
                            VALUES (
                                :cliente_id, :nome, :email, :empresa_id, :cargo_id, 
                                :perfil_id, :segmento_id, :ativo, :ultimo_envio, 'Pendente',
                                SYSUTCDATETIME(), SYSUTCDATETIME()
                            )
                        """)
                        conn.execute(insert_sql, params_save)
                        inserted_count += 1

            # ==========================================
            # 📊 3. IMPORTAÇÃO DE HISTÓRICO DE RESPOSTAS
            # ==========================================
            elif tipo == 'respostas':
                
                mapa_clientes = {
                    "email_cliente": "email", "email": "email", "e-mail": "email",
                    "cliente_id": "cliente_id", "id_cliente": "cliente_id"
                }
                mapa_respostas = {
                    "data_resposta": "CAST(data_resposta AS DATE)",
                    "data": "CAST(data_resposta AS DATE)",
                    "resposta_id": "resposta_id", "id_resposta": "resposta_id"
                }

                for r in dados:
                    email_atual = str(r.get("email", r.get("e-mail", "Desconhecido")))
                    
                    # 💡 Traduzimos a Empresa instantaneamente através do dicionário!
                    emp_nome_raw = str(r.get("empresa", "")).strip()
                    empresa_id_banco = mapa_empresas.get(emp_nome_raw.lower())
                    
                    nota_str = str(r.get("nota", "")).strip()
                    try:
                        nota = int(nota_str)
                    except ValueError:
                        ignored_count += 1
                        detalhes_erros.append({"email": email_atual, "motivo": f"Nota inválida: '{nota_str}'"})
                        continue

                    if nota >= 9: categoria_nps = "Promotor"
                    elif nota >= 7: categoria_nps = "Neutro"
                    else: categoria_nps = "Detrator"

                    where_clauses = []
                    params_cliente = {}
                    has_null = False

                    for idx, col_arq in enumerate(chaves_cliente):
                        val = str(r.get(col_arq, "")).strip()
                        if not val:
                            has_null = True
                            break
                        col_db = mapa_clientes.get(col_arq.lower(), col_arq.lower())
                        param_name = f"c_param_{idx}"
                        where_clauses.append(f"{col_db} = :{param_name}")
                        params_cliente[param_name] = val
                        
                    if has_null or not where_clauses:
                        ignored_count += 1
                        detalhes_erros.append({"email": email_atual, "motivo": "Falta coluna de identificação."})
                        continue
                        
                    cliente_existente = conn.execute(text(f"SELECT cliente_id FROM dbo.nps_clientes WHERE {' AND '.join(where_clauses)}"), params_cliente).fetchone()

                    if not cliente_existente:
                        ignored_count += 1 
                        detalhes_erros.append({"email": email_atual, "motivo": "Cliente não existe no banco."})
                        continue
                    
                    cliente_id = cliente_existente.cliente_id
                    
                    resposta_existente_id = None
                    if chaves_resposta:
                        where_resp = ["cliente_id = :cid"]
                        params_resp = {"cid": cliente_id}
                        has_null_resp = False
                        
                        for idx, col_arq in enumerate(chaves_resposta):
                            val = str(r.get(col_arq, "")).strip()
                            if not val:
                                has_null_resp = True
                                break
                            col_db = mapa_respostas.get(col_arq.lower(), col_arq.lower())
                            param_name = f"r_param_{idx}"
                            
                            if "DATE" in col_db:
                                where_resp.append(f"{col_db} = CAST(:{param_name} AS DATE)")
                                params_resp[param_name] = val[:10] 
                            else:
                                where_resp.append(f"{col_db} = :{param_name}")
                                params_resp[param_name] = val
                                
                        if not has_null_resp:
                            resp_existente = conn.execute(text(f"SELECT resposta_id FROM dbo.nps_respostas WHERE {' AND '.join(where_resp)}"), params_resp).fetchone()
                            if resp_existente:
                                resposta_existente_id = resp_existente.resposta_id

                    dt_resposta = r.get("data_resposta")
                    if not dt_resposta or str(dt_resposta).strip() == "": dt_resposta = None
                    motivo = str(r.get("comentario", r.get("motivo", ""))).strip()

                    if resposta_existente_id:
                        if overwrite:
                            update_sql = text("""
                                UPDATE dbo.nps_respostas
                                SET nota = :nota, motivo = :motivo, categoria = :categoria,
                                    data_resposta = COALESCE(:dt_resp, data_resposta),
                                    empresa = COALESCE(NULLIF(:empresa, ''), empresa),
                                    empresa_id = COALESCE(:empresa_id, empresa_id),
                                    excluido = 0
                                WHERE resposta_id = :rid
                            """)
                            conn.execute(update_sql, {
                                "nota": nota, "motivo": motivo, "categoria": categoria_nps, "dt_resp": dt_resposta, 
                                "empresa": emp_nome_raw, "empresa_id": empresa_id_banco, "rid": resposta_existente_id
                            })
                            updated_count += 1
                        else:
                            ignored_count += 1
                            detalhes_erros.append({"email": email_atual, "motivo": "Resposta já existe e overwrite=False."})
                    else:
                        insert_sql = text("""
                            INSERT INTO dbo.nps_respostas (
                                resposta_id, cliente_id, nota, motivo, categoria, 
                                canal, excluido, data_resposta, created_at,
                                empresa, empresa_id
                            )
                            VALUES (
                                :rid, :cid, :nota, :motivo, :categoria, 
                                'Importacao_Manual', 0, :dt_resp, SYSUTCDATETIME(),
                                :empresa, :empresa_id
                            )
                        """)
                        conn.execute(insert_sql, {
                            "rid": str(random.randint(100000000, 999999999)), "cid": cliente_id, "nota": nota,
                            "motivo": motivo, "categoria": categoria_nps, "dt_resp": dt_resposta,
                            "empresa": emp_nome_raw, "empresa_id": empresa_id_banco
                        })
                        inserted_count += 1

        return {
            "status": "success", 
            "inseridos": inserted_count + updated_count,
            "erros": ignored_count,
            "detalhes": detalhes_erros
        }

    except Exception as e:
        import traceback
        print(f"🔥 Erro na importação: {traceback.format_exc()}")
        raise HTTPException(status_code=500, detail=str(e))
    
@app.delete("/api/admin/limpar-dados")
def limpar_dados_em_massa(tipo: str, usuario = Depends(get_current_user)):
    try:
        engine = get_engine()
        with engine.begin() as conn:
            if tipo == 'respostas':
                # Apaga apenas as respostas (mantém os clientes e empresas intactos)
                conn.execute(text("UPDATE dbo.nps_acoes SET resposta_id = NULL WHERE resposta_id IS NOT NULL"))
                conn.execute(text("DELETE FROM dbo.nps_respostas"))
                msg = "Todas as respostas (NPS) foram apagadas com sucesso."
                
            elif tipo == 'clientes':
                # Para apagar clientes, OBRIGATORIAMENTE temos de apagar as respostas deles primeiro
                conn.execute(text("UPDATE dbo.nps_acoes SET resposta_id = NULL WHERE resposta_id IS NOT NULL"))
                conn.execute(text("DELETE FROM dbo.nps_respostas"))
                conn.execute(text("DELETE FROM dbo.nps_disparos"))
                conn.execute(text("DELETE FROM dbo.nps_clientes"))
                msg = "Todos os clientes e respostas foram apagados com sucesso."
                
            elif tipo == 'empresas':
                # 👇 NOVA OPÇÃO: Para apagar empresas, apagamos a cadeia inteira
                conn.execute(text("DELETE FROM dbo.nps_acoes"))
                conn.execute(text("DELETE FROM dbo.nps_respostas"))
                conn.execute(text("DELETE FROM dbo.nps_disparos"))
                conn.execute(text("DELETE FROM dbo.nps_clientes"))
                conn.execute(text("DELETE FROM dbo.nps_empresas"))
                msg = "Toda a base (Empresas, Clientes e Respostas) foi limpa com sucesso."
                
            else:
                raise HTTPException(status_code=400, detail="Comando de limpeza inválido.")
                
        return {"status": "success", "message": msg}
        
    except Exception as e:
        import traceback
        error_msg = str(e)
        print(f"Erro ao limpar banco: {traceback.format_exc()}")
        
        if "REFERENCE constraint" in error_msg or "FOREIGN KEY" in error_msg or "foreign key" in error_msg:
            raise HTTPException(
                status_code=400, 
                detail="Bloqueio de segurança: Ainda existem dados vinculados a estas empresas."
            )
            
        raise HTTPException(status_code=500, detail=f"Erro interno ao limpar dados: {error_msg}")

# ==========================================
# 🔂 ROTAS: STATUS DE CONEXÃO
# ==========================================

@app.get("/api/status")
def check_status():
    try:
        engine = get_engine()
        with engine.connect() as conn:
            from sqlalchemy import text
            conn.execute(text("SELECT 1"))
        return {"banco_online": True, "api_status": "OK"}
    except Exception as e:
        return {"banco_online": False, "api_status": "ERROR", "detalhe": str(e)}
    
# ==========================================
# 🔂 ROTAS: CONFIGURAÇÃO
# ==========================================

@app.get("/api/settings/mostrar-sem-cliente")
def get_setting_mostrar():
    engine = get_engine()
    with engine.connect() as conn:
        res = conn.execute(text("SELECT valor FROM dbo.nps_configuracoes WHERE chave = 'mostrar_sem_cliente'")).scalar()
        return {"valor": res == 'true'}

@app.post("/api/settings/mostrar-sem-cliente")
def update_setting_mostrar(payload: SettingUpdate):
    engine = get_engine()
    with engine.connect() as conn:
        val_str = 'true' if payload.valor else 'false'
        conn.execute(text("UPDATE dbo.nps_configuracoes SET valor = :v WHERE chave = 'mostrar_sem_cliente'"), {"v": val_str})
        conn.commit()
        return {"status": "success"}

def obter_tipo_join():
    engine = get_engine()
    with engine.connect() as conn:
        res = conn.execute(text("SELECT valor FROM dbo.nps_configuracoes WHERE chave = 'mostrar_sem_cliente'")).scalar()
        return "LEFT JOIN" if res == 'true' else "INNER JOIN"
    
# ==========================================
# 🚀 ROTAS DE USUÁRIOS
# ==========================================
    
@app.put("/api/usuarios/{usuario_id}")
async def atualizar_usuario(usuario_id: str, data: dict, admin_email: str = Depends(exigir_admin)):
    try:
        engine = get_engine()
        with engine.begin() as conn:
            # Busca o ID do Admin que está aprovando
            admin_id = conn.execute(text("SELECT usuario_id FROM dbo.nps_usuarios WHERE email = :e"), {"e": admin_email}).scalar()

            query = text("""
                UPDATE dbo.nps_usuarios 
                SET nome = :nome, email = :email, cargo = :cargo, ativo = :ativo, tipo = :tipo 
                WHERE usuario_id = :id
            """)
            conn.execute(query, {
                "nome": data.get("nome"), "email": data.get("email"), "cargo": data.get("cargo"),
                "ativo": 1 if str(data.get("ativo")).strip().lower() in ("1", "true", "sim") else 0, "tipo": data.get("tipo", "Usuário"), "id": usuario_id
            })

            password = data.get("password")
            if password and password.strip():
                senha_hash = hash_password(password)
                conn.execute(text("UPDATE dbo.nps_usuarios SET senha_hash = :h WHERE usuario_id = :id"), {"h": senha_hash, "id": usuario_id})
            
            # --- AUDITORIA ---
            if str(data.get("ativo")) in ['1', 'true', 'True']:
                registrar_log(
                    acao="APROVACAO_USUARIO",
                    mensagem=f"O usuário {data.get('email')} teve o seu acesso aprovado/ativado.",
                    nivel="SUCCESS",
                    usuario_id=admin_id
                )

        return {"mensagem": "Usuário atualizado com sucesso"}
    except Exception as e:
        print(f"Erro ao atualizar: {e}")
        raise HTTPException(status_code=500, detail="Erro ao atualizar no banco")


# ==========================================
# 🚀 ROTAS DE CONFIGURAÇÕES DE E-MAIL
# ==========================================

@app.get("/api/config/email")
async def buscar_config_email():
    try:
        from database import get_engine
        from sqlalchemy import text
        from fastapi import HTTPException
        from services.crypto_svc import decrypt_data 
        
        engine = get_engine()
        with engine.connect() as conn:
            # 1. Busca as credenciais de e-mail
            query = text("SELECT * FROM dbo.nps_configuracoes_email LIMIT 1")
            res = conn.execute(query).fetchone()
            
            dados = dict(res._mapping) if res else {}
            
            # 🎯 2. Segredos nunca são devolvidos ao navegador
            dados.pop("client_secret", None)
            dados.pop("refresh_token", None)

            # Envio de e-mails da plataforma (ZeptoMail/Zoho ou Resend)
            dados["provedor"] = provedor_email() or "nao_configurado"
            dados["remetente_email"] = os.getenv("EMAIL_REMETENTE", "").strip() or dados.get("email_remetente") or ""
            dados["remetente_nome"] = os.getenv("EMAIL_REMETENTE_NOME", "").strip()
            
            # 3. Busca o estado da Chave Mestra e do SSO
            query_vars = text("SELECT chave, valor FROM dbo.nps_configuracoes WHERE chave IN ('envios_ativos', 'sso_microsoft_ativo')")
            res_vars = conn.execute(query_vars).fetchall()
            
            # O nome da variável devolvida ao Vue DEVE ser sso_microsoft_ativo
            dados["envios_ativos"] = True
            dados["sso_microsoft_ativo"] = False 
            
            for row in res_vars:
                if row.chave == 'envios_ativos':
                    dados["envios_ativos"] = str(row.valor).lower() == 'true'
                elif row.chave == 'sso_microsoft_ativo':
                    dados["sso_microsoft_ativo"] = str(row.valor).lower() == 'true' 
                
            return dados
            
    except Exception as e:
        from fastapi import HTTPException
        raise HTTPException(status_code=500, detail=str(e))

from services.crypto_svc import encrypt_data, decrypt_data

@app.post("/api/config/email")
async def salvar_config_email(config: ConfigEmailSchema, admin_email: str = Depends(exigir_admin)):
    engine = get_engine()
    try:
        # 🎯 ENCRIPTAMOS o segredo se ele foi preenchido no formulário
        secret_protegido = encrypt_data(config.client_secret) if config.client_secret else ""

        with engine.begin() as conn: 
            admin_id = conn.execute(
                text("SELECT usuario_id FROM dbo.nps_usuarios WHERE email = :e"), 
                {"e": admin_email}
            ).scalar()

            # 1. Atualiza as credenciais da Microsoft (Agora com proteção)
            existe = conn.execute(text("SELECT 1 FROM dbo.nps_configuracoes_email")).scalar()
            if existe:
                conn.execute(text("""
                    UPDATE dbo.nps_configuracoes_email 
                    SET tenant_id = :t, client_id = :c, 
                        client_secret = CASE WHEN :s = '' THEN client_secret ELSE :s END, 
                        email_remetente = :e, base_url_frontend = :b, atualizado_em = GETDATE()
                """), {
                    "t": config.tenant_id, 
                    "c": config.client_id, 
                    "s": secret_protegido, # 🛡️ Valor criptografado ou vazio
                    "e": config.email_remetente, 
                    "b": config.base_url_frontend
                })
            else:
                conn.execute(text("""
                    INSERT INTO dbo.nps_configuracoes_email (tenant_id, client_id, client_secret, email_remetente, base_url_frontend, atualizado_em)
                    VALUES (:t, :c, :s, :e, :b, GETDATE())
                """), {
                    "t": config.tenant_id, 
                    "c": config.client_id, 
                    "s": secret_protegido, 
                    "e": config.email_remetente, 
                    "b": config.base_url_frontend
                })
            
            # --- (O resto do seu código de logs e toggles permanece igual) ---
            sql_upsert_cfg = text("""
                INSERT INTO dbo.nps_configuracoes (chave, valor, updated_at)
                    VALUES (:chave, :valor, CURRENT_TIMESTAMP)
                    ON CONFLICT (conta_id, chave) DO UPDATE SET valor = EXCLUDED.valor, updated_at = CURRENT_TIMESTAMP
            """)

            # Toggle: Motor
            estado_motor = conn.execute(text("SELECT valor FROM dbo.nps_configuracoes WHERE chave = 'envios_ativos'")).scalar()
            novo_motor = 'true' if config.envios_ativos else 'false'
            if estado_motor != novo_motor:
                registrar_log(acao="CONFIG_MOTOR", mensagem=f"O usuário {'ATIVOU' if config.envios_ativos else 'DESATIVOU'} o Motor.", nivel="WARN", usuario_id=admin_id)
            conn.execute(sql_upsert_cfg, {"chave": "envios_ativos", "valor": novo_motor})

            # Toggle: Robô
            estado_robo = conn.execute(text("SELECT valor FROM dbo.nps_configuracoes WHERE chave = 'robo_ativo'")).scalar()
            novo_robo_bool = getattr(config, 'robo_ativo', False)
            novo_robo = 'true' if novo_robo_bool else 'false'
            if estado_robo != novo_robo:
                registrar_log(acao="CONFIG_ROBO", mensagem=f"O usuário {'ATIVOU' if novo_robo_bool else 'DESATIVOU'} o Robô.", nivel="WARN", usuario_id=admin_id)
            conn.execute(sql_upsert_cfg, {"chave": "robo_ativo", "valor": novo_robo})

            # SSO
            conn.execute(sql_upsert_cfg, {"chave": "sso_microsoft_ativo", "valor": 'true' if config.sso_microsoft_ativo else 'false'})

        return {"status": "sucesso", "mensagem": "Configurações e auditoria atualizadas."}
    
    except Exception as e:
        print(f"Erro ao salvar config e log: {e}")
        raise HTTPException(status_code=500, detail="Erro interno ao salvar configurações.")

@app.post("/api/config/email/autorizar")
async def autorizar_microsoft(requisicao: AutorizarEmailRequest):
    engine = get_engine()
    with engine.connect() as conn:
        config_row = conn.execute(text("""
            SELECT tenant_id, client_id, client_secret
            FROM dbo.nps_configuracoes_email LIMIT 1
        """)).fetchone()
        
        if not config_row:
            raise HTTPException(status_code=400, detail="Configurações não encontradas no banco.")

        config = dict(config_row._mapping)

        # 🔓 DESCRIPTOGRAFIA: Recuperamos o segredo real para falar com a Microsoft
        secret_real = decrypt_data(config['client_secret'])

        url = f"https://login.microsoftonline.com/{config['tenant_id']}/oauth2/v2.0/token"
        
        payload = {
            'client_id': config['client_id'],
            'client_secret': secret_real,
            'code': requisicao.code,
            'grant_type': 'authorization_code',
            
            # 🎯 Usa a URL dinâmica que o Vue enviou, zero hardcode!
            'redirect_uri': requisicao.redirect_uri, 
            
            'scope': 'offline_access mail.send'
        }
        
        headers = {'Content-Type': 'application/x-www-form-urlencoded'}
        
        # 🚀 Apenas UM pedido HTTP com os headers corretos
        response = requests.post(url, data=payload, headers=headers)
        res = response.json()

        if "refresh_token" not in res:
            print(f"❌ Erro Microsoft: {res}") 
            raise HTTPException(status_code=400, detail=res.get("error_description", "Falha no token"))

        # 🎯 ENCRIPTOGRAFIA: Protegemos o token devolvido antes de o salvar no SQL
        token_protegido = encrypt_data(res["refresh_token"])

        with engine.begin() as conn_tx:
            conn_tx.execute(text("UPDATE dbo.nps_configuracoes_email SET refresh_token = :rt, atualizado_em = GETDATE()"), 
                         {"rt": token_protegido})
        
    return {"status": "conectado"}

@app.post("/api/config/email/teste")
async def testar_envio_email(usuario_email: str = Depends(get_current_user)):
    try:
        from services.email_svc import enviar_email_teste
        
        FRONTEND_URL = os.getenv("FRONTEND_URL", "http://localhost:5173")
        link_teste = f"{FRONTEND_URL}/configuracoes"
        
        ok = enviar_email_teste(usuario_email)
        
        if ok:
            return {"status": "success", "message": "E-mail de teste enviado!"}
        else:
            raise HTTPException(status_code=500, detail="O motor de envio devolveu falha. Verifique o terminal do Python.")
            
    except Exception as e:
        print(f"❌ ERRO NO TESTE DE ENVIO: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))
    
@app.get("/api/configuracoes/dominios")
def get_dominios():
    try:
        engine = get_engine()
        with engine.connect() as conn:
            valor = conn.execute(text("SELECT valor FROM dbo.nps_configuracoes WHERE chave = 'dominios_permitidos'")).scalar()
            return {"dominios": valor if valor else ""}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.put("/api/configuracoes/dominios")
def update_dominios(dados: DominiosUpdate):
    try:
        engine = get_engine()
        with engine.begin() as conn:
            # Verifica se a chave já existe
            existe = conn.execute(text("SELECT 1 FROM dbo.nps_configuracoes WHERE chave = 'dominios_permitidos'")).scalar()
            
            if existe:
                conn.execute(text("UPDATE dbo.nps_configuracoes SET valor = :valor WHERE chave = 'dominios_permitidos'"), {"valor": dados.dominios})
            else:
                conn.execute(text("""
                    INSERT INTO dbo.nps_configuracoes (chave, valor, descricao) 
                    VALUES ('dominios_permitidos', :valor, 'Lista de domínios permitidos')
                """), {"valor": dados.dominios})
                
            return {"mensagem": "Domínios atualizados com sucesso!"}
    except Exception as e:
        raise HTTPException(status_code=500, detail="Erro ao salvar domínios.")
    
@app.get("/api/config/nps/elegiveis")
def contar_elegiveis_nps():
    """Conta corretamente quantos clientes estão prontos para receber o NPS hoje"""
    try:
        engine = get_engine()
        with engine.connect() as conn:
            sql = text("""
                SELECT COUNT(*) 
                FROM dbo.nps_clientes c
                LEFT JOIN dbo.nps_disparos d ON c.cliente_id = d.cliente_id
                WHERE c.ativo = 1 
                AND (
                    -- 1. Clientes que NUNCA receberam a pesquisa (campos de data nulos)
                    COALESCE(d.data_ultimo_lembrete, d.data_envio_inicial, c.ultimo_envio) IS NULL 
                    
                    OR 
                    
                    -- 2. Clientes que já cumpriram o tempo de carência (recorrencia_dias)
                    GETDATE() >= DATEADD('day', 
                        COALESCE((SELECT dbo.try_int(valor) FROM dbo.nps_configuracoes WHERE chave = 'recorrencia_dias' LIMIT 1), 90), 
                        COALESCE(d.data_ultimo_lembrete, d.data_envio_inicial, c.ultimo_envio)
                    )
                )
            """)
            
            resultado = conn.execute(sql).scalar()
            
            total = int(resultado) if resultado is not None else 0
            
        return {"total": total}
    except Exception as e:
        print(f"Erro ao contar elegíveis: {e}")
        raise HTTPException(status_code=500, detail="Erro ao calcular fila de disparos.")

@app.post("/api/config/nps/forcar-disparo")
def forcar_disparo_nps(background_tasks: BackgroundTasks):
    """Inicia a rotina de disparo imediatamente em segundo plano"""
    try:
        from services.email_svc import processar_disparos_nps
        # Adiciona a tarefa ao background para responder ao Frontend imediatamente
        background_tasks.add_task(_bg(processar_disparos_nps))
        return {"status": "success", "message": "Disparo iniciado com sucesso! A enviar em segundo plano."}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

# ==========================================
# 🖼️ GESTOR DE IMAGENS (E-MAIL TEMPLATES)
# ==========================================

os.makedirs("uploads", exist_ok=True)

app.mount("/uploads", StaticFiles(directory="uploads"), name="uploads")

def _pasta_imagens_da_conta() -> str:
    """Cada conta tem a sua pasta de imagens (uploads/c<id>)."""
    from database import conta_atual
    pasta = os.path.join("uploads", f"c{conta_atual()}")
    os.makedirs(pasta, exist_ok=True)
    return pasta


EXTENSOES_IMAGEM = ('.png', '.jpg', '.jpeg', '.gif', '.webp')


@app.post("/api/upload-imagem")
async def upload_imagem_email(file: UploadFile = File(...), request: Request = None):
    nome = os.path.basename(file.filename or "").replace(" ", "_")
    if not nome or not nome.lower().endswith(EXTENSOES_IMAGEM):
        raise HTTPException(status_code=400, detail="Envie uma imagem (png, jpg, gif ou webp).")
    try:
        pasta = _pasta_imagens_da_conta()
        with open(os.path.join(pasta, nome), "wb+") as file_object:
            shutil.copyfileobj(file.file, file_object)
        base_url = str(request.base_url).rstrip("/")
        return {"nome": nome, "url": f"{base_url}/{pasta}/{nome}"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
        
@app.get("/api/config/imagens")
def listar_imagens(request: Request):
    """Devolve as imagens hospedadas da conta logada"""
    try:
        base_url = str(request.base_url).rstrip("/")
        pasta = _pasta_imagens_da_conta()
        imagens = [
            {"nome": f, "url": f"{base_url}/{pasta}/{f}"}
            for f in os.listdir(pasta) if f.lower().endswith(EXTENSOES_IMAGEM)
        ]
        imagens.sort(key=lambda x: os.path.getmtime(os.path.join(pasta, x['nome'])), reverse=True)
        return imagens
    except Exception as e:
        return []

@app.delete("/api/config/imagens/{nome_arquivo}")
def remover_imagem(nome_arquivo: str):
    file_path = os.path.join(_pasta_imagens_da_conta(), os.path.basename(nome_arquivo))
    if os.path.exists(file_path):
        os.remove(file_path)
        return {"status": "success"}
    raise HTTPException(status_code=404, detail="Imagem não encontrada.")
    
# ==========================================
# 👤 GESTOR DE AVATARES DE PERFIL
# ==========================================
@app.post("/api/usuarios/me/avatar")
async def upload_meu_avatar(file: UploadFile = File(...), usuario_email: str = Depends(get_current_user), request: Request = None):
    try:
        engine = get_engine()
        with engine.begin() as conn:
            # 1. Busca dados do usuário
            user = conn.execute(text("SELECT usuario_id, avatar_url FROM dbo.nps_usuarios WHERE email = :e"), {"e": usuario_email}).mappings().first()
            if not user:
                raise HTTPException(status_code=404, detail="Usuário não encontrado.")

            # 2. Pasta de destino
            AVATAR_PATH = "uploads/avatars"
            os.makedirs(AVATAR_PATH, exist_ok=True)

            # 3. Nome único
            ext = os.path.splitext(file.filename)[1]
            novo_nome = f"avatar_{user['usuario_id']}_{uuid.uuid4().hex}{ext}"
            caminho_fisico = os.path.join(AVATAR_PATH, novo_nome)

            # 4. Grava o arquivo no disco
            with open(caminho_fisico, "wb") as buffer:
                shutil.copyfileobj(file.file, buffer)

            # 5. Limpeza da foto antiga (Lógica melhorada para caminhos relativos ou absolutos)
            if user.get('avatar_url'):
                # Extrai apenas o nome do arquivo, ignorando se era localhost ou relativo
                foto_antiga = user['avatar_url'].split('/')[-1]
                caminho_antigo = os.path.join(AVATAR_PATH, foto_antiga)
                if os.path.exists(caminho_antigo):
                    try: os.remove(caminho_antigo)
                    except: pass

            # 🎯 6. Salva apenas o caminho relativo (Ex: /uploads/avatars/foto.jpg)
            url_relativa = f"/uploads/avatars/{novo_nome}"
            
            conn.execute(text("UPDATE dbo.nps_usuarios SET avatar_url = :url WHERE usuario_id = :id"), 
                         {"url": url_relativa, "id": user['usuario_id']})
            
            # 7. Retorna a URL completa apenas para o Frontend exibir agora
            base_url = str(request.base_url).rstrip("/")
            return {"status": "success", "avatar_url": f"{base_url}{url_relativa}"}
            
    except Exception as e:
        print(f"❌ Erro no upload: {e}")
        raise HTTPException(status_code=500, detail="Erro ao processar imagem.")

@app.delete("/api/usuarios/me/avatar")
async def remover_meu_avatar(usuario_email: str = Depends(get_current_user)):
    """Remove a foto de perfil do usuário e devolve ao estado de iniciais"""
    try:
        engine = get_engine()
        with engine.begin() as conn:
            user = conn.execute(text("SELECT usuario_id, avatar_url FROM dbo.nps_usuarios WHERE email = :e"), {"e": usuario_email}).mappings().first()
            
            if user and user.get('avatar_url'):
                # Tenta apagar o arquivo fisicamente
                antigo_relativo = user['avatar_url'].split('/uploads/')[-1]
                antigo_fisico = os.path.join("uploads", antigo_relativo)
                if os.path.exists(antigo_fisico):
                    try: os.remove(antigo_fisico)
                    except: pass
                    
                # Limpa a coluna no banco
                conn.execute(text("UPDATE dbo.nps_usuarios SET avatar_url = NULL WHERE usuario_id = :id"), {"id": user['usuario_id']})
                
        return {"status": "success", "message": "Avatar removido"}
    except Exception as e:
        print(f"❌ Erro ao remover avatar: {e}")
        raise HTTPException(status_code=500, detail=str(e))

# ==========================================
# 🚀 ROTAS DE SESSÕES DE USUÁRIOS
# ==========================================

# --- ROTA PARA LISTAR SESSÕES REAIS ---
@app.get("/api/usuarios/sessoes")
async def listar_sessoes(usuario_id: int): # Em produção, pegamos o ID do Token JWT
    try:
        engine = get_engine()
        with engine.connect() as conn:
            query = text("""
                SELECT id, dispositivo, ip_address as ip, localizacao as local, 
                       criado_em as data, revogado
                FROM dbo.nps_sessoes_ativas 
                WHERE usuario_id = :uid AND revogado = 0
                ORDER BY criado_em DESC
            """)
            res = conn.execute(query, {"uid": usuario_id}).fetchall()
            return [dict(r._mapping) for r in res]
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

# --- ROTA PARA REVOGAR (ENCERRAR) SESSÃO ---
@app.delete("/api/usuarios/sessoes/{sessao_id}")
async def encerrar_sessao(sessao_id: int):
    try:
        engine = get_engine()
        with engine.connect() as conn:
            conn.execute(text("UPDATE dbo.nps_sessoes_ativas SET revogado = 1 WHERE id = :sid"), {"sid": sessao_id})
            conn.commit()
            return {"detail": "Sessão encerrada"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    
# ==========================================
# 🔒 ROTAS DE CONFIGURAÇÃO DE SEGURANÇA
# ==========================================

@app.get("/api/config/seguranca")
def obter_configuracoes_seguranca(usuario_email: str = Depends(get_current_user)):
    try:
        engine = get_engine()
        with engine.connect() as conn:
            # Vai buscar o tempo atual ao banco de dados
            query = text("SELECT valor FROM dbo.nps_configuracoes WHERE chave = 'sessao_expiracao_minutos'")
            resultado = conn.execute(query).scalar()
            
            # Se não encontrar ou houver erro, assume 60 minutos por segurança
            tempo = int(resultado) if resultado and str(resultado).isdigit() else 60
            
            return {"tempo_minutos": tempo}
            
    except Exception as e:
        print(f"Erro ao obter configuração de segurança: {e}")
        raise HTTPException(status_code=500, detail="Erro ao carregar configurações de segurança.")

@app.put("/api/config/seguranca")
def salvar_configuracoes_seguranca(payload: SegurancaConfig, usuario_email: str = Depends(get_current_user)):
    try:
        engine = get_engine()
        with engine.begin() as conn:
            conn.execute(text("""
                INSERT INTO dbo.nps_configuracoes (chave, valor, updated_at)
                    VALUES ('sessao_expiracao_minutos', :valor, CURRENT_TIMESTAMP)
                    ON CONFLICT (conta_id, chave) DO UPDATE SET valor = EXCLUDED.valor, updated_at = CURRENT_TIMESTAMP
            """), {"valor": str(payload.tempo_minutos)})
            
        return {"status": "success", "message": "Tempo de sessão atualizado com sucesso!"}
    except Exception as e:
        print(f"Erro ao salvar configuração de segurança: {e}")
        raise HTTPException(status_code=500, detail="Erro ao salvar configurações de segurança.")

# ==========================================
# 🛠️ FUNÇÃO AUXILIAR: MONTADOR DE FILTROS SQL
# ==========================================
def build_bi_filters(periodo: str, segmento: str, arr: str, safra: str):
    where_clauses = ["r.excluido = 0"]
    params = {}

    # 1. PERÍODO (Baseado na data da resposta)
    if periodo == "Últimos 3 Meses":
        where_clauses.append("r.data_resposta >= DATEADD('month', -3, GETDATE())")
    elif periodo == "Últimos 6 Meses":
        where_clauses.append("r.data_resposta >= DATEADD('month', -6, GETDATE())")
    elif periodo == "Este Ano":
        where_clauses.append("YEAR(r.data_resposta) = YEAR(GETDATE())")

    # 2. SEGMENTO
    if segmento != "Todos":
        where_clauses.append("e.segmento = :segmento")
        params["segmento"] = segmento

    # 3. ARR (Receita)
    if arr == "> € 100k":
        where_clauses.append("e.valor_contrato > 100000")
    elif arr == "€ 50k - € 100k":
        where_clauses.append("e.valor_contrato BETWEEN 50000 AND 100000")
    elif arr == "< € 50k":
        where_clauses.append("e.valor_contrato < 50000")

    # 4. SAFRA / TEMPO DE CASA (Assumindo que a empresa tem coluna 'created_at')
    # Se a sua coluna se chamar 'data_criacao', altere abaixo:
    if safra == "0-3 Meses (Onboarding)":
        where_clauses.append("DATEDIFF('month', COALESCE(e.created_at, GETDATE()), GETDATE()) <= 3")
    elif safra == "3-12 Meses":
        where_clauses.append("DATEDIFF('month', COALESCE(e.created_at, GETDATE()), GETDATE()) > 3 AND DATEDIFF('month', COALESCE(e.created_at, GETDATE()), GETDATE()) <= 12")
    elif safra == "+1 Ano":
        where_clauses.append("DATEDIFF('month', COALESCE(e.created_at, GETDATE()), GETDATE()) > 12")

    where_sql = " AND ".join(where_clauses)
    return where_sql, params


# ==========================================
# 📊 LABORATÓRIO ANALÍTICO (BI ENGINE)
# ==========================================

# --- 2. ROTA DE PERFORMANCE DO GESTOR ---
@app.get("/api/reports/gestor")
def obter_performance_gestor(gestor_id: int, usuario_email: str = Depends(get_current_user)):
    try:
        engine = get_engine()
        with engine.connect() as conn:
            # 1. Indicadores Gerais com COALESCE para evitar NoneType
            sql_nps = text("""
                SELECT 
                    COUNT(r.resposta_id) as total,
                    COALESCE(SUM(CASE WHEN r.nota >= 9 THEN 1 ELSE 0 END), 0) as promotores,
                    COALESCE(SUM(CASE WHEN r.nota <= 6 THEN 1 ELSE 0 END), 0) as detratores
                FROM dbo.nps_respostas r
                INNER JOIN dbo.nps_clientes c ON r.cliente_id = c.cliente_id
                INNER JOIN dbo.nps_empresas e ON c.empresa_id = e.id
                WHERE e.gestor_id = :gestor_id
            """)
            res = conn.execute(sql_nps, {"gestor_id": gestor_id}).mappings().first()
            
            total = res['total'] or 0
            promotores = res['promotores']
            detratores = res['detratores']
            neutros = total - (promotores + detratores)

            # 2. Cálculo do NPS seguro
            nps = 0
            if total > 0:
                nps = ((promotores - detratores) / total) * 100

            # 3. Ranking de empresas da carteira
            sql_empresas = text("""
                SELECT 
                    e.nome,
                    COALESCE(AVG(CAST(r.nota AS FLOAT)), 0) as media_nota,
                    COUNT(r.resposta_id) as qtd_respostas
                FROM dbo.nps_empresas e
                LEFT JOIN dbo.nps_respostas r ON e.nome = r.empresa
                WHERE e.gestor_id = :gestor_id
                GROUP BY e.nome
                ORDER BY media_nota DESC
            """)
            empresas_perf = conn.execute(sql_empresas, {"gestor_id": gestor_id}).mappings().all()

            return {
                "nps": round(nps, 1),
                "total_respostas": total,
                "distribuicao": {
                    "promotores": promotores,
                    "detratores": detratores,
                    "neutros": neutros
                },
                "ranking_empresas": [dict(row) for row in empresas_perf]
            }
    except Exception as e:
        print(f"Erro na performance do gestor: {e}")
        raise HTTPException(status_code=500, detail=str(e))

# 1. MATRIZ DE PRIORIZAÇÃO (SCATTER CHART)
@app.get("/api/reports/bi-scatter")
async def get_bi_scatter(periodo: str = Query("Últimos 6 Meses"), segmento: str = Query("Todos"), arr: str = Query("Todos"), safra: str = Query("Todos")):
    try:
        where_sql, params = build_bi_filters(periodo, segmento, arr, safra)
        
        engine = get_engine()
        with engine.connect() as conn:
            # Fazemos o JOIN com a empresa para que os filtros de segmento e ARR funcionem
            sql = text(f"""
                SELECT 
                    COALESCE(r.categoria, 'Sem Classificação') as tema,
                    COUNT(r.resposta_id) as frequencia,
                    AVG(CAST(r.nota AS FLOAT)) as nota_media
                FROM dbo.nps_respostas r
                LEFT JOIN dbo.nps_empresas e ON r.empresa_id = e.id
                WHERE {where_sql} AND r.categoria IS NOT NULL
                GROUP BY r.categoria
                HAVING COUNT(r.resposta_id) > 1
            """)
            
            resultados = conn.execute(sql, params).mappings().all()
            
            scatter_data = [{"x": r['frequencia'], "y": round(r['nota_media'], 1), "r": 8, "tema": r['tema']} for r in resultados]
            return scatter_data
    except Exception as e:
        print(f"❌ Erro BI Scatter: {e}")
        return []

# 2. ANÁLISE DE SAFRA (STACKED BAR) - AGORA COM DADOS REAIS
@app.get("/api/reports/bi-safra")
async def get_bi_safra(periodo: str = Query("Últimos 6 Meses"), segmento: str = Query("Todos"), arr: str = Query("Todos"), safra: str = Query("Todos")):
    try:
        where_sql, params = build_bi_filters(periodo, segmento, arr, safra)
        
        engine = get_engine()
        with engine.connect() as conn:
            sql = text(f"""
                SELECT 
                    CASE 
                        WHEN DATEDIFF('month', e.created_at, GETDATE()) <= 3 THEN '0-3 Meses'
                        WHEN DATEDIFF('month', e.created_at, GETDATE()) <= 6 THEN '3-6 Meses'
                        WHEN DATEDIFF('month', e.created_at, GETDATE()) <= 12 THEN '6-12 Meses'
                        ELSE '+1 Ano'
                    END as safra_grupo,
                    SUM(CASE WHEN r.nota >= 9 THEN 1 ELSE 0 END) as promotores,
                    SUM(CASE WHEN r.nota BETWEEN 7 AND 8 THEN 1 ELSE 0 END) as neutros,
                    SUM(CASE WHEN r.nota <= 6 THEN 1 ELSE 0 END) as detratores
                FROM dbo.nps_respostas r
                INNER JOIN dbo.nps_empresas e ON r.empresa_id = e.id
                WHERE {where_sql}
                GROUP BY 
                    CASE 
                        WHEN DATEDIFF('month', e.created_at, GETDATE()) <= 3 THEN '0-3 Meses'
                        WHEN DATEDIFF('month', e.created_at, GETDATE()) <= 6 THEN '3-6 Meses'
                        WHEN DATEDIFF('month', e.created_at, GETDATE()) <= 12 THEN '6-12 Meses'
                        ELSE '+1 Ano'
                    END
            """)
            
            resultados = conn.execute(sql, params).mappings().all()
            
            # Estrutura base de retorno
            data = {
                "labels": ['0-3 Meses', '3-6 Meses', '6-12 Meses', '+1 Ano'],
                "promotores": [0, 0, 0, 0],
                "neutros": [0, 0, 0, 0],
                "detratores": [0, 0, 0, 0]
            }
            
            # Preenche o json com os totais reais do banco
            for r in resultados:
                if r['safra_grupo'] in data['labels']:
                    idx = data['labels'].index(r['safra_grupo'])
                    data['promotores'][idx] = r['promotores']
                    data['neutros'][idx] = r['neutros']
                    data['detratores'][idx] = r['detratores']
                    
            return data
    except Exception as e:
        print(f"❌ Erro BI Safra: {e}")
        return {"labels": [], "promotores": [], "neutros": [], "detratores": []}

# 3. RISCO FINANCEIRO (BUBBLE CHART)
@app.get("/api/reports/bi-risco")
async def get_bi_risco(periodo: str = Query("Últimos 6 Meses"), segmento: str = Query("Todos"), arr: str = Query("Todos"), safra: str = Query("Todos")):
    try:
        where_sql, params = build_bi_filters(periodo, segmento, arr, safra)
        
        engine = get_engine()
        with engine.connect() as conn:
            sql = text(f"""
                SELECT 
                    e.id as empresa_id,
                    e.nome as nome_empresa,
                    COUNT(r.resposta_id) as total_respostas,
                    SUM(CASE WHEN r.nota >= 9 THEN 1 ELSE 0 END) as promotores,
                    SUM(CASE WHEN r.nota <= 6 THEN 1 ELSE 0 END) as detratores,
                    MAX(COALESCE(e.valor_contrato, 0)) as arr
                FROM dbo.nps_respostas r
                INNER JOIN dbo.nps_empresas e ON r.empresa_id = e.id
                WHERE {where_sql}
                GROUP BY e.id, e.nome
            """)
            
            resultados = conn.execute(sql, params).mappings().all()
            
            bolhas = []
            for r in resultados:
                if r['total_respostas'] > 0:
                    nps = round(((r['promotores'] - r['detratores']) / r['total_respostas']) * 100)
                    bolhas.append({
                        "id": r['empresa_id'], 
                        "x": nps, 
                        "y": float(r['arr']), 
                        "r": min(max(r['total_respostas'] * 2, 5), 30), 
                        "empresa": r['nome_empresa']
                    })
                    
            return bolhas
    except Exception as e:
        print(f"❌ Erro BI Risco: {e}")
        return []

# 4. GAUGE AI - CONSULTORIA PARETO
@app.get("/api/reports/bi-ia")
async def get_bi_ia_reports(periodo: str = Query("Últimos 6 Meses"), segmento: str = Query("Todos"), arr: str = Query("Todos"), safra: str = Query("Todos")):
    try:
        where_sql, params = build_bi_filters(periodo, segmento, arr, safra)
        
        engine = get_engine()
        with engine.connect() as conn:
            from services.config_svc import ia_disponivel, registrar_uso_ia
            api_key, msg_ia = ia_disponivel()
            
            if not api_key:
                return {
                    "resumoParetoIA": msg_ia, 
                    "recomendacaoIA": ""
                }

            # 👉 BUSCAMOS O CONTEXTO REAL PARA ALIMENTAR A IA
            sql_contexto = text(f"""
                SELECT 
                    COUNT(r.resposta_id) as total_respostas,
                    SUM(CASE WHEN r.nota <= 6 THEN 1 ELSE 0 END) as total_detratores,
                    SUM(CASE WHEN r.nota >= 9 THEN 1 ELSE 0 END) as total_promotores
                FROM dbo.nps_respostas r
                LEFT JOIN dbo.nps_empresas e ON r.empresa_id = e.id
                WHERE {where_sql}
            """)
            dados = conn.execute(sql_contexto, params).mappings().first()
            
        # Proteção contra bases vazias
        if not dados or dados['total_respostas'] == 0:
            return {
                "resumoParetoIA": f"Não foram encontradas respostas no período de <strong>{periodo}</strong> para os filtros selecionados.",
                "recomendacaoIA": "Experimente alargar o seu intervalo de pesquisa ou remover alguns filtros."
            }

        client = openai.OpenAI(api_key=str(api_key).strip())
        registrar_uso_ia()
        
        prompt = f"""
        Atue como a 'Rakiti AI', um Consultor Sênior de Sucesso do Cliente. Responda em português do Brasil, com linguagem simples para pequenas e médias empresas.
        
        CONTEXTO ATUAL (Filtros aplicados pelo usuário):
        - Período: {periodo}
        - Segmento: {segmento}
        - Tamanho/ARR: {arr}
        - Tempo de Casa (Safra): {safra}
        
        DADOS DESTE CORTE:
        - Total de Respostas: {dados['total_respostas']}
        - Detratores: {dados['total_detratores']}
        - Promotores: {dados['total_promotores']}
        
        Crie um parecer executivo divido em duas partes:
        1. "resumoParetoIA": Um parágrafo detalhado (usando tags HTML como <strong> para negrito) explicando a situação deste grupo de clientes. Foque-se no risco de churn.
        2. "recomendacaoIA": Uma recomendação tática, clara e direta do que o time de CS deve fazer nesta semana para este grupo de segmentação.
        
        Responda estritamente em JSON com as chaves "resumoParetoIA" e "recomendacaoIA".
        """

        response = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[{"role": "system", "content": prompt}],
            temperature=0.7, 
            response_format={ "type": "json_object" }
        )
        
        return json.loads(response.choices[0].message.content)

    except Exception as e:
        print(f"❌ Erro na BI IA: {e}")
        return {
            "resumoParetoIA": "Analisando os filtros aplicados, identificamos uma falha de conexão com o motor cognitivo.",
            "recomendacaoIA": "Por favor, tente gerar a análise novamente."
        }
    
# 5. NPS POR SEGMENTO (BAR CHART) - FOCO NA EMPRESA
@app.get("/api/reports/bi-segmento")
async def get_bi_segmento(periodo: str = Query("Últimos 6 Meses"), segmento: str = Query("Todos"), arr: str = Query("Todos"), safra: str = Query("Todos")):
    try:
        where_sql, params = build_bi_filters(periodo, segmento, arr, safra)
        
        engine = get_engine()
        with engine.connect() as conn:
            # 💡 A MUDANÇA: O agrupamento agora é 100% baseado na tabela de Empresas (e)
            sql = text(f"""
                SELECT 
                    COALESCE(e.segmento, 'Sem Segmento') as segmento,
                    COUNT(r.resposta_id) as total_respostas,
                    ROUND(
                        (SUM(CASE WHEN r.nota >= 9 THEN 1.0 ELSE 0 END) / NULLIF(COUNT(r.resposta_id), 0) * 100) - 
                        (SUM(CASE WHEN r.nota <= 6 THEN 1.0 ELSE 0 END) / NULLIF(COUNT(r.resposta_id), 0) * 100), 0
                    ) as nps
                FROM dbo.nps_respostas r
                -- O vínculo crucial é r.empresa_id -> e.id
                INNER JOIN dbo.nps_empresas e ON r.empresa_id = e.id 
                WHERE {where_sql} 
                  AND (r.excluido = 0 OR r.excluido IS NULL)
                GROUP BY COALESCE(e.segmento, 'Sem Segmento')
                ORDER BY nps DESC
            """)
            
            resultados = conn.execute(sql, params).mappings().all()
            return [dict(r) for r in resultados]
            
    except Exception as e:
        print(f"❌ Erro BI Segmento: {e}")
        return []

@app.get("/api/reports/jornada")
def relatorio_jornada(empresa: str, data_inicio: str = None, data_fim: str = None):
    """Retorna o histórico de feedbacks e o NPS exato de uma empresa específica"""
    try:
        engine = get_engine()
        with engine.connect() as conn:
            # 1. Prepara os filtros dinâmicos de Data
            filtro_data = ""
            params = {"empresa": empresa}
            
            if data_inicio and data_fim:
                filtro_data = "AND COALESCE(r.data_resposta, r.created_at) BETWEEN :data_inicio AND :data_fim"
                params["data_inicio"] = f"{data_inicio} 00:00:00"
                params["data_fim"] = f"{data_fim} 23:59:59"

            # =========================================================
            # 💡 CORREÇÃO: Usar exatamente a mesma fórmula do Dashboard!
            # =========================================================
            sql_nps = text(f"""
                SELECT 
                    COUNT(r.resposta_id) as total_respostas,
                    ROUND(
                        (SUM(CASE WHEN r.nota >= 9 THEN 1.0 ELSE 0 END) / NULLIF(COUNT(r.resposta_id), 0) * 100) - 
                        (SUM(CASE WHEN r.nota <= 6 THEN 1.0 ELSE 0 END) / NULLIF(COUNT(r.resposta_id), 0) * 100), 0
                    ) as nps_atual
                FROM dbo.nps_respostas r
                LEFT JOIN dbo.nps_clientes c ON r.cliente_id = c.cliente_id
                LEFT JOIN dbo.nps_empresas e ON c.empresa_id = e.id
WHERE e.nome = :empresa
                AND (r.excluido = 0 OR r.excluido IS NULL) -- 👈 O SEGREDO ESTÁ AQUI: Ignorar apagados!
                {filtro_data}
            """)
            
            resultado_nps = conn.execute(sql_nps, params).fetchone()
            
            # 2. Busca a Linha do Tempo (Timeline)
            sql_historico = text(f"""
                SELECT 
                    r.nota, 
                    c.nome as cliente_nome, 
                    COALESCE(c.cargo, 'Sem Cargo') as cargo, 
                    r.motivo, 
                    COALESCE(r.data_resposta, r.created_at) as data_bruta, 
                    'E-mail' as canal -- 👈 CORRIGIDO: Removido o r.origem que causava o erro 207
                FROM dbo.nps_respostas r
                LEFT JOIN dbo.nps_clientes c ON r.cliente_id = c.cliente_id
                LEFT JOIN dbo.nps_empresas e ON c.empresa_id = e.id
                WHERE e.nome = :empresa
                AND (r.excluido = 0 OR r.excluido IS NULL)
                {filtro_data}
                ORDER BY COALESCE(r.data_resposta, r.created_at) DESC
            """)
            
            # Converte os resultados do SQL para uma lista de dicionários
            historico = [dict(row) for row in conn.execute(sql_historico, params).mappings().all()]
            
            # Formata a data no formato legível para o Frontend (DD/MM/YYYY)
            for item in historico:
                if item.get("data_bruta"):
                    item["data_formatada"] = item["data_bruta"].strftime("%d/%m/%Y")
                else:
                    item["data_formatada"] = "---"

            # 3. Retorna os dados normalizados
            return {
                "nps_atual": int(resultado_nps.nps_atual) if resultado_nps and resultado_nps.nps_atual is not None else 0,
                "total_respostas": int(resultado_nps.total_respostas) if resultado_nps else 0,
                "historico": historico
            }
            
    except Exception as e:
        import traceback
        print(f"Erro ao gerar Jornada: {traceback.format_exc()}")
        raise HTTPException(status_code=500, detail="Erro interno ao calcular a jornada do cliente.")

# --- ROTA PARA A ABA OPERACIONAL (KPIs DE EXECUÇÃO) ---
@app.get("/api/reports/operacional")
def obter_dados_operacionais(
    data_inicio: Optional[str] = Query(None), 
    data_fim: Optional[str] = Query(None),
    usuario_email: str = Depends(get_current_user)
):
    # Preparação de parâmetros para evitar SQL Injection e tratar valores nulos
    params = {
        "inicio": f"{data_inicio} 00:00:00" if data_inicio else None,
        "fim": f"{data_fim} 23:59:59" if data_fim else None
    }
    
    try:
        engine = get_engine()
        with engine.connect() as conn:
            # 1. Taxa de Resposta (Injetando o filtro de data no numerador)
            sql_taxa = text("""
                SELECT 
                    -- Denominador: Todos os clientes ativos (Base Real)
                    (SELECT COUNT(*) FROM dbo.nps_clientes WHERE ativo = 1) as total_base,
                    
                    -- Numerador: Respondentes únicos que estão ativos e dentro do período
                    (SELECT COUNT(DISTINCT r.cliente_id) 
                     FROM dbo.nps_respostas r
                     INNER JOIN dbo.nps_clientes c ON r.cliente_id = c.cliente_id
                     WHERE c.ativo = 1 
                       AND r.excluido = 0
                       AND (CAST(:inicio AS TIMESTAMP) IS NULL OR r.data_resposta >= CAST(:inicio AS TIMESTAMP))
                       AND (CAST(:fim AS TIMESTAMP) IS NULL OR r.data_resposta <= CAST(:fim AS TIMESTAMP))
                    ) as total_respostas
            """)
            res_taxa = conn.execute(sql_taxa, params).mappings().first()
            
            # 2. SLA Médio de Fechamento (Ajustado para usar os params corretamente)
            sql_sla = text("""
                SELECT 
                    AVG(CAST(DATEDIFF('minute', created_at, updated_at) AS FLOAT) / 60.0 / 24.0) as sla_real_dias
                FROM dbo.nps_acoes 
                WHERE status = 'Concluído' 
                  AND updated_at IS NOT NULL 
                  AND updated_at >= created_at
                  AND (CAST(:inicio AS TIMESTAMP) IS NULL OR updated_at >= CAST(:inicio AS TIMESTAMP))
                  AND (CAST(:fim AS TIMESTAMP) IS NULL OR updated_at <= CAST(:fim AS TIMESTAMP))
            """)
            res_sla = conn.execute(sql_sla, params).scalar() or 0

            return {
                "taxa_resposta": round((res_taxa['total_respostas'] / res_taxa['total_base'] * 100), 1) if res_taxa['total_base'] > 0 else 0,
                "sla_medio_dias": round(res_sla, 1)
            }
    except Exception as e:
        print(f"Erro Operacional: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))
    
@app.get("/api/reports/lista-gestores")
def obter_lista_gestores_com_empresas(usuario_email: str = Depends(get_current_user)):
    try:
        engine = get_engine()
        with engine.connect() as conn:
            sql = text("""
                SELECT id, nome 
                FROM dbo.nps_gestores 
                ORDER BY nome
            """)
            result = conn.execute(sql).fetchall()
            return [{"id": linha[0], "nome": linha[1]} for linha in result]
    except Exception as e:
        raise HTTPException(status_code=500, detail="Erro ao processar lista de gestores")
    
@app.get("/api/reports/operacional/inativos")
def relatorio_clientes_inativos(usuario_email: str = Depends(get_current_user)):
    try:
        engine = get_engine()
        with engine.connect() as conn:
            # 1. Puxa a regra de recorrência atual
            sql_regra = text("SELECT dbo.try_int(valor) FROM dbo.nps_configuracoes WHERE chave = 'recorrencia_dias' LIMIT 1")
            recorrencia_dias = conn.execute(sql_regra).scalar()
            recorrencia_dias = recorrencia_dias if recorrencia_dias is not None else 90

            # 2. Busca os clientes "Vencidos" (Disparados, não respondidos e que ultrapassaram a data limite)
            sql = text("""
                SELECT 
                    COALESCE(c.empresa, 'Sem Empresa') as empresa,
                    c.nome as cliente_nome,
                    c.email as cliente_email,
                    -- Pega a data mais recente de interação (envio ou resposta)
                    MAX(COALESCE(d.data_ultimo_lembrete, d.data_envio_inicial, c.ultimo_envio)) as data_envio,
                    DATEDIFF('day', MAX(COALESCE(d.data_ultimo_lembrete, d.data_envio_inicial, c.ultimo_envio)), GETDATE()) as dias_sem_resposta
                FROM dbo.nps_clientes c
                LEFT JOIN dbo.nps_empresas e ON c.empresa_id = e.id
                LEFT JOIN dbo.nps_disparos d ON c.cliente_id = d.cliente_id
                
                -- 👇 A CORREÇÃO ENTRA AQUI NO WHERE 👇
                WHERE c.ativo = 1 
                AND (e.ativo = 1 OR e.ativo IS NULL) -- Garante que a empresa do cliente também não deu churn
                
                -- Filtros normais da sua query (exemplo):
                -- AND DATEDIFF('day', ..., GETDATE()) > recorrencia_dias
                
                GROUP BY c.empresa, c.nome, c.email
                ORDER BY dias_sem_resposta DESC
            """)
            
            result = conn.execute(sql, {"recorrencia": recorrencia_dias}).fetchall()
            
            lista = [
                {
                    "empresa": r[0] or "Sem Empresa",
                    "cliente_nome": r[1],
                    "cliente_email": r[2],
                    "data_envio": r[3].isoformat() if r[3] else None,
                    "dias_sem_resposta": r[4]
                }
                for r in result
            ]
            
            return {"recorrencia_dias": recorrencia_dias, "lista": lista}
            
    except Exception as e:
        print(f"Erro no relatorio inativos: {e}")
        raise HTTPException(status_code=500, detail="Erro ao buscar clientes inativos.")

# ==========================================
# 🎯 ROTAS: PLANOS DE AÇÃO (Close the Loop)
# ==========================================

@app.post("/api/acoes")
def criar_acao(acao: AcaoCriar):
    try:
        engine = get_engine()
        with engine.begin() as conn:
            sql = text("""
                INSERT INTO dbo.nps_acoes 
                (resposta_id, empresa_id, gestor_id, titulo, descricao, resolucao, prioridade, prazo_limite)
                VALUES (:rid, :eid, :gid, :t, :d, :resol, :p, :pl)
            """)
            conn.execute(sql, {
                "rid": acao.resposta_id, 
                "eid": acao.empresa_id, 
                "gid": acao.gestor_id,
                "t": acao.titulo, 
                "d": acao.descricao, 
                "resol": acao.resolucao, # 🎯 Persistindo resolucao
                "p": acao.prioridade, 
                "pl": acao.prazo_limite if acao.prazo_limite else None
            })
        return {"status": "success", "message": "Ação criada com sucesso!"}
    except Exception as e:
        print(f"Erro ao criar ação: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/api/acoes")
def listar_acoes(gestor_id: Optional[int] = None, status: Optional[str] = None):
    try:
        engine = get_engine()
        with engine.connect() as conn:
            filtros = []
            params = {}
            
            if gestor_id:
                filtros.append("a.gestor_id = :gid")
                params["gid"] = gestor_id
            if status and status != "Todas":
                filtros.append("a.status = :status")
                params["status"] = status
                
            condicao = " WHERE " + " AND ".join(filtros) if filtros else ""

            # String SQL limpa (sem emojis ou comentários internos que quebram o driver)
            sql = text(f"""
                SELECT 
                    a.id,
                    a.titulo,
                    a.descricao,
                    a.resolucao,
                    a.status,
                    a.prioridade,
                    a.prazo_limite,
                    a.gestor_id,
                    a.empresa_id,
                    a.created_at,
                    COALESCE(e.nome, r.empresa, c.empresa, 'Conta Geral') as empresa_nome,
                    COALESCE(g.nome, e.gestor, 'Sem Gestor') as gestor_nome,
                    g.avatar as gestor_avatar,
                    r.nota as resposta_nota
                FROM dbo.nps_acoes a
                LEFT JOIN dbo.nps_empresas e ON a.empresa_id = e.id
                LEFT JOIN dbo.nps_respostas r ON a.resposta_id = r.resposta_id
                LEFT JOIN dbo.nps_clientes c ON r.cliente_id = c.cliente_id
                LEFT JOIN dbo.nps_gestores g ON a.gestor_id = g.id
                {condicao}
                ORDER BY 
                    CASE a.status 
                        WHEN 'Pendente' THEN 1 
                        WHEN 'Em Andamento' THEN 2 
                        WHEN 'Concluído' THEN 3 
                    END,
                    a.prazo_limite ASC, 
                    a.created_at DESC
            """)
                        
            resultados = conn.execute(sql, params).mappings().all()
            return [dict(r) for r in resultados]
    except Exception as e:
        print(f"ERRO CRÍTICO SQL: {str(e)}")
        raise HTTPException(status_code=500, detail="Erro ao listar ações. Verifique se a coluna 'resolucao' existe no banco.")

@app.put("/api/acoes/{acao_id}")
def atualizar_acao(acao_id: int, acao: AcaoAtualizar):
    try:
        engine = get_engine()
        with engine.begin() as conn:
            sql = text("""
                UPDATE dbo.nps_acoes 
                SET status = COALESCE(:s, status),
                    prioridade = COALESCE(:p, prioridade),
                    descricao = COALESCE(:d, descricao),
                    resolucao = COALESCE(:resol, resolucao), -- 🎯 Persistindo resolucao
                    prazo_limite = COALESCE(:pl, prazo_limite),
                    gestor_id = COALESCE(:gid, gestor_id),
                    empresa_id = COALESCE(:eid, empresa_id),
                    updated_at = CURRENT_TIMESTAMP
                WHERE id = :id
            """)
            conn.execute(sql, {
                "id": acao_id, 
                "s": acao.status, 
                "p": acao.prioridade, 
                "d": acao.descricao, 
                "resol": acao.resolucao,
                "pl": acao.prazo_limite, 
                "gid": acao.gestor_id, 
                "eid": acao.empresa_id
            })
        return {"status": "success", "message": "Ação atualizada!"}
    except Exception as e:
        print(f"Erro ao atualizar ação: {e}")
        raise HTTPException(status_code=500, detail=str(e))
    
@app.delete("/api/acoes/{acao_id}")
def excluir_acao(acao_id: int):
    try:
        engine = get_engine()
        with engine.begin() as conn:
            # Remove a ação pelo ID único
            sql = text("DELETE FROM dbo.nps_acoes WHERE id = :id")
            conn.execute(sql, {"id": acao_id})
        return {"status": "success", "message": "Ação excluída com sucesso!"}
    except Exception as e:
        print(f"Erro ao excluir ação: {e}")
        raise HTTPException(status_code=500, detail=str(e))
    

@app.get("/api/cadastros/corrigir-historico")
def corrigir_historico_nomes(tabela: str, coluna: str, de_nome: str, para_nome: str):
    """Rota de emergência para corrigir nomes em massa no histórico"""
    try:
        from sqlalchemy import text
        engine = get_engine()
        with engine.begin() as conn:
            permitidos = {
                "dbo.nps_respostas": {"empresa"}, "dbo.nps_clientes": {"empresa", "gestor", "cargo"},
                "dbo.nps_empresas": {"nome", "gestor", "segmento", "companhia"},
            }
            if tabela not in permitidos or coluna not in permitidos[tabela]:
                return {"erro": "Tabela ou coluna não permitida."}
            sql = text(f"UPDATE {tabela} SET {coluna} = :para WHERE {coluna} = :de")
            resultado = conn.execute(sql, {"para": para_nome, "de": de_nome})
            linhas_afetadas = resultado.rowcount
            
        return {
            "status": "Sucesso", 
            "mensagem": f"Foram atualizados {linhas_afetadas} registros de '{de_nome}' para '{para_nome}' na tabela {tabela}!"
        }
    except Exception as e:
        return {"erro": str(e)}
    
# ==========================================
# 🎯 LOGS: Tela de Logs
# ==========================================

@app.get("/api/logs")
def listar_logs(usuario: Any = Depends(exigir_admin)):
    """Retorna os logs de auditoria do sistema (Últimos 200)"""
    try:
        engine = get_engine()
        with engine.connect() as conn:
            query = text("""
                SELECT 
                    l.id, 
                    l.nivel, 
                    l.acao, 
                    l.mensagem, 
                    l.data_criacao,
                    u.nome as usuario_nome
                FROM dbo.nps_logs l
                LEFT JOIN dbo.nps_usuarios u ON l.usuario_id = u.usuario_id
                ORDER BY l.data_criacao DESC LIMIT 200
            """)
            
            resultados = conn.execute(query).mappings().all()
            
            return [
                {
                    "id": r["id"],
                    "nivel": r["nivel"],
                    "acao": r["acao"],
                    "mensagem": r["mensagem"],
                    "usuario_nome": r["usuario_nome"] or "🤖 Sistema",
                    "data_criacao": r["data_criacao"].isoformat() if r["data_criacao"] else None
                }
                for r in resultados
            ]
    except Exception as e:
        print(f"❌ ERRO CRÍTICO LOGS: {str(e)}")
        return []
    
def registrar_log(acao: str, mensagem: str, nivel: str = 'INFO', usuario_id: int = None):
    """
    Grava eventos críticos na tabela de auditoria.
    Níveis permitidos: 'INFO', 'SUCCESS', 'WARN', 'ERROR'
    """
    try:
        from database import get_engine
        from sqlalchemy import text
        
        engine = get_engine()
        with engine.begin() as conn:  # .begin() faz o commit automático
            sql = text("""
                INSERT INTO dbo.nps_logs (nivel, acao, mensagem, usuario_id)
                VALUES (:nivel, :acao, :mensagem, :usuario_id)
            """)
            conn.execute(sql, {
                "nivel": nivel,
                "acao": acao,
                "mensagem": mensagem,
                "usuario_id": usuario_id
            })
    except Exception as e:
        # Se o log falhar, o sistema não deve parar, apenas avisa no terminal
        print(f"🚨 Falha crítica ao gravar log no banco: {e}")

@app.get("/api/logs/emails")
def listar_logs_emails():
    try:
        engine = get_engine()
        # 🎯 Alteramos para usar a coluna REAL 'assunto' e melhorar a 'mensagem'
        sql = text("""
            SELECT 
                id, 
                nome as nome_cliente, 
                email as destinatario, 
                -- 1. Usa o assunto gravado no banco. Se for nulo, tenta identificar pelo link
                COALESCE(assunto, 
                    CASE 
                        WHEN survey_url LIKE '%verificar-email%' THEN 'Verificação de Conta'
                        WHEN survey_url LIKE '%redefinir-senha%' THEN 'Recuperação de Acesso'
                        WHEN survey_url LIKE '%fillout%' OR survey_url LIKE '%/r/%' THEN 'Convite de Pesquisa NPS'
                        ELSE 'Notificação de Sistema' 
                    END
                ) as assunto, 
                
                status, 
                
                -- 2. Monta o log técnico para o modal do Frontend
                CONCAT(
                    '📧 Assunto: ', COALESCE(assunto, 'N/A'), CHR(10),
                    '📍 URL/Link: ', COALESCE(survey_url, 'N/A'), CHR(10), CHR(10),
                    '⚠️ Log de Erro: ', COALESCE(erro_msg, 'Disparo realizado com sucesso.')
                ) as mensagem, 
                
                COALESCE(data_envio_inicial, created_at) as data_envio
            FROM dbo.nps_disparos
            ORDER BY created_at DESC
        """)
        
        with engine.connect() as conn:
            # Usando mappings().all() para garantir compatibilidade com o que o Vue espera
            resultados = conn.execute(sql).mappings().all()
            return [dict(r) for r in resultados]
            
    except Exception as e:
        print(f"❌ Erro ao listar logs: {e}")
        raise HTTPException(status_code=500, detail=str(e))


# ==========================================
# 🏢 CONTA (empresa cliente) E ADMINISTRAÇÃO DA PLATAFORMA
# ==========================================
class NovaContaRequest(BaseModel):
    nome: str
    admin_nome: str
    admin_email: str
    admin_senha: str
    dominios: Optional[str] = ""
    status_assinatura: Optional[str] = "cortesia"   # cortesia | teste


def _url_publica_api(request: Request) -> str:
    return (os.getenv("API_PUBLIC_URL", "").strip() or str(request.base_url)).rstrip("/")


@app.get("/api/conta")
def dados_da_conta(request: Request, usuario_email: str = Depends(get_current_user)):
    """Dados da conta logada, incluindo a URL (com token) para configurar o webhook no Fillout."""
    from database import conta_atual, modo_sistema as _ms
    conta_id = conta_atual()
    with _ms():
        with get_engine().connect() as conn:
            conta = conn.execute(text("SELECT id, nome, plano, webhook_token FROM dbo.nps_contas WHERE id = :id"), {"id": conta_id}).mappings().first()
    if not conta:
        raise HTTPException(status_code=404, detail="Conta não encontrada.")
    return {
        "id": conta["id"],
        "nome": conta["nome"],
        "plano": conta["plano"],
        "webhook_url": f"{_url_publica_api(request)}/api/webhook/fillout?token={conta['webhook_token']}",
        "csat_api_url": f"{_url_publica_api(request)}/api/integracao/csat",
        "api_key": conta["webhook_token"],
    }


@app.post("/api/conta/webhook/regenerar")
def regenerar_token_webhook(request: Request, admin_email: str = Depends(exigir_admin)):
    """Gera um novo token (o link antigo para de funcionar)."""
    import secrets
    from database import conta_atual, modo_sistema as _ms
    novo = secrets.token_hex(24)
    with _ms():
        with get_engine().begin() as conn:
            conn.execute(text("UPDATE dbo.nps_contas SET webhook_token = :t WHERE id = :id"), {"t": novo, "id": conta_atual()})
    return {"webhook_url": f"{_url_publica_api(request)}/api/webhook/fillout?token={novo}"}


@app.get("/api/superadmin/contas")
def listar_contas_plataforma():
    from database import modo_sistema as _ms
    with _ms():
        with get_engine().connect() as conn:
            linhas = conn.execute(text("""
                SELECT c.id, c.nome, c.plano, c.ativo, c.criado_em, c.status_assinatura, c.teste_ate,
                       c.limite_clientes, c.origem, c.email_cobranca,
                       (SELECT COUNT(*) FROM dbo.nps_usuarios u WHERE u.conta_id = c.id) AS usuarios,
                       (SELECT COUNT(*) FROM dbo.nps_clientes cl WHERE cl.conta_id = c.id) AS clientes,
                       (SELECT COUNT(*) FROM dbo.nps_respostas r WHERE r.conta_id = c.id) AS respostas
                FROM dbo.nps_contas c ORDER BY c.id
            """)).mappings().all()
    return [dict(l) for l in linhas]


@app.post("/api/superadmin/contas")
def criar_conta_plataforma(req: NovaContaRequest):
    """Cria uma nova empresa cliente com o seu primeiro usuário Admin."""
    from database import modo_sistema as _ms, usando_conta
    from bootstrap_db import criar_configuracoes_padrao, criar_usuario_admin
    email = req.admin_email.strip().lower()
    if not req.nome.strip() or "@" not in email:
        raise HTTPException(status_code=400, detail="Informe o nome da empresa e um e-mail válido.")
    validar_senha_forte(req.admin_senha)
    engine = get_engine()
    with _ms():
        with engine.connect() as conn:
            if conn.execute(text("SELECT 1 FROM dbo.nps_usuarios WHERE email = :e"), {"e": email}).scalar():
                raise HTTPException(status_code=400, detail="Este e-mail já é usado por outro usuário da plataforma.")
        with engine.begin() as conn:
            conta_id = conn.execute(text("INSERT INTO dbo.nps_contas (nome, status_assinatura) VALUES (:n, 'cortesia') RETURNING id"),
                                    {"n": req.nome.strip()}).scalar()
            if req.status_assinatura == "teste":
                from services.planos_svc import iniciar_teste
                iniciar_teste(conn, conta_id)
                conn.execute(text("UPDATE dbo.nps_contas SET origem = 'plataforma' WHERE id = :id"), {"id": conta_id})
    dominios = (req.dominios or "").strip() or email.split("@")[-1]
    with usando_conta(conta_id):
        with engine.begin() as conn:
            criar_configuracoes_padrao(conn, dominios)
            criar_usuario_admin(conn, req.admin_nome.strip(), email, req.admin_senha)
    return {"status": "success", "conta_id": conta_id, "message": f"Conta '{req.nome.strip()}' criada."}


@app.get("/api/ia/uso")
def uso_da_ia(usuario_email: str = Depends(get_current_user)):
    """Uso de análises de IA da conta no mês (a IA está incluída no plano)."""
    from services.config_svc import uso_mensal_ia, limite_mensal_ia, get_openai_token
    return {"usadas": uso_mensal_ia(), "limite": limite_mensal_ia(), "ativa": bool(get_openai_token())}


@app.get("/api/onboarding")
def progresso_primeiros_passos(usuario_email: str = Depends(get_current_user)):
    """Progresso do guia de primeiro acesso da conta."""
    with get_engine().connect() as conn:
        cfg = {r[0]: r[1] for r in conn.execute(text(
            "SELECT chave, valor FROM dbo.nps_configuracoes WHERE chave IN ('survey_url', 'envios_ativos', 'formulario_tipo')"))}
        clientes = conn.execute(text("SELECT COUNT(*) FROM dbo.nps_clientes")).scalar() or 0
        respostas = conn.execute(text("SELECT COUNT(*) FROM dbo.nps_respostas WHERE excluido = 0 OR excluido IS NULL")).scalar() or 0
        disparos = conn.execute(text("SELECT COUNT(*) FROM dbo.nps_disparos WHERE status IN ('Enviado', 'Respondido')")).scalar() or 0
    passos = {
        "clientes": clientes > 0,
        "formulario": str(cfg.get("formulario_tipo") or "proprio") != "externo" or str(cfg.get("survey_url") or "").startswith("https://"),
        "envio": disparos > 0,
        "respostas": respostas > 0,
    }
    return {
        "passos": passos,
        "concluido": all(passos.values()),
        "totais": {"clientes": clientes, "respostas": respostas, "disparos": disparos},
        "envios_ativos": str(cfg.get("envios_ativos") or "").lower() in ("true", "1"),
    }


# ==========================================
# 📝 FORMULÁRIO PRÓPRIO DE PESQUISA (NPS + CSAT)
# ==========================================
class RespostaPesquisa(BaseModel):
    respostas: Optional[dict] = None   # {id_da_pergunta: valor}
    nota: Optional[int] = None         # formato antigo
    comentario: Optional[str] = ""
    referencia: Optional[str] = ""     # link público: ?ref=... (ex.: loja, mesa, pedido)


class EnvioCSAT(BaseModel):
    email: Optional[str] = ""
    nome: Optional[str] = ""
    telefone: Optional[str] = ""
    referencia: Optional[str] = ""     # ex.: nº do pedido / da entrega
    assunto: Optional[str] = ""        # ex.: "a entrega do pedido 123"
    enviar_email: bool = True
    formulario_id: Optional[int] = None  # opcional: usa o formulário padrão de CSAT


@app.get("/api/pesquisa/{token}")
@limiter.limit("30/minute")
def pesquisa_publica(token: str, request: Request):
    """Dados do formulário público (sem login)."""
    from services.pesquisa_svc import obter_pesquisa
    dados = obter_pesquisa(token)
    if not dados:
        raise HTTPException(status_code=404, detail="Pesquisa não encontrada. Confira o link recebido.")
    return dados


@app.post("/api/pesquisa/{token}")
@limiter.limit("10/minute")
def responder_pesquisa_publica(token: str, payload: RespostaPesquisa, request: Request):
    from services.pesquisa_svc import registrar_resposta
    ok, msg = registrar_resposta(token, respostas=payload.respostas, nota=payload.nota, comentario=payload.comentario or "")
    if not ok:
        raise HTTPException(status_code=400, detail=msg)
    return {"status": "success", "message": msg}


def _enviar_csat_ou_erro(payload: EnvioCSAT):
    from services.pesquisa_svc import enviar_csat
    try:
        return enviar_csat(payload.email, payload.nome, payload.referencia, payload.assunto,
                           payload.telefone, payload.enviar_email, payload.formulario_id)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.post("/api/csat/enviar")
def enviar_csat_manual(payload: EnvioCSAT, usuario_email: str = Depends(get_current_user)):
    """Envia (ou gera o link de) uma pesquisa de satisfação CSAT pela tela."""
    return _enviar_csat_ou_erro(payload)


@app.get("/api/csat/resumo")
def resumo_do_csat(dias: int = 90, data_inicio: Optional[str] = None, data_fim: Optional[str] = None,
                   companhia: Optional[str] = None, usuario_email: str = Depends(get_current_user)):
    from services.pesquisa_svc import resumo_csat
    return resumo_csat(max(1, min(dias, 3650)), data_inicio, data_fim, companhia)


@app.post("/api/integracao/csat")
@limiter.limit("120/minute")
def integracao_csat(payload: EnvioCSAT, request: Request):
    """Para o sistema do cliente (ERP/TMS) disparar CSAT após uma entrega ou atendimento.
    Autenticação: cabeçalho X-Api-Key com a chave da conta (a mesma do webhook)."""
    from database import usando_conta as _uc
    conta_id = _conta_por_webhook_token(request.headers.get("x-api-key", "").strip())
    if conta_id is None:
        raise HTTPException(status_code=401, detail="Chave de API ausente ou inválida (cabeçalho X-Api-Key).")
    with _uc(conta_id):
        return _enviar_csat_ou_erro(payload)


# ==========================================
# 🧩 CONSTRUTOR DE FORMULÁRIOS
# ==========================================
from services import formularios_svc as _fs


class FormularioCriar(BaseModel):
    nome: Optional[str] = ""
    modelo: Optional[str] = "em_branco"


class FormularioSalvar(BaseModel):
    nome: str
    descricao: Optional[str] = ""
    perguntas: list = []
    tema: dict = {}
    publico: bool = False
    ativo: bool = True


class FormularioPadrao(BaseModel):
    uso: str  # 'nps' | 'csat'


def _url_frontend():
    return os.getenv("FRONTEND_URL", "http://localhost:5173").rstrip("/")


def _form_ou_404(conn, form_id):
    f = _fs.obter(conn, form_id)
    if not f:
        raise HTTPException(status_code=404, detail="Formulário não encontrado.")
    return f


def _form_para_api(f, padroes):
    return {
        "id": f["id"], "nome": f["nome"], "descricao": f.get("descricao") or "", "tipo": f["tipo"],
        "perguntas": f["perguntas"], "tema": dict(_fs.TEMA_PADRAO, **(f.get("tema") or {})),
        "publico": bool(f["publico"]), "ativo": bool(f["ativo"]), "codigo": f["codigo"],
        "link_publico": f"{_url_frontend()}/f/{f['codigo']}",
        "padrao_nps": padroes.get("nps") == f["id"], "padrao_csat": padroes.get("csat") == f["id"],
        "updated_at": f["updated_at"].isoformat() if f.get("updated_at") else None,
    }


def _padroes(conn):
    return {"nps": _fs.id_padrao(conn, "nps"), "csat": _fs.id_padrao(conn, "csat")}


@app.get("/api/formularios/modelos")
def listar_modelos_formulario(usuario_email: str = Depends(get_current_user)):
    return [{k: m[k] for k in ("chave", "nome", "descricao", "icone")} | {"qtd_perguntas": len([p for p in m["perguntas"] if p["tipo"] != "pagina"])}
            for m in _fs.modelos()]


@app.get("/api/formularios")
def listar_formularios(incluir_arquivados: bool = False, usuario_email: str = Depends(get_current_user)):
    with get_engine().connect() as conn:
        padroes = _padroes(conn)
        linhas = conn.execute(text(f"""
            SELECT f.*, COALESCE(r.total, 0) AS respostas, r.ultima
            FROM dbo.nps_formularios f
            LEFT JOIN (SELECT formulario_id, COUNT(*) AS total, MAX(created_at) AS ultima
                       FROM dbo.nps_formulario_respostas GROUP BY formulario_id) r ON r.formulario_id = f.id
            {'' if incluir_arquivados else 'WHERE f.ativo = 1'}
            ORDER BY f.ativo DESC, f.updated_at DESC
        """)).mappings().all()
    saida = []
    for l in linhas:
        f = _fs._linha_para_form(l)
        item = _form_para_api(f, padroes)
        item["qtd_perguntas"] = len([p for p in f["perguntas"] if p["tipo"] != "pagina"])
        item["respostas"] = l["respostas"]
        item["ultima_resposta"] = l["ultima"].isoformat() if l["ultima"] else None
        saida.append(item)
    return saida


@app.post("/api/formularios")
def criar_formulario(payload: FormularioCriar, usuario_email: str = Depends(get_current_user)):
    m = _fs.modelo(payload.modelo or "em_branco") or _fs.modelo("em_branco")
    try:
        with get_engine().begin() as conn:
            fid = _fs.criar(conn, (payload.nome or "").strip() or m["nome"], m["perguntas"], descricao=m["descricao"] if m["chave"] != "em_branco" else "")
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {"id": fid}


@app.get("/api/formularios/{form_id}")
def obter_formulario(form_id: int, usuario_email: str = Depends(get_current_user)):
    with get_engine().connect() as conn:
        return _form_para_api(_form_ou_404(conn, form_id), _padroes(conn))


@app.put("/api/formularios/{form_id}")
def salvar_formulario(form_id: int, payload: FormularioSalvar, usuario_email: str = Depends(get_current_user)):
    import json as _json
    try:
        perguntas = _fs.normalizar_perguntas(payload.perguntas)
        tema = _fs.normalizar_tema(payload.tema)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    if not (payload.nome or "").strip():
        raise HTTPException(status_code=400, detail="Dê um nome ao formulário.")
    with get_engine().begin() as conn:
        atual = _form_ou_404(conn, form_id)
        padroes = _padroes(conn)
        novo_tipo = _fs.tipo_do_formulario(perguntas)
        for uso in ("nps", "csat"):
            if padroes.get(uso) == form_id and (novo_tipo != uso or not payload.ativo):
                raise HTTPException(status_code=400, detail=(
                    f"Este é o formulário padrão de {uso.upper()}: ele precisa continuar ativo e com uma pergunta "
                    f"{'de NPS (0 a 10)' if uso == 'nps' else 'de satisfação (CSAT ou estrelas)'}. "
                    "Escolha outro formulário padrão antes."))
        conn.execute(text("""
            UPDATE dbo.nps_formularios SET nome = :n, descricao = :d, perguntas = CAST(:p AS JSONB), tema = CAST(:t AS JSONB),
                   publico = :pub, ativo = :at, updated_at = CURRENT_TIMESTAMP WHERE id = :id
        """), {"n": payload.nome.strip()[:150], "d": (payload.descricao or "")[:1000],
               "p": _json.dumps(perguntas, ensure_ascii=False), "t": _json.dumps(tema, ensure_ascii=False),
               "pub": 1 if payload.publico else 0, "at": 1 if payload.ativo else 0, "id": form_id})
        return _form_para_api(_fs.obter(conn, form_id), _padroes(conn))


@app.post("/api/formularios/{form_id}/duplicar")
def duplicar_formulario(form_id: int, usuario_email: str = Depends(get_current_user)):
    with get_engine().begin() as conn:
        f = _form_ou_404(conn, form_id)
        novo = _fs.criar(conn, f"{f['nome']} (cópia)"[:150], f["perguntas"], f.get("tema"), f.get("descricao") or "")
    return {"id": novo}


@app.delete("/api/formularios/{form_id}")
def excluir_formulario(form_id: int, usuario_email: str = Depends(get_current_user)):
    with get_engine().begin() as conn:
        _form_ou_404(conn, form_id)
        padroes = _padroes(conn)
        if form_id in padroes.values():
            raise HTTPException(status_code=400, detail="Este formulário é o padrão de envio. Escolha outro padrão antes de excluir.")
        tem_respostas = conn.execute(text("SELECT 1 FROM dbo.nps_formulario_respostas WHERE formulario_id = :id LIMIT 1"), {"id": form_id}).scalar()
        if tem_respostas:
            conn.execute(text("UPDATE dbo.nps_formularios SET ativo = 0, publico = 0, updated_at = CURRENT_TIMESTAMP WHERE id = :id"), {"id": form_id})
            return {"status": "arquivado", "message": "O formulário tinha respostas, então foi arquivado (as respostas foram mantidas)."}
        conn.execute(text("UPDATE dbo.nps_disparos SET formulario_id = NULL WHERE formulario_id = :id"), {"id": form_id})
        conn.execute(text("DELETE FROM dbo.nps_formularios WHERE id = :id"), {"id": form_id})
    return {"status": "excluido", "message": "Formulário excluído."}


@app.post("/api/formularios/{form_id}/padrao")
def definir_formulario_padrao(form_id: int, payload: FormularioPadrao, usuario_email: str = Depends(get_current_user)):
    if payload.uso not in ("nps", "csat"):
        raise HTTPException(status_code=400, detail="Uso inválido.")
    with get_engine().begin() as conn:
        f = _form_ou_404(conn, form_id)
        if not f["ativo"]:
            raise HTTPException(status_code=400, detail="Reative o formulário antes de usá-lo nos envios.")
        if f["tipo"] != payload.uso:
            exigido = "uma pergunta de NPS (0 a 10)" if payload.uso == "nps" else "uma pergunta de satisfação (CSAT ou estrelas) e nenhuma de NPS"
            raise HTTPException(status_code=400, detail=f"Para ser o padrão de {payload.uso.upper()}, o formulário precisa ter {exigido}.")
        _fs.definir_padrao(conn, payload.uso, form_id)
    return {"status": "success"}


@app.get("/api/formularios/{form_id}/resultados")
def resultados_formulario(form_id: int, dias: int = 90, usuario_email: str = Depends(get_current_user)):
    with get_engine().connect() as conn:
        f = _form_ou_404(conn, form_id)
        r = _fs.resultados(conn, f, max(1, min(dias, 3650)))
    registros = [{"id": x["id"], "data": x["created_at"].isoformat(), "cliente": x["cliente"], "email": x["email"],
                  "referencia": x["referencia"], "nota": x["nota_principal"], "respostas": x["respostas"]}
                 for x in r["registros"][:300]]
    return {"total": r["total"], "perguntas": r["perguntas"], "registros": registros}


@app.get("/api/formularios/{form_id}/resultados.csv")
def exportar_resultados_formulario(form_id: int, dias: int = 365, usuario_email: str = Depends(get_current_user)):
    from fastapi.responses import Response as _Resp
    with get_engine().connect() as conn:
        f = _form_ou_404(conn, form_id)
        r = _fs.resultados(conn, f, max(1, min(dias, 3650)))
    nome = re.sub(r"[^A-Za-z0-9_-]+", "-", f["nome"]).strip("-")[:40] or "formulario"
    return _Resp(content=_fs.csv_resultados(f, r["registros"]), media_type="text/csv; charset=utf-8",
                 headers={"Content-Disposition": f'attachment; filename="respostas-{nome}.csv"'})


# ---- link público do formulário (/f/{codigo}), sem login
@app.get("/api/pesquisa/f/{codigo}")
@limiter.limit("30/minute")
def formulario_publico(codigo: str, request: Request):
    from services.pesquisa_svc import obter_formulario_publico
    dados = obter_formulario_publico(codigo)
    if not dados:
        raise HTTPException(status_code=404, detail="Formulário não encontrado ou fora do ar.")
    return dados


@app.post("/api/pesquisa/f/{codigo}")
@limiter.limit("5/minute")
def responder_formulario_publico(codigo: str, payload: RespostaPesquisa, request: Request):
    from services.pesquisa_svc import registrar_resposta_publica
    ok, msg = registrar_resposta_publica(codigo, payload.respostas or {}, payload.referencia or "")
    if not ok:
        raise HTTPException(status_code=400, detail=msg)
    return {"status": "success", "message": msg}


# ==========================================
# 🔔 LEMBRETES DE PESQUISA
# ==========================================
@app.get("/api/lembretes/previa")
def previa_lembretes(usuario_email: str = Depends(get_current_user)):
    """Quantos lembretes sairiam agora (para a tela de Configurações)."""
    from services.lembretes_svc import _regras, pendentes
    with get_engine().connect() as conn:
        regras = _regras(conn)
        lista = pendentes(conn, regras) if regras["qtd"] > 0 else []
    return {"ativo": regras["ativo"], "qtd_maxima": regras["qtd"], "dias": regras["dias"][:regras["qtd"]],
            "pendentes_agora": len(lista)}


@app.post("/api/lembretes/executar")
def executar_lembretes(admin_email: str = Depends(exigir_admin)):
    """Envia agora os lembretes devidos (a rotina automática roda todo dia às 10h20)."""
    from services.lembretes_svc import processar_lembretes
    return {"enviados": processar_lembretes()}


# ==========================================
# 💳 PLANOS, CADASTRO PRÓPRIO E COBRANÇA (ASAAS)
# ==========================================
from services import planos_svc as _planos
from services import asaas_svc as _asaas

DOMINIOS_GRATUITOS = {"gmail.com", "hotmail.com", "outlook.com", "live.com", "yahoo.com", "yahoo.com.br", "icloud.com",
                      "bol.com.br", "uol.com.br", "terra.com.br", "ig.com.br", "msn.com", "protonmail.com", "gmx.com"}


def _mensagem_limite(texto: str):
    m = re.search(r"Limite do plano atingido[^\n\"]*?Assinatura\.", texto or "")
    return m.group(0) if m else None


@app.exception_handler(HTTPException)
async def _tratar_http(request: Request, exc: HTTPException):
    # erros de banco embrulhados em HTTP 500 que na verdade são "limite do plano"
    msg = _mensagem_limite(str(exc.detail)) if exc.status_code >= 500 else None
    if msg:
        return _JSONResponse(status_code=402, content={"detail": msg})
    return _JSONResponse(status_code=exc.status_code, content={"detail": exc.detail}, headers=getattr(exc, "headers", None))


from sqlalchemy.exc import DBAPIError as _DBAPIError


@app.exception_handler(_DBAPIError)
async def _tratar_banco(request: Request, exc: _DBAPIError):
    msg = _mensagem_limite(str(exc))
    if msg:
        return _JSONResponse(status_code=402, content={"detail": msg})
    print(f"❌ Erro de banco: {exc}")
    return _JSONResponse(status_code=500, content={"detail": "Erro interno ao acessar o banco de dados."})


@app.get("/api/planos")
def listar_planos():
    return {"planos": _planos.PLANOS, "dias_teste": _planos.DIAS_TESTE}


class CadastroEmpresa(BaseModel):
    empresa: str
    nome: str
    email: str
    senha: str
    telefone: Optional[str] = ""
    aceite_termos: bool = False


@app.post("/api/cadastro-empresa")
@limiter.limit("3/minute")
def cadastrar_empresa(req: CadastroEmpresa, request: Request, background_tasks: BackgroundTasks):
    """Cadastro próprio: cria a empresa com 14 dias de teste grátis e o primeiro Admin."""
    from database import modo_sistema as _ms, usando_conta
    from bootstrap_db import criar_configuracoes_padrao
    email = (req.email or "").strip().lower()
    if not req.empresa.strip() or not req.nome.strip() or not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", email):
        raise HTTPException(status_code=400, detail="Preencha o nome da empresa, o seu nome e um e-mail válido.")
    if not req.aceite_termos:
        raise HTTPException(status_code=400, detail="É preciso aceitar os termos de uso e a política de privacidade.")
    validar_senha_forte(req.senha)
    engine = get_engine()
    with _ms():
        with engine.connect() as conn:
            if conn.execute(text("SELECT 1 FROM dbo.nps_usuarios WHERE email = :e"), {"e": email}).scalar():
                raise HTTPException(status_code=400, detail="Este e-mail já tem acesso à Rakiti. Use 'Esqueci a senha' se precisar.")
        with engine.begin() as conn:
            conta_id = conn.execute(text("""
                INSERT INTO dbo.nps_contas (nome, telefone, email_cobranca) VALUES (:n, :t, :e) RETURNING id
            """), {"n": req.empresa.strip()[:200], "t": (req.telefone or "").strip()[:30] or None, "e": email}).scalar()
            _planos.iniciar_teste(conn, conta_id)
    dominio = email.split("@")[-1]
    # domínio de e-mail gratuito não serve para liberar colegas automaticamente
    dominios = "" if dominio in DOMINIOS_GRATUITOS else dominio
    with usando_conta(conta_id):
        with engine.begin() as conn:
            criar_configuracoes_padrao(conn, dominios)
            conn.execute(text("""
                INSERT INTO dbo.nps_usuarios (nome, email, senha_hash, tipo, cargo, ativo, email_verificado)
                VALUES (:n, :e, :h, 'Admin', 'Administrador', 1, 0)
            """), {"n": req.nome.strip()[:150], "e": email, "h": hash_password(req.senha)})
    frontend = os.getenv("FRONTEND_URL", "http://localhost:5173").rstrip("/")
    backend = _url_publica_api(request)
    with usando_conta(conta_id):
        background_tasks.add_task(_bg(enviar_email_confirmacao), email, req.nome.strip(), SECRET_KEY, ALGORITHM, frontend, backend)
        registrar_log(acao="CADASTRO_EMPRESA", mensagem=f"Empresa criada pelo cadastro: {req.empresa.strip()} ({email})", nivel="INFO")
    return {"status": "success", "message": "Conta criada! Enviamos um link para o seu e-mail. Confirme para entrar."}


def _conta_ou_404():
    from database import conta_atual
    conta = _planos.dados_conta(conta_atual())
    if not conta:
        raise HTTPException(status_code=404, detail="Conta não encontrada.")
    return conta


@app.get("/api/assinatura")
def ver_assinatura(usuario_email: str = Depends(get_current_user)):
    conta = _conta_ou_404()
    sit = _planos.situacao(conta)
    with get_engine().connect() as conn:
        cobrancas = conn.execute(text("""
            SELECT valor, status, forma, vencimento, pago_em, link FROM dbo.nps_cobrancas ORDER BY vencimento DESC NULLS LAST, id DESC LIMIT 24
        """)).mappings().all()
    link_aberto = next((c["link"] for c in cobrancas if c["status"] in ("PENDING", "OVERDUE") and c["link"]), None)
    return {
        "plano": conta["plano"], "status": sit["status"], "mensagem": sit["mensagem"], "pode_enviar": sit["pode_enviar"],
        "dias_restantes": sit["dias_restantes"], "teste_ate": conta["teste_ate"].isoformat() if conta["teste_ate"] else None,
        "limite_clientes": conta["limite_clientes"], "uso": _planos.uso(),
        "dados_cobranca": {"cpf_cnpj": conta["cpf_cnpj"] or "", "email": conta["email_cobranca"] or "", "telefone": conta["telefone"] or "",
                           "nome": conta["nome"]},
        "assinatura_ativa": bool(conta["asaas_subscription_id"]) and sit["status"] != "cancelada",
        "link_pagamento": link_aberto,
        "cobrancas": [dict(c) for c in cobrancas],
        "planos": _planos.PLANOS, "cobranca_configurada": _asaas.configurado(),
    }


class AssinarRequest(BaseModel):
    plano: str
    cpf_cnpj: str
    email_cobranca: str
    telefone: Optional[str] = ""
    razao_social: Optional[str] = ""


@app.post("/api/assinatura")
def assinar_plano(req: AssinarRequest, admin_email: str = Depends(exigir_admin)):
    """Cria (ou troca) a assinatura mensal no Asaas e devolve o link da fatura."""
    from database import modo_sistema as _ms
    if req.plano not in _planos.PLANOS:
        raise HTTPException(status_code=400, detail="Plano inválido.")
    if not _asaas.documento_valido(req.cpf_cnpj):
        raise HTTPException(status_code=400, detail="CPF ou CNPJ inválido.")
    if "@" not in (req.email_cobranca or ""):
        raise HTTPException(status_code=400, detail="Informe o e-mail que vai receber as faturas.")
    conta = _conta_ou_404()
    plano = _planos.PLANOS[req.plano]
    limite = plano["limite_clientes"]
    if limite is not None and _planos.uso()["clientes_ativos"] > limite:
        raise HTTPException(status_code=400, detail=f"Você tem mais de {limite} clientes ativos. Escolha um plano maior ou desative clientes.")
    nome = (req.razao_social or "").strip() or conta["nome"]
    descricao = f"Rakiti - plano {plano['nome']}"
    try:
        customer = conta["asaas_customer_id"]
        if customer:
            _asaas.atualizar_cliente(customer, nome, req.cpf_cnpj, req.email_cobranca, req.telefone)
        else:
            customer = _asaas.criar_cliente(nome, req.cpf_cnpj, req.email_cobranca, req.telefone, conta["id"])
        assinatura = conta["asaas_subscription_id"] if conta["status_assinatura"] != "cancelada" else None
        if assinatura:
            _asaas.alterar_valor(assinatura, plano["preco"], descricao)
        else:
            assinatura = _asaas.criar_assinatura(customer, plano["preco"], descricao, conta["id"])
        fatura = _asaas.fatura_em_aberto(assinatura)
        link = (fatura or {}).get("invoiceUrl")
    except _asaas.ErroAsaas as e:
        raise HTTPException(status_code=502, detail=str(e))
    with _ms():
        with get_engine().begin() as conn:
            conn.execute(text("""
                UPDATE dbo.nps_contas SET asaas_customer_id = :c, asaas_subscription_id = :s, cpf_cnpj = :doc,
                       email_cobranca = :e, telefone = COALESCE(NULLIF(:t, ''), telefone)
                WHERE id = :id
            """), {"c": customer, "s": assinatura, "doc": _asaas.so_digitos(req.cpf_cnpj), "e": req.email_cobranca.strip(),
                   "t": (req.telefone or "").strip(), "id": conta["id"]})
            _planos.aplicar_plano(conn, conta["id"], req.plano)
            if fatura and fatura.get("id"):   # já mostra a fatura na tela, antes do aviso do webhook
                conn.execute(text("""
                    INSERT INTO dbo.nps_cobrancas (conta_id, asaas_payment_id, valor, status, forma, vencimento, link, ultimo_evento)
                    VALUES (:c, :p, :v, :st, :f, :venc, :link, 'CRIADA_PELA_TELA')
                    ON CONFLICT (asaas_payment_id) DO UPDATE SET valor = EXCLUDED.valor, link = EXCLUDED.link, updated_at = CURRENT_TIMESTAMP
                """), {"c": conta["id"], "p": fatura["id"], "v": fatura.get("value") or plano["preco"], "st": fatura.get("status"),
                       "f": fatura.get("billingType"), "venc": fatura.get("dueDate"), "link": fatura.get("invoiceUrl")})
    registrar_log(acao="ASSINATURA", mensagem=f"Plano {plano['nome']} escolhido.", nivel="INFO")
    return {"status": "success", "link_pagamento": link}


@app.post("/api/assinatura/cancelar")
def cancelar_plano(admin_email: str = Depends(exigir_admin)):
    from database import modo_sistema as _ms
    conta = _conta_ou_404()
    if conta["asaas_subscription_id"]:
        try:
            _asaas.cancelar_assinatura(conta["asaas_subscription_id"])
        except _asaas.ErroAsaas as e:
            raise HTTPException(status_code=502, detail=str(e))
    with _ms():
        with get_engine().begin() as conn:
            conn.execute(text("UPDATE dbo.nps_contas SET status_assinatura = 'cancelada' WHERE id = :id"), {"id": conta["id"]})
    registrar_log(acao="ASSINATURA", mensagem="Assinatura cancelada pelo cliente.", nivel="WARN")
    return {"status": "success", "message": "Assinatura cancelada. Seus dados continuam guardados."}


@app.post("/api/webhook/asaas")
async def webhook_asaas(request: Request):
    """Recebe os avisos de pagamento do Asaas e atualiza a situação da conta."""
    import hmac
    from database import modo_sistema as _ms
    esperado = os.getenv("ASAAS_WEBHOOK_TOKEN", "").strip()
    recebido = request.headers.get("asaas-access-token", "").strip()
    if not esperado or not hmac.compare_digest(esperado, recebido):
        raise HTTPException(status_code=401, detail="Token do webhook inválido.")
    evento = await request.json()
    tipo = evento.get("event", "")
    pagamento = evento.get("payment") or {}
    assinatura = pagamento.get("subscription") or (evento.get("subscription") or {}).get("id")
    ref = pagamento.get("externalReference") or (evento.get("subscription") or {}).get("externalReference") or ""
    with _ms():
        with get_engine().begin() as conn:
            conta_id = None
            if assinatura:
                conta_id = conn.execute(text("SELECT id FROM dbo.nps_contas WHERE asaas_subscription_id = :s"), {"s": assinatura}).scalar()
            if not conta_id and ref.startswith("conta:") and ref[6:].isdigit():
                conta_id = int(ref[6:])
            if not conta_id:
                return {"status": "ignorado"}   # 200 para o Asaas não reenviar

            if pagamento.get("id"):
                conn.execute(text("""
                    INSERT INTO dbo.nps_cobrancas (conta_id, asaas_payment_id, valor, status, forma, vencimento, pago_em, link, ultimo_evento)
                    VALUES (:c, :p, :v, :st, :f, :venc, :pago, :link, :ev)
                    ON CONFLICT (asaas_payment_id) DO UPDATE SET valor = EXCLUDED.valor, status = EXCLUDED.status, forma = EXCLUDED.forma,
                        vencimento = EXCLUDED.vencimento, pago_em = EXCLUDED.pago_em, link = EXCLUDED.link,
                        ultimo_evento = EXCLUDED.ultimo_evento, updated_at = CURRENT_TIMESTAMP
                """), {"c": conta_id, "p": pagamento["id"], "v": pagamento.get("value"), "st": pagamento.get("status"),
                       "f": pagamento.get("billingType"), "venc": pagamento.get("dueDate"),
                       "pago": pagamento.get("paymentDate") or pagamento.get("confirmedDate"),
                       "link": pagamento.get("invoiceUrl"), "ev": tipo})

            if tipo in ("PAYMENT_CONFIRMED", "PAYMENT_RECEIVED"):
                conn.execute(text("UPDATE dbo.nps_contas SET status_assinatura = 'ativa', atrasada_desde = NULL, teste_ate = NULL WHERE id = :id"),
                             {"id": conta_id})
            elif tipo == "PAYMENT_OVERDUE":
                conn.execute(text("""
                    UPDATE dbo.nps_contas SET status_assinatura = 'atrasada', atrasada_desde = COALESCE(atrasada_desde, CURRENT_TIMESTAMP)
                    WHERE id = :id AND status_assinatura IN ('ativa', 'atrasada', 'teste')
                """), {"id": conta_id})
            elif tipo in ("SUBSCRIPTION_DELETED", "SUBSCRIPTION_INACTIVATED"):
                conn.execute(text("UPDATE dbo.nps_contas SET status_assinatura = 'cancelada' WHERE id = :id"), {"id": conta_id})
    return {"status": "ok"}


class AjusteContaPlataforma(BaseModel):
    plano: Optional[str] = None
    status_assinatura: Optional[str] = None
    dias_teste: Optional[int] = None


@app.put("/api/superadmin/contas/{conta_id}")
def ajustar_conta_plataforma(conta_id: int, req: AjusteContaPlataforma):
    """Ajuste manual pelo dono da plataforma (cortesia, estender teste, trocar plano)."""
    from database import modo_sistema as _ms
    from datetime import timedelta as _td
    with _ms():
        with get_engine().begin() as conn:
            if not conn.execute(text("SELECT 1 FROM dbo.nps_contas WHERE id = :id"), {"id": conta_id}).scalar():
                raise HTTPException(status_code=404, detail="Conta não encontrada.")
            if req.plano:
                if req.plano not in _planos.PLANOS:
                    raise HTTPException(status_code=400, detail="Plano inválido.")
                _planos.aplicar_plano(conn, conta_id, req.plano)
            if req.status_assinatura:
                if req.status_assinatura not in ("cortesia", "teste", "ativa", "atrasada", "cancelada"):
                    raise HTTPException(status_code=400, detail="Situação inválida.")
                conn.execute(text("UPDATE dbo.nps_contas SET status_assinatura = :s WHERE id = :id"), {"s": req.status_assinatura, "id": conta_id})
                if req.status_assinatura == "cortesia":
                    conn.execute(text("UPDATE dbo.nps_contas SET limite_clientes = NULL WHERE id = :id"), {"id": conta_id})
            if req.dias_teste:
                conn.execute(text("UPDATE dbo.nps_contas SET status_assinatura = 'teste', teste_ate = :f WHERE id = :id"),
                             {"f": _planos.agora() + _td(days=max(1, min(req.dias_teste, 90))), "id": conta_id})
    return {"status": "success"}
