"""
Lembretes automáticos para quem recebeu a pesquisa e ainda não respondeu.

Regras (por conta, em Configurações > Pesquisa):
  lembrete_qtd_maxima   quantos lembretes no máximo (0 desliga)
  lembrete_dias_1..3    dias depois do envio original para mandar o 1º, 2º e 3º lembrete
  email_template_lembrete_1..3   HTML próprio (opcional); aceita {nome}, {empresa}, {survey_url}, {botoes_nota}

O lembrete usa o MESMO link do convite (o token continua valendo até a resposta),
então a resposta cai no convite original. Só entra quem:
  - recebeu o convite (status Enviado) e não respondeu;
  - não recebeu um convite mais novo do mesmo tipo depois deste;
  - ainda não atingiu o número máximo de lembretes.
"""
import html as _html
from datetime import datetime, timedelta, timezone
from sqlalchemy import text

from database import get_engine

LOTE_MAXIMO = 200


def _regras(conn):
    cfg = {r[0]: r[1] for r in conn.execute(text(
        "SELECT chave, valor FROM dbo.nps_configuracoes WHERE chave LIKE 'lembrete_%' OR chave LIKE 'email_template_lembrete_%' "
        "OR chave IN ('envios_ativos')"))}

    def num(chave, padrao):
        try:
            return max(0, int(str(cfg.get(chave) or padrao).strip()))
        except ValueError:
            return padrao
    qtd = min(num("lembrete_qtd_maxima", 2), 3)
    dias = [num(f"lembrete_dias_{i}", p) for i, p in ((1, 3), (2, 7), (3, 15))]
    # garante ordem crescente (ex.: 3, 7, 15)
    for i in range(1, 3):
        if dias[i] <= dias[i - 1]:
            dias[i] = dias[i - 1] + 1
    return {
        "ativo": str(cfg.get("envios_ativos") or "").lower() in ("true", "1") and qtd > 0,
        "qtd": qtd,
        "dias": dias,
        "templates": [cfg.get(f"email_template_lembrete_{i}") or "" for i in (1, 2, 3)],
    }


def pendentes(conn, regras, agora=None):
    """Convites que devem receber lembrete agora."""
    agora = agora or datetime.now(timezone.utc).replace(tzinfo=None)
    linhas = conn.execute(text("""
        SELECT d.id, d.email, d.nome, d.cliente_id, d.empresa_id, d.survey_url, d.token, d.tipo_pesquisa,
               d.formulario_id, d.assunto_pesquisa, d.referencia, d.lembretes_enviados,
               COALESCE(d.data_envio_inicial, d.created_at) AS enviado_em,
               COALESCE(emp.nome::text, c.empresa) AS empresa_cliente
        FROM dbo.nps_disparos d
        LEFT JOIN dbo.nps_clientes c ON c.cliente_id = d.cliente_id
        LEFT JOIN dbo.nps_empresas emp ON emp.id = COALESCE(d.empresa_id, c.empresa_id)
        WHERE d.status = 'Enviado'
          AND d.respondido_em IS NULL
          AND d.email IS NOT NULL AND d.email <> ''
          AND COALESCE(d.survey_url, '') <> ''
          AND COALESCE(d.lembretes_enviados, 0) < :qtd
          AND COALESCE(d.data_envio_inicial, d.created_at) >= :limite
          AND (c.ativo IS NULL OR c.ativo = 1)
          -- existe um convite mais novo do mesmo tipo para o mesmo e-mail? então este ficou velho
          AND NOT EXISTS (
              SELECT 1 FROM dbo.nps_disparos n
              WHERE n.email = d.email AND n.id > d.id
                AND COALESCE(n.tipo_pesquisa, 'nps') = COALESCE(d.tipo_pesquisa, 'nps')
                AND n.status IN ('Enviado', 'Respondido'))
          -- formulário externo (sem token): considera respondido se chegou resposta depois do envio
          AND (d.token IS NOT NULL OR NOT EXISTS (
              SELECT 1 FROM dbo.nps_respostas r
              WHERE r.cliente_id = d.cliente_id AND r.created_at >= COALESCE(d.data_envio_inicial, d.created_at)))
        ORDER BY d.id
        LIMIT :lote
    """), {"qtd": regras["qtd"], "lote": LOTE_MAXIMO,
           "limite": agora - timedelta(days=max(regras["dias"][:regras["qtd"]] or [0]) + 7)}).mappings().all()

    saida = []
    for d in linhas:
        n = int(d["lembretes_enviados"] or 0)            # quantos já foram
        dias_necessarios = regras["dias"][n]             # prazo do próximo
        if d["enviado_em"] and agora >= d["enviado_em"] + timedelta(days=dias_necessarios):
            saida.append({**dict(d), "numero": n + 1})
    return saida


