# =========================================================
# 🔄 MIGRAÇÃO V4 - TEMPORIZADOR ENTRE MENSAGENS
# =========================================================

import os
import logging
from sqlalchemy import create_engine, text

logger = logging.getLogger(__name__)

def executar_migracao_v4():
    """
    Adiciona a coluna 'delay_seconds' na tabela bot_flow_steps:
    - delay_seconds (INTEGER) - Segundos de espera antes de enviar próxima mensagem
    
    Esta função é chamada automaticamente no startup do main.py
    """
    try:
        # Pega a URL do ambiente
        DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///./sql_app.db")
        if DATABASE_URL.startswith("postgres://"):
            DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql://", 1)

        engine = create_engine(DATABASE_URL)
        
        logger.info("🔄 [MIGRAÇÃO V4] Iniciando adição da coluna delay_seconds...")
        
        with engine.connect() as conn:
            # Adiciona coluna 'delay_seconds' (padrão: 0 = sem delay)
            logger.info("   ➡️  Adicionando coluna 'delay_seconds'...")
            sql_delay = """
            ALTER TABLE bot_flow_steps 
            ADD COLUMN IF NOT EXISTS delay_seconds INTEGER DEFAULT 0;
            """
            conn.execute(text(sql_delay))
            conn.commit()
            logger.info("   ✅ Coluna 'delay_seconds' adicionada!")
            
            # Atualiza registros existentes (garantia)
            logger.info("   ➡️  Atualizando registros antigos com valor padrão...")
            sql_update = """
            UPDATE bot_flow_steps 
            SET delay_seconds = 0 
            WHERE delay_seconds IS NULL;
            """
            conn.execute(text(sql_update))
            conn.commit()
            logger.info("   ✅ Registros atualizados!")
            
            logger.info("🎉 [MIGRAÇÃO V4] Concluída com sucesso!")
            return True
            
    except Exception as e:
        # Se der erro de "column already exists", não é problema
        if "already exists" in str(e).lower():
            logger.info("ℹ️  [MIGRAÇÃO V4] Coluna já existe, migração já foi executada anteriormente.")
            return True
        else:
            logger.error(f"❌ [MIGRAÇÃO V4] Erro: {e}")
            return False