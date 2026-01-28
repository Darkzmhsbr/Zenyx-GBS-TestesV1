import os
from sqlalchemy import create_engine, Column, Integer, String, Float, DateTime, Boolean, Text, ForeignKey, JSON, text
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker, relationship
from sqlalchemy.pool import QueuePool
from sqlalchemy.sql import func
from datetime import datetime

DATABASE_URL = os.getenv("DATABASE_URL")

# Ajuste para compatibilidade com Railway (postgres -> postgresql)
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
    
    # ✅ CAMPOS ESSENCIAIS RESTAURADOS
    is_active = Column(Boolean, default=True)
    is_superuser = Column(Boolean, default=False)
    
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    
    # 🆕 CAMPOS FINANCEIROS
    pushin_pay_id = Column(String, nullable=True)
    taxa_venda = Column(Integer, default=60)  # ✅ TIPO CORRETO: Integer (centavos)

    # ✅ RELACIONAMENTOS COMPLETOS
    bots = relationship("Bot", back_populates="owner")
    audit_logs = relationship("AuditLog", back_populates="user")
    notifications = relationship("Notification", back_populates="user")

# =========================================================
# ⚙️ CONFIGURAÇÕES GERAIS
# =========================================================
class SystemConfig(Base):
    __tablename__ = "system_config"
    key = Column(String, primary_key=True, index=True) 
    value = Column(String)
    updated_at = Column(DateTime, default=datetime.utcnow)

# =========================================================
# 🤖 BOTS
# =========================================================
class Bot(Base):
    __tablename__ = "bots"

    id = Column(Integer, primary_key=True, index=True)
    nome = Column(String)
    token = Column(String, unique=True, index=True)
    username = Column(String, nullable=True)
    id_canal_vip = Column(String)
    admin_principal_id = Column(String, nullable=True)
    
    # 🔥 Username do Suporte
    suporte_username = Column(String, nullable=True)
    
    status = Column(String, default="ativo")
    
    # Token Individual por Bot
    pushin_token = Column(String, nullable=True) 

    created_at = Column(DateTime, default=datetime.utcnow)
    
    # 🆕 RELACIONAMENTO COM USUÁRIO (OWNER)
    owner_id = Column(Integer, ForeignKey("users.id"), nullable=True)
    owner = relationship("User", back_populates="bots")
    
    # --- RELACIONAMENTOS (CASCADE) ---
    planos = relationship("PlanoConfig", back_populates="bot", cascade="all, delete-orphan")
    fluxo = relationship("BotFlow", back_populates="bot", uselist=False, cascade="all, delete-orphan")
    steps = relationship("BotFlowStep", back_populates="bot", cascade="all, delete-orphan")
    admins = relationship("BotAdmin", back_populates="bot", cascade="all, delete-orphan")
    
    # RELACIONAMENTOS PARA EXCLUSÃO AUTOMÁTICA
    pedidos = relationship("Pedido", backref="bot_ref", cascade="all, delete-orphan")
    leads = relationship("Lead", backref="bot_ref", cascade="all, delete-orphan")
    
    # ✅ RELACIONAMENTO COM REMARKETING
    remarketing_campaigns = relationship("RemarketingCampaign", back_populates="bot", cascade="all, delete-orphan")
    
    # Relacionamento com Order Bump
    order_bump = relationship("OrderBumpConfig", uselist=False, back_populates="bot", cascade="all, delete-orphan")
    
    # Relacionamento com Tracking
    tracking_links = relationship("TrackingLink", back_populates="bot", cascade="all, delete-orphan")

    # 🔥 Relacionamento com Mini App
    miniapp_config = relationship("MiniAppConfig", uselist=False, back_populates="bot", cascade="all, delete-orphan")
    miniapp_categories = relationship("MiniAppCategory", back_populates="bot", cascade="all, delete-orphan")
    
    # ✅ NOVOS RELACIONAMENTOS PARA REMARKETING AUTOMÁTICO
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

