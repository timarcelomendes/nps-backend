"""
Construtor de formulários da Rakiti.

Um formulário é uma lista de perguntas (JSON) + tema. Tipos de pergunta:

  nps               nota de 0 a 10
  csat              satisfação de 1 a 5 (rostos)
  estrelas          1 a 5 estrelas
  escala            escala numérica configurável (ex.: esforço 1 a 7)
  texto_curto       resposta curta (formato: texto | email | telefone | numero)
  texto_longo       comentário
  escolha_unica     uma opção
  escolha_multipla  várias opções
  sim_nao           Sim / Não
  data              data (AAAA-MM-DD)
  pagina            quebra de página (não é pergunta)

Nota principal: a primeira pergunta "nps" (vai para o painel de NPS) ou, se não houver,
a primeira "csat"/"estrelas" (vai para o painel de CSAT). As outras perguntas podem ter
"condicao" baseada nessa nota:
  {"tipo": "grupo", "valor": "detrator" | "neutro" | "promotor"}
  {"tipo": "lte" | "gte", "valor": N}
"""
import re
import json
import uuid
import secrets
from datetime import date
from sqlalchemy import text

from database import get_engine, modo_sistema, usando_conta

TIPOS_NOTA = {"nps", "csat", "estrelas", "escala"}
TIPOS_TEXTO = {"texto_curto", "texto_longo"}
TIPOS_ESCOLHA = {"escolha_unica", "escolha_multipla"}
TIPOS_VALIDOS = TIPOS_NOTA | TIPOS_TEXTO | TIPOS_ESCOLHA | {"sim_nao", "data", "pagina"}
FORMATOS = {"texto", "email", "telefone", "numero"}

MAX_PERGUNTAS = 60
MAX_OPCOES = 30
MAX_LOGO = 400_000  # caracteres do data URL (~300 KB)

TEMA_PADRAO = {
    "cor": "#f97316",
    "logo": "",
    "layout": "uma_por_vez",        # uma_por_vez | lista
    "titulo_final": "Obrigado!",
    "mensagem_final": "Sua resposta foi enviada. Ela ajuda a {empresa} a melhorar.",
    "texto_botao": "Enviar resposta",
    "mensagem_inicial": "",
}


# =====================================================================
# Modelos prontos
# =====================================================================
def _p(tipo, titulo, **extra):
    return {"id": _novo_id(), "tipo": tipo, "titulo": titulo, "descricao": "", "obrigatoria": False, **extra}


def _novo_id():
    return "p_" + uuid.uuid4().hex[:8]


