import os
import logging
import telebot
import requests
import time
import urllib.parse
import threading
from telebot import types
import json
import uuid

# --- IMPORTS CORRIGIDOS ---
from sqlalchemy import func, desc, text
from fastapi import FastAPI, HTTPException, Depends, Request, BackgroundTasks
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from sqlalchemy.orm import Session
from typing import List, Optional
from datetime import datetime, timedelta
from database import Lead  # Não esqueça de importar Lead!
from force_migration import forcar_atualizacao_tabelas

# 🆕 ADICIONAR ESTES IMPORTS PARA AUTENTICAÇÃO
from passlib.context import CryptContext
from jose import JWTError, jwt
from datetime import timedelta
from fastapi.security import OAuth2PasswordBearer, OAuth2PasswordRequestForm
from pydantic import BaseModel, EmailStr

# Importa o banco e o script de reparo
from database import SessionLocal, init_db, Bot, PlanoConfig, BotFlow, BotFlowStep, Pedido, SystemConfig, RemarketingCampaign, BotAdmin, Lead, OrderBumpConfig, TrackingFolder, TrackingLink, MiniAppConfig, MiniAppCategory, engine
import update_db 

from migration_v3 import executar_migracao_v3
from migration_v4 import executar_migracao_v4
from migration_v5 import executar_migracao_v5  # <--- ADICIONE ESTA LINHA
from migration_v6 import executar_migracao_v6  # <--- ADICIONE AQUI

# Configuração de Log
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = FastAPI(title="Zenyx Gbot SaaS")

# 🔥 FORÇA A CRIAÇÃO DAS COLUNAS AO INICIAR
try:
    forcar_atualizacao_tabelas()
except Exception as e:
    print(f"Erro na migração forçada: {e}")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# =========================================================
# 🔐 CONFIGURAÇÕES DE AUTENTICAÇÃO JWT
# =========================================================
SECRET_KEY = os.getenv("SECRET_KEY", "zenyx-secret-key-change-in-production-2026")
ALGORITHM = "HS256"
ACCESS_TOKEN_EXPIRE_MINUTES = 60 * 24 * 7  # 7 dias

pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")
oauth2_scheme = OAuth2PasswordBearer(tokenUrl="api/auth/login")

# =========================================================
# 📦 SCHEMAS PYDANTIC PARA AUTENTICAÇÃO
# =========================================================
class UserCreate(BaseModel):
    username: str
    email: EmailStr
    password: str
    full_name: str = None

class UserLogin(BaseModel):
    username: str
    password: str

class Token(BaseModel):
    access_token: str
    token_type: str
    user_id: int
    username: str

class TokenData(BaseModel):
    username: str = None

# ========================================================
# 1. FUNÇÃO DE CONEXÃO COM BANCO (TEM QUE SER A PRIMEIRA)
# =========================================================
def get_db():
    """Gera conexão com o banco de dados"""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
# =========================================================
# 🔧 FUNÇÕES AUXILIARES DE AUTENTICAÇÃO (CORRIGIDAS)
# =========================================================
def verify_password(plain_password: str, hashed_password: str) -> bool:
    """Verifica se a senha está correta"""
    return pwd_context.verify(plain_password, hashed_password)

def get_password_hash(password: str) -> str:
    """Gera hash da senha (com truncamento automático para bcrypt)"""
    # Bcrypt tem limite de 72 bytes
    if len(password.encode('utf-8')) > 72:
        password = password[:72]
    return pwd_context.hash(password)

def create_access_token(data: dict, expires_delta: timedelta = None):
    """Cria token JWT"""
    to_encode = data.copy()
    if expires_delta:
        expire = datetime.utcnow() + expires_delta
    else:
        expire = datetime.utcnow() + timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES)
    
    to_encode.update({"exp": expire})
    encoded_jwt = jwt.encode(to_encode, SECRET_KEY, algorithm=ALGORITHM)
    return encoded_jwt

async def get_current_user(token: str = Depends(oauth2_scheme)):
    """Decodifica token e retorna usuário atual"""
    credentials_exception = HTTPException(
        status_code=401,
        detail="Não foi possível validar as credenciais",
        headers={"WWW-Authenticate": "Bearer"},
    )
    
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        username: str = payload.get("sub")
        user_id: int = payload.get("user_id")
        
        if username is None:
            raise credentials_exception
            
    except JWTError:
        raise credentials_exception
    
    db = SessionLocal()
    try:
        from database import User
        user = db.query(User).filter(User.id == user_id).first()
        
        if user is None:
            raise credentials_exception
        
        return user
    finally:
        db.close()

# =========================================================
# 🔒 FUNÇÃO HELPER: VERIFICAR PROPRIEDADE DO BOT
# =========================================================
def verificar_bot_pertence_usuario(bot_id: int, user_id: int, db: Session):
    """
    Verifica se o bot pertence ao usuário.
    Retorna o bot se pertencer, caso contrário levanta HTTPException 404.
    """
    bot = db.query(Bot).filter(
        Bot.id == bot_id,
        Bot.owner_id == user_id
    ).first()
    
    if not bot:
        raise HTTPException(
            status_code=404, 
            detail="Bot não encontrado ou você não tem permissão para acessá-lo"
        )
    
    return bot

# ============================================================
# 👇 COLE TODAS AS 5 FUNÇÕES AQUI (DEPOIS DO get_db)
# ============================================================

# FUNÇÃO 1: CRIAR OU ATUALIZAR LEAD (TOPO)
# FUNÇÃO 1: CRIAR OU ATUALIZAR LEAD (TOPO) - ATUALIZADA
def criar_ou_atualizar_lead(
    db: Session,
    user_id: str,
    nome: str,
    username: str,
    bot_id: int,
    tracking_id: Optional[int] = None # 🔥 Novo Parâmetro
):
    lead = db.query(Lead).filter(
        Lead.user_id == user_id,
        Lead.bot_id == bot_id
    ).first()
    
    agora = datetime.utcnow()
    
    if lead:
        lead.ultimo_contato = agora
        lead.nome = nome
        lead.username = username
        # Se veio tracking novo, atualiza (atribuição de último clique)
        if tracking_id:
            lead.tracking_id = tracking_id
    else:
        lead = Lead(
            user_id=user_id,
            nome=nome,
            username=username,
            bot_id=bot_id,
            primeiro_contato=agora,
            ultimo_contato=agora,
            status='topo',
            funil_stage='lead_frio',
            tracking_id=tracking_id # 🔥 Salva a origem
        )
        db.add(lead)
    
    db.commit()
    db.refresh(lead)
    return lead

# FUNÇÃO 2: MOVER LEAD PARA PEDIDO (MEIO)
def mover_lead_para_pedido(
    db: Session,
    user_id: str,
    bot_id: int,
    pedido_id: int
):
    """
    Quando um Lead gera PIX, ele vira Pedido (MEIO)
    """
    lead = db.query(Lead).filter(
        Lead.user_id == user_id,
        Lead.bot_id == bot_id
    ).first()
    
    pedido = db.query(Pedido).filter(Pedido.id == pedido_id).first()
    
    if lead and pedido:
        pedido.primeiro_contato = lead.primeiro_contato
        pedido.escolheu_plano_em = datetime.utcnow()
        pedido.gerou_pix_em = datetime.utcnow()
        pedido.status_funil = 'meio'
        pedido.funil_stage = 'lead_quente'
        
        db.delete(lead)
        db.commit()
        logger.info(f"📊 Lead movido para MEIO (Pedido): {pedido.first_name}")
    
    return pedido


# FUNÇÃO 3: MARCAR COMO PAGO (FUNDO)
def marcar_como_pago(
    db: Session,
    pedido_id: int
):
    """
    Marca pedido como PAGO (FUNDO do funil)
    """
    pedido = db.query(Pedido).filter(Pedido.id == pedido_id).first()
    
    if not pedido:
        return None
    
    agora = datetime.utcnow()
    pedido.pagou_em = agora
    pedido.status_funil = 'fundo'
    pedido.funil_stage = 'cliente'
    
    if pedido.primeiro_contato:
        dias = (agora - pedido.primeiro_contato).days
        pedido.dias_ate_compra = dias
        logger.info(f"✅ PAGAMENTO APROVADO! {pedido.first_name} - Dias até compra: {dias}")
    else:
        pedido.dias_ate_compra = 0
    
    db.commit()
    db.refresh(pedido)
    return pedido


# FUNÇÃO 4: MARCAR COMO EXPIRADO
def marcar_como_expirado(
    db: Session,
    pedido_id: int
):
    """
    Marca pedido como EXPIRADO (PIX venceu)
    """
    pedido = db.query(Pedido).filter(Pedido.id == pedido_id).first()
    
    if pedido:
        pedido.status_funil = 'expirado'
        pedido.funil_stage = 'lead_quente'
        db.commit()
        logger.info(f"⏰ PIX EXPIRADO: {pedido.first_name}")
    
    return pedido


# FUNÇÃO 5: REGISTRAR REMARKETING
def registrar_remarketing(
    db: Session,
    user_id: str,
    bot_id: int
):
    """
    Registra que usuário recebeu remarketing
    """
    agora = datetime.utcnow()
    
    # Atualiza Lead (se for TOPO)
    lead = db.query(Lead).filter(
        Lead.user_id == user_id,
        Lead.bot_id == bot_id
    ).first()
    
    if lead:
        lead.ultimo_remarketing = agora
        lead.total_remarketings += 1
        db.commit()
        logger.info(f"📧 Remarketing registrado (TOPO): {lead.nome}")
        return
    
    # Atualiza Pedido (se for MEIO/EXPIRADO)
    pedido = db.query(Pedido).filter(
        Pedido.telegram_id == user_id,
        Pedido.bot_id == bot_id
    ).first()
    
    if pedido:
        pedido.ultimo_remarketing = agora
        pedido.total_remarketings += 1
        db.commit()
        logger.info(f"📧 Remarketing registrado (MEIO): {pedido.first_name}")

    # 2. FORÇA A CRIAÇÃO DE TODAS AS COLUNAS FALTANTES (TODAS AS VERSÕES)
    try:
        with engine.connect() as conn:
            logger.info("🔧 [STARTUP] Verificando integridade completa do banco...")
            
            comandos_sql = [
                # --- [CORREÇÃO 1] TABELA DE PLANOS ---
                "ALTER TABLE planos_config ADD COLUMN IF NOT EXISTS key_id VARCHAR;",
                "ALTER TABLE planos_config ADD COLUMN IF NOT EXISTS descricao TEXT;",
                "ALTER TABLE planos_config ADD COLUMN IF NOT EXISTS preco_cheio FLOAT;",

                # --- [CORREÇÃO 2] TABELA DE PEDIDOS ---
                "ALTER TABLE pedidos ADD COLUMN IF NOT EXISTS plano_id INTEGER;",
                "ALTER TABLE pedidos ADD COLUMN IF NOT EXISTS plano_nome VARCHAR;",
                "ALTER TABLE pedidos ADD COLUMN IF NOT EXISTS txid VARCHAR;",
                "ALTER TABLE pedidos ADD COLUMN IF NOT EXISTS qr_code TEXT;",
                "ALTER TABLE pedidos ADD COLUMN IF NOT EXISTS transaction_id VARCHAR;", 
                "ALTER TABLE pedidos ADD COLUMN IF NOT EXISTS data_aprovacao TIMESTAMP WITHOUT TIME ZONE;",
                "ALTER TABLE pedidos ADD COLUMN IF NOT EXISTS data_expiracao TIMESTAMP WITHOUT TIME ZONE;",
                "ALTER TABLE pedidos ADD COLUMN IF NOT EXISTS custom_expiration TIMESTAMP WITHOUT TIME ZONE;",
                "ALTER TABLE pedidos ADD COLUMN IF NOT EXISTS link_acesso VARCHAR;",
                "ALTER TABLE pedidos ADD COLUMN IF NOT EXISTS mensagem_enviada BOOLEAN DEFAULT FALSE;",
                "ALTER TABLE pedidos ADD COLUMN IF NOT EXISTS tem_order_bump BOOLEAN DEFAULT FALSE;", 
                "ALTER TABLE pedidos ADD COLUMN IF NOT EXISTS tracking_id INTEGER;",

                # --- [CORREÇÃO 3] FLUXO DE MENSAGENS ---
                "ALTER TABLE bot_flows ADD COLUMN IF NOT EXISTS autodestruir_1 BOOLEAN DEFAULT FALSE;",
                "ALTER TABLE bot_flows ADD COLUMN IF NOT EXISTS msg_2_texto TEXT;",
                "ALTER TABLE bot_flows ADD COLUMN IF NOT EXISTS msg_2_media VARCHAR;",
                "ALTER TABLE bot_flows ADD COLUMN IF NOT EXISTS mostrar_planos_2 BOOLEAN DEFAULT TRUE;",
                "ALTER TABLE bot_flows ADD COLUMN IF NOT EXISTS mostrar_planos_1 BOOLEAN DEFAULT FALSE;",
                
                # --- [CORREÇÃO 4] REMARKETING AVANÇADO ---
                "ALTER TABLE remarketing_campaigns ADD COLUMN IF NOT EXISTS target VARCHAR DEFAULT 'todos';",
                "ALTER TABLE remarketing_campaigns ADD COLUMN IF NOT EXISTS type VARCHAR DEFAULT 'massivo';",
                "ALTER TABLE remarketing_campaigns ADD COLUMN IF NOT EXISTS plano_id INTEGER;",
                "ALTER TABLE remarketing_campaigns ADD COLUMN IF NOT EXISTS promo_price FLOAT;",
                "ALTER TABLE remarketing_campaigns ADD COLUMN IF NOT EXISTS expiration_at TIMESTAMP WITHOUT TIME ZONE;",
                "ALTER TABLE remarketing_campaigns ADD COLUMN IF NOT EXISTS dia_atual INTEGER DEFAULT 0;",
                "ALTER TABLE remarketing_campaigns ADD COLUMN IF NOT EXISTS data_inicio TIMESTAMP WITHOUT TIME ZONE DEFAULT now();",
                "ALTER TABLE remarketing_campaigns ADD COLUMN IF NOT EXISTS proxima_execucao TIMESTAMP WITHOUT TIME ZONE;",
                
                # --- [CORREÇÃO 5] TABELA NOVA (FLOW V2) ---
                """
                CREATE TABLE IF NOT EXISTS bot_flow_steps (
                    id SERIAL PRIMARY KEY,
                    bot_id INTEGER REFERENCES bots(id),
                    step_order INTEGER DEFAULT 1,
                    msg_texto TEXT,
                    msg_media VARCHAR,
                    btn_texto VARCHAR DEFAULT 'Próximo ▶️',
                    mostrar_botao BOOLEAN DEFAULT TRUE,
                    autodestruir BOOLEAN DEFAULT FALSE,
                    delay_seconds INTEGER DEFAULT 0,
                    created_at TIMESTAMP WITHOUT TIME ZONE DEFAULT now()
                );
                """,
                
                # --- [CORREÇÃO 6] SUPORTE NO BOT ---
                "ALTER TABLE bots ADD COLUMN IF NOT EXISTS suporte_username VARCHAR;",

                # --- [CORREÇÃO 7] TABELAS DE TRACKING ---
                """
                CREATE TABLE IF NOT EXISTS tracking_folders (
                    id SERIAL PRIMARY KEY,
                    nome VARCHAR,
                    plataforma VARCHAR,
                    created_at TIMESTAMP WITHOUT TIME ZONE DEFAULT now()
                );
                """,
                """
                CREATE TABLE IF NOT EXISTS tracking_links (
                    id SERIAL PRIMARY KEY,
                    folder_id INTEGER REFERENCES tracking_folders(id),
                    bot_id INTEGER REFERENCES bots(id),
                    nome VARCHAR,
                    codigo VARCHAR UNIQUE,
                    origem VARCHAR DEFAULT 'outros',
                    clicks INTEGER DEFAULT 0,
                    leads INTEGER DEFAULT 0,
                    vendas INTEGER DEFAULT 0,
                    faturamento FLOAT DEFAULT 0.0,
                    created_at TIMESTAMP WITHOUT TIME ZONE DEFAULT now()
                );
                """,
                "ALTER TABLE leads ADD COLUMN IF NOT EXISTS tracking_id INTEGER REFERENCES tracking_links(id);",

                # --- [CORREÇÃO 8] 🔥 TABELAS DA LOJA (MINI APP) ---
                """
                CREATE TABLE IF NOT EXISTS miniapp_config (
                    bot_id INTEGER PRIMARY KEY REFERENCES bots(id),
                    logo_url VARCHAR,
                    background_type VARCHAR DEFAULT 'solid',
                    background_value VARCHAR DEFAULT '#000000',
                    hero_video_url VARCHAR,
                    hero_title VARCHAR DEFAULT 'ACERVO PREMIUM',
                    hero_subtitle VARCHAR DEFAULT 'O maior acervo da internet.',
                    hero_btn_text VARCHAR DEFAULT 'LIBERAR CONTEÚDO 🔓',
                    enable_popup BOOLEAN DEFAULT FALSE,
                    popup_video_url VARCHAR,
                    popup_text VARCHAR DEFAULT 'VOCÊ GANHOU UM PRESENTE!',
                    footer_text VARCHAR DEFAULT '© 2026 Premium Club.'
                );
                """,
                """
                CREATE TABLE IF NOT EXISTS miniapp_categories (
                    id SERIAL PRIMARY KEY,
                    bot_id INTEGER REFERENCES bots(id),
                    slug VARCHAR,
                    title VARCHAR,
                    description VARCHAR,
                    cover_image VARCHAR,
                    theme_color VARCHAR DEFAULT '#c333ff',
                    deco_line_url VARCHAR,
                    is_direct_checkout BOOLEAN DEFAULT FALSE,
                    is_hacker_mode BOOLEAN DEFAULT FALSE,
                    banner_desk_url VARCHAR,
                    banner_mob_url VARCHAR,
                    footer_banner_url VARCHAR,
                    content_json TEXT
                );
                """,

                # --- [CORREÇÃO 9] NOVAS COLUNAS PARA CATEGORIA RICA ---
                "ALTER TABLE miniapp_categories ADD COLUMN IF NOT EXISTS bg_color VARCHAR DEFAULT '#000000';",
                "ALTER TABLE miniapp_categories ADD COLUMN IF NOT EXISTS banner_desk_url VARCHAR;",
                "ALTER TABLE miniapp_categories ADD COLUMN IF NOT EXISTS video_preview_url VARCHAR;",
                "ALTER TABLE miniapp_categories ADD COLUMN IF NOT EXISTS model_img_url VARCHAR;",
                "ALTER TABLE miniapp_categories ADD COLUMN IF NOT EXISTS model_name VARCHAR;",
                "ALTER TABLE miniapp_categories ADD COLUMN IF NOT EXISTS model_desc TEXT;",
                "ALTER TABLE miniapp_categories ADD COLUMN IF NOT EXISTS footer_banner_url VARCHAR;",
                "ALTER TABLE miniapp_categories ADD COLUMN IF NOT EXISTS deco_lines_url VARCHAR;",
                "ALTER TABLE miniapp_categories ADD COLUMN IF NOT EXISTS model_name_color VARCHAR DEFAULT '#ffffff';",
                "ALTER TABLE miniapp_categories ADD COLUMN IF NOT EXISTS model_desc_color VARCHAR DEFAULT '#cccccc';",

                # --- [CORREÇÃO 10] TOKEN PUSHINPAY E ORDER BUMP ---
                "ALTER TABLE bots ADD COLUMN IF NOT EXISTS pushin_token VARCHAR;",
                "ALTER TABLE order_bump_config ADD COLUMN IF NOT EXISTS autodestruir BOOLEAN DEFAULT FALSE;",

                # 👇👇👇 [CORREÇÃO 11] SUPORTE A WEB APP NO FLUXO (CRÍTICO) 👇👇👇
                "ALTER TABLE bot_flows ADD COLUMN IF NOT EXISTS start_mode VARCHAR DEFAULT 'padrao';",
                "ALTER TABLE bot_flows ADD COLUMN IF NOT EXISTS miniapp_url VARCHAR;",
                "ALTER TABLE bot_flows ADD COLUMN IF NOT EXISTS miniapp_btn_text VARCHAR DEFAULT 'ABRIR LOJA 🛍️';"
            ]
            
            for cmd in comandos_sql:
                try:
                    conn.execute(text(cmd))
                    conn.commit()
                except Exception as e_sql:
                    # Ignora erro se a coluna já existir (segurança para não parar o deploy)
                    if "duplicate column" not in str(e_sql) and "already exists" not in str(e_sql):
                        logger.warning(f"Aviso SQL: {e_sql}")
            
            logger.info("✅ [STARTUP] Banco de dados 100% Verificado!")
            
    except Exception as e:
        logger.error(f"❌ Falha no reparo do banco: {e}")

    # 3. Inicia o Agendador (Scheduler)
    try:
        # Se você usa scheduler, inicia aqui
        if 'scheduler' in globals():
            scheduler.add_job(verificar_vencimentos, 'interval', hours=12)
            scheduler.add_job(executar_remarketing, 'interval', minutes=30) 
            scheduler.start()
            logger.info("⏰ [STARTUP] Agendador de tarefas iniciado.")
    except Exception as e:
        logger.error(f"❌ [STARTUP] Erro no Scheduler: {e}")

    # 3. Inicia o Ceifador
    thread = threading.Thread(target=loop_verificar_vencimentos)
    thread.daemon = True
    thread.start()
    logger.info("💀 O Ceifador (Auto-Kick) foi iniciado!")

# =========================================================
# 💀 O CEIFADOR: VERIFICA VENCIMENTOS E REMOVE (KICK SUAVE)
# =========================================================
def loop_verificar_vencimentos():
    """Roda a cada 60 segundos para remover usuários vencidos"""
    while True:
        try:
            logger.info("⏳ Verificando assinaturas vencidas...")
            verificar_expiracao_massa()
        except Exception as e:
            logger.error(f"Erro no loop de vencimento: {e}")
        
        time.sleep(60) # 🔥 VOLTOU PARA 60 SEGUNDOS (Verificação Rápida)