# =========================================================
# 🛒 ORDER BUMP (OFERTA EXTRA NO CHECKOUT)
# =========================================================
class OrderBumpConfig(Base):
    __tablename__ = "order_bump_config"
    id = Column(Integer, primary_key=True, index=True)
    bot_id = Column(Integer, ForeignKey("bots.id"), unique=True)
    
    ativo = Column(Boolean, default=False)
    nome_produto = Column(String)
    preco = Column(Float)
    link_acesso = Column(String, nullable=True)

    autodestruir = Column(Boolean, default=False)
    
    # Conteúdo da Oferta
    msg_texto = Column(Text, default="Gostaria de adicionar este item?")
    msg_media = Column(String, nullable=True)
    
    # Botões
    btn_aceitar = Column(String, default="✅ SIM, ADICIONAR")
    btn_recusar = Column(String, default="❌ NÃO, OBRIGADO")
    
    bot = relationship("Bot", back_populates="order_bump")

# =========================================================
# 💲 PLANOS
# =========================================================
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

# =========================================================
# 📢 REMARKETING - CAMPANHAS (SISTEMA ANTIGO)
# =========================================================
class RemarketingCampaign(Base):
    __tablename__ = "remarketing_campaigns"
    
    # Identificação
    id = Column(Integer, primary_key=True, index=True)
    bot_id = Column(Integer, ForeignKey("bots.id"))
    campaign_id = Column(String, unique=True)
    
    # Configuração
    target = Column(String, default="todos")
    type = Column(String, default="massivo")
    config = Column(Text)
    
    # Status e Controle
    status = Column(String, default="agendado")
    
    # Agendamento
    dia_atual = Column(Integer, default=0)
    data_inicio = Column(DateTime, default=datetime.utcnow)
    proxima_execucao = Column(DateTime, nullable=True)
    
    # Estatísticas
    total_enviados = Column(Integer, default=0)
    total_erros = Column(Integer, default=0)
    total_conversoes = Column(Integer, default=0)
    
    created_at = Column(DateTime, default=datetime.utcnow)
    
    bot = relationship("Bot", back_populates="remarketing_campaigns")

# =========================================================
# 🛒 PEDIDOS - ⚠️ MANTENDO expires_at TEMPORARIAMENTE
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
    paid_at = Column(DateTime, nullable=True)
    
    # ⚠️ MANTENDO expires_at TEMPORARIAMENTE ATÉ EXECUTAR SQL
    expires_at = Column(DateTime, nullable=True)
    
    # ✅ ADICIONANDO validade TAMBÉM (para compatibilidade futura)
    validade = Column(DateTime, nullable=True)
    
    tracking_id = Column(String, nullable=True)
# =========================================================
# 👥 LEADS
# =========================================================
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

# =========================================================
# 📊 TRACKING
# =========================================================
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

# =========================================================
# 💬 FLUXO DE CONVERSA
# =========================================================
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

# =========================================================
# 🎨 MINI APP (TEMPLATE PERSONALIZÁVEL)
# =========================================================
class MiniAppConfig(Base):
    __tablename__ = "miniapp_config"
    bot_id = Column(Integer, ForeignKey("bots.id"), primary_key=True)
    
    # Visual Base
    logo_url = Column(String, nullable=True)
    background_type = Column(String, default="solid")
    background_value = Column(String, default="#000000")
    
    # Hero Section
    hero_video_url = Column(String, nullable=True)
    hero_title = Column(String, default="ACERVO PREMIUM")
    hero_subtitle = Column(String, default="O maior acervo da internet.")
    hero_btn_text = Column(String, default="LIBERAR CONTEÚDO 🔓")
    
    # Popup Promocional
    enable_popup = Column(Boolean, default=False)
    popup_video_url = Column(String, nullable=True)
    popup_text = Column(String, default="VOCÊ GANHOU UM PRESENTE!")
    
    # Rodapé
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
    
    # Visual Rico
    bg_color = Column(String, default="#000000")
    banner_desk_url = Column(String, nullable=True)
    video_preview_url = Column(String, nullable=True)
    model_img_url = Column(String, nullable=True)
    model_name = Column(String, nullable=True)
    model_desc = Column(String, nullable=True)
    footer_banner_url = Column(String, nullable=True)
    deco_lines_url = Column(String, nullable=True)
    
    # Cores de Texto
    model_name_color = Column(String, default="#ffffff")
    model_desc_color = Column(String, default="#cccccc")
    
    theme_color = Column(String, default="#c333ff")
    is_direct_checkout = Column(Boolean, default=False)
    is_hacker_mode = Column(Boolean, default=False)
    content_json = Column(Text)
    
    bot = relationship("Bot", back_populates="miniapp_categories")

