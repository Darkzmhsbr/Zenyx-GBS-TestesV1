import os
from sqlalchemy import create_engine, Column, Integer, String, Float, DateTime, Boolean, Text, ForeignKey, JSON
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
    
    is_active = Column(Boolean, default=True)
    is_superuser = Column(Boolean, default=False)
    
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    
    # 🆕 NOVOS CAMPOS FINANCEIROS
    pushin_pay_id = Column(String, nullable=True) # ID da conta do membro na Pushin
    taxa_venda = Column(Integer, default=60)      # Taxa em centavos (Padrão: 60)

    # RELACIONAMENTO: Um usuário possui vários bots
    bots = relationship("Bot", back_populates="owner")
    
    # Relacionamentos de Logs e Notificações
    audit_logs = relationship("AuditLog", back_populates="user")
    notifications = relationship("Notification", back_populates="user") # 🔥 ADICIONADO PARA O SISTEMA DE NOTIFICAÇÃO

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
    owner_id = Column(Integer, ForeignKey("users.id"), nullable=True)  # nullable=True para migração
    owner = relationship("User", back_populates="bots")
    
    # --- RELACIONAMENTOS (CASCADE) ---
    planos = relationship("PlanoConfig", back_populates="bot", cascade="all, delete-orphan")
    fluxo = relationship("BotFlow", back_populates="bot", uselist=False, cascade="all, delete-orphan")
    steps = relationship("BotFlowStep", back_populates="bot", cascade="all, delete-orphan")
    admins = relationship("BotAdmin", back_populates="bot", cascade="all, delete-orphan")
    
    # RELACIONAMENTOS PARA EXCLUSÃO AUTOMÁTICA
    pedidos = relationship("Pedido", backref="bot_ref", cascade="all, delete-orphan")
    leads = relationship("Lead", backref="bot_ref", cascade="all, delete-orphan")
    
    # ✅ CORREÇÃO APLICADA AQUI:
    # Mudamos de 'campanhas' para 'remarketing_campaigns' e usamos back_populates="bot"
    # para casar perfeitamente com a nova classe RemarketingCampaign
    remarketing_campaigns = relationship("RemarketingCampaign", back_populates="bot", cascade="all, delete-orphan")
    
    # Relacionamento com Order Bump
    order_bump = relationship("OrderBumpConfig", uselist=False, back_populates="bot", cascade="all, delete-orphan")
    
    # Relacionamento com Tracking (Links pertencem a um bot)
    tracking_links = relationship("TrackingLink", back_populates="bot", cascade="all, delete-orphan")

    # 🔥 Relacionamento com Mini App (Template Personalizável)
    miniapp_config = relationship("MiniAppConfig", uselist=False, back_populates="bot", cascade="all, delete-orphan")
    miniapp_categories = relationship("MiniAppCategory", back_populates="bot", cascade="all, delete-orphan")
    
    # ✅ NOVOS: REMARKETING AUTOMÁTICO
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
    nome_produto = Column(String) # Nome do produto extra
    preco = Column(Float)         # Valor a ser somado
    link_acesso = Column(String, nullable=True) # Link do canal/grupo extra

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
    is_lifetime = Column(Boolean, default=False)  # ← ADICIONAR ESTA LINHA
    key_id = Column(String(100), unique=True)
    created_at = Column(DateTime, default=datetime.utcnow)

    # 👇 ADICIONE ESTA LINHA AQUI 👇
    id_canal_destino = Column(String, nullable=True) 
    # 👆 FIM DA ADIÇÃO 👆
    
    # Relacionamentos (manter tudo que já existe abaixo)
    bot = relationship("Bot", back_populates="planos")

