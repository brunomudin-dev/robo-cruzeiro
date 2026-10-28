"""
Robô de anúncios do Cruzeiro
----------------------------
Procura anúncios novos na OLX, Mercado Livre e Enjoei e avisa no WhatsApp
(via CallMeBot). Roda sozinho no GitHub Actions a cada 10 minutos.
Só avisa de anúncio criado depois que o robô começou (compara o número do
anúncio), para não avisar de anúncio antigo que reaparece na busca.

Você NÃO precisa mexer neste arquivo. Para mudar os termos de busca,
edite o arquivo "termos.txt".
"""

import json
import os
import re
import time
import random
import urllib.parse
from datetime import datetime, timezone, timedelta

import requests
from bs4 import BeautifulSoup

# ---------------------------------------------------------------- ajustes
ARQUIVO_TERMOS = "termos.txt"
ARQUIVO_ESTADO = "vistos.json"
MAX_MENSAGENS_POR_RODADA = 30      # o resto vai na próxima rodada
PAUSA_ENTRE_MENSAGENS = 4          # segundos (o CallMeBot não gosta de rajada)
FALHAS_PARA_ALERTAR = 6            # 6 rodadas seguidas (~1h) com erro -> avisa
DIAS_PARA_ESQUECER = 120           # limpa anúncios antigos do arquivo de vistos

FUSO_BR = timezone(timedelta(hours=-3))

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/130.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "pt-BR,pt;q=0.9,en;q=0.8",
}

PRECO_RE = re.compile(r"R\$\s?[\d\.]+(?:,\d{2})?")


# ---------------------------------------------------------------- utilidades
def agora():
    return datetime.now(FUSO_BR).isoformat(timespec="seconds")


ULTIMA_PAGINA = {}


def baixar(url):
    r = requests.get(url, headers=HEADERS, timeout=30)
    if r.status_code != 200:
        raise RuntimeError(f"HTTP {r.status_code}")
    t = re.search(r"<title[^>]*>(.*?)</title>", r.text, re.S | re.I)
    ULTIMA_PAGINA.update({
        "url_final": r.url,
        "tamanho": len(r.text),
        "titulo": limpar(t.group(1))[:70] if t else "(sem título)",
    })
    return r.text


def limpar(texto):
    return re.sub(r"\s+", " ", (texto or "")).strip()


def achar_preco(tag, niveis=6):
    """Sobe na página a partir do link procurando um 'R$ ...' por perto."""
    no = tag
    for _ in range(niveis):
        if no is None:
            break
        m = PRECO_RE.search(no.get_text(" ", strip=True))
        if m:
            return m.group(0)
        no = no.parent
    return "preço não informado"


def extrair_links(html, padrao_id, base=""):
    """
    Método genérico: pega todos os links que parecem ser de anúncio,
    usando uma expressão que captura o ID do anúncio na URL.
    """
    soup = BeautifulSoup(html, "html.parser")
    anuncios = {}
    for a in soup.find_all("a", href=True):
        href = a["href"]
        if href.startswith("/"):
            href = base + href
        m = padrao_id.search(href)
        if not m:
            continue
        aid = m.group(1)
        titulo = limpar(a.get("title") or a.get_text(" ", strip=True))
        if aid in anuncios and len(anuncios[aid]["titulo"]) >= len(titulo):
            continue
        anuncios[aid] = {
            "id": aid,
            "titulo": titulo or "(sem título)",
            "preco": achar_preco(a),
            "link": href.split("#")[0],
        }
    if not anuncios and base:
        # plano B: procura endereços de anúncio no texto bruto da página
        for m in padrao_id.finditer(html.replace("\\/", "/")):
            aid = m.group(1)
            if aid not in anuncios:
                trecho = m.group(0)
                link = trecho if trecho.startswith("http") else base + "/" + trecho.split(".br/", 1)[-1]
                anuncios[aid] = {"id": aid, "titulo": "(ver anúncio)",
                                 "preco": "preço não informado", "link": link}
    return list(anuncios.values())


def procurar_dicts(obj, chaves):
    """Varre um JSON qualquer e devolve os dicionários que têm as chaves."""
    achados = []
    pilha = [obj]
    while pilha:
        atual = pilha.pop()
        if isinstance(atual, dict):
            if all(k in atual for k in chaves):
                achados.append(atual)
            pilha.extend(atual.values())
        elif isinstance(atual, list):
            pilha.extend(atual)
    return achados


