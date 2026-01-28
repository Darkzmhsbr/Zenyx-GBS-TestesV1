import os
from sqlalchemy import create_engine, Column, Integer, String, Float, DateTime, Boolean, Text, ForeignKey, JSON, text
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker, relationship
from sqlalchemy.pool import QueuePool
from sqlalchemy.sql import func
from datetime import datetime

DATABASE_URL = os.getenv("DATABASE_URL")

if DATABASE_URL and DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql://", 1)

if DATABASE_URL:
    engine = create_engine(
        DATABASE_URL,
        poolclass=QueuePool,
        pool_size=5,
        max_overflow=10,
        pool_timeout=30,
        pool_recycle=1800
    )
else:
    engine = create_engine("sqlite:///./sql_app.db")

SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()

def init_db():
    Base.metadata.create_all(bind=engine)

# =========================================================
# 👤 USUÁRIOS
# =========================================================
class User(Base):
    __tablename__ = "users"
    
    id = Column(Integer, primary_key=True, index=True)
    username = Column(String, unique=True, nullable=False, index=True)
    email = Column(String, unique=True, nullable=False, index=True)
    password_hash = Column(String, nullable=False)
    full_name = Column(String, nullable=True)
    is_active = Column(Boolean, default=True)
    is_superuser = Column(Boolean, default=False)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    pushin_pay_id = Column(String, nullable=True)
    taxa_venda = Column(Integer, default=60)
    bots = relationship("Bot", back_populates="owner")
    audit_logs = relationship("AuditLog", back_populates="user")
    notifications = relationship("Notification", back_populates="user")

class SystemConfig(Base):
    __tablename__ = "system_config"
    key = Column(String, primary_key=True, index=True) 
    value = Column(String)
    updated_at = Column(DateTime, default=datetime.utcnow)

