"""Uma empresa cliente nunca pode ver ou alterar dados de outra."""
from conftest import sql


def test_rotas_exigem_login(client):
    assert client.get("/api/formularios").status_code == 401
    assert client.get("/api/dashboard/kpis").status_code == 401


def test_formularios_padrao_criados_para_cada_conta(client, admin, outra_conta):
    a = {f["nome"] for f in client.get("/api/formularios", headers=admin).json()}
    b = client.get("/api/formularios", headers=outra_conta).json()
    assert {"Pesquisa NPS", "Satisfação pós-entrega"} <= a
    assert {f["nome"] for f in b} >= {"Pesquisa NPS", "Satisfação pós-entrega"}


def test_conta_nao_ve_nem_altera_formulario_da_outra(client, admin, outra_conta):
    form_a = client.get("/api/formularios", headers=admin).json()[0]
    ids_b = {f["id"] for f in client.get("/api/formularios", headers=outra_conta).json()}
    assert form_a["id"] not in ids_b
    assert client.get(f"/api/formularios/{form_a['id']}", headers=outra_conta).status_code == 404
    r = client.put(f"/api/formularios/{form_a['id']}", headers=outra_conta,
                   json={"nome": "invadido", "perguntas": [], "tema": {}})
    assert r.status_code == 404
    assert client.get(f"/api/formularios/{form_a['id']}", headers=admin).json()["nome"] != "invadido"


def test_csat_por_api_usa_so_a_conta_da_chave(client, admin, outra_conta, emails):
    chave_b = client.get("/api/conta", headers=outra_conta).json()["api_key"]
    form_a = client.get("/api/formularios", headers=admin).json()[0]["id"]
    r = client.post("/api/integracao/csat", headers={"X-Api-Key": chave_b},
                    json={"email": "cliente@x.test", "formulario_id": form_a})
    assert r.status_code == 400  # formulário de outra conta = não encontrado
    r = client.post("/api/integracao/csat", headers={"X-Api-Key": "chave-errada-com-mais-de-vinte"}, json={"email": "a@b.test"})
    assert r.status_code == 401


def test_rls_no_banco(client, admin, outra_conta):
    contas = [r["conta_id"] for r in sql("SELECT DISTINCT conta_id FROM dbo.nps_formularios")]
    assert len(contas) >= 2
    # consultando como a conta 1, só aparecem formulários da conta 1
    visiveis = sql("SELECT DISTINCT conta_id FROM dbo.nps_formularios", conta=contas[0])
    assert [r["conta_id"] for r in visiveis] == [contas[0]]
