"""
Formulário próprio de pesquisa da Rakiti (NPS 0-10 e CSAT 1-5).

Cada convite tem um token único. O cliente abre {FRONTEND_URL}/r/{token}, responde e a
resposta entra no mesmo fluxo das respostas do Fillout (plano de ação, alertas, e-mail).
"""
import os
import secrets
import html as _html
from sqlalchemy import text

from database import get_engine, conta_atual, modo_sistema, usando_conta


# ------------------------------------------------------------------ utilidades
def gerar_token() -> str:
    return secrets.token_urlsafe(24)


def url_formulario(token: str) -> str:
    base = (os.getenv("FRONTEND_URL", "") or "http://localhost:5173").rstrip("/")
    return f"{base}/r/{token}"


def usa_formulario_proprio(regras: dict) -> bool:
    return str(regras.get("formulario_tipo") or "proprio").strip().lower() != "externo"


def nome_da_conta(conta_id=None) -> str:
    conta_id = conta_id or conta_atual()
    try:
        with modo_sistema():
            with get_engine().connect() as conn:
                return conn.execute(text("SELECT nome FROM dbo.nps_contas WHERE id = :id"), {"id": conta_id}).scalar() or "nossa empresa"
    except Exception:
        return "nossa empresa"


def _config(conn, chave, padrao=""):
    valor = conn.execute(text("SELECT valor FROM dbo.nps_configuracoes WHERE chave = :c"), {"c": chave}).scalar()
    return valor if valor not in (None, "") else padrao


# ------------------------------------------------------------------ e-mails
_CORES_NPS = {**{n: "#ef4444" for n in range(0, 7)}, 7: "#f59e0b", 8: "#f59e0b", 9: "#10b981", 10: "#10b981"}
_ROSTOS_CSAT = {1: "😡", 2: "🙁", 3: "😐", 4: "🙂", 5: "😍"}


def _moldura(conteudo: str, remetente: str) -> str:
    return f"""<!DOCTYPE html><html><head><meta charset="utf-8"></head>
<body style="margin:0;padding:0;background:#f1f5f9;font-family:Arial,Helvetica,sans-serif;">
<table width="100%" cellpadding="0" cellspacing="0" style="background:#f1f5f9;padding:32px 12px;"><tr><td align="center">
<table width="100%" cellpadding="0" cellspacing="0" style="max-width:560px;background:#ffffff;border-radius:16px;padding:32px;">
<tr><td style="font-size:13px;color:#64748b;padding-bottom:16px;">{_html.escape(remetente)}</td></tr>
<tr><td>{conteudo}</td></tr>
<tr><td style="font-size:11px;color:#94a3b8;padding-top:24px;">Leva menos de 1 minuto. Obrigado!</td></tr>
</table></td></tr></table></body></html>"""


def botoes_nps_html(url: str) -> str:
    celulas = "".join(
        f'<td style="padding:2px;"><a href="{url}?nota={n}" style="display:block;width:36px;line-height:36px;'
        f'text-align:center;border-radius:8px;background:{_CORES_NPS[n]};color:#fff;font-weight:bold;'
        f'font-size:14px;text-decoration:none;">{n}</a></td>'
        for n in range(0, 11)
    )
    return (f'<table cellpadding="0" cellspacing="0" style="margin:16px 0;"><tr>{celulas}</tr></table>'
            '<table width="100%" style="font-size:11px;color:#94a3b8;"><tr><td>Nada provável</td>'
            '<td align="right">Muito provável</td></tr></table>')


def html_convite_nps(nome: str, url: str, pergunta: str, remetente: str, com_botoes: bool = True) -> str:
    botoes = botoes_nps_html(url) if com_botoes else ""
    conteudo = f"""<p style="font-size:16px;color:#0f172a;">Olá, {_html.escape(nome)}!</p>
<p style="font-size:16px;color:#0f172a;font-weight:bold;">{_html.escape(pergunta)}</p>
{botoes}
<p style="margin-top:20px;"><a href="{url}" style="display:inline-block;background:#f97316;color:#fff;padding:12px 20px;border-radius:10px;text-decoration:none;font-weight:bold;">Responder pesquisa</a></p>"""
    return _moldura(conteudo, remetente)


def html_convite_csat(nome: str, url: str, pergunta: str, remetente: str) -> str:
    celulas = "".join(
        f'<td style="padding:4px;"><a href="{url}?nota={n}" style="display:block;width:48px;line-height:48px;'
        f'text-align:center;border-radius:12px;background:#f8fafc;border:1px solid #e2e8f0;font-size:26px;'
        f'text-decoration:none;">{_ROSTOS_CSAT[n]}</a></td>'
        for n in range(1, 6)
    )
    conteudo = f"""<p style="font-size:16px;color:#0f172a;">Olá, {_html.escape(nome)}!</p>
<p style="font-size:16px;color:#0f172a;font-weight:bold;">{_html.escape(pergunta)}</p>
<table cellpadding="0" cellspacing="0" style="margin:16px 0;"><tr>{celulas}</tr></table>
<p style="font-size:11px;color:#94a3b8;">Toque no rosto que representa sua experiência.</p>"""
    return _moldura(conteudo, remetente)


