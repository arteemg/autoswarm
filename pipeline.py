"""Multi-agent pipeline harness: --agent-import-path pipeline:AutoAgent."""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

import yaml
from agents import Agent, Runner, function_tool
from openai import AsyncOpenAI
from agents.items import (
    ItemHelpers,
    MessageOutputItem,
    ReasoningItem,
    ToolCallItem,
    ToolCallOutputItem,
)
from agents.tool import FunctionTool
from agents.usage import Usage
from harbor.agents.base import BaseAgent
from harbor.environments.base import BaseEnvironment
from harbor.models.agent.context import AgentContext


# ============================================================================
# EDITABLE HARNESS — reads pipeline_spec.yaml and runs the pipeline
# ============================================================================

PIPELINE_SPEC_PATH = Path(__file__).parent / "pipeline_spec.yaml"

COMPRESSION_MODEL = "gpt-5.4-mini"

COMPRESSION_PROMPT = """\
You are compressing the output of stage {stage_a_id} so that stage {stage_b_id} can do its job.

Stage {stage_b_id}'s role:
{stage_b_role}

Stage {stage_a_id}'s output:
{output}

Produce a summary in <={token_budget} tokens that preserves everything stage {stage_b_id} needs to succeed. Drop anything stage {stage_b_id} won't use. Preserve exact identifiers, file paths, error messages, and numbers verbatim — do not paraphrase these. Format: {format_hint}.
"""


def load_spec() -> dict:
    with open(PIPELINE_SPEC_PATH) as f:
        return yaml.safe_load(f)


def make_shell_tool(environment: BaseEnvironment) -> FunctionTool:
    @function_tool
    async def run_shell(command: str) -> str:
        """Run a shell command in the task environment. Returns stdout and stderr."""
        try:
            result = await environment.exec(command=command, timeout_sec=120)
            out = ""
            if result.stdout:
                out += result.stdout
            if result.stderr:
                out += f"\nSTDERR:\n{result.stderr}" if out else f"STDERR:\n{result.stderr}"
            return out or "(no output)"
        except Exception as exc:
            return f"ERROR: {exc}"

    return run_shell


def build_stage_agent(stage_cfg: dict, environment: BaseEnvironment, default_model: str) -> Agent:
    tools = []
    if "run_shell" in stage_cfg.get("tools", []):
        tools.append(make_shell_tool(environment))
    return Agent(
        name=stage_cfg["id"],
        instructions=stage_cfg["system_prompt"].strip(),
        tools=tools,
        model=stage_cfg.get("model", default_model),
    )


async def compress_handoff(
    output: str,
    handoff_cfg: dict,
    current_stage_cfg: dict,
    next_stage_cfg: dict,
) -> str:
    """Targeted summarization of stage output, grounded in the next stage's role.

    If the output already fits the char-equivalent budget (4 chars ≈ 1 token),
    pass it through verbatim — no API call. Otherwise call gpt-5.4-mini with
    the downstream stage's system prompt so the summary keeps what that stage
    actually needs. Char-truncation remains the fallback if the API call
    fails for any reason.
    """
    token_budget = handoff_cfg.get("token_budget", 500)
    fmt = handoff_cfg.get("format", "prose")
    budget_chars = token_budget * 4

    if len(output) <= budget_chars:
        return output

    prompt = COMPRESSION_PROMPT.format(
        stage_a_id=current_stage_cfg["id"],
        stage_b_id=next_stage_cfg["id"],
        stage_b_role=next_stage_cfg["system_prompt"].strip(),
        output=output,
        token_budget=token_budget,
        format_hint=fmt,
    )

    try:
        client = AsyncOpenAI(api_key=os.environ.get("OPENAI_API_KEY"))
        response = await client.chat.completions.create(
            model=COMPRESSION_MODEL,
            messages=[{"role": "user", "content": prompt}],
            max_tokens=token_budget + 200,
        )
        compressed = (response.choices[0].message.content or "").strip()
        if compressed:
            return compressed
    except Exception:
        pass

    truncated = output[:budget_chars]
    return f"[truncated to ~{token_budget} tokens, format={fmt}]\n{truncated}"


