"""
drill_loop.py — The Oxe Protocol automaticity loop.

Orchestrates the full 1+T review cycle:
  1. Query next word from SRS
  2. Generate chunk + carrier sentence (Soteropolitano context)
  3. Generate image via DALL-E (conceptual, no text)
  4. Generate audio via ElevenLabs (Multilingual v3, Baiano voice)
  5. Play audio, show image
  6. Wait for shadowing response
  7. Score pronunciation via biometric_checker
  8. Log latency + rating to SRS

Also handles:
  - Chorusing protocol (score < 85 → repeat with native model)
  - Shadowing drills (200ms delay repetition)
  - Minimal-pair drills for ão/lh errors
  - Pragmatic Trap mode (hidden double meanings → test cultural reflex)

Usage:
    python3 drill_loop.py              Run one review cycle
    python3 drill_loop.py --session N  Run N review cycles
    python3 drill_loop.py --shadow     Shadowing mode (rapid-fire)
    python3 drill_loop.py --chorus ID  Chorusing drill for specific word
    python3 drill_loop.py --trap       Run a single pragmatic trap
"""

import json
import os
import subprocess
import sys
import time
import random
from datetime import datetime
from pathlib import Path

from fsrs import Rating

from srs_engine import (
    get_next_word, get_due_words, record_review,
    get_unlocked_tier, tier_progress, TIER_LABELS, DB_PATH
)

AUDIO_DIR = Path(__file__).parent / "voca_vault" / "audios"
IMAGE_DIR = Path(__file__).parent / "voca_vault" / "images"
LOG_DIR = Path(__file__).parent / "voca_vault" / "logs"

NATIVENESS_THRESHOLD = 85
LATENCY_THRESHOLD_MS = 1500
TRAP_LATENCY_MS = 800       # Must react to trap within 800ms
TRAP_PROBABILITY = 0.15     # 15% chance of a trap during a session cycle
LARANJADA_PENALTY_COUNT = 5 # Number of next chunks to make harder after a laranjada

# Valid trap reactions — the Baiano reflex responses to absurdity / double meanings
TRAP_REACTIONS = {"lá ele", "la ele", "oxe", "oxente", "eita", "vixe"}

# Baiano sentence-building elements
INTERJECTIONS = [
    "Oxe,", "Vixe,", "Rapaz,", "Eita,", "Ô meu,", "Ave Maria,",
    "Misericórdia,", "Ô xente,", "Meu irmão,",
]

TAGS = [
    "viu!", "visse.", "tá ligado?", "meu irmão.", "rapaz.",
    "né?", "é mermo.", "sabe como é.", "acredita?",
]

LOCATIONS = [
    "no Pelourinho", "lá no Rio Vermelho", "na Barra", "em Itapuã",
    "no Candeal", "no Comércio", "na Ribeira", "na Pituba",
    "no Campo Grande", "na Liberdade", "no Bonfim",
]

# Carrier sentence templates — {word} is replaced with the target
CARRIER_TEMPLATES = [
    "{intj} tu sabe o que é {word}? {tag}",
    "{intj} ontem eu vi um negócio de {word} {loc}, {tag}",
    "Eu tava pensando em {word} agora mesmo, {tag}",
    "{intj} {word} é uma coisa que todo baiano conhece, {tag}",
    "Tu já ouviu falar de {word}? {tag}",
    "{intj} me lembra de {word} quando eu era moleque, {tag}",
    "A gente sempre fala de {word} {loc}, {tag}",
    "{intj} sem {word} não dá pra viver, {tag}",
    "Todo mundo {loc} sabe que {word} é importante, {tag}",
    "{intj} {word} é barril demais, {tag}",
]

# Minimal pair sets for ão and lh
MINIMAL_PAIRS_AO = [
    ("não", "na"), ("mão", "ma"), ("pão", "pa"),
    ("irmão", "irmã"), ("coração", "corações"), ("avião", "avia"),
]

MINIMAL_PAIRS_LH = [
    ("barulho", "barulo"), ("trabalho", "trabalo"), ("olho", "olo"),
    ("filho", "filo"), ("espelho", "espelo"), ("joelho", "joelo"),
]


# ---------------------------------------------------------------------------
# Pragmatic Trap — sentences with hidden double meanings / absurdity.
# A real Baiano would instinctively fire back "Lá ele!" or "Oxe!" before
# even thinking. If you hesitate, you took the laranjada.
# ---------------------------------------------------------------------------

