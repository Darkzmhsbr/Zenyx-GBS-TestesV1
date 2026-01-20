# =========================================================
# 🔄 MIGRAÇÃO V6 - ORDER BUMP AUTODESTRUIR
# =========================================================

import os
import logging
from sqlalchemy import create_engine, text

logger = logging.getLogger(__name__)

def executar_migracao_v6():
    """
    Adiciona a coluna 'autodestruir' na tabela order_bump_config.
    """
    try:
        # Pega a URL do ambiente
        DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///./sql_app.db")
        if DATABASE_URL.startswith("postgres://"):
            DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql://", 1)

        engine = create_engine(DATABASE_URL)
        
        logger.info("🔄 [MIGRAÇÃO V6] Adicionando coluna autodestruir ao Order Bump...")
        
        with engine.connect() as conn:
            # Adiciona coluna 'autodestruir' (padrão: FALSE)
            sql_coluna = """
            ALTER TABLE order_bump_config 
            ADD COLUMN IF NOT EXISTS autodestruir BOOLEAN DEFAULT FALSE;
            """
            conn.execute(text(sql_coluna))
            conn.commit()
            logger.info("   ✅ Coluna 'autodestruir' adicionada com sucesso!")
            
            return True
            
    except Exception as e:
        if "already exists" in str(e).lower():
            logger.info("ℹ️  [MIGRAÇÃO V6] Coluna já existe.")
            return True
        else:
            logger.error(f"❌ [MIGRAÇÃO V6] Erro: {e}")
            return False