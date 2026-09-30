from services.mail_provider import post_email, usando_resend
import os
import re
import requests
from sqlalchemy import text, bindparam
import urllib.parse
from database import get_engine
from fastapi import HTTPException
from jose import jwt
from datetime import datetime, timedelta, timezone

def registrar_log_disparo(email, nome, status, assunto, erro=None, cliente_id=None, empresa_id=None, url=None,
                          token=None, tipo="nps", referencia=None, assunto_pesquisa=None, formulario_id=None):
    """
    Função unificada para gravar qualquer disparo de e-mail na tabela nps_disparos.
    """
    c_id = None if str(cliente_id).strip() == "" else cliente_id
    e_id = None if str(empresa_id).strip() == "" else empresa_id

    try:
        from database import get_engine
        engine = get_engine()
        with engine.begin() as conn:
            sql = text("""
                INSERT INTO dbo.nps_disparos 
                (cliente_id, empresa_id, nome, email, status, assunto, survey_url, erro_msg, data_envio_inicial, created_at, lembretes_enviados,
                 token, tipo_pesquisa, referencia, assunto_pesquisa, formulario_id)
                VALUES 
                (:cid, :eid, :nome, :email, :status, :assunto, :url, :erro, GETDATE(), GETDATE(), 0,
                 :token, :tipo, :ref, :assp, :fid)
            """)
            conn.execute(sql, {
                "cid": cliente_id,
                "eid": empresa_id,
                "nome": nome or "Usuário Sistema",
                "email": email,
                "status": status,
                "assunto": assunto,
                "url": url if url else "", 
                "erro": str(erro) if erro else "",
                "token": token,
                "tipo": tipo or "nps",
                "ref": referencia,
                "assp": assunto_pesquisa,
                "fid": formulario_id,
            })
    except Exception as e:
        # Deixa o erro evidente no console para debug rápido
        print(f"❌ ERRO GRAVE NO LOG DE E-MAIL para {email}: {str(e)}")

def link_pesquisa_configurado(regras: dict) -> bool:
    from services.pesquisa_svc import usa_formulario_proprio
    if usa_formulario_proprio(regras):
        return True
    url = str(regras.get("survey_url") or "").strip()
    return url.startswith("https://")


def montar_convite_nps(regras: dict, cliente, template_customizado: str, campos_permitidos: list):
    """Monta link, HTML e assunto do convite de NPS.
    Formulário próprio: link único /r/{token} com o formulário padrão de NPS da conta.
    Externo: link configurado + parâmetros do cliente.
    Retorna dict com url, html, assunto, token e formulario_id (None no externo)."""
    from database import get_engine
    from services import formularios_svc as fs
    from services.pesquisa_svc import (usa_formulario_proprio, gerar_token, url_formulario, nome_da_conta,
                                       html_convite_nps, html_convite_formulario, botoes_nps_html)
    nome_exibicao = cliente["nome"].split(" ")[0] if cliente["nome"] else "cliente"
    empresa_exibicao = cliente["empresa"] or "sua empresa"
    remetente = nome_da_conta()
    token, form_id, render = None, None, None
    if usa_formulario_proprio(regras):
        with get_engine().connect() as conn:
            form_id = fs.id_padrao(conn, "nps")
            form = fs.obter(conn, form_id) if form_id else None
        if not form:
            raise Exception("Nenhum formulário de NPS ativo. Crie um em Formulários.")
        token = gerar_token()
        survey_url = url_formulario(token)
        render = fs.renderizar(form, {"empresa": remetente, "nome": nome_exibicao})
    else:
        params = {"clienteId": cliente["cliente_id"], "email": cliente["email"], "nome": cliente["nome"],
                  "empresa": cliente["empresa"] or "", "empresa_id": str(cliente["empresa_id"]) if cliente["empresa_id"] else ""}
        query_string = urllib.parse.urlencode({k: v for k, v in params.items() if k.lower() in campos_permitidos and v})
        survey_url = montar_link_pesquisa(regras.get("survey_url"), query_string)

    if template_customizado and "{survey_url}" in template_customizado:
        botoes = botoes_nps_html(survey_url) if render and render.get("principal_tipo") == "nps" else ""
        mail_html = (template_customizado.replace("{nome}", nome_exibicao).replace("{empresa}", empresa_exibicao)
                     .replace("{botoes_nota}", botoes).replace("{survey_url}", survey_url))
    elif render:
        mail_html = html_convite_formulario(render, nome_exibicao, survey_url, remetente)
    else:
        pergunta = f"De 0 a 10, quanto você recomendaria a {remetente} a um amigo ou colega?"
        mail_html = html_convite_nps(nome_exibicao, survey_url, pergunta, remetente, com_botoes=False)

    return {"url": survey_url, "html": mail_html, "token": token, "nome": nome_exibicao, "formulario_id": form_id,
            "assunto": f"{remetente} quer saber a sua opinião"}


