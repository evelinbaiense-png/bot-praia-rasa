from flask import Flask, request, jsonify
import anthropic
import requests
import json
import re
import os
import time
import hashlib
import tempfile
import threading
import math
from datetime import datetime
from apscheduler.schedulers.background import BackgroundScheduler
import csv

app = Flask(__name__)

# ─── CONFIGURAÇÕES ──────────────────────────────────────────────────────────
ANTHROPIC_API_KEY       = os.environ.get('ANTHROPIC_API_KEY')
OPENAI_API_KEY          = os.environ.get('OPENAI_API_KEY')
UAZAPI_URL              = os.environ.get('UAZAPI_URL', 'https://evelinabreu.uazapi.com')
UAZAPI_TOKEN            = os.environ.get('UAZAPI_TOKEN')
INSTANCE_NAME           = os.environ.get('INSTANCE_NAME', 'evelin')
RECOVERY_INTERVAL_HOURS = float(os.environ.get('RECOVERY_INTERVAL_HOURS', '2'))
ALERT_NUMBERS           = ['5522999004419', '5522995511909']

# ─── MODELO ─────────────────────────────────────────────────────────────────
AI_MODEL            = os.environ.get('AI_MODEL', 'claude-sonnet-5')
HISTORY_LIMIT       = int(os.environ.get('HISTORY_LIMIT', '30'))
MAX_TOKENS_REPLY    = int(os.environ.get('MAX_TOKENS_REPLY', '2000'))
MAX_TOKENS_FOLLOWUP = int(os.environ.get('MAX_TOKENS_FOLLOWUP', '1000'))
MAX_TOKENS_EXTRACT  = int(os.environ.get('MAX_TOKENS_EXTRACT', '500'))

# ─── AGRUPAMENTO DE MENSAGENS ────────────────────────────────────────────────
# Janela de espera antes de responder: mensagens que o cliente manda em sequência
# são juntadas numa única chamada de IA (evita uma resposta completa por linha).
DEBOUNCE_SECONDS = float(os.environ.get('DEBOUNCE_SECONDS', '8'))

# ─── TRAVA DE PAUSA ──────────────────────────────────────────────────────────
RESUME_KEYWORD         = '.'
PAUSE_TTL              = int(os.environ.get('PAUSE_TTL_HOURS', '12')) * 3600
PAUSE_GUARD_SECONDS    = float(os.environ.get('PAUSE_GUARD_SECONDS', '5'))
MANUAL_ACTIVITY_WINDOW = int(os.environ.get('MANUAL_ACTIVITY_WINDOW', '120'))

# ─── FOLLOW-UP ───────────────────────────────────────────────────────────────
FOLLOWUP_ENABLED    = os.environ.get('FOLLOWUP_ENABLED', 'true').lower() == 'true'
FOLLOWUP_STAGE1_MIN = int(os.environ.get('FOLLOWUP_STAGE1_MIN', '10'))
FOLLOWUP_STAGE2_MIN = int(os.environ.get('FOLLOWUP_STAGE2_MIN', '60'))
FOLLOWUP_STAGE3_MIN = int(os.environ.get('FOLLOWUP_STAGE3_MIN', '360'))
FOLLOWUP_DAY_START  = int(os.environ.get('FOLLOWUP_DAY_START', '8'))
FOLLOWUP_DAY_END    = int(os.environ.get('FOLLOWUP_DAY_END', '21'))
FOLLOWUP_CHECK_MIN  = int(os.environ.get('FOLLOWUP_CHECK_MIN', '5'))

# ─── MENSAGEM AUTOMÁTICA DO FACEBOOK ─────────────────────────────────────────
FB_AUTO_GREETING_KEYWORDS = [
    'mensagem de saudação automática',
    'que bom ter você por aqui',
    'os lotes ficam a poucos minutos da praia',
]
FB_AD_BUTTON_TEXT = 'gostaria de saber valores e disponibilidade'

# ─── COORDENADAS DO EMPREENDIMENTO ───────────────────────────────────────────
EMPREENDIMENTO_LAT = -22.7238716
EMPREENDIMENTO_LNG = -42.001362

# ─── REFERÊNCIAS GEOGRÁFICAS CONHECIDAS ──────────────────────────────────────
REFERENCIAS_CONHECIDAS = {
    'pórtico':           {'dist_km': 11,  'desc': 'Pórtico de Búzios'},
    'portico':           {'dist_km': 11,  'desc': 'Pórtico de Búzios'},
    'cruzeiro':          {'dist_km': 5,   'desc': 'Praça do Cruzeiro'},
    'inej':              {'dist_km': 5,   'desc': 'INEJ'},
    'inef':              {'dist_km': 5,   'desc': 'INEJ'},
    'praia rasa':        {'dist_km': 0.8, 'desc': 'Praia Rasa'},
    'geribá':            {'dist_km': 8,   'desc': 'Praia do Geribá'},
    'geriba':            {'dist_km': 8,   'desc': 'Praia do Geribá'},
    'cabo frio':         {'dist_km': 18,  'desc': 'Cabo Frio centro'},
    'arraial do cabo':   {'dist_km': 25,  'desc': 'Arraial do Cabo'},
    'arraial':           {'dist_km': 25,  'desc': 'Arraial do Cabo'},
    'búzios':            {'dist_km': 11,  'desc': 'centro de Búzios'},
    'buzios':            {'dist_km': 11,  'desc': 'centro de Búzios'},
    'praia do forte':    {'dist_km': 12,  'desc': 'Praia do Forte'},
    'ferradura':         {'dist_km': 14,  'desc': 'Praia da Ferradura'},
    'tucuns':            {'dist_km': 13,  'desc': 'Praia de Tucuns'},
    'joão fernandes':    {'dist_km': 16,  'desc': 'Praia de João Fernandes'},
    'joao fernandes':    {'dist_km': 16,  'desc': 'Praia de João Fernandes'},
    'manguinhos':        {'dist_km': 9,   'desc': 'Praia de Manguinhos'},
    'rasa':              {'dist_km': 2,   'desc': 'Vila da Rasa'},
    'rj-106':            {'dist_km': 0,   'desc': 'RJ-106'},
}

def buscar_distancia_referencia(texto):
    texto_lower = texto.lower()
    for chave, dados in REFERENCIAS_CONHECIDAS.items():
        if chave in texto_lower:
            return dados
    return None

def buscar_distancia_osm(local_nome):
    """Fallback: busca via OpenStreetMap Nominatim — 100% gratuito, sem API key."""
    try:
        url    = "https://nominatim.openstreetmap.org/search"
        params = {'q': f"{local_nome}, Búzios, Rio de Janeiro, Brasil", 'format': 'json', 'limit': 1}
        headers = {'User-Agent': 'BotPraiaRasa/1.0'}
        resp   = requests.get(url, params=params, headers=headers, timeout=5)
        data   = resp.json() if resp.status_code == 200 else []
        if not data:
            params['q'] = f"{local_nome}, Rio de Janeiro, Brasil"
            resp = requests.get(url, params=params, headers=headers, timeout=5)
            data = resp.json() if resp.status_code == 200 else []
        if not data:
            return None
        lat = float(data[0]['lat'])
        lng = float(data[0]['lon'])
        R   = 6371
        dlat = math.radians(lat - EMPREENDIMENTO_LAT)
        dlng = math.radians(lng - EMPREENDIMENTO_LNG)
        a    = math.sin(dlat/2)**2 + math.cos(math.radians(EMPREENDIMENTO_LAT)) * math.cos(math.radians(lat)) * math.sin(dlng/2)**2
        return round(R * 2 * math.asin(math.sqrt(a)), 1)
    except Exception as e:
        print(f"OSM error: {e}")
        return None

# ─── REDIS ───────────────────────────────────────────────────────────────────
import redis as _redis_lib

REDIS_URL         = os.environ.get('REDIS_URL', '')
CONV_TTL          = 7 * 24 * 3600
MEDIA_SENT_TTL    = 7 * 24 * 3600
HOT_LEAD_ALERT_TTL = 24 * 3600
_redis_client     = None
_redis_warned     = False

def get_redis():
    global _redis_client, _redis_warned
    if _redis_client is not None:
        return _redis_client
    if not REDIS_URL:
        if not _redis_warned:
            print("⚠️ REDIS_URL NÃO CONFIGURADA — memória desligada.")
            _redis_warned = True
        return None
    try:
        client = _redis_lib.from_url(REDIS_URL, decode_responses=True)
        client.ping()
        _redis_client = client
        print("✅ Redis conectado.")
        return _redis_client
    except Exception as e:
        if not _redis_warned:
            print(f"❌ FALHA REDIS: {e}")
            _redis_warned = True
        return None

def get_conversation(phone):
    r = get_redis()
    if not r: return []
    try:
        data = r.get(f"conv:{phone}")
        return json.loads(data) if data else []
    except Exception as e:
        print(f"Redis get error ({phone}): {e}")
        return []

def save_conversation(phone, messages):
    r = get_redis()
    if not r: return
    try:
        r.setex(f"conv:{phone}", CONV_TTL, json.dumps(messages))
    except Exception as e:
        print(f"Redis save error ({phone}): {e}")

def append_message(phone, role, content):
    history = get_conversation(phone)
    history.append({"role": role, "content": content})
    save_conversation(phone, history)
    return history

def is_paused(phone):
    r = get_redis()
    if not r: return False
    try:
        return r.exists(f"pause:{phone}") == 1
    except Exception as e:
        print(f"Redis is_paused error ({phone}): {e}")
        return False

def set_pause(phone):
    r = get_redis()
    if not r: return
    try:
        r.setex(f"pause:{phone}", PAUSE_TTL, "1")
        print(f"⏸️  Bot PAUSADO para {phone}.")
    except Exception as e:
        print(f"Redis set_pause error ({phone}): {e}")

def clear_pause(phone):
    r = get_redis()
    if not r: return
    try:
        r.delete(f"pause:{phone}")
        print(f"▶️  Bot REATIVADO para {phone}.")
    except Exception as e:
        print(f"Redis clear_pause error ({phone}): {e}")

