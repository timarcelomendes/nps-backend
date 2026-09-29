"""
Prepara o banco automaticamente na inicialização da API.

1. Se as tabelas ainda não existem, executa db/schema_postgres.sql.
2. Aplica as migrações pendentes (db/migracao_*.sql), em ordem.
3. Se não existe nenhum usuário, cria o Admin inicial da conta 1 a partir de:
     ADMIN_EMAIL, ADMIN_SENHA, ADMIN_NOME (opcional), DOMINIOS_PERMITIDOS (opcional)

É seguro rodar a cada inicialização: nada é recriado se já existir.
"""
import os
from pathlib import Path
from sqlalchemy import text

from database import get_engine, modo_sistema, usando_conta

PASTA_DB = Path(__file__).parent / "db"
SCHEMA_FILE = PASTA_DB / "schema_postgres.sql"

# Configurações padrão de toda conta nova
CONFIGURACOES_PADRAO = [
    ("dominios_permitidos", "", "Domínios de e-mail que podem acessar (separados por vírgula)"),
    ("survey_url", "", "Link do formulário de pesquisa (ex.: https://forms.fillout.com/t/SEU_FORM)"),
    ("recorrencia_dias", "90", "Intervalo entre pesquisas para o mesmo cliente"),
    ("envios_ativos", "0", "Liga/desliga o disparo automático de pesquisas"),
    ("robo_ativo", "0", "Liga/desliga o agendador"),
    ("sessao_expiracao_minutos", "480", "Tempo de sessão do usuário"),
    ("mostrar_sem_cliente", "0", "Mostrar respostas sem cliente vinculado"),
    ("sso_microsoft_ativo", "0", "Login com Microsoft"),
    ("sla_detrator_dias", "2", "SLA de tratamento de detratores"),
    ("sla_neutro_dias", "5", "SLA de tratamento de neutros"),
    ("sla_promotor_dias", "10", "SLA de tratamento de promotores"),
    ("openai_model", "gpt-4o-mini", "Modelo de IA"),
    ("scheduler_hora_inicio", "09:00", "Horário do primeiro envio do dia"),
    ("scheduler_horas", "6", "Intervalo entre rodadas de envio (horas)"),
    ("teams_horario_resumo", "08:00", "Horário do resumo diário no Teams"),
    ("lembrete_qtd_maxima", "2", "Quantidade máxima de lembretes"),
    ("lembrete_dias_1", "3", "Dias até o 1º lembrete"),
    ("lembrete_dias_2", "7", "Dias até o 2º lembrete"),
    ("lembrete_dias_3", "15", "Dias até o 3º lembrete"),
    ("fillout_campos", "clienteId,email,nome,empresa,empresa_id", "Dados enviados ao formulário"),
    ("ai_temperature", "0.3", "Criatividade da IA"),
]


def _executar_script(engine, caminho: Path):
    sql = caminho.read_text(encoding="utf-8")
    raw = engine.raw_connection()
    try:
        cur = raw.cursor()
        cur.execute(sql)  # script inteiro (sem parâmetros) em uma única transação
        raw.commit()
    except Exception:
        raw.rollback()
        raise
    finally:
        raw.close()


def criar_configuracoes_padrao(conn, dominios: str = ""):
    """Insere as configurações padrão na conta ativa (não sobrescreve as existentes)."""
    for chave, valor, descricao in CONFIGURACOES_PADRAO:
        if chave == "dominios_permitidos" and dominios:
            valor = dominios
        conn.execute(text("""
            INSERT INTO dbo.nps_configuracoes (chave, valor, descricao)
            VALUES (:c, :v, :d)
            ON CONFLICT (conta_id, chave) DO NOTHING
        """), {"c": chave, "v": valor, "d": descricao})


def criar_usuario_admin(conn, nome: str, email: str, senha: str):
    from services.auth_svc import hash_password
    conn.execute(text("""
        INSERT INTO dbo.nps_usuarios (nome, email, senha_hash, tipo, cargo, ativo, email_verificado)
        VALUES (:nome, :email, :hash, 'Admin', 'Administrador', 1, 1)
    """), {"nome": nome or "Administrador", "email": email, "hash": hash_password(senha)})


def _criar_admin_inicial(engine):
    email = os.getenv("ADMIN_EMAIL", "").strip()
    senha = os.getenv("ADMIN_SENHA", "")
    if not email or not senha:
        print("ℹ️ Nenhum usuário no banco. Defina ADMIN_EMAIL e ADMIN_SENHA para criar o Admin inicial.")
        return
    nome = os.getenv("ADMIN_NOME", "Administrador").strip()
    dominios = os.getenv("DOMINIOS_PERMITIDOS", "").strip() or email.split("@")[-1].lower()
    with usando_conta(1):
        with engine.begin() as conn:
            criar_usuario_admin(conn, nome, email, senha)
            criar_configuracoes_padrao(conn, dominios)
            conn.execute(text("""
                UPDATE dbo.nps_configuracoes SET valor = :v, updated_at = CURRENT_TIMESTAMP
                WHERE chave = 'dominios_permitidos'
            """), {"v": dominios})
    print(f"👤 Admin inicial criado: {email} (domínios permitidos: {dominios})")


def preparar_banco():
    try:
        engine = get_engine()
        with modo_sistema():
            with engine.connect() as conn:
                existe = conn.execute(text("SELECT to_regclass('dbo.nps_usuarios') IS NOT NULL")).scalar()
            if not existe:
                print("🧱 Banco vazio: criando tabelas a partir de db/schema_postgres.sql ...")
                _executar_script(engine, SCHEMA_FILE)
                print("✅ Tabelas criadas.")

            # Migrações são idempotentes: rodam sempre, em ordem alfabética
            for arquivo in sorted(PASTA_DB.glob("migracao_*.sql")):
                _executar_script(engine, arquivo)
            print("✅ Migrações aplicadas.")

            with engine.connect() as conn:
                total = conn.execute(text("SELECT COUNT(*) FROM dbo.nps_usuarios")).scalar()
        if total == 0:
            _criar_admin_inicial(engine)
        else:
            # garante as configurações novas (ex.: survey_url) em todas as contas existentes
            with modo_sistema():
                with engine.connect() as conn:
                    contas = [r[0] for r in conn.execute(text("SELECT id FROM dbo.nps_contas"))]
            for conta in contas:
                with usando_conta(conta):
                    with engine.begin() as conn:
                        criar_configuracoes_padrao(conn)
    except Exception as e:
        print(f"❌ Erro ao preparar o banco: {e}")
