#!/usr/bin/env python3
"""
expand_100k.py — Expand word bank to 100K with maximum throughput.

Uses highly specific sub-categories to minimize duplicates, 100 words per batch,
and parallel generation + caching.

Usage:
    source ~/.profile && python3 -u expand_100k.py
"""

import json
import os
import sqlite3
import sys
import time
import random
from concurrent.futures import ThreadPoolExecutor, as_completed

import openai

DB = os.path.join(os.path.dirname(__file__), "voca_20k.db")
TARGET = 100000

# Highly specific sub-categories to maximize unique word yield
SUBCATEGORIES = [
    # ── Regional slang (deep cuts) ──
    ("Gírias baianas de rua", "gírias de rua de Salvador que só baiano entende — termos de Pelourinho, Liberdade, Itapuã, Barra", 5),
    ("Gírias baianas de comida", "nomes de comidas, ingredientes, temperos e pratos típicos da Bahia — acarajé, vatapá, moqueca, abará, etc", 4),
    ("Gírias do carnaval baiano", "vocabulário do carnaval de Salvador — bloco, trio elétrico, pipoca, camarote, abadá, axé", 5),
    ("Candomblé e religião afro", "termos do candomblé, umbanda e religiões afro-brasileiras — orixá, terreiro, ebó, axé, atabaque", 5),
    ("Capoeira vocabulário", "golpes, movimentos, instrumentos e termos da capoeira — berimbau, ginga, meia-lua, roda, mestre", 5),
    ("Gírias SP zona leste", "gírias da periferia de São Paulo — termos de quebrada, favela, baile funk, pivetada", 6),
    ("Gírias SP corporativo", "jargão corporativo paulistano — startup, coworking, networking, pitch, sprint, daily", 4),
    ("Gírias RJ praia e surf", "vocabulário de praia, surf, bodyboard e vida litorânea carioca", 5),
    ("Gírias RJ funk", "vocabulário do funk carioca — baile, passinho, DJ, MC, bonde, putaria (termos reais)", 6),
    ("Expressões MG do interior", "termos do interior de Minas — queijo, fazenda, roça, vocabulário rural mineiro", 4),
    ("Expressões MG urbanas BH", "gírias de Belo Horizonte — bar, buteco, happy hour, pão de queijo, mineiridade", 5),
    ("Gauchismos puro", "vocabulário gaúcho puro — chimarrão, churrasco, galpão, estância, peão, laço", 5),
    ("Gírias Curitiba e Paraná", "expressões paranaenses — piá, guri, de boa, vocabulário curitibano", 5),
    ("Gírias Floripa e SC", "expressões catarinenses — manezinho, Floripa, praia, ostras, vocabulário ilhéu", 5),
    ("Gírias Pará e Amazônia", "expressões paraenses — égua, mano, açaí, tacacá, farinha, vocabulário amazônico", 5),
    ("Gírias Manaus", "expressões de Manaus e Amazonas — termos ribeirinhos, peixes, frutas amazônicas", 5),
    ("Gírias Ceará", "expressões cearenses — abestado, arretado, cabra, rapadura, vocabulário fortalezense", 5),
    ("Gírias Pernambuco", "expressões pernambucanas — oxente, maracatu, frevo, vocabulário recifense", 5),
    ("Gírias Maranhão", "expressões maranhenses — bumba-meu-boi, vocabulário de São Luís", 5),
    ("Gírias Goiás e Centro-Oeste", "expressões goianas e do cerrado — trem, uai, sertanejo, agronegócio", 5),
    ("Gírias Brasília", "expressões brasilienses — quadra, setor, candango, vocabulário do DF", 5),

    # ── Practical domains (deep vocabulary) ──
    ("Finanças pessoais", "vocabulário detalhado de finanças — investimento, poupança, CDB, Tesouro Direto, FII, dividendo, IPCA", 4),
    ("Mercado imobiliário BR", "termos de imóveis no Brasil — escritura, ITBI, financiamento, parcela, amortização, Minha Casa", 4),
    ("Empreendedorismo BR", "vocabulário de abrir e gerir negócio no Brasil — CNPJ, MEI, Simples Nacional, nota fiscal, alvará", 4),
    ("Direito e burocracia BR", "termos jurídicos e burocráticos do dia-a-dia — cartório, procuração, RG, CPF, habilitação, boletim de ocorrência", 3),
    ("Namoro e paquera moderna", "vocabulário de apps de namoro, paquera, relacionamentos — crush, match, ghosting em PT, ficar, namorar, rolo", 5),
    ("Casamento e família BR", "vocabulário de casamento, família brasileira — sogra, cunhado, enteado, pensão, guarda, divórcio", 3),
    ("Gravidez e bebê", "vocabulário de gravidez, parto, bebê — ultrassom, pré-natal, berço, mamadeira, fralda, pediatra", 3),
    ("Corpo humano detalhado", "partes do corpo, órgãos, sistemas — fígado, rim, pulmão, cotovelo, canela, nuca, omoplata", 3),
    ("Doenças e sintomas", "doenças comuns, sintomas, remédios — gripe, dengue, febre, tosse, dor de cabeça, antibiótico", 3),
    ("Academia e fitness", "vocabulário de academia — supino, agachamento, esteira, whey, treino, séries, repetições", 4),
    ("Futebol vocabulário", "termos de futebol brasileiro — impedimento, escanteio, pênalti, drible, golaço, gandula", 4),
    ("Outros esportes BR", "vocabulário de vôlei, basquete, MMA, surfe, skate, atletismo no Brasil", 4),
    ("Cozinha e receitas", "verbos e termos culinários — refogar, dourar, escaldar, banho-maria, fogão, panela de pressão", 3),
    ("Ingredientes brasileiros", "ingredientes típicos brasileiros — mandioca, dendê, tucupi, jambu, pequi, buriti, cupuaçu", 4),
    ("Bebidas brasileiras", "cachaça, caipirinha, cerveja, suco, vitamina, mate, guaraná, termos de bar", 4),
    ("Roupas e moda", "vocabulário de roupas, moda, sapatos — bermuda, chinelo, havaianas, regata, moletom", 3),
    ("Casa e móveis", "vocabulário doméstico — sofá, geladeira, fogão, ventilador, chuveiro, torneira, pia", 3),
    ("Carro e mecânica", "vocabulário automotivo — embreagem, marcha, freio, pneu, oficina, lanternagem, funilaria", 4),
    ("Tecnologia e apps", "termos tech em PT-BR — aplicativo, notificação, atualização, senha, login, configuração", 3),
    ("Redes sociais BR", "vocabulário de redes sociais — stories, feed, curtir, compartilhar, seguidor, engajamento", 4),
    ("Música brasileira gêneros", "termos musicais — pagode, forró, sertanejo, brega, MPB, maracatu, baião, repente", 5),
    ("Instrumentos musicais", "instrumentos brasileiros e gerais — pandeiro, zabumba, sanfona, cavaquinho, atabaque, agogô", 4),
    ("Natureza e animais BR", "fauna e flora brasileira — onça, tucano, arara, jacaré, capivara, ipê, baobá, jequitibá", 4),
    ("Clima e geografia BR", "termos geográficos e climáticos — sertão, cerrado, mangue, caatinga, pantanal, chapada", 4),
    ("Festas e feriados", "vocabulário de festas brasileiras — São João, Natal, Réveillon, Festa Junina, quadrilha, fogueira", 4),
    ("Educação e escola", "vocabulário escolar — vestibular, ENEM, faculdade, matrícula, bolsa, prova, nota, redação", 3),
    ("Transporte público BR", "vocabulário de transporte — ônibus, metrô, bilhete único, Uber, mototáxi, lotação, barca", 3),
    ("Construção civil", "termos de obra e construção — pedreiro, massa, reboco, alicerce, laje, telha, encanamento", 4),
    ("Agricultura e agro", "vocabulário agrícola — safra, colheita, plantio, irrigação, soja, café, pecuária, gado", 4),
    ("Política brasileira", "termos políticos — deputado, senador, vereador, prefeito, urna, voto, impeachment, CPI", 4),
    ("Economia e mercado", "termos econômicos — inflação, PIB, Selic, câmbio, dólar, bolsa, ação, dividendo", 4),
    ("Internet e memes BR", "vocabulário de internet brasileira — mitada, lacrar, cancelar, exposed, textão, biscoiteiro", 6),
    ("Palavrões regionais", "xingamentos e palavrões de diferentes regiões do Brasil — cada região tem os seus", 6),
    ("Expressões de tempo", "expressões temporais brasileiras — agora pouco, daqui a pouco, outro dia, semana que vem, faz tempo", 3),
    ("Expressões de quantidade", "expressões de quantidade — um monte, um bocado, uma porrada, um tantão, uma caralhada", 4),
    ("Conectivos e preenchimentos", "palavras de ligação e preenchimento — tipo, tipo assim, sei lá, enfim, aliás, inclusive", 3),
    ("Onomatopeias BR", "onomatopeias brasileiras — tchau, psiu, ué, ih, eita, ufa, opa, ahn, hein", 4),
    ("Verbos informais", "verbos coloquiais — zoar, curtir, rolar, trampar, meter, colar, desenrolar, lacrar", 4),
    ("Adjetivos coloquiais", "adjetivos informais — massa, top, firmeza, suave, de boa, sinistro, brabo, foda", 5),
    ("Profissões brasileiras", "profissões comuns — pedreiro, motorista, frentista, caixa, garçom, porteiro, faxineira", 3),
    ("Animais domésticos", "vocabulário de pets — cachorro, gato, ração, veterinário, coleira, castração, vacina", 3),
    ("Compras e comércio", "vocabulário de compras — promoção, desconto, parcela, boleto, pix, troco, nota fiscal", 3),
    ("Saúde mental", "vocabulário de saúde mental — ansiedade, depressão, terapia, psicólogo, síndrome, burnout", 4),
    ("Viagem e turismo BR", "vocabulário de viagem — passagem, hospedagem, pousada, mochilão, trilha, cachoeira", 3),
    ("Praia e litoral", "vocabulário de praia — onda, maré, areia, protetor solar, guarda-sol, boia, canga", 3),
    ("Favela e comunidade", "vocabulário de comunidade — beco, viela, barraco, laje, biqueira, bonde, cria", 6),
    ("Sertanejo e interior", "vocabulário do interior do Brasil — viola, modão, rodeio, peão, laço, boiada, porteira", 5),
    ("Termos médicos populares", "como brasileiros chamam procedimentos médicos — tirar sangue, fazer exame, bater chapa, tomar soro", 3),
    ("Gírias de jovens 2024", "gírias atuais da juventude brasileira — slay, ate, vibes, based, NPC, main character em PT", 6),
    ("Expressões de concordância", "formas de concordar — é isso aí, falou, fechou, pode crer, exato, com certeza, sem dúvida", 3),
    ("Expressões de discordância", "formas de discordar — que nada, nada a ver, tá doido, nem ferrando, de jeito nenhum", 4),
    ("Expressões de surpresa", "reações de surpresa — oxe, eita, caramba, nossa, meu Deus, misericórdia, Ave Maria", 4),
    ("Termos de trânsito", "vocabulário de trânsito — semáforo, rotatória, radar, multa, CNH, balão, retorno, lombada", 3),
    ("Termos bancários", "vocabulário bancário — conta corrente, extrato, transferência, TED, DOC, pix, saque, depósito", 3),
    ("Termos de aluguel", "vocabulário de aluguel — fiador, caução, contrato, reajuste, condomínio, IPTU, vistoria", 4),
]

