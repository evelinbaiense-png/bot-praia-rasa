import os
import json
import time
import re
import threading
from datetime import datetime, timedelta
from flask import Flask, request, jsonify
import anthropic
import requests
from google.oauth2 import service_account
from googleapiclient.discovery import build

app = Flask(__name__)

# ─── Configuração ────────────────────────────────────────────────────────────
ANTHROPIC_API_KEY       = os.environ.get("ANTHROPIC_API_KEY")
UAZAPI_TOKEN            = os.environ.get("UAZAPI_TOKEN")
UAZAPI_URL              = os.environ.get("UAZAPI_URL")
GOOGLE_CREDENTIALS_JSON = os.environ.get("GOOGLE_CREDENTIALS_JSON")
CALENDAR_ID             = "evelinbaiense@gmail.com"
HUMAN_PAUSE_MINUTOS     = 30
REATIVACAO_HORAS        = 3   # horas sem resposta para reativar

# ─── Mídias ──────────────────────────────────────────────────────────────────
FOTOS = [
    "https://res.cloudinary.com/dd6o3z4ma/image/upload/v1779039971/WhatsApp_Image_2026-05-17_at_13.23.56_itxlrx.jpg",
    "https://res.cloudinary.com/dd6o3z4ma/image/upload/v1779039971/WhatsApp_Image_2026-05-17_at_13.23.57_wmlvhl.jpg",
    "https://res.cloudinary.com/dd6o3z4ma/image/upload/v1779039971/WhatsApp_Image_2026-05-17_at_13.23.35_eioep1.jpg",
    "https://res.cloudinary.com/dd6o3z4ma/image/upload/v1779039971/WhatsApp_Image_2026-05-17_at_13.23.57_1_mszdep.jpg",
    "https://res.cloudinary.com/dd6o3z4ma/image/upload/v1779039971/WhatsApp_Image_2026-05-17_at_13.23.35_1_kidkrk.jpg",
    "https://res.cloudinary.com/dd6o3z4ma/image/upload/v1779039971/WhatsApp_Image_2026-05-17_at_13.23.34_cxhs57.jpg",
    "https://res.cloudinary.com/dd6o3z4ma/image/upload/v1779040450/Editedimage_1776197718307_ms669e.png",
]
VIDEO_1 = "https://res.cloudinary.com/dd6o3z4ma/video/upload/v1779039974/WhatsApp_Video_2026-05-17_at_13.15.06_zrc6fs.mp4"
VIDEO_2 = "https://res.cloudinary.com/dd6o3z4ma/video/upload/v1779039973/WhatsApp_Video_2026-05-17_at_13.19.32_b5ptia.mp4"

# ─── Memória ─────────────────────────────────────────────────────────────────
conversas     = {}   # {telefone: [{"role": ..., "content": ...}]}
humano_ativo  = {}   # {telefone: timestamp}
ultima_msg    = {}   # {telefone: timestamp} para reativação
midia_enviada = {}   # {telefone: bool}

client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