def montar_link_pesquisa(base: str, query_string: str) -> str:
    """Junta o link do formulário da conta (Configurações) com os parâmetros do cliente."""
    base = str(base or "").strip()
    if not query_string:
        return base
    return f"{base}{'&' if '?' in base else '?'}{query_string}"


def obter_regras_dinamicas():
    """Lê as parametrizações de negócio da base de dados"""
    from database import get_engine
    from sqlalchemy import text
    
    regras = {
        "sla_detrator_dias": 2,
        "sla_neutro_dias": 5,
        "sla_promotor_dias": 7,
        "fillout_campos": "clienteId,email,nome,empresa,empresa_id",
        "survey_url": "",
        "formulario_tipo": "proprio",
        "pergunta_nps": "",
        "pergunta_csat": "",
        "email_template_html": "",
        "email_agradecimento_promotor": "",
        "email_agradecimento_neutro": "",
        "email_agradecimento_detrator": "",
        "email_template_lembrete_1": "",
        "email_template_lembrete_2": "",
        "email_template_lembrete_3": ""
    }
    
    try:
        engine = get_engine()
        with engine.connect() as conn:
            query = text("""
                SELECT chave, valor 
                FROM dbo.nps_configuracoes 
                WHERE chave IN (
                    'sla_detrator_dias', 'sla_neutro_dias', 'sla_promotor_dias', 
                    'fillout_campos', 'survey_url', 'email_template_html', 'formulario_tipo', 'pergunta_nps', 'pergunta_csat',
                    'email_agradecimento_promotor', 'email_agradecimento_neutro', 'email_agradecimento_detrator',
                    'email_template_lembrete_1', 'email_template_lembrete_2', 'email_template_lembrete_3'
                )
            """)
            for linha in conn.execute(query).fetchall():
                if linha.chave in ['sla_detrator_dias', 'sla_neutro_dias', 'sla_promotor_dias']:
                    regras[linha.chave] = int(linha.valor) if linha.valor else regras[linha.chave]
                else:
                    regras[linha.chave] = linha.valor
    except Exception as e:
        print(f"⚠️ Usando regras padrão. Erro ao ler banco: {e}")
        
    return regras

def tornar_links_absolutos(html_content: str, dominio_contexto: str = None) -> str:
    if not html_content:
        return ""

    dominio_front = dominio_contexto
    if not dominio_front:
        try:
            from database import get_engine
            from sqlalchemy import text
            with get_engine().connect() as conn:
                res = conn.execute(text("SELECT valor FROM dbo.nps_configuracoes WHERE chave = 'url_sistema'")).scalar()
                dominio_front = res
        except Exception:
            pass

    dominio_front = (dominio_front or "https://nps-intelligence.gauge.com.br").rstrip("/")

    azure_host = os.getenv("WEBSITE_HOSTNAME")
    if azure_host:
        url_real_backend = f"https://{azure_host}"
    else:
        url_real_backend = "http://localhost:8000"

    html_corrigido = html_content.replace("{backend_url}", url_real_backend)
    html_corrigido = re.sub(r'src=["\']/(?!/)', f'src="{url_real_backend}/', html_corrigido)

    return html_corrigido

def obter_configuracoes_email():
    """Procura as credenciais ativas na base de dados e DESCRIPTOGRAFA de forma segura."""
    from database import get_engine 
    from sqlalchemy import text
    try:
        from services.crypto_svc import decrypt_data
    except ImportError:
        decrypt_data = lambda x: x # Previne erro se o arquivo crypto_svc não existir ainda

    engine = get_engine()
    try:
        with engine.connect() as conn:
            query = text("""
                SELECT tenant_id, client_id, client_secret, refresh_token, email_remetente 
                FROM dbo.nps_configuracoes_email LIMIT 1
            """)
            resultado = conn.execute(query).mappings().first()
            if resultado:
                config = dict(resultado)
                
                # 🎯 Descriptografa na memória (se estiver em texto plano no banco, o decrypt ignora e devolve igual)
                if config.get('client_secret'):
                    config['client_secret'] = decrypt_data(config['client_secret'])
                if config.get('refresh_token'):
                    config['refresh_token'] = decrypt_data(config['refresh_token'])
                    
                return config
            return None
    except Exception as e:
        print(f"❌ Erro ao ler banco: {e}")
        return None