SYSTEM = (
    "Tu é um linguista especialista em português brasileiro falado. "
    "Gera listas de palavras e expressões REAIS usadas no dia-a-dia. "
    "NUNCA inventa palavras. Todas devem ser usadas por brasileiros de verdade. "
    "Foca em palavras DIFERENTES das comuns — vai no específico, no regional, no técnico. "
    "Responde SOMENTE em JSON."
)

_client = None
def get_client():
    global _client
    if _client is None:
        _client = openai.OpenAI(max_retries=2, timeout=60)
    return _client


def get_existing_words():
    conn = sqlite3.connect(DB)
    rows = conn.execute("SELECT LOWER(word) FROM word_bank").fetchall()
    conn.close()
    return set(r[0] for r in rows)


def get_max_rank():
    conn = sqlite3.connect(DB)
    row = conn.execute("SELECT MAX(frequency_rank) FROM word_bank").fetchone()
    conn.close()
    return row[0] or 67092


def generate_batch(name, prompt_desc, existing, batch_size=100):
    """Generate a batch of new words via GPT-4o-mini."""
    client = get_client()

    # Send a random sample of existing words in this domain to avoid
    sample = random.sample(list(existing), min(300, len(existing)))
    exclude_str = ", ".join(sample)

    prompt = f"""Gera uma lista de EXATAMENTE {batch_size} palavras/expressões de português brasileiro.

CATEGORIA: {name}
FOCO: {prompt_desc}

REGRAS:
- Cada item deve ser uma palavra ou expressão REAL usada por brasileiros
- Inclui palavras simples E expressões de 2-5 palavras
- Prioriza palavras ESPECÍFICAS e INCOMUNS — não as óbvias
- NUNCA inclui palavras em inglês puro
- NÃO repete nenhuma destas: {exclude_str}

Responde em JSON:
{{"words": [
  {{"word": "palavra ou expressão", "definition": "definição curta"}},
  ...
]}}"""

    try:
        resp = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[
                {"role": "system", "content": SYSTEM},
                {"role": "user", "content": prompt},
            ],
            response_format={"type": "json_object"},
            temperature=0.95,
            max_tokens=8192,
        )
        data = json.loads(resp.choices[0].message.content)
        return data.get("words", [])
    except Exception as e:
        print(f"  [ERROR] {name}: {e}")
        return []


