import os
from sqlalchemy import create_engine, Column, Integer, String, Float, DateTime, Boolean, Text, ForeignKey, JSON
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker, relationship
from sqlalchemy.pool import QueuePool
from sqlalchemy.sql import func
from datetime import datetime
from pytz import timezone

# 🔥 CONFIGURAÇÃO DE FUSO HORÁRIO - BRASÍLIA/SÃO PAULO
BRAZIL_TZ = timezone('America/Sao_Paulo')

def now_brazil():
    """Retorna datetime atual no horário de Brasília/São Paulo"""
    return datetime.now(BRAZIL_TZ)

DATABASE_URL = os.getenv("DATABASE_URL")

# Ajuste para compatibilidade com Railway (postgres -> postgresql)
if DATABASE_URL and DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql://", 1)

if DATABASE_URL:
    engine = create_engine(
        DATABASE_URL,
        poolclass=QueuePool,
        pool_size=5,            # 🔧 REDUZIDO: De 10 para 5 (Railway tem limite de threads/conexões)
        max_overflow=10,        # 🔧 REDUZIDO: De 20 para 10 (total máx: 15 conexões)
        pool_timeout=20,        # 🔧 REDUZIDO: De 30 para 20s (fail-fast em vez de travar)
        pool_recycle=180,       # 🔧 REDUZIDO: De 300 para 180s (Railway dropa conexões idle rápido)
        pool_pre_ping=True      # 🔧 CRÍTICO: Testa conexão antes de usar (evita "connection closed")
    )
else:
    engine = create_engine("sqlite:///./sql_app.db")

SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine, expire_on_commit=False)
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
    
    created_at = Column(DateTime, default=now_brazil)
    updated_at = Column(DateTime, default=now_brazil, onupdate=now_brazil)
    
    # 🆕 CAMPOS FINANCEIROS (MULTI-GATEWAY)
    pushin_pay_id = Column(String, nullable=True)   # ID da conta do membro na PushinPay
    wiinpay_user_id = Column(String, nullable=True)  # ID da conta do membro na WiinPay
    
    # 🆕 MULTI-GATEWAY: Sync Pay (ID para Taxa de Split)
    syncpay_client_id = Column(String, nullable=True) # ID da conta do membro na Sync Pay
    
    taxa_venda = Column(Integer, default=60)         # Taxa em centavos (Padrão: 60)

    # 🆕 SISTEMA DE LIMITES DE BOTS
    plano_plataforma = Column(String, default="free")  # 'free', 'vip', 'enterprise'
    max_bots = Column(Integer, default=20)             # Limite de bots (Free=20, VIP=100, Enterprise=ilimitado)

    # 🚨 SISTEMA DE PUNIÇÕES (DENÚNCIAS)
    is_banned = Column(Boolean, default=False)
    banned_reason = Column(String, nullable=True)
    bots_paused_until = Column(DateTime(timezone=True), nullable=True)
    strike_count = Column(Integer, default=0)

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
    updated_at = Column(DateTime, default=now_brazil)

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
    
    # ✅ Canal de Notificações (onde o bot envia avisos de vendas, toggle, etc)
    id_canal_notificacao = Column(String, nullable=True)
    notificar_no_bot = Column(Boolean, default=True)  # 🔥 NOVO: Toggle notificação no bot
    
    status = Column(String, default="ativo")
    
    # Token Individual por Bot (PUSHIN PAY)
    pushin_token = Column(String, nullable=True) 
    
    # 🆕 MULTI-GATEWAY: WiinPay
    wiinpay_api_key = Column(String, nullable=True)   # Chave API do usuário na WiinPay
    
    # 🆕 MULTI-GATEWAY: Sync Pay
    syncpay_client_id = Column(String, nullable=True)     # Chave Pública
    syncpay_client_secret = Column(String, nullable=True) # Chave Privada
    syncpay_access_token = Column(String, nullable=True)  # Token temporário de 1h
    syncpay_token_expires_at = Column(DateTime, nullable=True) # Validade do Token
    
    # 🆕 MULTI-GATEWAY: Controle de Gateways
    gateway_principal = Column(String, default="pushinpay")  # "pushinpay", "wiinpay" ou "syncpay"
    gateway_fallback = Column(String, nullable=True)          # Gateway de contingência
    pushinpay_ativo = Column(Boolean, default=False)          # Gateway PushinPay ativa para este bot
    wiinpay_ativo = Column(Boolean, default=False)            # Gateway WiinPay ativa para este bot
    syncpay_ativo = Column(Boolean, default=False)            # Gateway Sync Pay ativa para este bot

    # 🔒 PROTEÇÃO DE CONTEÚDO (Telegram protect_content)
    # Quando ativo, todas as mídias e mensagens enviadas pelo bot ficam protegidas:
    # - Não podem ser encaminhadas
    # - Não podem ser salvas/baixadas
    # - Texto não pode ser copiado
    protect_content = Column(Boolean, default=False)

    created_at = Column(DateTime, default=now_brazil)
    
    # 🆕 ORDEM NO SELETOR DE BOTS (drag-and-drop)
    selector_order = Column(Integer, default=0)  # 0 = sem ordem definida, menor = aparece primeiro
    
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
    
    # 🆕 Relacionamentos com Upsell e Downsell
    upsell_config = relationship("UpsellConfig", uselist=False, back_populates="bot", cascade="all, delete-orphan")
    downsell_config = relationship("DownsellConfig", uselist=False, back_populates="bot", cascade="all, delete-orphan")
    
    # Relacionamento com Tracking (Links pertencem a um bot)
    tracking_links = relationship("TrackingLink", back_populates="bot", cascade="all, delete-orphan")

    # 🔥 Relacionamento com Mini App (Template Personalizável)
    miniapp_config = relationship("MiniAppConfig", uselist=False, back_populates="bot", cascade="all, delete-orphan")
    miniapp_categories = relationship("MiniAppCategory", back_populates="bot", cascade="all, delete-orphan")
    
    # ✅ NOVOS: REMARKETING AUTOMÁTICO
    remarketing_config = relationship("RemarketingConfig", uselist=False, back_populates="bot", cascade="all, delete-orphan")
    alternating_messages = relationship("AlternatingMessages", uselist=False, back_populates="bot", cascade="all, delete-orphan")
    remarketing_logs = relationship("RemarketingLog", back_populates="bot", cascade="all, delete-orphan")
    
    # ✅ NOVO: GRUPOS E CANAIS (ESTEIRA DE PRODUTOS)
    bot_groups = relationship("BotGroup", back_populates="bot", cascade="all, delete-orphan")

