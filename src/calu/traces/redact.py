"""Redaction of a session trace: nothing reaches the trace file before it passed here (D232).

Two stages, in this order, for every block of the trace:

    text ──► stage 1, deterministic (`Secrets.redact`, `redact_shapes`)
               the values calu knows to be secret   -> "[redacted: api key]", ...
               common shapes of keys and tokens     -> "[redacted: aws access key]", ...
         ──► stage 2, the redaction agent (`Redactor.redact`), if `traces.redact` is on
               secrets and personal data            -> "[redacted: e-mail address]", ...
               the answer is checked (`checked`); stage 1 runs once more on it
         ──► the text for the file

Stage 1 always runs. Stage 2 is a model call, and a model can fail: `Redactor.redact` then
raises `RedactionFailed`, and the caller writes `WITHHELD` in place of the block
(`calu.traces.writer`). The text that went into a failed call is never written.

Stage 2 reads a long text in pieces of at most `CHUNK_CHARS` characters, cut at line ends, so
that every answer fits into the model's output; one failed piece fails the whole text.
"""

from __future__ import annotations

import logging
import os
import re
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field

import anyio
from pydantic import BaseModel, Field
from pydantic_ai import Agent, UsageLimits
from pydantic_ai.models import Model
from pydantic_ai.settings import ModelSettings

from calu.markdown import fence
from calu.models import run_bounded

log = logging.getLogger(__name__)

WITHHELD = "[this step was withheld: redaction failed]"
"""What the trace holds in place of a block the redaction agent could not redact."""

MIN_SECRET_CHARS = 8
"""A known secret value shorter than this is not replaced: a value such as "1" or "dev" would
be replaced all over ordinary text."""

CHUNK_CHARS = 8_000
"""The largest piece of text one call of the redaction agent reads."""

MAX_GROWTH = 1.2
"""An answer of the redaction agent may be this many times as long as its input ..."""

GROWTH_ALLOWANCE = 200
"""... plus this many characters (placeholders are longer than short values). A longer answer
is not a redaction of the input, and the block is withheld."""

REDACTOR_MAX_TOKENS = 8_000
"""Cap on the output tokens of one answer of the redaction agent: room for `CHUNK_CHARS`
characters and the model's own reasoning."""


def placeholder(kind: str) -> str:
    """Return the typed placeholder that stands for a removed value.

    Args:
        kind: What was removed, e.g. "api key".

    Returns:
        E.g. "[redacted: api key]".
    """
    return f"[redacted: {kind}]"


_CREDENTIAL_NAME = re.compile(
    r"(^|_)(KEY|TOKEN|SECRET|PASSWORD|PASSWD|PASSPHRASE|CREDENTIALS?)(_|$)", re.IGNORECASE
)
"""An environment variable whose name has one of these words holds a credential."""

_URL_PASSWORD = re.compile(r"[a-zA-Z][a-zA-Z0-9+.-]*://[^\s/:@]*:([^\s/@]+)@")
"""A URL with `user:password@`; group 1 is the password."""


def kind_of_variable(name: str) -> str:
    """Name the placeholder type for the value of an environment variable.

    Args:
        name: The variable's name, e.g. "MISTRAL_API_KEY".

    Returns:
        "password" for a name with PASSWORD, PASSWD or PASSPHRASE, "token" for one with TOKEN,
        "api key" for one with KEY, else "secret value".
    """
    upper = name.upper()
    if "PASSWORD" in upper or "PASSWD" in upper or "PASSPHRASE" in upper:
        return "password"
    if "TOKEN" in upper:
        return "token"
    if "KEY" in upper:
        return "api key"
    return "secret value"


