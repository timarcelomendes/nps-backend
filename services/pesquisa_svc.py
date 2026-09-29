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
from services import formularios_svc as fs


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


def html_convite_csat(nome: str, url: str, pergunta: str, remetente: str, estrelas: bool = False) -> str:
    if estrelas:
        simbolos = {n: "★" * n for n in range(1, 6)}
        estilo = "font-size:14px;color:#f59e0b;width:auto;padding:0 8px;"
    else:
        simbolos = _ROSTOS_CSAT
        estilo = "font-size:26px;width:48px;"
    celulas = "".join(
        f'<td style="padding:4px;"><a href="{url}?nota={n}" style="display:block;{estilo}line-height:48px;'
        f'text-align:center;border-radius:12px;background:#f8fafc;border:1px solid #e2e8f0;'
        f'text-decoration:none;">{simbolos[n]}</a></td>'
        for n in range(1, 6)
    )
    dica = "Toque nas estrelas para avaliar." if estrelas else "Toque no rosto que representa sua experiência."
    conteudo = f"""<p style="font-size:16px;color:#0f172a;">Olá, {_html.escape(nome)}!</p>
<p style="font-size:16px;color:#0f172a;font-weight:bold;">{_html.escape(pergunta)}</p>
<table cellpadding="0" cellspacing="0" style="margin:16px 0;"><tr>{celulas}</tr></table>
<p style="font-size:11px;color:#94a3b8;">{dica}</p>"""
    return _moldura(conteudo, remetente)


def html_convite_formulario(form_render: dict, nome: str, url: str, remetente: str) -> str:
    """Convite por e-mail conforme a nota principal do formulário (botões de nota clicáveis)."""
    tipo = form_render.get("principal_tipo")
    principal = next((p for p in form_render["perguntas"] if p["id"] == form_render.get("principal_id")), None)
    if tipo == "nps":
        return html_convite_nps(nome, url, principal["titulo"], remetente, com_botoes=True)
    if tipo in ("csat", "estrelas"):
        return html_convite_csat(nome, url, principal["titulo"], remetente, estrelas=(tipo == "estrelas"))
    cor = (form_render.get("tema") or {}).get("cor") or "#f97316"
    conteudo = f"""<p style="font-size:16px;color:#0f172a;">Olá, {_html.escape(nome)}!</p>
<p style="font-size:16px;color:#0f172a;font-weight:bold;">{_html.escape(form_render.get("nome") or "Queremos ouvir você")}</p>
<p style="margin-top:20px;"><a href="{url}" style="display:inline-block;background:{cor};color:#fff;padding:12px 20px;border-radius:10px;text-decoration:none;font-weight:bold;">Responder pesquisa</a></p>"""
    return _moldura(conteudo, remetente)


# ------------------------------------------------------------------ leitura / resposta pública
def _disparo_por_token(conn, token: str):
    return conn.execute(text("""
        SELECT d.id, d.conta_id, d.cliente_id, d.empresa_id, d.nome, d.email, d.tipo_pesquisa, d.formulario_id,
               d.referencia, d.assunto_pesquisa, d.respondido_em,
               COALESCE(e.nome::text, c.empresa) AS empresa_cliente
        FROM dbo.nps_disparos d
        LEFT JOIN dbo.nps_clientes c ON c.cliente_id = d.cliente_id
        LEFT JOIN dbo.nps_empresas e ON e.id = COALESCE(d.empresa_id, c.empresa_id)
        WHERE d.token = :t
    """), {"t": token}).mappings().first()


def _form_do_disparo(conn, d):
    fid = d["formulario_id"] or fs.id_padrao(conn, "csat" if d["tipo_pesquisa"] == "csat" else "nps")
    return fs.obter(conn, fid) if fid else None


def _contexto(d, token, conta_nome, form_id):
    return {
        "disparo_id": d["id"], "token": token, "cliente_id": d["cliente_id"], "empresa_id": d["empresa_id"],
        "email": d["email"], "nome": (d["nome"] or "").split(" ")[0], "empresa_cliente": d["empresa_cliente"],
        "referencia": d["referencia"], "assunto": d["assunto_pesquisa"] or "o nosso atendimento",
        "empresa": conta_nome, "formulario_id": form_id,
    }


def obter_pesquisa(token: str):
    """Formulário para a página pública. None se o link não existir."""
    if not token or len(token) < 16:
        return None
    with modo_sistema():
        with get_engine().connect() as conn:
            d = _disparo_por_token(conn, token)
    if not d:
        return None
    with usando_conta(d["conta_id"]):
        with get_engine().connect() as conn:
            form = _form_do_disparo(conn, d)
        conta_nome = nome_da_conta(d["conta_id"])
    if not form:
        return None
    ctx = _contexto(d, token, conta_nome, form["id"])
    return {
        "empresa": conta_nome,
        "nome": ctx["nome"],
        "referencia": d["referencia"],
        "respondida": d["respondido_em"] is not None,
        "formulario": fs.renderizar(form, ctx),
    }


