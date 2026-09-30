"""Lembretes automáticos para quem não respondeu."""
from conftest import sql


def _conta_do_admin():
    return sql("SELECT conta_id FROM dbo.nps_usuarios WHERE email = 'admin@rakiti.test'")[0]["conta_id"]


def _configurar(qtd=2, ativos="true"):
    conta = _conta_do_admin()
    for chave, valor in (("envios_ativos", ativos), ("lembrete_qtd_maxima", str(qtd)),
                         ("lembrete_dias_1", "3"), ("lembrete_dias_2", "7")):
        sql("""INSERT INTO dbo.nps_configuracoes (conta_id, chave, valor) VALUES (:c, :k, :v)
               ON CONFLICT (conta_id, chave) DO UPDATE SET valor = EXCLUDED.valor""", {"c": conta, "k": chave, "v": valor})
    return conta


def _rodar(conta):
    from database import usando_conta
    from services.lembretes_svc import processar_lembretes
    with usando_conta(conta):
        return processar_lembretes()


def test_lembrete_segue_prazos_e_para_quando_responde(client, admin, emails):
    conta = _configurar()
    envio = client.post("/api/csat/enviar", headers=admin, json={"email": "lembrar@cliente.test", "nome": "Lia", "assunto": "a entrega 77"}).json()
    assert envio["status"] == "Enviado"
    sql("UPDATE dbo.nps_disparos SET data_envio_inicial = CURRENT_TIMESTAMP - INTERVAL '1 day' WHERE token = :t", {"t": envio["token"]})
    emails.clear()

    assert _rodar(conta) == 0                       # ainda não chegou o 1º prazo (3 dias)
    sql("UPDATE dbo.nps_disparos SET data_envio_inicial = CURRENT_TIMESTAMP - INTERVAL '4 days' WHERE token = :t", {"t": envio["token"]})
    assert _rodar(conta) == 1                       # 1º lembrete
    assert emails and emails[-1]["json"]["subject"].startswith("Lembrete")
    assert envio["token"] in emails[-1]["json"]["htmlbody"]   # mesmo link do convite
    assert _rodar(conta) == 0                       # não repete no mesmo prazo

    sql("UPDATE dbo.nps_disparos SET data_envio_inicial = CURRENT_TIMESTAMP - INTERVAL '8 days' WHERE token = :t", {"t": envio["token"]})
    assert _rodar(conta) == 1                       # 2º lembrete
    sql("UPDATE dbo.nps_disparos SET data_envio_inicial = CURRENT_TIMESTAMP - INTERVAL '20 days' WHERE token = :t", {"t": envio["token"]})
    assert _rodar(conta) == 0                       # máximo de 2 atingido
    assert sql("SELECT lembretes_enviados FROM dbo.nps_disparos WHERE token = :t", {"t": envio["token"]})[0]["lembretes_enviados"] == 2


def test_quem_respondeu_nao_recebe(client, admin, emails):
    conta = _configurar()
    envio = client.post("/api/csat/enviar", headers=admin, json={"email": "respondeu@cliente.test", "assunto": "a entrega 88"}).json()
    form = client.get(f"/api/pesquisa/{envio['token']}").json()["formulario"]
    assert client.post(f"/api/pesquisa/{envio['token']}", json={"respostas": {form["principal_id"]: 5}}).status_code == 200
    sql("UPDATE dbo.nps_disparos SET data_envio_inicial = CURRENT_TIMESTAMP - INTERVAL '4 days' WHERE token = :t", {"t": envio["token"]})
    assert _rodar(conta) == 0


def test_convite_mais_novo_substitui_o_antigo(client, admin, emails):
    conta = _configurar()
    velho = client.post("/api/csat/enviar", headers=admin, json={"email": "repetido@cliente.test"}).json()
    client.post("/api/csat/enviar", headers=admin, json={"email": "repetido@cliente.test"})
    sql("UPDATE dbo.nps_disparos SET data_envio_inicial = CURRENT_TIMESTAMP - INTERVAL '4 days' WHERE email = 'repetido@cliente.test'")
    emails.clear()
    assert _rodar(conta) == 1   # só o convite mais novo recebe lembrete
    assert velho["token"] not in emails[-1]["json"]["htmlbody"]


def test_desligado_nao_envia(client, admin, emails):
    conta = _configurar(qtd=0)
    client.post("/api/csat/enviar", headers=admin, json={"email": "desligado@cliente.test"})
    sql("UPDATE dbo.nps_disparos SET data_envio_inicial = CURRENT_TIMESTAMP - INTERVAL '4 days' WHERE email = 'desligado@cliente.test'")
    assert _rodar(conta) == 0
    _configurar()  # volta ao normal para os outros testes