# =========================================================
# 💀 O CEIFADOR: REMOVEDOR BASEADO EM DATA (SAAS)
# =========================================================
def verificar_expiracao_massa():
    db = SessionLocal()
    try:
        # Pega todos os bots do sistema
        bots = db.query(Bot).all()
        
        for bot_data in bots:
            if not bot_data.token or not bot_data.id_canal_vip: continue
            
            try:
                # Conecta no Telegram deste bot específico
                tb = telebot.TeleBot(bot_data.token)
                
                # Tratamento ROBUSTO do ID do canal (Remove espaços e caracteres invisíveis)
                try: 
                    raw_id = str(bot_data.id_canal_vip).strip()
                    canal_id = int(raw_id)
                except: 
                    logger.error(f"ID do canal inválido para o bot {bot_data.nome}")
                    continue
                
                agora = datetime.utcnow()
                
                # --- QUERY INTELIGENTE ---
                # Busca usuários deste bot que:
                # 1. Estão com status 'paid' ou 'approved' (Ativos)
                # 2. Têm uma data de expiração definida (NÃO SÃO VITALÍCIOS)
                # 3. A data de expiração é MENOR que agora (Já venceu)
                vencidos = db.query(Pedido).filter(
                    Pedido.bot_id == bot_data.id,
                    Pedido.status.in_(['paid', 'approved', 'active']), # 🔥 Pega todos os status de ativos
                    Pedido.custom_expiration != None, 
                    Pedido.custom_expiration < agora
                ).all()
                
                for u in vencidos:
                    # 🔥 Proteção: Admin nunca é removido
                    # Verifica na tabela BotAdmin
                    eh_admin = db.query(BotAdmin).filter(
                        BotAdmin.telegram_id == str(u.telegram_id),
                        BotAdmin.bot_id == bot_data.id
                    ).first()
                    
                    # Verifica se é o dono do bot
                    if str(u.telegram_id) == str(bot_data.admin_principal_id) or eh_admin:
                        logger.info(f"👑 Ignorando remoção de Admin vencido: {u.telegram_id}")
                        continue
                    
                    try:
                        logger.info(f"💀 Ceifando usuário vencido: {u.first_name} (Bot: {bot_data.nome})")
                        
                        # 1. Kick Suave (Ban + Unban para remover do canal mas permitir volta futura)
                        tb.ban_chat_member(canal_id, int(u.telegram_id))
                        tb.unban_chat_member(canal_id, int(u.telegram_id))
                        
                        # 2. Atualiza Status no Banco IMEDIATAMENTE
                        u.status = 'expired'
                        db.commit()
                        
                        # 3. Avisa o usuário
                        try: 
                            tb.send_message(int(u.telegram_id), "🚫 <b>Seu plano venceu!</b>\n\nSeu tempo de acesso acabou. Para renovar e voltar ao canal, digite /start", parse_mode="HTML")
                        except: pass
                        
                    except Exception as e_kick:
                        err_msg = str(e_kick)
                        if "participant_id_invalid" in err_msg or "user not found" in err_msg:
                            # Se o user já saiu, apenas marca como expirado
                            logger.info(f"Usuário {u.telegram_id} já não estava no canal. Marcando expired.")
                            u.status = 'expired'
                            db.commit()
                        else:
                            logger.error(f"Erro ao remover {u.telegram_id}: {e_kick}")
                        
            except Exception as e_bot:
                logger.error(f"Erro ao processar bot {bot_data.id}: {e_bot}")
                
    finally: 
        db.close()

# =========================================================
# 🔌 INTEGRAÇÃO PUSHIN PAY (DINÂMICA)
# =========================================================
def get_pushin_token():
    """Busca o token no banco, se não achar, tenta variável de ambiente"""
    db = SessionLocal()
    try:
        # Tenta pegar do banco de dados (Painel de Integrações)
        config = db.query(SystemConfig).filter(SystemConfig.key == "pushin_pay_token").first()
        if config and config.value:
            return config.value
        # Se não tiver no banco, pega do Railway Variables
        return os.getenv("PUSHIN_PAY_TOKEN")
    finally:
        db.close()

# =========================================================
# 🔌 INTEGRAÇÃO PUSHIN PAY (CORRIGIDA)
# =========================================================
def gerar_pix_pushinpay(valor_float: float, transaction_id: str):
    token = get_pushin_token()
    
    if not token:
        logger.error("❌ Token Pushin Pay não configurado!")
        return None
    
    url = "https://api.pushinpay.com.br/api/pix/cashIn"
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "Accept": "application/json"
    }
    
    # URL DO RAILWAY FIXA (Garante que o Webhook chegue)
    seus_dominio = "https://zenyx-gbs-testesv1-production.up.railway.app" 
    
    payload = {
        "value": int(valor_float * 100), 
        "webhook_url": f"https://{seus_dominio}/webhook/pix",
        "external_reference": transaction_id
    }

    try:
        logger.info(f"📤 Gerando PIX. Webhook definido para: https://{seus_dominio}/webhook/pix")
        response = requests.post(url, json=payload, headers=headers, timeout=10)
        
        if response.status_code in [200, 201]:
            return response.json()
        else:
            logger.error(f"Erro PushinPay: {response.text}")
            return None
    except Exception as e:
        logger.error(f"Exceção PushinPay: {e}")
        return None

# --- HELPER: Notificar Admin Principal ---
# --- HELPER: Notificar TODOS os Admins (Principal + Extras) ---
# --- HELPER: Notificar TODOS os Admins (Principal + Extras) ---
def notificar_admin_principal(bot_db: Bot, mensagem: str):
    """
    Envia notificação para o Admin Principal E para os Admins Extras configurados.
    """
    ids_unicos = set()

    # 1. Adiciona Admin Principal (Prioridade)
    if bot_db.admin_principal_id:
        ids_unicos.add(str(bot_db.admin_principal_id).strip())

    # 2. Adiciona Admins Extras (Com proteção contra lazy loading)
    try:
        if bot_db.admins:
            for admin in bot_db.admins:
                if admin.telegram_id:
                    ids_unicos.add(str(admin.telegram_id).strip())
    except Exception as e:
        # Se der erro ao ler admins extras (ex: sessão fechada), ignora e manda só pro principal
        logger.warning(f"Não foi possível ler admins extras: {e}")

    if not ids_unicos:
        return

    try:
        sender = telebot.TeleBot(bot_db.token)
        for chat_id in ids_unicos:
            try:
                # 🔥 GARANTE O PARSE_MODE HTML
                sender.send_message(chat_id, mensagem, parse_mode="HTML")
            except Exception as e_send:
                logger.error(f"Erro ao notificar admin {chat_id}: {e_send}")
                
    except Exception as e:
        logger.error(f"Falha geral na notificação: {e}")

# --- ROTAS DE INTEGRAÇÃO (SALVAR TOKEN) ---
# =========================================================
# 🔌 ROTAS DE INTEGRAÇÃO (SALVAR TOKEN PUSHIN PAY)
# =========================================================

# Modelo para receber o JSON do frontend
# =========================================================
# 🔌 ROTAS DE INTEGRAÇÃO (AGORA POR BOT)
# =========================================================

# Modelo para receber o JSON do frontend
class IntegrationUpdate(BaseModel):
    token: str

@app.get("/api/admin/integrations/pushinpay/{bot_id}")
def get_pushin_status(bot_id: int, db: Session = Depends(get_db)):
    # Busca o BOT específico
    bot = db.query(Bot).filter(Bot.id == bot_id).first()
    
    if not bot:
        return {"status": "erro", "msg": "Bot não encontrado"}
    
    token = bot.pushin_token
    
    # Fallback: Se não tiver no bot, tenta pegar o global antigo
    if not token:
        config = db.query(SystemConfig).filter(SystemConfig.key == "pushin_pay_token").first()
        token = config.value if config else None

    if not token:
        return {"status": "desconectado", "token_mask": ""}
    
    # Cria máscara para segurança
    mask = f"{token[:4]}...{token[-4:]}" if len(token) > 8 else "****"
    return {"status": "conectado", "token_mask": mask}

@app.post("/api/admin/integrations/pushinpay/{bot_id}")
def save_pushin_token(bot_id: int, data: IntegrationUpdate, db: Session = Depends(get_db)):
    # 1. Busca o Bot
    bot = db.query(Bot).filter(Bot.id == bot_id).first()
    if not bot:
        raise HTTPException(status_code=404, detail="Bot não encontrado")
    
    # 2. Limpa e Salva NO BOT
    token_limpo = data.token.strip()
    
    if len(token_limpo) < 10:
        return {"status": "erro", "msg": "Token muito curto ou inválido."}

    bot.pushin_token = token_limpo
    db.commit()
    
    logger.info(f"🔑 Token PushinPay atualizado para o BOT {bot.nome}: {token_limpo[:5]}...")
    
    return {"status": "conectado", "msg": f"Integração salva para {bot.nome}!"}

# --- MODELOS ---
class BotCreate(BaseModel):
    nome: str
    token: str
    id_canal_vip: str
    admin_principal_id: Optional[str] = None
    suporte_username: Optional[str] = None # 🔥 NOVO CAMPO

# Novo modelo para Atualização
class BotUpdate(BaseModel):
    nome: Optional[str] = None
    token: Optional[str] = None
    id_canal_vip: Optional[str] = None
    admin_principal_id: Optional[str] = None
    suporte_username: Optional[str] = None # 🔥 NOVO CAMPO

# Modelo para Criar Admin
class BotAdminCreate(BaseModel):
    telegram_id: str
    nome: Optional[str] = "Admin"

class BotResponse(BotCreate):
    id: int
    status: str
    leads: int = 0
    revenue: float = 0.0
    class Config:
        from_attributes = True

class PlanoCreate(BaseModel):
    bot_id: int
    nome_exibicao: str
    preco: float
    dias_duracao: int

# --- Adicione logo após a classe PlanoCreate ---
# 👇 COLE ISSO LOGO ABAIXO DA CLASS 'PlanoCreate'
# COLE AQUI (LOGO APÓS AS CLASSES INICIAIS)
class PlanoUpdate(BaseModel):
    nome_exibicao: Optional[str] = None
    preco: Optional[float] = None
    dias_duracao: Optional[int] = None
    
    # Adiciona essa config para permitir que o Pydantic ignore tipos estranhos se possível
    class Config:
        arbitrary_types_allowed = True
class FlowUpdate(BaseModel):
    msg_boas_vindas: str
    media_url: Optional[str] = None
    btn_text_1: str
    autodestruir_1: bool
    msg_2_texto: Optional[str] = None
    msg_2_media: Optional[str] = None
    mostrar_planos_2: bool
    mostrar_planos_1: Optional[bool] = False # 🔥 NOVO CAMPO

    # 🔥 NOVOS CAMPOS (ESSENCIAIS PARA O MINI APP)
    start_mode: Optional[str] = "padrao"
    miniapp_url: Optional[str] = None
    miniapp_btn_text: Optional[str] = None

class FlowStepCreate(BaseModel):
    msg_texto: str
    msg_media: Optional[str] = None
    btn_texto: str = "Próximo ▶️"
    step_order: int

class FlowStepUpdate(BaseModel):
    """Modelo para atualizar um passo existente"""
    msg_texto: Optional[str] = None
    msg_media: Optional[str] = None
    btn_texto: Optional[str] = None
    autodestruir: Optional[bool] = None      # [NOVO V3]
    mostrar_botao: Optional[bool] = None     # [NOVO V3]
    delay_seconds: Optional[int] = None  # [NOVO V4]


class UserUpdateCRM(BaseModel):
    first_name: Optional[str] = None
    username: Optional[str] = None
    # Recebe a data como string do frontend
    custom_expiration: Optional[str] = None 
    status: Optional[str] = None

# --- MODELOS ORDER BUMP ---
class OrderBumpCreate(BaseModel):
    ativo: bool
    nome_produto: str
    preco: float
    link_acesso: str
    autodestruir: Optional[bool] = False  # <--- ADICIONE AQUI
    msg_texto: Optional[str] = None
    msg_media: Optional[str] = None
    btn_aceitar: Optional[str] = "✅ SIM, ADICIONAR"
    btn_recusar: Optional[str] = "❌ NÃO, OBRIGADO"

class IntegrationUpdate(BaseModel):
    token: str

# --- MODELOS TRACKING (Certifique-se de que estão no topo, junto com os outros Pydantic models) ---
class TrackingFolderCreate(BaseModel):
    nome: str
    plataforma: str # 'facebook', 'instagram', etc

class TrackingLinkCreate(BaseModel):
    folder_id: int
    bot_id: int
    nome: str
    origem: Optional[str] = "outros" 
    codigo: Optional[str] = None

# --- MODELOS MINI APP (TEMPLATE) ---
class MiniAppConfigUpdate(BaseModel):
    # Visual
    logo_url: Optional[str] = None
    background_type: Optional[str] = None # 'solid', 'gradient', 'image'
    background_value: Optional[str] = None
    
    # Hero Section
    hero_video_url: Optional[str] = None
    hero_title: Optional[str] = None
    hero_subtitle: Optional[str] = None
    hero_btn_text: Optional[str] = None
    
    # Popup
    enable_popup: Optional[bool] = None
    popup_video_url: Optional[str] = None
    popup_text: Optional[str] = None
    
    # Footer
    footer_text: Optional[str] = None
    
    # Flags Especiais
    is_direct_checkout: bool = False
    is_hacker_mode: bool = False

    # Detalhes Visuais
    banner_desk_url: Optional[str] = None
    banner_mob_url: Optional[str] = None
    footer_banner_url: Optional[str] = None
    deco_line_url: Optional[str] = None
    
    # Conteúdo (JSON String)
    content_json: Optional[str] = "[]" # Lista de vídeos/cards

# =========================================================
# 👇 COLE ISSO NO SEU MAIN.PY (Perto da linha 630)
# =========================================================

class CategoryCreate(BaseModel):
    id: Optional[int] = None
    bot_id: int
    title: str
    slug: Optional[str] = None  # <--- GARANTINDO O SLUG AQUI
    description: Optional[str] = None
    cover_image: Optional[str] = None
    banner_mob_url: Optional[str] = None
    theme_color: Optional[str] = "#c333ff"
    is_direct_checkout: bool = False
    is_hacker_mode: bool = False
    content_json: Optional[str] = "[]"
    # --- VISUAL RICO ---
    bg_color: Optional[str] = "#000000"
    banner_desk_url: Optional[str] = None
    video_preview_url: Optional[str] = None
    model_img_url: Optional[str] = None
    model_name: Optional[str] = None
    model_desc: Optional[str] = None
    footer_banner_url: Optional[str] = None
    deco_lines_url: Optional[str] = None
    # --- NOVAS CORES ---
    model_name_color: Optional[str] = "#ffffff"
    model_desc_color: Optional[str] = "#cccccc"

# --- MODELO DE PERFIL ---
class ProfileUpdate(BaseModel):
    name: str
    avatar_url: Optional[str] = None

# ✅ MODELO COMPLETO PARA O WIZARD DE REMARKETING
# =========================================================
# ✅ MODELO DE DADOS (ESPELHO DO REMARKETING.JSX)
# =========================================================
class RemarketingRequest(BaseModel):
    bot_id: int
    # O Frontend manda 'target', contendo: 'todos', 'pendentes', 'pagantes' ou 'expirados'
    target: str = "todos" 
    mensagem: str
    media_url: Optional[str] = None
    
    # Oferta (Alinhado com o JSX)
    incluir_oferta: bool = False
    plano_oferta_id: Optional[str] = None
    
    # Preço e Validade (Alinhado com o JSX)
    price_mode: str = "original" # 'original' ou 'custom'
    custom_price: Optional[float] = 0.0
    expiration_mode: str = "none" # 'none', 'minutes', 'hours', 'days'
    expiration_value: Optional[int] = 0
    
    # Controle (Isso vem do api.js na função sendRemarketing)
    is_test: bool = False
    specific_user_id: Optional[str] = None

    # Campos de compatibilidade (Opcionais, pois seu frontend NÃO está mandando isso agora)
    tipo_envio: Optional[str] = None 
    expire_timestamp: Optional[int] = 0


# =========================================================
# 📢 ROTAS DE REMARKETING (FALTANDO)
# =========================================================

# --- NOVA ROTA: DISPARO INDIVIDUAL (VIA HISTÓRICO) ---
class IndividualRemarketingRequest(BaseModel):
    bot_id: int
    user_telegram_id: str
    campaign_history_id: int # ID do histórico para copiar a msg

# Modelo para envio
class RemarketingSend(BaseModel):
    bot_id: int
    target: str # 'todos', 'topo', 'meio', 'fundo', 'expirados'
    mensagem: str
    media_url: Optional[str] = None
    incluir_oferta: bool = False
    plano_oferta_id: Optional[str] = None # Pode vir como string do front
    agendar: bool = False
    data_agendamento: Optional[datetime] = None
    is_test: bool = False
    specific_user_id: Optional[str] = None

@app.post("/api/admin/bots/{bot_id}/remarketing/send")
def send_remarketing(
    bot_id: int, 
    data: RemarketingSend, 
    background_tasks: BackgroundTasks, 
    db: Session = Depends(get_db),
    current_user = Depends(get_current_user) # 🔒 AUTH
):
    # 🔒 VERIFICA PROPRIEDADE
    verificar_bot_pertence_usuario(bot_id, current_user.id, db)

    try:
        logger.info(f"📢 Iniciando Remarketing para Bot {bot_id} | Target: {data.target}")
        
        # 1. Configura a Campanha
        campaign_id = str(uuid.uuid4())
        nova_campanha = RemarketingCampaign(
            bot_id=bot_id,
            campaign_id=campaign_id,
            target=data.target,
            type='teste' if data.is_test else 'massivo',
            config=json.dumps({
                "mensagem": data.mensagem,
                "media": data.media_url,
                "oferta": data.incluir_oferta,
                "plano_id": data.plano_oferta_id
            }),
            status='agendado' if data.agendar else 'enviando',
            data_envio=datetime.utcnow()
        )
        db.add(nova_campanha)
        db.commit()

        # 2. Se for teste, envia só para o admin/user específico
        if data.is_test:
            target_id = data.specific_user_id
            if not target_id:
                # Tenta pegar o admin do bot
                bot = db.query(Bot).filter(Bot.id == bot_id).first()
                target_id = bot.admin_principal_id
            
            if target_id:
                background_tasks.add_task(
                    disparar_mensagem_individual, 
                    bot_id, 
                    target_id, 
                    data.mensagem, 
                    data.media_url
                )
                return {"status": "success", "message": f"Teste enviado para {target_id}"}
            else:
                return {"status": "error", "message": "Nenhum ID definido para teste"}

        # 3. Se for envio real (Massivo)
        if not data.agendar:
            background_tasks.add_task(processar_remarketing_massivo, campaign_id, db)
        
        return {"status": "success", "campaign_id": campaign_id}

    except Exception as e:
        logger.error(f"Erro no remarketing: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/api/admin/bots/{bot_id}/remarketing/history")
def get_remarketing_history(bot_id: int, page: int = 1, limit: int = 10, db: Session = Depends(get_db)):
    try:
        # Garante limites seguros
        limit = min(limit, 50)
        skip = (page - 1) * limit
        
        # Query
        query = db.query(RemarketingCampaign).filter(RemarketingCampaign.bot_id == bot_id)
        
        total = query.count()
        campanhas = query.order_by(desc(RemarketingCampaign.data_envio)).offset(skip).limit(limit).all()
            
        # Formata Resposta
        data = []
        for c in campanhas:
            data.append({
                "id": c.id,
                "data": c.data_envio,
                "target": c.target,
                "total": c.total_leads,
                "sent_success": c.sent_success, # Importante: Garante nome correto
                "blocked_count": c.blocked_count, # Importante: Garante nome correto
                "config": c.config
            })

        return {
            "data": data,
            "total": total,
            "page": page,
            "total_pages": (total // limit) + (1 if total % limit > 0 else 0)
        }
    except Exception as e:
        logger.error(f"Erro ao buscar histórico: {e}")
        return {"data": [], "total": 0, "page": 1, "total_pages": 0}

# Função Auxiliar (Adicione se não existir)
def processar_remarketing_massivo(campaign_id: str, db: Session):
    # Lógica simplificada de disparo (você pode expandir depois)
    logger.info(f"🚀 Processando campanha {campaign_id}...")
    # Aqui iria a lógica de buscar usuários e loop de envio
    pass

    # ---   
# Modelo para Atualização de Usuário (CRM)
class UserUpdate(BaseModel):
    role: Optional[str] = None
    status: Optional[str] = None
    custom_expiration: Optional[str] = None # 'vitalicio', 'remover' ou data YYYY-MM-DD

# =========================================================
# 💰 ROTA DE PAGAMENTO (PIX) - CRÍTICO PARA O MINI APP
# =========================================================

# Modelo de dados recebido do Frontend
class PixCreateRequest(BaseModel):
    bot_id: int
    plano_id: int
    plano_nome: str
    valor: float
    telegram_id: str
    first_name: str
    username: str
    tem_order_bump: bool = False

# =========================================================
# 1. GERAÇÃO DE PIX (COM LIMPEZA DE DADOS)
# =========================================================
# =========================================================
# 1. GERAÇÃO DE PIX (COM LIMPEZA)
# =========================================================
@app.post("/api/pagamento/pix")
def gerar_pix(data: PixCreateRequest, db: Session = Depends(get_db)):
    try:
        logger.info(f"💰 Iniciando pagamento para: {data.first_name} (R$ {data.valor})")
        bot_atual = db.query(Bot).filter(Bot.id == data.bot_id).first()
        pushin_token = bot_atual.pushin_token if bot_atual else None
        
        if not pushin_token:
            config_sys = db.query(SystemConfig).filter(SystemConfig.key == "pushin_pay_token").first()
            pushin_token = config_sys.value if (config_sys and config_sys.value) else os.getenv("PUSHIN_PAY_TOKEN")

        user_clean = str(data.username).strip().lower().replace("@", "") if data.username else "anonimo"
        tid_clean = str(data.telegram_id).strip()
        if not tid_clean.isdigit(): tid_clean = user_clean

        if not pushin_token:
            fake_txid = str(uuid.uuid4())
            novo_pedido = Pedido(
                bot_id=data.bot_id, telegram_id=tid_clean, first_name=data.first_name, username=user_clean,   
                valor=data.valor, status='pending', plano_id=data.plano_id, plano_nome=data.plano_nome,
                txid=fake_txid, qr_code="pix-fake-copia-cola", transaction_id=fake_txid, tem_order_bump=data.tem_order_bump
            )
            db.add(novo_pedido)
            db.commit()
            return {"txid": fake_txid, "copia_cola": "pix-fake", "qr_code": "https://fake.com/qr.png"}

        url = "https://api.pushinpay.com.br/api/pix/cashIn"
        headers = { "Authorization": f"Bearer {pushin_token}", "Content-Type": "application/json", "Accept": "application/json" }
        domain = os.getenv("RAILWAY_PUBLIC_DOMAIN", "zenyx-gbs-testesv1-production.up.railway.app/")
        if domain.startswith("https://"): domain = domain.replace("https://", "")
        
        payload = {
            "value": int(data.valor * 100),
            "webhook_url": f"https://{domain}/webhook/pix",
            "external_reference": f"bot_{data.bot_id}_{user_clean}_{int(time.time())}"
        }

        req = requests.post(url, json=payload, headers=headers)
        if req.status_code in [200, 201]:
            resp = req.json()
            txid = str(resp.get('id') or resp.get('txid'))
            copia_cola = resp.get('qr_code_text') or resp.get('pixCopiaEcola')
            qr_image = resp.get('qr_code_image_url') or resp.get('qr_code')

            novo_pedido = Pedido(
                bot_id=data.bot_id, telegram_id=tid_clean, first_name=data.first_name, username=user_clean,
                valor=data.valor, status='pending', plano_id=data.plano_id, plano_nome=data.plano_nome,
                txid=txid, qr_code=qr_image, transaction_id=txid, tem_order_bump=data.tem_order_bump
            )
            db.add(novo_pedido)
            db.commit()
            return {"txid": txid, "copia_cola": copia_cola, "qr_code": qr_image}
        else:
            logger.error(f"Erro PushinPay: {req.text}")
            raise HTTPException(status_code=400, detail="Erro Gateway")
    except Exception as e:
        logger.error(f"Erro fatal PIX: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/api/pagamento/status/{txid}")
def check_status(txid: str, db: Session = Depends(get_db)):
    pedido = db.query(Pedido).filter((Pedido.txid == txid) | (Pedido.transaction_id == txid)).first()
    if not pedido: return {"status": "not_found"}
    return {"status": pedido.status}

# =========================================================
# 🔐 ROTAS DE AUTENTICAÇÃO
# =========================================================

@app.post("/api/auth/register", response_model=Token, tags=["Autenticação"])
async def register(user_data: UserCreate):
    """
    Registra um novo usuário no sistema
    """
    db = SessionLocal()
    try:
        from database import User
        
        # Verifica se usuário já existe
        existing_user = db.query(User).filter(
            (User.username == user_data.username) | (User.email == user_data.email)
        ).first()
        
        if existing_user:
            raise HTTPException(status_code=400, detail="Usuário ou email já cadastrado")
        
        # Cria novo usuário
        hashed_password = get_password_hash(user_data.password)
        new_user = User(
            username=user_data.username,
            email=user_data.email,
            password_hash=hashed_password,
            full_name=user_data.full_name
        )
        
        db.add(new_user)
        db.commit()
        db.refresh(new_user)
        
        # Gera token de acesso
        access_token_expires = timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES)
        access_token = create_access_token(
            data={"sub": new_user.username, "user_id": new_user.id},
            expires_delta=access_token_expires
        )
        
        print(f"✅ Novo usuário registrado: {new_user.username} (ID: {new_user.id})")
        
        return {
            "access_token": access_token,
            "token_type": "bearer",
            "user_id": new_user.id,
            "username": new_user.username
        }
        
    except HTTPException:
        raise
    except Exception as e:
        db.rollback()
        print(f"❌ Erro ao registrar usuário: {str(e)}")
        raise HTTPException(status_code=500, detail=f"Erro ao registrar usuário: {str(e)}")
    finally:
        db.close()


