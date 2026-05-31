from flask import Flask, request, jsonify
import anthropic
import requests
import json
import os
import time
import tempfile
import threading
from datetime import datetime
from apscheduler.schedulers.background import BackgroundScheduler
import csv

app = Flask(__name__)

ANTHROPIC_API_KEY = os.environ.get('ANTHROPIC_API_KEY')
OPENAI_API_KEY    = os.environ.get('OPENAI_API_KEY')
UAZAPI_URL        = os.environ.get('UAZAPI_URL', 'https://evelinabreu.uazapi.com')
UAZAPI_TOKEN      = os.environ.get('UAZAPI_TOKEN')
INSTANCE_NAME     = os.environ.get('INSTANCE_NAME', 'evelin')
RECOVERY_INTERVAL_HOURS = float(os.environ.get('RECOVERY_INTERVAL_HOURS', '2'))
ALERT_NUMBERS     = ['5522999004419', '5522995511909']

# ─── TRAVA DE PAUSA (ATENDIMENTO HUMANO) ─────────────────────────────────────
# Quando a Evelin digita manualmente numa conversa, o bot PAUSA aquele contato.
# Ele só volta quando ela enviar a palavra-chave abaixo, ou após PAUSE_TTL.
RESUME_KEYWORD = '.'                                              # ponto final pra reativar o bot
PAUSE_TTL = int(os.environ.get('PAUSE_TTL_HOURS', '12')) * 3600   # tempo de segurança (12h padrão)

# ─── FOLLOW-UP AUTOMÁTICO (REENGAJAMENTO) ────────────────────────────────────
# Quando o cliente para de responder, o bot cutuca de novo em 3 estágios.
FOLLOWUP_ENABLED    = os.environ.get('FOLLOWUP_ENABLED', 'true').lower() == 'true'
FOLLOWUP_STAGE1_MIN = int(os.environ.get('FOLLOWUP_STAGE1_MIN', '10'))    # 1º toque: poucos minutos
FOLLOWUP_STAGE2_MIN = int(os.environ.get('FOLLOWUP_STAGE2_MIN', '60'))    # 2º toque: ~1 hora
FOLLOWUP_STAGE3_MIN = int(os.environ.get('FOLLOWUP_STAGE3_MIN', '360'))   # 3º toque: ~6h (e só à noite)
FOLLOWUP_DAY_START  = int(os.environ.get('FOLLOWUP_DAY_START', '8'))      # não cutuca antes das 8h
FOLLOWUP_DAY_END    = int(os.environ.get('FOLLOWUP_DAY_END', '21'))       # nem depois das 21h
FOLLOWUP_CHECK_MIN  = int(os.environ.get('FOLLOWUP_CHECK_MIN', '5'))      # verifica a cada 5 min

# ─── REDIS (MEMÓRIA PERSISTENTE) ─────────────────────────────────────────────
import redis as _redis_lib

REDIS_URL = os.environ.get('REDIS_URL', '')
CONV_TTL  = 7 * 24 * 3600  # 7 dias em segundos
_redis_client = None
_redis_warned = False

def get_redis():
    """Conecta no Redis com ping. Loga claramente se falhar (em vez de falhar calado)."""
    global _redis_client, _redis_warned
    if _redis_client is not None:
        return _redis_client
    if not REDIS_URL:
        if not _redis_warned:
            print("⚠️⚠️⚠️ REDIS_URL NÃO CONFIGURADA — O BOT ESTÁ SEM MEMÓRIA! "
                  "Ele vai tratar cada mensagem como conversa nova. Configure REDIS_URL no Railway.")
            _redis_warned = True
        return None
    try:
        client = _redis_lib.from_url(REDIS_URL, decode_responses=True)
        client.ping()
        _redis_client = client
        print("✅ Redis conectado — memória ativa.")
        return _redis_client
    except Exception as e:
        if not _redis_warned:
            print(f"❌❌❌ FALHA AO CONECTAR NO REDIS: {e} — MEMÓRIA DESLIGADA. "
                  "O bot vai se perder. Verifique o serviço Redis.")
            _redis_warned = True
        return None

def get_conversation(phone):
    r = get_redis()
    if not r:
        return []
    try:
        data = r.get(f"conv:{phone}")
        return json.loads(data) if data else []
    except Exception as e:
        print(f"Redis get error ({phone}): {e}")
        return []

def save_conversation(phone, messages):
    r = get_redis()
    if not r:
        return
    try:
        r.setex(f"conv:{phone}", CONV_TTL, json.dumps(messages))
    except Exception as e:
        print(f"Redis save error ({phone}): {e}")

def append_message(phone, role, content):
    history = get_conversation(phone)
    history.append({"role": role, "content": content})
    save_conversation(phone, history)
    return history

# ── Funções da trava de pausa ────────────────────────────────────────────────
def is_paused(phone):
    r = get_redis()
    if not r:
        return False
    try:
        return r.exists(f"pause:{phone}") == 1
    except Exception as e:
        print(f"Redis is_paused error ({phone}): {e}")
        return False

def set_pause(phone):
    r = get_redis()
    if not r:
        return
    try:
        r.setex(f"pause:{phone}", PAUSE_TTL, "1")
        print(f"⏸️  Bot PAUSADO para {phone} (atendimento humano).")
    except Exception as e:
        print(f"Redis set_pause error ({phone}): {e}")