class Secrets:
    """The values calu knows to be secret, each with the type its placeholder names.

    The set may grow while a session runs (`add`), e.g. by the client token that is issued
    after the session started; `redact` uses the values it holds at the moment of the call.
    """

    def __init__(self, values: Mapping[str, str] | None = None) -> None:
        """Create the set.

        Args:
            values: Secret value -> placeholder type (e.g. "api key"); None starts empty.
                Values shorter than `MIN_SECRET_CHARS` are left out.
        """
        self._values: dict[str, str] = {}
        for value, kind in (values or {}).items():
            self.add(value, kind)

    def add(self, value: str, kind: str) -> None:
        """Add one secret value.

        Args:
            value: The secret. A value shorter than `MIN_SECRET_CHARS` characters (after
                stripping white space) is ignored.
            kind: The type its placeholder names, e.g. "client token".
        """
        value = value.strip()
        if len(value) >= MIN_SECRET_CHARS:
            self._values.setdefault(value, kind)

    def update(self, other: Secrets) -> None:
        """Add every value of another set.

        Args:
            other: The set to take the values from; it is not changed.
        """
        for value, kind in other._values.items():
            self._values.setdefault(value, kind)

    def __len__(self) -> int:
        """Count the values.

        Returns:
            How many secret values the set holds.
        """
        return len(self._values)

    def redact(self, text: str) -> str:
        """Replace every occurrence of a known secret value by its placeholder.

        Args:
            text: Any text.

        Returns:
            The text with each value replaced by `placeholder(kind)`. Longer values are
            replaced first, so a value that contains another one is replaced as a whole.
        """
        for value in sorted(self._values, key=len, reverse=True):
            if value in text:
                text = text.replace(value, placeholder(self._values[value]))
        return text

    @classmethod
    def from_environment(
        cls, names: Iterable[str] = (), environ: Mapping[str, str] | None = None
    ) -> Secrets:
        """Collect the secret values that are present in the process environment.

        Collected are the values of the variables in `names` (the variables calu hides from
        Claude Code: provider keys, the names of calu's env file, `hidden_env`), the values of
        every variable whose name says that it holds a credential (a KEY, TOKEN, SECRET,
        PASSWORD or CREDENTIAL word in its name), and the password of every variable whose
        value is a URL with `user:password@`.

        Args:
            names: Names of variables whose values are secret whatever they are called.
            environ: The environment to read; None uses `os.environ`.

        Returns:
            The set. A variable that is not set, or whose value is shorter than
            `MIN_SECRET_CHARS`, contributes nothing.
        """
        env = os.environ if environ is None else environ
        secrets = cls()
        for name in names:
            value = env.get(name)
            if value:
                secrets.add(value, kind_of_variable(name))
        for name, value in env.items():
            if _CREDENTIAL_NAME.search(name):
                secrets.add(value, kind_of_variable(name))
            found = _URL_PASSWORD.match(value)
            if found:
                secrets.add(found.group(1), "password")
        return secrets


def _conninfo_line(match: re.Match[str]) -> str:
    """Replace the password of one line that is a libpq connection string.

    Args:
        match: A match of `_CONNINFO_PASSWORD`: group 1 is `password=`, group 2 the value.

    Returns:
        The matched text with the value replaced, if the line also names a host, a database
        or a user as a connection string does; otherwise the matched text unchanged.
    """
    line_start = match.string.rfind("\n", 0, match.start()) + 1
    line_end = match.string.find("\n", match.end())
    line = match.string[line_start : len(match.string) if line_end < 0 else line_end]
    if not re.search(r"\b(host|hostaddr|dbname|user|port)=", line):
        return match.group(0)
    return match.group(1) + placeholder("password")


_CONNINFO_PASSWORD = re.compile(
    r"(?m)(?:^|(?<=[\s\"']))(password=)('[^'\n]*'|[^\s'\"`&;,)\]\[]+)(?=[\s'\"]|$)"
)
"""`password=value` as a libpq connection string writes it; `_conninfo_line` decides."""

SHAPES: tuple[tuple[str, re.Pattern[str], str | Callable[[re.Match[str]], str]], ...] = (
    (
        "private key block",
        # The body is base64 lines; `\n` written out (a key inside JSON) counts as a line end.
        # An unfinished block is replaced up to where the base64 text ends.
        re.compile(
            r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----"
            r"(?:[A-Za-z0-9+/=\s]|\\n|\\r)*"
            r"(?:-----END [A-Z0-9 ]*PRIVATE KEY-----)?"
        ),
        placeholder("private key"),
    ),
    (
        "AWS access key id",
        re.compile(r"\b(?:AKIA|ASIA|AIDA|AROA|AGPA|ANPA|AIPA|ANVA)[A-Z0-9]{16}\b"),
        placeholder("aws access key"),
    ),
    (
        "GitHub token",
        re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{22,})\b"),
        placeholder("github token"),
    ),
    (
        "sk- key",
        # With a digit in it: a hyphenated name such as a CSS class has none.
        re.compile(r"\bsk-(?=[A-Za-z_-]*[0-9])[A-Za-z0-9_-]{20,}"),
        placeholder("api key"),
    ),
    (
        "Google API key",
        re.compile(r"\bAIza[0-9A-Za-z_-]{35}\b"),
        placeholder("api key"),
    ),
    (
        "Slack token",
        re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}"),
        placeholder("token"),
    ),
    (
        "JSON Web Token",
        re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}"),
        placeholder("jwt"),
    ),
    (
        "bearer token",
        re.compile(r"(?i)(\bauthorization\s*[:=]\s*[\"']?bearer\s+)[A-Za-z0-9._~+/=-]{16,}"),
        lambda match: match.group(1) + placeholder("token"),
    ),
    (
        "password in a URL",
        re.compile(r"([a-zA-Z][a-zA-Z0-9+.-]*://[^\s/:@\"'`]*:)[^\s/@\"'`]+@"),
        lambda match: match.group(1) + placeholder("password") + "@",
    ),
    (
        "secret in a URL's query",
        re.compile(
            r"(?i)([?&](?:password|passwd|pwd|secret|client_secret|token|access_token|"
            r"api_key|apikey|signature)=)[^&\s#\"'`<>)\]]+"
        ),
        lambda match: match.group(1) + placeholder("secret value"),
    ),
    ("password in a connection string", _CONNINFO_PASSWORD, _conninfo_line),
)
"""The shapes stage 1 replaces: (name, pattern, replacement). Each pattern matches a value
that is a credential by its form alone, so that ordinary text and code are left as they
are; what only a reader can recognise is the redaction agent's part."""