@app.post("/api/auth/login", response_model=Token, tags=["Autenticação"])
async def login(user_data: UserLogin):
    """
    Realiza login e retorna token JWT
    """
    db = SessionLocal()
    try:
        from database import User
        
        # Busca usuário
        user = db.query(User).filter(User.username == user_data.username).first()
        
        if not user:
            raise HTTPException(status_code=401, detail="Usuário ou senha incorretos")
        
        # Verifica senha
        if not verify_password(user_data.password, user.password_hash):
            raise HTTPException(status_code=401, detail="Usuário ou senha incorretos")
        
        # Verifica se está ativo
        if not user.is_active:
            raise HTTPException(status_code=403, detail="Usuário inativo")
        
        # Gera token
        access_token_expires = timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES)
        access_token = create_access_token(
            data={"sub": user.username, "user_id": user.id},
            expires_delta=access_token_expires
        )
        
        print(f"✅ Login realizado: {user.username} (ID: {user.id})")
        
        return {
            "access_token": access_token,
            "token_type": "bearer",
            "user_id": user.id,
            "username": user.username
        }
        
    except HTTPException:
        raise
    except Exception as e:
        print(f"❌ Erro ao fazer login: {str(e)}")
        raise HTTPException(status_code=500, detail="Erro ao fazer login")
    finally:
        db.close()


@app.get("/api/auth/me", tags=["Autenticação"])
async def get_me(current_user = Depends(get_current_user)):
    """
    Retorna informações do usuário logado
    """
    return {
        "id": current_user.id,
        "username": current_user.username,
        "email": current_user.email,
        "full_name": current_user.full_name,
        "is_active": current_user.is_active
    }

# =========================================================
# ⚙️ HELPER: CONFIGURAR MENU (COMANDOS)
# =========================================================
def configurar_menu_bot(token):
    try:
        tb = telebot.TeleBot(token)
        tb.set_my_commands([
            telebot.types.BotCommand("start", "🚀 Iniciar"),
            telebot.types.BotCommand("suporte", "💬 Falar com Suporte"),
            telebot.types.BotCommand("status", "⭐ Minha Assinatura")
        ])
        logger.info(f"✅ Menu de comandos configurado para o token {token[:10]}...")
    except Exception as e:
        logger.error(f"❌ Erro ao configurar menu: {e}")

# ===========================
# ⚙️ GESTÃO DE BOTS
# ===========================

@app.post("/api/admin/bots", response_model=BotResponse)
def criar_bot(
    bot_data: BotCreate, 
    db: Session = Depends(get_db),
    current_user = Depends(get_current_user)  # 🔒 ADICIONA AUTH
):
    if db.query(Bot).filter(Bot.token == bot_data.token).first():
        raise HTTPException(status_code=400, detail="Token já cadastrado.")

    try:
        tb = telebot.TeleBot(bot_data.token)
        bot_info = tb.get_me()
        username = bot_info.username if hasattr(bot_info, 'username') else None
        
        # Configura webhook
        public_url = os.getenv("RAILWAY_PUBLIC_DOMAIN", "")
        if public_url:
            webhook_url = f"https://{public_url}/webhook/{bot_data.token}"
            tb.set_webhook(url=webhook_url)
        
        # 🔥 CONFIGURA MENU
        configurar_menu_bot(bot_data.token)
        
        status = "ativo"
    except Exception as e:
        logger.error(f"Erro ao criar bot: {e}")
        raise HTTPException(status_code=400, detail="Token inválido ou erro ao conectar.")

    novo_bot = Bot(
        nome=bot_data.nome,
        token=bot_data.token,
        username=username,
        id_canal_vip=bot_data.id_canal_vip,
        status=status,
        admin_principal_id=bot_data.admin_principal_id,
        suporte_username=bot_data.suporte_username,
        owner_id=current_user.id  # 🔒 ATRIBUI AO USUÁRIO ATUAL
    )
    db.add(novo_bot)
    db.commit()
    db.refresh(novo_bot)
    
    logger.info(f"✅ Bot criado: {novo_bot.nome} (Owner: {current_user.username})")
    
    return {
        "id": novo_bot.id,
        "nome": novo_bot.nome,
        "token": novo_bot.token,
        "username": novo_bot.username,
        "id_canal_vip": novo_bot.id_canal_vip,
        "admin_principal_id": novo_bot.admin_principal_id,
        "status": novo_bot.status,
        "leads": 0,
        "revenue": 0.0,
        "created_at": novo_bot.created_at
    }

@app.put("/api/admin/bots/{bot_id}")
def update_bot(bot_id: int, dados: BotUpdate, db: Session = Depends(get_db)):
    bot_db = db.query(Bot).filter(Bot.id == bot_id).first()
    if not bot_db: raise HTTPException(404, "Bot não encontrado")
    
    old_token = bot_db.token

    # 1. Atualiza campos administrativos
    if dados.id_canal_vip: bot_db.id_canal_vip = dados.id_canal_vip
    if dados.admin_principal_id is not None: bot_db.admin_principal_id = dados.admin_principal_id
    if dados.suporte_username is not None: bot_db.suporte_username = dados.suporte_username # 🔥 ATUALIZA
    
    # 2. LÓGICA DE TROCA DE TOKEN
    if dados.token and dados.token != old_token:
        try:
            logger.info(f"🔄 Detectada troca de token para o bot ID {bot_id}...")
            new_tb = telebot.TeleBot(dados.token)
            bot_info = new_tb.get_me()
            
            bot_db.token = dados.token
            bot_db.nome = bot_info.first_name
            bot_db.username = bot_info.username
            
            try:
                old_tb = telebot.TeleBot(old_token)
                old_tb.delete_webhook()
            except: pass

            public_url = os.getenv("RAILWAY_PUBLIC_DOMAIN", "https://zenyx-gbs-testesv1-production.up.railway.app")
            if public_url.startswith("https://"): public_url = public_url.replace("https://", "")
            
            webhook_url = f"https://{public_url}/webhook/{dados.token}"
            new_tb.set_webhook(url=webhook_url)
            
            bot_db.status = "ativo"
            
        except Exception as e:
            raise HTTPException(status_code=400, detail=f"Token inválido: {str(e)}")
            
    else:
        if dados.nome: bot_db.nome = dados.nome
    
    # 🔥 ATUALIZA O MENU SEMPRE QUE SALVAR
    configurar_menu_bot(bot_db.token)
    
    db.commit()
    db.refresh(bot_db)
    return {"status": "ok", "msg": "Bot atualizado com sucesso"}

# --- NOVA ROTA: LIGAR/DESLIGAR BOT (TOGGLE) ---
# --- NOVA ROTA: LIGAR/DESLIGAR BOT (TOGGLE) ---
@app.post("/api/admin/bots/{bot_id}/toggle")
def toggle_bot(
    bot_id: int, 
    db: Session = Depends(get_db),
    current_user = Depends(get_current_user)  # 🔒 ADICIONA AUTH
):
    # 🔒 VERIFICA SE PERTENCE AO USUÁRIO
    bot = verificar_bot_pertence_usuario(bot_id, current_user.id, db)
    
    # Inverte o status
    novo_status = "ativo" if bot.status != "ativo" else "pausado"
    bot.status = novo_status
    db.commit()
    
    # 🔔 Notifica Admin (EM HTML)
    try:
        emoji = "🟢" if novo_status == "ativo" else "🔴"
        msg = f"{emoji} <b>STATUS DO BOT ALTERADO</b>\n\nO bot <b>{bot.nome}</b> agora está: <b>{novo_status.upper()}</b>"
        notificar_admin_principal(bot, msg)
    except Exception as e:
        logger.error(f"Erro ao notificar admin sobre toggle: {e}")
    
    logger.info(f"🔄 Bot toggled: {bot.nome} -> {novo_status} (Owner: {current_user.username})")
    
    return {"status": novo_status}

# --- NOVA ROTA: EXCLUIR BOT ---
# --- NOVA ROTA: EXCLUIR BOT (COM LIMPEZA TOTAL) ---
@app.delete("/api/admin/bots/{bot_id}")
def deletar_bot(
    bot_id: int, 
    db: Session = Depends(get_db),
    current_user = Depends(get_current_user)  # 🔒 ADICIONA AUTH
):
    # 🔒 VERIFICA SE PERTENCE AO USUÁRIO
    bot_db = verificar_bot_pertence_usuario(bot_id, current_user.id, db)
    
    try:
        # 1. Tenta remover o Webhook do Telegram para limpar no servidor deles
        try:
            tb = telebot.TeleBot(bot_db.token)
            tb.delete_webhook()
        except:
            pass  # Se der erro (ex: token inválido), continua e apaga do banco
        
        # 2. LIMPEZA PESADA (Exclui manualmente os dependentes para evitar erro de integridade)
        # Apaga PEDIDOS vinculados
        db.query(Pedido).filter(Pedido.bot_id == bot_id).delete(synchronize_session=False)
        
        # Apaga LEADS vinculados
        db.query(Lead).filter(Lead.bot_id == bot_id).delete(synchronize_session=False)
        
        # Apaga CAMPANHAS de Remarketing vinculadas
        db.query(RemarketingCampaign).filter(RemarketingCampaign.bot_id == bot_id).delete(synchronize_session=False)
        
        # 3. Apaga o Bot (Planos, Fluxos e Admins já caem por cascade)
        db.delete(bot_db)
        db.commit()
        
        logger.info(f"🗑 Bot {bot_id} e todos os seus dados foram excluídos com sucesso (Owner: {current_user.username})")
        
        return {"status": "deleted", "msg": "Bot e todos os dados vinculados removidos com sucesso"}
        
    except Exception as e:
        db.rollback()  # Desfaz se der erro no meio do caminho
        logger.error(f"Erro ao deletar bot {bot_id}: {e}")
        raise HTTPException(status_code=500, detail=f"Erro interno ao excluir bot: {str(e)}")

# =========================================================
# 🛡️ GESTÃO DE ADMINISTRADORES (FASE 2 - COM EDIÇÃO)
# =========================================================

# =========================================================
# 🛡️ GESTÃO DE ADMINISTRADORES (BLINDADO)
# =========================================================

@app.get("/api/admin/bots/{bot_id}/admins")
def listar_admins(
    bot_id: int, 
    db: Session = Depends(get_db),
    current_user = Depends(get_current_user) # 🔒 AUTH
):
    # 🔒 VERIFICA PROPRIEDADE
    verificar_bot_pertence_usuario(bot_id, current_user.id, db)
    
    admins = db.query(BotAdmin).filter(BotAdmin.bot_id == bot_id).all()
    return admins

@app.post("/api/admin/bots/{bot_id}/admins")
def adicionar_admin(
    bot_id: int, 
    dados: BotAdminCreate, 
    db: Session = Depends(get_db),
    current_user = Depends(get_current_user) # 🔒 AUTH
):
    # 🔒 VERIFICA PROPRIEDADE
    verificar_bot_pertence_usuario(bot_id, current_user.id, db)
    
    # Verifica duplicidade
    existente = db.query(BotAdmin).filter(
        BotAdmin.bot_id == bot_id, 
        BotAdmin.telegram_id == dados.telegram_id
    ).first()
    
    if existente:
        raise HTTPException(status_code=400, detail="Este ID já é administrador deste bot.")
    
    novo_admin = BotAdmin(bot_id=bot_id, telegram_id=dados.telegram_id, nome=dados.nome)
    db.add(novo_admin)
    db.commit()
    db.refresh(novo_admin)
    return novo_admin

@app.put("/api/admin/bots/{bot_id}/admins/{admin_id}")
def atualizar_admin(
    bot_id: int, 
    admin_id: int, 
    dados: BotAdminCreate, 
    db: Session = Depends(get_db),
    current_user = Depends(get_current_user) # 🔒 AUTH
):
    # 🔒 VERIFICA PROPRIEDADE
    verificar_bot_pertence_usuario(bot_id, current_user.id, db)
    
    admin_db = db.query(BotAdmin).filter(BotAdmin.id == admin_id, BotAdmin.bot_id == bot_id).first()
    if not admin_db:
        raise HTTPException(status_code=404, detail="Administrador não encontrado")
    
    # Atualiza dados
    admin_db.telegram_id = dados.telegram_id
    admin_db.nome = dados.nome
    db.commit()
    return admin_db

@app.delete("/api/admin/bots/{bot_id}/admins/{telegram_id}")
def remover_admin(
    bot_id: int, 
    telegram_id: str, 
    db: Session = Depends(get_db),
    current_user = Depends(get_current_user) # 🔒 AUTH
):
    # 🔒 VERIFICA PROPRIEDADE
    verificar_bot_pertence_usuario(bot_id, current_user.id, db)
    
    admin_db = db.query(BotAdmin).filter(BotAdmin.bot_id == bot_id, BotAdmin.telegram_id == telegram_id).first()
    if not admin_db:
        raise HTTPException(status_code=404, detail="Administrador não encontrado")
    
    db.delete(admin_db)
    db.commit()
    return {"status": "deleted"}

# --- NOVA ROTA: LISTAR BOTS ---

# =========================================================
# 🤖 LISTAR BOTS (COM KPI TOTAIS E USERNAME CORRIGIDO)
# =========================================================
# ============================================================
# 🔥 ROTA CORRIGIDA: /api/admin/bots
# SUBSTITUA a rota existente no main.py
# CORRIGE: Conta LEADS + PEDIDOS (sem duplicatas)
# ============================================================

@app.get("/api/admin/bots")
def listar_bots(
    db: Session = Depends(get_db),
    current_user = Depends(get_current_user)  # 🔒 ADICIONA AUTH
):
    """
    🔥 [CORRIGIDO] Lista bots + Revenue (Pagos/Expirados) + Suporte Username
    🔒 PROTEGIDO: Apenas bots do usuário logado
    """
    # 🔒 FILTRA APENAS BOTS DO USUÁRIO
    bots = db.query(Bot).filter(Bot.owner_id == current_user.id).all()
    
    # ... RESTO DO CÓDIGO PERMANECE IGUAL (não mude nada abaixo daqui)
    result = []
    for bot in bots:
        # 1. CONTAGEM DE LEADS ÚNICOS
        leads_ids = set()
        leads_query = db.query(Lead.user_id).filter(Lead.bot_id == bot.id).all()
        for lead in leads_query:
            leads_ids.add(str(lead.user_id))
        
        pedidos_ids = set()
        pedidos_query = db.query(Pedido.telegram_id).filter(Pedido.bot_id == bot.id).all()
        for pedido in pedidos_query:
            pedidos_ids.add(str(pedido.telegram_id))
        
        contatos_unicos = leads_ids.union(pedidos_ids)
        leads_count = len(contatos_unicos)
        
        # 2. REVENUE (PAGOS + EXPIRADOS)
        status_financeiro = ["approved", "paid", "active", "completed", "succeeded", "expired"]
        vendas_aprovadas = db.query(Pedido).filter(
            Pedido.bot_id == bot.id,
            Pedido.status.in_(status_financeiro)
        ).all()
        
        revenue = sum([v.valor for v in vendas_aprovadas]) if vendas_aprovadas else 0.0
        
        result.append({
            "id": bot.id,
            "nome": bot.nome,
            "token": bot.token,
            "username": bot.username or None,
            "id_canal_vip": bot.id_canal_vip,
            "admin_principal_id": bot.admin_principal_id,
            "suporte_username": bot.suporte_username,
            "status": bot.status,
            "leads": leads_count,
            "revenue": revenue,
            "created_at": bot.created_at
        })
    
    return result

# ===========================
# 💎 PLANOS & FLUXO
# ===========================

# =========================================================
# 💲 GERENCIAMENTO DE PLANOS (CRUD COMPLETO)
# =========================================================

# 1. LISTAR PLANOS
# =========================================================
# 💎 GERENCIAMENTO DE PLANOS (CORRIGIDO E UNIFICADO)
# =========================================================

# 1. LISTAR PLANOS
@app.get("/api/admin/bots/{bot_id}/plans")
def list_plans(
    bot_id: int, 
    db: Session = Depends(get_db),
    current_user = Depends(get_current_user)  # 🔒 ADICIONA AUTH
):
    # 🔒 VERIFICA SE O BOT PERTENCE AO USUÁRIO
    verificar_bot_pertence_usuario(bot_id, current_user.id, db)
    
    # ... RESTO DO CÓDIGO PERMANECE IGUAL
    planos = db.query(PlanoConfig).filter(PlanoConfig.bot_id == bot_id).all()
    return planos

# 2. CRIAR PLANO (CORRIGIDO)
@app.post("/api/admin/bots/{bot_id}/plans")
async def create_plan(
    bot_id: int, 
    req: Request, 
    db: Session = Depends(get_db),
    current_user = Depends(get_current_user)  # 🔒 ADICIONA AUTH
):
    # 🔒 VERIFICA SE O BOT PERTENCE AO USUÁRIO
    verificar_bot_pertence_usuario(bot_id, current_user.id, db)
    
    # ... RESTO DO CÓDIGO PERMANECE EXATAMENTE IGUAL (NÃO MUDE NADA ABAIXO)
    try:
        data = await req.json()
        logger.info(f"📝 Criando plano para Bot {bot_id}: {data}")
        
        # Tenta pegar preco_original, se não tiver, usa 0.0
        preco_orig = float(data.get("preco_original", 0.0))
        
        # Se o preço original for 0, define como o dobro do atual (padrão de marketing)
        if preco_orig == 0:
            preco_orig = float(data.get("preco_atual", 0.0)) * 2
        
        novo_plano = PlanoConfig(
            bot_id=bot_id,
            nome_exibicao=data.get("nome_exibicao", "Novo Plano"),
            descricao=data.get("descricao", f"Acesso de {data.get('dias_duracao')} dias"),
            preco_atual=float(data.get("preco_atual", 0.0)),
            preco_cheio=preco_orig,
            dias_duracao=int(data.get("dias_duracao", 30)),
            key_id=f"plan_{bot_id}_{int(time.time())}"
        )
        
        db.add(novo_plano)
        db.commit()
        db.refresh(novo_plano)
        
        return novo_plano
        
    except TypeError as te:
        logger.warning(f"⚠️ Tentando criar plano sem 'preco_cheio' devido a erro: {te}")
        db.rollback()
        try:
            novo_plano_fallback = PlanoConfig(
                bot_id=bot_id,
                nome_exibicao=data.get("nome_exibicao"),
                descricao=data.get("descricao"),
                preco_atual=float(data.get("preco_atual")),
                dias_duracao=int(data.get("dias_duracao")),
                key_id=f"plan_{bot_id}_{int(time.time())}"
            )
            db.add(novo_plano_fallback)
            db.commit()
            db.refresh(novo_plano_fallback)
            return novo_plano_fallback
        except Exception as e2:
            logger.error(f"Erro fatal ao criar plano: {e2}")
            raise HTTPException(status_code=500, detail=str(e2))
    except Exception as e:
        logger.error(f"Erro genérico ao criar plano: {e}")
        raise HTTPException(status_code=500, detail=str(e))

# 3. EDITAR PLANO (ROTA UNIFICADA)
@app.put("/api/admin/bots/{bot_id}/plans/{plano_id}")
async def update_plan(
    bot_id: int, 
    plano_id: int, 
    req: Request, 
    db: Session = Depends(get_db),
    current_user = Depends(get_current_user)  # 🔒 ADICIONA AUTH
):
    # 🔒 VERIFICA SE O BOT PERTENCE AO USUÁRIO
    verificar_bot_pertence_usuario(bot_id, current_user.id, db)
    
    try:
        data = await req.json()
        logger.info(f"✏️ Editando plano {plano_id} do Bot {bot_id}: {data} (Owner: {current_user.username})")
        
        plano = db.query(PlanoConfig).filter(
            PlanoConfig.id == plano_id, 
            PlanoConfig.bot_id == bot_id
        ).first()
        
        if not plano:
            raise HTTPException(status_code=404, detail="Plano não encontrado.")
            
        # Atualiza campos se existirem no payload
        if "nome_exibicao" in data: plano.nome_exibicao = data["nome_exibicao"]
        if "descricao" in data: plano.descricao = data["descricao"]
        if "preco_atual" in data: plano.preco_atual = float(data["preco_atual"])
        if "preco_original" in data: plano.preco_original = float(data["preco_original"])
        if "dias_duracao" in data: plano.dias_duracao = int(data["dias_duracao"])
        
        db.commit()
        db.refresh(plano)
        
        logger.info(f"✅ Plano {plano_id} atualizado com sucesso")
        
        return plano
        
    except HTTPException as he:
        raise he
    except Exception as e:
        logger.error(f"Erro ao editar plano: {e}")
        raise HTTPException(status_code=500, detail=str(e))

# 4. DELETAR PLANO (COM SEGURANÇA)
@app.delete("/api/admin/plans/{pid}")
def del_plano(
    pid: int, 
    db: Session = Depends(get_db),
    current_user = Depends(get_current_user)  # 🔒 ADICIONA AUTH
):
    try:
        # 1. Busca o plano
        p = db.query(PlanoConfig).filter(PlanoConfig.id == pid).first()
        if not p:
            return {"status": "deleted", "msg": "Plano não existia"}
        
        # 🔒 VERIFICA SE O BOT DO PLANO PERTENCE AO USUÁRIO
        verificar_bot_pertence_usuario(p.bot_id, current_user.id, db)
        
        # 2. Desvincula de Campanhas de Remarketing (Para não travar)
        db.query(RemarketingCampaign).filter(RemarketingCampaign.plano_id == pid).update(
            {RemarketingCampaign.plano_id: None},
            synchronize_session=False
        )
        
        # 3. Desvincula de Pedidos/Vendas (Para manter o histórico mas permitir deletar)
        db.query(Pedido).filter(Pedido.plano_id == pid).update(
            {Pedido.plano_id: None},
            synchronize_session=False
        )
        
        # 4. Deleta o plano
        db.delete(p)
        db.commit()
        
        logger.info(f"🗑️ Plano deletado: {pid} (Owner: {current_user.username})")
        
        return {"status": "deleted"}
        
    except Exception as e:
        logger.error(f"Erro ao deletar plano {pid}: {e}")
        raise HTTPException(status_code=400, detail=f"Erro ao deletar: {str(e)}")