def get_followup_state(phone):
    r = get_redis()
    if not r: return None
    try:
        data = r.get(f"fu:{phone}")
        return json.loads(data) if data else None
    except Exception as e:
        print(f"Redis fu get error ({phone}): {e}")
        return None

def set_followup_state(phone, state):
    r = get_redis()
    if not r: return
    try:
        r.setex(f"fu:{phone}", CONV_TTL, json.dumps(state))
    except Exception as e:
        print(f"Redis fu set error ({phone}): {e}")

def mark_media_sent(phone):
    r = get_redis()
    if not r: return
    try:
        r.setex(f"media_sent:{phone}", MEDIA_SENT_TTL, "1")
    except Exception as e:
        print(f"Redis mark_media_sent error ({phone}): {e}")

def was_media_sent(phone):
    r = get_redis()
    if not r: return False
    try:
        return r.exists(f"media_sent:{phone}") == 1
    except Exception as e:
        print(f"Redis was_media_sent error ({phone}): {e}")
        return False

def should_send_hot_lead_alert(phone):
    """Retorna True e marca o cooldown se ainda não houve alerta de lead quente nas últimas 24h."""
    r = get_redis()
    if not r: return True
    try:
        key = f"hotlead_alert:{phone}"
        if r.exists(key):
            return False
        r.setex(key, HOT_LEAD_ALERT_TTL, "1")
        return True
    except Exception as e:
        print(f"Redis should_send_hot_lead_alert error ({phone}): {e}")
        return True

def mark_manual_activity(phone):
    """Marca que houve mensagem manual (fromMe) recente nesse número — usado para
    decidir se vale a pena aplicar o atraso de segurança antes do próximo envio."""
    r = get_redis()
    if not r: return
    try:
        r.setex(f"manual_activity:{phone}", MANUAL_ACTIVITY_WINDOW, "1")
    except Exception as e:
        print(f"Redis mark_manual_activity error ({phone}): {e}")

def had_recent_manual_activity(phone):
    r = get_redis()
    if not r: return False
    try:
        return r.exists(f"manual_activity:{phone}") == 1
    except Exception as e:
        print(f"Redis had_recent_manual_activity error ({phone}): {e}")
        return False

# ─── META CONVERSIONS API — LEAD QUALIFICADO ─────────────────────────────────
# Devolve pro Meta Ads um evento "LeadQualificado" quando o cliente confirma que
# consegue pagar a parcela — assim a campanha pode otimizar por qualidade real,
# não só por "conversa iniciada". Requer META_DATASET_ID e META_CAPI_TOKEN nas
# variáveis de ambiente (Railway); sem eles o recurso fica desligado.
META_DATASET_ID      = os.environ.get('META_DATASET_ID', '')
META_CAPI_TOKEN      = os.environ.get('META_CAPI_TOKEN', '')
META_API_VERSION     = os.environ.get('META_API_VERSION', 'v25.0')  # atual em jul/2026
META_TEST_EVENT_CODE = os.environ.get('META_TEST_EVENT_CODE', '')
CAPI_MAX_ATTEMPTS    = 8
CAPI_STATE_TTL       = 90 * 24 * 3600

_CTWA_RE = re.compile(r'"ctwa_clid"\s*:\s*"([^"]+)"')

def store_ctwa_clid(phone, data):
    """Guarda o id de clique do anúncio (ctwa_clid), que chega no webhook quando
    a conversa nasce de um anúncio click-to-WhatsApp — é o identificador que o
    Meta exige para eventos com action_source=business_messaging."""
    r = get_redis()
    if not r: return
    try:
        m = _CTWA_RE.search(json.dumps(data, default=str))
        if m:
            r.setex(f"ctwa:{phone}", CAPI_STATE_TTL, m.group(1))
            print(f"🎯 ctwa_clid capturado para {phone}")
    except Exception as e:
        print(f"store_ctwa_clid error ({phone}): {e}")

def _capi_send_once(item):
    """Uma tentativa de envio. Retorna 'ok', 'retry' (falha temporária: rede,
    5xx, rate limit) ou 'fatal' (4xx de config/payload — repetir não resolve)."""
    try:
        url  = f"https://graph.facebook.com/{META_API_VERSION}/{META_DATASET_ID}/events"
        body = {"data": [item['event']]}
        if META_TEST_EVENT_CODE:
            body["test_event_code"] = META_TEST_EVENT_CODE
        resp = requests.post(url, params={"access_token": META_CAPI_TOKEN}, json=body, timeout=15)
        if resp.status_code == 200:
            print(f"🎯 CAPI: LeadQualificado aceito ({item['event']['event_id']})")
            return 'ok'
        print(f"🎯 CAPI recusou ({resp.status_code}): {resp.text[:300]}")
        return 'retry' if (resp.status_code == 429 or resp.status_code >= 500) else 'fatal'
    except Exception as e:
        print(f"🎯 CAPI erro de rede: {e}")
        return 'retry'

def _capi_process(item):
    status = _capi_send_once(item)
    if status == 'ok':
        return
    if status == 'fatal':
        print(f"🎯 CAPI: falha permanente — confira dataset/token/evento no Events Manager. Evento: {json.dumps(item['event'])[:300]}")
        for number in ALERT_NUMBERS:
            _send_raw(number, f"⚠️ O Meta recusou o evento LeadQualificado (+{item['phone']}). Provável problema de configuração (dataset/token) — ver logs.")
        return
    item['attempts'] = item.get('attempts', 0) + 1
    if item['attempts'] > CAPI_MAX_ATTEMPTS:
        print(f"🎯 CAPI: desistindo após {CAPI_MAX_ATTEMPTS} tentativas ({item['phone']}).")
        for number in ALERT_NUMBERS:
            _send_raw(number, f"⚠️ Não consegui enviar o evento LeadQualificado pro Meta (+{item['phone']}) após várias tentativas — ver logs.")
        return
    delay = 60 * (2 ** (item['attempts'] - 1))  # 1, 2, 4, 8... minutos
    item['next_ts'] = time.time() + delay
    r = get_redis()
    if r:
        try:
            r.rpush("capi_retry", json.dumps(item))
            print(f"🎯 CAPI: reagendado em {delay // 60} min (tentativa {item['attempts']}).")
            return
        except Exception as e:
            print(f"CAPI requeue error: {e}")
    # Sem Redis: espera na própria thread daemon — não bloqueia o atendimento.
    time.sleep(delay)
    _capi_process(item)

def capi_retry_sweep():
    """Roda no scheduler: reprocessa eventos que falharam, respeitando o backoff."""
    r = get_redis()
    if not r: return
    try:
        for _ in range(r.llen("capi_retry")):
            raw = r.lpop("capi_retry")
            if not raw: break
            item = json.loads(raw)
            if item.get('next_ts', 0) > time.time():
                r.rpush("capi_retry", raw)  # ainda não é hora — devolve pro fim da fila
                continue
            _capi_process(item)
    except Exception as e:
        print(f"capi_retry_sweep error: {e}")

def queue_lead_qualified_event(phone):
    """Dispara o evento LeadQualificado — no máximo uma vez por telefone."""
    if not META_DATASET_ID or not META_CAPI_TOKEN:
        print("🎯 CAPI desligada (META_DATASET_ID/META_CAPI_TOKEN ausentes) — evento não enviado.")
        return
    # Ids @lid não são telefone de verdade — hashear isso mandaria lixo pro matching.
    if not (phone.startswith('55') and len(phone) in (12, 13)):
        print(f"🎯 CAPI: '{phone}' não parece telefone BR — evento não enviado.")
        return
    r = get_redis()
    if r:
        try:
            if not r.set(f"capi_lq:{phone}", "1", nx=True, ex=CAPI_STATE_TTL):
                print(f"🎯 CAPI: LeadQualificado já enviado para {phone} — ignorando.")
                return
        except Exception as e:
            print(f"queue_lead_qualified error ({phone}): {e}")
    now   = int(time.time())
    event = {
        "event_name":    "LeadQualificado",
        "event_time":    now,
        "action_source": "chat",
        "user_data":     {"ph": [hashlib.sha256(phone.encode('utf-8')).hexdigest()]},
        "custom_data":   {"currency": "BRL", "value": 899},
        "event_id":      f"lq_{phone}_{now}",
    }
    ctwa = None
    if r:
        try:
            ctwa = r.get(f"ctwa:{phone}")
        except Exception:
            pass
    if ctwa:
        # Conversa nasceu de anúncio click-to-WhatsApp: o Meta pede
        # business_messaging + o id do clique para atribuir direto à campanha.
        event["action_source"]          = "business_messaging"
        event["messaging_channel"]      = "whatsapp"
        event["user_data"]["ctwa_clid"] = ctwa
    _capi_process({"phone": phone, "event": event, "attempts": 0})

# ─── ENCERRAMENTO DE CONVERSA (evita loop de despedida) ──────────────────────
CLOSING_KEYWORDS = [
    'obrigad', 'valeu', 'vlw', 'agradeç', 'agradec', 'boas vendas', 'boa venda',
    'ótimos negócios', 'otimos negocios', 'sucesso', 'fico à dispos',
    'fico a dispos', 'à disposição', 'a disposicao', 'combinado', 'abraço', 'abraco',
    'até mais', 'ate mais', 'até logo', 'ate logo', 'nos falamos',
    'grato', 'grata', 'de nada', 'deus abençoe', 'ótimo dia e', 'otimo dia e',
    'imagina', 'qualquer coisa', 'só chamar', 'so chamar', 'amém', 'amem',
    'ótimas vendas', 'otimas vendas', 'boas negociações',
]

def is_closing_message(text):
    """Mensagem de pura cortesia/despedida: curta, sem pergunta, sem sinal de compra.
    Palavras ambíguas que também servem de saudação de abertura (bom dia, boa tarde)
    ficam de fora de propósito, para não confundir início de conversa com despedida."""
    t = (text or '').lower().strip()
    if not t or '?' in t:
        return False
    if len(t.split()) > 8:
        return False
    if any(s in t for s in HOT_LEAD_SIGNALS):
        return False
    return any(k in t for k in CLOSING_KEYWORDS)

def in_closing_state(phone):
    r = get_redis()
    if not r: return False
    try:
        return r.exists(f"closing:{phone}") == 1
    except Exception:
        return False

