"""
expand_corpus.py — Expand word_bank from 20K to 100K words.

Three sources:
  1. Remaining 30K from pt_br_50k.txt (ranks 20001-50000)
  2. Curated regional slang & gíria (Baiano, Carioca, Paulista, Mineiro,
     Nordestino, Gaúcho, Nortista) — hardcoded lists
  3. GPT-4o generated batches: verb conjugations, colloquial forms,
     augmentatives/diminutives, compound expressions

Usage:
    python3 expand_corpus.py --frequency   Add ranks 20001-50000 from freq file
    python3 expand_corpus.py --slang       Add curated regional slang
    python3 expand_corpus.py --generate    Generate additional words via GPT-4o
    python3 expand_corpus.py --all         All three steps
    python3 expand_corpus.py --status      Show current word count and tier distribution
"""

import json
import os
import sqlite3
import sys
import time
from pathlib import Path

import openai

from srs_engine import DB_PATH, get_connection, init_db

DATA_DIR = Path(__file__).parent / "data"
FREQ_FILE = DATA_DIR / "pt_br_50k.txt"

# ── Expanded tier ranges for 100K words ──────────────────────────────
# Tier 1: Survival (1-1000) — most common words
# Tier 2: Daily (1001-5000)
# Tier 3: Conversational (5001-15000)
# Tier 4: Fluency (15001-35000)
# Tier 5: Nuance (35001-65000)
# Tier 6: Near-Native (65001-100000)

EXPANDED_TIER_RANGES = {
    1: (1, 1000),
    2: (1001, 5000),
    3: (5001, 15000),
    4: (15001, 35000),
    5: (35001, 65000),
    6: (65001, 100000),
}


def get_expanded_tier(rank):
    for tier, (lo, hi) in EXPANDED_TIER_RANGES.items():
        if lo <= rank <= hi:
            return tier
    return 6


def _serialize_card():
    """Create a fresh FSRS card state."""
    from fsrs import Card
    card = Card()
    d = card.to_dict()
    for k, v in d.items():
        if hasattr(v, "isoformat"):
            d[k] = v.isoformat()
    return json.dumps(d)


def add_word_bulk(conn, word, rank, freq_count=0):
    """Insert a word, return True if inserted, False if duplicate."""
    tier = get_expanded_tier(rank)
    try:
        conn.execute(
            """INSERT INTO word_bank
               (word, frequency_rank, frequency_count, difficulty_tier,
                srs_stability, srs_difficulty, mastery_level, srs_state)
               VALUES (?, ?, ?, ?, 0.0, 0.0, 0, ?)""",
            (word, rank, freq_count, tier, _serialize_card()),
        )
        return True
    except sqlite3.IntegrityError:
        return False


def update_tiers(conn):
    """Update existing words to use expanded tier ranges."""
    for tier, (lo, hi) in EXPANDED_TIER_RANGES.items():
        conn.execute(
            "UPDATE word_bank SET difficulty_tier = ? WHERE frequency_rank BETWEEN ? AND ?",
            (tier, lo, hi),
        )
    conn.commit()
    print("Updated all existing words to expanded tier ranges.")


# ── Step 1: Add remaining frequency words (20001-50000) ─────────────

def add_frequency_words():
    """Add ranks 20001-50000 from the frequency file."""
    if not FREQ_FILE.exists():
        print("Frequency file not found. Run build_corpus.py --download first.")
        sys.exit(1)

    init_db()
    conn = get_connection()

    # First, update existing tier assignments
    update_tiers(conn)

    words = []
    with open(FREQ_FILE, encoding="utf-8") as f:
        for i, line in enumerate(f, start=1):
            if i <= 20000:
                continue  # skip already-imported words
            parts = line.strip().split()
            if len(parts) >= 2:
                word = parts[0]
                freq = int(parts[1])
                words.append((word, freq, i))

    print(f"Adding {len(words)} frequency words (ranks 20001-50000)...")
    inserted = 0
    for word, freq, rank in words:
        if add_word_bulk(conn, word, rank, freq):
            inserted += 1
        if inserted % 5000 == 0 and inserted > 0:
            conn.commit()
            print(f"  ...{inserted} inserted")

    conn.commit()
    conn.close()
    print(f"Done: {inserted} new frequency words added.")