# =========================================================
# 🛒 ORDER BUMP API
# =========================================================
# =========================================================
# 🛒 ORDER BUMP API (BLINDADO)
# =========================================================
@app.get("/api/admin/bots/{bot_id}/order-bump")
def get_order_bump(
    bot_id: int, 
    db: Session = Depends(get_db),
    current_user = Depends(get_current_user) # 🔒 AUTH
):
    # 🔒 VERIFICA PROPRIEDADE
    verificar_bot_pertence_usuario(bot_id, current_user.id, db)
    
    bump = db.query(OrderBumpConfig).filter(OrderBumpConfig.bot_id == bot_id).first()
    if not bump:
        return {
            "ativo": False, "nome_produto": "", "preco": 0.0, "link_acesso": "",
            "msg_texto": "", "msg_media": "", 
            "btn_aceitar": "✅ SIM, ADICIONAR", "btn_recusar": "❌ NÃO, OBRIGADO"
        }
    return bump

@app.post("/api/admin/bots/{bot_id}/order-bump")
def save_order_bump(
    bot_id: int, 
    dados: OrderBumpCreate, 
    db: Session = Depends(get_db),
    current_user = Depends(get_current_user) # 🔒 AUTH
):
    # 🔒 VERIFICA PROPRIEDADE
    verificar_bot_pertence_usuario(bot_id, current_user.id, db)
    
    bump = db.query(OrderBumpConfig).filter(OrderBumpConfig.bot_id == bot_id).first()
    if not bump:
        bump = OrderBumpConfig(bot_id=bot_id)
        db.add(bump)
    
    bump.ativo = dados.ativo
    bump.nome_produto = dados.nome_produto
    bump.preco = dados.preco
    bump.link_acesso = dados.link_acesso
    bump.autodestruir = dados.autodestruir
    bump.msg_texto = dados.msg_texto
    bump.msg_media = dados.msg_media
    bump.btn_aceitar = dados.btn_aceitar
    bump.btn_recusar = dados.btn_recusar
    
    db.commit()
    return {"status": "ok"}

# =========================================================
# 🗑️ ROTA DELETAR PLANO (COM DESVINCULAÇÃO SEGURA)
# =========================================================
@app.delete("/api/admin/plans/{pid}")
def del_plano(pid: int, db: Session = Depends(get_db)):
    try:
        # 1. Busca o plano
        p = db.query(PlanoConfig).filter(PlanoConfig.id == pid).first()
        if not p:
            return {"status": "deleted", "msg": "Plano não existia"}

        # 2. Desvincula de Campanhas de Remarketing (Para não travar)
        db.query(RemarketingCampaign).filter(RemarketingCampaign.plano_id == pid).update(
            {RemarketingCampaign.plano_id: None}, 
            synchronize_session=False
        )

        # 3. Desvincula de Pedidos/Vendas (Para manter o histórico mas permitir deletar)
        db.query(Pedido).filter(Pedido.plano_id == pid).update(
            {Pedido.plano_id: None}, 
            synchronize_session=False
        )

        # 4. Deleta o plano
        db.delete(p)
        db.commit()
        
        return {"status": "deleted"}
        
    except Exception as e:
        logger.error(f"Erro ao deletar plano {pid}: {e}")
        raise HTTPException(status_code=400, detail=f"Erro ao deletar: {str(e)}")

# --- ROTA NOVA: ATUALIZAR PLANO ---
@app.put("/api/admin/plans/{plan_id}")
def atualizar_plano(
    plan_id: int, 
    dados: PlanoUpdate, 
    db: Session = Depends(get_db),
    current_user = Depends(get_current_user)  # 🔒 ADICIONA AUTH
):
    plano = db.query(PlanoConfig).filter(PlanoConfig.id == plan_id).first()
    if not plano:
        raise HTTPException(status_code=404, detail="Plano não encontrado")
    
    # 🔒 VERIFICA SE O BOT DO PLANO PERTENCE AO USUÁRIO
    verificar_bot_pertence_usuario(plano.bot_id, current_user.id, db)
    
    # Atualiza apenas se o campo foi enviado e não é None
    if dados.nome_exibicao is not None:
        plano.nome_exibicao = dados.nome_exibicao
    if dados.preco is not None:
        plano.preco_atual = dados.preco
        plano.preco_cheio = dados.preco * 2
    if dados.dias_duracao is not None:
        plano.dias_duracao = dados.dias_duracao
        plano.key_id = f"plan_{plano.bot_id}_{dados.dias_duracao}d"
        plano.descricao = f"Acesso de {dados.dias_duracao} dias"
    
    db.commit()
    db.refresh(plano)
    
    logger.info(f"✏️ Plano atualizado (rota legada): {plano.nome_exibicao} (Owner: {current_user.username})")
    
    return {"status": "success", "msg": "Plano atualizado"}

# =========================================================
# 💬 FLUXO DO BOT (V2)
# =========================================================
@app.get("/api/admin/bots/{bot_id}/flow")
def obter_fluxo(
    bot_id: int, 
    db: Session = Depends(get_db),
    current_user = Depends(get_current_user)  # 🔒 ADICIONA AUTH
):
    # 🔒 VERIFICA SE O BOT PERTENCE AO USUÁRIO
    verificar_bot_pertence_usuario(bot_id, current_user.id, db)
    
    # ... RESTO DO CÓDIGO PERMANECE IGUAL
    fluxo = db.query(BotFlow).filter(BotFlow.bot_id == bot_id).first()
    
    if not fluxo:
        # Retorna padrão se não existir
        return {
            "msg_boas_vindas": "Olá! Seja bem-vindo(a).",
            "media_url": "",
            "btn_text_1": "🔓 DESBLOQUEAR ACESSO",
            "autodestruir_1": False,
            "msg_2_texto": "Escolha seu plano abaixo:",
            "msg_2_media": "",
            "mostrar_planos_2": True,
            "mostrar_planos_1": False,
            "start_mode": "padrao",
            "miniapp_url": "",
            "miniapp_btn_text": "ABRIR LOJA"
        }
    
    return fluxo

class FlowUpdate(BaseModel):
    msg_boas_vindas: Optional[str] = None
    media_url: Optional[str] = None
    btn_text_1: Optional[str] = None
    autodestruir_1: Optional[bool] = False
    msg_2_texto: Optional[str] = None
    msg_2_media: Optional[str] = None
    mostrar_planos_2: Optional[bool] = True
    mostrar_planos_1: Optional[bool] = False
    start_mode: Optional[str] = "padrao"
    miniapp_url: Optional[str] = None
    miniapp_btn_text: Optional[str] = None

@app.post("/api/admin/bots/{bot_id}/flow")
def salvar_fluxo(
    bot_id: int, 
    flow: FlowUpdate, 
    db: Session = Depends(get_db),
    current_user = Depends(get_current_user)  # 🔒 ADICIONA AUTH
):
    # 🔒 VERIFICA SE O BOT PERTENCE AO USUÁRIO
    verificar_bot_pertence_usuario(bot_id, current_user.id, db)
    
    # ... RESTO DO CÓDIGO PERMANECE EXATAMENTE IGUAL
    fluxo_db = db.query(BotFlow).filter(BotFlow.bot_id == bot_id).first()
    
    if not fluxo_db:
        fluxo_db = BotFlow(bot_id=bot_id)
        db.add(fluxo_db)
    
    # Atualiza campos básicos
    if flow.msg_boas_vindas is not None: fluxo_db.msg_boas_vindas = flow.msg_boas_vindas
    if flow.media_url is not None: fluxo_db.media_url = flow.media_url
    if flow.btn_text_1 is not None: fluxo_db.btn_text_1 = flow.btn_text_1
    if flow.autodestruir_1 is not None: fluxo_db.autodestruir_1 = flow.autodestruir_1
    if flow.msg_2_texto is not None: fluxo_db.msg_2_texto = flow.msg_2_texto
    if flow.msg_2_media is not None: fluxo_db.msg_2_media = flow.msg_2_media
    if flow.mostrar_planos_2 is not None: fluxo_db.mostrar_planos_2 = flow.mostrar_planos_2
    if flow.mostrar_planos_1 is not None: fluxo_db.mostrar_planos_1 = flow.mostrar_planos_1
    
    # Atualiza campos do Mini App
    if flow.start_mode: fluxo_db.start_mode = flow.start_mode
    if flow.miniapp_url is not None: fluxo_db.miniapp_url = flow.miniapp_url
    if flow.miniapp_btn_text: fluxo_db.miniapp_btn_text = flow.miniapp_btn_text
    
    db.commit()
    
    logger.info(f"💾 Fluxo do Bot {bot_id} salvo com sucesso (Owner: {current_user.username})")
    
    return {"status": "saved"}

# =========================================================
# 🔗 ROTAS DE TRACKING (RASTREAMENTO)
# =========================================================
@app.get("/api/admin/tracking/folders")
def list_tracking_folders(db: Session = Depends(get_db)):
    """Lista pastas com contagem de links E métricas somadas"""
    try:
        folders = db.query(TrackingFolder).all()
        result = []
        for f in folders:
            # Conta links
            link_count = db.query(TrackingLink).filter(TrackingLink.folder_id == f.id).count()
            
            # Soma cliques e vendas de todos os links desta pasta
            stats = db.query(
                func.sum(TrackingLink.clicks).label('total_clicks'),
                func.sum(TrackingLink.vendas).label('total_vendas')
            ).filter(TrackingLink.folder_id == f.id).first()
            
            clicks = stats.total_clicks or 0
            vendas = stats.total_vendas or 0

            result.append({
                "id": f.id, 
                "nome": f.nome, 
                "plataforma": f.plataforma, 
                "link_count": link_count,
                "total_clicks": clicks,   # 🔥 Dado Real
                "total_vendas": vendas,   # 🔥 Dado Real
                "created_at": f.created_at
            })
        return result
    except Exception as e:
        logger.error(f"Erro ao listar pastas: {e}")
        return []

@app.post("/api/admin/tracking/folders")
def create_tracking_folder(dados: TrackingFolderCreate, db: Session = Depends(get_db)):
    try:
        nova_pasta = TrackingFolder(nome=dados.nome, plataforma=dados.plataforma)
        db.add(nova_pasta)
        db.commit()
        db.refresh(nova_pasta)
        return {"status": "ok", "id": nova_pasta.id}
    except Exception as e:
        logger.error(f"Erro ao criar pasta: {e}")
        raise HTTPException(status_code=500, detail="Erro interno ao criar pasta")

@app.get("/api/admin/tracking/links/{folder_id}")
def list_tracking_links(folder_id: int, db: Session = Depends(get_db)):
    return db.query(TrackingLink).filter(TrackingLink.folder_id == folder_id).all()

@app.post("/api/admin/tracking/links")
def create_tracking_link(dados: TrackingLinkCreate, db: Session = Depends(get_db)):
    # Gera código aleatório se não informado
    if not dados.codigo:
        import random, string
        chars = string.ascii_lowercase + string.digits
        dados.codigo = ''.join(random.choice(chars) for _ in range(8))
    
    # Verifica duplicidade
    exists = db.query(TrackingLink).filter(TrackingLink.codigo == dados.codigo).first()
    if exists:
        raise HTTPException(400, "Este código de rastreamento já existe.")
        
    novo_link = TrackingLink(
        folder_id=dados.folder_id,
        bot_id=dados.bot_id,
        nome=dados.nome,
        codigo=dados.codigo,
        origem=dados.origem
    )
    db.add(novo_link)
    db.commit()
    return {"status": "ok", "link": novo_link}

@app.delete("/api/admin/tracking/folders/{fid}")
def delete_folder(fid: int, db: Session = Depends(get_db)):
    # Apaga links dentro da pasta primeiro
    db.query(TrackingLink).filter(TrackingLink.folder_id == fid).delete()
    db.query(TrackingFolder).filter(TrackingFolder.id == fid).delete()
    db.commit()
    return {"status": "deleted"}

@app.delete("/api/admin/tracking/links/{lid}")
def delete_link(lid: int, db: Session = Depends(get_db)):
    db.query(TrackingLink).filter(TrackingLink.id == lid).delete()
    db.commit()
    return {"status": "deleted"}

# =========================================================
# 🧩 ROTAS DE PASSOS DINÂMICOS (FLOW V2)
# =========================================================
@app.get("/api/admin/bots/{bot_id}/flow/steps")
def listar_passos_flow(bot_id: int, db: Session = Depends(get_db)):
    return db.query(BotFlowStep).filter(BotFlowStep.bot_id == bot_id).order_by(BotFlowStep.step_order).all()

@app.post("/api/admin/bots/{bot_id}/flow/steps")
def adicionar_passo_flow(bot_id: int, payload: FlowStepCreate, db: Session = Depends(get_db)):
    bot = db.query(Bot).filter(Bot.id == bot_id).first()
    if not bot: raise HTTPException(404, "Bot não encontrado")
    
    # Cria o novo passo
    novo_passo = BotFlowStep(
        bot_id=bot_id, step_order=payload.step_order,
        msg_texto=payload.msg_texto, msg_media=payload.msg_media,
        btn_texto=payload.btn_texto
    )
    db.add(novo_passo)
    db.commit()
    return {"status": "success"}

@app.put("/api/admin/bots/{bot_id}/flow/steps/{step_id}")
def atualizar_passo_flow(bot_id: int, step_id: int, dados: FlowStepUpdate, db: Session = Depends(get_db)):
    """Atualiza um passo intermediário existente"""
    passo = db.query(BotFlowStep).filter(
        BotFlowStep.id == step_id,
        BotFlowStep.bot_id == bot_id
    ).first()
    
    if not passo:
        raise HTTPException(status_code=404, detail="Passo não encontrado")
    
    # Atualiza apenas os campos enviados
    if dados.msg_texto is not None:
        passo.msg_texto = dados.msg_texto
    if dados.msg_media is not None:
        passo.msg_media = dados.msg_media
    if dados.btn_texto is not None:
        passo.btn_texto = dados.btn_texto
    if dados.autodestruir is not None:
        passo.autodestruir = dados.autodestruir
    if dados.mostrar_botao is not None:
        passo.mostrar_botao = dados.mostrar_botao
    if dados.delay_seconds is not None:
        passo.delay_seconds = dados.delay_seconds
    
    db.commit()
    db.refresh(passo)
    return {"status": "success", "passo": passo}


@app.delete("/api/admin/bots/{bot_id}/flow/steps/{sid}")
def remover_passo_flow(bot_id: int, sid: int, db: Session = Depends(get_db)):
    passo = db.query(BotFlowStep).filter(BotFlowStep.id == sid, BotFlowStep.bot_id == bot_id).first()
    if passo:
        db.delete(passo)
        db.commit()
    return {"status": "deleted"}

# =========================================================
# 📱 ROTAS DE MINI APP (LOJA VIRTUAL) & GESTÃO DE MODO
# =========================================================

# 0. Trocar Modo do Bot (Tradicional <-> Mini App)
class BotModeUpdate(BaseModel):
    modo: str # 'tradicional' ou 'miniapp'

@app.post("/api/admin/bots/{bot_id}/mode")
def switch_bot_mode(bot_id: int, dados: BotModeUpdate, db: Session = Depends(get_db)):
    """Alterna entre Bot de Conversa (Tradicional) e Loja Web (Mini App)"""
    bot = db.query(Bot).filter(Bot.id == bot_id).first()
    if not bot:
        raise HTTPException(status_code=404, detail="Bot não encontrado")
    
    # Aqui poderíamos salvar no banco se tivéssemos a coluna 'modo', 
    # mas por enquanto vamos assumir que a existência de configuração de MiniApp
    # ativa o modo. Se quiser formalizar, adicione 'modo' na tabela Bot.
    
    # Se mudar para MiniApp, cria config padrão se não existir
    if dados.modo == 'miniapp':
        config = db.query(MiniAppConfig).filter(MiniAppConfig.bot_id == bot_id).first()
        if not config:
            new_config = MiniAppConfig(bot_id=bot_id)
            db.add(new_config)
            db.commit()
            
    return {"status": "ok", "msg": f"Modo alterado para {dados.modo}"}


# 2. Salvar Configuração Global
@app.post("/api/admin/bots/{bot_id}/miniapp/config")
def save_miniapp_config(
    bot_id: int, 
    dados: MiniAppConfigUpdate, 
    db: Session = Depends(get_db),
    current_user = Depends(get_current_user) # 🔒 AUTH
):
    # 🔒 VERIFICA PROPRIEDADE
    verificar_bot_pertence_usuario(bot_id, current_user.id, db)

    config = db.query(MiniAppConfig).filter(MiniAppConfig.bot_id == bot_id).first()
    
    if not config:
        config = MiniAppConfig(bot_id=bot_id)
        db.add(config)
    
    # Atualiza campos se enviados
    if dados.logo_url is not None: config.logo_url = dados.logo_url
    if dados.background_type is not None: config.background_type = dados.background_type
    if dados.background_value is not None: config.background_value = dados.background_value
    
    if dados.hero_title is not None: config.hero_title = dados.hero_title
    if dados.hero_subtitle is not None: config.hero_subtitle = dados.hero_subtitle
    if dados.hero_video_url is not None: config.hero_video_url = dados.hero_video_url
    if dados.hero_btn_text is not None: config.hero_btn_text = dados.hero_btn_text
    
    if dados.enable_popup is not None: config.enable_popup = dados.enable_popup
    if dados.popup_video_url is not None: config.popup_video_url = dados.popup_video_url
    if dados.popup_text is not None: config.popup_text = dados.popup_text
    
    if dados.footer_text is not None: config.footer_text = dados.footer_text
    
    db.commit()
    return {"status": "ok", "msg": "Configuração da loja salva!"}

# 3. Criar Categoria
@app.post("/api/admin/miniapp/categories")
def create_or_update_category(data: CategoryCreate, db: Session = Depends(get_db)):
    try:
        # Se não vier slug, cria um baseado no título
        final_slug = data.slug
        if not final_slug and data.title:
            import re
            import unicodedata
            # Normaliza slug (ex: "Praia de Nudismo" -> "praia-de-nudismo")
            s = unicodedata.normalize('NFKD', data.title).encode('ascii', 'ignore').decode('utf-8')
            final_slug = re.sub(r'[^a-zA-Z0-9]+', '-', s.lower()).strip('-')

        if data.id:
            # --- EDIÇÃO ---
            categoria = db.query(MiniAppCategory).filter(MiniAppCategory.id == data.id).first()
            if not categoria:
                raise HTTPException(status_code=404, detail="Categoria não encontrada")
            
            categoria.title = data.title
            categoria.slug = final_slug # <--- SALVANDO SLUG
            categoria.description = data.description
            categoria.cover_image = data.cover_image
            categoria.banner_mob_url = data.banner_mob_url
            categoria.theme_color = data.theme_color
            categoria.is_direct_checkout = data.is_direct_checkout
            categoria.is_hacker_mode = data.is_hacker_mode
            categoria.content_json = data.content_json
            
            # Campos Visuais
            categoria.bg_color = data.bg_color
            categoria.banner_desk_url = data.banner_desk_url
            categoria.video_preview_url = data.video_preview_url
            categoria.model_img_url = data.model_img_url
            categoria.model_name = data.model_name
            categoria.model_desc = data.model_desc
            categoria.footer_banner_url = data.footer_banner_url
            categoria.deco_lines_url = data.deco_lines_url
            
            # Cores Texto
            categoria.model_name_color = data.model_name_color
            categoria.model_desc_color = data.model_desc_color
            
            db.commit()
            db.refresh(categoria)
            return categoria
        
        else:
            # --- CRIAÇÃO ---
            nova_cat = MiniAppCategory(
                bot_id=data.bot_id,
                title=data.title,
                slug=final_slug, # <--- SALVANDO SLUG
                description=data.description,
                cover_image=data.cover_image,
                banner_mob_url=data.banner_mob_url,
                theme_color=data.theme_color,
                is_direct_checkout=data.is_direct_checkout,
                is_hacker_mode=data.is_hacker_mode,
                content_json=data.content_json,
                bg_color=data.bg_color,
                banner_desk_url=data.banner_desk_url,
                video_preview_url=data.video_preview_url,
                model_img_url=data.model_img_url,
                model_name=data.model_name,
                model_desc=data.model_desc,
                footer_banner_url=data.footer_banner_url,
                deco_lines_url=data.deco_lines_url,
                model_name_color=data.model_name_color,
                model_desc_color=data.model_desc_color
            )
            db.add(nova_cat)
            db.commit()
            db.refresh(nova_cat)
            return nova_cat

    except Exception as e:
        logger.error(f"Erro ao salvar categoria: {e}")
        raise HTTPException(status_code=500, detail=str(e))

# 4. Listar Categorias de um Bot
@app.get("/api/admin/bots/{bot_id}/miniapp/categories")
def list_bot_categories(bot_id: int, db: Session = Depends(get_db)):
    return db.query(MiniAppCategory).filter(MiniAppCategory.bot_id == bot_id).all()

# 5. Deletar Categoria
@app.delete("/api/admin/miniapp/categories/{cat_id}")
def delete_miniapp_category(cat_id: int, db: Session = Depends(get_db)):
    cat = db.query(MiniAppCategory).filter(MiniAppCategory.id == cat_id).first()
    if cat:
        db.delete(cat)
        db.commit()
    return {"status": "deleted"}