class BotAdmin(Base):
    __tablename__ = "bot_admins"
    id = Column(Integer, primary_key=True, index=True)
    bot_id = Column(Integer, ForeignKey("bots.id"))
    telegram_id = Column(String)
    nome = Column(String, nullable=True)
    created_at = Column(DateTime, default=now_brazil)
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

    # ✅ FASE 2: Referência ao catálogo de Grupos e Canais
    group_id = Column(Integer, ForeignKey('bot_groups.id', ondelete='SET NULL'), nullable=True)

    autodestruir = Column(Boolean, default=False)
    
    # Conteúdo da Oferta
    msg_texto = Column(Text, default="Gostaria de adicionar este item?")
    msg_media = Column(String, nullable=True)
    
    # 🔊 ÁUDIO SEPARADO (Combo: áudio + mídia)
    audio_url = Column(String, nullable=True)           # URL do áudio OGG separado
    audio_delay_seconds = Column(Integer, default=3)    # Delay entre áudio e mídia+texto
    
    # Botões
    btn_aceitar = Column(String, default="✅ SIM, ADICIONAR")
    btn_recusar = Column(String, default="❌ NÃO, OBRIGADO")
    
    bot = relationship("Bot", back_populates="order_bump")

# =========================================================
# 🚀 UPSELL (OFERTA PÓS-COMPRA #1)
# =========================================================
class UpsellConfig(Base):
    __tablename__ = "upsell_config"
    id = Column(Integer, primary_key=True, index=True)
    bot_id = Column(Integer, ForeignKey("bots.id"), unique=True)
    
    ativo = Column(Boolean, default=False)
    nome_produto = Column(String)
    preco = Column(Float)
    link_acesso = Column(String, nullable=True)
    
    # ✅ FASE 2: Referência ao catálogo de Grupos e Canais
    group_id = Column(Integer, ForeignKey('bot_groups.id', ondelete='SET NULL'), nullable=True)
    
    # Delay em minutos após pagamento do plano principal
    delay_minutos = Column(Integer, default=2)
    
    # Conteúdo da Oferta
    msg_texto = Column(Text, default="🔥 Oferta exclusiva para você!")
    msg_media = Column(String, nullable=True)
    
    # 🔊 ÁUDIO SEPARADO (Combo: áudio + mídia)
    audio_url = Column(String, nullable=True)           # URL do áudio OGG separado
    audio_delay_seconds = Column(Integer, default=3)    # Delay entre áudio e mídia+texto
    
    # Botões
    btn_aceitar = Column(String, default="✅ QUERO ESSA OFERTA!")
    btn_recusar = Column(String, default="❌ NÃO, OBRIGADO")
    
    # Auto-destruição
    autodestruir = Column(Boolean, default=False)
    
    created_at = Column(DateTime, default=now_brazil)
    updated_at = Column(DateTime, default=now_brazil, onupdate=now_brazil)
    
    bot = relationship("Bot", back_populates="upsell_config")

