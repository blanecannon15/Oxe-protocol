#!/usr/bin/env python3
"""Priority-cache all slang words with 7 dictionary tabs."""
import sqlite3, sys, time, os
sys.path.insert(0, os.path.dirname(__file__))
from dictionary_engine import (
    get_definition_cached, get_examples_cached, get_pronunciation_cached,
    get_expressions_cached, get_conjugation_cached, get_synonyms_cached,
    get_word_chunks_cached
)

DB = os.path.join(os.path.dirname(__file__), "voca_20k.db")
TABS = ['definition','examples','pronunciation','expressions','conjugation','synonyms','chunks']
TAB_FNS = {
    'definition': get_definition_cached,
    'examples': get_examples_cached,
    'pronunciation': get_pronunciation_cached,
    'expressions': get_expressions_cached,
    'conjugation': get_conjugation_cached,
    'synonyms': get_synonyms_cached,
    'chunks': get_word_chunks_cached,
}

SLANG = ['oxe','massa','arretado','vixe','lá ele','tá ligado','mano','véi','parada','bagulho','trampo','mó','firmeza','suave','da hora','sinistro','brabo','paia','vacilão','zica','corre','grau','bonde','quebrada','responsa','chavoso','perrengue','rolê','cabuloso','treta','migué','gato','pegar','ficar','rolar','mandar ver','se liga','valeu','falou','é nóis','tmj','bora','partiu','simbora','eita','ôxe','misericórdia','ave maria','cruz credo','meu deus','vish','caramba','putz','nossa','puts','ih','ué','opa','oi','ei','psiu','po','pô','rapaz','cara','brother','parceiro','chapa','chegado','camarada','irmão','mermão','maluco','doido','bicho','nego','mina','gata','coroa','véia','tio','mozão','bichinho','fofo','lindão','lindeza','top','show','irado','demais','pra caramba','animal','insano','monstro','foda','do caralho','pica','fera','craque','mito','lenda','rei','rainha','diva','lacrar','arrasar','detonar','mandar bem','dar um show','abafar','causar','impressionar','botar pra quebrar','meter bronca','dar conta','aguentar','segurar a onda','dar um jeito','se virar','desenrolar','meu rei','minha fia','mó doido','cê ligou','que é isso','aff','tá bonzin','eita lasqueira','qual é','fala tu','mó da hora','climão']

def main():
    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row

    placeholders = ','.join(['?' for _ in SLANG])
    rows = conn.execute(f'SELECT id, word FROM word_bank WHERE word IN ({placeholders})', SLANG).fetchall()

    total_generated = 0
    total_skipped = 0

    for i, row in enumerate(rows):
        wid, word = row['id'], row['word']
        # Check which tabs are missing
        cached = set(r[0] for r in conn.execute(
            'SELECT tab_name FROM dictionary_cache WHERE word_id=?', (wid,)).fetchall())
        missing = [t for t in TABS if t not in cached]

        if not missing:
            total_skipped += 1
            continue

        print(f"[{i+1}/{len(rows)}] {word} — {len(missing)} tabs missing: {', '.join(missing)}")

        for tab in missing:
            fn = TAB_FNS[tab]
            try:
                result = fn(wid, word, DB)
                if result:
                    total_generated += 1
                    print(f"  ✓ {tab}")
                else:
                    print(f"  ✗ {tab} (empty)")
                time.sleep(0.3)
            except Exception as e:
                print(f"  ✗ {tab}: {e}")
                time.sleep(1)

    conn.close()
    print(f"\nDone: {total_generated} tabs generated, {total_skipped} words already complete")

if __name__ == '__main__':
    main()