# 🔔 WEBHOOK PIX (INTELIGENTE: RESOLVE IDS + ENTREGA BUMP)
# =========================================================
@app.post("/webhook/pix")
async def webhook_pix(request: Request, db: Session = Depends(get_db)):
    print("🔔 WEBHOOK PIX CHEGOU!") 
    try:
        body_bytes = await request.body()
        body_str = body_bytes.decode("utf-8")
        try: data = json.loads(body_str)
        except: 
            try: data = {k: v[0] for k, v in urllib.parse.parse_qs(body_str).items()}
            except: return {"status": "ignored"}

        raw_tx_id = data.get("id") or data.get("external_reference") or data.get("uuid")
        tx_id = str(raw_tx_id).lower() if raw_tx_id else None
        status_pix = str(data.get("status", "")).lower()
        
        if status_pix not in ["paid", "approved", "completed", "succeeded"]: return {"status": "ignored"}

        pedido = db.query(Pedido).filter((Pedido.txid == tx_id) | (Pedido.transaction_id == tx_id)).first()
        if not pedido or pedido.status in ["approved", "paid"]: return {"status": "ok"}

        now = datetime.utcnow()
        pedido.status = "approved" 
        pedido.data_aprovacao = now
        pedido.pagou_em = now
        pedido.status_funil = 'fundo'
        
        if pedido.plano_id:
            pid = int(pedido.plano_id) if str(pedido.plano_id).isdigit() else None
            if pid:
                plano_db = db.query(PlanoConfig).filter(PlanoConfig.id == pid).first()
                if plano_db and plano_db.dias_duracao and plano_db.dias_duracao < 90000:
                    pedido.data_expiracao = now + timedelta(days=plano_db.dias_duracao)
                    pedido.custom_expiration = pedido.data_expiracao

        pedido.mensagem_enviada = False 
        db.commit()
        
        sucesso_entrega = False
        try:
            bot_data = db.query(Bot).filter(Bot.id == pedido.bot_id).first()
            if bot_data:
                tb = telebot.TeleBot(bot_data.token)
                target_id = str(pedido.telegram_id).strip()
                
                # 🔥 AUTO-CORREÇÃO DE ID
                if not target_id.isdigit():
                    logger.info(f"⚠️ ID '{target_id}' não é numérico. Buscando Lead correspondente...")
                    clean_user = str(pedido.username).lower().replace("@", "").strip()
                    lead = db.query(Lead).filter(
                        Lead.bot_id == pedido.bot_id,
                        (func.lower(Lead.username) == clean_user) | (func.lower(Lead.username) == f"@{clean_user}")
                    ).order_by(desc(Lead.created_at)).first()
                    if lead and lead.user_id and lead.user_id.isdigit():
                        logger.info(f"✅ ID Resolvido via Lead: {lead.user_id}")
                        target_id = lead.user_id
                        pedido.telegram_id = target_id
                        db.commit()

                if target_id.isdigit():
                    try:
                        # 1. ENTREGA PRINCIPAL
                        canal_str = str(bot_data.id_canal_vip).strip()
                        canal_id = int(canal_str) if canal_str.lstrip('-').isdigit() else canal_str
                        try: tb.unban_chat_member(canal_id, int(target_id))
                        except: pass
                        
                        convite = tb.create_chat_invite_link(chat_id=canal_id, member_limit=1, name=f"Venda {pedido.first_name}")
                        val_txt = pedido.data_expiracao.strftime("%d/%m/%Y") if pedido.data_expiracao else "VITALÍCIO"
                        
                        msg = f"✅ <b>Pagamento Confirmado!</b>\n📅 Validade: <b>{val_txt}</b>\n\nSeu acesso:\n👉 {convite.invite_link}"
                        tb.send_message(int(target_id), msg, parse_mode="HTML")
                        
                        # 🔥 2. ENTREGA DO ORDER BUMP (NOVO)
                        if pedido.tem_order_bump:
                            bump_conf = db.query(OrderBumpConfig).filter(OrderBumpConfig.bot_id == pedido.bot_id).first()
                            if bump_conf and bump_conf.link_acesso:
                                msg_bump = f"🎁 <b>BÔNUS: {bump_conf.nome_produto}</b>\n\nAqui está seu acesso extra:\n👉 {bump_conf.link_acesso}"
                                tb.send_message(int(target_id), msg_bump, parse_mode="HTML")
                                logger.info("✅ Order Bump entregue!")

                        sucesso_entrega = True
                        logger.info(f"✅ Entrega realizada para ID: {target_id}")
                    except Exception as e_send:
                        logger.error(f"Erro envio Telegram: {e_send}")
        except Exception as e_bot:
            logger.error(f"Erro lógica bot: {e_bot}")

        if sucesso_entrega:
            pedido.mensagem_enviada = True
            db.commit()

        return {"status": "received"}
    except Exception as e:
        logger.error(f"Erro webhook: {e}")
        return {"status": "error"}

# =========================================================
# 💳 WEBHOOK PIX (PUSHIN PAY) - VERSÃO FINAL BLINDADA
# =========================================================
@app.post("/webhook/pix")
async def webhook_pix(request: Request, db: Session = Depends(get_db)):
    print("🔔 WEBHOOK PIX CHEGOU!") 
    try:
        # 1. PEGA O CORPO BRUTO (Mais seguro contra erros de parsing)
        body_bytes = await request.body()
        body_str = body_bytes.decode("utf-8")
        
        # Tratamento de JSON ou Form Data
        try:
            data = json.loads(body_str)
            if isinstance(data, list):
                data = data[0]
        except:
            try:
                parsed = urllib.parse.parse_qs(body_str)
                data = {k: v[0] for k, v in parsed.items()}
            except:
                logger.error(f"❌ Não foi possível ler o corpo do webhook: {body_str}")
                return {"status": "ignored"}

        # 2. EXTRAÇÃO E NORMALIZAÇÃO DO ID
        raw_tx_id = data.get("id") or data.get("external_reference") or data.get("uuid")
        tx_id = str(raw_tx_id).lower() if raw_tx_id else None
        
        # Status
        status_pix = str(data.get("status", "")).lower()
        
        # 🔥 FILTRO: SÓ PASSA SE FOR PAGO
        if status_pix not in ["paid", "approved", "completed", "succeeded"]:
            return {"status": "ignored"}

        # 3. BUSCA O PEDIDO
        pedido = db.query(Pedido).filter((Pedido.txid == tx_id) | (Pedido.transaction_id == tx_id)).first()

        if not pedido:
            print(f"❌ Pedido {tx_id} não encontrado no banco.")
            return {"status": "ok", "msg": "Order not found"}

        if pedido.status == "approved" or pedido.status == "paid":
            return {"status": "ok", "msg": "Already paid"}

        # --- 4. CÁLCULO DA DATA DE EXPIRAÇÃO ---
        now = datetime.utcnow()
        data_validade = None 
        
        # A) Pelo ID do plano
        if pedido.plano_id:
            pid = int(pedido.plano_id) if str(pedido.plano_id).isdigit() else None
            if pid:
                plano_db = db.query(PlanoConfig).filter(PlanoConfig.id == pid).first()
                if plano_db and plano_db.dias_duracao and plano_db.dias_duracao < 90000:
                    data_validade = now + timedelta(days=plano_db.dias_duracao)

        # B) Fallback pelo nome
        if not data_validade and pedido.plano_nome:
            nm = pedido.plano_nome.lower()
            if "vital" not in nm and "mega" not in nm and "eterno" not in nm:
                dias = 30 # Padrão
                if "24" in nm or "diario" in nm or "1 dia" in nm: dias = 1
                elif "semanal" in nm: dias = 7
                elif "trimestral" in nm: dias = 90
                elif "anual" in nm: dias = 365
                data_validade = now + timedelta(days=dias)

        # 5. ATUALIZA O PEDIDO
        pedido.status = "approved" 
        pedido.data_aprovacao = now
        pedido.data_expiracao = data_validade     
        pedido.custom_expiration = data_validade
        pedido.mensagem_enviada = True
        
        # 🔥 Atualiza Funil (Para os gráficos funcionarem)
        pedido.status_funil = 'fundo'
        pedido.pagou_em = now
        
        # 🔥 ATUALIZA TRACKING (MANTIDO INTACTO!)
        # (Isso é crucial para seus gráficos de vendas)
        if pedido.tracking_id:
            try:
                t_link = db.query(TrackingLink).filter(TrackingLink.id == pedido.tracking_id).first()
                if t_link:
                    t_link.vendas += 1
                    t_link.faturamento += pedido.valor
                    logger.info(f"📈 Tracking atualizado: {t_link.nome} (+R$ {pedido.valor})")
            except Exception as e_track:
                logger.error(f"Erro ao atualizar tracking: {e_track}")

        db.commit() # Salva tudo no banco antes de enviar mensagens

        texto_validade = data_validade.strftime("%d/%m/%Y") if data_validade else "VITALÍCIO ♾️"
        print(f"✅ Pedido {tx_id} APROVADO! Validade: {texto_validade}")
        
        # 6. ENTREGA E NOTIFICAÇÕES (LÓGICA BLINDADA)
        try:
            bot_data = db.query(Bot).filter(Bot.id == pedido.bot_id).first()
            if bot_data:
                tb = telebot.TeleBot(bot_data.token)
                
                # --- A) ENTREGA PRODUTO PRINCIPAL ---
                try: 
                    # Tratamento Robusto do ID do Canal
                    raw_cid = str(bot_data.id_canal_vip).strip()
                    canal_id = int(raw_cid) if raw_cid.lstrip('-').isdigit() else raw_cid

                    # 1. Tenta desbanir antes (Correção para ex-assinantes)
                    try: tb.unban_chat_member(canal_id, int(pedido.telegram_id))
                    except: pass

                    # 2. Gera Link Único
                    link_acesso = None
                    try:
                        convite = tb.create_chat_invite_link(
                            chat_id=canal_id, 
                            member_limit=1, 
                            name=f"Venda {pedido.first_name}"
                        )
                        link_acesso = convite.invite_link
                    except Exception as e_link:
                        logger.warning(f"Erro ao gerar link único: {e_link}. Usando link salvo.")
                        link_acesso = pedido.link_acesso 

                    # 3. Envia Mensagem ao Cliente
                    if link_acesso:
                        msg_cliente = (
                            f"✅ <b>Pagamento Confirmado!</b>\n\n"
                            f"🎉 Parabéns! Seu acesso foi liberado.\n"
                            f"📅 Validade: <b>{texto_validade}</b>\n\n"
                            f"👇 <b>Toque no link para entrar:</b>\n👉 {link_acesso}\n\n"
                            f"<i>Este link é exclusivo para você.</i>"
                        )
                        tb.send_message(int(pedido.telegram_id), msg_cliente, parse_mode="HTML")
                    else:
                        tb.send_message(int(pedido.telegram_id), f"✅ <b>Pagamento Confirmado!</b>\nTente entrar no canal agora ou digite /start.", parse_mode="HTML")

                except Exception as e_main:
                    logger.error(f"Erro na entrega principal: {e_main}")

                # --- B) ENTREGA DO ORDER BUMP (MANTIDO INTACTO!) ---
                if pedido.tem_order_bump:
                    logger.info(f"🎁 [PIX] Entregando Order Bump...")
                    try:
                        bump_config = db.query(OrderBumpConfig).filter(OrderBumpConfig.bot_id == bot_data.id).first()
                        if bump_config and bump_config.link_acesso:
                            msg_bump = f"🎁 <b>BÔNUS LIBERADO!</b>\n\nVocê também garantiu acesso ao: <b>{bump_config.nome_produto}</b>\n\n🔗 <b>Acesse aqui:</b>\n{bump_config.link_acesso}"
                            tb.send_message(int(pedido.telegram_id), msg_bump, parse_mode="HTML")
                    except Exception as e_bump:
                        logger.error(f"Erro ao entregar Bump: {e_bump}")

                # --- C) NOTIFICAÇÃO AO ADMIN (MANTIDO E CORRIGIDO) ---
                try:
                    msg_admin = (
                        f"💰 <b>VENDA REALIZADA!</b>\n\n"
                        f"🤖 Bot: <b>{bot_data.nome}</b>\n"
                        f"👤 Cliente: {pedido.first_name} (@{pedido.username})\n"
                        f"📦 Plano: {pedido.plano_nome}\n"
                        f"💵 Valor: <b>R$ {pedido.valor:.2f}</b>\n"
                        f"📅 Vence em: {texto_validade}"
                    )
                    # Chama a função auxiliar (Certifique-se que ela também foi atualizada)
                    notificar_admin_principal(bot_data, msg_admin)
                except Exception as e_notif:
                    logger.error(f"Erro ao notificar admin: {e_notif}")

        except Exception as e_tg:
            print(f"❌ Erro Telegram/Entrega: {e_tg}")

        return {"status": "received"}

    except Exception as e:
        print(f"❌ ERRO CRÍTICO NO WEBHOOK: {e}")
        return {"status": "error"}

# =========================================================
# 🧠 FUNÇÕES AUXILIARES DE FLUXO (RECURSIVIDADE)
# =========================================================

def enviar_oferta_final(bot_temp, chat_id, fluxo, bot_id, db):
    """Envia a oferta final (Planos) com HTML"""
    mk = types.InlineKeyboardMarkup()
    if fluxo and fluxo.mostrar_planos_2:
        planos = db.query(PlanoConfig).filter(PlanoConfig.bot_id == bot_id).all()
        for p in planos:
            mk.add(types.InlineKeyboardButton(
                f"💎 {p.nome_exibicao} - R$ {p.preco_atual:.2f}", 
                callback_data=f"checkout_{p.id}"
            ))
    
    texto = fluxo.msg_2_texto if (fluxo and fluxo.msg_2_texto) else "Confira nossos planos:"
    media = fluxo.msg_2_media if fluxo else None
    
    try:
        if media:
            if media.lower().endswith(('.mp4', '.mov', '.avi')): 
                # 🔥 parse_mode="HTML"
                bot_temp.send_video(chat_id, media, caption=texto, reply_markup=mk, parse_mode="HTML")
            else: 
                # 🔥 parse_mode="HTML"
                bot_temp.send_photo(chat_id, media, caption=texto, reply_markup=mk, parse_mode="HTML")
        else:
            # 🔥 parse_mode="HTML"
            bot_temp.send_message(chat_id, texto, reply_markup=mk, parse_mode="HTML")
            
    except Exception as e:
        logger.error(f"Erro ao enviar oferta final: {e}")
        # Fallback sem HTML
        bot_temp.send_message(chat_id, texto, reply_markup=mk)

def enviar_passo_automatico(bot_temp, chat_id, passo_atual, bot_db, db):
    """
    Envia um passo e, se não tiver botão e tiver delay, 
    agenda e envia o PRÓXIMO (ou a oferta) automaticamente.
    """
    try:
        # 1. Configura botão se houver
        markup_step = types.InlineKeyboardMarkup()
        if passo_atual.mostrar_botao:
            # Verifica se existe um PRÓXIMO passo depois deste
            prox = db.query(BotFlowStep).filter(
                BotFlowStep.bot_id == bot_db.id, 
                BotFlowStep.step_order == passo_atual.step_order + 1
            ).first()
            
            callback = f"next_step_{passo_atual.step_order}" if prox else "go_checkout"
            markup_step.add(types.InlineKeyboardButton(text=passo_atual.btn_texto, callback_data=callback))

        # 2. Envia a mensagem deste passo
        sent_msg = None
        if passo_atual.msg_media:
            try:
                if passo_atual.msg_media.lower().endswith(('.mp4', '.mov')):
                    sent_msg = bot_temp.send_video(chat_id, passo_atual.msg_media, caption=passo_atual.msg_texto, reply_markup=markup_step if passo_atual.mostrar_botao else None)
                else:
                    sent_msg = bot_temp.send_photo(chat_id, passo_atual.msg_media, caption=passo_atual.msg_texto, reply_markup=markup_step if passo_atual.mostrar_botao else None)
            except:
                sent_msg = bot_temp.send_message(chat_id, passo_atual.msg_texto, reply_markup=markup_step if passo_atual.mostrar_botao else None)
        else:
            sent_msg = bot_temp.send_message(chat_id, passo_atual.msg_texto, reply_markup=markup_step if passo_atual.mostrar_botao else None)

        # 3. Lógica Automática (Sem botão + Delay)
        if not passo_atual.mostrar_botao and passo_atual.delay_seconds > 0:
            logger.info(f"⏳ [BOT {bot_db.id}] Passo {passo_atual.step_order}: Aguardando {passo_atual.delay_seconds}s...")
            time.sleep(passo_atual.delay_seconds)
            
            # Auto-destruir este passo (se configurado)
            if passo_atual.autodestruir and sent_msg:
                try:
                    bot_temp.delete_message(chat_id, sent_msg.message_id)
                except: pass
            
            # 🔥 DECISÃO: Chama o próximo passo OU a Oferta Final
            proximo_passo = db.query(BotFlowStep).filter(
                BotFlowStep.bot_id == bot_db.id, 
                BotFlowStep.step_order == passo_atual.step_order + 1
            ).first()
            
            if proximo_passo:
                enviar_passo_automatico(bot_temp, chat_id, proximo_passo, bot_db, db)
            else:
                # FIM DA LINHA -> Manda Oferta
                enviar_oferta_final(bot_temp, chat_id, bot_db.fluxo, bot_db.id, db)

    except Exception as e:
        logger.error(f"Erro no passo automático {passo_atual.step_order}: {e}")