# =========================================================
# 📉 DOWNSELL (OFERTA PÓS-COMPRA #2 - APÓS UPSELL)
# =========================================================
class DownsellConfig(Base):
    __tablename__ = "downsell_config"
    id = Column(Integer, primary_key=True, index=True)
    bot_id = Column(Integer, ForeignKey("bots.id"), unique=True)
    
    ativo = Column(Boolean, default=False)
    nome_produto = Column(String)
    preco = Column(Float)
    link_acesso = Column(String, nullable=True)
    
    # ✅ FASE 2: Referência ao catálogo de Grupos e Canais
    group_id = Column(Integer, ForeignKey('bot_groups.id', ondelete='SET NULL'), nullable=True)
    
    # Delay em minutos após pagamento do upsell
    delay_minutos = Column(Integer, default=10)
    
    # Conteúdo da Oferta
    msg_texto = Column(Text, default="🎁 Última chance! Oferta especial só para você!")
    msg_media = Column(String, nullable=True)
    
    # 🔊 ÁUDIO SEPARADO (Combo: áudio + mídia)
    audio_url = Column(String, nullable=True)           # URL do áudio OGG separado
    audio_delay_seconds = Column(Integer, default=3)    # Delay entre áudio e mídia+texto
    
    # Botões
    btn_aceitar = Column(String, default="✅ QUERO ESSA OFERTA!")
    btn_recusar = Column(String, default="❌ NÃO, OBRIGADO")
    
    # Auto-destruição
    autodestruir = Column(Boolean, default=False)
    
    created_at = Column(DateTime, default=now_brazil)
    updated_at = Column(DateTime, default=now_brazil, onupdate=now_brazil)
    
    bot = relationship("Bot", back_populates="downsell_config")

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
    created_at = Column(DateTime, default=now_brazil)

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
    data_inicio = Column(DateTime, default=now_brazil)
    proxima_execucao = Column(DateTime, nullable=True)
    
    # Oferta Promocional
    plano_id = Column(Integer, nullable=True)
    promo_price = Column(Float, nullable=True)
    expiration_at = Column(DateTime, nullable=True)
    
    # Métricas de Execução
    total_leads = Column(Integer, default=0)
    sent_success = Column(Integer, default=0)
    blocked_count = Column(Integer, default=0)
    data_envio = Column(DateTime, default=now_brazil)
    
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
    webhook_type = Column(String(50))  # "pushinpay" ou "wiinpay"
    payload = Column(Text)
    attempts = Column(Integer, default=0)
    max_attempts = Column(Integer, default=5)
    next_retry = Column(DateTime, nullable=True)
    status = Column(String(20), default='pending')
    created_at = Column(DateTime, default=now_brazil)
    updated_at = Column(DateTime, default=now_brazil, onupdate=now_brazil)
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
    
    created_at = Column(DateTime, default=now_brazil)
    bot = relationship("Bot", back_populates="steps")