def extract_final_output(run_result: object) -> str:
    """Pull the last text message from a RunResult."""
    for item in reversed(run_result.new_items):
        if isinstance(item, MessageOutputItem):
            text = ItemHelpers.text_message_output(item)
            if text:
                return text
    return "(no text output)"


async def run_task(
    environment: BaseEnvironment,
    instruction: str,
) -> tuple[PipelineResult, int]:
    """Execute the pipeline defined in pipeline_spec.yaml."""
    spec = load_spec()
    pipeline_cfg = spec["pipeline"]
    stages = pipeline_cfg["stages"]
    handoffs = pipeline_cfg.get("handoffs", {})
    default_model = pipeline_cfg.get("model", "gpt-5")

    context = instruction
    stage_traces: list[dict] = []
    t0 = time.time()

    for i, stage_cfg in enumerate(stages):
        agent = build_stage_agent(stage_cfg, environment, default_model)
        prompt = f"TASK:\n{instruction}\n\nCONTEXT FROM PREVIOUS STAGES:\n{context}"

        run_result = await Runner.run(
            agent,
            input=prompt,
            max_turns=stage_cfg.get("max_turns", 10),
        )

        output = extract_final_output(run_result)
        stage_traces.append({
            "stage": stage_cfg["id"],
            "model": stage_cfg.get("model", default_model),
            "input": context,
            "output": output,
            "run_result": run_result,
        })

        if i < len(stages) - 1:
            next_stage_cfg = stages[i + 1]
            handoff_key = f"{stage_cfg['id']}→{next_stage_cfg['id']}"
            handoff_cfg = handoffs.get(handoff_key, {})
            include_raw = handoff_cfg.get("include_raw_output", False)
            context = output if include_raw else await compress_handoff(
                output, handoff_cfg, stage_cfg, next_stage_cfg)

    duration_ms = int((time.time() - t0) * 1000)
    final_output = stage_traces[-1]["output"] if stage_traces else "(no output)"
    return PipelineResult(stage_traces=stage_traces, final_output=final_output), duration_ms


# ============================================================================
# FIXED ADAPTER BOUNDARY: do not modify unless the human explicitly asks.
# Harbor integration and trajectory serialization live here.
# ============================================================================

@dataclass
class PipelineResult:
    stage_traces: list[dict] = field(default_factory=list)
    final_output: str = ""


def to_atif(result: PipelineResult, model: str, duration_ms: int = 0) -> dict:
    """Serialize a PipelineResult to ATIF trajectory format."""
    steps: list[dict] = []
    step_id = 0
    now = datetime.now(timezone.utc).isoformat()

    def _step(source: str, message: str, **extra: object) -> dict:
        nonlocal step_id
        step_id += 1
        step = {"step_id": step_id, "timestamp": now,
                "source": source, "message": message}
        step.update({k: v for k, v in extra.items() if v is not None})
        return step

    total_usage = Usage()

    for trace in result.stage_traces:
        stage_id = trace["stage"]
        stage_model = trace.get("model", model)
        run_result = trace.get("run_result")
        if run_result is None:
            continue

        steps.append(
            _step("agent", f"[stage:{stage_id}]", model_name=stage_model))

        pending_tool_call = None
        for item in run_result.new_items:
            if isinstance(item, MessageOutputItem):
                text = ItemHelpers.text_message_output(item)
                if text:
                    steps.append(_step("agent", text, model_name=stage_model))
            elif isinstance(item, ReasoningItem):
                summaries = getattr(item.raw_item, "summary", None)
                reasoning = (
                    "\n".join(s.text for s in summaries if hasattr(s, "text"))
                    if summaries
                    else None
                )
                if reasoning:
                    steps.append(
                        _step("agent", "(thinking)",
                              reasoning_content=reasoning, model_name=stage_model)
                    )
            elif isinstance(item, ToolCallItem):
                raw = item.raw_item
                if hasattr(raw, "name"):
                    pending_tool_call = raw
            elif isinstance(item, ToolCallOutputItem) and pending_tool_call:
                arguments = (
                    json.loads(pending_tool_call.arguments)
                    if isinstance(pending_tool_call.arguments, str)
                    else pending_tool_call.arguments
                )
                steps.append(
                    _step(
                        "agent",
                        f"Tool: {pending_tool_call.name}",
                        tool_calls=[
                            {
                                "tool_call_id": pending_tool_call.call_id,
                                "function_name": pending_tool_call.name,
                                "arguments": arguments,
                            }
                        ],
                        observation={
                            "results": [
                                {
                                    "source_call_id": pending_tool_call.call_id,
                                    "content": str(item.output) if item.output else "",
                                }
                            ]
                        },
                    )
                )
                pending_tool_call = None

        if pending_tool_call:
            arguments = (
                json.loads(pending_tool_call.arguments)
                if isinstance(pending_tool_call.arguments, str)
                else pending_tool_call.arguments
            )
            steps.append(
                _step(
                    "agent",
                    f"Tool: {pending_tool_call.name}",
                    tool_calls=[
                        {
                            "tool_call_id": pending_tool_call.call_id,
                            "function_name": pending_tool_call.name,
                            "arguments": arguments,
                        }
                    ],
                )
            )

        for response in run_result.raw_responses:
            total_usage.add(response.usage)

    if not steps:
        steps.append(_step("user", "(empty)"))

    return {
        "schema_version": "ATIF-v1.6",
        "session_id": "pipeline",
        "agent": {"name": "autoswarm", "version": "0.1.0", "model_name": model},
        "steps": steps,
        "final_metrics": {
            "total_prompt_tokens": total_usage.input_tokens,
            "total_completion_tokens": total_usage.output_tokens,
            "total_cached_tokens": getattr(total_usage.input_tokens_details, "cached_tokens", 0) or 0,
            "total_cost_usd": None,
            "total_steps": len(steps),
            "extra": {
                "duration_ms": duration_ms,
                "pipeline_topology": "→".join(t["stage"] for t in result.stage_traces),
            },
        },
    }


