"""Tests for worktrees_hives.attribution module."""

from __future__ import annotations

import pytest

from worktrees_hives.attribution import (
    DEFAULT_AGENT_ID,
    AttributionConfig,
    AttributionPlacement,
    ReplyTemplate,
    format_attribution,
    format_reply,
)


class TestAttributionConfig:
    """Tests for AttributionConfig."""

    def test_default_values(self) -> None:
        config = AttributionConfig()
        assert config.agent_id == DEFAULT_AGENT_ID
        assert config.include_sha_on_fix is True
        assert config.placement == AttributionPlacement.FOOTER

    def test_custom_values(self) -> None:
        config = AttributionConfig(
            agent_id="custom agent",
            include_sha_on_fix=False,
            placement=AttributionPlacement.HEADER,
        )
        assert config.agent_id == "custom agent"
        assert config.include_sha_on_fix is False
        assert config.placement == AttributionPlacement.HEADER

    def test_for_platform(self) -> None:
        config = AttributionConfig.for_platform("Claude Code")
        assert config.agent_id == "Claude Code: worktrees-hives agent"
        assert config.include_sha_on_fix is True
        assert config.placement == AttributionPlacement.FOOTER

    def test_for_platform_with_overrides(self) -> None:
        config = AttributionConfig.for_platform(
            "Codex",
            include_sha_on_fix=False,
            placement=AttributionPlacement.HEADER,
        )
        assert config.agent_id == "Codex: worktrees-hives agent"
        assert config.include_sha_on_fix is False
        assert config.placement == AttributionPlacement.HEADER

    def test_frozen(self) -> None:
        config = AttributionConfig()
        with pytest.raises(AttributeError):
            config.agent_id = "new"  # type: ignore[misc]


class TestAttributionPlacementCoerce:
    """Tests for AttributionPlacement.coerce."""

    def test_coerce_enum(self) -> None:
        result = AttributionPlacement.coerce(AttributionPlacement.HEADER)
        assert result == AttributionPlacement.HEADER

    def test_coerce_string_header(self) -> None:
        assert AttributionPlacement.coerce("header") == AttributionPlacement.HEADER

    def test_coerce_string_footer(self) -> None:
        assert AttributionPlacement.coerce("footer") == AttributionPlacement.FOOTER

    def test_coerce_invalid_string(self) -> None:
        assert AttributionPlacement.coerce("invalid") == AttributionPlacement.FOOTER


class TestFormatAttribution:
    """Tests for format_attribution."""

    def test_without_sha(self) -> None:
        config = AttributionConfig()
        result = format_attribution(config)
        assert result == DEFAULT_AGENT_ID

    def test_with_sha(self) -> None:
        config = AttributionConfig()
        result = format_attribution(config, commit_sha="abc1234")
        assert result == f"{DEFAULT_AGENT_ID}: fixed in abc1234"

    def test_with_sha_always_included(self) -> None:
        config = AttributionConfig(include_sha_on_fix=False)
        result = format_attribution(config, commit_sha="abc1234")
        assert result == f"{DEFAULT_AGENT_ID}: fixed in abc1234"

    def test_with_sha_none(self) -> None:
        config = AttributionConfig()
        result = format_attribution(config, commit_sha=None)
        assert result == DEFAULT_AGENT_ID

    def test_with_sha_empty_string(self) -> None:
        config = AttributionConfig()
        result = format_attribution(config, commit_sha="")
        assert result == DEFAULT_AGENT_ID

    def test_custom_agent_id(self) -> None:
        config = AttributionConfig(agent_id="Custom Bot")
        result = format_attribution(config, commit_sha="abc1234")
        assert result == "Custom Bot: fixed in abc1234"


class TestReplyTemplate:
    """Tests for ReplyTemplate."""

    def test_thread_reply_footer(self) -> None:
        template = ReplyTemplate(body="Looks good!")
        result = template.render()
        assert result == "Looks good!\n\n---\nworktrees-hives agent"

    def test_thread_reply_header(self) -> None:
        config = AttributionConfig(placement=AttributionPlacement.HEADER)
        template = ReplyTemplate(body="Looks good!", attribution_config=config)
        result = template.render()
        assert result == "worktrees-hives agent\n\n---\nLooks good!"

    def test_pr_comment_footer(self) -> None:
        template = ReplyTemplate(body="All checks passed.", is_thread_reply=False)
        result = template.render()
        assert result == "All checks passed.\n\nworktrees-hives agent"

    def test_pr_comment_header(self) -> None:
        config = AttributionConfig(placement=AttributionPlacement.HEADER)
        template = ReplyTemplate(
            body="All checks passed.",
            attribution_config=config,
            is_thread_reply=False,
        )
        result = template.render()
        assert result == "worktrees-hives agent\n\nAll checks passed."

    def test_with_commit_sha(self) -> None:
        template = ReplyTemplate(
            body="Fixed the issue.",
            commit_sha="abc1234",
        )
        result = template.render()
        assert result == "Fixed the issue.\n\n---\nworktrees-hives agent: fixed in abc1234"

    def test_with_commit_sha_header(self) -> None:
        config = AttributionConfig(placement=AttributionPlacement.HEADER)
        template = ReplyTemplate(
            body="Fixed the issue.",
            attribution_config=config,
            commit_sha="abc1234",
        )
        result = template.render()
        assert result == "worktrees-hives agent: fixed in abc1234\n\n---\nFixed the issue."

    def test_frozen(self) -> None:
        template = ReplyTemplate(body="test")
        with pytest.raises(AttributeError):
            template.body = "new"  # type: ignore[misc]


class TestFormatReply:
    """Tests for format_reply convenience function."""

    def test_defaults(self) -> None:
        result = format_reply("Looks good!")
        assert result == "Looks good!\n\n---\nworktrees-hives agent"

    def test_with_config(self) -> None:
        config = AttributionConfig(agent_id="Custom Bot")
        result = format_reply("Looks good!", config=config)
        assert result == "Looks good!\n\n---\nCustom Bot"

    def test_with_commit_sha(self) -> None:
        result = format_reply("Fixed it.", commit_sha="abc1234")
        assert result == "Fixed it.\n\n---\nworktrees-hives agent: fixed in abc1234"

    def test_pr_comment(self) -> None:
        result = format_reply("All done.", is_thread_reply=False)
        assert result == "All done.\n\nworktrees-hives agent"

    def test_full_scenario(self) -> None:
        config = AttributionConfig.for_platform("Claude Code")
        result = format_reply(
            "Resolved thread.",
            config=config,
            commit_sha="def5678",
            is_thread_reply=True,
        )
        expected = "Resolved thread.\n\n---\nClaude Code: worktrees-hives agent: fixed in def5678"
        assert result == expected

    def test_sha_always_included_when_provided(self) -> None:
        config = AttributionConfig(include_sha_on_fix=False)
        result = format_reply("Fixed.", config=config, commit_sha="abc1234")
        assert result == "Fixed.\n\n---\nworktrees-hives agent: fixed in abc1234"
