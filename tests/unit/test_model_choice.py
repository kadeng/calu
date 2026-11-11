"""Choosing the checkers' model from the credentials found (`calu.model_choice`, D129)."""

from typing import Any

from calu.model_choice import (
    ANTHROPIC_MODEL,
    BEDROCK_FALLBACK_PROFILE,
    FIREWORKS_MODEL,
    MISTRAL_MODEL,
    ModelChoice,
    bedrock_choice,
    detect_model,
)


def no_aws() -> None:
    """A Bedrock probe that finds no AWS credentials."""
    return None


ALL_KEYS = {"ANTHROPIC_API_KEY": "a", "FIREWORKS_API_KEY": "f", "MISTRAL_API_KEY": "m"}


def test_the_owners_order_of_providers_is_kept() -> None:
    """Bedrock, then Anthropic, then Fireworks, then Mistral: the first one found wins."""
    bedrock = ModelChoice("AWS Bedrock", "bedrock:eu.x", None, "sts")
    assert detect_model(ALL_KEYS, bedrock=lambda: bedrock) is bedrock

    anthropic = detect_model(ALL_KEYS, bedrock=no_aws)
    assert anthropic is not None and anthropic.model == ANTHROPIC_MODEL
    assert anthropic.effort == "medium", "the owner asked for medium thinking on Haiku"

    without_anthropic = {k: v for k, v in ALL_KEYS.items() if k != "ANTHROPIC_API_KEY"}
    fireworks = detect_model(without_anthropic, bedrock=no_aws)
    assert fireworks is not None and fireworks.model == FIREWORKS_MODEL

    mistral = detect_model({"MISTRAL_API_KEY": "m"}, bedrock=no_aws)
    assert mistral is not None and mistral.model == MISTRAL_MODEL

    # A key that is set but blank is not a key.
    assert detect_model({"ANTHROPIC_API_KEY": "  "}, bedrock=no_aws) is None
    assert detect_model({}, bedrock=no_aws) is None


class FakeClient:
    """Answers the two AWS calls the probe makes."""

    def __init__(self, profiles: list[str] | None, fail: bool = False) -> None:
        self.profiles = profiles
        self.fail = fail

    def get_caller_identity(self) -> dict[str, str]:
        if self.fail:
            raise RuntimeError("InvalidClientTokenId")
        return {"Account": "123456789012"}

    def list_inference_profiles(self, **_kwargs: Any) -> dict[str, Any]:
        if self.profiles is None:
            raise RuntimeError("AccessDeniedException")
        return {
            "inferenceProfileSummaries": [
                {"inferenceProfileId": name, "status": "ACTIVE"} for name in self.profiles
            ]
        }


class FakeSession:
    """A boto3 session with or without credentials."""

    def __init__(self, credentials: bool, client: FakeClient, region: str | None = None) -> None:
        self.credentials = credentials
        self.fake = client
        self.region_name = region
        self.listed_from: list[str | None] = []

    def get_credentials(self) -> object | None:
        return object() if self.credentials else None

    def client(self, name: str, region_name: str | None = None) -> FakeClient:
        if name == "bedrock":
            self.listed_from.append(region_name)
        return self.fake


def test_bedrock_picks_the_newest_eu_haiku_profile_the_account_lists() -> None:
    """Discovered, not hard-coded: AWS adds profile IDs as models are released."""
    session = FakeSession(
        True,
        FakeClient(
            [
                "us.anthropic.claude-haiku-5-5-v1:0",
                "eu.anthropic.claude-haiku-4-5-20251001-v1:0",
                "eu.anthropic.claude-haiku-5-5-v1:0",
                "eu.anthropic.claude-sonnet-5-5-v1:0",
            ]
        ),
        region="eu-west-1",
    )
    choice = bedrock_choice(lambda: session)
    assert choice is not None
    assert choice.model == "bedrock:eu.anthropic.claude-haiku-5-5-v1:0"
    assert choice.effort is None, "thinking stays off on Bedrock until it is measured there"
    assert session.listed_from[0] == "eu-west-1", "a configured EU region is asked first"
    assert "123456789012" not in choice.reason, "the account number is not printed whole"


def test_bedrock_falls_back_when_the_account_may_not_list_profiles() -> None:
    choice = bedrock_choice(lambda: FakeSession(True, FakeClient(None)))
    assert choice is not None and choice.model == f"bedrock:{BEDROCK_FALLBACK_PROFILE}"
    assert "calu evals" in choice.reason, "and says how to check it"


def test_no_bedrock_without_credentials_that_sts_accepts() -> None:
    assert bedrock_choice(lambda: FakeSession(False, FakeClient([]))) is None
    assert bedrock_choice(lambda: FakeSession(True, FakeClient([], fail=True))) is None

    def broken() -> FakeSession:
        raise ImportError("no boto3")

    assert bedrock_choice(broken) is None