# ── Step 2: Curated regional slang ──────────────────────────────────

REGIONAL_SLANG = {
    "baiano": [
        # Interjeições e expressões
        "oxe", "oxente", "vixe", "eita", "eitaporra", "mainha", "painho",
        "mermo", "é mermo", "arretado", "arretada", "massa", "massinha",
        "barril", "barril dobrado", "zuada", "zueira", "migué", "dar migué",
        "laranjada", "piseiro", "pagodão", "axé", "acarajé", "abará",
        "vatapá", "caruru", "dendê", "tabuleiro", "baiana", "pelô",
        "pelourinho", "farol da barra", "barra", "pituba", "ondina",
        "itapuã", "cabula", "liberdade", "boca do rio", "brotas",
        "lá ele", "ôxe", "ôxi", "rapaz", "rapaiz", "mah", "mahvado",
        "porra", "desgramado", "desgraçado", "peste", "abestado",
        "abestalhado", "aperreado", "aperreio", "avexado", "avexamento",
        "besteira", "besteirol", "bichim", "bicho", "boy", "cabra",
        "cabra macho", "caba", "caô", "carniça", "chinfra", "coisa de louco",
        "danado", "danada", "demais da conta", "dismenino", "doido",
        "égua", "fio", "fulero", "gaiato", "home", "ixi", "jeito",
        "lascado", "lascou", "maneiro", "mano", "mei", "meladinha",
        "moleque", "moça", "moço", "muchacho", "neguim", "neguinha",
        "num", "num sei", "ó", "ôpa", "parada", "parça", "peba",
        "pegar firme", "pega leve", "preguiça", "qualé", "que nada",
        "quengo", "resenha", "rolar", "sacana", "sacanagem",
        "simbora", "tá ligado", "tá massa", "trem", "troço", "trucada",
        "véi", "véia", "vixi maria", "xibiu", "xingar", "xoxo",
        "zuado", "zoado", "zoeira", "mó", "bolado", "cabuloso",
        "paia", "firmeza", "suave", "de boa", "bora", "borimbora",
        "pegar onda", "se liga", "mancada", "vacilo", "vacilão",
        "pilantra", "malandragem", "malandro", "sangue bom", "sangue bão",
        "pivete", "piveta", "manja", "manjado", "presepada", "presepeiro",
        "tabacudo", "xibungo", "cachaceiro", "bebo", "bêbo", "cabaço",
        "cabaça", "coroa", "tiozão", "tiazinha", "mozão", "mozinha",
        "corre", "correria", "pegação", "rolê", "rolezinho", "baile",
        "pagode", "samba", "forró", "arrocha", "swingueira", "brega",
        "sofrência", "pisadinha",
    ],
    "carioca": [
        "mermão", "caraca", "caralho", "irado", "sinistro", "tu é doido",
        "comédia", "responsa", "responsa total", "mano", "maluco",
        "menor", "cria", "comunidade", "morro", "asfalto", "alemão",
        "complexo", "bonde", "bondar", "colar", "colou", "chegar junto",
        "papo reto", "é nóis", "firmeza", "sussa", "suave na nave",
        "talkey", "beleza", "bizu", "caô", "chapado", "chapou",
        "corre", "correria", "comédia", "de boas", "endoidou",
        "falou", "favela", "funkeiro", "galeris", "geral", "já era",
        "jão", "mala", "mané", "mó", "noia", "noinha", "parada",
        "parceiro", "plá", "playboy", "pretinho", "psé", "quente",
        "quietão", "rochedo", "saideira", "sem neurose", "sinistro",
        "suingue", "tá tranquilo", "tá favorável", "treta", "truta",
        "vacilão", "xerecão", "xibiu", "zika", "zikado", "bolado",
        "bolou", "brabeza", "brabo", "coroinha", "cuzão", "doidera",
        "embrazado", "embaçado", "fita", "gringo", "jacaré", "lacoste",
        "molecote", "novinha", "novinho", "patrão", "pique", "pomba",
        "porreta", "responsa", "sangue bom", "saravá", "sujeira",
        "talarico", "trombada", "visão", "vulgo",
    ],
    "paulista": [
        "mano", "mina", "trampo", "trampar", "trampando", "rolê",
        "balada", "biqueira", "biqueireiro", "brother", "cabeça",
        "caminhão", "cano", "chácara", "corre", "da hora", "daora",
        "de boa", "embaçado", "embassado", "fita", "firmeza",
        "grande abraço", "irado", "joia", "ligado", "louco", "maluco",
        "manja", "marginal", "meu", "meuzovo", "mó", "nóis",
        "orra meu", "pá", "parada", "parça", "perrengue", "perreco",
        "pica", "pira", "pirar", "pirou", "quebrada", "quebradinha",
        "rolar", "sampa", "sangue", "sinistro", "suave", "tá ligado",
        "treta", "truta", "vacilão", "zoar", "zoeira", "zica",
        "zicado", "bagunça", "bagulho", "baguio", "baita", "bolado",
        "brabo", "cachorro", "caô", "chave", "chavoso", "cria",
        "cruel", "dar um grau", "do nada", "é mole", "fechou",
        "fera", "firmão", "gato", "gata", "grau", "humildão",
        "ideia", "jogue", "ladrão", "loko", "maldade", "neguinho",
        "noção", "papo", "playboy", "psicose", "saída", "selva",
        "tá pago", "tipo", "trombada", "vacilo",
    ],
    "mineiro": [
        "uai", "sô", "trem", "trem bão", "trem ruim", "nó", "nô",
        "cê", "ocê", "pra mode", "mode", "cê tá doido", "ichi",
        "bão", "bão demais", "demais da conta", "mucado", "pior",
        "custoso", "gostoso demais", "lá pras banda", "levado da breca",
        "mineirim", "mineirinho", "angu", "tutu", "pão de queijo",
        "queijo minas", "goiabada", "romeu e julieta", "cachaça",
        "pinga", "cana", "caninha", "dosim", "dose", "buteco",
        "boteco", "barzinho", "prosa", "prosear", "conversa fiada",
        "bobeira", "bobagem", "fiasco", "fiasqueira", "fulagem",
        "fuleragem", "nhenhenhém", "porcaria", "trocim", "troquim",
        "mixuruca", "michuruca", "engomadim", "frescura", "fresco",
        "granjeiro", "capiau", "caipira", "da roça", "roceiro",
        "matuto", "jeca", "jecão", "mato", "morro", "grotão",
        "arraial", "quitanda", "quitandeira", "quitute", "cafundó",
        "bocaina", "cafuné", "dengoso", "dengosa", "meiguice",
        "neném", "criancinha", "danisco", "pestinha", "pentelha",
        "pentelho", "chato", "saco cheio", "encher o saco",
    ],
    "gaucho": [
        "bah", "tchê", "guri", "guria", "gurizão", "guriazinha",
        "tri", "tri legal", "barbaridade", "mas bah", "bah tchê",
        "capaz", "não capaz", "de mais", "bagual", "bergamota",
        "chimango", "churrasco", "churras", "galpão", "mate",
        "chimarrão", "cuia", "bomba", "erva", "erva-mate",
        "pilcha", "pilchado", "bombachas", "poncho", "laço",
        "rodeio", "prenda", "peão", "charqueada", "estância",
        "campanha", "fronteira", "coxilha", "bugio", "butuca",
        "facão", "fandango", "gaita", "gringo", "gaudério",
        "guasca", "invernada", "laçador", "matear", "mates",
        "parelho", "parelheiro", "patrona", "pelego", "piquete",
        "querência", "rincão", "tafona", "tropeiro", "vaqueano",
        "entrevero", "campeiro", "changa", "galponeira", "mateada",
        "gauchada", "baita", "abichornado", "apiá", "arvorado",
        "avoado", "bolacha", "carcará", "carneada", "china",
        "cocoreco", "costeiro", "cupincha", "desembestado", "entojado",
        "gauchismo", "guaiaca", "lomba", "macanudo", "pealo",
        "peleador", "redomão", "tordilho", "xucro",
    ],
    "nordestino_geral": [
        "visse", "macho", "cabra da peste", "arretado", "abestado",
        "aperreado", "aperreio", "avexado", "avexamento", "besteira",
        "bodega", "brocado", "broco", "buchada", "cabrito", "cacete",
        "calango", "capeta", "catingudo", "chibata", "cheiro",
        "cheirinho", "coisinha", "danado", "desgramado", "eita",
        "eitaporra", "enxerido", "enxerimento", "esculhambado",
        "esculhambar", "esculhambação", "fuxicar", "fuxico",
        "fuxico", "gabiru", "injuriado", "jerimum", "lasca",
        "lascado", "macaxeira", "mangaba", "mangar", "mangazo",
        "medonho", "menino", "menina", "mistura", "mofado",
        "moqueca", "mungunzá", "ôxe", "peba", "peido", "peidão",
        "pisa", "pisada", "quenga", "quengo", "rabudo", "rebuceteio",
        "safado", "safada", "safadeza", "sustança", "tapioca",
        "torada", "troço", "umbuzada", "xaxado", "xenhenhém",
        "xinxim", "xô", "zumbi", "cangaço", "cangaceiro",
        "forrozeiro", "forrozeira", "matuto", "matuta", "vaqueiro",
        "sertão", "sertanejo", "sertaneja", "agreste", "caatinga",
        "baião", "embolada", "repente", "repentista", "cantador",
        "cantadeira", "sanfoneiro", "sanfona", "zabumba", "triângulo",
    ],
    "nortista": [
        "égua", "é mano", "tu é", "pai d'égua", "maninho",
        "maninha", "mana", "cunhã", "curumim", "muiraquitã",
        "tucupi", "tacacá", "açaí", "cupuaçu", "guaraná",
        "jambu", "maniçoba", "pato no tucupi", "farinha",
        "farinhada", "farinha d'água", "tapioca", "beiju",
        "pirarucu", "tambaqui", "tucunaré", "jaraqui",
        "bodó", "pacu", "curimatã", "matrinxã", "pescada",
        "igarapé", "igapó", "várzea", "terra firme", "beiradão",
        "ribeirinho", "caboclo", "cabocla", "tapuio", "boto",
        "cobra grande", "curupira", "mapinguari", "matinta pereira",
        "saci", "iara", "boiúna", "uirapuru", "tucano",
        "arara", "papagaio", "garça", "jacaré", "onça",
        "capivara", "preguiça", "bicho-preguiça", "macaco",
        "anta", "paca", "cutia", "jabuti", "tracajá",
        "quelônio", "malhadeiro", "regatão", "rabeta", "batelão",
        "voadeira", "bajara", "montaria", "toco", "roçado",
        "maniva", "mandioca", "pupunha", "buriti", "babaçu",
        "seringueiro", "seringal", "castanheiro", "castanhal",
    ],
    "giria_jovem": [
        "mano", "mina", "crush", "stalkear", "shippar", "shippado",
        "biscoitar", "biscoiteiro", "biscoiteira", "lacrar", "lacrou",
        "arrasar", "arrasou", "mitou", "mitar", "bugou", "bugar",
        "cringe", "ranço", "rancoroso", "exposed", "cancelar",
        "cancelado", "textão", "lacração", "problematizar",
        "desconstruir", "empoderado", "empoderada", "sororidade",
        "lugar de fala", "gatilho", "tóxico", "tóxica", "red flag",
        "surtar", "surtado", "surtada", "pirar", "pirado", "pirada",
        "vibe", "bad", "good vibes", "rolê", "rolezinho", "baladeiro",
        "baladeira", "boladão", "boladona", "chavoso", "chavosa",
        "ostentação", "ostentar", "corre", "correria", "tá pago",
        "fechou", "tropa", "tropinha", "parça", "parceiro", "parceira",
        "broder", "migão", "migona", "amigão", "amigona",
        "stalker", "flopar", "flopou", "hype", "hypar", "trend",
        "trending", "viral", "viralizar", "viralizou", "meme",
        "memar", "zoar", "zoeira", "zuar", "zueira", "trollar",
        "trollou", "fake", "faker", "hater", "ranço",
        "boy lixo", "gado", "gadão", "gadear", "simp", "simpar",
        "crush", "ficante", "contatinho", "peguete", "affair",
        "relacionamento", "pegação", "ficada", "beijo",
    ],
    "coloquial_geral": [
        "ó", "olha só", "peraí", "peraê", "pô", "poxa", "putz",
        "caramba", "caraca", "meu deus", "nossa", "nossa senhora",
        "cruz credo", "deus me livre", "ave maria", "misericórdia",
        "pelo amor de deus", "valha-me deus", "tá brincando",
        "tá de sacanagem", "tá zuando", "tá de brincadeira",
        "sem chance", "nem pensar", "nem a pau", "nem ferrando",
        "nem fudendo", "tô fora", "fora essa", "nada a ver",
        "sei lá", "que sei eu", "tanto faz", "foda-se", "dane-se",
        "azar", "problema teu", "culpa tua", "faz parte",
        "bola pra frente", "segue o jogo", "segue o baile",
        "vida que segue", "deixa quieto", "deixa pra lá",
        "esquece", "bora", "partiu", "vamo", "vambora", "simbora",
        "se joga", "manda ver", "mete bronca", "vai fundo",
        "chega mais", "cola aqui", "dá um salve", "salve",
        "e aí", "beleza", "suave", "firmeza", "tranquilo",
        "na boa", "de boa", "sem estresse", "relax", "relaxa",
        "calma", "calma aí", "esfria", "fica frio", "fica tranquilo",
        "segura", "aguenta", "se garante", "manda bem", "mandou bem",
        "show", "showzaço", "sensacional", "genial", "brabo",
        "top", "topzera", "irado", "insano", "animal", "bizarro",
        "absurdo", "surreal", "demais", "pra caramba", "pra caralho",
        "pra burro", "pacas", "à beça", "à toa", "de graça",
        "na faixa", "na maciota", "mamão com açúcar", "barbada",
        "mole mole", "fácil fácil", "fichinha", "café pequeno",
        "osso", "roubada", "furada", "cilada", "pegadinha",
        "sacanagem", "putaria", "palhaçada", "mancada", "vacilo",
        "furo", "gafe", "mico", "pagou mico", "passou vergonha",
        "quebrou a cara", "deu ruim", "deu merda", "deu zebra",
        "deu chabu", "bugou", "zuou", "ferrou", "fodeu", "lascou",
    ],
}