def gerar_access_token(config):
    """Troca o refresh_token por um access_token válido."""
    try:
        url = f"https://login.microsoftonline.com/{config['tenant_id']}/oauth2/v2.0/token"
        payload = {
            'client_id': config['client_id'],
            'client_secret': config['client_secret'],
            'refresh_token': config['refresh_token'],
            'grant_type': 'refresh_token',
            'scope': 'mail.send'
        }
        res = requests.post(url, data=payload).json()
        
        if 'error' in res:
            print(f"❌ Erro Microsoft Token: {res.get('error_description')}")
            return None
            
        return res.get('access_token')
    except Exception as e:
        print(f"❌ Erro na renovação do token: {e}")
        return None
    
def get_valid_access_token():
    """Função mestre para obter um token pronto para uso"""
    if usando_resend():
        return "plataforma"  # provedor da plataforma (ZeptoMail/Resend): não há token OAuth
    config = obter_configuracoes_email()
    if not config or not config['refresh_token']:
        print("⚠️ E-mail não configurado ou não autorizado.")
        return None
    return gerar_access_token(config)

def enviar_email_recuperacao(email_destino, nome_usuario, token):
    """Envia o e-mail com o link de recuperação de senha com design premium."""
    access_token = get_valid_access_token()
    if not access_token:
        print("❌ Falha crítica: Não foi possível obter Access Token para recuperação de senha.")
        return False

    # 🎯 Define o remetente correto baseado na sua configuração do Graph
    url_send = "https://graph.microsoft.com/v1.0/me/sendMail"
    frontend_url = os.getenv("FRONTEND_URL", "http://localhost:5173").rstrip('/') 
    link_recuperacao = f"{frontend_url}/redefinir-senha?token={token}"
    
    # Se o nome vier vazio por algum motivo, usamos uma saudação genérica amigável
    saudacao_nome = nome_usuario if nome_usuario else "Usuário"

    payload = {
        "message": {
            "subject": "Redefinição de Senha - Rakiti",
            "body": {
                "contentType": "HTML",
                "content": f"""
                <!DOCTYPE html>
                <html>
                <head><meta charset="utf-8"></head>
                <body style="margin: 0; padding: 0; background-color: #f1f5f9; font-family: Arial, sans-serif;">
                    <table width="100%" cellpadding="0" cellspacing="0" style="background-color: #f1f5f9; padding: 40px 20px;">
                        <tr>
                            <td align="center">
                                <table width="100%" style="max-width: 500px; background-color: #ffffff; border-radius: 16px; border: 1px solid #e2e8f0; overflow: hidden;">
                                    <tr>
                                        <td align="center" style="padding: 30px; border-bottom: 1px solid #f8fafc;">
                                            <span style="font-size: 24px; font-weight: 900; color: #0f172a; font-style: italic;">
                                                NPS <span style="color: #f97316;">Intelligence</span>
                                            </span>
                                        </td>
                                    </tr>
                                    <tr>
                                        <td style="padding: 40px; text-align: left;">
                                            <h2 style="color: #0f172a; font-size: 20px; margin-bottom: 15px;">Recuperação de Acesso</h2>
                                            <p style="color: #475569; font-size: 15px; line-height: 1.6;">
                                                Olá, <strong>{saudacao_nome}</strong>,<br><br>
                                                Recebemos um pedido para repor a senha da sua conta. Clique no botão abaixo para prosseguir:
                                            </p>
                                            
                                            <div style="text-align: center; margin: 30px 0;">
                                                <a href="{link_recuperacao}" style="background-color: #f97316; color: #ffffff; padding: 14px 25px; text-decoration: none; border-radius: 10px; font-weight: bold; display: inline-block; text-transform: uppercase; font-size: 13px;">
                                                    Criar Nova Senha
                                                </a>
                                            </div>
                                            
                                            <p style="color: #64748b; font-size: 13px; background-color: #f8fafc; padding: 15px; border-radius: 8px; border-left: 4px solid #f97316;">
                                                <strong>Atenção:</strong> Este link expira em 30 minutos por motivos de segurança.
                                            </p>
                                        </td>
                                    </tr>
                                </table>
                            </td>
                        </tr>
                    </table>
                </body>
                </html>
                """
            },
            "toRecipients": [{"emailAddress": {"address": email_destino}}]
        },
        "saveToSentItems": "true"
    }

    headers = {'Authorization': f'Bearer {access_token}', 'Content-Type': 'application/json'}

    try:
        response = post_email(url_send, json=payload, headers=headers)
        
        # 🎯 REGISTRO DE LOG COM O NOME REAL DO BANCO
        status = "Enviado" if response.status_code == 202 else "Erro"
        erro_msg = None if response.status_code == 202 else response.text
        
        registrar_log_disparo(
            email_destino, 
            saudacao_nome, # 👈 Aqui o log deixa de ser genérico!
            status, 
            "Recuperação de Senha", 
            erro=erro_msg, 
            url=link_recuperacao
        )
        
        return response.status_code == 202
    except Exception as e:
        registrar_log_disparo(email_destino, saudacao_nome, "Erro", "Recuperação de Senha", erro=str(e), url=link_recuperacao)
        return False
    