def clear_pause(phone):
    r = get_redis()
    if not r:
        return
    try:
        r.delete(f"pause:{phone}")
        print(f"▶️  Bot REATIVADO para {phone}.")
    except Exception as e:
        print(f"Redis clear_pause error ({phone}): {e}")

# ── Estado do follow-up por conversa ─────────────────────────────────────────
def get_followup_state(phone):
    r = get_redis()
    if not r:
        return None
    try:
        data = r.get(f"fu:{phone}")
        return json.loads(data) if data else None
    except Exception as e:
        print(f"Redis fu get error ({phone}): {e}")
        return None

def set_followup_state(phone, state):
    r = get_redis()
    if not r:
        return
    try:
        r.setex(f"fu:{phone}", CONV_TTL, json.dumps(state))
    except Exception as e:
        print(f"Redis fu set error ({phone}): {e}")

# ─── MÍDIAS ──────────────────────────────────────────────────────────────────

PHOTOS = [
    "https://res.cloudinary.com/dd6o3z4ma/image/upload/v1779039971/WhatsApp_Image_2026-05-17_at_13.23.56_itxlrx.jpg",
    "https://res.cloudinary.com/dd6o3z4ma/image/upload/v1779039971/WhatsApp_Image_2026-05-17_at_13.23.57_wmlvhl.jpg",
    "https://res.cloudinary.com/dd6o3z4ma/image/upload/v1779039971/WhatsApp_Image_2026-05-17_at_13.23.35_eioep1.jpg",
    "https://res.cloudinary.com/dd6o3z4ma/image/upload/v1779039971/WhatsApp_Image_2026-05-17_at_13.23.57_1_mszdep.jpg",
    "https://res.cloudinary.com/dd6o3z4ma/image/upload/v1779039971/WhatsApp_Image_2026-05-17_at_13.23.35_1_kidkrk.jpg",
    "https://res.cloudinary.com/dd6o3z4ma/image/upload/v1779039971/WhatsApp_Image_2026-05-17_at_13.23.34_cxhs57.jpg",
    "https://res.cloudinary.com/dd6o3z4ma/image/upload/v1779040450/Editedimage_1776197718307_ms669e.png",
]
VIDEO_URL_1 = "https://res.cloudinary.com/dd6o3z4ma/video/upload/v1779039974/WhatsApp_Video_2026-05-17_at_13.15.06_zrc6fs.mp4"
VIDEO_URL_2 = "https://res.cloudinary.com/dd6o3z4ma/video/upload/v1779039973/WhatsApp_Video_2026-05-17_at_13.19.32_b5ptia.mp4"

# ─── MENSAGENS ────────────────────────────────────────────────────────────────

GREETING = "Oi! Aqui é a Evelin Abreu, corretora de imóveis. Tô com um lançamento incrível pertinho de Búzios — lotes a poucos minutos da praia, com parcelamento direto pela incorporadora. Você já ouviu falar do Praia Rasa de Búzios 2 ou é a primeira vez?"

