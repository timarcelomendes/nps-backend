import requests
from datetime import datetime
from sqlalchemy import text
from database import get_engine

# ==========================================
# 🚀 1. ALERTA EM TEMPO REAL (NPS RECEBIDO)
# ==========================================
def enviar_alerta_teams(resposta_id: str, cliente_id: str, nome: str, email: str, empresa: str, nota: int, categoria: str, motivo: str, expectativas: str, o_que_faltava: str, form_id: str, submission_id: str):
    """Monta um Adaptive Card com layout avançado e envia para o canal global do Teams"""
    try:
        engine = get_engine()
        with engine.connect() as conn:
            query_webhook = text("SELECT valor FROM dbo.nps_configuracoes WHERE chave = 'teams_webhook_url'")
            webhook_url = conn.execute(query_webhook).scalar()
            
            perfil, segmento = "-", "-"
            if cliente_id:
                query_cli = text("SELECT p.nome AS perfil_decisor, s.nome AS segmento FROM dbo.nps_clientes c LEFT JOIN dbo.nps_perfis p ON p.id = c.perfil_id LEFT JOIN dbo.nps_segmentos s ON s.id = c.segmento_id WHERE c.cliente_id = :cid")
                res_cli = conn.execute(query_cli, {"cid": cliente_id}).fetchone()
                if res_cli:
                    perfil = res_cli.perfil_decisor or "-"
                    segmento = res_cli.segmento or "-"

        if not webhook_url:
            print("⚠️ Webhook Global do Teams não configurado. Alerta de nova resposta ignorado.")
            return

        if categoria == 'Detrator':
            emoji, cor_nota = '🚨', 'Attention'
        elif categoria == 'Neutro':
            emoji, cor_nota = '⚠️', 'Warning'
        elif categoria == 'Promotor':
            emoji, cor_nota = '✅', 'Good'
        else:
            emoji, cor_nota = '📊', 'Default'

        motivo_txt = motivo if motivo else "Sem comentário."
        expectativas_txt = expectativas if expectativas else "Não respondido."
        data_hoje = datetime.now().strftime("%Y-%m-%d")

        url_painel = f"https://build.fillout.com/editor/{form_id}/results"
        url_resposta = f"https://build.fillout.com/editor/{form_id}/results?sessionId={submission_id}"

        card_body = [
            {
                "type": "ColumnSet",
                "columns": [
                    {
                        "type": "Column",
                        "width": "stretch",
                        "items": [
                            { "type": "TextBlock", "text": f"{emoji} NPS Fillout — {categoria}", "weight": "Bolder", "size": "Large", "wrap": True },
                            { "type": "TextBlock", "text": f"Empresa: {empresa if empresa else '-'}", "wrap": True, "spacing": "None", "isSubtle": True }
                        ]
                    },
                    {
                        "type": "Column",
                        "width": "auto",
                        "items": [
                            { "type": "TextBlock", "text": f"{nota}/10", "weight": "Bolder", "size": "ExtraLarge", "color": cor_nota, "horizontalAlignment": "Right" }
                        ]
                    }
                ]
            },
            {
                "type": "FactSet",
                "spacing": "Medium",
                "facts": [
                    { "title": "Contato:", "value": nome if nome else "-" },
                    { "title": "E-mail:", "value": email if email else "-" },
                    { "title": "Data:", "value": data_hoje },
                    { "title": "ClienteId:", "value": cliente_id if cliente_id else "-" },
                    { "title": "Perfil:", "value": perfil },
                    { "title": "Segmento:", "value": segmento },
                    { "title": "RespostaId:", "value": resposta_id }
                ]
            },
            { "type": "TextBlock", "text": f"**Motivo da nota:**\n{motivo_txt}", "wrap": True, "spacing": "Medium" },
            { "type": "TextBlock", "text": f"**Atendeu às expectativas?**\n{expectativas_txt}", "wrap": True, "spacing": "Small" }
        ]

        if o_que_faltava:
            card_body.append({ "type": "TextBlock", "text": f"**O que estava faltando?**\n{o_que_faltava}", "wrap": True, "spacing": "Small" })

        card_body.append({
            "type": "TextBlock", 
            "text": "🤖 *Enviado automaticamente pelo Hub de NPS*", 
            "wrap": True, 
            "spacing": "Large", 
            "size": "Small", 
            "isSubtle": True
        })

        payload_teams = {
            "type": "message",
            "attachments": [{
                "contentType": "application/vnd.microsoft.card.adaptive",
                "contentUrl": None,
                "content": {
                    "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
                    "type": "AdaptiveCard",
                    "version": "1.4",
                    "body": card_body,
                    "actions": [
                        { "type": "Action.OpenUrl", "title": "Ver Painel Geral", "url": url_painel },
                        { "type": "Action.OpenUrl", "title": "Ver Resposta Específica", "url": url_resposta }
                    ]
                }
            }]
        }

        resposta = requests.post(webhook_url, json=payload_teams, headers={"Content-Type": "application/json"})
        
        if resposta.status_code in (200, 201, 202):
            print(f"📣 Alerta Teams enviado com sucesso para {nome}!")
        else:
            print(f"❌ Falha ao enviar alerta para o Teams. HTTP {resposta.status_code}: {resposta.text}")

    except Exception as e:
        print(f"❌ Erro interno ao enviar alerta do Teams: {e}")