# ─── Prompt ──────────────────────────────────────────────────────────────────
SYSTEM_PROMPT = """Você é Evelin Abreu, corretora de imóveis do Praia Rasa de Búzios 2.
Atenda como a própria Evelin no WhatsApp — humana, próxima, animada com o empreendimento.

IDIOMA: Responda SEMPRE em português do Brasil, independente do idioma do cliente.

TOM E COMPORTAMENTO:
- Espelhe o cliente. Se ele for informal, relaxe. Se for formal, mantenha.
- Respostas curtas. Máximo 2-3 frases. Nunca textão.
- Uma pergunta por vez. NUNCA faça duas perguntas na mesma mensagem.
- Nunca revele que é IA. Se perguntarem, diga que é a própria Evelin.
- Não invente informações. Se não souber, convide para visitar.
- Seja entusiasmada com o empreendimento — é genuíno, você acredita nele.

ERROS DE DIGITAÇÃO E INTERPRETAÇÃO:
- O cliente pode escrever errado, com gírias, abreviações ou frases incompletas. NUNCA comente o erro.
- Interprete sempre pela intenção mais provável. Exemplos: 'ou' = 'oi', 'valer' = 'valor', 'preco' = 'preço', 'qnt' = 'quanto', 'td' = 'tudo', 'blz' = 'beleza'.
- Se não entender, faça UMA pergunta curta e simpática para esclarecer, sem mencionar o erro.
- NUNCA diga 'sua mensagem saiu incompleta' ou qualquer variação. Isso é rude e constrangedor.

PEDIDO DE NOME:
- Peça em mensagem SEPARADA, sozinha, sem nenhuma outra pergunta.
- Exemplo CORRETO: "Qual é o seu nome?" — só isso.
- Exemplo ERRADO: "300m² ou 600m²? E seu nome?" — PROIBIDO.

FLUXO DA CONVERSA:
1. Na PRIMEIRA mensagem: responda o que o cliente perguntou E faça a pergunta de interesse.
   - Se ele não disse o interesse: "Você está buscando para morar, veraneio ou investimento?"
   - Se ele já disse o interesse: pule essa pergunta e vá para o passo 2.
2. Na SEGUNDA mensagem: ofereça as mídias.
   - "Que legal! Posso te mandar uns vídeos e fotos do empreendimento pra você já ter uma ideia? 😊"
   - Se cliente não deu interesse claro: "Posso mandar uns vídeos e fotos? Fica mais fácil de visualizar 😊"
3. Quando o cliente aceitar as mídias: inclua [ENVIAR_MIDIA] no final da resposta.
4. Após as mídias serem enviadas, o bot automaticamente pergunta sobre região — aguarde essa resposta.
5. Quando o cliente responder sobre a região: dê os valores PROATIVAMENTE na mesma mensagem.
   - Use: "Os lotes de 300m² saem a partir de R$899/mês em até 156x, com entrada de R$7.000 — financiamento direto, sem banco e sem SPC 😊 Você prefere 300m² ou 600m²?"
6. Só depois de dar valores e tirar dúvidas, conduza para o agendamento.

SEQUÊNCIA PÓS-MÍDIA (siga essa ordem):
a) Cliente responde sobre região/moradia
b) Você dá os valores proativamente (300m² e 600m² com parcelas e entrada)
c) Pergunta se prefere 300m² ou 600m²
d) Tira dúvidas sobre valores, financiamento, infraestrutura
e) Só então convida para visita
5. Use o script do plantão UMA VEZ quando o interesse for evidente.
6. Só conduza para visita DEPOIS de ter dado informações suficientes e criado interesse real.

REGRA DE OURO — INFORMAR ANTES DE VENDER:
- Quando o cliente disser "quero saber mais", "me conta mais", "como é?" → DÊ INFORMAÇÕES. Não peça o nome, não redirecione para visita.
- Quando o cliente disser "não sei" sobre tamanho/interesse → apresente as opções com informação: "O de 300m² sai a partir de R$899/mês e o de 600m² a partir de R$1.599/mês. Qual se encaixa melhor no seu bolso?"
- Quando o cliente hesitar em visitar → dê mais uma informação relevante antes de insistir na visita.
- NÃO repita o convite para visita mais de 2 vezes seguidas sem dar nova informação no meio.
- A visita é consequência do interesse — crie o interesse primeiro.

SCRIPT DO PLANTÃO — usar UMA VEZ:
"[Nome], deixa eu te contar uma coisa 😊 Trabalho por comissão e meu plantão é por escala — se você for lá sem agendar comigo, outro corretor te atende e eu perco essa venda. Me avisa antes, atendo qualquer dia e horário, sem compromisso!"

INFORMAÇÕES DO EMPREENDIMENTO:
Localização: Estrada dos Búzios (RJ-106), Bairro da Rasa — entre a Praia Rasa e Búzios.
- 3 minutos da Praia Rasa | Geribá a 8km | Região de kitesurf
- Maps: https://www.google.com/maps/@-22.7238716,-42.001362,493m
- Após dar o link: "Você conseguiu ver? Fica entre a Praia Rasa e Búzios — localização bem estratégica 😊"

Infraestrutura: fechado, murado, portão, posteamento elétrico feito, água em breve, playground, praça, bosque, área verde, futura associação de moradores, taxa 10% salário mínimo (só após entrega).

LOTES E VALORES (nunca altere):
300m² (10x30 ou 7,5x40): R$899/mês em até 156x, entrada R$7.000
Vista mar 300m²: a partir de R$1.199/mês em até 156x
600m²: R$1.599/mês em até 156x, entrada R$14.000
Vista mar 600m²: a partir de R$1.999/mês em até 156x
Todos com reajuste anual pelo IGPM.
À vista 300m²: R$90.000 | À vista 600m²: R$160.000 (só se perguntarem)

FINANCIAMENTO:
- Direto pela incorporadora, sem banco, sem SPC/Serasa
- Planos de 12 a 156x, juros de acordo com o plano + IGPM anual
- Entrada em 3x sem juros (ato/30/60 dias) ou 10x no cartão (juros do cartão)
- Pode construir com 3 parcelas pagas, com autorização
- Primeira parcela em até 45 dias

DOCUMENTAÇÃO: RGI em processo na prefeitura. Após liberação, quem estiver quitado pode transferir — opcional, por conta do comprador.

OBJEÇÕES:
"Tá longe": "Na verdade fica 3 minutos da praia pela RJ-106. Você está em qual região?"
"Tá caro": "O parcelamento começa em R$899/mês direto com a incorporadora, sem banco e sem SPC. Prefere 300m² ou 600m²?"
"Tem juros?": "Tem juros de acordo com o plano (12 a 156x) e reajuste anual do IGPM. A entrada em 3x é sem juros."
"Mora perto?": "Você está na região de Búzios? Porque o empreendimento fica na Estrada dos Búzios — quase certinho que você passou na frente! 😊 Quer visitar essa semana?"

ATENÇÃO — DISTINÇÃO IMPORTANTE:
- "Estou visitando a região" → pergunte se consegue passar hoje.
- Se CONSEGUE passar hoje → dê o endereço e agende.
- Se NÃO CONSEGUE (está saindo, ocupado, longe) → ofereça as mídias imediatamente: "Sem problema! Posso te mandar uns vídeos e fotos pra você ver com calma? 😊" e inclua [ENVIAR_MIDIA] quando aceitar.
- "Quero visitar o empreendimento" = cliente quer agendar → confirma dia e horário.
- O fluxo SEMPRE passa por: interesse → mídias → nome → detalhes → agendamento. Não pule nenhuma etapa.
- NUNCA seja passivo dizendo só "quando você voltar me avisa" sem oferecer as mídias antes.

GATILHOS (usar naturalmente):
- "Imagina escapar todo final de semana com a praia a 3 minutos, sem depender de hotel."
- "Quem reserva agora ainda escolhe o lote. As unidades estão saindo."
- "Não precisa decidir nada na hora — vem conhecer e vê se faz sentido."

REPETIÇÃO DE VALORES:
Se o cliente perguntar o mesmo valor de novo, responda em UMA frase curta e já direcione para a visita.
Exemplo: "É R$899/mês com entrada de R$7.000 😊 Você consegue vir conhecer pessoalmente?"

AGENDAMENTO:
- Quando o cliente disser "domingo que vem", "sábado", ou qualquer dia já citado, NÃO pergunte a data de novo.
- Confirme o dia e pergunte só o horário se necessário.
- Correto: "Domingo que vem está ótimo! Qual horário você prefere?"
- PROIBIDO: "Que dia é domingo que vem? (DD/MM)"
- "As visitas são de domingo a domingo 😊 Qual é o melhor dia e horário pra você?"
Quando confirmar dia e hora, inclua no final: {"agendar": true, "nome": "NOME", "data": "DD/MM/YYYY", "hora": "HH:MM"}
"""


