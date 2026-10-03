"""What qwen3-asr says when it heard no speech.

qwen3-asr (Whisper-family) never answers non-speech with an empty string. It
answers with a short, confident phrase, and that phrase went straight into the
composer: "I asked how many beneficiaries, it typed 'Okay.'"

Measured live (2026-09-25) on 40 speech-free 16 kHz WAVs — white, brown, mains
hum, clicks, breath-like noise; peak levels 0.001-0.06; 1.5 s and 4 s — the
outputs were exactly:

    "Okay."          22
    "I'm not sure."  11
    "Oh."             4
    "Oh, yeah."       3

A data question is never just one of these, so a transcript that is WHOLLY one
of them is reported as "no speech heard" instead of being typed for the user.

Why not a level/energy check on the audio instead: the ASR still transcribes
speech buried in noise that an energy detector cannot see ("How many
beneficiaries?" at speech peak 0.1 over noise RMS 0.03 had zero frames above
the noise floor). A pre-filter would throw away questions the model can read;
checking the model's own answer cannot.
"""
from __future__ import annotations

import re

# Measured outputs above, plus their close variants. "yes"/"no" are
# deliberately absent: they are real answers to the chatbot's clarifications.
_NO_SPEECH_OUTPUTS = frozenset({
    "okay", "ok", "oh", "oh yeah", "oh okay", "yeah",
    "im not sure", "i am not sure",
    "thank you", "thanks", "thank you for watching", "thanks for watching",
    "hmm", "hm", "mm", "uh", "um", "ah",
})


def _normalise(text: str) -> str:
    folded = (text or "").lower().replace("'", "").replace("’", "")
    return " ".join(re.sub(r"[^a-z ]+", " ", folded).split())


# A real question can name a few districts in a row ("East Khasi Hills and
# West Khasi Hills" shares 6 words with the prompt); an echo repeats whole
# sentences of it. 8 consecutive shared words separates the two.
_ECHO_RUN_WORDS = 8


def _longest_shared_run(a: list[str], b: list[str]) -> int:
    best, prev = 0, [0] * (len(b) + 1)
    for x in a:
        cur = [0] * (len(b) + 1)
        for j, y in enumerate(b, 1):
            if x == y:
                cur[j] = prev[j - 1] + 1
                if cur[j] > best:
                    best = cur[j]
        prev = cur
    return best


def is_prompt_echo(text: str, prompt: str) -> bool:
    """True when the transcript repeats the vocabulary prompt.

    With a `prompt` the ASR answers speech-free audio (silence, steady noise,
    distant chatter too quiet to read) with the prompt itself, verbatim —
    measured on white noise, silence and a Bluetooth headset's room tone. It is
    a cleaner no-speech signal than the stock phrases, so treat it as one."""
    if not prompt:
        return False
    t, p = _normalise(text).split(), _normalise(prompt).split()
    if not t:
        return False
    return (_longest_shared_run(t, p) >= min(_ECHO_RUN_WORDS, len(p))
            or t[:5] == p[:5])


def is_no_speech(text: str) -> bool:
    """True when the whole transcript is empty or one of the ASR's no-speech outputs."""
    norm = _normalise(text)
    return not norm or norm in _NO_SPEECH_OUTPUTS
