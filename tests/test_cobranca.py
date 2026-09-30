"""Cadastro próprio, teste grátis, limite do plano e cobrança pelo Asaas (Asaas simulado)."""
import re
import pytest
from conftest import sql, login

EMAIL, SENHA = "dona@padaria.test", "Senha@Forte123"


@pytest.fixture(scope="module")
def conta_cadastro(client, emails):
    r = client.post("/api/cadastro-empresa", json={"empresa": "Padaria Teste", "nome": "Dona Rosa", "email": EMAIL,
                                                   "senha": SENHA, "aceite_termos": True})
    assert r.status_code == 200, r.text
    conta = sql("SELECT * FROM dbo.contas_view_teste" if False else
                "SELECT c.* FROM dbo.nps_contas c JOIN dbo.nps_usuarios u ON u.conta_id = c.id WHERE u.email = :e", {"e": EMAIL})[0]
    return dict(conta)


def test_planos_publicos(client):
    p = client.get("/api/planos").json()
    assert set(p["planos"]) == {"essencial", "profissional", "empresa"} and p["dias_teste"] == 14


def test_cadastro_cria_teste_e_exige_confirmar_email(client, conta_cadastro, emails):
    assert conta_cadastro["status_assinatura"] == "teste" and conta_cadastro["plano"] == "profissional"
    assert conta_cadastro["limite_clientes"] == 1500
    # formulários prontos também foram criados
    assert len(sql("SELECT 1 FROM dbo.nps_formularios WHERE conta_id = :c", {"c": conta_cadastro["id"]})) == 2
    assert client.post("/api/login", json={"email": EMAIL, "password": SENHA}).status_code == 403  # e-mail não confirmado

    confirmacao = [e for e in emails if "Confirme" in (e["json"] or {}).get("subject", "")]
    assert confirmacao, "e-mail de confirmação não foi enviado"
    link = re.search(r'href="([^"]*verificar-email\?token=[^"]+)"', confirmacao[-1]["json"]["htmlbody"]).group(1)
    caminho = link.split("://", 1)[1].split("/", 1)[1]
    r = client.get("/" + caminho, follow_redirects=False)
    assert r.status_code in (302, 307) and "status=confirmado" in r.headers["location"]
    h = login(client, EMAIL, SENHA)   # admin continua ativo depois de confirmar
    a = client.get("/api/assinatura", headers=h).json()
    assert a["status"] == "teste" and a["dias_restantes"] in (13, 14) and a["pode_enviar"]


def test_cadastro_recusa_email_repetido_e_sem_aceite(client, conta_cadastro):
    assert client.post("/api/cadastro-empresa", json={"empresa": "X", "nome": "Y", "email": EMAIL, "senha": SENHA, "aceite_termos": True}).status_code == 400
    assert client.post("/api/cadastro-empresa", json={"empresa": "X", "nome": "Y", "email": "novo@x.test", "senha": SENHA}).status_code == 400


def test_limite_de_clientes_do_plano(client, conta_cadastro):
    h = login(client, EMAIL, SENHA)
    sql("UPDATE dbo.nps_contas SET limite_clientes = 1 WHERE id = :c", {"c": conta_cadastro["id"]})
    assert client.post("/api/csat/enviar", headers=h, json={"email": "c1@cliente.test", "enviar_email": False}).status_code == 200
    r = client.post("/api/csat/enviar", headers=h, json={"email": "c2@cliente.test", "enviar_email": False})
    assert r.status_code == 402 and "Limite do plano" in r.json()["detail"]
    sql("UPDATE dbo.nps_contas SET limite_clientes = 1500 WHERE id = :c", {"c": conta_cadastro["id"]})


def test_teste_expirado_pausa_envios(client, conta_cadastro):
    h = login(client, EMAIL, SENHA)
    sql("UPDATE dbo.nps_contas SET teste_ate = CURRENT_TIMESTAMP - INTERVAL '1 day' WHERE id = :c", {"c": conta_cadastro["id"]})
    a = client.get("/api/assinatura", headers=h).json()
    assert a["status"] == "teste_expirado" and not a["pode_enviar"]
    r = client.post("/api/csat/enviar", headers=h, json={"email": "c1@cliente.test"})
    assert r.status_code == 400 and "teste grátis terminou" in r.json()["detail"]


