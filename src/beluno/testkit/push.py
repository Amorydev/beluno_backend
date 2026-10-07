"""A push sender that records what would go to FCM, with scripted answers per token."""

from __future__ import annotations

from dataclasses import dataclass, field

from beluno.push import PushMessage, PushResult


@dataclass
class RecordingPushSender:
    sent: list[PushMessage] = field(default_factory=list)
    answers: dict[str, PushResult] = field(default_factory=dict)  # token -> result

    async def send_many(self, messages: list[PushMessage]) -> list[PushResult]:
        self.sent.extend(messages)
        return [self.answers.get(message.token, PushResult.DELIVERED) for message in messages]

    def to(self, token: str) -> list[PushMessage]:
        return [message for message in self.sent if message.token == token]