def set_closing_state(phone):
    r = get_redis()
    if not r: return
    try:
        r.setex(f"closing:{phone}", 1800, "1")  # 30 min
    except Exception as e:
        print(f"Redis set_closing_state error ({phone}): {e}")

def clear_closing_state(phone):
    r = get_redis()
    if not r: return
    try:
        r.delete(f"closing:{phone}")
    except Exception:
        pass

# ─── MÍDIAS ──────────────────────────────────────────────────────────────────
PHOTOS = [
    "https://res.cloudinary.com/dd6o3z4ma/image/upload/v1783794693/foto-01-pergola_kcvsxw.jpg",
    "https://res.cloudinary.com/dd6o3z4ma/image/upload/v1783794693/foto-02-vista-mar-postes_ox8vnn.jpg",
    "https://res.cloudinary.com/dd6o3z4ma/image/upload/v1783794693/foto-03-terreno-caminhao_a9vwmd.jpg",
    "https://res.cloudinary.com/dd6o3z4ma/image/upload/v1783794693/foto-04-casa-arvore_otqppf.jpg",
    "https://res.cloudinary.com/dd6o3z4ma/image/upload/v1783794693/foto-05-terreno-caminhao-2_rti3gm.jpg",
    "https://res.cloudinary.com/dd6o3z4ma/image/upload/v1783794693/foto-06-estrada-postes_qjwp2j.jpg",
    "https://res.cloudinary.com/dd6o3z4ma/image/upload/v1779040450/Editedimage_1776197718307_ms669e.png",
]
VIDEO_URL_1 = "https://res.cloudinary.com/dd6o3z4ma/video/upload/v1779039974/WhatsApp_Video_2026-05-17_at_13.15.06_zrc6fs.mp4"
VIDEO_URL_2 = "https://res.cloudinary.com/dd6o3z4ma/video/upload/v1779039973/WhatsApp_Video_2026-05-17_at_13.19.32_b5ptia.mp4"

# ─── TEXTOS ──────────────────────────────────────────────────────────────────
GREETING = "Oi! Aqui é a Evelin Abreu, corretora de imóveis. Tô com um lançamento incrível pertinho de Búzios — lotes a poucos minutos da praia, com parcelamento direto pela incorporadora. Você já ouviu falar do Praia Rasa de Búzios 2 ou é a primeira vez?"

SYSTEM_PROMPT = """Você é Evelin Abreu, corretora de imóveis do Praia Rasa de Búzios 2. Atenda como a própria Evelin no WhatsApp — simpática, direta, como um bate-papo profissional. Nunca revele que é IA. Se perguntarem, é a própria Evelin.

════════════════════════════════════════════
REGRA ABSOLUTA: RESPONDA A PERGUNTA PRIMEIRO
═══════════════════════════════════════════
Se o cliente fizer qualquer pergunta direta — localização, preço, documentação, infraestrutura — RESPONDA PRIMEIRO. Só depois avance no fluxo. NUNCA ignore uma pergunta para seguir o script.

═══════════════════════════════════════════
GUIE A CONVERSA COMO UMA VENDEDORA EXPERIENTE
═══════════════════════════════════════════
- Você não está só respondendo perguntas — está conduzindo o cliente até a
  visita. Toda resposta deve avançar a conversa para o próximo passo natural
  (localização → valores → visita), nunca deixar a conversa "morrer" sem rumo.
- Use técnica de venda: gere interesse antes de detalhar tudo de uma vez,
  crie senso de oportunidade quando fizer sentido, e sempre guie o cliente
  a responder e dar o próximo passo — não apenas informe e pare.
- Isso NÃO significa perguntar sobre visita a cada mensagem (ver PASSO 5) —
  significa que cada resposta sua tem direção clara, incentivando o cliente
  a continuar engajado, nunca uma resposta que só informa e encerra o assunto.
- Você é uma vendedora profissional falando com um cliente, NÃO uma amiga
  batendo papo. Seja simpática, mas direta e objetiva — sem enrolar, sem
  ficar reafirmando a mesma coisa de formas diferentes para "parecer" mais
  completa. Firmeza e clareza vendem mais do que simpatia excessiva.

═══════════════════════════════════════════
DETECTE O PERFIL DO CLIENTE E ADAPTE
══════════════════════════════════════════

PERFIL A — MORADOR LOCAL / CONHECE A REGIÃO
Sinais: menciona praias, bairros, referências locais (Cruzeiro, pórtico, Rasa, Geribá, INEJ), diz "conheço a região", "passo por lá", "moro em Búzios/Cabo Frio/Arraial"

Como atender:
- NÃO explique que a região é valorizada — ele já sabe
- NÃO fale de kitesurf, condomínios de alto padrão como novidade — ele conhece
- Vá direto para o produto: localização exata, infraestrutura, valores, financiamento
- Use referências locais naturalmente: "fica depois do Cruzeiro, lado direito da RJ-106"
- Tom: conversa entre quem conhece a região

PERFIL B — PESSOA DE FORA / NÃO CONHECE A REGIÃO
Sinais: pergunta "onde fica?", "é perto de quê?", não usa referências locais, menciona cidade de origem distante

Como atender:
- Contextualize a região primeiro: "fica na divisa de Búzios com Cabo Frio, a 800m da Praia Rasa"
- Mencione o potencial de valorização, o estilo de vida, as praias próximas
- Use o mapa como apoio: https://www.google.com/maps/@-22.7238716,-42.001362,493m
- Tom: apresentando uma oportunidade em uma região que ele não conhece bem

QUANDO NÃO SOUBER O PERFIL: siga o fluxo normal e vá adaptando conforme ele fala.

═══════════════════════════════════════════
PRIMEIRA MENSAGEM DO CLIENTE (vinda do anúncio)
═══════════════════════════════════════════
Se a primeira mensagem for "Olá! Gostaria de saber valores e disponibilidade":
- É intenção real — o cliente clicou no anúncio
- Responda com simpatia e qualifique rapidamente: "Oi! Que ótimo 😊 Me conta uma coisa: você está pensando em morar, ter uma casa de veraneio ou é mais como investimento?"
- NÃO mande mídia ainda — qualifique primeiro

═══════════════════════════════════════════
SEU FLUXO NATURAL
═══════════════════════════════════════════
Siga essa ordem quando o cliente deixar você conduzir. Se ele puxar outro assunto, acompanhe — responda e depois retome.

PASSO 1 — Objetivo:
"Você está pensando em morar, ter uma casa de veraneio ou investir?"

PASSO 2 — Mídia (sempre, após entender o objetivo):
"Deixa eu já te mostrar o empreendimento!" + [ENVIAR_MIDIA]

PASSO 3 — Localização (se ele não perguntou antes):
"Posso te mandar a localização?"
Se sim: https://www.google.com/maps/@-22.7238716,-42.001362,493m

PASSO 4 — Valores (se ele não perguntou antes):
"Posso te passar os valores?"
Se sim: apresente 300m² e 600m² com PARCELAS.
⚠️ NUNCA mencione valor à vista por iniciativa. Só se perguntarem.

PASSO 5 — Visita: seu objetivo final é levar o cliente a agendar uma visita, mas
NÃO pergunte sobre visita em toda mensagem. Só traga o assunto quando o cliente
der uma brecha — perguntar disponibilidade, dizer que quer conhecer, reagir bem
depois de ver fotos/valores, ou você perceber que já passou por localização e
valores sem objeção. Depois de perguntar uma vez, não repita insistentemente.

═══════════════════════════════════════════
LOCALIZAÇÃO — RESPOSTAS PRONTAS
═══════════════════════════════════════════
Empreendimento: Estrada dos Búzios (RJ-106), Bairro da Rasa, divisa Búzios/Cabo Frio.
Fica na MARGEM DIREITA da RJ-106 sentido Búzios.
(Lado esquerdo é reserva da Marinha — impossível ter empreendimento lá)

Referência para quem conhece a região:
Entrando pelo pórtico da Rasa sentido Búzios, passa pela Vila da Rasa, o empreendimento fica logo depois, na margem direita, antes de chegar ao centro de Búzios.

DISTÂNCIAS CONFIRMADAS:
- Praia Rasa: 800m (3 min a pé)
- Praça do Cruzeiro: ~5km
- INEJ: ~5km
- Vila da Rasa: ~2km
- Praia de Manguinhos: ~9km
- Praia do Geribá: ~8km
- Pórtico de Búzios: ~11km
- Centro de Búzios: ~11km
- Praia da Ferradura: ~14km
- Praia de Tucuns: ~13km
- Praia do Forte: ~12km
- Praia de João Fernandes: ~16km
- Cabo Frio centro: ~18km
- Arraial do Cabo: ~25km

Para locais NÃO listados: "Fica na Estrada dos Búzios (RJ-106), Bairro da Rasa. Te mando o mapa pra você ver a distância exata 😊" + link do mapa.
NUNCA invente distância.

═══════════════════════════════════════════
REGRA DE PERGUNTAS
═══════════════════════════════════════════
- NUNCA envie mais de uma pergunta na mesma mensagem.
- NUNCA envie uma pergunta de qualificação logo após já ter feito outra sem
  o cliente ter respondido a primeira.
- Se você tiver várias perguntas de qualificação pendentes na estratégia
  (finalidade, tamanho, prazo, etc.), escolha APENAS UMA por vez e espere
  a resposta do cliente antes de perguntar a próxima.

Exemplo ERRADO: "Você procura para morar ou investir? E qual tamanho de
lote te interessa mais, 300 ou 600m²?"
Exemplo CORRETO: "Você procura para morar ou investir?" (aguarda resposta,
só depois pergunta o próximo item)

═══════════════════════════════════════════
NÃO REPITA O QUE JÁ FOI DITO
═══════════════════════════════════════════
Não repita informação que você já mencionou nesta conversa (valores, condições,
se já perguntou finalidade morar/investir/veraneio), a menos que o cliente
pergunte de novo ou peça para relembrar. Antes de responder, olhe o histórico
da conversa e confirme se aquilo já não foi dito.

═══════════════════════════════════════════
TAMANHO DE RESPOSTA
═══════════════════════════════════════════
- Padrão: curto, direto, tom de WhatsApp profissional (1-3 frases).
- Pode ser mais longo APENAS quando o assunto exigir detalhe real:
  valores e condições de pagamento completas, localização/como chegar,
  documentação (RGI, associação de moradores).
- Fora desses tópicos, nunca alongar a resposta artificialmente.
- Nunca envie um resumo de valores se o cliente não pediu e você já
  mencionou isso antes nesta conversa.
- Se a resposta tiver mais de uma ideia separada, quebre em parágrafos
  curtos (linha em branco entre eles) — cada parágrafo vira uma mensagem
  separada no WhatsApp.

⚠️ "Mais longo" NUNCA significa repetir. Cada parágrafo tem que trazer
informação NOVA — nunca reafirme o mesmo preço, a mesma pergunta ou a
mesma frase (mesmo reformulada) em outro parágrafo da MESMA resposta.
Antes de enviar, releia sua própria resposta e corte qualquer parágrafo
que só repita algo que outro parágrafo dela já disse.

Exemplo ERRADO (repete dentro da mesma resposta):
"Lotes de 600m²: entrada R$14.000, parcelas a partir de R$1.599/mês. [...]
Qual tamanho te interessou mais, 300 ou 600m²? [...] Lotes de 600m²: entrada
R$14.000, parcelas a partir de R$1.599/mês. [...] Algum tamanho te chamou
mais atenção, o de 300 ou o 600?"
Exemplo CORRETO: cada dado aparece UMA vez, e a pergunta de qualificação
também aparece UMA vez só, no final.

═══════════════════════════════════════════
COMO CONVERSAR
═══════════════════════════════════════════
- Termine com pergunta ou próximo passo claro.
- NUNCA repita a mesma frase em mensagens seguidas.
- Clientes mais velhos: "o senhor" / "a senhora".
- Comentário religioso: "Amém!" / "Dia abençoado".
- Português sempre, mesmo que o cliente escreva em espanhol.
- Emojis neutros e com moderação (😊 🏡 👍 📍). NUNCA use corações ou beijos.
- Aviso de plantão por escala: mencione apenas UMA vez por conversa, quando
  houver interesse real de visita — não repita em mensagens seguintes.
- No MEIO da qualificação, não abandone: "ok", "obrigada", "valeu" ditos
  enquanto vocês ainda falam do produto NÃO são fim de atendimento — continue.
- MAS quando o cliente estiver claramente só se despedindo ou trocando
  cortesias (agradecer, "boas vendas", "ótimo dia", "fico à disposição",
  "combinado", "abraço"), responda de forma breve e calorosa UMA vez e
  ENCERRE — sem nova pergunta, sem oferecer mais nada, sem gancho que force
  o cliente a responder. Ex.: "Obrigada, [nome]! Qualquer coisa estou por
  aqui 😊" e pare. NUNCA fique devolvendo cortesia infinitamente — se o
  cliente responder outra cortesia, não precisa responder de novo.

═══════════════════════════════════════════
QUANDO NÃO SOUBER RESPONDER
═══════════════════════════════════════════
Apenas quando genuinamente não souber — NÃO use para localização (você tem as distâncias):
"Deixa eu confirmar essa informação pra você! 😊" + [ALERTA]

═══════════════════════════════════════════
AGENDAMENTO
═══════════════════════════════════════════
Só use isto quando o cliente já deu uma brecha (ver PASSO 5). Não ofereça visita
por conta própria em toda mensagem — o objetivo é chegar lá naturalmente, não
insistir.

"[Nome], as visitas são de terça a domingo. Qual dia funciona melhor? Prefere manhã ou tarde?"

- Confirmou dia + período → confirmado. Pare de perguntar.
- Confirmou só o dia → também confirmado.
- ⛔ NUNCA peça hora específica.
- Quando confirmado: colete nome completo e avise sobre plantão por escala.
- Visita confirmada com nome: não peça confirmação de novo.

CLIENTE VAGO SOBRE A VISITA ("vou ver", "vou pensar", "depois eu vejo", "fico
de olho"): NÃO aceite passivamente e NUNCA encerre com algo como "fico
aguardando" — isso deixa o lead esfriar. Feche com uma sugestão CONCRETA em
vez de pergunta aberta:
- Perto do fim de semana: "Que tal já aproveitar esse fim de semana? Pode ser
  sábado?"
- Senão, proponha um dia específico próximo: "Domingo de manhã funciona pra
  você?" — não pergunte "qual dia você quer".
Se ele recusar ou continuar vago mesmo depois dessa tentativa, aí sim deixe a
porta aberta — mas tente fechar com algo concreto antes de desistir.

═══════════════════════════════════════════
DADOS DO EMPREENDIMENTO
═══════════════════════════════════════════
⚠️ Este é um LOTEAMENTO FECHADO (venda de lotes de terreno) — NUNCA chame o
empreendimento de "condomínio". "Condomínio" só se aplica aos empreendimentos
vizinhos, que são de terceiros.

INFRAESTRUTURA
- Loteamento fechado e murado, meio-fio instalado, rede elétrica em andamento, água em breve.
- Guarita 24h após fundação da associação de moradores.
- Playground, praça, área verde, bosque. Quadras com vista mar e vista serra.
- Próximo a condomínios de alto padrão (empreendimentos vizinhos); região de kitesurf.
- Taxa da associação: 10% do salário mínimo, só após entrega, prevista em contrato.

LOTES 300m²
- Entrada R$7.000 | Parcelas a partir de R$899/mês (reajuste anual pelo IGPM).
- Vista mar: a partir de R$1.199/mês.

LOTES 600m²
- Entrada R$14.000 | Parcelas a partir de R$1.599/mês (reajuste anual pelo IGPM).
- Vista mar: a partir de R$1.999/mês.

VALOR À VISTA — nunca ofereça. Só se perguntarem:
- 300m²: a partir de R$90.000. | 600m²: a partir de R$160.000.

FINANCIAMENTO
- Direto pela incorporadora, sem SPC/Serasa, sem banco.
- Primeira parcela em 45 dias. Pode construir com 3 parcelas pagas.
- Prazo: de 12 até 156 parcelas (até 13 anos). O prazo MÁXIMO é 156 parcelas —
  nunca informe outro número no lugar desse. IGPM: correção anual.
- ⚠️ Se o cliente perguntar "quantas parcelas", "em quantos meses", "quantas
  vezes" ou "quantas parcelas de R$899/R$1.599": RESPONDA COM O NÚMERO, não
  desvie para a visita. A parcela mais baixa (R$899 no 300m², R$1.599 no 600m²)
  é calculada no prazo MAIS LONGO, 156x. Prazos menores deixam a parcela maior.
  Ex.: "A de R$899 é no prazo mais longo, 156x (13 anos). Se quiser quitar mais
  rápido, dá pra fazer em menos parcelas, aí a mensal sobe um pouco."
- Só a simulação PERSONALIZADA (com entrada específica, prazo exato, datas) é
  que fica pra visita — a estrutura geral acima você SEMPRE informa na hora.

DOCUMENTAÇÃO (RGI)
Tem RGI. A incorporadora está finalizando na prefeitura. Transferência para o nome do comprador é opcional e por conta do cliente após quitar.

VISITAS: terça a domingo, qualquer horário combinado.

═══════════════════════════════════════════
MARCADOR INTERNO DE LEAD QUALIFICADO
═══════════════════════════════════════════
Quando o cliente confirmar CLARAMENTE que consegue pagar a entrada ou a parcela
mensal (ex.: "consigo pagar", "cabe no meu bolso", "essa parcela dá sim",
"fecho nessas condições", confirma que quer seguir com o financiamento),
acrescente o marcador [LEAD_QUALIFICADO] no final da resposta. O marcador é
interno e é removido antes do envio — o cliente nunca vê. Use no máximo UMA vez
por conversa e SOMENTE com confirmação explícita de capacidade de pagamento —
interesse genérico ("gostei", "quero saber mais", "me manda fotos") NÃO conta.
"""