def modelos():
    """Modelos de formulário (gera ids novos a cada chamada)."""
    return [
        {
            "chave": "nps_padrao", "nome": "Pesquisa NPS", "icone": "pi pi-chart-line",
            "descricao": "Recomendação de 0 a 10 com pergunta de acompanhamento conforme a nota.",
            "perguntas": [
                _p("nps", "De 0 a 10, quanto você recomendaria a {empresa} a um amigo ou colega?", obrigatoria=True),
                _p("texto_longo", "Poxa! O que podemos melhorar?", condicao={"tipo": "grupo", "valor": "detrator"}),
                _p("texto_longo", "O que faltou para você dar nota 10?", condicao={"tipo": "grupo", "valor": "neutro"}),
                _p("texto_longo", "Que bom! O que você mais gosta na {empresa}?", condicao={"tipo": "grupo", "valor": "promotor"}),
            ],
        },
        {
            "chave": "pos_entrega", "nome": "Satisfação pós-entrega", "icone": "pi pi-truck",
            "descricao": "CSAT da entrega, prazo e o que deu errado (para transportadoras e distribuidoras).",
            "perguntas": [
                _p("csat", "Como você avalia {assunto}?", obrigatoria=True),
                _p("sim_nao", "A entrega chegou no prazo combinado?"),
                _p("escolha_multipla", "O que não foi bem?", condicao={"tipo": "lte", "valor": 3},
                   opcoes=["Atraso", "Produto avariado", "Falta ou troca de itens", "Atendimento do entregador",
                           "Nota fiscal / documentos", "Outro"]),
                _p("texto_longo", "Quer deixar um comentário?"),
            ],
        },
        {
            "chave": "pos_atendimento", "nome": "Pós-atendimento", "icone": "pi pi-headphones",
            "descricao": "Satisfação, resolução e esforço (CES) depois de um atendimento ou chamado.",
            "perguntas": [
                _p("csat", "Como você avalia o atendimento que recebeu?", obrigatoria=True),
                _p("sim_nao", "Seu problema foi resolvido?"),
                _p("escala", "Foi fácil resolver o que você precisava?",
                   escala={"min": 1, "max": 7, "rotulo_min": "Muito difícil", "rotulo_max": "Muito fácil"}),
                _p("texto_longo", "O que poderíamos ter feito melhor?", condicao={"tipo": "lte", "valor": 3}),
            ],
        },
        {
            "chave": "nps_distribuidora", "nome": "NPS com motivos (distribuidora)", "icone": "pi pi-box",
            "descricao": "NPS + o que mais pesa na nota + avaliação do vendedor.",
            "perguntas": [
                _p("nps", "De 0 a 10, quanto você recomendaria a {empresa} a outro lojista?", obrigatoria=True),
                _p("escolha_multipla", "O que mais pesa na sua nota?",
                   opcoes=["Prazo de entrega", "Preço", "Variedade de produtos", "Atendimento do vendedor",
                           "Facilidade para fazer pedidos", "Condições de pagamento"]),
                {"id": _novo_id(), "tipo": "pagina"},
                _p("estrelas", "Como você avalia o seu vendedor?"),
                _p("texto_longo", "O que faria você comprar mais com a gente?"),
            ],
        },
        {
            "chave": "cadastro_evento", "nome": "Pesquisa rápida com identificação", "icone": "pi pi-id-card",
            "descricao": "Para link público ou QR Code: nota, nome, e-mail e comentário.",
            "perguntas": [
                _p("estrelas", "Como foi a sua experiência hoje?", obrigatoria=True),
                _p("texto_curto", "Seu nome", formato="texto"),
                _p("texto_curto", "Seu e-mail", formato="email",
                   descricao="Opcional. Só para responder você, se precisar."),
                _p("texto_longo", "Quer contar mais?"),
            ],
        },
        {
            "chave": "em_branco", "nome": "Em branco", "icone": "pi pi-file",
            "descricao": "Comece do zero.",
            "perguntas": [],
        },
    ]


def modelo(chave):
    return next((m for m in modelos() if m["chave"] == chave), None)


# =====================================================================
# Validação da definição do formulário (ao salvar)
# =====================================================================
def _txt(v, limite):
    return str(v or "").strip()[:limite]


def _int(v, padrao):
    try:
        return int(v)
    except (TypeError, ValueError):
        return padrao