def enviar_email_senha_alterada(email_destino: str, nome_usuario: str):
    """
    Envia um aviso de segurança informando que a senha foi alterada.
    """
    try:
        access_token = get_valid_access_token()
        if not access_token: return False

        engine = get_engine()
        with engine.connect() as conn:
            config = conn.execute(text("SELECT email_remetente FROM dbo.nps_configuracoes_email")).mappings().first()
            if (not config or not config["email_remetente"]) and not usando_resend(): return False

            send_url = f"https://graph.microsoft.com/v1.0/users/{(config or {}).get('email_remetente', '')}/sendMail"
            
            html_content = f"""
            <div style="font-family: Arial, sans-serif; max-width: 500px; padding: 20px; border: 1px solid #e2e8f0; border-radius: 10px;">
                <h2 style="color: #0f172a;">Aviso de Segurança</h2>
                <p>Olá, <strong>{nome_usuario}</strong>,</p>
                <p>Informamos que a sua senha no <strong>Rakiti</strong> foi alterada com sucesso.</p>
                <p style="background-color: #fff7ed; padding: 15px; border-radius: 8px; border-left: 4px solid #f97316; color: #9a3412;">
                    <strong>Não foi você?</strong> Se não realizou esta alteração, entre em contato com o administrador imediatamente.
                </p>
                <p style="font-size: 12px; color: #64748b; margin-top: 20px;">Este é um e-mail automático, por favor não responda.</p>
            </div>
            """

            email_body = {
                "message": {
                    "subject": "Segurança: Senha Alterada - Rakiti",
                    "body": {"contentType": "HTML", "content": html_content},
                    "toRecipients": [{"emailAddress": {"address": email_destino}}]
                }
            }
            
            headers = {'Authorization': f'Bearer {access_token}', 'Content-Type': 'application/json'}
            res = post_email(send_url, json=email_body, headers=headers)
            
            # 🎯 REGISTRO DE LOG COM O NOME REAL
            status = "Enviado" if res.status_code == 202 else "Erro"
            registrar_log_disparo(
                email_destino, 
                nome_usuario, 
                status, 
                "Aviso: Senha Alterada", 
                erro=res.text if status == "Erro" else None
            )
            return res.status_code == 202

    except Exception as e:
        # 🎯 LOG MESMO EM CASO DE FALHA TÉCNICA
        registrar_log_disparo(email_destino, nome_usuario, "Erro", "Aviso: Senha Alterada", erro=str(e))
        return False
    
def enviar_email_teste(email_destino):
    access_token = get_valid_access_token()
    if not access_token:
        print("⚠️ E-mail não configurado ou não autorizado.")
        return False

    url_send = "https://graph.microsoft.com/v1.0/me/sendMail"
    payload = {
        "message": {
            "subject": "Teste de Conexão - Rakiti ✅",
            "body": {
                "contentType": "HTML",
                "content": """<div style="font-family: sans-serif; border: 2px solid #10b981; padding: 20px; border-radius: 15px;"><h2 style="color: #10b981;">Conexão Bem-sucedida!</h2></div>"""
            },
            "toRecipients": [{"emailAddress": {"address": email_destino}}]
        },
        "saveToSentItems": True
    }

    headers = {'Authorization': f'Bearer {access_token}', 'Content-Type': 'application/json'}

    try:
        response = post_email(url_send, json=payload, headers=headers)
        if response.status_code == 202:
            registrar_log_disparo(email_destino, "Administrador", "Enviado", "Teste de Conexão - Rakiti ✅")
            return True
        else:
            registrar_log_disparo(email_destino, "Administrador", "Erro", "Teste de Conexão - Rakiti ✅", erro=response.text)
            return False 
    except Exception as e:
        registrar_log_disparo(email_destino, "Administrador", "Erro", "Teste de Conexão - Rakiti ✅", erro=str(e))
        return False

