"""Optional Claude layer: a slow, high-level second opinion.

Deliberately narrow scope. Claude does *not* pick entries - an API round trip
takes seconds and the setups this engine trades last minutes, so by the time a
reply arrives the tick that triggered it is history. What it can do is look at
the wider picture every few minutes and answer one question: does the current
regime look tradable, and in which direction is the risk skewed?

Its verdict can only ever *remove* trades. `avoid`/`wait` blocks, a bias that
disagrees with the technical signal blocks, agreement changes nothing. That
asymmetry is on purpose - a language model's market opinion is not an edge, but
it is a reasonable filter for "something odd is going on".

Requires ANTHROPIC_API_KEY. Without it the engine runs unchanged and every
verdict is neutral.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

log = logging.getLogger("analyst")

VERDICT_SCHEMA = {
    "type": "object",
    "properties": {
        "bias": {
            "type": "string",
            "enum": ["bullish", "bearish", "neutral"],
            "description": "Directional skew over the next 15-60 minutes.",
        },
        "confidence": {
            "type": "number",
            "description": "0.0 (no view) to 1.0 (strong view).",
        },
        "action": {
            "type": "string",
            "enum": ["trade", "wait", "avoid"],
            "description": (
                "trade = conditions are normal; wait = unclear, sit out; "
                "avoid = conditions are dangerous (news, thin liquidity, "
                "erratic spread)."
            ),
        },
        "reasoning": {
            "type": "string",
            "description": "One sentence, max 200 characters.",
        },
    },
    "required": ["bias", "confidence", "action", "reasoning"],
    "additionalProperties": False,
}

SYSTEM_PROMPT = """You are a risk filter for an automated intraday trading engine.

You are given a market snapshot. Judge whether the current conditions are
suitable for a short-horizon trend-continuation trade, and which way the risk
is skewed. You are not picking entries - the engine does that.

Be conservative. Prefer "wait" when the picture is mixed and "avoid" when a
high-impact release is close, the spread is unstable, or the data looks
inconsistent. A missed trade costs nothing; a bad one costs real money. Never
claim certainty about direction."""


@dataclass
class Verdict:
    bias: str = "neutral"
    confidence: float = 0.0
    action: str = "trade"
    reasoning: str = "analyst disabled"
    created_at: datetime = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        if self.created_at is None:
            self.created_at = datetime.now(timezone.utc)

    def blocks(self, direction: str) -> str | None:
        """Reason this verdict vetoes `direction`, or None."""
        if self.action == "avoid":
            return f"analyst says avoid: {self.reasoning}"
        if self.action == "wait":
            return f"analyst says wait: {self.reasoning}"
        if direction == "BUY" and self.bias == "bearish" and self.confidence >= 0.5:
            return f"analyst is bearish: {self.reasoning}"
        if direction == "SELL" and self.bias == "bullish" and self.confidence >= 0.5:
            return f"analyst is bullish: {self.reasoning}"
        return None


NEUTRAL = Verdict()


class Analyst:
    """Caches one verdict per symbol and refreshes it on an interval."""

    def __init__(
        self,
        api_key: str,
        model: str = "claude-opus-5",
        interval_minutes: int = 10,
    ) -> None:
        self.model = model
        self.interval = timedelta(minutes=interval_minutes)
        self._client = None
        self._verdicts: dict[str, Verdict] = {}
        self._requested_at: dict[str, datetime] = {}
        self.enabled = bool(api_key)
        self.last_error = ""
        # Token usage from the most recent call, so the cost of this layer can
        # be measured rather than guessed.
        self.last_usage: dict | None = None

        if self.enabled:
            try:
                import anthropic

                self._client = anthropic.Anthropic(api_key=api_key)
            except Exception as error:  # missing package, bad key format, ...
                self.enabled = False
                self.last_error = str(error)
                log.warning("Analyst disabled: %s", error)

    def verdict(self, symbol: str) -> Verdict:
        return self._verdicts.get(symbol, NEUTRAL)

    def needs_refresh(self, symbol: str, now: datetime | None = None) -> bool:
        if not self.enabled:
            return False
        now = now or datetime.now(timezone.utc)
        requested = self._requested_at.get(symbol)
        return requested is None or now - requested >= self.interval

    def mark_requested(self, symbol: str, now: datetime | None = None) -> None:
        self._requested_at[symbol] = now or datetime.now(timezone.utc)

    def store(self, symbol: str, verdict: Verdict) -> None:
        self._verdicts[symbol] = verdict
        log.info(
            "Analyst %s: %s / %s (%.2f) - %s",
            symbol,
            verdict.action,
            verdict.bias,
            verdict.confidence,
            verdict.reasoning,
        )

    # -- blocking call; run it in a thread -----------------------------------

    def analyse(self, snapshot: dict) -> Verdict:
        """Blocking API call. Run it off the trade path via `engine._in_thread`."""
        if not self.enabled or self._client is None:
            return NEUTRAL

        prompt = (
            "Market snapshot (JSON):\n"
            f"{json.dumps(snapshot, indent=2, default=str)}\n\n"
            "Assess these conditions."
        )
        response = self._client.messages.create(
            model=self.model,
            # Thinking and response text share this budget. Adaptive thinking is
            # on by default on Opus 5, so a tight cap truncates the JSON and the
            # parse below blows up - keep the headroom even though the verdict
            # itself is tiny.
            max_tokens=16000,
            system=SYSTEM_PROMPT,
            thinking={"type": "adaptive"},
            output_config={
                "effort": "low",
                "format": {"type": "json_schema", "schema": VERDICT_SCHEMA},
            },
            messages=[{"role": "user", "content": prompt}],
        )

        usage = getattr(response, "usage", None)
        if usage is not None:
            self.last_usage = {
                "input_tokens": getattr(usage, "input_tokens", 0),
                "output_tokens": getattr(usage, "output_tokens", 0),
                "cache_read_input_tokens": getattr(
                    usage, "cache_read_input_tokens", 0
                ),
            }

        # Both of these fail safe: an unusable answer becomes "wait", which
        # blocks the trade rather than letting a half-parsed verdict through.
        if response.stop_reason == "refusal":
            return Verdict(action="wait", reasoning="model declined to answer")
        if response.stop_reason == "max_tokens":
            return Verdict(action="wait", reasoning="response truncated")

        text = next(
            (block.text for block in response.content if block.type == "text"), ""
        )
        data = json.loads(text)
        return Verdict(
            bias=data.get("bias", "neutral"),
            confidence=float(data.get("confidence", 0.0)),
            action=data.get("action", "wait"),
            reasoning=str(data.get("reasoning", ""))[:200],
        )