def normalizar_perguntas(perguntas):
    if not isinstance(perguntas, list):
        raise ValueError("Lista de perguntas inválida.")
    if len(perguntas) > MAX_PERGUNTAS:
        raise ValueError(f"Máximo de {MAX_PERGUNTAS} perguntas por formulário.")
    saida, ids = [], set()
    for bruta in perguntas:
        if not isinstance(bruta, dict):
            continue
        tipo = bruta.get("tipo")
        if tipo not in TIPOS_VALIDOS:
            raise ValueError(f"Tipo de pergunta desconhecido: {tipo}")
        pid = _txt(bruta.get("id"), 40)
        if not re.fullmatch(r"[A-Za-z0-9_-]{2,40}", pid or "") or pid in ids:
            pid = _novo_id()
        ids.add(pid)
        if tipo == "pagina":
            saida.append({"id": pid, "tipo": "pagina"})
            continue
        p = {
            "id": pid, "tipo": tipo,
            "titulo": _txt(bruta.get("titulo"), 500),
            "descricao": _txt(bruta.get("descricao"), 1000),
            "obrigatoria": bool(bruta.get("obrigatoria")),
        }
        if not p["titulo"]:
            raise ValueError("Toda pergunta precisa de um título.")
        if tipo in TIPOS_ESCOLHA:
            opcoes = []
            for o in bruta.get("opcoes") or []:
                o = _txt(o, 200)
                if o and o not in opcoes:
                    opcoes.append(o)
            if len(opcoes) < 2:
                raise ValueError(f"A pergunta \"{p['titulo'][:40]}\" precisa de pelo menos 2 opções.")
            p["opcoes"] = opcoes[:MAX_OPCOES]
        if tipo == "escala":
            e = bruta.get("escala") or {}
            mn = max(0, min(_int(e.get("min"), 1), 1))
            mx = max(mn + 2, min(_int(e.get("max"), 5), 10))
            p["escala"] = {"min": mn, "max": mx, "rotulo_min": _txt(e.get("rotulo_min"), 40),
                           "rotulo_max": _txt(e.get("rotulo_max"), 40)}
        if tipo in ("nps", "csat", "estrelas"):
            e = bruta.get("escala") or {}
            p["escala"] = {"rotulo_min": _txt(e.get("rotulo_min"), 40), "rotulo_max": _txt(e.get("rotulo_max"), 40)}
        if tipo == "texto_curto":
            fmt = bruta.get("formato") or "texto"
            p["formato"] = fmt if fmt in FORMATOS else "texto"
        c = bruta.get("condicao")
        if isinstance(c, dict) and c.get("tipo") in ("grupo", "lte", "gte"):
            if c["tipo"] == "grupo" and c.get("valor") in ("detrator", "neutro", "promotor"):
                p["condicao"] = {"tipo": "grupo", "valor": c["valor"]}
            elif c["tipo"] in ("lte", "gte"):
                p["condicao"] = {"tipo": c["tipo"], "valor": max(0, min(_int(c.get("valor"), 0), 10))}
        saida.append(p)

    # a condição só faz sentido depois da nota principal
    principal = pergunta_principal(saida)
    vista_principal = False
    for p in saida:
        if principal and p["id"] == principal["id"]:
            vista_principal = True
            p.pop("condicao", None)
        elif "condicao" in p and not vista_principal:
            p.pop("condicao")
    # remove quebras de página no início, no fim e repetidas
    limpa = []
    for p in saida:
        if p["tipo"] == "pagina" and (not limpa or limpa[-1]["tipo"] == "pagina"):
            continue
        limpa.append(p)
    while limpa and limpa[-1]["tipo"] == "pagina":
        limpa.pop()
    return limpa


def normalizar_tema(tema):
    tema = tema if isinstance(tema, dict) else {}
    t = dict(TEMA_PADRAO)
    cor = _txt(tema.get("cor"), 7)
    if re.fullmatch(r"#[0-9a-fA-F]{6}", cor):
        t["cor"] = cor
    logo = str(tema.get("logo") or "").strip()
    if logo:
        if len(logo) > MAX_LOGO:
            raise ValueError("A imagem do logo é grande demais (máximo ~300 KB).")
        if not (logo.startswith("data:image/") or logo.startswith("https://")):
            raise ValueError("Logo inválido: envie uma imagem ou um link https://.")
        t["logo"] = logo
    t["layout"] = "lista" if tema.get("layout") == "lista" else "uma_por_vez"
    for chave, lim in (("titulo_final", 120), ("mensagem_final", 600), ("texto_botao", 40), ("mensagem_inicial", 600)):
        if chave in tema:
            t[chave] = _txt(tema.get(chave), lim)
    t["texto_botao"] = t["texto_botao"] or TEMA_PADRAO["texto_botao"]
    return t


