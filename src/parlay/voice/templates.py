"""Selectable voice templates for the live AI (voice + persona presets).

Each template pairs one of the Gemini Live native-audio prebuilt HD voices with
a persona (system instruction). Every persona instructs the model to reply only
in Hindi, explicitly and with no fallback to another language, so a template
switch changes both how the AI sounds and how it behaves while keeping the
session Hindi-only.

Templates are addressed by a 1-based number so an operator can switch on demand
with a short command (e.g. `/tem 3`). The voice names here are drawn from the
30 prebuilt Gemini Live voices; changing a template's `voice` to any other
prebuilt name is safe.
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


@dataclass(frozen=True)
class VoiceTemplate:
    """One selectable preset: a label, a prebuilt voice, and a persona."""

    key: str
    label: str
    voice: str
    persona: str

    def system_instruction(self) -> str:
        """The persona with the Hindi-only rule appended."""
        return f"{self.persona}{_HINDI_RULE}"


# The ordered set of templates. The index (1-based) is the on-demand selector.
TEMPLATES: tuple[VoiceTemplate, ...] = (
    VoiceTemplate(
        key="warm",
        label="Warm",
        voice="Sulafat",
        persona=(
            "Tum ek garmjoshi bhare, dostana saathi ho. Dheere, narmi se aur "
            "apnepan ke saath baat karo, jaise kisi kareebi dost se baat kar rahe ho."
        ),
    ),
    VoiceTemplate(
        key="cool",
        label="Cool",
        voice="Charon",
        persona=(
            "Tum ek shaant, confident aur cool saathi ho. Aaram se, seedhi aur "
            "bina jaldbaazi ke baat karo, aawaz mein thoda thehraav rakho."
        ),
    ),
    VoiceTemplate(
        key="energetic",
        label="Energetic",
        voice="Puck",
        persona=(
            "Tum ek josheela, upbeat saathi ho. Utsaah ke saath, thodi tez aur "
            "lively andaaz mein baat karo, baat-cheet ko mazedaar banaye rakho."
        ),
    ),
    VoiceTemplate(
        key="calm",
        label="Calm",
        voice="Kore",
        persona=(
            "Tum ek sthir, sukoon dene wale saathi ho. Bahut shaant, sanyat aur "
            "aashwasan bhare tareeke se baat karo, sunne wale ko rahat mehsoos ho."
        ),
    ),
    VoiceTemplate(
        key="playful",
        label="Playful",
        voice="Aoede",
        persona=(
            "Tum ek shararati, khilandad saathi ho. Halke-phulke andaaz mein, "
            "thodi hansi-mazaak ke saath baat karo, par baat kaam ki rakho."
        ),
    ),
)

DEFAULT_TEMPLATE_INDEX = 1


def template_count() -> int:
    """How many templates are available."""
    return len(TEMPLATES)


def get_template(index: int) -> VoiceTemplate:
    """Return the template for a 1-based index.

    Raises ValueError when the index is outside 1..template_count().
    """
    if not 1 <= index <= len(TEMPLATES):
        raise ValueError(f"template must be from 1 to {len(TEMPLATES)}")
    return TEMPLATES[index - 1]


def describe_templates() -> str:
    """A one-line-per-template summary for operator messages."""
    return "\n".join(
        f"{i}. {t.label} ({t.voice})" for i, t in enumerate(TEMPLATES, start=1)
    )