# ─── DETECÇÃO DE LEAD QUENTE ─────────────────────────────────────────────────
HOT_LEAD_SIGNALS = [
    'quando posso visitar', 'quero visitar', 'posso ir ver', 'quero conhecer',
    'qual o endereço', 'como chego lá', 'tem disponível', 'ainda tem lote',
    'quantos lotes', 'quero reservar', 'quero fechar', 'assinar', 'contrato',
    'dou entrada', 'parcela cabe', 'consigo pagar', 'tenho interesse',
    'estou interessado', 'quero comprar', 'vou comprar',
]

def is_hot_lead(text, history):
    text_lower = text.lower()
    if any(s in text_lower for s in HOT_LEAD_SIGNALS):
        return True, "cliente demonstrou interesse direto em visita ou compra"
    client_msgs = [m for m in history if m.get('role') == 'user']
    if len(client_msgs) >= 5:
        return True, f"cliente muito engajado ({len(client_msgs)} mensagens)"
    return False, ""

# ─── ALERTAS ─────────────────────────────────────────────────────────────────
def _montar_resumo(phone, ultima_msg=""):
    history = get_conversation(phone)
    ultimas = history[-4:] if len(history) >= 4 else history
    resumo  = "\n".join([
        f"{'Cliente' if m['role']=='user' else 'Bot'}: {m['content'][:80]}"
        for m in ultimas
    ])
    return resumo

def send_alert(phone_client, motivo="pergunta sem resposta", ultima_msg=""):
    resumo    = _montar_resumo(phone_client, ultima_msg)
    alert_msg = (
        f"⚠️ *ALERTA — Assuma a conversa!*\n\n"
        f"📱 +{phone_client}\n"
        f"📌 Motivo: {motivo}\n"
        f"💬 Última msg: {ultima_msg[:100]}\n\n"
        f"*Últimas mensagens:*\n{resumo}\n\n"
        f"Digite qualquer coisa para o cliente para pausar o bot."
    )
    for number in ALERT_NUMBERS:
        _send_raw(number, alert_msg)

def send_hot_lead_alert(phone_client, motivo, ultima_msg=""):
    resumo    = _montar_resumo(phone_client, ultima_msg)
    alert_msg = (
        f"🔥 *LEAD QUENTE — Entre agora!*\n\n"
        f"📱 +{phone_client}\n"
        f"📌 Motivo: {motivo}\n"
        f"💬 Última msg: {ultima_msg[:100]}\n\n"
        f"*Últimas mensagens:*\n{resumo}\n\n"
        f"Digite qualquer coisa para o cliente para pausar o bot."
    )
    for number in ALERT_NUMBERS:
        _send_raw(number, alert_msg)