# =========================================================
# 🔗 TRACKING (RASTREAMENTO DE LINKS)
# =========================================================
class TrackingFolder(Base):
    __tablename__ = "tracking_folders"
    id = Column(Integer, primary_key=True, index=True)
    nome = Column(String)      # Ex: "Facebook Ads"
    plataforma = Column(String) # Ex: "facebook", "instagram"
    owner_id = Column(Integer, ForeignKey("users.id"), nullable=True)  # 🔥 NOVO: Dono da pasta
    created_at = Column(DateTime, default=now_brazil)
    
    links = relationship("TrackingLink", back_populates="folder", cascade="all, delete-orphan")
    owner = relationship("User", backref="tracking_folders")

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
    
    created_at = Column(DateTime, default=now_brazil)
    
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
    transaction_id = Column(String, nullable=True, index=True)
    
    # 🆕 MULTI-GATEWAY: Qual gateway processou este pagamento
    gateway_usada = Column(String, nullable=True)  # "pushinpay", "wiinpay" ou "syncpay"
    
    data_aprovacao = Column(DateTime, nullable=True)
    data_expiracao = Column(DateTime, nullable=True)
    custom_expiration = Column(DateTime, nullable=True)
    
    link_acesso = Column(String, nullable=True)
    mensagem_enviada = Column(Boolean, default=False)
    
    created_at = Column(DateTime, default=now_brazil)
    
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
    origem_entrada = Column(String, default="bot_direto")  # 🔥 NOVO: "bot_direto" ou "canal_free"
    
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
# 2. Categorias e Conteúdo
class MiniAppCategory(Base):
    __tablename__ = "miniapp_categories"
    
    id = Column(Integer, primary_key=True, index=True)
    bot_id = Column(Integer, ForeignKey('bots.id', ondelete='CASCADE'), nullable=False, index=True)
    slug = Column(String, nullable=False, index=True) 
    
    # Dados da Categoria (Vitrine)
    title = Column(String, nullable=False)
    description = Column(String, nullable=True)
    cover_image = Column(String, nullable=True)
    banner_mob_url = Column(String, nullable=True)
    bg_color = Column(String, default="#000000")
    
    # Novos campos (Atualização Visual)
    banner_desk_url = Column(String, nullable=True)
    video_preview_url = Column(String, nullable=True)
    
    # Modelo / Personagem
    model_img_url = Column(String, nullable=True)
    model_name = Column(String, nullable=True)
    model_desc = Column(String, nullable=True)
    
    # Footer / Decorativos
    footer_banner_url = Column(String, nullable=True)
    deco_lines_url = Column(String, nullable=True)
    
    # Cores Textos
    model_name_color = Column(String, default="#ffffff")
    model_desc_color = Column(String, default="#cccccc")
    theme_color = Column(String, default="#00ff88")
    
    # Configurações Avançadas
    is_direct_checkout = Column(Boolean, default=False)
    is_hacker_mode = Column(Boolean, default=False)
    
    # JSON para Itens (Produtos)
    content_json = Column(JSON, default=[])

    # --- ATUALIZAÇÃO RECENTE (Separadores, Paginação, etc) ---
    items_per_page = Column(Integer, default=None)
    separator_enabled = Column(Boolean, default=False)
    separator_color = Column(String, default='#333333')
    separator_text = Column(String, default=None)
    separator_btn_text = Column(String, default=None)
    separator_btn_url = Column(String, default=None)
    separator_logo_url = Column(String, default=None)
    model_img_shape = Column(String, default='square')

    # --- 🆕 NOVO: CORES DOS TEXTOS + NEON ---
    separator_text_color = Column(String, default='#ffffff')     # Cor do texto da barra
    separator_btn_text_color = Column(String, default='#ffffff') # Cor do texto do botão
    separator_is_neon = Column(Boolean, default=False)           # Efeito Neon/Glow na barra
    separator_neon_color = Column(String, default=None)            # Cor personalizada do Neon/Glow
    
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
    created_at = Column(DateTime, default=now_brazil, index=True)
    
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
    
    created_at = Column(DateTime, default=now_brazil)

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
    
    # 🔊 ÁUDIO SEPARADO (Combo: áudio + mídia)
    audio_url = Column(String(500), nullable=True)      # URL do áudio OGG separado
    audio_delay_seconds = Column(Integer, default=3)    # Delay entre áudio e mídia+texto
    
    # Timing
    delay_minutes = Column(Integer, default=5)
    
    # ✅ NOVO: Auto-destruição OPCIONAL
    auto_destruct_enabled = Column(Boolean, default=False)  # Se ativado, destrói a mensagem
    auto_destruct_seconds = Column(Integer, default=3)  # Só é usado se enabled=True
    auto_destruct_after_click = Column(Boolean, default=True)  # Se True, só destrói APÓS clicar no botão
    
    # Valores Promocionais (JSON)
    promo_values = Column(JSON, default={})  # {plano_id: valor_promo}
    
    # Auditoria
    created_at = Column(DateTime, default=now_brazil)
    updated_at = Column(DateTime, default=now_brazil, onupdate=now_brazil)
    
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
    
    # ✅ NOVOS CAMPOS
    max_duration_minutes = Column(Integer, default=60)
    last_message_auto_destruct = Column(Boolean, default=False)
    last_message_destruct_seconds = Column(Integer, default=60)
    
    # Auditoria
    created_at = Column(DateTime, default=now_brazil)
    updated_at = Column(DateTime, default=now_brazil, onupdate=now_brazil)
    
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
            "max_duration_minutes": self.max_duration_minutes,
            "last_message_auto_destruct": self.last_message_auto_destruct,
            "last_message_destruct_seconds": self.last_message_destruct_seconds
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
    sent_at = Column(DateTime, default=now_brazil, index=True)
    
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