class AutoAgent(BaseAgent):
    """Harbor agent adapter for the multi-agent pipeline."""

    SUPPORTS_ATIF = True

    def __init__(self, *args, extra_env: dict[str, str] | None = None, **kwargs):
        super().__init__(*args, **kwargs)
        self._extra_env = dict(extra_env) if extra_env else {}

    @staticmethod
    def name() -> str:
        return "autoswarm"

    def version(self) -> str | None:
        return "0.1.0"

    async def setup(self, environment: BaseEnvironment) -> None:
        pass

    async def run(self, instruction: str, environment: BaseEnvironment, context: AgentContext) -> None:
        await environment.exec(command="mkdir -p /task")
        instr_file = self.logs_dir / "instruction.md"
        instr_file.write_text(instruction)
        await environment.upload_file(source_path=instr_file, target_path="/task/instruction.md")

        result, duration_ms = await run_task(environment, instruction)

        spec = load_spec()
        default_model = spec["pipeline"].get("model", "gpt-5")
        atif = to_atif(result, model=default_model, duration_ms=duration_ms)
        traj_path = self.logs_dir / "trajectory.json"
        traj_path.write_text(json.dumps(atif, indent=2))

        stage_traces_path = self.logs_dir / "stage_traces.json"
        exportable = [
            {"stage": t["stage"], "model": t.get(
                "model", default_model), "output": t["output"]}
            for t in result.stage_traces
        ]
        stage_traces_path.write_text(json.dumps(exportable, indent=2))

        try:
            final_metrics = atif.get("final_metrics", {})
            context.n_input_tokens = final_metrics.get(
                "total_prompt_tokens", 0)
            context.n_output_tokens = final_metrics.get(
                "total_completion_tokens", 0)
            context.n_cache_tokens = final_metrics.get(
                "total_cached_tokens", 0)
        except Exception:
            pass

        total_turns = sum(
            len(t["run_result"].raw_responses)
            for t in result.stage_traces
            if t.get("run_result")
        )
        topology = "→".join(t["stage"] for t in result.stage_traces)
        print(
            f"topology={topology} turns={total_turns} duration_ms={duration_ms} "
            f"input={atif['final_metrics']['total_prompt_tokens']} "
            f"output={atif['final_metrics']['total_completion_tokens']}"
        )


__all__ = ["AutoAgent"]