# ─── FUNÇÕES DE ENVIO ─────────────────────────────────────────────────────────
def get_instance_token():
    return os.environ.get('INSTANCE_TOKEN', UAZAPI_TOKEN)

def _toggle_ninth_digit(phone):
    """Alterna o 9º dígito de um celular brasileiro (55 + DDD + número).
    Muitos números de WhatsApp — sobretudo em DDDs do interior — estão
    registrados sem o nono dígito. Quando a UAZAPI recusa com 'not on
    WhatsApp', tentamos a forma alternativa. Retorna None se não se aplica."""
    if not phone.startswith('55') or len(phone) not in (12, 13):
        return None
    country, ddd, rest = phone[:2], phone[2:4], phone[4:]
    if len(rest) == 9 and rest[0] == '9':
        return country + ddd + rest[1:]   # remove o 9º dígito
    if len(rest) == 8:
        return country + ddd + '9' + rest  # adiciona o 9º dígito
    return None

def _post_text_with_retry(phone, text, label):
    """Envia texto pela UAZAPI. Se vier 'not on WhatsApp', tenta uma vez com o
    9º dígito alternado (usado tanto para alertas quanto para clientes)."""
    url     = f"{UAZAPI_URL}/send/text"
    headers = {"token": get_instance_token(), "Content-Type": "application/json"}
    response = requests.post(url, headers=headers, json={"number": phone, "text": text}, timeout=10)
    print(f"{label} sent to {phone}: {response.status_code}")
    if response.status_code != 200:
        body = response.text[:300]
        print(f"{label} failure body ({phone}): {body}")
        alt = _toggle_ninth_digit(phone)
        if alt and ('not on whatsapp' in body.lower() or 'not exists' in body.lower() or 'invalid' in body.lower()):
            response = requests.post(url, headers=headers, json={"number": alt, "text": text}, timeout=10)
            print(f"{label} retry to {alt}: {response.status_code}")
            if response.status_code != 200:
                print(f"{label} retry failure body ({alt}): {response.text[:300]}")
    return response

def _send_raw(phone, text):
    try:
        return _post_text_with_retry(phone, text, "Alert")
    except Exception as e:
        print(f"Error sending alert to {phone}: {e}")
        return None

def send_message(phone, text):
    try:
        return _post_text_with_retry(phone, text, "Text")
    except Exception as e:
        print(f"Error sending text: {e}")
        return None

def send_image(phone, image_url, caption=""):
    headers = {"token": get_instance_token(), "Content-Type": "application/json"}
    data    = {"number": phone, "type": "image", "file": image_url, "caption": caption}
    try:
        response = requests.post(f"{UAZAPI_URL}/send/media", headers=headers, json=data, timeout=30)
        print(f"Image sent to {phone}: {response.status_code}")
        return response
    except Exception as e:
        print(f"Error sending image: {e}")
        return None

def send_video(phone, video_url, caption=""):
    headers = {"token": get_instance_token(), "Content-Type": "application/json"}
    data    = {"number": phone, "type": "video", "file": video_url, "caption": caption}
    try:
        response = requests.post(f"{UAZAPI_URL}/send/media", headers=headers, json=data, timeout=60)
        print(f"Video sent to {phone}: {response.status_code}")
        return response
    except Exception as e:
        print(f"Error sending video: {e}")
        return None

def send_media_package(phone):
    try:
        send_message(phone, "Olha só os vídeos do empreendimento 👇")
        send_video(phone, VIDEO_URL_1)
        time.sleep(2)
        send_video(phone, VIDEO_URL_2)
        time.sleep(2)
        send_message(phone, "E aqui algumas fotos 📍")
        for photo_url in PHOTOS:
            send_image(phone, photo_url)
            time.sleep(1)
        time.sleep(2)
        send_message(phone, "O que achou? 😊")
        mark_media_sent(phone)
        print(f"Media package complete for {phone}")
    except Exception as e:
        print(f"Error in send_media_package for {phone}: {e}")

def _split_into_chunks(text):
    """Quebra a resposta em partes por parágrafo (linha em branco), para simular
    mensagens curtas e sequenciais de WhatsApp em vez de um bloco único."""
    parts = [p.strip() for p in text.split('\n\n') if p.strip()]
    return parts if parts else [text]

def send_and_check(phone, text):
    chunks = _split_into_chunks(text)
    for i, chunk in enumerate(chunks):
        # Se a Evelin assumiu a conversa no meio do envio (apagou/respondeu à mão),
        # para de mandar o restante em vez de atropelar o atendimento humano.
        if i > 0 and is_paused(phone):
            print(f"⏸️ {phone} assumido durante o envio — abortando as partes restantes.")
            return False
        resp   = send_message(phone, chunk)
        status = getattr(resp, 'status_code', None) if resp is not None else None
        if status != 200:
            print(f"❌ FALHA DE ENVIO para {phone} (status {status}).")
            for number in ALERT_NUMBERS:
                if number != phone:
                    _send_raw(number, f"⚠️ Falha ao enviar para {phone} (status {status}). Verifique o WhatsApp.")
            return False
        if i < len(chunks) - 1:
            time.sleep(1.2)
    return True

def notify_ai_failure(phone):
    for number in ALERT_NUMBERS:
        if number != phone:
            _send_raw(number, f"⚠️ IA falhou para o cliente {phone}. Assuma a conversa!")
    send_message(phone, "Oi! 😊 Só um instante que já te respondo certinho.")

# ─── TRANSCRIÇÃO DE ÁUDIO ─────────────────────────────────────────────────────
def transcribe_audio(audio_url):
    if not OPENAI_API_KEY:
        return None
    try:
        import openai
        client   = openai.OpenAI(api_key=OPENAI_API_KEY)
        response = requests.get(audio_url, timeout=30)
        if response.status_code != 200:
            return None
        with tempfile.NamedTemporaryFile(suffix='.ogg', delete=False) as tmp:
            tmp.write(response.content)
            tmp_path = tmp.name
        with open(tmp_path, 'rb') as audio_file:
            transcript = client.audio.transcriptions.create(
                model="whisper-1", file=audio_file, language="pt"
            )
        os.unlink(tmp_path)
        return transcript.text
    except Exception as e:
        print(f"Error transcribing audio: {e}")
        return None

def extract_text(message):
    """Extrai texto de mensagem — garante que sempre retorna string."""
    val = (
        message.get('text') or message.get('body') or
        message.get('content') or message.get('conversation') or ''
    )
    if isinstance(val, dict):
        return val.get('text', '') or val.get('body', '') or ''
    return str(val) if val else ''

# ─── IA ───────────────────────────────────────────────────────────────────────
def _extract_text(response):
    """Extrai o primeiro bloco de texto da resposta — com thinking adaptativo
    ligado (padrão do Sonnet 5), a resposta pode conter blocos que não são texto."""
    for block in response.content:
        if getattr(block, 'type', None) == 'text':
            return block.text
    return ""

def _count_questions(text):
    return text.count('?')

def _has_duplicate_paragraphs(text):
    """Detecta parágrafo repetido verbatim dentro da mesma resposta — pega o caso
    de o modelo reafirmar o mesmo preço/pergunta em partes diferentes do texto."""
    parts = [p.strip().lower() for p in text.split('\n\n') if p.strip()]
    return len(parts) != len(set(parts))

def get_ai_response(phone, user_message):
    client  = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    history = append_message(phone, "user", user_message)

    import pytz
    try:
        br_time  = datetime.now(pytz.timezone("America/Sao_Paulo"))
        hora_int = br_time.hour
        saudacao = "Bom dia" if hora_int < 12 else ("Boa tarde" if hora_int < 18 else "Boa noite")
        time_info = f"\n\n[Horário: {br_time.strftime('%H:%M')} — use '{saudacao}' só no primeiro contato]"
    except Exception:
        time_info = ""

    # Enriquece com distância se pergunta de localização
    location_context = ""
    ref = buscar_distancia_referencia(user_message)
    if ref:
        location_context = (
            f"\n\n[LOCALIZAÇÃO CONFIRMADA: '{ref['desc']}' fica a ~{ref['dist_km']}km do empreendimento. "
            f"Use essa informação diretamente na resposta.]"
        )
    elif any(kw in user_message.lower() for kw in ['fica', 'onde', 'distância', 'longe', 'perto', 'km', 'minutos', 'localiz']):
        palavras = user_message.split()
        for i, p in enumerate(palavras):
            if len(p) > 4:
                dist = buscar_distancia_osm(' '.join(palavras[max(0, i-1):i+2]))
                if dist:
                    location_context = (
                        f"\n\n[DISTÂNCIA CALCULADA via mapa: ~{dist}km até o local mencionado. "
                        f"Use se relevante para a resposta.]"
                    )
                    break

    system     = SYSTEM_PROMPT + time_info + location_context
    last_msgs  = history[-HISTORY_LIMIT:]
    api_messages = [
        {"role": "user",      "content": "Olá"},
        {"role": "assistant", "content": GREETING},
    ] + last_msgs

    try:
        response = client.messages.create(
            model=AI_MODEL, max_tokens=MAX_TOKENS_REPLY, system=system, messages=api_messages
        )
    except Exception as e:
        print(f"❌ ERRO NA IA para {phone}: {e}")
        return None, False, False, False

    reply_raw   = _extract_text(response)
    alert_flag  = '[ALERTA]' in reply_raw
    media_flag  = '[ENVIAR_MIDIA]' in reply_raw
    lq_flag     = '[LEAD_QUALIFICADO]' in reply_raw
    reply_clean = (reply_raw.replace('[ALERTA]', '').replace('[ENVIAR_MIDIA]', '')
                            .replace('[LEAD_QUALIFICADO]', '').strip())

    # Auditoria/correção de empilhamento de perguntas e repetição interna
    stacked_questions  = _count_questions(reply_clean) > 1
    duplicated_content = _has_duplicate_paragraphs(reply_clean)
    if stacked_questions or duplicated_content:
        motivo = "múltiplas perguntas" if stacked_questions else "parágrafo repetido"
        if stacked_questions and duplicated_content:
            motivo = "múltiplas perguntas e parágrafo repetido"
        print(f"[EMPILHAMENTO] {phone}: resposta com {motivo} — tentando regenerar. Original: {reply_clean[:200]}")
        try:
            retry_system = system + (
                "\n\n[CORREÇÃO: sua última resposta tinha problema de qualidade "
                f"({motivo}). Regenere a resposta: NO MÁXIMO uma pergunta "
                "(REGRA DE PERGUNTAS) e NUNCA repita o mesmo preço, pergunta ou "
                "frase em mais de um parágrafo (TAMANHO DE RESPOSTA).]"
            )
            retry_response = client.messages.create(
                model=AI_MODEL, max_tokens=MAX_TOKENS_REPLY, system=retry_system, messages=api_messages
            )
            retry_raw   = _extract_text(retry_response)
            retry_clean = (retry_raw.replace('[ALERTA]', '').replace('[ENVIAR_MIDIA]', '')
                                    .replace('[LEAD_QUALIFICADO]', '').strip()) if retry_raw else ""
            if retry_raw and _count_questions(retry_clean) <= 1 and not _has_duplicate_paragraphs(retry_clean):
                reply_raw   = retry_raw
                alert_flag  = '[ALERTA]' in reply_raw
                media_flag  = '[ENVIAR_MIDIA]' in reply_raw
                lq_flag     = '[LEAD_QUALIFICADO]' in reply_raw
                reply_clean = retry_clean
            else:
                print(f"[EMPILHAMENTO] {phone}: regeneração não resolveu — mantendo original.")
        except Exception as e:
            print(f"[EMPILHAMENTO] {phone}: erro ao regenerar: {e}")

    hist_text = reply_clean + ("\n[Enviei fotos e vídeos e perguntei: O que achou?]" if media_flag else "")
    append_message(phone, "assistant", hist_text)

    return reply_clean, alert_flag, media_flag, lq_flag