def test_assinar_e_webhook(client, conta_cadastro, monkeypatch):
    import services.asaas_svc as asaas
    chamadas = []

    def falso(metodo, caminho, dados=None, params=None):
        chamadas.append((metodo, caminho, dados))
        if caminho == "/customers":
            return {"id": "cus_1"}
        if caminho == "/subscriptions":
            return {"id": "sub_1"}
        if caminho.endswith("/payments"):
            return {"data": [{"id": "pay_1", "status": "PENDING", "dueDate": "2026-10-01", "invoiceUrl": "https://asaas.test/i/1"}]}
        return {}
    monkeypatch.setattr(asaas, "_chamar", falso)
    h = login(client, EMAIL, SENHA)

    r = client.post("/api/assinatura", headers=h, json={"plano": "essencial", "cpf_cnpj": "11.222.333/0001-80", "email_cobranca": "fin@padaria.test"})
    assert r.status_code == 400  # CNPJ com dígito errado
    r = client.post("/api/assinatura", headers=h, json={"plano": "essencial", "cpf_cnpj": "11.222.333/0001-81", "email_cobranca": "fin@padaria.test"})
    assert r.status_code == 200 and r.json()["link_pagamento"] == "https://asaas.test/i/1"
    assinatura = next(d for m, c, d in chamadas if c == "/subscriptions")
    assert assinatura["value"] == 149.0 and assinatura["cycle"] == "MONTHLY" and assinatura["billingType"] == "UNDEFINED"
    conta = sql("SELECT * FROM dbo.nps_contas WHERE id = :c", {"c": conta_cadastro["id"]})[0]
    assert conta["asaas_subscription_id"] == "sub_1" and conta["plano"] == "essencial" and conta["limite_clientes"] == 300

    pagamento = {"id": "pay_1", "subscription": "sub_1", "value": 149.0, "status": "RECEIVED", "billingType": "PIX",
                 "dueDate": "2026-10-01", "paymentDate": "2026-09-30", "invoiceUrl": "https://asaas.test/i/1"}
    assert client.post("/api/webhook/asaas", json={"event": "PAYMENT_RECEIVED", "payment": pagamento},
                       headers={"asaas-access-token": "errado"}).status_code == 401
    assert client.post("/api/webhook/asaas", json={"event": "PAYMENT_RECEIVED", "payment": pagamento},
                       headers={"asaas-access-token": "webhook-teste"}).status_code == 200
    a = client.get("/api/assinatura", headers=h).json()
    assert a["status"] == "ativa" and a["pode_enviar"] and a["cobrancas"][0]["status"] == "RECEIVED"

    # atraso: continua enviando durante a tolerância, pausa depois
    client.post("/api/webhook/asaas", headers={"asaas-access-token": "webhook-teste"},
                json={"event": "PAYMENT_OVERDUE", "payment": {**pagamento, "id": "pay_2", "status": "OVERDUE", "dueDate": "2026-11-01"}})
    a = client.get("/api/assinatura", headers=h).json()
    assert a["status"] == "atrasada" and a["pode_enviar"] and a["link_pagamento"] == "https://asaas.test/i/1"
    sql("UPDATE dbo.nps_contas SET atrasada_desde = CURRENT_TIMESTAMP - INTERVAL '8 days' WHERE id = :c", {"c": conta_cadastro["id"]})
    assert not client.get("/api/assinatura", headers=h).json()["pode_enviar"]

    # cancelamento
    assert client.post("/api/assinatura/cancelar", headers=h).status_code == 200
    assert client.get("/api/assinatura", headers=h).json()["status"] == "cancelada"
    assert ("DELETE", "/subscriptions/sub_1", None) in chamadas


def test_webhook_de_outra_assinatura_e_ignorado(client):
    r = client.post("/api/webhook/asaas", headers={"asaas-access-token": "webhook-teste"},
                    json={"event": "PAYMENT_RECEIVED", "payment": {"id": "pay_x", "subscription": "sub_desconhecida"}})
    assert r.status_code == 200 and r.json()["status"] == "ignorado"


def test_documentos():
    from services.asaas_svc import documento_valido
    assert documento_valido("529.982.247-25") and documento_valido("11222333000181")
    assert not documento_valido("111.111.111-11") and not documento_valido("123")


def test_editar_contato_ativo_nao_esbarra_no_limite(client, conta_cadastro):
    c = conta_cadastro["id"]
    sql("UPDATE dbo.nps_contas SET limite_clientes = 1 WHERE id = :c", {"c": c})
    try:
        sql("UPDATE dbo.nps_clientes SET nome = 'Renomeado', ativo = 1 WHERE conta_id = :c", {"c": c})  # não pode dar erro
        assert sql("SELECT 1 FROM dbo.nps_clientes WHERE conta_id = :c AND nome = 'Renomeado'", {"c": c})
    finally:
        sql("UPDATE dbo.nps_contas SET limite_clientes = 1500 WHERE id = :c", {"c": c})
