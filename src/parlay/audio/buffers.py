def silence(n: int) -> bytes:
    """n bytes of S16LE silence."""
    return b"\x00" * max(0, n)