# ─── FOLLOW-UP ───────────────────────────────────────────────────────────────
def generate_followup(phone, stage):
    try:
        client  = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
        history = get_conversation(phone)
        situ = {
            1: "O cliente parou de responder há poucos minutos. Mande UMA mensagem curta e leve pra retomar, sem cobrar.",
            2: "Sem resposta há ~1 hora. Mensagem calorosa com pergunta NOVA pra reengajar.",
            3: "Fim do dia. Última mensagem simpática, sem pressão, porta aberta.",
        }
        system    = SYSTEM_PROMPT + f"\n\n[RETOMANDO CONTATO: {situ.get(stage, situ[1])} Gere SÓ a mensagem, curta e natural. NÃO cumprimente de novo.]"
        last_msgs = history[-HISTORY_LIMIT:]
        api_messages = [
            {"role": "user",      "content": "Olá"},
            {"role": "assistant", "content": GREETING},
        ] + last_msgs + [
            {"role": "user", "content": "[Cliente ficou em silêncio. Escreva a mensagem de retomada.]"}
        ]
        response = client.messages.create(model=AI_MODEL, max_tokens=MAX_TOKENS_FOLLOWUP, system=system, messages=api_messages)
        msg      = _extract_text(response)
        return (msg.replace('[ALERTA]', '').replace('[ENVIAR_MIDIA]', '')
                   .replace('[LEAD_QUALIFICADO]', '').strip())
    except Exception as e:
        print(f"generate_followup error ({phone}): {e}")
        return None

FOLLOWUP_STOP_SIGNALS = [
    "tá anotado", "tá confirmado", "agendado pra", "nos vemos",
    "até terça", "até segunda", "até quarta", "até quinta",
    "até sexta", "até sábado", "até domingo", "te espero lá",
    "terça às", "sábado às", "domingo às", "segunda às",
    "show, quinta", "show, quarta", "show, terça", "show, sábado",
    "fechado,", "fechado!", "✅", "te espero", "até lá",
]

def is_duplicate_msg(message, phone=''):
    r = get_redis()
    if not r: return False
    try:
        msg_id = (
            message.get('id') or
            message.get('messageId') or
            (message.get('key') or {}).get('id', '') or
            message.get('remoteJid', '') + str(message.get('timestamp', ''))
        )
        if not msg_id: return False
        key = f"dup:{msg_id}"
        if r.exists(key):
            print(f"🔁 Duplicata ignorada (id): {msg_id[:30]}")
            return True
        r.setex(key, 600, phone or "unknown")
        return False
    except Exception as e:
        print(f"Dedup error: {e}")
        return False

def is_duplicate_content(phone, text):
    """Segunda camada de dedup, por conteúdo — cobre o caso em que a UAZAPI manda
    o mesmo evento duas vezes sem um id/messageId estável (o dedup por id acima
    não pega esse caso, porque cada entrega chega sem identificador confiável)."""
    r = get_redis()
    if not r: return False
    try:
        key = f"dupcontent:{phone}:{text[:200]}"
        if r.exists(key):
            print(f"🔁 Duplicata ignorada (conteúdo): {phone} '{text[:40]}'")
            return True
        # 3 min: reentregas da UAZAPI podem chegar bem depois dos 15s originais —
        # foi uma das fontes das respostas em dobro vistas em produção.
        r.setex(key, 180, "1")
        return False
    except Exception as e:
        print(f"Dedup content error ({phone}): {e}")
        return False

def get_phone_from_msg_id(message):
    r = get_redis()
    if not r: return ''
    try:
        msg_id = (
            message.get('id') or
            message.get('messageId') or
            (message.get('key') or {}).get('id', '') or
            message.get('remoteJid', '') + str(message.get('timestamp', ''))
        )
        if not msg_id: return ''
        cached = r.get(f"dup:{msg_id}")
        return cached if cached and cached != 'unknown' else ''
    except Exception as e:
        print(f"get_phone_from_msg_id error: {e}")
        return ''

def is_visit_confirmed(history):
    assistant_texts = " ".join(
        m.get("content", "").lower() for m in history[-10:] if m.get("role") == "assistant"
    )
    return any(s in assistant_texts for s in FOLLOWUP_STOP_SIGNALS)

# ─── GOOGLE CALENDAR ─────────────────────────────────────────────────────────
CALENDAR_ID = os.environ.get('GOOGLE_CALENDAR_ID', 'evelinbaiense@gmail.com')

def get_calendar_service():
    try:
        from google.oauth2 import service_account
        from googleapiclient.discovery import build
        creds_json = os.environ.get('GOOGLE_CREDENTIALS_JSON', '')
        if not creds_json: return None
        credentials = service_account.Credentials.from_service_account_info(
            json.loads(creds_json), scopes=['https://www.googleapis.com/auth/calendar']
        )
        return build('calendar', 'v3', credentials=credentials)
    except Exception as e:
        print(f"Calendar service error: {e}")
        return None

def next_weekday_date(day_name):
    days_map = {'segunda': 0, 'terça': 1, 'terca': 1, 'quarta': 2, 'quinta': 3, 'sexta': 4, 'sábado': 5, 'sabado': 5, 'domingo': 6}
    target   = None
    for name, num in days_map.items():
        if name in day_name.lower():
            target = num
            break
    if target is None: return None
    try:
        import pytz
        from datetime import timedelta
        tz         = pytz.timezone('America/Sao_Paulo')
        today      = datetime.now(tz).date()
        days_ahead = target - today.weekday()
        if days_ahead <= 0: days_ahead += 7
        return (today + timedelta(days=days_ahead)).isoformat()
    except Exception:
        return None

def extract_and_save_visit(phone, history):
    r = get_redis()
    if not r: return
    if r.exists(f"visit:{phone}"): return
    try:
        client      = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
        recent_text = json.dumps(history[-12:], ensure_ascii=False)
        response    = client.messages.create(
            model=AI_MODEL, max_tokens=MAX_TOKENS_EXTRACT,
            thinking={"type": "disabled"},
            messages=[{"role": "user", "content": (
                "Analise esta conversa e extraia os dados da visita agendada. "
                "Responda APENAS em JSON válido:\n"
                '{"name": "nome do cliente", "day": "dia da semana em português", "period": "manhã, tarde, ou desconhecido"}\n\n'
                f"Conversa:\n{recent_text}"
            )}]
        )
        raw_text = _extract_text(response).strip()
        json_match = re.search(r'\{.*\}', raw_text, re.DOTALL)
        if not json_match:
            print(f"extract_and_save_visit: nenhum JSON encontrado na resposta ({phone}): {raw_text[:200]}")
            return
        data       = json.loads(json_match.group(0))
        name       = data.get('name', 'Cliente')
        day        = data.get('day', '')
        period     = data.get('period', 'desconhecido')
        visit_date = next_weekday_date(day) if day else None
        visit_info = {'name': name, 'phone': phone, 'day': day, 'period': period, 'date': visit_date}
        r.setex(f"visit:{phone}", 30 * 24 * 3600, json.dumps(visit_info))
        print(f"📅 Visita salva: {name} — {day} {period} ({visit_date})")
        if visit_date:
            service = get_calendar_service()
            if service:
                period_label = f" ({period})" if period != 'desconhecido' else ''
                event = {
                    'summary':     f'Visita — {name}{period_label}',
                    'location':    'Estrada dos Búzios (RJ-106), Bairro da Rasa',
                    'description': f'WhatsApp: +{phone}\nPeríodo: {period}',
                    'start':       {'date': visit_date, 'timeZone': 'America/Sao_Paulo'},
                    'end':         {'date': visit_date, 'timeZone': 'America/Sao_Paulo'},
                }
                service.events().insert(calendarId=CALENDAR_ID, body=event).execute()
                print(f"📅 Calendar: {name} em {visit_date}")
    except Exception as e:
        print(f"extract_and_save_visit error ({phone}): {e}")

