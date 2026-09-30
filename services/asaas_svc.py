"""
Cobrança recorrente pelo Asaas (Pix, boleto ou cartão; o cliente escolhe na fatura).

Variáveis de ambiente:
  ASAAS_API_KEY        chave da API ($aact_prod_... ou $aact_hmlg_... no sandbox)
  ASAAS_AMBIENTE       sandbox (padrão) | producao
  ASAAS_WEBHOOK_TOKEN  token configurado no webhook do Asaas (enviado no cabeçalho asaas-access-token)

Fluxo:
  1. Empresa escolhe o plano em Assinatura -> criamos o cliente e a assinatura mensal no Asaas.
  2. Devolvemos o link da primeira fatura (o cliente paga por Pix, boleto ou cartão).
  3. O Asaas avisa pelo webhook (/api/webhook/asaas) -> a conta fica "ativa" ou "atrasada".
"""
import os
import re
from datetime import date
import requests

URLS = {"sandbox": "https://api-sandbox.asaas.com/v3", "producao": "https://api.asaas.com/v3"}


class ErroAsaas(Exception):
    pass


def configurado() -> bool:
    return bool(os.getenv("ASAAS_API_KEY", "").strip())


def _base():
    if os.getenv("ASAAS_API_URL", "").strip():   # só para testes/homologação apontando para outro servidor
        return os.getenv("ASAAS_API_URL").strip().rstrip("/")
    return URLS.get(os.getenv("ASAAS_AMBIENTE", "sandbox").strip().lower(), URLS["sandbox"])


def _chamar(metodo, caminho, dados=None, params=None):
    if not configurado():
        raise ErroAsaas("Cobrança ainda não configurada na plataforma (ASAAS_API_KEY).")
    try:
        r = requests.request(metodo, f"{_base()}{caminho}", json=dados, params=params, timeout=30, headers={
            "access_token": os.getenv("ASAAS_API_KEY", "").strip(),
            "User-Agent": "Rakiti/1.0",
            "Content-Type": "application/json",
        })
    except requests.RequestException as e:
        raise ErroAsaas(f"Não foi possível falar com o Asaas: {e}")
    if r.status_code >= 400:
        try:
            erros = r.json().get("errors") or []
            msg = "; ".join(e.get("description", "") for e in erros) or r.text
        except ValueError:
            msg = r.text
        raise ErroAsaas(f"Asaas: {msg}")
    return r.json() if r.text else {}


def so_digitos(valor):
    return re.sub(r"\D", "", valor or "")


def documento_valido(doc: str) -> bool:
    """Valida CPF (11) ou CNPJ (14) pelos dígitos verificadores."""
    d = so_digitos(doc)
    if len(d) == 11 and len(set(d)) > 1:
        for n in (9, 10):
            soma = sum(int(d[i]) * (n + 1 - i) for i in range(n))
            if (soma * 10 % 11) % 10 != int(d[n]):
                return False
        return True
    if len(d) == 14 and len(set(d)) > 1:
        pesos = [5, 4, 3, 2, 9, 8, 7, 6, 5, 4, 3, 2]
        for n in (12, 13):
            p = pesos if n == 12 else [6] + pesos
            soma = sum(int(d[i]) * p[i] for i in range(n))
            dv = 0 if soma % 11 < 2 else 11 - soma % 11
            if dv != int(d[n]):
                return False
        return True
    return False


def criar_cliente(nome, cpf_cnpj, email, telefone, conta_id):
    return _chamar("POST", "/customers", {
        "name": nome, "cpfCnpj": so_digitos(cpf_cnpj), "email": email,
        "mobilePhone": so_digitos(telefone) or None, "externalReference": f"conta:{conta_id}",
        "notificationDisabled": False,
    })["id"]


def atualizar_cliente(customer_id, nome, cpf_cnpj, email, telefone):
    _chamar("POST", f"/customers/{customer_id}", {
        "name": nome, "cpfCnpj": so_digitos(cpf_cnpj), "email": email, "mobilePhone": so_digitos(telefone) or None})


def criar_assinatura(customer_id, valor, descricao, conta_id):
    return _chamar("POST", "/subscriptions", {
        "customer": customer_id, "billingType": "UNDEFINED", "value": valor,
        "nextDueDate": date.today().isoformat(), "cycle": "MONTHLY",
        "description": descricao, "externalReference": f"conta:{conta_id}",
    })["id"]


def alterar_valor(subscription_id, valor, descricao):
    _chamar("POST", f"/subscriptions/{subscription_id}", {
        "value": valor, "description": descricao, "updatePendingPayments": True})


def cancelar_assinatura(subscription_id):
    _chamar("DELETE", f"/subscriptions/{subscription_id}")


def cobrancas_da_assinatura(subscription_id):
    return _chamar("GET", f"/subscriptions/{subscription_id}/payments", params={"limit": 20}).get("data", [])


def fatura_em_aberto(subscription_id):
    """Cobrança pendente mais antiga da assinatura (para o botão Pagar), ou None."""
    abertas = [c for c in cobrancas_da_assinatura(subscription_id) if c.get("status") in ("PENDING", "OVERDUE")]
    abertas.sort(key=lambda c: c.get("dueDate") or "")
    return abertas[0] if abertas else None