# =====================================================================
# Regras da nota principal
# =====================================================================
def pergunta_principal(perguntas):
    for p in perguntas:
        if p.get("tipo") == "nps":
            return p
    for p in perguntas:
        if p.get("tipo") in ("csat", "estrelas"):
            return p
    return None


def tipo_do_formulario(perguntas):
    """'nps', 'csat' ou 'personalizado' (sem nota principal)."""
    p = pergunta_principal(perguntas)
    if not p:
        return "personalizado"
    return "nps" if p["tipo"] == "nps" else "csat"


def grupo_da_nota(tipo_principal, nota):
    if nota is None:
        return None
    if tipo_principal == "nps":
        return "detrator" if nota <= 6 else "neutro" if nota <= 8 else "promotor"
    return "detrator" if nota <= 2 else "neutro" if nota == 3 else "promotor"


def pergunta_visivel(p, principal, nota):
    c = p.get("condicao")
    if not c or not principal:
        return True
    if nota is None:
        return False
    if c["tipo"] == "grupo":
        return grupo_da_nota(principal["tipo"], nota) == c["valor"]
    if c["tipo"] == "lte":
        return nota <= c["valor"]
    return nota >= c["valor"]


def faixa(p):
    if p["tipo"] == "nps":
        return 0, 10
    if p["tipo"] in ("csat", "estrelas"):
        return 1, 5
    e = p.get("escala") or {}
    return int(e.get("min", 1)), int(e.get("max", 5))


# =====================================================================
# Validação das respostas
# =====================================================================
_EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def validar_respostas(perguntas, respostas):
    """Devolve (respostas_limpas, nota_principal). Lança ValueError com mensagem amigável."""
    if not isinstance(respostas, dict):
        raise ValueError("Respostas inválidas.")
    principal = pergunta_principal(perguntas)
    nota = None
    if principal:
        v = respostas.get(principal["id"])
        if v not in (None, ""):
            nota = _int(v, None)
            mn, mx = faixa(principal)
            if nota is None or not (mn <= nota <= mx):
                raise ValueError(f"Escolha uma nota de {mn} a {mx}.")

    limpas = {}
    for p in perguntas:
        if p["tipo"] == "pagina" or not pergunta_visivel(p, principal, nota):
            continue
        v = respostas.get(p["id"])
        vazio = v in (None, "", []) or (isinstance(v, str) and not v.strip())
        if vazio:
            if p.get("obrigatoria"):
                raise ValueError(f"Responda: {p['titulo'][:80]}")
            continue
        t = p["tipo"]
        if t in TIPOS_NOTA:
            n = _int(v, None)
            mn, mx = faixa(p)
            if n is None or not (mn <= n <= mx):
                raise ValueError(f"Nota inválida em: {p['titulo'][:60]}")
            limpas[p["id"]] = n
        elif t in TIPOS_TEXTO:
            s = str(v).strip()[: (300 if t == "texto_curto" else 4000)]
            fmt = p.get("formato", "texto")
            if t == "texto_curto" and fmt == "email" and not _EMAIL.match(s):
                raise ValueError("Informe um e-mail válido.")
            if t == "texto_curto" and fmt == "numero" and not re.fullmatch(r"-?\d+([.,]\d+)?", s):
                raise ValueError(f"Informe um número em: {p['titulo'][:60]}")
            if t == "texto_curto" and fmt == "telefone" and len(re.sub(r"\D", "", s)) < 8:
                raise ValueError("Informe um telefone válido.")
            limpas[p["id"]] = s
        elif t == "escolha_unica":
            if v not in p["opcoes"]:
                raise ValueError(f"Opção inválida em: {p['titulo'][:60]}")
            limpas[p["id"]] = v
        elif t == "escolha_multipla":
            lista = v if isinstance(v, list) else [v]
            lista = [o for o in p["opcoes"] if o in lista]
            if not lista:
                raise ValueError(f"Opção inválida em: {p['titulo'][:60]}")
            limpas[p["id"]] = lista
        elif t == "sim_nao":
            if isinstance(v, bool):
                v = "Sim" if v else "Não"
            if v not in ("Sim", "Não"):
                raise ValueError(f"Resposta inválida em: {p['titulo'][:60]}")
            limpas[p["id"]] = v
        elif t == "data":
            try:
                limpas[p["id"]] = date.fromisoformat(str(v)[:10]).isoformat()
            except ValueError:
                raise ValueError(f"Data inválida em: {p['titulo'][:60]}")
    if principal and principal.get("obrigatoria") and nota is None:
        raise ValueError(f"Responda: {principal['titulo'][:80]}")
    return limpas, nota