def visit_reminder_sweep():
    r = get_redis()
    if not r: return
    try:
        import pytz
        from datetime import timedelta
        tz  = pytz.timezone('America/Sao_Paulo')
        now = datetime.now(tz)
        if now.hour != 8: return
        tomorrow = (now.date() + timedelta(days=1)).isoformat()
        for key in r.scan_iter("visit:*"):
            phone_num = key.split("visit:", 1)[1]
            data_raw  = r.get(key)
            if not data_raw: continue
            visit = json.loads(data_raw)
            if visit.get('date') == tomorrow:
                name       = visit.get('name', 'Cliente')
                period     = visit.get('period', '')
                period_txt = f" — {period}" if period not in ('desconhecido', '') else ''
                msg = (
                    f"🗓️ *Visita amanhã!*\n\n"
                    f"👤 {name}\n📱 +{phone_num}\n"
                    f"📅 {visit.get('day','').capitalize()}{period_txt}\n"
                    f"📍 Praia Rasa de Búzios 2\n\n"
                    f"Confirme com o cliente antes de ir! 😊"
                )
                for alert_num in ALERT_NUMBERS:
                    _send_raw(alert_num, msg)
                print(f"🔔 Lembrete: {name} amanhã")
    except Exception as e:
        print(f"visit_reminder_sweep error: {e}")

def followup_sweep():
    if not FOLLOWUP_ENABLED: return
    r = get_redis()
    if not r: return
    try:
        import pytz
        hora = datetime.now(pytz.timezone("America/Sao_Paulo")).hour
    except Exception:
        hora = 12
    if not (FOLLOWUP_DAY_START <= hora < FOLLOWUP_DAY_END): return
    now = time.time()
    try:
        for key in r.scan_iter("fu:*"):
            phone = key.split("fu:", 1)[1]
            if is_paused(phone): continue
            if in_closing_state(phone): continue  # cliente já se despediu — não perturbar
            state = get_followup_state(phone)
            if not state: continue
            stage = state.get("stage", 0)
            if stage >= 3: continue
            silent_min = (now - state.get("last_client_ts", now)) / 60.0
            history    = get_conversation(phone)
            if not history or history[-1].get("role") != "assistant": continue
            if is_visit_confirmed(history):
                extract_and_save_visit(phone, history)
                state["stage"] = 3
                set_followup_state(phone, state)
                continue
            next_stage = None
            if   stage == 0 and silent_min >= FOLLOWUP_STAGE1_MIN: next_stage = 1
            elif stage == 1 and silent_min >= FOLLOWUP_STAGE2_MIN: next_stage = 2
            elif stage == 2 and silent_min >= FOLLOWUP_STAGE3_MIN and hora >= 18: next_stage = 3
            if not next_stage: continue
            msg = generate_followup(phone, next_stage)
            if msg:
                if is_paused(phone):
                    print(f"⏸️ {phone} pausado antes do envio do follow-up — descartando.")
                    continue
                if send_and_check(phone, msg):
                    append_message(phone, "assistant", msg)
                state["stage"] = next_stage
                set_followup_state(phone, state)
                print(f"📨 Follow-up estágio {next_stage} para {phone}")
    except Exception as e:
        print(f"followup_sweep error: {e}")

# ─── FILA POR TELEFONE (anti-resposta-dupla / anti-rajada) ───────────────────
# Agrupa mensagens que o cliente manda em sequência numa única chamada de IA e
# garante que nunca rodem duas gerações simultâneas para o mesmo telefone — era
# isso que produzia as respostas "em dobro" com palavras diferentes em produção.
# Obs.: estruturas em memória do processo — funciona porque o app roda num único
# processo Flask (ver Procfile). Se um dia migrar para gunicorn com vários
# workers, isso precisa virar lock/fila no Redis.
_phone_locks   = {}
_phone_buffers = {}
_phone_seq     = {}
_registry_lock = threading.Lock()

def _enqueue_text(phone, text):
    with _registry_lock:
        _phone_buffers.setdefault(phone, []).append(text)
        _phone_seq[phone] = _phone_seq.get(phone, 0) + 1
        return _phone_seq[phone], _phone_locks.setdefault(phone, threading.Lock())

def _drain_buffer(phone):
    with _registry_lock:
        msgs = _phone_buffers.get(phone) or []
        _phone_buffers[phone] = []
        return msgs

def process_client_text(phone, text):
    """Debounce + trava por telefone: espera DEBOUNCE_SECONDS por mensagens
    seguintes do mesmo cliente, junta tudo e roda UMA geração por vez."""
    seq, lock = _enqueue_text(phone, text)
    time.sleep(DEBOUNCE_SECONDS)
    with _registry_lock:
        if _phone_seq.get(phone, 0) != seq:
            # Chegou mensagem mais nova durante a espera — a thread dela assume
            # o turno inteiro (com o buffer acumulado).
            return
    with lock:
        while True:
            msgs = _drain_buffer(phone)
            if not msgs:
                break
            handle_turn(phone, "\n".join(msgs))

def handle_turn(phone, text):
    """Um turno completo de conversa: decide se responde, chama a IA e envia."""
    # Anti-loop de despedida: se o cliente está só trocando cortesias e o bot
    # já respondeu uma despedida, não responde de novo (evita loop infinito).
    if is_closing_message(text):
        if in_closing_state(phone):
            append_message(phone, "user", text)
            print(f"🤝 {phone}: cortesia repetida — encerrando sem responder.")
            return
        set_closing_state(phone)
    else:
        clear_closing_state(phone)

    print(f"phone='{phone}', text='{text[:80]}'")

    # Detecta lead quente ANTES de chamar a IA
    history_atual   = get_conversation(phone)
    hot, hot_motivo = is_hot_lead(text, history_atual)

    reply, alert_flag, media_flag, lq_flag = get_ai_response(phone, text)
    if reply is None:
        notify_ai_failure(phone)
        return

    # Rede de segurança para mídia — não reaciona se o pacote já foi enviado nesta conversa
    if not media_flag and not was_media_sent(phone):
        media_keywords = ['quero ver', 'queria ver', 'pode mandar', 'manda sim',
                          'com certeza', 'claro que sim', 'quero as fotos',
                          'foto', 'fotos', 'video', 'vídeo', 'videos', 'vídeos']
        history_now = get_conversation(phone)
        last_bot    = next((m['content'] for m in reversed(history_now[:-1]) if m['role'] == 'assistant'), '')
        if (any(kw in text.lower() for kw in media_keywords) and
                any(kw in last_bot.lower() for kw in ['foto', 'vídeo', 'video', 'imagens', 'mandar'])):
            media_flag = True

    # Fecha a janela de risco: se a Evelin pausou manualmente enquanto a IA gerava
    # a resposta, descarta a resposta do bot em silêncio. Só vale a pena esperar
    # se ela esteve ativa nesse número há pouco — senão só atrasa toda mensagem à toa.
    if had_recent_manual_activity(phone):
        time.sleep(PAUSE_GUARD_SECONDS)
    if is_paused(phone):
        print(f"⏸️ {phone} pausado durante geração da resposta — descartando resposta do bot.")
        return

    send_and_check(phone, reply)

    # Alertas em thread — não atrasa a resposta ao cliente
    if alert_flag:
        threading.Thread(target=send_alert, args=(phone, "pergunta sem resposta", text), daemon=True).start()
    elif hot and should_send_hot_lead_alert(phone):
        threading.Thread(target=send_hot_lead_alert, args=(phone, hot_motivo, text), daemon=True).start()

    if media_flag:
        threading.Thread(target=send_media_package, args=(phone,)).start()

    # Cliente confirmou que consegue pagar → devolve o sinal pro Meta Ads
    if lq_flag:
        threading.Thread(target=queue_lead_qualified_event, args=(phone,), daemon=True).start()

    # Detecta visita confirmada
    updated_history = get_conversation(phone)
    if is_visit_confirmed(updated_history):
        threading.Thread(target=extract_and_save_visit, args=(phone, updated_history), daemon=True).start()

def _process_incoming_safe(data, message, phone, text_cmd):
    try:
        process_incoming(data, message, phone, text_cmd)
    except Exception as e:
        print(f"process_incoming error ({phone}): {e}")
        import traceback
        traceback.print_exc()

def process_incoming(data, message, phone, text_cmd):
    """Parte lenta do webhook (transcrição, IA, envio) — roda em background para
    o HTTP responder na hora. Se a UAZAPI ficar esperando a IA (20-60s), ela
    estoura o timeout e REENTREGA o webhook, gerando resposta duplicada."""
    msg_type   = message.get('type', '') or message.get('messageType', '')
    media_type = message.get('mediaType', '')

    # Se a conversa nasceu de anúncio click-to-WhatsApp, o webhook traz o id do
    # clique (ctwa_clid) — guardamos para o evento de lead qualificado do Meta.
    store_ctwa_clid(phone, data)

    # ── BOT PAUSADO ──────────────────────────────────────────────────────────
    if is_paused(phone):
        if text_cmd:
            append_message(phone, "user", text_cmd)
        print(f"⏸️  {phone} em atendimento humano.")
        return

    set_followup_state(phone, {"last_client_ts": time.time(), "stage": 0})

    text = ""

    is_audio       = msg_type in ('audio', 'ptt', 'audioMessage', 'PTT')
    is_media_audio = msg_type == 'media' and media_type in ('audio', 'ptt', 'ogg', 'opus', '')

    if is_audio or is_media_audio:
        raw = (
            message.get('url') or message.get('mediaUrl') or
            message.get('audioUrl') or message.get('content') or message.get('body')
        )
        if isinstance(raw, dict):
            audio_url = raw.get('URL') or raw.get('url') or raw.get('directPath')
            media_key = raw.get('mediaKey', '')
            if audio_url and media_key:
                try:
                    decrypt_resp = requests.post(
                        f"{UAZAPI_URL}/media/decrypt",
                        headers={"token": get_instance_token(), "Content-Type": "application/json"},
                        json={"url": audio_url, "mediaKey": media_key, "type": "audio"},
                        timeout=30
                    )
                    if decrypt_resp.status_code == 200:
                        audio_url = decrypt_resp.json().get('url', audio_url)
                except Exception as e:
                    print(f"Decrypt error: {e}")
        else:
            audio_url = raw

        if audio_url:
            text = transcribe_audio(audio_url)
            if not text:
                send_message(phone, "Oi! 😊 Não consegui ouvir o áudio. Pode me mandar por texto?")
                return
        else:
            send_message(phone, "Oi! 😊 Não consegui ouvir o áudio. Pode me mandar por texto?")
            return

    elif msg_type in ('text', 'Conversation', 'extendedTextMessage'):
        text = text_cmd

    elif msg_type == 'media' and media_type in ('image', 'video', 'sticker', 'document'):
        text = "[cliente enviou uma imagem]"

    elif msg_type == 'media':
        # Tipo de mídia não suportado (localização, contato, enquete, etc.) — não trata como áudio.
        send_message(phone, "Oi! 😊 Recebi seu arquivo, mas não consigo processar esse tipo por aqui. Pode me contar em texto?")
        return
    else:
        print(f"Skipping type: {msg_type}")
        return

    if not text:
        return

    if is_duplicate_content(phone, text):
        return

    process_client_text(phone, text)

