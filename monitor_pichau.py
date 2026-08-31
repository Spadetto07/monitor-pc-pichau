"""
Monitor de precos - Montagem PC Pichau
Execucao unica por invocacao (orquestrada via cron do GitHub Actions a cada 30min).
Proibido while True / time.sleep: a repeticao e responsabilidade do agendador externo.

A Pichau devolve 403 (pagina "Site em Manutencao" do Cloudflare) para o IP dos
runners do GitHub. Por isso a busca passa por um leitor intermediario (r.jina.ai),
com tentativa direta como reserva. Nao e preciso navegador: o preco a vista no PIX
vem numa meta tag do HTML, e o preco no cartao e o estoque vem do JSON-LD.
"""

import json
import os
import re
import time
from typing import Optional

import requests

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
CHAT_ID = os.environ.get("CHAT_ID")
JINA_KEY = os.environ.get("JINA_API_KEY")  # opcional: so aumenta o limite de uso

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
ESTADO = os.path.join(BASE_DIR, "estado.json")
BASE_URL = "https://www.pichau.com.br"
LEITOR = "https://r.jina.ai/"

NAVEGADOR = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

# ---------------------------------------------------------------------------
# Matriz de alvos (escalavel: basta adicionar/remover dicionarios da lista)
#
# "nome" -> chave no estado.json e rotulo nas mensagens
# "slug" -> caminho do produto no site (o que vem depois de pichau.com.br)
# "qtd"  -> quantidade na montagem, usada para compor o total
# ---------------------------------------------------------------------------
ALVOS = [
    {
        "nome": "Processador Ryzen 5 5600GT",
        "slug": "/processador-amd-ryzen-5-5600gt-6-core-12-threads-3-6ghz-4-6ghz-turbo-cache-19mb-am4-100-100001488box",
        "qtd": 1,
    },
    {
        "nome": "Placa-mae MSI A520M-A PRO",
        "slug": "/placa-mae-msi-a520m-a-pro-ddr4-socket-am4-chipset-amd-a520",
        "qtd": 1,
    },
    {
        "nome": "Memoria Best Memory 8GB DDR4 3200",
        "slug": "/memoria-best-memory-value-8gb-1x8gb-ddr4-3200mhz-bt-d4-8g-3200v",
        "qtd": 1,
    },
    {
        "nome": "SSD Adata SU650 256GB",
        "slug": "/ssd-adata-su650-256gb-2-5-sata-iii-6gb-s-leituras-520mb-s-gravacao-450mb-s-asu650ss-256gt-r",
        "qtd": 1,
    },
    {
        "nome": "Fonte Mancer Thunder 400W",
        "slug": "/fonte-mancer-thunder-400w-bronze-80-plus-mcr-thr400-bl01",
        "qtd": 1,
    },
    {
        "nome": "Gabinete TGT B130",
        "slug": "/gabinete-tgt-b130-mini-tower-preto-tgt-b130-pr01",
        "qtd": 1,
    },
]

LIMIAR = 0.01  # diferenca minima, em R$, para considerar que o preco mudou

RE_META_PIX = re.compile(r'product:price:amount"\s*content="([^"]+)"')
RE_LD = re.compile(
    r'<script[^>]+type="application/ld\+json"[^>]*>(.*?)</script>',
    re.DOTALL | re.IGNORECASE,
)
RE_BLOQUEIO = re.compile(r"CLOUDFLARE_ERROR|Site em Manuten", re.IGNORECASE)


# ---------------------------------------------------------------------------
# Utilidades
# ---------------------------------------------------------------------------
def para_numero(texto) -> Optional[float]:
    """Le um preco em qualquer das duas convencoes que a pagina usa.

    A meta tag vem no formato americano ("R$ 1032.93") e o texto da tela no
    brasileiro ("R$ 1.032,93"). Decidir pelo separador que aparece por ultimo
    resolve os dois sem precisar saber de qual campo veio.
    """
    if texto is None:
        return None
    bruto = re.sub(r"[^\d,.]", "", str(texto))
    if not bruto:
        return None
    ult_virgula, ult_ponto = bruto.rfind(","), bruto.rfind(".")
    if ult_virgula > ult_ponto:
        bruto = bruto.replace(".", "").replace(",", ".")
    else:
        bruto = bruto.replace(",", "")
    try:
        return float(bruto)
    except ValueError:
        return None


def brl(valor: float) -> str:
    """Formata float como moeda BR: 2516.94 -> 'R$ 2.516,94'."""
    inteiro, centavos = f"{valor:,.2f}".split(".")
    return f"R$ {inteiro.replace(',', '.')},{centavos}"