# =========================================================
# 📢 REMARKETING
# =========================================================
class RemarketingCampaign(Base):
    __tablename__ = "remarketing_campaigns"
    
    # Identificação
    id = Column(Integer, primary_key=True, index=True)
    bot_id = Column(Integer, ForeignKey("bots.id"))
    campaign_id = Column(String, unique=True)
    
    # Configuração
    target = Column(String, default="todos")  # 'todos', 'compradores', 'nao_compradores', 'lead'
    type = Column(String, default="massivo")  # 'teste' ou 'massivo'
    config = Column(Text)  # JSON com mensagem, media_url, etc
    
    # Status e Controle
    status = Column(String, default="agendado")  # 'agendado', 'enviando', 'concluido', 'erro'
    
    # Agendamento
    dia_atual = Column(Integer, default=0)
    data_inicio = Column(DateTime, default=datetime.utcnow)
    proxima_execucao = Column(DateTime, nullable=True)
    
    # Oferta Promocional
    plano_id = Column(Integer, nullable=True)
    promo_price = Column(Float, nullable=True)
    expiration_at = Column(DateTime, nullable=True)
    
    # Métricas de Execução
    total_leads = Column(Integer, default=0)
    sent_success = Column(Integer, default=0)
    blocked_count = Column(Integer, default=0)
    data_envio = Column(DateTime, default=datetime.utcnow)
    
    # Relacionamento
    bot = relationship("Bot", back_populates="remarketing_campaigns")

# =========================================================
# 🔄 WEBHOOK RETRY SYSTEM
# =========================================================
class WebhookRetry(Base):
    """
    Rastreia webhooks que falharam e precisam ser reprocessados.
    Implementa exponential backoff automático.
    """
    __tablename__ = "webhook_retry"
    
    id = Column(Integer, primary_key=True, index=True)
    webhook_type = Column(String(50))
    payload = Column(Text)
    attempts = Column(Integer, default=0)
    max_attempts = Column(Integer, default=5)
    next_retry = Column(DateTime, nullable=True)
    status = Column(String(20), default='pending')
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    last_error = Column(Text, nullable=True)
    reference_id = Column(String(100), nullable=True)
    
    def __repr__(self):
        return f"<WebhookRetry(id={self.id}, type={self.webhook_type}, attempts={self.attempts}, status={self.status})>"

# =========================================================
# 💬 FLUXO (ESTRUTURA HÍBRIDA V1 + V2 + MINI APP)
# =========================================================
class BotFlow(Base):
    __tablename__ = "bot_flows"
    id = Column(Integer, primary_key=True, index=True)
    bot_id = Column(Integer, ForeignKey("bots.id"), unique=True)
    bot = relationship("Bot", back_populates="fluxo")
    
    # --- CONFIGURAÇÃO DE MODO DE INÍCIO ---
    start_mode = Column(String, default="padrao") # 'padrao', 'miniapp'
    miniapp_url = Column(String, nullable=True)   # URL da loja externa
    miniapp_btn_text = Column(String, default="🛒 ABRIR LOJA")
    
    # --- MENSAGEM 1 (BOAS-VINDAS) ---
    msg_boas_vindas = Column(Text, default="Olá! Bem-vindo(a)!")
    media_url = Column(String, nullable=True)
    btn_text_1 = Column(String, default="📋 Ver Planos")
    autodestruir_1 = Column(Boolean, default=False)
    mostrar_planos_1 = Column(Boolean, default=True)

    # 🔥 [NOVO] Configuração Avançada de Botões (JSON)
    # Ex: [{"type": "plan", "plan_id": 1}, {"type": "link", "text": "Canal Free", "url": "..."}]
    buttons_config = Column(JSON, nullable=True)
    
    # 🔥 [NOVO] Modo de botão da mensagem 1
    # Valores: "next_step" (botão próximo passo) ou "custom" (botões personalizados)
    button_mode = Column(String, default="next_step")
    
    # --- MENSAGEM 2 (SEGUNDO PASSO) ---
    msg_2_texto = Column(Text, nullable=True)
    msg_2_media = Column(String, nullable=True)
    mostrar_planos_2 = Column(Boolean, default=False)

    # 🔥 [NOVO] Configuração Avançada de Botões (JSON) - MENSAGEM 2 (OFERTA FINAL)
    buttons_config_2 = Column(JSON, nullable=True)

    # --- MENSAGEM PIX (PERSONALIZADA) ---
    msg_pix = Column(Text, nullable=True)