def texto_resumo(perguntas, respostas, pular_id=None, placeholders=None):
    """Transforma as respostas em texto legível (vai para o motivo/comentário e para a IA)."""
    linhas = []
    for p in perguntas:
        if p["tipo"] == "pagina" or p["id"] == pular_id or p["id"] not in respostas:
            continue
        v = respostas[p["id"]]
        if isinstance(v, list):
            v = ", ".join(v)
        titulo = aplicar_placeholders(p["titulo"], placeholders or {})
        linhas.append(f"{titulo}: {v}")
    return "\n".join(linhas)


def aplicar_placeholders(texto, valores):
    texto = texto or ""
    if not valores.get("nome"):  # "Olá, {nome}!" sem nome vira "Olá!"
        texto = texto.replace(", {nome}", "").replace(" {nome}", "")
    for chave in ("empresa", "nome", "assunto", "referencia"):
        texto = texto.replace("{" + chave + "}", str(valores.get(chave) or ""))
    return texto


def renderizar(form, valores):
    """Versão do formulário para o público (textos com placeholders resolvidos)."""
    perguntas = []
    for p in form["perguntas"]:
        q = dict(p)
        if q["tipo"] != "pagina":
            q["titulo"] = aplicar_placeholders(q.get("titulo"), valores)
            q["descricao"] = aplicar_placeholders(q.get("descricao"), valores)
        perguntas.append(q)
    tema = dict(TEMA_PADRAO, **(form.get("tema") or {}))
    for chave in ("titulo_final", "mensagem_final", "mensagem_inicial"):
        tema[chave] = aplicar_placeholders(tema.get(chave), valores)
    principal = pergunta_principal(perguntas)
    return {
        "id": form.get("id"), "nome": form.get("nome"), "perguntas": perguntas, "tema": tema,
        "principal_id": principal["id"] if principal else None,
        "principal_tipo": principal["tipo"] if principal else None,
    }


# =====================================================================
# Banco
# =====================================================================
def _linha_para_form(r):
    if not r:
        return None
    f = dict(r)
    for k in ("perguntas", "tema"):
        if isinstance(f.get(k), str):
            f[k] = json.loads(f[k])
    f["tipo"] = tipo_do_formulario(f["perguntas"])
    return f


def obter(conn, form_id):
    r = conn.execute(text("SELECT * FROM dbo.nps_formularios WHERE id = :id"), {"id": form_id}).mappings().first()
    return _linha_para_form(r)


def obter_por_codigo_publico(codigo):
    """Formulário do link público (sem login). Retorna (form, conta_id) ou (None, None)."""
    if not codigo or not re.fullmatch(r"[A-Za-z0-9_-]{6,24}", codigo):
        return None, None
    with modo_sistema():
        with get_engine().connect() as conn:
            r = conn.execute(text("""SELECT * FROM dbo.nps_formularios
                                     WHERE codigo = :c AND publico = 1 AND ativo = 1"""), {"c": codigo}).mappings().first()
    if not r:
        return None, None
    return _linha_para_form(r), r["conta_id"]