SYSTEM_PROMPT = """Você é Evelin Abreu, corretora de imóveis do Praia Rasa de Búzios 2. Atenda como a própria Evelin no WhatsApp — simpática, direta, como um bate-papo profissional. Nunca revele que é IA. Se perguntarem, é a própria Evelin.

═══════════════════════════════════════════
SEU FLUXO NATURAL
═══════════════════════════════════════════
Siga essa ordem quando o cliente deixar você conduzir. Se ele puxar outro assunto primeiro, acompanhe — não force a ordem.

PASSO 1 — Descubra o objetivo:
Pergunte de forma curta: "Você está pensando em morar, ter uma casa de veraneio ou investir?"

PASSO 2 — Mande as fotos e vídeos (sempre, independente da resposta):
Não peça permissão — avise e mande direto: "Deixa eu já te mostrar o empreendimento!" + [ENVIAR_MIDIA]

PASSO 3 — Pergunte se pode mandar a localização:
"Posso te mandar a localização pra você ter uma ideia de onde fica?"
Se sim: envie o link https://www.google.com/maps/@-22.7238716,-42.001362,493m

PASSO 4 — Pergunte se pode mandar os valores:
"Posso te passar os valores dos lotes?"
Se sim: apresente os lotes 300m² e 600m² com as PARCELAS.
⚠️ NUNCA mencione valor à vista por iniciativa própria. Só se o cliente perguntar explicitamente.

PASSO 5 — Conduza naturalmente:
Com objetivo, mídias, localização e valores passados, qualifique conforme a conversa e conduza para a visita quando sentir abertura.

═══════════════════════════════════════════
COMO CONVERSAR
═══════════════════════════════════════════
- Bate-papo profissional — leve, direto, sem formalidade excessiva.
- Uma pergunta por vez. Curta e objetiva.
- Sempre termine com uma pergunta ou próximo passo claro.
- NUNCA empilhe perguntas.
- NUNCA repita a mesma frase ou abertura em mensagens seguidas.
- Clientes mais velhos: "o senhor" / "a senhora" com naturalidade.
- Comentário religioso: "Amém!" / "Dia abençoado".
- Idioma: português, mesmo que o cliente escreva em espanhol.
- Emojis com moderação (😊 🏡 👍 📍).

═══════════════════════════════════════════
QUANDO NÃO SOUBER RESPONDER
═══════════════════════════════════════════
Diga: "Deixa eu confirmar essa informação pra você! 😊" + [ALERTA]
NUNCA invente dados, especialmente número de parcelas ou prazo de financiamento.
Se perguntarem sobre prazo ou quantidade de parcelas: "O consultor apresenta as condições detalhadas pessoalmente — assim você já vê os lotes e tira todas as dúvidas na hora 😊"

═══════════════════════════════════════════
AGENDAMENTO
═══════════════════════════════════════════
Quando o cliente demonstrar interesse em visitar:
"[Nome], as visitas são de terça a domingo. Qual dia funciona melhor pra você? Prefere manhã ou tarde?"

REGRAS DO AGENDAMENTO:
- Se o cliente confirmar dia + período (manhã/tarde) → visita confirmada. Pare de perguntar.
- Se o cliente confirmar só o dia (sem manhã/tarde) → também está confirmado. Não insista no período.
- ⛔ NUNCA peça hora específica (14h, 15h, 16h, etc.). Manhã ou tarde é suficiente. NUNCA sugira uma hora ("Pode ser 15h?"). Se o cliente mencionar uma hora, aceite — mas você nunca pergunta nem sugere.
- Quando confirmado: colete o nome completo do cliente e reforce o aviso de plantão:
  "Só me avisa antes de ir. Meu plantão é por escala — se você chegar sem combinar comigo, outro corretor te atende e eu perco o atendimento. É só confirmar aqui que eu te garanto."
- Quando a visita estiver confirmada (dia e nome coletados): não peça confirmação de novo.

═══════════════════════════════════════════
DADOS DO EMPREENDIMENTO — PRAIA RASA DE BÚZIOS 2
═══════════════════════════════════════════
Produto único: você vende apenas este empreendimento. Nunca pergunte em que cidade ou região o cliente busca.

LOCALIZAÇÃO
- Estrada dos Búzios (RJ-106), Bairro da Rasa, divisa Búzios/Cabo Frio.
- 800m da Praia Rasa | 3 minutos da praia | Geribá a 8km.
- Diga sempre "próximo a Búzios". Só mencione Cabo Frio se perguntarem o endereço.
- Link do mapa: https://www.google.com/maps/@-22.7238716,-42.001362,493m

⚠️ DISTÂNCIAS — REGRA ABSOLUTA:
Você só conhece as distâncias acima (Praia Rasa e Geribá). Para QUALQUER outra praia ou localidade que o cliente mencionar (Tucuns, Areté, João Fernandes, Ferradura, etc.), NUNCA invente uma distância nem diga que é "próximo", "pertinho" ou "ao lado". Em vez disso, informe onde o empreendimento fica e ofereça o link do mapa para ele verificar:
"O empreendimento fica na Estrada dos Búzios (RJ-106), Bairro da Rasa. Te mando o link do mapa pra você ver a distância exata de onde você está 😊 [link do mapa]"
Se não tiver certeza de qualquer informação geográfica: use [ALERTA].

INFRAESTRUTURA
- Condomínio fechado e murado, meio-fio instalado, rede elétrica em andamento, água em breve.
- Guarita 24h quando a associação de moradores for fundada.
- Playground, praça, área verde e bosque. Quadras com vista mar e vista serra.
- Próximo a condomínios de alto padrão; região de kitesurf.
- Taxa da associação: 10% do salário mínimo, só após a entrega, já prevista em contrato.

LOTES 300m²
- Entrada R$7.000 | Parcelas a partir de R$899/mês (reajuste anual pelo IGPM).
- Vista mar: a partir de R$1.199/mês.

LOTES 600m²
- Entrada R$14.000 | Parcelas a partir de R$1.599/mês (reajuste anual pelo IGPM).
- Vista mar: a partir de R$1.999/mês.

VALOR À VISTA — nunca ofereça. Só se o cliente perguntar:
- 300m²: a partir de R$90.000. | 600m²: a partir de R$160.000.

FINANCIAMENTO
- Direto pela incorporadora, sem SPC/Serasa, sem banco.
- Primeira parcela em 45 dias. Pode construir com 3 parcelas pagas.
- Prazo: de 12 a 156 parcelas (12 anos). Se o cliente quiser pagar em menos tempo, pode escolher um prazo menor — de 12 até 156x.
- IGPM: índice de correção anual, uma vez por ano.
- Para simular parcelas em prazo específico ou ver tabela completa: direcione para a visita ("o consultor faz a simulação na hora").

DOCUMENTAÇÃO (RGI)
"Tem RGI sim. A incorporadora está finalizando na prefeitura. Quem quitar o lote tem direito à transferência para o próprio nome — é opcional e fica por conta do cliente."

VISITAS
De terça a domingo, qualquer horário combinado. Confirme sempre dia, horário e nome. Reforce o aviso de plantão."""


# ─── FUNÇÕES DE ENVIO ─────────────────────────────────────────────────────────

def get_instance_token():
    return os.environ.get('INSTANCE_TOKEN', UAZAPI_TOKEN)