TRAP_SENTENCES = [
    # Obvious lies / exaggerations — demands "Lá ele!"
    ("Eu corro mais que o ônibus na Barra.", "brag",
     "Lá ele! Ninguém corre mais que ônibus, rapaz."),
    ("Meu vizinho disse que vai comprar o Farol da Barra.", "brag",
     "Lá ele! Comprar o Farol... tá de brinquedeira."),
    ("O cara falou que come 30 acarajés de uma vez.", "brag",
     "Lá ele! 30 acarajés? Vai explodir, meu irmão."),
    ("Disseram que vai nevar em Salvador amanhã.", "absurd",
     "Oxe! Nevar em Salvador? Tá maluco é?"),
    ("Meu primo disse que ele é melhor que Caetano no violão.", "brag",
     "Lá ele! Melhor que Caetano... sonha demais."),
    ("O prefeito vai dar praia particular pra cada um.", "absurd",
     "Oxe! Praia particular... lá ele com essa conversa."),
    ("Aquele ali disse que nunca suou na vida.", "absurd",
     "Vixe! Em Salvador e nunca suou? Lá ele!"),
    ("Meu tio falou que vai de bike pra Itaparica.", "absurd",
     "Oxe! De bike atravessando a Baía de Todos os Santos?"),
    ("O cara garantiu que o carnaval vai durar um mês esse ano.", "brag",
     "Lá ele! Um mês de carnaval... quem aguenta?"),
    ("Ela disse que faz vatapá melhor que a Dinha.", "brag",
     "Oxente! Melhor que a Dinha? Lá ele com essa ousadia."),
    # Innocent-sounding but with malícia / duplo sentido
    ("Tu quer ver meu negócio grande lá no Comércio?", "malicia",
     "Oxe! Que negócio grande é esse, rapaz?"),
    ("A moça disse que gosta de uma coisa bem grossa pela manhã.", "malicia",
     "Oxe! (Tá falando de tapioca, né?)"),
    ("Ele falou que entra por trás porque é mais rápido.", "malicia",
     "Eita! (A entrada dos fundos do mercado, claro.)"),
    ("Eu gosto de pegar bem forte com as duas mãos.", "malicia",
     "Oxe! (O balde de camarão, né meu irmão?)"),
    ("Ela me pediu pra meter com mais força.", "malicia",
     "Vixe! (A rede na parede, claro.)"),
    # Scam / street hustle — should trigger suspicion
    ("Esse celular aqui é original, confie em mim, só 50 reais.", "hustle",
     "Lá ele! Celular original por 50 conto? É cilada, Bino."),
    ("Eu achei essa carteira no chão, bora dividir?", "hustle",
     "Oxe! Achei carteira é golpe velho, sai fora."),
    ("Compra esse perfume importado, saiu do navio ontem.", "hustle",
     "Lá ele! Saiu do navio... é piratão, rapaz."),
    ("Investe comigo que triplica em uma semana.", "hustle",
     "Oxente! Triplica em uma semana? Lá ele!"),
    ("Meu irmão trabalha na alfândega, consigo qualquer coisa.", "hustle",
     "Lá ele com essa história de alfândega."),
]

# Session state for laranjada penalty
_laranjada_penalty_remaining = 0