def id_padrao(conn, uso):
    """Formulário padrão da conta para 'nps' ou 'csat'."""
    v = conn.execute(text("SELECT valor FROM dbo.nps_configuracoes WHERE chave = :c"),
                     {"c": f"formulario_padrao_{uso}"}).scalar()
    fid = _int(v, None)
    if fid and conn.execute(text("SELECT 1 FROM dbo.nps_formularios WHERE id = :id AND ativo = 1"), {"id": fid}).scalar():
        return fid
    # sem padrão válido: o formulário ativo mais antigo do tipo certo
    for r in conn.execute(text("SELECT id, perguntas FROM dbo.nps_formularios WHERE ativo = 1 ORDER BY id")).mappings():
        pergs = r["perguntas"] if not isinstance(r["perguntas"], str) else json.loads(r["perguntas"])
        if tipo_do_formulario(pergs) == uso:
            return r["id"]
    return None


def definir_padrao(conn, uso, form_id):
    conn.execute(text("""
        INSERT INTO dbo.nps_configuracoes (chave, valor, descricao, updated_at)
        VALUES (:c, :v, :d, CURRENT_TIMESTAMP)
        ON CONFLICT (conta_id, chave) DO UPDATE SET valor = EXCLUDED.valor, updated_at = CURRENT_TIMESTAMP
    """), {"c": f"formulario_padrao_{uso}", "v": str(form_id or ""),
           "d": f"Formulário usado nos envios de {uso.upper()}"})


def criar(conn, nome, perguntas, tema=None, descricao=""):
    perguntas = normalizar_perguntas(perguntas)
    tema = normalizar_tema(tema or {})
    return conn.execute(text("""
        INSERT INTO dbo.nps_formularios (nome, descricao, perguntas, tema, codigo)
        VALUES (:n, :d, CAST(:p AS JSONB), CAST(:t AS JSONB), :c) RETURNING id
    """), {"n": _txt(nome, 150) or "Formulário sem nome", "d": _txt(descricao, 1000),
           "p": json.dumps(perguntas, ensure_ascii=False), "t": json.dumps(tema, ensure_ascii=False),
           "c": secrets.token_urlsafe(9)}).scalar()


def garantir_formularios_padrao(conn):
    """Conta sem formulários ganha 'Pesquisa NPS' e 'Satisfação pós-entrega' (usando as perguntas já configuradas)."""
    if conn.execute(text("SELECT 1 FROM dbo.nps_formularios LIMIT 1")).scalar():
        return
    cfg = {r[0]: r[1] for r in conn.execute(text(
        "SELECT chave, valor FROM dbo.nps_configuracoes WHERE chave IN ('pergunta_nps', 'pergunta_csat')"))}
    nps = modelo("nps_padrao")
    if cfg.get("pergunta_nps"):
        nps["perguntas"][0]["titulo"] = cfg["pergunta_nps"]
    csat = modelo("pos_entrega")
    if cfg.get("pergunta_csat"):
        csat["perguntas"][0]["titulo"] = cfg["pergunta_csat"]
    id_nps = criar(conn, nps["nome"], nps["perguntas"])
    id_csat = criar(conn, csat["nome"], csat["perguntas"])
    definir_padrao(conn, "nps", id_nps)
    definir_padrao(conn, "csat", id_csat)