# Assign slang to rank ranges by category
SLANG_RANK_START = 50001


def add_slang_words():
    """Insert curated regional slang into word_bank."""
    init_db()
    conn = get_connection()
    update_tiers(conn)

    rank = SLANG_RANK_START
    inserted = 0
    skipped = 0

    for region, words in REGIONAL_SLANG.items():
        region_inserted = 0
        for word in words:
            word = word.strip().lower()
            if not word:
                continue
            if add_word_bulk(conn, word, rank):
                inserted += 1
                region_inserted += 1
                rank += 1
            else:
                skipped += 1
        print(f"  {region}: {region_inserted} new words")

    conn.commit()
    conn.close()
    print(f"Done: {inserted} slang words added, {skipped} duplicates skipped.")
    return rank  # return next available rank


# ── Step 3: GPT-4o generated word batches ────────────────────────────

GPT_CATEGORIES = [
    {
        "name": "verb_conjugations",
        "prompt": (
            "Lista 500 conjugações de verbos brasileiros comuns que faltam num "
            "dicionário de 50.000 palavras. Inclui formas coloquiais como "
            "'tô', 'tá', 'cê', 'peraí', 'vambora', contrações usadas no dia a dia. "
            "Inclui gerúndio, particípio, subjuntivo, imperativo. "
            "Uma palavra por linha, sem números, sem explicações."
        ),
    },
    {
        "name": "augmentatives_diminutives",
        "prompt": (
            "Lista 500 aumentativos e diminutivos brasileiros comuns: "
            "-ão, -ona, -aço, -inho, -inha, -zinho, -zinha, -ito, -ita. "
            "Ex: casarão, mulherona, amigão, gatinha, pertinho, rapidinho, "
            "calminha, belezinha, cervejinha. Uma por linha."
        ),
    },
    {
        "name": "food_culture",
        "prompt": (
            "Lista 500 palavras brasileiras de comida, bebida, culinária regional, "
            "e cultura gastronômica de todas as regiões: Bahia (acarajé, dendê, "
            "vatapá), Minas (pão de queijo, tutu), Sul (churrasco, chimarrão), "
            "Norte (açaí, tucupi, tacacá), São Paulo (pastel, coxinha), "
            "Rio (biscoito globo). Inclui ingredientes, pratos, temperos, "
            "modo de preparo, utensílios. Uma por linha."
        ),
    },
    {
        "name": "music_dance",
        "prompt": (
            "Lista 500 palavras brasileiras de música, dança, ritmos, e cultura "
            "musical: samba, pagode, axé, forró, brega, funk carioca, piseiro, "
            "arrocha, MPB, bossa nova, sertanejo, rap nacional. "
            "Inclui instrumentos, passos de dança, gêneros, gírias musicais, "
            "nomes de ritmos. Uma por linha."
        ),
    },
    {
        "name": "sports_futebol",
        "prompt": (
            "Lista 500 palavras brasileiras de esporte, especialmente futebol: "
            "gírias de torcida, posições, jogadas (drible, chapéu, caneta, "
            "elástico, lambreta, bicicleta), xingamentos de juiz, "
            "nomes de estádios famosos, cultura de arquibancada. "
            "Inclui também capoeira, surfe, vôlei de praia. Uma por linha."
        ),
    },
    {
        "name": "daily_life",
        "prompt": (
            "Lista 500 palavras do cotidiano brasileiro que faltam num dicionário "
            "padrão: transporte (ônibus, metrô, lotação, uber, moto-táxi), "
            "moradia (barraco, kitnet, quitinete, república, pensão), "
            "trabalho (trampo, bico, freela, CLT, PJ), dinheiro (grana, bufunfa, "
            "trocado, pila, conto, pau), documentos (RG, CPF, carteirinha). "
            "Uma por linha."
        ),
    },
    {
        "name": "internet_tech",
        "prompt": (
            "Lista 500 palavras brasileiras de internet, tecnologia, redes sociais: "
            "gírias de WhatsApp, Instagram, TikTok, Twitter/X, YouTube. "
            "Inclui: zap, zapzap, direct, stories, feed, like, curtir, "
            "compartilhar, repostar, seguir, bloquear, printar, print, "
            "áudio, figurinha, sticker, meme, viral, trending, lacrar. "
            "Uma por linha."
        ),
    },
    {
        "name": "emotions_personality",
        "prompt": (
            "Lista 500 palavras brasileiras para emoções, estados de espírito, "
            "e traços de personalidade. Coloquial e formal: triste, tristeza, "
            "cabisbaixo, pra baixo, na fossa, deprê, animado, empolgado, "
            "elétrico, ligadão, estressado, pistola, putasso, bravo, "
            "gente boa, gente fina, chato, mala, sem noção, palhaço, "
            "metido, esnobe, humilde, humildão. Uma por linha."
        ),
    },
    {
        "name": "nature_geography",
        "prompt": (
            "Lista 500 palavras brasileiras de natureza, geografia, clima, "
            "flora e fauna: biomas (cerrado, caatinga, pantanal, mata atlântica, "
            "amazônia), animais (onça, capivara, arara, tucano, jacaré), "
            "plantas (ipê, pau-brasil, babaçu, buriti, seringueira), "
            "geografia (serra, chapada, grota, morro, ribeirão, córrego, "
            "cachoeira, represa). Uma por linha."
        ),
    },
    {
        "name": "religion_spirituality",
        "prompt": (
            "Lista 500 palavras brasileiras de religião e espiritualidade: "
            "candomblé (orixá, terreiro, pai de santo, mãe de santo, ebó, "
            "axé, Oxalá, Iemanjá, Ogum, Xangô, Oxóssi, Oxum, Exu, Pomba Gira), "
            "umbanda, espiritismo (kardecismo, médium, incorporação, centro), "
            "catolicismo popular (romaria, promessa, santo, festa junina, "
            "são joão, quadrilha), evangélico (culto, louvor, dízimo, "
            "congregação). Uma por linha."
        ),
    },
    {
        "name": "body_health",
        "prompt": (
            "Lista 500 palavras brasileiras de corpo, saúde, medicina popular: "
            "partes do corpo (coloquial: bunda, peito, barriga, canela, "
            "panturrilha, sovaco), doenças (gripe, resfriado, dengue, "
            "dor de barriga, enxaqueca), remédios populares (chá, garrafada, "
            "benzedeira, simpatia, reza), exercício (academia, musculação, "
            "malhação, shape, definido, sarado, gostoso). Uma por linha."
        ),
    },
    {
        "name": "slang_expressions_extra",
        "prompt": (
            "Lista 500 expressões e gírias brasileiras atuais (2020-2026) "
            "que jovens usam no dia a dia. Inclui gírias de periferia, "
            "de internet, de balada, de relacionamento. "
            "Ex: ghostar, dar ghost, breadcrumbing, benching, "
            "orbitar, stalkear, dar match, flertar, paquerar, "
            "dar em cima, chegar junto, ficar, rolo, contatinho, "
            "crush, mozão, mozinha, flerte, paquera, affair, "
            "date, encontro, rolê, pegação. Uma por linha."
        ),
    },
]