# ─── Google Calendar ─────────────────────────────────────────────────────────
def get_calendar_service():
    credentials_info = json.loads(GOOGLE_CREDENTIALS_JSON)
    credentials = service_account.Credentials.from_service_account_info(
        credentials_info, scopes=["https://www.googleapis.com/auth/calendar"]
    )
    return build("calendar", "v3", credentials=credentials)

def criar_evento(nome, telefone, data_str, hora_str):
    try:
        service = get_calendar_service()
        dt = datetime.strptime(f"{data_str} {hora_str}", "%d/%m/%Y %H:%M")
        evento = {
            "summary": f"Visita Praia Rasa — {nome}",
            "description": f"Cliente: {nome}\nWhatsApp: {telefone}",
            "location": "Estrada dos Búzios (RJ-106), Bairro da Rasa",
            "start": {"dateTime": dt.strftime("%Y-%m-%dT%H:%M:%S"), "timeZone": "America/Sao_Paulo"},
            "end":   {"dateTime": (dt + timedelta(hours=2)).strftime("%Y-%m-%dT%H:%M:%S"), "timeZone": "America/Sao_Paulo"},
        }
        service.events().insert(calendarId=CALENDAR_ID, body=evento).execute()
        print(f"[AGENDA] {nome} em {data_str} às {hora_str}")
    except Exception as e:
        print(f"[AGENDA ERRO] {e}")


