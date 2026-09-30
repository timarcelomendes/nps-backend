"""
Planos, teste grátis e situação da assinatura de cada empresa cliente.

Os valores ficam aqui (um lugar só). Para mudar preço ou limite, altere PLANOS
e faça o deploy; assinaturas já existentes mantêm o valor até a próxima troca de plano.
"""
from datetime import datetime, timedelta, timezone
from sqlalchemy import text

from database import get_engine, modo_sistema, conta_atual

DIAS_TESTE = 14
DIAS_TOLERANCIA_ATRASO = 7   # depois disso os envios param até o pagamento

PLANOS = {
    "essencial": {
        "nome": "Essencial", "preco": 149.00, "limite_clientes": 300, "ia_mensal": 100,
        "destaques": ["Até 300 clientes ativos", "NPS e CSAT ilimitados", "Formulários e link público", "100 resumos de IA por mês"],
    },
    "profissional": {
        "nome": "Profissional", "preco": 349.00, "limite_clientes": 1500, "ia_mensal": 500,
        "destaques": ["Até 1.500 clientes ativos", "Tudo do Essencial", "API para ERP/TMS", "500 resumos de IA por mês"],
        "recomendado": True,
    },
    "empresa": {
        "nome": "Empresa", "preco": 799.00, "limite_clientes": None, "ia_mensal": 2000,
        "destaques": ["Clientes ilimitados", "Tudo do Profissional", "Vários grupos e unidades", "2.000 resumos de IA por mês"],
    },
}
PLANO_DO_TESTE = "profissional"   # durante o teste a empresa usa os recursos do Profissional


def agora():
    return datetime.now(timezone.utc).replace(tzinfo=None)


def dados_conta(conta_id=None):
    conta_id = conta_id or conta_atual()
    with modo_sistema():
        with get_engine().connect() as conn:
            r = conn.execute(text("""
                SELECT id, nome, plano, status_assinatura, teste_ate, atrasada_desde, limite_clientes,
                       cpf_cnpj, email_cobranca, telefone, asaas_customer_id, asaas_subscription_id, origem
                FROM dbo.nps_contas WHERE id = :id
            """), {"id": conta_id}).mappings().first()
    return dict(r) if r else None


def situacao(conta: dict) -> dict:
    """Resumo para telas e bloqueios: pode_enviar, rótulo, dias restantes etc."""
    status = conta.get("status_assinatura") or "cortesia"
    hoje = agora()
    info = {"status": status, "pode_enviar": True, "dias_restantes": None, "mensagem": ""}
    if status == "teste":
        fim = conta.get("teste_ate") or hoje
        dias = max(0, (fim - hoje).days + (1 if (fim - hoje).seconds > 0 else 0))
        info["dias_restantes"] = dias
        if fim <= hoje:
            info.update(status="teste_expirado", pode_enviar=False,
                        mensagem="O teste grátis terminou. Escolha um plano para voltar a enviar pesquisas.")
        else:
            info["mensagem"] = f"Teste grátis: {dias} {'dia' if dias == 1 else 'dias'} restantes."
    elif status == "atrasada":
        desde = conta.get("atrasada_desde") or hoje
        limite = desde + timedelta(days=DIAS_TOLERANCIA_ATRASO)
        if hoje >= limite:
            info.update(pode_enviar=False, mensagem="Pagamento em atraso. Os envios estão pausados até a confirmação do pagamento.")
        else:
            dias = max(1, (limite - hoje).days)
            info["mensagem"] = f"Pagamento em atraso. Regularize em até {dias} {'dia' if dias == 1 else 'dias'} para não pausar os envios."
    elif status == "cancelada":
        info.update(pode_enviar=False, mensagem="Assinatura cancelada. Escolha um plano para voltar a enviar pesquisas.")
    return info


def pode_enviar(conta_id=None) -> tuple[bool, str]:
    conta = dados_conta(conta_id)
    if not conta:
        return False, "Conta não encontrada."
    s = situacao(conta)
    return s["pode_enviar"], s["mensagem"]


def limite_ia(conta_id=None):
    conta = dados_conta(conta_id)
    if not conta or (conta.get("status_assinatura") or "cortesia") == "cortesia":
        return None
    plano = PLANOS.get(conta.get("plano") or "") or PLANOS[PLANO_DO_TESTE]
    return plano["ia_mensal"]


def aplicar_plano(conn, conta_id, plano: str):
    """Atualiza plano e limite de clientes da conta (conn em modo sistema)."""
    p = PLANOS[plano]
    conn.execute(text("UPDATE dbo.nps_contas SET plano = :p, limite_clientes = :l WHERE id = :id"),
                 {"p": plano, "l": p["limite_clientes"], "id": conta_id})


def iniciar_teste(conn, conta_id):
    conn.execute(text("""
        UPDATE dbo.nps_contas SET status_assinatura = 'teste', teste_ate = :fim, origem = 'cadastro'
        WHERE id = :id
    """), {"fim": agora() + timedelta(days=DIAS_TESTE), "id": conta_id})
    aplicar_plano(conn, conta_id, PLANO_DO_TESTE)


def uso(conta_id=None):
    conta_id = conta_id or conta_atual()
    with get_engine().connect() as conn:
        clientes = conn.execute(text("SELECT COUNT(*) FROM dbo.nps_clientes WHERE COALESCE(ativo, 1) = 1")).scalar() or 0
    return {"clientes_ativos": clientes}
