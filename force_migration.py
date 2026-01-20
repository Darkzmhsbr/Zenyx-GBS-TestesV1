import os
import time
from sqlalchemy import create_engine, text

# Pega a URL do banco do ambiente
DATABASE_URL = os.getenv("DATABASE_URL")
if DATABASE_URL and DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql://", 1)

def forcar_atualizacao_tabelas():
    if not DATABASE_URL:
        print("❌ DATABASE_URL não encontrada.")
        return

    print("🚀 Iniciando Migração Forçada de Colunas...")
    
    engine = create_engine(DATABASE_URL)
    
    # Lista de colunas novas para adicionar na tabela miniapp_categories
    novas_colunas = [
        "bg_color VARCHAR DEFAULT '#000000'",
        "banner_desk_url VARCHAR",
        "video_preview_url VARCHAR",
        "model_img_url VARCHAR",
        "model_name VARCHAR",
        "model_desc TEXT",
        "footer_banner_url VARCHAR",
        "deco_lines_url VARCHAR",
        # NOVAS
        "model_name_color VARCHAR DEFAULT '#ffffff'",
        "model_desc_color VARCHAR DEFAULT '#cccccc'"
    ]

    with engine.connect() as conn:
        # Habilita o commit automático
        conn.execution_options(isolation_level="AUTOCOMMIT")
        
        for coluna_sql in novas_colunas:
            col_name = coluna_sql.split()[0]
            try:
                sql = text(f"ALTER TABLE miniapp_categories ADD COLUMN IF NOT EXISTS {coluna_sql};")
                conn.execute(sql)
                print(f"✅ Coluna verificada/criada: {col_name}")
            except Exception as e:
                print(f"⚠️ Erro ao criar {col_name}: {e}")
                # Se der erro de transação abortada, tentamos rollback e seguir
                try:
                    conn.rollback()
                except:
                    pass

    print("🏁 Migração Forçada Concluída!")

if __name__ == "__main__":
    forcar_atualizacao_tabelas()