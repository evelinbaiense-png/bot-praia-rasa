import os
import json
import time
import re
from datetime import datetime, timedelta
from flask import Flask, request, jsonify
import anthropic
import requests
from google.oauth2 import service_account
from googleapiclient.discovery import build

app = Flask(__name__)

# ─── Configuração ───────────────────────────────────────────────────────────
ANTHROPIC_API_KEY       = os.environ.get("ANTHROPIC_API_KEY")
UAZAPI_TOKEN            = os.environ.get("UAZAPI_TOKEN")
UAZAPI_URL              = os.environ.get("UAZAPI_URL")
GOOGLE_CREDENTIALS_JSON = os.environ.get("GOOGLE_CREDENTIALS_JSON")
CALENDAR_ID             = "evelinbaiense@gmail.com"
HUMAN_PAUSE_MINUTOS     = 30 

# ─── Memória ────────────────────────────────────────────────────────────────
conversas    = {}
humano_ativo = {}

client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

# ─── Prompt ─────────────────────────────────────────────────────────────────
SYSTEM_PROMPT = """Você é Evelin Abreu, corretora de imóveis do Praia Rasa de Búzios 2.
Atenda como a própria Evelin no WhatsApp — humana, próxima, profissional sem ser rígida.

IDIOMA:
- Responda SEMPRE em português do Brasil, independentemente do idioma que o cliente usar.

REGRAS DE TOM E COMPORTAMENTO:
- Espelhe o cliente. Se ele for informal, relaxe. Se for formal, mantenha.
- Respostas curtas. Máximo 2 frases por mensagem. Sem textão.
- Uma pergunta por vez. NUNCA faça uma pergunta e peça o nome na mesma mensagem.
- Sempre espere a resposta do cliente antes de continuar.
- Nunca revele que é IA. Se perguntarem, diga que é a própria Evelin.
- Não invente informações. Se não souber, convide para visitar.
- Nunca mande mais de uma mensagem seguida sem resposta do cliente.

PEDIDO DE NOME:
- Peça o nome naturalmente no meio da conversa, em mensagem separada e sozinha, sem nenhuma outra pergunta.
- Exemplo CORRETO: "Qual é o seu nome?" — só isso.
- Exemplo ERRADO: "Você prefere 300m² ou 600m²? Ah, e qual é o seu nome?" — PROIBIDO.

FLUXO:
1. Responder imediatamente o que o cliente perguntou
2. Na saudação inicial, perguntar se já conhece o empreendimento:
   - Se JÁ CONHECE: "Que ótimo! Me conta, você está buscando para morar, veraneio ou investimento?"
   - Se NÃO CONHECE: apresentar brevemente e perguntar o que está buscando
   - NUNCA repetir a pergunta "já conhece" — só na abertura
3. Pedir o nome naturalmente no meio da conversa, em mensagem separada e sozinha
4. Quando o interesse estiver claro, apresentar o empreendimento com base no perfil
5. Usar o script do plantão UMA VEZ quando o interesse for evidente
6. Conduzir para agendamento de visita

SCRIPT DO PLANTÃO — usar UMA VEZ quando interesse for evidente:
"[Nome], deixa eu te contar uma coisa 😊 Trabalho por comissão e meu plantão é por escala — se você for lá sem agendar comigo, outro corretor te atende e eu perco uma possível venda e a oportunidade de te atender com tanta dedicação. Me avisa antes, atendo qualquer dia e horário, sem compromisso nenhum!"

EMPREENDIMENTO — Praia Rasa de Búzios 2:

LOCALIZAÇÃO:
- Estrada dos Búzios (RJ-106), Bairro da Rasa, divisa Búzios/Cabo Frio
- 800m da Praia Rasa | 3 minutos da praia | Geribá a 8km
- Sempre diga "próximo a Búzios". Só mencione Cabo Frio se perguntarem sobre endereço ou documentação.
- Maps: https://www.google.com/maps/@-22.7238716,-42.001362,493m

INFRAESTRUTURA:
- Fechado, murado, portão fechado
- Guarita — segurança e monitoramento serão implantados com a associação de moradores
- Meio-fio instalado, rede elétrica em andamento com posteamento já feito, água encanada em breve
- Futura associação de moradores
- Taxa de condomínio: 10% do salário mínimo (cobrada somente após entrega do empreendimento)
- Playground, praça de lazer, área verde, bosque
- Próximo a condomínios de alto padrão, região de kitesurf
- Temos quadras com vista para o mar e quadras com vista para a serra

LOTES E VALORES — nunca altere esses números:

Lote 300m² (dimensões: 10x30 ou 7,5x40):
- Parcelas: a partir de R$899/mês em até 156x com reajuste anual pelo IGPM
- Entrada: R$7.000
- Entrada parcelada em 3x sem juros (ato / 30 dias / 60 dias) OU em até 10x no cartão (juros do cartão)
- Vista mar: a partir de R$1.199/mês em até 156x com reajuste anual pelo IGPM
- À vista: a partir de R$90.000 — informar SOMENTE se o cliente perguntar

Lote 600m²:
- Parcelas: a partir de R$1.599/mês em até 156x com reajuste anual pelo IGPM
- Entrada: R$14.000
- Entrada parcelada em 3x sem juros (ato / 30 dias / 60 dias) OU em até 10x no cartão (juros do cartão)
- Vista mar: a partir de R$1.999/mês em até 156x com reajuste anual pelo IGPM
- À vista: a partir de R$160.000 — informar SOMENTE se o cliente perguntar

FINANCIAMENTO:
- Direto pela incorporadora, sem banco, sem SPC/Serasa, sem aprovação bancária
- Planos de 12 a 156 parcelas — os juros variam de acordo com o plano escolhido
- Primeira parcela em até 45 dias após a compra
- Pode construir com 3 parcelas pagas, mediante autorização da empresa
- Entrada parcelada em 3x é sem juros. Entrada no cartão tem juros do próprio cartão.

DOCUMENTAÇÃO (RGI):
"Tem RGI sim. A incorporadora está finalizando o processo na prefeitura. Após a liberação, quem estiver com o lote quitado terá direito à transferência para o seu nome — é opcional e fica por conta do comprador."

OBJEÇÕES COMUNS:
"Tá longe" / "Achei longe":
→ "Na verdade fica bem perto — 3 minutos da praia pela RJ-106. Você está em qual região?"

"Tá caro":
→ "Entendo. O parcelamento começa em R$899/mês direto pela incorporadora, sem banco e sem SPC. Você prefere ver os lotes de 300m² ou 600m²?"

"Tem juros?":
→ "O financiamento tem juros de acordo com o plano escolhido, de 12 a 156 parcelas, mais o reajuste anual do IGPM — que é o índice padrão do mercado imobiliário. A entrada parcelada em 3x é sem nenhum acréscimo."

GATILHOS — usar naturalmente, nunca forçar:
- "Imagina ter um lugar pra escapar todo final de semana, a praia a 3 minutos, sem depender de hotel."
- "Quem reserva agora ainda consegue escolher o lote. As unidades estão saindo rápido."
- "Não precisa decidir nada na hora — vem conhecer pessoalmente e vê se faz sentido pra você."

URGÊNCIA — quando o cliente hesitar em visitar:
→ "Já vendemos boa parte do empreendimento. Quem agenda logo ainda tem escolha de lote."

AGENDAMENTO:
Quando o cliente confirmar um dia e horário específico para visita, responda confirmando e inclua
no final da sua resposta, em linha separada, exatamente este JSON (sem markdown, sem formatação):
{"agendar": true, "nome": "NOME_DO_CLIENTE", "data": "DD/MM/YYYY", "hora": "HH:MM"}

Exemplo:
"Ótimo, [Nome]! Visita marcada para sábado às 10h. Te espero lá 😊"
{"agendar": true, "nome": "João Silva", "data": "24/05/2026", "hora": "10:00"}

Se o cliente não confirmar horário ainda:
"As visitas são de domingo a domingo 😊 Qual é o melhor dia e horário pra você? Me confirma aqui que já deixo anotado!"
"""

