"""
Monitor de precos - Montagem PC Pichau
Execucao unica por invocacao (orquestrada via cron do GitHub Actions a cada 30min).
Proibido while True / time.sleep: a repeticao e responsabilidade do agendador externo.
"""

import json
import os
import re
from typing import Optional

import requests
from playwright.sync_api import sync_playwright

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
CHAT_ID = os.environ.get("CHAT_ID")

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
ESTADO = os.path.join(BASE_DIR, "estado.json")
BASE_URL = "https://www.pichau.com.br"

# ---------------------------------------------------------------------------
# Matriz de alvos (escalavel: basta adicionar/remover dicionarios da lista)
#
# "nome"  -> chave no estado.json e rotulo curto nas mensagens
# "slug"  -> caminho do produto no site (o que vem depois de pichau.com.br/)
# "qtd"   -> quantidade na montagem, usada para compor o total
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

# Diferenca minima (em R$) para considerar que o preco mudou.
LIMIAR = 0.01


# ---------------------------------------------------------------------------
# Utilidades
# ---------------------------------------------------------------------------
def limpar_preco(texto: str) -> float:
    """Converte string de preco BR (ex: 'R$ 1.234,56') para float."""
    bruto = re.sub(r"[^\d,.]", "", texto)
    if not bruto:
        raise ValueError(f"Nao foi possivel extrair numero de: {texto!r}")
    return float(bruto.replace(".", "").replace(",", "."))


def pct(valor: float) -> str:
    """Percentual no formato BR, sempre com sinal: 0.8 -> '+0,80%'."""
    return f"{valor:+.2f}".replace(".", ",") + "%"


def brl(valor: float) -> str:
    """Formata float como moeda BR: 2516.94 -> 'R$ 2.516,94'."""
    inteiro, centavos = f"{valor:,.2f}".split(".")
    return f"R$ {inteiro.replace(',', '.')},{centavos}"


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
PADRAO_PRECO = re.compile(r"R\$\s*\d[\d.]*,\d{2}")


def primeiro_preco(page, seletor: str) -> Optional[float]:
    """Primeiro elemento do seletor cujo texto e de fato um valor em reais.

    As classes do MUI mudam de hash a cada build do site, mas mantem os
    sufixos ("price_vista", "price_total"). O casamento por sufixo tambem
    pega os rotulos irmaos - "a vista", "no PIX com 15% desconto" - entao
    e preciso varrer os candidatos e ficar com o que tem R$ de verdade.
    """
    for el in page.locator(seletor).all():
        try:
            texto = (el.text_content() or "").strip()
        except Exception:
            continue
        achado = PADRAO_PRECO.search(texto)
        if achado:
            return limpar_preco(achado.group(0))
    return None


def coletar(page, alvo: dict) -> dict:
    """Devolve {'pix': float, 'cartao': float, 'disponivel': bool} do produto."""
    page.goto(BASE_URL + alvo["slug"], wait_until="domcontentloaded", timeout=60000)

    # O bloco de precos e renderizado no cliente: o HTML cru que volta do
    # servidor nao tem nenhum valor em reais. Esperar o texto "R$" aparecer
    # dentro do bloco de preco, e nao so o elemento existir.
    page.wait_for_function(
        """() => {
            const els = document.querySelectorAll('[class*="price_total"], [class*="price_vista"]');
            return [...els].some(e => /R\\$\\s*\\d[\\d.]*,\\d{2}/.test(e.textContent || ''));
        }""",
        timeout=45000,
    )

    # Disponibilidade e preco no cartao saem do JSON-LD (schema.org/Product),
    # que o site renderiza no HTML e e mais confiavel que ler texto da tela.
    disponivel, cartao_ld = True, None
    for bloco in page.locator('script[type="application/ld+json"]').all():
        try:
            dados = json.loads(bloco.text_content() or "")
        except (json.JSONDecodeError, ValueError):
            continue
        if isinstance(dados, dict) and dados.get("@type") == "Product":
            oferta = dados.get("offers") or {}
            disponivel = "OutOfStock" not in str(oferta.get("availability", ""))
            try:
                cartao_ld = float(oferta.get("price"))
            except (TypeError, ValueError):
                cartao_ld = None
            break

    cartao = primeiro_preco(page, '[class*="price_total"]') or cartao_ld
    pix = primeiro_preco(page, '[class*="price_vista"]')

    # Sem o preco a vista na tela, cai para o preco no cartao. Nunca inventa
    # o desconto do PIX: um valor chutado dispararia alerta falso.
    if pix is None:
        if cartao is None:
            raise ValueError("nenhum preco encontrado na pagina")
        pix = cartao

    return {"pix": pix, "cartao": cartao or pix, "disponivel": disponivel}


