"""Construtor de formulários: validação, lógica por nota, link público e resultados."""
from conftest import sql


def _novo(client, admin, modelo="nps_padrao"):
    fid = client.post("/api/formularios", headers=admin, json={"modelo": modelo}).json()["id"]
    return client.get(f"/api/formularios/{fid}", headers=admin).json()


def test_valida_escolha_com_uma_opcao(client, admin):
    f = _novo(client, admin)
    f["perguntas"].append({"tipo": "escolha_unica", "titulo": "Motivo?", "opcoes": ["só uma"]})
    r = client.put(f"/api/formularios/{f['id']}", headers=admin, json={**f, "perguntas": f["perguntas"]})
    assert r.status_code == 400 and "2 opções" in r.json()["detail"]


def test_link_publico_logica_e_resultados(client, admin):
    f = _novo(client, admin)
    principal = f["perguntas"][0]["id"]
    detrator = next(p["id"] for p in f["perguntas"] if (p.get("condicao") or {}).get("valor") == "detrator")
    f["perguntas"][[p["id"] for p in f["perguntas"]].index(detrator)]["obrigatoria"] = True
    salvo = client.put(f"/api/formularios/{f['id']}", headers=admin,
                       json={"nome": f["nome"], "perguntas": f["perguntas"], "tema": f["tema"], "publico": True}).json()
    cod = salvo["codigo"]

    # detrator sem responder a pergunta obrigatória que só aparece para detratores
    r = client.post(f"/api/pesquisa/f/{cod}", json={"respostas": {principal: 3}})
    assert r.status_code == 400
    assert client.post(f"/api/pesquisa/f/{cod}", json={"respostas": {principal: 3, detrator: "Demorou"}, "referencia": "loja-1"}).status_code == 200
    # promotor: a pergunta de detrator é ignorada
    assert client.post(f"/api/pesquisa/f/{cod}", json={"respostas": {principal: 10, detrator: "ignorar"}}).status_code == 200

    res = client.get(f"/api/formularios/{f['id']}/resultados", headers=admin).json()
    assert res["total"] == 2
    nps = next(q for q in res["perguntas"] if q["id"] == principal)
    assert nps["nps"] == 0 and nps["grupos"] == {"promotor": 1, "neutro": 0, "detrator": 1}
    registros = sorted(res["registros"], key=lambda x: x["nota"])
    assert registros[0]["respostas"][detrator] == "Demorou" and registros[0]["referencia"] == "loja-1"
    assert detrator not in registros[1]["respostas"]

    # nota baixa vira plano de ação
    assert sql("SELECT 1 FROM dbo.nps_acoes WHERE titulo LIKE '[Detrator NPS 3]%'")


def test_formulario_padrao_nao_pode_perder_a_nota(client, admin):
    padrao = next(f for f in client.get("/api/formularios", headers=admin).json() if f["padrao_nps"])
    sem_nps = [p for p in padrao["perguntas"] if p["tipo"] != "nps"]
    r = client.put(f"/api/formularios/{padrao['id']}", headers=admin,
                   json={"nome": padrao["nome"], "perguntas": sem_nps, "tema": padrao["tema"]})
    assert r.status_code == 400


def test_excluir_com_respostas_arquiva(client, admin):
    f = _novo(client, admin, "cadastro_evento")
    client.put(f"/api/formularios/{f['id']}", headers=admin, json={"nome": f["nome"], "perguntas": f["perguntas"], "tema": f["tema"], "publico": True})
    cod = client.get(f"/api/formularios/{f['id']}", headers=admin).json()["codigo"]
    assert client.post(f"/api/pesquisa/f/{cod}", json={"respostas": {f["perguntas"][0]["id"]: 5}}).status_code == 200
    assert client.delete(f"/api/formularios/{f['id']}", headers=admin).json()["status"] == "arquivado"
    assert client.get(f"/api/pesquisa/f/{cod}").status_code == 404