def insert_words(words, tier, existing):
    """Insert new words into word_bank. Returns count inserted."""
    conn = sqlite3.connect(DB)
    max_rank = get_max_rank()
    inserted = 0
    new_card = json.dumps({
        "due": "2000-01-01T00:00:00+00:00",
        "stability": 0.0, "difficulty": 0.0,
        "elapsed_days": 0, "scheduled_days": 0,
        "reps": 0, "lapses": 0, "state": 0, "last_review": None,
    })

    for w in words:
        word = w.get("word", "").strip().lower()
        if not word or word in existing or len(word) < 2:
            continue
        max_rank += 1
        try:
            conn.execute(
                "INSERT OR IGNORE INTO word_bank (word, frequency_rank, frequency_count, difficulty_tier, srs_state) VALUES (?, ?, 0, ?, ?)",
                (word, max_rank, tier, new_card),
            )
            existing.add(word)
            inserted += 1
        except Exception:
            pass

    conn.commit()
    conn.close()
    return inserted


def cache_new_words():
    """Cache dictionary tabs for any uncached words."""
    from precache_fast import get_uncached_words, cache_word_bulk

    words = get_uncached_words()
    if not words:
        return 0

    print(f"\n  [CACHE] {len(words)} words to cache...")
    ok = 0
    fail = 0
    done = 0
    total = len(words)

    with ThreadPoolExecutor(max_workers=50) as pool:
        futures = {pool.submit(cache_word_bulk, wid, w): w for wid, w in words}
        for f in as_completed(futures):
            try:
                c, f2 = f.result()
                ok += c
                fail += f2
            except Exception:
                fail += 1
            done += 1
            if done % 200 == 0 or done == total:
                print(f"    [{done}/{total}] {ok} tabs ok, {fail} fail")

    print(f"  [CACHE] Done: {ok} tabs, {fail} failed")
    return ok