# =========================================================
# 3. WEBHOOK TELEGRAM (START + GATEKEEPER + COMANDOS)
# =========================================================
# =========================================================
# 3. WEBHOOK TELEGRAM (START + GATEKEEPER + COMANDOS)
# =========================================================
@app.post("/webhook/{token}")
async def receber_update_telegram(token: str, req: Request, db: Session = Depends(get_db)):
    if token == "pix": return {"status": "ignored"}
    
    bot_db = db.query(Bot).filter(Bot.token == token).first()
    if not bot_db or bot_db.status == "pausado": return {"status": "ignored"}

    try:
        body = await req.json()
        update = telebot.types.Update.de_json(body)
        bot_temp = telebot.TeleBot(token)
        message = update.message if update.message else None
        
        # ----------------------------------------
        # 🚪 1. O PORTEIRO (GATEKEEPER)
        # ----------------------------------------
        if message and message.new_chat_members:
            chat_id = str(message.chat.id)
            canal_vip_id = str(bot_db.id_canal_vip).replace(" ", "").strip()
            
            if chat_id == canal_vip_id:
                for member in message.new_chat_members:
                    if member.is_bot: continue
                    
                    # Verifica pagamento
                    pedido = db.query(Pedido).filter(
                        Pedido.bot_id == bot_db.id,
                        Pedido.telegram_id == str(member.id),
                        Pedido.status.in_(['paid', 'approved'])
                    ).order_by(desc(Pedido.created_at)).first()
                    
                    allowed = False
                    if pedido:
                        if pedido.data_expiracao:
                            if datetime.utcnow() < pedido.data_expiracao: allowed = True
                        elif pedido.plano_nome:
                            nm = pedido.plano_nome.lower()
                            if "vital" in nm or "mega" in nm or "eterno" in nm: allowed = True
                            else:
                                d = 30
                                if "diario" in nm or "24" in nm: d = 1
                                elif "semanal" in nm: d = 7
                                elif "trimestral" in nm: d = 90
                                elif "anual" in nm: d = 365
                                if pedido.created_at and datetime.utcnow() < (pedido.created_at + timedelta(days=d)): allowed = True
                    
                    if not allowed:
                        try:
                            bot_temp.ban_chat_member(chat_id, member.id)
                            bot_temp.unban_chat_member(chat_id, member.id)
                            try: bot_temp.send_message(member.id, "🚫 <b>Acesso Negado.</b>\nPor favor, realize o pagamento.", parse_mode="HTML")
                            except: pass
                        except: pass
            return {"status": "checked"}

        # ----------------------------------------
        # 👋 2. COMANDOS (/start, /suporte, /status)
        # ----------------------------------------
        if message and message.text:
            chat_id = message.chat.id
            txt = message.text.lower().strip()
            
            # --- /SUPORTE ---
            if txt == "/suporte":
                if bot_db.suporte_username:
                    sup = bot_db.suporte_username.replace("@", "")
                    bot_temp.send_message(chat_id, f"💬 <b>Falar com Suporte:</b>\n\n👉 @{sup}", parse_mode="HTML")
                else: bot_temp.send_message(chat_id, "⚠️ Nenhum suporte definido.")
                return {"status": "ok"}

            # --- /STATUS ---
            if txt == "/status":
                pedido = db.query(Pedido).filter(
                    Pedido.bot_id == bot_db.id,
                    Pedido.telegram_id == str(chat_id),
                    Pedido.status.in_(['paid', 'approved'])
                ).order_by(desc(Pedido.created_at)).first()
                
                if pedido:
                    validade = "VITALÍCIO ♾️"
                    if pedido.data_expiracao:
                        if datetime.utcnow() > pedido.data_expiracao:
                            bot_temp.send_message(chat_id, "❌ <b>Assinatura expirada!</b>", parse_mode="HTML")
                            return {"status": "ok"}
                        validade = pedido.data_expiracao.strftime("%d/%m/%Y")
                    bot_temp.send_message(chat_id, f"✅ <b>Assinatura Ativa!</b>\n\n💎 Plano: {pedido.plano_nome}\n📅 Vence em: {validade}", parse_mode="HTML")
                else: bot_temp.send_message(chat_id, "❌ <b>Nenhuma assinatura ativa.</b>", parse_mode="HTML")
                return {"status": "ok"}

            # --- /START ---
            if txt == "/start" or txt.startswith("/start "):
                first_name = message.from_user.first_name
                username_raw = message.from_user.username
                username_clean = str(username_raw).lower().replace("@", "").strip() if username_raw else ""
                user_id_str = str(chat_id)
                
                # 🔥 RECUPERAÇÃO DE VENDAS
                filtros_recuperacao = [
                    Pedido.bot_id == bot_db.id,
                    Pedido.status.in_(['paid', 'approved']),
                    Pedido.mensagem_enviada == False
                ]
                pendentes = db.query(Pedido).filter(*filtros_recuperacao).all()
                pedidos_resgate = []
                
                for p in pendentes:
                    db_user = str(p.username or "").lower().replace("@", "").strip()
                    db_id = str(p.telegram_id or "").strip()
                    match = False
                    if username_clean and db_user == username_clean: match = True
                    if db_id == user_id_str: match = True
                    if username_clean and db_id.lower().replace("@","") == username_clean: match = True
                    
                    if match: pedidos_resgate.append(p)

                if pedidos_resgate:
                    logger.info(f"🚑 RECUPERANDO {len(pedidos_resgate)} vendas para {first_name}")
                    for p in pedidos_resgate:
                        p.telegram_id = user_id_str
                        p.mensagem_enviada = True
                        db.commit()
                        try:
                            # 1. ENTREGA PRINCIPAL
                            canal_str = str(bot_db.id_canal_vip).strip()
                            canal_id = int(canal_str) if canal_str.lstrip('-').isdigit() else canal_str
                            try: bot_temp.unban_chat_member(canal_id, chat_id)
                            except: pass
                            convite = bot_temp.create_chat_invite_link(chat_id=canal_id, member_limit=1, name=f"Recup {first_name}")
                            msg_rec = f"🎉 <b>Pagamento Encontrado!</b>\n\nAqui está seu link:\n👉 {convite.invite_link}"
                            bot_temp.send_message(chat_id, msg_rec, parse_mode="HTML")

                            # 🔥 2. ENTREGA DO BUMP NA RECUPERAÇÃO (CORRIGIDO)
                            if p.tem_order_bump:
                                bump_conf = db.query(OrderBumpConfig).filter(OrderBumpConfig.bot_id == bot_db.id).first()
                                if bump_conf and bump_conf.link_acesso:
                                    msg_bump = f"🎁 <b>BÔNUS: {bump_conf.nome_produto}</b>\n\nAqui está seu acesso extra:\n👉 {bump_conf.link_acesso}"
                                    bot_temp.send_message(chat_id, msg_bump, parse_mode="HTML")
                                    logger.info("✅ Order Bump recuperado/entregue!")

                        except Exception as e_rec:
                            logger.error(f"Erro rec: {e_rec}")
                            bot_temp.send_message(chat_id, "✅ Pagamento confirmado! Tente entrar no canal.")

                # Tracking
                track_id = None
                parts = txt.split()
                if len(parts) > 1:
                    code = parts[1]
                    tl = db.query(TrackingLink).filter(TrackingLink.codigo == code).first()
                    if tl: 
                        tl.clicks += 1
                        track_id = tl.id
                        db.commit()

                # Lead
                try:
                    lead = db.query(Lead).filter(Lead.user_id == user_id_str, Lead.bot_id == bot_db.id).first()
                    if not lead:
                        lead = Lead(user_id=user_id_str, nome=first_name, username=username_raw, bot_id=bot_db.id, tracking_id=track_id)
                        db.add(lead)
                    db.commit()
                except: pass

                # Envio Menu
                flow = db.query(BotFlow).filter(BotFlow.bot_id == bot_db.id).first()
                modo = getattr(flow, 'start_mode', 'padrao') if flow else 'padrao'
                msg_txt = flow.msg_boas_vindas if flow else "Olá!"
                media = flow.media_url if flow else None
                
                mk = types.InlineKeyboardMarkup()
                
                # SE FOR MINI APP
                if modo == "miniapp" and flow and flow.miniapp_url:
                    url = flow.miniapp_url.replace("http://", "https://")
                    mk.add(types.InlineKeyboardButton(text=flow.miniapp_btn_text or "ABRIR LOJA 🛍️", web_app=types.WebAppInfo(url=url)))
                
                # SE FOR PADRÃO (AQUI ESTÁ A CORREÇÃO DOS PREÇOS ✅)
                else:
                    if flow and flow.mostrar_planos_1:
                        planos = db.query(PlanoConfig).filter(PlanoConfig.bot_id == bot_db.id).all()
                        for pl in planos: 
                            # Formata preço igual ao seu outro projeto
                            preco_txt = f"R$ {pl.preco_atual:.2f}".replace('.', ',')
                            mk.add(types.InlineKeyboardButton(f"💎 {pl.nome_exibicao} - {preco_txt}", callback_data=f"checkout_{pl.id}"))
                    else: 
                        mk.add(types.InlineKeyboardButton(flow.btn_text_1 if flow else "Ver Conteúdo", callback_data="step_1"))

                try:
                    if media:
                        if media.endswith(('.mp4', '.mov')): bot_temp.send_video(chat_id, media, caption=msg_txt, reply_markup=mk, parse_mode="HTML")
                        else: bot_temp.send_photo(chat_id, media, caption=msg_txt, reply_markup=mk, parse_mode="HTML")
                    else: bot_temp.send_message(chat_id, msg_txt, reply_markup=mk, parse_mode="HTML")
                except: bot_temp.send_message(chat_id, msg_txt, reply_markup=mk)

                return {"status": "ok"}

        # ----------------------------------------
        # 🎮 3. CALLBACKS (BOTÕES)
        # ----------------------------------------
        elif update.callback_query:
            try: 
                if not update.callback_query.data.startswith("check_payment_"):
                    bot_temp.answer_callback_query(update.callback_query.id)
            except: pass
            
            chat_id = update.callback_query.message.chat.id
            data = update.callback_query.data
            first_name = update.callback_query.from_user.first_name
            username = update.callback_query.from_user.username

            # --- A) NAVEGAÇÃO (step_) ---
            if data.startswith("step_"):
                try: current_step = int(data.split("_")[1])
                except: current_step = 1
                
                steps = db.query(BotFlowStep).filter(BotFlowStep.bot_id == bot_db.id).order_by(BotFlowStep.step_order).all()
                target_step = None
                is_last = False
                
                if current_step <= len(steps): target_step = steps[current_step - 1]
                else: is_last = True

                if target_step and not is_last:
                    mk = types.InlineKeyboardMarkup()
                    if target_step.mostrar_botao:
                        mk.add(types.InlineKeyboardButton(target_step.btn_texto or "Próximo ▶️", callback_data=f"step_{current_step + 1}"))
                    
                    sent_msg = None
                    try:
                        if target_step.msg_media:
                            if target_step.msg_media.lower().endswith(('.mp4', '.mov')):
                                sent_msg = bot_temp.send_video(chat_id, target_step.msg_media, caption=target_step.msg_texto, reply_markup=mk, parse_mode="HTML")
                            else:
                                sent_msg = bot_temp.send_photo(chat_id, target_step.msg_media, caption=target_step.msg_texto, reply_markup=mk, parse_mode="HTML")
                        else:
                            sent_msg = bot_temp.send_message(chat_id, target_step.msg_texto, reply_markup=mk, parse_mode="HTML")
                    except:
                        sent_msg = bot_temp.send_message(chat_id, target_step.msg_texto or "...", reply_markup=mk)

                    if not target_step.mostrar_botao and target_step.delay_seconds > 0:
                        time.sleep(target_step.delay_seconds)
                        if target_step.autodestruir and sent_msg:
                            try: bot_temp.delete_message(chat_id, sent_msg.message_id)
                            except: pass
                        
                        prox = db.query(BotFlowStep).filter(BotFlowStep.bot_id == bot_db.id, BotFlowStep.step_order == target_step.step_order + 1).first()
                        if prox: enviar_passo_automatico(bot_temp, chat_id, prox, bot_db, db)
                        else: enviar_oferta_final(bot_temp, chat_id, bot_db.fluxo, bot_db.id, db)
                else:
                    enviar_oferta_final(bot_temp, chat_id, bot_db.fluxo, bot_db.id, db)

            # --- B) CHECKOUT ---
            elif data.startswith("checkout_"):
                plano_id = data.split("_")[1]
                plano = db.query(PlanoConfig).filter(PlanoConfig.id == plano_id).first()
                if not plano: return {"status": "error"}

                lead_origem = db.query(Lead).filter(Lead.user_id == str(chat_id), Lead.bot_id == bot_db.id).first()
                track_id_pedido = lead_origem.tracking_id if lead_origem else None

                bump = db.query(OrderBumpConfig).filter(OrderBumpConfig.bot_id == bot_db.id, OrderBumpConfig.ativo == True).first()
                
                if bump:
                    mk = types.InlineKeyboardMarkup()
                    mk.row(
                        types.InlineKeyboardButton(f"{bump.btn_aceitar} (+ R$ {bump.preco:.2f})", callback_data=f"bump_yes_{plano.id}"),
                        types.InlineKeyboardButton(bump.btn_recusar, callback_data=f"bump_no_{plano.id}")
                    )
                    txt_bump = bump.msg_texto or f"Levar {bump.nome_produto} junto?"
                    try:
                        if bump.msg_media:
                            if bump.msg_media.lower().endswith(('.mp4','.mov')):
                                bot_temp.send_video(chat_id, bump.msg_media, caption=txt_bump, reply_markup=mk, parse_mode="HTML")
                            else:
                                bot_temp.send_photo(chat_id, bump.msg_media, caption=txt_bump, reply_markup=mk, parse_mode="HTML")
                        else:
                            bot_temp.send_message(chat_id, txt_bump, reply_markup=mk, parse_mode="HTML")
                    except:
                        bot_temp.send_message(chat_id, txt_bump, reply_markup=mk, parse_mode="HTML")
                else:
                    # PIX DIRETO
                    msg_wait = bot_temp.send_message(chat_id, "⏳ Gerando <b>PIX</b>...", parse_mode="HTML")
                    mytx = str(uuid.uuid4())
                    pix = gerar_pix_pushinpay(plano.preco_atual, mytx)
                    
                    if pix:
                        qr = pix.get('qr_code_text') or pix.get('qr_code')
                        txid = str(pix.get('id') or mytx).lower()
                        
                        novo_pedido = Pedido(
                            bot_id=bot_db.id, telegram_id=str(chat_id), first_name=first_name, username=username,
                            plano_nome=plano.nome_exibicao, plano_id=plano.id, valor=plano.preco_atual,
                            transaction_id=txid, qr_code=qr, status="pending", tem_order_bump=False, created_at=datetime.utcnow(),
                            tracking_id=track_id_pedido
                        )
                        db.add(novo_pedido)
                        db.commit()
                        
                        try: bot_temp.delete_message(chat_id, msg_wait.message_id)
                        except: pass
                        
                        markup_pix = types.InlineKeyboardMarkup()
                        markup_pix.add(types.InlineKeyboardButton("🔄 VERIFICAR STATUS DO PAGAMENTO", callback_data=f"check_payment_{txid}"))

                        msg_pix = f"🌟 Seu pagamento foi gerado com sucesso:\n🎁 Plano: <b>{plano.nome_exibicao}</b>\n💰 Valor: <b>R$ {plano.preco_atual:.2f}</b>\n🔐 Pague via Pix Copia e Cola:\n\n<pre>{qr}</pre>\n\n👆 Toque na chave PIX acima para copiá-la\n‼️ Após o pagamento, o acesso será liberado automaticamente!"
                        
                        bot_temp.send_message(chat_id, msg_pix, parse_mode="HTML", reply_markup=markup_pix)
                    else:
                        bot_temp.send_message(chat_id, "❌ Erro ao gerar PIX.")

            # --- C) BUMP YES/NO ---
            elif data.startswith("bump_yes_") or data.startswith("bump_no_"):
                aceitou = "yes" in data
                pid = data.split("_")[2]
                plano = db.query(PlanoConfig).filter(PlanoConfig.id == pid).first()
                
                lead_origem = db.query(Lead).filter(Lead.user_id == str(chat_id), Lead.bot_id == bot_db.id).first()
                track_id_pedido = lead_origem.tracking_id if lead_origem else None

                bump = db.query(OrderBumpConfig).filter(OrderBumpConfig.bot_id == bot_db.id).first()
                
                if bump and bump.autodestruir:
                    try: bot_temp.delete_message(chat_id, update.callback_query.message.message_id)
                    except: pass
                
                valor_final = plano.preco_atual
                nome_final = plano.nome_exibicao
                if aceitou and bump:
                    valor_final += bump.preco
                    nome_final += f" + {bump.nome_produto}"
                
                msg_wait = bot_temp.send_message(chat_id, f"⏳ Gerando PIX: <b>{nome_final}</b>...", parse_mode="HTML")
                mytx = str(uuid.uuid4())
                pix = gerar_pix_pushinpay(valor_final, mytx)
                
                if pix:
                    qr = pix.get('qr_code_text') or pix.get('qr_code')
                    txid = str(pix.get('id') or mytx).lower()
                    
                    novo_pedido = Pedido(
                        bot_id=bot_db.id, telegram_id=str(chat_id), first_name=first_name, username=username,
                        plano_nome=nome_final, plano_id=plano.id, valor=valor_final,
                        transaction_id=txid, qr_code=qr, status="pending", tem_order_bump=aceitou, created_at=datetime.utcnow(),
                        tracking_id=track_id_pedido
                    )
                    db.add(novo_pedido)
                    db.commit()
                    
                    try: bot_temp.delete_message(chat_id, msg_wait.message_id)
                    except: pass
                    
                    markup_pix = types.InlineKeyboardMarkup()
                    markup_pix.add(types.InlineKeyboardButton("🔄 VERIFICAR STATUS DO PAGAMENTO", callback_data=f"check_payment_{txid}"))

                    msg_pix = f"🌟 Seu pagamento foi gerado com sucesso:\n🎁 Plano: <b>{nome_final}</b>\n💰 Valor: <b>R$ {valor_final:.2f}</b>\n🔐 Pague via Pix Copia e Cola:\n\n<pre>{qr}</pre>\n\n👆 Toque na chave PIX acima para copiá-la\n‼️ Após o pagamento, o acesso será liberado automaticamente!"

                    bot_temp.send_message(chat_id, msg_pix, parse_mode="HTML", reply_markup=markup_pix)

            # --- D) PROMO ---
            elif data.startswith("promo_"):
                try: campanha_uuid = data.split("_")[1]
                except: campanha_uuid = ""
                
                campanha = db.query(RemarketingCampaign).filter(RemarketingCampaign.campaign_id == campanha_uuid).first()
                
                if not campanha:
                    bot_temp.send_message(chat_id, "❌ Oferta não encontrada ou expirada.")
                elif campanha.expiration_at and datetime.utcnow() > campanha.expiration_at:
                    bot_temp.send_message(chat_id, "🚫 <b>OFERTA ENCERRADA!</b>\n\nO tempo desta oferta acabou.", parse_mode="HTML")
                else:
                    plano = db.query(PlanoConfig).filter(PlanoConfig.id == campanha.plano_id).first()
                    if plano:
                        preco_final = campanha.promo_price if campanha.promo_price else plano.preco_atual
                        msg_wait = bot_temp.send_message(chat_id, "⏳ Gerando <b>OFERTA ESPECIAL</b>...", parse_mode="HTML")
                        mytx = str(uuid.uuid4())
                        pix = gerar_pix_pushinpay(preco_final, mytx)
                        
                        if pix:
                            qr = pix.get('qr_code_text') or pix.get('qr_code')
                            txid = str(pix.get('id') or mytx).lower()
                            
                            novo_pedido = Pedido(
                                bot_id=bot_db.id, telegram_id=str(chat_id), first_name=first_name, username=username,
                                plano_nome=f"{plano.nome_exibicao} (OFERTA)", plano_id=plano.id, valor=preco_final,
                                transaction_id=txid, qr_code=qr, status="pending", tem_order_bump=False, created_at=datetime.utcnow(),
                                tracking_id=None 
                            )
                            db.add(novo_pedido)
                            db.commit()
                            
                            try: bot_temp.delete_message(chat_id, msg_wait.message_id)
                            except: pass
                            
                            markup_pix = types.InlineKeyboardMarkup()
                            markup_pix.add(types.InlineKeyboardButton("🔄 VERIFICAR STATUS DO PAGAMENTO", callback_data=f"check_payment_{txid}"))

                            msg_pix = f"🌟 Seu pagamento foi gerado com sucesso:\n🎁 Plano: <b>{plano.nome_exibicao}</b>\n💰 Valor Promocional: <b>R$ {preco_final:.2f}</b>\n🔐 Pague via Pix Copia e Cola:\n\n<pre>{qr}</pre>\n\n👆 Toque na chave PIX acima para copiá-la\n‼️ Após o pagamento, o acesso será liberado automaticamente!"

                            bot_temp.send_message(chat_id, msg_pix, parse_mode="HTML", reply_markup=markup_pix)
                        else:
                            bot_temp.send_message(chat_id, "❌ Erro ao gerar PIX.")
                    else:
                        bot_temp.send_message(chat_id, "❌ Plano não encontrado.")

            # --- E) VERIFICAR STATUS ---
            elif data.startswith("check_payment_"):
                tx_id = data.split("_")[2]
                pedido = db.query(Pedido).filter(Pedido.transaction_id == tx_id).first()
                
                if not pedido:
                    bot_temp.answer_callback_query(update.callback_query.id, "❌ Pedido não encontrado.", show_alert=True)
                elif pedido.status in ['paid', 'approved', 'active']:
                    bot_temp.answer_callback_query(update.callback_query.id, "✅ Pagamento Aprovado!", show_alert=False)
                    bot_temp.send_message(chat_id, "✅ <b>O pagamento foi confirmado!</b>\nVerifique se você recebeu o link de acesso nas mensagens anteriores.", parse_mode="HTML")
                else:
                    bot_temp.answer_callback_query(update.callback_query.id, "⏳ Pagamento não identificado ainda. Tente novamente.", show_alert=True)

    except Exception as e:
        logger.error(f"Erro no webhook: {e}")

    return {"status": "ok"}

# ============================================================
# ROTA 1: LISTAR LEADS (TOPO DO FUNIL)
# ============================================================
@app.get("/api/admin/leads")
def listar_leads(
    bot_id: Optional[int] = None,
    page: int = 1,
    per_page: int = 50,
    db: Session = Depends(get_db)
):
    """
    Lista leads (usuários que só deram /start)
    """
    try:
        # Query base
        query = db.query(Lead)
        
        # Filtro por bot
        if bot_id:
            query = query.filter(Lead.bot_id == bot_id)
        
        # Contagem total
        total = query.count()
        
        # Paginação
        offset = (page - 1) * per_page
        leads = query.order_by(Lead.created_at.desc()).offset(offset).limit(per_page).all()
        
        # Formata resposta
        leads_data = []
        for lead in leads:
            leads_data.append({
                "id": lead.id,
                "user_id": lead.user_id,
                "nome": lead.nome,
                "username": lead.username,
                "bot_id": lead.bot_id,
                "status": lead.status,
                "funil_stage": lead.funil_stage,
                "primeiro_contato": lead.primeiro_contato.isoformat() if lead.primeiro_contato else None,
                "ultimo_contato": lead.ultimo_contato.isoformat() if lead.ultimo_contato else None,
                "total_remarketings": lead.total_remarketings,
                "ultimo_remarketing": lead.ultimo_remarketing.isoformat() if lead.ultimo_remarketing else None,
                "created_at": lead.created_at.isoformat() if lead.created_at else None
            })
        
        return {
            "data": leads_data,
            "total": total,
            "page": page,
            "per_page": per_page,
            "total_pages": (total + per_page - 1) // per_page
        }
    
    except Exception as e:
        logger.error(f"Erro ao listar leads: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))


# ============================================================
# ROTA 2: ESTATÍSTICAS DO FUNIL
# ============================================================
# ============================================================
# 🔥 ROTA ATUALIZADA: /api/admin/contacts/funnel-stats
# SUBSTITUA a rota existente por esta versão
# Calcula estatísticas baseando-se no campo 'status' (não status_funil)
# ============================================================

@app.get("/api/admin/contacts/funnel-stats")
def obter_estatisticas_funil(
    bot_id: Optional[int] = None,
    db: Session = Depends(get_db)
):
    """
    🔥 [CORRIGIDO] Retorna contadores de cada estágio do funil
    TOPO = Leads (tabela Lead)
    MEIO = Pedidos com status 'pending' (gerou PIX mas não pagou)
    FUNDO = Pedidos com status 'paid/active/approved' (pagou)
    EXPIRADO = Pedidos com status 'expired'
    """
    try:
        # ============================================================
        # TOPO: Contar LEADS (tabela Lead)
        # ============================================================
        query_topo = db.query(Lead)
        if bot_id:
            query_topo = query_topo.filter(Lead.bot_id == bot_id)
        topo = query_topo.count()
        
        # ============================================================
        # MEIO: Pedidos com status PENDING (gerou PIX, não pagou)
        # ============================================================
        query_meio = db.query(Pedido).filter(Pedido.status == 'pending')
        if bot_id:
            query_meio = query_meio.filter(Pedido.bot_id == bot_id)
        meio = query_meio.count()
        
        # ============================================================
        # FUNDO: Pedidos PAGOS (paid/active/approved)
        # ============================================================
        query_fundo = db.query(Pedido).filter(
            Pedido.status.in_(['paid', 'active', 'approved'])
        )
        if bot_id:
            query_fundo = query_fundo.filter(Pedido.bot_id == bot_id)
        fundo = query_fundo.count()
        
        # ============================================================
        # EXPIRADOS: Pedidos com status EXPIRED
        # ============================================================
        query_expirados = db.query(Pedido).filter(Pedido.status == 'expired')
        if bot_id:
            query_expirados = query_expirados.filter(Pedido.bot_id == bot_id)
        expirados = query_expirados.count()
        
        # Total
        total = topo + meio + fundo + expirados
        
        logger.info(f"📊 Estatísticas do funil: TOPO={topo}, MEIO={meio}, FUNDO={fundo}, EXPIRADOS={expirados}")
        
        return {
            "topo": topo,
            "meio": meio,
            "fundo": fundo,
            "expirados": expirados,
            "total": total
        }
    
    except Exception as e:
        logger.error(f"Erro ao obter estatísticas do funil: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))


# ============================================================
# ROTA 3: ATUALIZAR ROTA DE CONTATOS EXISTENTE
# ============================================================
# Procure a rota @app.get("/api/admin/contacts") no seu main.py
# e SUBSTITUA por esta versão atualizada:

# ============================================================
# 🔥 ROTA ATUALIZADA: /api/admin/contacts
# SUBSTITUA a rota existente por esta versão
# ADICIONA SUPORTE PARA FILTROS: meio, fundo, expirado
# ============================================================

# ============================================================
# 🔥 ROTA ATUALIZADA: /api/admin/contacts (CORREÇÃO DE FUSO HORÁRIO)
# ============================================================

# ============================================================
# 🔥 ROTA ATUALIZADA: /api/admin/contacts
# ============================================================
@app.get("/api/admin/contacts")
async def get_contacts(
    status: str = "todos",
    bot_id: Optional[int] = None,
    page: int = 1,
    per_page: int = 50,
    db: Session = Depends(get_db)
):
    try:
        offset = (page - 1) * per_page
        all_contacts = []
        
        # Helper para garantir data sem timezone
        def clean_date(dt):
            if not dt: return datetime.utcnow()
            return dt.replace(tzinfo=None)

        # 1. Filtro TODOS (Mescla Leads + Pedidos)
        if status == "todos":
            contatos_unicos = {}
            
            # Busca Leads
            q_leads = db.query(Lead)
            if bot_id: q_leads = q_leads.filter(Lead.bot_id == bot_id)
            leads = q_leads.all()
            
            for l in leads:
                tid = str(l.user_id)
                contatos_unicos[tid] = {
                    "id": l.id,
                    "telegram_id": tid,
                    "user_id": tid,
                    "first_name": l.nome or "Sem nome",
                    "username": l.username,
                    "plano_nome": "-",
                    "valor": 0.0,
                    "status": "pending",
                    "role": "user",
                    "created_at": clean_date(l.created_at),
                    "status_funil": "topo",
                    "origem": "lead"
                }
            
            # Busca Pedidos (Sobrescreve)
            q_pedidos = db.query(Pedido)
            if bot_id: q_pedidos = q_pedidos.filter(Pedido.bot_id == bot_id)
            pedidos = q_pedidos.all()
            
            for p in pedidos:
                tid = str(p.telegram_id)
                st_funil = "meio"
                if p.status in ["paid", "approved", "active"]: st_funil = "fundo"
                elif p.status == "expired": st_funil = "expirado"
                
                contatos_unicos[tid] = {
                    "id": p.id,
                    "telegram_id": tid,
                    "user_id": tid,
                    "first_name": p.first_name or "Sem nome",
                    "username": p.username,
                    "plano_nome": p.plano_nome,
                    "valor": p.valor,
                    "status": p.status,
                    "role": "user",
                    "created_at": clean_date(p.created_at),
                    "status_funil": st_funil,
                    "origem": "pedido",
                    "custom_expiration": p.custom_expiration
                }
            
            all_contacts = list(contatos_unicos.values())
            all_contacts.sort(key=lambda x: x["created_at"], reverse=True)
            
            total = len(all_contacts)
            paginated = all_contacts[offset:offset + per_page]
            
            return {
                "data": paginated,
                "total": total,
                "page": page,
                "per_page": per_page,
                "total_pages": (total + per_page - 1) // per_page
            }

        # 2. Outros Filtros (Consultam direto Pedido)
        else:
            query = db.query(Pedido)
            if bot_id: query = query.filter(Pedido.bot_id == bot_id)
            
            if status == "meio" or status == "pendentes":
                query = query.filter(Pedido.status == "pending")
            elif status == "fundo" or status == "pagantes":
                query = query.filter(Pedido.status.in_(["paid", "active", "approved"]))
            elif status == "expirado" or status == "expirados":
                query = query.filter(Pedido.status == "expired")
                
            total = query.count()
            pedidos = query.offset(offset).limit(per_page).all()
            
            contacts = []
            for p in pedidos:
                contacts.append({
                    "id": p.id,
                    "telegram_id": p.telegram_id,
                    "first_name": p.first_name,
                    "username": p.username,
                    "plano_nome": p.plano_nome,
                    "valor": p.valor,
                    "status": p.status,
                    "role": "user",
                    "created_at": clean_date(p.created_at),
                    "status_funil": status,
                    "custom_expiration": p.custom_expiration
                })
                
            return {
                "data": contacts,
                "total": total,
                "page": page,
                "per_page": per_page,
                "total_pages": (total + per_page - 1) // per_page
            }

    except Exception as e:
        logger.error(f"Erro contatos: {e}")
        raise HTTPException(500, str(e))

# ============================================================
# 🔥 ROTAS COMPLETAS - Adicione no main.py
# LOCAL: Após as rotas de /api/admin/contacts (linha ~2040)
# ============================================================

# ============================================================
# ROTA 1: Atualizar Usuário (UPDATE)
# ============================================================
@app.put("/api/admin/users/{user_id}")
async def update_user(user_id: int, data: dict, db: Session = Depends(get_db)):
    """
    ✏️ Atualiza informações de um usuário (status, role, custom_expiration)
    """
    try:
        # 1. Buscar pedido
        pedido = db.query(Pedido).filter(Pedido.id == user_id).first()
        
        if not pedido:
            logger.error(f"❌ Pedido {user_id} não encontrado")
            raise HTTPException(status_code=404, detail="Pedido não encontrado")
        
        # 2. Atualizar campos
        if "status" in data:
            pedido.status = data["status"]
            logger.info(f"✅ Status atualizado para: {data['status']}")
        
        if "role" in data:
            pedido.role = data["role"]
            logger.info(f"✅ Role atualizado para: {data['role']}")
        
        if "custom_expiration" in data:
            if data["custom_expiration"] == "remover" or data["custom_expiration"] == "":
                pedido.custom_expiration = None
                logger.info(f"✅ Data de expiração removida (Vitalício)")
            else:
                # Converter string para datetime
                try:
                    pedido.custom_expiration = datetime.strptime(data["custom_expiration"], "%Y-%m-%d")
                    logger.info(f"✅ Data de expiração atualizada: {data['custom_expiration']}")
                except:
                    # Se já for datetime, usa direto
                    pedido.custom_expiration = data["custom_expiration"]
        
        # 3. Salvar no banco
        db.commit()
        db.refresh(pedido)
        
        logger.info(f"✅ Usuário {user_id} atualizado com sucesso!")
        
        return {
            "status": "success",
            "message": "Usuário atualizado com sucesso!",
            "data": {
                "id": pedido.id,
                "telegram_id": pedido.telegram_id,
                "status": pedido.status,
                "role": pedido.role,
                "custom_expiration": pedido.custom_expiration.isoformat() if pedido.custom_expiration else None
            }
        }
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"❌ Erro ao atualizar usuário: {e}")
        db.rollback()
        raise HTTPException(status_code=500, detail=str(e))


# ============================================================
# ROTA 2: Reenviar Acesso
# ============================================================
@app.post("/api/admin/users/{user_id}/resend-access")
async def resend_user_access(user_id: int, db: Session = Depends(get_db)):
    """
    🔑 Reenvia o link de acesso VIP para um usuário que já pagou
    """
    try:
        # 1. Buscar pedido
        pedido = db.query(Pedido).filter(Pedido.id == user_id).first()
        
        if not pedido:
            logger.error(f"❌ Pedido {user_id} não encontrado")
            raise HTTPException(status_code=404, detail="Pedido não encontrado")
        
        # 2. Verificar se está pago
        if pedido.status not in ["paid", "active", "approved"]:
            logger.error(f"❌ Pedido {user_id} não está pago (status: {pedido.status})")
            raise HTTPException(
                status_code=400, 
                detail="Pedido não está pago. Altere o status para 'Ativo/Pago' primeiro."
            )
        
        # 3. Buscar bot
        bot_data = db.query(Bot).filter(Bot.id == pedido.bot_id).first()
        
        if not bot_data:
            logger.error(f"❌ Bot {pedido.bot_id} não encontrado")
            raise HTTPException(status_code=404, detail="Bot não encontrado")
        
        # 4. Verificar se bot tem canal configurado
        if not bot_data.id_canal_vip:
            logger.error(f"❌ Bot {pedido.bot_id} não tem canal VIP configurado")
            raise HTTPException(status_code=400, detail="Bot não tem canal VIP configurado")
        
        # 5. Gerar novo link e enviar
        try:
            tb = telebot.TeleBot(bot_data.token)
            
            # Tratamento do ID do Canal
            try: 
                canal_id = int(str(bot_data.id_canal_vip).strip())
            except: 
                canal_id = bot_data.id_canal_vip
            
            # Tenta desbanir antes (caso tenha sido banido)
            try:
                tb.unban_chat_member(canal_id, int(pedido.telegram_id))
                logger.info(f"🔓 Usuário {pedido.telegram_id} desbanido do canal")
            except Exception as e:
                logger.warning(f"⚠️ Não foi possível desbanir usuário: {e}")
            
            # Gera Link Único
            convite = tb.create_chat_invite_link(
                chat_id=canal_id,
                member_limit=1,
                name=f"Reenvio {pedido.first_name}"
            )
            
            # Formata data de validade
            texto_validade = "VITALÍCIO ♾️"
            if pedido.custom_expiration:
                texto_validade = pedido.custom_expiration.strftime("%d/%m/%Y")
            
            # Envia mensagem
            msg_cliente = (
                f"✅ <b>Acesso Reenviado!</b>\n"
                f"📅 Validade: <b>{texto_validade}</b>\n\n"
                f"Seu acesso exclusivo:\n👉 {convite.invite_link}\n\n"
                f"<i>Use este link para entrar no grupo VIP.</i>"
            )
            
            tb.send_message(int(pedido.telegram_id), msg_cliente, parse_mode="HTML")
            
            logger.info(f"✅ Acesso reenviado para {pedido.first_name} (ID: {pedido.telegram_id})")
            
            return {
                "status": "success",
                "message": "Acesso reenviado com sucesso!",
                "telegram_id": pedido.telegram_id,
                "nome": pedido.first_name,
                "validade": texto_validade
            }
            
        except telebot.apihelper.ApiTelegramException as e:
            logger.error(f"❌ Erro da API do Telegram: {e}")
            raise HTTPException(status_code=500, detail=f"Erro do Telegram: {str(e)}")
        except Exception as e_tg:
            logger.error(f"❌ Erro ao enviar acesso via Telegram: {e_tg}")
            raise HTTPException(status_code=500, detail=f"Erro ao enviar via Telegram: {str(e_tg)}")
    
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"❌ Erro ao reenviar acesso: {e}")
        raise HTTPException(status_code=500, detail=str(e))

