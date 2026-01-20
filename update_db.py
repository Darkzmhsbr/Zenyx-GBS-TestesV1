import os
from sqlalchemy import create_engine, text

# Pega a URL do ambiente
DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///./sql_app.db")
if DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql://", 1)

engine = create_engine(DATABASE_URL)

def adicionar_colunas():
    print("🔄 [V2] Criando tabela nova de passos...")
    with engine.connect() as conn:
        try:
            # CRIA A TABELA V2 LIMPA
            sql = """
            CREATE TABLE IF NOT EXISTS bot_flow_steps_v2 (
                id SERIAL PRIMARY KEY,
                bot_id INTEGER,
                step_order INTEGER DEFAULT 1,
                msg_texto TEXT,
                msg_media VARCHAR,
                btn_texto VARCHAR DEFAULT 'Próximo ▶️',
                created_at TIMESTAMP WITHOUT TIME ZONE DEFAULT now()
            );
            """
            conn.execute(text(sql))
            conn.commit()
            print("✅ Tabela 'bot_flow_steps_v2' criada com sucesso!")
        except Exception as e:
            print(f"⚠️ Erro ao criar tabela: {e}")

if __name__ == "__main__":
    adicionar_colunas()