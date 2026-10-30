"""
Robô de anúncios do Cruzeiro
----------------------------
Procura anúncios novos na OLX e no Enjoei e avisa no Telegram
(ou no WhatsApp via CallMeBot). Roda sozinho no GitHub Actions a cada 10 minutos.
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

# A OLX bloqueia (HTTP 403) quem busca demais. Por isso, a cada rodada ela
# consulta só uma parte dos termos, em rodízio, com pausas maiores; e, se
# começar a bloquear, o robô dá um tempo antes de tentar de novo.
TERMOS_POR_RODADA = {"OLX": 6}     # quantos termos a OLX busca por rodada
TERMOS_SEMPRE = ["camisa cruzeiro"]  # buscados em TODA rodada (fora do rodízio)
PAUSA_ENTRE_BUSCAS = {"OLX": (6, 12)}   # segundos entre uma busca e outra
BLOQUEIOS_PARA_PARAR = 2           # 403 seguidos -> encerra a OLX na rodada
DESCANSO_APOS_BLOQUEIO = 30 * 60   # segundos sem tentar a OLX após bloqueio
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
                m_id = re.search(r"-(\d{8,})(?:[/?#]|$)", str(it["url"]))
                aid = m_id.group(1) if m_id else str(it.get("listId") or it["url"])
                anuncios.append({
                    "id": aid,
                    "titulo": limpar(it.get("subject")),
                    "preco": limpar(str(it.get("price") or "preço não informado")),
                    "link": str(it["url"]).split("?")[0],
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


TODOS_OS_SITES = {
    "OLX": buscar_olx,
    "Enjoei": buscar_enjoei,
}

# Quais sites ESTE computador/celular consulta. No GitHub fica só o Enjoei
# (a OLX bloqueia servidores). No celular, ROBO_SITES="OLX".
_escolha = os.environ.get("ROBO_SITES", "Enjoei")
SITES = {
    nome: f for nome, f in TODOS_OS_SITES.items()
    if nome.lower() in [x.strip().lower() for x in _escolha.split(",")]
}


# ---------------------------------------------------------------- avisos
def enviar_telegram(texto, token, chat_id):
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={"chat_id": chat_id, "text": texto},
            timeout=30,
        )
        ok = r.status_code == 200 and r.json().get("ok")
        if not ok:
            print(f"   [erro Telegram] HTTP {r.status_code} — resposta: "
                  f"{r.text.replace(token, '***')[:300]}")
        return bool(ok)
    except Exception as e:
        print(f"   [erro Telegram] {str(e).replace(token, '***')}")
        return False


def enviar_whatsapp(texto):
    """Envia o aviso. Usa o Telegram se estiver configurado; senão, CallMeBot."""
    token = os.environ.get("TELEGRAM_TOKEN", "").strip()
    chat_id = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
    if token and chat_id:
        return enviar_telegram(texto, token, chat_id)
    return enviar_callmebot(texto)


def enviar_callmebot(texto):
    """
    Envia para um ou mais números. Para vários, separe por vírgula, na mesma
    ordem, o número e a apikey de cada um:
      CALLMEBOT_PHONE  = +553199999999,+553188888888
      CALLMEBOT_APIKEY = 123456,654321
    Conta como enviado se chegou para pelo menos um.
    """
    fones = [x.strip() for x in os.environ.get("CALLMEBOT_PHONE", "").split(",") if x.strip()]
    chaves = [x.strip() for x in os.environ.get("CALLMEBOT_APIKEY", "").split(",") if x.strip()]
    if not fones or not chaves:
        print("   [aviso] nenhum aviso configurado (Telegram/CallMeBot); mensagem não enviada")
        return False
    if len(fones) != len(chaves):
        print(f"   [aviso] {len(fones)} números e {len(chaves)} apikeys — "
              "precisa ter a mesma quantidade, na mesma ordem")
    algum_ok = False
    for i, (fone, chave) in enumerate(zip(fones, chaves), start=1):
        if i > 1:
            time.sleep(2)
        if _callmebot_um(texto, fone, chave, i):
            algum_ok = True
    return algum_ok


def _callmebot_um(texto, fone, chave, n):
    url = "https://api.callmebot.com/whatsapp.php?" + urllib.parse.urlencode(
        {"phone": fone, "text": texto, "apikey": chave}
    )
    try:
        r = requests.get(url, timeout=30)
        resposta = BeautifulSoup(r.text, "html.parser").get_text(" ", strip=True)
        resposta = resposta.replace(chave, "***").replace(fone, "***")[:300]
        ok = r.status_code == 200 and "error" not in resposta.lower() \
            and "invalid" not in resposta.lower()
        if not ok:
            print(f"   [erro CallMeBot, número {n}] HTTP {r.status_code} — resposta: {resposta}")
        return ok
    except Exception as e:
        print(f"   [erro CallMeBot, número {n}] {str(e).replace(chave, '***')}")
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
    # trava: se a rodada anterior ainda está rodando, esta não começa
    trava = ARQUIVO_ESTADO + ".lock"
    try:
        if time.time() - os.path.getmtime(trava) < 15 * 60:
            print(f"{agora()} — rodada anterior ainda em andamento; pulando")
            return
    except FileNotFoundError:
        pass
    open(trava, "w").close()
    try:
        rodada()
    finally:
        try:
            os.remove(trava)
        except FileNotFoundError:
            pass


def rodada():
    estado = carregar_estado()
    termos = ler_termos()
    novos = []
    print(f"Início {agora()} — {len(termos)} termos, {len(SITES)} sites")

    for site, funcao in SITES.items():
        erros_site, total_site = 0, 0
        pausa = estado.setdefault("pausa_ate", {}).get(site, 0)
        if time.time() < pausa:
            print(f"  {site:14} | descansando após bloqueio até "
                  f"{datetime.fromtimestamp(pausa, FUSO_BR):%H:%M}")
            continue

        termos_site = termos
        limite = TERMOS_POR_RODADA.get(site)
        if limite and limite < len(termos):
            sempre = [t for t in termos if t.lower() in [x.lower() for x in TERMOS_SEMPRE]]
            outros = [t for t in termos if t not in sempre]
            vagas = max(limite - len(sempre), 0)
            escolhidos = []
            if outros and vagas:
                pos = estado.setdefault("rodizio", {}).get(site, 0) % len(outros)
                escolhidos = (outros[pos:] + outros[:pos])[:vagas]
                estado["rodizio"][site] = (pos + vagas) % len(outros)
            termos_site = sempre + escolhidos
        intervalo = PAUSA_ENTRE_BUSCAS.get(site, (1, 3))

        bloqueios_seguidos, bloqueado = 0, False
        for termo in termos_site:
            chave_inicio = f"{site}|{termo}"
            try:
                anuncios = funcao(termo)
                bloqueios_seguidos = 0
            except Exception as e:
                erros_site += 1
                print(f"  {site:14} | {termo:22} | ERRO: {e}")
                if "403" in str(e):
                    bloqueios_seguidos += 1
                    if bloqueios_seguidos >= BLOQUEIOS_PARA_PARAR:
                        estado["pausa_ate"][site] = time.time() + DESCANSO_APOS_BLOQUEIO
                        print(f"  {site:14} | bloqueando — pausa de "
                              f"{DESCANSO_APOS_BLOQUEIO // 60} min")
                        bloqueado = True
                        break
                time.sleep(random.uniform(*intervalo))
                continue

            total_site += len(anuncios)
            if not anuncios:
                d = ULTIMA_PAGINA
                print(f"  {site:14} | {termo:22} |   0 achados | página: "
                      f"{d.get('tamanho')} caracteres, título '{d.get('titulo')}', "
                      f"endereço final {d.get('url_final')}")
                time.sleep(random.uniform(*intervalo))
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
            time.sleep(random.uniform(*intervalo))

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
            salvar_estado(estado)   # grava na hora: se a rodada cair, não repete
        time.sleep(PAUSA_ENTRE_MENSAGENS)

    salvar_estado(estado)
    print(f"Fim {agora()} — {len(novos)} novos, {enviados} enviados")


if __name__ == "__main__":
    main()
