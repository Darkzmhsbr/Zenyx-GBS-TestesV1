import os
import logging
import telebot
from telebot import TeleBot  # ← ADICIONADO: Import explícito do TeleBot
import httpx
import time
import urllib.parse
import threading
from telebot import types
import json
import uuid
from sqlalchemy.exc import IntegrityError

from sqlalchemy import func, desc, text
from fastapi import FastAPI, HTTPException, Depends, Request, BackgroundTasks
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

# Adicionamos 'Optional' aqui para evitar erro de validação bruta
from pydantic import BaseModel, EmailStr, Field 
from sqlalchemy.orm import Session
from typing import List, Optional 
from datetime import datetime, timedelta

# --- IMPORTS CORRIGIDOS ---
from database import Lead
from force_migration import forcar_atualizacao_tabelas

# 🆕 ADICIONAR ESTES IMPORTS PARA AUTENTICAÇÃO
from passlib.context import CryptContext
from jose import JWTError, jwt
from fastapi.security import OAuth2PasswordBearer, OAuth2PasswordRequestForm

# --- Scheduler ---
from apscheduler.schedulers.asyncio import AsyncIOScheduler

import asyncio
from threading import Lock

# Importa o banco e o script de reparo
# ← MODIFICADO: Renomear Bot para BotModel para evitar conflito com TeleBot
from database import (
    SessionLocal, 
    init_db, 
    Bot as BotModel,  # ← RENOMEADO
    PlanoConfig, 
    BotFlow, 
    BotFlowStep, 
    Pedido, 
    SystemConfig, 
    RemarketingCampaign, 
    BotAdmin, 
    Lead, 
    OrderBumpConfig, 
    TrackingFolder, 
    TrackingLink, 
    MiniAppConfig, 
    MiniAppCategory, 
    AuditLog, 
    Notification, 
    User, 
    engine,
    WebhookRetry,
    RemarketingConfig,      # ← ADICIONAR
    AlternatingMessages,    # ← ADICIONAR
    RemarketingSentLog      # ← ADICIONAR
)
import update_db 

from migration_v3 import executar_migracao_v3
from migration_v4 import executar_migracao_v4
from migration_v5 import executar_migracao_v5
from migration_v6 import executar_migracao_v6

# Configuração de Log
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = FastAPI(title="Zenyx Gbot SaaS")


# ============================================================
# PASSO 2: ADICIONAR VARIÁVEIS GLOBAIS (linha ~70-100, antes das funções)
# ============================================================
# Controle de remarketing
remarketing_lock = Lock()
remarketing_timers = {}  # {chat_id: asyncio.Task}
alternating_tasks = {}   # {chat_id: asyncio.Task}


# ============================================================
# FUNÇÕES DE JOBS AGENDADOS
# ============================================================

async def verificar_vencimentos():
    """
    Job agendado para verificar e processar vencimentos de assinaturas.
    Executa a cada 12 horas.
    """
    try:
        logger.info("🔄 [JOB] Iniciando verificação de vencimentos...")
        
        db = SessionLocal()
        
        try:
            # Buscar pedidos ativos que venceram
            pedidos_vencidos = db.query(Pedido).filter(
                Pedido.status == 'ativo',
                Pedido.validade < datetime.now()
            ).all()
            
            if not pedidos_vencidos:
                logger.info("✅ [JOB] Nenhum vencimento encontrado")
                return
            
            logger.info(f"📋 [JOB] {len(pedidos_vencidos)} vencimentos encontrados")
            
            # Processar cada vencimento
            for pedido in pedidos_vencidos:
                try:
                    # Atualizar status do pedido
                    pedido.status = 'vencido'
                    pedido.updated_at = datetime.now()
                    
                    # Remover do grupo Telegram (se configurado)
                    if pedido.grupo_id and pedido.user and pedido.user.telegram_id:
                        try:
                            # Buscar bot associado ao grupo
                            bot_config = db.query(BotModel).filter(
                                BotModel.grupo_telegram_id == pedido.grupo_id
                            ).first()
                            
                            if bot_config and bot_config.token:
                                # Criar instância do TeleBot
                                bot = TeleBot(bot_config.token)
                                
                                # Remover usuário do grupo
                                bot.ban_chat_member(
                                    chat_id=int(pedido.grupo_id),
                                    user_id=int(pedido.user.telegram_id)
                                )
                                bot.unban_chat_member(
                                    chat_id=int(pedido.grupo_id),
                                    user_id=int(pedido.user.telegram_id)
                                )
                                
                                logger.info(
                                    f"👋 [JOB] Usuário {pedido.user.nome} "
                                    f"removido do grupo"
                                )
                        except Exception as e:
                            logger.warning(
                                f"⚠️  [JOB] Erro ao remover do grupo: {str(e)}"
                            )
                    
                    db.commit()
                    logger.info(f"✅ [JOB] Pedido #{pedido.id} marcado como vencido")
                    
                except Exception as e:
                    logger.error(
                        f"❌ [JOB] Erro ao processar pedido #{pedido.id}: {str(e)}"
                    )
                    db.rollback()
                    continue
            
            logger.info("✅ [JOB] Verificação de vencimentos concluída")
            
        finally:
            db.close()
        
    except Exception as e:
        logger.error(f"❌ [JOB] Erro crítico na verificação de vencimentos: {str(e)}")


async def processar_webhooks_pendentes():
    """
    Job agendado para reprocessar webhooks falhados.
    Executa a cada 1 minuto.
    """
    try:
        logger.info("🔄 [WEBHOOK-RETRY] Iniciando reprocessamento...")
        
        db = SessionLocal()
        
        try:
            # Buscar webhooks pendentes que estão prontos para retry
            webhooks = db.query(WebhookRetry).filter(
                WebhookRetry.status == 'pending',
                WebhookRetry.attempts < WebhookRetry.max_attempts,
                (WebhookRetry.next_retry == None) | (WebhookRetry.next_retry <= datetime.now())
            ).order_by(WebhookRetry.created_at).limit(10).all()
            
            if not webhooks:
                return  # Sem webhooks para processar
            
            logger.info(f"📋 [WEBHOOK-RETRY] {len(webhooks)} webhooks para reprocessar")
            
            for webhook in webhooks:
                try:
                    # Deserializar payload
                    payload = json.loads(webhook.payload)
                    
                    # Incrementar tentativas
                    webhook.attempts += 1
                    
                    # Reprocessar baseado no tipo
                    success = False
                    error_msg = None
                    
                    if webhook.webhook_type == 'pushinpay':
                        try:
                            # TODO: Chamar função real de processamento
                            # await processar_webhook_pix(payload)
                            success = True  # Placeholder por enquanto
                        except Exception as e:
                            error_msg = str(e)
                    
                    # Atualizar registro
                    if success:
                        webhook.status = 'success'
                        webhook.updated_at = datetime.now()
                        logger.info(f"✅ [WEBHOOK-RETRY] Webhook #{webhook.id} processado")
                    else:
                        # Calcular próximo retry (exponential backoff)
                        backoff_minutes = 2 ** webhook.attempts  # 2, 4, 8, 16, 32
                        webhook.next_retry = datetime.now() + timedelta(minutes=backoff_minutes)
                        
                        # Verificar se esgotou tentativas
                        if webhook.attempts >= webhook.max_attempts:
                            webhook.status = 'failed'
                            webhook.next_retry = None
                            logger.error(
                                f"❌ [WEBHOOK-RETRY] Webhook #{webhook.id} "
                                f"esgotou tentativas"
                            )
                        else:
                            webhook.status = 'pending'
                        
                        webhook.last_error = error_msg
                        webhook.updated_at = datetime.now()
                    
                    db.commit()
                
                except Exception as e:
                    logger.error(
                        f"❌ [WEBHOOK-RETRY] Erro ao processar webhook "
                        f"#{webhook.id}: {str(e)}"
                    )
                    db.rollback()
                    continue
            
            logger.info("✅ [WEBHOOK-RETRY] Reprocessamento concluído")
            
        finally:
            db.close()
        
    except Exception as e:
        logger.error(f"❌ [WEBHOOK-RETRY] Erro crítico: {str(e)}")


# ============================================================
# CONFIGURAÇÃO DO SCHEDULER
# ============================================================

# ============ HTTPX CLIENT GLOBAL ============
http_client = None

# =========================================================
# 🚀 STARTUP: INICIALIZAÇÃO DO SERVIDOR
# =========================================================
@app.on_event("startup")
async def startup_event():
    """
    Executado quando o servidor FastAPI inicia.
    Inicializa componentes críticos do sistema.
    """
    global http_client
    
    try:
        # 1. INICIALIZAR HTTP CLIENT (httpx)
        http_client = httpx.AsyncClient(
            timeout=httpx.Timeout(30.0, connect=10.0),
            limits=httpx.Limits(max_keepalive_connections=20, max_connections=100),
            follow_redirects=True
        )
        logger.info("✅ [STARTUP] HTTP Client (httpx) inicializado")
        
        # 2. VERIFICAR BANCO DE DADOS
        db = SessionLocal()
        db.execute(text("SELECT 1"))
        db.close()
        logger.info("✅ [STARTUP] Conexão com banco de dados validada")
        
        # 3. INICIAR SCHEDULER
        scheduler.start()
        logger.info("✅ [STARTUP] Job de vencimentos agendado (12h)")
        logger.info("✅ [STARTUP] Job de retry de webhooks agendado (1 min)")
        logger.info("⏰ [STARTUP] Scheduler iniciado com sucesso")
        
        logger.info("=" * 60)
        logger.info("🚀 ZENYX GBOT v5.0 - Sistema iniciado com sucesso!")
        logger.info("=" * 60)
        
    except Exception as e:
        logger.error(f"❌ [STARTUP] Erro crítico na inicialização: {e}")
        # Não falhar completamente - permitir que a API suba


@app.on_event("shutdown")
async def shutdown_event():
    """
    Executado quando o servidor FastAPI é desligado.
    Fecha conexões e libera recursos.
    """
    global http_client
    
    # 1. Fechar HTTP Client
    if http_client:
        try:
            await http_client.aclose()
            logger.info("✅ [SHUTDOWN] HTTP Client fechado")
        except Exception as e:
            logger.error(f"❌ [SHUTDOWN] Erro ao fechar HTTP Client: {e}")
    
    # 2. Parar Scheduler
    try:
        if scheduler.running:
            scheduler.shutdown(wait=False)
            logger.info("✅ [SHUTDOWN] Scheduler encerrado")
    except Exception as e:
        logger.error(f"❌ [SHUTDOWN] Erro ao encerrar Scheduler: {e}")
    
    logger.info("👋 [SHUTDOWN] Sistema encerrado")

# ============================================================

# ============================================================
# 🔄 JOBS DE DISPARO AUTOMÁTICO (CORE LÓGICO)
# ============================================================

async def start_alternating_messages_job(
    bot_token: str,
    chat_id: int,
    message_id: int,
    messages: list,
    interval_seconds: int,
    stop_at: datetime,
    auto_destruct: bool,
    bot_id: int
):
    try:
        bot = TeleBot(bot_token, threaded=False)
        index = 0
        
        logger.info(f"✅ [ALTERNATING] Iniciado - User: {chat_id}, Msgs: {len(messages)}")
        
        while datetime.now() < stop_at:
            try:
                current_message = messages[index % len(messages)]
                bot.edit_message_text(
                    chat_id=chat_id,
                    message_id=message_id,
                    text=current_message,
                    parse_mode='HTML'
                )
                index += 1
                
                remaining = (stop_at - datetime.now()).total_seconds()
                sleep_time = min(interval_seconds, remaining)
                
                if sleep_time <= 0: break
                
                await asyncio.sleep(sleep_time)
                
            except Exception as e:
                # Ignora erros de "mensagem não encontrada" (usuário apagou)
                if "message to edit not found" in str(e).lower(): break
                await asyncio.sleep(interval_seconds)
        
        if auto_destruct:
            try:
                await asyncio.sleep(1)
                bot.delete_message(chat_id=chat_id, message_id=message_id)
            except: pass
            
    except asyncio.CancelledError:
        pass
    except Exception as e:
        logger.error(f"❌ [ALTERNATING] Erro: {e}")
    finally:
        with remarketing_lock:
            if chat_id in alternating_tasks:
                del alternating_tasks[chat_id]


async def send_remarketing_job(
    bot_token: str,
    chat_id: int,
    config_dict: dict,
    user_info: dict,
    bot_id: int
):
    try:
        delay = config_dict.get('delay_minutes', 5)
        await asyncio.sleep(delay * 60)
        
        db = SessionLocal()
        try:
            # Verifica pagamento
            if db.query(Pedido).filter(
                Pedido.bot_id == bot_id, 
                Pedido.telegram_id == str(chat_id), 
                Pedido.status.in_(['paid', 'ativo'])
            ).first():
                return

            # Verifica envio hoje
            hoje = datetime.now().date()
            if db.query(RemarketingSentLog).filter(
                RemarketingSentLog.bot_id == bot_id,
                RemarketingSentLog.user_telegram_id == chat_id,
                func.date(RemarketingSentLog.sent_at) == hoje
            ).first():
                return

            # Prepara mensagem
            msg_text = config_dict.get('message_text', '')
            if user_info:
                msg_text = msg_text.replace('{first_name}', user_info.get('first_name', ''))
                msg_text = msg_text.replace('{plano_original}', user_info.get('plano', 'VIP'))
                msg_text = msg_text.replace('{valor_original}', str(user_info.get('valor', '')))

            # Botões
            markup = types.InlineKeyboardMarkup()
            promos = config_dict.get('promo_values', {})
            for pid, pdata in promos.items():
                if isinstance(pdata, dict) and pdata.get('price'):
                    btn_txt = pdata.get('button_text', 'Ver Oferta 🔥')
                    markup.add(types.InlineKeyboardButton(btn_txt, callback_data=f"promo_{pid}"))

            bot = TeleBot(bot_token, threaded=False)
            sent_msg = None
            
            media = config_dict.get('media_url')
            mtype = config_dict.get('media_type')
            
            try:
                if media and mtype == 'photo':
                    sent_msg = bot.send_photo(chat_id, media, caption=msg_text, reply_markup=markup, parse_mode='HTML')
                elif media and mtype == 'video':
                    sent_msg = bot.send_video(chat_id, media, caption=msg_text, reply_markup=markup, parse_mode='HTML')
                else:
                    sent_msg = bot.send_message(chat_id, msg_text, reply_markup=markup, parse_mode='HTML')
                
                db.add(RemarketingSentLog(
                    bot_id=bot_id, user_telegram_id=chat_id, 
                    message_text=msg_text, status='sent', sent_at=datetime.now()
                ))
                db.commit()
                
                # Auto destruição
                destruct = config_dict.get('auto_destruct_seconds', 0)
                if destruct > 0 and sent_msg:
                    await asyncio.sleep(destruct)
                    try: bot.delete_message(chat_id, sent_msg.message_id)
                    except: pass

            except Exception as e:
                logger.error(f"Erro envio remarketing: {e}")

        finally:
            db.close()

    except asyncio.CancelledError:
        pass
    finally:
        with remarketing_lock:
            if chat_id in remarketing_timers:
                del remarketing_timers[chat_id]


async def cleanup_orphan_jobs():
    try:
        with remarketing_lock:
            active_users = list(remarketing_timers.keys())
        
        if not active_users: return
        
        db = SessionLocal()
        try:
            pagantes = db.query(Pedido.telegram_id).filter(
                Pedido.status == 'paid', 
                Pedido.telegram_id.in_([str(uid) for uid in active_users])
            ).all()
            
            for uid in [int(p.telegram_id) for p in pagantes]:
                with remarketing_lock:
                    if uid in remarketing_timers: 
                        remarketing_timers[uid].cancel()
                        del remarketing_timers[uid]
                    if uid in alternating_tasks: 
                        alternating_tasks[uid].cancel()
                        del alternating_tasks[uid]
        finally: db.close()
    except Exception as e: 
        logger.error(f"❌ [CLEANUP] Erro: {e}")

def schedule_remarketing_and_alternating(bot_id: int, chat_id: int, payment_message_id: int, user_info: dict):
    try:
        db = SessionLocal()
        try:
            config = db.query(RemarketingConfig).filter(RemarketingConfig.bot_id == bot_id, RemarketingConfig.is_active == True).first()
            if not config: return

            bot = db.query(BotModel).filter(BotModel.id == bot_id).first()
            if not bot or not bot.token: return

            config_dict = {
                'message_text': config.message_text, 'media_url': config.media_url, 'media_type': config.media_type,
                'delay_minutes': config.delay_minutes, 'auto_destruct_seconds': config.auto_destruct_seconds,
                'promo_values': config.promo_values or {}
            }

            # Agenda Alternating
            alt_config = db.query(AlternatingMessages).filter(AlternatingMessages.bot_id == bot_id, AlternatingMessages.is_active == True).first()
            if alt_config and alt_config.messages:
                stop_at = datetime.now() + timedelta(minutes=config.delay_minutes) - timedelta(seconds=alt_config.stop_before_remarketing_seconds)
                loop = asyncio.get_event_loop()
                task = loop.create_task(start_alternating_messages_job(
                    bot.token, chat_id, payment_message_id, alt_config.messages, 
                    alt_config.rotation_interval_seconds, stop_at, alt_config.auto_destruct_final, bot_id
                ))
                with remarketing_lock: alternating_tasks[chat_id] = task

            # Agenda Remarketing
            loop = asyncio.get_event_loop()
            task = loop.create_task(send_remarketing_job(bot.token, chat_id, config_dict, user_info, bot_id))
            with remarketing_lock: remarketing_timers[chat_id] = task

        finally: db.close()
    except Exception as e: logger.error(f"❌ [SCHEDULE] Erro: {e}")

# ============================================================
# CONFIGURAÇÃO DO SCHEDULER (MOVIDO PARA CÁ)
# ============================================================

scheduler = AsyncIOScheduler()

# Adicionar jobs
scheduler.add_job(
    verificar_vencimentos,
    'interval',
    hours=12,
    id='verificar_vencimentos'
)

scheduler.add_job(
    processar_webhooks_pendentes,
    'interval',
    minutes=1,
    id='webhook_retry_processor'
)

scheduler.add_job(
    cleanup_orphan_jobs,
    'interval',
    hours=1,
    id='cleanup_remarketing_jobs',
    replace_existing=True
)

logger.info("✅ [SCHEDULER] Job de vencimentos agendado (12h)")
logger.info("✅ [SCHEDULER] Job de retry de webhooks agendado (1 min)")
logger.info("✅ [SCHEDULER] Job de cleanup de remarketing agendado (1h)")


# =========================================================
# 🏥 HEALTH CHECK ENDPOINT
# =========================================================
@app.get("/api/health")
async def health_check():
    """
    Health check endpoint para monitoramento externo.
    Retorna status detalhado do sistema.
    """
    try:
        # Verificar conexão com banco de dados
        db_status = "ok"
        try:
            db = SessionLocal()
            db.execute(text("SELECT 1"))
            db.close()
        except Exception as e:
            db_status = f"error: {str(e)}"
        
        # Verificar scheduler
        scheduler_status = "running" if scheduler.running else "stopped"
        
        # Verificar webhooks pendentes
        webhook_stats = {"pending": 0, "failed": 0}
        try:
            db = SessionLocal()
            pending = db.query(WebhookRetry).filter(
                WebhookRetry.status == 'pending'
            ).count()
            failed = db.query(WebhookRetry).filter(
                WebhookRetry.status == 'failed'
            ).count()
            webhook_stats = {"pending": pending, "failed": failed}
            db.close()
        except:
            pass  # Tabela pode não existir ainda
        
        # Determinar status geral
        overall_status = "healthy"
        status_code = 200
        
        if db_status != "ok":
            overall_status = "unhealthy"
            status_code = 503
        elif scheduler_status != "running":
            overall_status = "degraded"
            status_code = 200
        
        health_status = {
            "status": overall_status,
            "timestamp": datetime.now().isoformat(),
            "checks": {
                "database": {"status": db_status},
                "scheduler": {"status": scheduler_status},
                "webhook_retry": webhook_stats
            },
            "version": "5.0"
        }
        
        return JSONResponse(content=health_status, status_code=status_code)
    
    except Exception as e:
        logger.error(f"❌ [HEALTH] Erro no health check: {str(e)}")
        return JSONResponse(
            content={
                "status": "unhealthy",
                "error": str(e),
                "timestamp": datetime.now().isoformat()
            },
            status_code=503
        )


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
    turnstile_token: Optional[str] = None

class UserLogin(BaseModel):
    username: str
    password: str
    turnstile_token: Optional[str] = None

class PlatformUserUpdate(BaseModel):
    full_name: Optional[str] = None
    email: Optional[EmailStr] = None
    pushin_pay_id: Optional[str] = None
    taxa_venda: Optional[int] = None

class Token(BaseModel):
    access_token: str
    token_type: str
    user_id: int
    username: str
    has_bots: bool

class TokenData(BaseModel):
    username: str = None

# =========================================================
# 🛡️ CONFIGURAÇÃO CLOUDFLARE TURNSTILE (BLINDADA)
# =========================================================
TURNSTILE_SECRET_KEY = "0x4AAAAAACOaNBxF24PV-Eem9fAQqzPODn0"

async def verify_turnstile(token: str) -> bool:
    """
    Valida token do Cloudflare Turnstile.
    Timeout de 5 segundos para não travar o registro.
    """
    if not token:
        return False
    
    secret_key = os.getenv("TURNSTILE_SECRET_KEY", "0x4AAAAAACOaNBxF24PV-Eem9fAQqzPODn0")
    
    payload = {
        "secret": secret_key,
        "response": token
    }
    
    try:
        response = await http_client.post(
            "https://challenges.cloudflare.com/turnstile/v0/siteverify",
            data=payload,
            timeout=5.0
        )
        
        if response.status_code == 200:
            result = response.json()
            return result.get("success", False)
        
        return False
        
    except httpx.TimeoutException:
        print("⚠️ Timeout na validação Turnstile")
        return False
    except Exception as e:
        print(f"❌ Erro Turnstile: {str(e)}")
        return False
        
# =========================================================
# 📦 SCHEMAS PYDANTIC PARA SUPER ADMIN (🆕 FASE 3.4)
# =========================================================
class UserStatusUpdate(BaseModel):
    is_active: bool

class UserPromote(BaseModel):
    is_superuser: bool

class UserDetailsResponse(BaseModel):
    id: int
    username: str
    email: str
    full_name: str = None
    is_active: bool
    is_superuser: bool
    created_at: str
    total_bots: int
    total_revenue: float
    total_sales: int

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
        from sqlalchemy.orm import joinedload
        
        # 🔥 EAGER LOADING para carregar bots ANTES de fechar sessão
        user = db.query(User).options(
            joinedload(User.bots)
        ).filter(User.id == user_id).first()
        
        if user is None:
            raise credentials_exception
        
        # 🔥 Forçar o carregamento da relação bots
        _ = user.bots  # Isso garante que está carregado
        
        return user
    finally:
        db.close()

# =========================================================
# 👑 MIDDLEWARE: VERIFICAR SE É SUPER-ADMIN (🆕 FASE 3.4)
# =========================================================
async def get_current_superuser(current_user = Depends(get_current_user)):
    """
    Verifica se o usuário logado é um super-administrador.
    Retorna o usuário se for super-admin, caso contrário levanta HTTPException 403.
    """
    if not current_user.is_superuser:
        raise HTTPException(
            status_code=403,
            detail="Acesso negado: esta funcionalidade requer privilégios de super-administrador"
        )
    
    logger.info(f"👑 Super-admin acessando: {current_user.username}")
    return current_user

# ============================================================
# 🚀 ROTAS DA API - AUTO-REMARKETING
# ============================================================

