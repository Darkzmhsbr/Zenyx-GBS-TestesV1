# =========================================================
# 🔄 MIGRAÇÃO V5 - ORDER BUMP (TABELAS E COLUNAS)
# =========================================================

import os
import logging
from sqlalchemy import create_engine, text

logger = logging.getLogger(__name__)

def executar_migracao_v5():
    """
    1. Adiciona coluna 'tem_order_bump' na tabela pedidos.
    2. Cria a tabela 'order_bump_config' se não existir.
    """
    try:
        # Pega a URL do ambiente
        DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///./sql_app.db")
        if DATABASE_URL.startswith("postgres://"):
            DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql://", 1)

        engine = create_engine(DATABASE_URL)
        
        logger.info("🔄 [MIGRAÇÃO V5] Iniciando estrutura do Order Bump...")
        
        with engine.connect() as conn:
            # 1. Adicionar coluna na tabela PEDIDOS (O ERRO CRÍTICO ESTÁ AQUI)
            logger.info("   ➡️  Verificando coluna 'tem_order_bump' em 'pedidos'...")
            sql_coluna = """
            ALTER TABLE pedidos 
            ADD COLUMN IF NOT EXISTS tem_order_bump BOOLEAN DEFAULT FALSE;
            """
            conn.execute(text(sql_coluna))
            conn.commit()
            logger.info("   ✅ Coluna 'tem_order_bump' verificada/adicionada!")

            # 2. Criar tabela ORDER_BUMP_CONFIG (Caso o SQLAlchemy não tenha criado)
            logger.info("   ➡️  Verificando tabela 'order_bump_config'...")
            sql_tabela = """
            CREATE TABLE IF NOT EXISTS order_bump_config (
                id SERIAL PRIMARY KEY,
                bot_id INTEGER UNIQUE,
                ativo BOOLEAN DEFAULT FALSE,
                nome_produto VARCHAR,
                preco FLOAT,
                link_acesso VARCHAR,
                msg_texto TEXT,
                msg_media VARCHAR,
                btn_aceitar VARCHAR DEFAULT '✅ SIM, ADICIONAR',
                btn_recusar VARCHAR DEFAULT '❌ NÃO, OBRIGADO',
                FOREIGN KEY (bot_id) REFERENCES bots(id)
            );
            """
            conn.execute(text(sql_tabela))
            conn.commit()
            logger.info("   ✅ Tabela 'order_bump_config' verificada/criada!")
            
            logger.info("🎉 [MIGRAÇÃO V5] Concluída com sucesso!")
            return True
            
    except Exception as e:
        if "already exists" in str(e).lower():
            logger.info("ℹ️  [MIGRAÇÃO V5] Estrutura já existe.")
            return True
        else:
            logger.error(f"❌ [MIGRAÇÃO V5] Erro: {e}")
            return False