# =========================================================
# 🤖 BOTS - MANTENDO NOMENCLATURA DO BANCO ATUAL
# =========================================================
class Bot(Base):
    __tablename__ = "bots"

    id = Column(Integer, primary_key=True, index=True)
    nome = Column(String)
    token = Column(String, unique=True, index=True)
    username = Column(String, nullable=True)
    id_canal_vip = Column(String)
    admin_principal_id = Column(String, nullable=True)
    suporte_username = Column(String, nullable=True)
    status = Column(String, default="ativo")
    pushin_token = Column(String, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    owner_id = Column(Integer, ForeignKey("users.id"), nullable=True)
    owner = relationship("User", back_populates="bots")
    planos = relationship("PlanoConfig", back_populates="bot", cascade="all, delete-orphan")
    fluxo = relationship("BotFlow", back_populates="bot", uselist=False, cascade="all, delete-orphan")
    steps = relationship("BotFlowStep", back_populates="bot", cascade="all, delete-orphan")
    admins = relationship("BotAdmin", back_populates="bot", cascade="all, delete-orphan")
    pedidos = relationship("Pedido", backref="bot_ref", cascade="all, delete-orphan")
    leads = relationship("Lead", backref="bot_ref", cascade="all, delete-orphan")
    remarketing_campaigns = relationship("RemarketingCampaign", back_populates="bot", cascade="all, delete-orphan")
    order_bump = relationship("OrderBumpConfig", uselist=False, back_populates="bot", cascade="all, delete-orphan")
    tracking_links = relationship("TrackingLink", back_populates="bot", cascade="all, delete-orphan")
    miniapp_config = relationship("MiniAppConfig", uselist=False, back_populates="bot", cascade="all, delete-orphan")
    miniapp_categories = relationship("MiniAppCategory", back_populates="bot", cascade="all, delete-orphan")
    remarketing_config = relationship("RemarketingConfig", uselist=False, back_populates="bot", cascade="all, delete-orphan")
    alternating_messages = relationship("AlternatingMessages", uselist=False, back_populates="bot", cascade="all, delete-orphan")
    remarketing_logs = relationship("RemarketingLog", back_populates="bot", cascade="all, delete-orphan")

class BotAdmin(Base):
    __tablename__ = "bot_admins"
    id = Column(Integer, primary_key=True, index=True)
    bot_id = Column(Integer, ForeignKey("bots.id"))
    telegram_id = Column(String)
    nome = Column(String, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    bot = relationship("Bot", back_populates="admins")

class OrderBumpConfig(Base):
    __tablename__ = "order_bump_config"
    id = Column(Integer, primary_key=True, index=True)
    bot_id = Column(Integer, ForeignKey("bots.id"), unique=True)
    ativo = Column(Boolean, default=False)
    nome_produto = Column(String)
    preco = Column(Float)
    link_acesso = Column(String, nullable=True)
    autodestruir = Column(Boolean, default=False)
    msg_texto = Column(Text, default="Gostaria de adicionar este item?")
    msg_media = Column(String, nullable=True)
    btn_aceitar = Column(String, default="✅ SIM, ADICIONAR")
    btn_recusar = Column(String, default="❌ NÃO, OBRIGADO")
    bot = relationship("Bot", back_populates="order_bump")

class PlanoConfig(Base):
    __tablename__ = "plano_config"
    id = Column(Integer, primary_key=True, index=True)
    bot_id = Column(Integer, ForeignKey("bots.id"))
    nome_exibicao = Column(String(100))
    descricao = Column(Text)
    preco_atual = Column(Float)
    preco_cheio = Column(Float)
    dias_duracao = Column(Integer, default=30)
    is_lifetime = Column(Boolean, default=False)
    key_id = Column(String(100), unique=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    bot = relationship("Bot", back_populates="planos")

class RemarketingCampaign(Base):
    __tablename__ = "remarketing_campaigns"
    id = Column(Integer, primary_key=True, index=True)
    bot_id = Column(Integer, ForeignKey("bots.id"))
    campaign_id = Column(String, unique=True)
    target = Column(String, default="todos")
    type = Column(String, default="massivo")
    config = Column(Text)
    status = Column(String, default="agendado")
    dia_atual = Column(Integer, default=0)
    data_inicio = Column(DateTime, default=datetime.utcnow)
    proxima_execucao = Column(DateTime, nullable=True)
    total_enviados = Column(Integer, default=0)
    total_erros = Column(Integer, default=0)
    total_conversoes = Column(Integer, default=0)
    created_at = Column(DateTime, default=datetime.utcnow)
    bot = relationship("Bot", back_populates="remarketing_campaigns")

# =========================================================
# 🛒 PEDIDOS - MANTENDO TODAS AS COLUNAS DO BANCO ATUAL
# =========================================================
class Pedido(Base):
    __tablename__ = "pedidos"
    
    id = Column(Integer, primary_key=True, index=True)
    bot_id = Column(Integer, ForeignKey("bots.id"))
    telegram_id = Column(String)
    first_name = Column(String)
    username = Column(String, nullable=True)
    plano_nome = Column(String)
    plano_id = Column(Integer, nullable=True)
    valor = Column(Float)
    transaction_id = Column(String, unique=True, index=True)
    qr_code = Column(Text, nullable=True)
    status = Column(String, default="pending")
    tem_order_bump = Column(Boolean, default=False)
    created_at = Column(DateTime, default=datetime.utcnow)
    
    # ✅ MANTENDO TODAS AS COLUNAS QUE EXISTEM NO BANCO
    paid_at = Column(DateTime, nullable=True)
    expires_at = Column(DateTime, nullable=True)  # Coluna antiga
    validade = Column(DateTime, nullable=True)     # Coluna nova
    
    tracking_id = Column(String, nullable=True)

class Lead(Base):
    __tablename__ = "leads"
    id = Column(Integer, primary_key=True, index=True)
    bot_id = Column(Integer, ForeignKey("bots.id"))
    user_id = Column(String)
    first_name = Column(String)
    username = Column(String, nullable=True)
    comprou = Column(Boolean, default=False)
    valor_gasto = Column(Float, default=0.0)
    ultima_interacao = Column(DateTime, default=datetime.utcnow)
    created_at = Column(DateTime, default=datetime.utcnow)
    tracking_id = Column(String, nullable=True)
    status = Column(String, default="active")

class TrackingFolder(Base):
    __tablename__ = "tracking_folders"
    id = Column(Integer, primary_key=True, index=True)
    bot_id = Column(Integer, ForeignKey("bots.id"))
    nome = Column(String)
    created_at = Column(DateTime, default=datetime.utcnow)

class TrackingLink(Base):
    __tablename__ = "tracking_links"
    id = Column(Integer, primary_key=True, index=True)
    folder_id = Column(Integer, ForeignKey("tracking_folders.id"), nullable=True)
    bot_id = Column(Integer, ForeignKey("bots.id"))
    nome = Column(String)
    url = Column(String)
    tracking_id = Column(String, unique=True, index=True)
    clicks = Column(Integer, default=0)
    conversoes = Column(Integer, default=0)
    created_at = Column(DateTime, default=datetime.utcnow)
    bot = relationship("Bot", back_populates="tracking_links")

class BotFlow(Base):
    __tablename__ = "bot_flows"
    id = Column(Integer, primary_key=True, index=True)
    bot_id = Column(Integer, ForeignKey("bots.id"), unique=True)
    start_mode = Column(String, default="padrao")
    miniapp_url = Column(String, nullable=True)
    miniapp_btn_text = Column(String, default="🛒 ABRIR LOJA")
    msg_boas_vindas = Column(Text, default="Olá! Bem-vindo(a)!")
    media_url = Column(String, nullable=True)
    btn_text_1 = Column(String, default="📋 Ver Planos")
    autodestruir_1 = Column(Boolean, default=False)
    mostrar_planos_1 = Column(Boolean, default=True)
    msg_2_texto = Column(Text, nullable=True)
    msg_2_media = Column(String, nullable=True)
    mostrar_planos_2 = Column(Boolean, default=False)
    bot = relationship("Bot", back_populates="fluxo")

class BotFlowStep(Base):
    __tablename__ = "bot_flow_steps"
    id = Column(Integer, primary_key=True, index=True)
    bot_id = Column(Integer, ForeignKey("bots.id"))
    step_order = Column(Integer, default=1)
    msg_texto = Column(Text, nullable=True)
    msg_media = Column(String, nullable=True)
    btn_texto = Column(String, default="Próximo ▶️")
    autodestruir = Column(Boolean, default=False)
    mostrar_botao = Column(Boolean, default=True)
    delay_seconds = Column(Integer, default=0)
    created_at = Column(DateTime, default=datetime.utcnow)
    bot = relationship("Bot", back_populates="steps")

class MiniAppConfig(Base):
    __tablename__ = "miniapp_config"
    bot_id = Column(Integer, ForeignKey("bots.id"), primary_key=True)
    logo_url = Column(String, nullable=True)
    background_type = Column(String, default="solid")
    background_value = Column(String, default="#000000")
    hero_video_url = Column(String, nullable=True)
    hero_title = Column(String, default="ACERVO PREMIUM")
    hero_subtitle = Column(String, default="O maior acervo da internet.")
    hero_btn_text = Column(String, default="LIBERAR CONTEÚDO 🔓")
    enable_popup = Column(Boolean, default=False)
    popup_video_url = Column(String, nullable=True)
    popup_text = Column(String, default="VOCÊ GANHOU UM PRESENTE!")
    footer_text = Column(String, default="© 2026 Premium Club.")
    bot = relationship("Bot", back_populates="miniapp_config")

class MiniAppCategory(Base):
    __tablename__ = "miniapp_categories"
    id = Column(Integer, primary_key=True, index=True)
    bot_id = Column(Integer, ForeignKey("bots.id"))
    slug = Column(String)
    title = Column(String)
    description = Column(String)
    cover_image = Column(String)
    banner_mob_url = Column(String)
    bg_color = Column(String, default="#000000")
    banner_desk_url = Column(String, nullable=True)
    video_preview_url = Column(String, nullable=True)
    model_img_url = Column(String, nullable=True)
    model_name = Column(String, nullable=True)
    model_desc = Column(String, nullable=True)
    footer_banner_url = Column(String, nullable=True)
    deco_lines_url = Column(String, nullable=True)
    model_name_color = Column(String, default="#ffffff")
    model_desc_color = Column(String, default="#cccccc")
    theme_color = Column(String, default="#c333ff")
    is_direct_checkout = Column(Boolean, default=False)
    is_hacker_mode = Column(Boolean, default=False)
    content_json = Column(Text)
    bot = relationship("Bot", back_populates="miniapp_categories")

class AuditLog(Base):
    __tablename__ = "audit_logs"
    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    username = Column(String, nullable=False)
    action = Column(String(50), nullable=False, index=True)
    resource_type = Column(String(50), nullable=False, index=True)
    resource_id = Column(Integer, nullable=True)
    description = Column(Text, nullable=True)
    details = Column(Text, nullable=True)
    ip_address = Column(String(50), nullable=True)
    user_agent = Column(Text, nullable=True)
    success = Column(Boolean, default=True, index=True)
    error_message = Column(Text, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow, index=True)
    user = relationship("User", back_populates="audit_logs")

class Notification(Base):
    __tablename__ = "notifications"
    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), index=True)
    title = Column(String, nullable=False)
    message = Column(String, nullable=False)
    type = Column(String, default="info")
    read = Column(Boolean, default=False)
    created_at = Column(DateTime, default=datetime.utcnow)
    user = relationship("User", back_populates="notifications")

class WebhookRetry(Base):
    __tablename__ = "webhook_retries"
    id = Column(Integer, primary_key=True, index=True)
    bot_id = Column(Integer, ForeignKey("bots.id"), nullable=False, index=True)
    webhook_url = Column(String(500), nullable=False)
    payload = Column(JSON, nullable=False)
    headers = Column(JSON, nullable=True)
    attempt_count = Column(Integer, default=0)
    max_attempts = Column(Integer, default=5)
    next_retry_at = Column(DateTime, nullable=True, index=True)
    status = Column(String(20), default="pending", index=True)
    last_error = Column(Text, nullable=True)
    completed_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

# =========================================================
# 🎯 REMARKETING AUTOMÁTICO
# =========================================================
class RemarketingConfig(Base):
    __tablename__ = "remarketing_config"
    id = Column(Integer, primary_key=True, index=True)
    bot_id = Column(Integer, ForeignKey('bots.id', ondelete='CASCADE'), nullable=False, unique=True, index=True)
    is_active = Column(Boolean, default=True, index=True)
    message_text = Column(Text, nullable=True)
    media_url = Column(String(500), nullable=True)
    media_type = Column(String(10), nullable=True)
    delay_minutes = Column(Integer, default=5)
    auto_destruct_seconds = Column(Integer, default=0)
    promo_values = Column(JSON, default={})
    alternating_enabled = Column(Boolean, default=False)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    bot = relationship("Bot", back_populates="remarketing_config")

class AlternatingMessages(Base):
    __tablename__ = "alternating_messages"
    id = Column(Integer, primary_key=True, index=True)
    bot_id = Column(Integer, ForeignKey('bots.id', ondelete='CASCADE'), nullable=False, unique=True, index=True)
    is_active = Column(Boolean, default=False, index=True)
    messages = Column(JSON, default=[])
    rotation_interval_seconds = Column(Integer, default=15)
    stop_before_remarketing_seconds = Column(Integer, default=60)
    auto_destruct_final = Column(Boolean, default=False)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    bot = relationship("Bot", back_populates="alternating_messages")

class RemarketingLog(Base):
    __tablename__ = "remarketing_logs"
    id = Column(Integer, primary_key=True, index=True)
    bot_id = Column(Integer, ForeignKey('bots.id', ondelete='CASCADE'), nullable=False, index=True)
    user_telegram_id = Column(Integer, nullable=False, index=True)
    sent_at = Column(DateTime, default=datetime.utcnow, index=True)
    message_text = Column(Text, nullable=True)
    promo_values = Column(JSON, nullable=True)
    status = Column(String(20), default='sent', index=True)
    error_message = Column(Text, nullable=True)
    converted = Column(Boolean, default=False, index=True)
    converted_at = Column(DateTime, nullable=True)
    bot = relationship("Bot", back_populates="remarketing_logs")

def forcar_atualizacao_tabelas():
    from sqlalchemy import inspect
    inspector = inspect(engine)
    
    # Adicionar colunas em pedidos se não existirem
    if 'pedidos' in inspector.get_table_names():
        columns = [c['name'] for c in inspector.get_columns('pedidos')]
        
        if 'paid_at' not in columns:
            with engine.connect() as conn:
                conn.execute(text("ALTER TABLE pedidos ADD COLUMN paid_at TIMESTAMP"))
                conn.commit()
                print("✅ Coluna 'paid_at' adicionada")
        
        if 'validade' not in columns:
            with engine.connect() as conn:
                conn.execute(text("ALTER TABLE pedidos ADD COLUMN validade TIMESTAMP"))
                conn.commit()
                print("✅ Coluna 'validade' adicionada")
                
                # Copiar dados de expires_at para validade
                if 'expires_at' in columns:
                    conn.execute(text("UPDATE pedidos SET validade = expires_at WHERE expires_at IS NOT NULL"))
                    conn.commit()
                    print("✅ Dados copiados de expires_at para validade")
    
    if 'plano_config' in inspector.get_table_names():
        columns = [c['name'] for c in inspector.get_columns('plano_config')]
        if 'is_lifetime' not in columns:
            with engine.connect() as conn:
                conn.execute(text("ALTER TABLE plano_config ADD COLUMN is_lifetime BOOLEAN DEFAULT FALSE"))
                conn.commit()
                print("✅ Coluna 'is_lifetime' adicionada")
    
    if 'leads' in inspector.get_table_names():
        columns = [c['name'] for c in inspector.get_columns('leads')]
        if 'status' not in columns:
            with engine.connect() as conn:
                conn.execute(text("ALTER TABLE leads ADD COLUMN status VARCHAR(20) DEFAULT 'active'"))
                conn.commit()
                print("✅ Coluna 'status' adicionada")
    
    print("✅ Verificação de colunas concluída")

if __name__ == "__main__":
    init_db()
    forcar_atualizacao_tabelas()
    print("✅ Banco de dados inicializado!")