def pct(valor: float) -> str:
    """Percentual no formato BR, sempre com sinal: 0.8 -> '+0,80%'."""
    return f"{valor:+.2f}".replace(".", ",") + "%"


def ler_estado() -> dict:
    if not os.path.exists(ESTADO):
        return {}
    try:
        with open(ESTADO, "r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return {}


def salvar_estado(estado: dict) -> None:
    with open(ESTADO, "w", encoding="utf-8") as f:
        json.dump(estado, f, ensure_ascii=False, indent=2, sort_keys=True)
        f.write("\n")


def enviar_telegram(mensagem: str) -> None:
    if not TELEGRAM_TOKEN or not CHAT_ID:
        print("[AVISO] TELEGRAM_TOKEN ou CHAT_ID nao configurados. Pulando envio.")
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {
        "chat_id": CHAT_ID,
        "text": mensagem,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }
    try:
        resp = requests.post(url, data=payload, timeout=15)
        resp.raise_for_status()
    except requests.RequestException as e:
        print(f"[ERRO] Falha ao enviar mensagem ao Telegram: {e}")


# ---------------------------------------------------------------------------
# Coleta
# ---------------------------------------------------------------------------
def baixar(slug: str) -> str:
    """HTML do produto, pelo leitor intermediario e, se falhar, direto.

    Devolve string vazia quando as duas vias falham ou respondem com a pagina
    de bloqueio - o chamador trata isso como "nao consegui ler", nunca como
    "o produto sumiu".
    """
    alvo = BASE_URL + slug
    cabecalho_leitor = {"x-respond-with": "html", "User-Agent": NAVEGADOR}
    if JINA_KEY:
        cabecalho_leitor["Authorization"] = f"Bearer {JINA_KEY}"

    vias = [
        ("leitor", LEITOR + alvo, cabecalho_leitor),
        ("direto", alvo, {"User-Agent": NAVEGADOR,
                          "Accept-Language": "pt-BR,pt;q=0.9"}),
    ]
    for nome, url, cabecalho in vias:
        try:
            resp = requests.get(url, headers=cabecalho, timeout=60)
        except requests.RequestException as e:
            print(f"    [{nome}] falhou: {type(e).__name__}")
            continue
        if resp.status_code != 200:
            print(f"    [{nome}] HTTP {resp.status_code}")
            continue
        if RE_BLOQUEIO.search(resp.text[:5000]):
            print(f"    [{nome}] pagina de bloqueio do Cloudflare")
            continue
        return resp.text
    return ""


def extrair(html: str) -> dict:
    """Tira {'pix', 'cartao', 'disponivel'} do HTML do produto."""
    achado = RE_META_PIX.search(html)
    pix = para_numero(achado.group(1)) if achado else None

    cartao, disponivel = None, True
    for bruto in RE_LD.findall(html):
        try:
            dados = json.loads(bruto.strip())
        except (json.JSONDecodeError, ValueError):
            continue
        if isinstance(dados, dict) and dados.get("@type") == "Product":
            oferta = dados.get("offers") or {}
            cartao = para_numero(oferta.get("price"))
            disponivel = "OutOfStock" not in str(oferta.get("availability", ""))
            break

    if pix is None and cartao is None:
        raise ValueError("nenhum preco encontrado no HTML")
    # Sem o preco a vista, cai para o do cartao. Nunca aplica o desconto do PIX
    # por conta propria: um valor calculado dispararia alerta falso.
    if pix is None:
        pix = cartao
    return {"pix": pix, "cartao": cartao or pix, "disponivel": disponivel}


def coletar(alvo: dict) -> dict:
    html = baixar(alvo["slug"])
    if not html:
        raise RuntimeError("nao consegui baixar a pagina por nenhuma via")
    return extrair(html)


# ---------------------------------------------------------------------------
# Mensagens
# ---------------------------------------------------------------------------
def linha_mudanca(nome: str, antes: float, agora: float) -> str:
    dif = agora - antes
    variacao = (dif / antes * 100) if antes else 0.0
    seta = "\U0001F4C8" if dif > 0 else "\U0001F4C9"
    sinal = "+" if dif > 0 else "-"
    return (
        f"{seta} <b>{nome}</b>\n"
        f"   {brl(antes)} → <b>{brl(agora)}</b> "
        f"({sinal}{brl(abs(dif))} / {pct(variacao)})"
    )


def montar_mensagem(mudancas: list, estoque: list, total_novo: float,
                    total_antigo: Optional[float], indisponiveis: list) -> str:
    partes = ["\U0001F5A5️ <b>Montagem Pichau — mudou de preço</b>", ""]
    partes.append(f"<b>Total à vista no PIX: {brl(total_novo)}</b>")

    if total_antigo is not None and abs(total_novo - total_antigo) >= LIMIAR:
        dif = total_novo - total_antigo
        variacao = (dif / total_antigo * 100) if total_antigo else 0.0
        seta = "\U0001F4C8 subiu" if dif > 0 else "\U0001F4C9 caiu"
        sinal = "+" if dif > 0 else "-"
        partes.append(
            f"Antes: {brl(total_antigo)} — {seta} "
            f"{sinal}{brl(abs(dif))} ({pct(variacao)})"
        )
    elif total_antigo is not None:
        partes.append(f"Total sem alteração ({brl(total_antigo)})")

    if mudancas:
        partes += ["", "<b>O que mudou:</b>"] + mudancas
    if estoque:
        partes += ["", "<b>Estoque:</b>"] + estoque
    if indisponiveis:
        partes += ["", "⚠️ Fora de estoque: " + ", ".join(indisponiveis)]
    return "\n".join(partes)


# ---------------------------------------------------------------------------
def main() -> None:
    estado = ler_estado()
    itens = estado.get("itens", {})
    novos, falhas = {}, []

    for alvo in ALVOS:
        nome = alvo["nome"]
        print(f"--- Verificando: {nome} ---")
        try:
            novos[nome] = coletar(alvo)
            print(f"    PIX {novos[nome]['pix']:.2f} | "
                  f"cartao {novos[nome]['cartao']:.2f} | "
                  f"estoque {novos[nome]['disponivel']}")
        except Exception as e:
            print(f"[ERRO] Falha ao coletar {nome}: {e}")
            falhas.append(nome)
        time.sleep(1)  # cortesia com o leitor intermediario

    if not novos:
        print("[ERRO] Nenhum preco coletado. Estado preservado, nada enviado.")
        raise SystemExit(1)

    # Peca que falhou nesta rodada mantem o ultimo valor conhecido, para o total
    # nao dar um salto falso e disparar alerta a toa.
    for alvo in ALVOS:
        nome = alvo["nome"]
        if nome not in novos and nome in itens:
            novos[nome] = itens[nome]

    qtd = {a["nome"]: a["qtd"] for a in ALVOS}
    total_novo = round(sum(v["pix"] * qtd.get(k, 1) for k, v in novos.items()), 2)
    total_antigo = estado.get("total")

    if not itens:
        linhas = [f"• {a['nome']}: {brl(novos[a['nome']]['pix'])}"
                  for a in ALVOS if a["nome"] in novos]
        enviar_telegram(
            "\U0001F195 <b>Monitoramento iniciado — Montagem Pichau</b>\n\n"
            + "\n".join(linhas)
            + f"\n\n<b>Total à vista no PIX: {brl(total_novo)}</b>"
            + "\n\nAviso a cada mudança de preço, para cima ou para baixo."
        )
        salvar_estado({"total": total_novo, "itens": novos})
        print("[INFO] Linha de base gravada.")
        return

    mudancas, estoque, indisponiveis = [], [], []
    for alvo in ALVOS:
        nome = alvo["nome"]
        if nome not in novos:
            continue
        atual, anterior = novos[nome], itens.get(nome)
        if anterior is None:
            mudancas.append(
                f"\U0001F195 <b>{nome}</b>\n   entrou na lista: {brl(atual['pix'])}"
            )
        elif abs(atual["pix"] - anterior["pix"]) >= LIMIAR:
            mudancas.append(linha_mudanca(nome, anterior["pix"], atual["pix"]))
        if anterior and anterior.get("disponivel") != atual["disponivel"]:
            marca = "✅" if atual["disponivel"] else "\U0001F6AB"
            texto = "voltou ao estoque" if atual["disponivel"] else "saiu de estoque"
            estoque.append(f"{marca} <b>{nome}</b>: {texto}")
        if not atual["disponivel"]:
            indisponiveis.append(nome)

    if not mudancas and not estoque:
        print(f"[INFO] Sem alteracao. Total {brl(total_novo)}")
        salvar_estado({"total": total_novo, "itens": novos})
        return

    enviar_telegram(
        montar_mensagem(mudancas, estoque, total_novo, total_antigo, indisponiveis)
    )
    salvar_estado({"total": total_novo, "itens": novos})
    print(f"[INFO] Alerta enviado. {len(mudancas)} peca(s) mudaram de preco.")


if __name__ == "__main__":
    main()