SAUDACAO = "Olá, tudo bem? Eu sou Evelin Abreu, corretora de imóveis 😊 Você já conhece o Praia Rasa de Búzios 2?"


# ─── Google Calendar ────────────────────────────────────────────────────────
def get_calendar_service():
    credentials_info = json.loads(GOOGLE_CREDENTIALS_JSON)
    credentials = service_account.Credentials.from_service_account_info(
        credentials_info,
        scopes=["https://www.googleapis.com/auth/calendar"]
    )
    return build("calendar", "v3", credentials=credentials)


def criar_evento(nome_cliente, telefone, data_str, hora_str):
    try:
        service = get_calendar_service()
        dt = datetime.strptime(f"{data_str} {hora_str}", "%d/%m/%Y %H:%M")
        dt_fim = dt + timedelta(hours=2)
        evento = {
            "summary": f"Visita Praia Rasa — {nome_cliente}",
            "description": f"Cliente: {nome_cliente}\nWhatsApp: {telefone}",
            "location": "Estrada dos Búzios (RJ-106), Bairro da Rasa, Cabo Frio – RJ",
            "start": {"dateTime": dt.strftime("%Y-%m-%dT%H:%M:%S"), "timeZone": "America/Sao_Paulo"},
            "end":   {"dateTime": dt_fim.strftime("%Y-%m-%dT%H:%M:%S"), "timeZone": "America/Sao_Paulo"},
        }
        service.events().insert(calendarId=CALENDAR_ID, body=evento).execute()
        print(f"[AGENDA] Evento criado: {nome_cliente} em {data_str} às {hora_str}")
        return True
    except Exception as e:
        print(f"[AGENDA ERRO] {e}")
        return False