# ---------------------------------------------------------------- sites
def buscar_olx(termo):
    url = "https://www.olx.com.br/brasil?" + urllib.parse.urlencode(
        {"q": termo, "sf": "1"}  # sf=1 -> mais recentes primeiro
    )
    html = baixar(url)

    # 1ª tentativa: dados estruturados que a OLX embute na página
    m = re.search(
        r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', html, re.S
    )
    if m:
        try:
            dados = json.loads(m.group(1))
            itens = procurar_dicts(dados, ("subject", "url"))
            anuncios = []
            for it in itens:
                aid = str(it.get("listId") or it.get("id") or it["url"])
                anuncios.append({
                    "id": aid,
                    "titulo": limpar(it.get("subject")),
                    "preco": limpar(str(it.get("price") or "preço não informado")),
                    "link": it["url"],
                })
            if anuncios:
                return anuncios
        except Exception:
            pass

    # 2ª tentativa: links da página
    return extrair_links(
        html, re.compile(r"olx\.com\.br/.+?-(\d{8,})(?:[/?#]|$)"),
        base="https://www.olx.com.br",
    )


def buscar_mercadolivre(termo):
    slug = urllib.parse.quote(termo.replace(" ", "-"))
    anuncios = []
    # O ML não tem "mais recentes"; olhamos as 2 primeiras páginas.
    for pagina_url in (
        f"https://lista.mercadolivre.com.br/{slug}",
        f"https://lista.mercadolivre.com.br/{slug}_Desde_49_NoIndex_True",
    ):
        html = baixar(pagina_url)
        anuncios += extrair_links(
            html, re.compile(r"(?:https?://)?[a-z.]*mercadolivre\.com\.br/[^\s\"'<>]*?(MLB-?\d{6,})"),
            base="https://www.mercadolivre.com.br",
        )
        time.sleep(random.uniform(1, 3))
    # normaliza o ID (MLB-123 e MLB123 são o mesmo anúncio)
    unicos = {}
    for a in anuncios:
        a["id"] = a["id"].replace("-", "")
        unicos.setdefault(a["id"], a)
    return list(unicos.values())


ENJOEI_API = "https://enjusearch.enjoei.com.br/graphql-search-x"
ENJOEI_PAGINAS = 5          # 48 anúncios por página


def buscar_enjoei(termo):
    """Usa a mesma busca que o site do Enjoei usa (Brasil inteiro)."""
    anuncios, depois = [], None
    for _ in range(ENJOEI_PAGINAS):
        params = {
            "first": "48",
            "operation_name": "searchProducts",
            "query_id": "c5faa5f85fb47bf0beaa97b67d8a9189",
            "search_context": "products_search_default",
            "term": termo,
        }
        if depois:
            params["after"] = depois
        r = requests.get(ENJOEI_API, params=params, headers=HEADERS, timeout=30)
        if r.status_code != 200:
            raise RuntimeError(f"HTTP {r.status_code}")
        ULTIMA_PAGINA.update({"url_final": r.url, "tamanho": len(r.text),
                              "titulo": "API do Enjoei"})
        arestas = r.json()["data"]["search"]["products"]["edges"]
        for e in arestas:
            n = e["node"]
            preco = (n.get("price") or {}).get("current")
            anuncios.append({
                "id": str(n["id"]),
                "titulo": limpar((n.get("title") or {}).get("name")) or "(sem título)",
                "preco": f"R$ {preco:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")
                         if isinstance(preco, (int, float)) else "preço não informado",
                "link": "https://www.enjoei.com.br/p/" + n["path"],
            })
        if len(arestas) < 48:
            break
        depois = arestas[-1]["cursor"]
        time.sleep(random.uniform(0.5, 1.5))
    return anuncios


# OLX e Mercado Livre bloqueiam os servidores do GitHub; ficam desligados
# aqui. Para ligar (ex.: rodando no celular/PC), apague o "#" da linha.
SITES = {
    # "OLX": buscar_olx,
    # "Mercado Livre": buscar_mercadolivre,
    "Enjoei": buscar_enjoei,
}


# ---------------------------------------------------------------- WhatsApp
def enviar_whatsapp(texto):
    fone = os.environ.get("CALLMEBOT_PHONE", "").strip()
    chave = os.environ.get("CALLMEBOT_APIKEY", "").strip()
    if not fone or not chave:
        print("   [aviso] segredos do CallMeBot não configurados; mensagem não enviada")
        return False
    url = "https://api.callmebot.com/whatsapp.php?" + urllib.parse.urlencode(
        {"phone": fone, "text": texto, "apikey": chave}
    )
    try:
        r = requests.get(url, timeout=30)
        ok = r.status_code == 200
        if not ok:
            print(f"   [erro CallMeBot] HTTP {r.status_code}")
        return ok
    except Exception as e:
        print(f"   [erro CallMeBot] {e}")
        return False