@app.get("/api/admin/auto-remarketing/{bot_id}")
def get_auto_remarketing_config(
    bot_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """Retorna configuração de remarketing automático"""
    try:
        bot = db.query(BotModel).filter(BotModel.id == bot_id).first()
        if not bot:
            raise HTTPException(status_code=404, detail="Bot não encontrado")
        
        if bot.owner_id != current_user.id and not current_user.is_superuser:
            raise HTTPException(status_code=403, detail="Acesso negado")
        
        config = db.query(RemarketingConfig).filter(
            RemarketingConfig.bot_id == bot_id
        ).first()
        
        if not config:
            return {
                "bot_id": bot_id,
                "is_active": False,
                "message_text": "",
                "media_url": None,
                "media_type": None,
                "delay_minutes": 5,
                "auto_destruct_seconds": 0,
                "promo_values": {}
            }
        
        return {
            "id": config.id,
            "bot_id": config.bot_id,
            "is_active": config.is_active,
            "message_text": config.message_text,
            "media_url": config.media_url,
            "media_type": config.media_type,
            "delay_minutes": config.delay_minutes,
            "auto_destruct_seconds": config.auto_destruct_seconds,
            "promo_values": config.promo_values or {},
            "created_at": config.created_at.isoformat() if config.created_at else None,
            "updated_at": config.updated_at.isoformat() if config.updated_at else None
        }
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"❌ [API] Erro ao buscar config: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/admin/auto-remarketing/{bot_id}")
def save_auto_remarketing_config(
    bot_id: int,
    data: dict,
    request: Request,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """Salva/atualiza configuração de remarketing automático"""
    try:
        bot = db.query(BotModel).filter(BotModel.id == bot_id).first()
        if not bot:
            raise HTTPException(status_code=404, detail="Bot não encontrado")
        
        if bot.owner_id != current_user.id and not current_user.is_superuser:
            raise HTTPException(status_code=403, detail="Acesso negado")
        
        delay_minutes = data.get("delay_minutes", 5)
        if delay_minutes < 1 or delay_minutes > 1440:
            raise HTTPException(status_code=400, detail="delay_minutes entre 1-1440")
        
        config = db.query(RemarketingConfig).filter(
            RemarketingConfig.bot_id == bot_id
        ).first()
        
        if config:
            config.is_active = data.get("is_active", config.is_active)
            config.message_text = data.get("message_text", config.message_text)
            config.media_url = data.get("media_url", config.media_url)
            config.media_type = data.get("media_type", config.media_type)
            config.delay_minutes = delay_minutes
            config.auto_destruct_seconds = data.get("auto_destruct_seconds", config.auto_destruct_seconds)
            config.promo_values = data.get("promo_values", config.promo_values)
            config.updated_at = datetime.now()
        else:
            config = RemarketingConfig(
                bot_id=bot_id,
                is_active=data.get("is_active", False),
                message_text=data.get("message_text", ""),
                media_url=data.get("media_url"),
                media_type=data.get("media_type"),
                delay_minutes=delay_minutes,
                auto_destruct_seconds=data.get("auto_destruct_seconds", 0),
                promo_values=data.get("promo_values", {})
            )
            db.add(config)
        
        db.commit()
        db.refresh(config)
        
        logger.info(f"✅ Config salva - Bot: {bot_id}, User: {current_user.username}")
        
        return {
            "id": config.id,
            "bot_id": config.bot_id,
            "is_active": config.is_active,
            "message_text": config.message_text,
            "media_url": config.media_url,
            "media_type": config.media_type,
            "delay_minutes": config.delay_minutes,
            "auto_destruct_seconds": config.auto_destruct_seconds,
            "promo_values": config.promo_values,
            "updated_at": config.updated_at.isoformat()
        }
        
    except HTTPException:
        raise
    except Exception as e:
        db.rollback()
        logger.error(f"❌ Erro ao salvar: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/admin/auto-remarketing/{bot_id}/messages")
def get_auto_remarketing_messages(
    bot_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """Retorna configuração de mensagens alternantes"""
    try:
        bot = db.query(BotModel).filter(BotModel.id == bot_id).first()
        if not bot:
            raise HTTPException(status_code=404, detail="Bot não encontrado")
        
        if bot.owner_id != current_user.id and not current_user.is_superuser:
            raise HTTPException(status_code=403, detail="Acesso negado")
        
        config = db.query(AlternatingMessages).filter(
            AlternatingMessages.bot_id == bot_id
        ).first()
        
        if not config:
            return {
                "bot_id": bot_id,
                "is_active": False,
                "messages": [],
                "rotation_interval_seconds": 15,
                "stop_before_remarketing_seconds": 60,
                "auto_destruct_final": False
            }
        
        return {
            "id": config.id,
            "bot_id": config.bot_id,
            "is_active": config.is_active,
            "messages": config.messages or [],
            "rotation_interval_seconds": config.rotation_interval_seconds,
            "stop_before_remarketing_seconds": config.stop_before_remarketing_seconds,
            "auto_destruct_final": config.auto_destruct_final,
            "created_at": config.created_at.isoformat() if config.created_at else None,
            "updated_at": config.updated_at.isoformat() if config.updated_at else None
        }
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"❌ Erro: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/admin/auto-remarketing/{bot_id}/messages")
def save_auto_remarketing_messages(
    bot_id: int,
    data: dict,
    request: Request,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """Salva configuração de mensagens alternantes"""
    try:
        bot = db.query(BotModel).filter(BotModel.id == bot_id).first()
        if not bot:
            raise HTTPException(status_code=404, detail="Bot não encontrado")
        
        if bot.owner_id != current_user.id and not current_user.is_superuser:
            raise HTTPException(status_code=403, detail="Acesso negado")
        
        messages = data.get("messages", [])
        if not isinstance(messages, list):
            raise HTTPException(status_code=400, detail="messages deve ser array")
        
        if data.get("is_active") and len(messages) < 2:
            raise HTTPException(status_code=400, detail="Mínimo 2 mensagens")
        
        config = db.query(AlternatingMessages).filter(
            AlternatingMessages.bot_id == bot_id
        ).first()
        
        if config:
            config.is_active = data.get("is_active", config.is_active)
            config.messages = messages
            config.rotation_interval_seconds = data.get("rotation_interval_seconds", config.rotation_interval_seconds)
            config.stop_before_remarketing_seconds = data.get("stop_before_remarketing_seconds", config.stop_before_remarketing_seconds)
            config.auto_destruct_final = data.get("auto_destruct_final", config.auto_destruct_final)
            config.updated_at = datetime.now()
        else:
            config = AlternatingMessages(
                bot_id=bot_id,
                is_active=data.get("is_active", False),
                messages=messages,
                rotation_interval_seconds=data.get("rotation_interval_seconds", 15),
                stop_before_remarketing_seconds=data.get("stop_before_remarketing_seconds", 60),
                auto_destruct_final=data.get("auto_destruct_final", False)
            )
            db.add(config)
        
        db.commit()
        db.refresh(config)
        
        logger.info(f"✅ Mensagens salvas - Bot: {bot_id}")
        
        return {
            "id": config.id,
            "bot_id": config.bot_id,
            "is_active": config.is_active,
            "messages": config.messages,
            "rotation_interval_seconds": config.rotation_interval_seconds,
            "stop_before_remarketing_seconds": config.stop_before_remarketing_seconds,
            "auto_destruct_final": config.auto_destruct_final,
            "updated_at": config.updated_at.isoformat()
        }
        
    except HTTPException:
        raise
    except Exception as e:
        db.rollback()
        logger.error(f"❌ Erro: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/admin/auto-remarketing/{bot_id}/stats")
def get_auto_remarketing_stats(
    bot_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """Retorna estatísticas de remarketing"""
    try:
        bot = db.query(BotModel).filter(BotModel.id == bot_id).first()
        if not bot:
            raise HTTPException(status_code=404, detail="Bot não encontrado")
        
        if bot.owner_id != current_user.id and not current_user.is_superuser:
            raise HTTPException(status_code=403, detail="Acesso negado")
        
        total_sent = db.query(RemarketingSentLog).filter(
            RemarketingSentLog.bot_id == bot_id
        ).count()
        
        total_converted = db.query(RemarketingSentLog).filter(
            RemarketingSentLog.bot_id == bot_id,
            RemarketingSentLog.converted == True
        ).count()
        
        conversion_rate = (total_converted / total_sent * 100) if total_sent > 0 else 0
        
        hoje = datetime.now().date()
        today_sent = db.query(RemarketingSentLog).filter(
            RemarketingSentLog.bot_id == bot_id,
            func.date(RemarketingSentLog.sent_at) == hoje
        ).count()
        
        recent_logs = db.query(RemarketingSentLog).filter(
            RemarketingSentLog.bot_id == bot_id
        ).order_by(RemarketingSentLog.sent_at.desc()).limit(10).all()
        
        recent_data = [
            {
                "id": log.id,
                "user_telegram_id": log.user_telegram_id,
                "sent_at": log.sent_at.isoformat(),
                "status": log.status,
                "converted": log.converted,
                "error_message": log.error_message
            }
            for log in recent_logs
        ]
        
        return {
            "total_sent": total_sent,
            "total_converted": total_converted,
            "conversion_rate": round(conversion_rate, 2),
            "today_sent": today_sent,
            "recent_logs": recent_data
        }
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"❌ Erro: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))

# =========================================================
# 🔒 FUNÇÃO HELPER: VERIFICAR PROPRIEDADE DO BOT
# =========================================================
def verificar_bot_pertence_usuario(bot_id: int, user_id: int, db: Session):
    """
    Verifica se o bot pertence ao usuário.
    Retorna o bot se pertencer, caso contrário levanta HTTPException 404.
    """
    bot = db.query(BotModel).filter(
        BotModel.id == bot_id,
        BotModel.owner_id == user_id
    ).first()
    
    if not bot:
        raise HTTPException(
            status_code=404, 
            detail="Bot não encontrado ou você não tem permissão para acessá-lo"
        )
    
    return bot

# =========================================================
# 🌐 FUNÇÃO HELPER: EXTRAIR IP DO CLIENT (🆕 FASE 3.3)
# =========================================================
def get_client_ip(request: Request) -> str:
    """
    Extrai o IP real do cliente, considerando proxies (Railway, Vercel, etc)
    """
    # Tenta pegar do header X-Forwarded-For (proxies)
    forwarded = request.headers.get("X-Forwarded-For")
    if forwarded:
        # Pega o primeiro IP da lista (cliente real)
        return forwarded.split(",")[0].strip()
    
    # Tenta pegar do header X-Real-IP
    real_ip = request.headers.get("X-Real-IP")
    if real_ip:
        return real_ip
    
    # Fallback: IP direto da conexão
    if request.client:
        return request.client.host
    
    return "unknown"

# =========================================================
# 📋 FUNÇÃO HELPER: REGISTRAR AÇÃO DE AUDITORIA (BLINDADA)
# =========================================================
def log_action(
    db: Session,
    user_id: Optional[int], 
    username: str,
    action: str,
    resource_type: str,
    resource_id: int = None,
    description: str = None,
    details: dict = None,
    success: bool = True,
    error_message: str = None,
    ip_address: str = None,
    user_agent: str = None
):
    """
    Registra uma ação de auditoria.
    BLINDAGEM: Se não tiver user_id, apenas loga no console e ignora o banco
    para evitar erro de NotNullViolation.
    """
    try:
        # 🔥 BLINDAGEM ANTI-CRASH
        # Se não tem usuário logado (ex: erro de login/captcha), 
        # não tenta salvar no banco para não violar a regra NOT NULL.
        if user_id is None:
            logger.warning(f"🚫 Audit (Anônimo/Bloqueado): {action} - {description} (IP: {ip_address})")
            return # <--- PULO DO GATO: Sai da função antes de tentar gravar no banco

        # Converte details para JSON se for dict
        details_json = None
        if details:
            import json
            details_json = json.dumps(details, ensure_ascii=False)
        
        # Cria o registro de auditoria (Só chega aqui se tiver user_id)
        audit_log = AuditLog(
            user_id=user_id,
            username=username,
            action=action,
            resource_type=resource_type,
            resource_id=resource_id,
            description=description,
            details=details_json,
            success=success,
            error_message=error_message,
            ip_address=ip_address,
            user_agent=user_agent
        )
        
        db.add(audit_log)
        db.commit()
        
    except Exception as e:
        logger.error(f"❌ Erro ao criar log de auditoria: {e}")
        # Não propaga o erro para não quebrar a operação principal
        db.rollback()

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


# ============================================================
# FUNÇÃO AUXILIAR: CANCELAR REMARKETING (ADICIONAR LINHA ~1320)
# ============================================================
def cancel_remarketing_for_user(chat_id: int):
    """
    Cancela todos os jobs de remarketing para um usuário específico.
    Usado quando o usuário paga ou bloqueia o bot.
    
    Args:
        chat_id: ID do usuário no Telegram
    """
    try:
        canceled = []
        
        with remarketing_lock:
            # Cancela remarketing
            if chat_id in remarketing_timers:
                remarketing_timers[chat_id].cancel()
                del remarketing_timers[chat_id]
                canceled.append('remarketing')
            
            # Cancela alternating
            if chat_id in alternating_tasks:
                alternating_tasks[chat_id].cancel()
                del alternating_tasks[chat_id]
                canceled.append('alternating')
        
        if canceled:
            logger.info(
                f"🛑 [CANCEL] Jobs cancelados para User {chat_id}: "
                f"{', '.join(canceled)}"
            )
        
    except Exception as e:
        logger.error(f"❌ [CANCEL] Erro ao cancelar jobs: {str(e)}")


# ============================================================
# FUNÇÃO 3: MARCAR COMO PAGO (CORRIGIDA)
# ============================================================
def marcar_como_pago(db: Session, pedido_id: int):
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
    
    # ============================================================
    # 🎯 CANCELAR REMARKETING (USUÁRIO PAGOU)
    # ============================================================
    try:
        chat_id_int = int(pedido.telegram_id) if pedido.telegram_id.isdigit() else hash(pedido.telegram_id) % 1000000000
        cancel_remarketing_for_user(chat_id_int)
        logger.info(f"🛑 [REMARKETING] Jobs cancelados para {pedido.first_name} (pagou)")
    except Exception as e:
        logger.error(f"❌ [REMARKETING] Erro ao cancelar: {e}")
    # ============================================================
    
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

# ============================================================
# ============================================================
# 🌐 ROTAS DA API - REMARKETING
# ============================================================
#
# INSTRUÇÕES DE INSERÇÃO:
# - Localizar no main.py ANTES do final do arquivo
# - Geralmente linha ~7500-7800
# - Cole ANTES das últimas rotas ou antes do if __name__ == "__main__"
#
# ============================================================

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
        bots = db.query(BotModel).all()
        
        for bot_data in bots:
            if not bot_data.token or not bot_data.id_canal_vip: 
                continue
            
            try:
                # Conecta no Telegram deste bot específico
                tb = telebot.TeleBot(bot_data.token)
                
                # Tratamento ROBUSTO do ID do canal
                try: 
                    raw_id = str(bot_data.id_canal_vip).strip()
                    canal_id = int(raw_id)
                except: 
                    logger.error(f"ID do canal inválido para o bot {bot_data.nome}")
                    continue
                
                agora = datetime.utcnow()
                
                # Busca usuários vencidos
                vencidos = db.query(Pedido).filter(
                    Pedido.bot_id == bot_data.id,
                    Pedido.status.in_(['paid', 'approved', 'active']),
                    Pedido.custom_expiration != None, 
                    Pedido.custom_expiration < agora
                ).all()
                
                for u in vencidos:
                    # 🔥 Proteção: Admin nunca é removido
                    # ✅ CORRIGIDO: Comparação segura com None
                    eh_admin_principal = (
                        bot_data.admin_principal_id and 
                        str(u.telegram_id) == str(bot_data.admin_principal_id)
                    )
                    
                    # Verifica na tabela BotAdmin
                    eh_admin_extra = db.query(BotAdmin).filter(
                        BotAdmin.telegram_id == str(u.telegram_id),
                        BotAdmin.bot_id == bot_data.id
                    ).first()
                    
                    if eh_admin_principal or eh_admin_extra:
                        logger.info(f"👑 Ignorando remoção de Admin: {u.telegram_id}")
                        continue
                    
                    try:
                        logger.info(f"💀 Removendo usuário vencido: {u.first_name} (Bot: {bot_data.nome})")
                        
                        # 1. Kick Suave
                        tb.ban_chat_member(canal_id, int(u.telegram_id))
                        tb.unban_chat_member(canal_id, int(u.telegram_id))
                        
                        # 2. Atualiza Status
                        u.status = 'expired'
                        db.commit()
                        
                        # 3. Avisa o usuário
                        try: 
                            tb.send_message(
                                int(u.telegram_id), 
                                "🚫 <b>Seu plano venceu!</b>\n\nPara renovar, digite /start", 
                                parse_mode="HTML"
                            )
                        except: 
                            pass
                        
                    except Exception as e_kick:
                        err_msg = str(e_kick).lower()
                        if "participant_id_invalid" in err_msg or "user not found" in err_msg:
                            logger.info(f"Usuário {u.telegram_id} já havia saído. Marcando expired.")
                            u.status = 'expired'
                            db.commit()
                        else:
                            logger.error(f"Erro ao remover {u.telegram_id}: {e_kick}")
                        
            except Exception as e_bot:
                logger.error(f"Erro ao processar bot {bot_data.id}: {e_bot}")
                
    finally: 
        db.close()

# =========================================================
# 🔄 SISTEMA DE RETRY DE WEBHOOKS
# =========================================================

async def processar_webhooks_pendentes():
    """
    Job que roda a cada 1 minuto para reprocessar webhooks que falharam.
    Implementa exponential backoff: 1min, 2min, 4min, 8min, 16min
    """
    db = SessionLocal()
    try:
        now = datetime.utcnow()
        
        # Buscar webhooks pendentes que estão prontos para retry
        pendentes = db.query(WebhookRetry).filter(
            WebhookRetry.status == 'pending',
            WebhookRetry.next_retry <= now,
            WebhookRetry.attempts < WebhookRetry.max_attempts
        ).all()
        
        if not pendentes:
            logger.debug("🔄 Nenhum webhook pendente para retry")
            return
        
        logger.info(f"🔄 Processando {len(pendentes)} webhooks pendentes")
        
        for retry_item in pendentes:
            try:
                logger.info(f"🔄 Tentativa {retry_item.attempts + 1}/{retry_item.max_attempts} para webhook {retry_item.id}")
                
                # Deserializar payload
                payload = json.loads(retry_item.payload)
                
                # Reprocessar baseado no tipo
                if retry_item.webhook_type == 'pushinpay':
                    # Criar request fake para passar para a função
                    class FakeRequest:
                        async def body(self):
                            return retry_item.payload.encode('utf-8')
                        
                        async def json(self):
                            return payload
                    
                    fake_req = FakeRequest()
                    
                    # Chamar função de webhook
                    await webhook_pix(fake_req, db)
                    
                    # Se chegou aqui, sucesso!
                    retry_item.status = 'success'
                    retry_item.updated_at = datetime.utcnow()
                    db.commit()
                    
                    logger.info(f"✅ Webhook {retry_item.id} reprocessado com sucesso")
                    
                else:
                    logger.warning(f"⚠️ Tipo de webhook desconhecido: {retry_item.webhook_type}")
                    retry_item.status = 'failed'
                    retry_item.last_error = "Tipo de webhook não suportado"
                    db.commit()
                
            except Exception as e:
                # Incrementar tentativas
                retry_item.attempts += 1
                retry_item.last_error = str(e)
                retry_item.updated_at = datetime.utcnow()
                
                if retry_item.attempts >= retry_item.max_attempts:
                    # Esgotou tentativas
                    retry_item.status = 'failed'
                    logger.error(f"❌ Webhook {retry_item.id} falhou após {retry_item.attempts} tentativas: {e}")
                    
                    # CRÍTICO: Alertar equipe sobre falha definitiva
                    await alertar_falha_webhook_critica(retry_item, db)
                else:
                    # Agendar próximo retry com backoff exponencial
                    backoff_minutes = 2 ** retry_item.attempts  # 1, 2, 4, 8, 16 minutos
                    retry_item.next_retry = now + timedelta(minutes=backoff_minutes)
                    logger.warning(f"⚠️ Webhook {retry_item.id} falhou (tentativa {retry_item.attempts}). Próximo retry em {backoff_minutes}min")
                
                db.commit()
        
    except Exception as e:
        logger.error(f"❌ Erro no processador de webhooks pendentes: {e}")
    finally:
        db.close()


async def alertar_falha_webhook_critica(retry_item: WebhookRetry, db: Session):
    """
    Alerta sobre webhooks que falharam definitivamente.
    Envia notificação para admin via Telegram e registra no banco.
    """
    try:
        # Extrair informações do payload
        payload = json.loads(retry_item.payload)
        
        # Buscar pedido relacionado (se houver)
        pedido_id = retry_item.reference_id
        pedido_info = "Desconhecido"
        
        if pedido_id:
            pedido = db.query(Pedido).filter(Pedido.transaction_id == pedido_id).first()
            if pedido:
                pedido_info = f"{pedido.first_name} - R$ {pedido.valor:.2f}"
        
        # Mensagem de alerta
        alerta = (
            f"🚨 <b>WEBHOOK FALHOU DEFINITIVAMENTE</b>\n\n"
            f"📋 ID: {retry_item.id}\n"
            f"🔄 Tentativas: {retry_item.attempts}\n"
            f"📦 Pedido: {pedido_info}\n"
            f"❌ Último erro: {retry_item.last_error[:200]}\n\n"
            f"⚠️ <b>AÇÃO NECESSÁRIA:</b> Processar manualmente"
        )
        
        # Enviar para todos os Super Admins
        super_admins = db.query(User).filter(User.is_superuser == True).all()
        
        for admin in super_admins:
            if admin.telegram_id:
                try:
                    # Buscar bot principal (primeiro ativo)
                    bot = db.query(BotModel).filter(BotModel.status == 'ativo').first()
                    if bot:
                        tb = telebot.TeleBot(bot.token)
                        tb.send_message(int(admin.telegram_id), alerta, parse_mode="HTML")
                except Exception as e:
                    logger.error(f"Erro ao enviar alerta para admin {admin.id}: {e}")
        
        logger.info(f"📢 Alerta de webhook crítico enviado para {len(super_admins)} admins")
        
    except Exception as e:
        logger.error(f"❌ Erro ao alertar sobre falha de webhook: {e}")


def registrar_webhook_para_retry(
    webhook_type: str, 
    payload: dict, 
    reference_id: str = None
):
    """
    Registra um webhook para retry futuro.
    Chamado quando o processamento inicial falha.
    """
    db = SessionLocal()
    try:
        # Calcular primeiro retry (1 minuto no futuro)
        first_retry = datetime.utcnow() + timedelta(minutes=1)
        
        # Criar registro de retry
        retry_item = WebhookRetry(
            webhook_type=webhook_type,
            payload=json.dumps(payload),
            attempts=0,
            max_attempts=5,
            next_retry=first_retry,
            status='pending',
            reference_id=reference_id
        )
        
        db.add(retry_item)
        db.commit()
        db.refresh(retry_item)
        
        logger.info(f"📝 Webhook registrado para retry: ID {retry_item.id}, tipo {webhook_type}")
        return retry_item.id
        
    except Exception as e:
        logger.error(f"❌ Erro ao registrar webhook para retry: {e}")
        return None
    finally:
        db.close()

# =========================================================
# 🔄 PROCESSAMENTO BACKGROUND DE REMARKETING
# =========================================================

def processar_envio_massivo_background(
    campaign_id: int, 
    bot_id: int, 
    mensagem: str, 
    target: str,
    media_url: str = None,
    plano_oferta_id: int = None
):
    """
    Processa envio de remarketing em background.
    Roda em thread separada via BackgroundTasks do FastAPI.
    
    IMPORTANTE: Não pode usar a sessão do DB da requisição HTTP!
    Precisa criar nova sessão aqui.
    """
    # 1. CRIAR NOVA SESSÃO (threads não compartilham conexões)
    db = SessionLocal()
    
    try:
        logger.info(f"🚀 Iniciando envio background da campanha {campaign_id}")
        
        # 2. BUSCAR DADOS DO BOT
        bot_data = db.query(BotModel).filter(BotModel.id == bot_id).first()
        if not bot_data:
            logger.error(f"❌ Bot {bot_id} não encontrado")
            return
        
        bot = telebot.TeleBot(bot_data.token, threaded=False)
        
        # 3. FILTRAR LEADS COM BASE NO TARGET
        leads_query = db.query(Lead).filter(Lead.bot_id == bot_id)
        
        if target == 'todos':
            leads = leads_query.all()
        elif target == 'compradores':
            # Buscar IDs de quem já comprou
            compradores_ids = db.query(Pedido.telegram_id).filter(
                Pedido.bot_id == bot_id,
                Pedido.status.in_(['paid', 'approved'])
            ).distinct().all()
            ids_set = {str(c[0]) for c in compradores_ids}
            leads = [l for l in leads_query.all() if l.user_id in ids_set]
        elif target == 'nao_compradores':
            # Leads que nunca compraram
            compradores_ids = db.query(Pedido.telegram_id).filter(
                Pedido.bot_id == bot_id,
                Pedido.status.in_(['paid', 'approved'])
            ).distinct().all()
            ids_set = {str(c[0]) for c in compradores_ids}
            leads = [l for l in leads_query.all() if l.user_id not in ids_set]
        else:
            leads = leads_query.filter(Lead.status == target).all()
        
        total_leads = len(leads)
        logger.info(f"📊 Total de leads a enviar: {total_leads}")
        
        # 4. ATUALIZAR CAMPANHA COM TOTAL
        campanha = db.query(RemarketingCampaign).filter(
            RemarketingCampaign.id == campaign_id
        ).first()
        
        if campanha:
            campanha.total_leads = total_leads
            campanha.status = 'enviando'
            db.commit()
        
        # 5. MONTAR MENSAGEM COM BOTÃO (SE TIVER OFERTA)
        markup = None
        if plano_oferta_id:
            plano = db.query(PlanoConfig).filter(PlanoConfig.id == plano_oferta_id).first()
            if plano:
                markup = types.InlineKeyboardMarkup()
                btn_text = f"🔥 {plano.nome_exibicao} - R$ {plano.preco_atual:.2f}"
                markup.add(types.InlineKeyboardButton(
                    btn_text, 
                    callback_data=f"promo_{campanha.campaign_id}"
                ))
        
        # 6. ENVIO EM LOOP COM RATE LIMITING
        enviados = 0
        erros = 0
        
        for i, lead in enumerate(leads):
            try:
                # Tentar converter ID para inteiro
                try:
                    target_id = int(lead.user_id)
                except (ValueError, TypeError):
                    logger.warning(f"⚠️ ID inválido: {lead.user_id}")
                    erros += 1
                    continue
                
                # ENVIAR MENSAGEM
                if media_url:
                    # Detectar tipo de mídia
                    if media_url.lower().endswith(('.mp4', '.mov', '.avi')):
                        bot.send_video(
                            target_id, 
                            media_url, 
                            caption=mensagem, 
                            reply_markup=markup, 
                            parse_mode="HTML"
                        )
                    else:
                        bot.send_photo(
                            target_id, 
                            media_url, 
                            caption=mensagem, 
                            reply_markup=markup, 
                            parse_mode="HTML"
                        )
                else:
                    bot.send_message(
                        target_id, 
                        mensagem, 
                        reply_markup=markup, 
                        parse_mode="HTML"
                    )
                
                enviados += 1
                
                # RATE LIMITING: 28 msgs/segundo (margem de segurança)
                time.sleep(0.036)
                
                # LOG DE PROGRESSO A CADA 50 ENVIOS
                if (i + 1) % 50 == 0:
                    logger.info(f"📤 Progresso: {i+1}/{total_leads} ({(i+1)/total_leads*100:.1f}%)")
                
            except telebot.apihelper.ApiTelegramException as e:
                # Usuário bloqueou o bot ou ID inválido
                erros += 1
                logger.warning(f"⚠️ Telegram API error para {lead.user_id}: {e}")
            except Exception as e:
                erros += 1
                logger.error(f"❌ Erro ao enviar para {lead.user_id}: {e}")
        
        # 7. ATUALIZAR CAMPANHA COM RESULTADO FINAL
        if campanha:
            campanha.status = 'concluido'
            campanha.sent_success = enviados
            campanha.blocked_count = erros
            db.commit()
        
        logger.info(f"✅ Campanha {campaign_id} concluída: {enviados} enviados, {erros} erros")
        
    except Exception as e:
        logger.error(f"❌ ERRO CRÍTICO no processamento background: {e}")
        
        # Marcar campanha como erro
        try:
            campanha = db.query(RemarketingCampaign).filter(
                RemarketingCampaign.id == campaign_id
            ).first()
            if campanha:
                campanha.status = 'erro'
                db.commit()
        except:
            pass
    
    finally:
        # 8. SEMPRE FECHAR A SESSÃO
        db.close()
        logger.info(f"🔒 Sessão do DB fechada para campanha {campaign_id}")

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
# 🏢 BUSCAR PUSHIN PAY ID DA PLATAFORMA (ZENYX)
# =========================================================
def get_plataforma_pushin_id(db: Session) -> str:
    """
    Retorna o pushin_pay_id da plataforma Zenyx para receber as taxas.
    Prioridade:
    1. SystemConfig (pushin_plataforma_id)
    2. Primeiro Super Admin encontrado
    3. None se não encontrar
    """
    try:
        # 1. Tenta buscar da SystemConfig
        config = db.query(SystemConfig).filter(
            SystemConfig.key == "pushin_plataforma_id"  # ✅ CORRIGIDO: key ao invés de chave
        ).first()
        
        if config and config.value:  # ✅ CORRIGIDO: value ao invés de valor
            return config.value
        
        # 2. Busca o primeiro Super Admin com pushin_pay_id configurado
        from database import User
        super_admin = db.query(User).filter(
            User.is_superuser == True,
            User.pushin_pay_id.isnot(None)
        ).first()
        
        if super_admin and super_admin.pushin_pay_id:
            return super_admin.pushin_pay_id
        
        logger.warning("⚠️ Nenhum pushin_pay_id da plataforma configurado! Split desabilitado.")
        return None
        
    except Exception as e:
        logger.error(f"Erro ao buscar pushin_pay_id da plataforma: {e}")
        return None
# =========================================================
# 🔌 INTEGRAÇÃO PUSHIN PAY (CORRIGIDA)
# =========================================================
# =========================================================
# 🔌 INTEGRAÇÃO PUSHIN PAY (CORRIGIDA COM REMARKETING)
# =========================================================
async def gerar_pix_pushinpay(
    valor_float: float, 
    transaction_id: str, 
    bot_id: int, 
    db: Session,
    user_telegram_id: str = None,      # ← NOVO PARÂMETRO
    user_first_name: str = None,       # ← NOVO PARÂMETRO
    plano_nome: str = None            # ← NOVO PARÂMETRO
):
    """
    Gera PIX com Split automático de taxa para a plataforma.
    
    Args:
        valor_float: Valor do PIX em reais (ex: 100.50)
        transaction_id: ID único da transação
        bot_id: ID do bot que está gerando o PIX
        db: Sessão do banco de dados
        user_telegram_id: ID do usuário no Telegram (para remarketing)
        user_first_name: Nome do usuário (para remarketing)
        plano_nome: Nome do plano escolhido (para remarketing)
    
    Returns:
        dict: Resposta da API Pushin Pay ou None em caso de erro
    """
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
    
    # URL do Webhook
    seus_dominio = "zenyx-gbs-testesv1-production.up.railway.app" 
    
    # Valor em centavos
    valor_centavos = int(valor_float * 100)
    
    # Monta payload básico
    payload = {
        "value": valor_centavos, 
        "webhook_url": f"https://{seus_dominio}/webhook/pix",
        "external_reference": transaction_id
    }
    
    # ========================================
    # 💰 LÓGICA DE SPLIT (TAXA DA PLATAFORMA)
    # ========================================
    try:
        # 1. Busca o bot
        bot = db.query(BotModel).filter(BotModel.id == bot_id).first()
        
        if bot and bot.owner_id:
            # 2. Busca o dono do bot (membro)
            from database import User
            owner = db.query(User).filter(User.id == bot.owner_id).first()
            
            if owner:
                # 3. Busca o pushin_pay_id da PLATAFORMA (para receber a taxa)
                plataforma_id = get_plataforma_pushin_id(db)
                
                if plataforma_id:
                    # 4. Define a taxa (padrão: R$ 0,60)
                    taxa_centavos = owner.taxa_venda or 60
                    
                    # 5. Validação: Taxa não pode ser maior que o valor total
                    if taxa_centavos >= valor_centavos:
                        logger.warning(f"⚠️ Taxa ({taxa_centavos}) >= Valor Total ({valor_centavos}). Split ignorado.")
                    else:
                        # 6. Monta o split_rules
                        payload["split_rules"] = [
                            {
                                "value": taxa_centavos,
                                "account_id": plataforma_id
                            }
                        ]
                        
                        logger.info(f"💸 Split configurado: Taxa R$ {taxa_centavos/100:.2f} → Conta {plataforma_id[:8]}...")
                        logger.info(f"   Membro receberá: R$ {(valor_centavos - taxa_centavos)/100:.2f}")
                else:
                    logger.warning("⚠️ Pushin Pay ID da plataforma não configurado. Gerando PIX SEM split.")
            else:
                logger.warning(f"⚠️ Owner do bot {bot_id} não encontrado. Gerando PIX SEM split.")
        else:
            logger.warning(f"⚠️ Bot {bot_id} sem owner_id. Gerando PIX SEM split.")
            
    except Exception as e:
        logger.error(f"❌ Erro ao configurar split: {e}. Gerando PIX SEM split.")
        # Continua sem split em caso de erro
    
    # ========================================
    # 📤 ENVIA REQUISIÇÃO PARA PUSHIN PAY (HTTPX ASYNC)
    # ========================================
    try:
        logger.info(f"📤 Gerando PIX de R$ {valor_float:.2f}. Webhook: https://{seus_dominio}/webhook/pix")
        
        # ✅ MIGRAÇÃO: requests → httpx
        response = await http_client.post(url, json=payload, headers=headers, timeout=10)
        
        if response.status_code in [200, 201]:
            pix_response = response.json()
            logger.info(f"✅ PIX gerado com sucesso! ID: {pix_response.get('id')}")
            
            # ============================================================
            # 🎯 INTEGRAÇÃO: AGENDAR REMARKETING
            # ============================================================
            if user_telegram_id and user_first_name:
                try:
                    # Converte telegram_id para int (necessário para remarketing)
                    chat_id_int = int(user_telegram_id) if user_telegram_id.isdigit() else hash(user_telegram_id) % 1000000000
                    
                    # Agenda remarketing + mensagens alternantes
                    schedule_remarketing_and_alternating(
                        bot_id=bot_id,
                        chat_id=chat_id_int,
                        payment_message_id=0,  # ID da mensagem PIX (0 = não disponível)
                        user_info={
                            'first_name': user_first_name,
                            'plano': plano_nome or 'VIP',
                            'valor': valor_float
                        }
                    )
                    
                    logger.info(
                        f"📧 [REMARKETING] Agendado para {user_first_name} "
                        f"(Bot: {bot_id}, Chat: {chat_id_int})"
                    )
                    
                except Exception as e:
                    logger.error(f"❌ [REMARKETING] Erro ao agendar: {e}")
                    # Não falha o PIX se remarketing der erro
            # ============================================================
            
            return pix_response
        else:
            logger.error(f"❌ Erro PushinPay: {response.text}")
            return None
            
    except httpx.TimeoutException:
        logger.error("❌ Timeout ao conectar com PushinPay (10s)")
        return None
    except httpx.HTTPError as e:
        logger.error(f"❌ Erro HTTP ao chamar PushinPay: {e}")
        return None
    except Exception as e:
        logger.error(f"❌ Exceção ao gerar PIX: {e}")
        return None

# --- HELPER: Notificar Admin Principal ---
# --- HELPER: Notificar TODOS os Admins (Principal + Extras) ---
# --- HELPER: Notificar TODOS os Admins (Principal + Extras) ---
def notificar_admin_principal(bot_db: BotModel, mensagem: str):
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
    bot = db.query(BotModel).filter(BotModel.id == bot_id).first()
    
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
    bot = db.query(BotModel).filter(BotModel.id == bot_id).first()
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
                bot = db.query(BotModel).filter(BotModel.id == bot_id).first()
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

# =========================================================
# 💰 ROTA DE PAGAMENTO (PIX) - CRÍTICO PARA O MINI APP
# =========================================================
# =========================================================
# 1. GERAÇÃO DE PIX (COM SPLIT E WEBHOOK CORRIGIDO)
# =========================================================
# =========================================================
# 📦 1. MODELO DE DADOS (MANTENHA AQUI NO TOPO)
# =========================================================
class PixCreateRequest(BaseModel):
    bot_id: int
    valor: float
    first_name: str
    telegram_id: str
    username: Optional[str] = None
    plano_id: Optional[int] = None
    plano_nome: Optional[str] = None
    tem_order_bump: Optional[bool] = False

# =========================================================
# 💰 2. GERAÇÃO DE PIX (COM SPLIT FORÇADO SEMPRE)
# =========================================================
@app.post("/api/pagamento/pix")
async def gerar_pix(data: PixCreateRequest, db: Session = Depends(get_db)):
    try:
        logger.info(f"💰 Iniciando pagamento: {data.first_name} (R$ {data.valor})")
        
        # 1. Buscar o Bot
        bot_atual = db.query(BotModel).filter(BotModel.id == data.bot_id).first()
        if not bot_atual:
            raise HTTPException(status_code=404, detail="Bot não encontrado")

        # 2. Definir Token e ID da Plataforma
        PLATAFORMA_ID = "9D4FA0F6-5B3A-4A36-ABA3-E55ACDF5794E"
        
        config_sys = db.query(SystemConfig).filter(SystemConfig.key == "pushin_pay_token").first()
        token_plataforma = config_sys.value if (config_sys and config_sys.value) else os.getenv("PUSHIN_PAY_TOKEN")

        pushin_token = bot_atual.pushin_token 
        if not pushin_token:
            pushin_token = token_plataforma

        # Tratamento de ID
        user_clean = str(data.username).strip().lower().replace("@", "") if data.username else "anonimo"
        tid_clean = str(data.telegram_id).strip()
        if not tid_clean.isdigit(): 
            tid_clean = user_clean

        # Modo Teste
        if not pushin_token:
            fake_txid = str(uuid.uuid4())
            novo_pedido = Pedido(
                bot_id=data.bot_id, telegram_id=tid_clean, first_name=data.first_name, username=user_clean,   
                valor=data.valor, status='pending', plano_id=data.plano_id, plano_nome=data.plano_nome,
                txid=fake_txid, qr_code="pix-fake-copia-cola", transaction_id=fake_txid, tem_order_bump=data.tem_order_bump
            )
            db.add(novo_pedido)
            db.commit()
            db.refresh(novo_pedido)
            
            # ============================================================
            # 🎯 INTEGRAÇÃO: AGENDAR REMARKETING (MODO TESTE)
            # ============================================================
            try:
                # Converte telegram_id para int (necessário para o sistema de remarketing)
                chat_id_int = int(tid_clean) if tid_clean.isdigit() else hash(tid_clean) % 1000000000
                
                # Agenda remarketing + mensagens alternantes
                schedule_remarketing_and_alternating(
                    bot_id=data.bot_id,
                    chat_id=chat_id_int,
                    payment_message_id=0,  # Modo teste - não tem mensagem real
                    user_info={
                        'first_name': data.first_name,
                        'plano': data.plano_nome,
                        'valor': data.valor
                    }
                )
                logger.info(f"📧 [REMARKETING] Agendado para {data.first_name} (teste)")
            except Exception as e:
                logger.error(f"❌ [REMARKETING] Erro ao agendar (teste): {e}")
            # ============================================================
            
            return {"txid": fake_txid, "copia_cola": "pix-fake", "qr_code": "https://fake.com/qr.png"}

        # 3. Payload Básico
        valor_total_centavos = int(data.valor * 100)
        
        raw_domain = os.getenv("RAILWAY_PUBLIC_DOMAIN", "zenyx-gbs-testesv1-production.up.railway.app")
        clean_domain = raw_domain.replace("https://", "").replace("http://", "").strip("/")
        webhook_url_final = f"https://{clean_domain}/api/webhooks/pushinpay"
        
        payload = {
            "value": valor_total_centavos,
            "webhook_url": webhook_url_final,
            "external_reference": f"bot_{data.bot_id}_{user_clean}_{int(time.time())}"
        }

        # ======================================================================
        # 💸 LÓGICA DE SPLIT (SINTAXE CORRIGIDA)
        # ======================================================================
        membro_dono = None
        if bot_atual.owner_id:
            membro_dono = db.query(User).filter(User.id == bot_atual.owner_id).first()

        taxa_centavos = 60 
        if membro_dono and hasattr(membro_dono, 'taxa_venda') and membro_dono.taxa_venda:
            taxa_centavos = int(membro_dono.taxa_venda)

        # Regra: Taxa muito alta (>50%)
        if taxa_centavos >= (valor_total_centavos * 0.5):
            logger.warning(f"⚠️ Taxa muito alta. Split cancelado.")
        else:
            payload["split_rules"] = [
                {
                    "value": taxa_centavos,
                    "account_id": PLATAFORMA_ID,
                    "charge_processing_fee": False
                }
            ]
            logger.info(f"✅ SPLIT (split_rules): Admin R$ {taxa_centavos/100:.2f} -> Conta {PLATAFORMA_ID}")

        # ======================================================================
        # 4. ENVIA (HTTPX ASYNC)
        # ======================================================================
        url = "https://api.pushinpay.com.br/api/pix/cashIn"
        headers = { 
            "Authorization": f"Bearer {pushin_token}", 
            "Content-Type": "application/json", 
            "Accept": "application/json" 
        }
        
        req = await http_client.post(url, json=payload, headers=headers, timeout=15)
        
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
            db.refresh(novo_pedido)
            
            # ============================================================
            # 🎯 INTEGRAÇÃO: AGENDAR REMARKETING (PRODUÇÃO)
            # ============================================================
            try:
                # Converte telegram_id para int (necessário para o sistema de remarketing)
                chat_id_int = int(tid_clean) if tid_clean.isdigit() else hash(tid_clean) % 1000000000
                
                # ⚠️ IMPORTANTE: payment_message_id deve ser o ID da mensagem do Telegram
                # que contém o QR Code PIX. Se você não tem esse ID aqui, pode:
                # 1. Passar 0 (e o sistema de alternating não funcionará)
                # 2. Capturar esse ID ao enviar a mensagem no bot do Telegram
                
                # Agenda remarketing + mensagens alternantes
                schedule_remarketing_and_alternating(
                    bot_id=data.bot_id,
                    chat_id=chat_id_int,
                    payment_message_id=0,  # ⚠️ AJUSTAR: ID da mensagem do Telegram com QR Code
                    user_info={
                        'first_name': data.first_name,
                        'plano': data.plano_nome,
                        'valor': data.valor
                    }
                )
                
                logger.info(
                    f"📧 [REMARKETING] Agendado para {data.first_name} "
                    f"(Bot: {data.bot_id}, Chat: {chat_id_int})"
                )
                
            except Exception as e:
                # Não falha a transação se o agendamento falhar
                logger.error(f"❌ [REMARKETING] Erro ao agendar: {e}")
                # Sistema continua - PIX foi gerado com sucesso
            # ============================================================
            
            return {"txid": txid, "copia_cola": copia_cola, "qr_code": qr_image}
        else:
            logger.error(f"❌ Erro PushinPay: {req.text}")
            try: 
                detalhe = req.json().get('message', req.text)
            except: 
                detalhe = req.text
            raise HTTPException(status_code=400, detail=f"Erro Gateway: {detalhe}")

    except httpx.HTTPError as e:
        logger.error(f"❌ Erro HTTP PushinPay: {e}")
        raise HTTPException(status_code=503, detail="Gateway de pagamento indisponível")
    except Exception as e:
        logger.error(f"❌ Erro fatal PIX: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/api/pagamento/status/{txid}")
def check_status(txid: str, db: Session = Depends(get_db)):
    pedido = db.query(Pedido).filter((Pedido.txid == txid) | (Pedido.transaction_id == txid)).first()
    if not pedido: return {"status": "not_found"}
    return {"status": pedido.status}

# =========================================================
# 🔔 SISTEMA DE NOTIFICAÇÕES (HELPER)
# =========================================================
def create_notification(db: Session, user_id: int, title: str, message: str, type: str = "info"):
    """
    Cria uma notificação real para o usuário no painel.
    Types: info (azul), success (verde), warning (amarelo), error (vermelho)
    """
    try:
        notif = Notification(
            user_id=user_id,
            title=title,
            message=message,
            type=type
        )
        db.add(notif)
        db.commit()
    except Exception as e:
        logger.error(f"Erro ao criar notificação: {e}")

# =========================================================
# 🔐 ROTAS DE AUTENTICAÇÃO (ATUALIZADAS COM AUDITORIA 🆕)
# =========================================================
@app.post("/api/auth/register", response_model=Token)
async def register(user_data: UserCreate, request: Request, db: Session = Depends(get_db)):  # ✅ ASYNC
    """
    Registra um novo usuário no sistema (COM PROTEÇÃO TURNSTILE)
    """
    from database import User 

    # 1. 🛡️ VERIFICAÇÃO HUMANIDADE (TURNSTILE)
    # Comentado para evitar erro no auto-login (token queimado)
    # if not await verify_turnstile(user_data.turnstile_token):  # ✅ AWAIT
    #      log_action(db=db, user_id=None, username=user_data.username, action="login_bot_blocked", resource_type="auth", 
    #                description="Login bloqueado: Falha na verificação humana", success=False, ip_address=get_client_ip(request))
    #      raise HTTPException(status_code=400, detail="Erro de verificação humana (Captcha). Tente recarregar a página.")

    # Validações normais
    existing_user = db.query(User).filter(User.username == user_data.username).first()
    if existing_user:
        raise HTTPException(status_code=400, detail="Username já existe")
    
    existing_email = db.query(User).filter(User.email == user_data.email).first()
    if existing_email:
        raise HTTPException(status_code=400, detail="Email já cadastrado")
    
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
    
    # 📋 AUDITORIA
    log_action(db=db, user_id=new_user.id, username=new_user.username, action="user_registered", resource_type="auth", 
               resource_id=new_user.id, description=f"Novo usuário registrado: {new_user.username}", 
               details={"email": new_user.email}, ip_address=get_client_ip(request), user_agent=request.headers.get("user-agent"))
    
    # Gera token JWT
    access_token_expires = timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES)
    access_token = create_access_token(
        data={"sub": new_user.username, "user_id": new_user.id},
        expires_delta=access_token_expires
    )
    
    return {
        "access_token": access_token,
        "token_type": "bearer",
        "user_id": new_user.id,
        "username": new_user.username,
        "has_bots": False
    }
    
@app.post("/api/auth/login", response_model=Token)
async def login(user_data: UserLogin, request: Request, db: Session = Depends(get_db)):  # ✅ ASYNC
    from database import User
    
    logger.info(f"🔑 Tentativa de login: {user_data.username}")

    # VERIFICAÇÃO TURNSTILE
    if not await verify_turnstile(user_data.turnstile_token):  # ✅ AWAIT
         log_action(db=db, user_id=None, username=user_data.username, action="login_bot_blocked", resource_type="auth", 
                   description="Login bloqueado: Falha na verificação humana", success=False, ip_address=get_client_ip(request))
         raise HTTPException(status_code=400, detail="Erro de verificação humana (Captcha). Tente recarregar a página.")

    user = db.query(User).filter(User.username == user_data.username).first()
    
    if not user or not verify_password(user_data.password, user.password_hash):
        raise HTTPException(status_code=401, detail="Usuário ou senha incorretos")
    
    has_bots = len(user.bots) > 0

    log_action(db=db, user_id=user.id, username=user.username, action="login_success", resource_type="auth", 
               description="Login realizado", ip_address=get_client_ip(request), user_agent=request.headers.get("user-agent"))
    
    access_token_expires = timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES)
    access_token = create_access_token(
        data={"sub": user.username, "user_id": user.id},
        expires_delta=access_token_expires
    )
    
    return {
        "access_token": access_token,
        "token_type": "bearer",
        "user_id": user.id,
        "username": user.username,
        "has_bots": has_bots
    }

# =========================================================
# 💓 HEALTH CHECK PARA MONITORAMENTO
# =========================================================
@app.get("/api/health")
async def health_check():
    """
    Health check endpoint para monitoramento externo.
    Retorna status detalhado do sistema.
    """
    try:
        # Verificar conexão com banco de dados
        db_status = "ok"
        try:
            await database.execute("SELECT 1")
        except Exception as e:
            db_status = f"error: {str(e)}"
        
        # Verificar scheduler
        scheduler_status = "running" if scheduler.running else "stopped"
        
        # Verificar webhooks pendentes
        webhook_stats = {"pending": 0, "failed": 0}
        try:
            webhook_query = """
                SELECT 
                    status,
                    COUNT(*) as count
                FROM webhook_retry
                WHERE status IN ('pending', 'failed')
                GROUP BY status
            """
            webhook_result = await database.fetch_all(webhook_query)
            webhook_stats = {row['status']: row['count'] for row in webhook_result}
        except:
            pass  # Tabela pode não existir ainda
        
        # Determinar status geral
        overall_status = "healthy"
        status_code = 200
        
        if db_status != "ok":
            overall_status = "unhealthy"
            status_code = 503
        elif scheduler_status != "running":
            overall_status = "degraded"
            status_code = 200  # Ainda considerado "up"
        
        health_status = {
            "status": overall_status,
            "timestamp": datetime.now().isoformat(),
            "checks": {
                "database": {"status": db_status},
                "scheduler": {"status": scheduler_status},
                "webhook_retry": webhook_stats
            },
            "version": "5.0"
        }
        
        return JSONResponse(content=health_status, status_code=status_code)
    
    except Exception as e:
        logger.error(f"❌ [HEALTH] Erro no health check: {str(e)}")
        return JSONResponse(
            content={
                "status": "unhealthy",
                "error": str(e),
                "timestamp": datetime.now().isoformat()
            },
            status_code=503
        )


@app.get("/api/health/simple")
async def health_check_simple():
    """
    Versão simplificada do health check (mais rápida).
    Apenas retorna 200 se o servidor está vivo.
    """
    return {"status": "ok", "timestamp": datetime.utcnow().isoformat()}

@app.get("/api/auth/me")
async def get_current_user_info(current_user = Depends(get_current_user)):
    """
    Retorna informações do usuário logado e status de bots para o Onboarding
    """
    return {
        "id": current_user.id,
        "username": current_user.username,
        "email": current_user.email,
        "full_name": current_user.full_name,
        "is_superuser": current_user.is_superuser, 
        "is_active": current_user.is_active,
        "has_bots": len(current_user.bots) > 0 # 🔥 Crucial para destravar o Sidebar
    }
    
# 👇 COLE ISSO LOGO APÓS A FUNÇÃO get_current_user_info TERMINAR

# 🆕 ROTA PARA O MEMBRO ATUALIZAR SEU PRÓPRIO PERFIL FINANCEIRO
# 🆕 ROTA PARA O MEMBRO ATUALIZAR SEU PRÓPRIO PERFIL FINANCEIRO
@app.put("/api/auth/profile")
def update_own_profile(
    user_data: PlatformUserUpdate, 
    current_user = Depends(get_current_user), 
    db: Session = Depends(get_db)
):
    # 👇 A CORREÇÃO MÁGICA ESTÁ AQUI:
    from database import User 

    user = db.query(User).filter(User.id == current_user.id).first()
    
    if user_data.full_name:
        user.full_name = user_data.full_name
    if user_data.email:
        user.email = user_data.email
    # O membro só pode atualizar o ID de recebimento, não a taxa!
    if user_data.pushin_pay_id is not None:
        user.pushin_pay_id = user_data.pushin_pay_id
        
    db.commit()
    db.refresh(user)
    return user

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

# =========================================================
# 🤖 ROTAS DE BOTS (ATUALIZADAS COM AUDITORIA 🆕)
# =========================================================

@app.post("/api/admin/bots")
def criar_bot(
    bot_data: BotCreate,
    request: Request,
    db: Session = Depends(get_db),
    current_user = Depends(get_current_user)
):
    """
    Cria um novo bot. 
    🔥 BLINDAGEM: Se der duplo clique ou recarregar, recupera o bot existente 
    e devolve o ID para o Frontend continuar o fluxo (Step 1 -> Step 2).
    """
    
    # 1. VERIFICAÇÃO PREVENTIVA (Evita explosão de erro 500 no banco)
    bot_existente = db.query(BotModel).filter(BotModel.token == bot_data.token).first()
    if bot_existente:
        if bot_existente.owner_id == current_user.id:
            logger.info(f"🔄 Recuperando bot ID {bot_existente.id} para destravar fluxo.")
            return {"id": bot_existente.id, "nome": bot_existente.nome, "status": "recuperado", "has_bots": True}
        else:
            raise HTTPException(status_code=409, detail="Este token de bot já está sendo usado por outro usuário.")

    # 2. PREPARA O OBJETO BOT
    novo_bot = BotModel(
        nome=bot_data.nome,
        token=bot_data.token,
        id_canal_vip=bot_data.id_canal_vip,
        admin_principal_id=bot_data.admin_principal_id,
        suporte_username=bot_data.suporte_username,
        owner_id=current_user.id,  # 🔒 Atribui automaticamente
        status="ativo"
    )

    try:
        db.add(novo_bot)
        db.commit()
        db.refresh(novo_bot)
        
        # ==============================================================================
        # 🔌 CONEXÃO COM TELEGRAM (TEM QUE SER AQUI, ANTES DO RETURN!)
        # ==============================================================================
        try:
            # 1. Define a URL (Já com a correção do 'v1' forçada)
            public_url = os.getenv("RAILWAY_PUBLIC_DOMAIN", "https://zenyx-gbs-testesv1-production.up.railway.app")
            
            # Tratamento de string para evitar erros de URL
            if public_url.startswith("https://"):
                public_url = public_url.replace("https://", "")
            if public_url.endswith("/"):
                public_url = public_url[:-1]

            webhook_url = f"https://{public_url}/webhook/{novo_bot.token}"
            
            # 2. Conecta na API do Telegram e define o Webhook
            bot_telegram = telebot.TeleBot(novo_bot.token)
            bot_telegram.remove_webhook() # Limpa anterior por garantia
            time.sleep(0.5) # Respiro para a API
            bot_telegram.set_webhook(url=webhook_url)
            
            logger.info(f"🔗 Webhook definido com sucesso: {webhook_url}")
            
            # 3. 🆕 BUSCA O USERNAME DO BOT NA API DO TELEGRAM
            try:
                bot_info = bot_telegram.get_me()
                novo_bot.username = bot_info.username  # Salva o @username no banco
                db.commit()  # Persiste a atualização
                logger.info(f"✅ Username capturado: @{bot_info.username}")
            except Exception as e_username:
                logger.warning(f"⚠️ Não foi possível capturar username: {e_username}")

        except Exception as e_telegram:
            # Não vamos travar a criação se der erro no Telegram, mas vamos logar FEIO
            logger.error(f"❌ CRÍTICO: Bot criado no banco, mas falha ao definir Webhook: {e_telegram}")
        # ==============================================================================

        # 📋 AUDITORIA: Bot criado
        log_action(
            db=db,
            user_id=current_user.id,
            username=current_user.username,
            action="bot_created",
            resource_type="bot",
            resource_id=novo_bot.id,
            description=f"Criou bot '{novo_bot.nome}'",
            details={
                "bot_name": novo_bot.nome,
                "token_partial": bot_data.token[:10] + "...",
                "canal_vip": novo_bot.id_canal_vip
            },
            ip_address=get_client_ip(request),
            user_agent=request.headers.get("user-agent")
        )
        
        logger.info(f"✅ Bot criado: {novo_bot.nome} (ID: {novo_bot.id})")
        
        # 🏁 RETORNO DE SUCESSO (SÓ AGORA!)
        return {"id": novo_bot.id, "nome": novo_bot.nome, "status": "criado", "has_bots": True}

    except IntegrityError as e:
        db.rollback() # Limpa a transação falha
        
        error_msg = str(e.orig)
        # Verifica se o erro é duplicidade de Token
        if "ix_bots_token" in error_msg or "unique constraint" in error_msg:
            logger.warning(f"⚠️ Token duplicado detectado: {bot_data.token}")
            
            # Tenta achar o bot que JÁ EXISTE no banco
            bot_existente = db.query(BotModel).filter(BotModel.token == bot_data.token).first()
            
            # Se o bot existe E É DO MESMO DONO (o usuário atual)
            if bot_existente and bot_existente.owner_id == current_user.id:
                logger.info(f"🔄 Recuperando bot ID {bot_existente.id} para destravar fluxo.")
                return {"id": bot_existente.id, "nome": bot_existente.nome, "status": "recuperado", "has_bots": True}
            else:
                raise HTTPException(status_code=409, detail="Este token já pertence a outro usuário.")
        
        logger.error(f"Erro de integridade não tratado: {e}")
        raise HTTPException(status_code=400, detail="Erro de dados ao criar bot.")

    except Exception as e:
        db.rollback()
        
        # 📋 AUDITORIA: Falha genérica
        log_action(
            db=db,
            user_id=current_user.id,
            username=current_user.username,
            action="bot_create_failed",
            resource_type="bot",
            description=f"Falha fatal ao criar bot '{bot_data.nome}'",
            success=False,
            error_message=str(e),
            ip_address=get_client_ip(request),
            user_agent=request.headers.get("user-agent")
        )
        
        logger.error(f"❌ Erro fatal ao criar bot: {e}")
        raise HTTPException(status_code=500, detail="Erro interno ao processar solicitação.")

@app.put("/api/admin/bots/{bot_id}")
def update_bot(
    bot_id: int, 
    dados: BotUpdate, 
    request: Request,  # 🆕 ADICIONADO para auditoria
    db: Session = Depends(get_db),
    current_user = Depends(get_current_user)  # 🆕 ADICIONADO para auditoria e verificação
):
    """
    Atualiza bot (MANTÉM TODA A LÓGICA ORIGINAL + AUDITORIA 🆕)
    """
    # 🔒 VERIFICA SE O BOT PERTENCE AO USUÁRIO
    bot_db = verificar_bot_pertence_usuario(bot_id, current_user.id, db)
    
    # Guarda valores antigos para o log de auditoria
    old_values = {
        "nome": bot_db.nome,
        "token": "***" if bot_db.token else None,  # Não loga token completo
        "canal_vip": bot_db.id_canal_vip,
        "admin_principal": bot_db.admin_principal_id,
        "suporte": bot_db.suporte_username,
        "status": bot_db.status
    }
    
    old_token = bot_db.token
    changes = {}  # Rastreia mudanças para auditoria

    # 1. Atualiza campos administrativos
    if dados.id_canal_vip and dados.id_canal_vip != bot_db.id_canal_vip:
        changes["canal_vip"] = {"old": bot_db.id_canal_vip, "new": dados.id_canal_vip}
        bot_db.id_canal_vip = dados.id_canal_vip
    
    if dados.admin_principal_id is not None and dados.admin_principal_id != bot_db.admin_principal_id:
        changes["admin_principal"] = {"old": bot_db.admin_principal_id, "new": dados.admin_principal_id}
        bot_db.admin_principal_id = dados.admin_principal_id
    
    if dados.suporte_username is not None and dados.suporte_username != bot_db.suporte_username:
        changes["suporte"] = {"old": bot_db.suporte_username, "new": dados.suporte_username}
        bot_db.suporte_username = dados.suporte_username
    
    # 2. LÓGICA DE TROCA DE TOKEN (MANTIDA INTACTA)
    if dados.token and dados.token != old_token:
        try:
            logger.info(f"🔄 Detectada troca de token para o bot ID {bot_id}...")
            new_tb = telebot.TeleBot(dados.token)
            bot_info = new_tb.get_me()
            
            changes["token"] = {"old": "***", "new": "*** (alterado)"}
            changes["nome_via_api"] = {"old": bot_db.nome, "new": bot_info.first_name}
            changes["username_via_api"] = {"old": bot_db.username, "new": bot_info.username}
            
            bot_db.token = dados.token
            bot_db.nome = bot_info.first_name
            bot_db.username = bot_info.username
            
            try:
                old_tb = telebot.TeleBot(old_token)
                old_tb.delete_webhook()
            except: 
                pass

            public_url = os.getenv("RAILWAY_PUBLIC_DOMAIN", "https://zenyx-gbs-testesv1-production.up.railway.app")
            if public_url.startswith("https://"): 
                public_url = public_url.replace("https://", "")
            
            webhook_url = f"https://{public_url}/webhook/{dados.token}"
            new_tb.set_webhook(url=webhook_url)
            
            bot_db.status = "ativo"
            changes["status"] = {"old": old_values["status"], "new": "ativo"}
            
        except Exception as e:
            # 📋 AUDITORIA: Falha ao trocar token
            log_action(
                db=db,
                user_id=current_user.id,
                username=current_user.username,
                action="bot_token_change_failed",
                resource_type="bot",
                resource_id=bot_id,
                description=f"Falha ao trocar token do bot '{bot_db.nome}'",
                success=False,
                error_message=str(e),
                ip_address=get_client_ip(request),
                user_agent=request.headers.get("user-agent")
            )
            raise HTTPException(status_code=400, detail=f"Token inválido: {str(e)}")
            
    else:
        # Se não trocou token, permite atualizar nome manualmente
        if dados.nome and dados.nome != bot_db.nome:
            changes["nome"] = {"old": bot_db.nome, "new": dados.nome}
            bot_db.nome = dados.nome
    
    # 🔥 ATUALIZA O MENU SEMPRE QUE SALVAR (MANTIDO INTACTO)
    try:
        configurar_menu_bot(bot_db.token)
    except Exception as e:
        logger.warning(f"⚠️ Erro ao configurar menu do bot: {e}")
    
    db.commit()
    db.refresh(bot_db)
    
    # 📋 AUDITORIA: Bot atualizado com sucesso
    log_action(
        db=db,
        user_id=current_user.id,
        username=current_user.username,
        action="bot_updated",
        resource_type="bot",
        resource_id=bot_id,
        description=f"Atualizou bot '{bot_db.nome}'",
        details={"changes": changes} if changes else {"message": "Nenhuma alteração detectada"},
        ip_address=get_client_ip(request),
        user_agent=request.headers.get("user-agent")
    )
    
    logger.info(f"✅ Bot atualizado: {bot_db.nome} (Owner: {current_user.username})")
    return {"status": "ok", "msg": "Bot atualizado com sucesso"}

@app.delete("/api/admin/bots/{bot_id}")
def deletar_bot(
    bot_id: int,
    request: Request,
    db: Session = Depends(get_db),
    current_user = Depends(get_current_user)
):
    """
    Deleta bot apenas se pertencer ao usuário
    🆕 Agora com log de auditoria
    """
    bot = verificar_bot_pertence_usuario(bot_id, current_user.id, db)
    
    nome_bot = bot.nome
    canal_vip = bot.id_canal_vip
    username = bot.username
    
    db.delete(bot)
    db.commit()
    
    # 📋 AUDITORIA: Bot deletado
    log_action(
        db=db,
        user_id=current_user.id,
        username=current_user.username,
        action="bot_deleted",
        resource_type="bot",
        resource_id=bot_id,
        description=f"Deletou bot '{nome_bot}'",
        details={
            "bot_name": nome_bot,
            "username": username,
            "canal_vip": canal_vip
        },
        ip_address=get_client_ip(request),
        user_agent=request.headers.get("user-agent")
    )
    
    logger.info(f"🗑 Bot deletado: {nome_bot} (Owner: {current_user.username})")
    return {"status": "deletado", "bot_nome": nome_bot}
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
    
    # 🔔 Notifica Admin (Telegram - EM HTML)
    try:
        emoji = "🟢" if novo_status == "ativo" else "🔴"
        msg = f"{emoji} <b>STATUS DO BOT ALTERADO</b>\n\nO bot <b>{bot.nome}</b> agora está: <b>{novo_status.upper()}</b>"
        notificar_admin_principal(bot, msg)
    except Exception as e:
        logger.error(f"Erro ao notificar admin sobre toggle: {e}")

    # 🔔 NOTIFICAÇÃO NO PAINEL (Sino)
    try:
        msg_status = "Ativado" if novo_status == "ativo" else "Pausado"
        tipo_notif = "success" if novo_status == "ativo" else "warning"
        
        if bot.owner_id:
            create_notification(
                db=db, 
                user_id=bot.owner_id, 
                title=f"Bot {bot.nome} {msg_status}", 
                message=f"O status do seu bot foi alterado para {msg_status}.",
                type=tipo_notif
            )
    except Exception as e:
        logger.error(f"Erro ao criar notificação interna: {e}")
    
    # 👇 A LINHA QUE ESTAVA QUEBRADA AGORA ESTÁ CORRIGIDA:
    logger.info(f"🔄 Bot toggled: {bot.nome} -> {novo_status} (Owner: {current_user.username})")
    
    return {"status": novo_status}

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
    bots = db.query(BotModel).filter(BotModel.owner_id == current_user.id).all()
    
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
# 1. LISTAR PLANOS
# =========================================================
# 💎 GERENCIAMENTO DE PLANOS (CORRIGIDO E UNIFICADO)
# =========================================================

# 1. LISTAR PLANOS
# =========================================================
# 💎 GERENCIAMENTO DE PLANOS (CORRIGIDO E UNIFICADO)
# =========================================================

# 1. LISTAR PLANOS
@app.get("/api/admin/bots/{bot_id}/plans")
def list_plans(bot_id: int, db: Session = Depends(get_db)):
    planos = db.query(PlanoConfig).filter(PlanoConfig.bot_id == bot_id).all()
    return planos

# 2. CRIAR PLANO (CORRIGIDO)
@app.post("/api/admin/bots/{bot_id}/plans")
async def create_plan(bot_id: int, req: Request, db: Session = Depends(get_db)):
    try:
        data = await req.json()
        logger.info(f"📝 Criando plano para Bot {bot_id}: {data}")
        
        # Extrai is_lifetime do payload (padrão False se não vier)
        is_lifetime = data.get("is_lifetime", False)
        
        # Se for vitalício, dias_duracao é irrelevante (mas vamos manter no banco para histórico)
        dias_duracao = int(data.get("dias_duracao", 30))
        
        # Tenta pegar preco_original, se não tiver, usa 0.0
        preco_orig = float(data.get("preco_original", 0.0))
        # Se o preço original for 0, define como o dobro do atual (padrão de marketing)
        if preco_orig == 0:
            preco_orig = float(data.get("preco_atual", 0.0)) * 2

        novo_plano = PlanoConfig(
            bot_id=bot_id,
            nome_exibicao=data.get("nome_exibicao", "Novo Plano"),
            descricao=data.get("descricao", f"Acesso de {dias_duracao} dias"),
            preco_atual=float(data.get("preco_atual", 0.0)),
            preco_cheio=preco_orig,
            dias_duracao=dias_duracao,
            is_lifetime=is_lifetime,  # ← NOVO CAMPO
            key_id=f"plan_{bot_id}_{int(time.time())}"
        )
        
        db.add(novo_plano)
        db.commit()
        db.refresh(novo_plano)
        
        logger.info(f"✅ Plano criado: {novo_plano.nome_exibicao} | Vitalício: {is_lifetime}")
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
                is_lifetime=data.get("is_lifetime", False),  # ← NOVO CAMPO
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
@app.put("/api/admin/bots/{bot_id}/plans/{plan_id}")
async def update_plan(
    bot_id: int, 
    plan_id: int, 
    req: Request, 
    db: Session = Depends(get_db),
    current_user = Depends(get_current_user)
):
    """
    Atualiza um plano existente (incluindo is_lifetime)
    """
    try:
        data = await req.json()
        
        # Buscar plano
        plano = db.query(PlanoConfig).filter(
            PlanoConfig.id == plan_id,
            PlanoConfig.bot_id == bot_id
        ).first()
        
        if not plano:
            raise HTTPException(status_code=404, detail="Plano não encontrado")
        
        # Atualizar campos
        if "nome_exibicao" in data:
            plano.nome_exibicao = data["nome_exibicao"]
        if "descricao" in data:
            plano.descricao = data["descricao"]
        if "preco_atual" in data:
            plano.preco_atual = float(data["preco_atual"])
        if "dias_duracao" in data:
            plano.dias_duracao = int(data["dias_duracao"])
        if "is_lifetime" in data:  # ← NOVO CAMPO
            plano.is_lifetime = bool(data["is_lifetime"])
        
        db.commit()
        db.refresh(plano)
        
        logger.info(f"✏️ Plano {plano.id} atualizado: {plano.nome_exibicao} | Vitalício: {plano.is_lifetime}")
        return plano
        
    except Exception as e:
        logger.error(f"Erro ao atualizar plano: {e}")
        raise HTTPException(status_code=500, detail=str(e))

# 4. DELETAR PLANO (COM SEGURANÇA)
@app.delete("/api/admin/bots/{bot_id}/plans/{plano_id}")
def delete_plan(bot_id: int, plano_id: int, db: Session = Depends(get_db)):
    try:
        plano = db.query(PlanoConfig).filter(
            PlanoConfig.id == plano_id, 
            PlanoConfig.bot_id == bot_id
        ).first()
        
        if not plano:
            raise HTTPException(status_code=404, detail="Plano não encontrado.")
            
        # Desvincula de campanhas e pedidos para evitar erro de integridade
        db.query(RemarketingCampaign).filter(RemarketingCampaign.plano_id == plano_id).update({RemarketingCampaign.plano_id: None})
        db.query(Pedido).filter(Pedido.plano_id == plano_id).update({Pedido.plano_id: None})
        
        db.delete(plano)
        db.commit()
        return {"status": "deleted"}
    except Exception as e:
        logger.error(f"Erro ao deletar plano: {e}")
        raise HTTPException(status_code=500, detail="Erro ao deletar plano.")

# =========================================================
# 🛒 ORDER BUMP API (BLINDADO)
# =========================================================
@app.get("/api/admin/bots/{bot_id}/order-bump")
def get_order_bump(
    bot_id: int, 
    db: Session = Depends(get_db)
    # REMOVIDO current_user para evitar erro 401 no Mini App
):
    # Nota: No GET não usamos verificar_bot_pertence_usuario pois o acesso é público (Vitrine)
    
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
    current_user = Depends(get_current_user) # 🔒 AUTH MANTIDA NO SALVAR
):
    # 🔒 VERIFICA PROPRIEDADE (Só o dono pode alterar)
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
# =========================================================
# 🔗 ROTAS DE TRACKING (RASTREAMENTO) - VERSÃO CORRIGIDA
# =========================================================
# ⚠️ SUBSTITUIR AS LINHAS 2468-2553 DO SEU main.py POR ESTE CÓDIGO
# =========================================================