def processar_disparos_nps():
    print("⏳ Iniciando rotina de disparo de NPS...")
    engine = get_engine()
    
    try:
        from sqlalchemy import text
        with engine.connect() as conn:
            motor = conn.execute(text("SELECT valor FROM dbo.nps_configuracoes WHERE chave = 'envios_ativos'")).scalar()
            if str(motor).lower() not in ['true', '1']: return 
            
            robo = conn.execute(text("SELECT valor FROM dbo.nps_configuracoes WHERE chave = 'robo_ativo'")).scalar()
            if str(robo).lower() not in ['true', '1']: return 
    except Exception as e:
        return
    
    sql_busca = text("""
        SELECT c.cliente_id, c.email, c.nome, COALESCE(e.nome::text, c.empresa) AS empresa, e.id AS empresa_id
        FROM dbo.nps_clientes c
        LEFT JOIN dbo.nps_empresas e ON e.id = c.empresa_id OR (c.empresa_id IS NULL AND c.empresa = e.nome)
        WHERE c.ativo = 1 AND c.status_envio IN ('Pendente', 'Erro') AND (c.proximo_envio IS NULL OR c.proximo_envio <= CAST(GETDATE() AS DATE))
        ORDER BY COALESCE(c.proximo_envio, '1900-01-01') ASC LIMIT 100
    """)
    
    try:
        with engine.connect() as conn:
            elegiveis = conn.execute(sql_busca).mappings().all()
            
        if not elegiveis: return

        access_token = get_valid_access_token() 
        if not access_token: return

        from services.planos_svc import pode_enviar
        liberado, motivo = pode_enviar()
        if not liberado:
            print(f"⏸️ Disparos automáticos pausados: {motivo}")
            return
        
        headers = {"Authorization": f"Bearer {access_token}", "Content-Type": "application/json"}
        regras = obter_regras_dinamicas()
        if not link_pesquisa_configurado(regras):
            print("⚠️ Link do formulário de pesquisa não configurado (Configurações > Regras). Disparos suspensos.")
            return
        campos_permitidos = [c.strip().lower() for c in regras.get("fillout_campos", "").split(",")]
        template_customizado = tornar_links_absolutos(regras.get("email_template_html", ""))

        enviados = 0
        with engine.begin() as conn: 
            for cliente in elegiveis:
                try:
                    convite = montar_convite_nps(regras, cliente, template_customizado, campos_permitidos)
                    survey_url, nome_exibicao = convite["url"], convite["nome"]
                    payload = {
                        "message": {
                            "subject": convite["assunto"],
                            "body": {"contentType": "HTML", "content": convite["html"]},
                            "toRecipients": [{"emailAddress": {"address": cliente["email"]}}]
                        },
                        "saveToSentItems": True
                    }

                    resposta_ms = post_email("https://graph.microsoft.com/v1.0/me/sendMail", headers=headers, json=payload)
                    
                    if resposta_ms.status_code in (200, 202):
                        sql_update = text("UPDATE dbo.nps_clientes SET status_envio = 'Enviado', ultimo_envio = CAST(GETDATE() AS DATE), proximo_envio = DATEADD('day', 90, CAST(GETDATE() AS DATE)), ultimo_erro = NULL, updated_at = SYSUTCDATETIME() WHERE cliente_id = :id")
                        conn.execute(sql_update, {"id": cliente["cliente_id"]})
                        registrar_log_disparo(cliente["email"], nome_exibicao, "Enviado", payload["message"]["subject"], cliente_id=cliente["cliente_id"], empresa_id=cliente["empresa_id"], url=survey_url, token=convite["token"], formulario_id=convite["formulario_id"])
                        enviados += 1
                    else:
                        registrar_log_disparo(cliente["email"], nome_exibicao, "Erro", payload["message"]["subject"], erro=resposta_ms.text, cliente_id=cliente["cliente_id"], empresa_id=cliente["empresa_id"], url=survey_url, token=convite["token"], formulario_id=convite["formulario_id"])
                        raise Exception(f"Erro MS Graph: {resposta_ms.text}")

                except Exception as erro_cliente:
                    sql_erro = text("UPDATE dbo.nps_clientes SET status_envio = 'Erro', ultimo_erro = :erro, updated_at = SYSUTCDATETIME() WHERE cliente_id = :id")
                    conn.execute(sql_erro, {"erro": str(erro_cliente)[:250], "id": cliente["cliente_id"]})
                    registrar_log_disparo(cliente.get("email"), cliente.get("nome"), "Erro", "Disparo NPS Automático", erro=str(erro_cliente)[:250], cliente_id=cliente.get("cliente_id"), empresa_id=cliente.get("empresa_id"))

        if enviados > 0:
            try:
                from main import registrar_log
                registrar_log(acao="DISPARO_AUTOMATICO", mensagem=f"O Robô enviou com sucesso {enviados} pesquisas agendadas.", nivel="SUCCESS")
            except Exception:
                pass
    except Exception as e:
        print(f"❌ Erro Fatal na rotina de NPS: {e}")
        