# =========================================================
# 🧩 TABELA DE PASSOS INTERMEDIÁRIOS
# =========================================================
class BotFlowStep(Base):
    __tablename__ = "bot_flow_steps"
    id = Column(Integer, primary_key=True, index=True)
    bot_id = Column(Integer, ForeignKey("bots.id"))
    step_order = Column(Integer, default=1)
    msg_texto = Column(Text, nullable=True)
    msg_media = Column(String, nullable=True)
    btn_texto = Column(String, default="Próximo ▶️")

    # 🔥 [NOVO] Configuração Avançada de Botões para Passos Extras
    buttons_config = Column(JSON, nullable=True)
    
    # Controles de comportamento
    autodestruir = Column(Boolean, default=False)
    mostrar_botao = Column(Boolean, default=True)
    
    # Temporizador entre mensagens
    delay_seconds = Column(Integer, default=0)
    
    created_at = Column(DateTime, default=datetime.utcnow)
    bot = relationship("Bot", back_populates="steps")

# =========================================================
# 🔗 TRACKING (RASTREAMENTO DE LINKS)
# =========================================================
class TrackingFolder(Base):
    __tablename__ = "tracking_folders"
    id = Column(Integer, primary_key=True, index=True)
    nome = Column(String)      # Ex: "Facebook Ads"
    plataforma = Column(String) # Ex: "facebook", "instagram"
    created_at = Column(DateTime, default=datetime.utcnow)
    
    links = relationship("TrackingLink", back_populates="folder", cascade="all, delete-orphan")

class TrackingLink(Base):
    __tablename__ = "tracking_links"
    id = Column(Integer, primary_key=True, index=True)
    folder_id = Column(Integer, ForeignKey("tracking_folders.id"))
    bot_id = Column(Integer, ForeignKey("bots.id"))
    
    nome = Column(String)      # Ex: "Stories Manhã"
    codigo = Column(String, unique=True, index=True) # Ex: "xyz123" (o parâmetro do /start)
    origem = Column(String, default="outros") # Ex: "story", "reels", "feed"
    
    # Métricas
    clicks = Column(Integer, default=0)
    leads = Column(Integer, default=0)
    vendas = Column(Integer, default=0)
    faturamento = Column(Float, default=0.0)
    
    created_at = Column(DateTime, default=datetime.utcnow)
    
    folder = relationship("TrackingFolder", back_populates="links")
    bot = relationship("Bot", back_populates="tracking_links")

# =========================================================
# 🛒 PEDIDOS
# =========================================================
class Pedido(Base):
    __tablename__ = "pedidos"
    id = Column(Integer, primary_key=True, index=True)
    bot_id = Column(Integer, ForeignKey("bots.id"))
    
    telegram_id = Column(String)
    first_name = Column(String, nullable=True)
    username = Column(String, nullable=True)
    
    plano_nome = Column(String, nullable=True)
    plano_id = Column(Integer, nullable=True)
    valor = Column(Float)
    status = Column(String, default="pending") 
    
    txid = Column(String, unique=True, index=True) 
    qr_code = Column(Text, nullable=True)
    transaction_id = Column(String, nullable=True)
    
    data_aprovacao = Column(DateTime, nullable=True)
    data_expiracao = Column(DateTime, nullable=True)
    custom_expiration = Column(DateTime, nullable=True)
    
    link_acesso = Column(String, nullable=True)
    mensagem_enviada = Column(Boolean, default=False)
    
    created_at = Column(DateTime, default=datetime.utcnow)
    
    # Campo para identificar se comprou o Order Bump
    tem_order_bump = Column(Boolean, default=False)
    
    # --- CAMPOS FUNIL & TRACKING ---
    status_funil = Column(String(20), default='meio')
    funil_stage = Column(String(20), default='lead_quente')
    
    primeiro_contato = Column(DateTime(timezone=True))
    escolheu_plano_em = Column(DateTime(timezone=True))
    gerou_pix_em = Column(DateTime(timezone=True))
    pagou_em = Column(DateTime(timezone=True))
    
    dias_ate_compra = Column(Integer, default=0)
    ultimo_remarketing = Column(DateTime(timezone=True))
    total_remarketings = Column(Integer, default=0)
    
    origem = Column(String(50), default='bot')
    
    # Rastreamento
    tracking_id = Column(Integer, ForeignKey("tracking_links.id"), nullable=True)