# =========================================================
# 🆓 CANAL FREE (APROVAÇÃO AUTOMÁTICA)
# =========================================================
class CanalFreeConfig(Base):
    """
    Configuração do Canal Free - Aprovação automática de solicitações de entrada.
    Envia mensagem personalizada antes de aprovar.
    """
    __tablename__ = "canal_free_config"
    
    id = Column(Integer, primary_key=True, index=True)
    bot_id = Column(Integer, ForeignKey('bots.id', ondelete='CASCADE'), nullable=False, unique=True, index=True)
    
    # Identificação do Canal
    canal_id = Column(String, nullable=True)  # ID do canal/grupo Telegram
    canal_name = Column(String, nullable=True)  # Nome do canal (para exibição)
    
    # Status
    is_active = Column(Boolean, default=True, index=True)
    
    # Mensagem de Boas-Vindas
    message_text = Column(Text, nullable=False)
    media_url = Column(String(500), nullable=True)
    media_type = Column(String(10), nullable=True)  # 'photo', 'video', None
    
    # 🔊 ÁUDIO SEPARADO (Combo: áudio + mídia)
    audio_url = Column(String(500), nullable=True)      # URL do áudio OGG separado
    audio_delay_seconds = Column(Integer, default=3)    # Delay entre áudio e mídia+texto
    
    # Botões Personalizados (JSON Array)
    buttons = Column(JSON, default=[])
    # Formato: [{"text": "Ver Canal VIP", "url": "https://t.me/..."}]
    
    # Timing - Delay antes de aprovar (em segundos)
    delay_seconds = Column(Integer, default=60)
    
    # Auditoria
    created_at = Column(DateTime, default=now_brazil)
    updated_at = Column(DateTime, default=now_brazil, onupdate=now_brazil)
    
    # Relacionamento
    bot = relationship("Bot", backref="canal_free", uselist=False)
    
    def __repr__(self):
        return f"<CanalFreeConfig(bot_id={self.bot_id}, canal={self.canal_name}, active={self.is_active})>"

# =========================================================
# 📦 GRUPOS E CANAIS (ESTEIRA DE PRODUTOS / CATÁLOGO)
# =========================================================
class BotGroup(Base):
    """
    Catálogo de grupos/canais extras vinculados a um bot.
    Permite criar uma esteira de produtos onde cada grupo pode
    ser associado a planos específicos, Order Bumps, Upsells e Downsells.
    
    Exemplo de uso:
    - Produto Principal: Canal VIP (registrado na criação do bot)
    - Produto Upsell: Canal Premium (cadastrado aqui)
    - Produto Downsell: Grupo de Bônus (cadastrado aqui)
    """
    __tablename__ = "bot_groups"
    
    id = Column(Integer, primary_key=True, index=True)
    bot_id = Column(Integer, ForeignKey('bots.id', ondelete='CASCADE'), nullable=False, index=True)
    owner_id = Column(Integer, ForeignKey('users.id'), nullable=False, index=True)
    
    # Identificação do Grupo/Canal
    title = Column(String, nullable=False)          # Ex: "Grupo de Dietas", "Canal Premium"
    group_id = Column(String, nullable=False)        # ID do Telegram: "-1001234567890"
    link = Column(String, nullable=True)             # Link de convite: "t.me/+abc123"
    
    # Planos vinculados (quais planos dão acesso a este grupo)
    plan_ids = Column(JSON, default=[])              # Ex: [1, 3, 5]
    
    # Status
    is_active = Column(Boolean, default=True, index=True)
    
    # Auditoria
    created_at = Column(DateTime, default=now_brazil)
    updated_at = Column(DateTime, default=now_brazil, onupdate=now_brazil)
    
    # Relacionamentos
    bot = relationship("Bot", back_populates="bot_groups")
    owner = relationship("User")
    

    def __repr__(self):
        return f"<BotGroup(id={self.id}, bot_id={self.bot_id}, title='{self.title}', group_id='{self.group_id}', active={self.is_active})>"