# ---------------------------------------------------------------- estado
def carregar_estado():
    try:
        with open(ARQUIVO_ESTADO, encoding="utf-8") as f:
            estado = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        estado = {}
    estado.setdefault("vistos", {})       # "site|id" -> data
    estado.setdefault("iniciados_v2", [])    # "site|termo" já memorizados
    estado.setdefault("falhas", {})       # site -> rodadas seguidas com erro
    estado.setdefault("alertado", [])     # sites com aviso de falha enviado
    estado.setdefault("corte", {})        # site -> maior ID visto na 1ª leitura
    return estado


def salvar_estado(estado):
    limite = datetime.now(FUSO_BR) - timedelta(days=DIAS_PARA_ESQUECER)
    estado["vistos"] = {
        k: v for k, v in estado["vistos"].items()
        if datetime.fromisoformat(v) >= limite
    }
    with open(ARQUIVO_ESTADO, "w", encoding="utf-8") as f:
        json.dump(estado, f, ensure_ascii=False, indent=1, sort_keys=True)


def numero_id(aid):
    m = re.search(r"\d+", str(aid))
    return int(m.group(0)) if m else 0


def ler_termos():
    with open(ARQUIVO_TERMOS, encoding="utf-8") as f:
        termos = [limpar(l) for l in f if limpar(l) and not l.startswith("#")]
    return list(dict.fromkeys(termos))


# ---------------------------------------------------------------- principal
def main():
    estado = carregar_estado()
    termos = ler_termos()
    novos = []
    print(f"Início {agora()} — {len(termos)} termos, {len(SITES)} sites")

    for site, funcao in SITES.items():
        erros_site, total_site = 0, 0
        for termo in termos:
            chave_inicio = f"{site}|{termo}"
            try:
                anuncios = funcao(termo)
            except Exception as e:
                erros_site += 1
                print(f"  {site:14} | {termo:22} | ERRO: {e}")
                time.sleep(random.uniform(1, 3))
                continue

            total_site += len(anuncios)
            if not anuncios:
                d = ULTIMA_PAGINA
                print(f"  {site:14} | {termo:22} |   0 achados | página: "
                      f"{d.get('tamanho')} caracteres, título '{d.get('titulo')}', "
                      f"endereço final {d.get('url_final')}")
                time.sleep(random.uniform(1, 3))
                continue
            primeira_vez = chave_inicio not in estado["iniciados_v2"]
            maior = max(numero_id(a["id"]) for a in anuncios)
            if site not in estado["corte"]:
                estado["corte"][site] = maior
            corte = estado["corte"][site]
            n_novos = 0
            for a in anuncios:
                chave = f"{site}|{a['id']}"
                if chave in estado["vistos"]:
                    continue
                if primeira_vez or numero_id(a["id"]) <= corte:
                    estado["vistos"][chave] = agora()   # antigo: só memoriza
                elif not any(n["chave"] == chave for n in novos):
                    novos.append({**a, "site": site, "chave": chave})
                    n_novos += 1
            if primeira_vez and anuncios:
                estado["iniciados_v2"].append(chave_inicio)
            print(f"  {site:14} | {termo:22} | {len(anuncios):3} achados"
                  f" | {'memorizados (1ª vez)' if primeira_vez else f'{n_novos} novos'}")
            time.sleep(random.uniform(1, 3))

        # controle de falhas: o site falhou em TODOS os termos?
        if total_site == 0:
            estado["falhas"][site] = estado["falhas"].get(site, 0) + 1
        else:
            if site in estado["alertado"]:
                enviar_whatsapp(f"✅ Robô Cruzeiro: {site} voltou a funcionar.")
                estado["alertado"].remove(site)
            estado["falhas"][site] = 0

        if (estado["falhas"][site] >= FALHAS_PARA_ALERTAR
                and site not in estado["alertado"]):
            if enviar_whatsapp(
                f"⚠️ Robô Cruzeiro: não estou conseguindo ler o {site} "
                f"há cerca de 1 hora. Os outros sites seguem normais."
            ):
                estado["alertado"].append(site)

    # envia os novos (um por mensagem)
    enviados = 0
    for a in novos[:MAX_MENSAGENS_POR_RODADA]:
        texto = f"🦊 {a['site']}\n{a['titulo']}\n{a['preco']}\n{a['link']}"
        if enviar_whatsapp(texto):
            estado["vistos"][a["chave"]] = agora()
            enviados += 1
        time.sleep(PAUSA_ENTRE_MENSAGENS)

    salvar_estado(estado)
    print(f"Fim {agora()} — {len(novos)} novos, {enviados} enviados")


if __name__ == "__main__":
    main()