# =========================================================
# 🎯 TABELA: LEADS (TOPO DO FUNIL)
# =========================================================
class Lead(Base):
    __tablename__ = "leads"
    
    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(String, nullable=False)  # Telegram ID
    nome = Column(String, nullable=True)
    username = Column(String, nullable=True)
    phone = Column(String, nullable=True)     # Telefone (Adicionado para evitar erro na API)
    bot_id = Column(Integer, ForeignKey('bots.id'))
    
    # Classificação
    status = Column(String(20), default='topo')
    funil_stage = Column(String(20), default='lead_frio')
    
    # Timestamps
    primeiro_contato = Column(DateTime(timezone=True), server_default=func.now())
    ultimo_contato = Column(DateTime(timezone=True))
    
    # Métricas
    total_remarketings = Column(Integer, default=0)
    ultimo_remarketing = Column(DateTime(timezone=True))
    
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    
    # Rastreamento
    tracking_id = Column(Integer, ForeignKey("tracking_links.id"), nullable=True)
    
    # 🔥 CAMPO NOVO (CORREÇÃO DO VITALÍCIO/ERRO 500)
    expiration_date = Column(DateTime, nullable=True)

    # No arquivo database.py, dentro de class Lead(Base):

    # Substitua a linha antiga do relationship por esta:
    bot = relationship("Bot", back_populates="leads", overlaps="bot_ref")
    # Se TrackingLink tiver back_populates="leads", descomente abaixo:
    # tracking_link = relationship("TrackingLink", back_populates="leads")

# =========================================================
# 📱 MINI APP (TEMPLATE PERSONALIZÁVEL)
# =========================================================

# 1. Configuração Visual Global
class MiniAppConfig(Base):
    __tablename__ = "miniapp_config"
    bot_id = Column(Integer, ForeignKey("bots.id"), primary_key=True)
    
    # Visual Base
    logo_url = Column(String, nullable=True)
    background_type = Column(String, default="solid") # 'solid', 'gradient', 'image'
    background_value = Column(String, default="#000000") # Hex ou URL
    
    # Hero Section (Vídeo Topo)
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

# 2. Categorias e Conteúdo
class MiniAppCategory(Base):
    __tablename__ = "miniapp_categories"
    id = Column(Integer, primary_key=True, index=True)
    bot_id = Column(Integer, ForeignKey("bots.id"))
    slug = Column(String)
    title = Column(String)
    description = Column(String)
    cover_image = Column(String) # cardImg
    banner_mob_url = Column(String)
    
    # --- NOVOS CAMPOS VISUAL RICO ---
    bg_color = Column(String, default="#000000")
    banner_desk_url = Column(String, nullable=True)
    video_preview_url = Column(String, nullable=True)
    model_img_url = Column(String, nullable=True)
    model_name = Column(String, nullable=True)
    model_desc = Column(String, nullable=True)
    footer_banner_url = Column(String, nullable=True)
    deco_lines_url = Column(String, nullable=True)
    
    # NOVAS CORES DE TEXTO
    model_name_color = Column(String, default="#ffffff")
    model_desc_color = Column(String, default="#cccccc")
    # --------------------
    
    theme_color = Column(String, default="#c333ff")
    is_direct_checkout = Column(Boolean, default=False)
    is_hacker_mode = Column(Boolean, default=False)
    content_json = Column(Text)
    
    bot = relationship("Bot", back_populates="miniapp_categories")

# =========================================================
# 📋 AUDIT LOGS (FASE 3.3 - AUDITORIA)
# =========================================================
class AuditLog(Base):
    __tablename__ = "audit_logs"
    
    id = Column(Integer, primary_key=True, index=True)
    
    # 👤 Quem fez a ação
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    username = Column(String, nullable=False)  # Denormalizado para performance
    
    # 🎯 O que foi feito
    action = Column(String(50), nullable=False, index=True)  # Ex: "bot_created", "login_success"
    resource_type = Column(String(50), nullable=False, index=True)  # Ex: "bot", "plano", "auth"
    resource_id = Column(Integer, nullable=True)  # ID do recurso afetado
    
    # 📝 Detalhes
    description = Column(Text, nullable=True)  # Descrição legível para humanos
    details = Column(Text, nullable=True)  # JSON com dados extras
    
    # 🌐 Contexto
    ip_address = Column(String(50), nullable=True)
    user_agent = Column(Text, nullable=True)
    
    # ✅ Status
    success = Column(Boolean, default=True, index=True)
    error_message = Column(Text, nullable=True)
    
    # 🕒 Timestamp
    created_at = Column(DateTime, default=datetime.utcnow, index=True)
    
    # Relacionamento
    user = relationship("User", back_populates="audit_logs")