def _html_lembrete(d, regras, remetente):
    from services import formularios_svc as fs
    from services.pesquisa_svc import html_convite_formulario, html_convite_nps, botoes_nps_html
    nome = (d["nome"] or "").split(" ")[0] or "cliente"
    url = d["survey_url"]
    template = regras["templates"][d["numero"] - 1] or regras["templates"][0]

    render = None
    if d["token"]:
        with get_engine().connect() as conn:
            fid = d["formulario_id"] or fs.id_padrao(conn, "csat" if d["tipo_pesquisa"] == "csat" else "nps")
            form = fs.obter(conn, fid) if fid else None
        if form:
            render = fs.renderizar(form, {"empresa": remetente, "nome": nome,
                                          "assunto": d["assunto_pesquisa"] or "o nosso atendimento",
                                          "referencia": d["referencia"] or ""})

    if template and "{survey_url}" in template:
        botoes = botoes_nps_html(url) if render and render.get("principal_tipo") == "nps" else ""
        return (template.replace("{nome}", nome).replace("{empresa}", d["empresa_cliente"] or remetente)
                .replace("{botoes_nota}", botoes).replace("{survey_url}", url))

    if render:
        corpo = html_convite_formulario(render, nome, url, remetente)
    else:
        corpo = html_convite_nps(nome, url, f"De 0 a 10, quanto você recomendaria a {remetente} a um amigo ou colega?",
                                 remetente, com_botoes=False)
    aviso = ('<p style="font-size:14px;color:#475569;">Passando para lembrar: a sua opinião ainda não chegou até nós. '
             'Leva menos de 1 minuto.</p>')
    return corpo.replace("</p>", "</p>" + aviso, 1)  # logo depois do "Olá, fulano!"


def processar_lembretes(agora=None):
    """Envia os lembretes devidos da conta ativa. Retorna quantos foram enviados."""
    from services.mail_provider import enviar_mensagem_graph, envio_configurado
    from services.pesquisa_svc import nome_da_conta
    from services.planos_svc import pode_enviar
    if not envio_configurado() or not pode_enviar()[0]:
        return 0
    engine = get_engine()
    with engine.connect() as conn:
        regras = _regras(conn)
        if not regras["ativo"]:
            return 0
        lista = pendentes(conn, regras, agora)
    if not lista:
        return 0

    remetente = nome_da_conta()
    enviados = 0
    for d in lista:
        if d["tipo_pesquisa"] == "csat":
            assunto = f"Lembrete: como foi {d['assunto_pesquisa'] or 'o nosso atendimento'}?"
        else:
            assunto = f"Lembrete: {remetente} quer saber a sua opinião"
        try:
            r = enviar_mensagem_graph({"message": {
                "subject": assunto,
                "body": {"contentType": "HTML", "content": _html_lembrete(d, regras, remetente)},
                "toRecipients": [{"emailAddress": {"address": d["email"]}}]}})
            ok = r.status_code in (200, 202)
        except Exception as e:  # um e-mail com problema não trava os outros
            ok, r = False, type("R", (), {"text": str(e)})()
        with engine.begin() as conn:
            if ok:
                conn.execute(text("""
                    UPDATE dbo.nps_disparos
                    SET lembretes_enviados = COALESCE(lembretes_enviados, 0) + 1,
                        data_ultimo_lembrete = CURRENT_TIMESTAMP, updated_at = CURRENT_TIMESTAMP
                    WHERE id = :id AND respondido_em IS NULL
                """), {"id": d["id"]})
                enviados += 1
            else:
                conn.execute(text("UPDATE dbo.nps_disparos SET erro_msg = :e, updated_at = CURRENT_TIMESTAMP WHERE id = :id"),
                             {"e": f"Lembrete {d['numero']} não enviado: {str(r.text)[:400]}", "id": d["id"]})
    if enviados:
        try:
            from main import registrar_log
            registrar_log(acao="LEMBRETES", mensagem=f"{enviados} lembrete(s) de pesquisa enviados.", nivel="SUCCESS")
        except Exception:
            pass
    return enviados