# ---------------------------------------------------------------------------
# Mensagens
# ---------------------------------------------------------------------------
def linha_mudanca(nome: str, antes: float, agora: float) -> str:
    dif = agora - antes
    variacao = (dif / antes * 100) if antes else 0.0
    seta = "📈" if dif > 0 else "📉"
    sinal = "+" if dif > 0 else "-"
    return (
        f"{seta} <b>{nome}</b>\n"
        f"   {brl(antes)} → <b>{brl(agora)}</b> "
        f"({sinal}{brl(abs(dif))} / {pct(variacao)})"
    )


def montar_mensagem(mudancas: list, estoque: list, total_novo: float,
                    total_antigo: Optional[float], indisponiveis: list) -> str:
    partes = ["🖥️ <b>Montagem Pichau — mudou de preço</b>", ""]

    partes.append(f"<b>Total à vista no PIX: {brl(total_novo)}</b>")
    if total_antigo is not None and abs(total_novo - total_antigo) >= LIMIAR:
        dif = total_novo - total_antigo
        variacao = (dif / total_antigo * 100) if total_antigo else 0.0
        seta = "📈 subiu" if dif > 0 else "📉 caiu"
        sinal = "+" if dif > 0 else "-"
        partes.append(
            f"Antes: {brl(total_antigo)} — {seta} {sinal}{brl(abs(dif))} ({pct(variacao)})"
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

    novos = {}
    falhas = []

    with sync_playwright() as p:
        navegador = p.chromium.launch(headless=True)
        contexto = navegador.new_context(
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
            ),
            locale="pt-BR",
        )
        page = contexto.new_page()
        for alvo in ALVOS:
            nome = alvo["nome"]
            print(f"--- Verificando: {nome} ---")
            try:
                novos[nome] = coletar(page, alvo)
                print(f"    PIX {novos[nome]['pix']:.2f} | "
                      f"cartao {novos[nome]['cartao']:.2f} | "
                      f"estoque {novos[nome]['disponivel']}")
            except Exception as e:
                print(f"[ERRO] Falha ao coletar {nome}: {e}")
                falhas.append(nome)
        navegador.close()

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

    # Primeira execucao: grava a linha de base e avisa.
    if not itens:
        linhas = [f"• {a['nome']}: {brl(novos[a['nome']]['pix'])}"
                  for a in ALVOS if a["nome"] in novos]
        enviar_telegram(
            "🆕 <b>Monitoramento iniciado — Montagem Pichau</b>\n\n"
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
        atual = novos[nome]
        anterior = itens.get(nome)
        if anterior is None:
            mudancas.append(f"🆕 <b>{nome}</b>\n   entrou na lista: {brl(atual['pix'])}")
        elif abs(atual["pix"] - anterior["pix"]) >= LIMIAR:
            mudancas.append(linha_mudanca(nome, anterior["pix"], atual["pix"]))
        if anterior and anterior.get("disponivel") != atual["disponivel"]:
            estoque.append(
                f"{'✅' if atual['disponivel'] else '🚫'} <b>{nome}</b>: "
                f"{'voltou ao estoque' if atual['disponivel'] else 'saiu de estoque'}"
            )
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