def generate_words_batch(category, start_rank):
    """Call GPT-4o to generate a batch of words."""
    client = openai.OpenAI()

    system = (
        "Tu é um linguista especializado em português brasileiro. "
        "Tua função é gerar listas de palavras brasileiras. "
        "REGRAS: Uma palavra ou expressão curta por linha. "
        "Sem números, sem explicações, sem repetições. "
        "Inclui todas as regiões do Brasil."
    )

    try:
        resp = client.chat.completions.create(
            model="gpt-4o",
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": category["prompt"]},
            ],
            temperature=0.9,
            max_tokens=4096,
        )
        text = resp.choices[0].message.content
        words = []
        for line in text.strip().split("\n"):
            w = line.strip().strip("-•·0123456789. ").lower()
            if w and len(w) < 60 and not w.startswith("#"):
                words.append(w)
        return words
    except Exception as e:
        print(f"  Error generating {category['name']}: {e}")
        return []


def generate_all_words():
    """Generate words via GPT-4o for all categories."""
    init_db()
    conn = get_connection()
    update_tiers(conn)

    # Find the next available rank
    max_rank = conn.execute("SELECT MAX(frequency_rank) FROM word_bank").fetchone()[0]
    rank = max(max_rank + 1, 55001) if max_rank else 55001

    total_inserted = 0

    for cat in GPT_CATEGORIES:
        print(f"\nGenerating: {cat['name']}...")
        words = generate_words_batch(cat, rank)
        inserted = 0
        for w in words:
            if add_word_bulk(conn, w, rank):
                inserted += 1
                rank += 1
        conn.commit()
        total_inserted += inserted
        print(f"  → {inserted} new words (of {len(words)} generated)")
        time.sleep(1)  # rate limit courtesy

    conn.close()
    print(f"\nTotal GPT-generated words added: {total_inserted}")


