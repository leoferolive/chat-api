"""LLM-as-Judge async evaluator.

Runs as a separate process (a Kubernetes CronJob) against the same SQLite
database as the chat app. Scores recent assistant turns on three criteria
and persists the verdicts; the main app's ``/metrics-judge`` endpoint then
aggregates those scores for Prometheus.
"""

from .prompts import CRITERIA, judge_prompt
from .runner import evaluate_batch

__all__ = ["CRITERIA", "judge_prompt", "evaluate_batch"]