def send_message(phone, text):
    url = f"{UAZAPI_URL}/send/text"
    headers = {"token": get_instance_token(), "Content-Type": "application/json"}
    data = {"number": phone, "text": text}
    try:
        response = requests.post(url, headers=headers, json=data, timeout=10)
        print(f"Text sent to {phone}: {response.status_code}")
        return response
    except Exception as e:
        print(f"Error sending text: {e}")
        return None


def send_image(phone, image_url, caption=""):
    headers = {"token": get_instance_token(), "Content-Type": "application/json"}
    data = {"number": phone, "type": "image", "file": image_url, "caption": caption}
    try:
        response = requests.post(f"{UAZAPI_URL}/send/media", headers=headers, json=data, timeout=30)
        print(f"Image sent to {phone}: {response.status_code}")
        return response
    except Exception as e:
        print(f"Error sending image: {e}")
        return None


def send_video(phone, video_url, caption=""):
    headers = {"token": get_instance_token(), "Content-Type": "application/json"}
    data = {"number": phone, "type": "video", "file": video_url, "caption": caption}
    try:
        response = requests.post(f"{UAZAPI_URL}/send/media", headers=headers, json=data, timeout=60)
        print(f"Video sent to {phone}: {response.status_code}")
        return response
    except Exception as e:
        print(f"Error sending video: {e}")
        return None


def send_media_package(phone):
    """Envia vídeos e fotos + a pergunta de reengajamento.
    NÃO faz append no histórico: o registro já é feito (consolidado) em get_ai_response,
    evitando dois turnos de assistant seguidos."""
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


def send_alert(phone_client):
    alert_msg = f"⚠️ ALERTA — Cliente {phone_client} fez uma pergunta que não soube responder. Assuma a conversa!"
    for number in ALERT_NUMBERS:
        send_message(number, alert_msg)


def send_and_check(phone, text):
    """Envia e confirma. Se a uazapi não retornar 200 (ex.: 503 = WhatsApp
    desconectado), registra no log e tenta te avisar."""
    resp = send_message(phone, text)
    status = getattr(resp, 'status_code', None) if resp is not None else None
    if status != 200:
        print(f"❌ FALHA DE ENVIO para {phone} (status {status}). WhatsApp pode estar desconectado.")
        for number in ALERT_NUMBERS:
            if number != phone:
                send_message(number, f"⚠️ Não consegui enviar pro cliente {phone} (status {status}). "
                                     f"Verifique se o WhatsApp está conectado na uazapi.")
        return False
    return True


def notify_ai_failure(phone):
    """Quando a IA falha (sem saldo na API, limite ou instabilidade): te avisa e
    dá um retorno leve ao cliente, em vez de deixá-lo no vácuo."""
    for number in ALERT_NUMBERS:
        if number != phone:
            send_message(number, f"⚠️ A IA falhou ao responder o cliente {phone}. "
                                 f"Pode ser saldo da API esgotado, limite atingido ou instabilidade. Assuma a conversa!")
    send_message(phone, "Oi! 😊 Só um instante que já te respondo certinho.")


# ─── TRANSCRIÇÃO DE ÁUDIO ─────────────────────────────────────────────────────

def transcribe_audio(audio_url):
    if not OPENAI_API_KEY:
        return None
    try:
        import openai
        client = openai.OpenAI(api_key=OPENAI_API_KEY)
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


# ─── HELPER: extrair texto de uma mensagem ────────────────────────────────────

def extract_text(message):
    return (
        message.get('text') or message.get('body') or
        message.get('content') or message.get('conversation') or ''
    )


# ─── IA ───────────────────────────────────────────────────────────────────────

def get_ai_response(phone, user_message):
    """Gera a resposta, salva no histórico já LIMPA (sem as tags internas) e
    retorna (texto_limpo, alert_flag, media_flag)."""
    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

    history = append_message(phone, "user", user_message)

    # Contexto de horário (só no system prompt)
    import pytz
    try:
        br_time = datetime.now(pytz.timezone("America/Sao_Paulo"))
        hora = br_time.strftime("%H:%M")
        hora_int = br_time.hour
        saudacao = "Bom dia" if hora_int < 12 else ("Boa tarde" if hora_int < 18 else "Boa noite")
        time_info = f"\n\n[Horário atual: {hora} — use '{saudacao}' apenas se for o primeiro contato]"
    except Exception:
        time_info = ""

    system = SYSTEM_PROMPT + time_info

    last_20 = history[-20:]
    api_messages = [
        {"role": "user",      "content": "Olá"},
        {"role": "assistant", "content": GREETING},
    ] + last_20

    try:
        response = client.messages.create(
            model="claude-opus-4-8",
            max_tokens=600,
            system=system,
            messages=api_messages
        )
    except Exception as e:
        # Falha da IA: sem saldo na API (erro 400), limite atingido (429) ou
        # instabilidade. Em vez de quebrar o webhook e ficar mudo, sinaliza o
        # erro (reply=None) para o webhook tratar e te avisar.
        print(f"❌ ERRO NA IA (Anthropic) para {phone}: {e}")
        return None, False, False

    reply_raw   = response.content[0].text
    alert_flag  = '[ALERTA]' in reply_raw
    media_flag  = '[ENVIAR_MIDIA]' in reply_raw
    reply_clean = reply_raw.replace('[ALERTA]', '').replace('[ENVIAR_MIDIA]', '').strip()

    # Histórico: versão limpa (sem tags). Se enviou mídia, registra num ÚNICO turno
    # de assistant que já inclui a nota da pergunta de reengajamento.
    if media_flag:
        hist_text = reply_clean + "\n[Enviei as fotos e vídeos do empreendimento e perguntei: O que achou?]"
    else:
        hist_text = reply_clean
    append_message(phone, "assistant", hist_text)

    return reply_clean, alert_flag, media_flag