# =====================================================================
# Gravação da resposta
# =====================================================================
def gravar_resposta(form, respostas_brutas, contexto):
    """
    contexto: dict com disparo_id, token, cliente_id, empresa_id, email, nome, empresa_cliente,
              referencia, assunto, empresa (nome da conta).
    Deve ser chamada dentro de usando_conta(conta).
    """
    perguntas = form["perguntas"]
    limpas, nota = validar_respostas(perguntas, respostas_brutas)
    if not limpas:
        raise ValueError("Responda pelo menos uma pergunta.")
    principal = pergunta_principal(perguntas)

    # e-mail informado no formulário (link público) identifica o cliente
    email = contexto.get("email")
    if not email:
        for p in perguntas:
            if p["tipo"] == "texto_curto" and p.get("formato") == "email" and p["id"] in limpas:
                email = limpas[p["id"]]
                break

    resumo = texto_resumo(perguntas, limpas, pular_id=principal["id"] if principal else None, placeholders=contexto)
    engine = get_engine()
    with engine.begin() as conn:
        cliente_id = contexto.get("cliente_id")
        if not cliente_id and email:
            cliente_id = conn.execute(text("SELECT cliente_id FROM dbo.nps_clientes WHERE email = :e"), {"e": email}).scalar()
        conn.execute(text("""
            INSERT INTO dbo.nps_formulario_respostas
                (formulario_id, disparo_id, cliente_id, email, referencia, nota_principal, respostas, canal)
            VALUES (:f, :d, :c, :e, :r, :n, CAST(:j AS JSONB), :canal)
        """), {"f": form["id"], "d": contexto.get("disparo_id"), "c": cliente_id, "e": email or None,
               "r": contexto.get("referencia"), "n": nota, "j": json.dumps(limpas, ensure_ascii=False),
               "canal": contexto.get("canal") or "Formulário Rakiti"})

    if principal and nota is not None:
        if principal["tipo"] == "nps":
            _gravar_nps(contexto, cliente_id, email, nota, resumo)
        else:
            _gravar_csat(contexto, cliente_id, email, nota, resumo)
    return nota


def _gravar_nps(ctx, cliente_id, email, nota, resumo):
    """Reaproveita o fluxo do webhook (plano de ação, alertas e e-mail de agradecimento)."""
    from services.respostas_svc import processar_webhook_fillout
    params = [
        {"name": "clienteId", "value": cliente_id or ""},
        {"name": "email", "value": email or ""},
        {"name": "nome", "value": ctx.get("nome") or ""},
        {"name": "empresa", "value": ctx.get("empresa_cliente") or ""},
        {"name": "empresa_id", "value": str(ctx.get("empresa_id") or "")},
    ]
    sub_id = f"rakiti-{ctx['token']}" if ctx.get("token") else f"rakiti-f-{uuid.uuid4().hex}"
    processar_webhook_fillout({
        "formId": f"rakiti-{ctx.get('formulario_id') or ''}",
        "submission": {
            "submissionId": sub_id,
            "urlParameters": params,
            "questions": [
                {"type": "OpinionScale", "name": "nota", "value": nota},
                {"type": "LongAnswer", "name": "motivo", "value": resumo},
            ],
        },
    }, canal=ctx.get("canal") or "Formulário Rakiti")


def _gravar_csat(ctx, cliente_id, email, nota, resumo):
    with get_engine().begin() as conn:
        conn.execute(text("""
            INSERT INTO dbo.nps_csat_respostas (disparo_id, cliente_id, empresa_id, email, nota, comentario, referencia, assunto, canal)
            VALUES (:did, :cid, :eid, :em, :n, :c, :ref, :ass, :canal)
        """), {"did": ctx.get("disparo_id"), "cid": cliente_id, "eid": ctx.get("empresa_id"), "em": email,
               "n": nota, "c": resumo, "ref": ctx.get("referencia"), "ass": ctx.get("assunto"),
               "canal": ctx.get("canal") or "Formulário Rakiti"})
        if nota <= 2:
            quem = ctx.get("nome") or email or "cliente não identificado"
            titulo = f"[CSAT {nota}] Cliente insatisfeito: {ctx.get('assunto') or ctx.get('referencia') or quem}"
            descricao = f"Cliente: {quem}\nReferência: {ctx.get('referencia') or '-'}\n\n{resumo or 'Sem comentários.'}"
            conn.execute(text("""
                INSERT INTO dbo.nps_acoes (empresa_id, titulo, descricao, prioridade, status, prazo_limite, created_at, updated_at)
                VALUES (:eid, :t, :d, 'Alta', 'Pendente', CURRENT_TIMESTAMP + INTERVAL '2 days', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
            """), {"eid": ctx.get("empresa_id"), "t": titulo[:250], "d": descricao})