def run_pragmatic_trap():
    """
    Fire a pragmatic trap. Present a sentence with a hidden double meaning,
    absurd claim, or street hustle. The learner must react with the correct
    Baiano reflex (lá ele, oxe, oxente, etc.) within 800ms.

    Returns (passed: bool, latency_ms: int, reaction: str)
    """
    trap = random.choice(TRAP_SENTENCES)
    sentence, trap_type, expected_reaction = trap

    type_labels = {
        "brag": "🎭 PAPO FURADO",
        "absurd": "🎭 ABSURDO",
        "malicia": "🎭 DUPLO SENTIDO",
        "hustle": "🎭 GOLPE NA RUA",
    }

    print(f"\n  {'='*55}")
    print(f"  {type_labels.get(trap_type, '🎭 TRAP')}")
    print(f"  {'='*55}")
    print(f"\n  \"{sentence}\"\n")
    print(f"  ⚡ REACT NOW! (type your reaction + ENTER)")

    start = time.time()
    reaction = input("  > ").strip().lower()
    latency_ms = int((time.time() - start) * 1000)

    # Check if reaction matches any valid Baiano reflex
    passed = False
    for valid in TRAP_REACTIONS:
        if valid in reaction:
            passed = True
            break

    # Must also be under 800ms
    if latency_ms > TRAP_LATENCY_MS:
        passed = False

    if passed:
        print(f"\n  ✅ SOBREVIVEU! {latency_ms}ms")
        print(f"     Reação correta: \"{reaction}\"")
        print(f"     Resposta ideal: {expected_reaction}")
    else:
        reasons = []
        if latency_ms > TRAP_LATENCY_MS:
            reasons.append(f"latency {latency_ms}ms > {TRAP_LATENCY_MS}ms")
        if not any(v in reaction for v in TRAP_REACTIONS):
            reasons.append(f"reaction '{reaction}' — needed: lá ele / oxe / oxente / vixe / eita")
        print(f"\n  🍊 LARANJADA! Tomou na cara!")
        print(f"     {', '.join(reasons)}")
        print(f"     Resposta ideal: {expected_reaction}")
        print(f"     Próximos {LARANJADA_PENALTY_COUNT} chunks serão mais difíceis.")

    print()

    # Log the trap
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    log_file = LOG_DIR / f"session_{datetime.now().strftime('%Y-%m-%d')}.jsonl"
    entry = {
        "timestamp": datetime.now().isoformat(),
        "type": "pragmatic_trap",
        "trap_type": trap_type,
        "sentence": sentence,
        "reaction": reaction,
        "latency_ms": latency_ms,
        "passed": passed,
    }
    with open(log_file, "a") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")

    return passed, latency_ms, reaction


def apply_laranjada_penalty():
    """
    After a laranjada, the next 5 reviews are force-rated as Hard
    regardless of actual performance. Makes the SRS push them sooner.
    """
    global _laranjada_penalty_remaining
    _laranjada_penalty_remaining = LARANJADA_PENALTY_COUNT
    print(f"  🍊 Laranjada penalty active for next {LARANJADA_PENALTY_COUNT} reviews.\n")


def check_laranjada_penalty():
    """Check if laranjada penalty is active, decrement if so. Returns True if penalized."""
    global _laranjada_penalty_remaining
    if _laranjada_penalty_remaining > 0:
        _laranjada_penalty_remaining -= 1
        return True
    return False


def build_chunk(word):
    """Generate a natural chunk and carrier sentence for a word."""
    intj = random.choice(INTERJECTIONS)
    tag = random.choice(TAGS)
    loc = random.choice(LOCATIONS)
    template = random.choice(CARRIER_TEMPLATES)

    carrier = template.format(intj=intj, word=word, tag=tag, loc=loc)
    return word, carrier