# ─── FOLLOW-UP (REENGAJAMENTO QUANDO O CLIENTE SOME) ──────────────────────────

def generate_followup(phone, stage):
    """Gera uma mensagem de reengajamento conforme o estágio, usando o contexto da conversa."""
    try:
        client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
        history = get_conversation(phone)
        situ = {
            1: "O cliente parou de responder há poucos minutos. Mande UMA mensagem curta e leve pra retomar de onde parou, sem cobrar. Termine com uma pergunta fácil de responder.",
            2: "O cliente está sem responder há cerca de 1 hora. Mande uma mensagem calorosa com uma pergunta NOVA pra reengajar e puxar a visita ao empreendimento. Não repita o que já foi dito.",
            3: "É o fim do dia e o cliente não voltou. Mande uma última mensagem do dia, simpática e sem pressão, reforçando o convite pra conhecer pessoalmente e deixando a porta aberta pra ele responder quando puder.",
        }
        system = SYSTEM_PROMPT + (
            f"\n\n[VOCÊ ESTÁ RETOMANDO O CONTATO com um cliente que parou de responder. "
            f"{situ.get(stage, situ[1])} Gere SÓ a mensagem, curta e natural. "
            f"NÃO cumprimente de novo — a conversa já está em andamento.]"
        )
        last_20 = history[-20:]
        api_messages = [
            {"role": "user",      "content": "Olá"},
            {"role": "assistant", "content": GREETING},
        ] + last_20 + [
            {"role": "user", "content": "[O cliente ficou em silêncio. Escreva agora a mensagem de retomada, seguindo a instrução.]"}
        ]
        response = client.messages.create(
            model="claude-opus-4-8",
            max_tokens=300,
            system=system,
            messages=api_messages
        )
        msg = response.content[0].text
        return msg.replace('[ALERTA]', '').replace('[ENVIAR_MIDIA]', '').strip()
    except Exception as e:
        print(f"generate_followup error ({phone}): {e}")
        return None


FOLLOWUP_STOP_SIGNALS = [
    "tá anotado", "tá confirmado", "agendado pra", "nos vemos",
    "até terça", "até segunda", "até quarta", "até quinta",
    "até sexta", "até sábado", "até domingo", "te espero lá",
    "já tá anotado", "perfeito! terça", "perfeito! sábado",
    "terça às", "sábado às", "domingo às", "segunda às",
    "quinta à tarde", "quinta de manhã", "quarta à tarde", "quarta de manhã",
    "sábado à tarde", "sábado de manhã", "domingo à tarde", "domingo de manhã",
    "segunda à tarde", "segunda de manhã", "terça à tarde", "terça de manhã",
    "sexta à tarde", "sexta de manhã",
    "show, quinta", "show, quarta", "show, terça", "show, sábado",
    "show, sexta", "show, domingo", "show, segunda",
    "fechado,", "fechado!", "✅", "te espero", "até lá",
]

def is_duplicate_msg(message):
    """Evita resposta dupla quando o Webhook Global e o da instância disparam ao mesmo tempo."""
    r = get_redis()
    if not r:
        return False
    try:
        # Tenta extrair o ID único da mensagem em vários formatos do uazapi
        msg_id = (
            message.get('id') or
            message.get('messageId') or
            (message.get('key') or {}).get('id', '') or
            message.get('remoteJid', '') + str(message.get('timestamp', ''))
        )
        if not msg_id:
            return False
        key = f"dup:{msg_id}"
        if r.exists(key):
            print(f"🔁 Mensagem duplicada ignorada: {msg_id[:30]}")
            return True
        r.setex(key, 30, "1")  # TTL de 30 segundos
        return False
    except Exception as e:
        print(f"Dedup error: {e}")
        return False


def is_visit_confirmed(history):
    """True se o histórico recente indica que a visita já foi agendada — para os follow-ups."""
    assistant_texts = " ".join(
        m.get("content", "").lower()
        for m in history[-10:]
        if m.get("role") == "assistant"
    )
    return any(s in assistant_texts for s in FOLLOWUP_STOP_SIGNALS)



# ─── GOOGLE CALENDAR + LEMBRETE DE VISITA ────────────────────────────────────

CALENDAR_ID = os.environ.get('GOOGLE_CALENDAR_ID', 'evelinbaiense@gmail.com')

def get_calendar_service():
    """Retorna o serviço do Google Calendar usando as credenciais da service account."""
    try:
        from google.oauth2 import service_account
        from googleapiclient.discovery import build
        creds_json = os.environ.get('GOOGLE_CREDENTIALS_JSON', '')
        if not creds_json:
            return None
        creds_data = json.loads(creds_json)
        credentials = service_account.Credentials.from_service_account_info(
            creds_data, scopes=['https://www.googleapis.com/auth/calendar']
        )
        return build('calendar', 'v3', credentials=credentials)
    except Exception as e:
        print(f"Calendar service error: {e}")
        return None


