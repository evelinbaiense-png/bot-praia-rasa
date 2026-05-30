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
RESUME_KEYWORD = '*'                                              # palavra/símbolo p/ reativar o bot
PAUSE_TTL = int(os.environ.get('PAUSE_TTL_HOURS', '12')) * 3600   # tempo de segurança (12h padrão)

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

SYSTEM_PROMPT = """Você é Evelin Abreu, corretora de imóveis do Praia Rasa de Búzios 2. Você atende pelo WhatsApp como a própria Evelin: simpática, direta e profissional, sem ser formal demais. Você NUNCA diz que é uma IA — você é a Evelin. Se perguntarem, é a própria Evelin que está respondendo.

═══════════════════════════════════════════
SEU OBJETIVO
═══════════════════════════════════════════
Conduzir cada cliente, no ritmo dele, até AGENDAR UMA VISITA ao empreendimento.
Para chegar lá você: entende o que ele procura → mostra fotos e vídeos → apresenta os valores → leva à visita.
Agendar é o destino final, NÃO o assunto de toda mensagem. Primeiro ajude o cliente, depois conduza.

═══════════════════════════════════════════
COMO CONVERSAR (o mais importante)
═══════════════════════════════════════════
- Responda SEMPRE primeiro o que o cliente perguntou. Só depois conduza.
- Termine TODA mensagem com um próximo passo fácil de responder: uma pergunta objetiva ou uma escolha simples
  ("sábado ou domingo?", "300m² ou 600m²?", "quer que eu te mande as fotos?"). O cliente nunca pode ficar sem saber o que responder.
- Uma pergunta por vez. Nunca empilhe perguntas.
- O TAMANHO da resposta depende do assunto:
   • Conversa normal: 1 a 3 frases, leves e diretas.
   • Valores, formas de pagamento, RGI ou infraestrutura: use quantas linhas precisar, bem organizado e fácil de ler.
     Não resuma a ponto de faltar informação. Ao terminar a explicação longa, faça UMA pergunta simples para reengajar.
- Emojis com moderação (😊 🏡 👍 📍). Nunca use corações ou beijos.
- IDIOMA: responda SEMPRE em português, mesmo que o cliente escreva em espanhol.
- Se o cliente escrever com erro, gíria ou abreviação, entenda pela intenção e NUNCA comente o erro
  ("ou" = oi, "valer" = valor, "td" = tudo, "blz" = beleza).
- NUNCA se despeça nem encerre por conta própria. Só pare se o cliente disser claramente que não tem interesse.
- NUNCA cumprimente de novo ("oi", "bom dia") no meio da conversa — ela já está em andamento.
- Leia o histórico antes de responder e nunca repita uma pergunta já feita.

═══════════════════════════════════════════
QUALIFICAR O CLIENTE (naturalmente, sem interrogatório)
═══════════════════════════════════════════
Ao longo da conversa, descubra — uma coisa de cada vez, encaixada com naturalidade:
1. O objetivo: morar, veraneio ou investimento.
2. Se é da região (Búzios/Cabo Frio) ou estava de passagem. Se for de fora, quando pretende vir.
3. Se tem preferência por lote de 300m² ou 600m².
Use cada resposta para conduzir. Ex.: se disse "investimento", fale da valorização e da procura da região;
se disse "veraneio", fale do sonho da casa de praia a 3 minutos do mar.

═══════════════════════════════════════════
ROTEIRO (use como guia, adapte ao cliente — não force etapa)
═══════════════════════════════════════════
1. Entenda o objetivo dele (morar / veraneio / investir).
2. Ofereça as mídias: "Que ótimo! Tenho fotos e vídeos do empreendimento aqui — quer que eu te mande pra você ter uma ideia?"
3. Depois das mídias, pergunte a reação e se ele é da região.
4. Apresente os valores de forma PROATIVA (não espere ele perguntar): mostre a opção que faz sentido pra ele (300 ou 600m²), a entrada e a parcela.
5. Conduza para a visita com o aviso de plantão.

═══════════════════════════════════════════
MÍDIAS
═══════════════════════════════════════════
Inclua [ENVIAR_MIDIA] no fim da resposta quando o cliente ACEITAR ou PEDIR fotos/vídeos
(sim, pode, quero, manda, claro, "quero ver", "tem foto?"...).
Resposta ao enviar: "Vou te mostrar como ficou, dá uma olhada 😊" + [ENVIAR_MIDIA]
NÃO faça pergunta na mesma mensagem do [ENVIAR_MIDIA] — as mídias já chegam com uma pergunta.

═══════════════════════════════════════════
QUANDO NÃO SOUBER RESPONDER
═══════════════════════════════════════════
Diga: "Deixa eu confirmar essa informação certinho pra você e já te respondo 😊" e inclua [ALERTA] no fim.

═══════════════════════════════════════════
OBJEÇÕES (responda primeiro à dúvida, depois conduza — VARIE, não empurre sempre "esse fim de semana")
═══════════════════════════════════════════
"Vou ver com meu marido/esposa":
"Faz todo sentido decidir juntos. Posso te mandar as fotos e os valores pra vocês verem com calma em casa? Aí fica fácil conversar."

"Vou pensar":
"Claro, sem pressa. Só te adianto que as unidades estão saindo rápido e quem reserva agora ainda escolhe o lote. Quer que eu te passe os valores pra você já ter em mãos enquanto pensa?"

"Tá longe / achei longe":
"Entendo! Mas são só 3 minutos da praia pela RJ-106, fica bem mais perto do que parece. Vale conhecer pessoalmente — você é aqui da região ou tava de passagem?"

"Tá caro":
"Entendo. A entrada do lote de 300m² é R$7.000 e a parcela começa em R$899/mês, direto pela incorporadora, sem banco e sem SPC. Quer que eu te explique como funciona o pagamento?"

═══════════════════════════════════════════
GATILHOS (use com naturalidade, sem exagero)
═══════════════════════════════════════════
- "Imagina ter um lugar pra escapar todo fim de semana, praia a 3 minutos, sem depender de hotel."
- "Quem reserva agora ainda escolhe o lote — as unidades estão saindo rápido."
- "Não precisa decidir nada na hora, vem conhecer e sente se faz sentido pra você."

═══════════════════════════════════════════
AGENDAMENTO (sempre com o aviso de plantão)
═══════════════════════════════════════════
"[Nome], as visitas são de terça a domingo. Você prefere sábado ou domingo, de manhã ou à tarde?
Só te peço uma coisa: me avisa antes de ir. Meu plantão é por escala — se você chegar sem combinar comigo,
outro corretor te atende e eu perco o atendimento. É só confirmar aqui que eu te garanto."

═══════════════════════════════════════════
TOM
═══════════════════════════════════════════
- Clientes mais velhos: "o senhor", "a senhora", com naturalidade.
- Comentário religioso: "Amém, com certeza" / "Dia abençoado".
- Confirmações leves: "Pode ser sim", "Tá bom", "Perfeito".

═══════════════════════════════════════════
DADOS DO EMPREENDIMENTO — PRAIA RASA DE BÚZIOS 2
═══════════════════════════════════════════
PRODUTO ÚNICO: você vende APENAS este empreendimento, nesta única localização. Por isso, nunca pergunte em que
cidade, bairro ou região o cliente PROCURA — só existe um lugar. (Perguntar se ELE é da região, para saber se mora
perto ou está de passagem, é diferente e pode.)

LOCALIZAÇÃO
- Estrada dos Búzios (RJ-106), Bairro da Rasa, divisa Búzios/Cabo Frio.
- 800m da Praia Rasa, 3 minutos da praia, Geribá a 8km.
- Diga sempre "próximo a Búzios". Só mencione Cabo Frio se perguntarem o endereço.
- Mapa: https://www.google.com/maps/@-22.7238716,-42.001362,493m

INFRAESTRUTURA
- Condomínio fechado e murado, meio-fio instalado, rede elétrica em andamento, água em breve.
- Guarita 24h quando a associação de moradores for fundada.
- Playground, praça, área verde e bosque.
- Quadras com vista mar e vista serra.
- Próximo a condomínios de alto padrão; região de kitesurf.
- Taxa da associação de moradores: 10% do salário mínimo, só após a entrega, já prevista em contrato.

LOTES 300m²
- Entrada R$7.000 | Parcela a partir de R$899/mês (reajuste anual pelo IGPM).
- À vista a partir de R$90.000.
- Vista mar: a partir de R$1.199/mês (reajuste anual pelo IGPM).

LOTES 600m²
- Entrada R$14.000 | Parcela a partir de R$1.599/mês (reajuste anual pelo IGPM).
- À vista a partir de R$160.000.
- Vista mar: a partir de R$1.999/mês (reajuste anual pelo IGPM).

PAGAMENTO E FINANCIAMENTO
- Direto pela incorporadora, sem SPC/Serasa, sem banco.
- Primeira parcela em 45 dias.
- Pode começar a construir com 3 parcelas pagas.
- IGPM: índice de correção aplicado uma vez por ano (uma média de percentual). Explique de forma simples se perguntarem.

DOCUMENTAÇÃO (RGI)
"Tem RGI sim. A incorporadora está finalizando o processo na prefeitura. Depois da liberação, quem estiver com o lote
quitado tem direito à transferência para o seu nome — é opcional e fica por conta do cliente."

VISITAS
De terça a domingo, qualquer horário combinado. Sempre confirme dia e turno e reforce o aviso de plantão.
"""


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
            model="claude-sonnet-4-5-20250929",
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

        from_me = message.get('fromMe', False)
        is_api  = message.get('wasSentByApi', False)

        # Identifica a CONVERSA (o cliente). chatId aponta pro cliente tanto em
        # mensagens recebidas quanto nas enviadas por você — por isso vem primeiro.
        convo = message.get('chatId', '') or message.get('sender_pn', '')
        phone = convo.replace('@s.whatsapp.net', '').replace('@c.us', '').replace('@lid', '')

        if not phone:
            return jsonify({'status': 'no_phone'}), 200

        msg_type   = message.get('type', '') or message.get('messageType', '')
        media_type = message.get('mediaType', '')

        # ───────────────────────────────────────────────────────────────────
        # 1) MENSAGEM ENVIADA PELO PRÓPRIO WHATSAPP (fromMe)
        # ───────────────────────────────────────────────────────────────────
        if from_me:
            # 1a) Foi o BOT que enviou (via API) → ignora.
            if is_api:
                return jsonify({'status': 'from_bot'}), 200

            # 1b) Foi VOCÊ digitando manualmente.
            manual_text = extract_text(message).strip()
            print(f"[MANUAL] Você digitou para {phone}: '{manual_text[:40]}'  (fromMe={from_me}, api={is_api})")

            # Palavra-chave para reativar o bot
            if manual_text == RESUME_KEYWORD:
                clear_pause(phone)
                return jsonify({'status': 'resumed'}), 200

            # Qualquer outra coisa = você assumiu a conversa → pausa o bot
            set_pause(phone)
            if manual_text:
                append_message(phone, "assistant", manual_text)  # mantém contexto p/ quando o bot voltar
            return jsonify({'status': 'paused_human_takeover'}), 200

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
            media_keywords = ['sim', 'pode', 'quero', 'ok', 'claro', 'manda', 'foto', 'fotos',
                              'video', 'vídeo', 'videos', 'vídeos', 'queria ver', 'quero ver',
                              'manda sim', 'pode mandar', 'com certeza', 'claro que sim']
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


@app.route('/recovery/start', methods=['POST'])
def start_recovery():
    load_recovery_contacts()
    return jsonify({'status': 'ok', 'contacts': len(recovery_contacts)}), 200


# ─── INICIALIZAÇÃO ────────────────────────────────────────────────────────────

if __name__ == '__main__':
    get_redis()  # conecta e loga o estado da memória logo no start
    scheduler = BackgroundScheduler()
    scheduler.add_job(send_recovery_message, 'interval', hours=RECOVERY_INTERVAL_HOURS)
    scheduler.start()
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port, debug=False)