# =========================================================
# 🔔 NOTIFICAÇÕES REAIS (NOVA TABELA - ATUALIZAÇÃO)
# =========================================================
class Notification(Base):
    __tablename__ = "notifications"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), index=True)
    
    title = Column(String, nullable=False)       # Ex: "Venda Aprovada"
    message = Column(String, nullable=False)     # Ex: "João comprou Plano VIP"
    type = Column(String, default="info")        # info, success, warning, error
    read = Column(Boolean, default=False)        # Se o usuário já leu
    
    created_at = Column(DateTime, default=datetime.utcnow)

    # Relacionamento com Usuário
    user = relationship("User", back_populates="notifications")

# =========================================================
# 🎯 REMARKETING AUTOMÁTICO
# =========================================================
class RemarketingConfig(Base):
    """
    Configuração de remarketing automático por bot.
    Define quando e como enviar mensagens de reengajamento.
    """
    __tablename__ = "remarketing_config"
    
    id = Column(Integer, primary_key=True, index=True)
    bot_id = Column(Integer, ForeignKey('bots.id', ondelete='CASCADE'), nullable=False, index=True)
    
    # Controle
    is_active = Column(Boolean, default=True, index=True)
    
    # Conteúdo
    message_text = Column(Text, nullable=False)
    media_url = Column(String(500), nullable=True)
    media_type = Column(String(10), nullable=True)  # 'photo', 'video', None
    
    # Timing
    delay_minutes = Column(Integer, default=5)
    
    # ✅ NOVO: Auto-destruição OPCIONAL
    auto_destruct_enabled = Column(Boolean, default=False)  # Se ativado, destrói a mensagem
    auto_destruct_seconds = Column(Integer, default=3)  # Só é usado se enabled=True
    auto_destruct_after_click = Column(Boolean, default=True)  # Se True, só destrói APÓS clicar no botão
    
    # Valores Promocionais (JSON)
    promo_values = Column(JSON, default={})  # {plano_id: valor_promo}
    
    # Auditoria
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    
    bot = relationship("Bot", back_populates="remarketing_config")
    
    def __repr__(self):
        return f"<RemarketingConfig(bot_id={self.bot_id}, active={self.is_active}, delay={self.delay_minutes}min)>"


class AlternatingMessages(Base):
    """
    Mensagens que alternam durante o período de espera antes do remarketing.
    """
    __tablename__ = "alternating_messages"
    
    id = Column(Integer, primary_key=True, index=True)
    bot_id = Column(Integer, ForeignKey('bots.id', ondelete='CASCADE'), nullable=False, index=True)
    
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
    
    def log_config_values(self):
        """Log de debug para verificar valores salvos"""
        return {
            "is_active": self.is_active,
            "messages_count": len(self.messages) if self.messages else 0,
            "rotation_interval_seconds": self.rotation_interval_seconds,
            "stop_before_remarketing_seconds": self.stop_before_remarketing_seconds,
            "auto_destruct_final": self.auto_destruct_final,
            "max_duration_minutes": getattr(self, 'max_duration_minutes', 60)
        }


class RemarketingLog(Base):
    """
    Log de remarketing enviados para analytics e controle de duplicação.
    """
    __tablename__ = "remarketing_logs"
    
    id = Column(Integer, primary_key=True, index=True)
    bot_id = Column(Integer, ForeignKey('bots.id'), nullable=False, index=True)
    user_id = Column(String, nullable=False, index=True)
    
    # Dados do envio
    sent_at = Column(DateTime, default=datetime.utcnow, index=True)
    
    # ✅ CORREÇÃO: Apenas UM campo message_sent (tipo TEXT)
    message_sent = Column(Text, nullable=True)
    
    # Valores promocionais
    promo_values = Column(JSON, nullable=True)
    
    # Status do envio
    status = Column(String(20), default='sent', index=True)  # sent, error, paid
    error_message = Column(Text, nullable=True)
    
    # Conversão
    converted = Column(Boolean, default=False, index=True)
    converted_at = Column(DateTime, nullable=True)
    
    # Campaign tracking
    campaign_id = Column(String, nullable=True)
    
    # Relacionamento
    bot = relationship("Bot", back_populates="remarketing_logs")
    
    def __repr__(self):
        return f"<RemarketingLog(bot_id={self.bot_id}, user_id={self.user_id}, status={self.status})>"