def next_weekday_date(day_name):
    """Calcula a próxima data para um dia da semana em português."""
    days_map = {
        'segunda': 0, 'terça': 1, 'terca': 1, 'quarta': 2,
        'quinta': 3, 'sexta': 4, 'sábado': 5, 'sabado': 5, 'domingo': 6
    }
    target = None
    for name, num in days_map.items():
        if name in day_name.lower():
            target = num
            break
    if target is None:
        return None
    try:
        import pytz
        from datetime import timedelta
        tz = pytz.timezone('America/Sao_Paulo')
        today = datetime.now(tz).date()
        days_ahead = target - today.weekday()
        if days_ahead <= 0:
            days_ahead += 7
        return (today + timedelta(days=days_ahead)).isoformat()
    except Exception:
        return None


def extract_and_save_visit(phone, history):
    """Extrai detalhes da visita do histórico, salva no Redis e cria evento no Google Agenda."""
    r = get_redis()
    if not r:
        return
    if r.exists(f"visit:{phone}"):
        return  # já salvo
    try:
        client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
        recent_text = json.dumps(history[-12:], ensure_ascii=False)
        response = client.messages.create(
            model="claude-opus-4-8",
            max_tokens=150,
            messages=[{
                "role": "user",
                "content": (
                    "Analise esta conversa e extraia os dados da visita agendada. "
                    "Responda APENAS em JSON válido, sem texto adicional:\n"
                    '{"name": "nome do cliente", "day": "dia da semana em português", '
                    '"period": "manhã, tarde, ou desconhecido"}\n\n'
                    f"Conversa:\n{recent_text}"
                )
            }]
        )
        data = json.loads(response.content[0].text.strip())
        name = data.get('name', 'Cliente')
        day = data.get('day', '')
        period = data.get('period', 'desconhecido')
        visit_date = next_weekday_date(day) if day else None
        visit_info = {'name': name, 'phone': phone, 'day': day, 'period': period, 'date': visit_date}
        r.setex(f"visit:{phone}", 30 * 24 * 3600, json.dumps(visit_info))
        print(f"📅 Visita salva: {name} — {day} {period} ({visit_date})")
        # Google Agenda
        if visit_date:
            service = get_calendar_service()
            if service:
                period_label = f" ({period})" if period != 'desconhecido' else ''
                event = {
                    'summary': f'Visita — {name}{period_label}',
                    'location': 'Estrada dos Búzios (RJ-106), Bairro da Rasa',
                    'description': f'WhatsApp: +{phone}\nPeríodo: {period}',
                    'start': {'date': visit_date, 'timeZone': 'America/Sao_Paulo'},
                    'end':   {'date': visit_date, 'timeZone': 'America/Sao_Paulo'},
                }
                service.events().insert(calendarId=CALENDAR_ID, body=event).execute()
                print(f"📅 Google Calendar: evento criado para {name} em {visit_date}")
    except Exception as e:
        print(f"extract_and_save_visit error ({phone}): {e}")


def visit_reminder_sweep():
    """Roda diariamente às 8h: envia lembrete de visita de amanhã pro WhatsApp da Evelin."""
    r = get_redis()
    if not r:
        return
    try:
        import pytz
        from datetime import timedelta
        tz = pytz.timezone('America/Sao_Paulo')
        now = datetime.now(tz)
        if now.hour != 8:
            return
        tomorrow = (now.date() + timedelta(days=1)).isoformat()
        for key in r.scan_iter("visit:*"):
            phone_num = key.split("visit:", 1)[1]
            data_raw = r.get(key)
            if not data_raw:
                continue
            visit = json.loads(data_raw)
            if visit.get('date') == tomorrow:
                name = visit.get('name', 'Cliente')
                period = visit.get('period', '')
                period_txt = f" — {period}" if period not in ('desconhecido', '') else ''
                msg = (
                    f"🗓️ *Visita amanhã!*\n\n"
                    f"👤 {name}\n"
                    f"📱 +{phone_num}\n"
                    f"📅 {visit.get('day', '').capitalize()}{period_txt}\n"
                    f"📍 Praia Rasa de Búzios 2\n\n"
                    f"Confirme com o cliente antes de ir! 😊"
                )
                for alert_num in ALERT_NUMBERS:
                    send_message(alert_num, msg)
                print(f"🔔 Lembrete enviado: visita de {name} amanhã")
    except Exception as e:
        print(f"visit_reminder_sweep error: {e}")