import os
import requests
from datetime import datetime, date

def enviar_resumo_matinal_gestores():
    """
    Função que varre os tickets pendentes e envia um resumo diário para o webhook privado de cada gestor.
    """
    try:
        from database import get_engine
        from sqlalchemy import text
        engine = get_engine()
        
        with engine.begin() as conn:
            # Procura todos os gestores que têm um webhook configurado
            query_gestores = text("""
                SELECT id, nome, teams_webhook 
                FROM dbo.nps_gestores 
                WHERE teams_webhook IS NOT NULL AND teams_webhook != ''
            """)
            gestores = conn.execute(query_gestores).fetchall()

            for gestor in gestores:
                # Busca os tickets pendentes/atrasados deste gestor
                query_tickets = text("""
                    SELECT id, descricao, prioridade, prazo_limite
                    FROM dbo.nps_acoes
                    WHERE gestor_id = :gid AND status != 'Concluído'
                    ORDER BY prazo_limite ASC
                """)
                tickets = conn.execute(query_tickets, {"gid": gestor.id}).fetchall()

                if not tickets:
                    continue # Sem pendências, sem spam!

                atrasados_count = 0
                fatos_lista = []
                
                # Data de hoje para comparação segura (ignora horas)
                hoje = date.today()

                for t in tickets[:5]: # Mostra os 5 mais urgentes
                    # BLINDAGEM 1: Comparação de datas à prova de crash
                    atrasado = False
                    if t.prazo_limite:
                        try:
                            prazo = t.prazo_limite.date() if isinstance(t.prazo_limite, datetime) else t.prazo_limite
                            if prazo < hoje:
                                atrasado = True
                                atrasados_count += 1
                        except:
                            pass

                    status_prazo = "🔴 ATRASADO" if atrasado else "🟡 Pendente"
                    
                    # Corta a descrição para não estragar o layout do cartão se for muito grande
                    desc_curta = (t.descricao[:30] + '...') if t.descricao and len(t.descricao) > 30 else (t.descricao or 'Ação sem descrição')
                    
                    fatos_lista.append({
                        "title": f"#{str(t.id).zfill(3)} - {desc_curta}",
                        "value": f"{t.prioridade} | {status_prazo}"
                    })

                # A URL que aponta para o seu Frontend na aba de Planos de Ação
                frontend_url = os.getenv("FRONTEND_URL", "http://localhost:5173").rstrip('/')
                url_kanban = f"{frontend_url}/acoes"

                # BLINDAGEM 2: Adaptive Card na versão correta para o Teams (1.2)
                payload = {
                    "type": "message",
                    "attachments": [{
                        "contentType": "application/vnd.microsoft.card.adaptive",
                        "content": {
                            "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
                            "type": "AdaptiveCard",
                            "version": "1.2", # 👈 Muito importante para o Teams!
                            "body": [
                                {
                                    "type": "TextBlock",
                                    "text": f"Bom dia, {gestor.nome.split()[0]}! ☕",
                                    "size": "Large",
                                    "weight": "Bolder",
                                    "color": "Accent"
                                },
                                {
                                    "type": "TextBlock",
                                    "text": f"Tem **{len(tickets)} ações pendentes** no Kanban de NPS.",
                                    "wrap": True
                                },
                                {
                                    "type": "FactSet",
                                    "facts": fatos_lista
                                }
                            ],
                            "actions": [
                                {
                                    "type": "Action.OpenUrl",
                                    "title": "Abrir Kanban de Ações",
                                    "url": url_kanban
                                }
                            ]
                        }
                    }]
                }

                # BLINDAGEM 3: Captura exata do erro retornado pelo Teams
                resposta = requests.post(
                    gestor.teams_webhook, 
                    json=payload, 
                    headers={"Content-Type": "application/json"},
                    timeout=10
                )
                
                if resposta.status_code in (200, 201, 202):
                    print(f"✅ Resumo matinal do Teams enviado para: {gestor.nome}")
                else:
                    # Agora sim, se o Teams recusar, o console vai "gritar" o motivo!
                    print(f"❌ Erro Teams ({resposta.status_code}) para {gestor.nome}: {resposta.text}")

    except Exception as e:
        import traceback
        print(f"❌ Erro CRÍTICO ao enviar resumos do Teams:")
        traceback.print_exc()

