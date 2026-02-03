import logging
from sqlalchemy import text
from database import engine

# Configuração de Logs
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

def executar_migracao_v8():
    """
    MIGRAÇÃO V8: Criação da coluna 'msg_pix' na tabela 'bot_flows'.
    Executa via SQL puro para garantir.
    """
    logger.info("🚀 [V8] Iniciando migração para msg_pix...")
    
    try:
        with engine.connect() as conn:
            # Verifica se a coluna existe
            check_sql = text("SELECT column_name FROM information_schema.columns WHERE table_name='bot_flows' AND column_name='msg_pix'")
            result = conn.execute(check_sql).fetchone()
            
            if not result:
                logger.info("🔧 [V8] Coluna 'msg_pix' não encontrada. Criando...")
                conn.execute(text("ALTER TABLE bot_flows ADD COLUMN msg_pix TEXT"))
                conn.commit()
                logger.info("✅ [V8] Coluna 'msg_pix' criada com sucesso!")
            else:
                logger.info("✅ [V8] Coluna 'msg_pix' já existe. Nada a fazer.")
                
    except Exception as e:
        logger.error(f"❌ [V8] Erro crítico na migração: {e}")
        # Não damos raise aqui para não travar o boot se for erro de permissão simples

if __name__ == "__main__":
    executar_migracao_v8()