# ─── UAZAPI — envio de mensagens ──────────────────────────────────────────────
def _post(endpoint, payload):
    headers = {"Content-Type": "application/json", "token": UAZAPI_TOKEN}
    try:
        r = requests.post(f"{UAZAPI_URL}{endpoint}", json=payload, headers=headers, timeout=15)
        print(f"[{endpoint}] {payload.get('number','?')} → {r.status_code}")
        return r
    except Exception as e:
        print(f"[ERRO {endpoint}] {e}")

def enviar_texto(telefone, texto):
    texto = re.sub(r'\{[^{}]*"agendar"[^{}]*\}', '', texto).strip()
    if texto:
        _post("/send/text", {"number": telefone, "text": texto})

def enviar_imagem(telefone, url, caption=""):
    payload = {"number": telefone, "type": "image", "file": url}
    if caption:
        payload["caption"] = caption
    _post("/send/media", payload)

def enviar_video(telefone, url, caption=""):
    payload = {"number": telefone, "type": "video", "file": url}
    if caption:
        payload["caption"] = caption
    _post("/send/media", payload)

def enviar_midias(telefone):
    if midia_enviada.get(telefone):
        return
    midia_enviada[telefone] = True
    def _enviar():
        enviar_texto(telefone, "Olha só os vídeos do empreendimento 👇")
        time.sleep(1)
        enviar_video(telefone, VIDEO_1)
        time.sleep(3)
        enviar_video(telefone, VIDEO_2)
        time.sleep(3)
        enviar_texto(telefone, "E aqui algumas fotos 📍")
        for foto in FOTOS:
            enviar_imagem(telefone, foto)
            time.sleep(1)
        time.sleep(2)
        enviar_texto(telefone, "O que achou? 😊 Os lotes de 300m² saem a partir de R$899/mês em até 156x, com entrada de R$7.000 — tudo direto com a incorporadora, sem banco e sem SPC. Você prefere 300m² ou 600m²?\n\nVocê mora aqui na região ou estava visitando?")
    threading.Thread(target=_enviar, daemon=True).start()


# ─── Claude com memória ───────────────────────────────────────────────────────
def resposta_bot(telefone, mensagem, novo=False):
    if telefone not in conversas:
        conversas[telefone] = []

    # Se for nova conversa, instrui Claude a incluir a saudação na resposta
    system = SYSTEM_PROMPT
    if novo:
        system += "\n\nIMPORTANTE: Esta é a PRIMEIRA mensagem do cliente. Comece sua resposta com a saudação: 'Olá, tudo bem? Eu sou Evelin Abreu, corretora de imóveis 😊' e em seguida responda naturalmente o que ele perguntou ou pediu."

    conversas[telefone].append({"role": "user", "content": mensagem})
    historico = conversas[telefone][-20:]

    resposta = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=500,
        system=system,
        messages=historico
    )
    texto = resposta.content[0].text
    conversas[telefone].append({"role": "assistant", "content": texto})
    return texto


# ─── Reativação ───────────────────────────────────────────────────────────────
MSGS_REATIVACAO = [
    "Oi! 😊 Ainda temos algumas unidades disponíveis no Praia Rasa de Búzios 2. Posso te ajudar com mais alguma informação?",
    "Oi! Que tal dar uma passadinha para conhecer pessoalmente? As visitas são de domingo a domingo, qualquer horário 😊",
    "Oi! Os lotes estão saindo — quem agenda logo ainda tem escolha. Quer marcar uma visita rápida? 😊",
]
reativacao_indice = {}

def verificar_reativacao():
    while True:
        time.sleep(1800)  # verifica a cada 30 min
        agora = time.time()
        for tel, ts in list(ultima_msg.items()):
            if tel in humano_ativo:
                continue
            if (agora - ts) >= REATIVACAO_HORAS * 3600:
                idx = reativacao_indice.get(tel, 0)
                if idx < len(MSGS_REATIVACAO):
                    enviar_texto(tel, MSGS_REATIVACAO[idx])
                    reativacao_indice[tel] = idx + 1
                    ultima_msg[tel] = agora  # reseta o timer

