"""
Envio de e-mails da plataforma.

O código original montava as mensagens no formato do Microsoft Graph (Office 365).
Este módulo mantém esse formato de entrada e entrega por um provedor de e-mail transacional,
assim nenhum template ou regra de negócio precisou mudar.

Provedores (o primeiro configurado é usado; EMAIL_PROVEDOR força um deles):
  ZeptoMail (Zoho)  ZEPTOMAIL_TOKEN   token "Send Mail" do Mail Agent (com ou sem o prefixo Zoho-enczapikey)
                    ZEPTOMAIL_API_URL opcional; padrão https://api.zeptomail.com/v1.1/email
                                      (conta na Europa: https://api.zeptomail.eu/v1.1/email)
  Resend            RESEND_API_KEY

Comuns:
  EMAIL_REMETENTE       ex.: pesquisa@rakiti.com (domínio verificado no provedor)
  EMAIL_REMETENTE_NOME  ex.: Rakiti (opcional)
  EMAIL_RESPONDER_PARA  ex.: contato@rakiti.com (opcional; para onde vão as respostas dos clientes)
"""
import os
import requests

RESEND_URL = os.getenv("RESEND_API_URL", "https://api.resend.com/emails")
ZEPTOMAIL_URL = os.getenv("ZEPTOMAIL_API_URL", "https://api.zeptomail.com/v1.1/email")


def provedor_email() -> str | None:
    """'zeptomail', 'resend' ou None (envio não configurado)."""
    forcado = os.getenv("EMAIL_PROVEDOR", "").strip().lower()
    tem = {"zeptomail": bool(os.getenv("ZEPTOMAIL_TOKEN", "").strip()),
           "resend": bool(os.getenv("RESEND_API_KEY", "").strip())}
    if forcado in tem:
        return forcado if tem[forcado] else None
    return next((nome for nome, ok in tem.items() if ok), None)


def envio_configurado() -> bool:
    return provedor_email() is not None


# nome antigo, mantido porque o resto do código já o usa
usando_resend = envio_configurado


class _Resposta:
    """Imita o objeto de resposta do requests (status_code / text / json) usado pelo código legado."""
    def __init__(self, status_code: int, text: str = "", data: dict | None = None):
        self.status_code = status_code
        self.text = text
        self._data = data or {}

    def json(self):
        return self._data


def _remetente() -> str:
    email = os.getenv("EMAIL_REMETENTE", "").strip()
    if not email:
        # fallback: e-mail cadastrado na tela de Configurações
        try:
            from database import get_engine
            from sqlalchemy import text
            with get_engine().connect() as conn:
                email = conn.execute(text("SELECT email_remetente FROM dbo.nps_configuracoes_email LIMIT 1")).scalar() or ""
        except Exception:
            email = ""
    nome = os.getenv("EMAIL_REMETENTE_NOME", "").strip()
    return f"{nome} <{email}>" if nome and email else email


def _enderecos(lista) -> list[str]:
    return [r.get("emailAddress", {}).get("address") for r in (lista or []) if r.get("emailAddress", {}).get("address")]


def _separar_remetente():
    """'Nome <email>' -> (email, nome)"""
    bruto = _remetente()
    if "<" in bruto:
        nome, email = bruto.split("<", 1)
        return email.strip(" >"), nome.strip()
    return bruto.strip(), ""


def enviar_mensagem_graph(payload: dict) -> _Resposta:
    """Recebe o JSON no formato Graph ({"message": {...}}) e envia pelo provedor configurado."""
    msg = (payload or {}).get("message", payload or {})
    corpo = msg.get("body", {}) or {}
    email = {
        "to": _enderecos(msg.get("toRecipients")),
        "cc": _enderecos(msg.get("ccRecipients")),
        "bcc": _enderecos(msg.get("bccRecipients")),
        "reply_to": _enderecos(msg.get("replyTo")) or [e for e in [os.getenv("EMAIL_RESPONDER_PARA", "").strip()] if e],
        "subject": msg.get("subject", ""),
        "html": corpo.get("content", "") if str(corpo.get("contentType", "HTML")).upper() == "HTML" else None,
        "text": corpo.get("content", "") if str(corpo.get("contentType", "HTML")).upper() != "HTML" else None,
        "anexos": [{"nome": a.get("name", "anexo"), "conteudo": a.get("contentBytes", ""),
                    "tipo": a.get("contentType") or "application/octet-stream", "cid": a.get("contentId")}
                   for a in (msg.get("attachments") or [])],
    }
    remetente, nome = _separar_remetente()
    if not remetente:
        return _Resposta(400, "EMAIL_REMETENTE não configurado.")
    if not email["to"]:
        return _Resposta(400, "Destinatário vazio.")

    provedor = provedor_email()
    if provedor == "zeptomail":
        return _enviar_zeptomail(email, remetente, nome)
    if provedor == "resend":
        return _enviar_resend(email, remetente, nome)
    return _Resposta(503, "Envio de e-mail não configurado (defina ZEPTOMAIL_TOKEN ou RESEND_API_KEY).")