# =========================================================
# 📋 AUDIT LOGS (AUDITORIA)
# =========================================================
class AuditLog(Base):
    __tablename__ = "audit_logs"
    
    id = Column(Integer, primary_key=True, index=True)
    
    # Quem fez a ação
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    username = Column(String, nullable=False)
    
    # O que foi feito
    action = Column(String(50), nullable=False, index=True)
    resource_type = Column(String(50), nullable=False, index=True)
    resource_id = Column(Integer, nullable=True)
    
    # Detalhes
    description = Column(Text, nullable=True)
    details = Column(Text, nullable=True)
    
    # Contexto
    ip_address = Column(String(50), nullable=True)
    user_agent = Column(Text, nullable=True)
    
    # Status
    success = Column(Boolean, default=True, index=True)
    error_message = Column(Text, nullable=True)
    
    # Timestamp
    created_at = Column(DateTime, default=datetime.utcnow, index=True)
    
    user = relationship("User", back_populates="audit_logs")

# =========================================================
# 🔔 NOTIFICAÇÕES
# =========================================================
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

# =========================================================
# 🔄 WEBHOOK RETRY SYSTEM
# =========================================================
class WebhookRetry(Base):
    __tablename__ = "webhook_retries"
    
    id = Column(Integer, primary_key=True, index=True)
    bot_id = Column(Integer, ForeignKey("bots.id"), nullable=False, index=True)
    
    # Dados da requisição
    webhook_url = Column(String(500), nullable=False)
    payload = Column(JSON, nullable=False)
    headers = Column(JSON, nullable=True)
    
    # Controle de tentativas
    attempt_count = Column(Integer, default=0)
    max_attempts = Column(Integer, default=5)
    next_retry_at = Column(DateTime, nullable=True, index=True)
    
    # Status
    status = Column(String(20), default="pending", index=True)
    last_error = Column(Text, nullable=True)
    completed_at = Column(DateTime, nullable=True)
    
    # Timestamps
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

# =========================================================
# 🎯 REMARKETING AUTOMÁTICO - CONFIGURAÇÃO
# =========================================================
class RemarketingConfig(Base):
    """
    Configuração de remarketing automático e mensagens alternantes.
    Controla o disparo de ofertas promocionais após período de inatividade.
    """
    __tablename__ = "remarketing_config"
    
    id = Column(Integer, primary_key=True, index=True)
    bot_id = Column(Integer, ForeignKey('bots.id', ondelete='CASCADE'), nullable=False, unique=True, index=True)
    
    # ========== DISPARO AUTOMÁTICO ==========
    is_active = Column(Boolean, default=True, index=True)
    
    # Conteúdo
    message_text = Column(Text, nullable=True)
    media_url = Column(String(500), nullable=True)
    media_type = Column(String(10), nullable=True)  # 'photo', 'video', None
    
    # Timing
    delay_minutes = Column(Integer, default=5)
    auto_destruct_seconds = Column(Integer, default=0)  # 0 = não destrói
    
    # Valores Promocionais (JSON)
    promo_values = Column(JSON, default={})  # {plano_id: valor_promo}
    
    # ========== MENSAGENS ALTERNANTES ==========
    alternating_enabled = Column(Boolean, default=False)
    
    # Auditoria
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    
    bot = relationship("Bot", back_populates="remarketing_config")
    
    def __repr__(self):
        return f"<RemarketingConfig(bot_id={self.bot_id}, active={self.is_active}, delay={self.delay_minutes}min)>"