# ─── WEBHOOK ──────────────────────────────────────────────────────────────────
@app.route('/webhook', methods=['POST'])
def webhook():
    data = request.json
    print(f"Webhook received: {list(data.keys()) if data else 'None'}")

    try:
        if not data:
            return jsonify({'status': 'no_data'}), 200

        message = data.get('message', {})
        if not message or not isinstance(message, dict):
            return jsonify({'status': 'no_message'}), 200

        if message.get('isGroup', False):
            return jsonify({'status': 'group'}), 200

        # Ignora atualizações, edições e tipos não-mensagem
        msg_type = message.get('type', '') or message.get('messageType', '')
        # Edição de mensagem chega da UAZAPI em formatos variados (flag no topo ou
        # um bloco 'editedMessage' aninhado no conteúdo) — em produção o bot estava
        # respondendo de novo a mensagens editadas porque só olhava 'isEdit'.
        edited = any(message.get(k) for k in ('isEdit', 'isEdited', 'edited', 'wasEdited'))
        if not edited:
            try:
                edited = 'editedmessage' in json.dumps(message, default=str).lower()
            except Exception:
                edited = False
        if (edited or message.get('updateType') or
                msg_type in ('messageUpdate', 'editedMessage', 'protocolMessage',
                             'reactionMessage', 'reaction', 'senderKeyDistributionMessage',
                             'messageContextInfo', 'statusUpdate')):
            if edited:
                print(f"[SKIP] Mensagem editada ignorada (type={msg_type})")
            return jsonify({'status': 'ignored'}), 200

        from_me    = message.get('fromMe', False)
        is_api     = message.get('wasSentByApi', False)
        media_type = message.get('mediaType', '')

        convo = message.get('chatId', '') or message.get('sender_pn', '')
        phone = convo.replace('@s.whatsapp.net', '').replace('@c.us', '').replace('@lid', '')
        if not phone:
            return jsonify({'status': 'no_phone'}), 200

        # ── 1) MENSAGEM ENVIADA POR VOCÊ (fromMe) ───────────────────────────
        if from_me:
            if is_api:
                return jsonify({'status': 'from_bot'}), 200

            raw_text = extract_text(message)

            # Ignora saudação automática do Facebook
            if any(kw in raw_text.lower() for kw in FB_AUTO_GREETING_KEYWORDS):
                print(f"[FB_AUTO] Saudação automática ignorada")
                return jsonify({'status': 'fb_auto_ignored'}), 200

            # A UAZAPI identifica o mesmo chat ora pelo número real, ora por um id
            # @lid — se a pausa for gravada num identificador e o fluxo do cliente
            # consultar outro, a Evelin assume a conversa e o bot continua
            # respondendo (visto em produção). Por isso pausamos TODOS os
            # identificadores candidatos, não só um.
            def _clean_jid(raw):
                return (raw.replace('@s.whatsapp.net', '').replace('@c.us', '')
                           .replace('@lid', '').replace('+', '').strip())

            candidates = set()
            if phone:
                candidates.add(phone)
            cached = get_phone_from_msg_id(message)
            if cached:
                candidates.add(cached)
            chat_data = data.get('chat', {})
            if isinstance(chat_data, dict):
                for field in ('phone', 'jid', 'chatId', 'id', 'wa_chatid'):
                    raw = chat_data.get(field, '')
                    if raw and isinstance(raw, str):
                        cleaned = _clean_jid(raw)
                        if cleaned.isdigit():
                            candidates.add(cleaned)
            if not candidates:
                candidates.add(phone)

            manual_text = raw_text.strip()
            print(f"[MANUAL] Você digitou ({'/'.join(sorted(candidates))}): '{manual_text[:40]}'")
            print(f"[MANUAL] ids: msg_chatId='{message.get('chatId', '')}' chat={json.dumps(chat_data, default=str, ensure_ascii=False)[:200]}")

            # Evento fromMe vazio e sem mídia = webhook espúrio (recibo de entrega,
            # etc.), NÃO um atendimento manual — não pausa por 12h à toa.
            if not manual_text and not media_type:
                print(f"[MANUAL] Evento vazio sem mídia ignorado.")
                return jsonify({'status': 'empty_frommme_ignored'}), 200

            for cand in candidates:
                mark_manual_activity(cand)

            if manual_text == RESUME_KEYWORD:
                for cand in candidates:
                    clear_pause(cand)
                return jsonify({'status': 'resumed'}), 200

            for cand in candidates:
                set_pause(cand)
            if manual_text:
                append_message(phone or sorted(candidates)[0], "assistant", manual_text)
            return jsonify({'status': 'paused_human_takeover'}), 200

        # ── Dedup ────────────────────────────────────────────────────────────
        if is_duplicate_msg(message, phone):
            return jsonify({'status': 'duplicate'}), 200

        text_cmd = extract_text(message).strip()

        if text_cmd.lower() == '//.':
            set_pause(phone)
            return jsonify({'status': 'paused_by_command'}), 200

        # A reativação por "." é reservada a mensagens fromMe (ver seção 1 acima) —
        # um cliente digitando "." não deve conseguir despausar o próprio atendimento.

        # ── 2) PROCESSAMENTO EM BACKGROUND ───────────────────────────────────
        # Responde o HTTP imediatamente: transcrição + IA + envio rodam em
        # thread (ver process_incoming). Sem isso a UAZAPI estoura o timeout
        # esperando e reentrega o webhook — era a principal fonte de respostas
        # duplicadas em produção.
        threading.Thread(
            target=_process_incoming_safe, args=(data, message, phone, text_cmd), daemon=True
        ).start()
        return jsonify({'status': 'accepted'}), 200

    except Exception as e:
        print(f"Webhook error: {e}")
        import traceback
        traceback.print_exc()
        return jsonify({'status': 'error', 'message': str(e)}), 500

# ─── RECOVERY ─────────────────────────────────────────────────────────────────
recovery_contacts = []
recovery_index    = 0

def load_recovery_contacts():
    global recovery_contacts
    try:
        if os.path.exists('recovery.csv'):
            with open('recovery.csv', 'r', encoding='utf-8') as f:
                reader = csv.DictReader(f)
                recovery_contacts = [row for row in reader if row.get('sent', '').lower() != 'sim']
    except Exception as e:
        print(f"Error loading recovery contacts: {e}")

def send_recovery_message():
    global recovery_index, recovery_contacts
    load_recovery_contacts()
    if not recovery_contacts or recovery_index >= len(recovery_contacts):
        recovery_index = 0
        return
    contact    = recovery_contacts[recovery_index]
    phone      = contact.get('telefone', '').replace(' ', '').replace('-', '').replace('(', '').replace(')', '')
    name       = contact.get('nome', '')
    custom_msg = contact.get('mensagem', '')
    if not phone:
        recovery_index += 1
        return
    if is_paused(phone):
        recovery_index += 1
        return
    message = custom_msg or f"Oi{' ' + name if name else ''}! Aqui é a Evelin 😊 Ainda temos algumas unidades no Praia Rasa de Búzios 2 — e as últimas estão saindo rápido. Você ainda tem interesse?"
    send_message(phone, message)
    recovery_index += 1

# ─── ROTAS ────────────────────────────────────────────────────────────────────
@app.route('/health', methods=['GET'])
def health():
    r        = get_redis()
    redis_ok = False
    if r:
        try:
            r.ping()
            redis_ok = True
        except Exception:
            pass
    return jsonify({
        'status':        'running',
        'redis':         'ok' if redis_ok else 'OFFLINE',
        'model':         AI_MODEL,
        'history_limit': HISTORY_LIMIT,
        'timestamp':     datetime.now().isoformat()
    }), 200

@app.route('/pause/<path:phone>', methods=['GET'])
def pause_toggle(phone):
    key       = request.args.get('key', '')
    admin_key = os.environ.get('ADMIN_KEY', '')
    if not admin_key or key != admin_key:
        return 'Chave incorreta.', 403
    phone_clean = phone.replace('+', '').replace('-', '').replace(' ', '')
    if is_paused(phone_clean):
        clear_pause(phone_clean)
        return f'▶️ Bot RETOMADO para {phone_clean}.', 200
    else:
        set_pause(phone_clean)
        return f'⏸️ Bot PAUSADO para {phone_clean} por 12h.', 200

@app.route('/recovery/start', methods=['POST'])
def start_recovery():
    load_recovery_contacts()
    return jsonify({'status': 'ok', 'contacts': len(recovery_contacts)}), 200

# ─── INICIALIZAÇÃO ────────────────────────────────────────────────────────────
if __name__ == '__main__':
    get_redis()
    scheduler = BackgroundScheduler()
    scheduler.add_job(send_recovery_message, 'interval', hours=RECOVERY_INTERVAL_HOURS)
    scheduler.add_job(followup_sweep,        'interval', minutes=FOLLOWUP_CHECK_MIN)
    scheduler.add_job(visit_reminder_sweep,  'interval', minutes=30)
    scheduler.add_job(capi_retry_sweep,      'interval', minutes=2)
    scheduler.start()
    port = int(os.environ.get('PORT', 5000))
    # threaded=True é obrigatório: sem isso, o servidor processa um webhook por vez e o
    # guard de pausa (sleep + re-checagem) nunca vê a mensagem manual da Evelin chegar
    # enquanto o webhook do cliente está bloqueado esperando a IA.
    app.run(host='0.0.0.0', port=port, debug=False, threaded=True)