def registrar_resposta(token: str, respostas: dict = None, nota: int = None, comentario: str = ""):
    """Grava a resposta do link de um convite. Retorna (ok, mensagem)."""
    with modo_sistema():
        with get_engine().connect() as conn:
            d = _disparo_por_token(conn, token or "")
    if not d:
        return False, "Link de pesquisa inválido."
    if d["respondido_em"] is not None:
        return False, "Esta pesquisa já foi respondida. Obrigado!"
    with usando_conta(d["conta_id"]):
        with get_engine().connect() as conn:
            form = _form_do_disparo(conn, d)
        if not form:
            return False, "Formulário não encontrado."
        if respostas is None:  # formato antigo: {nota, comentario}
            respostas = _respostas_legado(form, nota, comentario)
        ctx = _contexto(d, token, nome_da_conta(d["conta_id"]), form["id"])
        try:
            fs.gravar_resposta(form, respostas, ctx)
        except ValueError as e:
            return False, str(e)
        with get_engine().begin() as conn:
            conn.execute(text("""UPDATE dbo.nps_disparos SET respondido_em = CURRENT_TIMESTAMP, status = 'Respondido',
                                 updated_at = CURRENT_TIMESTAMP WHERE id = :id"""), {"id": d["id"]})
    return True, "Resposta registrada. Obrigado!"


def _respostas_legado(form, nota, comentario):
    principal = fs.pergunta_principal(form["perguntas"])
    r = {}
    if principal and nota is not None:
        r[principal["id"]] = nota
        texto = next((p for p in form["perguntas"] if p["tipo"] == "texto_longo"
                      and fs.pergunta_visivel(p, principal, nota)), None)
        if texto and comentario:
            r[texto["id"]] = comentario
    return r


# ------------------------------------------------------------------ link público (/f/{codigo})
def obter_formulario_publico(codigo: str):
    form, conta_id = fs.obter_por_codigo_publico(codigo)
    if not form:
        return None
    conta_nome = nome_da_conta(conta_id)
    ctx = {"empresa": conta_nome, "nome": "", "assunto": "a sua experiência", "referencia": ""}
    return {"empresa": conta_nome, "nome": "", "referencia": None, "respondida": False,
            "formulario": fs.renderizar(form, ctx)}


def registrar_resposta_publica(codigo: str, respostas: dict, referencia: str = ""):
    form, conta_id = fs.obter_por_codigo_publico(codigo)
    if not form:
        return False, "Formulário não encontrado ou desativado."
    with usando_conta(conta_id):
        ctx = {"empresa": nome_da_conta(conta_id), "nome": "", "assunto": "a sua experiência",
               "referencia": (referencia or "")[:255] or None, "formulario_id": form["id"], "canal": "Link público"}
        try:
            fs.gravar_resposta(form, respostas, ctx)
        except ValueError as e:
            return False, str(e)
    return True, "Resposta registrada. Obrigado!"


# ------------------------------------------------------------------ envio de CSAT
def enviar_csat(email: str, nome: str = "", referencia: str = "", assunto: str = "", telefone: str = "",
                enviar_email: bool = True, formulario_id: int = None):
    """Cria o convite (link único) e envia por e-mail. Retorna dict com link e token."""
    from services.email_svc import registrar_log_disparo
    from services.mail_provider import enviar_mensagem_graph, usando_resend
    email = (email or "").strip()
    if "@" not in email and not telefone:
        raise ValueError("Informe o e-mail (ou telefone) do cliente.")
    engine = get_engine()
    with engine.begin() as conn:
        fid = formulario_id or fs.id_padrao(conn, "csat")
        form = fs.obter(conn, fid) if fid else None
        if not form or not form.get("ativo"):
            raise ValueError("Formulário não encontrado. Crie um em Formulários ou informe um formulario_id válido.")
        cli = conn.execute(text("SELECT cliente_id, nome, empresa_id FROM dbo.nps_clientes WHERE email = :e"), {"e": email}).mappings().first() if email else None
        if not cli and email:
            novo_id = str(secrets.randbelow(900000000) + 100000000)
            conn.execute(text("""
                INSERT INTO dbo.nps_clientes (cliente_id, nome, email, telefone, ativo, status_envio, created_at, updated_at)
                VALUES (:id, :n, :e, :t, 1, 'Pendente', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
            """), {"id": novo_id, "n": nome or email.split("@")[0], "e": email, "t": telefone or None})
            cli = {"cliente_id": novo_id, "nome": nome, "empresa_id": None}

    token = gerar_token()
    link = url_formulario(token)
    assunto_txt = assunto or "o nosso atendimento"
    remetente = nome_da_conta()
    nome_exib = (nome or (cli or {}).get("nome") or "").split(" ")[0] or "cliente"
    render = fs.renderizar(form, {"empresa": remetente, "nome": nome_exib, "assunto": assunto_txt, "referencia": referencia})
    titulo = f"{remetente}: como foi {assunto_txt}?"
    tipo = "csat" if form["tipo"] == "csat" else "nps" if form["tipo"] == "nps" else "form"

    status, erro = "Criado", None
    if enviar_email and email:
        if not usando_resend():
            status, erro = "Erro", "Envio de e-mail não configurado."
        else:
            r = enviar_mensagem_graph({"message": {
                "subject": titulo,
                "body": {"contentType": "HTML", "content": html_convite_formulario(render, nome_exib, link, remetente)},
                "toRecipients": [{"emailAddress": {"address": email}}]}})
            status, erro = ("Enviado", None) if r.status_code in (200, 202) else ("Erro", r.text)

    registrar_log_disparo(email, nome or nome_exib, status, titulo, erro=erro,
                          cliente_id=(cli or {}).get("cliente_id"), empresa_id=(cli or {}).get("empresa_id"),
                          url=link, token=token, tipo=tipo, referencia=referencia, assunto_pesquisa=assunto_txt,
                          formulario_id=form["id"])
    return {"status": status, "link": link, "token": token, "erro": erro, "formulario_id": form["id"]}


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