def play_audio(filepath):
    """Play audio file with afplay (macOS). Non-blocking."""
    if Path(filepath).exists():
        subprocess.Popen(
            ["afplay", str(filepath)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        ).wait()


def log_session(word_id, word, rating, latency_ms, score=None):
    """Append session data to log file."""
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    log_file = LOG_DIR / f"session_{datetime.now().strftime('%Y-%m-%d')}.jsonl"
    entry = {
        "timestamp": datetime.now().isoformat(),
        "word_id": word_id,
        "word": word,
        "rating": rating,
        "latency_ms": latency_ms,
        "nativeness_score": score,
    }
    with open(log_file, "a") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")


def measure_response():
    """Wait for user to press Enter after shadowing. Returns latency in ms."""
    print("\n  ⏱  Press ENTER after you shadow the sentence...")
    start = time.time()
    input()
    latency_ms = int((time.time() - start) * 1000)
    return latency_ms


def determine_rating(latency_ms, nativeness_score=None):
    """Determine FSRS rating from latency and optional nativeness score."""
    if nativeness_score is not None and nativeness_score < NATIVENESS_THRESHOLD:
        return Rating.Again

    if latency_ms <= 800:
        return Rating.Easy
    elif latency_ms <= LATENCY_THRESHOLD_MS:
        return Rating.Good
    elif latency_ms <= 3000:
        return Rating.Hard
    else:
        return Rating.Again


def _find_golden_audio(word_id=None):
    """
    Find the Golden Speaker audio file (your voice clone + native prosody).
    Checks word-specific golden file first, then falls back to default.
    """
    if word_id is not None:
        specific = AUDIO_DIR / f"golden_{word_id}.mp3"
        if specific.exists():
            return specific
    default = AUDIO_DIR / "golden_speaker.mp3"
    if default.exists():
        return default
    return None


def run_chorus_drill(word, audio_path=None, word_id=None):
    """
    Chorusing protocol using the Golden Speaker file.

    Priority:
      1. golden_{word_id}.mp3 — word-specific golden audio (your voice + native prosody)
      2. golden_speaker.mp3   — default golden audio
      3. native audio clip     — fallback to native model
      4. Manual repetition     — no audio available

    The Golden Speaker is YOU with perfect Baiano prosody, so you're
    imitating your own native-sounding self.
    """
    golden = _find_golden_audio(word_id)
    source_label = None

    if golden:
        audio_to_play = golden
        source_label = "GOLDEN SPEAKER (your voice + native prosody)"
    elif audio_path and Path(audio_path).exists():
        audio_to_play = audio_path
        source_label = "native model"
    else:
        audio_to_play = None

    print(f"\n  🔄 CHORUSING DRILL: '{word}'")
    if source_label:
        print(f"  Source: {source_label}")
    print("  Listen and speak AT THE SAME TIME (not after).\n")

    if audio_to_play:
        print("  ▶ Playing at 0.75x speed...")
        subprocess.run(
            ["afplay", "-r", "0.75", str(audio_to_play)],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        time.sleep(0.5)

        print("  ▶ Playing at 1.0x speed — shadow NOW...")
        play_audio(audio_to_play)
        time.sleep(0.5)

        print("  ▶ Again at 1.0x — match the rhythm...")
        play_audio(audio_to_play)
    else:
        print(f"  (No golden/native audio — repeat '{word}' 3 times)")
        print(f"  Tip: run 'python3 prosody_transplant.py golden <native.mp3> <your_voice.mp3>'")
        for i in range(3):
            input(f"  Press ENTER after attempt {i+1}/3...")

    print("  ✓ Chorusing complete.\n")


def run_minimal_pair_drill(sound_type):
    """Trigger minimal-pair drill for ão or lh."""
    if sound_type == "ao":
        pairs = MINIMAL_PAIRS_AO
        label = "ão (nasalization)"
    elif sound_type == "lh":
        pairs = MINIMAL_PAIRS_LH
        label = "lh (palatal lateral)"
    else:
        return

    print(f"\n  🎯 MINIMAL PAIR DRILL: {label}")
    selected = random.sample(pairs, min(3, len(pairs)))
    for correct, wrong in selected:
        print(f"    Correct: '{correct}'  vs  Wrong: '{wrong}'")
        input(f"    Say '{correct}' clearly, then press ENTER...")
    print("  ✓ Minimal pair drill complete.\n")


def run_shadowing_mode(count=10):
    """Rapid-fire shadowing: sentences with 200ms target delay."""
    print(f"\n  ⚡ SHADOWING MODE — {count} sentences")
    print("  Repeat each sentence with a 200ms delay.\n")

    due = get_due_words()
    if not due:
        print("  No words due.")
        return

    words = random.sample(list(due), min(count, len(due)))
    for i, row in enumerate(words, 1):
        word, carrier = build_chunk(row["word"])
        print(f"  [{i}/{count}] {carrier}")
        # Small pause to simulate the 200ms delay target
        time.sleep(0.2)
        latency_ms = measure_response()
        rating = determine_rating(latency_ms)
        record_review(row["id"], rating, latency_ms)
        rating_name = {1: "Again", 2: "Hard", 3: "Good", 4: "Easy"}[rating.value]
        print(f"    → {latency_ms}ms — {rating_name}")

    print(f"\n  ✓ Shadowing session complete ({count} words).\n")


def run_one_cycle():
    """Run a single 1+T review cycle."""
    row = get_next_word()
    if not row:
        print("Nenhuma palavra pra revisar agora. Descansa, parceiro!")
        return False

    word_id = row["id"]
    word = row["word"]
    tier = row["difficulty_tier"]
    mastery = row["mastery_level"]

    # Step 1: Build chunk + carrier
    chunk, carrier = build_chunk(word)

    print(f"\n{'='*60}")
    print(f"  Word #{word_id} | Tier {tier} ({TIER_LABELS[tier]}) | Mastery {mastery}/5")
    print(f"{'='*60}")
    print(f"\n  📝 Carrier: {carrier}")
    print(f"  🎯 Target:  {chunk}")

    # Step 2: Check for audio file
    audio_path = AUDIO_DIR / f"word_{word_id}.mp3"

    if audio_path.exists():
        print(f"\n  ▶ Playing audio...")
        play_audio(audio_path)

    # Step 3: Measure response latency
    latency_ms = measure_response()

    # Step 4: Determine rating
    rating = determine_rating(latency_ms)

    # Laranjada penalty: force Hard rating if penalty is active
    if check_laranjada_penalty():
        if rating.value > Rating.Hard.value:
            rating = Rating.Hard
        print(f"\n  🍊 Laranjada penalty active ({_laranjada_penalty_remaining} remaining)")

    rating_name = {1: "Again", 2: "Hard", 3: "Good", 4: "Easy"}[rating.value]

    print(f"\n  ⏱  Latency: {latency_ms}ms")
    print(f"  📊 Rating:  {rating_name}")

    if latency_ms > LATENCY_THRESHOLD_MS:
        print(f"  ⚠  Above {LATENCY_THRESHOLD_MS}ms threshold — not automatic yet.")

    # Step 5: Check for ão/lh words needing special attention
    if any(s in word for s in ["ão", "ões", "ção"]):
        if rating.value <= Rating.Hard.value:
            run_minimal_pair_drill("ao")
    if "lh" in word:
        if rating.value <= Rating.Hard.value:
            run_minimal_pair_drill("lh")

    # Step 6: Chorusing if needed
    if rating == Rating.Again:
        run_chorus_drill(word, audio_path if audio_path.exists() else None, word_id=word_id)

    # Step 7: Record review
    card, new_mastery = record_review(word_id, rating, latency_ms)
    print(f"  ✓ Mastery: {mastery} → {new_mastery} | Next due: {card.due}")

    # Step 8: Log
    log_session(word_id, word, rating.value, latency_ms)

    return True


def run_session(count):
    """Run multiple review cycles."""
    max_tier = get_unlocked_tier()
    print(f"\nOxe Protocol — Session ({count} reviews)")
    print(f"Current tier: {max_tier} ({TIER_LABELS[max_tier]})\n")

    completed = 0
    traps_fired = 0
    traps_passed = 0
    for i in range(count):
        # 15% chance of a pragmatic trap instead of a normal review
        if random.random() < TRAP_PROBABILITY:
            traps_fired += 1
            passed, lat, reaction = run_pragmatic_trap()
            if passed:
                traps_passed += 1
            else:
                apply_laranjada_penalty()
            continue

        if not run_one_cycle():
            break
        completed += 1

    print(f"\n{'='*60}")
    print(f"  Session complete: {completed}/{count} reviews")
    if traps_fired > 0:
        print(f"  Traps: {traps_passed}/{traps_fired} survived")
    print(f"{'='*60}")

    # Show progress
    progress = tier_progress()
    max_tier = get_unlocked_tier()
    print()
    for tier, label, mastered, total, pct in progress:
        if total > 0:
            bar_filled = int(pct / 10)
            bar = "█" * bar_filled + "░" * (10 - bar_filled)
            status = "✓" if tier < max_tier else ("→" if tier == max_tier else "🔒")
            print(f"  {status} Tier {tier} ({label:<14}): {bar} {pct:.0f}% [{mastered}/{total}]")


def main():
    if len(sys.argv) < 2:
        run_one_cycle()
        return

    arg = sys.argv[1]

    if arg == "--session":
        count = int(sys.argv[2]) if len(sys.argv) > 2 else 10
        run_session(count)
    elif arg == "--shadow":
        count = int(sys.argv[2]) if len(sys.argv) > 2 else 10
        run_shadowing_mode(count)
    elif arg == "--trap":
        passed, lat, reaction = run_pragmatic_trap()
        if not passed:
            apply_laranjada_penalty()
    elif arg == "--chorus":
        if len(sys.argv) < 3:
            print("Usage: python3 drill_loop.py --chorus <word> [--word-id N]")
            sys.exit(1)
        word = sys.argv[2]
        wid = None
        if "--word-id" in sys.argv:
            idx = sys.argv.index("--word-id")
            if idx + 1 < len(sys.argv):
                wid = int(sys.argv[idx + 1])
        run_chorus_drill(word, word_id=wid)
    else:
        print(__doc__.strip())
        sys.exit(1)


if __name__ == "__main__":
    main()