def disparar_convite_nps_especifico(cliente_ids: list, dominio_origem: str = None):
    if not cliente_ids: return
    
    from database import get_engine
    engine = get_engine()
    
    sql_busca = text("""
        SELECT c.cliente_id, c.email, c.nome, COALESCE(e.nome::text, c.empresa) AS empresa, e.id AS empresa_id 
        FROM dbo.nps_clientes c 
        LEFT JOIN dbo.nps_empresas e ON e.id = c.empresa_id OR (c.empresa_id IS NULL AND c.empresa = e.nome) 
        WHERE c.cliente_id IN :lista_ids
    """).bindparams(bindparam('lista_ids', expanding=True))
    
    try:
        with engine.connect() as conn:
            clientes = conn.execute(sql_busca, {"lista_ids": cliente_ids}).mappings().all()
            
        if not clientes: return

        access_token = get_valid_access_token() 
        if not access_token: return
        headers = {"Authorization": f"Bearer {access_token}", "Content-Type": "application/json"}
        from services.planos_svc import pode_enviar
        liberado, motivo = pode_enviar()
        if not liberado:
            for cliente in clientes:
                registrar_log_disparo(cliente["email"], cliente["nome"], "Erro", "[Pesquisa NPS]", motivo, cliente["cliente_id"], cliente["empresa_id"])
            return
        regras = obter_regras_dinamicas()
        if not link_pesquisa_configurado(regras):
            print("⚠️ Link do formulário de pesquisa não configurado (Configurações > Regras). Disparo cancelado.")
            for cliente in clientes:
                registrar_log_disparo(cliente["email"], cliente["nome"], "Erro", "[Pesquisa NPS]", "Link do formulário de pesquisa não configurado em Configurações > Regras.", cliente["cliente_id"], cliente["empresa_id"])
            return
        campos_raw = regras.get("fillout_campos") or "clienteId,email,nome"
        campos_permitidos = [c.strip().lower() for c in campos_raw.split(",")]
        template_customizado = tornar_links_absolutos(regras.get("email_template_html") or "", dominio_origem)

        with engine.begin() as conn:
            for cliente in clientes:
                try:
                    convite = montar_convite_nps(regras, cliente, template_customizado, campos_permitidos)
                    survey_url, nome_exibicao = convite["url"], convite["nome"]
                    payload = {
                        "message": {
                            "subject": convite["assunto"],
                            "body": {"contentType": "HTML", "content": convite["html"]},
                            "toRecipients": [{"emailAddress": {"address": cliente["email"]}}]
                        },
                        "saveToSentItems": True
                    }

                    resposta_ms = post_email("https://graph.microsoft.com/v1.0/me/sendMail", headers=headers, json=payload)
                    
                    if resposta_ms.status_code in (200, 202):
                        conn.execute(text("UPDATE dbo.nps_clientes SET status_envio = 'Enviado', ultimo_envio = CAST(GETDATE() AS DATE), proximo_envio = DATEADD('day', 90, CAST(GETDATE() AS DATE)), ultimo_erro = NULL, updated_at = SYSUTCDATETIME() WHERE cliente_id = :id"), {"id": cliente["cliente_id"]})
                        registrar_log_disparo(cliente["email"], nome_exibicao, "Enviado", payload["message"]["subject"], cliente_id=cliente["cliente_id"], empresa_id=cliente["empresa_id"], url=survey_url, token=convite["token"], formulario_id=convite["formulario_id"])
                    else:
                        registrar_log_disparo(cliente["email"], nome_exibicao, "Erro", payload["message"]["subject"], erro=resposta_ms.text, cliente_id=cliente["cliente_id"], empresa_id=cliente["empresa_id"], url=survey_url, token=convite["token"], formulario_id=convite["formulario_id"])
                        raise Exception(f"Erro na API da Microsoft: {resposta_ms.text}")

                except Exception as erro_cliente:
                    conn.execute(text("UPDATE dbo.nps_clientes SET status_envio = 'Erro', ultimo_erro = :erro, updated_at = SYSUTCDATETIME() WHERE cliente_id = :id"), {"erro": str(erro_cliente)[:250], "id": cliente["cliente_id"]})
                    registrar_log_disparo(cliente.get("email"), cliente.get("nome"), "Erro", "Disparo NPS Manual", erro=str(erro_cliente)[:250], cliente_id=cliente.get("cliente_id"), empresa_id=cliente.get("empresa_id"))

    except Exception as e:
        print(f"❌ Erro fatal no disparo manual: {e}")

