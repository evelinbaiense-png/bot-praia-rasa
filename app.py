from flask import Flask, request, jsonify
import anthropic
import requests
import json
import os
import time
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
# claude-haiku-4-5-20251001 = ~15x mais barato que Opus, suficiente para vendas
AI_MODEL      = os.environ.get('AI_MODEL', 'claude-haiku-4-5-20251001')
HISTORY_LIMIT = int(os.environ.get('HISTORY_LIMIT', '8'))

# ─── TRAVA DE PAUSA ──────────────────────────────────────────────────────────
RESUME_KEYWORD = '.'
PAUSE_TTL      = int(os.environ.get('PAUSE_TTL_HOURS', '12')) * 3600

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

REDIS_URL      = os.environ.get('REDIS_URL', '')
CONV_TTL       = 7 * 24 * 3600
_redis_client  = None
_redis_warned  = False

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

PASSO 5 — Visita.

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
COMO CONVERSAR
═══════════════════════════════════════════
- Uma pergunta por vez. Sempre.
- Termine com pergunta ou próximo passo claro.
- NUNCA empilhe perguntas.
- NUNCA repita a mesma frase em mensagens seguidas.
- NUNCA mande bloco grande de texto — quebre em mensagens curtas e naturais.
- Clientes mais velhos: "o senhor" / "a senhora".
- Comentário religioso: "Amém!" / "Dia abençoado".
- Português sempre, mesmo que o cliente escreva em espanhol.
- Emojis com moderação (😊 🏡 👍 📍).

═══════════════════════════════════════════
QUANDO NÃO SOUBER RESPONDER
═══════════════════════════════════════════
Apenas quando genuinamente não souber — NÃO use para localização (você tem as distâncias):
"Deixa eu confirmar essa informação pra você! 😊" + [ALERTA]

═══════════════════════════════════════════
AGENDAMENTO
═══════════════════════════════════════════
"[Nome], as visitas são de terça a domingo. Qual dia funciona melhor? Prefere manhã ou tarde?"

- Confirmou dia + período → confirmado. Pare de perguntar.
- Confirmou só o dia → também confirmado.
- ⛔ NUNCA peça hora específica.
- Quando confirmado: colete nome completo e avise sobre plantão por escala.
- Visita confirmada com nome: não peça confirmação de novo.

═══════════════════════════════════════════
DADOS DO EMPREENDIMENTO
═══════════════════════════════════════════
INFRAESTRUTURA
- Condomínio fechado e murado, meio-fio instalado, rede elétrica em andamento, água em breve.
- Guarita 24h após fundação da associação de moradores.
- Playground, praça, área verde, bosque. Quadras com vista mar e vista serra.
- Próximo a condomínios de alto padrão; região de kitesurf.
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
- Prazo: 12 a 156 parcelas (12 anos). IGPM: correção anual.
- Simulação detalhada: direcione para a visita.

DOCUMENTAÇÃO (RGI)
Tem RGI. A incorporadora está finalizando na prefeitura. Transferência para o nome do comprador é opcional e por conta do cliente após quitar.

VISITAS: terça a domingo, qualquer horário combinado.
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

def _send_raw(phone, text):
    url     = f"{UAZAPI_URL}/send/text"
    headers = {"token": get_instance_token(), "Content-Type": "application/json"}
    data    = {"number": phone, "text": text}
    try:
        response = requests.post(url, headers=headers, json=data, timeout=10)
        print(f"Alert sent to {phone}: {response.status_code}")
        return response
    except Exception as e:
        print(f"Error sending alert to {phone}: {e}")
        return None

def send_message(phone, text):
    url     = f"{UAZAPI_URL}/send/text"
    headers = {"token": get_instance_token(), "Content-Type": "application/json"}
    data    = {"number": phone, "text": text}
    try:
        response = requests.post(url, headers=headers, json=data, timeout=10)
        print(f"Text sent to {phone}: {response.status_code}")
        return response
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
        print(f"Media package complete for {phone}")
    except Exception as e:
        print(f"Error in send_media_package for {phone}: {e}")