# ------------------------------------------------------------------ leitura / resposta pública
def _disparo_por_token(conn, token: str):
    return conn.execute(text("""
        SELECT d.id, d.conta_id, d.cliente_id, d.empresa_id, d.nome, d.email, d.tipo_pesquisa,
               d.referencia, d.assunto_pesquisa, d.respondido_em,
               COALESCE(e.nome::text, c.empresa) AS empresa_cliente
        FROM dbo.nps_disparos d
        LEFT JOIN dbo.nps_clientes c ON c.cliente_id = d.cliente_id
        LEFT JOIN dbo.nps_empresas e ON e.id = COALESCE(d.empresa_id, c.empresa_id)
        WHERE d.token = :t
    """), {"t": token}).mappings().first()


def obter_pesquisa(token: str):
    """Dados para montar o formulário público. None se o link não existir."""
    if not token or len(token) < 16:
        return None
    with modo_sistema():
        with get_engine().connect() as conn:
            d = _disparo_por_token(conn, token)
    if not d:
        return None
    with usando_conta(d["conta_id"]):
        with get_engine().connect() as conn:
            conta_nome = nome_da_conta(d["conta_id"])
            tipo = d["tipo_pesquisa"] or "nps"
            if tipo == "csat":
                assunto = d["assunto_pesquisa"] or "o nosso atendimento"
                pergunta = _config(conn, "pergunta_csat", "Como você avalia {assunto}?").replace("{assunto}", assunto)
            else:
                pergunta = _config(conn, "pergunta_nps", "De 0 a 10, quanto você recomendaria a {empresa} a um amigo ou colega?").replace("{empresa}", conta_nome)
    return {
        "tipo": tipo,
        "empresa": conta_nome,
        "nome": (d["nome"] or "").split(" ")[0],
        "pergunta": pergunta,
        "referencia": d["referencia"],
        "respondida": d["respondido_em"] is not None,
    }


def registrar_resposta(token: str, nota: int, comentario: str = ""):
    """Grava a resposta do formulário próprio. Retorna (ok, mensagem)."""
    with modo_sistema():
        with get_engine().connect() as conn:
            d = _disparo_por_token(conn, token)
    if not d:
        return False, "Link de pesquisa inválido."
    if d["respondido_em"] is not None:
        return False, "Esta pesquisa já foi respondida. Obrigado!"
    tipo = d["tipo_pesquisa"] or "nps"
    comentario = (comentario or "").strip()[:4000]

    with usando_conta(d["conta_id"]):
        if tipo == "csat":
            if nota < 1 or nota > 5:
                return False, "Escolha uma nota de 1 a 5."
            _gravar_csat(d, nota, comentario)
        else:
            if nota < 0 or nota > 10:
                return False, "Escolha uma nota de 0 a 10."
            _gravar_nps(d, token, nota, comentario)
        with get_engine().begin() as conn:
            conn.execute(text("UPDATE dbo.nps_disparos SET respondido_em = CURRENT_TIMESTAMP, status = 'Respondido', updated_at = CURRENT_TIMESTAMP WHERE id = :id"), {"id": d["id"]})
    return True, "Resposta registrada. Obrigado!"


def _gravar_nps(d, token, nota, comentario):
    """Reaproveita o mesmo fluxo do webhook (ação automática, alertas, e-mail de agradecimento)."""
    from services.respostas_svc import processar_webhook_fillout
    params = [
        {"name": "clienteId", "value": d["cliente_id"] or ""},
        {"name": "email", "value": d["email"] or ""},
        {"name": "nome", "value": d["nome"] or ""},
        {"name": "empresa", "value": d["empresa_cliente"] or ""},
        {"name": "empresa_id", "value": str(d["empresa_id"] or "")},
    ]
    payload = {
        "formId": "rakiti",
        "submission": {
            "submissionId": f"rakiti-{token}",
            "urlParameters": params,
            "questions": [
                {"type": "OpinionScale", "name": "nota", "value": nota},
                {"type": "LongAnswer", "name": "motivo", "value": comentario},
            ],
        },
    }
    processar_webhook_fillout(payload, canal="Formulário Rakiti")