def followup_sweep():
    """Roda de tempos em tempos: cutuca clientes que pararam de responder, em 3 estágios."""
    if not FOLLOWUP_ENABLED:
        return
    r = get_redis()
    if not r:
        return
    try:
        import pytz
        hora = datetime.now(pytz.timezone("America/Sao_Paulo")).hour
    except Exception:
        hora = 12
    # Não incomoda de madrugada
    if not (FOLLOWUP_DAY_START <= hora < FOLLOWUP_DAY_END):
        return
    now = time.time()
    try:
        for key in r.scan_iter("fu:*"):
            phone = key.split("fu:", 1)[1]
            if is_paused(phone):
                continue
            state = get_followup_state(phone)
            if not state:
                continue
            stage = state.get("stage", 0)
            if stage >= 3:
                continue
            silent_min = (now - state.get("last_client_ts", now)) / 60.0
            history = get_conversation(phone)
            # Só cutuca se o último a falar foi o BOT (cliente realmente não respondeu)
            if not history or history[-1].get("role") != "assistant":
                continue
            # Para se a visita já foi confirmada
            if is_visit_confirmed(history):
                extract_and_save_visit(phone, history)
                state["stage"] = 3  # encerra o ciclo
                set_followup_state(phone, state)
                continue
            next_stage = None
            if stage == 0 and silent_min >= FOLLOWUP_STAGE1_MIN:
                next_stage = 1
            elif stage == 1 and silent_min >= FOLLOWUP_STAGE2_MIN:
                next_stage = 2
            elif stage == 2 and silent_min >= FOLLOWUP_STAGE3_MIN and hora >= 18:
                next_stage = 3   # 3º toque só à noite
            if not next_stage:
                continue
            msg = generate_followup(phone, next_stage)
            if msg:
                if send_and_check(phone, msg):
                    append_message(phone, "assistant", msg)
                state["stage"] = next_stage
                set_followup_state(phone, state)
                print(f"📨 Follow-up estágio {next_stage} enviado para {phone}")
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
        if not message:
            return jsonify({'status': 'no_message'}), 200

        if message.get('isGroup', False):
            return jsonify({'status': 'group'}), 200

        if (message.get('isEdit') or message.get('updateType') or
                message.get('messageType', '') in ('messageUpdate', 'editedMessage')):
            return jsonify({'status': 'edit_ignored'}), 200

        from_me = message.get('fromMe', False)
        is_api  = message.get('wasSentByApi', False)

        convo = message.get('chatId', '') or message.get('sender_pn', '')
        phone = convo.replace('@s.whatsapp.net', '').replace('@c.us', '').replace('@lid', '')

        if not phone:
            return jsonify({'status': 'no_phone'}), 200

        msg_type   = message.get('type', '') or message.get('messageType', '')
        media_type = message.get('mediaType', '')

        # ───────────────────────────────────────────────────────────────────
        # 1) MENSAGEM ENVIADA PELO PRÓPRIO WHATSAPP (fromMe) — ANTES do dedup
        # ───────────────────────────────────────────────────────────────────
        if from_me:
            if is_api:
                return jsonify({'status': 'from_bot'}), 200

            # Número do cliente está no campo top-level 'chat'
            chat_data = data.get('chat', {})
            if isinstance(chat_data, dict):
                raw_client = (chat_data.get('id', '') or
                              chat_data.get('chatId', '') or
                              chat_data.get('phone', ''))
            elif isinstance(chat_data, str):
                raw_client = chat_data
            else:
                raw_client = ''
            pause_phone = raw_client.replace('@s.whatsapp.net', '').replace('@c.us', '').replace('@lid', '').replace('+', '') if raw_client else phone
            print(f"[MANUAL] Você digitou para {pause_phone}: '{extract_text(message).strip()[:40]}'")

            manual_text = extract_text(message).strip()
            if manual_text == RESUME_KEYWORD:
                clear_pause(pause_phone)
                return jsonify({'status': 'resumed'}), 200

            set_pause(pause_phone)
            if manual_text:
                append_message(pause_phone, "assistant", manual_text)
            return jsonify({'status': 'paused_human_takeover'}), 200

        # Proteção contra duplicatas (só para mensagens de clientes)
        if is_duplicate_msg(message):
            return jsonify({'status': 'duplicate'}), 200

        # Ignora echo do "." (palavra de retomada) que chega pelo Instance Webhook
        text_preview = extract_text(message).strip()
        if text_preview == RESUME_KEYWORD:
            return jsonify({'status': 'resume_echo_ignored'}), 200

        # ───────────────────────────────────────────────────────────────────
        # 2) MENSAGEM DO CLIENTE — se a conversa está pausada, NÃO responde
        # ───────────────────────────────────────────────────────────────────
        if is_paused(phone):
            txt = extract_text(message).strip()
            if txt:
                append_message(phone, "user", txt)  # guarda contexto, sem responder
            print(f"⏸️  {phone} está em atendimento humano — bot não respondeu.")
            return jsonify({'status': 'paused_no_reply'}), 200

        # ───────────────────────────────────────────────────────────────────
        # 3) FLUXO NORMAL DO BOT
        # ───────────────────────────────────────────────────────────────────
        # Cliente está ativo agora → zera o ciclo de follow-up
        set_followup_state(phone, {"last_client_ts": time.time(), "stage": 0})

        text = ""

        # Áudio
        is_audio = msg_type in ('audio', 'ptt', 'audioMessage', 'PTT')
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
                    send_message(phone, "Oi! 😊 Não consegui ouvir o áudio. Pode me mandar por texto que te respondo na hora!")
                    return jsonify({'status': 'ok'}), 200
            else:
                send_message(phone, "Oi! 😊 Não consegui ouvir o áudio. Pode me mandar por texto que te respondo na hora!")
                return jsonify({'status': 'ok'}), 200

        # Texto
        elif msg_type in ('text', 'Conversation', 'extendedTextMessage'):
            text = extract_text(message).strip()

        # Cliente enviou imagem/vídeo
        elif msg_type == 'media' and media_type in ('image', 'video', 'sticker', 'document'):
            reply, alert_flag, media_flag = get_ai_response(phone, "[cliente enviou uma imagem]")
            if reply is None:
                notify_ai_failure(phone)
                return jsonify({'status': 'ai_error'}), 200
            send_and_check(phone, reply)
            if alert_flag:
                send_alert(phone)
            if media_flag:
                threading.Thread(target=send_media_package, args=(phone,)).start()
            return jsonify({'status': 'ok'}), 200
        else:
            print(f"Skipping type: {msg_type}")
            return jsonify({'status': 'not_supported'}), 200

        if not text:
            return jsonify({'status': 'no_text'}), 200

        print(f"phone='{phone}', text='{text[:80]}'")

        reply, alert_flag, media_flag = get_ai_response(phone, text)
        if reply is None:
            notify_ai_failure(phone)
            return jsonify({'status': 'ai_error'}), 200

        # Rede de segurança: se o modelo não emitiu a tag mas o cliente claramente
        # aceitou ver mídia logo após você oferecer.
        if not media_flag:
            media_keywords = ['quero ver', 'queria ver', 'pode mandar', 'manda sim',
                              'com certeza', 'claro que sim', 'quero as fotos',
                              'foto', 'fotos', 'video', 'vídeo', 'videos', 'vídeos']
            history = get_conversation(phone)
            last_bot = next((m['content'] for m in reversed(history[:-1])
                             if m['role'] == 'assistant'), '')
            if (any(kw in text.lower() for kw in media_keywords) and
                    any(kw in last_bot.lower() for kw in ['foto', 'vídeo', 'video', 'imagens', 'mandar'])):
                media_flag = True

        print(f"Sending reply: {reply[:80]}")
        send_and_check(phone, reply)

        if alert_flag:
            send_alert(phone)

        if media_flag:
            threading.Thread(target=send_media_package, args=(phone,)).start()

        # Detecção imediata de visita confirmada → agenda no Google Calendar
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
recovery_index = 0


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
    contact = recovery_contacts[recovery_index]
    phone = contact.get('telefone', '').replace(' ', '').replace('-', '').replace('(', '').replace(')', '')
    name  = contact.get('nome', '')
    custom_msg = contact.get('mensagem', '')
    if not phone:
        recovery_index += 1
        return
    # Não dispara recovery em quem está em atendimento humano
    if is_paused(phone):
        recovery_index += 1
        return
    message = custom_msg or f"Oi{' ' + name if name else ''}! Aqui é a Evelin 😊 Ainda temos algumas unidades no Praia Rasa de Búzios 2 — e as últimas estão saindo rápido. Você ainda tem interesse? Me avisa antes de visitar que garanto seu atendimento!"
    send_message(phone, message)
    recovery_index += 1