# ── Status ───────────────────────────────────────────────────────────

def show_status():
    conn = get_connection()
    total = conn.execute("SELECT COUNT(*) FROM word_bank").fetchone()[0]
    print(f"\nTotal words in word_bank: {total:,}")
    print(f"Target: 100,000\n")
    for tier in range(1, 7):
        count = conn.execute(
            "SELECT COUNT(*) FROM word_bank WHERE difficulty_tier = ?", (tier,)
        ).fetchone()[0]
        print(f"  Tier {tier}: {count:,} words")

    # Check for specific slang
    print("\nSlang spot-check:")
    for test in ["oxe", "oxente", "vixe", "uai", "tchê", "mermão", "trampo", "égua"]:
        row = conn.execute(
            "SELECT id, frequency_rank, difficulty_tier FROM word_bank WHERE word = ?",
            (test,)
        ).fetchone()
        if row:
            print(f"  ✓ '{test}' — rank {row[1]}, tier {row[2]}")
        else:
            print(f"  ✗ '{test}' — NOT FOUND")

    conn.close()


# ── Main ─────────────────────────────────────────────────────────────

def main():
    if len(sys.argv) < 2:
        print(__doc__.strip())
        sys.exit(1)

    arg = sys.argv[1]
    if arg == "--frequency":
        add_frequency_words()
    elif arg == "--slang":
        add_slang_words()
    elif arg == "--generate":
        generate_all_words()
    elif arg == "--all":
        add_frequency_words()
        add_slang_words()
        generate_all_words()
        show_status()
    elif arg == "--status":
        show_status()
    else:
        print(__doc__.strip())
        sys.exit(1)


if __name__ == "__main__":
    main()