def _enviar_zeptomail(email, remetente, nome) -> _Resposta:
    token = os.getenv("ZEPTOMAIL_TOKEN", "").strip()
    if not token.lower().startswith("zoho-enczapikey"):
        token = f"Zoho-enczapikey {token}"
    dados = {
        "from": {"address": remetente, **({"name": nome} if nome else {})},
        "to": [{"email_address": {"address": e}} for e in email["to"]],
        "subject": email["subject"],
    }
    if email["html"] is not None:
        dados["htmlbody"] = email["html"]
    else:
        dados["textbody"] = email["text"] or ""
    if email["cc"]:
        dados["cc"] = [{"email_address": {"address": e}} for e in email["cc"]]
    if email["bcc"]:
        dados["bcc"] = [{"email_address": {"address": e}} for e in email["bcc"]]
    if email["reply_to"]:
        dados["reply_to"] = [{"address": e} for e in email["reply_to"]]
    anexos = [a for a in email["anexos"] if not a["cid"]]
    inline = [a for a in email["anexos"] if a["cid"]]
    if anexos:
        dados["attachments"] = [{"name": a["nome"], "content": a["conteudo"], "mime_type": a["tipo"]} for a in anexos]
    if inline:  # imagens referenciadas no HTML como cid:
        dados["inline_images"] = [{"cid": a["cid"], "content": a["conteudo"], "mime_type": a["tipo"]} for a in inline]
    try:
        r = requests.post(ZEPTOMAIL_URL, json=dados, timeout=30, headers={
            "Authorization": token, "Accept": "application/json", "Content-Type": "application/json"})
    except Exception as e:
        return _Resposta(503, f"Falha de rede ao enviar e-mail: {e}")
    if r.status_code in (200, 201, 202):
        return _Resposta(202, r.text, r.json() if r.text else {})
    return _Resposta(r.status_code, f"ZeptoMail recusou o envio: {r.text}")


def _enviar_resend(email, remetente, nome) -> _Resposta:
    dados = {"from": f"{nome} <{remetente}>" if nome else remetente, "to": email["to"], "subject": email["subject"]}
    if email["html"] is not None:
        dados["html"] = email["html"]
    else:
        dados["text"] = email["text"] or ""
    for campo in ("cc", "bcc", "reply_to"):
        if email[campo]:
            dados[campo] = email[campo]
    if email["anexos"]:
        dados["attachments"] = [{"filename": a["nome"], "content": a["conteudo"], "content_type": a["tipo"],
                                 **({"content_id": a["cid"]} if a["cid"] else {})} for a in email["anexos"]]
    try:
        r = requests.post(RESEND_URL, json=dados, timeout=30, headers={
            "Authorization": f"Bearer {os.getenv('RESEND_API_KEY', '').strip()}", "Content-Type": "application/json"})
    except Exception as e:
        return _Resposta(503, f"Falha de rede ao enviar e-mail: {e}")
    if r.status_code in (200, 201, 202):
        # O Graph devolvia 202 Accepted; o código legado verifica esse código.
        return _Resposta(202, r.text, r.json() if r.text else {})
    return _Resposta(r.status_code, f"Resend recusou o envio: {r.text}")


def post_email(url, json=None, headers=None, **kwargs):
    """Substitui requests.post nas chamadas de envio de e-mail."""
    if envio_configurado() and "graph.microsoft.com" in str(url) and "sendMail" in str(url):
        return enviar_mensagem_graph(json)
    return requests.post(url, json=json, headers=headers, **kwargs)
