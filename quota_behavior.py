"""Detect a model parking itself for quota recovery before the handoff threshold.

This is advisory ONLY. It does not change thresholds, grant permission, or
advance quotas. Detection is intentionally conservative to avoid mistaking a
quoted procedure, a negated statement or a real user blocker for a refusal.
"""
from __future__ import annotations
import re

# The model's own decision to wait/stop, not a description of watchdog behaviour.
_INTENT = re.compile(
    r"\b(?:i(?:'m| am|'ll| will| should| need to| have to| plan to)?\s+"
    r"(?:just\s+)?(?:going to\s+)?|we(?:'re| are|'ll| will| should| need to)?\s+"
    r"(?:going to\s+)?|(?:my\s+)?next\s+step\s+is\s+to\s+)"
    r"(?:wait(?:ing)?|paus(?:e|ing)|hold(?:ing)?|stop(?:ping)?|sit(?:ting)?\s+idle)\b"
    r"|^(?:waiting|pausing|holding|stopping)\b",
    re.IGNORECASE,
)
_WAIT_TARGET = re.compile(r"\b(?:quota|rate\s*limit|usage\s*limit|token\s*budget|five.hour|5.hour|weekly\s*limit|reset|capacity)\b", re.IGNORECASE)
_NEGATED = re.compile(
    r"\b(?:i\s+(?:am\s+not)|i'm\s+not|won't|will\s+not|should(?:n't|\s+not)|do\s+not|don't|not\s+going\s+to|no\s+need\s+to)\s+"
    r"(?:\w+\s+){0,2}(?:wait|pause|hold|stop)\b",
    re.IGNORECASE,
)


def premature_quota_wait(message: str) -> bool:
    """True only for an affirmative first-person choice to await a quota reset."""
    if not isinstance(message, str) or not message.strip():
        return False
    message = message.replace('’', "'")
    in_fence = False
    for raw in message[-6000:].splitlines():
        line = raw.strip()
        if line.startswith('```'):
            in_fence = not in_fence
            continue
        if in_fence or line.startswith(('>', '|', '#', '- `', '* `')):
            continue
        # Short sentences prevent unrelated quota references and negations
        # from leaking between unrelated messages.
        for sentence in re.split(r'(?<=[.!?])\s+', line):
            if _NEGATED.search(sentence):
                continue
            intent = _INTENT.search(sentence)
            if intent and _WAIT_TARGET.search(sentence):
                remainder = sentence[intent.end():]
                # An agent waiting on a real user decision is NOT choosing to
                # idle for quota recovery, even if quota is mentioned later.
                if re.search(r'\b(?:for|on|until)\s+(?:the\s+)?(?:user|human|your|approval|permission|reply|response|decision)\b', remainder, re.I):
                    continue
                return True
            # "The quota is low so I will wait for the reset" is covered by
            # intent above; no generic third-person description is actionable.
    return False


def work_policy() -> str:
    return (
        'QUOTA CONTINUATION POLICY: A quota reading below 90% USED is NOT an instruction '
        'to wait for its reset. If verified quota is below 80% used, work normally; '
        'at 80–89% used, prepare the handoff and session summary while doing only '
        'short, independently safe, verifiable tasks. Do not voluntarily park or '
        'wait for a reset merely to conserve quota. If nothing useful and authorised '
        'remains, or the user explicitly pauses, stop for that genuine reason instead. '
        'Never burn tokens artificially to reach 90%. At 90%+ used, STOP implementation, '
        'finish the verified handoff and yield; ONLY the watchdog then waits for a '
        'provider with sufficient quota. Unknown/stale telemetry and sustained API '
        'outages follow the separate fail-safe policy. Never commit, push, deploy or '
        'change live systems without explicit approval.'
    )


def premature_wait_correction(used_percent=None) -> str:
    value = (f'Current verified limiting usage is {used_percent:.1f}% USED. '
             if isinstance(used_percent,(int,float)) else '')
    return (
        'PREMATURE QUOTA WAIT DETECTED. '+value+
        'You have not reached the 90%-used handoff threshold. Do NOT wait for '
        'a session/weekly reset just to conserve tokens. Continue the next '
        'authorised short, testable local task. At 80–89%, prepare/update the '
        'handoff and dated summary as you go, avoid large new jobs, and stop '
        'implementation at 90%. Do not waste tokens to reach the threshold. '
        'If no independently safe task exists, document the actual blocker '
        'and request user input; never invent permission. A direct user pause '
        'takes precedence. Do not commit, push, deploy or change live state.'
    )
