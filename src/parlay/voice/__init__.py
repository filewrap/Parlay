"""AI Voice Pipeline: provider-agnostic voice bridging.

The pipeline connects the Captured Stream to a real-time voice provider and
emits the provider's spoken reply to the Playback Sink. Providers are accessed
through the `VoiceProvider` interface so the model is swappable; the default
implementation is `GeminiVoiceProvider` over the Gemini Live API.
"""

from .provider import ProviderError, ReplyEvent, ReplyEventKind, VoiceProvider
from .session_manager import ProviderSessionManager

__all__ = [
    "ProviderError",
    "ProviderSessionManager",
    "ReplyEvent",
    "ReplyEventKind",
    "VoiceProvider",
]