# --- ROTAS FLOW V2 (HÍBRIDO) ---
@app.get("/api/admin/bots/{bot_id}/flow")
def get_flow(bot_id: int, db: Session = Depends(get_db)):
    f = db.query(BotFlow).filter(BotFlow.bot_id == bot_id).first()
    if not f: return {"msg_boas_vindas": "Olá!", "btn_text_1": "DESBLOQUEAR"}
    return f

@app.post("/api/admin/bots/{bot_id}/flow")
def save_flow(bot_id: int, flow: FlowUpdate, db: Session = Depends(get_db)):
    f = db.query(BotFlow).filter(BotFlow.bot_id == bot_id).first()
    if not f: f = BotFlow(bot_id=bot_id)
    db.add(f)
    f.msg_boas_vindas = flow.msg_boas_vindas
    f.media_url = flow.media_url
    f.btn_text_1 = flow.btn_text_1
    f.autodestruir_1 = flow.autodestruir_1
    f.msg_2_texto = flow.msg_2_texto
    f.msg_2_media = flow.msg_2_media
    f.mostrar_planos_2 = flow.mostrar_planos_2
    db.commit()
    return {"status": "saved"}

@app.get("/api/admin/bots/{bot_id}/flow/steps")
def list_steps(bot_id: int, db: Session = Depends(get_db)):
    return db.query(BotFlowStep).filter(BotFlowStep.bot_id == bot_id).order_by(BotFlowStep.step_order).all()

@app.post("/api/admin/bots/{bot_id}/flow/steps")
def add_step(bot_id: int, p: FlowStepCreate, db: Session = Depends(get_db)):
    ns = BotFlowStep(bot_id=bot_id, step_order=p.step_order, msg_texto=p.msg_texto, msg_media=p.msg_media, btn_texto=p.btn_texto)
    db.add(ns)
    db.commit()
    return {"status": "ok"}

@app.delete("/api/admin/bots/{bot_id}/flow/steps/{sid}")
def del_step(bot_id: int, sid: int, db: Session = Depends(get_db)):
    s = db.query(BotFlowStep).filter(BotFlowStep.id == sid).first()
    if s:
        db.delete(s)
        db.commit()
    return {"status": "deleted"}

# =========================================================
# FUNÇÃO DE BACKGROUND (CORRIGIDA: SESSÃO INDEPENDENTE)
# =========================================================
def processar_envio_remarketing(campaign_db_id: int, bot_id: int, payload: RemarketingRequest):
    """
    Executa o envio em background usando uma NOVA sessão de banco (SessionLocal).
    Isso impede que os dados fiquem zerados por queda de conexão.
    """
    # 🔥 CRIA NOVA SESSÃO DEDICADA (O SEGREDO PARA SALVAR OS DADOS)
    db = SessionLocal() 
    
    try:
        # 1. Recupera a Campanha criada na rota e o Bot
        campanha = db.query(RemarketingCampaign).filter(RemarketingCampaign.id == campaign_db_id).first()
        bot_db = db.query(Bot).filter(Bot.id == bot_id).first()
        
        if not campanha or not bot_db:
            return

        logger.info(f"🚀 INICIANDO DISPARO BACKGROUND | Bot: {bot_db.nome}")

        # 2. Configura Oferta (se houver)
        uuid_campanha = campanha.campaign_id
        plano_db = None
        preco_final = 0.0
        data_expiracao = None

        if payload.incluir_oferta and payload.plano_oferta_id:
            # Busca Flexível (String ou Int)
            plano_db = db.query(PlanoConfig).filter(
                (PlanoConfig.key_id == str(payload.plano_oferta_id)) | 
                (PlanoConfig.id == int(payload.plano_oferta_id) if str(payload.plano_oferta_id).isdigit() else False)
            ).first()

            if plano_db:
                # Lógica de Preço
                if payload.price_mode == 'custom' and payload.custom_price and payload.custom_price > 0:
                    preco_final = payload.custom_price
                else:
                    preco_final = plano_db.preco_atual
                
                # Lógica de Expiração
                if payload.expiration_mode != "none" and payload.expiration_value:
                    val = int(payload.expiration_value)
                    agora = datetime.utcnow()
                    if payload.expiration_mode == "minutes": data_expiracao = agora + timedelta(minutes=val)
                    elif payload.expiration_mode == "hours": data_expiracao = agora + timedelta(hours=val)
                    elif payload.expiration_mode == "days": data_expiracao = agora + timedelta(days=val)

        # 3. Define Lista de IDs
        bot_sender = telebot.TeleBot(bot_db.token)
        target = str(payload.target).lower()
        lista_final_ids = []

        if payload.is_test:
            if payload.specific_user_id: 
                lista_final_ids = [str(payload.specific_user_id).strip()]
            else:
                adm = db.query(BotAdmin).filter(BotAdmin.bot_id == bot_id).first()
                if adm: lista_final_ids = [str(adm.telegram_id).strip()]
        else:
            q_todos = db.query(Pedido.telegram_id).filter(Pedido.bot_id == bot_id).distinct()
            ids_todos = {str(r[0]).strip() for r in q_todos.all() if r[0]}
            
            q_pagos = db.query(Pedido.telegram_id).filter(Pedido.bot_id == bot_id, func.lower(Pedido.status).in_(['paid', 'active', 'approved', 'completed', 'succeeded'])).distinct()
            ids_pagantes = {str(r[0]).strip() for r in q_pagos.all() if r[0]}
            
            q_expirados = db.query(Pedido.telegram_id).filter(Pedido.bot_id == bot_id, func.lower(Pedido.status) == 'expired').distinct()
            ids_expirados = {str(r[0]).strip() for r in q_expirados.all() if r[0]}

            if target in ['pendentes', 'leads', 'nao_pagantes']:
                lista_final_ids = list(ids_todos - ids_pagantes - ids_expirados)
            elif target in ['pagantes', 'ativos']:
                lista_final_ids = list(ids_pagantes)
            elif target in ['expirados', 'ex_assinantes']:
                lista_final_ids = list(ids_expirados - ids_pagantes)
            else:
                lista_final_ids = list(ids_todos)

        # Atualiza Total Previsto no Banco
        # USAMOS UPDATE DIRETO PARA GARANTIR GRAVAÇÃO
        db.query(RemarketingCampaign).filter(RemarketingCampaign.id == campaign_db_id).update({"total_leads": len(lista_final_ids)})
        db.commit()

        # 4. Markup (Botão)
        markup = None
        if plano_db:
            markup = types.InlineKeyboardMarkup()
            preco_txt = f"{preco_final:.2f}".replace('.', ',')
            btn_text = f"🔥 {plano_db.nome_exibicao} - R$ {preco_txt}"
            cb_data = f"checkout_{plano_db.id}" if payload.is_test else f"promo_{uuid_campanha}"
            markup.add(types.InlineKeyboardButton(btn_text, callback_data=cb_data))

        # 5. Loop de Envio (HTML)
        sent_count = 0
        blocked_count = 0

        for uid in lista_final_ids:
            if not uid or len(uid) < 5: continue
            try:
                midia_ok = False
                if payload.media_url and len(payload.media_url) > 5:
                    try:
                        ext = payload.media_url.lower()
                        if ext.endswith(('.mp4', '.mov', '.avi')):
                            bot_sender.send_video(uid, payload.media_url, caption=payload.mensagem, reply_markup=markup, parse_mode="HTML")
                        else:
                            bot_sender.send_photo(uid, payload.media_url, caption=payload.mensagem, reply_markup=markup, parse_mode="HTML")
                        midia_ok = True
                    except: pass 
                
                if not midia_ok:
                    bot_sender.send_message(uid, payload.mensagem, reply_markup=markup, parse_mode="HTML")
                
                sent_count += 1
                time.sleep(0.05) # Delay anti-spam
                
            except Exception as e:
                err = str(e).lower()
                if "blocked" in err or "kicked" in err or "deactivated" in err or "not found" in err:
                    blocked_count += 1

        
        # 6. ATUALIZAÇÃO FINAL NO BANCO (JSON HÍBRIDO + UPDATE DIRETO)
        
        config_completa = {
            "msg": payload.mensagem,          # Chave curta (Legado)
            "mensagem": payload.mensagem,     # Chave longa (Frontend)
            "media": payload.media_url,       # Chave curta
            "media_url": payload.media_url,   # Chave longa
            "offer": payload.incluir_oferta,  # Chave curta
            "incluir_oferta": payload.incluir_oferta, # Chave longa
            "plano_id": payload.plano_oferta_id,
            "plano_oferta_id": payload.plano_oferta_id,
            "custom_price": preco_final,
            "price_mode": payload.price_mode,
            "expiration_mode": payload.expiration_mode,
            "expiration_value": payload.expiration_value
        }
        
        # MÁGICA: Update direto no banco para não perder os dados
        update_data = {
            "status": "concluido",
            "sent_success": sent_count,
            "blocked_count": blocked_count,
            "config": json.dumps(config_completa),
            "expiration_at": data_expiracao
        }
        
        if plano_db:
            update_data["plano_id"] = plano_db.id
            update_data["promo_price"] = preco_final

        db.query(RemarketingCampaign).filter(RemarketingCampaign.id == campaign_db_id).update(update_data)
        db.commit() # 🔥 Commit na sessão dedicada salva os números reais!
        
        logger.info(f"✅ FINALIZADO: {sent_count} envios / {blocked_count} bloqueados")

    except Exception as e:
        logger.error(f"Erro na thread de remarketing: {e}")
    finally:
        db.close() # Fecha a conexão dedicada

@app.post("/api/admin/remarketing/send")
def enviar_remarketing(payload: RemarketingRequest, background_tasks: BackgroundTasks, db: Session = Depends(get_db)):
    # 1. Validação de Teste
    if payload.is_test and not payload.specific_user_id:
        ultimo = db.query(Pedido).filter(Pedido.bot_id == payload.bot_id).order_by(Pedido.id.desc()).first()
        if ultimo: payload.specific_user_id = ultimo.telegram_id
        else:
            admin = db.query(BotAdmin).filter(BotAdmin.bot_id == payload.bot_id).first()
            if admin: payload.specific_user_id = admin.telegram_id
            else: raise HTTPException(400, "Nenhum usuário encontrado para teste.")

    # 2. Cria o Registro Inicial (Status: Enviando)
    uuid_campanha = str(uuid.uuid4())
    nova_campanha = RemarketingCampaign(
        bot_id=payload.bot_id,
        campaign_id=uuid_campanha,
        type="teste" if payload.is_test else "massivo",
        target=payload.target,
        # Salva config inicial compatível
        config=json.dumps({"msg": payload.mensagem, "mensagem": payload.mensagem, "media": payload.media_url}), 
        status="enviando",
        data_envio=datetime.utcnow(),
        total_leads=0,
        sent_success=0,
        blocked_count=0
    )
    db.add(nova_campanha)
    db.commit()
    db.refresh(nova_campanha)

    # 3. Inicia Background Task (Passa APENAS IDs, não a sessão)
    background_tasks.add_task(
        processar_envio_remarketing, 
        nova_campanha.id,  # ID da campanha para atualizar depois
        payload.bot_id, 
        payload
    )
    
    return {"status": "enviando", "msg": "Campanha iniciada! Acompanhe no histórico.", "campaign_id": nova_campanha.id}


# --- ROTA DE REENVIO INDIVIDUAL (CORRIGIDA PARA HTML) ---
@app.post("/api/admin/remarketing/send-individual")
def enviar_remarketing_individual(payload: IndividualRemarketingRequest, db: Session = Depends(get_db)):
    # 1. Busca Campanha
    campanha = db.query(RemarketingCampaign).filter(RemarketingCampaign.id == payload.campaign_history_id).first()
    if not campanha: raise HTTPException(404, "Campanha não encontrada")
    
    # 2. Parse Config
    try:
        config = json.loads(campanha.config) if isinstance(campanha.config, str) else campanha.config
        if isinstance(config, str): config = json.loads(config)
    except: config = {}

    # Busca chaves novas OU antigas (Compatibilidade Total)
    msg = config.get("mensagem") or config.get("msg", "")
    media = config.get("media_url") or config.get("media", "")

    # 3. Configura Bot
    bot_db = db.query(Bot).filter(Bot.id == payload.bot_id).first()
    if not bot_db: raise HTTPException(404, "Bot não encontrado")
    sender = telebot.TeleBot(bot_db.token)
    
    # 4. Botão
    markup = None
    if campanha.plano_id:
        plano = db.query(PlanoConfig).filter(PlanoConfig.id == campanha.plano_id).first()
        if plano:
            markup = types.InlineKeyboardMarkup()
            preco = campanha.promo_price if campanha.promo_price else plano.preco_atual
            btn_text = f"🔥 {plano.nome_exibicao} - R$ {preco:.2f}".replace('.', ',')
            markup.add(types.InlineKeyboardButton(btn_text, callback_data=f"checkout_{plano.id}"))

    # 5. Envio (HTML)
    try:
        if media:
            try:
                ext = media.lower()
                if ext.endswith(('.mp4', '.mov', '.avi')):
                    sender.send_video(payload.user_telegram_id, media, caption=msg, reply_markup=markup, parse_mode="HTML")
                else:
                    sender.send_photo(payload.user_telegram_id, media, caption=msg, reply_markup=markup, parse_mode="HTML")
            except:
                sender.send_message(payload.user_telegram_id, msg, reply_markup=markup, parse_mode="HTML")
        else:
            sender.send_message(payload.user_telegram_id, msg, reply_markup=markup, parse_mode="HTML")
            
        return {"status": "sent", "msg": "Reenviado com sucesso!"}
    except Exception as e:
        logger.error(f"Erro envio individual: {e}")
        raise HTTPException(500, detail=str(e))

@app.get("/api/admin/remarketing/status")
def status_remarketing():
    return CAMPAIGN_STATUS

