"""Stage-level LLM judge for the pipeline harness.

Scores each stage's output on how well it equips the next stage.
Called by the meta-agent after a run to get per-stage diagnostics.

Usage (standalone):
    uv run python evaluator.py jobs/latest/<task>/logs/stage_traces.json \
        --instruction "$(cat tasks/<task>/instruction.md)"

Usage (from code):
    from evaluator import evaluate_pipeline
    stage_scores = await evaluate_pipeline(stage_traces, instruction)
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
from pathlib import Path

from openai import AsyncOpenAI

JUDGE_MODEL = "gpt-4o-mini"

STAGE_JUDGE_PROMPT = """\
You are evaluating whether a pipeline stage produced useful output for the next stage.

Stage: {stage_id}
Task: {task_instruction}
Stage Input (truncated): {stage_input}
Stage Output: {stage_output}
Next Stage Role: {next_stage_role}

Score 0.0–1.0:
- 1.0: output fully equips the next stage to do its job
- 0.5: output is partially useful but missing key information
- 0.0: output is wrong, empty, or would actively mislead the next stage

Respond as JSON only (no markdown fence):
{{"score": <float>, "missing": "<what is absent or wrong>", "assessment": "<one sentence>"}}
"""

FINAL_STAGE_JUDGE_PROMPT = """\
You are evaluating whether the final pipeline stage produced a correct and complete answer.

Stage: {stage_id}
Task: {task_instruction}
Stage Output: {stage_output}

Score 0.0–1.0:
- 1.0: output fully and correctly resolves the task
- 0.5: partial or incomplete resolution
- 0.0: wrong, empty, or irrelevant

Respond as JSON only (no markdown fence):
{{"score": <float>, "missing": "<what is absent or wrong>", "assessment": "<one sentence>"}}
"""


async def evaluate_stage(
    client: AsyncOpenAI,
    stage_id: str,
    task_instruction: str,
    stage_input: str,
    stage_output: str,
    next_stage_role: str | None,
) -> dict:
    if next_stage_role is None:
        prompt = FINAL_STAGE_JUDGE_PROMPT.format(
            stage_id=stage_id,
            task_instruction=task_instruction[:800],
            stage_output=stage_output[:1200],
        )
    else:
        prompt = STAGE_JUDGE_PROMPT.format(
            stage_id=stage_id,
            task_instruction=task_instruction[:800],
            stage_input=stage_input[:400],
            stage_output=stage_output[:1200],
            next_stage_role=next_stage_role,
        )

    response = await client.chat.completions.create(
        model=JUDGE_MODEL,
        messages=[{"role": "user", "content": prompt}],
        temperature=0,
        max_tokens=256,
    )
    raw = response.choices[0].message.content or "{}"
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return {"score": 0.0, "missing": "parse error", "assessment": raw[:200]}


async def evaluate_pipeline(
    stage_traces: list[dict],
    task_instruction: str,
) -> dict[str, dict]:
    """Score every stage in a completed pipeline run.

    Args:
        stage_traces: list of {"stage": str, "input": str, "output": str}
        task_instruction: the original task text

    Returns:
        dict mapping stage_id → {"score": float, "missing": str, "assessment": str}
    """
    client = AsyncOpenAI(api_key=os.environ.get("OPENAI_API_KEY"))

    tasks = []
    for i, trace in enumerate(stage_traces):
        next_role = stage_traces[i + 1]["stage"] if i + 1 < len(stage_traces) else None
        tasks.append(
            evaluate_stage(
                client,
                stage_id=trace["stage"],
                task_instruction=task_instruction,
                stage_input=trace.get("input", ""),
                stage_output=trace.get("output", ""),
                next_stage_role=next_role,
            )
        )

    results_list = await asyncio.gather(*tasks)
    return {trace["stage"]: result for trace, result in zip(stage_traces, results_list)}


def format_stage_scores(scores: dict[str, dict]) -> str:
    """Compact string for results.tsv: explore:0.82,plan:0.71,execute:0.65,verify:0.88"""
    return ",".join(f"{stage}:{v['score']:.2f}" for stage, v in scores.items())


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

async def _main(traces_path: Path, instruction: str) -> None:
    stage_traces = json.loads(traces_path.read_text())
    scores = await evaluate_pipeline(stage_traces, instruction)
    print(json.dumps(scores, indent=2))
    print("\nstage_scores:", format_stage_scores(scores))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Evaluate pipeline stage traces")
    parser.add_argument("traces", type=Path, help="Path to stage_traces.json")
    parser.add_argument("--instruction", required=True, help="Original task instruction text")
    args = parser.parse_args()
    asyncio.run(_main(args.traces, args.instruction))