# =========================================================
# 🔗 ROTAS DE TRACKING (RASTREAMENTO) - VERSÃO FINAL CORRIGIDA
# =========================================================

# =========================================================
# 🎯 RASTREAMENTO (TRACKING) - BLINDADO E ISOLADO V2 (ASYNC)
# =========================================================

# --- 1. PASTAS (FOLDERS) ---

# =========================================================
# 🎯 RASTREAMENTO (TRACKING) - CORRIGIDO (SEM OWNER_ID)
# =========================================================

# --- 1. PASTAS (FOLDERS) ---

# =========================================================
# 🎯 RASTREAMENTO (TRACKING) - VERSÃO PERMISSIVA (VISUALIZAÇÃO IMEDIATA)
# =========================================================

# --- 1. PASTAS (FOLDERS) ---

# =========================================================
# 🎯 RASTREAMENTO (TRACKING) - V4 (MODO CHEFE SUPREMO)
# =========================================================

# --- 1. PASTAS (FOLDERS) ---

# =========================================================
# 🎯 RASTREAMENTO (TRACKING) - V5 (VISÃO LIMPA PARA ADMIN)
# =========================================================

# --- 1. PASTAS (FOLDERS) ---

@app.get("/api/admin/tracking/folders")
async def list_tracking_folders(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """
    Lista pastas.
    CORREÇÃO V5: Removemos o 'Modo Deus' da visualização.
    O Admin agora só vê:
    1. Pastas Vazias (que ele acabou de criar).
    2. Pastas com links de bots que ELE realmente possui (user.bots).
    Isso esconde as pastas de outros usuários da tela do Admin.
    """
    try:
        # 🔥 MUDANÇA: Admin usa a mesma lista de bots que um mortal.
        # Isso garante que ele não veja as pastas do "Mago" ou de outros clientes.
        user_bot_ids = [bot.id for bot in current_user.bots]
        
        # Busca todas as pastas (da mais nova para mais antiga)
        folders = db.query(TrackingFolder).order_by(desc(TrackingFolder.created_at)).all()
        
        result = []
        for f in folders:
            # Conta links totais na pasta (só para saber se está vazia)
            total_links_absoluto = db.query(TrackingLink).filter(TrackingLink.folder_id == f.id).count()
            
            # Conta links "meus" (Dos bots vinculados ao meu usuário)
            meus_links_count = 0
            stats = None
            
            if user_bot_ids:
                meus_links_count = db.query(TrackingLink).filter(
                    TrackingLink.folder_id == f.id,
                    TrackingLink.bot_id.in_(user_bot_ids)
                ).count()
                
                if meus_links_count > 0:
                    stats = db.query(
                        func.sum(TrackingLink.clicks).label('total_clicks'),
                        func.sum(TrackingLink.vendas).label('total_vendas')
                    ).filter(
                        TrackingLink.folder_id == f.id,
                        TrackingLink.bot_id.in_(user_bot_ids)
                    ).first()
            
            # --- LÓGICA DE EXIBIÇÃO (ISOLAMENTO) ---
            # Mostra SE:
            # 1. Tenho links meus lá dentro (meus_links_count > 0)
            # 2. OU a pasta está totalmente vazia (total_links_absoluto == 0)
            #    (Isso permite que o Admin crie a pasta 'FBC' e a veja, pois está vazia)
            should_show = (meus_links_count > 0) or (total_links_absoluto == 0)
            
            if should_show:
                result.append({
                    "id": f.id, 
                    "nome": f.nome, 
                    "plataforma": f.plataforma, 
                    "link_count": meus_links_count,
                    "total_clicks": (stats.total_clicks if stats else 0) or 0,
                    "total_vendas": (stats.total_vendas if stats else 0) or 0,
                    "created_at": f.created_at
                })
        
        logger.info(f"📂 Usuário {current_user.username} vê {len(result)} pastas (Visão Limpa)")
        return result
        
    except Exception as e:
        logger.error(f"Erro ao listar pastas: {e}")
        return []

@app.post("/api/admin/tracking/folders")
async def create_tracking_folder(
    dados: TrackingFolderCreate, 
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    try:
        # Verifica duplicidade
        existe = db.query(TrackingFolder).filter(
            func.lower(TrackingFolder.nome) == dados.nome.lower()
        ).first()
        
        if existe:
            return {"status": "ok", "id": existe.id, "msg": "Pasta já existia"}

        nova_pasta = TrackingFolder(
            nome=dados.nome, 
            plataforma=dados.plataforma
        )
        db.add(nova_pasta)
        db.commit()
        db.refresh(nova_pasta)
        
        logger.info(f"📁 Pasta '{dados.nome}' criada por {current_user.username}")
        return {"status": "ok", "id": nova_pasta.id}
        
    except Exception as e:
        logger.error(f"Erro ao criar pasta: {e}")
        raise HTTPException(status_code=500, detail="Erro interno ao criar pasta")

@app.delete("/api/admin/tracking/folders/{fid}")
async def delete_tracking_folder(
    fid: int, 
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    try:
        # Para deletar, mantemos o poder do Admin de apagar qualquer coisa se necessário
        # Mas para ser consistente, vamos usar a mesma lógica de bots
        user_bot_ids = [bot.id for bot in current_user.bots]
        is_admin = current_user.is_superuser
        
        folder = db.query(TrackingFolder).filter(TrackingFolder.id == fid).first()
        if not folder:
            raise HTTPException(404, "Pasta não encontrada")
        
        # Se NÃO for admin, verifica se tem links de outros
        if not is_admin:
            links_outros = db.query(TrackingLink).filter(
                TrackingLink.folder_id == fid,
                TrackingLink.bot_id.notin_(user_bot_ids)
            ).count()
            
            if links_outros > 0:
                raise HTTPException(403, "Pasta contém links de outros usuários.")
        
        # Admin ou Dono -> Deleta
        db.query(TrackingLink).filter(TrackingLink.folder_id == fid).delete()
        db.delete(folder)
        db.commit()
        
        return {"status": "deleted"}
    except HTTPException as he:
        raise he
    except Exception as e:
        logger.error(f"Erro ao deletar pasta: {e}")
        raise HTTPException(500, "Erro interno")

# --- 2. LINKS DE RASTREAMENTO ---

@app.get("/api/admin/tracking/links/{folder_id}")
async def list_tracking_links(
    folder_id: int, 
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """
    Lista links.
    """
    user_bot_ids = [bot.id for bot in current_user.bots]
    is_admin = current_user.is_superuser

    query = db.query(TrackingLink).filter(TrackingLink.folder_id == folder_id)
    
    # Se NÃO for admin, filtra só os meus bots
    if not is_admin:
        if not user_bot_ids: return []
        query = query.filter(TrackingLink.bot_id.in_(user_bot_ids))
    
    return query.order_by(desc(TrackingLink.created_at)).all()

@app.post("/api/admin/tracking/links")
async def create_tracking_link(
    dados: TrackingLinkCreate, 
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    try:
        user_bot_ids = [bot.id for bot in current_user.bots]
        is_admin = current_user.is_superuser
        
        # Admin pode criar link para qualquer bot. Usuário só para os seus.
        if not is_admin:
            if dados.bot_id not in user_bot_ids:
                raise HTTPException(403, "Você não tem permissão para criar links neste bot.")

        if not dados.codigo:
            import random, string
            chars = string.ascii_lowercase + string.digits
            dados.codigo = ''.join(random.choice(chars) for _ in range(8))
        
        exists = db.query(TrackingLink).filter(TrackingLink.codigo == dados.codigo).first()
        if exists:
            raise HTTPException(400, "Este código já existe.")

        novo_link = TrackingLink(
            folder_id=dados.folder_id,
            bot_id=dados.bot_id,
            nome=dados.nome,
            codigo=dados.codigo,
            origem=dados.origem,
            clicks=0,
            vendas=0,
            faturamento=0.0
        )
        db.add(novo_link)
        db.commit()
        db.refresh(novo_link)
        
        return {"status": "ok", "link": novo_link}

    except HTTPException as he:
        raise he
    except Exception as e:
        logger.error(f"Erro criar link: {e}")
        raise HTTPException(status_code=500, detail="Erro interno")

@app.delete("/api/admin/tracking/links/{lid}")
async def delete_link(
    lid: int, 
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    user_bot_ids = [bot.id for bot in current_user.bots]
    is_admin = current_user.is_superuser
    
    link = db.query(TrackingLink).filter(TrackingLink.id == lid).first()
    if not link:
        raise HTTPException(404, "Link não encontrado")
    
    if not is_admin:
        if link.bot_id not in user_bot_ids:
            raise HTTPException(403, "Acesso negado")
    
    db.delete(link)
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
    bot = db.query(BotModel).filter(BotModel.id == bot_id).first()
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
    bot = db.query(BotModel).filter(BotModel.id == bot_id).first()
    if not bot:
        raise HTTPException(status_code=404, detail="Bot não encontrado")
    
    # Aqui poderíamos salvar no banco se tivéssemos a coluna 'modo', 
    # mas por enquanto vamos assumir que a existência de configuração de MiniApp
    # ativa o modo. Se quiser formalizar, adicione 'modo' na tabela BotModel.
    
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

# =========================================================
# 💳 WEBHOOK PIX (PUSHIN PAY) - V4.0 (CORREÇÃO VITALÍCIO + NOTIFICAÇÃO)
# =========================================================
# =========================================================
# 💳 WEBHOOK PIX (PUSHIN PAY) - V5.0 COM RETRY
# =========================================================
@app.post("/api/webhooks/pushinpay")
@app.post("/webhook/pix")
async def webhook_pix(request: Request, db: Session = Depends(get_db)):
    """
    Webhook de pagamento com sistema de retry automático.
    Se falhar, agenda reprocessamento com exponential backoff.
    """
    print("🔔 WEBHOOK PIX CHEGOU!")
    
    try:
        # 1. EXTRAIR PAYLOAD
        body_bytes = await request.body()
        body_str = body_bytes.decode("utf-8")
        
        try:
            data = json.loads(body_str)
            if isinstance(data, list): 
                data = data[0]
        except:
            try:
                parsed = urllib.parse.parse_qs(body_str)
                data = {k: v[0] for k, v in parsed.items()}
            except:
                logger.error(f"❌ Payload inválido: {body_str[:200]}")
                return {"status": "ignored"}
        
        # 2. VALIDAR STATUS
        raw_tx_id = data.get("id") or data.get("external_reference") or data.get("uuid")
        tx_id = str(raw_tx_id).lower() if raw_tx_id else None
        status_pix = str(data.get("status", "")).lower()
        
        if status_pix not in ["paid", "approved", "completed", "succeeded"]:
            return {"status": "ignored"}
        
        # 3. BUSCAR PEDIDO
        pedido = db.query(Pedido).filter(
            (Pedido.txid == tx_id) | (Pedido.transaction_id == tx_id)
        ).first()
        
        if not pedido:
            logger.warning(f"⚠️ Pedido {tx_id} não encontrado")
            return {"status": "ok", "msg": "Order not found"}
        
        if pedido.status in ["approved", "paid", "active"]:
            return {"status": "ok", "msg": "Already paid"}
        
        # 4. PROCESSAR PAGAMENTO (LÓGICA CRÍTICA)
        try:
            # Calcular data de expiração (lógica refatorada com is_lifetime)
            now = datetime.utcnow()
            data_validade = None
            
            plano = None
            if pedido.plano_id:
                try:
                    plano_id_int = int(pedido.plano_id) if str(pedido.plano_id).isdigit() else None
                    if plano_id_int:
                        plano = db.query(PlanoConfig).filter(PlanoConfig.id == plano_id_int).first()
                except (ValueError, TypeError):
                    logger.warning(f"⚠️ plano_id inválido: {pedido.plano_id}")
            
            if plano:
                if plano.is_lifetime:
                    data_validade = None
                    logger.info(f"♾️ Plano '{plano.nome_exibicao}' é VITALÍCIO")
                else:
                    dias = plano.dias_duracao if plano.dias_duracao else 30
                    data_validade = now + timedelta(days=dias)
                    logger.info(f"📅 Plano válido por {dias} dias até {data_validade.strftime('%d/%m/%Y')}")
            else:
                logger.warning(f"⚠️ Plano não encontrado. Usando 30 dias padrão.")
                data_validade = now + timedelta(days=30)
            
            # Atualizar pedido
            pedido.status = "approved"
            pedido.data_aprovacao = now
            pedido.data_expiracao = data_validade
            pedido.custom_expiration = data_validade
            pedido.mensagem_enviada = False
            pedido.status_funil = 'fundo'
            pedido.pagou_em = now
            
            # Converter Lead em Cliente
            try:
                lead = db.query(Lead).filter(
                    Lead.bot_id == pedido.bot_id,
                    Lead.user_id == pedido.telegram_id
                ).first()
                
                if lead:
                    lead.status = "convertido"
                    lead.funil_stage = "cliente"
                    lead.expiration_date = data_validade
                    logger.info(f"✅ Lead {lead.username} convertido em cliente")
            except Exception as e_lead:
                logger.error(f"⚠️ Erro ao converter Lead: {e_lead}")
            
            db.commit()
            
            # Atualizar Tracking
            if pedido.tracking_id:
                try:
                    t_link = db.query(TrackingLink).filter(TrackingLink.id == pedido.tracking_id).first()
                    if t_link:
                        t_link.vendas += 1
                        t_link.faturamento += pedido.valor
                        db.commit()
                except:
                    pass
            
            texto_validade = data_validade.strftime("%d/%m/%Y") if data_validade else "VITALÍCIO ♾️"
            logger.info(f"✅ Pedido {tx_id} APROVADO! Validade: {texto_validade}")
            
            # 5. ENTREGA DO ACESSO
            try:
                bot_data = db.query(BotModel).filter(BotModel.id == pedido.bot_id).first()
                if bot_data:
                    tb = telebot.TeleBot(bot_data.token, threaded=False)
                    target_id = str(pedido.telegram_id).strip()
                    
                    # Corrigir ID se necessário
                    if not target_id.isdigit():
                        clean_user = str(pedido.username).lower().replace("@", "").strip()
                        lead = db.query(Lead).filter(
                            Lead.bot_id == pedido.bot_id,
                            (func.lower(Lead.username) == clean_user) | 
                            (func.lower(Lead.username) == f"@{clean_user}")
                        ).order_by(desc(Lead.created_at)).first()
                        
                        if lead and lead.user_id and lead.user_id.isdigit():
                            target_id = lead.user_id
                            pedido.telegram_id = target_id
                            db.commit()
                    
                    if target_id.isdigit():
                        # Entrega principal
                        try:
                            canal_id = bot_data.id_canal_vip
                            if str(canal_id).replace("-", "").isdigit():
                                canal_id = int(str(canal_id).strip())
                            
                            try:
                                tb.unban_chat_member(canal_id, int(target_id))
                            except:
                                pass
                            
                            convite = tb.create_chat_invite_link(
                                chat_id=canal_id,
                                member_limit=1,
                                name=f"Venda {pedido.first_name}"
                            )
                            
                            msg_cliente = (
                                f"✅ <b>Pagamento Confirmado!</b>\n"
                                f"📅 Validade: <b>{texto_validade}</b>\n\n"
                                f"Seu acesso exclusivo:\n👉 {convite.invite_link}"
                            )
                            
                            tb.send_message(int(target_id), msg_cliente, parse_mode="HTML")
                            logger.info(f"✅ Entrega enviada para {target_id}")
                            
                        except Exception as e_main:
                            logger.error(f"❌ Erro na entrega principal: {e_main}")
                        
                        # Entrega Order Bump
                        if pedido.tem_order_bump:
                            try:
                                bump_config = db.query(OrderBumpConfig).filter(
                                    OrderBumpConfig.bot_id == bot_data.id
                                ).first()
                                
                                if bump_config and bump_config.link_acesso:
                                    msg_bump = (
                                        f"🎁 <b>BÔNUS LIBERADO!</b>\n\n"
                                        f"👉 <b>{bump_config.nome_produto}</b>\n"
                                        f"🔗 {bump_config.link_acesso}"
                                    )
                                    tb.send_message(int(target_id), msg_bump, parse_mode="HTML")
                                    logger.info("✅ Order Bump entregue")
                            except Exception as e_bump:
                                logger.error(f"❌ Erro Bump: {e_bump}")
                        
                        # Notificar Admin
                        try:
                            msg_admin = (
                                f"💰 <b>VENDA REALIZADA!</b>\n\n"
                                f"🤖 Bot: <b>{bot_data.nome}</b>\n"
                                f"👤 Cliente: {pedido.first_name} (@{pedido.username})\n"
                                f"📦 Plano: {pedido.plano_nome}\n"
                                f"💵 Valor: <b>R$ {pedido.valor:.2f}</b>\n"
                                f"📅 Vence em: {texto_validade}"
                            )
                            notificar_admin_principal(bot_data, msg_admin)
                        except Exception as e_adm:
                            logger.error(f"❌ Erro notificação admin: {e_adm}")
                        
                        pedido.mensagem_enviada = True
                        db.commit()
                        
            except Exception as e_tg:
                logger.error(f"❌ Erro Telegram/Entrega: {e_tg}")
                # Não falhar o webhook por erro de entrega
            
            # Webhook processado com sucesso
            return {"status": "received"}
            
        except Exception as e_process:
            # ERRO CRÍTICO NO PROCESSAMENTO
            logger.error(f"❌ ERRO no processamento do webhook: {e_process}")
            
            # Registrar para retry
            registrar_webhook_para_retry(
                webhook_type='pushinpay',
                payload=data,
                reference_id=tx_id
            )
            
            # Retornar erro 500 para PushinPay tentar novamente
            raise HTTPException(status_code=500, detail="Erro interno, será reprocessado")
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"❌ ERRO CRÍTICO NO WEBHOOK: {e}")
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
    
    bot_db = db.query(BotModel).filter(BotModel.token == token).first()
    if not bot_db or bot_db.status == "pausado": return {"status": "ignored"}

    try:
        body = await req.json()
        update = telebot.types.Update.de_json(body)
        # 🔥 FIX: threaded=False obriga o envio a acontecer AGORA, sem criar thread paralela
        bot_temp = telebot.TeleBot(token, threaded=False)
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
                
                # SE FOR PADRÃO
                else:
                    if flow and flow.mostrar_planos_1:
                        planos = db.query(PlanoConfig).filter(PlanoConfig.bot_id == bot_db.id).all()
                        for pl in planos: 
                            preco_txt = f"R$ {pl.preco_atual:.2f}".replace('.', ',')
                            mk.add(types.InlineKeyboardButton(f"💎 {pl.nome_exibicao} - {preco_txt}", callback_data=f"checkout_{pl.id}"))
                    else: 
                        mk.add(types.InlineKeyboardButton(flow.btn_text_1 if flow else "Ver Conteúdo", callback_data="step_1"))

                # 🔥 BLOCO DE ENVIO COM LOG DE ERRO REAL
                try:
                    logger.info(f"📤 Tentando enviar menu para {chat_id}...")
                    if media:
                        if media.endswith(('.mp4', '.mov')): 
                            bot_temp.send_video(chat_id, media, caption=msg_txt, reply_markup=mk, parse_mode="HTML")
                        else: 
                            bot_temp.send_photo(chat_id, media, caption=msg_txt, reply_markup=mk, parse_mode="HTML")
                    else: 
                        bot_temp.send_message(chat_id, msg_txt, reply_markup=mk, parse_mode="HTML")
                    
                    logger.info("✅ Menu enviado com sucesso!")

                except Exception as e_envio:
                    logger.error(f"❌ ERRO AO ENVIAR MENSAGEM: {e_envio}")
                    # Tenta fallback sem HTML
                    try: bot_temp.send_message(chat_id, msg_txt, reply_markup=mk)
                    except: pass

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
                    # =========================================================
                    # ✅ CORREÇÃO 1: DECLARAR VARIÁVEIS ANTES DE USAR
                    # =========================================================
                    msg_wait = bot_temp.send_message(chat_id, "⏳ Gerando <b>PIX</b>...", parse_mode="HTML")
                    mytx = str(uuid.uuid4())
                    
                    pix = await gerar_pix_pushinpay(
                        valor_float=plano.preco_atual,
                        transaction_id=mytx,
                        bot_id=bot_db.id,
                        db=db,
                        user_telegram_id=str(chat_id),
                        user_first_name=first_name,
                        plano_nome=plano.nome_exibicao
                    )

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
                
                # =========================================================
                # ✅ CORREÇÃO 2: DECLARAR VARIÁVEIS ANTES DE USAR
                # =========================================================
                msg_wait = bot_temp.send_message(chat_id, f"⏳ Gerando PIX: <b>{nome_final}</b>...", parse_mode="HTML")
                mytx = str(uuid.uuid4())

                pix = await gerar_pix_pushinpay(
                    valor_float=valor_final,
                    transaction_id=mytx,
                    bot_id=bot_db.id,
                    db=db,
                    user_telegram_id=str(chat_id),
                    user_first_name=first_name,
                    plano_nome=nome_final
                )
                
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
                else:
                    bot_temp.send_message(chat_id, "❌ Erro ao gerar PIX.")

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
                       # =========================================================
                        # ✅ CORREÇÃO 3: DECLARAR VARIÁVEIS ANTES DE USAR
                        # =========================================================
                        msg_wait = bot_temp.send_message(chat_id, "⏳ Gerando <b>OFERTA ESPECIAL</b>...", parse_mode="HTML")
                        mytx = str(uuid.uuid4())

                        pix = await gerar_pix_pushinpay(
                            valor_float=preco_final,
                            transaction_id=mytx,
                            bot_id=bot_db.id,
                            db=db,
                            user_telegram_id=str(chat_id),
                            user_first_name=first_name,
                            plano_nome=f"{plano.nome_exibicao} (OFERTA)"
                        )
                        
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
# ============================================================
# ROTA 1: LISTAR LEADS (DEDUPLICAÇÃO FORÇADA NA MEMÓRIA)
# ============================================================
@app.get("/api/admin/leads")
async def listar_leads(
    bot_id: Optional[int] = None,
    page: int = 1,
    per_page: int = 50,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    try:
        # 1. Autenticação e Permissões
        user_bot_ids = [bot.id for bot in current_user.bots]
        if not user_bot_ids:
            return {"data": [], "total": 0, "page": page, "per_page": per_page, "total_pages": 0}

        bots_alvo = [bot_id] if (bot_id and bot_id in user_bot_ids) else user_bot_ids

        # 2. BUSCA TUDO (Sem paginação no SQL)
        raw_leads = db.query(Lead).filter(
            Lead.bot_id.in_(bots_alvo),
            Lead.status != "convertido"  # Exclui convertidos
        ).order_by(Lead.created_at.desc()).all()
        
        # 3. O FILTRO "PENTE FINO" 🧹
        leads_unicos = {}
        
        for lead in raw_leads:
            # TRATAMENTO AGRESSIVO DE ID
            # Remove espaços, converte pra string, força minúsculo
            tid_sujo = str(lead.user_id)
            tid_limpo = tid_sujo.strip().replace(" ", "")
            
            # Chave única: Bot + ID Limpo
            key = f"{lead.bot_id}_{tid_limpo}"
            
            # Se a chave ainda não existe, adicionamos.
            # Como a lista vem ordenada do MAIS NOVO, o primeiro que entra é o atual.
            # Os próximos (mais velhos) serão ignorados.
            if key not in leads_unicos:
                
                # Tratamento de datas seguro
                data_criacao = None
                if lead.created_at:
                    data_criacao = lead.created_at.isoformat()

                primeiro_contato = None
                if lead.primeiro_contato:
                    primeiro_contato = lead.primeiro_contato.isoformat()
                    
                ultimo_contato = None
                if lead.ultimo_contato:
                    ultimo_contato = lead.ultimo_contato.isoformat()

                # Tenta pegar expiration com segurança
                expiration = getattr(lead, 'expiration_date', None)
                expiration_str = expiration.isoformat() if expiration else None

                leads_unicos[key] = {
                    "id": lead.id,
                    "user_id": tid_limpo, # Retorna o ID limpo
                    "nome": lead.nome or "Sem nome",
                    "username": lead.username,
                    "bot_id": lead.bot_id,
                    "status": lead.status,
                    "funil_stage": lead.funil_stage,
                    "primeiro_contato": primeiro_contato,
                    "ultimo_contato": ultimo_contato,
                    "total_remarketings": lead.total_remarketings,
                    "ultimo_remarketing": lead.ultimo_remarketing.isoformat() if lead.ultimo_remarketing else None,
                    "created_at": data_criacao,
                    "expiration_date": expiration_str
                }
        
        # 4. PAGINAÇÃO MANUAL
        lista_final = list(leads_unicos.values())
        total = len(lista_final)
        
        offset = (page - 1) * per_page
        paginated_data = lista_final[offset:offset + per_page]
        
        return {
            "data": paginated_data,
            "total": total,
            "page": page,
            "per_page": per_page,
            "total_pages": (total + per_page - 1) // per_page if per_page > 0 else 0
        }
    
    except Exception as e:
        logger.error(f"Erro ao listar leads: {str(e)}")
        # Em caso de erro, retorna vazio em vez de quebrar a tela
        return {"data": [], "total": 0, "page": page, "per_page": per_page, "total_pages": 0}

# ============================================================
# ROTA 2: ESTATÍSTICAS DO FUNIL
# ============================================================
# ============================================================
# 🔥 ROTA DEFINITIVA: ESTATÍSTICAS DO FUNIL (DEDUPLICAÇÃO REAL)
# ============================================================
# ============================================================
# 🔥 ROTA DEFINITIVA: ESTATÍSTICAS DO FUNIL (CONTTAGEM REAL DE HUMANOS)
# ============================================================
@app.get("/api/admin/contacts/funnel-stats")
async def obter_estatisticas_funil(
    bot_id: Optional[int] = None,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    try:
        user_bot_ids = [bot.id for bot in current_user.bots]
        if not user_bot_ids:
            return {"topo": 0, "meio": 0, "fundo": 0, "expirados": 0, "total": 0}

        bots_alvo = [bot_id] if (bot_id and bot_id in user_bot_ids) else user_bot_ids

        # 1. Busca IDs únicos de cada etapa no banco
        # TOPO (Leads que não converteram)
        ids_topo = db.query(Lead.user_id).filter(
            Lead.bot_id.in_(bots_alvo),
            Lead.status != "convertido"
        ).distinct().all()
        
        # MEIO (Pedidos pendentes)
        ids_meio = db.query(Pedido.telegram_id).filter(
            Pedido.bot_id.in_(bots_alvo),
            Pedido.status == 'pending'
        ).distinct().all()
        
        # FUNDO (Clientes pagos)
        ids_fundo = db.query(Pedido.telegram_id).filter(
            Pedido.bot_id.in_(bots_alvo),
            Pedido.status.in_(['paid', 'active', 'approved'])
        ).distinct().all()
        
        # EXPIRADOS
        ids_expirados = db.query(Pedido.telegram_id).filter(
            Pedido.bot_id.in_(bots_alvo),
            Pedido.status == 'expired'
        ).distinct().all()

        # 2. Converte para Sets para garantir unicidade e limpeza de strings
        def extrair_e_limpar(lista_tuplas):
            return {str(item[0]).strip() for item in lista_tuplas if item[0]}

        set_topo = extrair_e_limpar(ids_topo)
        set_meio = extrair_e_limpar(ids_meio)
        set_fundo = extrair_e_limpar(ids_fundo)
        set_expirados = extrair_e_limpar(ids_expirados)

        # 3. O GRANDE TRUQUE: O Total é a união de todos os IDs sem repetir ninguém
        total_unicos = set_topo.union(set_meio).union(set_fundo).union(set_expirados)

        return {
            "topo": len(set_topo),
            "meio": len(set_meio),
            "fundo": len(set_fundo),
            "expirados": len(set_expirados),
            "total": len(total_unicos) # <--- Agora vai mostrar 6 e não 14!
        }
        
    except Exception as e:
        logger.error(f"Erro stats funil: {e}")
        return {"topo": 0, "meio": 0, "fundo": 0, "expirados": 0, "total": 0}

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
# ============================================================
# 🔥 ROTA DE CONTATOS (V4.0 - CORREÇÃO TOTAL DE DUPLICATAS)
# ============================================================
@app.get("/api/admin/contacts")
async def get_contacts(
    status: str = "todos",
    bot_id: Optional[int] = None,
    page: int = 1,
    per_page: int = 50,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    try:
        # 1. Busca IDs dos Bots de forma segura (SQL Direto)
        bot_ids_query = db.query(BotModel.id).filter(BotModel.owner_id == current_user.id).all()
        user_bot_ids = [b[0] for b in bot_ids_query]
        
        # Helper para limpar data e timezone
        def clean_date(dt):
            if not dt: return None
            return dt.replace(tzinfo=None)

        # Se não tiver bots, retorna vazio
        if not user_bot_ids:
            return {"data": [], "total": 0, "page": page, "per_page": per_page, "total_pages": 0}

        # Validação de segurança do bot_id
        if bot_id and bot_id not in user_bot_ids:
            return {"data": [], "total": 0, "page": page, "per_page": per_page, "total_pages": 0}

        # Define quais bots vamos consultar
        bots_alvo = [bot_id] if bot_id else user_bot_ids
        
        # Prepara a paginação
        offset = (page - 1) * per_page
        
        # Dicionário Mágico para Remover Duplicatas (Chave = BotID_TelegramID)
        contatos_unicos = {}

        # ============================================================
        # CENÁRIO 1: "TODOS" (Mescla Leads + Pedidos)
        # ============================================================
        if status == "todos":
            # A. Processa LEADS
            leads = db.query(Lead).filter(Lead.bot_id.in_(bots_alvo)).all()
            for l in leads:
                tid = str(l.user_id).strip()
                key = f"{l.bot_id}_{tid}"
                
                # Tenta pegar a data de expiração do lead (se existir a coluna)
                data_lead = getattr(l, 'expiration_date', None)

                contatos_unicos[key] = {
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
                    "origem": "lead",
                    "custom_expiration": clean_date(data_lead)
                }

            # B. Processa PEDIDOS (Sobrepõe Leads para atualizar status)
            pedidos = db.query(Pedido).filter(Pedido.bot_id.in_(bots_alvo)).order_by(Pedido.created_at.asc()).all()
            for p in pedidos:
                tid = str(p.telegram_id).strip()
                key = f"{p.bot_id}_{tid}"
                
                st_funil = "meio"
                if p.status in ["paid", "approved", "active"]: st_funil = "fundo"
                elif p.status == "expired": st_funil = "expirado"
                
                data_exp = clean_date(p.data_expiracao) or clean_date(p.custom_expiration)

                obj_pedido = {
                    "id": p.id,
                    "telegram_id": tid,
                    "user_id": tid,
                    "first_name": p.first_name or "Sem nome",
                    "username": p.username,
                    "plano_nome": p.plano_nome,
                    "valor": float(p.valor or 0),
                    "status": p.status,
                    "role": "user",
                    "created_at": clean_date(p.created_at),
                    "status_funil": st_funil,
                    "origem": "pedido",
                    "custom_expiration": data_exp
                }

                # ✅ LÓGICA DE MERGE CORRIGIDA: Pedido SEMPRE sobrepõe Lead
                # Se o usuário tem QUALQUER pedido, ele prevalece sobre o lead antigo
                contatos_unicos[key] = obj_pedido

        # ============================================================
        # CENÁRIO 2: FILTROS ESPECÍFICOS (PAGANTES, PENDENTES...)
        # ============================================================
        else:
            # Busca TODOS os pedidos do filtro (sem limit ainda, para poder deduplicar)
            query = db.query(Pedido).filter(Pedido.bot_id.in_(bots_alvo))
            
            if status == "meio" or status == "pendentes":
                query = query.filter(Pedido.status == "pending")
            elif status == "fundo" or status == "pagantes":
                query = query.filter(Pedido.status.in_(["paid", "active", "approved"]))
            elif status == "expirado" or status == "expirados":
                query = query.filter(Pedido.status == "expired")
            
            # Ordena ASCENDENTE: O último registro do loop será o mais recente
            raw_pedidos = query.order_by(Pedido.created_at.asc()).all()

            for p in raw_pedidos:
                tid = str(p.telegram_id).strip()
                key = f"{p.bot_id}_{tid}"
                
                # Como o loop roda do mais antigo pro mais novo, o dicionário
                # sempre vai ficar com a ÚLTIMA versão do pedido (eliminando os velhos)
                contatos_unicos[key] = {
                    "id": p.id,
                    "telegram_id": tid,
                    "user_id": tid,
                    "first_name": p.first_name or "Sem nome",
                    "username": p.username,
                    "plano_nome": p.plano_nome,
                    "valor": float(p.valor or 0),
                    "status": p.status,
                    "role": "user",
                    "created_at": clean_date(p.created_at),
                    "custom_expiration": clean_date(p.data_expiracao) or clean_date(p.custom_expiration),
                    "origem": "pedido"
                }

        # ============================================================
        # 3. FINALIZAÇÃO: ORDENAÇÃO E PAGINAÇÃO (NO PYTHON)
        # ============================================================
        
        # Converte o dicionário (que removeu as duplicatas) em lista
        all_contacts = list(contatos_unicos.values())
        
        # Ordena a lista final por data (Mais recentes primeiro)
        all_contacts.sort(key=lambda x: x["created_at"] or datetime.min, reverse=True)
        
        # Calcula totais
        total = len(all_contacts)
        
        # Aplica a paginação na LISTA LIMPA
        paginated = all_contacts[offset:offset + per_page]
        
        # Retorno final para o Frontend
        return {
            "data": paginated,
            "total": total,
            "page": page,
            "per_page": per_page,
            "total_pages": (total + per_page - 1) // per_page if per_page > 0 else 0
        }

    except Exception as e:
        logger.error(f"Erro contatos: {e}")
        # Retorna lista vazia para não quebrar a tela em caso de erro grave
        return {"data": [], "total": 0, "page": 1, "per_page": per_page, "total_pages": 0}
        
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
        bot_data = db.query(BotModel).filter(BotModel.id == pedido.bot_id).first()
        
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
        bot_db = db.query(BotModel).filter(BotModel.id == bot_id).first()
        
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
async def enviar_remarketing(
    payload: RemarketingRequest, 
    background_tasks: BackgroundTasks,  # ← CRÍTICO: Injeção do FastAPI
    db: Session = Depends(get_db),
    current_user = Depends(get_current_user)  # ← Adicionar autenticação
):
    """
    Envia campanha de remarketing em BACKGROUND.
    Retorna imediatamente sem bloquear o servidor.
    
    IMPORTANTE: Esta rota retorna em < 1 segundo.
    O processamento continua em background.
    """
    try:
        logger.info(f"📢 Nova campanha de remarketing: Bot {payload.bot_id}, Target: {payload.target}")
        
        # =========================================================
        # 1. VALIDAÇÃO DE TESTE
        # =========================================================
        if payload.is_test and not payload.specific_user_id:
            # Buscar último pedido para teste
            ultimo = db.query(Pedido).filter(
                Pedido.bot_id == payload.bot_id
            ).order_by(Pedido.id.desc()).first()
            
            if ultimo:
                payload.specific_user_id = ultimo.telegram_id
            else:
                # Fallback: Admin do bot
                admin = db.query(BotAdmin).filter(
                    BotAdmin.bot_id == payload.bot_id
                ).first()
                
                if admin:
                    payload.specific_user_id = admin.telegram_id
                else:
                    raise HTTPException(400, "Nenhum usuário encontrado para teste.")
        
        # =========================================================
        # 2. CRIAR REGISTRO DA CAMPANHA
        # =========================================================
        uuid_campanha = str(uuid.uuid4())
        nova_campanha = RemarketingCampaign(
            bot_id=payload.bot_id,
            campaign_id=uuid_campanha,
            type="teste" if payload.is_test else "massivo",
            target=payload.target,
            # Config com múltiplas chaves para retrocompatibilidade
            config=json.dumps({
                "mensagem": payload.mensagem,
                "msg": payload.mensagem,  # Compatibilidade com código antigo
                "media_url": payload.media_url,
                "media": payload.media_url,  # Compatibilidade
                "plano_oferta_id": getattr(payload, 'plano_oferta_id', None)
            }),
            status='agendado',  # Status inicial
            data_envio=datetime.utcnow(),
            total_leads=0,  # Será atualizado no background
            sent_success=0,
            blocked_count=0,
            plano_id=getattr(payload, 'plano_oferta_id', None),  # Se tiver oferta
            promo_price=None
        )
        db.add(nova_campanha)
        db.commit()
        db.refresh(nova_campanha)
        
        logger.info(f"✅ Campanha {nova_campanha.id} registrada no banco")
        
        # =========================================================
        # 3. SE FOR TESTE, ENVIA SÍNCRONO (1 MENSAGEM APENAS)
        # =========================================================
        if payload.is_test:
            try:
                bot_data = db.query(BotModel).filter(BotModel.id == payload.bot_id).first()
                if not bot_data:
                    raise HTTPException(404, "Bot não encontrado")
                
                bot = telebot.TeleBot(bot_data.token)
                target_id = int(payload.specific_user_id)
                
                # Enviar teste
                bot.send_message(target_id, payload.mensagem, parse_mode="HTML")
                
                # Atualizar campanha como concluída
                nova_campanha.status = 'concluido'
                nova_campanha.sent_success = 1
                nova_campanha.total_leads = 1
                db.commit()
                
                logger.info(f"✅ Teste enviado para {payload.specific_user_id}")
                
                return {
                    "status": "enviado",
                    "message": f"Teste enviado com sucesso para {payload.specific_user_id}!",
                    "campaign_id": nova_campanha.id
                }
                
            except Exception as e:
                logger.error(f"❌ Erro no teste: {e}")
                nova_campanha.status = 'erro'
                db.commit()
                raise HTTPException(500, f"Erro ao enviar teste: {str(e)}")
        
        # =========================================================
        # 4. SE FOR MASSIVO, AGENDAR BACKGROUND TASK
        # =========================================================
        background_tasks.add_task(
            processar_envio_massivo_background,  # ← Nome correto da função
            nova_campanha.id,
            payload.bot_id,
            payload.mensagem,
            payload.target,
            payload.media_url,
            getattr(payload, 'plano_oferta_id', None)
        )
        
        logger.info(f"🚀 Campanha {nova_campanha.id} agendada para background")
        
        # =========================================================
        # 5. RETORNAR IMEDIATAMENTE (< 1 segundo)
        # =========================================================
        return {
            "status": "enviando",
            "message": "Campanha iniciada! Acompanhe o progresso no histórico.",
            "campaign_id": nova_campanha.id
        }
        
    except HTTPException:
        # Re-lançar HTTPExceptions (são erros esperados)
        raise
    except Exception as e:
        logger.error(f"❌ Erro ao criar campanha: {e}")
        raise HTTPException(500, detail=str(e))


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
    bot_db = db.query(BotModel).filter(BotModel.id == payload.bot_id).first()
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
# =========================================================
# 📊 ROTA DE DASHBOARD V2 (COM FILTRO DE DATA E SUPORTE ADMIN)
# =========================================================
@app.get("/api/admin/dashboard/stats")
def dashboard_stats(
    bot_id: Optional[int] = None, 
    start_date: Optional[str] = None, 
    end_date: Optional[str] = None, 
    db: Session = Depends(get_db),
    current_user = Depends(get_current_user)
):
    """
    Dashboard com filtros de data e bot.
    
    🆕 LÓGICA ESPECIAL PARA SUPER ADMIN:
    - Se for super admin com split: calcula faturamento pelos splits (Taxas)
    - Se for usuário normal: calcula pelos próprios pedidos (Valor Bruto)
    
    ✅ CORREÇÃO: Retorna valores em CENTAVOS (frontend divide por 100)
    """
    try:
        # Converte datas
        if start_date:
            start = datetime.fromisoformat(start_date.replace('Z', '+00:00'))
        else:
            start = datetime.utcnow() - timedelta(days=30)
        
        if end_date:
            end = datetime.fromisoformat(end_date.replace('Z', '+00:00'))
        else:
            end = datetime.utcnow()
        
        logger.info(f"📊 Dashboard Stats - Período: {start.date()} a {end.date()}")
        
        # 🔥 VERIFICA SE É SUPER ADMIN COM SPLIT
        is_super_with_split = (
            current_user.is_superuser and 
            current_user.pushin_pay_id is not None and
            current_user.pushin_pay_id != ""
        )
        
        logger.info(f"📊 User: {current_user.username}, Super: {is_super_with_split}, Bot ID: {bot_id}")
        
        # ============================================
        # 🎯 DEFINE QUAIS BOTS BUSCAR
        # ============================================
        if bot_id:
            # Visão de bot único
            bot = db.query(BotModel).filter(
                BotModel.id == bot_id,
                # Admin vê qualquer bot, User só vê o seu
                (BotModel.owner_id == current_user.id) if not current_user.is_superuser else True
            ).first()
            
            if not bot:
                raise HTTPException(status_code=404, detail="Bot não encontrado")
            
            bots_ids = [bot_id]
            
        else:
            # Visão global
            if is_super_with_split:
                # Admin vê TUDO (mas vamos filtrar se precisar depois)
                # Para estatísticas de split, não precisamos filtrar bots específicos se for visão geral
                bots_ids = [] # Lista vazia sinaliza "todos" na lógica abaixo
            else:
                # Usuário vê SEUS bots
                user_bots = db.query(BotModel.id).filter(BotModel.owner_id == current_user.id).all()
                bots_ids = [b.id for b in user_bots]
        
        # Se for usuário comum e não tiver bots, retorna zeros
        if not is_super_with_split and not bots_ids and not bot_id:
            logger.info(f"📊 User {current_user.username}: Sem bots, retornando zeros")
            return {
                "total_revenue": 0,
                "active_users": 0,
                "sales_today": 0,
                "leads_mes": 0,
                "leads_hoje": 0,
                "ticket_medio": 0,
                "total_transacoes": 0,
                "reembolsos": 0,
                "taxa_conversao": 0,
                "chart_data": []
            }
        
        # ============================================
        # 💰 CÁLCULO DE FATURAMENTO DO PERÍODO
        # ============================================
        if is_super_with_split and not bot_id:
            # SUPER ADMIN (Visão Geral): Calcula pelos splits de TODAS as vendas da plataforma
            vendas_periodo = db.query(Pedido).filter(
                Pedido.status.in_(['approved', 'paid', 'active']),
                Pedido.data_aprovacao >= start,
                Pedido.data_aprovacao <= end
            ).all()
            
            # Faturamento = Quantidade de Vendas * Taxa Fixa (ex: 60 centavos)
            # Nota: Usamos a taxa configurada no perfil do admin como base
            taxa_centavos = current_user.taxa_venda or 60
            total_revenue = len(vendas_periodo) * taxa_centavos
            
            logger.info(f"💰 Super Admin - Período: {len(vendas_periodo)} vendas × R$ {taxa_centavos/100:.2f} = R$ {total_revenue/100:.2f} ({total_revenue} centavos)")
            
        else:
            # USUÁRIO NORMAL (ou Admin vendo bot específico): Soma valor total dos pedidos
            query = db.query(Pedido).filter(
                Pedido.status.in_(['approved', 'paid', 'active']),
                Pedido.data_aprovacao >= start,
                Pedido.data_aprovacao <= end
            )
            
            if bots_ids:
                query = query.filter(Pedido.bot_id.in_(bots_ids))
            
            vendas_periodo = query.all()
            
            # Se for admin vendo bot específico, ainda calcula como taxa ou valor cheio?
            # Geralmente admin quer ver o faturamento do cliente, então valor cheio.
            total_revenue = sum(int(p.valor * 100) if p.valor else 0 for p in vendas_periodo)
            
            logger.info(f"👤 User - Período: {len(vendas_periodo)} vendas = R$ {total_revenue/100:.2f} ({total_revenue} centavos)")
        
        # ============================================
        # 📊 OUTRAS MÉTRICAS
        # ============================================
        
        # Usuários ativos (assinaturas não expiradas)
        query_active = db.query(Pedido).filter(
            Pedido.status.in_(['approved', 'paid', 'active']),
            Pedido.data_expiracao > datetime.utcnow()
        )
        if not is_super_with_split or bot_id:
             if bots_ids: query_active = query_active.filter(Pedido.bot_id.in_(bots_ids))
        active_users = query_active.count()
        
        # Vendas de hoje
        hoje_start = datetime.utcnow().replace(hour=0, minute=0, second=0)
        query_hoje = db.query(Pedido).filter(
            Pedido.status.in_(['approved', 'paid', 'active']),
            Pedido.data_aprovacao >= hoje_start
        )
        if not is_super_with_split or bot_id:
            if bots_ids: query_hoje = query_hoje.filter(Pedido.bot_id.in_(bots_ids))
            
        vendas_hoje = query_hoje.all()
        
        if is_super_with_split and not bot_id:
            sales_today = len(vendas_hoje) * (current_user.taxa_venda or 60)
        else:
            sales_today = sum(int(p.valor * 100) if p.valor else 0 for p in vendas_hoje)
        
        # Leads do mês
        mes_start = datetime.utcnow().replace(day=1, hour=0, minute=0, second=0)
        query_leads_mes = db.query(Lead).filter(Lead.created_at >= mes_start)
        if not is_super_with_split or bot_id:
             if bots_ids: query_leads_mes = query_leads_mes.filter(Lead.bot_id.in_(bots_ids))
        leads_mes = query_leads_mes.count()
        
        # Leads de hoje
        query_leads_hoje = db.query(Lead).filter(Lead.created_at >= hoje_start)
        if not is_super_with_split or bot_id:
             if bots_ids: query_leads_hoje = query_leads_hoje.filter(Lead.bot_id.in_(bots_ids))
        leads_hoje = query_leads_hoje.count()
        
        # Ticket médio
        if vendas_periodo:
            if is_super_with_split and not bot_id:
                ticket_medio = (current_user.taxa_venda or 60) # Para admin, ticket médio é a taxa fixa
            else:
                ticket_medio = int(total_revenue / len(vendas_periodo))
        else:
            ticket_medio = 0
        
        # Total de transações
        total_transacoes = len(vendas_periodo)
        
        # Reembolsos (Placeholder)
        reembolsos = 0
        
        # Taxa de conversão
        if leads_mes > 0:
            taxa_conversao = round((total_transacoes / leads_mes) * 100, 2)
        else:
            taxa_conversao = 0
        
        # ============================================
        # 📈 DADOS DO GRÁFICO (AGRUPADO POR DIA)
        # ============================================
        chart_data = []
        current_date = start
        
        while current_date <= end:
            day_start = current_date.replace(hour=0, minute=0, second=0)
            day_end = current_date.replace(hour=23, minute=59, second=59)
            
            query_dia = db.query(Pedido).filter(
                Pedido.status.in_(['approved', 'paid', 'active']),
                Pedido.data_aprovacao >= day_start,
                Pedido.data_aprovacao <= day_end
            )
            
            if not is_super_with_split or bot_id:
                if bots_ids: query_dia = query_dia.filter(Pedido.bot_id.in_(bots_ids))
            
            vendas_dia = query_dia.all()
            
            if is_super_with_split and not bot_id:
                # Admin: Vendas * Taxa / 100 (para Reais)
                valor_dia = len(vendas_dia) * ((current_user.taxa_venda or 60) / 100)
            else:
                # User: Soma dos valores
                valor_dia = sum(p.valor for p in vendas_dia) if vendas_dia else 0
            
            chart_data.append({
                "name": current_date.strftime("%d/%m"),
                "value": round(valor_dia, 2)  # ✅ Em REAIS
            })
            
            current_date += timedelta(days=1)
        
        logger.info(f"📊 Retornando: revenue={total_revenue} centavos, active={active_users}, today={sales_today} centavos")
        
        return {
            "total_revenue": total_revenue,  # ✅ EM CENTAVOS
            "active_users": active_users,
            "sales_today": sales_today,  # ✅ EM CENTAVOS
            "leads_mes": leads_mes,
            "leads_hoje": leads_hoje,
            "ticket_medio": ticket_medio,  # ✅ EM CENTAVOS
            "total_transacoes": total_transacoes,
            "reembolsos": reembolsos,
            "taxa_conversao": taxa_conversao,
            "chart_data": chart_data  # ✅ EM REAIS
        }
        
    except Exception as e:
        logger.error(f"❌ Erro ao buscar stats do dashboard: {e}")
        import traceback
        logger.error(traceback.format_exc())
        raise HTTPException(status_code=500, detail=f"Erro ao buscar estatísticas: {str(e)}")

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
                    bot_db = db.query(BotModel).filter(BotModel.id == p.bot_id).first()
                    
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
                        bot_data = db.query(BotModel).filter(BotModel.id == p.bot_id).first()
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
# 👤 ENDPOINT ESPECÍFICO PARA STATS DO PERFIL (🆕)
# =========================================================
# =========================================================
# 👤 ENDPOINT ESPECÍFICO PARA STATS DO PERFIL (🆕)
# =========================================================
# =========================================================
# 👤 ENDPOINT ESPECÍFICO PARA STATS DO PERFIL (🆕)
# =========================================================
@app.get("/api/profile/stats")
def get_profile_stats(
    db: Session = Depends(get_db),
    current_user = Depends(get_current_user)
):
    """
    Retorna estatísticas do perfil do usuário logado.
    
    🆕 LÓGICA ESPECIAL PARA SUPER ADMIN:
    - Se for super admin: calcula faturamento pelos splits (Todas as vendas * taxa)
    - Se for usuário normal: calcula pelos próprios pedidos
    """
    try:
        # 👇 CORREÇÃO CRÍTICA: IMPORTAR OS MODELOS (User estava faltando)
        from database import User, Bot, Pedido, Lead

        user_id = current_user.id
        
        # 🔥 LÓGICA FLEXÍVEL: BASTA SER SUPERUSER PARA VER OS DADOS GLOBAIS
        # (Não exige mais o ID preenchido para visualizar, apenas para sacar)
        is_super_with_split = current_user.is_superuser
        
        logger.info(f"📊 Profile Stats - User: {current_user.username}, Super: {is_super_with_split}")
        
        if is_super_with_split:
            # ============================================
            # 💰 CÁLCULO ESPECIAL PARA SUPER ADMIN (SPLIT)
            # ============================================
            
            # 1. Conta TODAS as vendas aprovadas da PLATAFORMA INTEIRA
            total_vendas_sistema = db.query(Pedido).filter(
                Pedido.status.in_(['approved', 'paid', 'active'])
            ).count()
            
            # 2. Calcula faturamento: vendas × taxa (em centavos)
            taxa_centavos = current_user.taxa_venda or 60
            total_revenue = total_vendas_sistema * taxa_centavos
            
            # 3. Total de sales = todas as vendas do sistema
            total_sales = total_vendas_sistema
            
            logger.info(f"💰 Super Admin {current_user.username}: {total_vendas_sistema} vendas × R$ {taxa_centavos/100:.2f} = R$ {total_revenue/100:.2f} (retornando {total_revenue} centavos)")
            
            # Total de bots da plataforma (Visão Macro)
            total_bots = db.query(BotModel).count()
            
            # Total de membros da plataforma (AGORA VAI FUNCIONAR POIS IMPORTAMOS 'User')
            total_members = db.query(User).count()
            
        else:
            # ============================================
            # 👤 CÁLCULO NORMAL PARA USUÁRIO COMUM
            # ============================================
            
            # Busca todos os bots do usuário
            user_bots = db.query(BotModel.id).filter(BotModel.owner_id == user_id).all()
            bots_ids = [bot.id for bot in user_bots]
            
            if not bots_ids:
                logger.info(f"👤 User {current_user.username}: Sem bots, retornando zeros")
                return {
                    "total_bots": 0,
                    "total_members": 0,
                    "total_revenue": 0,
                    "total_sales": 0
                }
            
            # Soma pedidos aprovados dos bots do usuário
            pedidos_aprovados = db.query(Pedido).filter(
                Pedido.bot_id.in_(bots_ids),
                Pedido.status.in_(['approved', 'paid', 'active'])
            ).all()
            
            # Calcula revenue em centavos
            total_revenue = sum(int(p.valor * 100) if p.valor else 0 for p in pedidos_aprovados)
            total_sales = len(pedidos_aprovados)
            
            logger.info(f"👤 User {current_user.username}: {total_sales} vendas = R$ {total_revenue/100:.2f} (retornando {total_revenue} centavos)")
            
            # Total de bots do usuário
            total_bots = len(bots_ids)
            
            # Total de membros dos bots dele
            total_leads = db.query(Lead).filter(Lead.bot_id.in_(bots_ids)).count()
            total_pedidos_unicos = db.query(Pedido.telegram_id).filter(Pedido.bot_id.in_(bots_ids)).distinct().count()
            total_members = total_leads + total_pedidos_unicos
        
        logger.info(f"📊 Retornando: bots={total_bots}, members={total_members}, revenue={total_revenue}, sales={total_sales}")
        
        return {
            "total_bots": total_bots,
            "total_members": total_members,
            "total_revenue": total_revenue,  # ✅ EM CENTAVOS (frontend divide por 100)
            "total_sales": total_sales
        }
        
    except Exception as e:
        logger.error(f"❌ Erro ao buscar stats do perfil: {e}")
        import traceback
        logger.error(traceback.format_exc())
        raise HTTPException(status_code=500, detail=f"Erro ao buscar estatísticas: {str(e)}")

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
        user_bots = db.query(BotModel).filter(BotModel.owner_id == current_user.id).all()
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
# 📋 ROTA DE CONSULTA DE AUDIT LOGS (🆕 FASE 3.3)
# =========================================================
class AuditLogFilters(BaseModel):
    user_id: Optional[int] = None
    action: Optional[str] = None
    resource_type: Optional[str] = None
    success: Optional[bool] = None
    start_date: Optional[str] = None
    end_date: Optional[str] = None
    page: int = 1
    per_page: int = 50

@app.get("/api/admin/audit-logs")
def get_audit_logs(
    user_id: Optional[int] = None,
    action: Optional[str] = None,
    resource_type: Optional[str] = None,
    success: Optional[bool] = None,
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
    page: int = 1,
    per_page: int = 50,
    db: Session = Depends(get_db),
    current_user = Depends(get_current_user)
):
    """
    Retorna logs de auditoria com filtros opcionais
    
    Filtros disponíveis:
    - user_id: ID do usuário
    - action: Tipo de ação (ex: "bot_created", "login_success")
    - resource_type: Tipo de recurso (ex: "bot", "plano", "auth")
    - success: true/false (apenas ações bem-sucedidas ou falhas)
    - start_date: Data inicial (ISO format)
    - end_date: Data final (ISO format)
    - page: Página atual (padrão: 1)
    - per_page: Logs por página (padrão: 50, máx: 100)
    """
    try:
        # Limita per_page a 100
        if per_page > 100:
            per_page = 100
        
        # Query base
        query = db.query(AuditLog)
        
        # 🔒 IMPORTANTE: Se não for superusuário, só mostra logs do próprio usuário
        if not current_user.is_superuser:
            query = query.filter(AuditLog.user_id == current_user.id)
        
        # Aplica filtros
        if user_id is not None:
            query = query.filter(AuditLog.user_id == user_id)
        
        if action:
            query = query.filter(AuditLog.action == action)
        
        if resource_type:
            query = query.filter(AuditLog.resource_type == resource_type)
        
        if success is not None:
            query = query.filter(AuditLog.success == success)
        
        if start_date:
            try:
                start = datetime.fromisoformat(start_date.replace('Z', '+00:00'))
                query = query.filter(AuditLog.created_at >= start)
            except:
                pass
        
        if end_date:
            try:
                end = datetime.fromisoformat(end_date.replace('Z', '+00:00'))
                query = query.filter(AuditLog.created_at <= end)
            except:
                pass
        
        # Total de registros
        total = query.count()
        
        # Paginação
        offset = (page - 1) * per_page
        logs = query.order_by(AuditLog.created_at.desc()).offset(offset).limit(per_page).all()
        
        # Formata resposta
        logs_data = []
        for log in logs:
            # Parse JSON details se existir
            details_parsed = None
            if log.details:
                try:
                    import json
                    details_parsed = json.loads(log.details)
                except:
                    details_parsed = log.details
            
            logs_data.append({
                "id": log.id,
                "user_id": log.user_id,
                "username": log.username,
                "action": log.action,
                "resource_type": log.resource_type,
                "resource_id": log.resource_id,
                "description": log.description,
                "details": details_parsed,
                "ip_address": log.ip_address,
                "user_agent": log.user_agent,
                "success": log.success,
                "error_message": log.error_message,
                "created_at": log.created_at.isoformat() if log.created_at else None
            })
        
        return {
            "data": logs_data,
            "total": total,
            "page": page,
            "per_page": per_page,
            "total_pages": (total + per_page - 1) // per_page
        }
        
    except Exception as e:
        logger.error(f"Erro ao buscar audit logs: {e}")
        raise HTTPException(status_code=500, detail="Erro ao buscar logs de auditoria")

# =========================================================
# 👑 ROTAS SUPER ADMIN (🆕 FASE 3.4)
# =========================================================

@app.get("/api/superadmin/stats")
def get_superadmin_stats(
    db: Session = Depends(get_db),
    current_superuser = Depends(get_current_superuser)
):
    """
    👑 Painel Super Admin - Estatísticas globais do sistema
    
    🆕 ADICIONA FATURAMENTO DO SUPER ADMIN (SPLITS)
    """
    try:
        from database import User
        
        # ============================================
        # 📊 ESTATÍSTICAS GERAIS DO SISTEMA
        # ============================================
        
        # Total de usuários
        total_users = db.query(User).count()
        active_users = db.query(User).filter(User.is_active == True).count()
        inactive_users = total_users - active_users
        
        # Total de bots
        total_bots = db.query(BotModel).count()
        active_bots = db.query(BotModel).filter(BotModel.status == 'ativo').count()
        inactive_bots = total_bots - active_bots
        
        # Receita total do sistema
        todas_vendas = db.query(Pedido).filter(
            Pedido.status.in_(['approved', 'paid', 'active'])
        ).all()
        
        total_revenue = sum(int(p.valor * 100) for p in todas_vendas)
        total_sales = len(todas_vendas)
        
        # Ticket médio do sistema
        avg_ticket = int(total_revenue / total_sales) if total_sales > 0 else 0
        
        # ============================================
        # 💰 FATURAMENTO DO SUPER ADMIN (SPLITS)
        # ============================================
        taxa_super_admin = current_superuser.taxa_venda or 60
        super_admin_revenue = total_sales * taxa_super_admin
        
        logger.info(f"👑 Super Admin Revenue: {total_sales} vendas × R$ {taxa_super_admin/100:.2f} = R$ {super_admin_revenue/100:.2f}")
        
        # ============================================
        # 📈 USUÁRIOS RECENTES
        # ============================================
        recent_users = db.query(User).order_by(
            desc(User.created_at)
        ).limit(5).all()
        
        recent_users_data = []
        for u in recent_users:
            user_bots = db.query(BotModel).filter(BotModel.owner_id == u.id).count()
            user_sales = db.query(Pedido).filter(
                Pedido.bot_id.in_([b.id for b in u.bots]),
                Pedido.status.in_(['approved', 'paid'])
            ).count()
            
            recent_users_data.append({
                "id": u.id,
                "username": u.username,
                "email": u.email,
                "total_bots": user_bots,
                "total_sales": user_sales,
                "created_at": u.created_at.isoformat() if u.created_at else None
            })
        
        # ============================================
        # 📅 NOVOS USUÁRIOS (30 DIAS)
        # ============================================
        thirty_days_ago = datetime.utcnow() - timedelta(days=30)
        new_users_count = db.query(User).filter(
            User.created_at >= thirty_days_ago
        ).count()
        
        # Cálculo de crescimento
        if total_users > 0:
            growth_percentage = round((new_users_count / total_users) * 100, 2)
        else:
            growth_percentage = 0
        
        return {
            # Sistema
            "total_users": total_users,
            "active_users": active_users,
            "inactive_users": inactive_users,
            "total_bots": total_bots,
            "active_bots": active_bots,
            "inactive_bots": inactive_bots,
            
            # Financeiro (Sistema)
            "total_revenue": total_revenue,  # centavos
            "total_sales": total_sales,
            "avg_ticket": avg_ticket,  # centavos
            
            # 🆕 Financeiro (Super Admin)
            "super_admin_revenue": super_admin_revenue,  # centavos
            "super_admin_sales": total_sales,
            "super_admin_rate": taxa_super_admin,  # centavos
            
            # Crescimento
            "new_users_30d": new_users_count,
            "growth_percentage": growth_percentage,
            
            # Dados extras
            "recent_users": recent_users_data
        }
        
    except Exception as e:
        logger.error(f"Erro ao buscar stats super admin: {e}")
        raise HTTPException(status_code=500, detail="Erro ao buscar estatísticas")

@app.get("/api/superadmin/users")
def list_all_users(
    page: int = 1,
    per_page: int = 50,
    search: str = None,
    status: str = None,
    db: Session = Depends(get_db),
    current_superuser = Depends(get_current_superuser)
):
    """
    Lista todos os usuários do sistema (apenas super-admin)
    
    Filtros:
    - search: Busca por username, email ou nome completo
    - status: "active" ou "inactive"
    - page: Página atual (padrão: 1)
    - per_page: Usuários por página (padrão: 50, máx: 100)
    """
    try:
        from database import User
        
        # Limita per_page a 100
        if per_page > 100:
            per_page = 100
        
        # Query base
        query = db.query(User)
        
        # Filtro de busca
        if search:
            search_filter = f"%{search}%"
            query = query.filter(
                (User.username.ilike(search_filter)) |
                (User.email.ilike(search_filter)) |
                (User.full_name.ilike(search_filter))
            )
        
        # Filtro de status
        if status == "active":
            query = query.filter(User.is_active == True)
        elif status == "inactive":
            query = query.filter(User.is_active == False)
        
        # Total de registros
        total = query.count()
        
        # Paginação
        offset = (page - 1) * per_page
        users = query.order_by(User.created_at.desc()).offset(offset).limit(per_page).all()
        
        # Formata resposta com estatísticas de cada usuário
        users_data = []
        for user in users:
            # Busca bots do usuário
            user_bots = db.query(BotModel).filter(BotModel.owner_id == user.id).all()
            bot_ids = [b.id for b in user_bots]
            
            # Calcula receita e vendas
            user_revenue = 0.0
            user_sales = 0
            
            if bot_ids:
                user_revenue = db.query(func.sum(Pedido.valor)).filter(
                    Pedido.bot_id.in_(bot_ids),
                    Pedido.status == 'approved'
                ).scalar() or 0.0
                
                user_sales = db.query(Pedido).filter(
                    Pedido.bot_id.in_(bot_ids),
                    Pedido.status == 'approved'
                ).count()
            
            users_data.append({
                "id": user.id,
                "username": user.username,
                "email": user.email,
                "full_name": user.full_name,
                "is_active": user.is_active,
                "is_superuser": user.is_superuser,
                "created_at": user.created_at.isoformat() if user.created_at else None,
                "total_bots": len(user_bots),
                "total_revenue": float(user_revenue),
                "total_sales": user_sales
            })
        
        return {
            "data": users_data,
            "total": total,
            "page": page,
            "per_page": per_page,
            "total_pages": (total + per_page - 1) // per_page
        }
        
    except Exception as e:
        logger.error(f"Erro ao listar usuários: {e}")
        raise HTTPException(status_code=500, detail="Erro ao listar usuários")

@app.get("/api/superadmin/users/{user_id}")
def get_user_details(
    user_id: int,
    db: Session = Depends(get_db),
    current_superuser = Depends(get_current_superuser)
):
    """
    Retorna detalhes completos de um usuário específico (apenas super-admin)
    
    Inclui:
    - Dados básicos do usuário
    - Lista de bots do usuário
    - Estatísticas de receita e vendas
    - Últimas ações de auditoria
    """
    try:
        from database import User
        
        # Busca o usuário
        user = db.query(User).filter(User.id == user_id).first()
        
        if not user:
            raise HTTPException(status_code=404, detail="Usuário não encontrado")
        
        # Busca bots do usuário
        user_bots = db.query(BotModel).filter(BotModel.owner_id == user.id).all()
        bot_ids = [b.id for b in user_bots]
        
        # Calcula estatísticas
        user_revenue = 0.0
        user_sales = 0
        total_leads = 0
        
        if bot_ids:
            user_revenue = db.query(func.sum(Pedido.valor)).filter(
                Pedido.bot_id.in_(bot_ids),
                Pedido.status == 'approved'
            ).scalar() or 0.0
            
            user_sales = db.query(Pedido).filter(
                Pedido.bot_id.in_(bot_ids),
                Pedido.status == 'approved'
            ).count()
            
            total_leads = db.query(Lead).filter(Lead.bot_id.in_(bot_ids)).count()
        
        # Últimas ações de auditoria (últimas 10)
        recent_logs = db.query(AuditLog).filter(
            AuditLog.user_id == user_id
        ).order_by(AuditLog.created_at.desc()).limit(10).all()
        
        logs_data = []
        for log in recent_logs:
            logs_data.append({
                "id": log.id,
                "action": log.action,
                "resource_type": log.resource_type,
                "description": log.description,
                "success": log.success,
                "created_at": log.created_at.isoformat() if log.created_at else None
            })
        
        # Formata dados dos bots
        bots_data = []
        for bot in user_bots:
            bot_revenue = db.query(func.sum(Pedido.valor)).filter(
                Pedido.bot_id == bot.id,
                Pedido.status == 'approved'
            ).scalar() or 0.0
            
            bot_sales = db.query(Pedido).filter(
                Pedido.bot_id == bot.id,
                Pedido.status == 'approved'
            ).count()
            
            bots_data.append({
                "id": bot.id,
                "nome": bot.nome,
                "username": bot.username,
                "status": bot.status,
                "created_at": bot.created_at.isoformat() if bot.created_at else None,
                "revenue": float(bot_revenue),
                "sales": bot_sales
            })
        
        return {
            "user": {
                "id": user.id,
                "username": user.username,
                "email": user.email,
                "full_name": user.full_name,
                "is_active": user.is_active,
                "is_superuser": user.is_superuser,
                "created_at": user.created_at.isoformat() if user.created_at else None
            },
            "stats": {
                "total_bots": len(user_bots),
                "total_revenue": float(user_revenue),
                "total_sales": user_sales,
                "total_leads": total_leads
            },
            "bots": bots_data,
            "recent_activity": logs_data
        }
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Erro ao buscar detalhes do usuário: {e}")
        raise HTTPException(status_code=500, detail="Erro ao buscar detalhes")

@app.put("/api/superadmin/users/{user_id}/status")
def update_user_status(
    user_id: int,
    status_data: UserStatusUpdate,
    request: Request,
    db: Session = Depends(get_db),
    current_superuser = Depends(get_current_superuser)
):
    """
    Ativa ou desativa um usuário (apenas super-admin)
    
    Quando um usuário é desativado:
    - Não pode fazer login
    - Seus bots permanecem no sistema
    - Pode ser reativado posteriormente
    """
    try:
        from database import User
        
        # Busca o usuário
        user = db.query(User).filter(User.id == user_id).first()
        
        if not user:
            raise HTTPException(status_code=404, detail="Usuário não encontrado")
        
        # Não permite desativar a si mesmo
        if user.id == current_superuser.id:
            raise HTTPException(
                status_code=400, 
                detail="Você não pode desativar sua própria conta"
            )
        
        # Guarda status antigo
        old_status = user.is_active
        
        # Atualiza status
        user.is_active = status_data.is_active
        db.commit()
        
        # 📋 AUDITORIA: Mudança de status
        action = "user_activated" if status_data.is_active else "user_deactivated"
        description = f"{'Ativou' if status_data.is_active else 'Desativou'} usuário '{user.username}'"
        
        log_action(
            db=db,
            user_id=current_superuser.id,
            username=current_superuser.username,
            action=action,
            resource_type="user",
            resource_id=user.id,
            description=description,
            details={
                "target_user": user.username,
                "old_status": old_status,
                "new_status": status_data.is_active
            },
            ip_address=get_client_ip(request),
            user_agent=request.headers.get("user-agent")
        )
        
        logger.info(f"👑 Super-admin {current_superuser.username} {'ativou' if status_data.is_active else 'desativou'} usuário {user.username}")
        
        return {
            "status": "success",
            "message": f"Usuário {'ativado' if status_data.is_active else 'desativado'} com sucesso",
            "user": {
                "id": user.id,
                "username": user.username,
                "is_active": user.is_active
            }
        }
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Erro ao atualizar status do usuário: {e}")
        raise HTTPException(status_code=500, detail="Erro ao atualizar status")

# 👇 COLE ISSO NA SEÇÃO DE ROTAS DO SUPER ADMIN

# 🆕 ROTA PARA O SUPER ADMIN EDITAR DADOS FINANCEIROS DOS MEMBROS
# 🆕 ROTA PARA O SUPER ADMIN EDITAR DADOS FINANCEIROS DOS MEMBROS
# 🆕 ROTA PARA O SUPER ADMIN EDITAR DADOS FINANCEIROS DOS MEMBROS
@app.put("/api/superadmin/users/{user_id}")
def update_user_financials(
    user_id: int, 
    user_data: PlatformUserUpdate, 
    current_user = Depends(get_current_superuser), # Já corrigimos o nome aqui antes
    db: Session = Depends(get_db)
):
    # 👇 A CORREÇÃO MÁGICA ESTÁ AQUI TAMBÉM:
    from database import User

    user = db.query(User).filter(User.id == user_id).first()
    if not user:
        raise HTTPException(status_code=404, detail="Usuário não encontrado")
        
    if user_data.full_name:
        user.full_name = user_data.full_name
    if user_data.email:
        user.email = user_data.email
    if user_data.pushin_pay_id is not None:
        user.pushin_pay_id = user_data.pushin_pay_id
    # 👑 Só o Admin pode mudar a taxa que o membro paga
    if user_data.taxa_venda is not None:
        user.taxa_venda = user_data.taxa_venda
        
    db.commit()
    return {"status": "success", "message": "Dados financeiros do usuário atualizados"}

@app.delete("/api/superadmin/users/{user_id}")
def delete_user(
    user_id: int,
    request: Request,
    db: Session = Depends(get_db),
    current_superuser = Depends(get_current_superuser)
):
    """
    Deleta um usuário e todos os seus dados (apenas super-admin)
    
    ⚠️ ATENÇÃO: Esta ação é IRREVERSÍVEL!
    
    O que é deletado:
    - Usuário
    - Todos os bots do usuário (CASCADE)
    - Todos os planos dos bots
    - Todos os pedidos dos bots
    - Todos os leads dos bots
    - Todos os logs de auditoria do usuário
    """
    try:
        from database import User
        
        # Busca o usuário
        user = db.query(User).filter(User.id == user_id).first()
        
        if not user:
            raise HTTPException(status_code=404, detail="Usuário não encontrado")
        
        # Não permite deletar a si mesmo
        if user.id == current_superuser.id:
            raise HTTPException(
                status_code=400, 
                detail="Você não pode deletar sua própria conta"
            )
        
        # Não permite deletar outro super-admin
        if user.is_superuser:
            raise HTTPException(
                status_code=400, 
                detail="Não é possível deletar outro super-administrador"
            )
        
        # Guarda informações para o log
        username = user.username
        email = user.email
        total_bots = db.query(BotModel).filter(BotModel.owner_id == user.id).count()
        
        # Deleta o usuário (CASCADE vai deletar todos os relacionamentos)
        db.delete(user)
        db.commit()
        
        # 📋 AUDITORIA: Deleção de usuário
        log_action(
            db=db,
            user_id=current_superuser.id,
            username=current_superuser.username,
            action="user_deleted",
            resource_type="user",
            resource_id=user_id,
            description=f"Deletou usuário '{username}' e todos os seus dados",
            details={
                "deleted_user": username,
                "deleted_email": email,
                "total_bots_deleted": total_bots
            },
            ip_address=get_client_ip(request),
            user_agent=request.headers.get("user-agent")
        )
        
        logger.warning(f"👑 Super-admin {current_superuser.username} DELETOU usuário {username} (ID: {user_id})")
        
        return {
            "status": "success",
            "message": f"Usuário '{username}' e todos os seus dados foram deletados",
            "deleted": {
                "username": username,
                "email": email,
                "total_bots": total_bots
            }
        }
        
    except HTTPException:
        raise
    except Exception as e:
        db.rollback()
        logger.error(f"Erro ao deletar usuário: {e}")
        raise HTTPException(status_code=500, detail="Erro ao deletar usuário")

@app.put("/api/superadmin/users/{user_id}/promote")
def promote_user_to_superadmin(
    user_id: int,
    promote_data: UserPromote,
    request: Request,
    db: Session = Depends(get_db),
    current_superuser = Depends(get_current_superuser)
):
    """
    Promove ou rebaixa um usuário de/para super-admin (apenas super-admin)
    
    ⚠️ CUIDADO: Super-admins têm acesso total ao sistema
    """
    try:
        from database import User
        
        # Busca o usuário
        user = db.query(User).filter(User.id == user_id).first()
        
        if not user:
            raise HTTPException(status_code=404, detail="Usuário não encontrado")
        
        # Não permite alterar o próprio status
        if user.id == current_superuser.id:
            raise HTTPException(
                status_code=400, 
                detail="Você não pode alterar seu próprio status de super-admin"
            )
        
        # Guarda status antigo
        old_status = user.is_superuser
        
        # Atualiza status de super-admin
        user.is_superuser = promote_data.is_superuser
        db.commit()
        
        # 📋 AUDITORIA: Promoção/Rebaixamento
        action = "user_promoted_superadmin" if promote_data.is_superuser else "user_demoted_superadmin"
        description = f"{'Promoveu' if promote_data.is_superuser else 'Rebaixou'} usuário '{user.username}' {'para' if promote_data.is_superuser else 'de'} super-admin"
        
        log_action(
            db=db,
            user_id=current_superuser.id,
            username=current_superuser.username,
            action=action,
            resource_type="user",
            resource_id=user.id,
            description=description,
            details={
                "target_user": user.username,
                "old_superuser_status": old_status,
                "new_superuser_status": promote_data.is_superuser
            },
            ip_address=get_client_ip(request),
            user_agent=request.headers.get("user-agent")
        )
        
        logger.warning(f"👑 Super-admin {current_superuser.username} {'PROMOVEU' if promote_data.is_superuser else 'REBAIXOU'} usuário {user.username}")
        
        return {
            "status": "success",
            "message": f"Usuário {'promovido a' if promote_data.is_superuser else 'rebaixado de'} super-admin com sucesso",
            "user": {
                "id": user.id,
                "username": user.username,
                "is_superuser": user.is_superuser
            }
        }
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Erro ao promover/rebaixar usuário: {e}")
        raise HTTPException(status_code=500, detail="Erro ao alterar status de super-admin")

# =========================================================
# 🔔 ROTAS DE NOTIFICAÇÕES
# =========================================================
# =========================================================
# 🔔 ROTAS DE NOTIFICAÇÕES (CORRIGIDO)
# =========================================================
@app.get("/api/notifications")
def get_notifications(
    limit: int = 20, 
    db: Session = Depends(get_db),
    current_user = Depends(get_current_user) # <--- CORRIGIDO AQUI
):
    """Retorna as notificações do usuário logado"""
    notifs = db.query(Notification).filter(
        Notification.user_id == current_user.id
    ).order_by(desc(Notification.created_at)).limit(limit).all()
    
    # Conta não lidas
    unread_count = db.query(Notification).filter(
        Notification.user_id == current_user.id,
        Notification.read == False
    ).count()
    
    return {
        "notifications": notifs,
        "unread_count": unread_count
    }

@app.put("/api/notifications/read-all")
def mark_all_read(
    db: Session = Depends(get_db),
    current_user = Depends(get_current_user) # <--- CORRIGIDO AQUI
):
    """Marca todas como lidas"""
    db.query(Notification).filter(
        Notification.user_id == current_user.id,
        Notification.read == False
    ).update({"read": True})
    
    db.commit()
    return {"status": "ok"}

@app.put("/api/notifications/{notif_id}/read")
def mark_one_read(
    notif_id: int,
    db: Session = Depends(get_db),
    current_user = Depends(get_current_user) # <--- CORRIGIDO AQUI
):
    """Marca uma específica como lida"""
    notif = db.query(Notification).filter(
        Notification.id == notif_id,
        Notification.user_id == current_user.id
    ).first()
    
    if notif:
        notif.read = True
        db.commit()
    
    return {"status": "ok"}

# ========================================================================
# ENDPOINTS PÚBLICOS PARA LANDING PAGE
# ========================================================================

# ========================================================================
# ENDPOINTS PÚBLICOS PARA LANDING PAGE - CORRIGIDOS
# ========================================================================

@app.get("/api/public/activity-feed")
def get_public_activity_feed(db: Session = Depends(get_db)):
    """
    Retorna atividades recentes (últimas 20) para exibir na landing page
    SEM dados sensíveis (IDs de telegram ocultos, nomes parciais)
    """
    try:
        # Import local para evitar erro de referência circular ou 'not defined'
        from database import Pedido
        
        # Busca últimos 20 pedidos aprovados usando ORM
        pedidos = db.query(Pedido).filter(
            Pedido.status.in_(['approved', 'paid', 'active', 'expired'])
        ).order_by(desc(Pedido.created_at)).limit(20).all()
        
        # Lista de nomes fictícios para privacidade
        fake_names = [
            "João P.", "Maria S.", "Carlos A.", "Ana C.", "Lucas F.",
            "Patricia M.", "Rafael L.", "Julia O.", "Bruno N.", "Fernanda R.",
            "Diego T.", "Amanda B.", "Ricardo G.", "Camila V.", "Felipe H.",
            "Juliana K.", "Marcos E.", "Beatriz D.", "Gustavo W.", "Larissa Q."
        ]
        
        activities = []
        for idx, row in enumerate(pedidos):
            # Usa um nome da lista de forma cíclica
            name = fake_names[idx % len(fake_names)]
            
            # Define ação baseada no status
            if row.status in ['approved', 'active', 'paid']:
                action = 'ADICIONADO'
                icon = '✅'
            else:
                action = 'REMOVIDO'
                icon = '❌'
            
            activities.append({
                "name": name,
                "plan": row.plano_nome or "Plano VIP",
                "price": float(row.valor) if row.valor else 0.0,
                "action": action,
                "icon": icon,
                "timestamp": row.created_at.isoformat() if row.created_at else None
            })
        
        return {"activities": activities}
        
    except Exception as e:
        logger.error(f"❌ Erro ao buscar feed de atividades: {e}")
        return {"activities": []}

@app.get("/api/public/stats")
def get_public_platform_stats(db: Session = Depends(get_db)):
    """
    Retorna estatísticas gerais da plataforma (números públicos)
    """
    try:
        # Import local para garantir acesso aos modelos
        from database import Bot, Pedido
        
        # Conta total de bots criados (Ativos)
        total_bots = db.query(BotModel).filter(BotModel.status == 'ativo').count()
        
        # Conta total de pedidos aprovados
        total_sales = db.query(Pedido).filter(
            Pedido.status.in_(['approved', 'active', 'paid'])
        ).count()
        
        # Soma receita total processada
        total_revenue = db.query(func.sum(Pedido.valor)).filter(
            Pedido.status.in_(['approved', 'active', 'paid'])
        ).scalar()
        
        # Conta usuários ativos (Donos de Bots ativos)
        active_users = db.query(BotModel.owner_id).filter(
            BotModel.status == 'ativo'
        ).distinct().count()
        
        return {
            "total_bots": int(total_bots or 0),
            "total_sales": int(total_sales or 0),
            "total_revenue": float(total_revenue or 0.0),
            "active_users": int(active_users or 0)
        }
        
    except Exception as e:
        logger.error(f"❌ Erro ao buscar estatísticas públicas: {e}")
        return {
            "total_bots": 0,
            "total_sales": 0,
            "total_revenue": 0.0,
            "active_users": 0
        }
# =========================================================
# ⚙️ STARTUP OTIMIZADA (SEM MIGRAÇÕES REPETIDAS)
# =========================================================
@app.on_event("startup")
def on_startup():
    print("="*60)
    print("🚀 INICIANDO ZENYX GBOT SAAS")
    print("="*60)
    
    # 1. Cria tabelas básicas se não existirem
    try:
        print("📊 Inicializando banco de dados...")
        init_db()
        print("✅ Banco de dados inicializado")
    except Exception as e:
        logger.error(f"❌ ERRO CRÍTICO no init_db: {e}")
        import traceback
        traceback.print_exc()
        # NÃO pare a aplicação aqui, continue tentando
    
    # 2. Executa migrações existentes (COM FALLBACK)
    try:
        print("🔄 Executando migrações...")
        
        # Tenta cada migração individualmente
        try:
            executar_migracao_v3()
            print("✅ Migração v3 OK")
        except Exception as e:
            logger.warning(f"⚠️ Migração v3 falhou: {e}")
        
        try:
            executar_migracao_v4()
            print("✅ Migração v4 OK")
        except Exception as e:
            logger.warning(f"⚠️ Migração v4 falhou: {e}")
        
        try:
            executar_migracao_v5()
            print("✅ Migração v5 OK")
        except Exception as e:
            logger.warning(f"⚠️ Migração v5 falhou: {e}")
        
        try:
            executar_migracao_v6()
            print("✅ Migração v6 OK")
        except Exception as e:
            logger.warning(f"⚠️ Migração v6 falhou: {e}")
            
    except Exception as e:
        logger.error(f"❌ Erro geral nas migrações: {e}")
    
    # 3. Executa migração de Audit Logs (COM FALLBACK)
    try:
        print("📋 Configurando Audit Logs...")
        from migration_audit_logs import executar_migracao_audit_logs
        executar_migracao_audit_logs()
        print("✅ Audit Logs configurado")
    except ImportError:
        logger.warning("⚠️ Arquivo migration_audit_logs.py não encontrado")
    except Exception as e:
        logger.error(f"⚠️ Erro na migração Audit Logs: {e}")
    
    # 4. Configura pushin_pay_id (COM FALLBACK ROBUSTO)
    try:
        print("💳 Configurando sistema de pagamento...")
        db = SessionLocal()
        try:
            config = db.query(SystemConfig).filter(
                SystemConfig.key == "pushin_plataforma_id"
            ).first()
            
            if not config:
                config = SystemConfig(
                    key="pushin_plataforma_id",
                    value=""
                )
                db.add(config)
                db.commit()
                print("✅ Configuração de pagamento criada")
            else:
                print("✅ Configuração de pagamento encontrada")
        finally:
            db.close()
    except Exception as e:
        logger.warning(f"⚠️ Erro ao configurar pushin_pay_id: {e}")
    
    print("="*60)
    print("✅ SISTEMA INICIADO E PRONTO!")
    print("="*60)

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
        bots = db.query(BotModel).all()
        
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

# =========================================================
# 💀 CRON JOB: REMOVEDOR DE USUÁRIOS VENCIDOS
# =========================================================
@app.get("/cron/check-expired")
def cron_check_expired(db: Session = Depends(get_db)):
    """
    Roda periodicamente para remover usuários com acesso vencido.
    Deve ser chamado por um Cron Job externo (ex: Railway Cron ou EasyCron).
    """
    logger.info("💀 Iniciando verificação de vencidos...")
    now = datetime.utcnow()
    
    # 1. Busca pedidos aprovados que JÁ venceram (data_expiracao < agora)
    vencidos = db.query(Pedido).filter(
        Pedido.status.in_(['approved', 'active']), # Apenas ativos
        Pedido.data_expiracao != None,
        Pedido.data_expiracao < now
    ).all()
    
    removidos = 0
    erros = 0
    
    for pedido in vencidos:
        try:
            bot_data = db.query(BotModel).filter(BotModel.id == pedido.bot_id).first()
            if not bot_data: continue
            
            # Conecta no Telegram (Sem threads para evitar erro)
            tb = telebot.TeleBot(bot_data.token, threaded=False)
            
            # Identifica o Canal
            canal_id = bot_data.id_canal_vip
            if str(canal_id).replace("-","").isdigit(): canal_id = int(str(canal_id).strip())
            
            # --- AÇÃO DE REMOÇÃO ---
            try:
                # Banir (Kick) e Desbanir (Kick remove do grupo, Unban permite voltar pagando)
                tb.ban_chat_member(canal_id, int(pedido.telegram_id))
                time.sleep(1) # Espera 1s para o Telegram processar
                tb.unban_chat_member(canal_id, int(pedido.telegram_id))
                
                # Avisa o usuário no privado
                try:
                    tb.send_message(int(pedido.telegram_id), "🚫 <b>Seu acesso expirou!</b>\n\nObrigado por ter ficado conosco. Renove seu plano para voltar!", parse_mode="HTML")
                except: pass
                
                logger.info(f"💀 Usuário {pedido.first_name} ({pedido.telegram_id}) removido do bot {bot_data.nome}")
            except Exception as e_kick:
                logger.warning(f"⚠️ Erro ao remover {pedido.telegram_id} (Talvez já saiu): {e_kick}")
            
            # Atualiza status no Pedido
            pedido.status = 'expired'
            
            # Atualiza status no Lead (Sincronia)
            lead = db.query(Lead).filter(Lead.bot_id == pedido.bot_id, Lead.user_id == pedido.telegram_id).first()
            if lead:
                lead.status = 'expired'
            
            removidos += 1
            db.commit()
            
        except Exception as e:
            logger.error(f"❌ Erro ao processar vencido {pedido.id}: {e}")
            erros += 1

    return {
        "status": "completed", 
        "total_analisado": len(vencidos),
        "removidos_sucesso": removidos, 
        "erros": erros
    }

# =========================================================
# 🚑 ROTA DE EMERGÊNCIA V2 (SEM O CAMPO 'ROLE')
# =========================================================
@app.get("/api/admin/fix-account-emergency")
def fix_admin_account_emergency(db: Session = Depends(get_db)):
    try:
        # SEU ID DA PUSHIN PAY (FIXO)
        MY_PUSHIN_ID = "9D4FA0F6-5B3A-4A36-ABA3-E55ACDF5794E"
        USERNAME_ALVO = "AdminZenyx" 
        
        # 1. Tenta achar o usuário
        user = db.query(User).filter(User.username == USERNAME_ALVO).first()
        
        if user:
            # CENÁRIO A: Atualiza APENAS o ID e o Superuser
            msg_anterior = f"ID anterior: {getattr(user, 'pushin_pay_id', 'Não existe')}"
            
            user.pushin_pay_id = MY_PUSHIN_ID
            user.is_superuser = True
            # REMOVIDO: user.role = "admin" (Isso causava o erro!)
            
            db.commit()
            return {
                "status": "restored", 
                "msg": f"✅ Usuário {USERNAME_ALVO} corrigido!",
                "detail": f"{msg_anterior} -> Novo ID: {MY_PUSHIN_ID}"
            }
        
        else:
            # CENÁRIO B: Recria o usuário (Sem o campo role)
            from passlib.context import CryptContext
            pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")
            hashed_password = pwd_context.hash("123456")
            
            new_user = User(
                username=USERNAME_ALVO,
                email="admin@zenyx.com",
                hashed_password=hashed_password,
                is_active=True,
                is_superuser=True,
                # role="admin", <--- REMOVIDO DAQUI TAMBÉM
                pushin_pay_id=MY_PUSHIN_ID,
                created_at=datetime.utcnow()
            )
            db.add(new_user)
            db.commit()
            return {
                "status": "created", 
                "msg": f"⚠️ Usuário {USERNAME_ALVO} RECRIADO.",
                "info": "Senha temporária: 123456"
            }

    except Exception as e:
        return {"status": "error", "msg": str(e)}


# =========================================================
# 🛠️ FERRAMENTA DE CORREÇÃO RETROATIVA (SEM GASTAR 1 CENTAVO)
# =========================================================
@app.get("/api/admin/sync-leads-expiration")
def sync_leads_expiration(db: Session = Depends(get_db)):
    try:
        # 1. Pega todos os pedidos aprovados que têm data de expiração
        pedidos_validos = db.query(Pedido).filter(
            Pedido.status.in_(['approved', 'active', 'paid']),
            Pedido.data_expiracao != None
        ).order_by(desc(Pedido.created_at)).all()

        atualizados = 0

        for pedido in pedidos_validos:
            # 2. Busca o Lead correspondente
            lead = db.query(Lead).filter(
                Lead.bot_id == pedido.bot_id,
                Lead.user_id == pedido.telegram_id
            ).first()

            # 3. Se achou o lead, força a data do pedido nele
            if lead:
                # Atualiza a data do Lead para bater com a do Pedido
                lead.expiration_date = pedido.data_expiracao
                lead.status = 'active' # Garante que está marcado como ativo
                atualizados += 1

        db.commit()

        return {
            "status": "sucesso",
            "mensagem": f"✅ {atualizados} Contatos foram corrigidos com a data dos Pedidos!",
            "economia": f"Você economizou {atualizados} testes de R$ 0.30"
        }
    except Exception as e:
        return {"status": "erro", "detalhe": str(e)}

# =========================================================
# 🕵️‍♂️ RAIO-X BLINDADO (SEM ACESSAR 'ROLE')
# =========================================================
@app.get("/api/admin/debug-users-list")
def debug_users_list(db: Session = Depends(get_db)):
    try:
        # 1. Conexão
        db_url = str(engine.url)
        host_info = db_url.split("@")[-1]
        
        # 2. Busca Usuários
        users = db.query(User).all()
        
        lista_users = []
        for u in users:
            # 🔥 TÉCNICA SEGURA: Converte o objeto para Dicionário
            # Isso pega apenas as colunas que REALMENTE existem no banco
            dados_usuario = {}
            for key, value in u.__dict__.items():
                if not key.startswith('_'): # Ignora campos internos do SQLAlchemy
                    dados_usuario[key] = value
            
            lista_users.append(dados_usuario)
            
        return {
            "CONEXAO": host_info,
            "TOTAL": len(users),
            "DADOS_REAIS": lista_users
        }
    except Exception as e:
        return {"erro_fatal": str(e)}

# =========================================================
# 🚑 ROTA DE EMERGÊNCIA V2 (SEM O CAMPO 'ROLE')
# =========================================================
@app.get("/api/admin/fix-account-emergency")
def fix_admin_account_emergency(db: Session = Depends(get_db)):
    try:
        # SEU ID DA PUSHIN PAY (FIXO)
        MY_PUSHIN_ID = "9D4FA0F6-5B3A-4A36-ABA3-E55ACDF5794E"
        USERNAME_ALVO = "AdminZenyx" 
        
        # 1. Tenta achar o usuário
        user = db.query(User).filter(User.username == USERNAME_ALVO).first()
        
        if user:
            # CENÁRIO A: Atualiza APENAS o ID e o Superuser
            msg_anterior = f"ID anterior: {getattr(user, 'pushin_pay_id', 'Não existe')}"
            
            user.pushin_pay_id = MY_PUSHIN_ID
            user.is_superuser = True
            # REMOVIDO: user.role = "admin" (Isso causava o erro!)
            
            db.commit()
            return {
                "status": "restored", 
                "msg": f"✅ Usuário {USERNAME_ALVO} corrigido!",
                "detail": f"{msg_anterior} -> Novo ID: {MY_PUSHIN_ID}"
            }
        
        else:
            # CENÁRIO B: Recria o usuário (Sem o campo role)
            from passlib.context import CryptContext
            pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")
            hashed_password = pwd_context.hash("123456")
            
            new_user = User(
                username=USERNAME_ALVO,
                email="admin@zenyx.com",
                hashed_password=hashed_password,
                is_active=True,
                is_superuser=True,
                # role="admin", <--- REMOVIDO DAQUI TAMBÉM
                pushin_pay_id=MY_PUSHIN_ID,
                created_at=datetime.utcnow()
            )
            db.add(new_user)
            db.commit()
            return {
                "status": "created", 
                "msg": f"⚠️ Usuário {USERNAME_ALVO} RECRIADO.",
                "info": "Senha temporária: 123456"
            }

    except Exception as e:
        return {"status": "error", "msg": str(e)}

# =========================================================
# 🕵️‍♂️ RAIO-X BLINDADO (SEM ACESSAR 'ROLE')
# =========================================================
@app.get("/api/admin/debug-users-list")
def debug_users_list(db: Session = Depends(get_db)):
    try:
        # 1. Conexão
        db_url = str(engine.url)
        host_info = db_url.split("@")[-1]
        
        # 2. Busca Usuários
        users = db.query(User).all()
        
        lista_users = []
        for u in users:
            # 🔥 TÉCNICA SEGURA: Converte o objeto para Dicionário
            # Isso pega apenas as colunas que REALMENTE existem no banco
            dados_usuario = {}
            for key, value in u.__dict__.items():
                if not key.startswith('_'): # Ignora campos internos do SQLAlchemy
                    dados_usuario[key] = value
            
            lista_users.append(dados_usuario)
            
        return {
            "CONEXAO": host_info,
            "TOTAL": len(users),
            "DADOS_REAIS": lista_users
        }
    except Exception as e:
        return {"erro_fatal": str(e)}

# =========================================================
# 💀 CRON JOB: REMOVEDOR DE USUÁRIOS VENCIDOS
# =========================================================
@app.get("/cron/check-expired")
def cron_check_expired(db: Session = Depends(get_db)):
    """
    Roda periodicamente para remover usuários com acesso vencido.
    Deve ser chamado por um Cron Job externo (ex: Railway Cron ou EasyCron).
    """
    logger.info("💀 Iniciando verificação de vencidos...")
    now = datetime.utcnow()
    
    # 1. Busca pedidos aprovados que JÁ venceram (data_expiracao < agora)
    vencidos = db.query(Pedido).filter(
        Pedido.status.in_(['approved', 'active']), # Apenas ativos
        Pedido.data_expiracao != None,
        Pedido.data_expiracao < now
    ).all()
    
    removidos = 0
    erros = 0
    
    for pedido in vencidos:
        try:
            bot_data = db.query(BotModel).filter(BotModel.id == pedido.bot_id).first()
            if not bot_data: continue
            
            # Conecta no Telegram (Sem threads para evitar erro)
            tb = telebot.TeleBot(bot_data.token, threaded=False)
            
            # Identifica o Canal
            canal_id = bot_data.id_canal_vip
            if str(canal_id).replace("-","").isdigit(): canal_id = int(str(canal_id).strip())
            
            # --- AÇÃO DE REMOÇÃO ---
            try:
                # Banir (Kick) e Desbanir (Kick remove do grupo, Unban permite voltar pagando)
                tb.ban_chat_member(canal_id, int(pedido.telegram_id))
                time.sleep(1) # Espera 1s para o Telegram processar
                tb.unban_chat_member(canal_id, int(pedido.telegram_id))
                
                # Avisa o usuário no privado
                try:
                    tb.send_message(int(pedido.telegram_id), "🚫 <b>Seu acesso expirou!</b>\n\nObrigado por ter ficado conosco. Renove seu plano para voltar!", parse_mode="HTML")
                except: pass
                
                logger.info(f"💀 Usuário {pedido.first_name} ({pedido.telegram_id}) removido do bot {bot_data.nome}")
            except Exception as e_kick:
                logger.warning(f"⚠️ Erro ao remover {pedido.telegram_id} (Talvez já saiu): {e_kick}")
            
            # Atualiza status no Pedido
            pedido.status = 'expired'
            
            # Atualiza status no Lead (Sincronia)
            lead = db.query(Lead).filter(Lead.bot_id == pedido.bot_id, Lead.user_id == pedido.telegram_id).first()
            if lead:
                lead.status = 'expired'
            
            removidos += 1
            db.commit()
            
        except Exception as e:
            logger.error(f"❌ Erro ao processar vencido {pedido.id}: {e}")
            erros += 1

    return {
        "status": "completed", 
        "total_analisado": len(vencidos),
        "removidos_sucesso": removidos, 
        "erros": erros
    }
# =========================================================
# 🧹 FAXINA GERAL: REMOVE DUPLICATAS E CORRIGE DATAS
# =========================================================
@app.get("/api/admin/fix-duplicates-and-dates")
def fix_duplicates_and_dates(db: Session = Depends(get_db)):
    try:
        # 1. Busca TODOS os Leads
        leads = db.query(Lead).order_by(Lead.bot_id, Lead.user_id, desc(Lead.created_at)).all()
        
        unicos = {}
        deletados = 0
        atualizados = 0
        
        # 2. Lógica de Deduplicação
        for lead in leads:
            # Chave única: Bot + Telegram ID
            chave = f"{lead.bot_id}_{lead.user_id}"
            
            if chave not in unicos:
                # Se é a primeira vez que vemos este usuário, guardamos ele como o "OFICIAL"
                unicos[chave] = lead
            else:
                # Se já vimos, este é uma DUPLICATA (e como ordenamos por desc, é o mais antigo)
                lead_oficial = unicos[chave]
                
                # Se a duplicata tiver uma data melhor que o oficial, a gente rouba a data dela
                if lead.expiration_date and (not lead_oficial.expiration_date or lead.expiration_date > lead_oficial.expiration_date):
                    lead_oficial.expiration_date = lead.expiration_date
                    lead_oficial.status = 'active'
                
                # Marca para deletar do banco
                db.delete(lead)
                deletados += 1

        # 3. Agora varre os Pedidos para garantir que o Lead Oficial tenha a data certa
        # (Isso resolve o problema do "Vitalício")
        pedidos = db.query(Pedido).filter(Pedido.status == 'approved').all()
        for p in pedidos:
            chave_p = f"{p.bot_id}_{p.telegram_id}"
            if chave_p in unicos:
                lead_alvo = unicos[chave_p]
                # Se a data do pedido for melhor/mais nova, atualiza o lead
                if p.data_expiracao:
                    lead_alvo.expiration_date = p.data_expiracao
                    lead_alvo.status = 'active'
                    atualizados += 1

        db.commit()
        
        return {
            "status": "sucesso",
            "duplicatas_removidas": deletados,
            "leads_corrigidos_pelo_pedido": atualizados,
            "mensagem": "Sua tabela de contatos agora tem apenas 1 linha por cliente e as datas estão corretas."
        }
        
    except Exception as e:
        db.rollback()
        return {"erro": str(e)}


# =========================================================
# 🛠️ FIX DATABASE: CRIAR COLUNA FALTANTE
# =========================================================
@app.get("/api/admin/fix-lead-column")
def fix_lead_column_db(db: Session = Depends(get_db)):
    try:
        # Comando SQL direto para criar a coluna se não existir
        db.execute(text("ALTER TABLE leads ADD COLUMN IF NOT EXISTS expiration_date TIMESTAMP"))
        db.commit()
        return {"status": "sucesso", "msg": "Coluna 'expiration_date' criada na tabela 'leads'!"}
    except Exception as e:
        return {"status": "erro", "msg": str(e)}

# =========================================================
# 🛠️ FIX FINAL: CRIAR COLUNAS QUE FALTAM (PHONE E EXPIRATION)
# =========================================================
@app.get("/api/admin/fix-database-structure")
def fix_database_structure(db: Session = Depends(get_db)):
    try:
        # 1. Cria a coluna PHONE (que está causando o erro agora)
        db.execute(text("ALTER TABLE leads ADD COLUMN IF NOT EXISTS phone VARCHAR"))
        
        # 2. Cria a coluna EXPIRATION_DATE (para garantir o vitalício)
        db.execute(text("ALTER TABLE leads ADD COLUMN IF NOT EXISTS expiration_date TIMESTAMP"))
        
        db.commit()
        
        return {
            "status": "sucesso", 
            "msg": "✅ Colunas 'phone' e 'expiration_date' criadas com sucesso na tabela LEADS!"
        }
    except Exception as e:
        db.rollback()
        return {"status": "erro", "msg": str(e)}

# =========================================================
# 🧹 FAXINA NUCLEAR: APAGA LEADS DUPLICADOS DO BANCO
# =========================================================
@app.get("/api/admin/nuke-duplicate-leads")
def nuke_duplicate_leads(db: Session = Depends(get_db)):
    """
    ⚠️ PERIGO: Esta rota APAGA fisicamente registros duplicados da tabela LEADS.
    Mantém apenas o registro mais recente de cada usuário por bot.
    """
    try:
        # 1. Busca TODOS os leads de TODOS os bots
        all_leads = db.query(Lead).order_by(Lead.created_at.desc()).all()
        
        unicos = {}
        ids_para_deletar = []
        
        # 2. Identifica quem deve morrer 💀
        for lead in all_leads:
            # Limpeza agressiva do ID
            tid = str(lead.user_id).strip().replace(" ", "")
            chave = f"{lead.bot_id}_{tid}"
            
            if chave not in unicos:
                # Primeiro que aparece é o mais novo (por causa do order_by desc)
                # Esse SOBREVIVE
                unicos[chave] = lead.id
            else:
                # Se já vimos essa chave, é uma duplicata mais antiga.
                # Esse MORRE
                ids_para_deletar.append(lead.id)
        
        # 3. Execução em massa
        if ids_para_deletar:
            # Deleta em lotes para não travar o banco
            chunk_size = 100
            for i in range(0, len(ids_para_deletar), chunk_size):
                chunk = ids_para_deletar[i:i + chunk_size]
                db.query(Lead).filter(Lead.id.in_(chunk)).delete(synchronize_session=False)
            
            db.commit()
            
        return {
            "status": "sucesso", 
            "total_analisado": len(all_leads),
            "unicos_mantidos": len(unicos),
            "lixo_deletado": len(ids_para_deletar),
            "msg": f"✅ {len(ids_para_deletar)} Leads duplicados foram apagados do banco de dados."
        }
        
    except Exception as e:
        db.rollback()
        return {"status": "erro", "msg": str(e)}