# =========================================================
# ROTA DE HISTÓRICO (CORRIGIDA PARA COMPATIBILIDADE)
# =========================================================
# URL Ajustada para bater com o api.js antigo: /api/admin/remarketing/history/{bot_id}
@app.get("/api/admin/remarketing/history/{bot_id}") 
def get_remarketing_history(
    bot_id: int, 
    page: int = 1, 
    per_page: int = 10, # Frontend manda 'per_page', não 'limit'
    db: Session = Depends(get_db)
):
    try:
        limit = min(per_page, 50)
        skip = (page - 1) * limit
        
        # Filtra pelo bot_id
        query = db.query(RemarketingCampaign).filter(RemarketingCampaign.bot_id == bot_id)
        
        total = query.count()
        # Ordena por data (descrescente)
        campanhas = query.order_by(desc(RemarketingCampaign.data_envio)).offset(skip).limit(limit).all()
            
        data = []
        for c in campanhas:
            # Formatação segura da data
            data_formatada = c.data_envio.isoformat() if c.data_envio else None
            
            data.append({
                "id": c.id,
                "data": data_formatada, 
                "target": c.target,
                "total": c.total_leads,
                "sent_success": c.sent_success,
                "blocked_count": c.blocked_count,
                "config": c.config
            })

        # Cálculo correto de páginas
        total_pages = (total // limit) + (1 if total % limit > 0 else 0)

        return {
            "data": data,
            "total": total,
            "page": page,
            "per_page": limit,
            "total_pages": total_pages
        }
    except Exception as e:
        logger.error(f"Erro ao buscar histórico: {e}")
        return {"data": [], "total": 0, "page": 1, "total_pages": 0}

# ============================================================
# ROTA 2: DELETE HISTÓRICO (NOVA!)
# ============================================================
# COLE ESTA ROTA NOVA logo APÓS a rota de histórico:

@app.delete("/api/admin/remarketing/history/{history_id}")
def delete_remarketing_history(history_id: int, db: Session = Depends(get_db)):
    """
    Deleta uma campanha do histórico.
    """
    campanha = db.query(RemarketingCampaign).filter(
        RemarketingCampaign.id == history_id
    ).first()
    
    if not campanha:
        raise HTTPException(status_code=404, detail="Campanha não encontrada")
    
    db.delete(campanha)
    db.commit()
    
    return {"status": "ok", "message": "Campanha deletada com sucesso"}


# =========================================================
# 📊 ROTA DE DASHBOARD (KPIs REAIS E CUMULATIVOS)
# =========================================================
# =========================================================
# 📊 ROTA DE DASHBOARD V2 (COM FILTRO DE DATA)
# =========================================================
@app.get("/api/admin/dashboard/stats")
def dashboard_stats(
    bot_id: Optional[int] = None, 
    start_date: Optional[str] = None, 
    end_date: Optional[str] = None, 
    db: Session = Depends(get_db),
    current_user = Depends(get_current_user)  # 🔒 ADICIONA AUTH
):
    """
    Dashboard V2: Suporta filtro por período.
    🔥 CORREÇÃO: Inclui 'expired' no cálculo financeiro.
    🔒 PROTEGIDO: Apenas dados dos bots do usuário
    """
    try:
        # 1. DEFINIÇÃO DO PERÍODO
        agora = datetime.utcnow()
        
        if start_date and end_date:
            dt_inicio = datetime.fromisoformat(start_date.replace('Z', '+00:00')).replace(hour=0, minute=0, second=0, tzinfo=None)
            dt_fim = datetime.fromisoformat(end_date.replace('Z', '+00:00')).replace(hour=23, minute=59, second=59, tzinfo=None)
        else:
            dt_inicio = datetime(agora.year, agora.month, 1)
            dt_fim = agora

        hoje_inicio = datetime(agora.year, agora.month, agora.day)

        # 🔒 FILTRA APENAS BOTS DO USUÁRIO
        if bot_id:
            # Se bot_id fornecido, verifica se pertence ao usuário
            verificar_bot_pertence_usuario(bot_id, current_user.id, db)
            bots_ids = [bot_id]
        else:
            # Pega todos os bots do usuário
            bots = db.query(Bot).filter(Bot.owner_id == current_user.id).all()
            bots_ids = [b.id for b in bots]

        # QUERIES FILTRADAS PELOS BOTS DO USUÁRIO
        query_leads = db.query(Lead).filter(Lead.bot_id.in_(bots_ids))
        query_pedidos = db.query(Pedido).filter(Pedido.bot_id.in_(bots_ids))

        # Leads no período
        leads_periodo = query_leads.filter(
            Lead.created_at >= dt_inicio,
            Lead.created_at <= dt_fim
        ).count()

        leads_hoje = query_leads.filter(Lead.created_at >= hoje_inicio).count()
        
        # ============================================================
        # 💰 [CORRIGIDO] LISTA DE STATUS PAGOS (INCLUI EXPIRADO)
        # ============================================================
        status_pagos = ['paid', 'active', 'approved', 'completed', 'succeeded', 'expired']
        
        # Vendas no período selecionado
        vendas_periodo = query_pedidos.filter(
            Pedido.status.in_(status_pagos),
            Pedido.created_at >= dt_inicio,
            Pedido.created_at <= dt_fim
        )
        
        # Total Faturamento
        total_revenue = db.query(func.sum(Pedido.valor)).filter(
            Pedido.bot_id.in_(bots_ids),  # 🔒 FILTRO
            Pedido.status.in_(status_pagos),
            Pedido.created_at >= dt_inicio,
            Pedido.created_at <= dt_fim
        )
        valor_total_revenue = total_revenue.scalar() or 0.0

        # Assinantes (Transações no período)
        total_transactions = vendas_periodo.count()
        
        # Assinantes Ativos Totais (Base geral - AQUI NÃO CONTA EXPIRADO, POIS É "ATIVO")
        status_ativos_reais = ['paid', 'active', 'approved', 'completed', 'succeeded']
        total_active_users = db.query(Pedido).filter(
            Pedido.bot_id.in_(bots_ids),  # 🔒 FILTRO
            Pedido.status.in_(status_ativos_reais)
        )
        count_active_users = total_active_users.count()

        # Ticket Médio
        ticket_medio = (valor_total_revenue / total_transactions) if total_transactions > 0 else 0.0

        # Vendas Hoje
        sales_today_query = db.query(func.sum(Pedido.valor)).filter(
            Pedido.bot_id.in_(bots_ids),  # 🔒 FILTRO
            Pedido.status.in_(status_pagos),
            Pedido.created_at >= hoje_inicio
        )
        valor_sales_today = sales_today_query.scalar() or 0.0

        # Taxa de Conversão
        universo_total = leads_periodo + total_transactions
        conversao = (total_transactions / universo_total * 100) if universo_total > 0 else 0.0

        # --- DADOS DO GRÁFICO ---
        chart_data = []
        delta = (dt_fim - dt_inicio).days
        dias_para_grafico = min(delta + 1, 366)
        
        for i in range(dias_para_grafico):
            data_loop = dt_inicio + timedelta(days=i)
            data_loop_fim = data_loop + timedelta(days=1)
            
            soma_dia = db.query(func.sum(Pedido.valor)).filter(
                Pedido.bot_id.in_(bots_ids),  # 🔒 FILTRO
                Pedido.status.in_(status_pagos),
                Pedido.created_at >= data_loop,
                Pedido.created_at < data_loop_fim
            )
            
            val = soma_dia.scalar() or 0.0
            
            chart_data.append({
                "name": data_loop.strftime("%d/%m"), 
                "value": val
            })

        return {
            "total_revenue": valor_total_revenue,
            "active_users": count_active_users,
            "sales_today": valor_sales_today,
            "leads_mes": leads_periodo,
            "leads_hoje": leads_hoje,
            "ticket_medio": ticket_medio,
            "total_transacoes": total_transactions,
            "reembolsos": 0.0,
            "taxa_conversao": round(conversao, 2),
            "chart_data": chart_data
        }

    except Exception as e:
        logger.error(f"❌ Erro no dashboard stats: {str(e)}")
        return {
            "total_revenue": 0.0, "active_users": 0, "sales_today": 0.0,
            "leads_mes": 0, "leads_hoje": 0, "ticket_medio": 0.0,
            "total_transacoes": 0, "reembolsos": 0.0, "taxa_conversao": 0.0,
            "chart_data": []
        }
# =========================================================
# 💸 WEBHOOK DE PAGAMENTO (BLINDADO E TAGARELA)
# =========================================================
@app.post("/api/webhook")
async def webhook(req: Request, bg_tasks: BackgroundTasks):
    try:
        raw = await req.body()
        try: 
            payload = json.loads(raw)
        except: 
            # Fallback para formato x-www-form-urlencoded
            payload = {k: v[0] for k,v in urllib.parse.parse_qs(raw.decode()).items()}
        
        status_pag = str(payload.get('status')).upper()
        
        if status_pag in ['PAID', 'APPROVED', 'COMPLETED', 'SUCCEEDED']:
            db = SessionLocal()
            tx = str(payload.get('id')).lower() # ID da transação
            
            p = db.query(Pedido).filter(Pedido.transaction_id == tx).first()
            
            if p and p.status != 'paid':
                p.status = 'paid'
                db.commit() # Salva o status pago
                
                # --- 🔔 NOTIFICAÇÃO AO ADMIN ---
                try:
                    bot_db = db.query(Bot).filter(Bot.id == p.bot_id).first()
                    
                    if bot_db and bot_db.admin_principal_id:
                        msg_venda = (
                            f"💰 *VENDA APROVADA (SITE)!*\n\n"
                            f"👤 Cliente: {p.first_name}\n"
                            f"💎 Plano: {p.plano_nome}\n"
                            f"💵 Valor: R$ {p.valor:.2f}\n"
                            f"🆔 ID/User: {p.telegram_id}"
                        )
                        # Chama a função auxiliar de notificação (assumindo que existe no seu código)
                        notificar_admin_principal(bot_db, msg_venda) 
                except Exception as e_notify:
                    logger.error(f"Erro ao notificar admin: {e_notify}")

                # --- ENVIO DO LINK DE ACESSO AO CLIENTE ---
                if not p.mensagem_enviada:
                    try:
                        bot_data = db.query(Bot).filter(Bot.id == p.bot_id).first()
                        tb = telebot.TeleBot(bot_data.token)
                        
                        # 🔥 Tenta converter para INT. Se falhar (é username), ignora envio automático
                        target_chat_id = None
                        try:
                            target_chat_id = int(p.telegram_id)
                        except:
                            logger.warning(f"⚠️ ID não numérico ({p.telegram_id}). Cliente deve iniciar o bot manualmente.")
                        
                        if target_chat_id:
                            # Tenta converter o ID do canal VIP com segurança
                            try: canal_vip_id = int(str(bot_data.id_canal_vip).strip())
                            except: canal_vip_id = bot_data.id_canal_vip

                            # Tenta desbanir o usuário antes (garantia)
                            try: tb.unban_chat_member(canal_vip_id, target_chat_id)
                            except: pass

                            # Gera Link Único (Válido para 1 pessoa)
                            convite = tb.create_chat_invite_link(
                                chat_id=canal_vip_id, 
                                member_limit=1, 
                                name=f"Venda {p.first_name}"
                            )
                            link_acesso = convite.invite_link

                            msg_sucesso = f"""
✅ <b>Pagamento Confirmado!</b>

Seu acesso ao <b>{bot_data.nome}</b> foi liberado.
Toque no link abaixo para entrar no Canal VIP:

👉 {link_acesso}

⚠️ <i>Este link é único e válido apenas para você.</i>
"""
                            # Envia a mensagem com o link para o usuário
                            tb.send_message(target_chat_id, msg_sucesso, parse_mode="HTML")
                            
                            p.mensagem_enviada = True
                            db.commit()
                            logger.info(f"🏆 Link enviado para {p.first_name}")

                    except Exception as e_telegram:
                        logger.error(f"❌ ERRO TELEGRAM: {e_telegram}")
                        # Fallback (opcional): Tentar avisar se falhar
                        try:
                            if target_chat_id:
                                tb.send_message(target_chat_id, "✅ Pagamento recebido! \n\n⚠️ Houve um erro ao gerar seu link automático. Um administrador entrará em contato em breve.")
                        except: pass

            db.close()
        
        return {"status": "received"}

    except Exception as e:
        logger.error(f"❌ ERRO CRÍTICO NO WEBHOOK: {e}")
        return {"status": "error"}

# ============================================================
# TRECHO 3: FUNÇÃO "enviar_passo_automatico"
# ============================================================

# ============================================================
# TRECHO 3: FUNÇÃO "enviar_passo_automatico" (CORRIGIDA + HTML)
# ============================================================

def enviar_passo_automatico(bot_temp, chat_id, passo, bot_db, db):
    """
    Envia um passo automaticamente após o delay (COM HTML).
    Similar à lógica do next_step_, mas sem callback do usuário.
    """
    logger.info(f"✅ [BOT {bot_db.id}] Enviando passo {passo.step_order} automaticamente: {passo.msg_texto[:30]}...")
    
    # 1. Verifica se existe passo seguinte (CRÍTICO: Fazer isso ANTES de tudo)
    passo_seguinte = db.query(BotFlowStep).filter(
        BotFlowStep.bot_id == bot_db.id, 
        BotFlowStep.step_order == passo.step_order + 1
    ).first()
    
    # 2. Define o callback do botão (Baseado na existência do próximo)
    if passo_seguinte:
        next_callback = f"next_step_{passo.step_order}"
    else:
        next_callback = "go_checkout"
    
    # 3. Cria botão (se configurado para mostrar)
    markup_step = types.InlineKeyboardMarkup()
    if passo.mostrar_botao:
        markup_step.add(types.InlineKeyboardButton(
            text=passo.btn_texto, 
            callback_data=next_callback
        ))
    
    # 4. Envia a mensagem e SALVA o message_id (Com HTML)
    sent_msg = None
    try:
        if passo.msg_media:
            try:
                if passo.msg_media.lower().endswith(('.mp4', '.mov')):
                    sent_msg = bot_temp.send_video(
                        chat_id, 
                        passo.msg_media, 
                        caption=passo.msg_texto, 
                        reply_markup=markup_step if passo.mostrar_botao else None,
                        parse_mode="HTML" # 🔥 Adicionado HTML
                    )
                else:
                    sent_msg = bot_temp.send_photo(
                        chat_id, 
                        passo.msg_media, 
                        caption=passo.msg_texto, 
                        reply_markup=markup_step if passo.mostrar_botao else None,
                        parse_mode="HTML" # 🔥 Adicionado HTML
                    )
            except Exception as e_media:
                logger.error(f"Erro ao enviar mídia no passo automático: {e_media}")
                # Fallback para texto se a mídia falhar
                sent_msg = bot_temp.send_message(
                    chat_id, 
                    passo.msg_texto, 
                    reply_markup=markup_step if passo.mostrar_botao else None,
                    parse_mode="HTML" # 🔥 Adicionado HTML
                )
        else:
            sent_msg = bot_temp.send_message(
                chat_id, 
                passo.msg_texto, 
                reply_markup=markup_step if passo.mostrar_botao else None,
                parse_mode="HTML" # 🔥 Adicionado HTML
            )
        
        # 5. Lógica Automática (Recursividade e Delay)
        # Se NÃO tem botão E tem delay E tem próximo passo
        if not passo.mostrar_botao and passo.delay_seconds > 0 and passo_seguinte:
            logger.info(f"⏰ [BOT {bot_db.id}] Aguardando {passo.delay_seconds}s antes do próximo...")
            time.sleep(passo.delay_seconds)
            
            # Auto-destruir antes de enviar a próxima
            if passo.autodestruir and sent_msg:
                try:
                    bot_temp.delete_message(chat_id, sent_msg.message_id)
                    logger.info(f"💣 [BOT {bot_db.id}] Mensagem do passo {passo.step_order} auto-destruída (automático)")
                except:
                    pass
            
            # Chama o próximo passo (Recursivo)
            enviar_passo_automatico(bot_temp, chat_id, passo_seguinte, bot_db, db)
            
        # Se NÃO tem botão E NÃO tem próximo passo (Fim da Linha)
        elif not passo.mostrar_botao and not passo_seguinte:
            # Acabaram os passos, vai pro checkout (Oferta Final)
            # Se tiver delay no último passo antes da oferta, espera também
            if passo.delay_seconds > 0:
                 time.sleep(passo.delay_seconds)
                 
            enviar_oferta_final(bot_temp, chat_id, bot_db.fluxo, bot_db.id, db)
            
    except Exception as e:
        logger.error(f"❌ [BOT {bot_db.id}] Erro crítico ao enviar passo automático: {e}")

# =========================================================
# 📤 FUNÇÃO AUXILIAR: ENVIAR OFERTA FINAL
# =========================================================
def enviar_oferta_final(tb, cid, fluxo, bot_id, db):
    """Envia a oferta final (Planos)"""
    mk = types.InlineKeyboardMarkup()
    planos = db.query(PlanoConfig).filter(PlanoConfig.bot_id == bot_id).all()
    
    if fluxo and fluxo.mostrar_planos_2:
        for p in planos:
            mk.add(types.InlineKeyboardButton(
                f"💎 {p.nome_exibicao} - R$ {p.preco_atual:.2f}", 
                callback_data=f"checkout_{p.id}"
            ))
    
    txt = fluxo.msg_2_texto if (fluxo and fluxo.msg_2_texto) else "Escolha seu plano:"
    med = fluxo.msg_2_media if fluxo else None
    
    try:
        if med:
            if med.endswith(('.mp4','.mov')): 
                tb.send_video(cid, med, caption=txt, reply_markup=mk)
            else: 
                tb.send_photo(cid, med, caption=txt, reply_markup=mk)
        else:
            tb.send_message(cid, txt, reply_markup=mk)
    except:
        tb.send_message(cid, txt, reply_markup=mk)
# =========================================================
# 🏆 ROTA DE PERFIL & CONQUISTAS (PERFIL GLOBAL)
# =========================================================
# =========================================================
# 👤 PERFIL E ESTATÍSTICAS (BLINDADO FASE 2)
# =========================================================
@app.get("/api/admin/profile")
def get_user_profile(
    db: Session = Depends(get_db),
    current_user = Depends(get_current_user) # 🔒 AUTH OBRIGATÓRIA
):
    """
    Retorna dados do perfil, mas calcula estatísticas APENAS
    dos bots que pertencem ao usuário logado.
    """
    try:
        # 1. Identificar quais bots pertencem a este usuário
        user_bots = db.query(Bot).filter(Bot.owner_id == current_user.id).all()
        bot_ids = [b.id for b in user_bots]
        
        # Estatísticas Básicas (Filtradas pelo Dono)
        total_bots = len(user_bots)
        
        # Se o usuário não tem bots, retornamos zerado para evitar erro de SQL (IN empty)
        if total_bots == 0:
            return {
                "name": current_user.full_name or current_user.username,
                "avatar_url": None,
                "stats": {
                    "total_bots": 0,
                    "total_members": 0,
                    "total_revenue": 0.0,
                    "total_sales": 0
                },
                "gamification": {
                    "current_level": {"name": "Iniciante", "target": 100},
                    "next_level": {"name": "Empreendedor", "target": 1000},
                    "progress_percentage": 0
                }
            }

        # 2. Calcular Membros (Leads) apenas dos bots do usuário
        total_members = db.query(Lead).filter(Lead.bot_id.in_(bot_ids)).count()

        # 3. Calcular Vendas e Receita apenas dos bots do usuário
        total_sales = db.query(Pedido).filter(
            Pedido.bot_id.in_(bot_ids), 
            Pedido.status == 'approved'
        ).count()

        total_revenue = db.query(func.sum(Pedido.valor)).filter(
            Pedido.bot_id.in_(bot_ids), 
            Pedido.status == 'approved'
        ).scalar() or 0.0

        # 4. Lógica de Gamificação (Níveis baseados no Faturamento do Usuário)
        levels = [
            {"name": "Iniciante", "target": 100},
            {"name": "Empreendedor", "target": 1000},
            {"name": "Barão", "target": 5000},
            {"name": "Magnata", "target": 10000},
            {"name": "Imperador", "target": 50000}
        ]
        
        current_level = levels[0]
        next_level = levels[1]
        
        for i, level in enumerate(levels):
            if total_revenue >= level["target"]:
                current_level = level
                next_level = levels[i+1] if i+1 < len(levels) else None
        
        # Cálculo da porcentagem
        progress = 0
        if next_level:
            # Quanto falta para o próximo nível
            diff_target = next_level["target"] - current_level["target"]
            diff_current = total_revenue - current_level["target"]
            # Evita divisão por zero
            if diff_target > 0:
                progress = (diff_current / diff_target) * 100
                if progress > 100: progress = 100
                if progress < 0: progress = 0
        else:
            progress = 100 # Nível máximo atingido

        return {
            "name": current_user.full_name or current_user.username,
            "avatar_url": None, # Futuro: Adicionar campo no banco
            "stats": {
                "total_bots": total_bots,
                "total_members": total_members,
                "total_revenue": float(total_revenue),
                "total_sales": total_sales
            },
            "gamification": {
                "current_level": current_level,
                "next_level": next_level,
                "progress_percentage": round(progress, 1)
            }
        }

    except Exception as e:
        logger.error(f"Erro ao carregar perfil: {e}")
        raise HTTPException(status_code=500, detail="Erro interno ao carregar perfil")

@app.post("/api/admin/profile")
def update_profile(data: ProfileUpdate, db: Session = Depends(get_db)):
    """
    Atualiza Nome e Foto do Administrador
    """
    try:
        # Atualiza ou Cria Nome
        conf_name = db.query(SystemConfig).filter(SystemConfig.key == "admin_name").first()
        if not conf_name:
            conf_name = SystemConfig(key="admin_name")
            db.add(conf_name)
        conf_name.value = data.name
        
        # Atualiza ou Cria Avatar
        conf_avatar = db.query(SystemConfig).filter(SystemConfig.key == "admin_avatar").first()
        if not conf_avatar:
            conf_avatar = SystemConfig(key="admin_avatar")
            db.add(conf_avatar)
        conf_avatar.value = data.avatar_url or ""
        
        db.commit()
        return {"status": "success", "msg": "Perfil atualizado!"}
        
    except Exception as e:
        logger.error(f"Erro ao atualizar perfil: {e}")
        raise HTTPException(status_code=500, detail="Erro ao salvar perfil")

# =========================================================
# 🛒 ROTA PÚBLICA PARA O MINI APP (ESSA É A CORRETA ✅)
# =========================================================
@app.get("/api/miniapp/{bot_id}")
def get_miniapp_config(bot_id: int, db: Session = Depends(get_db)):
    # Busca configurações visuais
    config = db.query(MiniAppConfig).filter(MiniAppConfig.bot_id == bot_id).first()
    # Busca categorias
    cats = db.query(MiniAppCategory).filter(MiniAppCategory.bot_id == bot_id).all()
    # Busca fluxo (para saber link e texto do botão)
    flow = db.query(BotFlow).filter(BotFlow.bot_id == bot_id).first()
    
    # Se não tiver config, retorna padrão para não quebrar o front
    start_mode = getattr(flow, 'start_mode', 'padrao') if flow else 'padrao'
    
    if not config:
        return {
            "config": {
                "hero_title": "Loja VIP", 
                "background_value": "#000000",
                "start_mode": start_mode
            },
            "categories": [],
            "flow": {"start_mode": start_mode}
        }

    return {
        "config": config,
        "categories": cats,
        "flow": {
            "start_mode": start_mode,
            "miniapp_url": getattr(flow, 'miniapp_url', ''),
            "miniapp_btn_text": getattr(flow, 'miniapp_btn_text', 'ABRIR LOJA')
        }
    }

# =========================================================
# ⚙️ STARTUP OTIMIZADA (SEM MIGRAÇÕES REPETIDAS)
# =========================================================
@app.on_event("startup")
def on_startup():
    init_db()
    executar_migracao_v3()
    executar_migracao_v4()
    executar_migracao_v5()  # <--- ADICIONE ESTA LINHA
    executar_migracao_v6() # <--- ADICIONE AQUI
    
    print("Starting Container - Zenyx")
    
    # 1. Cria tabelas básicas se não existirem
    try:
        init_db()
    except Exception as e:
        logger.error(f"Erro no init_db: {e}")
    
    logger.info("✅ Sistema Iniciado e Pronto!")

@app.get("/")
def home():

    return {"status": "Zenyx SaaS Online - Banco Atualizado"}
@app.get("/admin/clean-leads-to-pedidos")
def limpar_leads_que_viraram_pedidos(db: Session = Depends(get_db)):
    """
    Remove da tabela LEADS os usuários que já geraram PEDIDOS.
    Evita duplicação entre TOPO (leads) e TODOS (pedidos).
    """
    try:
        total_removidos = 0
        bots = db.query(Bot).all()
        
        for bot in bots:
            # Buscar todos os telegram_ids que existem em PEDIDOS
            pedidos_ids = db.query(Pedido.telegram_id).filter(
                Pedido.bot_id == bot.id
            ).distinct().all()
            
            pedidos_ids = [str(pid[0]) for pid in pedidos_ids if pid[0]]
            
            # Deletar LEADS que têm user_id igual a algum telegram_id dos pedidos
            for telegram_id in pedidos_ids:
                leads_para_deletar = db.query(Lead).filter(
                    Lead.bot_id == bot.id,
                    Lead.user_id == telegram_id
                ).all()
                
                for lead in leads_para_deletar:
                    db.delete(lead)
                    total_removidos += 1
        
        db.commit()
        
        return {
            "status": "ok",
            "leads_removidos": total_removidos,
            "mensagem": f"Removidos {total_removidos} leads que viraram pedidos"
        }
    
    except Exception as e:
        db.rollback()
        logger.error(f"Erro: {e}")
        return {"status": "error", "mensagem": str(e)}