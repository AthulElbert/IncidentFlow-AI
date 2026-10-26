"""A scripted stand-in for the LLM client: replays canned replies and records what it was shown."""


class ScriptedLLM:
    def __init__(self, *replies):
        self.replies = list(replies)
        self.calls: list[list[dict]] = []

    def complete_json(self, messages, error_label="LLM"):
        self.calls.append([dict(m) for m in messages])
        reply = self.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return reply