# =========================================================
# ✨ EMOJIS PREMIUM (CUSTOM EMOJIS DO TELEGRAM)
# =========================================================
class PremiumEmojiPack(Base):
    """
    Pacotes/Categorias de emojis premium.
    Permite organizar emojis em categorias como "Populares", "Corações", "Animais", etc.
    Gerenciado exclusivamente pelo Super Admin.
    """
    __tablename__ = "premium_emoji_packs"
    
    id = Column(Integer, primary_key=True, index=True)
    name = Column(String(100), nullable=False, unique=True)  # Ex: "Populares", "Corações", "Animais"
    icon = Column(String(10), nullable=True)                   # Emoji fallback do pacote: "🔥", "❤️", "🐱"
    description = Column(String(255), nullable=True)           # Descrição breve
    sort_order = Column(Integer, default=0)                    # Ordem de exibição
    is_active = Column(Boolean, default=True, index=True)
    
    created_at = Column(DateTime, default=now_brazil)
    updated_at = Column(DateTime, default=now_brazil, onupdate=now_brazil)
    
    # Relacionamento com emojis
    emojis = relationship("PremiumEmoji", back_populates="pack", cascade="all, delete-orphan")
    
    def __repr__(self):
        return f"<PremiumEmojiPack(id={self.id}, name='{self.name}', emojis={len(self.emojis) if self.emojis else 0})>"


class PremiumEmoji(Base):
    """
    Catálogo de custom emojis premium do Telegram.
    Cada emoji tem um ID numérico (custom_emoji_id) que é usado na tag <tg-emoji>.
    O shortcode é o que o usuário digita/insere no campo de texto (ex: :fire_premium:).
    O fallback é o emoji padrão exibido se o premium falhar.
    
    Uso no Telegram (HTML):
        <tg-emoji emoji-id="5408846744727334338">🔥</tg-emoji>
    
    O Super Admin cadastra esses emojis via painel, e os usuários da plataforma
    inserem via Emoji Picker visual nas páginas de texto/legenda.
    """
    __tablename__ = "premium_emojis"
    
    id = Column(Integer, primary_key=True, index=True)
    
    # Identificação do Emoji no Telegram
    emoji_id = Column(String(50), unique=True, nullable=False, index=True)  # ID numérico do Telegram: "5408846744727334338"
    
    # Display
    fallback = Column(String(10), nullable=False)            # Emoji padrão como fallback: "🔥"
    name = Column(String(100), nullable=False)                # Nome amigável: "Fogo Animado"
    shortcode = Column(String(50), unique=True, nullable=False, index=True)  # Shortcode: ":fire_premium:"
    
    # Organização
    pack_id = Column(Integer, ForeignKey('premium_emoji_packs.id', ondelete='SET NULL'), nullable=True, index=True)
    sort_order = Column(Integer, default=0)  # Ordem dentro do pacote
    
    # Thumbnail (URL da preview - pode ser preenchida via API getCustomEmojiStickers)
    thumbnail_url = Column(String(500), nullable=True)
    
    # Tipo do emoji: 'static' ou 'animated'
    emoji_type = Column(String(20), default='static')
    
    # Status
    is_active = Column(Boolean, default=True, index=True)
    
    # Auditoria
    created_at = Column(DateTime, default=now_brazil)
    updated_at = Column(DateTime, default=now_brazil, onupdate=now_brazil)
    
    # Relacionamento com pacote
    pack = relationship("PremiumEmojiPack", back_populates="emojis")
    
    def to_html_tag(self):
        """Retorna a tag HTML do Telegram para este emoji premium."""
        return f'<tg-emoji emoji-id="{self.emoji_id}">{self.fallback}</tg-emoji>'
    
    def __repr__(self):
        return f"<PremiumEmoji(id={self.id}, name='{self.name}', shortcode='{self.shortcode}', emoji_id='{self.emoji_id}')>"