class AlternatingMessages(Base):
    """
    Mensagens que alternam durante o período de espera antes do remarketing.
    Mantém o usuário engajado enquanto aguarda a oferta final.
    """
    __tablename__ = "alternating_messages"
    
    id = Column(Integer, primary_key=True, index=True)
    bot_id = Column(Integer, ForeignKey('bots.id', ondelete='CASCADE'), nullable=False, unique=True, index=True)
    
    # Controle
    is_active = Column(Boolean, default=False, index=True)
    
    # Mensagens (Array de strings via JSON)
    messages = Column(JSON, default=[])  # ["msg1", "msg2", "msg3"]
    
    # Timing
    rotation_interval_seconds = Column(Integer, default=15)
    stop_before_remarketing_seconds = Column(Integer, default=60)
    auto_destruct_final = Column(Boolean, default=False)
    
    # Auditoria
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    
    bot = relationship("Bot", back_populates="alternating_messages")
    
    def __repr__(self):
        return f"<AlternatingMessages(bot_id={self.bot_id}, active={self.is_active}, msgs={len(self.messages)})>"


class RemarketingLog(Base):
    """
    Log de remarketing enviados para analytics e controle de duplicação.
    Rastreia todos os disparos automáticos e suas conversões.
    """
    __tablename__ = "remarketing_logs"
    
    id = Column(Integer, primary_key=True, index=True)
    bot_id = Column(Integer, ForeignKey('bots.id', ondelete='CASCADE'), nullable=False, index=True)
    user_telegram_id = Column(Integer, nullable=False, index=True)
    
    # Dados do envio
    sent_at = Column(DateTime, default=datetime.utcnow, index=True)
    message_text = Column(Text, nullable=True)
    promo_values = Column(JSON, nullable=True)
    
    # Status
    status = Column(String(20), default='sent', index=True)  # sent, error, paid
    error_message = Column(Text, nullable=True)
    
    # Conversão
    converted = Column(Boolean, default=False, index=True)
    converted_at = Column(DateTime, nullable=True)
    
    bot = relationship("Bot", back_populates="remarketing_logs")
    
    def __repr__(self):
        return f"<RemarketingLog(bot_id={self.bot_id}, user={self.user_telegram_id}, status={self.status})>"

# =========================================================
# 🔧 FUNÇÃO DE MIGRAÇÃO AUTOMÁTICA
# =========================================================
def forcar_atualizacao_tabelas():
    """
    Adiciona colunas faltantes em tabelas existentes sem quebrar dados.
    """
    from sqlalchemy import inspect
    
    inspector = inspect(engine)
    
    # Verificar e adicionar colunas em plano_config
    if 'plano_config' in inspector.get_table_names():
        columns = [c['name'] for c in inspector.get_columns('plano_config')]
        if 'is_lifetime' not in columns:
            with engine.connect() as conn:
                conn.execute(text("ALTER TABLE plano_config ADD COLUMN is_lifetime BOOLEAN DEFAULT FALSE"))
                conn.commit()
                print("✅ Coluna 'is_lifetime' adicionada em plano_config")
    
    # Verificar e adicionar colunas em remarketing_config
    if 'remarketing_config' in inspector.get_table_names():
        columns = [c['name'] for c in inspector.get_columns('remarketing_config')]
        
        if 'alternating_enabled' not in columns:
            with engine.connect() as conn:
                conn.execute(text("ALTER TABLE remarketing_config ADD COLUMN alternating_enabled BOOLEAN DEFAULT FALSE"))
                conn.commit()
                print("✅ Coluna 'alternating_enabled' adicionada")
    
    # Verificar e adicionar colunas em leads
    if 'leads' in inspector.get_table_names():
        columns = [c['name'] for c in inspector.get_columns('leads')]
        if 'status' not in columns:
            with engine.connect() as conn:
                conn.execute(text("ALTER TABLE leads ADD COLUMN status VARCHAR(20) DEFAULT 'active'"))
                conn.commit()
                print("✅ Coluna 'status' adicionada em leads")
    
    print("✅ Verificação de colunas concluída")

# =========================================================
# 🚀 INICIALIZAÇÃO
# =========================================================
if __name__ == "__main__":
    init_db()
    forcar_atualizacao_tabelas()
    print("✅ Banco de dados inicializado com sucesso!")