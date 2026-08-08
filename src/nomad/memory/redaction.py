"""Refuse to remember a credential.

A stored memory is not just a row: pinned memories are injected into every
future system prompt, and a recalled one is pasted into a live context. That
makes this store the worst possible place on the device for a secret — worse
than a file, because a file is not automatically re-read to a model forever.

**Narrow beats aggressive.** A false refusal is a bug with a user-visible
symptom: "my password manager is 1Password" is a perfectly good thing to
remember about an operator, and a matcher that rejects it makes the memory
feel broken and teaches the model to stop using it. So every pattern here
requires credential *shape*, not a credential-sounding *word*:

* a known token prefix followed by enough opaque characters to be a real key;
* a PEM private-key header;
* an assignment (`password:`, `api key =`) whose value is not an English word;
* a long unbroken run that is hex or mixed-case-plus-digits.

This is a guard, not a scanner. It will not catch a passphrase written as
three lowercase words, and it is not trying to.
"""

from __future__ import annotations

import re

#: Token prefixes worth recognising. The trailing length requirement is what
#: keeps the word "sk-" in a sentence from tripping this.
_PREFIXED_TOKEN = re.compile(
    r"(?:sk-|pk-|rk_|ghp_|gho_|ghu_|ghs_|ghr_|github_pat_|glpat-|npm_|"
    r"xox[baprs]-|xapp-|AKIA|ASIA|ya29\.|AIza)"
    r"[A-Za-z0-9_\-]{16,}"
)

_PEM_PRIVATE_KEY = re.compile(r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----")

_ASSIGNMENT = re.compile(
    r"\b(?:password|passwd|passphrase|api[ _-]?key|secret|"
    r"access[ _-]?token|auth[ _-]?token|bearer)\b\s*[:=]\s*(\S{6,})",
    re.IGNORECASE,
)

#: A run long enough to be an opaque credential. `/` and `.` are excluded so a
#: long filesystem path or dotted module name is not mistaken for a key.
_LONG_RUN = re.compile(r"[A-Za-z0-9+_=-]{40,}")

_HEX = re.compile(r"\A[0-9a-fA-F]+\Z")


def _value_looks_opaque(value: str) -> bool:
    """True unless the assigned value is a plain lowercase word.

    "password: hunter2" is a credential. "password: manager" — as in "his
    password manager: manager of passwords" — is prose that happened to be
    punctuated. Requiring a digit, a capital or a symbol splits the two
    without a dictionary.
    """
    return re.search(r"[^a-z]", value) is not None


def _run_looks_opaque(run: str) -> bool:
    if _HEX.match(run):
        return True
    return (
        any(c.isdigit() for c in run)
        and any(c.islower() for c in run)
        and any(c.isupper() for c in run)
    )


def looks_like_secret(text: str) -> str | None:
    """Return a short description of what matched, or `None` if it looks safe.

    Returning the description rather than a bare bool is deliberate: the
    refusal handed back to the model has to say *what* tripped, or the model
    will simply rephrase the same secret and try again.
    """
    if _PEM_PRIVATE_KEY.search(text):
        return "a PEM private key block"
    if _PREFIXED_TOKEN.search(text):
        return "an API-key-shaped token"
    match = _ASSIGNMENT.search(text)
    if match is not None and _value_looks_opaque(match.group(1)):
        return "a password or key assignment"
    for run in _LONG_RUN.findall(text):
        if _run_looks_opaque(run):
            return "a long opaque token"
    return None