# ─── ROTAS ────────────────────────────────────────────────────────────────────

@app.route('/health', methods=['GET'])
def health():
    r = get_redis()
    redis_ok = False
    if r:
        try:
            r.ping()
            redis_ok = True
        except Exception:
            redis_ok = False
    return jsonify({
        'status': 'running',
        'redis': 'ok' if redis_ok else 'OFFLINE',
        'memory_enabled': redis_ok,
        'timestamp': datetime.now().isoformat()
    }), 200


@app.route('/pause/<path:phone>', methods=['GET'])
def pause_toggle(phone):
    """URL de pausa manual — salve nos favoritos do celular.
    Use: /pause/NUMERO?key=SUA_CHAVE  (configure ADMIN_KEY no Railway)"""
    key = request.args.get('key', '')
    admin_key = os.environ.get('ADMIN_KEY', '')
    if not admin_key or key != admin_key:
        return 'Chave incorreta.', 403
    phone_clean = phone.replace('+', '').replace('-', '').replace(' ', '')
    if is_paused(phone_clean):
        clear_pause(phone_clean)
        return f'▶️ Bot RETOMADO para {phone_clean}. Ele voltará a responder normalmente.', 200
    else:
        set_pause(phone_clean)
        return f'⏸️ Bot PAUSADO para {phone_clean}. Ele ficará mudo por 12h (ou até você acessar esta URL de novo).', 200


@app.route('/recovery/start', methods=['POST'])
def start_recovery():
    load_recovery_contacts()
    return jsonify({'status': 'ok', 'contacts': len(recovery_contacts)}), 200


# ─── INICIALIZAÇÃO ────────────────────────────────────────────────────────────

if __name__ == '__main__':
    get_redis()  # conecta e loga o estado da memória logo no start
    scheduler = BackgroundScheduler()
    scheduler.add_job(send_recovery_message, 'interval', hours=RECOVERY_INTERVAL_HOURS)
    scheduler.add_job(followup_sweep, 'interval', minutes=FOLLOWUP_CHECK_MIN)
    scheduler.add_job(visit_reminder_sweep, 'interval', minutes=30)  # verifica a cada 30min, age só às 8h
    scheduler.start()
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port, debug=False)
