"""Selectable voice presets for the live AI (Gemini prebuilt voices + Hindi persona).

Each preset pairs one of the 30 Gemini Live native-audio prebuilt HD voices with
a shared Hindi-only persona (system instruction). Selecting a preset changes how
the AI sounds while keeping every session Hindi-only. Presets are addressed by a
1-based number so an operator can switch on demand with a short command
(e.g. `/tem 7`).

The persona is deliberately generic (one warm, friendly assistant) so all 30
voices are usable without inventing 30 distinct characters. The `label` per voice
is Google's own descriptor for that voice; `gender` is display metadata only (the
voice, not the persona, determines how it sounds). Changing a preset's `voice` to
any other prebuilt name is safe.
"""

from __future__ import annotations

from dataclasses import dataclass

# Shared Hindi-only directive appended to every persona. Native-audio Gemini
# models take their spoken language from the system instruction, so stating the
# constraint explicitly (and forbidding a fallback) is what keeps replies Hindi.
_HINDI_RULE = (
    " Tum hamesha sirf Hindi mein jawab doge. Har haal mein Hindi mein bolo, "
    "kisi bhi doosri bhasha (English ya koi aur) mein mat bolo, chahe user "
    "kisi aur bhasha mein baat kare. Koi fallback nahi."
)

# One generic, friendly persona shared by every voice.
_PERSONA = (
    "Tum ek garmjoshi bhare, dostana aur madadgaar saathi ho. Natural, insaani "
    "andaaz mein baat karo, chhote aur saaf jawab do, aur baat-cheet ko sehaj "
    "aur apnepan bhara rakho."
)


@dataclass(frozen=True)
class VoiceTemplate:
    """One selectable preset: a prebuilt voice plus the shared Hindi persona."""

    key: str
    label: str  # Google's character descriptor for the voice, e.g. "Warm".
    voice: str  # Prebuilt Gemini Live voice name.
    gender: str  # "Female" or "Male" (display metadata only).
    persona: str

    def system_instruction(self) -> str:
        """The persona with the Hindi-only rule appended."""
        return f"{self.persona}{_HINDI_RULE}"


def _voice(label: str, name: str, gender: str) -> VoiceTemplate:
    return VoiceTemplate(key=name.lower(), label=label, voice=name, gender=gender, persona=_PERSONA)


# All 30 Gemini Live prebuilt HD voices. Sulafat (Warm, female) is first so the
# default preset is a warm female voice.
TEMPLATES: tuple[VoiceTemplate, ...] = (
    _voice("Warm", "Sulafat", "Female"),
    _voice("Bright", "Zephyr", "Female"),
    _voice("Upbeat", "Puck", "Male"),
    _voice("Informative", "Charon", "Male"),
    _voice("Firm", "Kore", "Female"),
    _voice("Excitable", "Fenrir", "Male"),
    _voice("Youthful", "Leda", "Female"),
    _voice("Firm", "Orus", "Male"),
    _voice("Breezy", "Aoede", "Female"),
    _voice("Easy-going", "Callirrhoe", "Female"),
    _voice("Bright", "Autonoe", "Female"),
    _voice("Breathy", "Enceladus", "Male"),
    _voice("Clear", "Iapetus", "Male"),
    _voice("Easy-going", "Umbriel", "Male"),
    _voice("Smooth", "Algieba", "Male"),
    _voice("Smooth", "Despina", "Female"),
    _voice("Clear", "Erinome", "Female"),
    _voice("Gravelly", "Algenib", "Male"),
    _voice("Informative", "Rasalgethi", "Male"),
    _voice("Upbeat", "Laomedeia", "Female"),
    _voice("Soft", "Achernar", "Female"),
    _voice("Firm", "Alnilam", "Male"),
    _voice("Even", "Schedar", "Male"),
    _voice("Mature", "Gacrux", "Female"),
    _voice("Forward", "Pulcherrima", "Female"),
    _voice("Friendly", "Achird", "Male"),
    _voice("Casual", "Zubenelgenubi", "Male"),
    _voice("Gentle", "Vindemiatrix", "Female"),
    _voice("Lively", "Sadachbia", "Male"),
    _voice("Knowledgeable", "Sadaltager", "Male"),
)

DEFAULT_TEMPLATE_INDEX = 1


def template_count() -> int:
    """How many presets are available."""
    return len(TEMPLATES)


def get_template(index: int) -> VoiceTemplate:
    """Return the preset for a 1-based index.

    Raises ValueError when the index is outside 1..template_count().
    """
    if not 1 <= index <= len(TEMPLATES):
        raise ValueError(f"template must be from 1 to {len(TEMPLATES)}")
    return TEMPLATES[index - 1]


def describe_templates() -> str:
    """A one-line-per-preset summary for operator messages."""
    return "\n".join(
        f"{i}. {t.label} ({t.voice}) \u2014 {t.gender}" for i, t in enumerate(TEMPLATES, start=1)
    )