def extrair_agendamento(texto):
    match = re.search(r'\{[^{}]*"agendar"[^{}]*\}', texto)
    if match:
        try:
            return json.loads(match.group())
        except Exception:
            return None
    return None


def limpar_json(texto):
    return re.sub(r'\{[^{}]*"agendar"[^{}]*\}', '', texto).strip()


# ─── WhatsApp via UAZAPI ────────────────────────────────────────────────────
def enviar_mensagem(telefone, texto):
    texto_limpo = limpar_json(texto)
    if not texto_limpo:
        return

    headers = {"Content-Type": "application/json", "token": UAZAPI_TOKEN}
    payload = {"number": telefone, "text": texto_limpo}

    try:
        r = requests.post(
            f"{UAZAPI_URL}/send/text",
            json=payload,
            headers=headers,
            timeout=15
        )
        print(f"[WHATSAPP] Para {telefone} | status {r.status_code} | resposta: {r.text[:100]}")
    except Exception as e:
        print(f"[WHATSAPP ERRO] {e}")


# ─── Claude com memória ─────────────────────────────────────────────────────
def resposta_bot(telefone, mensagem_usuario):
    if telefone not in conversas:
        conversas[telefone] = []

    conversas[telefone].append({"role": "user", "content": mensagem_usuario})
    historico = conversas[telefone][-20:]

    resposta = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=500,
        system=SYSTEM_PROMPT,
        messages=historico
    )

    texto = resposta.content[0].text
    conversas[telefone].append({"role": "assistant", "content": texto})
    return texto


# ─── Webhook principal ──────────────────────────────────────────────────────
@app.route("/webhook", methods=["POST"])
def webhook():
    data = request.json or {}
    print(f"[WEBHOOK] Recebido: {json.dumps(data)[:300]}")

    try:
        # UAZAPI envia EventType no nível raiz
        event_type = data.get("EventType", data.get("event", ""))
        msg        = data.get("message", data.get("data", {}).get("message", {}))

        # Suporte aos dois formatos possíveis do UAZAPI
        chat_id      = msg.get("chatId") or msg.get("remoteJid") or data.get("data", {}).get("key", {}).get("remoteJid", "")
        from_me      = msg.get("fromMe", data.get("data", {}).get("key", {}).get("fromMe", False))
        was_by_api   = msg.get("wasSentByApi", False)
        is_group     = msg.get("isGroup", "@g.us" in str(chat_id))
        texto        = (
            msg.get("text") or
            msg.get("content") or
            msg.get("conversation") or
            msg.get("extendedTextMessage", {}).get("text") or
            ""
        ).strip()

        telefone = str(chat_id).replace("@s.whatsapp.net", "").replace("@c.us", "").replace("@g.us", "")

        print(f"[MSG] telefone={telefone} from_me={from_me} api={was_by_api} grupo={is_group} texto='{texto}'")

        # Ignora grupos
        if is_group:
            return jsonify({"status": "grupo_ignorado"}), 200

        # Ignora se não tiver telefone válido
        if not telefone or len(telefone) < 8:
            return jsonify({"status": "sem_telefone"}), 200

        # Mensagem enviada pela Evelin manualmente (não pelo bot)
        if from_me and not was_by_api:
            humano_ativo[telefone] = time.time()
            print(f"[HUMANO] Evelin assumiu {telefone}")
            return jsonify({"status": "humano_ativo"}), 200

        # Ignora mensagens enviadas pelo próprio bot via API
        if from_me and was_by_api:
            return jsonify({"status": "bot_ignorado"}), 200

        # Verifica pausa por humano
        if telefone in humano_ativo:
            decorrido = time.time() - humano_ativo[telefone]
            if decorrido < HUMAN_PAUSE_MINUTOS * 60:
                print(f"[HUMANO] Bot pausado para {telefone}")
                return jsonify({"status": "humano_no_controle"}), 200
            else:
                del humano_ativo[telefone]

        if not texto:
            return jsonify({"status": "sem_texto"}), 200

        # Nova conversa → saudação
        if telefone not in conversas:
            enviar_mensagem(telefone, SAUDACAO)
            conversas[telefone] = [{"role": "assistant", "content": SAUDACAO}]

        # Resposta do bot
        resposta = resposta_bot(telefone, texto)

        # Agenda se confirmado
        agendamento = extrair_agendamento(resposta)
        if agendamento and agendamento.get("agendar"):
            criar_evento(
                nome_cliente=agendamento.get("nome", "Cliente"),
                telefone=telefone,
                data_str=agendamento.get("data", ""),
                hora_str=agendamento.get("hora", "")
            )

        enviar_mensagem(telefone, resposta)

    except Exception as e:
        print(f"[ERRO] {e}")

    return jsonify({"status": "ok"}), 200


@app.route("/health", methods=["GET"])
def health():
    return jsonify({"status": "online", "bot": "Praia Rasa de Búzios 2"}), 200


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port)
