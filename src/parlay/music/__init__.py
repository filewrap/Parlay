"""Music Playback: the music control layer (REQ-MUS-001, 002, 004, 005, 006).

This package owns the queue, transport commands, and in-call messaging for
music. It composes the Media Sourcing Pipeline (for `Track` resolution and PCM)
and the Audio Output Arbiter (for call-output access), per the Music Playback
blueprint. Sourcing and arbitration internals live outside this package.
"""
