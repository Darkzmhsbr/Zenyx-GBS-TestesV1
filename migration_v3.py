# =========================================================
# 🔄 MIGRAÇÃO AUTOMÁTICA V3 - FLOW CHAT
# =========================================================
# Este arquivo será importado pelo main.py no startup

import os
import logging
from sqlalchemy import create_engine, text

logger = logging.getLogger(__name__)

def executar_migracao_v3():
    """
    Adiciona as novas colunas na tabela bot_flow_steps:
    - autodestruir (BOOLEAN) - Se deve apagar a mensagem após clicar
    - mostrar_botao (BOOLEAN) - Se deve mostrar botão de próximo passo
    
    Esta função é chamada automaticamente no startup do main.py
    """
    try:
        # Pega a URL do ambiente
        DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///./sql_app.db")
        if DATABASE_URL.startswith("postgres://"):
            DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql://", 1)

        engine = create_engine(DATABASE_URL)
        
        logger.info("🔄 [MIGRAÇÃO V3] Iniciando atualização da tabela bot_flow_steps...")
        
        with engine.connect() as conn:
            # 1. Adiciona coluna 'autodestruir' (padrão: FALSE)
            logger.info("   ➡️  Adicionando coluna 'autodestruir'...")
            sql_autodestruir = """
            ALTER TABLE bot_flow_steps 
            ADD COLUMN IF NOT EXISTS autodestruir BOOLEAN DEFAULT FALSE;
            """
            conn.execute(text(sql_autodestruir))
            conn.commit()
            logger.info("   ✅ Coluna 'autodestruir' adicionada!")
            
            # 2. Adiciona coluna 'mostrar_botao' (padrão: TRUE)
            logger.info("   ➡️  Adicionando coluna 'mostrar_botao'...")
            sql_mostrar_botao = """
            ALTER TABLE bot_flow_steps 
            ADD COLUMN IF NOT EXISTS mostrar_botao BOOLEAN DEFAULT TRUE;
            """
            conn.execute(text(sql_mostrar_botao))
            conn.commit()
            logger.info("   ✅ Coluna 'mostrar_botao' adicionada!")
            
            # 3. Atualiza registros existentes (garantia)
            logger.info("   ➡️  Atualizando registros antigos com valores padrão...")
            sql_update = """
            UPDATE bot_flow_steps 
            SET autodestruir = FALSE, mostrar_botao = TRUE 
            WHERE autodestruir IS NULL OR mostrar_botao IS NULL;
            """
            conn.execute(text(sql_update))
            conn.commit()
            logger.info("   ✅ Registros atualizados!")
            
            logger.info("🎉 [MIGRAÇÃO V3] Concluída com sucesso!")
            return True
            
    except Exception as e:
        # Se der erro de "column already exists", não é problema
        if "already exists" in str(e).lower():
            logger.info("ℹ️  [MIGRAÇÃO V3] Colunas já existem, migração já foi executada anteriormente.")
            return True
        else:
            logger.error(f"❌ [MIGRAÇÃO V3] Erro: {e}")
            return False