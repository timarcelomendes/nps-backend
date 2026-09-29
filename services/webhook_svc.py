import traceback
from services.respostas_svc import processar_webhook_fillout
from services.teams_svc import enviar_alerta_tecnico_teams

def processar_webhook_background(payload: dict, conta_id: int = None):
    """
    Processa o payload nativo do webhook em segundo plano, na conta dona do token.
    """
    from database import usando_conta
    with usando_conta(conta_id):
        _processar(payload)


def _processar(payload: dict):
    try:
        print("⏳ [Webhook SVC] A iniciar processamento em background...")
        
        if not payload:
            print("⚠️ [Webhook SVC] O webhook chegou sem payload (corpo vazio). Ignorando.")
            return
            
        processar_webhook_fillout(payload)
        
        print("✅ [Webhook SVC] Sucesso: Webhook processado e salvo no banco.")
        
    except Exception as e:
        err_msg = str(e)
        err_trace = traceback.format_exc()
        
        print(f"❌ [Webhook SVC] ERRO GRAVE: {err_msg}")
        print(err_trace)
        
        alerta = f"**Falha no Processamento do Webhook**\n\n**Erro:** {err_msg}\n\nVerifique os logs do servidor para ver o Traceback."
        try:
            enviar_alerta_tecnico_teams(alerta)
        except:
            pass