def enviar_alerta_tecnico_teams(mensagem_erro: str):
    """
    Envia um alerta crítico de sistema para o canal de TI/DevOps no Teams,
    buscando a URL do webhook dinamicamente na tabela de configurações.
    """
    import requests
    from datetime import datetime
    from sqlalchemy import text
    from database import get_engine
    
    webhook_url = None
    
    # 1. Busca a URL do Webhook na tabela de configurações
    try:
        engine = get_engine()
        with engine.connect() as conn:
            query = text("SELECT valor FROM dbo.nps_configuracoes WHERE chave = 'teams_alerts_webhook'")
            webhook_url = conn.execute(query).scalar()
    except Exception as db_err:
        print(f"❌ Erro ao buscar 'teams_alerts_webhook' no banco de dados: {db_err}")
        return
        
    if not webhook_url:
        print("⚠️ A chave 'teams_alerts_webhook' não está configurada na tabela nps_configuracoes. Alerta técnico cancelado.")
        return

    # 2. Monta e envia o payload para o Teams
    try:
        data_hora = datetime.now().strftime("%d/%m/%Y %H:%M:%S")
        
        payload = {
            "type": "message",
            "attachments": [{
                "contentType": "application/vnd.microsoft.card.adaptive",
                "content": {
                    "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
                    "type": "AdaptiveCard",
                    "version": "1.2",
                    "body": [
                        {
                            "type": "TextBlock",
                            "text": "🚨 ALERTA CRÍTICO - NPS INTELLIGENCE",
                            "weight": "Bolder",
                            "size": "Medium",
                            "color": "Attention" # Deixa o texto vermelho
                        },
                        {
                            "type": "TextBlock",
                            "text": f"**Data/Hora:** {data_hora}",
                            "wrap": True,
                            "size": "Small",
                            "isSubtle": True
                        },
                        {
                            "type": "TextBlock",
                            "text": mensagem_erro,
                            "wrap": True,
                            "spacing": "Medium"
                        }
                    ]
                }
            }]
        }

        resposta = requests.post(
            webhook_url, 
            json=payload, 
            headers={"Content-Type": "application/json"},
            timeout=10
        )
        
        if resposta.status_code not in (200, 201, 202):
            print(f"❌ Falha ao disparar alerta técnico no Teams: HTTP {resposta.status_code} - {resposta.text}")
        else:
            print("📨 [Teams SVC] Alerta técnico enviado com sucesso para a equipe.")
            
    except Exception as e:
        print(f"❌ Erro interno ao tentar enviar alerta técnico para o Teams: {e}")