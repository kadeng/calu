"""Choose the checkers' model from the credentials this machine has (PLAN.md D129).

A new installation should not need a model string typed in before its first check. The order
is the owner's, and the first provider whose credentials are present wins:

    1. AWS Bedrock    credentials that STS accepts  -> Claude Haiku, EU inference profile
    2. Anthropic      ANTHROPIC_API_KEY             -> Claude Haiku, effort "medium"
    3. Fireworks      FIREWORKS_API_KEY             -> Nemotron Lightning (as stay-in-the-loop)
    4. Mistral        MISTRAL_API_KEY               -> Mistral Medium

Bedrock is checked by asking STS who the caller is, because AWS credentials come from many
places (environment, `~/.aws`, an instance role, SSO) and their presence alone says nothing.
The API keys are checked by presence, as the owner specified: a key that is set but expired is
the provider's error at the first check, which escalates the step and names it -- `calu doctor`
is where to look then.

On Bedrock the EU profile is **discovered** with `ListInferenceProfiles` from an EU source
region rather than written in here. AWS recommends exactly that (profile IDs are added as models
are), and it means calu only offers a profile the account can call. When listing is not allowed,
the fallback is a profile ID calu has used before.

Effort "medium" is set for Haiku on the Anthropic API, where it was measured on 2026-11-11:
three known cases answered correctly with the structured decision. It is **not** set on
Bedrock: `calu.models.resolve_model` turns thinking off for Claude on Bedrock because, when last
measured there, Pydantic AI could not force the structured answer with thinking on, and a run's
effort would override that. Re-measure with `calu evals` on an account that has Bedrock before
changing it.
"""

import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

from calu.checkers.model import CheckerEffort

FIREWORKS_MODEL = "fireworks:accounts/fireworks/models/nemotron-lightning-3p5-30b-a3b"
"""The model stay-in-the-loop measures its checkers with (its CLAUDE.md)."""

MISTRAL_MODEL = "mistral:mistral-medium-latest"
"""Mistral Medium, by Mistral's `-latest` alias -- the same convention as the narrator default
`ministral-3b-latest`. Not verified against the model list: the key on the development machine
had expired when this was written."""

ANTHROPIC_MODEL = "anthropic:claude-haiku-5-5"
"""The current Haiku on the Anthropic API."""

BEDROCK_FALLBACK_PROFILE = "eu.anthropic.claude-haiku-4-5-20251001-v1:0"
"""The EU Haiku profile used when the account may not list profiles (it was used before)."""

EU_REGIONS = ("eu-central-1", "eu-west-1", "eu-west-3", "eu-north-1", "eu-south-1")
"""Source regions to list EU profiles from, the first one tried first."""


@dataclass(frozen=True)
class ModelChoice:
    """The model the checkers, the narrator and the summarizer should use, and why."""

    provider: str
    """"AWS Bedrock", "Anthropic", "Fireworks" or "Mistral"."""
    model: str
    """The Pydantic AI model string."""
    effort: CheckerEffort | None
    """Thinking effort for the checkers, or None for the provider's default."""
    reason: str
    """What was found that made this the choice, for the report."""


def _haiku_rank(profile_id: str) -> tuple[int, ...]:
    """Order Haiku profile IDs so that the newest comes last.

    Args:
        profile_id: E.g. `eu.anthropic.claude-haiku-4-5-20251001-v1:0`.

    Returns:
        The version numbers in the ID, e.g. (4, 5, 20251001, 1, 0).
    """
    return tuple(int(number) for number in re.findall(r"\d+", profile_id))


def bedrock_choice(
    session_factory: Callable[[], Any] | None = None, region: str | None = None
) -> ModelChoice | None:
    """Return the Bedrock choice if this machine has AWS credentials that STS accepts.

    Side effects: up to two AWS API calls, STS `GetCallerIdentity` and Bedrock
    `ListInferenceProfiles`; neither creates or changes anything.

    Args:
        session_factory: Builds a `boto3.session.Session`; None uses boto3 itself. Tests pass a
            stand-in.
        region: An EU source region to list profiles from; None tries `AWS_REGION` when it is
            an EU region, then `EU_REGIONS`.

    Returns:
        The choice, or None when boto3 is missing, there are no credentials, or STS refuses
        them.
    """
    try:
        if session_factory is None:
            # Optional: boto3 comes with the Bedrock extra of Pydantic AI.
            from boto3.session import Session

            session_factory = Session
        session = session_factory()
        if session.get_credentials() is None:
            return None
        account = session.client("sts").get_caller_identity().get("Account", "")
    except Exception:  # every failure means "no usable AWS credentials"
        return None
    configured = region or getattr(session, "region_name", None) or ""
    regions = [configured] if configured.startswith("eu-") else []
    regions += [name for name in EU_REGIONS if name not in regions]
    for name in regions:
        try:
            listed = session.client("bedrock", region_name=name).list_inference_profiles(
                typeEquals="SYSTEM_DEFINED"
            )
        except Exception:  # not allowed, or not offered there: try the next region
            continue
        found = [
            summary["inferenceProfileId"]
            for summary in listed.get("inferenceProfileSummaries", [])
            if str(summary.get("inferenceProfileId", "")).startswith("eu.anthropic.claude-haiku")
            and summary.get("status", "ACTIVE") == "ACTIVE"
        ]
        if found:
            newest = max(found, key=_haiku_rank)
            return ModelChoice(
                "AWS Bedrock",
                f"bedrock:{newest}",
                None,
                f"AWS credentials of account {account[-4:].rjust(len(account), '*')} "
                f"(the EU Haiku profile listed from {name}; set AWS_REGION={name})",
            )
    return ModelChoice(
        "AWS Bedrock",
        f"bedrock:{BEDROCK_FALLBACK_PROFILE}",
        None,
        "AWS credentials that STS accepts; the account may not list inference profiles, so "
        "the EU Haiku profile is the one calu has used before -- check it with `calu evals`",
    )


def detect_model(
    environ: Mapping[str, str], bedrock: Callable[[], ModelChoice | None] = bedrock_choice
) -> ModelChoice | None:
    """Choose the model by the owner's order of providers.

    Args:
        environ: The environment to look for API keys in, with calu's own `.env` already loaded
            into it (`calu.config.load_secrets`).
        bedrock: The Bedrock probe; tests pass a stand-in so that no AWS call is made.

    Returns:
        The first provider whose credentials are there, or None when there is none -- then the
        caller keeps the configured models and says how to add a key, or that `claude:haiku`
        runs the checks on this machine's own Claude Code account (D117).
    """
    found = bedrock()
    if found is not None:
        return found
    if environ.get("ANTHROPIC_API_KEY", "").strip():
        return ModelChoice(
            "Anthropic", ANTHROPIC_MODEL, "medium", "ANTHROPIC_API_KEY is set (effort medium)"
        )
    if environ.get("FIREWORKS_API_KEY", "").strip():
        return ModelChoice("Fireworks", FIREWORKS_MODEL, None, "FIREWORKS_API_KEY is set")
    if environ.get("MISTRAL_API_KEY", "").strip():
        return ModelChoice("Mistral", MISTRAL_MODEL, None, "MISTRAL_API_KEY is set")
    return None
