import os
import logging
import telebot
from telebot import TeleBot
from telebot.apihelper import ApiTelegramException
import httpx
import time
import urllib.parse
import threading
from telebot import types
import json
import uuid
import boto3
from sqlalchemy.exc import IntegrityError
import traceback  # 🔥 NOVO: Para logging detalhado de erros
import asyncio  # 🔥 Garantir que asyncio está importado
from concurrent.futures import ThreadPoolExecutor

from sqlalchemy import func, desc, text, and_, or_, extract
from fastapi import FastAPI, HTTPException, Depends, Request, BackgroundTasks, Query, File, UploadFile, Form 
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from pydantic import BaseModel, EmailStr, Field 
from sqlalchemy.orm import Session
from typing import List, Optional, Dict  # ✅ ADICIONAR Dict
from datetime import datetime, timedelta
from pytz import timezone

# --- IMPORTS DE MIGRATION ---
from force_migration import forcar_atualizacao_tabelas

# 🆕 AUTENTICAÇÃO
from passlib.context import CryptContext
from jose import JWTError, jwt
from fastapi.security import OAuth2PasswordBearer, OAuth2PasswordRequestForm

# --- SCHEDULER ---
from apscheduler.schedulers.asyncio import AsyncIOScheduler

from threading import Lock

# =========================================================
# 🧵 POOL DE THREADS GLOBAL (EVITA can't start new thread)
# =========================================================
thread_pool = ThreadPoolExecutor(max_workers=10, thread_name_prefix="zenyx")

# =========================================================
# ✅ IMPORTS CORRIGIDOS DO DATABASE
# =========================================================
from database import (
    SessionLocal, 
    init_db, 
    Bot as BotModel,  # ← RENOMEADO para evitar conflito com TeleBot
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
    # ✅ NOVOS IMPORTS PARA REMARKETING AUTOMÁTICO
    RemarketingConfig,
    AlternatingMessages,  # ✅ NOME CORRETO
    RemarketingLog,       # ✅ NOME CORRETO
    # ✅ NOVO IMPORT PARA CANAL FREE
    CanalFreeConfig,
    # ✅ NOVOS IMPORTS PARA UPSELL/DOWNSELL
    UpsellConfig,
    DownsellConfig,
    # ✅ NOVO IMPORT PARA GRUPOS E CANAIS
    BotGroup,
    # ✅ NOVO IMPORT PARA EMOJIS PREMIUM
    PremiumEmoji,
    PremiumEmojiPack,
    # ✅ NOVO IMPORT PARA DENÚNCIAS
    Report,
    UserStrike,
    # ✅ NOVO IMPORT PARA DIÁRIO DE MUDANÇAS
    ChangeLog
)

import update_db 

# ============================================================
# NOVA FUNÇÃO: AGENDAMENTO DE AUTO-DESTRUIÇÃO (SEM TRAVAR)
# ============================================================
def agendar_destruicao_msg(bot, chat_id, message_id, delay_seconds=5):
    """
    Agenda a exclusão de uma mensagem em uma thread separada para não travar o bot.
    """
    if delay_seconds <= 0: return

    def tarefa_destruir():
        time.sleep(delay_seconds)
        try:
            bot.delete_message(chat_id, message_id)
            logger.info(f"💣 Mensagem {message_id} destruída com sucesso.")
        except Exception as e:
            # Ignora erro se a mensagem já foi deletada ou não existe mais
            pass

    # Inicia via pool (evita criar threads infinitas)
    try:
        thread_pool.submit(tarefa_destruir)
    except RuntimeError:
        pass  # Pool cheio, ignora destruição

# =========================================================
# ✨ FUNÇÃO: CONVERTER SHORTCODES DE EMOJIS PREMIUM
# =========================================================
# Cache em memória para evitar consultas repetitivas ao banco
_premium_emoji_cache = {}
_premium_emoji_cache_ts = 0
PREMIUM_EMOJI_CACHE_TTL = 300  # 5 minutos

def convert_premium_emojis(text: str, db: Session = None) -> str:
    """
    Converte shortcodes como :fire_premium: para tags HTML do Telegram.
    Usa cache em memória para performance.
    
    Entrada:  "Olá! :fire_premium: Confira nossa oferta :star_premium:"
    Saída:    "Olá! <tg-emoji emoji-id=\"5408846744727334338\">🔥</tg-emoji> Confira nossa oferta <tg-emoji emoji-id=\"123456\">⭐</tg-emoji>"
    """
    global _premium_emoji_cache, _premium_emoji_cache_ts
    
    if not text or ':' not in text:
        return text
    
    import re
    # Verifica se existe algum padrão de shortcode no texto
    shortcodes_found = re.findall(r':[a-zA-Z0-9_]+:', text)
    if not shortcodes_found:
        return text
    
    logger.info(f"✨ [EMOJI CONVERT] Encontrados {len(shortcodes_found)} shortcodes no texto: {shortcodes_found[:5]}...")
    
    now = time.time()
    
    # Recarrega cache se expirou
    if now - _premium_emoji_cache_ts > PREMIUM_EMOJI_CACHE_TTL or not _premium_emoji_cache:
        try:
            if db is None:
                _db = SessionLocal()
                should_close = True
            else:
                _db = db
                should_close = False
            
            emojis = _db.query(PremiumEmoji).filter(PremiumEmoji.is_active == True).all()
            _premium_emoji_cache = {
                e.shortcode: f'<tg-emoji emoji-id="{e.emoji_id}">{e.fallback}</tg-emoji>' 
                for e in emojis
            }
            _premium_emoji_cache_ts = now
            logger.info(f"✨ [EMOJI CACHE] Recarregado com {len(_premium_emoji_cache)} emojis premium. Shortcodes: {list(_premium_emoji_cache.keys())[:10]}...")
            
            if should_close:
                _db.close()
        except Exception as e:
            logger.error(f"❌ [EMOJI CACHE] Erro ao carregar cache: {e}")
            import traceback
            logger.error(traceback.format_exc())
            if 'should_close' in dir() and should_close and '_db' in dir() and _db:
                try: _db.close()
                except: pass
            return text
    
    # Substitui shortcodes pelo HTML do Telegram
    converted_count = 0
    not_found = []
    for sc in shortcodes_found:
        if sc in _premium_emoji_cache:
            text = text.replace(sc, _premium_emoji_cache[sc])
            converted_count += 1
        else:
            not_found.append(sc)
    
    if converted_count > 0:
        logger.info(f"✨ [EMOJI CONVERT] Convertidos {converted_count}/{len(shortcodes_found)} emojis premium")
    if not_found:
        logger.warning(f"⚠️ [EMOJI CONVERT] Shortcodes NÃO encontrados no cache: {not_found[:10]}")
    
    return text


def strip_premium_emoji_tags(text: str) -> str:
    """
    Remove tags <tg-emoji> e mantém apenas o fallback.
    Usado quando o envio com emojis premium falha (bot sem premium).
    
    Entrada:  "Olá <tg-emoji emoji-id=\"123\">🔥</tg-emoji>"
    Saída:    "Olá 🔥"
    """
    import re
    return re.sub(r'<tg-emoji emoji-id="[^"]*">([^<]*)</tg-emoji>', r'\1', text)


def invalidate_premium_emoji_cache():
    """Invalida o cache de emojis premium (chamado após CRUD de emojis)."""
    global _premium_emoji_cache_ts
    _premium_emoji_cache_ts = 0
    logger.info("✨ [EMOJI CACHE] Cache invalidado")

# =========================================================
# 🆓 FUNÇÃO: APROVAR ENTRADA NO CANAL FREE
# =========================================================
def aprovar_entrada_canal_free(bot_token: str, canal_id: str, user_id: int):
    """
    Aprova entrada do usuário no canal após o delay configurado.
    Executado pelo scheduler.
    🔥 FIX: threaded=False para não criar threads extras no Railway.
    🔥 FIX: Retry com 3 tentativas para lidar com erros temporários.
    """
    max_retries = 3
    for attempt in range(max_retries):
        try:
            bot = telebot.TeleBot(bot_token, threaded=False)
            bot.approve_chat_join_request(int(canal_id), user_id)
            logger.info(f"✅ [CANAL FREE] Usuário {user_id} aprovado no canal {canal_id}")
            return  # Sucesso, sai da função
        except Exception as e:
            error_msg = str(e).lower()
            # Se o usuário já foi aprovado ou não tem request pendente, ignora
            if "user_already_participant" in error_msg or "hide_requester_missing" in error_msg or "request not found" in error_msg:
                logger.info(f"ℹ️ [CANAL FREE] Usuário {user_id} já aprovado/participante no canal {canal_id}")
                return
            
            if attempt < max_retries - 1:
                logger.warning(f"⚠️ [CANAL FREE] Tentativa {attempt+1}/{max_retries} falhou para {user_id}: {e}")
                import time
                time.sleep(2)  # Espera 2s antes de tentar novamente
            else:
                logger.error(f"❌ [CANAL FREE] Todas as {max_retries} tentativas falharam para {user_id}: {e}")

# Configuração de Log
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = FastAPI(title="Zenyx Gbot SaaS")

# 🔥 CONFIGURAÇÃO DE FUSO HORÁRIO - BRASÍLIA/SÃO PAULO
BRAZIL_TZ = timezone('America/Sao_Paulo')


# ============================================================
# 🔊 HELPER: ENVIO INTELIGENTE DE ÁUDIO OGG
# ============================================================
def is_audio_file(url):
    """Verifica se a URL é um arquivo de áudio"""
    if not url:
        return False
    return url.lower().endswith(('.ogg', '.mp3', '.wav'))

def _download_audio_bytes(media_url):
    """
    Baixa o áudio da URL e retorna (bytes, filename, duration_seconds).
    
    Necessário porque o Telegram só reconhece voice notes quando:
    1. O arquivo é enviado como bytes (não URL)
    2. O Content-Type está correto (audio/ogg)
    
    Também detecta a DURAÇÃO do áudio para simular "Enviando áudio..."
    pelo tempo real da gravação, tornando o envio mais realista.
    """
    try:
        resp = httpx.get(media_url, timeout=30, follow_redirects=True)
        resp.raise_for_status()
        audio_data = resp.content
        
        # Extrai extensão da URL
        ext = media_url.split('.')[-1].split('?')[0].lower()
        if ext not in ('ogg', 'mp3', 'wav'):
            ext = 'ogg'
        filename = f"voice_{uuid.uuid4().hex[:8]}.{ext}"
        
        # 🔊 Detecta duração do áudio usando mutagen
        duration = 0
        try:
            import io
            from mutagen import File as MutagenFile
            audio_file = MutagenFile(io.BytesIO(audio_data))
            if audio_file and audio_file.info:
                duration = int(audio_file.info.length)
                logger.info(f"🎙️ Áudio detectado: {duration}s de duração")
        except Exception as e_dur:
            logger.warning(f"⚠️ Não foi possível detectar duração do áudio: {e_dur}")
            duration = 0
        
        return audio_data, filename, duration
    except Exception as e:
        logger.error(f"❌ Erro ao baixar áudio de {media_url}: {e}")
        return None, None, 0

# ============================================================
# 🔄 HELPERS: CHAT ACTION EM LOOP (FIX DO TIMEOUT DE 4S)
# ============================================================
def _sleep_with_action(bot, chat_id, duration, action='record_voice'):
    """
    Mantém a ação (ex: 'gravando áudio') ativa visualmente durante TODO o delay.
    Renova o status a cada 4 segundos para evitar que o Telegram o remova.
    """
    start = time.time()
    while (time.time() - start) < duration:
        try:
            bot.send_chat_action(chat_id, action)
        except Exception:
            pass # Ignora erros de conexão/bloqueio durante o loop
            
        # Dorme 4s ou o restante do tempo (o que for menor)
        remaining = duration - (time.time() - start)
        if remaining <= 0: break
        time.sleep(min(remaining, 4.0))

async def _async_sleep_with_action(bot, chat_id, duration, action='record_voice'):
    """
    Versão ASYNC: Mantém a ação ativa visualmente durante TODO o delay.
    """
    start = time.time()
    while (time.time() - start) < duration:
        try:
            # Nota: TeleBot é síncrono, mas em função async não bloqueia o loop se for rápido
            bot.send_chat_action(chat_id, action)
        except Exception:
            pass
            
        remaining = duration - (time.time() - start)
        if remaining <= 0: break
        await asyncio.sleep(min(remaining, 4.0))

def enviar_audio_inteligente(bot, chat_id, media_url, texto=None, markup=None, parse_mode="HTML", protect_content=False, delay_pos_audio=2):
    """
    Envia áudio OGG como voice note nativo do Telegram.
    
    CORREÇÃO: Usa loop para manter 'Enviando áudio...' ativo por toda a duração.
    """
    sent_messages = []
    
    # 1. Baixa o áudio e detecta duração
    audio_bytes, filename, duration = _download_audio_bytes(media_url)
    
    # 2. Simula gravação pelo tempo real do áudio (COM LOOP DE RENOVAÇÃO)
    # Se temos duração real, usa ela (mínimo 2s, máximo 60s)
    if duration > 0:
        wait_time = min(max(duration, 2), 60)
        logger.info(f"🎙️ Simulando gravação por {wait_time}s (áudio real: {duration}s)")
    else:
        wait_time = 3  # Fallback padrão
    
    # 🔥 AQUI ESTÁ A CORREÇÃO: Usa o helper com loop
    _sleep_with_action(bot, chat_id, wait_time, 'record_voice')
    
    # 3. Envia como bytes (garante voice note nativo)
    try:
        if audio_bytes:
            voice_msg = bot.send_voice(chat_id, audio_bytes, protect_content=protect_content)
            sent_messages.append(voice_msg)
            logger.info(f"🎙️ Voice note enviado com sucesso para {chat_id} ({len(audio_bytes)} bytes)")
        else:
            # Fallback: tenta enviar direto pela URL mesmo assim
            logger.warning(f"⚠️ Download falhou, tentando enviar URL direta...")
            voice_msg = bot.send_voice(chat_id, media_url, protect_content=protect_content)
            sent_messages.append(voice_msg)
    except Exception as e:
        logger.error(f"❌ Erro ao enviar voice: {e}")
        try:
            bot.send_audio(chat_id, media_url, protect_content=protect_content)
        except:
            pass
        return sent_messages
    
    # 4. Se tem texto OU botões, envia em mensagem separada após delay
    if texto or markup:
        time.sleep(delay_pos_audio)
        try:
            if texto and markup:
                text_msg = bot.send_message(chat_id, texto, reply_markup=markup, parse_mode=parse_mode, protect_content=protect_content)
            elif texto:
                text_msg = bot.send_message(chat_id, texto, parse_mode=parse_mode, protect_content=protect_content)
            elif markup:
                text_msg = bot.send_message(chat_id, "⬇️ Escolha uma opção:", reply_markup=markup, protect_content=protect_content)
            sent_messages.append(text_msg)
        except Exception as e:
            logger.error(f"❌ Erro ao enviar texto pós-áudio: {e}")
    
    return sent_messages


def now_brazil():
    """Retorna datetime atual no horário de Brasília/São Paulo"""
    return datetime.now(BRAZIL_TZ)

# =========================================================
# ✅ VARIÁVEIS GLOBAIS PARA REMARKETING
# =========================================================
# Controle de remarketing
remarketing_lock = Lock()
remarketing_timers = {}  # {chat_id: asyncio.Task}
alternating_tasks = {}   # {chat_id: asyncio.Task}



# ============================================================
# 🎯 SISTEMA DE REMARKETING AUTOMÁTICO
# ============================================================

# Dicionário de usuários que já receberam remarketing (para não enviar duplicado)
usuarios_com_remarketing_enviado = set()

# ============================================================
# FUNÇÃO 1: MENSAGENS ALTERNANTES
# ============================================================
def alternar_mensagens_pagamento(bot_instance, chat_id, bot_id):
    """
    Inicia o loop de alternância de mensagens após envio do PIX.
    As mensagens alternam até XX segundos antes do disparo automático.
    """
    try:
        db = SessionLocal()
        
        # Busca configuração de mensagens alternantes
        config = db.query(AlternatingMessages).filter(
            AlternatingMessages.bot_id == bot_id,
            AlternatingMessages.is_active == True
        ).first()
        
        if not config or not config.messages or len(config.messages) < 2:
            logger.info(f"Mensagens alternantes desativadas ou insuficientes para bot {bot_id}")
            db.close()
            return
        
        # Busca config de remarketing para saber quando parar
        remarketing_cfg = db.query(RemarketingConfig).filter(
            RemarketingConfig.bot_id == bot_id
        ).first()
        
        if not remarketing_cfg:
            logger.warning(f"Config de remarketing não encontrada para bot {bot_id}")
            db.close()
            return
        
        db.close()
        
        # Calcula timing
        delay_remarketing = remarketing_cfg.delay_minutes * 60
        stop_before = config.stop_before_remarketing_seconds
        rotation_interval = config.rotation_interval_seconds
        
        # Tempo total de alternância
        tempo_total_alternacao = delay_remarketing - stop_before
        
        if tempo_total_alternacao <= 0:
            logger.warning(f"Tempo de alternância inválido para bot {bot_id}")
            return
        
        # Inicia thread de alternância
        def loop_alternancia():
            mensagens = config.messages
            index = 0
            ultimo_message_id = None
            tempo_inicio = time.time()
            
            while True:
                tempo_decorrido = time.time() - tempo_inicio
                
                # Para se atingiu o limite de tempo
                if tempo_decorrido >= tempo_total_alternacao:
                    logger.info(f"Alternância finalizada para {chat_id}")
                    
                    # Auto-destruir mensagem final se configurado
                    if config.auto_destruct_final and ultimo_message_id:
                        try:
                            bot_instance.delete_message(chat_id, ultimo_message_id)
                        except:
                            pass
                    break
                
                # Deleta mensagem anterior
                if ultimo_message_id:
                    try:
                        bot_instance.delete_message(chat_id, ultimo_message_id)
                    except:
                        pass
                
                # Envia nova mensagem
                try:
                    mensagem_atual = mensagens[index % len(mensagens)]
                    msg = bot_instance.send_message(chat_id, mensagem_atual)
                    ultimo_message_id = msg.message_id
                    index += 1
                except ApiTelegramException as e:
                    if "bot was blocked" in str(e):
                        logger.warning(f"Usuário {chat_id} bloqueou o bot")
                        break
                except Exception as e:
                    logger.error(f"Erro ao enviar mensagem alternante: {e}")
                    break
                
                # Aguarda próximo ciclo
                time.sleep(rotation_interval)
        
        # Inicia via pool (evita criar threads infinitas)
        try:
            future = thread_pool.submit(loop_alternancia)
            alternating_tasks[chat_id] = future
        except RuntimeError:
            logger.warning(f"⚠️ Pool cheio, alternância para {chat_id} ignorada")
            return
        
        logger.info(f"✅ Mensagens alternantes iniciadas para {chat_id} (bot {bot_id})")
        
    except Exception as e:
        logger.error(f"Erro ao iniciar mensagens alternantes: {e}")

# ============================================================
# FUNÇÃO 2: CANCELAR ALTERNAÇÃO
# ============================================================
def cancelar_alternacao_mensagens(chat_id):
    """Cancela o loop de mensagens alternantes"""
    if chat_id in alternating_tasks:
        try:
            # Thread será interrompida naturalmente
            alternating_tasks.pop(chat_id, None)
            logger.info(f"Alternação cancelada para {chat_id}")
        except Exception as e:
            logger.error(f"Erro ao cancelar alternação: {e}")

# ============================================================
# FUNÇÃO 3: DISPARO AUTOMÁTICO (THREADED)
# ============================================================
def enviar_remarketing_automatico(bot_instance, chat_id, bot_id):
    """
    Envia o disparo automático de remarketing após o tempo configurado.
    Inclui mídia, texto e botões com valores promocionais.
    ✅ CORRIGIDO: Auto-destruição agora é OPCIONAL e só acontece APÓS clicar no botão
    """
    try:
        # Remove do set de timers ativos para evitar vazamento de memória
        if chat_id in remarketing_timers:
            remarketing_timers.pop(chat_id, None)
        
        # ✅ BLOQUEIO: Verifica se já enviou nesta sessão
        if chat_id in usuarios_com_remarketing_enviado:
            logger.info(f"⏭️ Remarketing já enviado para {chat_id}, bloqueando reenvio")
            return
        
        db = SessionLocal()
        
        try:
            # Busca config de remarketing
            config = db.query(RemarketingConfig).filter(
                RemarketingConfig.bot_id == bot_id,
                RemarketingConfig.is_active == True
            ).first()
            
            if not config:
                logger.warning(f"⚠️ Config de remarketing não encontrada para bot {bot_id}")
                return
            
            # Busca planos para montar botões
            planos = db.query(PlanoConfig).filter(
                PlanoConfig.bot_id == bot_id
            ).all()
            
        finally:
            db.close() # Fecha conexão rápida de leitura
        
        # Para mensagens alternantes (se estiverem rodando)
        cancelar_alternacao_mensagens(chat_id)
        
        # Prepara mensagem
        mensagem = config.message_text or "🔥 OFERTA ESPECIAL! Não perca essa chance!"
        
        # ✨ CONVERTE EMOJIS PREMIUM
        mensagem = convert_premium_emojis(mensagem)
        
        # 🔒 Carrega flag de proteção
        _protect_auto = False
        try:
            db_temp = SessionLocal()
            bot_data = db_temp.query(BotModel).filter(BotModel.id == bot_id).first()
            _protect_auto = getattr(bot_data, 'protect_content', False) or False
            db_temp.close()
        except: pass
        
        # Envia mídia se configurado
        message_id = None
        try:
            # 🔊 COMBO: Se tem audio_url separado, envia áudio primeiro, depois mídia+texto+botões
            _audio_url_cfg = getattr(config, 'audio_url', None)
            _audio_delay_cfg = getattr(config, 'audio_delay_seconds', 3) or 3
            
            if _audio_url_cfg and _audio_url_cfg.strip():
                # MODO COMBO: Áudio separado + mídia com legenda e botões
                logger.info(f"🎙️ Modo combo: áudio separado + mídia para {chat_id}")
                
                # Passo 1: Envia áudio sozinho (voice note nativo)
                audio_combo_bytes, _, _dur_combo = _download_audio_bytes(_audio_url_cfg)
                
                # 🔥 CORREÇÃO: Usa _sleep_with_action para manter o status pelo tempo REAL
                _wait_combo = min(max(_dur_combo, 2), 60) if _dur_combo > 0 else 3
                _sleep_with_action(bot_instance, chat_id, _wait_combo, 'record_voice')
                
                if audio_combo_bytes:
                    bot_instance.send_voice(chat_id, audio_combo_bytes, protect_content=_protect_auto)
                else:
                    bot_instance.send_voice(chat_id, _audio_url_cfg, protect_content=_protect_auto)
                
                # Passo 2: Delay configurável entre áudio e mídia
                time.sleep(_audio_delay_cfg)
                
                # Passo 3: Envia mídia (foto/vídeo) + legenda + botões (como mensagem normal)
                if config.media_url and config.media_type:
                    if config.media_type == 'photo':
                        msg = bot_instance.send_photo(chat_id, config.media_url, caption=mensagem, parse_mode='HTML', protect_content=_protect_auto)
                    elif config.media_type == 'video':
                        msg = bot_instance.send_video(chat_id, config.media_url, caption=mensagem, parse_mode='HTML', protect_content=_protect_auto)
                    else:
                        msg = bot_instance.send_message(chat_id, mensagem, parse_mode='HTML', protect_content=_protect_auto)
                    message_id = msg.message_id
                else:
                    # Só tem áudio + texto (sem mídia extra)
                    msg = bot_instance.send_message(chat_id, mensagem, parse_mode='HTML', protect_content=_protect_auto)
                    message_id = msg.message_id
            
            elif config.media_url and config.media_type:
                # MODO NORMAL: Mídia única (foto, vídeo ou áudio)
                if config.media_type == 'photo':
                    msg = bot_instance.send_photo(chat_id, config.media_url, caption=mensagem, parse_mode='HTML', protect_content=_protect_auto)
                elif config.media_type == 'video':
                    msg = bot_instance.send_video(chat_id, config.media_url, caption=mensagem, parse_mode='HTML', protect_content=_protect_auto)
                elif config.media_type == 'audio' or config.media_url.lower().endswith(('.ogg', '.mp3', '.wav')):
                    # 🔊 ÁUDIO ÚNICO: Envia com duração inteligente
                    audio_msgs = enviar_audio_inteligente(
                        bot_instance, chat_id, config.media_url,
                        texto=mensagem if mensagem and mensagem.strip() else None,
                        protect_content=_protect_auto,
                        delay_pos_audio=2
                    )
                    msg = audio_msgs[-1] if audio_msgs else None
                    if not msg and audio_msgs:
                        msg = audio_msgs[0]
                else:
                    msg = bot_instance.send_message(chat_id, mensagem, parse_mode='HTML', protect_content=_protect_auto)
            else:
                msg = bot_instance.send_message(chat_id, mensagem, parse_mode='HTML', protect_content=_protect_auto)
            
            message_id = msg.message_id
            
        except ApiTelegramException as e:
            if "bot was blocked" in str(e) or "user is deactivated" in str(e):
                logger.warning(f"⚠️ Usuário {chat_id} bloqueou o bot")
                return
            logger.error(f"❌ Erro ao enviar mídia de remarketing: {e}")
            return
        except Exception as e:
            logger.error(f"❌ Erro genérico no envio: {e}")
            return
        
        # Monta botões com valores promocionais
        markup = types.InlineKeyboardMarkup(row_width=1)
        
        promo_values = config.promo_values or {}
        
        for plano in planos:
            plano_id_str = str(plano.id)
            # 🔥 SÓ adiciona botão se o plano está ATIVADO no promo_values
            if plano_id_str not in promo_values:
                continue
            
            promo_data = promo_values[plano_id_str]
            
            # Suporta tanto valor direto (float) quanto objeto {value, button_text}
            if isinstance(promo_data, dict):
                valor_promo = promo_data.get('value', plano.preco_atual) or plano.preco_atual
                botao_texto = promo_data.get('button_text', '').strip()
                if not botao_texto:
                    botao_texto = f"🔥 {plano.nome_exibicao} - R$ {float(valor_promo):.2f}"
            else:
                valor_promo = promo_data if promo_data else plano.preco_atual
                botao_texto = f"🔥 {plano.nome_exibicao} - R$ {float(valor_promo):.2f}"
            
            botao = types.InlineKeyboardButton(
                botao_texto,
                callback_data=f"remarketing_plano_{plano.id}"
            )
            markup.add(botao)
        
        # Envia botões inline com a última mensagem (mídia ou texto)
        # Se já enviou mídia sem markup, envia botões em mensagem separada
        buttons_message_id = None
        if len(markup.keyboard) > 0:
            try:
                buttons_msg = bot_instance.send_message(
                    chat_id,
                    "👇 Escolha seu plano com desconto:",
                    reply_markup=markup
                )
                buttons_message_id = buttons_msg.message_id
            except Exception as e:
                logger.error(f"Erro ao enviar botões: {e}")
        
        # ✅ MARCA COMO ENVIADO PARA BLOQUEAR REENVIO
        usuarios_com_remarketing_enviado.add(chat_id)
        
        # Registra no log
        db = SessionLocal()
        try:
            log = RemarketingLog(
                bot_id=bot_id,
                user_id=str(chat_id),
                sent_at=now_brazil(),
                message_sent=mensagem,
                promo_values=promo_values,
                status='sent'
            )
            db.add(log)
            db.commit()
        except Exception as e_log:
            logger.error(f"❌ Erro ao salvar log de remarketing: {e_log}")
            db.rollback()
        finally:
            db.close()
        
        # ✅ NOVA LÓGICA: Auto-destruição OPCIONAL e APÓS CLIQUE
        if config.auto_destruct_enabled and config.auto_destruct_seconds > 0 and message_id:
            
            if config.auto_destruct_after_click:
                if not hasattr(enviar_remarketing_automatico, 'pending_destructions'):
                    enviar_remarketing_automatico.pending_destructions = {}
                
                enviar_remarketing_automatico.pending_destructions[chat_id] = {
                    'message_id': message_id,
                    'buttons_message_id': buttons_message_id,
                    'bot_instance': bot_instance,
                    'destruct_seconds': config.auto_destruct_seconds
                }
                logger.info(f"💣 Auto-destruição agendada APÓS CLIQUE para {chat_id}")
            
            else:
                def auto_delete():
                    time.sleep(config.auto_destruct_seconds)
                    try:
                        bot_instance.delete_message(chat_id, message_id)
                        if buttons_message_id:
                            bot_instance.delete_message(chat_id, buttons_message_id)
                        logger.info(f"🗑️ Mensagem de remarketing auto-destruída para {chat_id}")
                    except Exception as e:
                        pass
                
                try:
                    thread_pool.submit(auto_delete)
                except RuntimeError:
                    pass
                logger.info(f"⏳ Auto-destruição IMEDIATA agendada para {config.auto_destruct_seconds}s")

        logger.info(f"✅ [REMARKETING] Enviado com sucesso para {chat_id} (bot {bot_id})")

    except Exception as e:
        logger.error(f"❌ Erro fatal no job de remarketing automático: {e}")

# ============================================================
# FUNÇÃO 4: AGENDAR REMARKETING
# ============================================================
def agendar_remarketing_automatico(bot_instance, chat_id, bot_id):
    """
    Agenda o disparo automático de remarketing após o tempo configurado.
    """
    try:
        # Verifica se já foi enviado
        if chat_id in usuarios_com_remarketing_enviado:
            logger.info(f"Remarketing já enviado anteriormente para {chat_id}")
            return
        
        # Busca config
        db = SessionLocal()
        config = db.query(RemarketingConfig).filter(
            RemarketingConfig.bot_id == bot_id
        ).first()
        db.close()
        
        if not config or not config.is_active:
            logger.info(f"Remarketing desativado para bot {bot_id}")
            return
        
        delay_seconds = config.delay_minutes * 60
        
        # Cancela timer anterior se existir
        if chat_id in remarketing_timers:
            try:
                remarketing_timers[chat_id].cancel()
            except:
                pass
        
        # Cria tarefa com delay via pool (evita criar threads infinitas)
        def delayed_remarketing():
            time.sleep(delay_seconds)
            enviar_remarketing_automatico(bot_instance, chat_id, bot_id)
        
        try:
            future = thread_pool.submit(delayed_remarketing)
            remarketing_timers[chat_id] = future
        except RuntimeError:
            logger.warning(f"⚠️ Pool cheio, remarketing para {chat_id} ignorado")
        
        logger.info(f"✅ Remarketing agendado para {chat_id} em {config.delay_minutes} minutos")
        
    except Exception as e:
        logger.error(f"Erro ao agendar remarketing: {e}")

# ============================================================
# FUNÇÃO 5: CANCELAR REMARKETING
# ============================================================
def cancelar_remarketing(chat_id):
    """
    Cancela o remarketing agendado (usado quando usuário paga).
    """
    try:
        # Cancela timer/future
        if chat_id in remarketing_timers:
            future = remarketing_timers.pop(chat_id, None)
            if future:
                try:
                    future.cancel()  # Funciona para Future do ThreadPoolExecutor
                except:
                    pass
        
        # Cancela mensagens alternantes
        cancelar_alternacao_mensagens(chat_id)
        
        logger.info(f"✅ Remarketing cancelado para {chat_id}")
        
    except Exception as e:
        logger.error(f"Erro ao cancelar remarketing: {e}")

# ============================================================
# FUNÇÕES DE JOBS AGENDADOS
# ============================================================

async def verificar_vencimentos():
    """
    Job agendado para verificar e processar vencimentos de assinaturas.
    Executa a cada 12 horas.
    
    Verifica AMBOS os campos: data_expiracao e custom_expiration.
    Remove do canal VIP principal E dos grupos extras (BotGroup).
    Protege admins contra remoção.
    """
    try:
        logger.info("🔄 [JOB] Iniciando verificação de vencimentos...")
        
        db = SessionLocal()
        
        try:
            agora = now_brazil()
            
            # Buscar pedidos ativos/aprovados que venceram
            # Verifica custom_expiration (prioridade) OU data_expiracao
            pedidos_vencidos = db.query(Pedido).filter(
                Pedido.status.in_(['approved', 'active', 'paid']),
                or_(
                    and_(Pedido.custom_expiration != None, Pedido.custom_expiration < agora),
                    and_(Pedido.custom_expiration == None, Pedido.data_expiracao != None, Pedido.data_expiracao < agora)
                )
            ).all()
            
            if not pedidos_vencidos:
                logger.info("✅ [JOB] Nenhum vencimento encontrado")
                return
            
            logger.info(f"📋 [JOB] {len(pedidos_vencidos)} vencimentos encontrados")
            
            removidos = 0
            erros = 0
            
            # Processar cada vencimento
            for pedido in pedidos_vencidos:
                try:
                    # Buscar o bot associado
                    bot_data = db.query(BotModel).filter(BotModel.id == pedido.bot_id).first()
                    if not bot_data or not bot_data.token:
                        pedido.status = 'expired'
                        db.commit()
                        continue
                    
                    # 🔥 Proteção: Admin nunca é removido
                    eh_admin_principal = (
                        bot_data.admin_principal_id and 
                        str(pedido.telegram_id) == str(bot_data.admin_principal_id)
                    )
                    eh_admin_extra = db.query(BotAdmin).filter(
                        BotAdmin.telegram_id == str(pedido.telegram_id),
                        BotAdmin.bot_id == bot_data.id
                    ).first()
                    
                    if eh_admin_principal or eh_admin_extra:
                        logger.info(f"👑 [JOB] Ignorando remoção de Admin: {pedido.telegram_id}")
                        continue
                    
                    # Conectar no Telegram
                    tb = telebot.TeleBot(bot_data.token, threaded=False)
                    
                    # === REMOÇÃO DO CANAL VIP PRINCIPAL ===
                    # 🔥 V2: Remove do canal CORRETO (específico do plano ou padrão do bot)
                    canal_remocao = None
                    canal_source = "nenhum"
                    
                    # 1. Primeiro tenta o canal específico do plano
                    if pedido.plano_id:
                        try:
                            plano_exp = db.query(PlanoConfig).filter(PlanoConfig.id == int(pedido.plano_id)).first()
                            if plano_exp and plano_exp.id_canal_destino and str(plano_exp.id_canal_destino).strip() not in ("", "None", "null"):
                                canal_remocao = int(str(plano_exp.id_canal_destino).strip())
                                canal_source = f"plano_{plano_exp.nome_exibicao}"
                                logger.info(f"🎯 [JOB] Canal específico do plano '{plano_exp.nome_exibicao}': {canal_remocao}")
                        except Exception as e_plano:
                            logger.warning(f"⚠️ [JOB] Erro ao buscar plano {pedido.plano_id}: {e_plano}")
                    
                    # 2. Fallback para canal padrão do bot
                    if not canal_remocao and bot_data.id_canal_vip:
                        try:
                            canal_remocao = int(str(bot_data.id_canal_vip).strip())
                            canal_source = "bot_default"
                        except:
                            pass
                    
                    # 3. Executar remoção
                    if canal_remocao:
                        try:
                            tb.ban_chat_member(canal_remocao, int(pedido.telegram_id))
                            time.sleep(0.5)
                            tb.unban_chat_member(canal_remocao, int(pedido.telegram_id))
                            
                            logger.info(f"👋 [JOB] Usuário {pedido.first_name} ({pedido.telegram_id}) removido do canal {canal_remocao} ({canal_source}) (Bot: {bot_data.nome})")
                        except Exception as e_kick:
                            err_msg = str(e_kick).lower()
                            if "participant_id_invalid" in err_msg or "user not found" in err_msg or "user_not_participant" in err_msg:
                                logger.info(f"ℹ️ [JOB] Usuário {pedido.telegram_id} já havia saído do canal {canal_remocao}.")
                            else:
                                logger.warning(f"⚠️ [JOB] Erro ao remover {pedido.telegram_id} do canal {canal_remocao}: {e_kick}")
                        
                        # 🔥 Se removeu do canal do plano, TAMBÉM remove do canal padrão por segurança
                        if canal_source != "bot_default" and bot_data.id_canal_vip:
                            try:
                                canal_padrao = int(str(bot_data.id_canal_vip).strip())
                                if canal_padrao != canal_remocao:
                                    tb.ban_chat_member(canal_padrao, int(pedido.telegram_id))
                                    time.sleep(0.3)
                                    tb.unban_chat_member(canal_padrao, int(pedido.telegram_id))
                                    logger.info(f"👋 [JOB] Também removido do canal padrão {canal_padrao}")
                            except:
                                pass  # Pode não estar no canal padrão
                    
                    # === REMOÇÃO DOS GRUPOS EXTRAS (BotGroup) ===
                    if pedido.plano_id:
                        try:
                            grupos_extras = db.query(BotGroup).filter(
                                BotGroup.bot_id == bot_data.id,
                                BotGroup.is_active == True
                            ).all()
                            
                            for grupo in grupos_extras:
                                # Verificar se o plano do pedido está nos planos vinculados ao grupo
                                plan_ids = grupo.plan_ids if grupo.plan_ids else []
                                if pedido.plano_id in plan_ids:
                                    try:
                                        grupo_id = int(str(grupo.group_id).strip())
                                        tb.ban_chat_member(grupo_id, int(pedido.telegram_id))
                                        time.sleep(0.3)
                                        tb.unban_chat_member(grupo_id, int(pedido.telegram_id))
                                        logger.info(f"👋 [JOB] Usuário {pedido.telegram_id} removido do grupo extra '{grupo.title}'")
                                    except Exception as e_grupo:
                                        err_msg = str(e_grupo).lower()
                                        if "participant_id_invalid" in err_msg or "user not found" in err_msg or "user_not_participant" in err_msg:
                                            pass  # Já saiu, sem problema
                                        else:
                                            logger.warning(f"⚠️ [JOB] Erro ao remover do grupo '{grupo.title}': {e_grupo}")
                        except Exception as e_grupos:
                            logger.warning(f"⚠️ [JOB] Erro ao processar grupos extras: {e_grupos}")
                    
                    # === ATUALIZAR STATUS ===
                    pedido.status = 'expired'
                    
                    # Sincronizar Lead
                    lead = db.query(Lead).filter(
                        Lead.bot_id == pedido.bot_id, 
                        Lead.user_id == str(pedido.telegram_id)
                    ).first()
                    if lead:
                        lead.status = 'expired'
                    
                    db.commit()
                    removidos += 1
                    
                    # Avisar o usuário no privado
                    try:
                        tb.send_message(
                            int(pedido.telegram_id),
                            "🚫 <b>Seu plano venceu!</b>\n\nPara renovar, digite /start",
                            parse_mode="HTML"
                        )
                    except:
                        pass
                    
                    logger.info(f"✅ [JOB] Pedido #{pedido.id} marcado como expired")
                    
                except Exception as e:
                    logger.error(f"❌ [JOB] Erro ao processar pedido #{pedido.id}: {str(e)}")
                    db.rollback()
                    erros += 1
                    continue
            
            logger.info(f"✅ [JOB] Verificação concluída: {removidos} removidos, {erros} erros")
            
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
                (WebhookRetry.next_retry == None) | (WebhookRetry.next_retry <= now_brazil())
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
                        webhook.updated_at = now_brazil()
                        logger.info(f"✅ [WEBHOOK-RETRY] Webhook #{webhook.id} processado")
                    else:
                        # Calcular próximo retry (exponential backoff)
                        backoff_minutes = 2 ** webhook.attempts  # 2, 4, 8, 16, 32
                        webhook.next_retry = now_brazil() + timedelta(minutes=backoff_minutes)
                        
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
                        webhook.updated_at = now_brazil()
                    
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
# 🚀 STARTUP: INICIALIZAÇÃO DO SERVIDOR (CORRIGIDO)
# =========================================================

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
# 🔄 JOBS DE DISPARO AUTOMÁTICO (CORE LÓGICO)
# ============================================================

async def start_alternating_messages_job(token: str, chat_id: int, payment_message_id: int, messages: list, interval_seconds: int, stop_at: datetime, auto_destruct_final: bool, bot_id: int):
    """
    ✅ CORRETO: Envia UMA mensagem e vai EDITANDO o conteúdo dela (alternância visual)
    - Envia mensagem 1 → aguarda → EDITA para mensagem 2 → aguarda → EDITA para mensagem 3
    - NO FINAL (se configurado): APAGA a última mensagem
    
    🔥 V2: Tratamento robusto para mensagens deletadas/usuário bloqueado
    """
    try:
        bot_alt = TeleBot(token, threaded=False)
        bot_alt.parse_mode = "HTML"
        
        tempo_inicio = now_brazil()
        logger.info(f"✅ [ALTERNATING] Iniciado - Chat: {chat_id}, Msgs: {len(messages)}")
        logger.info(f"🕐 [ALTERNATING-TIMER] Início: {tempo_inicio.strftime('%H:%M:%S')}")
        logger.info(f"🕐 [ALTERNATING-TIMER] Término previsto: {stop_at.strftime('%H:%M:%S')}")
        logger.info(f"⏱️ [ALTERNATING-DEBUG] Intervalo entre msgs: {interval_seconds}s")
        logger.info(f"🗑️ [ALTERNATING-DEBUG] Auto-destruir última: {auto_destruct_final}")
        
        # ✅ ENVIAR A PRIMEIRA MENSAGEM (criar a bolha)
        mensagem_index = 0
        total_mensagens = len(messages)
        
        primeira_msg = messages[0]
        texto_primeira = primeira_msg if isinstance(primeira_msg, str) else primeira_msg.get('content', '')
        
        # ✨ CONVERTE EMOJIS PREMIUM
        texto_primeira = convert_premium_emojis(texto_primeira)
        
        if not texto_primeira or not texto_primeira.strip():
            logger.error(f"❌ [ALTERNATING] Primeira mensagem está vazia!")
            return
        
        try:
            sent_msg = bot_alt.send_message(chat_id, texto_primeira)
            mensagem_id = sent_msg.message_id
        except ApiTelegramException as e:
            if "blocked" in str(e).lower() or "403" in str(e):
                logger.warning(f"🚫 [ALTERNATING] Usuário {chat_id} bloqueou o bot. Abortando.")
            else:
                logger.error(f"❌ [ALTERNATING] Erro ao enviar primeira mensagem: {e}")
            return
        
        logger.info(f"📤 [ALTERNATING] Mensagem inicial enviada (ID: {mensagem_id})")
        
        # 🔥 Contador de falhas consecutivas para evitar loop infinito
        falhas_consecutivas = 0
        MAX_FALHAS = 3
        
        # ✅ LOOP DE ALTERNÂNCIA (editar a mesma mensagem)
        mensagem_index = 1  # Começar da segunda mensagem
        
        while now_brazil() < stop_at:
            # Verificar se ainda há tempo disponível
            tempo_restante = (stop_at - now_brazil()).total_seconds()
            
            if tempo_restante <= 0:
                logger.info(f"⏰ [ALTERNATING-TIMER] TEMPO ESGOTADO!")
                break
            
            # Aguardar intervalo
            if tempo_restante < interval_seconds:
                logger.info(f"⏰ [ALTERNATING-TIMER] Tempo insuficiente para próxima alternância")
                break
            
            logger.info(f"⏳ [ALTERNATING] Aguardando {interval_seconds}s até próxima alternância...")
            await asyncio.sleep(interval_seconds)
            
            # ✅ EDITAR A MENSAGEM COM O PRÓXIMO CONTEÚDO
            mensagem_atual = messages[mensagem_index]
            texto_atual = mensagem_atual if isinstance(mensagem_atual, str) else mensagem_atual.get('content', '')
            
            # ✨ CONVERTE EMOJIS PREMIUM
            texto_atual = convert_premium_emojis(texto_atual)
            
            if not texto_atual or not texto_atual.strip():
                logger.warning(f"⚠️ [ALTERNATING] Mensagem {mensagem_index + 1} vazia, pulando...")
                mensagem_index = (mensagem_index + 1) % total_mensagens
                continue
            
            try:
                # ✅ EDITAR A MENSAGEM EXISTENTE (não enviar nova)
                bot_alt.edit_message_text(
                    chat_id=chat_id,
                    message_id=mensagem_id,
                    text=texto_atual
                )
                
                falhas_consecutivas = 0  # Reset no sucesso
                logger.info(f"✏️ [ALTERNATING] Mensagem editada para conteúdo {mensagem_index + 1}/{total_mensagens} | Tempo restante: {tempo_restante:.0f}s")
                
            except ApiTelegramException as e:
                err_str = str(e).lower()
                
                # 🔥 Usuário bloqueou o bot — parar imediatamente
                if "blocked" in err_str or "403" in err_str:
                    logger.warning(f"🚫 [ALTERNATING] Usuário {chat_id} bloqueou o bot. Parando.")
                    mensagem_id = None  # Não tentar deletar
                    break
                
                # 🔥 Mensagem deletada pelo usuário — tentar enviar nova
                if "message to edit not found" in err_str or "message is not modified" in err_str:
                    logger.warning(f"⚠️ [ALTERNATING] Mensagem {mensagem_id} não existe mais. Tentando reenviar...")
                    try:
                        sent_msg = bot_alt.send_message(chat_id, texto_atual)
                        mensagem_id = sent_msg.message_id
                        falhas_consecutivas = 0
                        logger.info(f"📤 [ALTERNATING] Nova mensagem enviada (ID: {mensagem_id})")
                    except ApiTelegramException as e2:
                        if "blocked" in str(e2).lower() or "403" in str(e2):
                            logger.warning(f"🚫 [ALTERNATING] Usuário bloqueou o bot ao reenviar. Parando.")
                            mensagem_id = None
                            break
                        falhas_consecutivas += 1
                        logger.error(f"❌ [ALTERNATING] Falha ao reenviar ({falhas_consecutivas}/{MAX_FALHAS}): {e2}")
                else:
                    falhas_consecutivas += 1
                    logger.error(f"❌ [ALTERNATING] Erro ao editar ({falhas_consecutivas}/{MAX_FALHAS}): {e}")
                
                # 🔥 Muitas falhas consecutivas — parar para não ficar em loop
                if falhas_consecutivas >= MAX_FALHAS:
                    logger.error(f"🛑 [ALTERNATING] {MAX_FALHAS} falhas consecutivas. Abortando para chat {chat_id}.")
                    break
                    
            except Exception as e:
                falhas_consecutivas += 1
                logger.error(f"❌ [ALTERNATING] Erro inesperado ({falhas_consecutivas}/{MAX_FALHAS}): {e}")
                if falhas_consecutivas >= MAX_FALHAS:
                    logger.error(f"🛑 [ALTERNATING] {MAX_FALHAS} falhas consecutivas. Abortando.")
                    break
            
            # ✅ AVANÇAR PARA A PRÓXIMA MENSAGEM NO CICLO
            mensagem_index = (mensagem_index + 1) % total_mensagens
        
        # FIM DO CICLO: Auto-destruir mensagem final se configurado
        if auto_destruct_final and mensagem_id:
            # ✅ BUSCAR O TEMPO CORRETO DO BANCO
            db = SessionLocal()
            try:
                alt_config = db.query(AlternatingMessages).filter(
                    AlternatingMessages.bot_id == bot_id
                ).first()
                
                tempo_destruicao = alt_config.last_message_destruct_seconds if alt_config else 60
                
            except Exception as e:
                logger.error(f"❌ Erro ao buscar tempo de destruição: {e}")
                tempo_destruicao = 60  # Fallback seguro
            finally:
                db.close()
            
            logger.info(f"🗑️ [ALTERNATING-FIM] Mensagem final (ID: {mensagem_id}) será destruída em {tempo_destruicao}s")
            asyncio.create_task(
                delayed_delete_message(token, chat_id, mensagem_id, tempo_destruicao)
            )
        
        tempo_total_decorrido = (now_brazil() - tempo_inicio).total_seconds()
        logger.info(f"✅ [ALTERNATING-CONCLUÍDO] Tempo decorrido: {tempo_total_decorrido:.0f}s")
        
    except asyncio.CancelledError:
        logger.info(f"🛑 [ALTERNATING] Task cancelada - Chat: {chat_id}")
    except Exception as e:
        logger.error(f"❌ [ALTERNATING] Erro fatal: {e}", exc_info=True)
    finally:
        # Limpar task do dicionário
        with remarketing_lock:
            if chat_id in alternating_tasks:
                del alternating_tasks[chat_id]

# ============================================================
# 🔄 JOBS DE DISPARO AUTOMÁTICO (CORE LÓGICO)
# ============================================================

async def send_remarketing_job(
    bot_token: str,
    chat_id: int,
    config_dict: dict,
    user_info: dict,
    bot_id: int
):
    """
    VERSÃO DE TESTE: Trava de envio diário DESATIVADA.
    """
    try:
        delay = config_dict.get('delay_minutes', 5)
        # Aguarda o tempo configurado
        await asyncio.sleep(delay * 60)
        
        db = SessionLocal()
        try:
            # 1. Verifica se o usuário JÁ PAGOU
            pagou = db.query(Pedido).filter(
                Pedido.bot_id == bot_id, 
                Pedido.telegram_id == str(chat_id), 
                Pedido.status.in_(['paid', 'active', 'approved'])
            ).first()
            
            if pagou:
                logger.info(f"💰 [REMARKETING] Cancelado: Usuário {chat_id} já pagou.")
                return

            # ==============================================================================
            # 🚨 MODO TESTE ATIVADO: A verificação de "Já enviou hoje" foi desativada abaixo
            # para permitir múltiplos disparos. Quando for para produção, descomente este bloco.
            # ==============================================================================
            
            # hoje = now_brazil().date()
            # ja_enviou = db.query(RemarketingLog).filter(
            #     RemarketingLog.bot_id == bot_id,
            #     RemarketingLog.user_id == str(chat_id), 
            #     func.date(RemarketingLog.sent_at) == hoje
            # ).first()

            # if ja_enviou:
            #     logger.info(f"⏭️ [REMARKETING] Já enviado hoje para {chat_id}")
            #     return
            
            # ==============================================================================

            # 3. Prepara a mensagem
            msg_text = config_dict.get('message_text', '')
            if user_info:
                msg_text = msg_text.replace('{first_name}', user_info.get('first_name', ''))
                msg_text = msg_text.replace('{plano_original}', user_info.get('plano', 'VIP'))
                msg_text = msg_text.replace('{valor_original}', str(user_info.get('valor', '')))

            # ✨ CONVERTE EMOJIS PREMIUM
            msg_text = convert_premium_emojis(msg_text)

            # ✅ NOVO: Aplicar preço promocional temporariamente
            db_session = SessionLocal()
            try:
                promos = config_dict.get('promo_values', {})
                for plano_id_str, promo_data in promos.items():
                    if isinstance(promo_data, dict) and promo_data.get('price'):
                        plano = db_session.query(PlanoConfig).filter(
                            PlanoConfig.id == int(plano_id_str)
                        ).first()
                        if plano:
                            # Salva preço original temporariamente
                            plano._preco_promocional = promo_data['price']
            finally:
                db_session.close()

            # 4. Prepara os Botões com PREÇO EMBUTIDO
            markup = types.InlineKeyboardMarkup()
            promos = config_dict.get('promo_values', {})
            for pid, pdata in promos.items():
                if isinstance(pdata, dict) and pdata.get('price'):
                    btn_txt = pdata.get('button_text', 'Ver Oferta 🔥')
                    # ✅ Envia plano_id E preço em centavos
                    preco_centavos = int(pdata['price'] * 100)
                    markup.add(types.InlineKeyboardButton(
                        btn_txt, 
                        callback_data=f"checkout_promo_{pid}_{preco_centavos}"
                    ))

            # 5. Envia a Mensagem
            bot = TeleBot(bot_token, threaded=False)
            sent_msg = None
            
            media = config_dict.get('media_url')
            mtype = config_dict.get('media_type')
            
            try:
                # 🔥 LÓGICA ATUALIZADA COM SUPORTE A ÁUDIO E ASYNCIO
                if media and mtype == 'photo':
                    sent_msg = bot.send_photo(chat_id, media, caption=msg_text, reply_markup=markup, parse_mode='HTML')
                elif media and mtype == 'video':
                    sent_msg = bot.send_video(chat_id, media, caption=msg_text, reply_markup=markup, parse_mode='HTML')
                elif media and (mtype == 'audio' or is_audio_file(media)):
                    # 🔊 ÁUDIO: Baixa e envia como bytes para garantir voice note nativo
                    try:
                        audio_bytes, _fname, _audio_dur = _download_audio_bytes(media)
                        
                        # 🔥 CORREÇÃO ASYNC: Loop de chat action
                        _wait = min(max(_audio_dur, 2), 60) if _audio_dur > 0 else 3
                        await _async_sleep_with_action(bot, chat_id, _wait, 'record_voice')
                        
                        if audio_bytes:
                            voice_msg = bot.send_voice(chat_id, audio_bytes)
                        else:
                            voice_msg = bot.send_voice(chat_id, media)
                        sent_msg = voice_msg
                        
                        # Envia texto e botões separadamente
                        if msg_text or markup:
                            await asyncio.sleep(2)
                            if msg_text and markup:
                                sent_msg = bot.send_message(chat_id, msg_text, reply_markup=markup, parse_mode='HTML')
                            elif msg_text:
                                sent_msg = bot.send_message(chat_id, msg_text, parse_mode='HTML')
                            elif markup:
                                sent_msg = bot.send_message(chat_id, "⬇️ Escolha uma opção:", reply_markup=markup)
                    except Exception as e_audio:
                        logger.error(f"❌ Erro envio áudio remarketing: {e_audio}")
                        sent_msg = bot.send_message(chat_id, msg_text or "...", reply_markup=markup, parse_mode='HTML')
                else:
                    sent_msg = bot.send_message(chat_id, msg_text, reply_markup=markup, parse_mode='HTML')
                
                # REGISTRO NO BANCO
                novo_log = RemarketingLog(
                    bot_id=bot_id, 
                    user_id=str(chat_id),
                    message_sent=msg_text,
                    status='sent', 
                    sent_at=now_brazil(),
                    promo_values=promos,
                    converted=False,
                    error_message=None
                )
                db.add(novo_log)
                db.commit()
                
                logger.info(f"📨 [REMARKETING] Enviado com sucesso para {chat_id}")
                
                # ==============================================================================
                # 6. Auto destruição (LÓGICA CORRIGIDA E UNIFICADA - VERSÃO MESTRE)
                # ==============================================================================
                is_enabled = config_dict.get('auto_destruct_enabled', False)
                destruct_seconds = config_dict.get('auto_destruct_seconds', 0)
                after_click = config_dict.get('auto_destruct_after_click', True)

                # Só entra aqui se estiver HABILITADO e tiver tempo configurado
                if is_enabled and destruct_seconds > 0 and sent_msg:
                    
                    if after_click:
                        # --- MODO: DESTRUIR APÓS CLIQUE ---
                        # Usamos o MESMO dicionário da função síncrona para que o callback funcione igual
                        if not hasattr(enviar_remarketing_automatico, 'pending_destructions'):
                            enviar_remarketing_automatico.pending_destructions = {}
                        
                        # Armazena usando STR e INT por segurança (conforme corrigimos no callback)
                        dados_destruicao = {
                            'message_id': sent_msg.message_id,
                            'buttons_message_id': None, # Async envia botões junto, não separado
                            'bot_instance': bot, # Instância do TeleBot
                            'destruct_seconds': destruct_seconds
                        }
                        
                        # Salva na memória global para o Callback pegar
                        enviar_remarketing_automatico.pending_destructions[chat_id] = dados_destruicao
                        enviar_remarketing_automatico.pending_destructions[str(chat_id)] = dados_destruicao
                        
                        logger.info(f"💣 [ASYNC] Auto-destruição agendada APÓS CLIQUE para {chat_id}")
                        
                    else:
                        # --- MODO: DESTRUIR IMEDIATAMENTE (TIMER) ---
                        logger.info(f"⏳ [ASYNC] Auto-destruição iniciada: {destruct_seconds}s")
                        await asyncio.sleep(destruct_seconds)
                        try: 
                            bot.delete_message(chat_id, sent_msg.message_id)
                            logger.info(f"🗑️ [ASYNC] Mensagem deletada automaticamente para {chat_id}")
                        except Exception as e_del: 
                            logger.warning(f"⚠️ Erro ao auto-deletar (Async): {e_del}")
                
                # ==============================================================================

            except Exception as e_send:
                # Registrar falha no banco
                try:
                    log_erro = RemarketingLog(
                        bot_id=bot_id,
                        user_id=str(chat_id),
                        message_sent=msg_text,
                        status='error',
                        error_message=str(e_send),
                        sent_at=now_brazil(),
                        converted=False
                    )
                    db.add(log_erro)
                    db.commit()
                except:
                    pass
                logger.error(f"❌ [REMARKETING] Erro no envio Telegram: {e_send}")
                # 🔥 Classifica erros para logs mais limpos
                err_remarketing = str(e_send).lower()
                if "blocked" in err_remarketing or "403" in err_remarketing:
                    logger.info(f"🚫 [REMARKETING] Usuário {chat_id} bloqueou o bot. Ignorando.")
                elif "chat not found" in err_remarketing:
                    logger.info(f"ℹ️ [REMARKETING] Chat {chat_id} não encontrado. Ignorando.")

        except Exception as e_db:
            logger.error(f"❌ [REMARKETING] Erro de Banco/Lógica: {e_db}")
        finally:
            db.close()

    except asyncio.CancelledError:
        pass
    except Exception as e:
        logger.error(f"❌ [REMARKETING] Erro crítico: {e}")
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
        # ✅ LOGS DE DEBUG:
        logger.info(f"🔔 [SCHEDULE] Iniciando agendamento - Bot: {bot_id}, Chat: {chat_id}")
        
        db = SessionLocal()
        try:
            # Busca config
            config = db.query(RemarketingConfig).filter(
                RemarketingConfig.bot_id == bot_id
            ).first()
            
            if not config:
                class DummyConfig:
                    is_active = False
                    message_text = ""
                    media_url = ""
                    media_type = None
                    delay_minutes = 30
                    auto_destruct_enabled = False
                    auto_destruct_seconds = 0
                    auto_destruct_after_click = False
                    promo_values = {}
                config = DummyConfig()

            # Valida Bot
            bot = db.query(BotModel).filter(BotModel.id == bot_id).first()
            if not bot or not bot.token:
                logger.error(f"❌ [SCHEDULE] Bot {bot_id} não encontrado ou sem token")
                return
            
            config_dict = {
                'message_text': config.message_text, 
                'media_url': config.media_url, 
                'media_type': config.media_type,
                'delay_minutes': config.delay_minutes, 
                'auto_destruct_enabled': config.auto_destruct_enabled,
                'auto_destruct_seconds': config.auto_destruct_seconds,
                'auto_destruct_after_click': config.auto_destruct_after_click,
                'promo_values': config.promo_values or {}
            }

            # ==================================================================
            # 1. Agenda Mensagens Alternantes
            # ==================================================================
            alt_config = db.query(AlternatingMessages).filter(
                AlternatingMessages.bot_id == bot_id, 
                AlternatingMessages.is_active == True
            ).first()
            
            if alt_config and alt_config.messages:
                logger.info(f"✅ [SCHEDULE] Mensagens alternantes ativadas - {len(alt_config.messages)} mensagens")
                
                # ✅ LOG DE DEBUG DOS VALORES SALVOS
                logger.info(f"🔍 [SCHEDULE-DEBUG] Config salva: {alt_config.log_config_values()}")
                
                agora = now_brazil()
                
                # 🧠 LÓGICA DE TEMPO CORRIGIDA:
                if config.is_active:
                    # Se remarketing ATIVO: Para X segundos antes do disparo
                    delay_base_minutes = config.delay_minutes
                    stop_at = agora + timedelta(minutes=delay_base_minutes) - timedelta(seconds=alt_config.stop_before_remarketing_seconds)
                    logger.info(f"⏰ [SCHEDULE] Modo: Remarketing Ativo. Parar em: {stop_at.strftime('%H:%M:%S')}")
                else:
                    # ✅ USAR O CAMPO DIRETO DO MODELO
                    duracao_rotacao = alt_config.max_duration_minutes
                    logger.info(f"🔍 [SCHEDULE-DEBUG-CRITICO] max_duration_minutes do banco: {duracao_rotacao}")
                    
                    stop_at = agora + timedelta(minutes=duracao_rotacao)
                    logger.info(f"⏰ [SCHEDULE] Modo: Remarketing Inativo. Rotação por {duracao_rotacao} min. Parar em: {stop_at.strftime('%H:%M:%S')}")
                
                loop = asyncio.get_event_loop()
                task = loop.create_task(start_alternating_messages_job(
                    bot.token, 
                    chat_id, 
                    payment_message_id, 
                    alt_config.messages, 
                    alt_config.rotation_interval_seconds, 
                    stop_at, 
                    alt_config.last_message_auto_destruct,  # ✅ CAMPO CORRETO
                    bot_id
                ))
                with remarketing_lock: 
                    alternating_tasks[chat_id] = task
                
                logger.info(f"✅ [SCHEDULE] Task de alternating iniciada")
            else:
                logger.info(f"ℹ️ [SCHEDULE] Mensagens alternantes desativadas")

            # ==================================================================
            # 2. Agenda Remarketing Automático
            # ==================================================================
            if config.is_active:
                logger.info(f"⏰ [SCHEDULE] Agendando remarketing para daqui a {config.delay_minutes} minutos")
                
                loop = asyncio.get_event_loop()
                task = loop.create_task(send_remarketing_job(
                    bot.token, 
                    chat_id, 
                    config_dict, 
                    user_info, 
                    bot_id
                ))
                with remarketing_lock: 
                    remarketing_timers[chat_id] = task
                
                logger.info(f"✅ [SCHEDULE] Task de remarketing criada")
            else:
                logger.info(f"⏸️ [SCHEDULE] Remarketing principal está INATIVO.")

        finally: 
            db.close()
            
    except Exception as e: 
        logger.error(f"❌ [SCHEDULE] Erro: {e}", exc_info=True)

# ============================================================
# CONFIGURAÇÃO DO SCHEDULER (MOVIDO PARA CÁ)
# ============================================================

# =========================================================
# 🔄 SYNC PAY POLLING: VERIFICAR PAGAMENTOS PENDENTES
# =========================================================
async def verificar_pagamentos_syncpay():
    """
    🔥 SOLUÇÃO DEFINITIVA: Consulta a API da Sync Pay para verificar
    se transações pendentes já foram pagas.
    
    A Sync Pay NEM SEMPRE envia o webhook de confirmação (cashin.update).
    Este job roda a cada 30 segundos e faz polling ativo.
    """
    db = SessionLocal()
    try:
        # Buscar pedidos PENDENTES que usaram Sync Pay (criados nas últimas 2 horas)
        from datetime import timedelta
        limite = now_brazil() - timedelta(hours=2)
        
        pedidos_pendentes = db.query(Pedido).filter(
            Pedido.status == "pending",
            Pedido.gateway_usada == "syncpay",
            Pedido.created_at >= limite
        ).all()
        
        if not pedidos_pendentes:
            return  # Nada para verificar
        
        logger.info(f"🔄 [SYNCPAY-POLL] Verificando {len(pedidos_pendentes)} pedidos pendentes...")
        
        for pedido in pedidos_pendentes:
            try:
                # Buscar o bot para obter credenciais
                bot = db.query(BotModel).filter(BotModel.id == pedido.bot_id).first()
                if not bot or not bot.syncpay_client_id:
                    continue
                
                # Obter token da Sync Pay
                token = await obter_token_syncpay(bot, db)
                if not token:
                    continue
                
                # Consultar status da transação na API
                tx_id = pedido.transaction_id or pedido.txid
                if not tx_id:
                    continue
                
                url = f"{SYNC_PAY_BASE_URL}/api/partner/v1/transaction/{tx_id}"
                headers = {
                    "Authorization": f"Bearer {token}",
                    "Accept": "application/json"
                }
                
                async with httpx.AsyncClient() as client:
                    response = await client.get(url, headers=headers, timeout=10)
                
                if response.status_code != 200:
                    logger.warning(f"⚠️ [SYNCPAY-POLL] Erro ao consultar {tx_id}: HTTP {response.status_code}")
                    continue
                
                resp_data = response.json()
                tx_data = resp_data.get("data", resp_data)
                status_api = str(tx_data.get("status", "")).lower()
                
                logger.info(f"🔍 [SYNCPAY-POLL] Pedido #{pedido.id} ({tx_id[:12]}...): status = {status_api}")
                
                if status_api == "completed":
                    logger.info(f"✅ [SYNCPAY-POLL] PAGAMENTO CONFIRMADO! Pedido #{pedido.id}. Processando entrega...")
                    
                    # Simular webhook_pix internamente
                    try:
                        import io
                        fake_payload = json.dumps({
                            "data": {
                                "id": tx_id,
                                "status": "completed",
                                "amount": pedido.valor
                            }
                        }).encode("utf-8")
                        
                        scope = {
                            "type": "http",
                            "method": "POST",
                            "path": "/webhook/pix",
                            "headers": [
                                (b"content-type", b"application/json"),
                                (b"event", b"cashin.update"),
                            ],
                        }
                        
                        class FakeReceive:
                            def __init__(self, body):
                                self._body = body
                                self._sent = False
                            async def __call__(self):
                                if not self._sent:
                                    self._sent = True
                                    return {"type": "http.request", "body": self._body}
                                return {"type": "http.disconnect"}
                        
                        fake_request = Request(scope, receive=FakeReceive(fake_payload))
                        
                        # Usar uma sessão nova para não conflitar
                        db_new = SessionLocal()
                        try:
                            result = await webhook_pix(fake_request, db_new)
                            logger.info(f"✅ [SYNCPAY-POLL] Resultado: {result}")
                        finally:
                            db_new.close()
                    
                    except Exception as e_process:
                        logger.error(f"❌ [SYNCPAY-POLL] Erro ao processar pedido #{pedido.id}: {e_process}", exc_info=True)
                
                elif status_api == "failed":
                    pedido.status = "failed"
                    db.commit()
                    logger.info(f"❌ [SYNCPAY-POLL] Pedido #{pedido.id} marcado como FAILED")
                
            except Exception as e_pedido:
                logger.error(f"❌ [SYNCPAY-POLL] Erro ao verificar pedido #{pedido.id}: {e_pedido}")
                continue
        
    except Exception as e:
        logger.error(f"❌ [SYNCPAY-POLL] Erro geral: {e}")
    finally:
        db.close()

scheduler = AsyncIOScheduler(timezone='America/Sao_Paulo')

# Adicionar jobs
scheduler.add_job(
    verificar_vencimentos,
    'interval',
    minutes=5,  # 🔥 CORREÇÃO: De hours=12 para minutes=5. Checa vencimentos o tempo todo!
    id='verificar_vencimentos',
    replace_existing=True
)

scheduler.add_job(
    processar_webhooks_pendentes,
    'interval',
    minutes=1,
    id='webhook_retry_processor',
    replace_existing=True
)

scheduler.add_job(
    cleanup_orphan_jobs,
    'interval',
    hours=1,
    id='cleanup_remarketing_jobs',
    replace_existing=True
)

logger.info("✅ [SCHEDULER] Job de vencimentos agendado (5 min)")
logger.info("✅ [SCHEDULER] Job de retry de webhooks agendado (1 min)")
logger.info("✅ [SCHEDULER] Job de cleanup de remarketing agendado (1h)")

# 🔥 SYNC PAY POLLING: Verifica pagamentos pendentes a cada 30s
scheduler.add_job(
    verificar_pagamentos_syncpay,
    'interval',
    seconds=30,
    id='syncpay_polling',
    replace_existing=True
)
logger.info("✅ [SCHEDULER] Job de polling Sync Pay agendado (30s)")

# ========================================
# 🔧 AUXILIAR: DELETE ATRASADO (MENSAGEM FINAL)
# ========================================
async def delayed_delete_message(token: str, chat_id: int, message_id: int, delay: int):
    """
    Aguarda X segundos e apaga a mensagem especificada.
    Usado para a auto-destruição da última mensagem alternante.
    🔥 V2: Trata mensagem já deletada/usuário bloqueado sem logar como erro
    """
    try:
        if delay > 0:
            await asyncio.sleep(delay)
            
        bot_del = TeleBot(token, threaded=False)
        bot_del.delete_message(chat_id, message_id)
        logger.info(f"💣 Mensagem {message_id} destruída com sucesso.")
    except ApiTelegramException as e:
        err_str = str(e).lower()
        if "message to delete not found" in err_str or "message can't be deleted" in err_str:
            logger.info(f"ℹ️ Mensagem {message_id} já foi deletada (pelo usuário ou Telegram).")
        elif "blocked" in err_str or "403" in err_str:
            logger.info(f"ℹ️ Não foi possível deletar mensagem {message_id} — usuário bloqueou o bot.")
        else:
            logger.error(f"❌ Erro ao destruir mensagem {message_id}: {e}")
    except Exception as e:
        logger.error(f"❌ Erro ao destruir mensagem {message_id}: {e}")

# ========================================
# 🔄 JOB: MENSAGENS ALTERNANTES (GLOBAL - V7 FINAL)
# ========================================
async def enviar_mensagens_alternantes():
    """
    Envia mensagens alternantes. 
    V7: Correção do nome do token (bot_db.token) e lógica de tempo.
    """
    db = SessionLocal()
    try:
        # Auto-Migração
        try:
            db.execute(text("ALTER TABLE alternating_messages ADD COLUMN IF NOT EXISTS last_message_auto_destruct BOOLEAN DEFAULT FALSE"))
            db.execute(text("ALTER TABLE alternating_messages ADD COLUMN IF NOT EXISTS last_message_destruct_seconds INTEGER DEFAULT 60"))
            db.execute(text("ALTER TABLE alternating_messages ADD COLUMN IF NOT EXISTS max_duration_minutes INTEGER DEFAULT 60"))
            
            # 🔊 MIGRAÇÃO: Campos de áudio separado (Combo áudio + mídia)
            db.execute(text("ALTER TABLE remarketing_config ADD COLUMN IF NOT EXISTS audio_url VARCHAR(500)"))
            db.execute(text("ALTER TABLE remarketing_config ADD COLUMN IF NOT EXISTS audio_delay_seconds INTEGER DEFAULT 3"))
            db.execute(text("ALTER TABLE canal_free_config ADD COLUMN IF NOT EXISTS audio_url VARCHAR(500)"))
            db.execute(text("ALTER TABLE canal_free_config ADD COLUMN IF NOT EXISTS audio_delay_seconds INTEGER DEFAULT 3"))
            db.execute(text("ALTER TABLE order_bump_config ADD COLUMN IF NOT EXISTS audio_url VARCHAR"))
            db.execute(text("ALTER TABLE order_bump_config ADD COLUMN IF NOT EXISTS audio_delay_seconds INTEGER DEFAULT 3"))
            db.execute(text("ALTER TABLE upsell_config ADD COLUMN IF NOT EXISTS audio_url VARCHAR"))
            db.execute(text("ALTER TABLE upsell_config ADD COLUMN IF NOT EXISTS audio_delay_seconds INTEGER DEFAULT 3"))
            db.execute(text("ALTER TABLE downsell_config ADD COLUMN IF NOT EXISTS audio_url VARCHAR"))
            db.execute(text("ALTER TABLE downsell_config ADD COLUMN IF NOT EXISTS audio_delay_seconds INTEGER DEFAULT 3"))
            logger.info("✅ Migração de audio_url concluída para todas as tabelas")
            db.commit()
        except Exception:
            db.rollback()

        bots = db.query(BotModel).all()
        
        for bot_db in bots:
            try:
                # ✅ CORREÇÃO CRÍTICA: O nome correto no model é 'token', não 'telegram_token'
                if not bot_db.token: continue

                # Busca configs
                remarketing_config = db.query(RemarketingConfig).filter(RemarketingConfig.bot_id == bot_db.id).first()
                alt_config = db.query(AlternatingMessages).filter(
                    AlternatingMessages.bot_id == bot_db.id,
                    AlternatingMessages.is_active == True
                ).first()
                
                if not alt_config or not alt_config.messages:
                    continue
                
                intervalo_segundos = alt_config.rotation_interval_seconds or 3600
                max_duration = getattr(alt_config, 'max_duration_minutes', 60)
                
                # 🧠 LÓGICA DE TEMPO DO JOB:
                # Se o lead foi criado há mais tempo que a duração permitida, IGNORA.
                if remarketing_config and remarketing_config.is_active:
                     tempo_limite_criacao = now_brazil() - timedelta(hours=24)
                else:
                     # Se remarketing OFF, usa a duração definida (ex: 1 min)
                     tempo_limite_criacao = now_brazil() - timedelta(minutes=max_duration)

                destruir_ultima = getattr(alt_config, 'last_message_auto_destruct', False)
                tempo_destruicao = getattr(alt_config, 'last_message_destruct_seconds', 60)
                
                # Query leads
                leads_elegiveis = db.query(Lead).outerjoin(
                    Pedido,
                    and_(
                        Lead.user_id == Pedido.telegram_id,
                        Pedido.bot_id == bot_db.id,
                        Pedido.status == "paid"
                    )
                ).filter(
                    Lead.bot_id == bot_db.id,
                    Lead.status != "blocked",
                    Pedido.id == None,
                    Lead.created_at > tempo_limite_criacao # ✅ Isso garante que pare após o tempo definido
                ).all()
                
                if not leads_elegiveis: continue
                
                # ✅ CORREÇÃO: Usando bot_db.token
                bot_temp = TeleBot(bot_db.token, threaded=False)
                bot_temp.parse_mode = "HTML"
                
                for lead in leads_elegiveis:
                    try:
                        state = db.query(AlternatingMessageState).filter(
                            AlternatingMessageState.bot_id == bot_db.id,
                            AlternatingMessageState.user_id == lead.user_id
                        ).first()
                        
                        if not state:
                            state = AlternatingMessageState(
                                bot_id=bot_db.id,
                                user_id=lead.user_id,
                                last_message_index=-1,
                                last_sent_at=now_brazil() - timedelta(days=1)
                            )
                            db.add(state)
                            db.commit()
                            db.refresh(state)
                        
                        mensagens = alt_config.messages
                        total_msgs = len(mensagens)

                        # 🛑 TRAVA DE FIM DE CICLO (Se chegou na última e deve destruir)
                        if destruir_ultima and state.last_message_index >= (total_msgs - 1):
                            continue 
                        
                        # Verifica Intervalo
                        tempo_desde_ultimo = (now_brazil() - state.last_sent_at).total_seconds()
                        if tempo_desde_ultimo < intervalo_segundos:
                            continue
                        
                        # Define mensagem
                        proximo_index = (state.last_message_index + 1) % total_msgs
                        mensagem_atual = mensagens[proximo_index]
                        
                        texto_envio = mensagem_atual if isinstance(mensagem_atual, str) else mensagem_atual.get('content', '')
                        
                        if not texto_envio or not texto_envio.strip(): continue
                        
                        # Envia
                        sent_msg = bot_temp.send_message(lead.user_id, texto_envio)
                        
                        # Lógica da Última Mensagem
                        eh_ultima = (proximo_index == total_msgs - 1)
                        
                        if eh_ultima and destruir_ultima:
                            logger.info(f"💣 [ALTERNATING] Última mensagem enviada para {lead.user_id}. Destruição em {tempo_destruicao}s.")
                            asyncio.create_task(delayed_delete_message(
                                bot_db.token, # ✅ Token correto aqui também
                                lead.user_id, 
                                sent_msg.message_id, 
                                tempo_destruicao
                            ))
                            
                        # Atualiza
                        state.last_message_index = proximo_index
                        state.last_sent_at = now_brazil()
                        db.commit()
                        
                        await asyncio.sleep(0.2)
                        
                    except Exception:
                        continue
                        
            except Exception as e_bot:
                logger.error(f"Erro ao processar bot {bot_db.id}: {e_bot}")
                continue
                
    except Exception as e:
        logger.error(f"❌ Erro crítico no job alternating: {str(e)}")
    finally:
        db.close()

# Agenda o job (mantido)
scheduler.add_job(
    enviar_mensagens_alternantes,
    'interval',
    minutes=5, # Executa a cada 5 min para checar intervalos menores
    id='alternating_messages_job',
    replace_existing=True
)
logger.info("✅ [SCHEDULER] Job de mensagens alternantes agendado (Verificação a cada 5 min)")


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
            "timestamp": now_brazil().isoformat(),
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
                "timestamp": now_brazil().isoformat()
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
    wiinpay_user_id: Optional[str] = None
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
        expire = now_brazil() + expires_delta
    else:
        expire = now_brazil() + timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES)
    
    to_encode.update({"exp": expire})
    encoded_jwt = jwt.encode(to_encode, SECRET_KEY, algorithm=ALGORITHM)
    return encoded_jwt

async def get_current_user(token: str = Depends(oauth2_scheme)):
    """
    Decodifica token e retorna usuário atual.
    🔥 FIX DEFINITIVO: Usa make_transient() para desconectar o objeto da sessão
    mantendo TODOS os atributos carregados acessíveis após fechar a sessão.
    """
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
        from sqlalchemy.orm.session import make_transient
        
        # 🔥 EAGER LOADING para carregar bots ANTES de fechar sessão
        user = db.query(User).options(
            joinedload(User.bots)
        ).filter(User.id == user_id).first()
        
        if user is None:
            raise credentials_exception
        
        # 🔥 Forçar o carregamento COMPLETO de relações e atributos escalares
        _ = user.bots
        _ = [b.id for b in user.bots]
        _ = user.is_superuser
        _ = user.username
        _ = user.is_active
        _ = user.id
        _ = getattr(user, 'pushin_pay_id', None)
        _ = getattr(user, 'taxa_venda', None)
        _ = getattr(user, 'full_name', None)
        _ = getattr(user, 'plano_plataforma', 'free')
        _ = getattr(user, 'max_bots', 20)
        
        # 🔥 FIX DEFINITIVO: Expunge + make_transient
        db.expunge(user)
        make_transient(user)
        for bot in user.bots:
            db.expunge(bot)
            make_transient(bot)
        
        return user
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"❌ Erro ao carregar usuário (ID: {user_id}): {e}")
        import traceback
        logger.error(traceback.format_exc())
        raise credentials_exception
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


# =========================================================
# 🛡️ VERIFICAÇÃO DE USUÁRIO ATIVO
# =========================================================
def get_current_active_user(current_user: User = Depends(get_current_user)):
    """
    Verifica se o usuário logado está ativo.
    Essencial para o sistema de Roles funcionar.
    """
    if not current_user.is_active:
        raise HTTPException(status_code=400, detail="Usuário inativo")
    return current_user


# =========================================================
# 🛡️ DECORATOR DE PERMISSÃO (RBAC)
# =========================================================
def require_role(allowed_roles: list):
    """
    Bloqueia a rota se o usuário não tiver um dos cargos permitidos.
    Uso: current_user = Depends(require_role(["SUPER_ADMIN", "ADMIN"]))
    """
    def role_checker(user: User = Depends(get_current_active_user)):
        # Super Admin (legado ou novo) tem passe livre
        if getattr(user, 'is_superuser', False) or getattr(user, 'role', 'USER') == 'SUPER_ADMIN':
            return user
        
        # Verifica se o cargo do usuário está na lista permitida
        user_role = getattr(user, 'role', 'USER')
        if user_role not in allowed_roles:
            raise HTTPException(
                status_code=403,
                detail=f"Acesso negado. Necessário cargo: {allowed_roles}"
            )
        return user
    
    return role_checker


# =========================================================
# 📋 SCHEMAS PARA CONFIG GLOBAL E BROADCAST
# =========================================================
class SystemConfigSchema(BaseModel):
    default_fee: int = 60
    master_pushin_pay_id: str = ""
    master_wiinpay_user_id: str = ""
    maintenance_mode: bool = False

class BroadcastSchema(BaseModel):
    title: str
    message: str
    type: str = "info"

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
                "auto_destruct_enabled": False,
                "auto_destruct_seconds": 3,
                "auto_destruct_after_click": True,
                "promo_values": {},
                "audio_url": None,
                "audio_delay_seconds": 3
            }
        
        return {
            "id": config.id,
            "bot_id": config.bot_id,
            "is_active": config.is_active,
            "message_text": config.message_text,
            "media_url": config.media_url,
            "media_type": config.media_type,
            "delay_minutes": config.delay_minutes,
            "auto_destruct_enabled": config.auto_destruct_enabled,
            "auto_destruct_seconds": config.auto_destruct_seconds,
            "auto_destruct_after_click": config.auto_destruct_after_click,
            "promo_values": config.promo_values or {},
            "audio_url": getattr(config, 'audio_url', None),
            "audio_delay_seconds": getattr(config, 'audio_delay_seconds', 3),
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
            config.auto_destruct_enabled = data.get("auto_destruct_enabled", config.auto_destruct_enabled)
            config.auto_destruct_seconds = data.get("auto_destruct_seconds", config.auto_destruct_seconds)
            config.auto_destruct_after_click = data.get("auto_destruct_after_click", config.auto_destruct_after_click)
            config.promo_values = data.get("promo_values", config.promo_values)
            config.audio_url = data.get("audio_url", config.audio_url)
            config.audio_delay_seconds = data.get("audio_delay_seconds", config.audio_delay_seconds)
            config.updated_at = now_brazil()
        else:
            config = RemarketingConfig(
                bot_id=bot_id,
                is_active=data.get("is_active", False),
                message_text=data.get("message_text", ""),
                media_url=data.get("media_url"),
                media_type=data.get("media_type"),
                delay_minutes=delay_minutes,
                auto_destruct_enabled=data.get("auto_destruct_enabled", False),
                auto_destruct_seconds=data.get("auto_destruct_seconds", 3),
                auto_destruct_after_click=data.get("auto_destruct_after_click", True),
                promo_values=data.get("promo_values", {}),
                audio_url=data.get("audio_url"),
                audio_delay_seconds=data.get("audio_delay_seconds", 3)
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
            "auto_destruct_enabled": config.auto_destruct_enabled,
            "auto_destruct_seconds": config.auto_destruct_seconds,
            "auto_destruct_after_click": config.auto_destruct_after_click,
            "promo_values": config.promo_values,
            "audio_url": config.audio_url,
            "audio_delay_seconds": config.audio_delay_seconds,
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
                "auto_destruct_final": False,
                # 🔥 NOVOS CAMPOS COM VALORES PADRÃO
                "max_duration_minutes": 60,
                "last_message_auto_destruct": False,
                "last_message_destruct_seconds": 60
            }
        
        return {
            "id": config.id,
            "bot_id": config.bot_id,
            "is_active": config.is_active,
            "messages": config.messages or [],
            "rotation_interval_seconds": config.rotation_interval_seconds,
            "stop_before_remarketing_seconds": config.stop_before_remarketing_seconds,
            "auto_destruct_final": config.auto_destruct_final,
            # 🔥 RETORNAR NOVOS CAMPOS (CORREÇÃO DO BUG)
            "max_duration_minutes": config.max_duration_minutes,
            "last_message_auto_destruct": config.last_message_auto_destruct,
            "last_message_destruct_seconds": config.last_message_destruct_seconds,
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
            # ✅ ATUALIZAR TODOS OS CAMPOS (incluindo os novos)
            config.is_active = data.get("is_active", config.is_active)
            config.messages = messages
            config.rotation_interval_seconds = data.get("rotation_interval_seconds", config.rotation_interval_seconds)
            config.stop_before_remarketing_seconds = data.get("stop_before_remarketing_seconds", config.stop_before_remarketing_seconds)
            config.auto_destruct_final = data.get("auto_destruct_final", config.auto_destruct_final)
            # ✅ NOVOS CAMPOS
            config.max_duration_minutes = data.get("max_duration_minutes", config.max_duration_minutes)
            config.last_message_auto_destruct = data.get("last_message_auto_destruct", config.last_message_auto_destruct)
            config.last_message_destruct_seconds = data.get("last_message_destruct_seconds", config.last_message_destruct_seconds)
            config.updated_at = now_brazil()
        else:
            # ✅ CRIAR COM TODOS OS CAMPOS (incluindo os novos)
            config = AlternatingMessages(
                bot_id=bot_id,
                is_active=data.get("is_active", False),
                messages=messages,
                rotation_interval_seconds=data.get("rotation_interval_seconds", 15),
                stop_before_remarketing_seconds=data.get("stop_before_remarketing_seconds", 60),
                auto_destruct_final=data.get("auto_destruct_final", False),
                # ✅ NOVOS CAMPOS
                max_duration_minutes=data.get("max_duration_minutes", 60),
                last_message_auto_destruct=data.get("last_message_auto_destruct", False),
                last_message_destruct_seconds=data.get("last_message_destruct_seconds", 60)
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
            # ✅ RETORNAR NOVOS CAMPOS
            "max_duration_minutes": config.max_duration_minutes,
            "last_message_auto_destruct": config.last_message_auto_destruct,
            "last_message_destruct_seconds": config.last_message_destruct_seconds,
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
    """Retorna estatísticas de remarketing (Versão Corrigida: Coluna 'nome')"""
    try:
        bot = db.query(BotModel).filter(BotModel.id == bot_id).first()
        if not bot:
            raise HTTPException(status_code=404, detail="Bot não encontrado")
        
        if bot.owner_id != current_user.id and not current_user.is_superuser:
            raise HTTPException(status_code=403, detail="Acesso negado")
        
        # 1. Métricas
        total_sent = db.query(RemarketingLog).filter(RemarketingLog.bot_id == bot_id).count()
        
        total_converted = db.query(RemarketingLog).filter(
            RemarketingLog.bot_id == bot_id,
            RemarketingLog.converted == True
        ).count()
        
        conversion_rate = (total_converted / total_sent * 100) if total_sent > 0 else 0
        
        hoje = now_brazil().date()
        today_sent = db.query(RemarketingLog).filter(
            RemarketingLog.bot_id == bot_id,
            func.date(RemarketingLog.sent_at) == hoje
        ).count()

        # 2. Receita
        receita_query = db.query(func.sum(Pedido.valor)).join(
            RemarketingLog,
            and_(
                RemarketingLog.user_id == Pedido.telegram_id,
                RemarketingLog.bot_id == Pedido.bot_id,
                RemarketingLog.converted == True
            )
        ).filter(Pedido.bot_id == bot_id, Pedido.status == 'paid')
        
        total_revenue = receita_query.scalar() or 0.0

        # 3. Logs Recentes + Nome do Lead (Usando tabela Lead com coluna 'nome')
        results = db.query(RemarketingLog, Lead).outerjoin(
            Lead, 
            and_(
                Lead.user_id == RemarketingLog.user_id, 
                Lead.bot_id == bot_id
            )
        ).filter(
            RemarketingLog.bot_id == bot_id
        ).order_by(RemarketingLog.sent_at.desc()).limit(20).all()
        
        recent_data = []
        
        for log, lead in results:
            # Lógica para definir o nome de exibição
            display_name = log.user_id # Fallback é o ID
            username_display = ""
            
            if lead:
                # ✅ CORREÇÃO AQUI: Usando 'nome' em vez de 'first_name'
                if lead.nome:
                    display_name = lead.nome
                elif lead.username:
                    display_name = lead.username
                
                if lead.username:
                    username_display = f"(@{lead.username})"

            recent_data.append({
                "id": log.id,
                "user_id": log.user_id,
                "user_name": display_name,       
                "user_username": username_display,
                "sent_at": log.sent_at.isoformat(),
                "status": log.status,
                "converted": log.converted,
                "error_message": getattr(log, 'error_message', None)
            })
        
        return {
            "total_sent": total_sent,
            "total_converted": total_converted,
            "conversion_rate": round(conversion_rate, 2),
            "today_sent": today_sent,
            "total_revenue": total_revenue,
            "logs": recent_data,
            "recent_logs": recent_data
        }
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"❌ Erro stats: {str(e)}")
        # Retorna zerado para não quebrar a tela se houver outro erro
        return {
            "total_sent": 0, "total_converted": 0, "conversion_rate": 0,
            "today_sent": 0, "total_revenue": 0, "logs": [], "recent_logs": []
        }
        
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
    
    agora = now_brazil()
    
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
        pedido.escolheu_plano_em = now_brazil()
        pedido.gerou_pix_em = now_brazil()
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
                try:
                    remarketing_timers[chat_id].cancel()
                except:
                    pass
                del remarketing_timers[chat_id]
                canceled.append('remarketing')
            
            # Cancela alternating
            if chat_id in alternating_tasks:
                try:
                    alternating_tasks[chat_id].cancel()
                except:
                    pass
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
    
    agora = now_brazil()
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
    agora = now_brazil()
    
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
                # ============================================================
                # 🔥 [CORREÇÃO 0 - NOVO] SISTEMA DE LIMITES E PLANO DA PLATAFORMA
                # ============================================================
                "ALTER TABLE users ADD COLUMN IF NOT EXISTS plano_plataforma VARCHAR DEFAULT 'free';",
                "ALTER TABLE users ADD COLUMN IF NOT EXISTS max_bots INTEGER DEFAULT 20;",

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
                "ALTER TABLE miniapp_categories ADD COLUMN IF NOT EXISTS model_name VARCHAR;",
                "ALTER TABLE miniapp_categories ADD COLUMN IF NOT EXISTS model_desc TEXT;",
                "ALTER TABLE miniapp_categories ADD COLUMN IF NOT EXISTS footer_banner_url VARCHAR;",
                "ALTER TABLE miniapp_categories ADD COLUMN IF NOT EXISTS deco_lines_url VARCHAR;",
                "ALTER TABLE miniapp_categories ADD COLUMN IF NOT EXISTS model_name_color VARCHAR DEFAULT '#ffffff';",
                "ALTER TABLE miniapp_categories ADD COLUMN IF NOT EXISTS model_desc_color VARCHAR DEFAULT '#cccccc';",

                # --- [MINI APP V2] SEPARADOR, PAGINAÇÃO, FORMATO E FAKE VIDEO ---
                "ALTER TABLE miniapp_categories ADD COLUMN IF NOT EXISTS items_per_page INTEGER;",
                "ALTER TABLE miniapp_categories ADD COLUMN IF NOT EXISTS separator_enabled BOOLEAN DEFAULT FALSE;",
                "ALTER TABLE miniapp_categories ADD COLUMN IF NOT EXISTS separator_color VARCHAR DEFAULT '#ffffff';",
                "ALTER TABLE miniapp_categories ADD COLUMN IF NOT EXISTS separator_text VARCHAR;",
                "ALTER TABLE miniapp_categories ADD COLUMN IF NOT EXISTS separator_btn_text VARCHAR;",
                "ALTER TABLE miniapp_categories ADD COLUMN IF NOT EXISTS separator_btn_url VARCHAR;",
                "ALTER TABLE miniapp_categories ADD COLUMN IF NOT EXISTS separator_logo_url VARCHAR;",
                "ALTER TABLE miniapp_categories ADD COLUMN IF NOT EXISTS model_img_shape VARCHAR DEFAULT 'square';",

                # --- [CORREÇÃO 10] TOKEN PUSHINPAY E ORDER BUMP ---
                "ALTER TABLE bots ADD COLUMN IF NOT EXISTS pushin_token VARCHAR;",
                "ALTER TABLE order_bump_config ADD COLUMN IF NOT EXISTS autodestruir BOOLEAN DEFAULT FALSE;",

                # --- [CORREÇÃO 10.1] 🆕 MULTI-GATEWAY (WIINPAY + CONTINGÊNCIA) ---
                "ALTER TABLE bots ADD COLUMN IF NOT EXISTS wiinpay_api_key VARCHAR;",
                "ALTER TABLE bots ADD COLUMN IF NOT EXISTS gateway_principal VARCHAR DEFAULT 'pushinpay';",
                "ALTER TABLE bots ADD COLUMN IF NOT EXISTS gateway_fallback VARCHAR;",
                "ALTER TABLE bots ADD COLUMN IF NOT EXISTS pushinpay_ativo BOOLEAN DEFAULT FALSE;",
                "ALTER TABLE bots ADD COLUMN IF NOT EXISTS wiinpay_ativo BOOLEAN DEFAULT FALSE;",
                "ALTER TABLE users ADD COLUMN IF NOT EXISTS wiinpay_user_id VARCHAR;",
                "ALTER TABLE pedidos ADD COLUMN IF NOT EXISTS gateway_usada VARCHAR;",

                # 🔒 PROTEÇÃO DE CONTEÚDO
                "ALTER TABLE bots ADD COLUMN IF NOT EXISTS protect_content BOOLEAN DEFAULT FALSE;",

                # 👇👇👇 [CORREÇÃO 11] SUPORTE A WEB APP NO FLUXO (CRÍTICO) 👇👇👇
                "ALTER TABLE bot_flows ADD COLUMN IF NOT EXISTS start_mode VARCHAR DEFAULT 'padrao';",
                "ALTER TABLE bot_flows ADD COLUMN IF NOT EXISTS miniapp_url VARCHAR;",
                "ALTER TABLE bot_flows ADD COLUMN IF NOT EXISTS miniapp_btn_text VARCHAR DEFAULT 'ABRIR LOJA 🛍️';",

                # ============================================================
                # 🔥 [CORREÇÃO 12] SOLUÇÃO DEFINITIVA REMARKETING LOGS 🔥
                # ============================================================
                # 1. Cria a tabela COMPLETA se não existir
                """
                CREATE TABLE IF NOT EXISTS remarketing_logs (
                    id SERIAL PRIMARY KEY,
                    bot_id INTEGER REFERENCES bots(id),
                    user_id VARCHAR NOT NULL,
                    sent_at TIMESTAMP WITHOUT TIME ZONE DEFAULT (NOW() AT TIME ZONE 'utc'),
                    message_text TEXT,
                    promo_values JSON,
                    status VARCHAR(20) DEFAULT 'sent',
                    error_message TEXT,
                    converted BOOLEAN DEFAULT FALSE,
                    converted_at TIMESTAMP WITHOUT TIME ZONE,
                    message_sent BOOLEAN DEFAULT TRUE,
                    campaign_id VARCHAR
                );
                """,
                
                # 2. Se a tabela já existir velha, ADICIONA AS COLUNAS FALTANTES NA MARRA
                "ALTER TABLE remarketing_logs ADD COLUMN IF NOT EXISTS user_id VARCHAR;",
                "ALTER TABLE remarketing_logs ADD COLUMN IF NOT EXISTS message_text TEXT;",
                "ALTER TABLE remarketing_logs ADD COLUMN IF NOT EXISTS promo_values JSON;",
                "ALTER TABLE remarketing_logs ADD COLUMN IF NOT EXISTS status VARCHAR(20) DEFAULT 'sent';",
                "ALTER TABLE remarketing_logs ADD COLUMN IF NOT EXISTS error_message TEXT;",
                "ALTER TABLE remarketing_logs ADD COLUMN IF NOT EXISTS converted BOOLEAN DEFAULT FALSE;",
                "ALTER TABLE remarketing_logs ADD COLUMN IF NOT EXISTS converted_at TIMESTAMP WITHOUT TIME ZONE;",
                "ALTER TABLE remarketing_logs ADD COLUMN IF NOT EXISTS message_sent BOOLEAN DEFAULT TRUE;",
                "ALTER TABLE remarketing_logs ADD COLUMN IF NOT EXISTS campaign_id VARCHAR;",

                # 3. MIGRAÇÃO DE DADOS: Se existir user_telegram_id, copia para user_id
                """
                DO $$
                BEGIN
                    IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name='remarketing_logs' AND column_name='user_telegram_id') THEN
                        UPDATE remarketing_logs SET user_id = CAST(user_telegram_id AS VARCHAR) WHERE user_id IS NULL;
                    END IF;
                END $$;
                """,

                # ============================================================
                # 🚀 [CORREÇÃO 13] TABELAS UPSELL E DOWNSELL
                # ============================================================
                """
                CREATE TABLE IF NOT EXISTS upsell_config (
                    id SERIAL PRIMARY KEY,
                    bot_id INTEGER UNIQUE REFERENCES bots(id),
                    ativo BOOLEAN DEFAULT FALSE,
                    nome_produto VARCHAR,
                    preco FLOAT,
                    link_acesso VARCHAR,
                    delay_minutos INTEGER DEFAULT 2,
                    msg_texto TEXT DEFAULT '🔥 Oferta exclusiva para você!',
                    msg_media VARCHAR,
                    btn_aceitar VARCHAR DEFAULT '✅ QUERO ESSA OFERTA!',
                    btn_recusar VARCHAR DEFAULT '❌ NÃO, OBRIGADO',
                    autodestruir BOOLEAN DEFAULT FALSE,
                    created_at TIMESTAMP WITHOUT TIME ZONE DEFAULT (NOW() AT TIME ZONE 'utc'),
                    updated_at TIMESTAMP WITHOUT TIME ZONE DEFAULT (NOW() AT TIME ZONE 'utc')
                );
                """,
                """
                CREATE TABLE IF NOT EXISTS downsell_config (
                    id SERIAL PRIMARY KEY,
                    bot_id INTEGER UNIQUE REFERENCES bots(id),
                    ativo BOOLEAN DEFAULT FALSE,
                    nome_produto VARCHAR,
                    preco FLOAT,
                    link_acesso VARCHAR,
                    delay_minutos INTEGER DEFAULT 10,
                    msg_texto TEXT DEFAULT '🎁 Última chance! Oferta especial só para você!',
                    msg_media VARCHAR,
                    btn_aceitar VARCHAR DEFAULT '✅ QUERO ESSA OFERTA!',
                    btn_recusar VARCHAR DEFAULT '❌ NÃO, OBRIGADO',
                    autodestruir BOOLEAN DEFAULT FALSE,
                    created_at TIMESTAMP WITHOUT TIME ZONE DEFAULT (NOW() AT TIME ZONE 'utc'),
                    updated_at TIMESTAMP WITHOUT TIME ZONE DEFAULT (NOW() AT TIME ZONE 'utc')
                );
                """,

                # ============================================================
                # 📅 [CORREÇÃO 14a] REMARKETING AGENDADO
                # ============================================================
                "ALTER TABLE remarketing_campaigns ADD COLUMN IF NOT EXISTS is_scheduled BOOLEAN DEFAULT FALSE;",
                "ALTER TABLE remarketing_campaigns ADD COLUMN IF NOT EXISTS schedule_days INTEGER DEFAULT 1;",
                "ALTER TABLE remarketing_campaigns ADD COLUMN IF NOT EXISTS schedule_time VARCHAR DEFAULT '10:00';",
                "ALTER TABLE remarketing_campaigns ADD COLUMN IF NOT EXISTS schedule_end_date TIMESTAMP WITHOUT TIME ZONE;",
                "ALTER TABLE remarketing_campaigns ADD COLUMN IF NOT EXISTS days_config TEXT;",  # JSON: [{day:1, msg, media_url, plano_id, promo_price}, ...]
                "ALTER TABLE remarketing_campaigns ADD COLUMN IF NOT EXISTS use_same_content BOOLEAN DEFAULT TRUE;",
                "ALTER TABLE remarketing_campaigns ADD COLUMN IF NOT EXISTS schedule_active BOOLEAN DEFAULT FALSE;",

                # 🚨 [CORREÇÃO 14b] SISTEMA DE DENÚNCIAS
                # ============================================================
                """
                CREATE TABLE IF NOT EXISTS reports (
                    id SERIAL PRIMARY KEY,
                    reporter_name VARCHAR(100),
                    reporter_telegram_id VARCHAR(50),
                    bot_username VARCHAR(100) NOT NULL,
                    bot_id INTEGER REFERENCES bots(id) ON DELETE SET NULL,
                    reason VARCHAR(50) NOT NULL,
                    description TEXT,
                    evidence_url VARCHAR(500),
                    status VARCHAR(20) DEFAULT 'pending',
                    resolution TEXT,
                    resolved_by INTEGER REFERENCES users(id) ON DELETE SET NULL,
                    resolved_at TIMESTAMP WITHOUT TIME ZONE,
                    action_taken VARCHAR(50),
                    strike_count INTEGER DEFAULT 0,
                    created_at TIMESTAMP WITHOUT TIME ZONE DEFAULT (NOW() AT TIME ZONE 'America/Sao_Paulo'),
                    updated_at TIMESTAMP WITHOUT TIME ZONE DEFAULT (NOW() AT TIME ZONE 'America/Sao_Paulo'),
                    ip_address VARCHAR(50)
                );
                """,
                """
                CREATE TABLE IF NOT EXISTS user_strikes (
                    id SERIAL PRIMARY KEY,
                    user_id INTEGER REFERENCES users(id) ON DELETE CASCADE NOT NULL,
                    report_id INTEGER REFERENCES reports(id) ON DELETE SET NULL,
                    reason TEXT NOT NULL,
                    strike_number INTEGER NOT NULL,
                    action VARCHAR(50) NOT NULL,
                    pause_until TIMESTAMP WITHOUT TIME ZONE,
                    tax_increase_pct FLOAT,
                    applied_by INTEGER REFERENCES users(id) ON DELETE SET NULL,
                    created_at TIMESTAMP WITHOUT TIME ZONE DEFAULT (NOW() AT TIME ZONE 'America/Sao_Paulo')
                );
                """,
                # Coluna de strikes no users (para acesso rápido)
                "ALTER TABLE users ADD COLUMN IF NOT EXISTS strike_count INTEGER DEFAULT 0;",
                "ALTER TABLE users ADD COLUMN IF NOT EXISTS is_banned BOOLEAN DEFAULT FALSE;",
                "ALTER TABLE users ADD COLUMN IF NOT EXISTS banned_reason TEXT;",
                "ALTER TABLE users ADD COLUMN IF NOT EXISTS bots_paused_until TIMESTAMP WITHOUT TIME ZONE;",
                
                # 🔥 [NOVO] COLUNAS V2: Identificação do Dono do Bot nas Denúncias
                "ALTER TABLE reports ADD COLUMN IF NOT EXISTS owner_id INTEGER REFERENCES users(id) ON DELETE SET NULL;",
                "ALTER TABLE reports ADD COLUMN IF NOT EXISTS owner_username VARCHAR(100);",
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

    # 3. Inicia o Agendador (Scheduler) - Jobs já registrados no nível do módulo
    try:
        if 'scheduler' in globals():
            # Jobs já adicionados (verificar_vencimentos, webhook_retry, cleanup_remarketing)
            # Apenas adiciona o remarketing se existir a função
            try:
                scheduler.add_job(executar_remarketing, 'interval', minutes=30, id='executar_remarketing', replace_existing=True)
            except Exception:
                pass
            logger.info("⏰ [STARTUP] Agendador de tarefas configurado.")
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
                tb = telebot.TeleBot(bot_data.token, threaded=False)
                
                # Tratamento ROBUSTO do ID do canal
                try: 
                    raw_id = str(bot_data.id_canal_vip).strip()
                    canal_id = int(raw_id)
                except: 
                    logger.error(f"ID do canal inválido para o bot {bot_data.nome}")
                    continue
                
                agora = now_brazil()
                
                # Busca usuários vencidos - verifica AMBOS os campos
                vencidos = db.query(Pedido).filter(
                    Pedido.bot_id == bot_data.id,
                    Pedido.status.in_(['paid', 'approved', 'active']),
                    or_(
                        and_(Pedido.custom_expiration != None, Pedido.custom_expiration < agora),
                        and_(Pedido.custom_expiration == None, Pedido.data_expiracao != None, Pedido.data_expiracao < agora)
                    )
                ).all()
                
                # Pré-carregar grupos extras do bot (uma vez por bot, evita N+1 queries)
                grupos_extras = db.query(BotGroup).filter(
                    BotGroup.bot_id == bot_data.id,
                    BotGroup.is_active == True
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
                        
                        # 🔥 V2: Determinar canal correto (específico do plano ou padrão)
                        canal_remocao_ceifador = canal_id  # Default do bot
                        if u.plano_id:
                            try:
                                plano_ceif = db.query(PlanoConfig).filter(PlanoConfig.id == int(u.plano_id)).first()
                                if plano_ceif and plano_ceif.id_canal_destino and str(plano_ceif.id_canal_destino).strip() not in ("", "None", "null"):
                                    canal_remocao_ceifador = int(str(plano_ceif.id_canal_destino).strip())
                                    logger.info(f"🎯 [CEIFADOR] Canal específico do plano '{plano_ceif.nome_exibicao}': {canal_remocao_ceifador}")
                            except:
                                pass
                        
                        # 1. Kick Suave do Canal VIP (correto por plano)
                        tb.ban_chat_member(canal_remocao_ceifador, int(u.telegram_id))
                        time.sleep(0.5)
                        tb.unban_chat_member(canal_remocao_ceifador, int(u.telegram_id))
                        
                        # 1b. Se canal do plano é diferente do padrão, remove do padrão também
                        if canal_remocao_ceifador != canal_id:
                            try:
                                tb.ban_chat_member(canal_id, int(u.telegram_id))
                                time.sleep(0.3)
                                tb.unban_chat_member(canal_id, int(u.telegram_id))
                                logger.info(f"👋 [CEIFADOR] Também removido do canal padrão {canal_id}")
                            except:
                                pass
                        
                        # 2. Kick dos Grupos Extras (BotGroup) vinculados ao plano
                        if u.plano_id and grupos_extras:
                            for grupo in grupos_extras:
                                plan_ids = grupo.plan_ids if grupo.plan_ids else []
                                if u.plano_id in plan_ids:
                                    try:
                                        grupo_id = int(str(grupo.group_id).strip())
                                        tb.ban_chat_member(grupo_id, int(u.telegram_id))
                                        time.sleep(0.3)
                                        tb.unban_chat_member(grupo_id, int(u.telegram_id))
                                        logger.info(f"👋 Removido de grupo extra '{grupo.title}': {u.telegram_id}")
                                    except Exception as e_g:
                                        err_g = str(e_g).lower()
                                        if "participant_id_invalid" in err_g or "user not found" in err_g or "user_not_participant" in err_g:
                                            pass
                                        else:
                                            logger.warning(f"⚠️ Erro ao remover do grupo '{grupo.title}': {e_g}")
                        
                        # 3. Atualiza Status
                        u.status = 'expired'
                        
                        # 4. Sincronizar Lead
                        lead = db.query(Lead).filter(
                            Lead.bot_id == u.bot_id,
                            Lead.user_id == str(u.telegram_id)
                        ).first()
                        if lead:
                            lead.status = 'expired'
                        
                        db.commit()
                        
                        # 5. Avisa o usuário
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
                        if "participant_id_invalid" in err_msg or "user not found" in err_msg or "user_not_participant" in err_msg:
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
        now = now_brazil()
        
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
                    retry_item.updated_at = now_brazil()
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
                retry_item.updated_at = now_brazil()
                
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
        first_retry = now_brazil() + timedelta(minutes=1)
        
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
# 🏢 BUSCAR PUSHIN PAY ID DA PLATAFORMA (ZENYX)
# =========================================================
def get_plataforma_pushin_id(db: Session) -> str:
    """
    Retorna o pushin_pay_id da plataforma Zenyx para receber as taxas.
    Prioridade:
    1. SystemConfig (master_pushin_pay_id OU pushin_plataforma_id)
    2. Primeiro Super Admin com pushin_pay_id
    3. None se não encontrar
    """
    try:
        # 1. Tenta buscar da SystemConfig (ambas as keys possíveis)
        for key_name in ["master_pushin_pay_id", "pushin_plataforma_id"]:
            config = db.query(SystemConfig).filter(
                SystemConfig.key == key_name
            ).first()
            if config and config.value and config.value.strip():
                return config.value.strip()
        
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
# 🏢 BUSCAR SYNC PAY ID DA PLATAFORMA (ZENYX)
# =========================================================
def get_plataforma_syncpay_id(db: Session) -> str:
    """
    Retorna o client_id da plataforma Zenyx para receber as taxas via Sync Pay.
    """
    try:
        # 1. Tenta buscar da SystemConfig
        config = db.query(SystemConfig).filter(SystemConfig.key == "master_syncpay_client_id").first()
        if config and config.value and config.value.strip():
            return config.value.strip()
        
        # 2. Busca o primeiro Super Admin com syncpay_client_id configurado (Fallback)
        from database import User
        super_admin = db.query(User).filter(
            User.is_superuser == True,
            User.syncpay_client_id.isnot(None)
        ).first()
        
        if super_admin and super_admin.syncpay_client_id:
            return super_admin.syncpay_client_id
        
        logger.warning("⚠️ Nenhum Client ID da plataforma Sync Pay configurado! Split desabilitado.")
        return None
        
    except Exception as e:
        logger.error(f"Erro ao buscar syncpay_client_id da plataforma: {e}")
        return None

# =========================================================
# 🔄 PROCESSAMENTO BACKGROUND DE REMARKETING
# =========================================================
# =========================================================
# 🔌 INTEGRAÇÃO SYNC PAY (NOVA)
# =========================================================
SYNC_PAY_BASE_URL = "https://api.syncpayments.com.br"

async def obter_token_syncpay(bot, db: Session):
    """
    Verifica se o token da Sync Pay ainda é válido.
    Se não for (ou não existir), faz uma requisição assíncrona para gerar um novo.
    """
    agora = now_brazil()
    
    # Se o token existir e a data de expiração for maior que agora (margem de 5 min)
    if bot.syncpay_access_token and bot.syncpay_token_expires_at:
        from datetime import timedelta
        expires_at = bot.syncpay_token_expires_at
        if expires_at.tzinfo is None:
            expires_at = expires_at.replace(tzinfo=agora.tzinfo)
            
        if expires_at > (agora + timedelta(minutes=5)):
            return bot.syncpay_access_token

    url = f"{SYNC_PAY_BASE_URL}/api/partner/v1/auth-token"
    payload = {
        "client_id": bot.syncpay_client_id,
        "client_secret": bot.syncpay_client_secret
    }
    headers = {"Content-Type": "application/json"}
    
    try:
        async with httpx.AsyncClient() as client:
            response = await client.post(url, json=payload, headers=headers, timeout=15)
        
        if response.status_code != 200:
            logger.error(f"❌ [SYNC PAY ERRO AUTH] HTTP {response.status_code}: {response.text}")
            return None

        data = response.json()
        
        if "access_token" in data:
            novo_token = data["access_token"]
            expires_in_seconds = data.get("expires_in", 3600)
            
            # Atualiza no banco de dados
            bot.syncpay_access_token = novo_token
            from datetime import timedelta
            bot.syncpay_token_expires_at = agora + timedelta(seconds=expires_in_seconds)
            
            db.commit()
            return novo_token
        else:
            logger.error(f"❌ [SYNC PAY ERRO AUTH] Resposta sem token: {data}")
            return None
            
    except Exception as e:
        logger.error(f"❌ [SYNC PAY EXCEPTION AUTH] {type(e).__name__}: {str(e)}")
        return None


async def gerar_pix_syncpay(
    valor_float: float, 
    transaction_id: str, 
    bot_id: int, 
    db: Session,
    user_telegram_id: str = None,      
    user_first_name: str = None,       
    plano_nome: str = None,
    agendar_remarketing: bool = True
):
    """Gera o Pix via Sync Pay de forma assíncrona com cálculo de Split em Porcentagem"""
    
    bot = db.query(BotModel).filter(BotModel.id == bot_id).first()
    if not bot: return None

    token = await obter_token_syncpay(bot, db)
    if not token:
        logger.error(f"❌ [SYNC PAY] Falha de autenticação. Verifique as credenciais do bot {bot_id}.")
        return None
        
    url = f"{SYNC_PAY_BASE_URL}/api/partner/v1/cash-in"
    
    # ======================================================================
    # 🔥 Lógica de Split: Converte Centavos em Porcentagem para a Plataforma
    # ======================================================================
    split_array = []
    
    # Pega o ID da plataforma Zenyx
    plataforma_id = get_plataforma_syncpay_id(db)
    
    # 🛡️ PROTEÇÃO 1: Se o Bot usa a MESMA chave da plataforma, NUNCA faz split para não dar Erro 500!
    if plataforma_id and bot.syncpay_client_id and bot.syncpay_client_id.strip() == plataforma_id.strip():
        logger.info(f"ℹ️ [SYNC PAY] O Bot usa a mesma chave Mestra da Plataforma. PIX SEM split.")
    
    elif bot.owner_id and plataforma_id:
        from database import User
        owner = db.query(User).filter(User.id == bot.owner_id).first()
        
        if owner:
            # 🛡️ PROTEÇÃO 2: Se o usuário dono do bot configurou o próprio ID igual ao da plataforma
            owner_sync_id = getattr(owner, 'syncpay_client_id', None)
            if owner_sync_id and owner_sync_id.strip() == plataforma_id.strip():
                logger.info(f"ℹ️ [SYNC PAY] Owner é a própria plataforma. PIX SEM split.")
            else:
                # Descobre o valor da taxa em centavos (Padrão 60)
                taxa_centavos = owner.taxa_venda
                if not taxa_centavos:
                    try:
                        cfg_fee = db.query(SystemConfig).filter(SystemConfig.key == "default_fee").first()
                        taxa_centavos = int(cfg_fee.value) if cfg_fee and cfg_fee.value else 60
                    except:
                        taxa_centavos = 60
                        
                # Converte os centavos em PORCENTAGEM
                taxa_reais = taxa_centavos / 100.0
                porcentagem = int((taxa_reais / valor_float) * 100)
                
                if porcentagem < 1 and taxa_reais > 0:
                    porcentagem = 1 # Mínimo de 1%
                    
                # Segurança: Não cobra mais de 50% de taxa
                if porcentagem >= 50:
                    logger.warning(f"⚠️ [SYNC PAY] Taxa muito alta ({porcentagem}%). Split ignorado.")
                elif porcentagem > 0:
                    split_array.append({
                        "percentage": porcentagem,
                        "user_id": plataforma_id
                    })
                    logger.info(f"✅ [SYNC PAY DEBUG] Split configurado: {porcentagem}% para a Plataforma ({plataforma_id[:8]}...)")

    # Domínio do seu Webhook
    raw_domain = os.getenv("RAILWAY_PUBLIC_DOMAIN", "zenyx-gbs-testesv1-production.up.railway.app")
    clean_domain = raw_domain.replace("https://", "").replace("http://", "").strip("/")
    
    payload = {
        "amount": round(valor_float, 2),
        "description": f"{plano_nome} - Bot {bot.nome}",
        "webhook_url": f"https://{clean_domain}/webhook/pix", # Aponta pro Webhook genérico
        "client": {
            "name": user_first_name or "Cliente Telegram",
            "cpf": "00000000000", 
            "email": f"cliente{user_telegram_id or 'anon'}@telegram.bot",
            "phone": "11999999999"
        }
    }
    
    if split_array:
        payload["split"] = split_array

    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "Accept": "application/json"
    }
    
    try:
        async with httpx.AsyncClient() as client:
            response = await client.post(url, json=payload, headers=headers, timeout=15)
        
        # 🔥 CORREÇÃO MESTRA: Se o token foi invalidado pela Sync Pay antes do tempo, forçamos a renovação
        if response.status_code == 401:
            logger.warning("⚠️ [SYNC PAY] Token expirado no servidor. Forçando renovação automática...")
            
            # Limpa o token velho no banco
            bot.syncpay_access_token = None
            db.commit()
            
            # Pega um token fresquinho
            token = await obter_token_syncpay(bot, db)
            if token:
                headers["Authorization"] = f"Bearer {token}"
                
                # Tenta gerar o PIX mais uma vez
                async with httpx.AsyncClient() as client:
                    response = await client.post(url, json=payload, headers=headers, timeout=15)

        # 🛡️ ESCUDO SUPREMO: Se der 422 ou 500, e tiver split no payload, retenta SEM split para salvar a venda!
        if response.status_code in [422, 500] and "split" in payload:
            logger.warning(f"⚠️ [SYNC PAY] Erro {response.status_code} ({response.text}). Retentando SEM split para salvar a venda!")
            del payload["split"]
            async with httpx.AsyncClient() as fallback_client:
                response = await fallback_client.post(url, json=payload, headers=headers, timeout=15)

        if response.status_code != 200:
            logger.error(f"❌ [SYNC PAY ERRO PIX] HTTP {response.status_code}: {response.text}")
            return None

        data = response.json()
        
        if "pix_code" in data:
            identifier = data.get("identifier", transaction_id)
            logger.info(f"✅ [SYNC PAY] PIX gerado com sucesso! ID: {identifier}")
            
            # Agenda remarketing se solicitado
            if agendar_remarketing and user_telegram_id:
                try:
                    chat_id_int = int(user_telegram_id) if str(user_telegram_id).isdigit() else None
                    if chat_id_int:
                        cancelar_remarketing(chat_id_int)
                        schedule_remarketing_and_alternating(
                            bot_id=bot_id, chat_id=chat_id_int, payment_message_id=0,
                            user_info={'first_name': user_first_name or "Cliente", 'plano': plano_nome, 'valor': valor_float}
                        )
                except: pass

            return {
                "id": identifier,
                "qr_code": data["pix_code"],
                "gateway": "syncpay"
            }
        else:
            logger.error(f"❌ [SYNC PAY ERRO PIX] Resposta sem PIX: {data}")
            return None
            
    except Exception as e:
        logger.error(f"❌ [SYNC PAY EXCEPTION PIX] {type(e).__name__}: {str(e)}")
        return None
# =========================================================
# 🔌 INTEGRAÇÃO PUSHIN PAY (DINÂMICA)
# =========================================================
async def gerar_pix_pushinpay(
    valor_float: float, 
    transaction_id: str, 
    bot_id: int, 
    db: Session,
    user_telegram_id: str = None,      
    user_first_name: str = None,       
    plano_nome: str = None,
    agendar_remarketing: bool = True
):
    """
    Gera PIX com Split automático de taxa para a plataforma + Remarketing integrado.
    
    Args:
        valor_float: Valor do PIX em reais (ex: 9.00)
        transaction_id: ID único da transação
        bot_id: ID do bot que está gerando o PIX
        db: Sessão do banco de dados
        user_telegram_id: ID do usuário no Telegram (para remarketing)
        user_first_name: Nome do usuário (para remarketing)
        plano_nome: Nome do plano escolhido (para remarketing)
        agendar_remarketing: Se deve agendar remarketing automático
    
    Returns:
        dict: Resposta da API Pushin Pay ou None em caso de erro
    """
    
    # ======================================================================
    # 🔥 ETAPA 1: Buscar o Bot e definir Token
    # ======================================================================
    bot = db.query(BotModel).filter(BotModel.id == bot_id).first()
    if not bot:
        logger.error(f"❌ Bot {bot_id} não encontrado!")
        return None
    
    # 🔥 USA TOKEN DO BOT (se existir) ou fallback para plataforma
    token = bot.pushin_token if bot.pushin_token else get_pushin_token()
    
    # 🔥 LOG DEBUG 1
    logger.info(f"🔍 [DEBUG] Bot ID: {bot_id}")
    logger.info(f"🔍 [DEBUG] Bot tem token? {'SIM ('+str(len(bot.pushin_token or ''))+' chars)' if bot.pushin_token else 'NÃO'}")
    if not bot.pushin_token:
        logger.warning(f"⚠️ [DEBUG] USANDO TOKEN DA PLATAFORMA (fallback)!")
    else:
        logger.info(f"✅ [DEBUG] USANDO TOKEN DO USUÁRIO: {token[:10]}...")
    
    if not token:
        logger.error("❌ NENHUM token disponível (nem do bot, nem da plataforma)!")
        return None
    
    # ======================================================================
    # 🔥 ETAPA 2: Configurar requisição
    # ======================================================================
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
    
    # ======================================================================
    # 🔥 ETAPA 3: Lógica de Split (TAXA DA PLATAFORMA)
    # ======================================================================
    try:
        if bot.owner_id:
            # Busca o dono do bot (membro)
            from database import User
            owner = db.query(User).filter(User.id == bot.owner_id).first()
            
            if owner:
                # Busca o pushin_pay_id da PLATAFORMA (para receber a taxa)
                plataforma_id = get_plataforma_pushin_id(db)
                
                if plataforma_id:
                    # ⚠️ PROTEÇÃO: Se o pushin_pay_id do owner é O MESMO da plataforma,
                    # significa que o bot pertence à plataforma — não cobra taxa de si mesmo.
                    owner_pushin_id = getattr(owner, 'pushin_pay_id', None)
                    if owner_pushin_id and owner_pushin_id.strip() == plataforma_id.strip():
                        logger.info(f"ℹ️ [DEBUG] Owner é a própria plataforma. PIX SEM split (mesma conta).")
                    else:
                        # Define a taxa: 1) User personalizada, 2) Config Global, 3) Padrão 60
                        taxa_centavos = owner.taxa_venda
                        if not taxa_centavos:
                            try:
                                cfg_fee = db.query(SystemConfig).filter(SystemConfig.key == "default_fee").first()
                                taxa_centavos = int(cfg_fee.value) if cfg_fee and cfg_fee.value else 60
                            except:
                                taxa_centavos = 60
                        
                        # 🔥 CALCULA TAXA DA PUSHINPAY (aproximadamente 3%)
                        taxa_pushinpay_estimada = int(valor_centavos * 0.03)
                        valor_disponivel_estimado = valor_centavos - taxa_pushinpay_estimada
                        
                        # 🔥 LOG DEBUG 2
                        logger.info(f"🔍 [DEBUG] Checando split:")
                        logger.info(f"  Valor total: R$ {valor_centavos/100:.2f} ({valor_centavos} centavos)")
                        logger.info(f"  Taxa PushinPay estimada (~3%): R$ {taxa_pushinpay_estimada/100:.2f}")
                        logger.info(f"  Valor disponível estimado: R$ {valor_disponivel_estimado/100:.2f}")
                        logger.info(f"  Sua taxa desejada: R$ {taxa_centavos/100:.2f} ({taxa_centavos} centavos)")
                        logger.info(f"  Percentual da sua taxa: {(taxa_centavos/valor_centavos)*100:.1f}%")
                        
                        # 🔥 VALIDAÇÃO: Sua taxa não pode ser maior que o valor disponível
                        if taxa_centavos >= valor_disponivel_estimado:
                            logger.warning(f"⚠️ [DEBUG] Taxa ({taxa_centavos}) >= Valor Disponível Estimado ({valor_disponivel_estimado}).")
                            logger.warning(f"   💡 SUGESTÃO: Use valores ≥ R$ 2,00 para garantir que o split funcione.")
                            logger.warning(f"   Split NÃO será aplicado nesta venda.")
                        else:
                            # ✅ Monta o split_rules
                            payload["split_rules"] = [
                                {
                                    "value": taxa_centavos,
                                    "account_id": plataforma_id,
                                    "charge_processing_fee": False
                                }
                            ]
                            
                            # 🔥 LOG DEBUG 3
                            logger.info(f"✅ [DEBUG] SPLIT CONFIGURADO!")
                            logger.info(f"  Split value: {taxa_centavos} centavos")
                            logger.info(f"  Account ID: {plataforma_id}")
                            logger.info(f"  Usuário receberá (estimado): R$ {(valor_disponivel_estimado - taxa_centavos)/100:.2f}")
                else:
                    logger.warning("⚠️ [DEBUG] Pushin Pay ID da plataforma não configurado. Gerando PIX SEM split.")
            else:
                logger.warning(f"⚠️ [DEBUG] Owner do bot {bot_id} não encontrado. Gerando PIX SEM split.")
        else:
            logger.warning(f"⚠️ [DEBUG] Bot {bot_id} sem owner_id. Gerando PIX SEM split.")
            
    except Exception as e:
        logger.error(f"❌ Erro ao configurar split: {e}. Gerando PIX SEM split.")
    
    # ======================================================================
    # 🔥 ETAPA 4: Envia requisição para PushinPay COM RETRY
    # ======================================================================
    max_retries = 3
    retry_count = 0
    last_error = None
    
    while retry_count < max_retries:
        try:
            # 🔥 LOG DEBUG 4
            if retry_count > 0:
                logger.warning(f"🔄 Tentativa {retry_count + 1}/{max_retries} de gerar PIX...")
            else:
                logger.info(f"📤 [DEBUG] Enviando para PushinPay:")
                logger.info(f"  Token usado: {token[:10]}...")
                logger.info(f"  Payload split_rules: {payload.get('split_rules', [])}")
            
            logger.info(f"📤 Gerando PIX de R$ {valor_float:.2f}. Webhook: https://{seus_dominio}/webhook/pix")
            
            # 🔥 TIMEOUT AUMENTADO: De 10s para 30s
            response = await http_client.post(url, json=payload, headers=headers, timeout=30)
            
            if response.status_code in [200, 201]:
                try:
                    pix_response = response.json()
                    
                    # 🔥 VALIDAÇÕES ESSENCIAIS
                    if not pix_response:
                        raise ValueError("Resposta vazia da API PushinPay")
                    
                    if not pix_response.get('id'):
                        raise ValueError("Resposta sem ID do PIX")
                    
                    # 🔥 LOG DEBUG 5
                    logger.info(f"✅ [DEBUG] Resposta PushinPay ({response.status_code}):")
                    logger.info(f"  PIX ID: {pix_response.get('id')}")
                    logger.info(f"  Split retornado: {pix_response.get('split_rules', [])}")
                    if not pix_response.get('split_rules'):
                        logger.warning(f"⚠️ [DEBUG] API NÃO RETORNOU SPLIT!")
                    
                    logger.info(f"✅ PIX gerado com sucesso! ID: {pix_response.get('id')}")
                    
                    # ======================================================================
                    # 🔥 ETAPA 5: Agendamento de Remarketing (se solicitado)
                    # ======================================================================
                    if agendar_remarketing and user_telegram_id:
                        try:
                            chat_id_int = int(user_telegram_id) if str(user_telegram_id).isdigit() else None
                            
                            if chat_id_int:
                                # Cancela agendamentos anteriores
                                cancelar_remarketing(chat_id_int)
                                
                                # Agenda novo ciclo
                                schedule_remarketing_and_alternating(
                                    bot_id=bot_id,
                                    chat_id=chat_id_int,
                                    payment_message_id=0,
                                    user_info={
                                        'first_name': user_first_name or "Cliente",
                                        'plano': plano_nome or "Plano",
                                        'valor': valor_float
                                    }
                                )
                                logger.info(f"📧 [REMARKETING] Ciclo iniciado para {user_first_name}")
                        except Exception as e:
                            logger.error(f"❌ Erro ao agendar remarketing: {e}")
                    
                    return pix_response
                    
                except (ValueError, KeyError, json.JSONDecodeError) as validation_error:
                    logger.error(f"❌ Resposta inválida da API PushinPay: {validation_error}")
                    logger.error(f"   Resposta recebida: {response.text[:500]}")
                    return None
                    
            elif response.status_code == 429:
                # Rate Limit - Espera mais tempo antes de retry
                wait_time = 5 * (retry_count + 1)  # 5s, 10s, 15s
                logger.warning(f"⚠️ Rate Limit (429). Aguardando {wait_time}s antes de retry...")
                await asyncio.sleep(wait_time)
                retry_count += 1
                continue
                
            elif response.status_code in [401, 403]:
                # Erro de autenticação - Não adianta retry
                logger.error(f"❌ Erro de autenticação ({response.status_code}): Token inválido ou sem permissão")
                logger.error(f"   Resposta: {response.text}")
                return None
                
            else:
                logger.error(f"❌ Erro PushinPay ({response.status_code}): {response.text}")
                
                # Para outros erros, tenta retry
                if retry_count < max_retries - 1:
                    wait_time = 2 ** retry_count  # Exponential backoff: 1s, 2s, 4s
                    logger.warning(f"⚠️ Tentando novamente em {wait_time}s...")
                    await asyncio.sleep(wait_time)
                    retry_count += 1
                    continue
                else:
                    return None
                    
        except httpx.TimeoutException as timeout_err:
            last_error = timeout_err
            logger.error(f"⏱️ Timeout na requisição para PushinPay (tentativa {retry_count + 1}/{max_retries})")
            
            if retry_count < max_retries - 1:
                wait_time = 2 ** retry_count
                logger.warning(f"⚠️ Retry em {wait_time}s...")
                await asyncio.sleep(wait_time)
                retry_count += 1
                continue
            else:
                logger.error(f"❌ Todas as {max_retries} tentativas falharam por timeout!")
                logger.error(f"   Erro detalhado: {type(timeout_err).__name__} - {str(timeout_err)}")
                return None
                
        except httpx.ConnectError as conn_err:
            last_error = conn_err
            logger.error(f"🔌 Erro de conexão com PushinPay (tentativa {retry_count + 1}/{max_retries})")
            
            if retry_count < max_retries - 1:
                wait_time = 3 ** retry_count  # 1s, 3s, 9s
                logger.warning(f"⚠️ Retry em {wait_time}s...")
                await asyncio.sleep(wait_time)
                retry_count += 1
                continue
            else:
                logger.error(f"❌ Todas as {max_retries} tentativas falharam por erro de conexão!")
                logger.error(f"   Erro detalhado: {type(conn_err).__name__} - {str(conn_err)}")
                return None
                
        except Exception as e:
            last_error = e
            logger.error(f"❌ Erro inesperado ao gerar PIX (tentativa {retry_count + 1}/{max_retries})")
            logger.error(f"   Tipo: {type(e).__name__}")
            logger.error(f"   Mensagem: {str(e)}")
            logger.error(f"   Traceback: {traceback.format_exc()}")
            
            if retry_count < max_retries - 1:
                wait_time = 2 ** retry_count
                logger.warning(f"⚠️ Retry em {wait_time}s...")
                await asyncio.sleep(wait_time)
                retry_count += 1
                continue
            else:
                logger.error(f"❌ Todas as {max_retries} tentativas falharam!")
                return None
    
    # Se chegou aqui, esgotou todas as tentativas
    logger.error(f"❌ Falha definitiva ao gerar PIX após {max_retries} tentativas")
    if last_error:
        logger.error(f"   Último erro: {type(last_error).__name__} - {str(last_error)}")
    return None
    
# =========================================================
# 🔌 INTEGRAÇÃO WIINPAY (DINÂMICA)
# =========================================================
async def gerar_pix_wiinpay(
    valor_float: float, 
    transaction_id: str, 
    bot_id: int, 
    db: Session,
    user_telegram_id: str = None,      
    user_first_name: str = None,       
    plano_nome: str = None,
    agendar_remarketing: bool = True
):
    """
    Gera PIX via WiinPay com Split automático de taxa para a plataforma.
    
    Diferenças em relação à PushinPay:
    - API Key vai no body (não no header)
    - Valor em reais (float), não centavos
    - Mínimo de R$ 3,00
    - Split usa user_id + value/percentage
    - Webhook retorna status "PAID" (maiúsculo)
    - Campos obrigatórios extras: name, email, description
    """
    
    # ======================================================================
    # 🔥 ETAPA 1: Buscar o Bot e definir API Key
    # ======================================================================
    bot = db.query(BotModel).filter(BotModel.id == bot_id).first()
    if not bot:
        logger.error(f"❌ [WIINPAY] Bot {bot_id} não encontrado!")
        return None
    
    api_key = bot.wiinpay_api_key
    if not api_key:
        logger.error(f"❌ [WIINPAY] Bot {bot_id} sem wiinpay_api_key configurada!")
        return None
    
    logger.info(f"🔍 [WIINPAY DEBUG] Bot ID: {bot_id}")
    logger.info(f"✅ [WIINPAY DEBUG] USANDO API KEY: {api_key[:15]}...")
    
    # ======================================================================
    # 🔥 ETAPA 2: Validação de valor mínimo
    # ======================================================================
    if valor_float < 3.00:
        logger.error(f"❌ [WIINPAY] Valor R$ {valor_float:.2f} abaixo do mínimo de R$ 3,00!")
        return None
    
    # ======================================================================
    # 🔥 ETAPA 3: Configurar requisição
    # ======================================================================
    url = "https://api-v2.wiinpay.com.br/payment/create"
    
    raw_domain = os.getenv("RAILWAY_PUBLIC_DOMAIN", "zenyx-gbs-testesv1-production.up.railway.app")
    clean_domain = raw_domain.replace("https://", "").replace("http://", "").strip("/")
    webhook_url = f"https://{clean_domain}/api/webhooks/wiinpay"
    
    # Monta payload WiinPay
    payload = {
        "api_key": api_key,
        "value": round(valor_float, 2),
        "name": user_first_name or "Cliente",
        "email": f"cliente_{user_telegram_id or 'anon'}@telegram.bot",
        "description": f"Pagamento {plano_nome or 'Plano'} - Bot {bot_id}",
        "webhook_url": webhook_url,
        "metadata": {
            "transaction_id": transaction_id,
            "bot_id": str(bot_id),
            "user_telegram_id": str(user_telegram_id or ""),
            "gateway": "wiinpay"
        }
    }
    
    # ======================================================================
    # 🔥 ETAPA 4: Lógica de Split (TAXA DA PLATAFORMA)
    # ======================================================================
    try:
        if bot.owner_id:
            from database import User
            owner = db.query(User).filter(User.id == bot.owner_id).first()
            
            if owner:
                from database import SystemConfig
                
                # 🎯 SOLUÇÃO DEFINITIVA: Buscando com a chave EXATA do seu banco de dados
                cfg_wiinpay_id = db.query(SystemConfig).filter(SystemConfig.key == "master_wiinpay_user_id").first()
                plataforma_wiinpay_id = cfg_wiinpay_id.value if cfg_wiinpay_id else None
                
                # Fallback de segurança: Puxando direto do Super Admin na tabela Users
                if not plataforma_wiinpay_id:
                    master_user = db.query(User).filter(User.id == 1).first()
                    if master_user:
                        plataforma_wiinpay_id = getattr(master_user, 'wiinpay_user_id', None)
                
                if plataforma_wiinpay_id:
                    # ⚠️ PROTEÇÃO: Se o owner do bot TEM wiinpay_user_id e é O MESMO da plataforma,
                    # significa que a API Key do bot pertence à mesma conta que receberia o split.
                    # A WiinPay rejeita split para a mesma conta (422).
                    owner_wiinpay_id = getattr(owner, 'wiinpay_user_id', None)
                    if owner_wiinpay_id and owner_wiinpay_id.strip() == plataforma_wiinpay_id.strip():
                        logger.info(f"ℹ️ [WIINPAY] Owner é a própria plataforma. PIX SEM split (mesma conta).")
                    else:
                        # Define a taxa
                        taxa_centavos = owner.taxa_venda
                        if not taxa_centavos:
                            try:
                                cfg_fee = db.query(SystemConfig).filter(SystemConfig.key == "default_fee").first()
                                taxa_centavos = int(cfg_fee.value) if cfg_fee and cfg_fee.value else 60
                            except:
                                taxa_centavos = 60
                        
                        # Converte taxa de centavos para reais (WiinPay usa reais)
                        taxa_reais = taxa_centavos / 100.0
                        
                        logger.info(f"🔍 [WIINPAY DEBUG] Checando split:")
                        logger.info(f"  Valor total: R$ {valor_float:.2f}")
                        logger.info(f"  Taxa desejada: R$ {taxa_reais:.2f}")
                        
                        if taxa_reais >= (valor_float * 0.5):
                            logger.warning(f"⚠️ [WIINPAY] Taxa muito alta! Split NÃO aplicado.")
                        else:
                            payload["split"] = {
                                "value": round(taxa_reais, 2),
                                "user_id": plataforma_wiinpay_id
                            }
                            logger.info(f"✅ [WIINPAY DEBUG] SPLIT CONFIGURADO!")
                            logger.info(f"  Split value: R$ {taxa_reais:.2f}")
                            logger.info(f"  User ID: {plataforma_wiinpay_id}")
                else:
                    logger.warning("⚠️ [WIINPAY] WiinPay User ID da plataforma não configurado. PIX SEM split.")
            else:
                logger.warning(f"⚠️ [WIINPAY] Owner do bot {bot_id} não encontrado. PIX SEM split.")
        else:
            logger.warning(f"⚠️ [WIINPAY] Bot {bot_id} sem owner_id. PIX SEM split.")
            
    except Exception as e:
        logger.error(f"❌ [WIINPAY] Erro ao configurar split: {e}. PIX SEM split.")
    
    # ======================================================================
    # 🔥 ETAPA 5: Envia requisição para WiinPay COM RETRY
    # ======================================================================
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json"
    }
    
    max_retries = 3
    retry_count = 0
    last_error = None
    
    while retry_count < max_retries:
        try:
            if retry_count > 0:
                logger.warning(f"🔄 [WIINPAY] Tentativa {retry_count + 1}/{max_retries}...")
            else:
                logger.info(f"📤 [WIINPAY DEBUG] Enviando para WiinPay:")
                logger.info(f"  URL: {url}")
                logger.info(f"  Valor: R$ {valor_float:.2f}")
                logger.info(f"  Split: {payload.get('split', {})}")
            
            response = await http_client.post(url, json=payload, headers=headers, timeout=30)
            
            if response.status_code in [200, 201]:
                try:
                    raw_response = response.json()
                    
                    if not raw_response:
                        raise ValueError("Resposta vazia da API WiinPay")
                    
                    # 🔍 SOLUÇÃO MESTRE AQUI: "Abre" a caixa 'data' se ela existir na resposta.
                    pix_data = raw_response.get('data', raw_response)
                    
                    # Procura o ID na chave que a WiinPay usa (paymentId em camelCase)
                    pix_id = pix_data.get('paymentId') or pix_data.get('payment_id') or pix_data.get('id') or pix_data.get('txid')
                    
                    if not pix_id:
                        raise ValueError(f"Resposta sem ID do PIX: {raw_response}")
                    
                    # 🛠️ TRUQUE NINJA: Criamos a chave 'id' manualmente para o restante do seu sistema achar fácil
                    pix_data['id'] = pix_id
                    
                    logger.info(f"✅ [WIINPAY DEBUG] Resposta WiinPay ({response.status_code}):")
                    logger.info(f"  PIX ID: {pix_id}")
                    logger.info(f"✅ [WIINPAY] PIX gerado com sucesso! ID: {pix_id}")
                    
                    # Agenda remarketing se solicitado
                    if agendar_remarketing and user_telegram_id:
                        try:
                            chat_id_int = int(user_telegram_id) if str(user_telegram_id).isdigit() else None
                            if chat_id_int:
                                cancelar_remarketing(chat_id_int)
                                schedule_remarketing_and_alternating(
                                    bot_id=bot_id,
                                    chat_id=chat_id_int,
                                    payment_message_id=0,
                                    user_info={
                                        'first_name': user_first_name or "Cliente",
                                        'plano': plano_nome or "Plano",
                                        'valor': valor_float
                                    }
                                )
                                logger.info(f"📧 [WIINPAY REMARKETING] Ciclo iniciado para {user_first_name}")
                        except Exception as e:
                            logger.error(f"❌ [WIINPAY] Erro ao agendar remarketing: {e}")
                    
                    # Retornamos o pix_data puro (já contendo o 'id' e 'qr_code' desembrulhados)
                    return pix_data
                    
                except (ValueError, KeyError, json.JSONDecodeError) as validation_error:
                    logger.error(f"❌ [WIINPAY] Resposta inválida: {validation_error}")
                    logger.error(f"   Resposta: {response.text[:500]}")
                    return None
                    
            elif response.status_code == 429:
                wait_time = 5 * (retry_count + 1)
                logger.warning(f"⚠️ [WIINPAY] Rate Limit (429). Aguardando {wait_time}s...")
                await asyncio.sleep(wait_time)
                retry_count += 1
                continue
                
            elif response.status_code in [401, 403]:
                logger.error(f"❌ [WIINPAY] Erro de autenticação ({response.status_code}): API Key inválida")
                logger.error(f"   Resposta: {response.text}")
                return None
                
            elif response.status_code == 422:
                # ⚠️ PROTEÇÃO AUTOMÁTICA: Se o erro é "mesma conta de split",
                # remove o split e retenta UMA VEZ sem split
                resp_text = response.text or ""
                if "mesma conta" in resp_text.lower() and "split" in payload:
                    logger.warning(f"⚠️ [WIINPAY] Split rejeitado (mesma conta). Retentando SEM split...")
                    del payload["split"]
                    retry_count = max_retries - 1  # Só mais uma tentativa
                    continue
                else:
                    logger.error(f"❌ [WIINPAY] Erro 422: {resp_text}")
                    return None
                
            else:
                logger.error(f"❌ [WIINPAY] Erro ({response.status_code}): {response.text}")
                if retry_count < max_retries - 1:
                    wait_time = 2 ** retry_count
                    await asyncio.sleep(wait_time)
                    retry_count += 1
                    continue
                else:
                    return None
                    
        except httpx.TimeoutException as timeout_err:
            last_error = timeout_err
            logger.error(f"⏱️ [WIINPAY] Timeout (tentativa {retry_count + 1}/{max_retries})")
            if retry_count < max_retries - 1:
                await asyncio.sleep(2 ** retry_count)
                retry_count += 1
                continue
            else:
                return None
                
        except Exception as e:
            last_error = e
            logger.error(f"❌ [WIINPAY] Erro inesperado: {type(e).__name__} - {str(e)}")
            if retry_count < max_retries - 1:
                await asyncio.sleep(2 ** retry_count)
                retry_count += 1
                continue
            else:
                return None
    
    logger.error(f"❌ [WIINPAY] Falha definitiva após {max_retries} tentativas")
    return None
# =========================================================
# 🔄 ORQUESTRADOR MULTI-GATEWAY COM CONTINGÊNCIA
# =========================================================
async def gerar_pix_gateway(
    valor_float: float,
    transaction_id: str,
    bot_id: int,
    db: Session,
    user_telegram_id: str = None,
    user_first_name: str = None,
    plano_nome: str = None,
    agendar_remarketing: bool = True
):
    """
    Orquestrador que decide qual gateway usar e implementa fallback automático.
    
    Lógica:
    1. Tenta a gateway_principal do bot
    2. Se falhar e houver gateway_fallback configurada, tenta a segunda
    3. Retorna o resultado + qual gateway foi usada
    
    Returns:
        tuple: (pix_response, gateway_usada) ou (None, None)
    """
    bot = db.query(BotModel).filter(BotModel.id == bot_id).first()
    if not bot:
        logger.error(f"❌ [GATEWAY] Bot {bot_id} não encontrado!")
        return None, None
    
    # Define ordem de tentativa
    principal = bot.gateway_principal or "pushinpay"
    fallback = bot.gateway_fallback
    
    # Verifica quais gateways estão ativas
    gateways_disponiveis = []
    
    if principal == "pushinpay" and bot.pushinpay_ativo and bot.pushin_token:
        gateways_disponiveis.append("pushinpay")
    elif principal == "wiinpay" and bot.wiinpay_ativo and bot.wiinpay_api_key:
        gateways_disponiveis.append("wiinpay")
    elif principal == "syncpay" and bot.syncpay_ativo and bot.syncpay_client_id:
        gateways_disponiveis.append("syncpay")
    
    # Adiciona fallback se diferente da principal e ativa
    if fallback and fallback != principal:
        if fallback == "pushinpay" and bot.pushinpay_ativo and bot.pushin_token:
            gateways_disponiveis.append("pushinpay")
        elif fallback == "wiinpay" and bot.wiinpay_ativo and bot.wiinpay_api_key:
            gateways_disponiveis.append("wiinpay")
        elif fallback == "syncpay" and bot.syncpay_ativo and bot.syncpay_client_id:
            gateways_disponiveis.append("syncpay")
    
    # Se nenhuma gateway multi está ativa, fallback para comportamento legado (pushinpay com token)
    if not gateways_disponiveis:
        if bot.pushin_token:
            gateways_disponiveis = ["pushinpay"]
            logger.warning(f"⚠️ [GATEWAY] Nenhuma gateway explicitamente ativa. Fallback legado PushinPay.")
        else:
            logger.error(f"❌ [GATEWAY] Nenhuma gateway disponível para bot {bot_id}!")
            return None, None
    
    logger.info(f"🔄 [GATEWAY] Bot {bot_id}: Ordem de tentativa = {gateways_disponiveis}")
    
    # Tenta cada gateway na ordem
    for gw in gateways_disponiveis:
        try:
            if gw == "pushinpay":
                logger.info(f"📤 [GATEWAY] Tentando PushinPay...")
                result = await gerar_pix_pushinpay(
                    valor_float=valor_float,
                    transaction_id=transaction_id,
                    bot_id=bot_id,
                    db=db,
                    user_telegram_id=user_telegram_id,
                    user_first_name=user_first_name,
                    plano_nome=plano_nome,
                    agendar_remarketing=agendar_remarketing
                )
                if result:
                    logger.info(f"✅ [GATEWAY] PushinPay respondeu com sucesso!")
                    return result, "pushinpay"
                else:
                    logger.warning(f"⚠️ [GATEWAY] PushinPay falhou. Tentando próxima...")
                    
            elif gw == "wiinpay":
                logger.info(f"📤 [GATEWAY] Tentando WiinPay...")
                result = await gerar_pix_wiinpay(
                    valor_float=valor_float,
                    transaction_id=transaction_id,
                    bot_id=bot_id,
                    db=db,
                    user_telegram_id=user_telegram_id,
                    user_first_name=user_first_name,
                    plano_nome=plano_nome,
                    agendar_remarketing=agendar_remarketing
                )
                if result:
                    logger.info(f"✅ [GATEWAY] WiinPay respondeu com sucesso!")
                    return result, "wiinpay"
                else:
                    logger.warning(f"⚠️ [GATEWAY] WiinPay falhou. Tentando próxima...")
                    
            elif gw == "syncpay":
                logger.info(f"📤 [GATEWAY] Tentando Sync Pay...")
                result = await gerar_pix_syncpay(
                    valor_float=valor_float,
                    transaction_id=transaction_id,
                    bot_id=bot_id,
                    db=db,
                    user_telegram_id=user_telegram_id,
                    user_first_name=user_first_name,
                    plano_nome=plano_nome,
                    agendar_remarketing=agendar_remarketing
                )
                if result:
                    logger.info(f"✅ [GATEWAY] Sync Pay respondeu com sucesso!")
                    return result, "syncpay"
                else:
                    logger.warning(f"⚠️ [GATEWAY] Sync Pay falhou. Tentando próxima...")
                    
        except Exception as e:
            logger.error(f"❌ [GATEWAY] Erro ao tentar {gw}: {e}")
            continue
    
    logger.error(f"❌ [GATEWAY] TODAS as gateways falharam para bot {bot_id}!")
    return None, None


# --- HELPER: Notificar TODOS os Admins (Principal + Extras) ---
def notificar_admin_principal(bot_db: BotModel, mensagem: str):
    """
    Envia notificação para:
    1. Canal de Notificações (se configurado) — mensagem vai para o canal
    2. Admin Principal (DM) — SE notificar_no_bot estiver ativo
    3. Admins Extras (DM) — SE notificar_no_bot estiver ativo
    """
    try:
        sender = telebot.TeleBot(bot_db.token, threaded=False)
    except Exception as e:
        logger.error(f"Falha ao criar bot para notificação: {e}")
        return
    
    # ✅ 1. ENVIO NO CANAL DE NOTIFICAÇÕES (PRIORIDADE)
    if bot_db.id_canal_notificacao and str(bot_db.id_canal_notificacao).strip():
        try:
            canal_notif = str(bot_db.id_canal_notificacao).strip()
            if canal_notif.replace("-", "").isdigit():
                canal_notif = int(canal_notif)
            sender.send_message(canal_notif, mensagem, parse_mode="HTML")
            logger.info(f"📢 Notificação enviada no canal {bot_db.id_canal_notificacao}")
        except Exception as e_canal:
            logger.error(f"❌ Erro ao enviar no canal de notificações {bot_db.id_canal_notificacao}: {e_canal}")
    
    # 🔥 2. ENVIO VIA DM PARA ADMINS (RESPEITA TOGGLE notificar_no_bot)
    notificar_bot = getattr(bot_db, 'notificar_no_bot', True)
    if notificar_bot is None:
        notificar_bot = True
    
    if not notificar_bot:
        logger.info(f"🔕 Notificação no bot desativada para bot {bot_db.id}")
        return
    
    ids_unicos = set()

    if bot_db.admin_principal_id:
        ids_unicos.add(str(bot_db.admin_principal_id).strip())

    try:
        if bot_db.admins:
            for admin in bot_db.admins:
                if admin.telegram_id:
                    ids_unicos.add(str(admin.telegram_id).strip())
    except Exception as e:
        logger.warning(f"Não foi possível ler admins extras: {e}")

    for chat_id in ids_unicos:
        try:
            sender.send_message(chat_id, mensagem, parse_mode="HTML")
        except Exception as e_send:
            logger.error(f"Erro ao notificar admin {chat_id}: {e_send}")

# =========================================================
# 🔌 ROTAS DE INTEGRAÇÃO (AGORA POR BOT)
# =========================================================

# Modelo para receber o JSON do frontend (PushinPay e WiinPay)
class IntegrationUpdate(BaseModel):
    token: str

# 🆕 NOVO: Modelo para receber os dados duplos da Sync Pay do frontend
class SyncPayIntegrationUpdate(BaseModel):
    client_id: str
    client_secret: str

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
    bot.pushinpay_ativo = True  # 🆕 Ativa automaticamente ao salvar
    
    # 🆕 Se não tem gateway principal definida, define PushinPay como principal
    if not bot.gateway_principal or bot.gateway_principal == "":
        bot.gateway_principal = "pushinpay"
    
    db.commit()
    
    logger.info(f"🔑 Token PushinPay atualizado para o BOT {bot.nome}: {token_limpo[:5]}...")
    
    return {"status": "conectado", "msg": f"Integração PushinPay salva para {bot.nome}!"}

# =========================================================
# 🔌 ROTAS DE INTEGRAÇÃO WIINPAY (POR BOT)
# =========================================================

@app.get("/api/admin/integrations/wiinpay/{bot_id}")
def get_wiinpay_status(bot_id: int, db: Session = Depends(get_db)):
    """Retorna status da integração WiinPay para um bot específico."""
    bot = db.query(BotModel).filter(BotModel.id == bot_id).first()
    
    if not bot:
        return {"status": "erro", "msg": "Bot não encontrado"}
    
    api_key = bot.wiinpay_api_key

    if not api_key:
        return {"status": "desconectado", "token_mask": ""}
    
    mask = f"{api_key[:8]}...{api_key[-6:]}" if len(api_key) > 14 else "****"
    return {
        "status": "conectado", 
        "token_mask": mask,
        "ativo": bot.wiinpay_ativo
    }

@app.post("/api/admin/integrations/wiinpay/{bot_id}")
def save_wiinpay_token(bot_id: int, data: IntegrationUpdate, db: Session = Depends(get_db)):
    """Salva API Key da WiinPay para um bot específico."""
    bot = db.query(BotModel).filter(BotModel.id == bot_id).first()
    if not bot:
        raise HTTPException(status_code=404, detail="Bot não encontrado")
    
    api_key_limpa = data.token.strip()
    
    if len(api_key_limpa) < 10:
        return {"status": "erro", "msg": "API Key muito curta ou inválida."}

    bot.wiinpay_api_key = api_key_limpa
    bot.wiinpay_ativo = True  # Ativa automaticamente ao salvar
    
    # Se não tem gateway principal, define WiinPay
    if not bot.gateway_principal or bot.gateway_principal == "":
        bot.gateway_principal = "wiinpay"
    # Se já tem principal (pushinpay), define wiinpay como fallback
    elif bot.gateway_principal == "pushinpay" and not bot.gateway_fallback:
        bot.gateway_fallback = "wiinpay"
    
    db.commit()
    
    logger.info(f"🔑 [WIINPAY] API Key salva para BOT {bot.nome}: {api_key_limpa[:8]}...")
    
    return {"status": "conectado", "msg": f"Integração WiinPay salva para {bot.nome}!"}

# =========================================================
# 🔌 ROTAS DE INTEGRAÇÃO SYNC PAY (NOVA - POR BOT)
# =========================================================

@app.get("/api/admin/integrations/syncpay/{bot_id}")
def get_syncpay_status(bot_id: int, db: Session = Depends(get_db)):
    """Retorna status da integração Sync Pay para um bot específico."""
    bot = db.query(BotModel).filter(BotModel.id == bot_id).first()
    
    if not bot:
        return {"status": "erro", "msg": "Bot não encontrado"}
    
    client_id = bot.syncpay_client_id

    if not client_id:
        return {"status": "desconectado", "client_id_mask": "", "client_secret_mask": ""}
    
    mask_id = f"{client_id[:8]}...{client_id[-4:]}" if len(client_id) > 12 else "****"
    mask_secret = f"****...{bot.syncpay_client_secret[-4:]}" if bot.syncpay_client_secret else "****"
    
    return {
        "status": "conectado", 
        "client_id_mask": mask_id,
        "client_secret_mask": mask_secret,
        "ativo": bot.syncpay_ativo
    }

@app.post("/api/admin/integrations/syncpay/{bot_id}")
def save_syncpay_token(bot_id: int, data: SyncPayIntegrationUpdate, db: Session = Depends(get_db)):
    """Salva credenciais duplas da Sync Pay para um bot específico."""
    bot = db.query(BotModel).filter(BotModel.id == bot_id).first()
    if not bot:
        raise HTTPException(status_code=404, detail="Bot não encontrado")
    
    client_id_limpo = data.client_id.strip()
    client_secret_limpo = data.client_secret.strip()
    
    if len(client_id_limpo) < 10 or len(client_secret_limpo) < 10:
        return {"status": "erro", "msg": "Credenciais muito curtas ou inválidas."}

    bot.syncpay_client_id = client_id_limpo
    bot.syncpay_client_secret = client_secret_limpo
    # Zera o token temporário para forçar uma nova geração com as novas chaves
    bot.syncpay_access_token = None
    bot.syncpay_token_expires_at = None
    bot.syncpay_ativo = True  # Ativa automaticamente ao salvar
    
    # Lógica de definição de Gateway
    if not bot.gateway_principal or bot.gateway_principal == "":
        bot.gateway_principal = "syncpay"
    elif bot.gateway_principal != "syncpay" and not bot.gateway_fallback:
        bot.gateway_fallback = "syncpay"
    
    db.commit()
    
    logger.info(f"🔑 [SYNC PAY] Credenciais salvas para BOT {bot.nome}: {client_id_limpo[:8]}...")
    
    return {"status": "conectado", "msg": f"Integração Sync Pay salva para {bot.nome}!"}

# =========================================================
# 🔄 ROTAS DE CONFIGURAÇÃO MULTI-GATEWAY (POR BOT)
# =========================================================

class GatewayConfigUpdate(BaseModel):
    gateway_principal: Optional[str] = None   # "pushinpay", "wiinpay" ou "syncpay"
    gateway_fallback: Optional[str] = None    # "pushinpay", "wiinpay", "syncpay" ou None
    pushinpay_ativo: Optional[bool] = None
    wiinpay_ativo: Optional[bool] = None
    syncpay_ativo: Optional[bool] = None      # 🆕 NOVO: Ativador da Sync Pay

@app.get("/api/admin/integrations/gateway-config/{bot_id}")
def get_gateway_config(bot_id: int, db: Session = Depends(get_db)):
    """Retorna configuração completa de gateways de um bot."""
    bot = db.query(BotModel).filter(BotModel.id == bot_id).first()
    if not bot:
        raise HTTPException(status_code=404, detail="Bot não encontrado")
    
    return {
        "bot_id": bot.id,
        "bot_nome": bot.nome,
        "gateway_principal": bot.gateway_principal or "pushinpay",
        "gateway_fallback": bot.gateway_fallback,
        "pushinpay": {
            "ativo": bot.pushinpay_ativo or False,
            "configurado": bool(bot.pushin_token),
            "token_mask": f"{bot.pushin_token[:4]}...{bot.pushin_token[-4:]}" if bot.pushin_token and len(bot.pushin_token) > 8 else ""
        },
        "wiinpay": {
            "ativo": bot.wiinpay_ativo or False,
            "configurado": bool(bot.wiinpay_api_key),
            "token_mask": f"{bot.wiinpay_api_key[:8]}...{bot.wiinpay_api_key[-6:]}" if bot.wiinpay_api_key and len(bot.wiinpay_api_key) > 14 else ""
        },
        "syncpay": {
            "ativo": bot.syncpay_ativo or False,
            "configurado": bool(bot.syncpay_client_id),
            "token_mask": f"{bot.syncpay_client_id[:8]}...{bot.syncpay_client_id[-4:]}" if bot.syncpay_client_id and len(bot.syncpay_client_id) > 12 else ""
        }
    }

@app.put("/api/admin/integrations/gateway-config/{bot_id}")
def update_gateway_config(bot_id: int, config: GatewayConfigUpdate, db: Session = Depends(get_db)):
    """Atualiza configuração de gateways (principal, fallback, ativar/pausar)."""
    bot = db.query(BotModel).filter(BotModel.id == bot_id).first()
    if not bot:
        raise HTTPException(status_code=404, detail="Bot não encontrado")
    
    if config.gateway_principal is not None:
        if config.gateway_principal not in ["pushinpay", "wiinpay", "syncpay"]:
            raise HTTPException(status_code=400, detail="Gateway principal inválida. Use 'pushinpay', 'wiinpay' ou 'syncpay'.")
        bot.gateway_principal = config.gateway_principal
        
    if config.gateway_fallback is not None:
        if config.gateway_fallback not in ["pushinpay", "wiinpay", "syncpay", ""]:
            raise HTTPException(status_code=400, detail="Gateway fallback inválida.")
        bot.gateway_fallback = config.gateway_fallback if config.gateway_fallback != "" else None
        
    if config.pushinpay_ativo is not None:
        if config.pushinpay_ativo and not bot.pushin_token:
            raise HTTPException(status_code=400, detail="Não é possível ativar PushinPay sem token configurado.")
        bot.pushinpay_ativo = config.pushinpay_ativo
        
    if config.wiinpay_ativo is not None:
        if config.wiinpay_ativo and not bot.wiinpay_api_key:
            raise HTTPException(status_code=400, detail="Não é possível ativar WiinPay sem API Key configurada.")
        bot.wiinpay_ativo = config.wiinpay_ativo
        
    if config.syncpay_ativo is not None:
        if config.syncpay_ativo and not bot.syncpay_client_id:
            raise HTTPException(status_code=400, detail="Não é possível ativar Sync Pay sem Credenciais configuradas.")
        bot.syncpay_ativo = config.syncpay_ativo
    
    db.commit()
    
    logger.info(f"⚙️ [GATEWAY] Config atualizada para bot {bot.nome}: principal={bot.gateway_principal}, fallback={bot.gateway_fallback}")
    
    return {
        "status": "success", 
        "msg": f"Configuração de gateways atualizada para {bot.nome}!",
        "gateway_principal": bot.gateway_principal,
        "gateway_fallback": bot.gateway_fallback,
        "pushinpay_ativo": bot.pushinpay_ativo,
        "wiinpay_ativo": bot.wiinpay_ativo,
        "syncpay_ativo": bot.syncpay_ativo
    }

# =========================================================
# 🔌 ROTA PARA EDITAR TOKEN/API KEY DE GATEWAY EXISTENTE
# =========================================================

@app.put("/api/admin/integrations/pushinpay/{bot_id}")
def update_pushin_token(bot_id: int, data: IntegrationUpdate, db: Session = Depends(get_db)):
    """Permite editar o token PushinPay de um bot já configurado."""
    bot = db.query(BotModel).filter(BotModel.id == bot_id).first()
    if not bot:
        raise HTTPException(status_code=404, detail="Bot não encontrado")
    
    token_limpo = data.token.strip()
    if len(token_limpo) < 10:
        return {"status": "erro", "msg": "Token muito curto ou inválido."}

    old_mask = f"{bot.pushin_token[:4]}..." if bot.pushin_token else "nenhum"
    bot.pushin_token = token_limpo
    db.commit()
    
    logger.info(f"🔄 Token PushinPay EDITADO para BOT {bot.nome}: {old_mask} → {token_limpo[:5]}...")
    return {"status": "conectado", "msg": f"Token PushinPay atualizado para {bot.nome}!"}

@app.put("/api/admin/integrations/wiinpay/{bot_id}")
def update_wiinpay_token(bot_id: int, data: IntegrationUpdate, db: Session = Depends(get_db)):
    """Permite editar a API Key WiinPay de um bot já configurado."""
    bot = db.query(BotModel).filter(BotModel.id == bot_id).first()
    if not bot:
        raise HTTPException(status_code=404, detail="Bot não encontrado")
    
    api_key_limpa = data.token.strip()
    if len(api_key_limpa) < 10:
        return {"status": "erro", "msg": "API Key muito curta ou inválida."}

    old_mask = f"{bot.wiinpay_api_key[:8]}..." if bot.wiinpay_api_key else "nenhum"
    bot.wiinpay_api_key = api_key_limpa
    db.commit()
    
    logger.info(f"🔄 [WIINPAY] API Key EDITADA para BOT {bot.nome}: {old_mask} → {api_key_limpa[:8]}...")
    return {"status": "conectado", "msg": f"API Key WiinPay atualizada para {bot.nome}!"}

@app.put("/api/admin/integrations/syncpay/{bot_id}")
def update_syncpay_token(bot_id: int, data: SyncPayIntegrationUpdate, db: Session = Depends(get_db)):
    """Permite editar as credenciais da Sync Pay de um bot já configurado."""
    bot = db.query(BotModel).filter(BotModel.id == bot_id).first()
    if not bot:
        raise HTTPException(status_code=404, detail="Bot não encontrado")
    
    client_id_limpo = data.client_id.strip()
    client_secret_limpo = data.client_secret.strip()
    
    if len(client_id_limpo) < 10 or len(client_secret_limpo) < 10:
        return {"status": "erro", "msg": "Credenciais muito curtas ou inválidas."}

    old_mask = f"{bot.syncpay_client_id[:8]}..." if bot.syncpay_client_id else "nenhum"
    
    bot.syncpay_client_id = client_id_limpo
    bot.syncpay_client_secret = client_secret_limpo
    bot.syncpay_access_token = None # Força renovação
    bot.syncpay_token_expires_at = None
    
    db.commit()
    
    logger.info(f"🔄 [SYNC PAY] Credenciais EDITADAS para BOT {bot.nome}: {old_mask} → {client_id_limpo[:8]}...")
    return {"status": "conectado", "msg": f"Credenciais Sync Pay atualizadas para {bot.nome}!"}

# --- MODELOS ---
class BotCreate(BaseModel):
    nome: str
    token: str
    id_canal_vip: str
    admin_principal_id: Optional[str] = None
    suporte_username: Optional[str] = None
    id_canal_notificacao: Optional[str] = None
    protect_content: Optional[bool] = False
    notificar_no_bot: Optional[bool] = True  # 🔥 NOVO: Toggle notificação no bot

# Novo modelo para Atualização
class BotUpdate(BaseModel):
    nome: Optional[str] = None
    token: Optional[str] = None
    id_canal_vip: Optional[str] = None
    admin_principal_id: Optional[str] = None
    suporte_username: Optional[str] = None
    id_canal_notificacao: Optional[str] = None
    protect_content: Optional[bool] = None
    notificar_no_bot: Optional[bool] = None  # 🔥 NOVO: Toggle notificação no bot

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
    is_lifetime: Optional[bool] = False
    id_canal_destino: Optional[str] = None  # ✅ NOVO CAMPO

class PlanoUpdate(BaseModel):
    nome_exibicao: Optional[str] = None
    preco: Optional[float] = None
    dias_duracao: Optional[int] = None
    is_lifetime: Optional[bool] = None
    id_canal_destino: Optional[str] = None  # ✅ NOVO CAMPO
    
    # Adiciona essa config para permitir que o Pydantic ignore tipos estranhos se possível
    class Config:
        arbitrary_types_allowed = True
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
    msg_pix: Optional[str] = None
    
    # 🔥 NOVOS CAMPOS PARA BOTÕES PERSONALIZADOS
    button_mode: Optional[str] = "next_step"  # "next_step" ou "custom"
    buttons_config: Optional[List[dict]] = None  # Botões da mensagem 1
    buttons_config_2: Optional[List[dict]] = None  # Botões da mensagem final
    
    steps: Optional[List[dict]] = None  # Passos extras

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
    group_id: Optional[int] = None  # ✅ FASE 2: Referência ao catálogo de Grupos
    autodestruir: Optional[bool] = False
    msg_texto: Optional[str] = None
    msg_media: Optional[str] = None
    btn_aceitar: Optional[str] = "✅ SIM, ADICIONAR"
    btn_recusar: Optional[str] = "❌ NÃO, OBRIGADO"
    audio_url: Optional[str] = None          # 🔊 Áudio separado
    audio_delay_seconds: Optional[int] = 3   # 🔊 Delay entre áudio e mídia

# 🚀 UPSELL/DOWNSELL MODELS
class UpsellCreate(BaseModel):
    ativo: bool = False
    nome_produto: str = ""
    preco: float = 0.0
    link_acesso: str = ""
    group_id: Optional[int] = None
    delay_minutos: int = 2
    msg_texto: Optional[str] = "🔥 Oferta exclusiva para você!"
    msg_media: Optional[str] = None
    btn_aceitar: Optional[str] = "✅ QUERO ESSA OFERTA!"
    btn_recusar: Optional[str] = "❌ NÃO, OBRIGADO"
    autodestruir: Optional[bool] = False
    audio_url: Optional[str] = None          # 🔊 Áudio separado
    audio_delay_seconds: Optional[int] = 3   # 🔊 Delay entre áudio e mídia

class DownsellCreate(BaseModel):
    ativo: bool = False
    nome_produto: str = ""
    preco: float = 0.0
    link_acesso: str = ""
    group_id: Optional[int] = None
    delay_minutos: int = 10
    msg_texto: Optional[str] = "🎁 Última chance! Oferta especial só para você!"
    msg_media: Optional[str] = None
    btn_aceitar: Optional[str] = "✅ QUERO ESSA OFERTA!"
    btn_recusar: Optional[str] = "❌ NÃO, OBRIGADO"
    autodestruir: Optional[bool] = False
    audio_url: Optional[str] = None          # 🔊 Áudio separado
    audio_delay_seconds: Optional[int] = 3   # 🔊 Delay entre áudio e mídia

# =========================================================
# 📦 GRUPOS E CANAIS - PYDANTIC MODELS
# =========================================================
class BotGroupCreate(BaseModel):
    title: str
    group_id: str
    link: Optional[str] = None
    plan_ids: Optional[List[int]] = []
    is_active: Optional[bool] = True

class BotGroupUpdate(BaseModel):
    title: Optional[str] = None
    group_id: Optional[str] = None
    link: Optional[str] = None
    plan_ids: Optional[List[int]] = None
    is_active: Optional[bool] = None

class IntegrationUpdate(BaseModel):
    token: str

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
    slug: Optional[str] = None
    description: Optional[str] = None
    cover_image: Optional[str] = None
    banner_mob_url: Optional[str] = None
    theme_color: Optional[str] = "#c333ff"
    is_direct_checkout: bool = False
    is_hacker_mode: bool = False
    content_json: Optional[List[dict]] = None
    
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
    
    # --- MINI APP V2: SEPARADOR, PAGINAÇÃO, FORMATO ---
    items_per_page: Optional[int] = None
    separator_enabled: Optional[bool] = False
    separator_color: Optional[str] = "#ffffff"
    separator_text: Optional[str] = None
    separator_btn_text: Optional[str] = None
    separator_btn_url: Optional[str] = None
    separator_logo_url: Optional[str] = None
    model_img_shape: Optional[str] = "square"

    # --- 🆕 NOVO: CORES DOS TEXTOS + NEON ---
    separator_text_color: Optional[str] = '#ffffff'
    separator_btn_text_color: Optional[str] = '#ffffff'
    separator_is_neon: Optional[bool] = False
    separator_neon_color: Optional[str] = None

# --- MODELO DE PERFIL ---
class ProfileUpdate(BaseModel):
    name: str
    avatar_url: Optional[str] = None

# 🆕 MODELS PARA ALTERAÇÃO DE SENHA E USERNAME
class ChangePasswordRequest(BaseModel):
    current_password: str
    new_password: str
    confirm_password: str

class ChangeUsernameRequest(BaseModel):
    new_username: str

class ChannelTestRequest(BaseModel):
    token: str
    channel_id: str

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
    
    # Preço Personalizado (CRUCIAL PARA O BUG DO PREÇO)
    price_mode: str = "original" # 'original' ou 'custom'
    custom_price: Optional[float] = None
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

    # ✅ NOVOS CAMPOS PARA CORREÇÃO DO PREÇO
    price_mode: Optional[str] = "original" # 'original' ou 'custom'
    custom_price: Optional[float] = None

    agendar: bool = False
    data_agendamento: Optional[datetime] = None
    is_test: bool = False
    specific_user_id: Optional[str] = None

# =========================================================
# 3. ROTA ENDPOINT (CONECTADA À FUNÇÃO NOVA)
# =========================================================
# =========================================================
# 3. ROTA ENDPOINT (CONECTADA À FUNÇÃO NOVA)
# =========================================================
@app.post("/api/admin/bots/{bot_id}/remarketing/send")
def send_remarketing(
    bot_id: int, 
    data: RemarketingRequest, 
    background_tasks: BackgroundTasks, 
    db: Session = Depends(get_db),
    current_user = Depends(get_current_user)
):
    # 🔒 Verifica permissão
    verificar_bot_pertence_usuario(bot_id, current_user.id, db)

    try:
        logger.info(f"📢 Remarketing Solicitado Bot {bot_id} | Target: {data.target}")
        
        campaign_id = str(uuid.uuid4())
        
        # 🔥 CORREÇÃO: Normaliza custom_price (vírgula → ponto, 2 casas decimais)
        normalized_price = None
        if data.price_mode == 'custom' and data.custom_price is not None:
            try:
                val_n = float(str(data.custom_price).replace(',', '.'))
                if val_n > 0:
                    normalized_price = round(val_n, 2)
            except (ValueError, TypeError):
                normalized_price = None
        
        # Prepara config JSON para o banco
        config_json = json.dumps({
            "mensagem": data.mensagem,
            "media": data.media_url,
            "oferta": data.incluir_oferta,
            "plano_id": data.plano_oferta_id,
            "price_mode": data.price_mode,
            "custom_price": normalized_price if normalized_price else data.custom_price
        })

        # 🔥 CORREÇÃO: Calcula promo_price já na criação
        promo_price_calc = None
        if data.incluir_oferta and data.plano_oferta_id:
            try:
                pid_str = str(data.plano_oferta_id)
                plano_t = db.query(PlanoConfig).filter(
                    (PlanoConfig.key_id == pid_str) | 
                    (PlanoConfig.id == int(pid_str) if pid_str.isdigit() else False)
                ).first()
                if plano_t:
                    if normalized_price and normalized_price > 0:
                        promo_price_calc = normalized_price
                    else:
                        promo_price_calc = float(plano_t.preco_atual)
            except Exception:
                pass

        nova_campanha = RemarketingCampaign(
            bot_id=bot_id,
            campaign_id=campaign_id,
            target=data.target,
            type='teste' if data.is_test else 'massivo',
            config=config_json,
            status='enviando',
            data_envio=now_brazil(),
            promo_price=promo_price_calc  # 🔥 JÁ SALVA PROMO_PRICE
        )
        db.add(nova_campanha)
        db.commit()
        db.refresh(nova_campanha)

        # Lógica de Teste
        if data.is_test and not data.specific_user_id:
            bot = db.query(BotModel).filter(BotModel.id == bot_id).first()
            data.specific_user_id = bot.admin_principal_id
        
        # 🚀 CHAMA A FUNÇÃO CORRIGIDA
        background_tasks.add_task(
            processar_envio_remarketing, 
            nova_campanha.id, 
            bot_id,
            data
        )
        
        return {"status": "success", "campaign_id": campaign_id, "message": "Disparo iniciado!"}

    except Exception as e:
        logger.error(f"Erro endpoint remarketing: {e}")
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

        # ADICIONE LOGO APÓS:
        logger.info(f"🔍 [DEBUG] Bot ID: {bot_atual.id}")
        logger.info(f"🔍 [DEBUG] Bot tem token? {'SIM ('+str(len(bot_atual.pushin_token or ''))+' chars)' if bot_atual.pushin_token else 'NÃO'}")
        if not bot_atual.pushin_token:
            logger.warning(f"⚠️ [DEBUG] USANDO TOKEN DA PLATAFORMA (fallback)!")
        else:
            logger.info(f"✅ [DEBUG] USANDO TOKEN DO USUÁRIO: {pushin_token[:10]}...")

        # Tratamento de ID
        user_clean = str(data.username).strip().lower().replace("@", "") if data.username else "anonimo"
        tid_clean = str(data.telegram_id).strip()
        if not tid_clean.isdigit(): 
            tid_clean = user_clean

        # Modo Teste
            if not pushin_token:
                fake_txid = str(uuid.uuid4())
                novo_pedido = Pedido(
                    bot_id=data.bot_id,
                    telegram_id=tid_clean,
                    first_name=data.first_name,
                    username=user_clean,   
                    valor=data.valor,
                    status='pending',
                    plano_id=data.plano_id,
                    plano_nome=data.plano_nome,
                    txid=fake_txid,
                    qr_code="pix-fake-copia-cola",
                    transaction_id=fake_txid,
                    tem_order_bump=data.tem_order_bump
                )
                db.add(novo_pedido)
                db.commit()
                db.refresh(novo_pedido)
                
                # ✅ Agenda remarketing (MODO TESTE)
                try:
                    chat_id_int = int(tid_clean) if tid_clean.isdigit() else hash(tid_clean) % 1000000000
                    
                    schedule_remarketing_and_alternating(
                        bot_id=data.bot_id,
                        chat_id=chat_id_int,
                        payment_message_id=0,
                        user_info={
                            'first_name': data.first_name,
                            'plano': data.plano_nome,
                            'valor': data.valor
                        }
                    )
                    logger.info(f"📧 Remarketing agendado (teste): {data.first_name}")
                except Exception as e:
                    logger.error(f"❌ Erro ao agendar remarketing (teste): {e}")
                
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
        else:
            # Fallback: consulta config global
            try:
                cfg_fee = db.query(SystemConfig).filter(SystemConfig.key == "default_fee").first()
                if cfg_fee and cfg_fee.value:
                    taxa_centavos = int(cfg_fee.value)
            except: pass

        # SUBSTITUA POR:
        logger.info(f"🔍 [DEBUG] Checando split:")
        logger.info(f"  Taxa: R$ {taxa_centavos/100:.2f} ({taxa_centavos} centavos)")
        logger.info(f"  Valor total: R$ {valor_total_centavos/100:.2f} ({valor_total_centavos} centavos)")
        logger.info(f"  Percentual: {(taxa_centavos/valor_total_centavos)*100:.1f}%")

        if taxa_centavos >= (valor_total_centavos * 0.5):
            logger.warning(f"⚠️ [DEBUG] TAXA MUITO ALTA! Split NÃO APLICADO!")
        else:
            payload["split_rules"] = [
                {
                    "value": taxa_centavos,
                    "account_id": PLATAFORMA_ID,
                    "charge_processing_fee": False
                }
            ]
            logger.info(f"✅ [DEBUG] SPLIT CONFIGURADO!")
            logger.info(f"  Split value: {taxa_centavos} centavos")
            logger.info(f"  Account ID: {PLATAFORMA_ID}")
            logger.info(f"  Usuário receberá: R$ {(valor_total_centavos - taxa_centavos)/100:.2f}")

        # ======================================================================
        # 4. ENVIA (HTTPX ASYNC)
        # ======================================================================
        url = "https://api.pushinpay.com.br/api/pix/cashIn"
        headers = { 
            "Authorization": f"Bearer {pushin_token}", 
            "Content-Type": "application/json", 
            "Accept": "application/json" 
        }

        # ADICIONE ANTES:
        logger.info(f"📤 [DEBUG] Enviando para PushinPay:")
        logger.info(f"  Token usado: {pushin_token[:10]}...")
        logger.info(f"  Payload split_rules: {payload.get('split_rules', [])}")
        
        req = await http_client.post(url, json=payload, headers=headers, timeout=15)
        
        if req.status_code in [200, 201]:
            resp = req.json()
            txid = str(resp.get('id') or resp.get('txid'))
            copia_cola = resp.get('qr_code_text') or resp.get('pixCopiaEcola')
            qr_image = resp.get('qr_code_image_url') or resp.get('qr_code')

            # ADICIONE LOGO APÓS:
            logger.info(f"✅ [DEBUG] Resposta PushinPay ({req.status_code}):")
            logger.info(f"  Split retornado: {resp.get('split_rules', [])}")
            if not resp.get('split_rules'):
                logger.warning(f"⚠️ [DEBUG] API NÃO RETORNOU SPLIT!")

        # Sucesso na geração do PIX
            novo_pedido = Pedido(
                bot_id=data.bot_id,
                telegram_id=tid_clean,
                first_name=data.first_name,
                username=user_clean,
                valor=data.valor,
                status='pending',
                plano_id=data.plano_id,
                plano_nome=data.plano_nome,
                txid=txid,
                qr_code=qr_image,
                transaction_id=txid,
                tem_order_bump=data.tem_order_bump
            )
            db.add(novo_pedido)
            db.commit()
            db.refresh(novo_pedido)
            
            # ✅ Agenda remarketing (PRODUÇÃO)
            try:
                chat_id_int = int(tid_clean) if tid_clean.isdigit() else hash(tid_clean) % 1000000000
                
                schedule_remarketing_and_alternating(
                    bot_id=data.bot_id,
                    chat_id=chat_id_int,
                    payment_message_id=0,
                    user_info={
                        'first_name': data.first_name,
                        'plano': data.plano_nome,
                        'valor': data.valor
                    }
                )
                logger.info(f"📧 Remarketing agendado: {data.first_name}")
                
            except Exception as e:
                logger.error(f"❌ Erro ao agendar remarketing: {e}")
            
            return {"txid": txid, "copia_cola": copia_cola, "qr_code": qr_image}
            
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
# 🔔 SISTEMA DE NOTIFICAÇÕES PUSH (ONESIGNAL) - VARIÁVEIS DE AMBIENTE
# =========================================================
async def enviar_push_onesignal(bot_id: int, nome_cliente: str, plano: str, valor: float, db: Session):
    """
    Dispara notificação Push para o celular/PC do dono do bot.
    As chaves estão protegidas nas variáveis de ambiente do Railway.
    """
    try:
        # 1. Busca o dono do bot
        bot = db.query(BotModel).filter(BotModel.id == bot_id).first()
        if not bot or not bot.owner_id: 
            return
            
        owner = db.query(User).filter(User.id == bot.owner_id).first()
        if not owner or not owner.username: 
            return
            
        # 2. Puxa as Credenciais Seguras do Servidor (Railway)
        app_id = os.getenv("ONESIGNAL_APP_ID")
        rest_api_key = os.getenv("ONESIGNAL_REST_API_KEY")
        
        if not app_id or not rest_api_key:
            logger.warning("⚠️ [PUSH ONESIGNAL] Chaves não configuradas no Railway. Ignorando envio.")
            return
            
        # 3. URL da API V2 e Cabeçalho 'key'
        url = "https://api.onesignal.com/notifications"
        headers = {
            "accept": "application/json",
            "content-type": "application/json",
            "Authorization": f"key {rest_api_key.strip()}"
        }
        
        # =========================================================
        # 4. Formata a mensagem (🔥 ALTERADO AQUI CONFORME SEU PEDIDO)
        # =========================================================
        valor_formatado = f"{valor:.2f}".replace('.', ',')
        
        titulo = "NOVA VENDA APROVADA!"
        mensagem = f"Você fez uma venda de R$ {valor_formatado}!"
        # =========================================================
        
        # 5. Payload Moderno V2
        payload = {
            "app_id": app_id.strip(),
            "target_channel": "push",
            "include_external_user_ids": [str(owner.username)],
            "include_aliases": {"external_id": [str(owner.username)]},
            "headings": {"en": titulo, "pt": titulo},
            "contents": {"en": mensagem, "pt": mensagem}
        }
        
        # 6. Dispara e lê a resposta
        if http_client:
            response = await http_client.post(url, json=payload, headers=headers, timeout=10.0)
            
            if response.status_code == 200:
                logger.info(f"✅ [PUSH ONESIGNAL] SUCESSO ABSOLUTO para {owner.username}! Resposta: {response.text}")
            else:
                logger.error(f"❌ [PUSH ONESIGNAL] Código de Erro: {response.status_code} | Resposta: {response.text}")
        
    except Exception as e:
        logger.error(f"❌ [PUSH ONESIGNAL] Erro Crítico: {e}")

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
    
    # 🚨 VERIFICAÇÃO DE BANIMENTO
    if getattr(user, 'is_banned', False):
        log_action(db=db, user_id=user.id, username=user.username, action="login_banned", resource_type="auth",
                   description=f"Login bloqueado: conta banida. Motivo: {getattr(user, 'banned_reason', 'N/A')}", 
                   success=False, ip_address=get_client_ip(request))
        raise HTTPException(status_code=403, detail="Sua conta foi suspensa por violação dos termos de uso. Entre em contato com o suporte.")
    
    # 🚨 VERIFICAÇÃO DE CONTA INATIVA
    if not getattr(user, 'is_active', True):
        raise HTTPException(status_code=403, detail="Conta desativada. Entre em contato com o suporte.")
    
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
        "has_bots": has_bots,
        "is_superuser": getattr(user, 'is_superuser', False)
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
        # Verificar conexão com banco de dados (SQLAlchemy síncrono)
        db_status = "ok"
        try:
            db_check = SessionLocal()
            try:
                db_check.execute(text("SELECT 1"))
            finally:
                db_check.close()
        except Exception as e:
            db_status = f"error: {str(e)}"
        
        # Verificar scheduler
        scheduler_status = "running" if scheduler.running else "stopped"
        
        # Verificar webhooks pendentes
        webhook_stats = {"pending": 0, "failed": 0}
        try:
            db_wh = SessionLocal()
            try:
                result = db_wh.execute(text("""
                    SELECT status, COUNT(*) as count
                    FROM webhook_retry
                    WHERE status IN ('pending', 'failed')
                    GROUP BY status
                """))
                for row in result:
                    webhook_stats[row[0]] = row[1]
            finally:
                db_wh.close()
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
            "timestamp": now_brazil().isoformat(),
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
                "timestamp": now_brazil().isoformat()
            },
            status_code=503
        )


@app.get("/api/health/simple")
async def health_check_simple():
    """
    Versão simplificada do health check (mais rápida).
    Apenas retorna 200 se o servidor está vivo.
    """
    return {"status": "ok", "timestamp": now_brazil().isoformat()}

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
    if user_data.wiinpay_user_id is not None:
        user.wiinpay_user_id = user_data.wiinpay_user_id
        
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
            telebot.types.BotCommand("status", "⭐ Minha Assinatura"),
            telebot.types.BotCommand("denunciar", "🚨 Fazer Denúncia") # 🔥 NOVO COMANDO ADICIONADO AQUI!
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
    
    # 🆕 VALIDAÇÃO DE LIMITE DE BOTS
    if not current_user.is_superuser:
        max_bots = getattr(current_user, 'max_bots', 20) or 20
        bots_ativos = db.query(BotModel).filter(
            BotModel.owner_id == current_user.id,
            BotModel.status != 'deletado'
        ).count()
        
        if bots_ativos >= max_bots:
            plano = getattr(current_user, 'plano_plataforma', 'free') or 'free'
            raise HTTPException(
                status_code=403, 
                detail=f"Limite de {max_bots} bots atingido no plano {plano.upper()}. Exclua bots inativos ou faça upgrade do seu plano."
            )
    
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
        id_canal_notificacao=bot_data.id_canal_notificacao,
        protect_content=getattr(bot_data, 'protect_content', False),
        notificar_no_bot=getattr(bot_data, 'notificar_no_bot', True),  # 🔥 NOVO
        owner_id=current_user.id,
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
    
    # ✅ Canal de Notificações
    if dados.id_canal_notificacao is not None and dados.id_canal_notificacao != bot_db.id_canal_notificacao:
        changes["canal_notificacao"] = {"old": bot_db.id_canal_notificacao, "new": dados.id_canal_notificacao}
        bot_db.id_canal_notificacao = dados.id_canal_notificacao if dados.id_canal_notificacao.strip() else None
    
    # 🔒 Proteção de Conteúdo
    if dados.protect_content is not None and dados.protect_content != getattr(bot_db, 'protect_content', False):
        changes["protect_content"] = {"old": getattr(bot_db, 'protect_content', False), "new": dados.protect_content}
        bot_db.protect_content = dados.protect_content
    
    # 🔥 NOVO: Toggle de notificação no bot
    if dados.notificar_no_bot is not None and dados.notificar_no_bot != getattr(bot_db, 'notificar_no_bot', True):
        changes["notificar_no_bot"] = {"old": getattr(bot_db, 'notificar_no_bot', True), "new": dados.notificar_no_bot}
        bot_db.notificar_no_bot = dados.notificar_no_bot
    
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

# ============================================================
# 🔁 CLONAR BOT (COPIA TODAS AS CONFIGURAÇÕES)
# ============================================================
class CloneBotRequest(BaseModel):
    nome: str
    token: str
    id_canal_vip: str

@app.post("/api/admin/bots/{bot_id}/clone")
def clonar_bot(
    bot_id: int,
    dados: CloneBotRequest,
    request: Request,
    db: Session = Depends(get_db),
    current_user = Depends(get_current_user)
):
    """
    Clona um bot existente com todas as configurações.
    O usuário fornece NOVO token e canal VIP.
    
    ✅ O que é copiado:
    - BotFlow + BotFlowStep (mensagens, mídias, botões)
    - PlanoConfig (planos e preços)
    - OrderBumpConfig + UpsellConfig + DownsellConfig
    - MiniAppConfig + MiniAppCategory (loja)
    - RemarketingConfig + AlternatingMessages
    - CanalFreeConfig
    - BotGroup (canais extras)
    - protect_content flag
    
    ❌ O que NÃO é copiado:
    - Leads, Pedidos (novo bot começa zerado)
    - TrackingLinks, RemarketingCampaigns (histórico)
    - Token, Canal VIP (definidos pelo usuário)
    """
    # 1. Verifica se o bot original pertence ao usuário
    bot_original = verificar_bot_pertence_usuario(bot_id, current_user.id, db)
    
    # 2. Verifica se o token já está em uso
    token_existente = db.query(BotModel).filter(BotModel.token == dados.token).first()
    if token_existente:
        raise HTTPException(status_code=409, detail="Este token já está sendo usado por outro bot.")
    
    # 3. Valida o token no Telegram
    try:
        new_tb = telebot.TeleBot(dados.token)
        bot_info = new_tb.get_me()
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Token inválido: {str(e)}")
    
    # 4. Cria o novo bot
    novo_bot = BotModel(
        nome=dados.nome,
        token=dados.token,
        username=bot_info.username,
        id_canal_vip=dados.id_canal_vip,
        admin_principal_id=bot_original.admin_principal_id,
        suporte_username=bot_original.suporte_username,
        id_canal_notificacao=None,  # Não clona canal de notificação
        protect_content=getattr(bot_original, 'protect_content', False),
        gateway_principal=bot_original.gateway_principal,
        gateway_fallback=bot_original.gateway_fallback,
        pushin_token=bot_original.pushin_token,
        pushinpay_ativo=bot_original.pushinpay_ativo,
        wiinpay_api_key=bot_original.wiinpay_api_key,
        wiinpay_ativo=bot_original.wiinpay_ativo,
        owner_id=current_user.id,
        status="ativo"
    )
    db.add(novo_bot)
    db.commit()
    db.refresh(novo_bot)
    
    novo_id = novo_bot.id
    erros = []
    
    # =======================================
    # 5. CLONAR FLUXO (BotFlow)
    # =======================================
    try:
        flow = db.query(BotFlow).filter(BotFlow.bot_id == bot_id).first()
        if flow:
            novo_flow = BotFlow(
                bot_id=novo_id,
                start_mode=flow.start_mode,
                miniapp_url=flow.miniapp_url,
                miniapp_btn_text=flow.miniapp_btn_text,
                msg_boas_vindas=flow.msg_boas_vindas,
                media_url=flow.media_url,
                btn_text_1=flow.btn_text_1,
                autodestruir_1=flow.autodestruir_1,
                mostrar_planos_1=flow.mostrar_planos_1,
                buttons_config=flow.buttons_config,
                button_mode=flow.button_mode,
                msg_2_texto=flow.msg_2_texto,
                msg_2_media=flow.msg_2_media,
                mostrar_planos_2=flow.mostrar_planos_2,
                buttons_config_2=flow.buttons_config_2,
                msg_pix=flow.msg_pix
            )
            db.add(novo_flow)
    except Exception as e:
        erros.append(f"Fluxo: {str(e)}")
    
    # =======================================
    # 6. CLONAR STEPS (BotFlowStep)
    # =======================================
    try:
        steps = db.query(BotFlowStep).filter(BotFlowStep.bot_id == bot_id).order_by(BotFlowStep.step_order).all()
        for step in steps:
            novo_step = BotFlowStep(
                bot_id=novo_id,
                step_order=step.step_order,
                msg_texto=step.msg_texto,
                msg_media=step.msg_media,
                btn_texto=step.btn_texto,
                buttons_config=step.buttons_config,
                autodestruir=step.autodestruir,
                mostrar_botao=step.mostrar_botao,
                delay_seconds=step.delay_seconds
            )
            db.add(novo_step)
    except Exception as e:
        erros.append(f"Steps: {str(e)}")
    
    # =======================================
    # 7. CLONAR PLANOS (PlanoConfig)
    # =======================================
    try:
        planos = db.query(PlanoConfig).filter(PlanoConfig.bot_id == bot_id).all()
        for plano in planos:
            novo_plano = PlanoConfig(
                bot_id=novo_id,
                nome_exibicao=plano.nome_exibicao,
                descricao=plano.descricao,
                preco_atual=plano.preco_atual,
                preco_cheio=plano.preco_cheio,
                dias_duracao=plano.dias_duracao,
                is_lifetime=plano.is_lifetime,
                key_id=f"clone_{novo_id}_{plano.id}_{uuid.uuid4().hex[:8]}",
                id_canal_destino=plano.id_canal_destino
            )
            db.add(novo_plano)
    except Exception as e:
        erros.append(f"Planos: {str(e)}")
    
    # =======================================
    # 8. CLONAR ORDER BUMP
    # =======================================
    try:
        bump = db.query(OrderBumpConfig).filter(OrderBumpConfig.bot_id == bot_id).first()
        if bump:
            novo_bump = OrderBumpConfig(
                bot_id=novo_id,
                ativo=bump.ativo,
                nome_produto=bump.nome_produto,
                preco=bump.preco,
                link_acesso=bump.link_acesso,
                autodestruir=bump.autodestruir,
                msg_texto=bump.msg_texto,
                msg_media=bump.msg_media,
                btn_aceitar=bump.btn_aceitar,
                btn_recusar=bump.btn_recusar
            )
            db.add(novo_bump)
    except Exception as e:
        erros.append(f"OrderBump: {str(e)}")
    
    # =======================================
    # 9. CLONAR UPSELL
    # =======================================
    try:
        upsell = db.query(UpsellConfig).filter(UpsellConfig.bot_id == bot_id).first()
        if upsell:
            novo_upsell = UpsellConfig(
                bot_id=novo_id,
                ativo=upsell.ativo,
                nome_produto=upsell.nome_produto,
                preco=upsell.preco,
                link_acesso=upsell.link_acesso,
                delay_minutos=upsell.delay_minutos,
                msg_texto=upsell.msg_texto,
                msg_media=upsell.msg_media,
                btn_aceitar=upsell.btn_aceitar,
                btn_recusar=upsell.btn_recusar,
                autodestruir=upsell.autodestruir
            )
            db.add(novo_upsell)
    except Exception as e:
        erros.append(f"Upsell: {str(e)}")
    
    # =======================================
    # 10. CLONAR DOWNSELL
    # =======================================
    try:
        downsell = db.query(DownsellConfig).filter(DownsellConfig.bot_id == bot_id).first()
        if downsell:
            novo_downsell = DownsellConfig(
                bot_id=novo_id,
                ativo=downsell.ativo,
                nome_produto=downsell.nome_produto,
                preco=downsell.preco,
                link_acesso=downsell.link_acesso,
                delay_minutos=downsell.delay_minutos,
                msg_texto=downsell.msg_texto,
                msg_media=downsell.msg_media,
                btn_aceitar=downsell.btn_aceitar,
                btn_recusar=downsell.btn_recusar,
                autodestruir=downsell.autodestruir
            )
            db.add(novo_downsell)
    except Exception as e:
        erros.append(f"Downsell: {str(e)}")
    
    # =======================================
    # 11. CLONAR MINI APP CONFIG
    # =======================================
    try:
        miniapp = db.query(MiniAppConfig).filter(MiniAppConfig.bot_id == bot_id).first()
        if miniapp:
            novo_miniapp = MiniAppConfig(
                bot_id=novo_id,
                logo_url=miniapp.logo_url,
                background_type=miniapp.background_type,
                background_value=miniapp.background_value,
                hero_video_url=miniapp.hero_video_url,
                hero_title=miniapp.hero_title,
                hero_subtitle=miniapp.hero_subtitle,
                hero_btn_text=miniapp.hero_btn_text,
                enable_popup=miniapp.enable_popup,
                popup_video_url=miniapp.popup_video_url,
                popup_text=miniapp.popup_text,
                footer_text=miniapp.footer_text
            )
            db.add(novo_miniapp)
    except Exception as e:
        erros.append(f"MiniApp: {str(e)}")
    
    # =======================================
    # 12. CLONAR MINI APP CATEGORIES
    # =======================================
    try:
        categorias = db.query(MiniAppCategory).filter(MiniAppCategory.bot_id == bot_id).all()
        for cat in categorias:
            nova_cat = MiniAppCategory(
                bot_id=novo_id,
                slug=cat.slug,
                title=cat.title,
                description=cat.description,
                cover_image=cat.cover_image,
                banner_mob_url=cat.banner_mob_url,
                bg_color=cat.bg_color,
                banner_desk_url=cat.banner_desk_url,
                video_preview_url=cat.video_preview_url,
                model_img_url=cat.model_img_url,
                model_name=cat.model_name,
                model_desc=cat.model_desc,
                footer_banner_url=cat.footer_banner_url,
                deco_lines_url=cat.deco_lines_url,
                model_name_color=cat.model_name_color,
                model_desc_color=cat.model_desc_color,
                theme_color=cat.theme_color,
                is_direct_checkout=cat.is_direct_checkout,
                is_hacker_mode=cat.is_hacker_mode,
                content_json=cat.content_json,
                items_per_page=cat.items_per_page,
                separator_enabled=cat.separator_enabled,
                separator_color=cat.separator_color,
                separator_text=cat.separator_text,
                separator_btn_text=cat.separator_btn_text,
                separator_btn_url=cat.separator_btn_url,
                separator_logo_url=cat.separator_logo_url,
                model_img_shape=cat.model_img_shape,
                separator_text_color=cat.separator_text_color,
                separator_btn_text_color=cat.separator_btn_text_color,
                separator_is_neon=cat.separator_is_neon,
                separator_neon_color=cat.separator_neon_color
            )
            db.add(nova_cat)
    except Exception as e:
        erros.append(f"MiniApp Categories: {str(e)}")
    
    # =======================================
    # 13. CLONAR REMARKETING CONFIG
    # =======================================
    try:
        rmkt = db.query(RemarketingConfig).filter(RemarketingConfig.bot_id == bot_id).first()
        if rmkt:
            novo_rmkt = RemarketingConfig(
                bot_id=novo_id,
                is_active=False,  # Começa desativado por segurança
                message_text=rmkt.message_text,
                media_url=rmkt.media_url,
                media_type=rmkt.media_type,
                delay_minutes=rmkt.delay_minutes,
                auto_destruct_enabled=rmkt.auto_destruct_enabled,
                auto_destruct_seconds=rmkt.auto_destruct_seconds,
                auto_destruct_after_click=rmkt.auto_destruct_after_click,
                promo_values=rmkt.promo_values
            )
            db.add(novo_rmkt)
    except Exception as e:
        erros.append(f"RemarketingConfig: {str(e)}")
    
    # =======================================
    # 14. CLONAR ALTERNATING MESSAGES
    # =======================================
    try:
        alt = db.query(AlternatingMessages).filter(AlternatingMessages.bot_id == bot_id).first()
        if alt:
            novo_alt = AlternatingMessages(
                bot_id=novo_id,
                is_active=False,  # Começa desativado
                messages=alt.messages,
                rotation_interval_seconds=alt.rotation_interval_seconds,
                stop_before_remarketing_seconds=alt.stop_before_remarketing_seconds,
                auto_destruct_final=alt.auto_destruct_final,
                max_duration_minutes=alt.max_duration_minutes,
                last_message_auto_destruct=alt.last_message_auto_destruct,
                last_message_destruct_seconds=alt.last_message_destruct_seconds
            )
            db.add(novo_alt)
    except Exception as e:
        erros.append(f"AlternatingMessages: {str(e)}")
    
    # =======================================
    # 15. CLONAR CANAL FREE CONFIG
    # =======================================
    try:
        cfree = db.query(CanalFreeConfig).filter(CanalFreeConfig.bot_id == bot_id).first()
        if cfree:
            novo_cfree = CanalFreeConfig(
                bot_id=novo_id,
                canal_id=None,  # Usuário precisa configurar
                canal_name=None,
                is_active=False,  # Desativado por segurança
                message_text=cfree.message_text,
                media_url=cfree.media_url,
                media_type=cfree.media_type,
                buttons=cfree.buttons,
                delay_seconds=cfree.delay_seconds
            )
            db.add(novo_cfree)
    except Exception as e:
        erros.append(f"CanalFree: {str(e)}")
    
    # =======================================
    # 16. CLONAR GRUPOS/CANAIS (BotGroup)
    # =======================================
    try:
        grupos = db.query(BotGroup).filter(BotGroup.bot_id == bot_id).all()
        for grupo in grupos:
            novo_grupo = BotGroup(
                bot_id=novo_id,
                owner_id=current_user.id,
                title=grupo.title,
                group_id=grupo.group_id,
                link=grupo.link,
                plan_ids=grupo.plan_ids,
                is_active=grupo.is_active
            )
            db.add(novo_grupo)
    except Exception as e:
        erros.append(f"BotGroups: {str(e)}")
    
    # =======================================
    # 17. COMMIT FINAL + WEBHOOK
    # =======================================
    db.commit()
    
    # Configura webhook no Telegram
    try:
        public_url = os.getenv("RAILWAY_PUBLIC_DOMAIN", "")
        if public_url.startswith("https://"):
            public_url = public_url.replace("https://", "")
        if public_url.endswith("/"):
            public_url = public_url[:-1]
        
        webhook_url = f"https://{public_url}/webhook/{dados.token}"
        new_tb.remove_webhook()
        time.sleep(0.5)
        new_tb.set_webhook(url=webhook_url)
        logger.info(f"🔗 Webhook clone definido: {webhook_url}")
    except Exception as e:
        erros.append(f"Webhook: {str(e)}")
    
    # Configura menu do bot
    try:
        configurar_menu_bot(dados.token)
    except:
        pass
    
    # 📋 AUDITORIA
    log_action(
        db=db,
        user_id=current_user.id,
        username=current_user.username,
        action="bot_cloned",
        resource_type="bot",
        resource_id=novo_id,
        description=f"Clonou bot '{bot_original.nome}' (ID: {bot_id}) → '{dados.nome}' (ID: {novo_id})",
        details={"original_bot_id": bot_id, "erros": erros if erros else None},
        ip_address=get_client_ip(request),
        user_agent=request.headers.get("user-agent")
    )
    
    logger.info(f"🔁 Bot clonado: '{bot_original.nome}' → '{dados.nome}' (ID: {novo_id}) | Erros: {len(erros)}")
    
    return {
        "status": "ok",
        "msg": f"Bot '{dados.nome}' clonado com sucesso!",
        "novo_bot_id": novo_id,
        "erros": erros if erros else None,
        "itens_copiados": [
            "Fluxo de mensagens", "Steps", "Planos", "Order Bump",
            "Upsell", "Downsell", "Mini App", "Categorias", 
            "Remarketing", "Mensagens Alternantes", "Canal Free", "Grupos"
        ]
    }

@app.delete("/api/admin/bots/{bot_id}")
def deletar_bot(
    bot_id: int,
    request: Request,
    db: Session = Depends(get_db),
    current_user = Depends(get_current_user)
):
    """
    Deleta bot - agora funciona com CASCADE do PostgreSQL
    """
    bot = verificar_bot_pertence_usuario(bot_id, current_user.id, db)
    
    dados_log = {
        "nome": bot.nome,
        "token": bot.token,
        "canal_vip": bot.id_canal_vip,
        "username": bot.username
    }
    
    try:
        # Remove webhook
        try:
            if dados_log["token"]:
                tb = telebot.TeleBot(dados_log["token"])
                tb.delete_webhook()
                logger.info(f"🔗 Webhook removido para bot {bot_id}")
        except Exception as e:
            logger.warning(f"⚠️ Webhook: {e}")

        # Deleta o bot (CASCADE faz o resto automaticamente)
        db.delete(bot)
        db.commit()
        
        # Auditoria
        log_action(
            db=db,
            user_id=current_user.id,
            username=current_user.username,
            action="bot_deleted",
            resource_type="bot",
            resource_id=bot_id,
            description=f"Deletou bot '{dados_log['nome']}'",
            details=dados_log,
            ip_address=get_client_ip(request),
            user_agent=request.headers.get("user-agent")
        )
        
        logger.info(f"🗑️ Bot {bot_id} deletado com sucesso")
        return {"status": "deletado", "bot_nome": dados_log["nome"]}

    except Exception as e:
        db.rollback()
        logger.error(f"❌ Erro ao deletar bot {bot_id}: {str(e)}")
        raise HTTPException(status_code=500, detail=f"Erro ao excluir bot: {str(e)}")

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
    # 🔒 FILTRA APENAS BOTS DO USUÁRIO (ordenados pela ordem do seletor)
    bots = db.query(BotModel).filter(
        BotModel.owner_id == current_user.id
    ).order_by(
        BotModel.selector_order.asc(),
        BotModel.created_at.desc()
    ).all()
    
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
        
        # 2. REVENUE (em CENTAVOS - CONSISTENTE com dashboard/profile/statistics)
        status_financeiro = ["approved", "paid", "active", "expired"]
        vendas_aprovadas = db.query(Pedido).filter(
            Pedido.bot_id == bot.id,
            Pedido.status.in_(status_financeiro)
        ).all()
        
        revenue = sum(int((v.valor or 0) * 100) for v in vendas_aprovadas)
        
        result.append({
            "id": bot.id,
            "nome": bot.nome,
            "token": bot.token,
            "username": bot.username or None,
            "id_canal_vip": bot.id_canal_vip,
            "admin_principal_id": bot.admin_principal_id,
            "suporte_username": bot.suporte_username,
            "id_canal_notificacao": bot.id_canal_notificacao,  # ✅ Canal de Notificações
            "protect_content": getattr(bot, 'protect_content', False),
            "notificar_no_bot": getattr(bot, 'notificar_no_bot', True),  # 🔥 NOVO
            "status": bot.status,
            "leads": leads_count,
            "revenue": revenue,
            "created_at": bot.created_at,
            "selector_order": getattr(bot, 'selector_order', 0) or 0
        })
    
    return result

# 🆕 ENDPOINT: CONSULTAR LIMITE DE BOTS DO USUÁRIO
@app.get("/api/admin/bot-limit")
def get_bot_limit(
    db: Session = Depends(get_db),
    current_user = Depends(get_current_user)
):
    """Retorna quantos bots o usuário tem e seu limite máximo"""
    bots_ativos = db.query(BotModel).filter(
        BotModel.owner_id == current_user.id,
        BotModel.status != 'deletado'
    ).count()
    
    max_bots = getattr(current_user, 'max_bots', 20) or 20
    plano = getattr(current_user, 'plano_plataforma', 'free') or 'free'
    
    # Super admin = ilimitado
    if current_user.is_superuser:
        max_bots = 9999
        plano = 'admin'
    
    return {
        "current": bots_ativos,
        "max": max_bots,
        "plano": plano,
        "can_create": bots_ativos < max_bots
    }

# 1. CRIAMOS O "MOLDE" PARA O FASTAPI ENTENDER O JSON DO FRONTEND
class SelectorOrderUpdate(BaseModel):
    order: List[int]

# 🆕 ENDPOINT: SALVAR ORDEM DOS BOTS NO SELETOR (drag-and-drop)
@app.put("/api/admin/bot-selector-order")
def update_selector_order(
    payload: SelectorOrderUpdate,  # Usamos o molde aqui em vez de 'dict'
    db: Session = Depends(get_db),
    current_user = Depends(get_current_user)
):
    """
    Recebe { "order": [bot_id_1, bot_id_2, ...] }
    Salva a ordem para exibir no seletor (os primeiros 7 aparecem no dropdown)
    """
    # Como usamos o molde, acessamos a lista diretamente com ".order"
    order_list = payload.order
    
    if not order_list:
        raise HTTPException(400, "Lista de ordem vazia")
    
    updated = 0
    for idx, bot_id in enumerate(order_list):
        # Mantive a sua lógica original de busca no banco!
        bot = db.query(BotModel).filter(
            BotModel.id == int(bot_id),
            BotModel.owner_id == current_user.id
        ).first()
        
        if bot:
            bot.selector_order = idx + 1  # 1-based order
            updated += 1
    
    db.commit()
    return {"status": "ok", "updated": updated}

# 🆕 ENDPOINT: MÉTRICAS AVANÇADAS POR BOT (para modal "Visão Geral")
@app.get("/api/admin/bots/{bot_id}/overview")
def get_bot_overview(
    bot_id: int,
    db: Session = Depends(get_db),
    current_user = Depends(get_current_user)
):
    """
    Retorna métricas avançadas de um bot específico:
    - Leads, faturamento total, assinantes ativos, conversão, plano mais vendido, etc.
    Todos os valores monetários em CENTAVOS (frontend divide por 100).
    """
    bot = verificar_bot_pertence_usuario(bot_id, current_user.id, db)
    
    # ============================================
    # 📊 STATUS PADRÃO (CONSISTENTE COM TODOS OS ENDPOINTS)
    # ============================================
    STATUS_FINANCEIRO = ['approved', 'paid', 'active', 'expired']
    
    # 1. VENDAS APROVADAS (ALL TIME)
    vendas = db.query(Pedido).filter(
        Pedido.bot_id == bot_id,
        Pedido.status.in_(STATUS_FINANCEIRO)
    ).all()
    
    # 2. FATURAMENTO TOTAL (em centavos, consistente com dashboard/profile)
    faturamento_total = sum(int((p.valor or 0) * 100) for p in vendas)
    total_vendas = len(vendas)
    
    # 3. LEADS TOTAIS (mesma lógica do listar_bots)
    leads_ids = set()
    leads_query = db.query(Lead.user_id).filter(Lead.bot_id == bot_id).all()
    for lead in leads_query:
        leads_ids.add(str(lead.user_id))
    
    pedidos_ids = set()
    pedidos_query = db.query(Pedido.telegram_id).filter(Pedido.bot_id == bot_id).all()
    for pedido in pedidos_query:
        pedidos_ids.add(str(pedido.telegram_id))
    
    leads_totais = len(leads_ids.union(pedidos_ids))
    
    # 4. ASSINANTES ATIVOS (não expirados OU vitalícios)
    assinantes_ativos = db.query(Pedido).filter(
        Pedido.bot_id == bot_id,
        Pedido.status.in_(STATUS_FINANCEIRO),
        or_(
            Pedido.data_expiracao > now_brazil(),
            Pedido.data_expiracao == None
        )
    ).count()
    
    # 5. TAXA DE CONVERSÃO (vendas / leads * 100)
    taxa_conversao = round((total_vendas / leads_totais) * 100, 2) if leads_totais > 0 else 0
    if taxa_conversao > 100:
        taxa_conversao = 100.0
    
    # 6. PLANO MAIS VENDIDO
    plano_mais_vendido = None
    try:
        from sqlalchemy import func as sqlfunc
        top_plano = db.query(
            Pedido.plano_nome,
            sqlfunc.count(Pedido.id).label('count')
        ).filter(
            Pedido.bot_id == bot_id,
            Pedido.status.in_(STATUS_FINANCEIRO),
            Pedido.plano_nome != None,
            Pedido.plano_nome != ''
        ).group_by(Pedido.plano_nome).order_by(sqlfunc.count(Pedido.id).desc()).first()
        
        if top_plano:
            plano_mais_vendido = {
                "nome": top_plano[0],
                "vendas": top_plano[1]
            }
    except Exception:
        pass
    
    # 7. TICKET MÉDIO (em centavos)
    ticket_medio = int(faturamento_total / total_vendas) if total_vendas > 0 else 0
    
    # 8. VENDAS HOJE
    hoje_start = now_brazil().replace(hour=0, minute=0, second=0, microsecond=0)
    vendas_hoje = db.query(Pedido).filter(
        Pedido.bot_id == bot_id,
        Pedido.status.in_(STATUS_FINANCEIRO),
        Pedido.data_aprovacao >= hoje_start
    ).count()
    
    # 9. FATURAMENTO ÚLTIMOS 30 DIAS (em centavos)
    data_30d = now_brazil() - timedelta(days=30)
    vendas_30d = db.query(Pedido).filter(
        Pedido.bot_id == bot_id,
        Pedido.status.in_(STATUS_FINANCEIRO),
        Pedido.data_aprovacao >= data_30d
    ).all()
    faturamento_30d = sum(int((p.valor or 0) * 100) for p in vendas_30d)
    
    # 10. VENDAS POR GATEWAY
    gateways = {}
    for v in vendas:
        gw = getattr(v, 'gateway_usada', None) or 'desconhecida'
        gateways[gw] = gateways.get(gw, 0) + 1
    
    return {
        "bot_id": bot_id,
        "bot_nome": bot.nome,
        "bot_username": bot.username or None,
        "status": bot.status,
        "created_at": str(bot.created_at) if bot.created_at else None,
        
        # Métricas principais (monetários em centavos)
        "leads_totais": leads_totais,
        "faturamento_total": faturamento_total,
        "faturamento_30d": faturamento_30d,
        "total_vendas": total_vendas,
        "vendas_hoje": vendas_hoje,
        "assinantes_ativos": assinantes_ativos,
        "taxa_conversao": taxa_conversao,
        "ticket_medio": ticket_medio,
        "plano_mais_vendido": plano_mais_vendido,
        "vendas_por_gateway": gateways
    }

# 🆕 ENDPOINT: MÉTRICAS AVANÇADAS GLOBAIS (todos os bots do usuário)
@app.get("/api/admin/bots-overview")
def get_all_bots_overview(
    db: Session = Depends(get_db),
    current_user = Depends(get_current_user)
):
    """
    Retorna métricas avançadas de TODOS os bots do usuário somados.
    Todos os valores monetários em CENTAVOS.
    """
    STATUS_FINANCEIRO = ['approved', 'paid', 'active', 'expired']
    
    # Buscar todos os bots do usuário
    if current_user.is_superuser:
        user_bots = db.query(BotModel).all()
    else:
        user_bots = db.query(BotModel).filter(BotModel.owner_id == current_user.id).all()
    
    bots_ids = [b.id for b in user_bots]
    
    if not bots_ids:
        return {
            "total_bots": 0, "bots_ativos": 0, "bots_pausados": 0,
            "leads_totais": 0, "faturamento_total": 0, "faturamento_30d": 0,
            "total_vendas": 0, "vendas_hoje": 0, "assinantes_ativos": 0,
            "taxa_conversao": 0, "ticket_medio": 0,
            "plano_mais_vendido": None, "vendas_por_gateway": {},
            "top_bot": None, "bots_ranking": []
        }
    
    # 1. VENDAS APROVADAS (ALL TIME)
    vendas = db.query(Pedido).filter(
        Pedido.bot_id.in_(bots_ids),
        Pedido.status.in_(STATUS_FINANCEIRO)
    ).all()
    
    # 2. FATURAMENTO TOTAL (centavos)
    faturamento_total = sum(int((p.valor or 0) * 100) for p in vendas)
    total_vendas = len(vendas)
    
    # 3. LEADS TOTAIS (todos os bots)
    leads_ids = set()
    for lid in db.query(Lead.user_id).filter(Lead.bot_id.in_(bots_ids)).all():
        leads_ids.add(str(lid.user_id))
    for pid in db.query(Pedido.telegram_id).filter(Pedido.bot_id.in_(bots_ids)).all():
        leads_ids.add(str(pid.telegram_id))
    leads_totais = len(leads_ids)
    
    # 4. ASSINANTES ATIVOS
    assinantes_ativos = db.query(Pedido).filter(
        Pedido.bot_id.in_(bots_ids),
        Pedido.status.in_(STATUS_FINANCEIRO),
        or_(
            Pedido.data_expiracao > now_brazil(),
            Pedido.data_expiracao == None
        )
    ).count()
    
    # 5. CONVERSÃO
    taxa_conversao = round((total_vendas / leads_totais) * 100, 2) if leads_totais > 0 else 0
    if taxa_conversao > 100:
        taxa_conversao = 100.0
    
    # 6. PLANO MAIS VENDIDO (global)
    plano_mais_vendido = None
    try:
        from sqlalchemy import func as sqlfunc
        top_plano = db.query(
            Pedido.plano_nome,
            sqlfunc.count(Pedido.id).label('count')
        ).filter(
            Pedido.bot_id.in_(bots_ids),
            Pedido.status.in_(STATUS_FINANCEIRO),
            Pedido.plano_nome != None,
            Pedido.plano_nome != ''
        ).group_by(Pedido.plano_nome).order_by(sqlfunc.count(Pedido.id).desc()).first()
        
        if top_plano:
            plano_mais_vendido = {"nome": top_plano[0], "vendas": top_plano[1]}
    except Exception:
        pass
    
    # 7. TICKET MÉDIO
    ticket_medio = int(faturamento_total / total_vendas) if total_vendas > 0 else 0
    
    # 8. VENDAS HOJE
    hoje_start = now_brazil().replace(hour=0, minute=0, second=0, microsecond=0)
    vendas_hoje = db.query(Pedido).filter(
        Pedido.bot_id.in_(bots_ids),
        Pedido.status.in_(STATUS_FINANCEIRO),
        Pedido.data_aprovacao >= hoje_start
    ).count()
    
    # 9. FATURAMENTO 30 DIAS
    data_30d = now_brazil() - timedelta(days=30)
    vendas_30d = db.query(Pedido).filter(
        Pedido.bot_id.in_(bots_ids),
        Pedido.status.in_(STATUS_FINANCEIRO),
        Pedido.data_aprovacao >= data_30d
    ).all()
    faturamento_30d = sum(int((p.valor or 0) * 100) for p in vendas_30d)
    
    # 10. GATEWAYS
    gateways = {}
    for v in vendas:
        gw = getattr(v, 'gateway_usada', None) or 'desconhecida'
        gateways[gw] = gateways.get(gw, 0) + 1
    
    # 11. BOTS ATIVOS/PAUSADOS
    bots_ativos = sum(1 for b in user_bots if b.status == 'ativo')
    bots_pausados = len(user_bots) - bots_ativos
    
    # 12. RANKING DOS BOTS (top por faturamento)
    bots_ranking = []
    for bot in user_bots:
        bot_vendas = [v for v in vendas if v.bot_id == bot.id]
        bot_revenue = sum(int((v.valor or 0) * 100) for v in bot_vendas)
        bots_ranking.append({
            "id": bot.id,
            "nome": bot.nome,
            "faturamento": bot_revenue,
            "vendas": len(bot_vendas),
            "status": bot.status
        })
    
    bots_ranking.sort(key=lambda x: x['faturamento'], reverse=True)
    top_bot = bots_ranking[0] if bots_ranking else None
    
    return {
        "total_bots": len(user_bots),
        "bots_ativos": bots_ativos,
        "bots_pausados": bots_pausados,
        "leads_totais": leads_totais,
        "faturamento_total": faturamento_total,
        "faturamento_30d": faturamento_30d,
        "total_vendas": total_vendas,
        "vendas_hoje": vendas_hoje,
        "assinantes_ativos": assinantes_ativos,
        "taxa_conversao": taxa_conversao,
        "ticket_medio": ticket_medio,
        "plano_mais_vendido": plano_mais_vendido,
        "vendas_por_gateway": gateways,
        "top_bot": top_bot,
        "bots_ranking": bots_ranking[:10]
    }

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

        # Tratamento do canal de destino (Pega do JSON recebido)
        # 🔥 CORREÇÃO: Usamos data.get() em vez de plano.id_canal_destino
        canal_destino = data.get("id_canal_destino")
        if not canal_destino or str(canal_destino).strip() == "":
            canal_destino = None

        novo_plano = PlanoConfig(
            bot_id=bot_id,
            nome_exibicao=data.get("nome_exibicao", "Novo Plano"),
            descricao=data.get("descricao", f"Acesso de {dias_duracao} dias"),
            preco_atual=float(data.get("preco_atual", 0.0)),
            preco_cheio=preco_orig,
            dias_duracao=dias_duracao,
            is_lifetime=is_lifetime, 
            id_canal_destino=canal_destino, # ✅ AGORA SALVA O CANAL CORRETAMENTE
            key_id=f"plan_{bot_id}_{int(time.time())}"
        )
        
        db.add(novo_plano)
        db.commit()
        db.refresh(novo_plano)
        
        logger.info(f"✅ Plano criado: {novo_plano.nome_exibicao} | Vitalício: {is_lifetime}")
        
        # 📋 AUDITORIA: Plano criado
        try:
            log_action(db=db, user_id=None, username="system", action="plan_created", resource_type="plan", 
                       resource_id=novo_plano.id, description=f"Plano '{novo_plano.nome_exibicao}' criado (R$ {novo_plano.preco_atual:.2f})")
        except: pass
        
        return novo_plano

    except TypeError as te:
        logger.warning(f"⚠️ Tentando criar plano sem 'preco_cheio' devido a erro: {te}")
        db.rollback()
        try:
            # Fallback para criação simples
            canal_destino = data.get("id_canal_destino")
            if not canal_destino or str(canal_destino).strip() == "":
                canal_destino = None

            novo_plano_fallback = PlanoConfig(
                bot_id=bot_id,
                nome_exibicao=data.get("nome_exibicao"),
                descricao=data.get("descricao"),
                preco_atual=float(data.get("preco_atual")),
                dias_duracao=int(data.get("dias_duracao")),
                is_lifetime=data.get("is_lifetime", False), 
                id_canal_destino=canal_destino, # ✅ CORRIGIDO AQUI TAMBÉM
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
    Atualiza um plano existente (incluindo is_lifetime e id_canal_destino)
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
        
        # --- ATUALIZAÇÃO DOS CAMPOS ---
        
        if "nome_exibicao" in data:
            plano.nome_exibicao = data["nome_exibicao"]
            
        if "descricao" in data:
            plano.descricao = data["descricao"]
            
        # ✅ CORREÇÃO MESTRE CÓDIGO FÁCIL: 
        # O nome da coluna no banco é 'preco_atual', não 'preco'.
        if "preco_atual" in data:
            plano.preco_atual = float(data["preco_atual"]) 
            
        if "dias_duracao" in data:
            plano.dias_duracao = int(data["dias_duracao"])
            
        if "is_lifetime" in data:
            plano.is_lifetime = bool(data["is_lifetime"])

        # ✅ NOVO CAMPO (V7): CANAL DE DESTINO
        if "id_canal_destino" in data:
            valor_canal = data["id_canal_destino"]
            # Se vier vazio ou nulo, salvamos None (para usar o padrão do bot)
            if not valor_canal or str(valor_canal).strip() == "":
                plano.id_canal_destino = None
            else:
                plano.id_canal_destino = str(valor_canal).strip()
        
        db.commit()
        db.refresh(plano)
        
        logger.info(f"✏️ Plano {plano.id} atualizado: {plano.nome_exibicao} | Canal: {plano.id_canal_destino}")
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
        
        nome_plano = plano.nome_exibicao
        
        db.delete(plano)
        db.commit()
        
        # 📋 AUDITORIA: Plano deletado
        try:
            log_action(db=db, user_id=None, username="system", action="plan_deleted", resource_type="plan",
                       resource_id=plano_id, description=f"Plano '{nome_plano}' deletado do bot {bot_id}")
        except: pass
        
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
            "btn_aceitar": "✅ SIM, ADICIONAR", "btn_recusar": "❌ NÃO, OBRIGADO",
            "audio_url": None, "audio_delay_seconds": 3
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
    bump.group_id = dados.group_id  # ✅ FASE 2
    bump.autodestruir = dados.autodestruir
    bump.msg_texto = dados.msg_texto
    bump.msg_media = dados.msg_media
    bump.btn_aceitar = dados.btn_aceitar
    bump.btn_recusar = dados.btn_recusar
    bump.audio_url = dados.audio_url
    bump.audio_delay_seconds = dados.audio_delay_seconds
    
    db.commit()
    return {"status": "ok"}

# =========================================================
# 🚀 ROTAS UPSELL
# =========================================================
@app.get("/api/admin/bots/{bot_id}/upsell")
def get_upsell(bot_id: int, db: Session = Depends(get_db)):
    config = db.query(UpsellConfig).filter(UpsellConfig.bot_id == bot_id).first()
    if not config:
        return {
            "ativo": False, "nome_produto": "", "preco": 0.0, "link_acesso": "",
            "delay_minutos": 2, "msg_texto": "🔥 Oferta exclusiva para você!", "msg_media": "",
            "btn_aceitar": "✅ QUERO ESSA OFERTA!", "btn_recusar": "❌ NÃO, OBRIGADO",
            "autodestruir": False, "audio_url": None, "audio_delay_seconds": 3
        }
    return config

@app.post("/api/admin/bots/{bot_id}/upsell")
def save_upsell(
    bot_id: int,
    dados: UpsellCreate,
    db: Session = Depends(get_db),
    current_user = Depends(get_current_user)
):
    verificar_bot_pertence_usuario(bot_id, current_user.id, db)
    
    config = db.query(UpsellConfig).filter(UpsellConfig.bot_id == bot_id).first()
    if not config:
        config = UpsellConfig(bot_id=bot_id)
        db.add(config)
    
    config.ativo = dados.ativo
    config.nome_produto = dados.nome_produto
    config.preco = dados.preco
    config.link_acesso = dados.link_acesso
    config.group_id = dados.group_id  # ✅ FASE 2
    config.delay_minutos = dados.delay_minutos
    config.msg_texto = dados.msg_texto
    config.msg_media = dados.msg_media
    config.btn_aceitar = dados.btn_aceitar
    config.btn_recusar = dados.btn_recusar
    config.autodestruir = dados.autodestruir
    config.audio_url = dados.audio_url
    config.audio_delay_seconds = dados.audio_delay_seconds
    
    db.commit()
    return {"status": "ok"}

# =========================================================
# 📉 ROTAS DOWNSELL
# =========================================================
@app.get("/api/admin/bots/{bot_id}/downsell")
def get_downsell(bot_id: int, db: Session = Depends(get_db)):
    config = db.query(DownsellConfig).filter(DownsellConfig.bot_id == bot_id).first()
    if not config:
        return {
            "ativo": False, "nome_produto": "", "preco": 0.0, "link_acesso": "",
            "delay_minutos": 10, "msg_texto": "🎁 Última chance! Oferta especial só para você!", "msg_media": "",
            "btn_aceitar": "✅ QUERO ESSA OFERTA!", "btn_recusar": "❌ NÃO, OBRIGADO",
            "autodestruir": False, "audio_url": None, "audio_delay_seconds": 3
        }
    return config

@app.post("/api/admin/bots/{bot_id}/downsell")
def save_downsell(
    bot_id: int,
    dados: DownsellCreate,
    db: Session = Depends(get_db),
    current_user = Depends(get_current_user)
):
    verificar_bot_pertence_usuario(bot_id, current_user.id, db)
    
    config = db.query(DownsellConfig).filter(DownsellConfig.bot_id == bot_id).first()
    if not config:
        config = DownsellConfig(bot_id=bot_id)
        db.add(config)
    
    config.ativo = dados.ativo
    config.nome_produto = dados.nome_produto
    config.preco = dados.preco
    config.link_acesso = dados.link_acesso
    config.group_id = dados.group_id  # ✅ FASE 2
    config.delay_minutos = dados.delay_minutos
    config.msg_texto = dados.msg_texto
    config.msg_media = dados.msg_media
    config.btn_aceitar = dados.btn_aceitar
    config.btn_recusar = dados.btn_recusar
    config.autodestruir = dados.autodestruir
    config.audio_url = dados.audio_url
    config.audio_delay_seconds = dados.audio_delay_seconds
    
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
    current_user = Depends(get_current_user)
):
    # 🔒 VERIFICA SE O BOT PERTENCE AO USUÁRIO
    verificar_bot_pertence_usuario(bot_id, current_user.id, db)
    
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
            "miniapp_btn_text": "ABRIR LOJA",
            "msg_pix": "",
            "button_mode": "next_step",  # 🔥 NOVO
            "buttons_config": [],  # 🔥 NOVO
            "buttons_config_2": []  # 🔥 NOVO
        }
    
    # 🔥 SERIALIZA MANUALMENTE PARA GARANTIR QUE buttons_config SEJA INCLUÍDO
    return {
        "id": fluxo.id,
        "bot_id": fluxo.bot_id,
        "msg_boas_vindas": fluxo.msg_boas_vindas,
        "media_url": fluxo.media_url,
        "btn_text_1": fluxo.btn_text_1,
        "autodestruir_1": fluxo.autodestruir_1,
        "msg_2_texto": fluxo.msg_2_texto,
        "msg_2_media": fluxo.msg_2_media,
        "mostrar_planos_2": fluxo.mostrar_planos_2,
        "mostrar_planos_1": fluxo.mostrar_planos_1,
        "start_mode": fluxo.start_mode,
        "miniapp_url": fluxo.miniapp_url,
        "miniapp_btn_text": fluxo.miniapp_btn_text,
        "msg_pix": fluxo.msg_pix,
        "button_mode": fluxo.button_mode if hasattr(fluxo, 'button_mode') else "next_step",  # 🔥 NOVO
        "buttons_config": fluxo.buttons_config if fluxo.buttons_config else [],  # 🔥 NOVO
        "buttons_config_2": fluxo.buttons_config_2 if fluxo.buttons_config_2 else []  # 🔥 NOVO
    }

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
    msg_pix: Optional[str] = None
    
    # 🔥 NOVOS CAMPOS PARA BOTÕES PERSONALIZADOS
    button_mode: Optional[str] = "next_step"  # "next_step" ou "custom"
    buttons_config: Optional[List[dict]] = None  # Botões da mensagem 1
    buttons_config_2: Optional[List[dict]] = None  # Botões da mensagem final
    
    steps: Optional[List[dict]] = None  # Passos extras

@app.post("/api/admin/bots/{bot_id}/flow")
def salvar_fluxo(
    bot_id: int, 
    flow: FlowUpdate, 
    db: Session = Depends(get_db),
    current_user = Depends(get_current_user)
):
    # 🔒 VERIFICA SE O BOT PERTENCE AO USUÁRIO
    verificar_bot_pertence_usuario(bot_id, current_user.id, db)
    
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
    
    # 🔥 ATUALIZA MENSAGEM DO PIX
    if flow.msg_pix is not None: fluxo_db.msg_pix = flow.msg_pix

    # 🔥 ATUALIZA MODO DE BOTÃO E CONFIGURAÇÕES
    if flow.button_mode is not None: 
        fluxo_db.button_mode = flow.button_mode
    
    if flow.buttons_config is not None: 
        fluxo_db.buttons_config = flow.buttons_config
    
    if flow.buttons_config_2 is not None: 
        fluxo_db.buttons_config_2 = flow.buttons_config_2

    # 🔥 ATUALIZA PASSOS EXTRAS (STEPS)
    if flow.steps is not None:
        # Remove passos antigos
        db.query(BotFlowStep).filter(BotFlowStep.bot_id == bot_id).delete()
        
        # Adiciona novos passos
        novos_passos = []
        for i, s in enumerate(flow.steps):
            novo = BotFlowStep(
                bot_id=bot_id,
                step_order=i + 1,
                msg_texto=s.get('msg_texto'),
                msg_media=s.get('msg_media'),
                btn_texto=s.get('btn_texto'),
                autodestruir=s.get('autodestruir', False),
                mostrar_botao=s.get('mostrar_botao', True),
                delay_seconds=s.get('delay_seconds', 0),
                buttons_config=s.get('buttons_config')  # 🔥 NOVO
            )
            novos_passos.append(novo)
        
        if novos_passos:
            db.add_all(novos_passos)

    db.commit()
    
    logger.info(f"💾 Fluxo do Bot {bot_id} salvo com sucesso (Owner: {current_user.username})")
    
    return {"status": "saved"}

# =========================================================
# 🔗 ROTAS DE TRACKING (RASTREAMENTO)
# =========================================================

# --- 1. PASTAS (FOLDERS) ---

# ============================================================
# 🛡️ SCHEMAS DE RASTREAMENTO (Adicione antes das rotas)
# ============================================================

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

# ============================================================
# 📂 ROTAS DE RASTREAMENTO (TRACKING) - SEGURANÇA APLICADA
# ============================================================

# --- 1. PASTAS (FOLDERS) ---

# --- 1. PASTAS (FOLDERS) ---

@app.get("/api/admin/tracking/folders")
def list_tracking_folders(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """
    Lista pastas com filtro de segurança:
    O usuário só vê pastas que são DELE (owner_id) ou que contêm links dos SEUS bots.
    Superadmin vê APENAS suas próprias pastas também (evita poluição).
    """
    try:
        user_bot_ids = [bot.id for bot in current_user.bots]
        
        # Busca todas as pastas
        folders = db.query(TrackingFolder).order_by(desc(TrackingFolder.created_at)).all()
        
        result = []
        for f in folders:
            # 🔥 FILTRO PRINCIPAL: Pasta pertence a este usuário?
            is_owner = (f.owner_id == current_user.id) if f.owner_id else False
            
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
            
            # 🔥 LÓGICA DE VISIBILIDADE (CORRIGIDA):
            # Mostra SE:
            # 1. Eu sou o dono da pasta (owner_id == meu id)
            # 2. OU tenho links meus lá dentro
            # 3. OU a pasta não tem dono (legado) E eu tenho links lá
            # Pastas vazias de outros usuários NÃO aparecem mais
            should_show = False
            
            if is_owner:
                should_show = True
            elif meus_links_count > 0:
                should_show = True
            elif f.owner_id is None and meus_links_count > 0:
                should_show = True
            # Pastas sem dono E sem links meus = não mostra (era aqui o bug)

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
        
        return result
        
    except Exception as e:
        logger.error(f"Erro ao listar pastas: {e}")
        return []

@app.post("/api/admin/tracking/folders")
def create_tracking_folder(
    dados: TrackingFolderCreate, 
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    try:
        # 🔥 Verifica duplicidade POR USUÁRIO (não global)
        existe = db.query(TrackingFolder).filter(
            func.lower(TrackingFolder.nome) == dados.nome.lower(),
            TrackingFolder.owner_id == current_user.id
        ).first()
        
        if existe:
            return {"status": "ok", "id": existe.id, "msg": "Pasta já existia"}

        nova_pasta = TrackingFolder(
            nome=dados.nome, 
            plataforma=dados.plataforma,
            owner_id=current_user.id,  # 🔥 NOVO: Marca o dono
            created_at=now_brazil()
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
def delete_tracking_folder(
    fid: int, 
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user) # ✅ CORRIGIDO
):
    try:
        user_bot_ids = [bot.id for bot in current_user.bots]
        is_admin = current_user.is_superuser
        
        folder = db.query(TrackingFolder).filter(TrackingFolder.id == fid).first()
        if not folder:
            raise HTTPException(404, "Pasta não encontrada")
        
        # 🔥 BLINDAGEM: Se NÃO for admin, verifica se tem links de outros usuários
        if not is_admin:
            links_outros = db.query(TrackingLink).filter(
                TrackingLink.folder_id == fid,
                TrackingLink.bot_id.notin_(user_bot_ids)
            ).count()
            
            if links_outros > 0:
                raise HTTPException(403, "Você não pode apagar esta pasta pois ela contém links de outros usuários.")
        
        # Limpeza
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
def list_tracking_links(
    folder_id: int, 
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user) # ✅ CORRIGIDO
):
    """
    Lista links, filtrando APENAS os que pertencem aos bots do usuário.
    """
    user_bot_ids = [bot.id for bot in current_user.bots]
    is_admin = current_user.is_superuser

    query = db.query(TrackingLink).filter(TrackingLink.folder_id == folder_id)
    
    # 🔥 BLINDAGEM: Filtra só os links dos MEUS bots
    if not is_admin:
        if not user_bot_ids: 
            return []
        query = query.filter(TrackingLink.bot_id.in_(user_bot_ids))
    
    return query.order_by(desc(TrackingLink.created_at)).all()

@app.post("/api/admin/tracking/links")
def create_tracking_link(
    dados: TrackingLinkCreate, 
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user) # ✅ CORRIGIDO
):
    try:
        user_bot_ids = [bot.id for bot in current_user.bots]
        is_admin = current_user.is_superuser
        
        # 🔥 BLINDAGEM: Verifica propriedade do bot
        if not is_admin:
            if dados.bot_id not in user_bot_ids:
                raise HTTPException(403, "Você não tem permissão para criar links neste bot.")

        # Gera código aleatório se vazio
        if not dados.codigo:
            import random, string
            chars = string.ascii_lowercase + string.digits
            dados.codigo = ''.join(random.choice(chars) for _ in range(8))
        
        # Verifica colisão
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
            faturamento=0.0,
            created_at=now_brazil()
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
def delete_link(
    lid: int, 
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    try:
        user_bot_ids = [bot.id for bot in current_user.bots]
        is_admin = current_user.is_superuser
        
        link = db.query(TrackingLink).filter(TrackingLink.id == lid).first()
        if not link:
            raise HTTPException(404, "Link não encontrado")
        
        # 🔥 BLINDAGEM: Verifica propriedade
        if not is_admin:
            if link.bot_id not in user_bot_ids:
                raise HTTPException(403, "Acesso negado. Você não é dono deste link.")
        
        # 🔥 FIX: Desvincula pedidos e leads que referenciam este link (SET NULL)
        db.query(Pedido).filter(Pedido.tracking_id == lid).update(
            {"tracking_id": None}, synchronize_session=False
        )
        db.query(Lead).filter(Lead.tracking_id == lid).update(
            {"tracking_id": None}, synchronize_session=False
        )
        
        db.delete(link)
        db.commit()
        logger.info(f"🗑️ Link #{lid} deletado por {current_user.username}")
        return {"status": "deleted"}
        
    except HTTPException as he:
        raise he
    except Exception as e:
        logger.error(f"Erro ao deletar link: {e}")
        db.rollback()
        raise HTTPException(500, "Erro interno")

# =========================================================
# 📊 ROTAS DE MÉTRICAS AVANÇADAS DE TRACKING
# =========================================================

@app.get("/api/admin/tracking/link/{link_id}/metrics")
def get_tracking_link_metrics(
    link_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """
    Métricas detalhadas de um link com breakdown Normal/Upsell/Downsell/Remarketing/DisparoAuto/OrderBump.
    """
    try:
        user_bot_ids = [bot.id for bot in current_user.bots]
        
        link = db.query(TrackingLink).filter(TrackingLink.id == link_id).first()
        if not link:
            raise HTTPException(404, "Link não encontrado")
        
        if not current_user.is_superuser and link.bot_id not in user_bot_ids:
            raise HTTPException(403, "Acesso negado")
        
        # 🔥 Busca preço do Order Bump do bot (para calcular faturamento isolado)
        bump_config = db.query(OrderBumpConfig).filter(
            OrderBumpConfig.bot_id == link.bot_id
        ).first()
        bump_preco = float(bump_config.preco) if bump_config and bump_config.preco else 0.0
        
        # Busca todos os pedidos aprovados vinculados a este tracking_id
        pedidos = db.query(Pedido).filter(
            Pedido.tracking_id == link_id,
            Pedido.status.in_(['paid', 'approved', 'active'])
        ).all()
        
        # Breakdown por tipo
        normais_vendas = 0
        normais_fat = 0.0
        upsell_vendas = 0
        upsell_fat = 0.0
        downsell_vendas = 0
        downsell_fat = 0.0
        remarketing_vendas = 0
        remarketing_fat = 0.0
        disparo_auto_vendas = 0
        disparo_auto_fat = 0.0
        order_bump_vendas = 0
        order_bump_fat = 0.0
        
        for p in pedidos:
            nome_lower = str(p.plano_nome or "").lower()
            origem = str(p.origem or "").lower()
            valor = float(p.valor or 0)
            
            # 🔥 Se tem Order Bump, separa o faturamento do bump
            if p.tem_order_bump and bump_preco > 0:
                order_bump_vendas += 1
                order_bump_fat += bump_preco
                valor = valor - bump_preco  # Valor restante é do plano/oferta
            
            if "upsell:" in nome_lower:
                upsell_vendas += 1
                upsell_fat += valor
            elif "downsell:" in nome_lower:
                downsell_vendas += 1
                downsell_fat += valor
            elif origem == 'remarketing' or "(oferta)" in nome_lower:
                remarketing_vendas += 1
                remarketing_fat += valor
            elif origem == 'disparo_auto' or "(oferta automática)" in nome_lower:
                disparo_auto_vendas += 1
                disparo_auto_fat += valor
            else:
                normais_vendas += 1
                normais_fat += valor
        
        total_vendas = normais_vendas + upsell_vendas + downsell_vendas + remarketing_vendas + disparo_auto_vendas + order_bump_vendas
        total_fat = normais_fat + upsell_fat + downsell_fat + remarketing_fat + disparo_auto_fat + order_bump_fat
        
        # 🔥 CORREÇÃO: Lógica infalível de conversão
        # Se vendas > leads, significa que leads não é uma base confiável → usa cliques
        # Conversão NUNCA deve ultrapassar 100%
        leads_count = getattr(link, 'leads', 0) or 0
        clicks_count = link.clicks or 0
        
        if total_vendas <= 0:
            conversao = 0.0
        elif leads_count > 0 and leads_count >= total_vendas:
            conversao = round((total_vendas / leads_count * 100), 2)
        elif clicks_count > 0:
            conversao = round((total_vendas / clicks_count * 100), 2)
        else:
            conversao = 0.0
        
        conversao = min(conversao, 100.0)
        
        return {
            "link_id": link.id,
            "codigo": link.codigo,
            "nome": link.nome,
            "cliques": link.clicks or 0,
            "leads": leads_count,
            "vendas_total": total_vendas,
            "faturamento_total": round(total_fat, 2),
            "conversao": conversao,
            "breakdown": {
                "normais": {"vendas": normais_vendas, "faturamento": round(normais_fat, 2)},
                "upsell": {"vendas": upsell_vendas, "faturamento": round(upsell_fat, 2)},
                "downsell": {"vendas": downsell_vendas, "faturamento": round(downsell_fat, 2)},
                "remarketing": {"vendas": remarketing_vendas, "faturamento": round(remarketing_fat, 2)},
                "disparo_auto": {"vendas": disparo_auto_vendas, "faturamento": round(disparo_auto_fat, 2)},
                "order_bump": {"vendas": order_bump_vendas, "faturamento": round(order_bump_fat, 2)}
            }
        }
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Erro métricas link {link_id}: {e}")
        raise HTTPException(500, "Erro interno")


@app.get("/api/admin/tracking/chart")
def get_tracking_chart(
    days: int = 7,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """
    Dados para gráfico de desempenho temporal (vendas por dia por código).
    """
    try:
        user_bot_ids = [bot.id for bot in current_user.bots]
        if not user_bot_ids:
            return {"labels": [], "datasets": []}
        
        # Busca links do usuário
        meus_links = db.query(TrackingLink).filter(
            TrackingLink.bot_id.in_(user_bot_ids)
        ).all()
        
        if not meus_links:
            return {"labels": [], "datasets": []}
        
        link_ids = [l.id for l in meus_links]
        link_map = {l.id: l.codigo for l in meus_links}
        
        # Período
        now = now_brazil()
        start_date = now - timedelta(days=days)
        
        # Busca pedidos aprovados no período
        pedidos = db.query(Pedido).filter(
            Pedido.tracking_id.in_(link_ids),
            Pedido.status.in_(['paid', 'approved', 'active']),
            Pedido.data_aprovacao >= start_date
        ).all()
        
        # Gera labels (datas)
        labels = []
        for i in range(days):
            d = start_date + timedelta(days=i+1)
            labels.append(d.strftime("%d/%m"))
        
        # Agrupa vendas por código por dia
        datasets_map = {}
        
        for p in pedidos:
            if not p.tracking_id or not p.data_aprovacao:
                continue
            
            codigo = link_map.get(p.tracking_id, "desconhecido")
            dia_label = p.data_aprovacao.strftime("%d/%m")
            
            if codigo not in datasets_map:
                datasets_map[codigo] = {label: 0 for label in labels}
            
            if dia_label in datasets_map[codigo]:
                datasets_map[codigo][dia_label] += 1
        
        # Converte para formato final
        datasets = []
        for codigo, dias_data in datasets_map.items():
            datasets.append({
                "codigo": codigo,
                "data": [dias_data.get(label, 0) for label in labels]
            })
        
        return {"labels": labels, "datasets": datasets}
        
    except Exception as e:
        logger.error(f"Erro tracking chart: {e}")
        return {"labels": [], "datasets": []}


@app.get("/api/admin/tracking/ranking")
def get_tracking_ranking(
    limit: int = 10,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """
    Top códigos por faturamento com breakdown.
    """
    try:
        user_bot_ids = [bot.id for bot in current_user.bots]
        if not user_bot_ids:
            return []
        
        # Busca links do usuário ordenados por faturamento
        meus_links = db.query(TrackingLink).filter(
            TrackingLink.bot_id.in_(user_bot_ids)
        ).order_by(desc(TrackingLink.faturamento)).limit(limit).all()
        
        result = []
        for link in meus_links:
            # Busca pedidos para breakdown
            pedidos = db.query(Pedido).filter(
                Pedido.tracking_id == link.id,
                Pedido.status.in_(['paid', 'approved', 'active'])
            ).all()
            
            # 🔥 Busca preço do Order Bump do bot
            bump_config = db.query(OrderBumpConfig).filter(
                OrderBumpConfig.bot_id == link.bot_id
            ).first()
            bump_preco = float(bump_config.preco) if bump_config and bump_config.preco else 0.0
            
            normais_fat = 0.0
            normais_v = 0
            upsell_fat = 0.0
            upsell_v = 0
            downsell_fat = 0.0
            downsell_v = 0
            remarketing_fat = 0.0
            remarketing_v = 0
            disparo_auto_fat = 0.0
            disparo_auto_v = 0
            order_bump_fat = 0.0
            order_bump_v = 0
            
            for p in pedidos:
                nome_lower = str(p.plano_nome or "").lower()
                origem = str(p.origem or "").lower()
                valor = float(p.valor or 0)
                
                # 🔥 Se tem Order Bump, separa o faturamento do bump
                if p.tem_order_bump and bump_preco > 0:
                    order_bump_v += 1
                    order_bump_fat += bump_preco
                    valor = valor - bump_preco
                
                if "upsell:" in nome_lower:
                    upsell_v += 1
                    upsell_fat += valor
                elif "downsell:" in nome_lower:
                    downsell_v += 1
                    downsell_fat += valor
                elif origem == 'remarketing' or "(oferta)" in nome_lower:
                    remarketing_v += 1
                    remarketing_fat += valor
                elif origem == 'disparo_auto' or "(oferta automática)" in nome_lower:
                    disparo_auto_v += 1
                    disparo_auto_fat += valor
                else:
                    normais_v += 1
                    normais_fat += valor
            
            total_vendas = normais_v + upsell_v + downsell_v + remarketing_v + disparo_auto_v + order_bump_v
            total_fat = normais_fat + upsell_fat + downsell_fat + remarketing_fat + disparo_auto_fat + order_bump_fat
            
            # 🔥 CORREÇÃO MESTRE: Lógica infalível para conversão
            leads_count = getattr(link, 'leads', 0) or 0
            base_calculo = leads_count if leads_count > 0 else (link.clicks or 0)
            conversao = round((total_vendas / base_calculo * 100), 2) if base_calculo > 0 else 0.0
            
            result.append({
                "id": link.id,
                "codigo": link.codigo,
                "nome": link.nome,
                "cliques": link.clicks or 0,
                "leads": leads_count,
                "vendas_total": total_vendas,
                "faturamento_total": round(total_fat, 2),
                "conversao": conversao,
                "breakdown": {
                    "normais": {"vendas": normais_v, "faturamento": round(normais_fat, 2)},
                    "upsell": {"vendas": upsell_v, "faturamento": round(upsell_fat, 2)},
                    "downsell": {"vendas": downsell_v, "faturamento": round(downsell_fat, 2)},
                    "remarketing": {"vendas": remarketing_v, "faturamento": round(remarketing_fat, 2)},
                    "disparo_auto": {"vendas": disparo_auto_v, "faturamento": round(disparo_auto_fat, 2)},
                    "order_bump": {"vendas": order_bump_v, "faturamento": round(order_bump_fat, 2)}
                }
            })
        
        return result
        
    except Exception as e:
        logger.error(f"Erro tracking ranking: {e}")
        return []

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
            
            # Mini App V2: Separador, Paginação, Formato
            categoria.items_per_page = data.items_per_page
            categoria.separator_enabled = data.separator_enabled
            categoria.separator_color = data.separator_color
            categoria.separator_text = data.separator_text
            categoria.separator_btn_text = data.separator_btn_text
            categoria.separator_btn_url = data.separator_btn_url
            categoria.separator_logo_url = data.separator_logo_url
            categoria.model_img_shape = data.model_img_shape

            # 🔥 CORREÇÃO: SALVANDO AS CORES DO TEXTO DA BARRA E BOTÃO + NEON
            categoria.separator_text_color = data.separator_text_color
            categoria.separator_btn_text_color = data.separator_btn_text_color
            categoria.separator_is_neon = data.separator_is_neon
            categoria.separator_neon_color = data.separator_neon_color
            
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
                bg_color=data.bg_color,
                banner_desk_url=data.banner_desk_url,
                video_preview_url=data.video_preview_url,
                model_img_url=data.model_img_url,
                model_name=data.model_name,
                model_desc=data.model_desc,
                footer_banner_url=data.footer_banner_url,
                deco_lines_url=data.deco_lines_url,
                model_name_color=data.model_name_color,
                model_desc_color=data.model_desc_color,
                theme_color=data.theme_color,
                is_direct_checkout=data.is_direct_checkout,
                is_hacker_mode=data.is_hacker_mode,
                content_json=data.content_json,
                # Campos Mini App V2
                items_per_page=data.items_per_page,
                separator_enabled=data.separator_enabled,
                separator_color=data.separator_color,
                separator_text=data.separator_text,
                separator_btn_text=data.separator_btn_text,
                separator_btn_url=data.separator_btn_url,
                separator_logo_url=data.separator_logo_url,
                model_img_shape=data.model_img_shape,
                # 🔥 CORREÇÃO: SALVANDO AS CORES DO TEXTO DA BARRA E BOTÃO + NEON
                separator_text_color=data.separator_text_color,
                separator_btn_text_color=data.separator_btn_text_color,
                separator_is_neon=data.separator_is_neon,
                separator_neon_color=data.separator_neon_color
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
# 📡 UTILITÁRIO: TESTAR CANAL (NOVO)
# =========================================================
@app.post("/api/admin/utils/test-channel")
def test_channel_connection(data: ChannelTestRequest, current_user: User = Depends(get_current_user)):
    """
    Testa se o bot tem acesso e permissão de admin no canal informado.
    """
    if not data.token or not data.channel_id:
        raise HTTPException(status_code=400, detail="Token e ID do Canal são obrigatórios")

    try:
        # Inicializa o bot temporariamente
        bot = TeleBot(data.token)
        
        # 1. Tenta obter informações do chat
        chat = bot.get_chat(data.channel_id)
        
        # 2. Verifica se é canal ou grupo
        if chat.type not in ['channel', 'group', 'supergroup']:
            return JSONResponse(status_code=400, content={
                "status": "error", 
                "message": "O ID informado não é de um Canal ou Grupo válido."
            })

        # 3. Verifica administradores para saber se o bot tem poder
        try:
            admins = bot.get_chat_administrators(data.channel_id)
            bot_info = bot.get_me()
            is_admin = False
            
            for admin in admins:
                if admin.user.id == bot_info.id:
                    is_admin = True
                    break
            
            if not is_admin:
                return JSONResponse(status_code=400, content={
                    "status": "warning",
                    "message": f"O Bot conecta no '{chat.title}', mas NÃO É ADMIN. Promova-o para gerar links."
                })
                
        except Exception as e:
            # Se falhar ao pegar admins, provavelmente não é admin
            return JSONResponse(status_code=400, content={
                "status": "warning", 
                "message": f"Conectado ao '{chat.title}', mas sem permissão de ver admins (Promova o bot)."
            })

        return {
            "status": "success",
            "message": f"✅ Sucesso! Conectado a: {chat.title}",
            "chat_title": chat.title,
            "chat_type": chat.type
        }

    except Exception as e:
        error_msg = str(e)
        if "Chat not found" in error_msg:
            msg = "Canal não encontrado. Verifique o ID ou se o bot foi adicionado."
        elif "Unauthorized" in error_msg:
            msg = "Token do Bot inválido."
        else:
            msg = f"Erro de conexão: {error_msg}"
            
        raise HTTPException(status_code=400, detail=msg)
# =========================================================
# 💳 WEBHOOK PIX (PUSHIN PAY) - V5.0 COM RETRY
# =========================================================
@app.post("/api/webhooks/pushinpay")
# =========================================================
# 🚀📉 FUNÇÃO AUXILIAR: ENVIAR OFERTA UPSELL/DOWNSELL
# =========================================================
async def enviar_oferta_upsell_downsell(bot_token: str, chat_id: int, bot_id: int, offer_type: str = "upsell"):
    """
    Envia oferta de Upsell ou Downsell para o usuário após delay configurado.
    Chamada via asyncio.create_task com asyncio.sleep para o delay.
    """
    try:
        db_local = SessionLocal()
        
        if offer_type == "upsell":
            config = db_local.query(UpsellConfig).filter(UpsellConfig.bot_id == bot_id).first()
        else:
            config = db_local.query(DownsellConfig).filter(DownsellConfig.bot_id == bot_id).first()
        
        if not config or not config.ativo:
            logger.info(f"ℹ️ {offer_type.upper()} não ativo para bot {bot_id}")
            db_local.close()
            return
        
        # Aguarda o delay configurado
        delay_seconds = (config.delay_minutos or 2) * 60
        logger.info(f"⏰ Aguardando {config.delay_minutos} min para enviar {offer_type.upper()} para {chat_id}")
        await asyncio.sleep(delay_seconds)
        
        tb = telebot.TeleBot(bot_token, threaded=False)
        
        # Monta botões
        mk = types.InlineKeyboardMarkup()
        mk.row(
            types.InlineKeyboardButton(
                f"{config.btn_aceitar} (R$ {config.preco:.2f})", 
                callback_data=f"{offer_type}_accept_{bot_id}"
            )
        )
        mk.row(
            types.InlineKeyboardButton(
                config.btn_recusar, 
                callback_data=f"{offer_type}_decline_{bot_id}"
            )
        )
        
        # Envia mensagem
        msg_texto = config.msg_texto or f"{'🚀' if offer_type == 'upsell' else '🎁'} Oferta especial!"
        
        # ✨ CONVERTE EMOJIS PREMIUM
        msg_texto = convert_premium_emojis(msg_texto)
        
        try:
            # 🔊 COMBO: Se tem audio_url separado, envia áudio primeiro
            _audio_url_up = getattr(config, 'audio_url', None)
            _audio_delay_up = getattr(config, 'audio_delay_seconds', 3) or 3
            
            if _audio_url_up and _audio_url_up.strip():
                # MODO COMBO: Áudio + mídia com legenda e botões
                logger.info(f"🎙️ Modo combo {offer_type}: áudio + mídia para {chat_id}")
                audio_combo_bytes, _, _dur = _download_audio_bytes(_audio_url_up)
                
                # 🔥 CORREÇÃO ASYNC
                _wait = min(max(_dur, 2), 60) if _dur > 0 else 3
                await _async_sleep_with_action(tb, chat_id, _wait, 'record_voice')
                
                if audio_combo_bytes:
                    tb.send_voice(chat_id, audio_combo_bytes)
                else:
                    tb.send_voice(chat_id, _audio_url_up)
                
                await asyncio.sleep(_audio_delay_up)
                
                # Envia mídia + texto + botões
                if config.msg_media:
                    media_url = config.msg_media.strip().lower()
                    if media_url.endswith(('.mp4', '.mov', '.avi')):
                        tb.send_video(chat_id, config.msg_media, caption=msg_texto, reply_markup=mk, parse_mode="HTML")
                    elif media_url.endswith(('.jpg', '.jpeg', '.png', '.gif', '.webp')):
                        tb.send_photo(chat_id, config.msg_media, caption=msg_texto, reply_markup=mk, parse_mode="HTML")
                    else:
                        tb.send_photo(chat_id, config.msg_media, caption=msg_texto, reply_markup=mk, parse_mode="HTML")
                else:
                    tb.send_message(chat_id, msg_texto, reply_markup=mk, parse_mode="HTML")
            
            elif config.msg_media and config.msg_media.lower().endswith(('.ogg', '.mp3', '.wav')):
                # 🔊 ÁUDIO ÚNICO: Baixa e envia como bytes
                try:
                    audio_bytes, _fname, _audio_dur = _download_audio_bytes(config.msg_media)
                    
                    # 🔥 CORREÇÃO ASYNC
                    _wait = min(max(_audio_dur, 2), 60) if _audio_dur > 0 else 3
                    await _async_sleep_with_action(tb, chat_id, _wait, 'record_voice')
                    
                    if audio_bytes:
                        tb.send_voice(chat_id, audio_bytes)
                    else:
                        tb.send_voice(chat_id, config.msg_media)
                    
                    if msg_texto or mk:
                        await asyncio.sleep(2)
                        if msg_texto:
                            tb.send_message(chat_id, msg_texto, reply_markup=mk, parse_mode="HTML")
                        else:
                            tb.send_message(chat_id, "⬇️ Escolha:", reply_markup=mk)
                except Exception as e_audio_up:
                    logger.error(f"❌ Erro áudio upsell/downsell: {e_audio_up}")
                    tb.send_message(chat_id, msg_texto, reply_markup=mk, parse_mode="HTML")
            elif config.msg_media:
                tb.send_photo(chat_id, config.msg_media, caption=msg_texto, reply_markup=mk, parse_mode="HTML")
            else:
                tb.send_message(chat_id, msg_texto, reply_markup=mk, parse_mode="HTML")
            
            logger.info(f"✅ {offer_type.upper()} enviado para {chat_id} (bot {bot_id})")
        except Exception as e_send:
            logger.error(f"❌ Falha ao enviar {offer_type}: {e_send}")
            # Fallback sem mídia
            try:
                tb.send_message(chat_id, msg_texto, reply_markup=mk, parse_mode="HTML")
            except:
                pass
        
        db_local.close()
        
    except asyncio.CancelledError:
        logger.info(f"🚫 {offer_type.upper()} cancelado para {chat_id}")
    except Exception as e:
        logger.error(f"❌ Erro enviar_oferta_upsell_downsell ({offer_type}): {e}", exc_info=True)

# =========================================================
# 💳 WEBHOOK PIX (WIINPAY) - Recebe confirmação de pagamento da WiinPay
# =========================================================
@app.post("/api/webhooks/wiinpay")
async def webhook_wiinpay(request: Request, db: Session = Depends(get_db)):
    """
    Webhook de pagamento da WiinPay.
    A WiinPay envia status "PAID" (maiúsculo).
    O transaction_id vem dentro de metadata.
    """
    print("🔔 WEBHOOK WIINPAY CHEGOU!")
    
    try:
        body_bytes = await request.body()
        body_str = body_bytes.decode("utf-8")
        
        logger.info(f"📩 [WIINPAY WEBHOOK] Payload recebido: {body_str[:500]}")
        
        try:
            data = json.loads(body_str)
            if isinstance(data, list):
                data = data[0]
        except:
            logger.error(f"❌ [WIINPAY WEBHOOK] Payload inválido: {body_str[:200]}")
            return {"status": "ignored"}
        
        # WiinPay retorna status "PAID" (maiúsculo)
        status_pix = str(data.get("status", "")).lower()
        
        if status_pix not in ["paid", "approved", "completed", "succeeded"]:
            logger.info(f"ℹ️ [WIINPAY WEBHOOK] Status ignorado: {data.get('status')}")
            return {"status": "ignored"}
        
        # Buscar transaction_id no metadata (onde guardamos na criação)
        metadata = data.get("metadata", {})
        tx_id_from_metadata = metadata.get("transaction_id") if isinstance(metadata, dict) else None
        
        # Tenta múltiplas formas de encontrar o ID
        raw_tx_id = tx_id_from_metadata or data.get("id") or data.get("payment_id") or data.get("external_reference")
        tx_id = str(raw_tx_id).lower() if raw_tx_id else None
        
        if not tx_id:
            logger.warning(f"⚠️ [WIINPAY WEBHOOK] Sem ID de transação no payload")
            return {"status": "ignored"}
        
        logger.info(f"🔍 [WIINPAY WEBHOOK] Buscando pedido com tx_id: {tx_id}")
        
        # Buscar pedido
        pedido = db.query(Pedido).filter(
            (Pedido.txid == tx_id) | (Pedido.transaction_id == tx_id)
        ).first()
        
        if not pedido:
            logger.warning(f"⚠️ [WIINPAY WEBHOOK] Pedido {tx_id} não encontrado")
            return {"status": "ok", "msg": "Order not found"}
        
        if pedido.status in ["approved", "paid", "active"]:
            logger.info(f"ℹ️ [WIINPAY WEBHOOK] Pedido {tx_id} já aprovado anteriormente")
            return {"status": "ok", "msg": "Already paid"}
        
        # Marca a gateway usada
        pedido.gateway_usada = "wiinpay"
        
        logger.info(f"✅ [WIINPAY WEBHOOK] Pagamento confirmado! Redirecionando para processamento padrão...")
        
        # Redireciona para o webhook_pix genérico passando o payload como se fosse padronizado
        # Fazemos isso criando um request fake com o mesmo formato
        from starlette.requests import Request as StarletteRequest
        from starlette.datastructures import Headers
        
        # Padroniza o payload para o formato que webhook_pix espera
        standardized = {
            "id": tx_id,
            "status": "paid",
            "metadata": metadata
        }
        
        # Atualiza direto no banco (para não depender do redirecionamento)
        # O processamento completo segue no webhook_pix padrão que já existe
        # Mas aqui vamos repassar para ele
        
        db.commit()
        
        # Chama diretamente o webhook_pix com os dados padronizados
        # Criando um mock de Request
        import io
        scope = {
            "type": "http",
            "method": "POST",
            "path": "/webhook/pix",
            "headers": [(b"content-type", b"application/json")],
        }
        body_standardized = json.dumps(standardized).encode("utf-8")
        
        class FakeReceive:
            def __init__(self, body):
                self._body = body
                self._sent = False
            async def __call__(self):
                if not self._sent:
                    self._sent = True
                    return {"type": "http.request", "body": self._body}
                return {"type": "http.disconnect"}
        
        fake_request = Request(scope, receive=FakeReceive(body_standardized))
        
        result = await webhook_pix(fake_request, db)
        logger.info(f"✅ [WIINPAY WEBHOOK] Processamento concluído: {result}")
        return result
        
    except Exception as e:
        logger.error(f"❌ [WIINPAY WEBHOOK] Erro: {e}", exc_info=True)
        # Registra para retry
        try:
            registrar_webhook_para_retry(
                webhook_type='wiinpay',
                payload=body_str if 'body_str' in dir() else "{}",
                reference_id=tx_id if 'tx_id' in dir() else None,
                db=db
            )
        except:
            pass
        return {"status": "error"}

# =========================================================
# 💳 WEBHOOK PIX (UNIFICADO) - V5.0 COM RETRY
# =========================================================
@app.post("/webhook/pix")
async def webhook_pix(request: Request, db: Session = Depends(get_db)):
    """
    Webhook de pagamento unificado (PushinPay, WiinPay, SyncPay)
    com sistema de retry automático e suporte a múltiplos canais VIP.
    """
    print("🔔 WEBHOOK PIX CHEGOU!")
    
    try:
        # 🔥 FIX: Força refresh da conexão do banco para evitar stale connection
        try:
            db.execute(text("SELECT 1"))
        except Exception:
            db.rollback()
        # 1. EXTRAIR PAYLOAD
        body_bytes = await request.body()
        body_str = body_bytes.decode("utf-8")
        
        # 🔥 Captura headers relevantes (Sync Pay envia "event" no header)
        header_event = ""
        try:
            header_event = request.headers.get("event", "") or ""
        except:
            pass
        
        # 🔥 LOG COMPLETO PARA DEBUG
        logger.info(f"📩 [WEBHOOK PIX] Payload recebido: {body_str[:500]}")
        if header_event:
            logger.info(f"📩 [WEBHOOK PIX] Header event: {header_event}")
        
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
        
        # 2. VALIDAR STATUS E ID (Suporta múltiplas Gateways)
        payload_data = data.get("data", data) if isinstance(data.get("data"), dict) else data
        
        # 🔥 Busca abrangente pelo ID do pedido
        raw_tx_id = (
            payload_data.get("id") or 
            payload_data.get("paymentId") or 
            payload_data.get("identifier") or 
            payload_data.get("reference_id") or
            payload_data.get("external_reference") or 
            payload_data.get("transaction_id") or 
            payload_data.get("idtransaction") or
            payload_data.get("uuid") or
            data.get("id") or
            data.get("identifier") or
            data.get("reference_id") or
            data.get("transaction_id")
        )
        tx_id = str(raw_tx_id).lower() if raw_tx_id else None
        
        # 🔥 LEITURA INTELIGENTE DO STATUS
        # Sync Pay: header event pode ser "cashin.create" ou "cashin.update"
        # Sync Pay: body status pode ser "WAITING_FOR_APPROVAL", "completed", "pending"
        # PushinPay/WiinPay: body status é "paid", "approved", etc
        
        inner_status = str(payload_data.get("status") or data.get("status") or "").lower()
        header_evt_lower = header_event.lower().strip()
        
        raw_status = inner_status or header_evt_lower or str(
            data.get("event") or payload_data.get("state") or data.get("state") or ""
        ).lower()
        
        logger.info(f"🔍 [WEBHOOK PIX] TxID: {tx_id} | Inner Status: {inner_status} | Header Event: {header_evt_lower} | Raw: {raw_status}")
        
        # =====================================================================
        # 🔥 SYNC PAY: cashin.create com WAITING_FOR_APPROVAL → IGNORAR
        # Isso significa que o PIX foi gerado mas ainda não pago
        # =====================================================================
        if header_evt_lower == "cashin.create" or inner_status in ["waiting_for_approval", "pending"]:
            # Verifica se NÃO tem indicação de pagamento real
            if inner_status not in ["completed", "paid", "approved"]:
                logger.info(f"ℹ️ [WEBHOOK PIX] Sync Pay criação/pendente (status: {inner_status}). Ignorando - aguardando pagamento.")
                return {"status": "ignored"}
        
        # 🔥 DETERMINAR SE É PAGAMENTO CONFIRMADO
        is_paid = any(s in raw_status for s in ["paid", "approved", "completed", "succeeded", "confirmed", "recebido"])
        
        # Sync Pay cashin.update = pagamento confirmado
        if header_evt_lower == "cashin.update" and inner_status == "completed":
            is_paid = True
            logger.info(f"✅ [WEBHOOK PIX] Sync Pay cashin.update COMPLETED! Processando pagamento.")
        
        if not is_paid:
            logger.info(f"ℹ️ [WEBHOOK PIX] Ignorado. Status não indica pagamento: {raw_status}")
            return {"status": "ignored"}
            
        if not tx_id:
            logger.warning("⚠️ [WEBHOOK PIX] Status é pago, mas não achei ID da transação no payload.")
            return {"status": "ignored"}
        
        # 3. BUSCAR PEDIDO (com fallback robusto para Sync Pay)
        pedido = db.query(Pedido).filter(
            (Pedido.txid == tx_id) | (Pedido.transaction_id == tx_id)
        ).first()
        
        # Fallback: tenta IDs alternativos do payload
        if not pedido:
            alt_ids = set()
            for field in ["id", "identifier", "reference_id", "idtransaction", "transaction_id", "uuid"]:
                val = payload_data.get(field) or data.get(field)
                if val and str(val).lower() != tx_id:
                    alt_ids.add(str(val).lower())
            
            for alt_id in alt_ids:
                pedido = db.query(Pedido).filter(
                    (Pedido.txid == alt_id) | (Pedido.transaction_id == alt_id)
                ).first()
                if pedido:
                    logger.info(f"✅ [WEBHOOK PIX] Pedido encontrado via ID alternativo: {alt_id}")
                    tx_id = alt_id
                    break
        
        if not pedido:
            logger.warning(f"⚠️ Pedido {tx_id} não encontrado")
            return {"status": "ok", "msg": "Order not found"}
        
        if pedido.status in ["approved", "paid", "active"]:
            logger.info(f"ℹ️ [WEBHOOK PIX] Pedido {tx_id} já processado anteriormente.")
            return {"status": "ok", "msg": "Already paid"}
        
        # 4. PROCESSAR PAGAMENTO (LÓGICA CRÍTICA)
        try:
            # 🔥 Detectar e marcar gateway usada (se ainda não marcada)
            if not pedido.gateway_usada:
                if header_evt_lower and "cashin" in header_evt_lower:
                    pedido.gateway_usada = "syncpay"
            
            # Calcular data de expiração
            now = now_brazil()
            data_validade = None
            
            # 🔥 DETECTAR SE É UPSELL/DOWNSELL (não tem plano_id, nome começa com prefixo)
            plano_nome_lower = str(pedido.plano_nome or "").lower()
            is_upsell_or_downsell = "upsell:" in plano_nome_lower or "downsell:" in plano_nome_lower
            
            plano = None
            if pedido.plano_id and not is_upsell_or_downsell:
                try:
                    plano_id_int = int(pedido.plano_id) if str(pedido.plano_id).isdigit() else None
                    if plano_id_int:
                        plano = db.query(PlanoConfig).filter(PlanoConfig.id == plano_id_int).first()
                except (ValueError, TypeError):
                    logger.warning(f"⚠️ plano_id inválido: {pedido.plano_id}")
            
            if is_upsell_or_downsell:
                # Upsell/Downsell: não tem plano associado, sem validade (produto avulso)
                data_validade = None
                logger.info(f"🚀 Pedido é {plano_nome_lower.split(':')[0].upper().strip()} - sem plano associado")
            elif plano:
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
            
            db.commit()
            
            # 📋 AUDITORIA: Venda aprovada
            try:
                log_action(db=db, user_id=None, username="webhook", action="sale_approved", resource_type="pedido", resource_id=pedido.id, description=f"Venda aprovada: {pedido.first_name} - {pedido.plano_nome} - R$ {pedido.valor:.2f}")
            except:
                pass

            # ======================================================================
            # 🔔 [NOVO] GATILHO DE NOTIFICAÇÃO PUSH ONESIGNAL (VENDA APROVADA)
            # ======================================================================
            try:
                if 'enviar_push_onesignal' in globals():
                    await enviar_push_onesignal(
                        bot_id=pedido.bot_id, 
                        nome_cliente=pedido.first_name, 
                        plano=pedido.plano_nome, 
                        valor=pedido.valor, 
                        db=db
                    )
            except Exception as e_push:
                logger.error(f"❌ Erro na chamada do Push: {e_push}")

            # ======================================================================
            # 🔔 NOTIFICAÇÃO NO PAINEL WEB (IN-APP) + ATUALIZAR LEAD
            # ======================================================================
            try:
                bot_notif = db.query(BotModel).filter(BotModel.id == pedido.bot_id).first()
                if bot_notif and bot_notif.owner_id:
                    valor_fmt = f"{pedido.valor:.2f}".replace('.', ',')
                    create_notification(
                        db=db,
                        user_id=bot_notif.owner_id,
                        title="💰 Nova Venda Aprovada!",
                        message=f"{pedido.first_name} comprou {pedido.plano_nome} por R$ {valor_fmt}",
                        type="success"
                    )
            except Exception as e_notif:
                logger.error(f"❌ Erro notificação in-app: {e_notif}")
            
            try:
                lead_update = db.query(Lead).filter(
                    Lead.bot_id == pedido.bot_id,
                    Lead.user_id == str(pedido.telegram_id)
                ).first()
                if lead_update:
                    lead_update.status = 'active'
                    db.commit()
                    logger.info(f"✅ Lead {pedido.telegram_id} atualizado para CLIENTE")
            except Exception as e_lead:
                logger.warning(f"⚠️ Erro ao atualizar Lead: {e_lead}")

            # ✅ CANCELAR REMARKETING (PAGAMENTO CONFIRMADO)
            try:
                chat_id_int = int(pedido.telegram_id) if str(pedido.telegram_id).isdigit() else None
                
                if chat_id_int:
                    # Cancela timers
                    with remarketing_lock:
                        if chat_id_int in remarketing_timers:
                            try:
                                remarketing_timers[chat_id_int].cancel()
                            except:
                                pass
                            del remarketing_timers[chat_id_int]
                        
                        if chat_id_int in alternating_tasks:
                            try:
                                alternating_tasks[chat_id_int].cancel()
                            except:
                                pass
                            del alternating_tasks[chat_id_int]
                    
                    logger.info(f"✅ Remarketing cancelado: {chat_id_int}")
            except Exception as e:
                logger.error(f"⚠️ Erro ao cancelar remarketing: {e}")

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
            
            # 5. ENTREGA DO ACESSO (COM LÓGICA MULTI-CANAIS)
            try:
                bot_data = db.query(BotModel).filter(BotModel.id == pedido.bot_id).first()
                if bot_data:
                    tb = telebot.TeleBot(bot_data.token, threaded=False)
                    target_id = str(pedido.telegram_id).strip()
                    
                    # Corrigir ID se necessário (busca por username se não for numérico)
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
                        # 🔥 UPSELL/DOWNSELL: Pula entrega de canal VIP (será tratado abaixo)
                        if not is_upsell_or_downsell:
                            # Entrega principal (APENAS para planos normais)
                            try:
                                # 🔥 LÓGICA V8: DEFINIÇÃO INTELIGENTE DO CANAL DE DESTINO 🔥
                                canal_id_final = bot_data.id_canal_vip # Default
                                canal_source = "bot_default"
                                
                                if plano and plano.id_canal_destino and str(plano.id_canal_destino).strip() not in ("", "None", "null"):
                                    canal_id_final = plano.id_canal_destino
                                    canal_source = "plano_especifico"
                                    logger.info(f"🎯 [ENTREGA] Usando Canal Específico do Plano '{plano.nome_exibicao}': {canal_id_final}")
                                else:
                                    logger.info(f"🎯 [ENTREGA] Usando Canal Padrão do Bot: {canal_id_final}")
                                    if plano:
                                        logger.warning(f"⚠️ [ENTREGA] Plano '{plano.nome_exibicao}' (ID:{plano.id}) NÃO tem id_canal_destino configurado! Defina um canal específico para cada plano na página de Planos.")
                                
                                logger.info(f"📊 [ENTREGA-DEBUG] pedido.plano_id={pedido.plano_id} | plano={plano.nome_exibicao if plano else 'None'} | plano.id_canal_destino={plano.id_canal_destino if plano else 'N/A'} | canal_final={canal_id_final} | source={canal_source}")

                                # Tratamento do ID do canal (remove traços extras se houver)
                                if str(canal_id_final).replace("-", "").isdigit():
                                    canal_id_final = int(str(canal_id_final).strip())
                                
                                # Tenta desbanir antes (boas práticas)
                                try:
                                    tb.unban_chat_member(canal_id_final, int(target_id))
                                except:
                                    pass
                                
                                # Gera Link Único para o canal decidido acima
                                convite = tb.create_chat_invite_link(
                                    chat_id=canal_id_final,
                                    member_limit=1,
                                    name=f"Venda {pedido.first_name}"
                                )
                                
                                msg_cliente = (
                                    f"✅ <b>Pagamento Confirmado!</b>\n"
                                    f"📅 Validade: <b>{texto_validade}</b>\n\n"
                                    f"Seu acesso exclusivo:\n👉 {convite.invite_link}"
                                )
                                
                                tb.send_message(int(target_id), msg_cliente, parse_mode="HTML")
                                logger.info(f"✅ Entrega enviada para {target_id} (Canal: {canal_id_final})")
                                
                            except Exception as e_main:
                                logger.error(f"❌ Erro na entrega principal (TeleBot): {e_main}")
                                # Fallback: Tenta avisar o usuário que houve erro na geração
                                try:
                                    tb.send_message(int(target_id), "✅ Pagamento recebido!\n⚠️ Erro ao gerar link automático. Contate o suporte.")
                                except: pass
                            
                            # =========================================================
                            # 📦 FASE 2: ENTREGA DE GRUPOS EXTRAS (CATÁLOGO)
                            # =========================================================
                            try:
                                if plano:
                                    grupos_extras = db.query(BotGroup).filter(
                                        BotGroup.bot_id == bot_data.id,
                                        BotGroup.is_active == True
                                    ).all()
                                    
                                    for grupo in grupos_extras:
                                        # Verifica se este plano está vinculado ao grupo
                                        plan_ids = grupo.plan_ids or []
                                        if plano.id in plan_ids:
                                            try:
                                                grupo_canal_id = int(str(grupo.group_id).strip())
                                                
                                                # Desbanir antes
                                                try:
                                                    tb.unban_chat_member(grupo_canal_id, int(target_id))
                                                except:
                                                    pass
                                                
                                                # Gerar convite único
                                                convite_extra = tb.create_chat_invite_link(
                                                    chat_id=grupo_canal_id,
                                                    member_limit=1,
                                                    name=f"Extra {pedido.first_name} - {grupo.title}"
                                                )
                                                
                                                msg_extra = (
                                                    f"🎁 <b>BÔNUS: {grupo.title}</b>\n\n"
                                                    f"👉 Acesse: {convite_extra.invite_link}"
                                                )
                                                tb.send_message(int(target_id), msg_extra, parse_mode="HTML")
                                                logger.info(f"✅ Grupo extra entregue: {grupo.title} para {target_id}")
                                                
                                            except Exception as e_grupo:
                                                logger.error(f"❌ Erro ao entregar grupo extra '{grupo.title}': {e_grupo}")
                            except Exception as e_grupos:
                                logger.error(f"⚠️ Erro geral ao entregar grupos extras: {e_grupos}")
                            
                            # Entrega Order Bump (só para planos normais)
                            if pedido.tem_order_bump:
                                try:
                                    bump_config = db.query(OrderBumpConfig).filter(
                                        OrderBumpConfig.bot_id == bot_data.id
                                    ).first()
                                    
                                    if bump_config:
                                        # ✅ FASE 2: Se tem group_id, gera convite automático
                                        if bump_config.group_id:
                                            try:
                                                grupo_bump = db.query(BotGroup).filter(
                                                    BotGroup.id == bump_config.group_id,
                                                    BotGroup.is_active == True
                                                ).first()
                                                
                                                if grupo_bump:
                                                    bump_canal_id = int(str(grupo_bump.group_id).strip())
                                                    try:
                                                        tb.unban_chat_member(bump_canal_id, int(target_id))
                                                    except:
                                                        pass
                                                    convite_bump = tb.create_chat_invite_link(
                                                        chat_id=bump_canal_id,
                                                        member_limit=1,
                                                        name=f"Bump {pedido.first_name}"
                                                    )
                                                    msg_bump = (
                                                        f"🎁 <b>BÔNUS LIBERADO!</b>\n\n"
                                                        f"👉 <b>{bump_config.nome_produto}</b>\n"
                                                        f"🔗 Acesse: {convite_bump.invite_link}"
                                                    )
                                                    tb.send_message(int(target_id), msg_bump, parse_mode="HTML")
                                                    logger.info("✅ Order Bump entregue (convite automático)")
                                            except Exception as e_bump_auto:
                                                logger.error(f"❌ Erro bump automático: {e_bump_auto}")
                                                # Fallback: usa link_acesso manual
                                                if bump_config.link_acesso:
                                                    msg_bump = (
                                                        f"🎁 <b>BÔNUS LIBERADO!</b>\n\n"
                                                        f"👉 <b>{bump_config.nome_produto}</b>\n"
                                                        f"🔗 {bump_config.link_acesso}"
                                                    )
                                                    tb.send_message(int(target_id), msg_bump, parse_mode="HTML")
                                        
                                        # Sem group_id → usa link_acesso como antes
                                        elif bump_config.link_acesso:
                                            msg_bump = (
                                                f"🎁 <b>BÔNUS LIBERADO!</b>\n\n"
                                                f"👉 <b>{bump_config.nome_produto}</b>\n"
                                                f"🔗 {bump_config.link_acesso}"
                                            )
                                            tb.send_message(int(target_id), msg_bump, parse_mode="HTML")
                                            logger.info("✅ Order Bump entregue (link manual)")
                                except Exception as e_bump:
                                    logger.error(f"❌ Erro Bump: {e_bump}")
                        
                        # Notificar Admin
                        try:
                            # ✅ Buscar código de tracking se existir
                            tracking_info = ""
                            if pedido.tracking_id:
                                try:
                                    tracking_link = db.query(TrackingLink).filter(TrackingLink.id == pedido.tracking_id).first()
                                    if tracking_link and tracking_link.codigo:
                                        tracking_info = f"\n📊 Origem: <b>{tracking_link.codigo}</b>"
                                except:
                                    pass
                            
                            # 🔥 FIX: Se pedido não tem tracking_id, tenta buscar via Lead
                            if not tracking_info and pedido.telegram_id:
                                try:
                                    lead_track = db.query(Lead).filter(
                                        Lead.user_id == str(pedido.telegram_id),
                                        Lead.bot_id == pedido.bot_id,
                                        Lead.tracking_id != None
                                    ).first()
                                    if lead_track and lead_track.tracking_id:
                                        tl_fallback = db.query(TrackingLink).filter(TrackingLink.id == lead_track.tracking_id).first()
                                        if tl_fallback and tl_fallback.codigo:
                                            tracking_info = f"\n📊 Origem: <b>{tl_fallback.codigo}</b> (via lead)"
                                except:
                                    pass
                            
                            msg_admin = (
                                f"💰 <b>VENDA REALIZADA!</b>\n\n"
                                f"🤖 Bot: <b>{bot_data.nome}</b>\n"
                                f"👤 Cliente: {pedido.first_name} (@{pedido.username})\n"
                                f"📦 Plano: {pedido.plano_nome}\n"
                                f"💵 Valor: <b>R$ {pedido.valor:.2f}</b>\n"
                                f"📅 Vence em: {texto_validade}"
                                f"{tracking_info}\n"
                                f"🆔 ID Pagamento: <code>{pedido.transaction_id or pedido.txid or 'N/A'}</code>\n"
                                f"🕐 Data/Hora: {now_brazil().strftime('%d/%m/%Y %H:%M:%S')}"
                            )
                            # Função auxiliar que você já deve ter no código
                            if 'notificar_admin_principal' in globals():
                                notificar_admin_principal(bot_data, msg_admin)
                            elif bot_data.admin_principal_id:
                                tb.send_message(bot_data.admin_principal_id, msg_admin, parse_mode="HTML")

                        except Exception as e_adm:
                            logger.error(f"❌ Erro notificação admin: {e_adm}")
                        
                        pedido.mensagem_enviada = True
                        db.commit()
                        
                        # =========================================================
                        # 🚀📉 AGENDAR UPSELL/DOWNSELL APÓS PAGAMENTO DO PLANO
                        # =========================================================
                        try:
                            plano_nome_lower = str(pedido.plano_nome or "").lower()
                            
                            # Só agenda se for compra de plano PRINCIPAL (não upsell/downsell/order bump)
                            if "upsell:" not in plano_nome_lower and "downsell:" not in plano_nome_lower:
                                # AGENDAR UPSELL
                                upsell_cfg = db.query(UpsellConfig).filter(
                                    UpsellConfig.bot_id == bot_data.id,
                                    UpsellConfig.ativo == True
                                ).first()
                                
                                if upsell_cfg:
                                    logger.info(f"🚀 Agendando UPSELL para {target_id} em {upsell_cfg.delay_minutos} min")
                                    asyncio.create_task(
                                        enviar_oferta_upsell_downsell(
                                            bot_token=bot_data.token,
                                            chat_id=int(target_id),
                                            bot_id=bot_data.id,
                                            offer_type="upsell"
                                        )
                                    )
                            
                            # Se for pagamento de UPSELL → agenda DOWNSELL
                            elif "upsell:" in plano_nome_lower:
                                # Entrega acesso do upsell
                                upsell_cfg = db.query(UpsellConfig).filter(UpsellConfig.bot_id == bot_data.id).first()
                                if upsell_cfg:
                                    # ✅ FASE 2: Se tem group_id, gera convite automático
                                    if upsell_cfg.group_id:
                                        try:
                                            grupo_up = db.query(BotGroup).filter(
                                                BotGroup.id == upsell_cfg.group_id,
                                                BotGroup.is_active == True
                                            ).first()
                                            if grupo_up:
                                                up_canal_id = int(str(grupo_up.group_id).strip())
                                                try:
                                                    tb.unban_chat_member(up_canal_id, int(target_id))
                                                except:
                                                    pass
                                                convite_up = tb.create_chat_invite_link(
                                                    chat_id=up_canal_id,
                                                    member_limit=1,
                                                    name=f"Upsell {pedido.first_name}"
                                                )
                                                msg_upsell_entrega = (
                                                    f"🎉 <b>UPSELL LIBERADO!</b>\n\n"
                                                    f"📦 <b>{upsell_cfg.nome_produto}</b>\n"
                                                    f"🔗 Acesse: {convite_up.invite_link}"
                                                )
                                                tb.send_message(int(target_id), msg_upsell_entrega, parse_mode="HTML")
                                                logger.info(f"✅ Upsell entregue (convite automático) para {target_id}")
                                        except Exception as e_up_auto:
                                            logger.error(f"❌ Erro upsell automático: {e_up_auto}")
                                            # Fallback: link manual
                                            if upsell_cfg.link_acesso:
                                                msg_upsell_entrega = (
                                                    f"🎉 <b>UPSELL LIBERADO!</b>\n\n"
                                                    f"📦 <b>{upsell_cfg.nome_produto}</b>\n"
                                                    f"🔗 Acesse: {upsell_cfg.link_acesso}"
                                                )
                                                tb.send_message(int(target_id), msg_upsell_entrega, parse_mode="HTML")
                                    elif upsell_cfg.link_acesso:
                                        try:
                                            msg_upsell_entrega = (
                                                f"🎉 <b>UPSELL LIBERADO!</b>\n\n"
                                                f"📦 <b>{upsell_cfg.nome_produto}</b>\n"
                                                f"🔗 Acesse: {upsell_cfg.link_acesso}"
                                            )
                                            tb.send_message(int(target_id), msg_upsell_entrega, parse_mode="HTML")
                                            logger.info(f"✅ Upsell entregue (link manual) para {target_id}")
                                        except Exception as e_up:
                                            logger.error(f"❌ Erro entrega upsell: {e_up}")
                                
                                # Agora agenda o DOWNSELL
                                downsell_cfg = db.query(DownsellConfig).filter(
                                    DownsellConfig.bot_id == bot_data.id,
                                    DownsellConfig.ativo == True
                                ).first()
                                
                                if downsell_cfg:
                                    logger.info(f"📉 Agendando DOWNSELL para {target_id} em {downsell_cfg.delay_minutos} min")
                                    asyncio.create_task(
                                        enviar_oferta_upsell_downsell(
                                            bot_token=bot_data.token,
                                            chat_id=int(target_id),
                                            bot_id=bot_data.id,
                                            offer_type="downsell"
                                        )
                                    )
                            
                            # Se for pagamento de DOWNSELL → entrega acesso
                            elif "downsell:" in plano_nome_lower:
                                downsell_cfg = db.query(DownsellConfig).filter(DownsellConfig.bot_id == bot_data.id).first()
                                if downsell_cfg:
                                    # ✅ FASE 2: Se tem group_id, gera convite automático
                                    if downsell_cfg.group_id:
                                        try:
                                            grupo_down = db.query(BotGroup).filter(
                                                BotGroup.id == downsell_cfg.group_id,
                                                BotGroup.is_active == True
                                            ).first()
                                            if grupo_down:
                                                down_canal_id = int(str(grupo_down.group_id).strip())
                                                try:
                                                    tb.unban_chat_member(down_canal_id, int(target_id))
                                                except:
                                                    pass
                                                convite_down = tb.create_chat_invite_link(
                                                    chat_id=down_canal_id,
                                                    member_limit=1,
                                                    name=f"Downsell {pedido.first_name}"
                                                )
                                                msg_down_entrega = (
                                                    f"🎉 <b>ACESSO LIBERADO!</b>\n\n"
                                                    f"📦 <b>{downsell_cfg.nome_produto}</b>\n"
                                                    f"🔗 Acesse: {convite_down.invite_link}"
                                                )
                                                tb.send_message(int(target_id), msg_down_entrega, parse_mode="HTML")
                                                logger.info(f"✅ Downsell entregue (convite automático) para {target_id}")
                                        except Exception as e_down_auto:
                                            logger.error(f"❌ Erro downsell automático: {e_down_auto}")
                                            if downsell_cfg.link_acesso:
                                                msg_down_entrega = (
                                                    f"🎉 <b>ACESSO LIBERADO!</b>\n\n"
                                                    f"📦 <b>{downsell_cfg.nome_produto}</b>\n"
                                                    f"🔗 Acesse: {downsell_cfg.link_acesso}"
                                                )
                                                tb.send_message(int(target_id), msg_down_entrega, parse_mode="HTML")
                                    elif downsell_cfg.link_acesso:
                                        try:
                                            msg_down_entrega = (
                                                f"🎉 <b>ACESSO LIBERADO!</b>\n\n"
                                                f"📦 <b>{downsell_cfg.nome_produto}</b>\n"
                                                f"🔗 Acesse: {downsell_cfg.link_acesso}"
                                            )
                                            tb.send_message(int(target_id), msg_down_entrega, parse_mode="HTML")
                                            logger.info(f"✅ Downsell entregue (link manual) para {target_id}")
                                        except Exception as e_down:
                                            logger.error(f"❌ Erro entrega downsell: {e_down}")
                                            
                        except Exception as e_upsell_schedule:
                            logger.error(f"⚠️ Erro ao agendar upsell/downsell: {e_upsell_schedule}")
                        
            except Exception as e_tg:
                logger.error(f"❌ Erro Telegram/Entrega Geral: {e_tg}")
                # Não falhar o webhook por erro de entrega (o pagamento já foi processado)
            
            # Webhook processado com sucesso
            return {"status": "received"}
            
        except Exception as e_process:
            # ERRO CRÍTICO NO PROCESSAMENTO (BANCO, DADOS, ETC)
            logger.error(f"❌ ERRO no processamento do webhook: {e_process}", exc_info=True)
            
            # Registrar para retry (se a função existir no seu escopo global)
            if 'registrar_webhook_para_retry' in globals():
                registrar_webhook_para_retry(
                    webhook_type='syncpay',
                    payload=data,
                    reference_id=tx_id
                )
            
            # Retornar erro 500 para gateway tentar novamente
            raise HTTPException(status_code=500, detail="Erro interno, será reprocessado")
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"❌ ERRO CRÍTICO NO WEBHOOK: {e}")
        return {"status": "error"}

# =========================================================
# 📤 FUNÇÃO AUXILIAR: ENVIAR OFERTA FINAL (MENSAGEM 2)
# =========================================================
def enviar_oferta_final(bot_temp, chat_id, fluxo, bot_id, db):
    """
    Envia a oferta final (Mensagem 2 / Planos) com suporte a:
    - buttons_config_2 (botões personalizados)
    - Formatação de preços correta
    - Fallback para lógica antiga
    """
    mk = types.InlineKeyboardMarkup()
    
    # 🔥 VERIFICA SE TEM BOTÕES PERSONALIZADOS NA MENSAGEM FINAL
    if fluxo and fluxo.buttons_config_2 and len(fluxo.buttons_config_2) > 0:
        # 🔥 MODO: BOTÕES PERSONALIZADOS (buttons_config_2)
        for btn in fluxo.buttons_config_2:
            btn_type = btn.get('type')
            
            if btn_type == 'plan':
                # 🔥 BOTÃO DE PLANO COM PREÇO FORMATADO
                plan_id = btn.get('plan_id')
                plano = db.query(PlanoConfig).filter(PlanoConfig.id == plan_id).first()
                if plano:
                    # 🔥 FORMATO: "NOME DO PLANO - por R$XX,XX"
                    preco_formatado = f"R${plano.preco_atual:.2f}".replace(".", ",")
                    texto_botao = f"{plano.nome_exibicao} - por {preco_formatado}"
                    mk.add(types.InlineKeyboardButton(
                        texto_botao,
                        callback_data=f"checkout_{plano.id}"
                    ))
            
            elif btn_type == 'link':
                # 🔥 BOTÃO DE LINK (URL)
                mk.add(types.InlineKeyboardButton(
                    btn.get('text', 'Link'),
                    url=btn.get('url')
                ))
    
    else:
        # 🔥 FALLBACK: Lógica antiga (mostrar todos os planos)
        if fluxo and fluxo.mostrar_planos_2:
            planos = db.query(PlanoConfig).filter(PlanoConfig.bot_id == bot_id).all()
            for p in planos:
                preco_formatado = f"R${p.preco_atual:.2f}".replace(".", ",")
                texto_botao = f"{p.nome_exibicao} - por {preco_formatado}"
                mk.add(types.InlineKeyboardButton(
                    texto_botao,
                    callback_data=f"checkout_{p.id}"
                ))
    
    # Texto e mídia da mensagem final
    texto = fluxo.msg_2_texto if (fluxo and fluxo.msg_2_texto) else "Escolha seu plano:"
    media = fluxo.msg_2_media if fluxo else None
    
    # ✨ CONVERTE EMOJIS PREMIUM
    texto = convert_premium_emojis(texto)
    
    # 🔥 ENVIO COM TRATAMENTO DE ERRO
    # 🔥 ENVIO COM TRATAMENTO DE ERRO E ÁUDIO
    try:
        if media:
            media_low = media.lower()
            if media_low.endswith(('.mp4', '.mov', '.avi')): 
                bot_temp.send_video(chat_id, media, caption=texto, reply_markup=mk, parse_mode="HTML")
            elif is_audio_file(media):
                # 🔊 ÁUDIO: Envia sozinho sem caption/markup
                audio_msgs = enviar_audio_inteligente(
                    bot_temp, chat_id, media,
                    texto=texto if texto and texto.strip() else None,
                    markup=mk,
                    delay_pos_audio=2
                )
            else: 
                bot_temp.send_photo(chat_id, media, caption=texto, reply_markup=mk, parse_mode="HTML")
        else:
            bot_temp.send_message(chat_id, texto, reply_markup=mk, parse_mode="HTML")
            
    except Exception as e:
        logger.error(f"❌ Erro ao enviar oferta final: {e}")
        # Fallback sem HTML
        try:
            bot_temp.send_message(chat_id, texto, reply_markup=mk)
        except Exception as e2:
            logger.error(f"❌ Erro no fallback da oferta final: {e2}")

def enviar_passo_automatico(bot_temp, chat_id, passo_atual, bot_db, db):
    """
    Envia um passo e, se não tiver botão e tiver delay, 
    agenda e envia o PRÓXIMO (ou a oferta) automaticamente.
    BLINDADA COM HTML EM TODOS OS CENÁRIOS.
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

        # 2. Envia a mensagem deste passo (AGORA COM HTML ✅)
        # 2. Envia a mensagem deste passo (AGORA COM HTML E ÁUDIO ✅)
        sent_msg = None
        if passo_atual.msg_media:
            try:
                media_low = passo_atual.msg_media.lower()
                if media_low.endswith(('.mp4', '.mov', '.avi')):
                    sent_msg = bot_temp.send_video(
                        chat_id, passo_atual.msg_media, caption=passo_atual.msg_texto, 
                        reply_markup=markup_step if passo_atual.mostrar_botao else None, parse_mode="HTML"
                    )
                elif is_audio_file(passo_atual.msg_media):
                    # 🔊 ÁUDIO: Envia sozinho sem caption/markup
                    audio_msgs = enviar_audio_inteligente(
                        bot_temp, chat_id, passo_atual.msg_media,
                        texto=passo_atual.msg_texto if passo_atual.msg_texto and passo_atual.msg_texto.strip() else None,
                        markup=markup_step if passo_atual.mostrar_botao else None,
                        delay_pos_audio=2
                    )
                    sent_msg = audio_msgs[-1] if audio_msgs else None
                else:
                    sent_msg = bot_temp.send_photo(
                        chat_id, passo_atual.msg_media, caption=passo_atual.msg_texto, 
                        reply_markup=markup_step if passo_atual.mostrar_botao else None, parse_mode="HTML"
                    )
            except Exception as e_media:
                logger.error(f"Erro ao enviar mídia passo {passo_atual.step_order}: {e_media}")
                # Fallback se a mídia falhar: envia texto com HTML
                sent_msg = bot_temp.send_message(
                    chat_id, 
                    passo_atual.msg_texto, 
                    reply_markup=markup_step if passo_atual.mostrar_botao else None,
                    parse_mode="HTML" # 🔥 CORREÇÃO AQUI
                )
        else:
            sent_msg = bot_temp.send_message(
                chat_id, 
                passo_atual.msg_texto, 
                reply_markup=markup_step if passo_atual.mostrar_botao else None,
                parse_mode="HTML" # 🔥 CORREÇÃO AQUI
            )

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
@app.post("/webhook/{token}")
async def receber_update_telegram(token: str, req: Request, db: Session = Depends(get_db)):
    if token == "pix": return {"status": "ignored"}
    
    bot_db = db.query(BotModel).filter(BotModel.token == token).first()
    if not bot_db or bot_db.status == "pausado": return {"status": "ignored"}
    
    # =========================================================
    # 🚨 VERIFICAÇÃO BLINDADA: PUNIÇÕES, BANS E PAUSAS (DENÚNCIAS)
    # =========================================================
    try:
        # Pega o ID do dono com segurança
        dono_id = getattr(bot_db, 'owner_id', None) or getattr(bot_db, 'user_id', None)
        
        if dono_id:
            owner = db.query(User).filter(User.id == dono_id).first()
            if owner:
                # 1. Checa Banimento Permanente
                if getattr(owner, 'is_banned', False):
                    logger.warning(f"🚫 [PUNIÇÃO ATIVA] Bot @{bot_db.username} ignorado. Dono ({owner.username}) está BANIDO.")
                    return {"status": "ignored", "reason": "owner_banned"}
                
                # 2. Checa Pausa Temporária (Pause Bots)
                pause_date = getattr(owner, 'bots_paused_until', None)
                if pause_date:
                    # Garante que a data tem o fuso horário correto para não dar erro na comparação
                    from pytz import timezone
                    tz_br = timezone('America/Sao_Paulo')
                    
                    if pause_date.tzinfo is None:
                        pause_date = tz_br.localize(pause_date)
                    
                    agora = now_brazil()
                    if agora.tzinfo is None:
                        agora = tz_br.localize(agora)
                    
                    if pause_date > agora:
                        logger.warning(f"⏸️ [PUNIÇÃO ATIVA] Bot @{bot_db.username} ignorado. Bots de ({owner.username}) PAUSADOS até {pause_date.strftime('%d/%m/%Y %H:%M')}.")
                        return {"status": "ignored", "reason": "bots_paused"}
    except Exception as e:
        logger.error(f"❌ Erro ao checar punição do bot no webhook: {e}")

    # 🔒 Flag de proteção de conteúdo — aplicada em todos os envios de mídia/mensagem do vendedor
    _protect = getattr(bot_db, 'protect_content', False) or False

    try:
        body = await req.json()
        update = telebot.types.Update.de_json(body)
        # 🔥 FIX: threaded=False obriga o envio a acontecer AGORA, sem criar thread paralela
        bot_temp = telebot.TeleBot(token, threaded=False)
        message = update.message if update.message else None
        
        # ========================================
        # 🆓 HANDLER: SOLICITAÇÃO DE ENTRADA NO CANAL FREE
        # ========================================
        if update.chat_join_request:
            try:
                join_request = update.chat_join_request
                canal_id = str(join_request.chat.id)
                user_id = join_request.from_user.id
                user_name = join_request.from_user.first_name if join_request.from_user.first_name else ""
                username = join_request.from_user.username
                
                logger.info(f"🆓 [CANAL FREE] Solicitação de entrada - User: {user_name} ({user_id}), Canal: {canal_id}")
                
                # 🔥 FIX: DEDUPLICAÇÃO - Se já existe um job agendado para este usuário/canal, ignora
                job_id = f"approve_free_{canal_id}_{user_id}"
                existing_job = None
                try:
                    existing_job = scheduler.get_job(job_id)
                except:
                    pass
                
                if existing_job:
                    logger.info(f"ℹ️ [CANAL FREE] Aprovação já agendada para {user_name} ({user_id}), ignorando duplicata")
                    return {"status": "ok", "message": "Já agendado"}
                
                # Buscar configuração do Canal Free
                config = db.query(CanalFreeConfig).filter(
                    CanalFreeConfig.bot_id == bot_db.id,
                    CanalFreeConfig.canal_id == canal_id,
                    CanalFreeConfig.is_active == True
                ).first()
                
                if not config:
                    logger.warning(f"⚠️ [CANAL FREE] Canal {canal_id} não configurado para bot {bot_db.id}")
                    return {"status": "ok", "message": "Canal não configurado"}
                
                # -----------------------------------------------------------
                # 🔥 CORREÇÃO: SUBSTITUIÇÃO DE VARIÁVEIS NA MENSAGEM
                # -----------------------------------------------------------
                final_message = config.message_text or ""
                
                # 1. Substitui {first_name}
                final_message = final_message.replace("{first_name}", user_name)
                
                # 2. Substitui {username} (adiciona @ se existir, senão fica vazio)
                str_user = f"@{username}" if username else ""
                final_message = final_message.replace("{username}", str_user)
                
                # 3. Substitui {id}
                final_message = final_message.replace("{id}", str(user_id))
                # -----------------------------------------------------------
                
                # ✨ CONVERTE EMOJIS PREMIUM
                final_message = convert_premium_emojis(final_message)

                # Enviar mensagem de boas-vindas
                try:
                    markup = None
                    
                    # Montar botões se configurados
                    if config.buttons and len(config.buttons) > 0:
                        markup = types.InlineKeyboardMarkup()
                        for btn in config.buttons:
                            if btn.get('text') and btn.get('url'):
                                markup.add(types.InlineKeyboardButton(
                                    text=btn['text'],
                                    url=btn['url']
                                ))
                    
                    # 🔥 LÓGICA DE MÍDIA ATUALIZADA (COMBO ÁUDIO + MÍDIA)
                    _audio_url_cf = getattr(config, 'audio_url', None)
                    _audio_delay_cf = getattr(config, 'audio_delay_seconds', 3) or 3
                    
                    if _audio_url_cf and _audio_url_cf.strip():
                        # MODO COMBO: Áudio separado + mídia com legenda e botões
                        logger.info(f"🎙️ [CANAL FREE] Modo combo: áudio + mídia para {user_id}")
                        audio_combo_cf, _, _dur_cf = _download_audio_bytes(_audio_url_cf)
                        
                        # 🔥 CORREÇÃO (Sync)
                        _wt_cf = min(max(_dur_cf, 2), 60) if _dur_cf > 0 else 3
                        _sleep_with_action(bot_temp, user_id, _wt_cf, 'record_voice')
                        
                        if audio_combo_cf:
                            bot_temp.send_voice(user_id, audio_combo_cf)
                        else:
                            bot_temp.send_voice(user_id, _audio_url_cf)
                        
                        time.sleep(_audio_delay_cf)
                        
                        if config.media_url and config.media_type in ('photo', 'video'):
                            if config.media_type == 'video':
                                bot_temp.send_video(user_id, config.media_url, caption=final_message, reply_markup=markup, parse_mode="HTML")
                            else:
                                bot_temp.send_photo(user_id, config.media_url, caption=final_message, reply_markup=markup, parse_mode="HTML")
                        else:
                            bot_temp.send_message(user_id, final_message or "⬇️ Escolha:", reply_markup=markup, parse_mode="HTML")
                    
                    elif config.media_url:
                        media_low = config.media_url.lower()
                        if config.media_type == 'video' or media_low.endswith(('.mp4', '.mov', '.avi')):
                            bot_temp.send_video(
                                user_id,
                                config.media_url,
                                caption=final_message,
                                reply_markup=markup,
                                parse_mode="HTML"
                            )
                        elif config.media_type == 'audio' or media_low.endswith(('.ogg', '.mp3', '.wav')):
                            # 🔊 ÁUDIO ÚNICO: Baixa e envia como bytes
                            audio_bytes_cf, _fname_cf, _audio_dur_cf = _download_audio_bytes(config.media_url)
                            
                            # 🔥 CORREÇÃO (Sync)
                            _wait_cf = min(max(_audio_dur_cf, 2), 60) if _audio_dur_cf > 0 else 3
                            _sleep_with_action(bot_temp, user_id, _wait_cf, 'record_voice')
                            
                            if audio_bytes_cf:
                                bot_temp.send_voice(user_id, audio_bytes_cf)
                            else:
                                bot_temp.send_voice(user_id, config.media_url)
                            if final_message or markup:
                                time.sleep(2)
                                bot_temp.send_message(
                                    user_id,
                                    final_message or "⬇️ Escolha:",
                                    reply_markup=markup,
                                    parse_mode="HTML"
                                )
                        else:  # photo ou padrão
                            bot_temp.send_photo(
                                user_id,
                                config.media_url,
                                caption=final_message,
                                reply_markup=markup,
                                parse_mode="HTML"
                            )
                    else:
                        bot_temp.send_message(
                            user_id,
                            final_message,
                            reply_markup=markup,
                            parse_mode="HTML"
                        )
                    
                    logger.info(f"✅ [CANAL FREE] Mensagem enviada para {user_name}")
                    
                except Exception as e_msg:
                    logger.error(f"❌ [CANAL FREE] Erro ao enviar mensagem: {e_msg}")
                
                # Salvar lead se não existir
                try:
                    lead_existente = db.query(Lead).filter(
                        Lead.user_id == str(user_id),
                        Lead.bot_id == bot_db.id
                    ).first()
                    
                    if not lead_existente:
                        lead = Lead(
                            user_id=str(user_id),
                            nome=user_name,
                            username=username,
                            bot_id=bot_db.id,
                            status='topo',
                            funil_stage='lead_frio',
                            origem_entrada='canal_free'  # 🔥 NOVO: Marca origem
                        )
                        db.add(lead)
                        db.commit()
                        logger.info(f"✅ [CANAL FREE] Lead salvo: {user_name}")
                    else:
                        # 🔥 Se lead já existe mas não tem origem marcada, atualiza
                        if not lead_existente.origem_entrada or lead_existente.origem_entrada == 'bot_direto':
                            lead_existente.origem_entrada = 'canal_free'
                            db.commit()
                    
                except Exception as e_lead:
                    logger.error(f"❌ [CANAL FREE] Erro ao salvar lead: {e_lead}")
                    db.rollback()
                
                # Agendar aprovação automática
                try:
                    run_date = now_brazil() + timedelta(seconds=config.delay_seconds)
                    
                    scheduler.add_job(
                        aprovar_entrada_canal_free,
                        'date',
                        run_date=run_date,
                        args=[token, canal_id, user_id],
                        id=f"approve_free_{canal_id}_{user_id}",
                        replace_existing=True
                    )
                    
                    logger.info(f"⏰ [CANAL FREE] Aprovação agendada para {config.delay_seconds}s - User: {user_name}")
                    
                except Exception as e_schedule:
                    logger.error(f"❌ [CANAL FREE] Erro ao agendar aprovação: {e_schedule}")
                
                return {"status": "ok", "message": "Canal Free processado"}
                
            except Exception as e_free:
                logger.error(f"❌ [CANAL FREE] Erro geral: {e_free}")
                return {"status": "error", "message": str(e_free)}
        
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
                            if now_brazil() < pedido.data_expiracao: allowed = True
                        elif pedido.plano_nome:
                            nm = pedido.plano_nome.lower()
                            if "vital" in nm or "mega" in nm or "eterno" in nm: allowed = True
                            else:
                                d = 30
                                if "diario" in nm or "24" in nm: d = 1
                                elif "semanal" in nm: d = 7
                                elif "trimestral" in nm: d = 90
                                elif "anual" in nm: d = 365
                                if pedido.created_at and now_brazil() < (pedido.created_at + timedelta(days=d)): allowed = True
                    
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
                        if now_brazil() > pedido.data_expiracao:
                            bot_temp.send_message(chat_id, "❌ <b>Assinatura expirada!</b>", parse_mode="HTML")
                            return {"status": "ok"}
                        validade = pedido.data_expiracao.strftime("%d/%m/%Y")
                    bot_temp.send_message(chat_id, f"✅ <b>Assinatura Ativa!</b>\n\n💎 Plano: {pedido.plano_nome}\n📅 Vence em: {validade}", parse_mode="HTML")
                else: bot_temp.send_message(chat_id, "❌ <b>Nenhuma assinatura ativa.</b>", parse_mode="HTML")
                return {"status": "ok"}

            # --- /DENUNCIAR ---
            if txt == "/denunciar":
                msg_denuncia = (
                    "🚨 <b>Canal de Denúncias</b>\n\n"
                    "Se você identificou conteúdo ilegal ou abusivo em algum bot desta plataforma, "
                    "você pode fazer uma denúncia de forma <b>segura e anônima</b>.\n\n"
                    "⚠️ <b>Seu nome e dados NÃO serão expostos ao denunciado.</b>\n\n"
                    "👉 Acesse o portal de denúncias:\n"
                    "🔗 https://www.zenyxvips.com/denunciar\n\n"
                    "📋 <b>Como denunciar:</b>\n"
                    "1. Acesse o link acima\n"
                    "2. Informe o @username do bot\n"
                    "3. Selecione o motivo\n"
                    "4. Descreva o ocorrido\n"
                    "5. Envie a denúncia\n\n"
                    "⚠️ <b>Importante sobre reembolsos:</b>\n"
                    "Questões relacionadas a reembolso <b>não</b> são tratadas por este canal, pois não temos "
                    "controle sobre as transações financeiras entre compradores e vendedores. "
                    "Para solicitar um reembolso, entre em contato diretamente com o dono do bot "
                    "ou com a sua instituição financeira (banco/app de pagamento). "
                    "Sua instituição poderá intermediar a solicitação junto ao meio de pagamento utilizado.\n\n"
                    "✅ Todas as denúncias são analisadas pela equipe de segurança."
                )
                bot_temp.send_message(chat_id, msg_denuncia, parse_mode="HTML", disable_web_page_preview=True)
                return {"status": "ok"}

            # --- /START ---
            if txt == "/start" or txt.startswith("/start "):
                
                # 🔥 SOLUÇÃO PARA BOTS EXISTENTES: Atualiza o menu silenciosamente em background 
                # na primeira vez que alguém dá /start no bot, aplicando o comando /denunciar
                try:
                    threading.Thread(target=configurar_menu_bot, args=(token,)).start()
                except:
                    pass

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
                tl = None
                parts = txt.split()
                if len(parts) > 1:
                    code = parts[1]
                    tl = db.query(TrackingLink).filter(TrackingLink.codigo == code).first()
                    if tl: 
                        # 🔥 CORREÇÃO: Não contabiliza clique no /start para links de remarketing (rmkt_)
                        # O clique de remarketing é contabilizado quando o usuário clica no botão da oferta
                        if not code.startswith("rmkt_"):
                            tl.clicks = (tl.clicks or 0) + 1
                        track_id = tl.id
                        db.commit()

                # Lead
                try:
                    lead = db.query(Lead).filter(Lead.user_id == user_id_str, Lead.bot_id == bot_db.id).first()
                    if not lead:
                        lead = Lead(user_id=user_id_str, nome=first_name, username=username_raw, bot_id=bot_db.id, tracking_id=track_id)
                        db.add(lead)
                        
                        # 🔥 CORREÇÃO MESTRE: Contabilizar o Lead gerado na tabela de Rastreamento!
                        if tl and track_id:
                            tl.leads = (tl.leads or 0) + 1
                            
                    db.commit()
                except: pass

                # Envio Menu
                flow = db.query(BotFlow).filter(BotFlow.bot_id == bot_db.id).first()
                modo = getattr(flow, 'start_mode', 'padrao') if flow else 'padrao'
                msg_txt = flow.msg_boas_vindas if flow else "Olá!"
                media = flow.media_url if flow else None
                
                # ✨ CONVERTE SHORTCODES DE EMOJIS PREMIUM → TAGS HTML DO TELEGRAM
                msg_txt = convert_premium_emojis(msg_txt, db)
                
                mk = types.InlineKeyboardMarkup()
                
                # SE FOR MINI APP
                if modo == "miniapp" and flow and flow.miniapp_url:
                    url = flow.miniapp_url.replace("http://", "https://")
                    mk.add(types.InlineKeyboardButton(text=flow.miniapp_btn_text or "ABRIR LOJA 🛍️", web_app=types.WebAppInfo(url=url)))
                
                # SE FOR PADRÃO
                else:
                    # 🔥 VERIFICA O MODO DE BOTÃO DA MENSAGEM 1
                    button_mode = getattr(flow, 'button_mode', 'next_step') if flow else 'next_step'
                    
                    if button_mode == "custom" and flow and flow.buttons_config and len(flow.buttons_config) > 0:
                        # 🔥 MODO: BOTÕES PERSONALIZADOS (NOVA LÓGICA CORRIGIDA)
                        for btn in flow.buttons_config:
                            btn_type = btn.get('type')
                            
                            if btn_type == 'plan':
                                # 🔥 BOTÃO DE PLANO - Busca nome e preço do banco
                                plan_id = btn.get('plan_id')
                                plano = db.query(PlanoConfig).filter(
                                    PlanoConfig.id == plan_id,
                                    PlanoConfig.bot_id == bot_db.id
                                ).first()
                                
                                if plano:
                                    # 🔥 FORMATO CORRETO: "NOME DO PLANO - por R$XX,XX"
                                    preco_formatado = f"R${plano.preco_atual:.2f}".replace(".", ",")
                                    texto_botao = f"{plano.nome_exibicao} - por {preco_formatado}"
                                    
                                    mk.add(types.InlineKeyboardButton(
                                        texto_botao, 
                                        callback_data=f"checkout_{plano.id}"
                                    ))
                            
                            elif btn_type == 'link':
                                # 🔥 BOTÃO DE LINK - Usa texto personalizado do usuário
                                texto_link = btn.get('text', 'Link')
                                url_link = btn.get('url', '')
                                
                                if url_link:
                                    mk.add(types.InlineKeyboardButton(
                                        texto_link, 
                                        url=url_link
                                    ))
                    
                    else:
                        # 🔥 MODO: BOTÃO "PRÓXIMO PASSO" (LÓGICA TRADICIONAL)
                        if flow and flow.mostrar_planos_1:
                            # Mostra planos já na primeira mensagem
                            planos = db.query(PlanoConfig).filter(PlanoConfig.bot_id == bot_db.id).all()
                            for pl in planos: 
                                preco_formatado = f"R${pl.preco_atual:.2f}".replace(".", ",")
                                texto_botao = f"{pl.nome_exibicao} - por {preco_formatado}"
                                mk.add(types.InlineKeyboardButton(texto_botao, callback_data=f"checkout_{pl.id}"))
                        else:
                            # Mostra apenas botão de próximo passo
                            mk.add(types.InlineKeyboardButton(
                                flow.btn_text_1 if flow else "Ver Conteúdo", 
                                callback_data="step_1"
                            ))

                # 🔥 BLOCO DE ENVIO COM LOG E ÁUDIO
                try:
                    logger.info(f"📤 Tentando enviar menu para {chat_id}...")
                    sent_msg_start = None
                    
                    if media:
                        media_low = media.lower()
                        if media_low.endswith(('.mp4', '.mov', '.avi')): 
                            sent_msg_start = bot_temp.send_video(chat_id, media, caption=msg_txt, reply_markup=mk, parse_mode="HTML", protect_content=_protect)
                        elif media_low.endswith(('.ogg', '.mp3', '.wav')):
                            # 🔊 ÁUDIO: Envia sozinho sem caption/markup (senão vira arquivo)
                            audio_msgs = enviar_audio_inteligente(
                                bot_temp, chat_id, media,
                                texto=msg_txt if msg_txt.strip() else None,
                                markup=mk,
                                protect_content=_protect,
                                delay_pos_audio=2
                            )
                            sent_msg_start = audio_msgs[-1] if audio_msgs else None
                        else: 
                            sent_msg_start = bot_temp.send_photo(chat_id, media, caption=msg_txt, reply_markup=mk, parse_mode="HTML", protect_content=_protect)
                    else: 
                        sent_msg_start = bot_temp.send_message(chat_id, msg_txt, reply_markup=mk, parse_mode="HTML", protect_content=_protect)
                    
                    logger.info("✅ Menu enviado com sucesso!")

                    # 🔥 AUTO-DESTRUIÇÃO REMOVIDA - Agora só deleta ao clicar no botão
                    # if sent_msg_start and flow and flow.autodestruir_1:
                    #     agendar_destruicao_msg(bot_temp, chat_id, sent_msg_start.message_id, 5)

                except ApiTelegramException as e_envio:
                    err_str = str(e_envio).lower()
                    if "blocked" in err_str or "403" in err_str:
                        logger.info(f"🚫 Usuário {chat_id} bloqueou o bot no /start")
                    else:
                        logger.error(f"❌ ERRO AO ENVIAR MENSAGEM START: {e_envio}")
                        try: bot_temp.send_message(chat_id, msg_txt, reply_markup=mk)
                        except: pass
                except Exception as e_envio:
                    logger.error(f"❌ ERRO AO ENVIAR MENSAGEM START: {e_envio}")
                    try: bot_temp.send_message(chat_id, msg_txt, reply_markup=mk)
                    except: pass

                return {"status": "ok"}

        # ----------------------------------------
        # 🎮 3. CALLBACKS (BOTÕES) - ORDEM CORRIGIDA
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

            # --- 🔄 VERIFICAR PAGAMENTO (check_payment_) ---
            if data.startswith("check_payment_"):
                try:
                    txid = data.replace("check_payment_", "").strip()
                    
                    if not txid:
                        try:
                            bot_temp.answer_callback_query(
                                update.callback_query.id,
                                text="❌ Código de transação inválido.",
                                show_alert=True
                            )
                        except Exception as e_net:
                            logger.warning(f"⚠️ Aviso de rede ignorado: {e_net}")
                        return {"status": "ok"}
                    
                    # Busca o pedido no banco
                    pedido = db.query(Pedido).filter(
                        (Pedido.transaction_id == txid) | (Pedido.txid == txid)
                    ).first()
                    
                    if not pedido:
                        try:
                            bot_temp.answer_callback_query(
                                update.callback_query.id,
                                text="❌ Transação não encontrada. Tente novamente mais tarde.",
                                show_alert=True
                            )
                        except Exception as e_net:
                            logger.warning(f"⚠️ Aviso de rede ignorado: {e_net}")
                        return {"status": "ok"}
                    
                    status = str(pedido.status).lower() if pedido.status else "pending"
                    
                    if status in ['paid', 'active', 'approved', 'completed', 'succeeded']:
                        # ✅ Pagamento confirmado
                        try:
                            bot_temp.answer_callback_query(
                                update.callback_query.id,
                                text="✅ Pagamento CONFIRMADO! Seu acesso está sendo liberado.",
                                show_alert=True
                            )
                        except Exception as e_net:
                            logger.warning(f"⚠️ Aviso de rede ignorado: {e_net}")
                    elif status == 'expired':
                        # ⏰ PIX expirado
                        try:
                            bot_temp.answer_callback_query(
                                update.callback_query.id,
                                text="⏰ Este PIX expirou! Por favor, gere um novo pagamento.",
                                show_alert=True
                            )
                        except Exception as e_net:
                            logger.warning(f"⚠️ Aviso de rede ignorado: {e_net}")
                    else:
                        # ⏳ Ainda pendente
                        try:
                            bot_temp.answer_callback_query(
                                update.callback_query.id,
                                text="⏳ Pagamento ainda NÃO identificado.\n\nSe você já pagou, aguarde alguns instantes e tente novamente. O sistema verifica automaticamente.",
                                show_alert=True
                            )
                        except Exception as e_net:
                            logger.warning(f"⚠️ Aviso de rede ignorado: {e_net}")
                    
                except Exception as e:
                    logger.error(f"❌ Erro no handler check_payment_: {e}", exc_info=True)
                    try:
                        bot_temp.answer_callback_query(
                            update.callback_query.id,
                            text="❌ Erro ao verificar pagamento. Tente novamente.",
                            show_alert=True
                        )
                    except: pass
                
                return {"status": "ok"}

            # --- A) NAVEGAÇÃO (step_) COM AUTO-DESTRUIÇÃO INTELIGENTE ---
            elif data.startswith("step_"):
                try: current_step = int(data.split("_")[1])
                except: current_step = 1
                
                # Carrega todos os passos
                steps = db.query(BotFlowStep).filter(BotFlowStep.bot_id == bot_db.id).order_by(BotFlowStep.step_order).all()
                target_step = None
                is_last = False
                
                # ==============================================================================
                # 🗑️ GUILHOTINA: LÓGICA DE LIMPEZA IMEDIATA AO CLICAR
                # ==============================================================================
                msg_anterior_id = update.callback_query.message.message_id
                
                # CASO 1: O usuário clicou no botão da MENSAGEM DE BOAS VINDAS (indo para o passo 1)
                if current_step == 1:
                    # A mensagem de boas-vindas agora SÓ é deletada quando o usuário clica (sem timer paralelo)
                    # Isso resolve o bug onde a mensagem sumia antes do usuário clicar, parando o fluxo
                    try: 
                        bot_temp.delete_message(chat_id, msg_anterior_id)
                        logger.info(f"🗑️ Mensagem de Boas-Vindas deletada IMEDIATAMENTE após clique.")
                    except Exception as e: 
                        # Falha silenciosa caso a mensagem já tenha sido deletada manualmente
                        pass

                # CASO 2: O usuário clicou em um PASSO DO FLUXO (indo para 2, 3...)
                elif current_step > 1:
                    # Verifica se o passo ANTERIOR (que o usuário acabou de ler) tinha autodestruir ligado
                    # Matematica: Se vou pro passo 2 (index 1), o anterior foi o passo 1 (index 0).
                    # Index do anterior = current_step - 2
                    prev_index = current_step - 2
                    
                    if prev_index >= 0 and prev_index < len(steps):
                        passo_anterior = steps[prev_index]
                        if passo_anterior.autodestruir:
                            try:
                                bot_temp.delete_message(chat_id, msg_anterior_id)
                                logger.info(f"🗑️ Passo {passo_anterior.step_order} deletado IMEDIATAMENTE após clique.")
                            except: pass
                # ==============================================================================

                # Define qual é o PRÓXIMO passo a ser enviado
                if current_step <= len(steps): target_step = steps[current_step - 1]
                else: is_last = True

                if target_step and not is_last:
                    mk = types.InlineKeyboardMarkup()
                    if target_step.mostrar_botao:
                        mk.add(types.InlineKeyboardButton(target_step.btn_texto or "Próximo ▶️", callback_data=f"step_{current_step + 1}"))
                    
                    # ✨ CONVERTE EMOJIS PREMIUM no texto do step
                    _step_txt = convert_premium_emojis(target_step.msg_texto) if target_step.msg_texto else target_step.msg_texto
                    
                    sent_msg = None
                    try:
                        if target_step.msg_media:
                            media_step_low = target_step.msg_media.lower()
                            if media_step_low.endswith(('.mp4', '.mov', '.avi')):
                                sent_msg = bot_temp.send_video(chat_id, target_step.msg_media, caption=_step_txt, reply_markup=mk, parse_mode="HTML", protect_content=_protect)
                            elif is_audio_file(target_step.msg_media):
                                # 🔊 ÁUDIO: Envia sozinho sem caption/markup
                                audio_msgs = enviar_audio_inteligente(
                                    bot_temp, chat_id, target_step.msg_media,
                                    texto=_step_txt if _step_txt and _step_txt.strip() else None,
                                    markup=mk if target_step.mostrar_botao else None,
                                    protect_content=_protect,
                                    delay_pos_audio=2
                                )
                                sent_msg = audio_msgs[-1] if audio_msgs else None
                            else:
                                sent_msg = bot_temp.send_photo(chat_id, target_step.msg_media, caption=_step_txt, reply_markup=mk, parse_mode="HTML", protect_content=_protect)
                        else:
                            sent_msg = bot_temp.send_message(chat_id, _step_txt, reply_markup=mk, parse_mode="HTML", protect_content=_protect)
                    except:
                        # Fallback caso falhe HTML ou Mídia
                        sent_msg = bot_temp.send_message(chat_id, strip_premium_emoji_tags(_step_txt) or "...", reply_markup=mk, protect_content=_protect)

                    # 🔥 AUTO-DESTRUIÇÃO REMOVIDA - Agora só deleta ao clicar no botão
                    # if sent_msg and target_step.autodestruir:
                    #     tempo = target_step.delay_seconds if target_step.delay_seconds > 0 else 20
                    #     agendar_destruicao_msg(bot_temp, chat_id, sent_msg.message_id, tempo)

                    # Lógica de Navegação Automática (Recursividade para passos SEM botão)
                    if not target_step.mostrar_botao:
                        # Se não tem botão, usamos o delay para ditar o ritmo
                        delay = target_step.delay_seconds if target_step.delay_seconds > 0 else 0
                        if delay > 0: time.sleep(delay)
                        
                        # Chama o próximo
                        prox = db.query(BotFlowStep).filter(BotFlowStep.bot_id == bot_db.id, BotFlowStep.step_order == target_step.step_order + 1).first()
                        if prox: enviar_passo_automatico(bot_temp, chat_id, prox, bot_db, db)
                        else: enviar_oferta_final(bot_temp, chat_id, bot_db.fluxo, bot_db.id, db)
                else:
                    enviar_oferta_final(bot_temp, chat_id, bot_db.fluxo, bot_db.id, db)

            # 🔥 CORREÇÃO: CHECKOUT PROMO VEM ANTES DO CHECKOUT NORMAL!
            # --- B1) CHECKOUT PROMOCIONAL (REMARKETING & DISPAROS) ---
            elif data.startswith("checkout_promo_"):
                # 🔥 FIX CRÍTICO: Cancela timers antigos de remarketing
                try: cancelar_remarketing(int(chat_id))
                except: pass

                # ==============================================================================
                # 💣 CORREÇÃO MESTRE: AUTO-DESTRUIÇÃO AO CLICAR (Agora no handler correto!)
                # ==============================================================================
                try:
                    # Verifica se existe o dicionário de destruições pendentes
                    if hasattr(enviar_remarketing_automatico, 'pending_destructions'):
                        dict_pendente = enviar_remarketing_automatico.pending_destructions
                        
                        # Procura o agendamento (Tenta chave INT e STR para garantir)
                        dados_destruicao = dict_pendente.get(chat_id) or dict_pendente.get(str(chat_id))
                        
                        if dados_destruicao:
                            logger.info(f"💣 [CHECKOUT] Encontrado agendamento de destruição para {chat_id}")
                            
                            msg_id_to_del = dados_destruicao.get('message_id')
                            btns_id_to_del = dados_destruicao.get('buttons_message_id')
                            # Tempo de segurança para o usuário ver que clicou (ex: 2s) ou o configurado
                            tempo_para_explodir = dados_destruicao.get('destruct_seconds', 3)
                            
                            def auto_delete_task():
                                time.sleep(tempo_para_explodir)
                                try:
                                    bot_temp.delete_message(chat_id, msg_id_to_del)
                                    if btns_id_to_del:
                                        bot_temp.delete_message(chat_id, btns_id_to_del)
                                    logger.info(f"🗑️ Mensagem destruída APÓS clique no Checkout ({chat_id})")
                                except Exception as e:
                                    logger.warning(f"⚠️ Falha ao deletar msg (já deletada?): {e}")

                            # Inicia via pool (evita can't start new thread)
                            try:
                                thread_pool.submit(auto_delete_task)
                            except RuntimeError:
                                pass
                            
                            # Limpa do dicionário para não tentar deletar de novo
                            if chat_id in dict_pendente: del dict_pendente[chat_id]
                            if str(chat_id) in dict_pendente: del dict_pendente[str(chat_id)]
                except Exception as e_destruct:
                    logger.error(f"⚠️ Erro não fatal na lógica de destruição: {e_destruct}")
                # ==============================================================================

                try:
                    parts = data.split("_")
                    # Formato: checkout_promo_{plano_id}_{preco_centavos}
                    if len(parts) < 4:
                        bot_temp.send_message(chat_id, "❌ Link de oferta inválido.")
                        return {"status": "error"}

                    plano_id = int(parts[2])
                    preco_centavos = int(parts[3])
                    preco_promo = preco_centavos / 100.0
                    
                    plano = db.query(PlanoConfig).filter(PlanoConfig.id == plano_id).first()
                    if not plano:
                        bot_temp.send_message(chat_id, "❌ Plano não encontrado.")
                        return {"status": "error"}
                    
                    lead_origem = db.query(Lead).filter(Lead.user_id == str(chat_id), Lead.bot_id == bot_db.id).first()
                    # 🔥 FIX: Busca tracking do remarketing automático (não do link original do lead)
                    track_id_pedido = None
                    try:
                        _rmkt_track = db.query(TrackingLink).filter(
                            TrackingLink.bot_id == bot_db.id,
                            TrackingLink.origem == 'remarketing'
                        ).order_by(TrackingLink.created_at.desc()).first()
                        if _rmkt_track:
                            track_id_pedido = _rmkt_track.id
                            logger.info(f"📊 [CHECKOUT-PROMO] Tracking #{_rmkt_track.id} vinculado ao pedido de {chat_id}")
                    except Exception as e_track:
                        logger.warning(f"⚠️ Erro ao buscar tracking checkout promo: {e_track}")
                    
                    # Calcula desconto visual
                    desconto_percentual = 0
                    if plano.preco_atual > preco_promo:
                        desconto_percentual = int(((plano.preco_atual - preco_promo) / plano.preco_atual) * 100)
                    
                    msg_wait = bot_temp.send_message(
                        chat_id, 
                        f"⏳ Gerando <b>OFERTA ESPECIAL</b>{f' com {desconto_percentual}% OFF' if desconto_percentual > 0 else ''}...", 
                        parse_mode="HTML"
                    )
                    mytx = str(uuid.uuid4())
                    
                    # Passamos agendar_remarketing=False para NÃO reiniciar o ciclo de mensagens
                    pix, _gw_usada = await gerar_pix_gateway(
                        valor_float=preco_promo,
                        transaction_id=mytx,
                        bot_id=bot_db.id,
                        db=db,
                        user_telegram_id=str(chat_id),
                        user_first_name=first_name,
                        plano_nome=f"{plano.nome_exibicao} (OFERTA)",
                        agendar_remarketing=False  # <--- BLOQUEIA O RESTART DO CICLO
                    )
                    
                    if pix:
                        qr = pix.get('qr_code_text') or pix.get('qr_code')
                        txid = str(pix.get('id') or mytx).lower()
                        
                        # Salva pedido
                        novo_pedido = Pedido(
                            bot_id=bot_db.id,
                            telegram_id=str(chat_id),
                            first_name=first_name,
                            username=username,
                            plano_nome=f"{plano.nome_exibicao} (PROMO {desconto_percentual}% OFF)",
                            plano_id=plano.id,
                            valor=preco_promo,
                            transaction_id=txid,
                            txid=txid,
                            qr_code=qr,
                            status="pending",
                            tem_order_bump=False,
                            created_at=now_brazil(),
                            tracking_id=track_id_pedido,
                            gateway_usada=_gw_usada,
                        )
                        db.add(novo_pedido)
                        db.commit()
                        
                        try:
                            bot_temp.delete_message(chat_id, msg_wait.message_id)
                        except:
                            pass
                        
                        markup_pix = types.InlineKeyboardMarkup()
                        markup_pix.add(types.InlineKeyboardButton("🔄 VERIFICAR STATUS", callback_data=f"check_payment_{txid}"))
                        
                        # -----------------------------------------------------------
                        # 🔥 LÓGICA DE MENSAGEM INTELIGENTE (COM {oferta})
                        # -----------------------------------------------------------
                        flow_config = db.query(BotFlow).filter(BotFlow.bot_id == bot_db.id).first()
                        custom_msg = flow_config.msg_pix if flow_config and flow_config.msg_pix else None
                        
                        # 1. Constrói o BLOCO DA OFERTA (Bonito)
                        if desconto_percentual > 0:
                            oferta_block = f"💵 De: <s>R$ {plano.preco_atual:.2f}</s>\n"
                            oferta_block += f"✨ Por apenas: <b>R$ {preco_promo:.2f}</b>\n"
                            oferta_block += f"📊 Economia: <b>{desconto_percentual}% OFF</b>"
                        else:
                            oferta_block = f"💰 Valor: <b>R$ {preco_promo:.2f}</b>"

                        msg_pix = ""

                        if custom_msg:
                            # --- MODO PERSONALIZADO ---
                            # Se o usuário colocou {oferta}, substitui pelo bloco bonito
                            # Se não, substitui {valor} pelo preço simples
                            val_fmt = f"{preco_promo:.2f}".replace('.', ',')
                            
                            msg_pix = custom_msg.replace("{nome}", first_name)\
                                                .replace("{plano}", plano.nome_exibicao)\
                                                .replace("{valor}", val_fmt)\
                                                .replace("{oferta}", oferta_block) # 🔥 AQUI ESTÁ O SEGREDO

                            if "{qrcode}" in msg_pix:
                                msg_pix = msg_pix.replace("{qrcode}", f"<pre>{qr}</pre>")
                            else:
                                msg_pix += f"\n\n👇 Copie o código abaixo:\n<pre>{qr}</pre>"
                        else:
                            # --- MODO PADRÃO (HARDCODED) ---
                            msg_pix = f"🔥 <b>OFERTA ESPECIAL GERADA!</b>\n\n"
                            msg_pix += f"🎁 Plano: <b>{plano.nome_exibicao}</b>\n"
                            msg_pix += f"{oferta_block}\n\n" # Usa o bloco bonito
                            msg_pix += f"🔐 Pix Copia e Cola:\n\n<pre>{qr}</pre>\n\n"
                            msg_pix += "👆 Toque na chave PIX para copiar\n"
                            msg_pix += "⚡ Acesso liberado automaticamente!"
                        
                        # ✨ CONVERTE EMOJIS PREMIUM na mensagem do PIX
                        msg_pix = convert_premium_emojis(msg_pix)
                        bot_temp.send_message(chat_id, msg_pix, parse_mode="HTML", reply_markup=markup_pix)
                        
                    else:
                        try:
                            bot_temp.delete_message(chat_id, msg_wait.message_id)
                        except:
                            pass
                        bot_temp.send_message(chat_id, "❌ Erro ao gerar PIX.")
                        
                except Exception as e:
                    logger.error(f"❌ Erro no handler checkout_promo_: {str(e)}", exc_info=True)
                    bot_temp.send_message(chat_id, "❌ Erro ao processar oferta.", parse_mode="HTML")

            # --- B1.5) HANDLER DE BOTÃO DE REMARKETING AUTOMÁTICO ---
            elif data.startswith("remarketing_plano_"):
                try:
                    plano_id = int(data.split("_")[2])
                    plano = db.query(PlanoConfig).filter(PlanoConfig.id == plano_id).first()
                    
                    if not plano:
                        bot_temp.send_message(chat_id, "❌ Plano não encontrado.")
                        return {"status": "error"}
                    
                    # Busca config de remarketing
                    remarketing_cfg = db.query(RemarketingConfig).filter(
                        RemarketingConfig.bot_id == bot_db.id
                    ).first()
                    
                    promo_values = remarketing_cfg.promo_values or {} if remarketing_cfg else {}
                    # Converte chave para string para garantir compatibilidade com JSON
                    valor_final = promo_values.get(str(plano_id), plano.preco_atual)
                    
                    # ==============================================================================
                    # 💣 CORREÇÃO MESTRE: AUTO-DESTRUIÇÃO APÓS CLIQUE (Bulletproof)
                    # ==============================================================================
                    if (remarketing_cfg and 
                        remarketing_cfg.auto_destruct_enabled and 
                        remarketing_cfg.auto_destruct_after_click and
                        hasattr(enviar_remarketing_automatico, 'pending_destructions')):
                        
                        dict_pendente = enviar_remarketing_automatico.pending_destructions
                        
                        dados_destruicao = dict_pendente.get(chat_id) or dict_pendente.get(str(chat_id))
                        
                        if dados_destruicao:
                            logger.info(f"💣 [CALLBACK] Encontrado agendamento de destruição para {chat_id}")
                            
                            msg_id_to_del = dados_destruicao.get('message_id')
                            btns_id_to_del = dados_destruicao.get('buttons_message_id')
                            tempo_para_explodir = dados_destruicao.get('destruct_seconds', 5)
                            
                            def auto_delete_after_click():
                                time.sleep(tempo_para_explodir)
                                try:
                                    bot_temp.delete_message(chat_id, msg_id_to_del)
                                    if btns_id_to_del:
                                        bot_temp.delete_message(chat_id, btns_id_to_del)
                                    logger.info(f"🗑️ Mensagem de remarketing auto-destruída APÓS clique ({chat_id})")
                                except Exception as e:
                                    logger.warning(f"⚠️ Falha ao deletar msg após clique (já deletada?): {e}")

                            try:
                                thread_pool.submit(auto_delete_after_click)
                            except RuntimeError:
                                pass
                            
                            if chat_id in dict_pendente: del dict_pendente[chat_id]
                            if str(chat_id) in dict_pendente: del dict_pendente[str(chat_id)]
                        else:
                            logger.warning(f"⚠️ Clique detectado, mas não achei agendamento para {chat_id} (Restartou o servidor?)")

                    # ==============================================================================
                    
                    # Gera PIX com valor promocional
                    lead_origem = db.query(Lead).filter(Lead.user_id == str(chat_id), Lead.bot_id == bot_db.id).first()
                    # 🔥 FIX: Busca tracking do remarketing automático (não do link original do lead)
                    # Tenta buscar o TrackingLink vinculado ao remarketing automático desse bot
                    track_id_pedido = None
                    try:
                        # Busca o tracking link de remarketing automático mais recente para esse bot
                        _rmkt_track = db.query(TrackingLink).filter(
                            TrackingLink.bot_id == bot_db.id,
                            TrackingLink.origem == 'remarketing'
                        ).order_by(TrackingLink.created_at.desc()).first()
                        if _rmkt_track:
                            track_id_pedido = _rmkt_track.id
                            logger.info(f"📊 [REMARKETING-AUTO] Tracking #{_rmkt_track.id} vinculado ao pedido de {chat_id}")
                    except Exception as e_track:
                        logger.warning(f"⚠️ Erro ao buscar tracking remarketing auto: {e_track}")
                    
                    desconto_percentual = 0
                    if plano.preco_atual > valor_final:
                        desconto_percentual = int(((plano.preco_atual - valor_final) / plano.preco_atual) * 100)
                    
                    msg_wait = bot_temp.send_message(
                        chat_id, 
                        f"⏳ Gerando <b>OFERTA ESPECIAL</b>{f' com {desconto_percentual}% OFF' if desconto_percentual > 0 else ''}...", 
                        parse_mode="HTML"
                    )
                    
                    mytx = str(uuid.uuid4())
                    
                    # 🔥 NÃO REINICIA O CICLO DE REMARKETING
                    pix, _gw_usada = await gerar_pix_gateway(
                        valor_float=valor_final,
                        transaction_id=mytx,
                        bot_id=bot_db.id,
                        db=db,
                        user_telegram_id=str(chat_id),
                        user_first_name=first_name,
                        plano_nome=f"{plano.nome_exibicao} (OFERTA AUTOMÁTICA)",
                        agendar_remarketing=False  # <--- BLOQUEIA O RESTART DO CICLO
                    )
                    
                    if pix:
                        qr = pix.get('qr_code_text') or pix.get('qr_code')
                        txid = str(pix.get('id') or mytx).lower()
                        
                        # Salva pedido
                        novo_pedido = Pedido(
                            bot_id=bot_db.id,
                            telegram_id=str(chat_id),
                            first_name=first_name,
                            username=username,
                            plano_nome=f"{plano.nome_exibicao} (PROMO {desconto_percentual}% OFF)" if desconto_percentual > 0 else plano.nome_exibicao,
                            plano_id=plano.id,
                            valor=valor_final,
                            transaction_id=txid,
                            txid=txid,
                            qr_code=qr,
                            status="pending",
                            tem_order_bump=False,
                            created_at=now_brazil(),
                            tracking_id=track_id_pedido,
                            origem='disparo_auto',
                            gateway_usada=_gw_usada,
                        )
                        db.add(novo_pedido)
                        db.commit()
                        
                        try:
                            bot_temp.delete_message(chat_id, msg_wait.message_id)
                        except:
                            pass
                        
                        markup_pix = types.InlineKeyboardMarkup()
                        markup_pix.add(types.InlineKeyboardButton("🔄 VERIFICAR STATUS", callback_data=f"check_payment_{txid}"))
                        
                        # -----------------------------------------------------------
                        # 🔥 LÓGICA DE MENSAGEM INTELIGENTE (COM {oferta})
                        # -----------------------------------------------------------
                        flow_config = db.query(BotFlow).filter(BotFlow.bot_id == bot_db.id).first()
                        custom_msg = flow_config.msg_pix if flow_config and flow_config.msg_pix else None
                        
                        if desconto_percentual > 0:
                            oferta_block = f"💵 De: <s>R$ {plano.preco_atual:.2f}</s>\n"
                            oferta_block += f"✨ Por apenas: <b>R$ {valor_final:.2f}</b>\n"
                            oferta_block += f"📊 Economia: <b>{desconto_percentual}% OFF</b>"
                        else:
                            oferta_block = f"💰 Valor: <b>R$ {valor_final:.2f}</b>"

                        msg_pix = ""
                        
                        if custom_msg:
                            val_fmt = f"{valor_final:.2f}".replace('.', ',')
                            msg_pix = custom_msg.replace("{nome}", first_name)\
                                                .replace("{plano}", plano.nome_exibicao)\
                                                .replace("{valor}", val_fmt)\
                                                .replace("{oferta}", oferta_block)
                            
                            if "{qrcode}" in msg_pix:
                                msg_pix = msg_pix.replace("{qrcode}", f"<pre>{qr}</pre>")
                            else:
                                msg_pix += f"\n\n👇 Copie o código abaixo:\n<pre>{qr}</pre>"
                        else:
                            msg_pix = f"🔥 <b>OFERTA ESPECIAL GERADA!</b>\n\n"
                            msg_pix += f"🎁 Plano: <b>{plano.nome_exibicao}</b>\n"
                            msg_pix += f"{oferta_block}\n\n"
                            msg_pix += f"🔐 Pix Copia e Cola:\n\n<pre>{qr}</pre>\n\n"
                            msg_pix += "👆 Toque na chave PIX para copiar\n"
                            msg_pix += "⚡ Acesso liberado automaticamente!"
                        
                        # Inicia mensagens alternantes NOVAMENTE após clicar
                        alternar_mensagens_pagamento(bot_temp, chat_id, bot_db.id)
                        
                        # Agenda remarketing novamente (se configurado)
                        agendar_remarketing_automatico(bot_temp, chat_id, bot_db.id)
                        
                        # ✨ CONVERTE EMOJIS PREMIUM na mensagem do PIX
                        msg_pix = convert_premium_emojis(msg_pix)
                        bot_temp.send_message(chat_id, msg_pix, parse_mode="HTML", reply_markup=markup_pix)
                        
                    else:
                        try:
                            bot_temp.delete_message(chat_id, msg_wait.message_id)
                        except:
                            pass
                        bot_temp.send_message(chat_id, "❌ Erro ao gerar PIX.")
                        
                except Exception as e:
                    logger.error(f"❌ Erro no handler remarketing_plano_: {str(e)}", exc_info=True)
                    bot_temp.send_message(chat_id, "❌ Erro ao processar oferta.", parse_mode="HTML")

            # --- B2) CHECKOUT NORMAL (AGORA VEM DEPOIS) ---
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
                    # ✨ CONVERTE EMOJIS PREMIUM
                    txt_bump = convert_premium_emojis(txt_bump)
                    try:
                        if bump.msg_media:
                            if bump.msg_media.lower().endswith(('.mp4','.mov')):
                                bot_temp.send_video(chat_id, bump.msg_media, caption=txt_bump, reply_markup=mk, parse_mode="HTML", protect_content=_protect)
                            else:
                                bot_temp.send_photo(chat_id, bump.msg_media, caption=txt_bump, reply_markup=mk, parse_mode="HTML", protect_content=_protect)
                        else:
                            bot_temp.send_message(chat_id, txt_bump, reply_markup=mk, parse_mode="HTML", protect_content=_protect)
                    except:
                        bot_temp.send_message(chat_id, txt_bump, reply_markup=mk, parse_mode="HTML", protect_content=_protect)
                else:
                    # PIX DIRETO (SEM ORDER BUMP)
                    msg_wait = bot_temp.send_message(chat_id, "⏳ Gerando <b>PIX</b>...", parse_mode="HTML")
                    mytx = str(uuid.uuid4())
                    
                    # Gera PIX com remarketing integrado
                    pix, _gw_usada = await gerar_pix_gateway(
                        valor_float=plano.preco_atual,
                        transaction_id=mytx,
                        bot_id=bot_db.id,
                        db=db,
                        user_telegram_id=str(chat_id),  # ✅ PASSA TELEGRAM ID
                        user_first_name=first_name,     # ✅ PASSA NOME
                        plano_nome=plano.nome_exibicao  # ✅ PASSA PLANO
                    )

                    if pix:
                        qr = pix.get('qr_code_text') or pix.get('qr_code')
                        txid = str(pix.get('id') or mytx).lower()
                        
                        # Salva pedido
                        novo_pedido = Pedido(
                            bot_id=bot_db.id,
                            telegram_id=str(chat_id),
                            first_name=first_name,
                            username=username,
                            plano_nome=plano.nome_exibicao,
                            plano_id=plano.id,
                            valor=plano.preco_atual,
                            transaction_id=txid,
                            txid=txid,
                            qr_code=qr,
                            status="pending",
                            tem_order_bump=False,
                            created_at=now_brazil(),
                            tracking_id=track_id_pedido,
                            gateway_usada=_gw_usada,
                        )
                        db.add(novo_pedido)
                        db.commit()
                        
                        try:
                            bot_temp.delete_message(chat_id, msg_wait.message_id)
                        except:
                            pass
                        
                        markup_pix = types.InlineKeyboardMarkup()
                        markup_pix.add(types.InlineKeyboardButton("🔄 VERIFICAR STATUS", callback_data=f"check_payment_{txid}"))

                        # -----------------------------------------------------------
                        # 🎨 MENSAGEM PIX: PERSONALIZADA vs PADRÃO
                        # -----------------------------------------------------------
                        flow_config = db.query(BotFlow).filter(BotFlow.bot_id == bot_db.id).first()
                        custom_msg = flow_config.msg_pix if flow_config and flow_config.msg_pix else None
                        
                        msg_pix = ""
                        val_fmt = f"{plano.preco_atual:.2f}".replace('.', ',')
                        
                        if custom_msg:
                            # Aqui {oferta} é igual a {valor} porque não tem desconto
                            oferta_simple = f"💰 Valor: <b>R$ {val_fmt}</b>"
                            msg_pix = custom_msg.replace("{nome}", first_name)\
                                                .replace("{plano}", plano.nome_exibicao)\
                                                .replace("{valor}", val_fmt)\
                                                .replace("{oferta}", oferta_simple)
                            
                            if "{qrcode}" in msg_pix:
                                msg_pix = msg_pix.replace("{qrcode}", f"<pre>{qr}</pre>")
                            else:
                                msg_pix += f"\n\n👇 Copie o código abaixo:\n<pre>{qr}</pre>"
                        else:
                            # --- MODO PADRÃO (ANTIGO) ---
                            msg_pix = (
                                f"🌟 Seu pagamento foi gerado:\n"
                                f"🎁 Plano: <b>{plano.nome_exibicao}</b>\n"
                                f"💰 Valor: <b>R$ {val_fmt}</b>\n"
                                f"🔐 Pix Copia e Cola:\n\n"
                                f"<pre>{qr}</pre>\n\n"
                                f"👆 Toque na chave PIX para copiar\n"
                                f"⚡ Acesso liberado automaticamente!"
                            )
                        
                        # ✨ CONVERTE EMOJIS PREMIUM na mensagem do PIX
                        msg_pix = convert_premium_emojis(msg_pix)
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
                    try:
                        bot_temp.delete_message(chat_id, update.callback_query.message.message_id)
                    except:
                        pass
                
                valor_final = plano.preco_atual
                nome_final = plano.nome_exibicao
                if aceitou and bump:
                    valor_final += bump.preco
                    nome_final += f" + {bump.nome_produto}"
                
                msg_wait = bot_temp.send_message(chat_id, f"⏳ Gerando PIX: <b>{nome_final}</b>...", parse_mode="HTML")
                mytx = str(uuid.uuid4())

                # Gera PIX com remarketing integrado
                pix, _gw_usada = await gerar_pix_gateway(
                    valor_float=valor_final,
                    transaction_id=mytx,
                    bot_id=bot_db.id,
                    db=db,
                    user_telegram_id=str(chat_id),  # ✅ PASSA TELEGRAM ID
                    user_first_name=first_name,     # ✅ PASSA NOME
                    plano_nome=nome_final           # ✅ PASSA PLANO
                )
                
                if pix:
                    qr = pix.get('qr_code_text') or pix.get('qr_code')
                    txid = str(pix.get('id') or mytx).lower()
                    
                    # Salva pedido
                    novo_pedido = Pedido(
                        bot_id=bot_db.id,
                        telegram_id=str(chat_id),
                        first_name=first_name,
                        username=username,
                        plano_nome=nome_final,
                        plano_id=plano.id,
                        valor=valor_final,
                        transaction_id=txid,
                        txid=txid,
                        qr_code=qr,
                        status="pending",
                        tem_order_bump=aceitou,
                        created_at=now_brazil(),
                        tracking_id=track_id_pedido,
                        gateway_usada=_gw_usada,
                    )
                    db.add(novo_pedido)
                    db.commit()
                    
                    try:
                        bot_temp.delete_message(chat_id, msg_wait.message_id)
                    except:
                        pass
                    
                    markup_pix = types.InlineKeyboardMarkup()
                    markup_pix.add(types.InlineKeyboardButton("🔄 VERIFICAR STATUS", callback_data=f"check_payment_{txid}"))

                    # -----------------------------------------------------------
                    # 🎨 MENSAGEM PIX (BUMP): PERSONALIZADA vs PADRÃO
                    # -----------------------------------------------------------
                    flow_config = db.query(BotFlow).filter(BotFlow.bot_id == bot_db.id).first()
                    custom_msg = flow_config.msg_pix if flow_config and flow_config.msg_pix else None
                    
                    msg_pix = ""
                    val_fmt = f"{valor_final:.2f}".replace('.', ',')
                    
                    if custom_msg:
                        # --- MODO PERSONALIZADO ---
                        oferta_simple = f"💰 Valor: <b>R$ {val_fmt}</b>"
                        msg_pix = custom_msg.replace("{nome}", first_name)\
                                            .replace("{plano}", nome_final)\
                                            .replace("{valor}", val_fmt)\
                                            .replace("{oferta}", oferta_simple)
                        
                        if "{qrcode}" in msg_pix:
                            msg_pix = msg_pix.replace("{qrcode}", f"<pre>{qr}</pre>")
                        else:
                            msg_pix += f"\n\n👇 Copie o código abaixo:\n<pre>{qr}</pre>"
                    else:
                        # --- MODO PADRÃO (ANTIGO) ---
                        msg_pix = (
                            f"🌟 Pagamento gerado:\n"
                            f"🎁 Plano: <b>{nome_final}</b>\n"
                            f"💰 Valor: <b>R$ {val_fmt}</b>\n"
                            f"🔐 Pix Copia e Cola:\n\n"
                            f"<pre>{qr}</pre>\n\n"
                            f"👆 Toque para copiar\n"
                            f"⚡ Acesso automático!"
                        )

                    # 🔥 LÓGICA DE MÍDIA ATUALIZADA NO FINAL (Se houver mídia no PIX/BUMP)
                    # NOTA: O fluxo original apenas enviava a msg de texto do PIX. 
                    # Mas se você quiser garantir que se por acaso tiver mídia ele leia:
                    # ✨ CONVERTE EMOJIS PREMIUM na mensagem do PIX
                    msg_pix = convert_premium_emojis(msg_pix)
                    bot_temp.send_message(chat_id, msg_pix, parse_mode="HTML", reply_markup=markup_pix)
                    
                else:
                    bot_temp.send_message(chat_id, "❌ Erro ao gerar PIX.")

            # --- D) PROMO (Campanhas Manuais) - LÓGICA BLINDADA ---
            elif data.startswith("promo_"):
                try:
                    try: 
                        campanha_uuid = data.split("_")[1]
                    except: 
                        campanha_uuid = ""
                    
                    campanha = db.query(RemarketingCampaign).filter(RemarketingCampaign.campaign_id == campanha_uuid).first()
                    
                    if not campanha:
                        bot_temp.send_message(chat_id, "❌ Oferta não encontrada ou link inválido.")
                        return {"status": "error"}
                    
                    if hasattr(campanha, 'expiration_at') and campanha.expiration_at:
                        exp_at = campanha.expiration_at if campanha.expiration_at.tzinfo else campanha.expiration_at.replace(tzinfo=BRAZIL_TZ)
                        if now_brazil() > exp_at:
                            bot_temp.send_message(chat_id, "🚫 <b>OFERTA ENCERRADA!</b>\n\nO tempo desta oferta acabou.", parse_mode="HTML")
                            return {"status": "expired"}
                    
                    plano = db.query(PlanoConfig).filter(PlanoConfig.id == campanha.plano_id).first()
                    
                    if not plano:
                        bot_temp.send_message(chat_id, "❌ O plano desta oferta não existe mais.")
                        return {"status": "error"}

                    # Define Preço (Custom ou Original)
                    preco_final = float(plano.preco_atual)
                    
                    # 🔥 CORREÇÃO: Verifica promo_price corretamente (None != 0, float(0) é falsy)
                    if hasattr(campanha, 'promo_price') and campanha.promo_price is not None:
                        try:
                            promo_val = float(campanha.promo_price)
                            if promo_val > 0:
                                preco_final = round(promo_val, 2)
                        except (ValueError, TypeError):
                            pass  # Mantém preco_final = preco_atual
                    
                    # 🔥 CORREÇÃO EXTRA: Se promo_price falhou, tenta buscar do config JSON
                    if preco_final == float(plano.preco_atual) and campanha.config:
                        try:
                            cfg = json.loads(campanha.config) if isinstance(campanha.config, str) else campanha.config
                            if isinstance(cfg, str): cfg = json.loads(cfg)
                            cfg_price_mode = cfg.get('price_mode', 'original')
                            cfg_custom_price = cfg.get('custom_price')
                            if cfg_price_mode == 'custom' and cfg_custom_price is not None:
                                val_cfg = float(str(cfg_custom_price).replace(',', '.'))
                                if val_cfg > 0:
                                    preco_final = round(val_cfg, 2)
                                    logger.info(f"💰 [PROMO] Preço recuperado do config JSON: R${preco_final}")
                        except Exception as e_cfg:
                            logger.warning(f"⚠️ Erro ao ler config para preço: {e_cfg}")
                    
                    desconto_percentual = 0
                    if plano.preco_atual > preco_final:
                        try:
                            desconto_percentual = int(((plano.preco_atual - preco_final) / plano.preco_atual) * 100)
                        except:
                            desconto_percentual = 0

                    msg_wait = bot_temp.send_message(chat_id, "⏳ Gerando <b>OFERTA ESPECIAL</b>...", parse_mode="HTML")
                    
                    mytx = str(uuid.uuid4())
                    
                    try:
                        # 🔥 NÃO REINICIA O CICLO DE REMARKETING
                        pix, _gw_usada = await gerar_pix_gateway(
                            valor_float=preco_final,
                            transaction_id=mytx,
                            bot_id=bot_db.id,
                            db=db,
                            user_telegram_id=str(chat_id),
                            user_first_name=first_name,
                            plano_nome=f"{plano.nome_exibicao} (OFERTA)",
                            agendar_remarketing=False 
                        )
                    except Exception as e_pix:
                        logger.error(f"❌ Erro CRÍTICO ao gerar PIX: {e_pix}", exc_info=True)
                        bot_temp.send_message(chat_id, "❌ Erro ao conectar com o banco de pagamentos.")
                        return {"status": "error"}

                    if pix:
                        qr = pix.get('qr_code_text') or pix.get('qr_code')
                        txid = str(pix.get('id') or mytx).lower()
                        
                        # 🔥 Extrai tracking_link_id da campanha para metrificar
                        _track_id_rmkt = None
                        try:
                            _cfg = json.loads(campanha.config) if isinstance(campanha.config, str) else (campanha.config or {})
                            if isinstance(_cfg, str): _cfg = json.loads(_cfg)
                            _track_id_rmkt = _cfg.get("tracking_link_id")
                        except: pass
                        
                        novo_pedido = Pedido(
                            bot_id=bot_db.id, 
                            telegram_id=str(chat_id), 
                            first_name=first_name, 
                            username=username, 
                            plano_nome=f"{plano.nome_exibicao} (OFERTA)", 
                            plano_id=plano.id, 
                            valor=preco_final, 
                            transaction_id=txid, 
                            txid=txid,
                            qr_code=qr, 
                            status="pending", 
                            tem_order_bump=False, 
                            created_at=now_brazil(), 
                            tracking_id=_track_id_rmkt,
                            origem='remarketing',
                            gateway_usada=_gw_usada,
                        )
                        db.add(novo_pedido)
                        
                        try:
                            if hasattr(campanha, 'clicks'):
                                if campanha.clicks is None:
                                    campanha.clicks = 0
                                campanha.clicks += 1
                                logger.info(f"📊 Clique contabilizado para campanha {campanha_uuid}")
                            else:
                                logger.warning(f"⚠️ Tabela RemarketingCampaign sem coluna 'clicks'. Analytics ignorado para {campanha_uuid}")
                            
                            # 🔥 CORREÇÃO: Também contabiliza clique no TrackingLink vinculado
                            if _track_id_rmkt:
                                _tl_rmkt = db.query(TrackingLink).filter(TrackingLink.id == _track_id_rmkt).first()
                                if _tl_rmkt:
                                    _tl_rmkt.clicks = (_tl_rmkt.clicks or 0) + 1
                                    logger.info(f"📊 Clique contabilizado no TrackingLink #{_track_id_rmkt}")
                        except Exception as e_click:
                            logger.warning(f"⚠️ Erro não fatal ao contar clique: {e_click}")
                        
                        db.commit()
                        
                        try: bot_temp.delete_message(chat_id, msg_wait.message_id)
                        except: pass
                        
                        markup_pix = types.InlineKeyboardMarkup()
                        markup_pix.add(types.InlineKeyboardButton("🔄 VERIFICAR PAGAMENTO", callback_data=f"check_payment_{txid}"))

                        # -----------------------------------------------------------
                        # 🔥 LÓGICA DE MENSAGEM INTELIGENTE (COM {oferta})
                        # -----------------------------------------------------------
                        flow_config = db.query(BotFlow).filter(BotFlow.bot_id == bot_db.id).first()
                        custom_msg = flow_config.msg_pix if flow_config and flow_config.msg_pix else None
                        
                        # 1. Constrói o BLOCO DA OFERTA (Bonito)
                        if desconto_percentual > 0:
                            oferta_block = f"💵 De: <s>R$ {plano.preco_atual:.2f}</s>\n"
                            oferta_block += f"✨ Por apenas: <b>R$ {preco_final:.2f}</b>\n"
                            oferta_block += f"📉 Economia: <b>{desconto_percentual}% OFF</b>"
                        else:
                            oferta_block = f"💰 Valor Promocional: <b>R$ {preco_final:.2f}</b>"

                        msg_pix = ""

                        if custom_msg:
                            # --- MODO PERSONALIZADO ---
                            val_fmt = f"{preco_final:.2f}".replace('.', ',')
                            msg_pix = custom_msg.replace("{nome}", first_name)\
                                                .replace("{plano}", plano.nome_exibicao)\
                                                .replace("{valor}", val_fmt)\
                                                .replace("{oferta}", oferta_block)

                            if "{qrcode}" in msg_pix:
                                msg_pix = msg_pix.replace("{qrcode}", f"<pre>{qr}</pre>")
                            else:
                                msg_pix += f"\n\n👇 Copie o código abaixo:\n<pre>{qr}</pre>"
                        else:
                            # --- MODO PADRÃO (ANTIGO) ---
                            msg_pix = f"🔥 <b>OFERTA ATIVADA!</b>\n\n"
                            msg_pix += f"🎁 Plano: <b>{plano.nome_exibicao}</b>\n"
                            msg_pix += f"{oferta_block}\n"
                            msg_pix += f"\n🔐 Pague via Pix Copia e Cola:\n\n<pre>{qr}</pre>\n\n👆 Toque na chave PIX acima para copiá-la\n‼️ Após o pagamento, o acesso será liberado automaticamente!"

                        # ✨ CONVERTE EMOJIS PREMIUM na mensagem do PIX
                        msg_pix = convert_premium_emojis(msg_pix)
                        bot_temp.send_message(chat_id, msg_pix, parse_mode="HTML", reply_markup=markup_pix)
                    else:
                        try: bot_temp.delete_message(chat_id, msg_wait.message_id)
                        except: pass
                        bot_temp.send_message(chat_id, "❌ Erro ao gerar QRCode. Tente novamente.")

                except Exception as e:
                    logger.error(f"❌ Erro GERAL no handler promo_: {e}", exc_info=True)
                    try: bot_temp.send_message(chat_id, "❌ Ocorreu um erro ao processar sua solicitação.")
                    except: pass

            # --- 🚀 UPSELL: ACEITAR ---
            elif data.startswith("upsell_accept_"):
                try:
                    bot_id_str = data.replace("upsell_accept_", "").strip()
                    upsell_cfg = db.query(UpsellConfig).filter(UpsellConfig.bot_id == int(bot_id_str)).first()
                    
                    if not upsell_cfg or not upsell_cfg.ativo:
                        bot_temp.send_message(chat_id, "❌ Esta oferta não está mais disponível.")
                        return {"status": "ok"}
                    
                    # Auto-destruir a mensagem da oferta se configurado
                    if upsell_cfg.autodestruir:
                        try: bot_temp.delete_message(chat_id, update.callback_query.message.message_id)
                        except: pass
                    
                    msg_wait = bot_temp.send_message(chat_id, "⏳ Gerando pagamento da oferta...", parse_mode="HTML")
                    
                    mytx = str(uuid.uuid4())
                    preco_upsell = round(float(upsell_cfg.preco), 2)
                    
                    try:
                        pix, _gw_usada = await gerar_pix_gateway(
                            valor_float=preco_upsell,
                            transaction_id=mytx,
                            bot_id=bot_db.id,
                            db=db,
                            user_telegram_id=str(chat_id),
                            user_first_name=first_name,
                            plano_nome=f"UPSELL: {upsell_cfg.nome_produto}",
                            agendar_remarketing=False
                        )
                    except Exception as e_pix:
                        logger.error(f"❌ Erro PIX upsell: {e_pix}", exc_info=True)
                        try: bot_temp.delete_message(chat_id, msg_wait.message_id)
                        except: pass
                        bot_temp.send_message(chat_id, "❌ Erro ao gerar pagamento. Tente novamente.")
                        return {"status": "error"}
                    
                    if pix:
                        qr = pix.get('qr_code_text') or pix.get('qr_code')
                        txid = str(pix.get('id') or mytx).lower()
                        
                        novo_pedido = Pedido(
                            bot_id=bot_db.id,
                            telegram_id=str(chat_id),
                            first_name=first_name,
                            username=username,
                            plano_nome=f"UPSELL: {upsell_cfg.nome_produto}",
                            plano_id=None,
                            valor=preco_upsell,
                            transaction_id=txid,
                            txid=txid,
                            qr_code=qr,
                            status="pending",
                            tem_order_bump=False,
                            created_at=now_brazil(),
                            status_funil='fundo',
                            origem='upsell',
                            tracking_id=(db.query(Lead).filter(Lead.user_id == str(chat_id), Lead.bot_id == bot_db.id).first() or type('', (), {'tracking_id': None})).tracking_id,
                            gateway_usada=_gw_usada
                        )
                        db.add(novo_pedido)
                        db.commit()
                        
                        try: bot_temp.delete_message(chat_id, msg_wait.message_id)
                        except: pass
                        
                        markup_pix = types.InlineKeyboardMarkup()
                        markup_pix.add(types.InlineKeyboardButton("🔄 VERIFICAR PAGAMENTO", callback_data=f"check_payment_{txid}"))
                        
                        msg_pix_txt = (
                            f"🚀 <b>OFERTA UPSELL</b>\n\n"
                            f"📦 {upsell_cfg.nome_produto}\n"
                            f"💰 Valor: <b>R$ {preco_upsell:.2f}</b>\n\n"
                            f"🔐 Pix Copia e Cola:\n\n<pre>{qr}</pre>\n\n"
                            f"👆 Toque na chave PIX acima para copiá-la\n"
                            f"⚡ Acesso liberado automaticamente!"
                        )
                        
                        # ✨ CONVERTE EMOJIS PREMIUM na mensagem do PIX (Upsell)
                        msg_pix_txt = convert_premium_emojis(msg_pix_txt)
                        bot_temp.send_message(chat_id, msg_pix_txt, parse_mode="HTML", reply_markup=markup_pix)
                    
                except Exception as e:
                    logger.error(f"❌ Erro handler upsell_accept_: {e}", exc_info=True)
                    try: bot_temp.send_message(chat_id, "❌ Erro ao processar oferta.")
                    except: pass

            # --- 🚀 UPSELL: RECUSAR ---
            elif data.startswith("upsell_decline_"):
                try:
                    # Auto-destruir se configurado
                    bot_id_str = data.replace("upsell_decline_", "").strip()
                    upsell_cfg = db.query(UpsellConfig).filter(UpsellConfig.bot_id == int(bot_id_str)).first()
                    
                    if upsell_cfg and upsell_cfg.autodestruir:
                        try: bot_temp.delete_message(chat_id, update.callback_query.message.message_id)
                        except: pass
                    
                    bot_temp.send_message(chat_id, "👍 Tudo bem! Se mudar de ideia, é só avisar.")
                    logger.info(f"❌ Upsell recusado por {chat_id}")
                    
                except Exception as e:
                    logger.error(f"❌ Erro upsell_decline_: {e}")

            # --- 📉 DOWNSELL: ACEITAR ---
            elif data.startswith("downsell_accept_"):
                try:
                    bot_id_str = data.replace("downsell_accept_", "").strip()
                    downsell_cfg = db.query(DownsellConfig).filter(DownsellConfig.bot_id == int(bot_id_str)).first()
                    
                    if not downsell_cfg or not downsell_cfg.ativo:
                        bot_temp.send_message(chat_id, "❌ Esta oferta não está mais disponível.")
                        return {"status": "ok"}
                    
                    # Auto-destruir a mensagem da oferta se configurado
                    if downsell_cfg.autodestruir:
                        try: bot_temp.delete_message(chat_id, update.callback_query.message.message_id)
                        except: pass
                    
                    msg_wait = bot_temp.send_message(chat_id, "⏳ Gerando pagamento da oferta...", parse_mode="HTML")
                    
                    mytx = str(uuid.uuid4())
                    preco_downsell = round(float(downsell_cfg.preco), 2)
                    
                    try:
                        pix, _gw_usada = await gerar_pix_gateway(
                            valor_float=preco_downsell,
                            transaction_id=mytx,
                            bot_id=bot_db.id,
                            db=db,
                            user_telegram_id=str(chat_id),
                            user_first_name=first_name,
                            plano_nome=f"DOWNSELL: {downsell_cfg.nome_produto}",
                            agendar_remarketing=False
                        )
                    except Exception as e_pix:
                        logger.error(f"❌ Erro PIX downsell: {e_pix}", exc_info=True)
                        try: bot_temp.delete_message(chat_id, msg_wait.message_id)
                        except: pass
                        bot_temp.send_message(chat_id, "❌ Erro ao gerar pagamento. Tente novamente.")
                        return {"status": "error"}
                    
                    if pix:
                        qr = pix.get('qr_code_text') or pix.get('qr_code')
                        txid = str(pix.get('id') or mytx).lower()
                        
                        novo_pedido = Pedido(
                            bot_id=bot_db.id,
                            telegram_id=str(chat_id),
                            first_name=first_name,
                            username=username,
                            plano_nome=f"DOWNSELL: {downsell_cfg.nome_produto}",
                            plano_id=None,
                            valor=preco_downsell,
                            transaction_id=txid,
                            txid=txid,
                            qr_code=qr,
                            status="pending",
                            tem_order_bump=False,
                            created_at=now_brazil(),
                            status_funil='fundo',
                            origem='downsell',
                            tracking_id=(db.query(Lead).filter(Lead.user_id == str(chat_id), Lead.bot_id == bot_db.id).first() or type('', (), {'tracking_id': None})).tracking_id,
                            gateway_usada=_gw_usada
                        )
                        db.add(novo_pedido)
                        db.commit()
                        
                        try: bot_temp.delete_message(chat_id, msg_wait.message_id)
                        except: pass
                        
                        markup_pix = types.InlineKeyboardMarkup()
                        markup_pix.add(types.InlineKeyboardButton("🔄 VERIFICAR PAGAMENTO", callback_data=f"check_payment_{txid}"))
                        
                        msg_pix_txt = (
                            f"🎁 <b>OFERTA ESPECIAL</b>\n\n"
                            f"📦 {downsell_cfg.nome_produto}\n"
                            f"💰 Valor: <b>R$ {preco_downsell:.2f}</b>\n\n"
                            f"🔐 Pix Copia e Cola:\n\n<pre>{qr}</pre>\n\n"
                            f"👆 Toque na chave PIX acima para copiá-la\n"
                            f"⚡ Acesso liberado automaticamente!"
                        )
                        
                        # ✨ CONVERTE EMOJIS PREMIUM na mensagem do PIX (Downsell)
                        msg_pix_txt = convert_premium_emojis(msg_pix_txt)
                        bot_temp.send_message(chat_id, msg_pix_txt, parse_mode="HTML", reply_markup=markup_pix)
                    
                except Exception as e:
                    logger.error(f"❌ Erro handler downsell_accept_: {e}", exc_info=True)
                    try: bot_temp.send_message(chat_id, "❌ Erro ao processar oferta.")
                    except: pass

            # --- 📉 DOWNSELL: RECUSAR ---
            elif data.startswith("downsell_decline_"):
                try:
                    bot_id_str = data.replace("downsell_decline_", "").strip()
                    downsell_cfg = db.query(DownsellConfig).filter(DownsellConfig.bot_id == int(bot_id_str)).first()
                    
                    if downsell_cfg and downsell_cfg.autodestruir:
                        try: bot_temp.delete_message(chat_id, update.callback_query.message.message_id)
                        except: pass
                    
                    bot_temp.send_message(chat_id, "👍 Tudo bem! Obrigado pela preferência! 😊")
                    logger.info(f"❌ Downsell recusado por {chat_id}")
                    
                except Exception as e:
                    logger.error(f"❌ Erro downsell_decline_: {e}")

    except ApiTelegramException as e:
        err_str = str(e).lower()
        if "blocked" in err_str or "403" in err_str:
            logger.info(f"🚫 Webhook: Usuário bloqueou o bot (403)")
        elif "chat not found" in err_str or "400" in err_str:
            logger.warning(f"⚠️ Webhook: Chat não encontrado ou mensagem inválida: {e}")
        else:
            logger.error(f"❌ Erro no webhook (Telegram): {e}")
    except Exception as e:
        logger.error(f"❌ Erro no webhook: {e}")

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
                    "origem_entrada": getattr(l, 'origem_entrada', 'bot_direto') or 'bot_direto',
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
        elif status == "canal_free":
            # 🔥 NOVO: Filtro exclusivo para leads do Canal Free
            leads_cf = db.query(Lead).filter(
                Lead.bot_id.in_(bots_alvo),
                Lead.origem_entrada == "canal_free"
            ).all()
            
            for l in leads_cf:
                tid = str(l.user_id).strip()
                key = f"{l.bot_id}_{tid}"
                data_lead = getattr(l, 'expiration_date', None)
                
                contatos_unicos[key] = {
                    "id": l.id,
                    "telegram_id": tid,
                    "user_id": tid,
                    "first_name": l.nome or "Sem nome",
                    "username": l.username,
                    "plano_nome": "-",
                    "valor": 0.0,
                    "status": l.status or "pending",
                    "role": "user",
                    "created_at": clean_date(l.created_at),
                    "status_funil": l.funil_stage or "topo",
                    "origem": "canal_free",
                    "custom_expiration": clean_date(data_lead)
                }
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
async def resend_user_access(
    user_id: int, 
    plano_id: int = None,
    db: Session = Depends(get_db)
):
    """
    🔑 Reenvia o link de acesso VIP para um usuário que já pagou.
    Se plano_id for informado, envia o link do canal daquele plano específico.
    Caso contrário, usa o canal VIP padrão do bot.
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
        
        # 4. 🔥 DETERMINAR CANAL CORRETO (por plano ou padrão)
        canal_id_str = None
        plano_nome_reenvio = "Canal Padrão"
        
        if plano_id:
            # Buscar o plano específico para usar seu canal destino
            plano_obj = db.query(PlanoConfig).filter(PlanoConfig.id == plano_id, PlanoConfig.bot_id == bot_data.id).first()
            if plano_obj and plano_obj.id_canal_destino:
                canal_id_str = str(plano_obj.id_canal_destino).strip()
                plano_nome_reenvio = plano_obj.nome_exibicao or f"Plano #{plano_id}"
                logger.info(f"🎯 Reenvio via plano '{plano_nome_reenvio}' → canal {canal_id_str}")
        
        # Se não achou canal via plano, usa o padrão do bot
        if not canal_id_str:
            if not bot_data.id_canal_vip:
                logger.error(f"❌ Bot {pedido.bot_id} não tem canal VIP configurado")
                raise HTTPException(status_code=400, detail="Bot não tem canal VIP configurado")
            canal_id_str = str(bot_data.id_canal_vip).strip()
        
        # 5. Gerar novo link e enviar
        try:
            tb = telebot.TeleBot(bot_data.token)
            
            try: 
                canal_id = int(canal_id_str)
            except: 
                canal_id = canal_id_str
            
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
                f"📦 Plano: <b>{plano_nome_reenvio}</b>\n"
                f"📅 Validade: <b>{texto_validade}</b>\n\n"
                f"Seu acesso exclusivo:\n👉 {convite.invite_link}\n\n"
                f"<i>Use este link para entrar no grupo VIP.</i>"
            )
            
            tb.send_message(int(pedido.telegram_id), msg_cliente, parse_mode="HTML")
            
            logger.info(f"✅ Acesso reenviado para {pedido.first_name} (ID: {pedido.telegram_id}) via plano '{plano_nome_reenvio}'")
            
            return {
                "status": "success",
                "message": f"Acesso reenviado com sucesso via {plano_nome_reenvio}!",
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

# =========================================================
# 🚫 REMOVER USUÁRIO DO VIP (MANUAL)
# =========================================================
@app.post("/api/admin/users/{user_id}/remove-vip")
async def remove_user_from_vip(user_id: int, db: Session = Depends(get_db)):
    """
    Remove manualmente um usuário de TODOS os canais/grupos VIP do bot,
    marca o pedido como expired e atualiza o Lead.
    """
    try:
        pedido = db.query(Pedido).filter(Pedido.id == user_id).first()
        if not pedido:
            raise HTTPException(404, "Pedido não encontrado")
        
        if pedido.status in ["expired", "failed"]:
            raise HTTPException(400, "Pedido já está expirado/cancelado")
        
        bot_data = db.query(BotModel).filter(BotModel.id == pedido.bot_id).first()
        if not bot_data:
            raise HTTPException(404, "Bot não encontrado")
        
        tb = telebot.TeleBot(bot_data.token)
        telegram_id = int(pedido.telegram_id)
        canais_removidos = []
        erros_remocao = []
        
        # 1. Remover do canal VIP padrão
        if bot_data.id_canal_vip:
            try:
                canal_id = int(str(bot_data.id_canal_vip).strip())
                tb.ban_chat_member(canal_id, telegram_id)
                tb.unban_chat_member(canal_id, telegram_id)
                canais_removidos.append(f"Padrão ({canal_id})")
            except Exception as e:
                erros_remocao.append(f"Padrão: {str(e)}")
        
        # 2. Remover de canais específicos dos planos
        planos = db.query(Plano).filter(Plano.bot_id == bot_data.id).all()
        for plano in planos:
            if plano.id_canal_destino and str(plano.id_canal_destino).strip() != str(bot_data.id_canal_vip or "").strip():
                try:
                    canal_plano = int(str(plano.id_canal_destino).strip())
                    tb.ban_chat_member(canal_plano, telegram_id)
                    tb.unban_chat_member(canal_plano, telegram_id)
                    canais_removidos.append(f"{plano.nome_exibicao} ({canal_plano})")
                except Exception as e:
                    erros_remocao.append(f"{plano.nome_exibicao}: {str(e)}")
        
        # 3. Remover de grupos extras (se tiver)
        try:
            grupos = db.query(GrupoVip).filter(GrupoVip.bot_id == bot_data.id).all()
            for grupo in grupos:
                try:
                    tb.ban_chat_member(int(grupo.chat_id), telegram_id)
                    tb.unban_chat_member(int(grupo.chat_id), telegram_id)
                    canais_removidos.append(f"Grupo: {grupo.title}")
                except:
                    pass
        except:
            pass
        
        # 4. Atualizar status do pedido
        pedido.status = "expired"
        pedido.custom_expiration = now_brazil()
        
        # 5. Atualizar Lead
        lead = db.query(Lead).filter(
            Lead.bot_id == pedido.bot_id,
            Lead.user_id == str(telegram_id)
        ).first()
        if lead:
            lead.status = "expired"
            lead.funil_stage = "expirado"
        
        db.commit()
        
        # 6. Notificar o usuário
        try:
            tb.send_message(
                telegram_id,
                "🚫 <b>Seu acesso VIP foi removido.</b>\n\nPara renovar, digite /start",
                parse_mode="HTML"
            )
        except:
            pass
        
        logger.info(f"🚫 Usuário {pedido.first_name} ({telegram_id}) removido do VIP manualmente. Canais: {canais_removidos}")
        
        return {
            "status": "success",
            "message": f"Usuário removido de {len(canais_removidos)} canal(is)",
            "canais_removidos": canais_removidos,
            "erros": erros_remocao
        }
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"❌ Erro ao remover do VIP: {e}")
        db.rollback()
        raise HTTPException(500, str(e))

# =========================================================
# 📋 LISTAR PLANOS DO BOT (Para modal de reenvio de acesso)
# =========================================================
@app.get("/api/admin/bots/{bot_id}/planos-canais")
def listar_planos_com_canais(
    bot_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """
    Retorna lista de planos do bot com seus canais de destino,
    para o frontend montar o seletor de reenvio de acesso.
    """
    try:
        bot = db.query(BotModel).filter(BotModel.id == bot_id).first()
        if not bot:
            raise HTTPException(404, "Bot não encontrado")
        
        planos = db.query(PlanoConfig).filter(PlanoConfig.bot_id == bot_id).all()
        
        result = []
        
        # Canal padrão do bot
        if bot.id_canal_vip:
            result.append({
                "id": None,
                "nome": "Canal VIP Padrão",
                "canal_id": bot.id_canal_vip,
                "is_default": True
            })
        
        # Planos com canais específicos
        for p in planos:
            result.append({
                "id": p.id,
                "nome": p.nome_exibicao or f"Plano #{p.id}",
                "preco": p.preco_atual,
                "canal_id": p.id_canal_destino or bot.id_canal_vip,
                "is_default": False
            })
        
        return result
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Erro ao listar planos: {e}")
        return []

# =========================================================
# 🧪 ENDPOINT GENÉRICO: ENVIAR TESTE (para qualquer página)
# =========================================================
class TestSendRequest(BaseModel):
    """Envio de teste genérico - funciona para todas as páginas"""
    message: Optional[str] = ""           # Texto HTML (com shortcodes de emoji premium)
    media_url: Optional[str] = None       # URL da mídia (foto/vídeo)
    media_type: Optional[str] = None      # "photo", "video", "audio"
    audio_url: Optional[str] = None       # URL de áudio separado (voice note)
    source: Optional[str] = "generic"     # "remarketing", "auto_remarketing", "canal_free", "order_bump", "upsell", "downsell"
    buttons: Optional[list] = None        # [{"text": "...", "url": "..."}, ...]
    # Campos específicos do remarketing com oferta
    incluir_oferta: Optional[bool] = False
    plano_oferta_id: Optional[int] = None
    preco_custom: Optional[float] = None
    price_mode: Optional[str] = "original"

@app.post("/api/admin/bots/{bot_id}/send-test-message")
def send_test_message(
    bot_id: int,
    req: TestSendRequest,
    db: Session = Depends(get_db),
    current_user = Depends(get_current_user)
):
    """
    Envia uma mensagem de teste COMPLETA para o admin_principal do bot.
    Inclui: mídia, texto com emojis premium, botões, e oferta simulada.
    """
    verificar_bot_pertence_usuario(bot_id, current_user.id, db)
    
    bot_db = db.query(BotModel).filter(BotModel.id == bot_id).first()
    if not bot_db:
        raise HTTPException(404, "Bot não encontrado")
    
    admin_id = bot_db.admin_principal_id
    if not admin_id:
        raise HTTPException(400, "Nenhum admin principal configurado para este bot. Vá em Configurações do Bot e defina o Admin.")
    
    try:
        bot_sender = TeleBot(bot_db.token, threaded=False)
        
        # ✨ Converte emojis premium e variáveis
        texto = req.message or ""
        texto = texto.replace("{first_name}", "Usuário Teste").replace("{nome}", "Teste").replace("{username}", "@teste").replace("{id}", str(admin_id))
        texto = convert_premium_emojis(texto)
        
        # 🔒 Proteção de conteúdo
        protect = getattr(bot_db, 'protect_content', False) or False
        
        # Monta markup de botões
        markup = None
        if req.buttons and len(req.buttons) > 0:
            markup = types.InlineKeyboardMarkup()
            for btn in req.buttons:
                if btn.get("url"):
                    markup.add(types.InlineKeyboardButton(btn["text"], url=btn["url"]))
                elif btn.get("callback_data"):
                    markup.add(types.InlineKeyboardButton(btn["text"], callback_data=btn["callback_data"]))
                else:
                    markup.add(types.InlineKeyboardButton(btn["text"], callback_data="test_noop"))
        
        # Se remarketing com oferta, adiciona botão simulado
        if req.incluir_oferta and req.plano_oferta_id:
            plano = db.query(PlanoConfig).filter(PlanoConfig.id == req.plano_oferta_id).first()
            if plano:
                preco = req.preco_custom if req.price_mode == "custom" and req.preco_custom else (plano.preco_atual or plano.preco_cheio or 0)
                if not markup:
                    markup = types.InlineKeyboardMarkup()
                markup.add(types.InlineKeyboardButton(
                    f"🔥{plano.nome_exibicao} - R$ {preco:.2f}",
                    callback_data="test_oferta_noop"
                ))
        
        # ===== ENVIO =====
        sent = False
        
        # 1. Áudio/voice note separado (chega primeiro)
        if req.audio_url:
            try:
                audio_bytes, _, _ = _download_audio_bytes(req.audio_url)
                bot_sender.send_chat_action(admin_id, 'record_voice')
                time.sleep(1)
                if audio_bytes:
                    bot_sender.send_voice(admin_id, audio_bytes, protect_content=protect)
                else:
                    bot_sender.send_voice(admin_id, req.audio_url, protect_content=protect)
            except Exception as e_audio:
                logger.warning(f"⚠️ [TEST-SEND] Falha ao enviar áudio: {e_audio}")
        
        # 2. Mídia com caption + botões inline
        if req.media_url:
            try:
                url_lower = req.media_url.lower()
                media_type = req.media_type or ""
                
                if media_type == 'video' or url_lower.endswith(('.mp4', '.mov')):
                    bot_sender.send_video(admin_id, req.media_url, caption=texto, reply_markup=markup, parse_mode="HTML", protect_content=protect)
                    sent = True
                elif media_type == 'audio' or url_lower.endswith(('.ogg', '.mp3', '.wav')):
                    audio_bytes, _, _ = _download_audio_bytes(req.media_url)
                    bot_sender.send_chat_action(admin_id, 'record_voice')
                    time.sleep(2)
                    if audio_bytes:
                        bot_sender.send_voice(admin_id, audio_bytes, protect_content=protect)
                    else:
                        bot_sender.send_voice(admin_id, req.media_url, protect_content=protect)
                    if texto or markup:
                        time.sleep(1)
                        bot_sender.send_message(admin_id, texto or "⬇️", reply_markup=markup, parse_mode="HTML", protect_content=protect)
                    sent = True
                else:
                    bot_sender.send_photo(admin_id, req.media_url, caption=texto, reply_markup=markup, parse_mode="HTML", protect_content=protect)
                    sent = True
            except Exception as e_media:
                logger.warning(f"⚠️ [TEST-SEND] Falha ao enviar mídia: {e_media}")
        
        # 3. Só texto + botões (se não enviou mídia)
        if not sent:
            if not texto and not markup:
                texto = "🧪 Mensagem de teste"
            bot_sender.send_message(admin_id, texto or "🧪", reply_markup=markup, parse_mode="HTML", protect_content=protect)
        
        logger.info(f"🧪 [TEST-SEND] Teste completo enviado para admin {admin_id} (bot={bot_id}, source={req.source})")
        
        return {"status": "sent", "message": f"Teste enviado para o admin {admin_id}!", "admin_id": admin_id}
        
    except Exception as e:
        logger.error(f"❌ [TEST-SEND] Erro: {e}", exc_info=True)
        raise HTTPException(500, detail=f"Erro ao enviar teste: {str(e)}")

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
# 🔄 FUNÇÃO DE BACKGROUND (LÓGICA BLINDADA V3: SETS PUROS)
# =========================================================
def processar_envio_remarketing(campaign_db_id: int, bot_id: int, payload: RemarketingRequest):
    """
    Executa o envio em background.
    CORREÇÃO APLICADA: 
    1. Botão agora aponta para 'promo_{uuid}' (evita automação indesejada).
    2. Salva 'promo_price' na coluna do banco (corrige o valor do PIX).
    3. Mantém lógica robusta de seleção de público.
    """
    # 🔥 CRIA NOVA SESSÃO DEDICADA
    db = SessionLocal() 
    
    try:
        # --- A. RECUPERAÇÃO DE DADOS BÁSICOS ---
        campanha = db.query(RemarketingCampaign).filter(RemarketingCampaign.id == campaign_db_id).first()
        bot_db = db.query(BotModel).filter(BotModel.id == bot_id).first()
        
        if not campanha or not bot_db: return

        logger.info(f"🚀 INICIANDO DISPARO | Bot: {bot_db.nome} | Target: {payload.target}")

        # --- B. PREPARAÇÃO DA OFERTA (PREÇO CUSTOMIZADO) ---
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
                # Lógica: Se for Customizado E valor > 0, usa custom. Senão, usa original.
                if payload.price_mode == 'custom' and payload.custom_price is not None:
                     try:
                        val_custom = float(str(payload.custom_price).replace(',', '.'))
                        preco_final = round(val_custom, 2) if val_custom > 0 else float(plano_db.preco_atual)
                     except (ValueError, TypeError):
                        preco_final = float(plano_db.preco_atual)
                else:
                    preco_final = float(plano_db.preco_atual)
                
                # 🔥 CORREÇÃO: Garante sempre 2 casas decimais
                preco_final = round(preco_final, 2)
                
                # Cálculo de Expiração (se houver)
                if payload.expiration_mode != "none" and payload.expiration_value:
                    val = int(payload.expiration_value)
                    agora = now_brazil()
                    if payload.expiration_mode == "minutes": data_expiracao = agora + timedelta(minutes=val)
                    elif payload.expiration_mode == "hours": data_expiracao = agora + timedelta(hours=val)
                    elif payload.expiration_mode == "days": data_expiracao = agora + timedelta(days=val)

        # --- C. SELEÇÃO DE PÚBLICO ---
        bot_sender = telebot.TeleBot(bot_db.token)
        target = str(payload.target).lower().strip()
        lista_final_ids = []

        if payload.is_test:
            # Modo Teste: Apenas 1 ID
            if payload.specific_user_id: 
                lista_final_ids = [str(payload.specific_user_id).strip()]
            else:
                adm = db.query(BotAdmin).filter(BotAdmin.bot_id == bot_id).first()
                if adm: lista_final_ids = [str(adm.telegram_id).strip()]
        else:
            # --- LÓGICA DE CONJUNTOS ---
            # 1. Pega TODOS os Pedidos (Status e ID)
            q_pedidos = db.query(Pedido.telegram_id, Pedido.status).filter(Pedido.bot_id == bot_id).all()
            
            ids_pagantes = set()    # Já Pagou (Fundo)
            ids_pendentes = set()   # Gerou pedido mas não pagou (Meio / Lead Quente)
            ids_com_pedido = set()  # Conjunto de todos que tem pedido
            
            status_pagos = ['paid', 'active', 'approved', 'completed', 'succeeded']
            
            for p in q_pedidos:
                if not p.telegram_id: continue
                tid = str(p.telegram_id).strip()
                ids_com_pedido.add(tid)
                
                st = str(p.status).lower() if p.status else ""
                if st in status_pagos:
                    ids_pagantes.add(tid)
                elif st == 'expired':
                    pass 
                else:
                    ids_pendentes.add(tid) 
            
            # 2. Pega TODOS os Leads
            q_leads = db.query(Lead.user_id).filter(Lead.bot_id == bot_id).all()
            ids_todos_leads = {str(l.user_id).strip() for l in q_leads if l.user_id}

            # 3. Cruzamento
            if target == 'topo': 
                # TOPO = Leads Totais - Leads com Pedido (Nunca tentaram comprar)
                lista_final_ids = list(ids_todos_leads - ids_com_pedido)
                
            elif target == 'meio':
                # MEIO = Pedidos Pendentes - Pedidos Pagos (Tentou mas não pagou)
                lista_final_ids = list(ids_pendentes - ids_pagantes)
                
            elif target == 'fundo' or target == 'clientes':
                # FUNDO = Pagantes
                lista_final_ids = list(ids_pagantes)
                
            elif target == 'todos': 
                # TODOS
                lista_final_ids = list(ids_todos_leads.union(ids_com_pedido))
                
            else: # Fallback (Expirados)
                 q_exp = db.query(Pedido.telegram_id).filter(Pedido.bot_id == bot_id, Pedido.status == 'expired').all()
                 ids_exp = {str(x.telegram_id).strip() for x in q_exp if x.telegram_id}
                 lista_final_ids = list(ids_exp - ids_pagantes)

        # Atualiza contagem INICIAL e marca como "enviando"
        logger.info(f"📊 Filtro '{target}' resultou em {len(lista_final_ids)} leads.")
        db.query(RemarketingCampaign).filter(RemarketingCampaign.id == campaign_db_id).update({
            "total_leads": len(lista_final_ids),
            "status": "enviando"  # 🔥 NOVO: Marca como "enviando"
        })
        db.commit()

        # --- D. MONTAGEM DA MENSAGEM (CORREÇÃO DO BOTÃO) ---
        markup = None
        if plano_db:
            markup = types.InlineKeyboardMarkup()
            preco_txt = f"{preco_final:.2f}".replace('.', ',')
            btn_text = f"🔥 {plano_db.nome_exibicao} - R$ {preco_txt}"
            
            # 🔥 CORREÇÃO 1: Aponta para 'promo_', que usa o preço customizado e NÃO ativa remarketing
            cb_data = f"promo_{uuid_campanha}" 
            markup.add(types.InlineKeyboardButton(btn_text, callback_data=cb_data))

        # --- E. ENVIO COM ATUALIZAÇÃO EM TEMPO REAL ---
        sent_count = 0
        blocked_count = 0
        batch_size = 10  # Atualiza DB a cada 10 envios

        # 🔒 Carrega flag de proteção para o bot
        _protect_rmkt = getattr(bot_db, 'protect_content', False) or False

        # 🔊 PRÉ-DOWNLOAD: Se é áudio, baixa UMA vez antes do loop
        _bulk_audio_bytes = None
        _bulk_audio_dur = 0
        if payload.media_url and payload.media_url.lower().endswith(('.ogg', '.mp3', '.wav')):
            _bulk_audio_bytes, _, _bulk_audio_dur = _download_audio_bytes(payload.media_url)
            if _bulk_audio_bytes:
                logger.info(f"🎙️ Áudio pré-baixado para envio em massa ({len(_bulk_audio_bytes)} bytes)")

        for idx, uid in enumerate(lista_final_ids):
            if not uid or len(uid) < 5: continue
            try:
                midia_ok = False
                texto_envio = payload.mensagem.replace("{nome}", "Cliente")
                
                # ✨ CONVERTE EMOJIS PREMIUM (shortcodes → tg-emoji HTML tags)
                texto_envio = convert_premium_emojis(texto_envio)

                if payload.media_url and len(payload.media_url) > 5:
                    try:
                        ext = payload.media_url.lower()
                        if ext.endswith(('.mp4', '.mov', '.avi')):
                            bot_sender.send_video(uid, payload.media_url, caption=texto_envio, reply_markup=markup, parse_mode="HTML", protect_content=_protect_rmkt)
                        elif ext.endswith(('.ogg', '.mp3', '.wav')):
                            # 🔊 ÁUDIO: Envia bytes pré-baixados como voice note nativo
                            
                            # 🔥 USA O HELPER SINCRONO AQUI
                            _wait_bulk = min(max(_bulk_audio_dur, 2), 60) if _bulk_audio_dur and _bulk_audio_dur > 0 else 3
                            _sleep_with_action(bot_sender, uid, _wait_bulk, 'record_voice')
                            
                            if _bulk_audio_bytes:
                                bot_sender.send_voice(uid, _bulk_audio_bytes, protect_content=_protect_rmkt)
                            else:
                                bot_sender.send_voice(uid, payload.media_url, protect_content=_protect_rmkt)
                            
                            if texto_envio or markup:
                                time.sleep(1)
                                bot_sender.send_message(uid, texto_envio or "⬇️ Escolha:", reply_markup=markup, parse_mode="HTML", protect_content=_protect_rmkt)
                        else:
                            bot_sender.send_photo(uid, payload.media_url, caption=texto_envio, reply_markup=markup, parse_mode="HTML", protect_content=_protect_rmkt)
                        midia_ok = True
                    except: pass
                
                if not midia_ok:
                    bot_sender.send_message(uid, texto_envio, reply_markup=markup, parse_mode="HTML", protect_content=_protect_rmkt)
                
                sent_count += 1
                time.sleep(0.04)
                
            except Exception as e:
                err = str(e).lower()
                if "blocked" in err or "kicked" in err or "deactivated" in err or "not found" in err:
                    blocked_count += 1
            
            # 🔥 NOVO: Atualiza progresso no banco a cada batch_size envios
            if (idx + 1) % batch_size == 0 or (idx + 1) == len(lista_final_ids):
                try:
                    db.query(RemarketingCampaign).filter(
                        RemarketingCampaign.id == campaign_db_id
                    ).update({
                        "sent_success": sent_count,
                        "blocked_count": blocked_count
                    })
                    db.commit()
                except Exception as e:
                    logger.warning(f"⚠️ Erro ao atualizar progresso: {e}")

        # 🔥 CORRIGIDO: Mantém mesma estrutura que o config_data inicial
        # 🔥 CORREÇÃO: Salva custom_price com round(2) para evitar "9,9" ao reutilizar
        config_completa = {
            "mensagem": payload.mensagem,
            "media_url": payload.media_url,
            "incluir_oferta": payload.incluir_oferta,
            "plano_oferta_id": payload.plano_oferta_id,
            "price_mode": payload.price_mode,
            "custom_price": round(preco_final, 2) if preco_final > 0 else None,
            "expiration_mode": payload.expiration_mode,
            "expiration_value": payload.expiration_value
        }
        
        update_data = {
            "status": "concluido",  # 🔥 Marca como concluído
            "sent_success": sent_count,
            "blocked_count": blocked_count, 
            "config": json.dumps(config_completa),
            "expiration_at": data_expiracao
        }
        
        if plano_db:
            update_data["plano_id"] = plano_db.id
            # 🔥 CORREÇÃO 2: Salva o preço na coluna 'promo_price' para o handler usar (com round)
            update_data["promo_price"] = round(preco_final, 2) if preco_final > 0 else None
        
        db.query(RemarketingCampaign).filter(RemarketingCampaign.id == campaign_db_id).update(update_data)
        db.commit()

        logger.info(f"✅ Disparo concluído. Sucesso: {sent_count} | Bloqueados: {blocked_count}")

    except Exception as e:
        logger.error(f"❌ Erro thread remarketing: {e}", exc_info=True)
        try:
            db.query(RemarketingCampaign).filter(RemarketingCampaign.id == campaign_db_id).update({"status": "erro"})
            db.commit()
        except: pass
    finally:
        db.close()

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
        # 2. CRIAR REGISTRO DA CAMPANHA (ATUALIZADO PARA INCLUIR PREÇO CUSTOM)
        # =========================================================
        uuid_campanha = str(uuid.uuid4())
        
        # 🔥 CORREÇÃO: Normaliza custom_price ANTES de salvar (vírgula → ponto, 2 casas)
        normalized_custom_price = None
        if getattr(payload, 'price_mode', 'original') == 'custom' and getattr(payload, 'custom_price', None) is not None:
            try:
                val_norm = float(str(payload.custom_price).replace(',', '.'))
                if val_norm > 0:
                    normalized_custom_price = round(val_norm, 2)
            except (ValueError, TypeError):
                normalized_custom_price = None
        
        # 🔥 CORRIGIDO: Cria JSON de config com TODOS os campos necessários
        config_data = {
            "mensagem": payload.mensagem,
            "media_url": payload.media_url,
            "incluir_oferta": getattr(payload, 'incluir_oferta', False),  # ✅ CAMPO ADICIONADO
            "plano_oferta_id": getattr(payload, 'plano_oferta_id', None),
            "price_mode": getattr(payload, 'price_mode', 'original'),
            "custom_price": normalized_custom_price if normalized_custom_price else getattr(payload, 'custom_price', None),
            "expiration_mode": getattr(payload, 'expiration_mode', 'none'),
            "expiration_value": getattr(payload, 'expiration_value', 0)
        }

        # 🔥 CORREÇÃO: Já calcula promo_price na criação para o handler promo_ usar imediatamente
        promo_price_inicial = None
        if getattr(payload, 'incluir_oferta', False) and getattr(payload, 'plano_oferta_id', None):
            try:
                plano_oferta_id_str = str(payload.plano_oferta_id)
                plano_temp = db.query(PlanoConfig).filter(
                    (PlanoConfig.key_id == plano_oferta_id_str) | 
                    (PlanoConfig.id == int(plano_oferta_id_str) if plano_oferta_id_str.isdigit() else False)
                ).first()
                if plano_temp:
                    if normalized_custom_price and normalized_custom_price > 0:
                        promo_price_inicial = normalized_custom_price
                    else:
                        promo_price_inicial = float(plano_temp.preco_atual)
            except Exception as e_price:
                logger.warning(f"⚠️ Erro ao calcular promo_price inicial: {e_price}")

        nova_campanha = RemarketingCampaign(
            bot_id=payload.bot_id,
            campaign_id=uuid_campanha,
            type="teste" if payload.is_test else "massivo",
            target=payload.target,
            config=json.dumps(config_data),
            status='agendado', 
            data_envio=now_brazil(),
            total_leads=0,
            sent_success=0,
            blocked_count=0,
            plano_id=getattr(payload, 'plano_oferta_id', None),
            promo_price=promo_price_inicial  # 🔥 JÁ SALVA O PREÇO CORRETO AQUI
        )
        db.add(nova_campanha)
        db.commit()
        db.refresh(nova_campanha)
        
        # 🔥 AUTO-TRACKING: Cria TrackingLink automático para metrificar esta campanha
        tracking_link_id = None
        try:
            # Busca ou cria pasta "Remarketing" automaticamente
            pasta_rmkt = db.query(TrackingFolder).filter(
                func.lower(TrackingFolder.nome) == "remarketing"
            ).first()
            
            if not pasta_rmkt:
                pasta_rmkt = TrackingFolder(
                    nome="Remarketing",
                    plataforma="telegram",
                    created_at=now_brazil()
                )
                db.add(pasta_rmkt)
                db.commit()
                db.refresh(pasta_rmkt)
            
            # Cria link de tracking vinculado à campanha
            data_label = now_brazil().strftime("%d%b_%H%M").lower()
            tipo_label = "teste" if payload.is_test else payload.target
            codigo_track = f"rmkt_{data_label}_{tipo_label}"[:50]
            
            novo_track = TrackingLink(
                folder_id=pasta_rmkt.id,
                bot_id=payload.bot_id,
                nome=f"Campanha {data_label} ({tipo_label})",
                codigo=codigo_track,
                origem="remarketing",
                clicks=0,
                leads=0,
                vendas=0,
                faturamento=0.0,
                created_at=now_brazil()
            )
            db.add(novo_track)
            db.commit()
            db.refresh(novo_track)
            tracking_link_id = novo_track.id
            
            # Salva o tracking_link_id na campanha config para referência
            config_data["tracking_link_id"] = tracking_link_id
            nova_campanha.config = json.dumps(config_data)
            db.commit()
            
            logger.info(f"📊 [AUTO-TRACKING] TrackingLink #{tracking_link_id} criado para campanha {uuid_campanha}")
        except Exception as e_track:
            logger.warning(f"⚠️ Erro ao criar auto-tracking (não fatal): {e_track}")
            db.rollback()
        
        logger.info(f"✅ Campanha {nova_campanha.id} registrada no banco")
        
        # 📋 AUDITORIA: Campanha de remarketing criada
        try:
            log_action(db=db, user_id=current_user.id, username=current_user.username, 
                       action="remarketing_campaign_created", resource_type="campaign",
                       resource_id=nova_campanha.id, 
                       description=f"Campanha remarketing criada (target: {payload.target}, bot: {payload.bot_id})")
        except: pass
        
        # =========================================================
        # 3. SE FOR TESTE, ENVIA SÍNCRONO (1 MENSAGEM APENAS)
        # =========================================================
        if payload.is_test:
            try:
                bot_data = db.query(BotModel).filter(BotModel.id == payload.bot_id).first()
                if not bot_data:
                    raise HTTPException(404, "Bot não encontrado")
                
                # Para teste simples, usamos a função de background mesmo, 
                # mas com o ID específico já setado no payload.
                # Isso garante que a lógica de botão e preço seja testada também!
                background_tasks.add_task(
                    processar_envio_remarketing, # Chamando a mesma função para garantir consistência
                    nova_campanha.id,
                    payload.bot_id,
                    payload # Passa o objeto completo
                )
                
                return {
                    "status": "enviado",
                    "message": f"Teste iniciado para {payload.specific_user_id}!",
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
            processar_envio_remarketing,  # <--- ✅ NOME CORRETO DA FUNÇÃO QUE VOCÊ QUER
            nova_campanha.id,
            payload.bot_id,
            payload # <--- ✅ PASSANDO O OBJETO INTEIRO (RemarketingRequest)
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
    
    # ✨ CONVERTE EMOJIS PREMIUM
    msg = convert_premium_emojis(msg)

    # 3. Configura Bot
    bot_db = db.query(BotModel).filter(BotModel.id == payload.bot_id).first()
    if not bot_db: raise HTTPException(404, "Bot não encontrado")
    sender = telebot.TeleBot(bot_db.token)
    
    # 4. Botão com preço promocional REAL (usa checkout_promo_ para garantir o valor correto)
    markup = None
    if campanha.plano_id:
        plano = db.query(PlanoConfig).filter(PlanoConfig.id == campanha.plano_id).first()
        if plano:
            markup = types.InlineKeyboardMarkup()
            preco = campanha.promo_price if campanha.promo_price else (plano.preco_atual or plano.preco_cheio or 0)
            preco_float = float(preco) if preco else 0
            preco_centavos = int(preco_float * 100)
            btn_text = f"🔥 {plano.nome_exibicao} - R$ {preco_float:.2f}".replace('.', ',')
            # 🔥 FIX: Usa checkout_promo_ para garantir que o PIX gerado use o valor promo
            markup.add(types.InlineKeyboardButton(btn_text, callback_data=f"checkout_promo_{plano.id}_{preco_centavos}"))

    # 5. Envio (HTML)
    # 5. Envio (HTML E ÁUDIO)
    try:
        if media:
            try:
                ext = media.lower()
                if ext.endswith(('.mp4', '.mov', '.avi')):
                    sender.send_video(payload.user_telegram_id, media, caption=msg, reply_markup=markup, parse_mode="HTML")
                elif ext.endswith(('.ogg', '.mp3', '.wav')):
                    # 🔊 ÁUDIO: Baixa e envia como bytes para voice note nativo
                    audio_bytes_ind, _fname_ind, _audio_dur_ind = _download_audio_bytes(media)
                    sender.send_chat_action(payload.user_telegram_id, 'record_voice')
                    _wait_ind = min(max(_audio_dur_ind, 2), 60) if _audio_dur_ind > 0 else 3
                    time.sleep(_wait_ind)
                    if audio_bytes_ind:
                        sender.send_voice(payload.user_telegram_id, audio_bytes_ind)
                    else:
                        sender.send_voice(payload.user_telegram_id, media)
                    if msg or markup:
                        time.sleep(2)
                        sender.send_message(payload.user_telegram_id, msg or "⬇️ Escolha:", reply_markup=markup, parse_mode="HTML")
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

# ============================================================
# ROTA 3: PROGRESSO DA CAMPANHA EM TEMPO REAL (NOVA!)
# ============================================================
@app.get("/api/admin/remarketing/progress/{campaign_id}")
def get_campaign_progress(campaign_id: int, db: Session = Depends(get_db)):
    """
    Retorna o progresso em tempo real de uma campanha de remarketing.
    Usado pelo widget flutuante no frontend para atualização ao vivo.
    """
    try:
        campanha = db.query(RemarketingCampaign).filter(
            RemarketingCampaign.id == campaign_id
        ).first()
        
        if not campanha:
            raise HTTPException(status_code=404, detail="Campanha não encontrada")
        
        # Calcula porcentagem de progresso
        total = campanha.total_leads or 0
        enviados = (campanha.sent_success or 0) + (campanha.blocked_count or 0)
        porcentagem = int((enviados / total * 100)) if total > 0 else 0
        
        return {
            "campaign_id": campanha.id,
            "status": campanha.status,  # 'agendado', 'enviando', 'concluido', 'erro'
            "total_leads": total,
            "sent_success": campanha.sent_success or 0,
            "blocked_count": campanha.blocked_count or 0,
            "processed": enviados,  # Total processado (sucesso + bloqueados)
            "percentage": porcentagem,
            "is_complete": campanha.status in ['concluido', 'erro'],
            "data_envio": campanha.data_envio.isoformat() if campanha.data_envio else None
        }
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Erro ao buscar progresso da campanha: {e}")
        raise HTTPException(500, detail=str(e))

# =========================================================
# 🆓 CANAL FREE - ENDPOINTS DA API
# =========================================================

@app.get("/api/admin/canal-free/{bot_id}")
def get_canal_free_config(
    bot_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """Retorna configuração do Canal Free para o bot"""
    try:
        # Verificar permissão
        bot = db.query(BotModel).filter(BotModel.id == bot_id).first()
        if not bot:
            raise HTTPException(status_code=404, detail="Bot não encontrado")
        
        if bot.owner_id != current_user.id and not current_user.is_superuser:
            raise HTTPException(status_code=403, detail="Acesso negado")
        
        # Buscar configuração
        config = db.query(CanalFreeConfig).filter(
            CanalFreeConfig.bot_id == bot_id
        ).first()
        
        if not config:
            # Retornar config padrão se não existir
            return {
                "bot_id": bot_id,
                "canal_id": None,
                "canal_name": None,
                "is_active": False,
                "message_text": "Olá! Em breve você será aceito no canal. Enquanto isso, que tal conhecer nosso canal VIP?",
                "media_url": None,
                "media_type": None,
                "buttons": [],
                "delay_seconds": 60
            }
        
        return {
            "id": config.id,
            "bot_id": config.bot_id,
            "canal_id": config.canal_id,
            "canal_name": config.canal_name,
            "is_active": config.is_active,
            "message_text": config.message_text,
            "media_url": config.media_url,
            "media_type": config.media_type,
            "buttons": config.buttons or [],
            "delay_seconds": config.delay_seconds,
            "audio_url": getattr(config, 'audio_url', None),
            "audio_delay_seconds": getattr(config, 'audio_delay_seconds', 3),
            "created_at": config.created_at.isoformat() if config.created_at else None,
            "updated_at": config.updated_at.isoformat() if config.updated_at else None
        }
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"❌ Erro ao buscar config Canal Free: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/admin/canal-free/{bot_id}")
def save_canal_free_config(
    bot_id: int,
    data: dict,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """Salva/atualiza configuração do Canal Free"""
    try:
        # Verificar permissão
        bot = db.query(BotModel).filter(BotModel.id == bot_id).first()
        if not bot:
            raise HTTPException(status_code=404, detail="Bot não encontrado")
        
        if bot.owner_id != current_user.id and not current_user.is_superuser:
            raise HTTPException(status_code=403, detail="Acesso negado")
        
        # Validações
        message_text = data.get("message_text", "").strip()
        if not message_text:
            raise HTTPException(status_code=400, detail="Mensagem de boas-vindas é obrigatória")
        
        delay_seconds = data.get("delay_seconds", 60)
        if delay_seconds < 1 or delay_seconds > 86400:  # 1s a 24h
            raise HTTPException(status_code=400, detail="Delay deve estar entre 1 e 86400 segundos")
        
        # Buscar configuração existente
        config = db.query(CanalFreeConfig).filter(
            CanalFreeConfig.bot_id == bot_id
        ).first()
        
        if config:
            # Atualizar existente
            config.canal_id = data.get("canal_id")
            config.canal_name = data.get("canal_name")
            config.is_active = data.get("is_active", False)
            config.message_text = message_text
            config.media_url = data.get("media_url")
            config.media_type = data.get("media_type")
            config.buttons = data.get("buttons", [])
            config.delay_seconds = delay_seconds
            config.audio_url = data.get("audio_url")
            config.audio_delay_seconds = data.get("audio_delay_seconds", 3)
            config.updated_at = now_brazil()
        else:
            # Criar nova
            config = CanalFreeConfig(
                bot_id=bot_id,
                canal_id=data.get("canal_id"),
                canal_name=data.get("canal_name"),
                is_active=data.get("is_active", False),
                message_text=message_text,
                media_url=data.get("media_url"),
                media_type=data.get("media_type"),
                buttons=data.get("buttons", []),
                delay_seconds=delay_seconds,
                audio_url=data.get("audio_url"),
                audio_delay_seconds=data.get("audio_delay_seconds", 3)
            )
            db.add(config)
        
        db.commit()
        db.refresh(config)
        
        logger.info(f"✅ Canal Free configurado - Bot: {bot_id}")
        
        return {
            "id": config.id,
            "bot_id": config.bot_id,
            "canal_id": config.canal_id,
            "canal_name": config.canal_name,
            "is_active": config.is_active,
            "message_text": config.message_text,
            "media_url": config.media_url,
            "media_type": config.media_type,
            "buttons": config.buttons,
            "delay_seconds": config.delay_seconds,
            "updated_at": config.updated_at.isoformat()
        }
        
    except HTTPException:
        raise
    except Exception as e:
        db.rollback()
        logger.error(f"❌ Erro ao salvar Canal Free: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/admin/canal-free/{bot_id}/canais-disponiveis")
def get_canais_disponiveis(
    bot_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """
    Lista canais onde o bot é admin e pode aprovar solicitações.
    Usa a API do Telegram para buscar chats onde o bot é administrador.
    """
    try:
        # Verificar permissão
        bot = db.query(BotModel).filter(BotModel.id == bot_id).first()
        if not bot:
            raise HTTPException(status_code=404, detail="Bot não encontrado")
        
        if bot.owner_id != current_user.id and not current_user.is_superuser:
            raise HTTPException(status_code=403, detail="Acesso negado")
        
        # Tentar buscar canais (isso requer que o bot tenha sido adicionado aos canais)
        # Como não temos acesso direto via API, retornamos instruções
        
        return {
            "message": "Para configurar, adicione o bot como administrador no canal com todas as permissões",
            "instructions": [
                "1. Crie um canal privado no Telegram",
                "2. Adicione o bot como administrador",
                "3. Conceda todas as permissões ao bot",
                "4. Crie um link de convite com 'Pedir aprovação de admins'",
                "5. Copie o ID do canal e configure abaixo"
            ],
            "canais": []
        }
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"❌ Erro ao buscar canais: {e}")
        raise HTTPException(status_code=500, detail=str(e))

# =========================================================
# 📦 GRUPOS E CANAIS - CATÁLOGO DE PRODUTOS (ESTEIRA)
# =========================================================

@app.get("/api/admin/bots/{bot_id}/groups")
def list_bot_groups(
    bot_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """Lista todos os grupos/canais extras cadastrados para o bot"""
    try:
        # Verificar permissão
        bot = db.query(BotModel).filter(BotModel.id == bot_id).first()
        if not bot:
            raise HTTPException(status_code=404, detail="Bot não encontrado")
        
        if bot.owner_id != current_user.id and not current_user.is_superuser:
            raise HTTPException(status_code=403, detail="Acesso negado")
        
        # Buscar grupos do bot
        groups = db.query(BotGroup).filter(
            BotGroup.bot_id == bot_id
        ).order_by(BotGroup.created_at.desc()).all()
        
        # Buscar planos do bot para enriquecer a resposta
        planos = db.query(PlanoConfig).filter(PlanoConfig.bot_id == bot_id).all()
        planos_map = {p.id: p.nome_exibicao for p in planos}
        
        result = []
        for g in groups:
            plan_ids = g.plan_ids or []
            plan_names = [planos_map.get(pid, f"Plano #{pid}") for pid in plan_ids]
            
            result.append({
                "id": g.id,
                "bot_id": g.bot_id,
                "title": g.title,
                "group_id": g.group_id,
                "link": g.link,
                "plan_ids": plan_ids,
                "plan_names": plan_names,
                "is_active": g.is_active,
                "created_at": g.created_at.isoformat() if g.created_at else None,
                "updated_at": g.updated_at.isoformat() if g.updated_at else None
            })
        
        return {"groups": result, "total": len(result)}
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"❌ Erro ao listar grupos do bot {bot_id}: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/admin/bots/{bot_id}/groups")
def create_bot_group(
    bot_id: int,
    data: BotGroupCreate,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """Cria um novo grupo/canal extra para o bot"""
    try:
        # Verificar permissão
        bot = db.query(BotModel).filter(BotModel.id == bot_id).first()
        if not bot:
            raise HTTPException(status_code=404, detail="Bot não encontrado")
        
        if bot.owner_id != current_user.id and not current_user.is_superuser:
            raise HTTPException(status_code=403, detail="Acesso negado")
        
        # Validações
        if not data.title or not data.title.strip():
            raise HTTPException(status_code=400, detail="Título é obrigatório")
        
        if not data.group_id or not data.group_id.strip():
            raise HTTPException(status_code=400, detail="ID do grupo é obrigatório")
        
        # Verificar se já existe um grupo com mesmo group_id para este bot
        existing = db.query(BotGroup).filter(
            BotGroup.bot_id == bot_id,
            BotGroup.group_id == data.group_id.strip()
        ).first()
        
        if existing:
            raise HTTPException(
                status_code=400, 
                detail=f"Já existe um grupo cadastrado com o ID {data.group_id} neste bot"
            )
        
        # Validar se os plan_ids existem para este bot
        if data.plan_ids:
            planos_existentes = db.query(PlanoConfig.id).filter(
                PlanoConfig.bot_id == bot_id,
                PlanoConfig.id.in_(data.plan_ids)
            ).all()
            ids_validos = [p.id for p in planos_existentes]
            ids_invalidos = [pid for pid in data.plan_ids if pid not in ids_validos]
            if ids_invalidos:
                raise HTTPException(
                    status_code=400,
                    detail=f"Planos não encontrados: {ids_invalidos}"
                )
        
        # Criar grupo
        new_group = BotGroup(
            bot_id=bot_id,
            owner_id=current_user.id,
            title=data.title.strip(),
            group_id=data.group_id.strip(),
            link=data.link.strip() if data.link else None,
            plan_ids=data.plan_ids or [],
            is_active=data.is_active if data.is_active is not None else True
        )
        
        db.add(new_group)
        db.commit()
        db.refresh(new_group)
        
        logger.info(f"✅ Grupo criado: '{new_group.title}' (ID: {new_group.group_id}) para Bot {bot_id}")
        
        # Audit Log
        try:
            audit = AuditLog(
                user_id=current_user.id,
                username=current_user.username,
                action="group_created",
                resource_type="bot_group",
                resource_id=new_group.id,
                description=f"Grupo '{new_group.title}' criado para Bot {bot_id}",
                success=True
            )
            db.add(audit)
            db.commit()
        except Exception:
            pass
        
        return {
            "id": new_group.id,
            "bot_id": new_group.bot_id,
            "title": new_group.title,
            "group_id": new_group.group_id,
            "link": new_group.link,
            "plan_ids": new_group.plan_ids,
            "is_active": new_group.is_active,
            "created_at": new_group.created_at.isoformat() if new_group.created_at else None
        }
        
    except HTTPException:
        raise
    except Exception as e:
        db.rollback()
        logger.error(f"❌ Erro ao criar grupo: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.put("/api/admin/bots/{bot_id}/groups/{group_id}")
def update_bot_group(
    bot_id: int,
    group_id: int,
    data: BotGroupUpdate,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """Atualiza um grupo/canal extra existente"""
    try:
        # Verificar permissão
        bot = db.query(BotModel).filter(BotModel.id == bot_id).first()
        if not bot:
            raise HTTPException(status_code=404, detail="Bot não encontrado")
        
        if bot.owner_id != current_user.id and not current_user.is_superuser:
            raise HTTPException(status_code=403, detail="Acesso negado")
        
        # Buscar grupo
        group = db.query(BotGroup).filter(
            BotGroup.id == group_id,
            BotGroup.bot_id == bot_id
        ).first()
        
        if not group:
            raise HTTPException(status_code=404, detail="Grupo não encontrado")
        
        # Atualizar campos (apenas os que foram enviados)
        if data.title is not None:
            if not data.title.strip():
                raise HTTPException(status_code=400, detail="Título não pode ser vazio")
            group.title = data.title.strip()
        
        if data.group_id is not None:
            if not data.group_id.strip():
                raise HTTPException(status_code=400, detail="ID do grupo não pode ser vazio")
            # Verificar duplicidade (excluindo o próprio registro)
            existing = db.query(BotGroup).filter(
                BotGroup.bot_id == bot_id,
                BotGroup.group_id == data.group_id.strip(),
                BotGroup.id != group_id
            ).first()
            if existing:
                raise HTTPException(
                    status_code=400,
                    detail=f"Já existe outro grupo com o ID {data.group_id}"
                )
            group.group_id = data.group_id.strip()
        
        if data.link is not None:
            group.link = data.link.strip() if data.link else None
        
        if data.plan_ids is not None:
            # Validar planos
            if data.plan_ids:
                planos_existentes = db.query(PlanoConfig.id).filter(
                    PlanoConfig.bot_id == bot_id,
                    PlanoConfig.id.in_(data.plan_ids)
                ).all()
                ids_validos = [p.id for p in planos_existentes]
                ids_invalidos = [pid for pid in data.plan_ids if pid not in ids_validos]
                if ids_invalidos:
                    raise HTTPException(
                        status_code=400,
                        detail=f"Planos não encontrados: {ids_invalidos}"
                    )
            group.plan_ids = data.plan_ids
        
        if data.is_active is not None:
            group.is_active = data.is_active
        
        db.commit()
        db.refresh(group)
        
        logger.info(f"✅ Grupo atualizado: '{group.title}' (ID: {group.group_id}) - Bot {bot_id}")
        
        return {
            "id": group.id,
            "bot_id": group.bot_id,
            "title": group.title,
            "group_id": group.group_id,
            "link": group.link,
            "plan_ids": group.plan_ids,
            "is_active": group.is_active,
            "created_at": group.created_at.isoformat() if group.created_at else None,
            "updated_at": group.updated_at.isoformat() if group.updated_at else None
        }
        
    except HTTPException:
        raise
    except Exception as e:
        db.rollback()
        logger.error(f"❌ Erro ao atualizar grupo {group_id}: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.delete("/api/admin/bots/{bot_id}/groups/{group_id}")
def delete_bot_group(
    bot_id: int,
    group_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """Remove um grupo/canal extra do bot"""
    try:
        # Verificar permissão
        bot = db.query(BotModel).filter(BotModel.id == bot_id).first()
        if not bot:
            raise HTTPException(status_code=404, detail="Bot não encontrado")
        
        if bot.owner_id != current_user.id and not current_user.is_superuser:
            raise HTTPException(status_code=403, detail="Acesso negado")
        
        # Buscar grupo
        group = db.query(BotGroup).filter(
            BotGroup.id == group_id,
            BotGroup.bot_id == bot_id
        ).first()
        
        if not group:
            raise HTTPException(status_code=404, detail="Grupo não encontrado")
        
        group_title = group.title
        group_telegram_id = group.group_id
        
        db.delete(group)
        db.commit()
        
        logger.info(f"🗑️ Grupo removido: '{group_title}' (ID: {group_telegram_id}) - Bot {bot_id}")
        
        # Audit Log
        try:
            audit = AuditLog(
                user_id=current_user.id,
                username=current_user.username,
                action="group_deleted",
                resource_type="bot_group",
                resource_id=group_id,
                description=f"Grupo '{group_title}' removido do Bot {bot_id}",
                success=True
            )
            db.add(audit)
            db.commit()
        except Exception:
            pass
        
        return {"status": "ok", "message": f"Grupo '{group_title}' removido com sucesso"}
        
    except HTTPException:
        raise
    except Exception as e:
        db.rollback()
        logger.error(f"❌ Erro ao deletar grupo {group_id}: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/admin/bots/{bot_id}/groups/{group_id}")
def get_bot_group(
    bot_id: int,
    group_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """Retorna detalhes de um grupo/canal específico"""
    try:
        # Verificar permissão
        bot = db.query(BotModel).filter(BotModel.id == bot_id).first()
        if not bot:
            raise HTTPException(status_code=404, detail="Bot não encontrado")
        
        if bot.owner_id != current_user.id and not current_user.is_superuser:
            raise HTTPException(status_code=403, detail="Acesso negado")
        
        group = db.query(BotGroup).filter(
            BotGroup.id == group_id,
            BotGroup.bot_id == bot_id
        ).first()
        
        if not group:
            raise HTTPException(status_code=404, detail="Grupo não encontrado")
        
        # Enriquecer com nomes dos planos
        planos = db.query(PlanoConfig).filter(PlanoConfig.bot_id == bot_id).all()
        planos_map = {p.id: p.nome_exibicao for p in planos}
        plan_ids = group.plan_ids or []
        plan_names = [planos_map.get(pid, f"Plano #{pid}") for pid in plan_ids]
        
        return {
            "id": group.id,
            "bot_id": group.bot_id,
            "title": group.title,
            "group_id": group.group_id,
            "link": group.link,
            "plan_ids": plan_ids,
            "plan_names": plan_names,
            "is_active": group.is_active,
            "created_at": group.created_at.isoformat() if group.created_at else None,
            "updated_at": group.updated_at.isoformat() if group.updated_at else None
        }
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"❌ Erro ao buscar grupo {group_id}: {e}")
        raise HTTPException(status_code=500, detail=str(e))


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
        # Converte datas (UTC → Brasília para gráfico correto)
        tz_br = timezone('America/Sao_Paulo')
        
        if start_date:
            try:
                start_utc = datetime.fromisoformat(start_date.replace('Z', '+00:00'))
                start = start_utc.astimezone(tz_br)
            except:
                # Fallback: interpreta como data local (Brasília)
                start = datetime.fromisoformat(start_date.split('.')[0])
            # 🔥 FIX: Garantir que start começa no INÍCIO do dia (00:00:00)
            start = start.replace(hour=0, minute=0, second=0, microsecond=0)
        else:
            start = now_brazil() - timedelta(days=30)
            start = start.replace(hour=0, minute=0, second=0, microsecond=0)
        
        if end_date:
            try:
                end_utc = datetime.fromisoformat(end_date.replace('Z', '+00:00'))
                end = end_utc.astimezone(tz_br)
            except:
                end = datetime.fromisoformat(end_date.split('.')[0])
            # 🔥 FIX: Garantir que end vai até o FINAL do dia (23:59:59)
            end = end.replace(hour=23, minute=59, second=59, microsecond=999999)
        else:
            end = now_brazil()
            end = end.replace(hour=23, minute=59, second=59, microsecond=999999)
        
        logger.info(f"📊 Dashboard Stats - Período: {start.date()} a {end.date()} (Brasília)")
        
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
                Pedido.status.in_(['approved', 'paid', 'active', 'expired']),
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
                Pedido.status.in_(['approved', 'paid', 'active', 'expired']),
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
        
        # Usuários ativos (assinaturas não expiradas OU vitalícios sem data de expiração)
        query_active = db.query(Pedido).filter(
            Pedido.status.in_(['approved', 'paid', 'active', 'expired']),
            or_(
                Pedido.data_expiracao > now_brazil(),
                Pedido.data_expiracao == None
            )
        )
        if not is_super_with_split or bot_id:
             if bots_ids: query_active = query_active.filter(Pedido.bot_id.in_(bots_ids))
        active_users = query_active.count()
        
        # Vendas de hoje
        hoje_start = now_brazil().replace(hour=0, minute=0, second=0)
        query_hoje = db.query(Pedido).filter(
            Pedido.status.in_(['approved', 'paid', 'active', 'expired']),
            Pedido.data_aprovacao >= hoje_start
        )
        if not is_super_with_split or bot_id:
            if bots_ids: query_hoje = query_hoje.filter(Pedido.bot_id.in_(bots_ids))
            
        vendas_hoje = query_hoje.all()
        
        if is_super_with_split and not bot_id:
            sales_today = len(vendas_hoje) * (current_user.taxa_venda or 60)
        else:
            sales_today = sum(int(p.valor * 100) if p.valor else 0 for p in vendas_hoje)
        
        # Leads do mês (para exibição)
        mes_start = now_brazil().replace(day=1, hour=0, minute=0, second=0)
        query_leads_mes = db.query(Lead).filter(Lead.created_at >= mes_start)
        if not is_super_with_split or bot_id:
             if bots_ids: query_leads_mes = query_leads_mes.filter(Lead.bot_id.in_(bots_ids))
        leads_mes = query_leads_mes.count()
        
        # Leads do período (mesma janela temporal das vendas - para taxa de conversão)
        query_leads_periodo = db.query(Lead).filter(
            Lead.created_at >= start,
            Lead.created_at <= end
        )
        if not is_super_with_split or bot_id:
             if bots_ids: query_leads_periodo = query_leads_periodo.filter(Lead.bot_id.in_(bots_ids))
        leads_periodo = query_leads_periodo.count()
        
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
        
        # Taxa de conversão (usa leads do MESMO período das vendas)
        if leads_periodo > 0:
            taxa_conversao = round((total_transacoes / leads_periodo) * 100, 2)
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
                Pedido.status.in_(['approved', 'paid', 'active', 'expired']),
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
# 📊 ESTATÍSTICAS AVANÇADAS (PÁGINA DEDICADA)
# =========================================================
@app.get("/api/admin/statistics")
def advanced_statistics(
    bot_id: Optional[int] = None,
    period: Optional[str] = "30d",  # 7d, 30d, 90d, all
    db: Session = Depends(get_db),
    current_user = Depends(get_current_user)
):
    """
    Estatísticas avançadas com métricas detalhadas.
    Receita, Ticket Médio, LTV, Vendas por Plano, 
    Taxa de Conversão, Picos de Desempenho.
    """
    try:
        tz_br = timezone('America/Sao_Paulo')
        agora = now_brazil()

        # --- PERÍODO ---
        if period == "7d":
            start = agora - timedelta(days=7)
        elif period == "90d":
            start = agora - timedelta(days=90)
        elif period == "all":
            start = datetime(2020, 1, 1, tzinfo=tz_br)
        else:
            start = agora - timedelta(days=30)
        
        end = agora

        # --- FILTRAR BOTS DO USUÁRIO ---
        is_super = current_user.is_superuser
        
        if bot_id:
            bots_ids = [bot_id]
        elif is_super:
            bots_ids = []  # Todos
        else:
            user_bots = db.query(BotModel.id).filter(BotModel.owner_id == current_user.id).all()
            bots_ids = [b.id for b in user_bots]

        if not is_super and not bots_ids:
            return _empty_statistics()

        # ============================================
        # 📦 QUERIES BASE
        # ============================================
        
        # 🔧 Helper: normalizar timezone para comparações seguras
        def _safe_tz(dt_val):
            if dt_val is None: return None
            if dt_val.tzinfo is None: return tz_br.localize(dt_val)
            return dt_val
        
        def apply_bot_filter(query):
            if bots_ids:
                return query.filter(Pedido.bot_id.in_(bots_ids))
            return query

        # Vendas aprovadas no período (🔥 CORRIGIDO: Inclui 'expired' para não sumir o faturamento)
        q_vendas = apply_bot_filter(
            db.query(Pedido).filter(
                Pedido.status.in_(['approved', 'paid', 'active', 'expired']),
                Pedido.data_aprovacao >= start,
                Pedido.data_aprovacao <= end
            )
        )
        vendas = q_vendas.all()

        # Vendas pendentes no período
        q_pendentes = apply_bot_filter(
            db.query(Pedido).filter(
                Pedido.status == 'pending',
                Pedido.created_at >= start,
                Pedido.created_at <= end
            )
        )
        pendentes = q_pendentes.all()

        # Todas as vendas aprovadas (para LTV) (🔥 CORRIGIDO: Inclui 'expired')
        q_all_vendas = apply_bot_filter(
            db.query(Pedido).filter(
                Pedido.status.in_(['approved', 'paid', 'active', 'expired'])
            )
        )
        todas_vendas = q_all_vendas.all()

        # Leads no período
        q_leads = db.query(Lead).filter(
            Lead.created_at >= start,
            Lead.created_at <= end
        )
        if bots_ids:
            q_leads = q_leads.filter(Lead.bot_id.in_(bots_ids))
        total_leads = q_leads.count()

        # Usuários ativos (assinatura não expirada OU vitalício sem data)
        q_ativos = apply_bot_filter(
            db.query(Pedido).filter(
                Pedido.status.in_(['approved', 'paid', 'active', 'expired']),
                or_(
                    Pedido.data_expiracao > agora,
                    Pedido.data_expiracao == None
                )
            )
        )
        total_ativos = q_ativos.count()

        # ============================================
        # 💰 MÉTRICAS PRINCIPAIS
        # ============================================
        is_super_split = is_super and current_user.pushin_pay_id
        taxa_centavos = current_user.taxa_venda or 60

        if is_super_split and not bot_id:
            receita_total = len(vendas) * taxa_centavos
            receita_pendentes = len(pendentes) * taxa_centavos
        else:
            receita_total = sum(int((p.valor or 0) * 100) for p in vendas)
            receita_pendentes = sum(int((p.valor or 0) * 100) for p in pendentes)

        total_vendas = len(vendas)
        total_pendentes = len(pendentes)
        total_geradas = total_vendas + total_pendentes

        # Ticket Médio
        ticket_medio = int(receita_total / total_vendas) if total_vendas > 0 else 0

        # LTV Médio (Receita Histórica / Usuários Únicos)
        if is_super_split and not bot_id:
            receita_historica = len(todas_vendas) * taxa_centavos
        else:
            receita_historica = sum(int((p.valor or 0) * 100) for p in todas_vendas)
        
        usuarios_unicos = len(set(p.telegram_id for p in todas_vendas if p.telegram_id))
        ltv_medio = int(receita_historica / usuarios_unicos) if usuarios_unicos > 0 else 0

        # Taxa de Conversão
        taxa_conversao = round((total_vendas / total_leads) * 100, 2) if total_leads > 0 else 0

        # ============================================
        # 📈 GRÁFICO: RECEITA POR DIA
        # ============================================
        chart_receita = []
        current_date = start
        while current_date <= end:
            day_start = current_date.replace(hour=0, minute=0, second=0, microsecond=0)
            day_end = current_date.replace(hour=23, minute=59, second=59, microsecond=999999)
            
            # Garante timezone-aware para comparação segura
            if day_start.tzinfo is None:
                day_start = tz_br.localize(day_start)
            if day_end.tzinfo is None:
                day_end = tz_br.localize(day_end)
            
            vendas_dia = []
            for v in vendas:
                if v.data_aprovacao:
                    da = v.data_aprovacao
                    # Normaliza para aware se necessário
                    if da.tzinfo is None:
                        da = tz_br.localize(da)
                    if day_start <= da <= day_end:
                        vendas_dia.append(v)
            
            if is_super_split and not bot_id:
                valor = round(len(vendas_dia) * (taxa_centavos / 100), 2)
            else:
                valor = round(sum((v.valor or 0) for v in vendas_dia), 2)
            
            chart_receita.append({
                "date": current_date.strftime("%d/%m"),
                "value": valor
            })
            current_date += timedelta(days=1)

        # ============================================
        # 🥇 TOP PLANOS MAIS VENDIDOS
        # ============================================
        planos_count = {}
        for v in vendas:
            nome = v.plano_nome or "Sem Plano"
            if nome not in planos_count:
                planos_count[nome] = {"count": 0, "revenue": 0}
            planos_count[nome]["count"] += 1
            if is_super_split and not bot_id:
                planos_count[nome]["revenue"] += taxa_centavos
            else:
                planos_count[nome]["revenue"] += int((v.valor or 0) * 100)

        top_planos = sorted(
            [{"name": k, "count": v["count"], "revenue": v["revenue"]} for k, v in planos_count.items()],
            key=lambda x: x["count"], reverse=True
        )[:10]

        # ============================================
        # 🕐 PICOS: HORÁRIOS COM MAIS VENDAS
        # ============================================
        horas_count = {}
        for v in vendas:
            if v.data_aprovacao:
                h = v.data_aprovacao.hour
                horas_count[h] = horas_count.get(h, 0) + 1
        
        top_horas = sorted(
            [{"hour": f"{h:02d}:00", "count": c} for h, c in horas_count.items()],
            key=lambda x: x["count"], reverse=True
        )[:5]

        # ============================================
        # 📅 PICOS: DIAS DA SEMANA COM MAIS VENDAS
        # ============================================
        dias_semana_map = {0: "Segunda", 1: "Terça", 2: "Quarta", 3: "Quinta", 4: "Sexta", 5: "Sábado", 6: "Domingo"}
        dias_count = {}
        for v in vendas:
            if v.data_aprovacao:
                d = v.data_aprovacao.weekday()
                dias_count[d] = dias_count.get(d, 0) + 1
        
        top_dias = sorted(
            [{"day": dias_semana_map.get(d, "?"), "count": c} for d, c in dias_count.items()],
            key=lambda x: x["count"], reverse=True
        )[:5]

        # ============================================
        # 📊 NOVAS MÉTRICAS AVANÇADAS (COMPLETAS)
        # ============================================
        
        # === MAPA DE COMPRADORES ===
        compradores_map = {}
        for v in todas_vendas:
            tid = v.telegram_id or 'unknown'
            compradores_map[tid] = compradores_map.get(tid, 0) + 1
        total_compradores = len(compradores_map)
        recorrentes = sum(1 for c in compradores_map.values() if c > 1)
        taxa_retencao = round((recorrentes / total_compradores) * 100, 1) if total_compradores > 0 else 0
        vendas_por_usuario = round(len(todas_vendas) / total_compradores, 1) if total_compradores > 0 else 0

        # Tempo Médio Retorno (dias entre recompras)
        tempo_retorno_dias = 0
        retorno_count = 0
        for tid, count in compradores_map.items():
            if count > 1:
                pedidos_user = sorted([v for v in todas_vendas if (v.telegram_id or '') == tid], key=lambda x: x.data_aprovacao or datetime(2020,1,1))
                for i in range(1, len(pedidos_user)):
                    if pedidos_user[i].data_aprovacao and pedidos_user[i-1].data_aprovacao:
                        try:
                            da_i = pedidos_user[i].data_aprovacao
                            da_prev = pedidos_user[i-1].data_aprovacao
                            if da_i.tzinfo is None: da_i = tz_br.localize(da_i)
                            if da_prev.tzinfo is None: da_prev = tz_br.localize(da_prev)
                            diff = (da_i - da_prev).days
                        except: diff = 0
                        tempo_retorno_dias += diff
                        retorno_count += 1
        avg_retorno = round(tempo_retorno_dias / retorno_count, 1) if retorno_count > 0 else 0

        vips_ativos = total_ativos

        # === 8 TAXAS DO CONCORRENTE ===
        
        # Taxa Upsell: pedidos com origem upsell / total vendas
        upsell_vendas = sum(1 for v in vendas if getattr(v, 'origem', '') == 'upsell')
        taxa_upsell = round((upsell_vendas / total_vendas) * 100, 1) if total_vendas > 0 else 0
        
        # Taxa Downsell: pedidos com origem downsell / total recusas (pendentes expirados)
        downsell_vendas = sum(1 for v in vendas if getattr(v, 'origem', '') == 'downsell')
        total_recusas = max(total_pendentes, 1)
        taxa_downsell = round((downsell_vendas / total_recusas) * 100, 1) if total_recusas > 0 else 0
        
        # Taxa OrderBump: pedidos com order bump / total checkouts
        orderbump_vendas = sum(1 for v in vendas if getattr(v, 'tem_order_bump', False))
        total_checkouts = total_geradas if total_geradas > 0 else 1
        taxa_orderbump = round((orderbump_vendas / total_checkouts) * 100, 1) if total_checkouts > 0 else 0
        
        # Taxa Recuperação: vendas de remarketing / total pendentes antigos
        remarketing_vendas = sum(1 for v in vendas if getattr(v, 'origem', '') == 'remarketing')
        taxa_recuperacao = round((remarketing_vendas / max(total_pendentes, 1)) * 100, 1) if total_pendentes > 0 else 0
        
        # Taxa Recorrência: compradores recorrentes / total compradores
        taxa_recorrencia = round((recorrentes / total_compradores) * 100, 1) if total_compradores > 0 else 0
        
        # Taxa Upgrade (= taxa_upsell but from buyers perspective)
        taxa_upgrade = round((upsell_vendas / total_compradores) * 100, 1) if total_compradores > 0 else 0
        
        # Taxa Abandono: ex-VIPs que não renovaram / total compradores
        expirados_count = 0
        expirados_tids = set()
        for v in todas_vendas:
            if v.status == 'expired' and v.data_expiracao:
                try:
                    de = _safe_tz(v.data_expiracao)
                    if de < agora:
                        expirados_count += 1
                        if v.telegram_id:
                            expirados_tids.add(v.telegram_id)
                except: pass
        expirados_unicos = len(expirados_tids)
        taxa_abandono = round((expirados_unicos / total_compradores) * 100, 1) if total_compradores > 0 else 0

        # === TEMPO MÉDIO /START → PAGAMENTO ===
        tempos_start_pag = []
        for v in vendas:
            pc = getattr(v, 'primeiro_contato', None)
            pa = v.data_aprovacao
            if pc and pa:
                try:
                    diff_sec = (_safe_tz(pa) - _safe_tz(pc)).total_seconds()
                    if 0 < diff_sec < 86400 * 30:
                        tempos_start_pag.append(diff_sec)
                except: pass
        
        if tempos_start_pag:
            avg_tempo_sec = sum(tempos_start_pag) / len(tempos_start_pag)
            tempo_medio = {
                "segundos": int(avg_tempo_sec % 60),
                "minutos": int((avg_tempo_sec // 60) % 60),
                "horas": int(avg_tempo_sec // 3600),
                "dataset": len(tempos_start_pag)
            }
        else:
            tempo_medio = {"segundos": 0, "minutos": 0, "horas": 0, "dataset": 0}

        # === CALENDÁRIO DE VENDAS (dias do mês atual) ===
        hoje = agora
        primeiro_dia_mes = hoje.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        if primeiro_dia_mes.tzinfo is None:
            primeiro_dia_mes = tz_br.localize(primeiro_dia_mes)
            
        import calendar as cal_module
        dias_no_mes = cal_module.monthrange(hoje.year, hoje.month)[1]
        
        calendario = []
        for dia_num in range(1, dias_no_mes + 1):
            dia_date = primeiro_dia_mes.replace(day=dia_num)
            dia_end = dia_date.replace(hour=23, minute=59, second=59)
            vendas_dia_cal = 0
            receita_dia_cal = 0
            for v in vendas:
                if v.data_aprovacao:
                    da = v.data_aprovacao
                    if da.tzinfo is None:
                        da = tz_br.localize(da)
                    if dia_date <= da <= dia_end:
                        vendas_dia_cal += 1
                        if is_super_split and not bot_id:
                            receita_dia_cal += taxa_centavos
                        else:
                            receita_dia_cal += int((v.valor or 0) * 100)
            calendario.append({
                "day": dia_num,
                "weekday": dia_date.weekday(),
                "vendas": vendas_dia_cal,
                "receita": receita_dia_cal,
                "is_today": dia_num == hoje.day,
            })

        # === CONTADORES EXPANDIDOS ===
        contadores_usuarios = {
            "total_compradores": total_compradores,
            "recorrentes": recorrentes,
            "vips_ativos": vips_ativos,
            "upsellers": sum(1 for tid in compradores_map if any(getattr(v, 'origem', '') == 'upsell' for v in todas_vendas if (v.telegram_id or '') == tid)),
            "downsellers": sum(1 for tid in compradores_map if any(getattr(v, 'origem', '') == 'downsell' for v in todas_vendas if (v.telegram_id or '') == tid)),
            "remarketing": sum(1 for tid in compradores_map if any(getattr(v, 'origem', '') == 'remarketing' for v in todas_vendas if (v.telegram_id or '') == tid)),
        }

        # === GRÁFICOS TEMPORAIS ===
        chart_horas = [{"hour": f"{h:02d}:00", "count": horas_count.get(h, 0)} for h in range(24)]
        
        chart_semana = [{"day": dias_semana_map.get(d, "?"), "day_short": dias_semana_map.get(d, "?")[:3], "count": dias_count.get(d, 0)} for d in range(7)]
        
        # === TOP 5 BOTS ===
        top_bots = []
        if not bot_id:
            bots_count = {}
            for v in vendas:
                bid = v.bot_id
                if bid:
                    if bid not in bots_count:
                        bot_obj = db.query(BotModel).filter(BotModel.id == bid).first()
                        bots_count[bid] = {"name": bot_obj.nome if bot_obj else f"Bot #{bid}", "count": 0, "revenue": 0}
                    bots_count[bid]["count"] += 1
                    if is_super_split:
                        bots_count[bid]["revenue"] += taxa_centavos
                    else:
                        bots_count[bid]["revenue"] += int((v.valor or 0) * 100)
            top_bots = sorted(bots_count.values(), key=lambda x: x["count"], reverse=True)[:5]

        # === DIÁRIO DE MUDANÇAS ===
        try:
            changelogs = db.query(ChangeLog).filter(
                ChangeLog.user_id == current_user.id
            ).order_by(desc(ChangeLog.date)).limit(20).all()
            diario = [{
                "id": cl.id,
                "date": cl.date.strftime("%d/%m/%Y") if cl.date else "",
                "category": cl.category,
                "content": cl.content,
            } for cl in changelogs]
        except:
            diario = []

        # === TOP TRACKING LINKS (Códigos de Venda) ===
        top_tracking = []
        try:
            tracking_count = {}
            for v in vendas:
                tid = getattr(v, 'tracking_id', None)
                if tid:
                    if tid not in tracking_count:
                        tlink = db.query(TrackingLink).filter(TrackingLink.id == tid).first()
                        tracking_count[tid] = {"name": tlink.slug if tlink else f"Link #{tid}", "count": 0, "revenue": 0}
                    tracking_count[tid]["count"] += 1
                    if is_super_split and not bot_id:
                        tracking_count[tid]["revenue"] += taxa_centavos
                    else:
                        tracking_count[tid]["revenue"] += int((v.valor or 0) * 100)
            top_tracking = sorted(tracking_count.values(), key=lambda x: x["revenue"], reverse=True)[:5]
        except: pass

        # === TOP CAMPANHAS DE REMARKETING ===
        top_campanhas = []
        try:
            campanha_count = {}
            for v in vendas:
                orig = getattr(v, 'origem', '') or ''
                if orig == 'remarketing':
                    rmk_id = getattr(v, 'total_remarketings', 0) or 0
                    key = f"Remarketing #{rmk_id}" if rmk_id else "Remarketing"
                    if key not in campanha_count:
                        campanha_count[key] = {"name": key, "count": 0}
                    campanha_count[key]["count"] += 1
            top_campanhas = sorted(campanha_count.values(), key=lambda x: x["count"], reverse=True)[:5]
        except: pass

        # 🍩 DONUT CONVERSÃO
        donut_conversao = {
            "convertidas": total_vendas,
            "pendentes": total_pendentes,
            "perdidas": max(0, total_leads - total_geradas)
        }

        # 🆕 CRESCIMENTO vs PERÍODO ANTERIOR
        crescimento = {}
        try:
            periodo_duracao = (end - start).days
            prev_start = start - timedelta(days=periodo_duracao)
            prev_end = start

            q_prev_vendas = apply_bot_filter(
                db.query(Pedido).filter(
                    Pedido.status.in_(['approved', 'paid', 'active', 'expired']),
                    Pedido.data_aprovacao >= prev_start,
                    Pedido.data_aprovacao <= prev_end
                )
            )
            prev_vendas = q_prev_vendas.all()
            
            if is_super_split and not bot_id:
                prev_receita = len(prev_vendas) * taxa_centavos
            else:
                prev_receita = sum(int((p.valor or 0) * 100) for p in prev_vendas)
            
            prev_total_vendas = len(prev_vendas)
            prev_ticket = int(prev_receita / prev_total_vendas) if prev_total_vendas > 0 else 0
            
            q_prev_leads = db.query(Lead).filter(Lead.created_at >= prev_start, Lead.created_at <= prev_end)
            if bots_ids:
                q_prev_leads = q_prev_leads.filter(Lead.bot_id.in_(bots_ids))
            prev_leads = q_prev_leads.count()

            q_prev_ativos = apply_bot_filter(
                db.query(Pedido).filter(
                    Pedido.status.in_(['approved', 'paid', 'active', 'expired']),
                    or_(Pedido.data_expiracao > prev_end, Pedido.data_expiracao == None)
                )
            )
            prev_ativos = q_prev_ativos.count()

            def calc_growth(current, previous):
                if previous == 0:
                    return 100.0 if current > 0 else 0.0
                return round(((current - previous) / previous) * 100, 1)

            prev_usu_unicos = len(set(p.telegram_id for p in prev_vendas if p.telegram_id))
            prev_ltv = int(prev_receita / prev_usu_unicos) if prev_usu_unicos > 0 else 0

            crescimento = {
                "receita": calc_growth(receita_total, prev_receita),
                "vendas": calc_growth(total_vendas, prev_total_vendas),
                "ticket_medio": calc_growth(ticket_medio, prev_ticket),
                "vips": calc_growth(total_ativos, prev_ativos),
                "ltv": calc_growth(ltv_medio, prev_ltv),
                "leads": calc_growth(total_leads, prev_leads),
            }
        except Exception as e_cresc:
            logger.warning(f"Erro crescimento: {e_cresc}")
            crescimento = {}

        # 🆕 FUNIL VISUAL
        funil_visual = {}
        try:
            funil_visual = {
                "starts": total_leads,
                "leads": total_leads,
                "checkouts": total_geradas,
                "vendas": total_vendas,
                "taxa_start_lead": round((total_leads / max(total_leads, 1)) * 100, 1) if total_leads > 0 else 0,
                "taxa_lead_venda": round((total_vendas / max(total_leads, 1)) * 100, 1) if total_leads > 0 else 0,
                "taxa_aprovacao": round((total_vendas / max(total_geradas, 1)) * 100, 1) if total_geradas > 0 else 0,
            }
        except:
            funil_visual = {"starts":0,"leads":0,"checkouts":0,"vendas":0,"taxa_start_lead":0,"taxa_lead_venda":0,"taxa_aprovacao":0}

        # 🆕 HEATMAP SEMANAL
        heatmap_semanal = []
        try:
            heat_map = {}
            for v in vendas:
                if v.data_aprovacao:
                    heat_map[(v.data_aprovacao.weekday(), v.data_aprovacao.hour)] = heat_map.get((v.data_aprovacao.weekday(), v.data_aprovacao.hour), 0) + 1
            for wd in range(7):
                for hr in range(24):
                    heatmap_semanal.append({"weekday": wd, "hour": hr, "count": heat_map.get((wd, hr), 0)})
        except:
            pass

        # 🆕 RECEITA POR GATEWAY
        receita_por_gateway = {"items": []}
        try:
            gw_map = {}
            for v in vendas:
                gw = getattr(v, 'gateway_usada', None) or 'desconhecido'
                if gw not in gw_map:
                    gw_map[gw] = {"name": gw, "count": 0, "receita": 0}
                gw_map[gw]["count"] += 1
                if is_super_split and not bot_id:
                    gw_map[gw]["receita"] += taxa_centavos
                else:
                    gw_map[gw]["receita"] += int((v.valor or 0) * 100)
            gw_list = sorted(gw_map.values(), key=lambda x: x["receita"], reverse=True)
            total_gw = sum(g["receita"] for g in gw_list) or 1
            for gw in gw_list:
                gw["pct"] = round((gw["receita"] / total_gw) * 100, 1)
            receita_por_gateway = {"items": gw_list, "total": total_gw}
        except:
            pass

        # 🆕 VENDAS POR ORIGEM
        vendas_por_origem = []
        try:
            origem_map = {}
            for v in vendas:
                orig = getattr(v, 'origem', 'bot') or 'bot'
                origem_map[orig] = origem_map.get(orig, 0) + 1
            labels = {'bot':'Bot Direto','upsell':'Upsell','downsell':'Downsell','remarketing':'Remarketing','order_bump':'Order Bump','canal_free':'Canal Free'}
            vendas_por_origem = [{"name": labels.get(k, k.replace('_',' ').title()), "value": v} for k, v in sorted(origem_map.items(), key=lambda x: x[1], reverse=True)]
        except:
            pass

        # 🆕 PROJEÇÃO MENSAL
        projecao_mensal = {"receita_projetada": 0, "vendas_projetadas": 0}
        try:
            import calendar as cal_mod
            dias_mes = cal_mod.monthrange(agora.year, agora.month)[1]
            dia_atual = agora.day
            inicio_mes = agora.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
            if inicio_mes.tzinfo is None:
                inicio_mes = tz_br.localize(inicio_mes)
            vendas_mes = [v for v in vendas if v.data_aprovacao and _safe_tz(v.data_aprovacao) >= inicio_mes]
            if is_super_split and not bot_id:
                receita_mes = len(vendas_mes) * taxa_centavos
            else:
                receita_mes = sum(int((v.valor or 0) * 100) for v in vendas_mes)
            if dia_atual > 0:
                projecao_mensal = {
                    "receita_projetada": int((receita_mes / dia_atual) * dias_mes),
                    "vendas_projetadas": int((len(vendas_mes) / dia_atual) * dias_mes),
                    "media_diaria_receita": int(receita_mes / dia_atual),
                    "media_diaria_vendas": round(len(vendas_mes) / dia_atual, 1),
                    "dias_restantes": dias_mes - dia_atual,
                }
        except:
            pass

        # 📊 RETORNO FINAL
        return {
            "metricas": {
                "receita_total": receita_total,
                "ticket_medio": ticket_medio,
                "total_usuarios": total_ativos,
                "ltv_medio": ltv_medio,
                "total_vendas": total_vendas,
                "total_pendentes": total_pendentes,
                "receita_pendentes": receita_pendentes,
                "total_geradas": total_geradas,
                "taxa_conversao": taxa_conversao,
                "total_leads": total_leads
            },
            "chart_receita": chart_receita,
            "top_planos": top_planos,
            "top_horas": top_horas,
            "top_dias": top_dias,
            "donut_conversao": donut_conversao,
            "chart_horas": chart_horas,
            "chart_semana": chart_semana,
            "top_bots": top_bots,
            "top_tracking": top_tracking,
            "top_campanhas": top_campanhas,
            "contadores_usuarios": contadores_usuarios,
            "metricas_avancadas": {
                "taxa_retencao": taxa_retencao,
                "vendas_por_usuario": vendas_por_usuario,
                "avg_retorno_dias": avg_retorno,
                "total_compradores": total_compradores,
                "recorrentes": recorrentes,
                "vips_ativos": vips_ativos,
                "taxa_upsell": taxa_upsell,
                "taxa_downsell": taxa_downsell,
                "taxa_orderbump": taxa_orderbump,
                "taxa_recuperacao": taxa_recuperacao,
                "taxa_recorrencia": taxa_recorrencia,
                "taxa_upgrade": taxa_upgrade,
                "taxa_abandono": taxa_abandono,
                "upsell_vendas": upsell_vendas,
                "downsell_vendas": downsell_vendas,
                "orderbump_vendas": orderbump_vendas,
                "remarketing_vendas": remarketing_vendas,
                "expirados_unicos": expirados_unicos,
            },
            "tempo_medio": tempo_medio,
            "calendario": calendario,
            "diario": diario,
            "crescimento": crescimento,
            "funil_visual": funil_visual,
            "heatmap_semanal": heatmap_semanal,
            "receita_por_gateway": receita_por_gateway,
            "vendas_por_origem": vendas_por_origem,
            "projecao_mensal": projecao_mensal,
            "periodo": {
                "inicio": start.strftime("%d/%m/%Y"),
                "fim": end.strftime("%d/%m/%Y"),
                "label": period
            }
        }

    except Exception as e:
        logger.error(f"Erro estatisticas: {e}")
        import traceback
        logger.error(traceback.format_exc())
        raise HTTPException(status_code=500, detail=f"Erro nas estatísticas: {str(e)}")

def _empty_statistics():
    return {
        "metricas": {"receita_total":0,"ticket_medio":0,"total_usuarios":0,"ltv_medio":0,"total_vendas":0,"total_pendentes":0,"receita_pendentes":0,"total_geradas":0,"taxa_conversao":0,"total_leads":0},
        "chart_receita":[],"top_planos":[],"top_horas":[],"top_dias":[],
        "donut_conversao":{"convertidas":0,"pendentes":0,"perdidas":0},
        "chart_horas":[],"chart_semana":[],"top_bots":[],
        "top_tracking":[],"top_campanhas":[],
        "contadores_usuarios":{"total_compradores":0,"recorrentes":0,"vips_ativos":0,"upsellers":0,"downsellers":0,"remarketing":0},
        "metricas_avancadas":{"taxa_retencao":0,"vendas_por_usuario":0,"avg_retorno_dias":0,"total_compradores":0,"recorrentes":0,"vips_ativos":0,"taxa_upsell":0,"taxa_downsell":0,"taxa_orderbump":0,"taxa_recuperacao":0,"taxa_recorrencia":0,"taxa_upgrade":0,"taxa_abandono":0,"upsell_vendas":0,"downsell_vendas":0,"orderbump_vendas":0,"remarketing_vendas":0,"expirados_unicos":0},
        "tempo_medio":{"segundos":0,"minutos":0,"horas":0,"dataset":0},
        "calendario":[],"diario":[],
        "crescimento":{},
        "funil_visual":{"starts":0,"leads":0,"checkouts":0,"vendas":0,"taxa_start_lead":0,"taxa_lead_venda":0,"taxa_aprovacao":0},
        "heatmap_semanal":[],
        "receita_por_gateway":{"items":[]},
        "vendas_por_origem":[],
        "projecao_mensal":{"receita_projetada":0,"vendas_projetadas":0},
        "periodo":{"inicio":"","fim":"","label":"30d"}
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
                    bot_db = db.query(BotModel).filter(BotModel.id == p.bot_id).first()
                    
                    if bot_db and bot_db.admin_principal_id:
                        # ✅ Buscar código de tracking se existir
                        tracking_info_site = ""
                        if p.tracking_id:
                            try:
                                tracking_link_site = db.query(TrackingLink).filter(TrackingLink.id == p.tracking_id).first()
                                if tracking_link_site and tracking_link_site.codigo:
                                    tracking_info_site = f"\n📊 Origem: <b>{tracking_link_site.codigo}</b>"
                            except:
                                pass
                        
                        msg_venda = (
                            f"💰 <b>VENDA APROVADA (SITE)!</b>\n\n"
                            f"👤 Cliente: {p.first_name}\n"
                            f"💎 Plano: {p.plano_nome}\n"
                            f"💵 Valor: R$ {p.valor:.2f}\n"
                            f"🆔 ID/User: {p.telegram_id}"
                            f"{tracking_info_site}\n"
                            f"🆔 ID Pagamento: <code>{p.transaction_id or p.txid or 'N/A'}</code>\n"
                            f"🕐 Data/Hora: {now_brazil().strftime('%d/%m/%Y %H:%M:%S')}"
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
# TRECHO 3: FUNÇÃO "enviar_passo_automatico" (CORRIGIDA + HTML)
# ============================================================

# ============================================================
# TRECHO 3: FUNÇÃO "enviar_passo_automatico" (CORRIGIDA COMPLETA)
# ============================================================

def enviar_passo_automatico(bot_temp, chat_id, passo, bot_db, db):
    """
    Envia um passo automaticamente e gerencia auto-destruição e próximo passo.
    """
    logger.info(f"✅ [BOT {bot_db.id}] Enviando passo {passo.step_order}: {passo.msg_texto[:30]}...")
    
    # 1. Verifica se existe passo seguinte
    passo_seguinte = db.query(BotFlowStep).filter(
        BotFlowStep.bot_id == bot_db.id, 
        BotFlowStep.step_order == passo.step_order + 1
    ).first()
    
    # 2. Define o callback do botão
    if passo_seguinte:
        next_callback = f"next_step_{passo.step_order}"
    else:
        next_callback = "go_checkout"
    
    # 3. Cria botão (se configurado)
    markup_step = types.InlineKeyboardMarkup()
    if passo.mostrar_botao:
        markup_step.add(types.InlineKeyboardButton(
            text=passo.btn_texto, 
            callback_data=next_callback
        ))
    
    # 4. Envia a mensagem
    sent_msg = None
    try:
        # Tenta enviar Mídia
        if passo.msg_media:
            try:
                media_low_pa = passo.msg_media.lower()
                if media_low_pa.endswith(('.mp4', '.mov', '.avi')):
                    sent_msg = bot_temp.send_video(
                        chat_id, passo.msg_media, caption=passo.msg_texto, 
                        reply_markup=markup_step if passo.mostrar_botao else None,
                        parse_mode="HTML"
                    )
                elif is_audio_file(passo.msg_media):
                    # 🔊 ÁUDIO: Envia sozinho sem caption/markup
                    audio_msgs = enviar_audio_inteligente(
                        bot_temp, chat_id, passo.msg_media,
                        texto=passo.msg_texto if passo.msg_texto and passo.msg_texto.strip() else None,
                        markup=markup_step if passo.mostrar_botao else None,
                        delay_pos_audio=2
                    )
                    sent_msg = audio_msgs[-1] if audio_msgs else None
                else:
                    sent_msg = bot_temp.send_photo(
                        chat_id, passo.msg_media, caption=passo.msg_texto, 
                        reply_markup=markup_step if passo.mostrar_botao else None,
                        parse_mode="HTML"
                    )
            except Exception as e_media:
                logger.error(f"Erro mídia passo auto: {e_media}")
                # Fallback para texto
                sent_msg = bot_temp.send_message(
                    chat_id, passo.msg_texto, 
                    reply_markup=markup_step if passo.mostrar_botao else None,
                    parse_mode="HTML"
                )
        else:
            # Envia Apenas Texto
            sent_msg = bot_temp.send_message(
                chat_id, passo.msg_texto, 
                reply_markup=markup_step if passo.mostrar_botao else None,
                parse_mode="HTML"
            )

        # ==============================================================================
        # 🔥 CORREÇÃO MESTRE: AUTO-DESTRUIÇÃO DESACOPLADA
        # Agora roda INDEPENDENTE se tem botão, se tem delay ou se é o último passo.
        # ==============================================================================
        if sent_msg and passo.autodestruir:
            # Se tiver delay configurado no passo, usa ele. Se não, usa 10s padrão de leitura.
            tempo_vida = passo.delay_seconds if passo.delay_seconds > 0 else 10
            logger.info(f"💣 Agendando destruição do passo {passo.step_order} para daqui {tempo_vida}s")
            agendar_destruicao_msg(bot_temp, chat_id, sent_msg.message_id, tempo_vida)

        # 5. Lógica de Navegação Automática (Recursividade)
        # Se NÃO tem botão, o bot deve chamar o próximo passo sozinho após o delay
        if not passo.mostrar_botao:
            delay = passo.delay_seconds if passo.delay_seconds > 0 else 0
            
            if delay > 0:
                time.sleep(delay) # Espera o tempo antes de enviar o PRÓXIMO

            if passo_seguinte:
                enviar_passo_automatico(bot_temp, chat_id, passo_seguinte, bot_db, db)
            else:
                # Fim da linha -> Oferta Final
                enviar_oferta_final(bot_temp, chat_id, bot_db.fluxo, bot_db.id, db)
            
    except Exception as e:
        logger.error(f"❌ [BOT {bot_db.id}] Erro crítico passo automático: {e}")
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
            # 1. Conta TODAS as vendas aprovadas da PLATAFORMA INTEIRA (🔥 CORRIGIDO)
            total_vendas_sistema = db.query(Pedido).filter(
                Pedido.status.in_(['approved', 'paid', 'active', 'expired'])
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

            # Soma pedidos aprovados dos bots do usuário (🔥 CORRIGIDO: Inclui 'expired')
            pedidos_aprovados = db.query(Pedido).filter(
                Pedido.bot_id.in_(bots_ids),
                Pedido.status.in_(['approved', 'paid', 'active', 'expired'])
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
        # 🔥 FIX: Incluir TODOS os status de vendas aprovadas (não apenas 'approved')
        total_sales = db.query(Pedido).filter(
            Pedido.bot_id.in_(bot_ids), 
            Pedido.status.in_(['approved', 'paid', 'active', 'expired'])
        ).count()

        total_revenue = db.query(func.sum(Pedido.valor)).filter(
            Pedido.bot_id.in_(bot_ids), 
            Pedido.status.in_(['approved', 'paid', 'active', 'expired'])
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
# 🔒 ALTERAR SENHA DO USUÁRIO
# =========================================================
@app.post("/api/admin/profile/change-password")
def change_password(
    data: ChangePasswordRequest, 
    db: Session = Depends(get_db), 
    current_user = Depends(get_current_user)
):
    """
    Permite ao usuário alterar sua própria senha.
    Requer: senha atual válida + nova senha com confirmação.
    """
    try:
        from database import User
        
        # 1. Validar que nova senha e confirmação são iguais
        if data.new_password != data.confirm_password:
            raise HTTPException(status_code=400, detail="A nova senha e a confirmação não coincidem.")
        
        # 2. Validar tamanho mínimo da nova senha
        if len(data.new_password) < 6:
            raise HTTPException(status_code=400, detail="A nova senha deve ter pelo menos 6 caracteres.")
        
        # 3. Buscar o usuário no banco
        user = db.query(User).filter(User.id == current_user.id).first()
        if not user:
            raise HTTPException(status_code=404, detail="Usuário não encontrado.")
        
        # 4. Verificar se a senha atual está correta
        if not verify_password(data.current_password, user.password_hash):
            raise HTTPException(status_code=401, detail="Senha atual incorreta.")
        
        # 5. Verificar se a nova senha é diferente da atual
        if verify_password(data.new_password, user.password_hash):
            raise HTTPException(status_code=400, detail="A nova senha não pode ser igual à senha atual.")
        
        # 6. Gerar hash e salvar nova senha
        user.password_hash = get_password_hash(data.new_password)
        db.commit()
        
        # 7. Log de auditoria
        log_action(
            db=db, user_id=user.id, username=user.username, 
            action="password_changed", resource_type="auth",
            description="Senha alterada com sucesso pelo próprio usuário"
        )
        
        logger.info(f"🔒 Senha alterada com sucesso para o usuário: {user.username}")
        
        return {"status": "success", "msg": "Senha alterada com sucesso!"}
        
    except HTTPException:
        raise  # Re-lança exceções HTTP sem modificar
    except Exception as e:
        db.rollback()
        logger.error(f"❌ Erro ao alterar senha: {e}")
        raise HTTPException(status_code=500, detail="Erro interno ao alterar senha.")

# =========================================================
# 👤 ALTERAR USERNAME DO USUÁRIO
# =========================================================
@app.post("/api/admin/profile/change-username")
def change_username(
    data: ChangeUsernameRequest, 
    db: Session = Depends(get_db), 
    current_user = Depends(get_current_user)
):
    """
    Permite ao usuário alterar seu username.
    Verifica se o novo username já está em uso por outro usuário.
    Retorna um novo JWT com o username atualizado.
    """
    try:
        from database import User
        
        new_username = data.new_username.strip()
        
        # 1. Validar tamanho do username
        if len(new_username) < 3:
            raise HTTPException(status_code=400, detail="O username deve ter pelo menos 3 caracteres.")
        
        if len(new_username) > 30:
            raise HTTPException(status_code=400, detail="O username deve ter no máximo 30 caracteres.")
        
        # 2. Validar caracteres permitidos (letras, números, underscore, ponto)
        import re
        if not re.match(r'^[a-zA-Z0-9_.]+$', new_username):
            raise HTTPException(status_code=400, detail="O username pode conter apenas letras, números, underscore (_) e ponto (.).")
        
        # 3. Buscar o usuário atual
        user = db.query(User).filter(User.id == current_user.id).first()
        if not user:
            raise HTTPException(status_code=404, detail="Usuário não encontrado.")
        
        # 4. Verificar se o novo username é igual ao atual
        if user.username == new_username:
            raise HTTPException(status_code=400, detail="O novo username é igual ao atual.")
        
        # 5. Verificar se o username já está em uso por outro usuário
        existing_user = db.query(User).filter(
            User.username == new_username,
            User.id != current_user.id
        ).first()
        
        if existing_user:
            raise HTTPException(
                status_code=409, 
                detail=f"O username '{new_username}' já está em uso. Escolha outro."
            )
        
        # 6. Salvar o antigo username para log
        old_username = user.username
        
        # 7. Atualizar o username
        user.username = new_username
        db.commit()
        
        # 8. Gerar novo token JWT com o username atualizado
        access_token_expires = timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES)
        new_token = create_access_token(
            data={"sub": new_username, "user_id": user.id},
            expires_delta=access_token_expires
        )
        
        # 9. Log de auditoria
        log_action(
            db=db, user_id=user.id, username=new_username,
            action="username_changed", resource_type="auth",
            description=f"Username alterado de '{old_username}' para '{new_username}'"
        )
        
        logger.info(f"👤 Username alterado: {old_username} → {new_username}")
        
        return {
            "status": "success", 
            "msg": f"Username alterado para '{new_username}'!",
            "new_username": new_username,
            "new_token": new_token  # Frontend deve salvar este novo token
        }
        
    except HTTPException:
        raise
    except Exception as e:
        db.rollback()
        logger.error(f"❌ Erro ao alterar username: {e}")
        raise HTTPException(status_code=500, detail="Erro interno ao alterar username.")

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
            Pedido.status.in_(['approved', 'paid', 'active', 'expired'])
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
        thirty_days_ago = now_brazil() - timedelta(days=30)
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
    if user_data.wiinpay_user_id is not None:
        user.wiinpay_user_id = user_data.wiinpay_user_id
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

# =========================================================
# 🤖 SUPER ADMIN - GERENCIAMENTO DE BOTS (NOVO)
# =========================================================

@app.get("/api/superadmin/bots")
def list_all_bots_system(
    page: int = 1,
    per_page: int = 50,
    search: str = None,
    status: str = None,
    db: Session = Depends(get_db),
    current_superuser = Depends(get_current_superuser)
):
    """
    Lista TODOS os bots do sistema (visão global).
    Permite filtrar por nome do bot, username do bot ou username do DONO.
    """
    try:
        if per_page > 100: per_page = 100
        
        # Query base com JOIN no dono
        query = db.query(BotModel).outerjoin(User, BotModel.owner_id == User.id)
        
        # Filtro de Busca
        if search:
            search_term = f"%{search}%"
            query = query.filter(
                (BotModel.nome.ilike(search_term)) |
                (BotModel.username.ilike(search_term)) |
                (User.username.ilike(search_term)) |
                (User.email.ilike(search_term))
            )
        
        # Filtro de Status
        if status and status != "todos":
            query = query.filter(BotModel.status == status)
            
        total = query.count()
        bots = query.order_by(BotModel.created_at.desc()).offset((page - 1) * per_page).limit(per_page).all()
        
        bots_data = []
        for bot in bots:
            receita = db.query(func.sum(Pedido.valor)).filter(
                Pedido.bot_id == bot.id,
                Pedido.status.in_(['approved', 'paid', 'active', 'expired'])
            ).scalar() or 0.0
            
            vendas = db.query(Pedido).filter(
                Pedido.bot_id == bot.id,
                Pedido.status.in_(['approved', 'paid', 'active', 'expired'])
            ).count()
            
            leads = db.query(Lead).filter(Lead.bot_id == bot.id).count()
            
            dono = bot.owner if hasattr(bot, 'owner') and bot.owner else None
            
            bots_data.append({
                "id": bot.id,
                "nome": bot.nome,
                "username": bot.username,
                "status": bot.status,
                "created_at": str(bot.created_at) if bot.created_at else None,
                "owner": {
                    "id": dono.id if dono else None,
                    "username": dono.username if dono else "Sem dono",
                    "email": dono.email if dono else None
                },
                "stats": {
                    "receita": round(float(receita), 2),
                    "vendas": vendas,
                    "leads": leads
                }
            })
        
        return {
            "bots": bots_data,
            "total": total,
            "page": page,
            "per_page": per_page,
            "total_pages": (total + per_page - 1) // per_page
        }
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"❌ Erro ao listar bots (superadmin): {e}")
        raise HTTPException(500, str(e))


@app.delete("/api/superadmin/bots/{bot_id}")
def delete_bot_force(
    bot_id: int,
    request: Request,
    db: Session = Depends(get_db),
    current_superuser = Depends(get_current_superuser)
):
    """Super Admin deleta qualquer bot (forçado)."""
    try:
        bot = db.query(BotModel).filter(BotModel.id == bot_id).first()
        if not bot:
            raise HTTPException(status_code=404, detail="Bot não encontrado")
            
        nome_bot = bot.nome
        dono = bot.owner.username if hasattr(bot, 'owner') and bot.owner else "Desconhecido"
        
        db.delete(bot)
        db.commit()
        
        # Log de Auditoria
        try:
            log = AuditLog(
                user_id=current_superuser.id,
                username=current_superuser.username,
                action="bot_deleted_force",
                resource_type="bot",
                resource_id=bot_id,
                description=f"Super Admin deletou bot '{nome_bot}' do usuário '{dono}'",
                success=True
            )
            db.add(log)
            db.commit()
        except: pass
                    
        return {"message": f"Bot '{nome_bot}' deletado com sucesso"}
        
    except HTTPException:
        raise
    except Exception as e:
        db.rollback()
        logger.error(f"❌ Erro ao deletar bot forçado: {e}")
        raise HTTPException(status_code=500, detail="Erro ao deletar bot")


@app.post("/api/superadmin/impersonate/{user_id}")
def impersonate_user(
    user_id: int,
    db: Session = Depends(get_db),
    current_superuser = Depends(get_current_superuser)
):
    """
    Gera um token válido para acessar a conta de QUALQUER usuário.
    Apenas SUPER_ADMIN pode fazer isso.
    """
    try:
        target_user = db.query(User).filter(User.id == user_id).first()
        if not target_user:
            raise HTTPException(status_code=404, detail="Usuário alvo não encontrado")
            
        # Gera token para o alvo
        access_token = create_access_token(
            data={
                "sub": target_user.username, 
                "user_id": target_user.id
            }
        )
        
        has_bots = len(target_user.bots) > 0 if hasattr(target_user, 'bots') else False
        
        logger.warning(f"🕵️ IMPERSONATION: {current_superuser.username} entrou na conta de {target_user.username}")
        
        # Log de Auditoria
        try:
            log = AuditLog(
                user_id=current_superuser.id,
                username=current_superuser.username,
                action="impersonate_user",
                resource_type="user",
                resource_id=user_id,
                description=f"Impersonou usuário '{target_user.username}'",
                success=True
            )
            db.add(log)
            db.commit()
        except: pass
        
        return {
            "access_token": access_token,
            "token_type": "bearer",
            "user_id": target_user.id,
            "username": target_user.username,
            "has_bots": has_bots,
            "is_impersonation": True
        }
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"❌ Erro impersonation: {e}")
        raise HTTPException(500, str(e))


# =========================================================
# ✨ SCHEMAS: EMOJIS PREMIUM
# =========================================================
class PremiumEmojiPackCreate(BaseModel):
    name: str
    icon: Optional[str] = "📦"
    description: Optional[str] = None
    sort_order: int = 0

class PremiumEmojiPackUpdate(BaseModel):
    name: Optional[str] = None
    icon: Optional[str] = None
    description: Optional[str] = None
    sort_order: Optional[int] = None
    is_active: Optional[bool] = None

class PremiumEmojiCreate(BaseModel):
    emoji_id: str
    fallback: str
    name: str
    shortcode: str
    pack_id: Optional[int] = None
    sort_order: int = 0
    emoji_type: str = "static"
    thumbnail_url: Optional[str] = None

class PremiumEmojiBulkCreate(BaseModel):
    emojis: List[PremiumEmojiCreate]

class PremiumEmojiUpdate(BaseModel):
    emoji_id: Optional[str] = None
    fallback: Optional[str] = None
    name: Optional[str] = None
    shortcode: Optional[str] = None
    pack_id: Optional[int] = None
    sort_order: Optional[int] = None
    emoji_type: Optional[str] = None
    thumbnail_url: Optional[str] = None
    is_active: Optional[bool] = None

# =========================================================
# ✨ EMOJIS PREMIUM - ROTAS SUPER ADMIN (CRUD COMPLETO)
# =========================================================

# ===================== PACOTES (CATEGORIAS) =====================

@app.get("/api/superadmin/premium-emojis/packs")
def list_premium_emoji_packs(
    db: Session = Depends(get_db),
    current_superuser = Depends(get_current_superuser)
):
    """Lista todos os pacotes de emojis premium (Super Admin)."""
    packs = db.query(PremiumEmojiPack).order_by(PremiumEmojiPack.sort_order.asc(), PremiumEmojiPack.id.asc()).all()
    return [{
        "id": p.id,
        "name": p.name,
        "icon": p.icon,
        "description": p.description,
        "sort_order": p.sort_order,
        "is_active": p.is_active,
        "emoji_count": len(p.emojis) if p.emojis else 0,
        "created_at": str(p.created_at) if p.created_at else None
    } for p in packs]


@app.post("/api/superadmin/premium-emojis/packs")
def create_premium_emoji_pack(
    pack_data: PremiumEmojiPackCreate,
    db: Session = Depends(get_db),
    current_superuser = Depends(get_current_superuser)
):
    """Cria um novo pacote de emojis premium."""
    # Verifica duplicidade de nome
    existing = db.query(PremiumEmojiPack).filter(PremiumEmojiPack.name == pack_data.name).first()
    if existing:
        raise HTTPException(400, f"Já existe um pacote com o nome '{pack_data.name}'")
    
    pack = PremiumEmojiPack(
        name=pack_data.name,
        icon=pack_data.icon,
        description=pack_data.description,
        sort_order=pack_data.sort_order
    )
    db.add(pack)
    db.commit()
    db.refresh(pack)
    
    logger.info(f"✨ [PREMIUM EMOJI] Pack '{pack.name}' criado por {current_superuser.username}")
    return {"message": f"Pacote '{pack.name}' criado com sucesso", "id": pack.id}


@app.put("/api/superadmin/premium-emojis/packs/{pack_id}")
def update_premium_emoji_pack(
    pack_id: int,
    pack_data: PremiumEmojiPackUpdate,
    db: Session = Depends(get_db),
    current_superuser = Depends(get_current_superuser)
):
    """Atualiza um pacote de emojis premium."""
    pack = db.query(PremiumEmojiPack).filter(PremiumEmojiPack.id == pack_id).first()
    if not pack:
        raise HTTPException(404, "Pacote não encontrado")
    
    for field, value in pack_data.dict(exclude_unset=True).items():
        if value is not None:
            setattr(pack, field, value)
    
    db.commit()
    invalidate_premium_emoji_cache()
    return {"message": f"Pacote '{pack.name}' atualizado com sucesso"}


@app.delete("/api/superadmin/premium-emojis/packs/{pack_id}")
def delete_premium_emoji_pack(
    pack_id: int,
    db: Session = Depends(get_db),
    current_superuser = Depends(get_current_superuser)
):
    """Deleta um pacote e todos seus emojis."""
    pack = db.query(PremiumEmojiPack).filter(PremiumEmojiPack.id == pack_id).first()
    if not pack:
        raise HTTPException(404, "Pacote não encontrado")
    
    nome = pack.name
    db.delete(pack)
    db.commit()
    invalidate_premium_emoji_cache()
    
    logger.info(f"🗑️ [PREMIUM EMOJI] Pack '{nome}' deletado por {current_superuser.username}")
    return {"message": f"Pacote '{nome}' e todos seus emojis foram deletados"}


# ===================== EMOJIS INDIVIDUAIS =====================

@app.get("/api/superadmin/premium-emojis")
def list_premium_emojis_admin(
    pack_id: Optional[int] = None,
    search: Optional[str] = None,
    page: int = 1,
    per_page: int = 100,
    db: Session = Depends(get_db),
    current_superuser = Depends(get_current_superuser)
):
    """Lista todos os emojis premium com filtros (Super Admin)."""
    query = db.query(PremiumEmoji)
    
    if pack_id:
        query = query.filter(PremiumEmoji.pack_id == pack_id)
    if search:
        query = query.filter(
            or_(
                PremiumEmoji.name.ilike(f"%{search}%"),
                PremiumEmoji.shortcode.ilike(f"%{search}%"),
                PremiumEmoji.fallback.ilike(f"%{search}%")
            )
        )
    
    total = query.count()
    emojis = query.order_by(PremiumEmoji.pack_id.asc(), PremiumEmoji.sort_order.asc()) \
                  .offset((page - 1) * per_page) \
                  .limit(per_page) \
                  .all()
    
    return {
        "total": total,
        "page": page,
        "per_page": per_page,
        "emojis": [{
            "id": e.id,
            "emoji_id": e.emoji_id,
            "fallback": e.fallback,
            "name": e.name,
            "shortcode": e.shortcode,
            "pack_id": e.pack_id,
            "pack_name": e.pack.name if e.pack else None,
            "sort_order": e.sort_order,
            "emoji_type": e.emoji_type,
            "thumbnail_url": e.thumbnail_url,
            "is_active": e.is_active,
            "html_tag": e.to_html_tag(),
            "created_at": str(e.created_at) if e.created_at else None
        } for e in emojis]
    }


@app.post("/api/superadmin/premium-emojis")
def create_premium_emoji(
    emoji_data: PremiumEmojiCreate,
    db: Session = Depends(get_db),
    current_superuser = Depends(get_current_superuser)
):
    """Cadastra um novo emoji premium no catálogo."""
    # Sanitiza shortcode (garante formato :xxx:)
    shortcode = emoji_data.shortcode.strip()
    if not shortcode.startswith(':'):
        shortcode = ':' + shortcode
    if not shortcode.endswith(':'):
        shortcode = shortcode + ':'
    
    # Verifica duplicidade
    existing = db.query(PremiumEmoji).filter(
        or_(
            PremiumEmoji.emoji_id == emoji_data.emoji_id,
            PremiumEmoji.shortcode == shortcode
        )
    ).first()
    if existing:
        if existing.emoji_id == emoji_data.emoji_id:
            raise HTTPException(400, f"Emoji com ID '{emoji_data.emoji_id}' já está cadastrado")
        raise HTTPException(400, f"Shortcode '{shortcode}' já está em uso")
    
    # Valida se o pack existe (se informado)
    if emoji_data.pack_id:
        pack = db.query(PremiumEmojiPack).filter(PremiumEmojiPack.id == emoji_data.pack_id).first()
        if not pack:
            raise HTTPException(404, f"Pacote com ID {emoji_data.pack_id} não encontrado")
    
    emoji = PremiumEmoji(
        emoji_id=emoji_data.emoji_id,
        fallback=emoji_data.fallback,
        name=emoji_data.name,
        shortcode=shortcode,
        pack_id=emoji_data.pack_id,
        sort_order=emoji_data.sort_order,
        emoji_type=emoji_data.emoji_type,
        thumbnail_url=emoji_data.thumbnail_url
    )
    db.add(emoji)
    db.commit()
    db.refresh(emoji)
    invalidate_premium_emoji_cache()
    
    logger.info(f"✨ [PREMIUM EMOJI] Emoji '{emoji.name}' ({shortcode}) cadastrado por {current_superuser.username}")
    return {
        "message": f"Emoji '{emoji.name}' cadastrado com sucesso",
        "id": emoji.id,
        "shortcode": shortcode,
        "html_tag": emoji.to_html_tag()
    }


@app.post("/api/superadmin/premium-emojis/bulk")
def bulk_create_premium_emojis(
    bulk_data: PremiumEmojiBulkCreate,
    db: Session = Depends(get_db),
    current_superuser = Depends(get_current_superuser)
):
    """Cadastra vários emojis premium de uma vez."""
    created = 0
    skipped = 0
    errors = []
    
    for emoji_data in bulk_data.emojis:
        try:
            shortcode = emoji_data.shortcode.strip()
            if not shortcode.startswith(':'):
                shortcode = ':' + shortcode
            if not shortcode.endswith(':'):
                shortcode = shortcode + ':'
            
            existing = db.query(PremiumEmoji).filter(
                or_(
                    PremiumEmoji.emoji_id == emoji_data.emoji_id,
                    PremiumEmoji.shortcode == shortcode
                )
            ).first()
            
            if existing:
                skipped += 1
                continue
            
            emoji = PremiumEmoji(
                emoji_id=emoji_data.emoji_id,
                fallback=emoji_data.fallback,
                name=emoji_data.name,
                shortcode=shortcode,
                pack_id=emoji_data.pack_id,
                sort_order=emoji_data.sort_order,
                emoji_type=emoji_data.emoji_type,
                thumbnail_url=emoji_data.thumbnail_url
            )
            db.add(emoji)
            created += 1
        except Exception as e:
            skipped += 1
            errors.append(f"{emoji_data.name}: {str(e)}")
    
    db.commit()
    invalidate_premium_emoji_cache()
    
    logger.info(f"✨ [PREMIUM EMOJI] Bulk: {created} criados, {skipped} ignorados por {current_superuser.username}")
    return {
        "message": f"{created} emojis cadastrados, {skipped} ignorados",
        "created": created,
        "skipped": skipped,
        "errors": errors[:10] if errors else []
    }


# ===================== IMPORTAR PACK COMPLETO DO TELEGRAM =====================

class ImportPackRequest(BaseModel):
    pack_link: str                  # Ex: "https://t.me/addemoji/DecorationEmojiPack" ou apenas "DecorationEmojiPack"
    pack_name: Optional[str] = None # Nome customizado para o pacote (se None, usa o título do Telegram)
    pack_icon: Optional[str] = "📦"
    auto_create_pack: bool = True   # Cria o pacote automaticamente no sistema

@app.post("/api/superadmin/premium-emojis/import-pack")
def import_emoji_pack_from_telegram(
    req: ImportPackRequest,
    db: Session = Depends(get_db),
    current_superuser = Depends(get_current_superuser)
):
    """
    Importa um pack COMPLETO de emojis premium do Telegram.
    Uusa a Bot API (getStickerSet) para buscar todos os emojis do pack.
    Aceita link (https://t.me/addemoji/NomeDoPack) ou shortname direto.
    """
    import re as _re
    
    # 1. Extrair shortname do link
    raw = req.pack_link.strip()
    # Suporta: https://t.me/addemoji/NomeDoPack, t.me/addemoji/NomeDoPack, ou apenas NomeDoPack
    match = _re.search(r'(?:t\.me/addemoji/|^)([A-Za-z0-9_]+)$', raw.split('?')[0].rstrip('/'))
    if not match:
        raise HTTPException(status_code=400, detail="Link inválido. Use formato: https://t.me/addemoji/NomeDoPack ou apenas o nome do pack.")
    
    short_name = match.group(1)
    logger.info(f"✨ [IMPORT PACK] Importando pack '{short_name}' por {current_superuser.username}")
    
    # 2. Buscar um bot ativo no sistema para usar a Bot API
    bot_for_api = db.query(BotModel).filter(BotModel.status == "ativo").first()
    if not bot_for_api:
        raise HTTPException(status_code=400, detail="Nenhum bot ativo encontrado no sistema para consultar a API do Telegram.")
    
    bot_token = bot_for_api.token
    
    # 3. Chamar getStickerSet via Bot API
    try:
        import httpx as _httpx
        api_url = f"https://api.telegram.org/bot{bot_token}/getStickerSet"
        logger.info(f"✨ [IMPORT PACK] Chamando getStickerSet name={short_name}")
        
        resp = _httpx.get(
            api_url,
            params={"name": short_name},
            timeout=15
        )
        data = resp.json()
        
        logger.info(f"✨ [IMPORT PACK] Resposta Telegram ok={data.get('ok')}, keys={list(data.get('result', {}).keys()) if data.get('ok') else 'N/A'}")
        
        if not data.get("ok"):
            error_desc = data.get("description", "Pack não encontrado")
            logger.error(f"❌ [IMPORT PACK] Telegram retornou erro: {error_desc}")
            raise HTTPException(status_code=400, detail=f"Erro do Telegram: {error_desc}")
        
        sticker_set = data["result"]
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"❌ [IMPORT PACK] Erro ao buscar pack: {e}")
        raise HTTPException(status_code=500, detail=f"Erro ao comunicar com Telegram: {str(e)}")
    
    pack_title = sticker_set.get("title", short_name)
    sticker_type = sticker_set.get("sticker_type", "unknown")
    stickers = sticker_set.get("stickers", [])
    
    logger.info(f"✨ [IMPORT PACK] Pack '{pack_title}', type={sticker_type}, stickers={len(stickers)}")
    
    if not stickers:
        raise HTTPException(status_code=400, detail="Pack está vazio ou não contém emojis.")
    
    # Log do primeiro sticker para debug
    if stickers:
        first = stickers[0]
        logger.info(f"✨ [IMPORT PACK] Primeiro sticker keys: {list(first.keys())}")
        logger.info(f"✨ [IMPORT PACK] custom_emoji_id={first.get('custom_emoji_id', 'AUSENTE')}, emoji={first.get('emoji', 'N/A')}, type={first.get('type', 'N/A')}")
    
    # 4. Filtrar apenas custom emojis (que têm custom_emoji_id)
    custom_emojis = []
    for s in stickers:
        eid = s.get("custom_emoji_id")
        if eid:
            fallback = s.get("emoji", "⭐")
            custom_emojis.append({
                "emoji_id": str(eid),
                "fallback": fallback,
                "sticker_type": s.get("type", "custom_emoji")
            })
    
    logger.info(f"✨ [IMPORT PACK] {len(custom_emojis)} de {len(stickers)} stickers têm custom_emoji_id")
    
    if not custom_emojis:
        # Se nenhum tem custom_emoji_id, retorna erro detalhado com sample
        sample_keys = list(stickers[0].keys()) if stickers else []
        raise HTTPException(
            status_code=400, 
            detail=f"Este pack ({sticker_type}) não retornou custom_emoji_id. Tipo: {sticker_type}. Keys do sticker: {sample_keys}. Se for um pack de emojis premium, o bot pode não ter permissão."
        )
    
    logger.info(f"✨ [IMPORT PACK] Pack '{pack_title}' tem {len(custom_emojis)} custom emojis")
    
    # 5. Criar pacote no sistema (se auto_create_pack)
    target_pack_id = None
    if req.auto_create_pack:
        nome_pacote = req.pack_name or pack_title
        existing_pack = db.query(PremiumEmojiPack).filter(PremiumEmojiPack.name == nome_pacote).first()
        
        if existing_pack:
            target_pack_id = existing_pack.id
            logger.info(f"✨ [IMPORT PACK] Pacote '{nome_pacote}' já existe (id={target_pack_id}), adicionando emojis a ele")
        else:
            new_pack = PremiumEmojiPack(
                name=nome_pacote,
                icon=req.pack_icon or "📦",
                description=f"Importado de t.me/addemoji/{short_name}",
                sort_order=db.query(PremiumEmojiPack).count() + 1,
                is_active=True
            )
            db.add(new_pack)
            db.flush()
            target_pack_id = new_pack.id
            logger.info(f"✨ [IMPORT PACK] Pacote '{nome_pacote}' criado (id={target_pack_id})")
    
    # 6. Cadastrar emojis em massa (skip duplicados)
    created = 0
    skipped = 0
    
    for idx, ce in enumerate(custom_emojis):
        # Gerar shortcode automático baseado no nome do pack + índice
        safe_name = _re.sub(r'[^a-zA-Z0-9]', '_', short_name.lower()).strip('_')
        auto_shortcode = f":{safe_name}_{idx + 1}:"
        auto_name = f"{pack_title} #{idx + 1}"
        
        # Verificar se emoji_id já existe
        existing = db.query(PremiumEmoji).filter(PremiumEmoji.emoji_id == ce["emoji_id"]).first()
        if existing:
            skipped += 1
            continue
        
        # Verificar se shortcode já existe (incrementa se necessário)
        sc = auto_shortcode
        attempts = 0
        while db.query(PremiumEmoji).filter(PremiumEmoji.shortcode == sc).first():
            attempts += 1
            sc = f":{safe_name}_{idx + 1}_{attempts}:"
        
        emoji = PremiumEmoji(
            emoji_id=ce["emoji_id"],
            fallback=ce["fallback"],
            name=auto_name,
            shortcode=sc,
            pack_id=target_pack_id,
            sort_order=idx + 1,
            emoji_type="animated",  # Custom emojis premium são geralmente animados
            is_active=True
        )
        db.add(emoji)
        created += 1
    
    db.commit()
    invalidate_premium_emoji_cache()
    
    logger.info(f"✨ [IMPORT PACK] Finalizado: {created} criados, {skipped} ignorados de '{pack_title}'")
    
    return {
        "status": "success",
        "message": f"Pack '{pack_title}' importado com sucesso!",
        "pack_title": pack_title,
        "short_name": short_name,
        "total_in_pack": len(custom_emojis),
        "created": created,
        "skipped": skipped,
        "pack_id": target_pack_id
    }


@app.put("/api/superadmin/premium-emojis/{emoji_id}")
def update_premium_emoji(
    emoji_id: int,
    emoji_data: PremiumEmojiUpdate,
    db: Session = Depends(get_db),
    current_superuser = Depends(get_current_superuser)
):
    """Atualiza um emoji premium existente."""
    emoji = db.query(PremiumEmoji).filter(PremiumEmoji.id == emoji_id).first()
    if not emoji:
        raise HTTPException(404, "Emoji não encontrado")
    
    update_dict = emoji_data.dict(exclude_unset=True)
    
    # Sanitiza shortcode se estiver sendo atualizado
    if 'shortcode' in update_dict and update_dict['shortcode']:
        sc = update_dict['shortcode'].strip()
        if not sc.startswith(':'):
            sc = ':' + sc
        if not sc.endswith(':'):
            sc = sc + ':'
        update_dict['shortcode'] = sc
        
        # Verifica duplicidade
        existing = db.query(PremiumEmoji).filter(
            PremiumEmoji.shortcode == sc,
            PremiumEmoji.id != emoji_id
        ).first()
        if existing:
            raise HTTPException(400, f"Shortcode '{sc}' já está em uso por outro emoji")
    
    for field, value in update_dict.items():
        if value is not None:
            setattr(emoji, field, value)
    
    db.commit()
    invalidate_premium_emoji_cache()
    return {"message": f"Emoji '{emoji.name}' atualizado com sucesso"}


@app.delete("/api/superadmin/premium-emojis/{emoji_id}")
def delete_premium_emoji(
    emoji_id: int,
    db: Session = Depends(get_db),
    current_superuser = Depends(get_current_superuser)
):
    """Remove um emoji premium do catálogo."""
    emoji = db.query(PremiumEmoji).filter(PremiumEmoji.id == emoji_id).first()
    if not emoji:
        raise HTTPException(404, "Emoji não encontrado")
    
    nome = emoji.name
    db.delete(emoji)
    db.commit()
    invalidate_premium_emoji_cache()
    
    logger.info(f"🗑️ [PREMIUM EMOJI] Emoji '{nome}' removido por {current_superuser.username}")
    return {"message": f"Emoji '{nome}' removido do catálogo"}


# ===================== ROTA PÚBLICA (PARA O EMOJI PICKER DOS USUÁRIOS) =====================

@app.get("/api/premium-emojis/catalog")
def get_premium_emojis_catalog(
    db: Session = Depends(get_db),
    current_user = Depends(get_current_user)
):
    """
    Retorna o catálogo completo de emojis premium organizados por pacotes.
    Usado pelo componente EmojiPicker no frontend.
    Apenas emojis e pacotes ativos são retornados.
    """
    packs = db.query(PremiumEmojiPack).filter(
        PremiumEmojiPack.is_active == True
    ).order_by(PremiumEmojiPack.sort_order.asc()).all()
    
    # Emojis sem pacote (avulsos)
    orphan_emojis = db.query(PremiumEmoji).filter(
        PremiumEmoji.is_active == True,
        PremiumEmoji.pack_id == None
    ).order_by(PremiumEmoji.sort_order.asc()).all()
    
    result = []
    
    # Adiciona pacotes com seus emojis
    for pack in packs:
        active_emojis = [e for e in pack.emojis if e.is_active]
        if active_emojis:  # Só inclui pacotes que têm emojis ativos
            result.append({
                "id": pack.id,
                "name": pack.name,
                "icon": pack.icon,
                "emojis": [{
                    "id": e.id,
                    "emoji_id": e.emoji_id,
                    "fallback": e.fallback,
                    "name": e.name,
                    "shortcode": e.shortcode,
                    "emoji_type": e.emoji_type,
                    "thumbnail_url": e.thumbnail_url
                } for e in sorted(active_emojis, key=lambda x: x.sort_order)]
            })
    
    # Adiciona emojis avulsos (sem pacote) como "Outros"
    if orphan_emojis:
        result.append({
            "id": 0,
            "name": "Outros",
            "icon": "✨",
            "emojis": [{
                "id": e.id,
                "emoji_id": e.emoji_id,
                "fallback": e.fallback,
                "name": e.name,
                "shortcode": e.shortcode,
                "emoji_type": e.emoji_type,
                "thumbnail_url": e.thumbnail_url
            } for e in orphan_emojis]
        })
    
    return {
        "total_packs": len(result),
        "total_emojis": sum(len(p["emojis"]) for p in result),
        "packs": result
    }


@app.get("/api/premium-emojis/search")
def search_premium_emojis(
    q: str = "",
    db: Session = Depends(get_db),
    current_user = Depends(get_current_user)
):
    """Busca emojis premium por nome, shortcode ou fallback."""
    if not q or len(q) < 2:
        return {"emojis": []}
    
    emojis = db.query(PremiumEmoji).filter(
        PremiumEmoji.is_active == True,
        or_(
            PremiumEmoji.name.ilike(f"%{q}%"),
            PremiumEmoji.shortcode.ilike(f"%{q}%"),
            PremiumEmoji.fallback.ilike(f"%{q}%")
        )
    ).limit(20).all()
    
    return {
        "emojis": [{
            "id": e.id,
            "emoji_id": e.emoji_id,
            "fallback": e.fallback,
            "name": e.name,
            "shortcode": e.shortcode,
            "emoji_type": e.emoji_type,
            "thumbnail_url": e.thumbnail_url
        } for e in emojis]
    }


# =========================================================
# ⚙️ CONFIG GLOBAL + BROADCAST (SUPER ADMIN)
# =========================================================

# 🔥 ATUALIZAÇÃO DO SCHEMA: Adicionado o campo master_syncpay_client_id
# Se este schema já existir em outra parte do seu código, substitua por este
# ou apenas certifique-se de que a linha do syncpay está nele.
class SystemConfigSchema(BaseModel):
    default_fee: int = 60
    master_pushin_pay_id: Optional[str] = ""
    master_wiinpay_user_id: Optional[str] = ""
    master_syncpay_client_id: Optional[str] = ""  # 🆕 NOVO: Chave Mestra Sync Pay
    maintenance_mode: bool = False

class BroadcastSchema(BaseModel):
    title: str
    message: str
    type: str = "info"


@app.get("/api/admin/config")
def get_global_config(
    db: Session = Depends(get_db), 
    current_user = Depends(get_current_user)
):
    """Busca configurações globais do sistema."""
    if not current_user.is_superuser:
        raise HTTPException(status_code=403, detail="Acesso negado")
    
    configs = db.query(SystemConfig).all()
    config_map = {c.key: c.value for c in configs}
    
    return {
        "default_fee": int(config_map.get("default_fee", "60")),
        "master_pushin_pay_id": config_map.get("master_pushin_pay_id", ""),
        "master_wiinpay_user_id": config_map.get("master_wiinpay_user_id", ""),
        "master_syncpay_client_id": config_map.get("master_syncpay_client_id", ""), # 🆕 ADICIONADO AQUI
        "maintenance_mode": config_map.get("maintenance_mode", "false") == "true"
    }


@app.post("/api/admin/config")
def update_global_config(
    config: SystemConfigSchema, 
    db: Session = Depends(get_db), 
    current_user = Depends(get_current_user)
):
    """Salva configurações globais do sistema."""
    if not current_user.is_superuser:
        raise HTTPException(status_code=403, detail="Acesso negado")
    
    def upsert(key, value):
        item = db.query(SystemConfig).filter(SystemConfig.key == key).first()
        if item:
            item.value = str(value)
        else:
            db.add(SystemConfig(key=key, value=str(value)))
    
    try:
        upsert("default_fee", config.default_fee)
        upsert("master_pushin_pay_id", config.master_pushin_pay_id)
        upsert("master_wiinpay_user_id", config.master_wiinpay_user_id)
        upsert("master_syncpay_client_id", config.master_syncpay_client_id) # 🆕 ADICIONADO AQUI
        upsert("maintenance_mode", "true" if config.maintenance_mode else "false")
        db.commit()
        
        logger.info(f"⚙️ Config global atualizada por {current_user.username}")
        logger.info(f"  master_pushin_pay_id: {config.master_pushin_pay_id}")
        logger.info(f"  master_wiinpay_user_id: {config.master_wiinpay_user_id}")
        logger.info(f"  master_syncpay_client_id: {config.master_syncpay_client_id}") # 🆕 ADICIONADO AQUI
        return {"message": "Configurações salvas com sucesso!"}
    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/admin/broadcast")
def send_broadcast(
    broadcast: BroadcastSchema, 
    db: Session = Depends(get_db), 
    current_user = Depends(get_current_user)
):
    """Envia notificação em massa para todos os usuários ativos."""
    if not current_user.is_superuser:
        raise HTTPException(status_code=403, detail="Acesso negado")
    
    try:
        users = db.query(User).filter(User.is_active == True).all()
        count = 0
        for user in users:
            db.add(Notification(
                user_id=user.id, 
                title=broadcast.title, 
                message=broadcast.message, 
                type=broadcast.type
            ))
            count += 1
        
        db.commit()
        
        logger.info(f"📢 Broadcast enviado por {current_user.username} para {count} usuários")
        return {"message": f"Notificação enviada para {count} usuários!"}
    except Exception as e:
        db.rollback()
        logger.error(f"❌ Erro broadcast: {e}")
        raise HTTPException(500, str(e))

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
# 🏆 NOVA ROTA: RANKING DE TOP VENDEDORES (TOP 10)
# =========================================================
@app.get("/api/ranking")
def obter_ranking(
    mes: int = Query(..., description="Mês numérico (Ex: 2 para Fevereiro)"),
    ano: int = Query(..., description="Ano com 4 dígitos (Ex: 2026)"),
    db: Session = Depends(get_db)
):
    try:
        resultado = (
            db.query(
                User.username,
                func.sum(Pedido.valor).label("total_faturado"),
                func.count(Pedido.id).label("total_vendas")
            )
            .join(BotModel, BotModel.owner_id == User.id)
            .join(Pedido, Pedido.bot_id == BotModel.id)
            .filter(Pedido.data_aprovacao != None)
            .filter(Pedido.status.in_(['approved', 'paid', 'active', 'expired']))
            .filter(User.is_superuser == False)
            .filter(extract('month', Pedido.data_aprovacao) == mes)
            .filter(extract('year', Pedido.data_aprovacao) == ano)
            .group_by(User.id)
            .order_by(func.sum(Pedido.valor).desc())
            .limit(10)
            .all()
        )

        ranking_formatado = []
        for index, row in enumerate(resultado):
            ranking_formatado.append({
                "posicao": index + 1,
                "username": row.username,
                "total_faturado": round(row.total_faturado, 2) if row.total_faturado else 0.0,
                "total_vendas": row.total_vendas  # 🔥 NOVO: Envia o número de vendas para o site
            })

        return {"status": "success", "ranking": ranking_formatado}

    except Exception as e:
        return {"status": "error", "message": f"Erro ao gerar ranking: {str(e)}"}

# =========================================================
# 📁 ROTA DE UPLOAD DE MÍDIA (BACKBLAZE B2)
# =========================================================
# Configurações do seu Balde Backblaze
B2_ENDPOINT = "https://s3.us-east-005.backblazeb2.com"
B2_KEY_ID = "0053eddc50d26a30000000005"
B2_APP_KEY = "K0057bAugoXm4Vz9IHf8sVBnM4+yBEo"
B2_BUCKET_NAME = "Zenyx-mid" # <-- ⚠️ ATENÇÃO: Escreva o nome exato do seu balde aqui!

# Inicializa o cliente B2 (Protocolo S3)
b2_client = boto3.client(
    's3',
    endpoint_url=B2_ENDPOINT,
    aws_access_key_id=B2_KEY_ID,
    aws_secret_access_key=B2_APP_KEY
)

@app.post("/api/admin/media/upload")
async def upload_media(
    file: UploadFile = File(...),
    type: str = Form("flow"),
    db: Session = Depends(get_db),
    current_user = Depends(get_current_user)
):
    """Recebe o arquivo do Frontend (React) e envia direto para o Backblaze B2"""
    try:
        # 1. Valida a extensão (Já inclui OGG e MP3 para os nossos áudios humanos!)
        ext = file.filename.split('.')[-1].lower()
        allowed_exts = ['jpg', 'jpeg', 'png', 'gif', 'mp4', 'mov', 'avi', 'mp3', 'wav', 'ogg']
        
        if ext not in allowed_exts:
            raise HTTPException(status_code=400, detail=f"Formato .{ext} não suportado.")

        # 2. Gera um nome único aleatório para não sobrescrever arquivos com o mesmo nome
        unique_filename = f"{type}_{uuid.uuid4().hex}.{ext}"
        
        # 3. Lê o conteúdo do arquivo enviado pelo usuário
        file_content = await file.read()
        
        # 4. Faz o upload para o Backblaze B2 silenciosamente
        b2_client.put_object(
            Bucket=B2_BUCKET_NAME,
            Key=unique_filename,
            Body=file_content,
            ContentType=file.content_type
        )
        
        # 5. Gera a URL pública padrão do S3 no Backblaze
        # A URL fica no formato: https://{nome-do-balde}.s3.us-east-005.backblazeb2.com/{nome_do_arquivo}
        endpoint_domain = B2_ENDPOINT.replace("https://", "")
        public_url = f"https://{B2_BUCKET_NAME}.{endpoint_domain}/{unique_filename}"
        
        logger.info(f"✅ Upload B2 concluído com sucesso: {public_url}")
        
        # Retorna o link para o Frontend colocar no campo de texto automaticamente
        return {"status": "success", "url": public_url}
        
    except Exception as e:
        logger.error(f"❌ Erro fatal no upload para o Backblaze: {e}")
        raise HTTPException(status_code=500, detail=f"Erro interno no upload: {str(e)}")

# =========================================================
# 🚑 MIGRAÇÃO DE EMERGÊNCIA (CORREÇÃO DE COLUNA)
# =========================================================
def check_and_fix_interaction_count():
    """
    Cria a coluna interaction_count na tabela LEADS se não existir.
    Isso resolve o erro crítico de atributo inexistente 'interaction_count'.
    """
    try:
        # Usa o engine importado do database
        with engine.connect() as conn:
            # Verifica se a coluna existe consultando o schema
            result = conn.execute(text(
                "SELECT column_name FROM information_schema.columns WHERE table_name='leads' AND column_name='interaction_count'"
            ))
            # Se não retornar nada, a coluna não existe
            if not result.fetchone():
                logger.info("🚑 [FIX] Criando coluna 'interaction_count' na tabela LEADS...")
                conn.execute(text("ALTER TABLE leads ADD COLUMN interaction_count INTEGER DEFAULT 0"))
                conn.commit()
                logger.info("✅ [FIX] Coluna 'interaction_count' criada com sucesso!")
            else:
                logger.info("✅ [FIX] Coluna 'interaction_count' já existe.")
    except Exception as e:
        # Loga o erro mas não para o sistema (pode ser erro de permissão ou sqlite vs postgres)
        logger.error(f"❌ Erro ao verificar interaction_count: {e}")

# =========================================================
# 🚀 STARTUP UNIFICADO (FUSION V7 + ORIGINAL)
# =========================================================
# =========================================================
# 🚀 STARTUP UNIFICADO (COM CORREÇÃO DE REMARKETING)
# =========================================================
@app.on_event("startup")
async def startup_event():
    """
    Inicialização Mestra: Banco, Migrações, Pagamentos, HTTP e Scheduler.
    """
    global http_client
    print("="*60)
    print("🚀 INICIANDO ZENYX GBOT (STARTUP UNIFICADO)")
    print("="*60)

    # 0. 🚑 CORREÇÃO DE EMERGÊNCIA (REMARKETING)
    # Executa antes de tudo para garantir que a coluna exista
    check_and_fix_interaction_count()

    # 1. INICIALIZAR HTTP CLIENT
    try:
        http_client = httpx.AsyncClient(
            timeout=httpx.Timeout(30.0, connect=10.0),
            limits=httpx.Limits(max_keepalive_connections=20, max_connections=100),
            follow_redirects=True
        )
        logger.info("✅ [1/5] HTTP Client inicializado")
    except Exception as e:
        logger.error(f"❌ Erro HTTP Client: {e}")

    # 2. GARANTIR BANCO E COLUNAS BÁSICAS
    try:
        print("📊 Inicializando banco de dados...")
        from database import Base, engine, SystemConfig, SessionLocal, init_db
        Base.metadata.create_all(bind=engine)
        
        from force_migration import forcar_atualizacao_tabelas
        print("🔧 Verificando integridade e colunas faltantes...")
        forcar_atualizacao_tabelas()
        
        print("✅ [2/5] Banco de dados inicializado e corrigido")
    except Exception as e:
        logger.error(f"❌ ERRO CRÍTICO no Banco de Dados: {e}")

    # 3. EXECUTAR MIGRAÇÕES DE VERSÃO
    try:
        print("🔄 Executando migrações de versão...")
        # Imports Locais
        from migration_v3 import executar_migracao_v3
        from migration_v4 import executar_migracao_v4
        from migration_v5 import executar_migracao_v5
        from migration_v6 import executar_migracao_v6
        from migration_v7 import executar_migracao_v7
        from migration_audit_logs import executar_migracao_audit_logs
        # Import da nova migração V8 (criação da msg_pix)
        try:
            from migration_v8 import executar_migracao_v8
        except ImportError:
            # Caso o arquivo ainda não exista ou tenha outro nome, loga o aviso
            logger.warning("⚠️ Arquivo migration_v8.py não encontrado (import failed).")
            executar_migracao_v8 = None


        try: executar_migracao_v3() 
        except Exception as e: logger.warning(f"⚠️ V3: {e}")
        
        try: executar_migracao_v4() 
        except Exception as e: logger.warning(f"⚠️ V4: {e}")
        
        try: executar_migracao_v5() 
        except Exception as e: logger.warning(f"⚠️ V5: {e}")
        
        try: executar_migracao_v6() 
        except Exception as e: logger.warning(f"⚠️ V6: {e}")
        
        try: 
            executar_migracao_v7()
            print("✅ Migração V7 (Canais) verificada")
        except Exception as e: logger.warning(f"⚠️ V7: {e}")

        # --- MIGRAÇÃO V8 (MSG PIX) ---
        if executar_migracao_v8:
            try:
                executar_migracao_v8()
                print("✅ Migração V8 (Msg Pix) verificada")
            except Exception as e:
                logger.warning(f"⚠️ V8: {e}")

        try: executar_migracao_audit_logs()
        except Exception as e: logger.warning(f"⚠️ AuditLogs: {e}")

        print("✅ [3/5] Migrações de versão concluídas")
        
    except ImportError as e:
        logger.warning(f"⚠️ Algum arquivo de migração está faltando: {e}")
    except Exception as e:
        logger.error(f"❌ Erro geral nas migrações: {e}")

    # 4. CONFIGURAÇÃO DE PAGAMENTO
    try:
        print("💳 Configurando sistema de pagamento...")
        db = SessionLocal()
        try:
            config = db.query(SystemConfig).filter(SystemConfig.key == "pushin_plataforma_id").first()
            if not config:
                config = SystemConfig(key="pushin_plataforma_id", value="")
                db.add(config)
                db.commit()
                print("✅ Configuração de pagamento criada (Vazia)")
            else:
                print("✅ [4/5] Configuração de pagamento encontrada")
        finally:
            db.close()
    except Exception as e:
        logger.warning(f"⚠️ Erro ao configurar pushin_pay_id: {e}")

    # 5. INICIAR SCHEDULER
    try:
        if not scheduler.running:
            scheduler.start()
            logger.info("✅ [5/5] Scheduler iniciado")
    except Exception as e:
        logger.error(f"❌ Erro Scheduler: {e}")

    print("="*60)
    print("✅ SISTEMA TOTALMENTE OPERACIONAL (V7 + V8)")
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
    now = now_brazil()
    
    # 1. Busca pedidos aprovados que JÁ venceram
    vencidos = db.query(Pedido).filter(
        Pedido.status.in_(['approved', 'active', 'paid']),
        or_(
            and_(Pedido.custom_expiration != None, Pedido.custom_expiration < now),
            and_(Pedido.custom_expiration == None, Pedido.data_expiracao != None, Pedido.data_expiracao < now)
        )
    ).all()
    
    removidos = 0
    erros = 0
    
    for pedido in vencidos:
        try:
            bot_data = db.query(BotModel).filter(BotModel.id == pedido.bot_id).first()
            if not bot_data or not bot_data.token: 
                pedido.status = 'expired'
                db.commit()
                removidos += 1
                continue
            
            # 🔥 Proteção: Admin nunca é removido
            eh_admin_principal = (
                bot_data.admin_principal_id and 
                str(pedido.telegram_id) == str(bot_data.admin_principal_id)
            )
            eh_admin_extra = db.query(BotAdmin).filter(
                BotAdmin.telegram_id == str(pedido.telegram_id),
                BotAdmin.bot_id == bot_data.id
            ).first()
            
            if eh_admin_principal or eh_admin_extra:
                logger.info(f"👑 Ignorando remoção de Admin: {pedido.telegram_id}")
                continue
            
            # Conecta no Telegram (Sem threads para evitar erro)
            tb = telebot.TeleBot(bot_data.token, threaded=False)
            
            # === REMOÇÃO DO CANAL VIP PRINCIPAL ===
            if bot_data.id_canal_vip:
                canal_id = bot_data.id_canal_vip
                if str(canal_id).replace("-","").isdigit(): canal_id = int(str(canal_id).strip())
                
                try:
                    tb.ban_chat_member(canal_id, int(pedido.telegram_id))
                    time.sleep(0.5)
                    tb.unban_chat_member(canal_id, int(pedido.telegram_id))
                    logger.info(f"💀 Usuário {pedido.first_name} ({pedido.telegram_id}) removido do bot {bot_data.nome}")
                except Exception as e_kick:
                    err_msg = str(e_kick).lower()
                    if "participant_id_invalid" in err_msg or "user not found" in err_msg or "user_not_participant" in err_msg:
                        logger.info(f"ℹ️ Usuário {pedido.telegram_id} já havia saído do canal VIP.")
                    else:
                        logger.warning(f"⚠️ Erro ao remover {pedido.telegram_id}: {e_kick}")
            
            # === REMOÇÃO DOS GRUPOS EXTRAS (BotGroup) ===
            if pedido.plano_id:
                try:
                    grupos_extras = db.query(BotGroup).filter(
                        BotGroup.bot_id == bot_data.id,
                        BotGroup.is_active == True
                    ).all()
                    
                    for grupo in grupos_extras:
                        plan_ids = grupo.plan_ids if grupo.plan_ids else []
                        if pedido.plano_id in plan_ids:
                            try:
                                grupo_id = int(str(grupo.group_id).strip())
                                tb.ban_chat_member(grupo_id, int(pedido.telegram_id))
                                time.sleep(0.3)
                                tb.unban_chat_member(grupo_id, int(pedido.telegram_id))
                                logger.info(f"👋 Removido de grupo extra '{grupo.title}': {pedido.telegram_id}")
                            except Exception as e_g:
                                err_g = str(e_g).lower()
                                if "participant_id_invalid" in err_g or "user not found" in err_g or "user_not_participant" in err_g:
                                    pass
                                else:
                                    logger.warning(f"⚠️ Erro ao remover do grupo '{grupo.title}': {e_g}")
                except Exception as e_grupos:
                    logger.warning(f"⚠️ Erro ao processar grupos extras: {e_grupos}")
            
            # Avisa o usuário no privado
            try:
                tb.send_message(int(pedido.telegram_id), "🚫 <b>Seu acesso expirou!</b>\n\nObrigado por ter ficado conosco. Renove seu plano para voltar!", parse_mode="HTML")
            except: pass
            
            # Atualiza status no Pedido
            pedido.status = 'expired'
            
            # Atualiza status no Lead (Sincronia)
            lead = db.query(Lead).filter(Lead.bot_id == pedido.bot_id, Lead.user_id == str(pedido.telegram_id)).first()
            if lead:
                lead.status = 'expired'
            
            removidos += 1
            db.commit()
            
        except Exception as e:
            logger.error(f"❌ Erro ao processar vencido {pedido.id}: {e}")
            db.rollback()
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
                created_at=now_brazil()
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

# ============================================================
# 🔧 ROTA DE MIGRAÇÃO - ADICIONAR COLUNAS FALTANTES
# ============================================================
@app.get("/migrate-button-fields")
async def migrate_button_fields(db: Session = Depends(get_db)):
    """
    🔥 Migração Manual: Adiciona as novas colunas do sistema de botões personalizados
    Acesse: https://zenyx-gbs-testesv1-production.up.railway.app/migrate-button-fields
    """
    try:
        from sqlalchemy import text
        
        resultados = []
        
        # 1. Adicionar coluna button_mode
        try:
            db.execute(text("""
                ALTER TABLE bot_flows 
                ADD COLUMN button_mode VARCHAR(20) DEFAULT 'next_step';
            """))
            db.commit()
            resultados.append("✅ Coluna 'button_mode' criada com sucesso!")
        except Exception as e:
            db.rollback()
            if "already exists" in str(e).lower() or "duplicate column" in str(e).lower():
                resultados.append("ℹ️ Coluna 'button_mode' já existe")
            else:
                resultados.append(f"❌ Erro ao criar 'button_mode': {str(e)}")
        
        # 2. Adicionar coluna buttons_config_2
        try:
            db.execute(text("""
                ALTER TABLE bot_flows 
                ADD COLUMN buttons_config_2 JSON DEFAULT '[]'::json;
            """))
            db.commit()
            resultados.append("✅ Coluna 'buttons_config_2' criada com sucesso!")
        except Exception as e:
            db.rollback()
            if "already exists" in str(e).lower() or "duplicate column" in str(e).lower():
                resultados.append("ℹ️ Coluna 'buttons_config_2' já existe")
            else:
                resultados.append(f"❌ Erro ao criar 'buttons_config_2': {str(e)}")
        
        # 3. Verificar se buttons_config existe (deveria já existir)
        try:
            db.execute(text("SELECT buttons_config FROM bot_flows LIMIT 1;"))
            resultados.append("✅ Coluna 'buttons_config' já existe")
        except Exception as e:
            # Se não existir, criar
            try:
                db.execute(text("""
                    ALTER TABLE bot_flows 
                    ADD COLUMN buttons_config JSON DEFAULT '[]'::json;
                """))
                db.commit()
                resultados.append("✅ Coluna 'buttons_config' criada com sucesso!")
            except Exception as e2:
                db.rollback()
                resultados.append(f"❌ Erro ao criar 'buttons_config': {str(e2)}")
        
        # 4. Atualizar valores NULL para defaults
        try:
            db.execute(text("""
                UPDATE bot_flows 
                SET button_mode = 'next_step' 
                WHERE button_mode IS NULL;
            """))
            db.execute(text("""
                UPDATE bot_flows 
                SET buttons_config = '[]'::json 
                WHERE buttons_config IS NULL;
            """))
            db.execute(text("""
                UPDATE bot_flows 
                SET buttons_config_2 = '[]'::json 
                WHERE buttons_config_2 IS NULL;
            """))
            db.commit()
            resultados.append("✅ Valores NULL atualizados para defaults")
        except Exception as e:
            db.rollback()
            resultados.append(f"⚠️ Aviso ao atualizar NULLs: {str(e)}")
        
        return {
            "status": "success",
            "message": "Migração concluída!",
            "resultados": resultados
        }
        
    except Exception as e:
        db.rollback()
        return {
            "status": "error",
            "message": f"Erro geral na migração: {str(e)}",
            "detalhes": str(e)
        }

# ============================================================
# 🔧 ROTA DE MIGRAÇÃO - ADICIONAR COLUNA max_duration_minutes
# ============================================================
@app.get("/migrate-alternating-duration")
async def migrate_alternating_duration(db: Session = Depends(get_db)):
    """
    🔥 Migração Manual: Adiciona a coluna max_duration_minutes na tabela alternating_messages
    Acesse: https://zenyx-gbs-testesv1-production.up.railway.app/migrate-alternating-duration
    """
    try:
        from sqlalchemy import text
        
        resultados = []
        
        # 1. Adicionar coluna max_duration_minutes
        try:
            db.execute(text("""
                ALTER TABLE alternating_messages 
                ADD COLUMN max_duration_minutes INTEGER DEFAULT 60;
            """))
            db.commit()
            resultados.append("✅ Coluna 'max_duration_minutes' criada com sucesso!")
        except Exception as e:
            db.rollback()
            if "already exists" in str(e).lower() or "duplicate column" in str(e).lower():
                resultados.append("ℹ️ Coluna 'max_duration_minutes' já existe")
            else:
                resultados.append(f"❌ Erro ao criar 'max_duration_minutes': {str(e)}")
        
        # 2. Adicionar coluna last_message_auto_destruct (se não existir)
        try:
            db.execute(text("""
                ALTER TABLE alternating_messages 
                ADD COLUMN last_message_auto_destruct BOOLEAN DEFAULT FALSE;
            """))
            db.commit()
            resultados.append("✅ Coluna 'last_message_auto_destruct' criada com sucesso!")
        except Exception as e:
            db.rollback()
            if "already exists" in str(e).lower() or "duplicate column" in str(e).lower():
                resultados.append("ℹ️ Coluna 'last_message_auto_destruct' já existe")
            else:
                resultados.append(f"❌ Erro ao criar 'last_message_auto_destruct': {str(e)}")
        
        # 3. Adicionar coluna last_message_destruct_seconds (se não existir)
        try:
            db.execute(text("""
                ALTER TABLE alternating_messages 
                ADD COLUMN last_message_destruct_seconds INTEGER DEFAULT 60;
            """))
            db.commit()
            resultados.append("✅ Coluna 'last_message_destruct_seconds' criada com sucesso!")
        except Exception as e:
            db.rollback()
            if "already exists" in str(e).lower() or "duplicate column" in str(e).lower():
                resultados.append("ℹ️ Coluna 'last_message_destruct_seconds' já existe")
            else:
                resultados.append(f"❌ Erro ao criar 'last_message_destruct_seconds': {str(e)}")
        
        # 4. Atualizar valores NULL para defaults
        try:
            db.execute(text("""
                UPDATE alternating_messages 
                SET max_duration_minutes = 60 
                WHERE max_duration_minutes IS NULL;
            """))
            db.execute(text("""
                UPDATE alternating_messages 
                SET last_message_auto_destruct = FALSE 
                WHERE last_message_auto_destruct IS NULL;
            """))
            db.execute(text("""
                UPDATE alternating_messages 
                SET last_message_destruct_seconds = 60 
                WHERE last_message_destruct_seconds IS NULL;
            """))
            db.commit()
            resultados.append("✅ Valores NULL atualizados para defaults")
        except Exception as e:
            db.rollback()
            resultados.append(f"⚠️ Aviso ao atualizar NULLs: {str(e)}")
        
        # 5. Verificar estrutura final
        try:
            resultado = db.execute(text("""
                SELECT column_name, data_type, column_default 
                FROM information_schema.columns 
                WHERE table_name = 'alternating_messages' 
                AND column_name IN ('max_duration_minutes', 'last_message_auto_destruct', 'last_message_destruct_seconds')
                ORDER BY column_name;
            """))
            colunas = resultado.fetchall()
            
            if colunas:
                resultados.append("📊 Estrutura final verificada:")
                for col in colunas:
                    resultados.append(f"   - {col[0]}: {col[1]} (default: {col[2]})")
            else:
                resultados.append("⚠️ Não foi possível verificar a estrutura final")
                
        except Exception as e:
            resultados.append(f"⚠️ Erro ao verificar estrutura: {str(e)}")
        
        return {
            "status": "success",
            "message": "✅ Migração concluída!",
            "resultados": resultados
        }
        
    except Exception as e:
        db.rollback()
        return {
            "status": "error",
            "message": f"❌ Erro geral na migração: {str(e)}",
            "detalhes": str(e)
        }
# ============================================================
# 🔧 ROTA DE MIGRAÇÃO - GRUPOS E CANAIS (FASE 1 + FASE 2)
# ============================================================
@app.get("/migrate-bot-groups")
async def migrate_bot_groups(db: Session = Depends(get_db)):
    """
    🔥 Migração Manual: Cria a tabela bot_groups e adiciona colunas group_id
    nas tabelas de ofertas (order_bump_config, upsell_config, downsell_config).
    Acesse: https://zenyx-gbs-testesv1-production.up.railway.app/migrate-bot-groups
    """
    try:
        from sqlalchemy import text
        
        resultados = []
        
        # 1. Criar tabela bot_groups
        try:
            db.execute(text("""
                CREATE TABLE IF NOT EXISTS bot_groups (
                    id SERIAL PRIMARY KEY,
                    bot_id INTEGER NOT NULL REFERENCES bots(id) ON DELETE CASCADE,
                    owner_id INTEGER NOT NULL REFERENCES users(id),
                    title VARCHAR NOT NULL,
                    group_id VARCHAR NOT NULL,
                    link VARCHAR,
                    plan_ids JSON DEFAULT '[]'::json,
                    is_active BOOLEAN DEFAULT TRUE,
                    created_at TIMESTAMP DEFAULT NOW(),
                    updated_at TIMESTAMP DEFAULT NOW()
                );
            """))
            db.commit()
            resultados.append("✅ Tabela 'bot_groups' criada com sucesso!")
        except Exception as e:
            db.rollback()
            if "already exists" in str(e).lower():
                resultados.append("ℹ️ Tabela 'bot_groups' já existe")
            else:
                resultados.append(f"❌ Erro ao criar tabela 'bot_groups': {str(e)}")
        
        # 2. Criar índices
        try:
            db.execute(text("CREATE INDEX IF NOT EXISTS idx_bot_groups_bot_id ON bot_groups(bot_id);"))
            db.execute(text("CREATE INDEX IF NOT EXISTS idx_bot_groups_owner_id ON bot_groups(owner_id);"))
            db.execute(text("CREATE INDEX IF NOT EXISTS idx_bot_groups_is_active ON bot_groups(is_active);"))
            db.commit()
            resultados.append("✅ Índices criados com sucesso!")
        except Exception as e:
            db.rollback()
            resultados.append(f"⚠️ Índices: {str(e)}")
        
        # 3. Adicionar coluna group_id na order_bump_config
        try:
            db.execute(text("""
                ALTER TABLE order_bump_config 
                ADD COLUMN group_id INTEGER REFERENCES bot_groups(id) ON DELETE SET NULL;
            """))
            db.commit()
            resultados.append("✅ Coluna 'group_id' adicionada em order_bump_config!")
        except Exception as e:
            db.rollback()
            if "already exists" in str(e).lower() or "duplicate column" in str(e).lower():
                resultados.append("ℹ️ Coluna 'group_id' já existe em order_bump_config")
            else:
                resultados.append(f"❌ Erro order_bump_config: {str(e)}")
        
        # 4. Adicionar coluna group_id na upsell_config
        try:
            db.execute(text("""
                ALTER TABLE upsell_config 
                ADD COLUMN group_id INTEGER REFERENCES bot_groups(id) ON DELETE SET NULL;
            """))
            db.commit()
            resultados.append("✅ Coluna 'group_id' adicionada em upsell_config!")
        except Exception as e:
            db.rollback()
            if "already exists" in str(e).lower() or "duplicate column" in str(e).lower():
                resultados.append("ℹ️ Coluna 'group_id' já existe em upsell_config")
            else:
                resultados.append(f"❌ Erro upsell_config: {str(e)}")
        
        # 5. Adicionar coluna group_id na downsell_config
        try:
            db.execute(text("""
                ALTER TABLE downsell_config 
                ADD COLUMN group_id INTEGER REFERENCES bot_groups(id) ON DELETE SET NULL;
            """))
            db.commit()
            resultados.append("✅ Coluna 'group_id' adicionada em downsell_config!")
        except Exception as e:
            db.rollback()
            if "already exists" in str(e).lower() or "duplicate column" in str(e).lower():
                resultados.append("ℹ️ Coluna 'group_id' já existe em downsell_config")
            else:
                resultados.append(f"❌ Erro downsell_config: {str(e)}")
        
        # 6. Verificar estrutura final
        try:
            resultado = db.execute(text("""
                SELECT table_name, column_name, data_type 
                FROM information_schema.columns 
                WHERE (table_name = 'bot_groups')
                OR (table_name IN ('order_bump_config', 'upsell_config', 'downsell_config') AND column_name = 'group_id')
                ORDER BY table_name, column_name;
            """))
            colunas = resultado.fetchall()
            
            if colunas:
                resultados.append("📊 Estrutura verificada:")
                for col in colunas:
                    resultados.append(f"   - {col[0]}.{col[1]}: {col[2]}")
        except Exception as e:
            resultados.append(f"⚠️ Erro ao verificar estrutura: {str(e)}")
        
        return {
            "status": "success",
            "message": "✅ Migração Grupos e Canais concluída!",
            "resultados": resultados
        }
        
    except Exception as e:
        db.rollback()
        return {
            "status": "error",
            "message": f"❌ Erro geral na migração: {str(e)}",
            "detalhes": str(e)
        }
# ============================================================
# 🔧 ROTA DE MIGRAÇÃO - CANAL DE NOTIFICAÇÕES
# ============================================================
@app.get("/migrate-canal-notificacao")
async def migrate_canal_notificacao(db: Session = Depends(get_db)):
    """
    Migração: Adiciona coluna id_canal_notificacao na tabela bots.
    Acesse: https://zenyx-gbs-testesv1-production.up.railway.app/migrate-canal-notificacao
    """
    try:
        from sqlalchemy import text
        
        resultados = []
        
        # 1. Adicionar coluna id_canal_notificacao
        try:
            db.execute(text("""
                ALTER TABLE bots 
                ADD COLUMN id_canal_notificacao VARCHAR;
            """))
            db.commit()
            resultados.append("✅ Coluna 'id_canal_notificacao' criada com sucesso!")
        except Exception as e:
            db.rollback()
            if "already exists" in str(e).lower() or "duplicate column" in str(e).lower():
                resultados.append("ℹ️ Coluna 'id_canal_notificacao' já existe")
            else:
                resultados.append(f"❌ Erro: {str(e)}")
        
        # 2. Verificar
        try:
            resultado = db.execute(text("""
                SELECT column_name, data_type 
                FROM information_schema.columns 
                WHERE table_name = 'bots' AND column_name = 'id_canal_notificacao';
            """))
            cols = resultado.fetchall()
            if cols:
                resultados.append(f"✅ Verificado: bots.{cols[0][0]} ({cols[0][1]})")
            else:
                resultados.append("⚠️ Coluna não encontrada após migração")
        except Exception as e:
            resultados.append(f"⚠️ Erro ao verificar: {str(e)}")
        
        return {
            "status": "success",
            "message": "✅ Migração Canal de Notificações concluída!",
            "resultados": resultados
        }
        
    except Exception as e:
        db.rollback()
        return {
            "status": "error",
            "message": f"❌ Erro geral: {str(e)}",
            "detalhes": str(e)
        }


# ============================================================
# 🔧 MIGRAÇÃO: MULTI-GATEWAY (WIINPAY + CONTINGÊNCIA + SYNC PAY)
# ============================================================
@app.get("/migrate-multi-gateway")
async def migrate_multi_gateway(db: Session = Depends(get_db)):
    """
    Migração: Adiciona todas as colunas do sistema multi-gateway.
    Acesse UMA VEZ: https://zenyx-gbs-testesv1-production.up.railway.app/migrate-multi-gateway
    """
    try:
        from sqlalchemy import text
        
        resultados = []
        
        comandos = [
            # --- GATEWAYS EXISTENTES ---
            ("bots", "wiinpay_api_key", "ALTER TABLE bots ADD COLUMN wiinpay_api_key VARCHAR;"),
            ("bots", "gateway_principal", "ALTER TABLE bots ADD COLUMN gateway_principal VARCHAR DEFAULT 'pushinpay';"),
            ("bots", "gateway_fallback", "ALTER TABLE bots ADD COLUMN gateway_fallback VARCHAR;"),
            ("bots", "pushinpay_ativo", "ALTER TABLE bots ADD COLUMN pushinpay_ativo BOOLEAN DEFAULT FALSE;"),
            ("bots", "wiinpay_ativo", "ALTER TABLE bots ADD COLUMN wiinpay_ativo BOOLEAN DEFAULT FALSE;"),
            ("users", "wiinpay_user_id", "ALTER TABLE users ADD COLUMN wiinpay_user_id VARCHAR;"),
            ("pedidos", "gateway_usada", "ALTER TABLE pedidos ADD COLUMN gateway_usada VARCHAR;"),
            
            # --- NOVA GATEWAY: SYNC PAY ---
            ("users", "syncpay_client_id", "ALTER TABLE users ADD COLUMN syncpay_client_id VARCHAR;"),
            ("bots", "syncpay_client_id", "ALTER TABLE bots ADD COLUMN syncpay_client_id VARCHAR;"),
            ("bots", "syncpay_client_secret", "ALTER TABLE bots ADD COLUMN syncpay_client_secret VARCHAR;"),
            ("bots", "syncpay_access_token", "ALTER TABLE bots ADD COLUMN syncpay_access_token VARCHAR;"),
            ("bots", "syncpay_token_expires_at", "ALTER TABLE bots ADD COLUMN syncpay_token_expires_at TIMESTAMP;"),
            ("bots", "syncpay_ativo", "ALTER TABLE bots ADD COLUMN syncpay_ativo BOOLEAN DEFAULT FALSE;")
        ]
        
        for tabela, coluna, sql in comandos:
            try:
                db.execute(text(sql))
                db.commit()
                resultados.append(f"✅ {tabela}.{coluna} criada com sucesso!")
            except Exception as e:
                db.rollback()
                if "already exists" in str(e).lower() or "duplicate column" in str(e).lower():
                    resultados.append(f"ℹ️ {tabela}.{coluna} já existe")
                else:
                    resultados.append(f"❌ {tabela}.{coluna}: {str(e)}")
        
        # Verificação final (AGORA COM SYNC PAY INCLUÍDA!)
        try:
            check = db.execute(text("""
                SELECT column_name FROM information_schema.columns 
                WHERE table_name = 'bots' AND column_name IN 
                ('wiinpay_api_key', 'gateway_principal', 'gateway_fallback', 'pushinpay_ativo', 'wiinpay_ativo', 
                 'syncpay_client_id', 'syncpay_client_secret', 'syncpay_access_token', 'syncpay_ativo')
            """))
            cols_bots = [r[0] for r in check.fetchall()]
            resultados.append(f"✅ Verificação bots: {cols_bots}")
        except:
            pass
        
        return {
            "status": "success",
            "message": "✅ Migração Multi-Gateway concluída!",
            "resultados": resultados
        }
        
    except Exception as e:
        db.rollback()
        return {
            "status": "error",
            "message": f"❌ Erro geral: {str(e)}",
            "detalhes": str(e)
        }

# ============================================================
# 🔧 MIGRAÇÃO: MINI APP V2 (SEPARADORES E PAGINAÇÃO)
# ============================================================
@app.get("/migrate-miniapp-v2")
async def migrate_miniapp_v2(db: Session = Depends(get_db)):
    """
    Migração EXCLUSIVA para as novas colunas do Mini App.
    Acesse UMA VEZ: https://zenyx-gbs-testesv1-production.up.railway.app/migrate-miniapp-v2
    """
    try:
        from sqlalchemy import text
        resultados = []
        
        # Lista EXCLUSIVA das colunas do Mini App (Atualizada com Cores de Texto e NEON)
        comandos_miniapp = [
            ("miniapp_categories", "items_per_page", "ALTER TABLE miniapp_categories ADD COLUMN IF NOT EXISTS items_per_page INTEGER DEFAULT NULL;"),
            ("miniapp_categories", "separator_enabled", "ALTER TABLE miniapp_categories ADD COLUMN IF NOT EXISTS separator_enabled BOOLEAN DEFAULT FALSE;"),
            ("miniapp_categories", "separator_color", "ALTER TABLE miniapp_categories ADD COLUMN IF NOT EXISTS separator_color VARCHAR DEFAULT '#333333';"),
            ("miniapp_categories", "separator_text", "ALTER TABLE miniapp_categories ADD COLUMN IF NOT EXISTS separator_text VARCHAR DEFAULT NULL;"),
            ("miniapp_categories", "separator_btn_text", "ALTER TABLE miniapp_categories ADD COLUMN IF NOT EXISTS separator_btn_text VARCHAR DEFAULT NULL;"),
            ("miniapp_categories", "separator_btn_url", "ALTER TABLE miniapp_categories ADD COLUMN IF NOT EXISTS separator_btn_url VARCHAR DEFAULT NULL;"),
            ("miniapp_categories", "separator_logo_url", "ALTER TABLE miniapp_categories ADD COLUMN IF NOT EXISTS separator_logo_url VARCHAR DEFAULT NULL;"),
            ("miniapp_categories", "model_img_shape", "ALTER TABLE miniapp_categories ADD COLUMN IF NOT EXISTS model_img_shape VARCHAR DEFAULT 'square';"),
            
            # 🆕 NOVAS COLUNAS DE COR DE TEXTO
            ("miniapp_categories", "separator_text_color", "ALTER TABLE miniapp_categories ADD COLUMN IF NOT EXISTS separator_text_color VARCHAR DEFAULT '#ffffff';"),
            ("miniapp_categories", "separator_btn_text_color", "ALTER TABLE miniapp_categories ADD COLUMN IF NOT EXISTS separator_btn_text_color VARCHAR DEFAULT '#ffffff';"),
            
            # 🆕 NOVO: EFEITO NEON
            ("miniapp_categories", "separator_is_neon", "ALTER TABLE miniapp_categories ADD COLUMN IF NOT EXISTS separator_is_neon BOOLEAN DEFAULT FALSE;"),
            ("miniapp_categories", "separator_neon_color", "ALTER TABLE miniapp_categories ADD COLUMN IF NOT EXISTS separator_neon_color VARCHAR DEFAULT NULL;")
        ]
        
        for tabela, coluna, sql in comandos_miniapp:
            try:
                db.execute(text(sql))
                db.commit()
                resultados.append(f"✅ {tabela}.{coluna} verificada/criada.")
            except Exception as e:
                db.rollback()
                # Ignora erros se a coluna já existir
                if "already exists" in str(e).lower() or "duplicate column" in str(e).lower():
                    resultados.append(f"ℹ️ {tabela}.{coluna} já existe (Ignorado)")
                else:
                    resultados.append(f"❌ {tabela}.{coluna}: {str(e)}")
        
        return {
            "status": "success",
            "message": "✅ Migração do Mini App V2 concluída (Com Cores e Neon)!",
            "log": resultados
        }
        
    except Exception as e:
        db.rollback()
        return {
            "status": "error",
            "message": f"❌ Erro crítico: {str(e)}"
        }

# ============================================================
# 🔒 MIGRAÇÃO: PROTEÇÃO DE CONTEÚDO (BOTS)
# ============================================================
@app.get("/migrate-protect-content")
async def migrate_protect_content(db: Session = Depends(get_db)):
    """
    Migração para a coluna protect_content na tabela bots.
    Acesse UMA VEZ: https://zenyx-gbs-testesv1-production.up.railway.app/migrate-protect-content
    """
    try:
        from sqlalchemy import text
        
        db.execute(text("ALTER TABLE bots ADD COLUMN IF NOT EXISTS protect_content BOOLEAN DEFAULT FALSE;"))
        db.commit()
        
        return {
            "status": "success",
            "message": "✅ Coluna 'protect_content' criada/verificada na tabela bots!"
        }
        
    except Exception as e:
        db.rollback()
        return {
            "status": "error",
            "message": f"❌ Erro: {str(e)}"
        }

# ============================================================
# 🔒 MIGRAÇÃO: AUDIO FEATURE (COMBO ÁUDIO + MÍDIA)
# ============================================================
@app.get("/migrate-audio-features")
async def migrate_audio_features(db: Session = Depends(get_db)):
    """
    Migração para adicionar colunas de áudio separado e delay nas tabelas de configuração.
    Tabelas afetadas: remarketing_config, canal_free_config, order_bump_config, upsell_config, downsell_config.
    
    Acesse UMA VEZ: https://zenyx-gbs-testesv1-production.up.railway.app/migrate-audio-features
    """
    try:
        from sqlalchemy import text
        
        # Lista das tabelas que receberão as novas colunas
        tabelas = [
            "remarketing_config",
            "canal_free_config",
            "order_bump_config",
            "upsell_config",
            "downsell_config"
        ]
        
        log_msgs = []
        
        for tabela in tabelas:
            # 1. Adicionar coluna audio_url (Texto/String)
            try:
                db.execute(text(f"ALTER TABLE {tabela} ADD COLUMN IF NOT EXISTS audio_url VARCHAR;"))
                log_msgs.append(f"✅ {tabela}: audio_url verificado.")
            except Exception as e:
                log_msgs.append(f"⚠️ {tabela} (audio_url): {str(e)}")

            # 2. Adicionar coluna audio_delay_seconds (Inteiro, padrão 0)
            try:
                db.execute(text(f"ALTER TABLE {tabela} ADD COLUMN IF NOT EXISTS audio_delay_seconds INTEGER DEFAULT 0;"))
                log_msgs.append(f"✅ {tabela}: audio_delay_seconds verificado.")
            except Exception as e:
                log_msgs.append(f"⚠️ {tabela} (audio_delay_seconds): {str(e)}")

        db.commit()
        
        return {
            "status": "success",
            "message": "Migração de Áudio + Mídia concluída!",
            "details": log_msgs
        }
        
    except Exception as e:
        db.rollback()
        return {
            "status": "error",
            "message": f"❌ Erro crítico na migração: {str(e)}"
        }
# ============================================================
# 🔒 MIGRAÇÃO: VERSÃO PRIME - AJUSTES E MELHORIAS
# ============================================================
@app.get("/migrate-prime-v1")
async def migrate_prime_v1(db: Session = Depends(get_db)):
    """
    Migração para adicionar novas colunas da Versão Prime:
    1. tracking_folders.owner_id (Integer FK users.id)
    2. bots.notificar_no_bot (Boolean DEFAULT TRUE)
    3. leads.origem_entrada (VARCHAR DEFAULT 'bot_direto')
    
    Acesse UMA VEZ: https://zenyx-gbs-testesv1-production.up.railway.app/migrate-prime-v1
    """
    try:
        from sqlalchemy import text
        
        log_msgs = []
        
        # 1. tracking_folders.owner_id
        try:
            db.execute(text("ALTER TABLE tracking_folders ADD COLUMN IF NOT EXISTS owner_id INTEGER REFERENCES users(id);"))
            log_msgs.append("✅ tracking_folders: owner_id adicionado")
        except Exception as e:
            log_msgs.append(f"⚠️ tracking_folders.owner_id: {str(e)}")
        
        # 2. bots.notificar_no_bot
        try:
            db.execute(text("ALTER TABLE bots ADD COLUMN IF NOT EXISTS notificar_no_bot BOOLEAN DEFAULT TRUE;"))
            log_msgs.append("✅ bots: notificar_no_bot adicionado")
        except Exception as e:
            log_msgs.append(f"⚠️ bots.notificar_no_bot: {str(e)}")
        
        # 3. leads.origem_entrada
        try:
            db.execute(text("ALTER TABLE leads ADD COLUMN IF NOT EXISTS origem_entrada VARCHAR DEFAULT 'bot_direto';"))
            log_msgs.append("✅ leads: origem_entrada adicionado")
        except Exception as e:
            log_msgs.append(f"⚠️ leads.origem_entrada: {str(e)}")
        
        # 4. Atribuir owner_id às pastas existentes baseado nos links dentro delas
        try:
            # Para cada pasta sem owner, tenta detectar o dono pelos links
            pastas_sem_dono = db.execute(text("SELECT id FROM tracking_folders WHERE owner_id IS NULL")).fetchall()
            pastas_corrigidas = 0
            
            for row in pastas_sem_dono:
                pasta_id = row[0]
                # Busca o owner do primeiro bot que tem link nessa pasta
                result = db.execute(text("""
                    SELECT DISTINCT b.owner_id 
                    FROM tracking_links tl 
                    JOIN bots b ON tl.bot_id = b.id 
                    WHERE tl.folder_id = :pid AND b.owner_id IS NOT NULL 
                    LIMIT 1
                """), {"pid": pasta_id}).fetchone()
                
                if result:
                    db.execute(text("UPDATE tracking_folders SET owner_id = :uid WHERE id = :pid"), 
                              {"uid": result[0], "pid": pasta_id})
                    pastas_corrigidas += 1
            
            log_msgs.append(f"✅ {pastas_corrigidas} pastas existentes receberam owner_id")
        except Exception as e:
            log_msgs.append(f"⚠️ Atribuição de owner_id: {str(e)}")
        
        db.commit()
        
        return {
            "status": "success",
            "message": "🚀 Migração Prime V1 concluída!",
            "details": log_msgs
        }
        
    except Exception as e:
        db.rollback()
        return {
            "status": "error",
            "message": f"❌ Erro crítico na migração: {str(e)}"
        }

# ============================================================
# 🔒 MIGRAÇÃO: SISTEMA DE LIMITES DE BOTS + SELETOR INTELIGENTE
# ============================================================
@app.get("/migrate-bot-limits-v1")
async def migrate_bot_limits_v1(db: Session = Depends(get_db)):
    """
    Migração para o sistema de limites de bots e seletor inteligente:
    1. users.plano_plataforma (VARCHAR DEFAULT 'free')
    2. users.max_bots (INTEGER DEFAULT 20)
    3. bots.selector_order (INTEGER DEFAULT 0)
    
    Acesse UMA VEZ: https://zenyx-gbs-testesv1-production.up.railway.app/migrate-bot-limits-v1
    """
    try:
        from sqlalchemy import text
        
        log_msgs = []
        
        # 1. users.plano_plataforma
        try:
            db.execute(text("ALTER TABLE users ADD COLUMN IF NOT EXISTS plano_plataforma VARCHAR DEFAULT 'free';"))
            db.commit()
            log_msgs.append("✅ users.plano_plataforma adicionado (default: 'free')")
        except Exception as e:
            db.rollback()
            log_msgs.append(f"⚠️ users.plano_plataforma: {str(e)}")
        
        # 2. users.max_bots
        try:
            db.execute(text("ALTER TABLE users ADD COLUMN IF NOT EXISTS max_bots INTEGER DEFAULT 20;"))
            db.commit()
            log_msgs.append("✅ users.max_bots adicionado (default: 20)")
        except Exception as e:
            db.rollback()
            log_msgs.append(f"⚠️ users.max_bots: {str(e)}")
        
        # 3. bots.selector_order
        try:
            db.execute(text("ALTER TABLE bots ADD COLUMN IF NOT EXISTS selector_order INTEGER DEFAULT 0;"))
            db.commit()
            log_msgs.append("✅ bots.selector_order adicionado (default: 0)")
        except Exception as e:
            db.rollback()
            log_msgs.append(f"⚠️ bots.selector_order: {str(e)}")
        
        # 4. Definir ordem inicial para bots existentes (por data de criação)
        try:
            owners = db.execute(text("SELECT DISTINCT owner_id FROM bots WHERE owner_id IS NOT NULL")).fetchall()
            total_updated = 0
            for row in owners:
                owner_id = row[0]
                bots = db.execute(text(
                    "SELECT id FROM bots WHERE owner_id = :oid ORDER BY created_at ASC"
                ), {"oid": owner_id}).fetchall()
                for idx, bot_row in enumerate(bots):
                    db.execute(text(
                        "UPDATE bots SET selector_order = :order WHERE id = :bid"
                    ), {"order": idx + 1, "bid": bot_row[0]})
                    total_updated += 1
            db.commit()
            log_msgs.append(f"✅ {total_updated} bots receberam ordem inicial no seletor")
        except Exception as e:
            db.rollback()
            log_msgs.append(f"⚠️ Ordem inicial: {str(e)}")
        
        return {
            "status": "success",
            "message": "🚀 Migração Bot Limits V1 concluída!",
            "details": log_msgs
        }
        
    except Exception as e:
        db.rollback()
        return {
            "status": "error",
            "message": f"❌ Erro crítico na migração: {str(e)}"
        }
# ============================================================
# ✨ MIGRAÇÃO: SISTEMA DE EMOJIS PREMIUM DO TELEGRAM
# ============================================================
@app.get("/migrate-premium-emojis-v1")
async def migrate_premium_emojis_v1(db: Session = Depends(get_db)):
    """
    Migração para criar as tabelas do sistema de emojis premium:
    1. premium_emoji_packs (categorias de emojis)
    2. premium_emojis (catálogo de custom emojis do Telegram)
    3. Popula com pacotes e emojis iniciais mais populares
    
    Acesse UMA VEZ: https://zenyx-gbs-testesv1-production.up.railway.app/migrate-premium-emojis-v1
    """
    try:
        from sqlalchemy import text
        
        log_msgs = []
        
        # 1. CRIAR TABELA premium_emoji_packs
        try:
            db.execute(text("""
                CREATE TABLE IF NOT EXISTS premium_emoji_packs (
                    id SERIAL PRIMARY KEY,
                    name VARCHAR(100) UNIQUE NOT NULL,
                    icon VARCHAR(10),
                    description VARCHAR(255),
                    sort_order INTEGER DEFAULT 0,
                    is_active BOOLEAN DEFAULT TRUE,
                    created_at TIMESTAMP WITHOUT TIME ZONE DEFAULT (NOW() AT TIME ZONE 'America/Sao_Paulo'),
                    updated_at TIMESTAMP WITHOUT TIME ZONE DEFAULT (NOW() AT TIME ZONE 'America/Sao_Paulo')
                );
            """))
            db.commit()
            log_msgs.append("✅ Tabela premium_emoji_packs criada")
        except Exception as e:
            db.rollback()
            log_msgs.append(f"⚠️ premium_emoji_packs: {str(e)}")
        
        # 2. CRIAR TABELA premium_emojis
        try:
            db.execute(text("""
                CREATE TABLE IF NOT EXISTS premium_emojis (
                    id SERIAL PRIMARY KEY,
                    emoji_id VARCHAR(50) UNIQUE NOT NULL,
                    fallback VARCHAR(10) NOT NULL,
                    name VARCHAR(100) NOT NULL,
                    shortcode VARCHAR(50) UNIQUE NOT NULL,
                    pack_id INTEGER REFERENCES premium_emoji_packs(id) ON DELETE SET NULL,
                    sort_order INTEGER DEFAULT 0,
                    thumbnail_url VARCHAR(500),
                    emoji_type VARCHAR(20) DEFAULT 'static',
                    is_active BOOLEAN DEFAULT TRUE,
                    created_at TIMESTAMP WITHOUT TIME ZONE DEFAULT (NOW() AT TIME ZONE 'America/Sao_Paulo'),
                    updated_at TIMESTAMP WITHOUT TIME ZONE DEFAULT (NOW() AT TIME ZONE 'America/Sao_Paulo')
                );
            """))
            db.commit()
            log_msgs.append("✅ Tabela premium_emojis criada")
        except Exception as e:
            db.rollback()
            log_msgs.append(f"⚠️ premium_emojis: {str(e)}")
        
        # 3. CRIAR ÍNDICES
        try:
            db.execute(text("CREATE INDEX IF NOT EXISTS idx_premium_emojis_pack ON premium_emojis(pack_id);"))
            db.execute(text("CREATE INDEX IF NOT EXISTS idx_premium_emojis_active ON premium_emojis(is_active);"))
            db.execute(text("CREATE INDEX IF NOT EXISTS idx_premium_emojis_shortcode ON premium_emojis(shortcode);"))
            db.execute(text("CREATE INDEX IF NOT EXISTS idx_premium_emoji_packs_active ON premium_emoji_packs(is_active);"))
            db.commit()
            log_msgs.append("✅ Índices criados")
        except Exception as e:
            db.rollback()
            log_msgs.append(f"⚠️ Índices: {str(e)}")
        
        # 4. POPULAR COM PACOTES INICIAIS (APENAS SE TABELA ESTIVER VAZIA)
        try:
            pack_count = db.execute(text("SELECT COUNT(*) FROM premium_emoji_packs")).scalar()
            if pack_count == 0:
                packs_iniciais = [
                    ("Populares", "🔥", "Emojis premium mais usados", 1),
                    ("Corações", "❤️", "Corações e amor", 2),
                    ("Mãos e Gestos", "👋", "Gestos e mãos animadas", 3),
                    ("Animais", "🐱", "Animais fofos e animados", 4),
                    ("Estrelas e Brilhos", "⭐", "Estrelas, brilhos e magia", 5),
                    ("Rostos", "😎", "Expressões e rostos animados", 6),
                    ("Objetos", "💎", "Objetos diversos", 7),
                    ("Natureza", "🌸", "Flores, plantas e natureza", 8),
                ]
                for name, icon, desc, order in packs_iniciais:
                    db.execute(text(
                        "INSERT INTO premium_emoji_packs (name, icon, description, sort_order) VALUES (:name, :icon, :desc, :order)"
                    ), {"name": name, "icon": icon, "desc": desc, "order": order})
                db.commit()
                log_msgs.append(f"✅ {len(packs_iniciais)} pacotes iniciais criados")
            else:
                log_msgs.append(f"ℹ️ Pacotes já existem ({pack_count}), pulando seed")
        except Exception as e:
            db.rollback()
            log_msgs.append(f"⚠️ Seed pacotes: {str(e)}")
        
        # 5. POPULAR COM EMOJIS PREMIUM POPULARES (SE TABELA VAZIA)
        try:
            emoji_count = db.execute(text("SELECT COUNT(*) FROM premium_emojis")).scalar()
            if emoji_count == 0:
                # Buscar ID do pacote "Populares"
                pop_pack = db.execute(text("SELECT id FROM premium_emoji_packs WHERE name = 'Populares' LIMIT 1")).fetchone()
                hearts_pack = db.execute(text("SELECT id FROM premium_emoji_packs WHERE name = 'Corações' LIMIT 1")).fetchone()
                stars_pack = db.execute(text("SELECT id FROM premium_emoji_packs WHERE name = 'Estrelas e Brilhos' LIMIT 1")).fetchone()
                
                pop_id = pop_pack[0] if pop_pack else None
                hearts_id = hearts_pack[0] if hearts_pack else None
                stars_id = stars_pack[0] if stars_pack else None
                
                # Emojis Premium Populares (IDs reais do Telegram)
                # NOTA: Esses IDs são exemplos conhecidos. O Super Admin pode adicionar mais via painel.
                emojis_iniciais = [
                    # Populares
                    ("5368324170671202286", "🔥", "Fogo Animado", ":fire_premium:", pop_id, "animated", 1),
                    ("5271930982462988357", "⭐", "Estrela Brilhante", ":star_premium:", pop_id, "animated", 2),
                    ("5443038326535759171", "💎", "Diamante Azul", ":diamond_premium:", pop_id, "animated", 3),
                    ("5420323339421498693", "🚀", "Foguete Animado", ":rocket_premium:", pop_id, "animated", 4),
                    ("5368324170671202286", "✅", "Check Verde", ":check_premium:", pop_id, "animated", 5),
                    ("5247151702498836708", "🎯", "Alvo Certeiro", ":target_premium:", pop_id, "animated", 6),
                    ("5407025283456835913", "👑", "Coroa Dourada", ":crown_premium:", pop_id, "animated", 7),
                    ("5386654653003864312", "🎁", "Presente Animado", ":gift_premium:", pop_id, "animated", 8),
                    # Corações
                    ("5368324170671202286", "❤️", "Coração Vermelho", ":heart_premium:", hearts_id, "animated", 1),
                    ("5445284980978621387", "💜", "Coração Roxo", ":purple_heart_premium:", hearts_id, "animated", 2),
                    ("5368324170671202286", "❤️‍🔥", "Coração em Chamas", ":fire_heart_premium:", hearts_id, "animated", 3),
                    # Estrelas
                    ("5368324170671202286", "🌟", "Estrela Glow", ":glow_star_premium:", stars_id, "animated", 1),
                    ("5368324170671202286", "✨", "Brilho Mágico", ":sparkle_premium:", stars_id, "animated", 2),
                ]
                
                inserted = 0
                for eid, fb, name, sc, pid, etype, sorder in emojis_iniciais:
                    try:
                        db.execute(text(
                            """INSERT INTO premium_emojis (emoji_id, fallback, name, shortcode, pack_id, emoji_type, sort_order) 
                               VALUES (:eid, :fb, :name, :sc, :pid, :etype, :sorder)"""
                        ), {"eid": eid + str(sorder), "fb": fb, "name": name, "sc": sc, "pid": pid, "etype": etype, "sorder": sorder})
                        inserted += 1
                    except Exception:
                        pass  # Ignora duplicados
                
                db.commit()
                log_msgs.append(f"✅ {inserted} emojis premium iniciais cadastrados")
                log_msgs.append("⚠️ IMPORTANTE: Os emoji_ids iniciais são EXEMPLOS. Use @JsonDumpBot no Telegram para obter IDs reais e atualize pelo painel Super Admin.")
            else:
                log_msgs.append(f"ℹ️ Emojis já existem ({emoji_count}), pulando seed")
        except Exception as e:
            db.rollback()
            log_msgs.append(f"⚠️ Seed emojis: {str(e)}")
        
        return {
            "status": "success",
            "message": "✨ Migração Premium Emojis V1 concluída!",
            "details": log_msgs,
            "next_steps": [
                "1. Acesse o Painel Super Admin → Emojis Premium",
                "2. Use @JsonDumpBot no Telegram para descobrir emoji_ids reais",
                "3. Cadastre emojis com IDs corretos pelo painel",
                "4. Os usuários poderão usar os emojis nas páginas de texto/legenda"
            ]
        }
        
    except Exception as e:
        db.rollback()
        return {
            "status": "error",
            "message": f"❌ Erro crítico na migração: {str(e)}"
        }

# --- ENDPOINT PÚBLICO: Enviar Denúncia (sem autenticação) ---
class ReportSubmit(BaseModel):
    reporter_name: Optional[str] = None
    bot_username: str
    reason: str  # 'cp', 'fraud', 'scam', 'spam', 'illegal', 'other'
    description: Optional[str] = None
    evidence_url: Optional[str] = None

@app.post("/api/public/reports")
def submit_report(data: ReportSubmit, request: Request, db: Session = Depends(get_db)):
    """Endpoint PÚBLICO para enviar denúncia (acessível via portal /denunciar)"""
    
    # Validação básica
    valid_reasons = ['cp', 'fraud', 'scam', 'spam', 'illegal', 'harassment', 'other']
    if data.reason not in valid_reasons:
        raise HTTPException(400, f"Motivo inválido. Opções: {', '.join(valid_reasons)}")
    
    if not data.bot_username or len(data.bot_username.strip()) < 2:
        raise HTTPException(400, "Username do bot é obrigatório")
    
    # Tenta encontrar o bot no sistema
    clean_username = data.bot_username.replace("@", "").strip().lower()
    
    # 🔥 CORREÇÃO: Busca usando 'or_' para bater com o USERNAME ou com o NOME do Bot
    bot_found = db.query(BotModel).filter(
        or_(
            func.lower(BotModel.username) == clean_username,
            func.lower(BotModel.nome) == clean_username
        )
    ).first()
    
    # 🔥 NOVO: Captura os dados do dono do bot com segurança tripla
    owner_id = None
    owner_username = None
    if bot_found:
        # Pega o dono do bot. Alguns sistemas usam owner_id ou user_id
        bot_owner_id = getattr(bot_found, 'owner_id', None) or getattr(bot_found, 'user_id', None)
        if bot_owner_id:
            owner = db.query(User).filter(User.id == bot_owner_id).first()
            if owner:
                owner_id = owner.id
                owner_username = owner.username
    
    # Captura IP do denunciante (para segurança)
    client_ip = request.headers.get("x-forwarded-for", request.client.host if request.client else "unknown")
    if "," in client_ip:
        client_ip = client_ip.split(",")[0].strip()
    
    report = Report(
        reporter_name=data.reporter_name,
        bot_username=clean_username,
        bot_id=bot_found.id if bot_found else None,
        owner_id=owner_id,              # 🔥 AGORA VAI SALVAR CORRETAMENTE
        owner_username=owner_username,  # 🔥 AGORA VAI SALVAR CORRETAMENTE
        reason=data.reason,
        description=data.description,
        evidence_url=data.evidence_url,
        status='pending',
        ip_address=client_ip
    )
    
    db.add(report)
    db.commit()
    db.refresh(report)
    
    # Notifica Super Admins
    try:
        super_admins = db.query(User).filter(User.is_superuser == True).all()
        reason_labels = {
            'cp': '🔴 Pornografia Infantil', 'fraud': '🟠 Fraude', 'scam': '🟠 Golpe',
            'spam': '🟡 Spam', 'illegal': '🔴 Conteúdo Ilegal', 'harassment': '🟡 Assédio', 'other': '⚪ Outro'
        }
        for admin in super_admins:
            notif = Notification(
                user_id=admin.id,
                title="🚨 Nova Denúncia Recebida",
                message=f"Bot: @{clean_username} | Motivo: {reason_labels.get(data.reason, data.reason)}",
                type="report",
                link="/superadmin/reports"
            )
            db.add(notif)
        db.commit()
    except:
        pass
    
    logger.info(f"🚨 [REPORT] Nova denúncia #{report.id} | Bot: @{clean_username} | Owner: {owner_username} | Motivo: {data.reason}")
    
    return {
        "status": "success", 
        "message": "Denúncia enviada com sucesso. Nossa equipe irá analisar.",
        "report_id": report.id
    }

# --- SUPERADMIN: Listar Denúncias ---
@app.get("/api/superadmin/reports")
def list_reports(
    status: Optional[str] = None,
    page: int = 1,
    per_page: int = 20,
    db: Session = Depends(get_db),
    current_user = Depends(get_current_user)
):
    if not getattr(current_user, 'is_superuser', False):
        raise HTTPException(403, "Acesso negado")
    
    query = db.query(Report).order_by(desc(Report.created_at))
    
    if status:
        query = query.filter(Report.status == status)
    
    total = query.count()
    reports = query.offset((page - 1) * per_page).limit(per_page).all()
    
    return {
        "reports": [{
            "id": r.id,
            "reporter_name": r.reporter_name,
            "bot_username": r.bot_username,
            "bot_id": r.bot_id,
            "owner_id": getattr(r, 'owner_id', None),              # 🔥 INSERIDO
            "owner_username": getattr(r, 'owner_username', None),  # 🔥 INSERIDO
            "reason": r.reason,
            "description": r.description,
            "evidence_url": r.evidence_url,
            "status": r.status,
            "resolution": r.resolution,
            "action_taken": r.action_taken,
            "created_at": r.created_at.isoformat() if r.created_at else None,
            "resolved_at": r.resolved_at.isoformat() if r.resolved_at else None,
        } for r in reports],
        "total": total,
        "page": page,
        "pages": (total + per_page - 1) // per_page
    }


# --- SUPERADMIN: Resolver Denúncia + Aplicar Punição ---
class ReportResolve(BaseModel):
    status: str  # 'resolved', 'dismissed'
    resolution: Optional[str] = None
    action: Optional[str] = None  # 'warning', 'strike', 'pause_bots', 'ban_account', 'none'
    pause_days: Optional[int] = None
    tax_increase_pct: Optional[float] = None

@app.put("/api/superadmin/reports/{report_id}/resolve")
def resolve_report(
    report_id: int,
    data: ReportResolve,
    db: Session = Depends(get_db),
    current_user = Depends(get_current_user)
):
    if not getattr(current_user, 'is_superuser', False):
        raise HTTPException(403, "Acesso negado")
    
    report = db.query(Report).filter(Report.id == report_id).first()
    if not report:
        raise HTTPException(404, "Denúncia não encontrada")
    
    report.status = data.status
    report.resolution = data.resolution
    report.resolved_by = current_user.id
    report.resolved_at = now_brazil()
    report.action_taken = data.action
    
    # Se tem bot vinculado (ou owner_id mapeado), aplica punição
    user_target = None
    
    if getattr(report, 'owner_id', None):
        user_target = db.query(User).filter(User.id == report.owner_id).first()
    elif report.bot_id:
        bot = db.query(BotModel).filter(BotModel.id == report.bot_id).first()
        if bot:
            user_target = db.query(User).filter(User.id == bot.owner_id).first()

    if data.action and data.action != 'none' and user_target:
        if data.action == 'strike':
            # Incrementa strike
            current_strikes = user_target.strike_count or 0
            new_strike = current_strikes + 1
            user_target.strike_count = new_strike
            
            strike = UserStrike(
                user_id=user_target.id,
                report_id=report.id,
                reason=data.resolution or f"Denúncia #{report.id}",
                strike_number=new_strike,
                action='strike',
                applied_by=current_user.id
            )
            db.add(strike)
            
            # 3 strikes = ban automático
            if new_strike >= 3:
                user_target.is_banned = True
                user_target.banned_reason = f"3 strikes atingidos. Última denúncia: #{report.id}"
                user_target.is_active = False
                # Pausar todos os bots
                for ub in db.query(BotModel).filter(BotModel.owner_id == user_target.id).all():
                    ub.status = "pausado"
                report.action_taken = 'ban_account'
                logger.warning(f"🚫 [REPORT] Usuário {user_target.username} BANIDO (3 strikes)")
            
        elif data.action == 'pause_bots':
            days = data.pause_days or 7
            user_target.bots_paused_until = now_brazil() + timedelta(days=days)
            
            current_strikes = user_target.strike_count or 0
            new_strike = current_strikes + 1
            
            strike = UserStrike(
                user_id=user_target.id, report_id=report.id,
                reason=data.resolution or f"Bots pausados por {days} dias",
                strike_number=new_strike,
                action='pause_bots', pause_until=user_target.bots_paused_until,
                applied_by=current_user.id
            )
            db.add(strike)
            user_target.strike_count = new_strike
            logger.warning(f"⏸️ [REPORT] Bots do usuário {user_target.username} pausados por {days} dias")
            
        elif data.action == 'ban_account':
            user_target.is_banned = True
            user_target.banned_reason = data.resolution or f"Banido por denúncia #{report.id}"
            user_target.is_active = False
            
            # 🔒 Pausar TODOS os bots do usuário banido
            user_bots = db.query(BotModel).filter(BotModel.owner_id == user_target.id).all()
            for ub in user_bots:
                ub.status = "pausado"
            
            # Registrar strike de ban
            current_strikes = user_target.strike_count or 0
            new_strike = current_strikes + 1
            strike = UserStrike(
                user_id=user_target.id, report_id=report.id,
                reason=data.resolution or f"Conta banida por denúncia #{report.id}",
                strike_number=new_strike,
                action='ban_account',
                applied_by=current_user.id
            )
            db.add(strike)
            user_target.strike_count = new_strike
            logger.warning(f"🚫 [REPORT] Usuário {user_target.username} BANIDO | {len(user_bots)} bots pausados")
            
        elif data.action == 'warning':
            # Apenas registra aviso sem punição real
            strike = UserStrike(
                user_id=user_target.id, report_id=report.id,
                reason=data.resolution or f"Aviso por denúncia #{report.id}",
                strike_number=(user_target.strike_count or 0),
                action='warning',
                applied_by=current_user.id
            )
            db.add(strike)
            logger.info(f"⚠️ [REPORT] Aviso registrado para {user_target.username}")
            
        elif data.action == 'tax_increase':
            pass
    
    db.commit()
    
    logger.info(f"✅ [REPORT] Denúncia #{report_id} resolvida | Ação: {data.action} | Por: {current_user.username}")
    
    return {"status": "success", "message": f"Denúncia resolvida com ação: {data.action}"}


# --- SUPERADMIN: REVERTER PUNIÇÃO DE DENÚNCIA ---
class ReportRevert(BaseModel):
    reason: Optional[str] = None

@app.put("/api/superadmin/reports/{report_id}/revert")
def revert_report_punishment(
    report_id: int,
    data: ReportRevert,
    db: Session = Depends(get_db),
    current_user = Depends(get_current_user)
):
    """Reverte TODAS as punições aplicadas por uma denúncia, restaurando conta e bots."""
    if not getattr(current_user, 'is_superuser', False):
        raise HTTPException(403, "Acesso negado")
    
    report = db.query(Report).filter(Report.id == report_id).first()
    if not report:
        raise HTTPException(404, "Denúncia não encontrada")
    
    if report.status not in ['resolved']:
        raise HTTPException(400, "Só é possível reverter denúncias resolvidas")
    
    # Encontrar o usuário alvo
    user_target = None
    if getattr(report, 'owner_id', None):
        user_target = db.query(User).filter(User.id == report.owner_id).first()
    elif report.bot_id:
        bot = db.query(BotModel).filter(BotModel.id == report.bot_id).first()
        if bot:
            user_target = db.query(User).filter(User.id == bot.owner_id).first()
    
    if not user_target:
        raise HTTPException(404, "Usuário alvo não encontrado para reverter")
    
    action_was = report.action_taken
    revert_details = []
    
    # 1. Remover BAN
    if user_target.is_banned:
        user_target.is_banned = False
        user_target.banned_reason = None
        user_target.is_active = True
        revert_details.append("Ban removido")
    
    # 2. Remover PAUSA temporária
    if user_target.bots_paused_until:
        user_target.bots_paused_until = None
        revert_details.append("Pausa de bots removida")
    
    # 3. Reativar bots pausados
    user_bots = db.query(BotModel).filter(
        BotModel.owner_id == user_target.id,
        BotModel.status == "pausado"
    ).all()
    if user_bots:
        for ub in user_bots:
            ub.status = "ativo"
        revert_details.append(f"{len(user_bots)} bot(s) reativado(s)")
    
    # 4. Decrementar strike (mínimo 0)
    if action_was in ['strike', 'pause_bots', 'ban_account']:
        current_strikes = user_target.strike_count or 0
        if current_strikes > 0:
            user_target.strike_count = current_strikes - 1
            revert_details.append(f"Strike: {current_strikes} → {current_strikes - 1}")
    
    # 5. Remover registro de strike vinculado a esta denúncia
    deleted = db.query(UserStrike).filter(UserStrike.report_id == report.id).delete()
    if deleted:
        revert_details.append(f"{deleted} registro(s) de strike removido(s)")
    
    # 6. Reativar conta se estava inativa
    if not user_target.is_active:
        user_target.is_active = True
        revert_details.append("Conta reativada")
    
    # 7. Atualizar denúncia
    report.status = "dismissed"
    report.resolution = (report.resolution or "") + f"\n\n🔄 REVERTIDO por @{current_user.username}: {data.reason or 'Sem motivo'}"
    report.action_taken = f"revertido_{action_was or 'unknown'}"
    
    db.commit()
    
    summary = " | ".join(revert_details) if revert_details else "Nenhuma alteração necessária"
    logger.info(f"🔄 [REPORT] Denúncia #{report_id} REVERTIDA por {current_user.username} | {summary}")
    
    return {
        "status": "success",
        "message": "Punição revertida com sucesso",
        "details": revert_details,
        "user": user_target.username
    }


# ============================================================
# 🚨 MIGRAÇÃO V1: SISTEMA DE DENÚNCIAS 
# ============================================================
@app.get("/migrate-reports-v1")
async def migrate_reports_v1(db: Session = Depends(get_db)):
    """
    Migração para criar tabelas do sistema de denúncias.
    """
    try:
        from sqlalchemy import text
        log_msgs = []
        
        try:
            db.execute(text("""
                CREATE TABLE IF NOT EXISTS reports (
                    id SERIAL PRIMARY KEY,
                    reporter_name VARCHAR(100),
                    reporter_telegram_id VARCHAR(50),
                    bot_username VARCHAR(100) NOT NULL,
                    bot_id INTEGER REFERENCES bots(id) ON DELETE SET NULL,
                    reason VARCHAR(50) NOT NULL,
                    description TEXT,
                    evidence_url VARCHAR(500),
                    status VARCHAR(20) DEFAULT 'pending',
                    resolution TEXT,
                    resolved_by INTEGER REFERENCES users(id) ON DELETE SET NULL,
                    resolved_at TIMESTAMP WITHOUT TIME ZONE,
                    action_taken VARCHAR(50),
                    strike_count INTEGER DEFAULT 0,
                    created_at TIMESTAMP WITHOUT TIME ZONE DEFAULT (NOW() AT TIME ZONE 'America/Sao_Paulo'),
                    updated_at TIMESTAMP WITHOUT TIME ZONE DEFAULT (NOW() AT TIME ZONE 'America/Sao_Paulo'),
                    ip_address VARCHAR(50)
                );
            """))
            db.commit()
            log_msgs.append("✅ Tabela reports criada")
        except Exception as e:
            db.rollback()
            log_msgs.append(f"⚠️ reports: {str(e)}")
        
        try:
            db.execute(text("""
                CREATE TABLE IF NOT EXISTS user_strikes (
                    id SERIAL PRIMARY KEY,
                    user_id INTEGER REFERENCES users(id) ON DELETE CASCADE NOT NULL,
                    report_id INTEGER REFERENCES reports(id) ON DELETE SET NULL,
                    reason TEXT NOT NULL,
                    strike_number INTEGER NOT NULL,
                    action VARCHAR(50) NOT NULL,
                    pause_until TIMESTAMP WITHOUT TIME ZONE,
                    tax_increase_pct FLOAT,
                    applied_by INTEGER REFERENCES users(id) ON DELETE SET NULL,
                    created_at TIMESTAMP WITHOUT TIME ZONE DEFAULT (NOW() AT TIME ZONE 'America/Sao_Paulo')
                );
            """))
            db.commit()
            log_msgs.append("✅ Tabela user_strikes criada")
        except Exception as e:
            db.rollback()
            log_msgs.append(f"⚠️ user_strikes: {str(e)}")
        
        # Colunas extras na tabela users
        for col_sql in [
            "ALTER TABLE users ADD COLUMN IF NOT EXISTS strike_count INTEGER DEFAULT 0;",
            "ALTER TABLE users ADD COLUMN IF NOT EXISTS is_banned BOOLEAN DEFAULT FALSE;",
            "ALTER TABLE users ADD COLUMN IF NOT EXISTS banned_reason TEXT;",
            "ALTER TABLE users ADD COLUMN IF NOT EXISTS bots_paused_until TIMESTAMP WITHOUT TIME ZONE;",
        ]:
            try:
                db.execute(text(col_sql))
                db.commit()
            except:
                db.rollback()
        
        log_msgs.append("✅ Colunas de punição adicionadas à tabela users")
        
        # Índices
        try:
            db.execute(text("CREATE INDEX IF NOT EXISTS idx_reports_status ON reports(status);"))
            db.execute(text("CREATE INDEX IF NOT EXISTS idx_reports_bot ON reports(bot_username);"))
            db.execute(text("CREATE INDEX IF NOT EXISTS idx_user_strikes_user ON user_strikes(user_id);"))
            db.commit()
            log_msgs.append("✅ Índices criados")
        except:
            db.rollback()
        
        return {
            "status": "success",
            "message": "🚨 Migração de Denúncias V1 concluída!",
            "details": log_msgs
        }
    except Exception as e:
        db.rollback()
        return {"status": "error", "message": f"❌ Erro: {str(e)}"}

# ============================================================
# 🚨 MIGRAÇÃO V2: ATUALIZAÇÃO DO SISTEMA DE DENÚNCIAS 
# ============================================================
@app.get("/migrate-reports-v2")
async def migrate_reports_v2(db: Session = Depends(get_db)):
    """
    Migração para adicionar as colunas owner_id e owner_username na tabela reports.
    Acesse UMA VEZ: https://zenyx-gbs-testesv1-production.up.railway.app/migrate-reports-v2
    """
    try:
        from sqlalchemy import text
        log_msgs = []
        
        try:
            db.execute(text("ALTER TABLE reports ADD COLUMN IF NOT EXISTS owner_id INTEGER REFERENCES users(id) ON DELETE SET NULL;"))
            db.commit()
            log_msgs.append("✅ Coluna owner_id adicionada em reports")
        except Exception as e:
            db.rollback()
            log_msgs.append(f"⚠️ owner_id: {str(e)}")
            
        try:
            db.execute(text("ALTER TABLE reports ADD COLUMN IF NOT EXISTS owner_username VARCHAR(100);"))
            db.commit()
            log_msgs.append("✅ Coluna owner_username adicionada em reports")
        except Exception as e:
            db.rollback()
            log_msgs.append(f"⚠️ owner_username: {str(e)}")
        
        # Opcional: Atualizar relatórios antigos retroativamente
        try:
            reports = db.execute(text("SELECT id, bot_id FROM reports WHERE owner_id IS NULL AND bot_id IS NOT NULL")).fetchall()
            updated = 0
            for r in reports:
                bot = db.execute(text("SELECT owner_id FROM bots WHERE id = :bid"), {"bid": r[1]}).fetchone()
                if bot and bot[0]:
                    user = db.execute(text("SELECT username FROM users WHERE id = :uid"), {"uid": bot[0]}).fetchone()
                    if user:
                        db.execute(text("UPDATE reports SET owner_id = :uid, owner_username = :uname WHERE id = :rid"), 
                                   {"uid": bot[0], "uname": user[0], "rid": r[0]})
                        updated += 1
            db.commit()
            log_msgs.append(f"✅ {updated} denúncias antigas retroativamente atualizadas com o dono do bot.")
        except Exception as e:
            db.rollback()
            log_msgs.append(f"⚠️ Atualização retroativa: {str(e)}")

        return {
            "status": "success",
            "message": "🚨 Migração de Denúncias V2 concluída!",
            "details": log_msgs
        }
    except Exception as e:
        db.rollback()
        return {"status": "error", "message": f"❌ Erro: {str(e)}"}

# ============================================================
# 📅 REMARKETING AGENDADO - ENDPOINTS
# ============================================================

class ScheduledCampaignCreate(BaseModel):
    bot_id: int
    target: str = 'todos'  # todos, topo, meio, fundo, expirados
    schedule_days: int = 7  # 1-365 dias
    schedule_time: str = '10:00'  # Horário HH:MM (Brasília)
    use_same_content: bool = True
    # Conteúdo padrão (se use_same_content=True)
    message: Optional[str] = None
    media_url: Optional[str] = None
    plano_id: Optional[int] = None
    promo_price: Optional[float] = None
    # Conteúdo por dia (se use_same_content=False)
    days_config: Optional[list] = None  # [{day:1, message, media_url, plano_id, promo_price}, ...]

@app.post("/api/admin/remarketing/schedule")
def create_scheduled_campaign(
    data: ScheduledCampaignCreate,
    db: Session = Depends(get_db),
    current_user = Depends(get_current_user)
):
    """Cria campanha de remarketing agendada"""
    
    if data.schedule_days < 1 or data.schedule_days > 365:
        raise HTTPException(400, "Dias deve ser entre 1 e 365")
    
    # Monta config
    config_data = {
        "mensagem": data.message or "",
        "media_url": data.media_url or "",
    }
    
    # Config por dia
    days_json = None
    if not data.use_same_content and data.days_config:
        days_json = json.dumps(data.days_config)
    
    uuid_campanha = f"sched_{uuid.uuid4().hex[:8]}"
    
    # Calcula próxima execução
    tz_br = timezone('America/Sao_Paulo')
    hora, minuto = map(int, data.schedule_time.split(':'))
    agora = now_brazil()
    proxima = agora.replace(hour=hora, minute=minuto, second=0, microsecond=0)
    if proxima <= agora:
        proxima += timedelta(days=1)
    
    end_date = proxima + timedelta(days=data.schedule_days - 1)
    
    nova = RemarketingCampaign(
        bot_id=data.bot_id,
        campaign_id=uuid_campanha,
        type="agendado",
        target=data.target,
        config=json.dumps(config_data),
        status='agendado',
        is_scheduled=True,
        schedule_days=data.schedule_days,
        schedule_time=data.schedule_time,
        schedule_end_date=end_date,
        days_config=days_json,
        use_same_content=data.use_same_content,
        schedule_active=True,
        dia_atual=0,
        data_inicio=agora,
        proxima_execucao=proxima,
        plano_id=data.plano_id,
        promo_price=data.promo_price,
        total_leads=0,
        sent_success=0,
        blocked_count=0,
        data_envio=agora
    )
    
    db.add(nova)
    db.commit()
    db.refresh(nova)
    
    logger.info(f"📅 [SCHEDULED] Campanha #{nova.id} criada | {data.schedule_days} dias | {data.schedule_time} | Target: {data.target}")
    
    return {
        "status": "success",
        "message": f"Campanha agendada para {data.schedule_days} dias, disparando às {data.schedule_time}.",
        "campaign_id": nova.id,
        "proxima_execucao": proxima.isoformat()
    }


@app.get("/api/admin/remarketing/scheduled/{bot_id}")
def list_scheduled_campaigns(bot_id: int, db: Session = Depends(get_db)):
    """Lista campanhas agendadas de um bot"""
    campaigns = db.query(RemarketingCampaign).filter(
        RemarketingCampaign.bot_id == bot_id,
        RemarketingCampaign.is_scheduled == True
    ).order_by(desc(RemarketingCampaign.created_at if hasattr(RemarketingCampaign, 'created_at') else RemarketingCampaign.data_envio)).all()
    
    return [{
        "id": c.id,
        "target": c.target,
        "schedule_days": c.schedule_days,
        "schedule_time": c.schedule_time,
        "dia_atual": c.dia_atual,
        "schedule_active": c.schedule_active,
        "use_same_content": c.use_same_content,
        "proxima_execucao": c.proxima_execucao.isoformat() if c.proxima_execucao else None,
        "schedule_end_date": c.schedule_end_date.isoformat() if c.schedule_end_date else None,
        "plano_id": c.plano_id,
        "promo_price": c.promo_price,
        "status": c.status,
        "sent_success": c.sent_success,
        "data_inicio": c.data_inicio.isoformat() if c.data_inicio else None,
    } for c in campaigns]


@app.put("/api/admin/remarketing/scheduled/{campaign_id}/toggle")
def toggle_scheduled_campaign(campaign_id: int, db: Session = Depends(get_db)):
    """Ativa/desativa campanha agendada"""
    c = db.query(RemarketingCampaign).filter(RemarketingCampaign.id == campaign_id).first()
    if not c:
        raise HTTPException(404, "Campanha não encontrada")
    c.schedule_active = not c.schedule_active
    db.commit()
    return {"status": "success", "active": c.schedule_active}


@app.delete("/api/admin/remarketing/scheduled/{campaign_id}")
def delete_scheduled_campaign(campaign_id: int, db: Session = Depends(get_db)):
    """Deleta campanha agendada"""
    c = db.query(RemarketingCampaign).filter(RemarketingCampaign.id == campaign_id).first()
    if not c:
        raise HTTPException(404)
    db.delete(c)
    db.commit()
    return {"status": "deleted"}


# ============================================================
# 📅 MIGRAÇÃO: REMARKETING AGENDADO
# ============================================================
@app.get("/migrate-scheduled-remarketing-v1")
async def migrate_scheduled_remarketing_v1(db: Session = Depends(get_db)):
    """
    Migração para adicionar colunas de remarketing agendado.
    Acesse UMA VEZ: /migrate-scheduled-remarketing-v1
    """
    try:
        from sqlalchemy import text
        log_msgs = []
        
        cols = [
            "ALTER TABLE remarketing_campaigns ADD COLUMN IF NOT EXISTS is_scheduled BOOLEAN DEFAULT FALSE;",
            "ALTER TABLE remarketing_campaigns ADD COLUMN IF NOT EXISTS schedule_days INTEGER DEFAULT 1;",
            "ALTER TABLE remarketing_campaigns ADD COLUMN IF NOT EXISTS schedule_time VARCHAR DEFAULT '10:00';",
            "ALTER TABLE remarketing_campaigns ADD COLUMN IF NOT EXISTS schedule_end_date TIMESTAMP WITHOUT TIME ZONE;",
            "ALTER TABLE remarketing_campaigns ADD COLUMN IF NOT EXISTS days_config TEXT;",
            "ALTER TABLE remarketing_campaigns ADD COLUMN IF NOT EXISTS use_same_content BOOLEAN DEFAULT TRUE;",
            "ALTER TABLE remarketing_campaigns ADD COLUMN IF NOT EXISTS schedule_active BOOLEAN DEFAULT FALSE;",
        ]
        
        for sql in cols:
            try:
                db.execute(text(sql))
                db.commit()
            except:
                db.rollback()
        
        log_msgs.append("✅ Colunas de agendamento adicionadas à remarketing_campaigns")
        
        # Índice
        try:
            db.execute(text("CREATE INDEX IF NOT EXISTS idx_remarketing_scheduled ON remarketing_campaigns(is_scheduled, schedule_active);"))
            db.commit()
            log_msgs.append("✅ Índice criado")
        except:
            db.rollback()
        
        return {
            "status": "success",
            "message": "📅 Migração Remarketing Agendado V1 concluída!",
            "details": log_msgs
        }
    except Exception as e:
        db.rollback()
        return {"status": "error", "message": f"❌ Erro: {str(e)}"}


# =========================================================
# 📓 DIÁRIO DE MUDANÇAS - ENDPOINTS
# =========================================================

class ChangeLogCreate(BaseModel):
    date: Optional[str] = None
    category: Optional[str] = "geral"
    content: str

@app.post("/api/admin/changelog")
def create_changelog(
    data: ChangeLogCreate,
    db: Session = Depends(get_db),
    current_user = Depends(get_current_user)
):
    from datetime import datetime as dt
    note_date = now_brazil()
    if data.date:
        try:
            note_date = dt.strptime(data.date, "%Y-%m-%d")
            note_date = timezone('America/Sao_Paulo').localize(note_date)
        except:
            pass
    
    cl = ChangeLog(
        user_id=current_user.id,
        date=note_date,
        category=data.category or "geral",
        content=data.content
    )
    db.add(cl)
    db.commit()
    db.refresh(cl)
    return {"id": cl.id, "date": cl.date.strftime("%d/%m/%Y"), "category": cl.category, "content": cl.content}

@app.get("/api/admin/changelog")
def list_changelog(
    db: Session = Depends(get_db),
    current_user = Depends(get_current_user)
):
    logs = db.query(ChangeLog).filter(
        ChangeLog.user_id == current_user.id
    ).order_by(desc(ChangeLog.date)).limit(50).all()
    return [{
        "id": cl.id,
        "date": cl.date.strftime("%d/%m/%Y") if cl.date else "",
        "category": cl.category,
        "content": cl.content,
    } for cl in logs]

@app.delete("/api/admin/changelog/{log_id}")
def delete_changelog(
    log_id: int,
    db: Session = Depends(get_db),
    current_user = Depends(get_current_user)
):
    cl = db.query(ChangeLog).filter(ChangeLog.id == log_id, ChangeLog.user_id == current_user.id).first()
    if not cl:
        raise HTTPException(404, "Nota não encontrada")
    db.delete(cl)
    db.commit()
    return {"ok": True}


# =========================================================
# 🗄️ MIGRAÇÃO V2: Diário de Mudanças
# =========================================================
@app.get("/migrate-changelog-v1")
def migrate_changelog_v1(db: Session = Depends(get_db)):
    """
    Cria tabela change_logs para o Diário de Mudanças.
    Acesse UMA VEZ: /migrate-changelog-v1
    """
    log_msgs = []
    try:
        db.execute(text("""
            CREATE TABLE IF NOT EXISTS change_logs (
                id SERIAL PRIMARY KEY,
                user_id INTEGER REFERENCES users(id) ON DELETE CASCADE,
                bot_id INTEGER REFERENCES bots(id) ON DELETE SET NULL,
                date TIMESTAMP DEFAULT NOW(),
                category VARCHAR(50) DEFAULT 'geral',
                content TEXT NOT NULL,
                created_at TIMESTAMP DEFAULT NOW()
            );
        """))
        db.commit()
        log_msgs.append("✅ Tabela change_logs criada")
        
        db.execute(text("CREATE INDEX IF NOT EXISTS idx_changelog_user ON change_logs(user_id);"))
        db.commit()
        log_msgs.append("✅ Índice criado")
        
        return {"status": "success", "message": "📓 Migração Diário de Mudanças V1 concluída!", "details": log_msgs}
    except Exception as e:
        db.rollback()
        return {"status": "error", "message": f"❌ Erro: {str(e)}"}

@app.get("/migrate-statistics-v18")
def migrate_statistics_v18(db: Session = Depends(get_db)):
    """Cria índices para performance V18. Acesse UMA VEZ: https://zenyx-gbs-testesv1-production.up.railway.app/migrate-statistics-v18"""
    log_msgs = []
    try:
        cmds = [
            "ALTER TABLE pedidos ADD COLUMN IF NOT EXISTS origem VARCHAR(50) DEFAULT 'bot';",
            "ALTER TABLE pedidos ADD COLUMN IF NOT EXISTS gateway_usada VARCHAR;",
            "CREATE INDEX IF NOT EXISTS idx_pedidos_data_aprov ON pedidos(data_aprovacao);",
            "CREATE INDEX IF NOT EXISTS idx_pedidos_origem ON pedidos(origem);",
            "CREATE INDEX IF NOT EXISTS idx_pedidos_gateway ON pedidos(gateway_usada);",
            "CREATE INDEX IF NOT EXISTS idx_pedidos_status ON pedidos(status);",
            "CREATE INDEX IF NOT EXISTS idx_leads_created ON leads(created_at);",
            "CREATE INDEX IF NOT EXISTS idx_pedidos_bot_status ON pedidos(bot_id, status);",
        ]
        for cmd in cmds:
            try:
                db.execute(text(cmd))
                db.commit()
                log_msgs.append(f"✅ OK")
            except Exception as e_cmd:
                if "already exists" not in str(e_cmd):
                    log_msgs.append(f"⚠️ {e_cmd}")
        return {"status": "success", "message": "📊 Migração V18 concluída!", "details": log_msgs}
    except Exception as e:
        db.rollback()
        return {"status": "error", "message": f"❌ Erro: {str(e)}"}

# ============================================================
# 🚨 MIGRAÇÃO V3: CORREÇÃO RETROATIVA DOS DONOS DOS BOTS 
# ============================================================
@app.get("/migrate-reports-v3")
async def migrate_reports_v3(db: Session = Depends(get_db)):
    """
    Corrige denúncias antigas que ficaram com "Desconhecido" e atualiza 
    os donos corretos buscando ativamente pelo @username do bot.
    Acesse UMA VEZ: https://zenyx-gbs-testesv1-production.up.railway.app/migrate-reports-v3
    """
    try:
        from sqlalchemy import text
        log_msgs = []
        
        # 1. Pega todas as denúncias onde o dono tá vazio
        reports = db.execute(text("SELECT id, bot_username FROM reports WHERE owner_id IS NULL")).fetchall()
        updated = 0
        
        for r in reports:
            r_id = r[0]
            b_uname = str(r[1]).replace("@", "").strip().lower()
            
            # Procura o bot pelo username real
            bot = db.execute(text("SELECT id, owner_id FROM bots WHERE LOWER(username) = :uname OR LOWER(nome) = :uname LIMIT 1"), {"uname": b_uname}).fetchone()
            
            if bot and bot[1]:
                # Pega os dados do dono
                user = db.execute(text("SELECT username FROM users WHERE id = :uid"), {"uid": bot[1]}).fetchone()
                if user:
                    # Atualiza a denúncia com bot_id, owner_id e owner_username!
                    db.execute(text("""
                        UPDATE reports 
                        SET bot_id = :bid, owner_id = :uid, owner_username = :uname 
                        WHERE id = :rid
                    """), {"bid": bot[0], "uid": bot[1], "uname": user[0], "rid": r_id})
                    updated += 1
        
        db.commit()
        log_msgs.append(f"✅ {updated} denúncias antigas foram corrigidas e vinculadas aos seus donos!")
        
        return {
            "status": "success",
            "message": "🚨 Correção de Donos V3 concluída!",
            "details": log_msgs
        }
    except Exception as e:
        db.rollback()
        return {"status": "error", "message": f"❌ Erro: {str(e)}"}