def send_and_check(phone, text):
    resp   = send_message(phone, text)
    status = getattr(resp, 'status_code', None) if resp is not None else None
    if status != 200:
        print(f"❌ FALHA DE ENVIO para {phone} (status {status}).")
        for number in ALERT_NUMBERS:
            if number != phone:
                _send_raw(number, f"⚠️ Falha ao enviar para {phone} (status {status}). Verifique o WhatsApp.")
        return False
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
            model=AI_MODEL, max_tokens=600, system=system, messages=api_messages
        )
    except Exception as e:
        print(f"❌ ERRO NA IA para {phone}: {e}")
        return None, False, False

    reply_raw   = response.content[0].text
    alert_flag  = '[ALERTA]' in reply_raw
    media_flag  = '[ENVIAR_MIDIA]' in reply_raw
    reply_clean = reply_raw.replace('[ALERTA]', '').replace('[ENVIAR_MIDIA]', '').strip()

    hist_text = reply_clean + ("\n[Enviei fotos e vídeos e perguntei: O que achou?]" if media_flag else "")
    append_message(phone, "assistant", hist_text)

    return reply_clean, alert_flag, media_flag

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
        response = client.messages.create(model=AI_MODEL, max_tokens=300, system=system, messages=api_messages)
        msg      = response.content[0].text
        return msg.replace('[ALERTA]', '').replace('[ENVIAR_MIDIA]', '').strip()
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
            print(f"🔁 Duplicata ignorada: {msg_id[:30]}")
            return True
        r.setex(key, 120, phone or "unknown")
        return False
    except Exception as e:
        print(f"Dedup error: {e}")
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
            model=AI_MODEL, max_tokens=150,
            messages=[{"role": "user", "content": (
                "Analise esta conversa e extraia os dados da visita agendada. "
                "Responda APENAS em JSON válido:\n"
                '{"name": "nome do cliente", "day": "dia da semana em português", "period": "manhã, tarde, ou desconhecido"}\n\n'
                f"Conversa:\n{recent_text}"
            )}]
        )
        data       = json.loads(response.content[0].text.strip())
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
                if send_and_check(phone, msg):
                    append_message(phone, "assistant", msg)
                state["stage"] = next_stage
                set_followup_state(phone, state)
                print(f"📨 Follow-up estágio {next_stage} para {phone}")
    except Exception as e:
        print(f"followup_sweep error: {e}")

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
        if (message.get('isEdit') or message.get('updateType') or
                msg_type in ('messageUpdate', 'editedMessage', 'protocolMessage',
                             'reactionMessage', 'senderKeyDistributionMessage',
                             'messageContextInfo', 'statusUpdate')):
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

            pause_phone = get_phone_from_msg_id(message)
            if not pause_phone:
                chat_data   = data.get('chat', {})
                raw         = (chat_data.get('phone', '') or chat_data.get('jid', '') or chat_data.get('chatId', '')) if isinstance(chat_data, dict) else ''
                pause_phone = raw.replace('@s.whatsapp.net', '').replace('@c.us', '').replace('+', '') if raw else phone

            manual_text = raw_text.strip()
            print(f"[MANUAL] Você digitou para {pause_phone}: '{manual_text[:40]}'")

            if manual_text == RESUME_KEYWORD:
                clear_pause(pause_phone)
                return jsonify({'status': 'resumed'}), 200

            set_pause(pause_phone)
            if manual_text:
                append_message(pause_phone, "assistant", manual_text)
            return jsonify({'status': 'paused_human_takeover'}), 200

        # ── Dedup ────────────────────────────────────────────────────────────
        if is_duplicate_msg(message, phone):
            return jsonify({'status': 'duplicate'}), 200

        text_cmd = extract_text(message).strip()

        if text_cmd.lower() == '//.':
            set_pause(phone)
            return jsonify({'status': 'paused_by_command'}), 200

        if text_cmd == RESUME_KEYWORD:
            clear_pause(phone)
            return jsonify({'status': 'resumed'}), 200

        # ── 2) BOT PAUSADO ───────────────────────────────────────────────────
        if is_paused(phone):
            if text_cmd:
                append_message(phone, "user", text_cmd)
            print(f"⏸️  {phone} em atendimento humano.")
            return jsonify({'status': 'paused_no_reply'}), 200

        # ── 3) FLUXO NORMAL ──────────────────────────────────────────────────
        set_followup_state(phone, {"last_client_ts": time.time(), "stage": 0})

        text = ""

        is_audio       = msg_type in ('audio', 'ptt', 'audioMessage', 'PTT')
        is_media_audio = msg_type == 'media' and media_type not in ('image', 'video', 'document', 'sticker')

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
                    return jsonify({'status': 'ok'}), 200
            else:
                send_message(phone, "Oi! 😊 Não consegui ouvir o áudio. Pode me mandar por texto?")
                return jsonify({'status': 'ok'}), 200

        elif msg_type in ('text', 'Conversation', 'extendedTextMessage'):
            text = text_cmd

        elif msg_type == 'media' and media_type in ('image', 'video', 'sticker', 'document'):
            reply, alert_flag, media_flag = get_ai_response(phone, "[cliente enviou uma imagem]")
            if reply is None:
                notify_ai_failure(phone)
                return jsonify({'status': 'ai_error'}), 200
            send_and_check(phone, reply)
            if alert_flag:
                threading.Thread(target=send_alert, args=(phone, "pergunta sem resposta", "[imagem]"), daemon=True).start()
            if media_flag:
                threading.Thread(target=send_media_package, args=(phone,)).start()
            return jsonify({'status': 'ok'}), 200
        else:
            print(f"Skipping type: {msg_type}")
            return jsonify({'status': 'not_supported'}), 200

        if not text:
            return jsonify({'status': 'no_text'}), 200

        print(f"phone='{phone}', text='{text[:80]}'")

        # Detecta lead quente ANTES de chamar a IA
        history_atual   = get_conversation(phone)
        hot, hot_motivo = is_hot_lead(text, history_atual)

        reply, alert_flag, media_flag = get_ai_response(phone, text)
        if reply is None:
            notify_ai_failure(phone)
            return jsonify({'status': 'ai_error'}), 200

        # Rede de segurança para mídia
        if not media_flag:
            media_keywords = ['quero ver', 'queria ver', 'pode mandar', 'manda sim',
                              'com certeza', 'claro que sim', 'quero as fotos',
                              'foto', 'fotos', 'video', 'vídeo', 'videos', 'vídeos']
            history_now = get_conversation(phone)
            last_bot    = next((m['content'] for m in reversed(history_now[:-1]) if m['role'] == 'assistant'), '')
            if (any(kw in text.lower() for kw in media_keywords) and
                    any(kw in last_bot.lower() for kw in ['foto', 'vídeo', 'video', 'imagens', 'mandar'])):
                media_flag = True

        send_and_check(phone, reply)

        # Alertas em thread — não atrasa a resposta ao cliente
        if alert_flag:
            threading.Thread(target=send_alert, args=(phone, "pergunta sem resposta", text), daemon=True).start()
        elif hot:
            threading.Thread(target=send_hot_lead_alert, args=(phone, hot_motivo, text), daemon=True).start()

        if media_flag:
            threading.Thread(target=send_media_package, args=(phone,)).start()

        # Detecta visita confirmada
        updated_history = get_conversation(phone)
        if is_visit_confirmed(updated_history):
            threading.Thread(target=extract_and_save_visit, args=(phone, updated_history), daemon=True).start()

        return jsonify({'status': 'ok'}), 200

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
    scheduler.start()
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port, debug=False)