def _gravar_csat(d, nota, comentario):
    with get_engine().begin() as conn:
        conn.execute(text("""
            INSERT INTO dbo.nps_csat_respostas (disparo_id, cliente_id, empresa_id, email, nota, comentario, referencia, assunto)
            VALUES (:did, :cid, :eid, :em, :n, :c, :ref, :ass)
        """), {"did": d["id"], "cid": d["cliente_id"], "eid": d["empresa_id"], "em": d["email"], "n": nota,
               "c": comentario, "ref": d["referencia"], "ass": d["assunto_pesquisa"]})
        if nota <= 2:
            # Insatisfação: vira Plano de Ação para o responsável tratar
            titulo = f"[CSAT {nota}] Cliente insatisfeito: {d['assunto_pesquisa'] or d['referencia'] or 'atendimento'}"
            descricao = f"Cliente: {d['nome'] or d['email']}\nReferência: {d['referencia'] or '-'}\nComentário: \"{comentario or 'sem comentário'}\""
            conn.execute(text("""
                INSERT INTO dbo.nps_acoes (empresa_id, titulo, descricao, prioridade, status, prazo_limite, created_at, updated_at)
                VALUES (:eid, :t, :d, 'Alta', 'Pendente', CURRENT_TIMESTAMP + INTERVAL '2 days', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
            """), {"eid": d["empresa_id"], "t": titulo[:250], "d": descricao})


# ------------------------------------------------------------------ envio de CSAT
def enviar_csat(email: str, nome: str = "", referencia: str = "", assunto: str = "", telefone: str = "",
                enviar_email: bool = True):
    """Cria o convite de CSAT (e envia por e-mail). Retorna dict com link e token."""
    from services.email_svc import registrar_log_disparo
    from services.mail_provider import enviar_mensagem_graph, usando_resend
    email = (email or "").strip()
    if "@" not in email and not telefone:
        raise ValueError("Informe o e-mail (ou telefone) do cliente.")
    engine = get_engine()
    with engine.begin() as conn:
        cli = conn.execute(text("SELECT cliente_id, nome, empresa_id FROM dbo.nps_clientes WHERE email = :e"), {"e": email}).mappings().first() if email else None
        if not cli and email:
            novo_id = str(secrets.randbelow(900000000) + 100000000)
            conn.execute(text("""
                INSERT INTO dbo.nps_clientes (cliente_id, nome, email, telefone, ativo, status_envio, created_at, updated_at)
                VALUES (:id, :n, :e, :t, 1, 'Pendente', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
            """), {"id": novo_id, "n": nome or email.split("@")[0], "e": email, "t": telefone or None})
            cli = {"cliente_id": novo_id, "nome": nome, "empresa_id": None}
        pergunta_modelo = _config(conn, "pergunta_csat", "Como você avalia {assunto}?")

    token = gerar_token()
    link = url_formulario(token)
    assunto_txt = assunto or "o nosso atendimento"
    pergunta = pergunta_modelo.replace("{assunto}", assunto_txt)
    remetente = nome_da_conta()
    nome_exib = (nome or (cli or {}).get("nome") or "").split(" ")[0] or "cliente"
    titulo = f"{remetente}: como foi {assunto_txt}?"

    status, erro = "Criado", None
    if enviar_email and email:
        if not usando_resend():
            status, erro = "Erro", "Envio de e-mail não configurado."
        else:
            r = enviar_mensagem_graph({"message": {
                "subject": titulo,
                "body": {"contentType": "HTML", "content": html_convite_csat(nome_exib, link, pergunta, remetente)},
                "toRecipients": [{"emailAddress": {"address": email}}]}})
            status, erro = ("Enviado", None) if r.status_code in (200, 202) else ("Erro", r.text)

    registrar_log_disparo(email, nome or nome_exib, status, titulo, erro=erro,
                          cliente_id=(cli or {}).get("cliente_id"), empresa_id=(cli or {}).get("empresa_id"),
                          url=link, token=token, tipo="csat", referencia=referencia, assunto_pesquisa=assunto_txt)
    return {"status": status, "link": link, "token": token, "erro": erro}


def resumo_csat(dias: int = 90):
    with get_engine().connect() as conn:
        r = conn.execute(text("""
            SELECT COUNT(*) AS total, ROUND(AVG(nota)::numeric, 2) AS media,
                   ROUND(100.0 * SUM(CASE WHEN nota >= 4 THEN 1 ELSE 0 END) / NULLIF(COUNT(*), 0), 1) AS satisfeitos_pct
            FROM dbo.nps_csat_respostas WHERE created_at >= CURRENT_TIMESTAMP - make_interval(days => :d)
        """), {"d": dias}).mappings().first()
        ultimas = conn.execute(text("""
            SELECT r.id, r.nota, r.comentario, r.referencia, r.assunto, r.created_at, COALESCE(c.nome, r.email::text) AS cliente
            FROM dbo.nps_csat_respostas r LEFT JOIN dbo.nps_clientes c ON c.cliente_id = r.cliente_id
            ORDER BY r.created_at DESC LIMIT 20
        """)).mappings().all()
    return {"total": r["total"], "media": float(r["media"] or 0), "satisfeitos_pct": float(r["satisfeitos_pct"] or 0),
            "ultimas": [dict(u) for u in ultimas]}
