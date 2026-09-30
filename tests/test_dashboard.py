"""Visão geral: os números dos cartões e do gráfico precisam bater."""


def test_cartoes_e_grafico_contam_as_mesmas_respostas(client, admin):
    q = "?apenas_ativos=true"
    kpis = client.get(f"/api/dashboard/kpis{q}", headers=admin).json()["kpis"]
    trend = client.get(f"/api/dashboard/trend{q}", headers=admin).json()
    assert kpis["total_respostas"] == sum(trend["totais"])
    assert kpis["promotores"] + kpis["neutros"] + kpis["detratores"] == kpis["total_respostas"]


def test_variacao_sem_periodo_anterior_e_nula(client, admin):
    k = client.get("/api/dashboard/kpis?apenas_ativos=true&data_inicio=2030-01-01&data_fim=2030-01-31", headers=admin).json()["kpis"]
    assert k["total_respostas"] == 0 and k["variacao_nps"] is None


def test_temas_para_pme(client, admin):
    temas = {t["tema"] for t in client.get("/api/dashboard/kpis?apenas_ativos=false", headers=admin).json()["kpis"]["topicos_criticos"]}
    assert not temas & {"Bugs", "UX/UI", "Integração"}