def main():
    print(f"[expand_100k] Target: {TARGET:,} words")

    round_num = 0
    while True:
        existing = get_existing_words()
        current = len(existing)
        remaining = TARGET - current

        if remaining <= 0:
            print(f"\n[expand_100k] TARGET REACHED! {current:,} words in bank.")
            break

        round_num += 1
        print(f"\n{'='*60}")
        print(f"[Round {round_num}] Current: {current:,} | Remaining: {remaining:,}")
        print(f"{'='*60}")

        # Shuffle categories each round to get variety
        cats = list(SUBCATEGORIES)
        random.shuffle(cats)

        # Generate words in parallel (5 concurrent API calls)
        total_inserted = 0
        batch_results = []

        with ThreadPoolExecutor(max_workers=5) as pool:
            futures = {}
            for name, desc, tier in cats:
                if total_inserted >= min(remaining, 2000):
                    break
                f = pool.submit(generate_batch, name, desc, existing, 100)
                futures[f] = (name, tier)

            for f in as_completed(futures):
                name, tier = futures[f]
                try:
                    words = f.result()
                    if words:
                        inserted = insert_words(words, tier, existing)
                        total_inserted += inserted
                        print(f"  [{name}] +{inserted} ({len(words)} gen, {len(words)-inserted} dup)")
                except Exception as e:
                    print(f"  [{name}] ERROR: {e}")

        print(f"\n[Round {round_num}] Inserted: {total_inserted}")

        if total_inserted > 0:
            cache_new_words()

        new_total = len(get_existing_words())
        print(f"[Round {round_num}] Word bank: {current:,} → {new_total:,} (+{new_total - current})")

        if total_inserted < 50:
            print("[expand_100k] Yield too low, adding more specific categories...")
            # Dynamically add more specific subcategories
            SUBCATEGORIES.extend([
                (f"Vocabulário específico round {round_num} pt1",
                 "palavras raras mas reais do português brasileiro — termos técnicos, regionalismos obscuros, gírias antigas", 5),
                (f"Vocabulário específico round {round_num} pt2",
                 "expressões multipalavra do português brasileiro — locuções verbais, adverbiais, prepositivas", 4),
                (f"Vocabulário específico round {round_num} pt3",
                 "substantivos compostos, verbos pronominais e palavras derivadas do português brasileiro", 4),
            ])


if __name__ == "__main__":
    main()
