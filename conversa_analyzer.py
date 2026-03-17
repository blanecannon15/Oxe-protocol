"""
conversa_analyzer.py — Conversa Error Analysis Engine for the Oxe Protocol.

After each conversation session, GPT-4o analyzes the learner's messages
for grammar gaps, missing articles, wrong prepositions, and creates
targeted drills from actual mistakes.

All analysis is in Portuguese (NUNCA usa inglês).
"""

import json
import sqlite3
from typing import Dict, List, Optional

from dictionary_engine import _chat
from srs_engine import DB_PATH, get_connection, add_chunk


# ── GPT-4o Analysis Prompt ─────────────────────────────────────────

ANALYZER_SYSTEM_PROMPT = (
    "Tu é um professor baiano de português que analisa conversas de um aprendiz. "
    "Tua função é encontrar erros de gramática, artigos faltando, preposições erradas, "
    "e avaliar a fluência natural do aprendiz.\n\n"
    "REGRAS:\n"
    "- NUNCA usa inglês. Nem uma palavra.\n"
    "- Analisa SOMENTE as mensagens do aprendiz (role: user), não as do AI.\n"
    "- Foca em: conjugação verbal, uso de artigos (o/a/os/as), contrações de preposição "
    "(no/na/do/da/pelo/pela), concordância de gênero, naturalidade dos chunks.\n"
    "- Seja encorajador mas honesto — estilo baiano, oxe!\n"
    "- A nota de fluência (0-100) é baseada em naturalidade, não perfeição gramatical.\n"
    "- Se o aprendiz usou gírias baianas corretamente, destaca isso como positivo."
)


def analyze_conversation(messages, db_path=DB_PATH):
    """Analyze learner messages from a conversa session via GPT-4o.

    Args:
        messages: list of role/content dicts from the conversation
        db_path: path to the database

    Returns:
        dict with erros, padroes_fracos, chunks_corretos, fluencia_score, nota_geral
    """
    fallback = {
        "erros": [],
        "padroes_fracos": [],
        "chunks_corretos": [],
        "fluencia_score": 0,
        "nota_geral": "Não consegui analisar a conversa.",
    }

    # Extract only learner messages
    learner_msgs = [m["content"] for m in messages if m.get("role") == "user"]
    if not learner_msgs:
        return fallback

    # Build the full conversation context for GPT-4o
    conv_text_parts = []
    for m in messages:
        role_label = "APRENDIZ" if m.get("role") == "user" else "AI"
        conv_text_parts.append("%s: %s" % (role_label, m.get("content", "")))
    conv_text = "\n".join(conv_text_parts)

    user_prompt = (
        "Analisa esta conversa. Foca SOMENTE nas mensagens do APRENDIZ.\n\n"
        "CONVERSA:\n%s\n\n"
        "Responde em JSON com EXATAMENTE estas chaves:\n"
        "- \"erros\": lista de erros encontrados. Cada item tem:\n"
        "  - \"original\": o que o aprendiz disse\n"
        "  - \"corrigido\": a forma correta\n"
        "  - \"tipo\": tipo do erro (conjugação, artigo, preposição, concordância, "
        "ordem, vocabulário, ortografia)\n"
        "  - \"explicacao\": explicação curta em português simples baiano\n"
        "- \"padroes_fracos\": padrões que o aprendiz precisa treinar mais. Cada item tem:\n"
        "  - \"padrao\": nome do padrão (ex: preposições de lugar)\n"
        "  - \"exemplos\": lista de exemplos do erro\n"
        "  - \"sugestao_drill\": sugestão de treino com chunks\n"
        "- \"chunks_corretos\": lista de chunks/expressões que o aprendiz usou "
        "corretamente e de forma natural\n"
        "- \"fluencia_score\": nota de 0 a 100 baseada em naturalidade "
        "(não perfeição gramatical)\n"
        "- \"nota_geral\": feedback geral encorajador em estilo baiano "
        "(2-3 frases)\n\n"
        "Se não encontrar erros, a lista de erros fica vazia mas dá feedback positivo.\n"
        "NUNCA usa inglês. Responde SOMENTE em JSON."
    ) % conv_text

    result = _chat(ANALYZER_SYSTEM_PROMPT, user_prompt, json_mode=True)
    if result is None:
        return fallback

    return {
        "erros": result.get("erros", []),
        "padroes_fracos": result.get("padroes_fracos", []),
        "chunks_corretos": result.get("chunks_corretos", []),
        "fluencia_score": result.get("fluencia_score", 0),
        "nota_geral": result.get("nota_geral", ""),
    }