def enviar_email_resposta(email_destino: str, nome: str, empresa: str, nota: int, categoria: str, motivo: str = "", expectativas: str = "", o_que_faltava: str = ""):
    if not email_destino or email_destino == "-": return
    access_token = get_valid_access_token()
    if not access_token: return

    primeiro_nome = nome.split(" ")[0] if nome else "Parceiro"
    empresa_exibicao = empresa if empresa else "sua empresa" 
    regras = obter_regras_dinamicas()
    
    if categoria == 'Promotor':
        assunto, template_customizado = f"Obrigado pela sua nota {nota}! 🌟", regras.get("email_agradecimento_promotor", "")
    elif categoria == 'Neutro':
        assunto, template_customizado = "Recebemos a sua avaliação. Vamos melhorar! 🚀", regras.get("email_agradecimento_neutro", "")
    else:
        assunto, template_customizado = "O seu feedback é muito importante para nós 💡", regras.get("email_agradecimento_detrator", "")

    if template_customizado:
        mail_html = tornar_links_absolutos(template_customizado).replace("{nome}", primeiro_nome).replace("{empresa}", empresa_exibicao).replace("{nota}", str(nota)).replace("{motivo}", motivo if motivo else "N/A").replace("{expectativas}", expectativas if expectativas else "N/A").replace("{o_que_faltava}", o_que_faltava if o_que_faltava else "N/A")
    else:
        mail_html = f"<h2>Obrigado, {primeiro_nome}!</h2><p>A sua nota {nota} foi cadastrada para a empresa {empresa_exibicao}.</p>"

    payload = {
        "message": {"subject": assunto, "body": {"contentType": "HTML", "content": mail_html}, "toRecipients": [{"emailAddress": {"address": email_destino}}]},
        "saveToSentItems": True
    }
    headers = {"Authorization": f"Bearer {access_token}", "Content-Type": "application/json"}

    try:
        resposta_ms = post_email("https://graph.microsoft.com/v1.0/me/sendMail", headers=headers, json=payload)
        if resposta_ms.status_code in (200, 202):
            registrar_log_disparo(email_destino, primeiro_nome, "Enviado", assunto)
        else:
            registrar_log_disparo(email_destino, primeiro_nome, "Erro", assunto, erro=resposta_ms.text)
    except Exception as e:
        registrar_log_disparo(email_destino, nome, "Erro", assunto, erro=str(e))

def validar_dominio_email(email: str, conn):
    query = text("SELECT valor FROM dbo.nps_configuracoes WHERE chave = 'dominios_permitidos'")
    config_dominios = conn.execute(query).scalar()
    
    if not config_dominios or not config_dominios.strip():
        raise HTTPException(status_code=403, detail="Segurança: O sistema não possui domínios autorizados configurados.")

    try:
        dominio_usuario = email.split('@')[1].lower().strip()
        dominios_validos = [d.strip().lower() for d in config_dominios.split(',') if d.strip()]
        if dominio_usuario not in dominios_validos:
            raise HTTPException(status_code=403, detail=f"Acesso negado: O domínio '@{dominio_usuario}' não está na lista de permissões.")
    except IndexError:
        raise HTTPException(status_code=400, detail="O formato do e-mail é inválido.")