def redact_shapes(text: str) -> str:
    """Replace the common shapes of keys, tokens and passwords by typed placeholders.

    Args:
        text: Any text.

    Returns:
        The text with every match of `SHAPES` replaced. A placeholder is not matched again.
    """
    for _name, pattern, replacement in SHAPES:
        text = pattern.sub(replacement, text)
    return text


def redact_deterministic(text: str, secrets: Secrets) -> str:
    """Run stage 1: the known secret values, then the shapes.

    Args:
        text: Any text.
        secrets: The values calu knows to be secret.

    Returns:
        The redacted text.
    """
    return redact_shapes(secrets.redact(text))


class RedactionFailed(Exception):
    """The redaction agent gave no usable answer; the text must not be written."""


class Redaction(BaseModel):
    """The redaction agent's output."""

    text: str = Field(
        description="The excerpt with secrets and personal data replaced by placeholders of "
        "the form `[redacted: <type>]`, and otherwise exactly as given."
    )


def chunks(text: str, limit: int = CHUNK_CHARS) -> list[str]:
    """Cut a text into pieces of at most `limit` characters, at line ends where possible.

    Args:
        text: The text.
        limit: The largest size of a piece; at least 1.

    Returns:
        The pieces in order; joined, they are `text`. A line longer than `limit` is cut inside
        the line. An empty text gives no piece.
    """
    pieces: list[str] = []
    current = ""
    for line in text.splitlines(keepends=True):
        while len(line) > limit:
            if current:
                pieces.append(current)
                current = ""
            pieces.append(line[:limit])
            line = line[limit:]
        if len(current) + len(line) > limit:
            pieces.append(current)
            current = ""
        current += line
    if current:
        pieces.append(current)
    return pieces


def checked(original: str, answer: str) -> str:
    """Check one answer of the redaction agent against the text it was given.

    Args:
        original: The piece of text the agent read.
        answer: What it returned.

    Returns:
        The answer, with the line end of `original` restored (models drop a trailing newline).

    Raises:
        RedactionFailed: If the answer is empty although the text was not, or is longer than
            `MAX_GROWTH` times the text plus `GROWTH_ALLOWANCE` characters.
    """
    if original.strip() and not answer.strip():
        raise RedactionFailed("the answer is empty")
    if len(answer) > len(original) * MAX_GROWTH + GROWTH_ALLOWANCE:
        raise RedactionFailed(
            f"the answer has {len(answer)} characters for a text of {len(original)}"
        )
    body = answer.rstrip("\n")
    return body + original[len(original.rstrip("\n")) :]


@dataclass
class Redactor:
    """The redaction agent: replaces secrets and personal data in a text (stage 2).

    `model` is a Pydantic AI model string or a `Model`. The instructions are fixed
    (`calu.defaults.instructions.redaction_instructions`); the caller passes them, so that
    this module reads no file.
    """

    instructions: str
    model: str | Model
    timeout_s: float = 60.0
    """Seconds one text may take in all (all its pieces together)."""
    usage_limits: UsageLimits = field(
        default_factory=lambda: UsageLimits(request_limit=2, output_tokens_limit=16_000)
    )
    _agent: Agent[None, Redaction] | None = field(default=None, init=False, repr=False)

    @property
    def agent(self) -> Agent[None, Redaction]:
        """The Pydantic AI agent, built at the first use.

        Returns:
            The agent.
        """
        if self._agent is None:
            self._agent = Agent(
                output_type=Redaction, instructions=self.instructions, name="trace-redactor"
            )
        return self._agent

    async def redact(self, text: str) -> str:
        """Let the agent replace secrets and personal data in `text`.

        Side effects: one model call per piece of `chunks(text)`.

        Args:
            text: The text, after stage 1.

        Returns:
            The redacted text; a text without visible characters unchanged, without a model
            call.

        Raises:
            RedactionFailed: If the model failed, answered unusably (`checked`), or the text
                took longer than `timeout_s`. Nothing of `text` may be written then.
        """
        if not text.strip():
            return text
        parts: list[str] = []
        try:
            with anyio.fail_after(self.timeout_s):
                for piece in chunks(text):
                    result = await run_bounded(
                        self.agent,
                        "# Excerpt to redact\n\n" + fence(piece),
                        model=self.model,
                        usage_limits=self.usage_limits,
                        model_settings=ModelSettings(),
                        max_tokens=REDACTOR_MAX_TOKENS,
                    )
                    parts.append(checked(piece, result.output.text))
        except RedactionFailed:
            raise
        except TimeoutError as exc:
            raise RedactionFailed(f"no answer within {self.timeout_s:g} s") from exc
        except Exception as exc:
            raise RedactionFailed(f"{type(exc).__name__}: {exc}") from exc
        return "".join(parts)