def generate_correction_drills(errors, db_path=DB_PATH):
    """Create SRS drill items from conversation errors.

    For each error, creates a chunk using the corrected form and seeds
    it into the SRS queue via add_chunk().

    Args:
        errors: list of error dicts from analyze_conversation
        db_path: path to the database

    Returns:
        list of chunk strings that were added
    """
    added_chunks = []
    if not errors:
        return added_chunks

    conn = get_connection(db_path)

    for err in errors:
        corrigido = err.get("corrigido", "")
        if not corrigido:
            continue

        # Use the corrected sentence as the carrier
        carrier = corrigido

        # Try to find a matching word_id from the corrected form
        word_id = None
        for token in corrigido.split():
            clean = token.strip(".,!?;:\"'()")
            if not clean:
                continue
            row = conn.execute(
                "SELECT id FROM word_bank WHERE LOWER(word) = ? LIMIT 1",
                (clean.lower(),),
            ).fetchone()
            if row:
                word_id = row["id"]
                break

        # Build a natural chunk from the corrected form
        # Use the full corrected phrase as the target chunk
        chunk_text = corrigido.strip()
        if len(chunk_text.split()) > 6:
            # Too long — extract the core correction
            words = chunk_text.split()
            chunk_text = " ".join(words[:5])

        chunk_id = add_chunk(
            word_id, chunk_text, carrier, "manual", db_path
        )
        if chunk_id is not None:
            added_chunks.append(chunk_text)

    conn.close()
    return added_chunks


def get_conversation_analysis(session_id, db_path=DB_PATH):
    """Retrieve or generate analysis for a specific conversa session.

    Checks conversa_sessions.post_extraction for existing analysis.
    If not found, fetches messages, runs analyze_conversation, stores result.

    Args:
        session_id: the conversa session ID
        db_path: path to the database

    Returns:
        dict with analysis data, or None if session not found
    """
    conn = get_connection(db_path)
    row = conn.execute(
        "SELECT messages, post_extraction FROM conversa_sessions WHERE id = ?",
        (session_id,),
    ).fetchone()
    conn.close()

    if not row:
        return None

    # Check if analysis already exists in post_extraction
    post_extraction = {}
    if row["post_extraction"]:
        try:
            post_extraction = json.loads(row["post_extraction"])
        except (json.JSONDecodeError, TypeError):
            pass

    if "analysis" in post_extraction:
        return post_extraction["analysis"]

    # No analysis yet — run it
    messages = []
    if row["messages"]:
        try:
            messages = json.loads(row["messages"])
        except (json.JSONDecodeError, TypeError):
            pass

    if not messages:
        return None

    analysis = analyze_conversation(messages, db_path)

    # Store the analysis back into post_extraction
    post_extraction["analysis"] = analysis
    conn = get_connection(db_path)
    conn.execute(
        "UPDATE conversa_sessions SET post_extraction = ? WHERE id = ?",
        (json.dumps(post_extraction, ensure_ascii=False), session_id),
    )
    conn.commit()
    conn.close()

    return analysis


def get_analysis_history(limit=10, db_path=DB_PATH):
    """Return recent conversation analyses with fluency scores over time.

    Args:
        limit: max number of sessions to return
        db_path: path to the database

    Returns:
        list of dicts with session_id, date, fluencia_score, nota_geral, error_count
    """
    conn = get_connection(db_path)
    rows = conn.execute(
        "SELECT id, date, post_extraction FROM conversa_sessions "
        "WHERE post_extraction IS NOT NULL "
        "ORDER BY id DESC LIMIT ?",
        (limit,),
    ).fetchall()
    conn.close()

    history = []
    for row in rows:
        post = {}
        if row["post_extraction"]:
            try:
                post = json.loads(row["post_extraction"])
            except (json.JSONDecodeError, TypeError):
                pass

        analysis = post.get("analysis", {})
        if not analysis:
            continue

        history.append({
            "session_id": row["id"],
            "date": row["date"],
            "fluencia_score": analysis.get("fluencia_score", 0),
            "nota_geral": analysis.get("nota_geral", ""),
            "error_count": len(analysis.get("erros", [])),
            "chunks_corretos_count": len(analysis.get("chunks_corretos", [])),
            "padroes_fracos_count": len(analysis.get("padroes_fracos", [])),
        })

    return history