def enviar_email_confirmacao(email_destino: str, nome_usuario: str, secret_key: str, algorithm: str, url_frontend: str, url_backend: str):
    expire = datetime.now(timezone.utc) + timedelta(hours=24)
    
    # 🎯 1. EMBUTIR A URL DO SITE NO TOKEN
    payload = {
        "sub": email_destino, 
        "exp": expire, 
        "tipo_token": "confirmacao_email",
        "origin": url_frontend  # O token memoriza de onde o usuário veio!
    }
    token = jwt.encode(payload, secret_key, algorithm=algorithm)
    
    # 🎯 2. O LINK DO E-MAIL APONTA PARA A API (Backend)
    link_confirmacao = f"{url_backend.rstrip('/')}/api/auth/verificar-email?token={token}"

    try:
        access_token = get_valid_access_token()
        if not access_token: return False

        engine = get_engine()
        with engine.connect() as conn:
            config = conn.execute(text("SELECT email_remetente FROM dbo.nps_configuracoes_email")).mappings().first()
            if (not config or not config["email_remetente"]) and not usando_resend(): return False

            send_url = f"https://graph.microsoft.com/v1.0/users/{(config or {}).get('email_remetente', '')}/sendMail"
            
            # Melhoria de UX: Saudação personalizada no HTML
            html_content = f"""
            <div style="font-family: Arial, sans-serif; max-width: 500px; padding: 20px; border: 1px solid #e2e8f0; border-radius: 10px;">
                <h2 style="color: #1e293b;">Confirme o seu e-mail</h2>
                <p>Olá, <strong>{nome_usuario}</strong>!</p>
                <p>Recebemos um pedido de registro no Rakiti com este e-mail.</p>
                <p>Para comprovar a titularidade da conta, por favor clique no botão abaixo:</p>
                <a href="{link_confirmacao}" style="display: inline-block; background-color: #f97316; color: white; padding: 12px 24px; text-decoration: none; border-radius: 6px; font-weight: bold; margin: 20px 0;">Verificar Meu E-mail</a>
                <p style="font-size: 12px; color: #64748b;">Se não solicitou este registro, pode ignorar este e-mail.</p>
            </div>
            """

            email_body = {
                "message": {
                    "subject": "Confirme o seu e-mail - Rakiti", 
                    "body": {"contentType": "HTML", "content": html_content}, 
                    "toRecipients": [{"emailAddress": {"address": email_destino}}]
                }, 
                "saveToSentItems": "true"
            }
            headers = {'Authorization': f'Bearer {access_token}', 'Content-Type': 'application/json'}
            res_email = post_email(send_url, json=email_body, headers=headers)
            
            # 🎯 3. REGISTRO DE LOG COM O NOME REAL
            if res_email.status_code == 202:
                registrar_log_disparo(email_destino, nome_usuario, "Enviado", "Confirme o seu e-mail", url=link_confirmacao)
                return True
            else:
                registrar_log_disparo(email_destino, nome_usuario, "Erro", "Confirme o seu e-mail", erro=res_email.text, url=link_confirmacao)
                return False

    except Exception as e:
        registrar_log_disparo(email_destino, nome_usuario, "Erro", "Confirme o seu e-mail", erro=str(e), url=link_confirmacao)
        return False
    
def validar_senha_forte(password: str):
    """
    Critérios de segurança atualizados para evitar estouro do Bcrypt
    """
    if len(password) < 8:
        raise HTTPException(status_code=400, detail="A senha deve ter pelo menos 8 caracteres.")
    
    if len(password) > 70:
        raise HTTPException(status_code=400, detail="A senha é demasiado longa. O limite é de 70 caracteres.")
    
    if not re.search(r"[A-Z]", password):
        raise HTTPException(status_code=400, detail="A senha deve conter pelo menos uma letra maiúscula.")
        
    if not re.search(r"[0-9]", password):
        raise HTTPException(status_code=400, detail="A senha deve conter pelo menos um número.")
        
    if not re.search(r"[@$!%*?&]", password):
        raise HTTPException(status_code=400, detail="A senha deve conter pelo menos um caractere especial.")