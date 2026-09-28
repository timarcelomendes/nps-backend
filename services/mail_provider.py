"""
Envio de e-mails da plataforma.

O código original montava as mensagens no formato do Microsoft Graph (Office 365).
Este módulo mantém esse formato de entrada e entrega pelo Resend quando RESEND_API_KEY está
configurada — assim nenhum template ou regra de negócio precisou mudar.

Variáveis de ambiente:
  RESEND_API_KEY        chave da API do Resend (obrigatória para enviar)
  EMAIL_REMETENTE       ex.: pesquisa@rakiti.com (domínio verificado no Resend)
  EMAIL_REMETENTE_NOME  ex.: Rakiti (opcional)
"""
import os
import requests

RESEND_URL = os.getenv("RESEND_API_URL", "https://api.resend.com/emails")


def usando_resend() -> bool:
    return bool(os.getenv("RESEND_API_KEY", "").strip())


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


def enviar_mensagem_graph(payload: dict) -> _Resposta:
    """Recebe o JSON no formato Graph ({"message": {...}}) e envia pelo Resend."""
    msg = (payload or {}).get("message", payload or {})
    corpo = msg.get("body", {}) or {}
    dados = {
        "from": _remetente(),
        "to": _enderecos(msg.get("toRecipients")),
        "subject": msg.get("subject", ""),
    }
    if str(corpo.get("contentType", "HTML")).upper() == "HTML":
        dados["html"] = corpo.get("content", "")
    else:
        dados["text"] = corpo.get("content", "")
    cc = _enderecos(msg.get("ccRecipients"))
    bcc = _enderecos(msg.get("bccRecipients"))
    reply = _enderecos(msg.get("replyTo"))
    if cc: dados["cc"] = cc
    if bcc: dados["bcc"] = bcc
    if reply: dados["reply_to"] = reply

    anexos = []
    for a in msg.get("attachments", []) or []:
        item = {"filename": a.get("name", "anexo"), "content": a.get("contentBytes", "")}
        if a.get("contentId"):
            item["content_id"] = a["contentId"]  # imagens inline (cid:)
        if a.get("contentType"):
            item["content_type"] = a["contentType"]
        anexos.append(item)
    if anexos:
        dados["attachments"] = anexos

    if not dados["from"]:
        return _Resposta(400, "EMAIL_REMETENTE não configurado.")
    if not dados["to"]:
        return _Resposta(400, "Destinatário vazio.")

    try:
        r = requests.post(
            RESEND_URL,
            headers={"Authorization": f"Bearer {os.getenv('RESEND_API_KEY', '').strip()}", "Content-Type": "application/json"},
            json=dados,
            timeout=30,
        )
    except Exception as e:
        return _Resposta(503, f"Falha de rede ao enviar e-mail: {e}")

    if r.status_code in (200, 201, 202):
        # O Graph devolvia 202 Accepted; o código legado verifica esse código.
        return _Resposta(202, r.text, r.json() if r.text else {})
    return _Resposta(r.status_code, f"Resend recusou o envio: {r.text}")


def post_email(url, json=None, headers=None, **kwargs):
    """Substitui requests.post nas chamadas de envio de e-mail."""
    if usando_resend() and "graph.microsoft.com" in str(url) and "sendMail" in str(url):
        return enviar_mensagem_graph(json)
    return requests.post(url, json=json, headers=headers, **kwargs)
