"""Prompt construction for the generation graph."""
from __future__ import annotations

from typing import Any

PERSONAS: dict[str, str] = {
    "Default": "Your everyday voice. Neutral, natural, as you'd normally write.",
    "Boss": "Writing up to a manager: clear, concise, respectful, outcome-first, no fluff.",
    "Friend": "Writing to a friend: relaxed, warm, casual; slang and humour are fine if the user uses them.",
    "Investor": "Writing to an investor: confident, precise, metric-minded, polished but not stiff.",
}

_CHAT_APPS = ("slack", "discord", "teams", "messages", "imessage", "whatsapp", "telegram", "signal", "messenger")
_MAIL_APPS = ("mail", "gmail", "outlook", "superhuman", "spark", "hey")
_SOCIAL_APPS = ("linkedin", "twitter", "x.com", "threads", "bluesky")
_DOC_APPS = ("notion", "docs", "word", "pages", "notes", "obsidian", "confluence")


def persona_guidance(persona: str) -> str:
    return PERSONAS.get(persona) or f"Writing as the user's '{persona}' persona; match that relationship's tone."


def app_context_hint(app_context: str | None) -> str | None:
    if not app_context:
        return None
    a = app_context.lower()
    if any(k in a for k in _CHAT_APPS):
        return f"Destination: {app_context} (chat). Keep it short — 1-3 sentences, no greeting line, no sign-off."
    if any(k in a for k in _MAIL_APPS):
        return (
            f"Destination: {app_context} (email). Short paragraphs; a greeting or sign-off only if the user's "
            "intent implies one, and never invent names for it."
        )
    if any(k in a for k in _SOCIAL_APPS):
        return f"Destination: {app_context} (social post). Punchy and self-contained."
    if any(k in a for k in _DOC_APPS):
        return f"Destination: {app_context} (document). Complete sentences, may be slightly longer."
    return f"Destination app: {app_context}."


def _rate_word(rate: float) -> str:
    if rate >= 0.15:
        return "often"
    if rate >= 0.03:
        return "occasionally"
    return "rarely"


def build_system_prompt(constraints: dict[str, Any], persona: str, app_context: str | None = None) -> str:
    c = constraints
    n = int(c.get("sample_count") or 0)
    lines: list[str] = [
        ("You are Polytone. You rewrite the user's rough intent into the finished message they would "
        "have written themselves, in THEIR personal writing style."),
        "",
    ]
    if n == 0:
        lines += [
            ("STYLE: no writing samples yet. Write plainly and naturally: short sentences, standard "
            "sentence case, everyday words, no corporate filler."),
        ]
    else:
        rules: list[str] = []
        avg = float(c.get("avg_sentence_len") or 0)
        if avg > 0:
            tol = max(2.0, round(avg * 0.25, 1))
            p90 = float(c.get("sentence_len_p90") or 0)
            rule = f"Sentence length: average {avg:.1f} words per sentence (stay within ±{tol:g})"
            if p90:
                rule += f"; no sentence longer than about {p90:.0f} words"
            rules.append(rule + ".")
        casing = c.get("casing") or "sentence"
        rules.append(
            {
                "lower": "Casing: all lowercase — the user does not capitalize sentence starts (keep proper nouns as the user would).",
                "sentence": "Casing: standard sentence case.",
                "mixed": "Casing: mixed — the user often starts sentences in lowercase; casual is fine.",
            }.get(casing, "Casing: standard sentence case.")
        )
        cr = float(c.get("contractions_rate") or 0)
        if cr >= 0.5:
            rules.append(f"Contraction rate {cr:.2f}: use contractions (don't, I'm, it's) wherever natural.")
        elif cr <= 0.2 and n >= 3:
            rules.append(f"Contraction rate {cr:.2f}: avoid contractions; write 'do not', 'I am'.")
        else:
            rules.append(f"Contraction rate {cr:.2f}: mix contractions and full forms.")
        pr: dict[str, float] = c.get("punctuation_rates") or {}
        punct_bits = []
        named = {"—": "em-dashes (—)", "!": "exclamation marks", "…": "ellipses (…)", ";": "semicolons",
                 "?": "question marks", ",": "commas", "(": "parentheses", ":": "colons"}
        for sym, label in named.items():
            rate = float(pr.get(sym, 0.0))
            if rate > 0:
                punct_bits.append(f"{label}: {_rate_word(rate)} ({rate:.2f}/sentence)")
            elif sym in ("—", "!", "…", ";") and n >= 3:
                punct_bits.append(f"{label}: never")
        if punct_bits:
            rules.append("Punctuation habits — " + "; ".join(punct_bits) + ".")
        if c.get("top_openers"):
            rules.append("Prefer starting sentences with: " + ", ".join(f'"{o}"' for o in c["top_openers"][:6]) + ".")
        if c.get("top_transitions"):
            rules.append("Preferred transitions: " + ", ".join(f'"{t}"' for t in c["top_transitions"][:6]) + ".")
        if c.get("lexicon"):
            rules.append("Prefer the user's own vocabulary when it fits: " + ", ".join(c["lexicon"][:20]) + ".")
        if c.get("top_bigrams"):
            pairs = ", ".join(f'"{a} {b}"' for a, b, *_ in c["top_bigrams"][:10])
            rules.append(f"Word pairs the user often uses: {pairs}.")
        lines.append(f"HARD STYLE RULES (learned from {n} of the user's own writing samples — follow them):")
        lines += [f"{i}. {r}" for i, r in enumerate(rules, 1)]
    lines += [
        "",
        f"PERSONA: {persona}. {persona_guidance(persona)}",
    ]
    hint = app_context_hint(app_context)
    if hint:
        lines.append(f"CONTEXT: {hint}")
    lines += [
        "",
        "CONTENT RULES:",
        ("- Convey exactly what the intent says. Never invent facts, names, dates, numbers, links or "
        "commitments that are not in the intent."),
        "- Turn bullet points into flowing text unless the intent is clearly a list.",
        ("- Output ONLY the final message text. No preamble (no \"Here's...\"), no quotes around it, "
        "no explanations, no alternatives, no markdown headings."),
    ]
    return "\n".join(lines)


def build_user_prompt(intent: str) -> str:
    return f"Rewrite this rough intent as my message:\n<intent>\n{intent}\n</intent>"
