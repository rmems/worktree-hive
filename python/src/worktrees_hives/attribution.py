"""Reply attribution configuration and templates for worktrees-hives agents.

Provides configurable attribution for automated PR review replies and comments.
Ensures transparency by identifying which automation stack responded and,
when code changed, which commit landed the fix.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum

DEFAULT_AGENT_ID = "worktrees-hives agent"


class AttributionPlacement(Enum):
    """Where attribution appears in a reply."""

    FOOTER = "footer"
    HEADER = "header"


@dataclass(frozen=True)
class AttributionConfig:
    """Configuration for reply attribution.

    Attributes:
        agent_id: Identity line on replies (e.g. "worktrees-hives agent",
                  "Claude Code: worktrees-hives agent").
        include_sha_on_fix: Whether to attach commit SHA after code fixes.
        placement: Where the attribution line appears (footer or header).
    """

    agent_id: str = DEFAULT_AGENT_ID
    include_sha_on_fix: bool = True
    placement: AttributionPlacement = AttributionPlacement.FOOTER

    @classmethod
    def for_platform(cls, platform: str, **kwargs: object) -> AttributionConfig:
        """Create config with platform-specific agent_id.

        Args:
            platform: Platform name (e.g. "Claude Code", "Codex", "OpenClaw").
            **kwargs: Additional config overrides.

        Returns:
            AttributionConfig with platform-specific agent_id.
        """
        agent_id = f"{platform}: worktrees-hives agent"
        return cls(agent_id=agent_id, **kwargs)  # type: ignore[arg-type]


def format_attribution(
    config: AttributionConfig,
    commit_sha: str | None = None,
) -> str:
    """Format the attribution line.

    Args:
        config: Attribution configuration.
        commit_sha: Optional commit SHA to include (only when code was fixed).

    Returns:
        Formatted attribution string.
    """
    if config.include_sha_on_fix and commit_sha:
        return f"{config.agent_id}: fixed in {commit_sha}"
    return config.agent_id


@dataclass(frozen=True)
class ReplyTemplate:
    """Template for formatting automated replies.

    Attributes:
        body: The main reply content.
        attribution_config: Attribution configuration.
        commit_sha: Optional commit SHA (set when code fix was pushed).
        is_thread_reply: Whether this is a thread reply (vs PR comment).
    """

    body: str
    attribution_config: AttributionConfig = field(default_factory=AttributionConfig)
    commit_sha: str | None = None
    is_thread_reply: bool = True

    def render(self) -> str:
        """Render the full reply with attribution.

        Returns:
            Complete reply text with attribution placed according to config.
        """
        attribution = format_attribution(
            self.attribution_config,
            commit_sha=self.commit_sha,
        )
        separator = "---\n" if self.is_thread_reply else "\n"

        if self.attribution_config.placement == AttributionPlacement.HEADER:
            return f"{attribution}\n{separator}{self.body}"
        return f"{self.body}\n{separator}{attribution}"


def format_reply(
    body: str,
    config: AttributionConfig | None = None,
    commit_sha: str | None = None,
    is_thread_reply: bool = True,
) -> str:
    """Format a reply with attribution.

    Convenience function that creates a ReplyTemplate and renders it.

    Args:
        body: The main reply content.
        config: Attribution configuration (uses defaults if None).
        commit_sha: Optional commit SHA (set when code fix was pushed).
        is_thread_reply: Whether this is a thread reply (vs PR comment).

    Returns:
        Complete reply text with attribution.
    """
    if config is None:
        config = AttributionConfig()
    template = ReplyTemplate(
        body=body,
        attribution_config=config,
        commit_sha=commit_sha,
        is_thread_reply=is_thread_reply,
    )
    return template.render()