# ============================================================
# 🚨 SISTEMA DE DENÚNCIAS
# ============================================================
class Report(Base):
    __tablename__ = 'reports'
    
    id = Column(Integer, primary_key=True, index=True)
    
    # Quem denunciou (opcional - anonimato)
    reporter_name = Column(String(100), nullable=True)          # Nome do denunciante (opcional)
    reporter_telegram_id = Column(String(50), nullable=True)    # Telegram ID do denunciante (capturado automaticamente)
    
    # Bot/Conta denunciada
    bot_username = Column(String(100), nullable=False)          # @username do bot denunciado
    bot_id = Column(Integer, ForeignKey('bots.id', ondelete='SET NULL'), nullable=True)  # Vincula ao bot se encontrado
    
    # 🔥 NOVAS COLUNAS V2: Identificação do Dono do Bot
    owner_id = Column(Integer, ForeignKey('users.id', ondelete='SET NULL'), nullable=True)
    owner_username = Column(String(100), nullable=True)
    
    # Detalhes da denúncia
    reason = Column(String(50), nullable=False)                 # Categoria: 'cp', 'fraud', 'scam', 'spam', 'illegal', 'other'
    description = Column(Text, nullable=True)                   # Descrição detalhada
    evidence_url = Column(String(500), nullable=True)           # Link de evidência (print, etc)
    
    # Status e processamento
    status = Column(String(20), default='pending')              # 'pending', 'reviewing', 'resolved', 'dismissed'
    resolution = Column(Text, nullable=True)                    # Ação tomada pelo admin
    resolved_by = Column(Integer, ForeignKey('users.id', ondelete='SET NULL'), nullable=True)
    resolved_at = Column(DateTime, nullable=True)
    
    # Punição aplicada
    action_taken = Column(String(50), nullable=True)            # 'warning', 'strike', 'pause_bots', 'ban_account', 'none'
    strike_count = Column(Integer, default=0)                   # Strikes acumulados nesta denúncia
    
    # Auditoria
    created_at = Column(DateTime, default=now_brazil)
    updated_at = Column(DateTime, default=now_brazil, onupdate=now_brazil)
    ip_address = Column(String(50), nullable=True)              # IP do denunciante (para segurança)
    
    def __repr__(self):
        return f"<Report(id={self.id}, bot='{self.bot_username}', owner='{self.owner_username}', reason='{self.reason}', status='{self.status}')>"


# ============================================================
# 🚨 STRIKES E PUNIÇÕES (Controle por conta de usuário)
# ============================================================
class UserStrike(Base):
    __tablename__ = 'user_strikes'
    
    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey('users.id', ondelete='CASCADE'), nullable=False)
    report_id = Column(Integer, ForeignKey('reports.id', ondelete='SET NULL'), nullable=True)
    
    reason = Column(Text, nullable=False)                       # Motivo do strike
    strike_number = Column(Integer, nullable=False)             # 1, 2 ou 3
    
    # Punição
    action = Column(String(50), nullable=False)                 # 'warning', 'tax_increase', 'pause_bots', 'ban_account'
    pause_until = Column(DateTime, nullable=True)               # Se pausado, até quando
    tax_increase_pct = Column(Float, nullable=True)             # Aumento da taxa (ex: 5.0 = +5%)
    
    applied_by = Column(Integer, ForeignKey('users.id', ondelete='SET NULL'), nullable=True)
    created_at = Column(DateTime, default=now_brazil)
    
    def __repr__(self):
        return f"<UserStrike(user_id={self.user_id}, strike={self.strike_number}, action='{self.action}')>"

# =========================================================
# 📓 TABELA: DIÁRIO DE MUDANÇAS (CHANGELOG)
# =========================================================
class ChangeLog(Base):
    __tablename__ = "change_logs"
    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey('users.id', ondelete='CASCADE'))
    bot_id = Column(Integer, ForeignKey('bots.id', ondelete='SET NULL'), nullable=True)
    date = Column(DateTime, default=now_brazil)
    category = Column(String(50), default='geral')  # geral, upsell, downsell, fluxo, preco, remarketing, plano
    content = Column(Text, nullable=False)
    created_at = Column(DateTime, default=now_brazil)

    def __repr__(self):
        return f"<ChangeLog(id={self.id}, category='{self.category}')>"