threading.Thread(target=verificar_reativacao, daemon=True).start()


# ─── Webhook ──────────────────────────────────────────────────────────────────
@app.route("/webhook", methods=["POST"])
def webhook():
    data = request.json or {}
    print(f"[WEBHOOK] {json.dumps(data)[:200]}")

    try:
        msg      = data.get("message", data)
        chat_obj = data.get("chat", {})

        raw_id = msg.get("chatId") or data.get("chatId") or ""
        # Se for @lid (dispositivo vinculado), usa sender_pn que tem o número real
        if "@lid" in str(raw_id) or not raw_id:
            chat_id = msg.get("sender_pn") or msg.get("sender") or data.get("sender_pn") or data.get("sender") or raw_id
        else:
            chat_id = raw_id

        from_me          = data.get("fromMe", msg.get("fromMe", False))
        was_by_api       = data.get("wasSentByApi", msg.get("wasSentByApi", False))
        is_group         = data.get("isGroup", msg.get("isGroup", "@g.us" in str(chat_id)))
        chatbot_disabled = int(chat_obj.get("chatbot_disabled", 0))
        texto            = (data.get("text") or msg.get("text") or data.get("content") or msg.get("content") or msg.get("conversation") or "").strip()

        telefone = str(chat_id).replace("@s.whatsapp.net","").replace("@c.us","").replace("@g.us","").replace("@lid","")

        print(f"[MSG] tel={telefone} from_me={from_me} api={was_by_api} grupo={is_group} chatbot_disabled={chatbot_disabled} texto='{texto}'")

        if is_group:
            return jsonify({"status": "grupo"}), 200
        if not telefone or len(telefone) < 8:
            return jsonify({"status": "sem_tel"}), 200

        # UAZAPI desativou o chatbot para esse chat (Evelin assumiu pelo Multiatendimento)
        if chatbot_disabled:
            humano_ativo[telefone] = time.time()
            print(f"[HUMANO] chatbot_disabled=1 para {telefone}")
            return jsonify({"status": "chatbot_disabled"}), 200

        # Mensagem manual da Evelin
        if from_me and not was_by_api:
            cmd = texto.strip().upper()
            if cmd in ["RETOMAR", "Retomar", "VOLTAR", "Voltar", "retomar", "voltar"]:
                # Evelin reativa o bot digitando RETOMAR
                if telefone in humano_ativo:
                    del humano_ativo[telefone]
                print(f"[RETOMAR] Bot reativado para {telefone}")
            else:
                # Qualquer outra mensagem manual pausa o bot
                humano_ativo[telefone] = time.time()
                print(f"[HUMANO] Evelin assumiu {telefone}")
            return jsonify({"status": "humano"}), 200

        # Mensagem do bot → ignora
        if from_me and was_by_api:
            return jsonify({"status": "bot_ignorado"}), 200

        # Verifica pausa por humano — sem expiração, só volta com RETOMAR
        if telefone in humano_ativo:
            return jsonify({"status": "pausado"}), 200

        if not texto:
            return jsonify({"status": "sem_texto"}), 200

        # Atualiza timestamp para reativação
        ultima_msg[telefone] = time.time()
        if telefone in reativacao_indice:
            reativacao_indice[telefone] = 0  # reseta ao responder

        # Gera resposta
        novo = telefone not in conversas
        resposta = resposta_bot(telefone, texto, novo=novo)

        # Verifica se deve enviar mídias
        if "[ENVIAR_MIDIA]" in resposta:
            resposta = resposta.replace("[ENVIAR_MIDIA]", "").strip()
            enviar_texto(telefone, resposta)
            time.sleep(1)
            enviar_midias(telefone)
        else:
            enviar_texto(telefone, resposta)

        # Verifica agendamento
        match = re.search(r'\{[^{}]*"agendar"[^{}]*\}', resposta)
        if match:
            try:
                ag = json.loads(match.group())
                if ag.get("agendar"):
                    criar_evento(ag.get("nome","Cliente"), telefone, ag.get("data",""), ag.get("hora",""))
            except:
                pass

    except Exception as e:
        print(f"[ERRO] {e}")

    return jsonify({"status": "ok"}), 200


@app.route("/health", methods=["GET"])
def health():
    return jsonify({"status": "online", "bot": "Praia Rasa de Búzios 2"}), 200


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port)