# =====================================================================
# Resultados
# =====================================================================
def resultados(conn, form, dias=90):
    linhas = conn.execute(text("""
        SELECT r.id, r.created_at, r.respostas, r.nota_principal, r.referencia, r.email,
               COALESCE(c.nome::text, r.email::text, 'Anônimo') AS cliente
        FROM dbo.nps_formulario_respostas r
        LEFT JOIN dbo.nps_clientes c ON c.cliente_id = r.cliente_id
        WHERE r.formulario_id = :f AND r.created_at >= CURRENT_TIMESTAMP - make_interval(days => :d)
        ORDER BY r.created_at DESC
    """), {"f": form["id"], "d": dias}).mappings().all()
    registros = []
    for l in linhas:
        resp = l["respostas"] if not isinstance(l["respostas"], str) else json.loads(l["respostas"])
        registros.append({**dict(l), "respostas": resp})

    perguntas = [p for p in form["perguntas"] if p["tipo"] != "pagina"]
    principal = pergunta_principal(perguntas)
    resumo = []
    for p in perguntas:
        valores = [r["respostas"][p["id"]] for r in registros if p["id"] in r["respostas"]]
        item = {"id": p["id"], "tipo": p["tipo"], "titulo": p["titulo"], "respostas": len(valores),
                "principal": bool(principal and p["id"] == principal["id"])}
        if p["tipo"] in TIPOS_NOTA:
            mn, mx = faixa(p)
            dist = {str(n): 0 for n in range(mn, mx + 1)}
            for v in valores:
                dist[str(v)] = dist.get(str(v), 0) + 1
            item["distribuicao"] = dist
            item["media"] = round(sum(valores) / len(valores), 2) if valores else None
            if p["tipo"] == "nps" and valores:
                prom = sum(1 for v in valores if v >= 9)
                det = sum(1 for v in valores if v <= 6)
                item["nps"] = round(100 * (prom - det) / len(valores))
                item["grupos"] = {"promotor": prom, "neutro": len(valores) - prom - det, "detrator": det}
            if p["tipo"] in ("csat", "estrelas") and valores:
                item["satisfeitos_pct"] = round(100 * sum(1 for v in valores if v >= 4) / len(valores), 1)
        elif p["tipo"] in TIPOS_ESCOLHA or p["tipo"] == "sim_nao":
            opcoes = p.get("opcoes") or ["Sim", "Não"]
            cont = {o: 0 for o in opcoes}
            for v in valores:
                for o in (v if isinstance(v, list) else [v]):
                    cont[o] = cont.get(o, 0) + 1
            item["contagem"] = cont
        else:
            item["ultimas"] = [
                {"valor": r["respostas"][p["id"]], "cliente": r["cliente"], "data": r["created_at"].isoformat()}
                for r in registros if p["id"] in r["respostas"]
            ][:50]
        resumo.append(item)
    return {"total": len(registros), "perguntas": resumo, "registros": registros}


def csv_resultados(form, registros):
    import csv
    import io
    perguntas = [p for p in form["perguntas"] if p["tipo"] != "pagina"]
    buf = io.StringIO()
    w = csv.writer(buf, delimiter=";")
    w.writerow(["Data", "Cliente", "E-mail", "Referência"] + [p["titulo"] for p in perguntas])
    for r in registros:
        linha = [r["created_at"].strftime("%d/%m/%Y %H:%M"), r["cliente"], r["email"] or "", r["referencia"] or ""]
        for p in perguntas:
            v = r["respostas"].get(p["id"], "")
            linha.append(", ".join(v) if isinstance(v, list) else v)
        w.writerow(linha)
    return "﻿" + buf.getvalue()  # BOM para o Excel abrir acentos corretamente
