# AutoSwarm

> A multi-agent pipeline harness that self-optimizes its own topology. A meta-agent edits stage prompts, tool assignments, turn budgets, and pipeline structure — then reruns the benchmark, checks the score, keeps or discards the change, and repeats.

## How it works

- **`pipeline_spec.yaml`** — the topology the meta-agent edits. Defines stages (system prompt, tools, turn budget, output format) and handoffs (token budget, context format) between them. This is the primary edit surface.
- **`pipeline.py`** — the runner. Reads `pipeline_spec.yaml` and executes the pipeline. Contains a small editable section (tool definitions, compression logic) and a fixed Harbor adapter boundary.
- **`evaluator.py`** — per-stage LLM judge. After each run, scores every stage on how well its output equipped the next stage. Produces `stage_scores` for `results.tsv` so the meta-agent can identify exactly which stage is failing.
- **`program_pipeline.md`** — meta-agent instructions. Defines the experiment loop, credit assignment rules, structural edit rules, and keep/discard criteria.
- **`agent.py`** — the original single-agent baseline harness (unchanged). Used for comparison runs.
- **`program.md`** — meta-agent instructions for the single-agent loop (unchanged).
- **`tasks/`** — evaluation tasks in [Harbor](https://github.com/laude-institute/harbor) format.

The metric is total **passed** tasks. The meta-agent hill-climbs on this score by editing the pipeline topology.

## Quick start

**Requirements:** Docker, Python 3.12+, [uv](https://docs.astral.sh/uv/), `OPENAI_API_KEY`.

```bash
# 1. Install uv (if you don't have it)
curl -LsSf https://astral.sh/uv/install.sh | sh

# 2. Install dependencies
uv sync

# 3. Set credentials
cat > .env << 'EOF'
OPENAI_API_KEY=...
EOF

# 4. Build base image
docker build -f Dockerfile.base -t autoswarm-base .

# 5. Add tasks to tasks/ (see Task format below)

# 6. Run all tasks with the pipeline harness
rm -rf jobs; mkdir -p jobs && uv run harbor run -p tasks/ -n 100 \
  --agent-import-path pipeline:AutoAgent -o jobs --job-name latest > run.log 2>&1

# 7. Run all tasks with the single-agent baseline (for comparison)
rm -rf jobs; mkdir -p jobs && uv run harbor run -p tasks/ -n 100 \
  --agent-import-path agent:AutoAgent -o jobs --job-name latest > run.log 2>&1
```

## Running the meta-agent

Point your coding agent at the repo and prompt:

```
Read program_pipeline.md and let's kick off a new experiment!
```

The meta-agent will read the directive, inspect `pipeline_spec.yaml`, run the benchmark, score each stage with `evaluator.py`, edit the topology, and iterate.

## Project structure

```text
pipeline_spec.yaml             -- pipeline topology (primary edit surface)
pipeline.py                    -- pipeline runner + Harbor adapter
  editable section             -- load_spec, tools, compress_handoff, run_task
  fixed adapter section        -- PipelineResult, to_atif, AutoAgent
evaluator.py                   -- per-stage LLM judge
program_pipeline.md            -- meta-agent instructions for pipeline optimization
agent.py                       -- single-agent baseline harness
program.md                     -- meta-agent instructions for single-agent loop
Dockerfile.base                -- base image (copies both harnesses)
tasks/                         -- benchmark tasks
jobs/                          -- Harbor job outputs (gitignored)
results.tsv                    -- experiment log (gitignored)
run.log                        -- latest run output (gitignored)
```

## pipeline_spec.yaml

This is what the meta-agent reads and edits. Stage-level fields:

| Field | Description |
|---|---|
| `system_prompt` | Instructions for this stage's agent |
| `tools` | Tool list — currently `[run_shell]` or `[]` |
| `max_turns` | Turn budget for this stage |
| `output_format` | Hint to the agent: `bullet_list` \| `json` \| `prose` \| `structured_json` |
| `model` | Model override (inherits `pipeline.model` if omitted) |

Handoff fields between stages:

| Field | Description |
|---|---|
| `token_budget` | Max tokens of context passed to the next stage |
| `format` | Format hint for context compression |
| `include_raw_output` | If true, passes full output uncompressed |

## Stage evaluator

After a run, score stage traces:

```bash
for task_dir in jobs/latest/*/; do
  traces="$task_dir/logs/stage_traces.json"
  instr="$(cat tasks/$(basename $task_dir)/instruction.md 2>/dev/null || echo '')"
  [ -f "$traces" ] && uv run python evaluator.py "$traces" --instruction "$instr"
done
```

Output: `explore:0.82,plan:0.71,execute:0.65,verify:0.88` — goes into `results.tsv` as `stage_scores`.

## results.tsv schema

```text
commit  avg_score  passed  task_scores  stage_scores  pipeline_topology  cost_usd  status  description
```

`pipeline_topology` records the stage sequence at time of run (e.g. `explore→plan→execute→verify`), so structural changes are traceable across the experiment log.

## Task format

Tasks follow [Harbor's format](https://harborframework.com/docs/tasks):

```text
tasks/my-task/
  task.toml           -- config (timeouts, metadata)
  instruction.md      -- prompt sent to the agent
  tests/
    test.sh           -- entry point, writes /logs/reward.txt
    test.py           -- verification (deterministic or LLM-as-judge)
  environment/
    Dockerfile        -- task container (FROM autoswarm-base)
  files/              -- reference files mounted into container
```

## Design choices

- **Topology as config.** The pipeline structure lives in `pipeline_spec.yaml`, not in code. The meta-agent edits YAML, not Python, for most experiments.
- **Per-stage credit assignment.** `evaluator.py` scores each stage independently, giving the meta-agent a diagnostic signal that end-to-end score alone cannot provide.
- **Score-driven.** Every experiment produces a numeric score. Keep if better, discard if not.
- **Docker isolation.** Both harnesses run in containers.
- **Backward-compatible.** `agent.py` and `program.md` are untouched — single-agent baseline runs work exactly as before.

## Cleanup

```bash
uv run harbor cache clean -f        # Harbor task image cache
docker system prune -a -f           # full Docker nuke
docker container prune -f           # just dead containers
```

If Docker becomes unresponsive after many concurrent runs:

```bash
killall Docker && open -a Docker
```

## Built on AutoAgent

AutoSwarm is built on top of [AutoAgent](https://github.com/thirdlayer/autoagent) — the original single-agent self-improvement loop. The core experiment loop, Harbor integration, `agent.py` harness, and `program.md` format are inherited directly. AutoSwarm extends the edit surface from a single agent to a multi-agent pipeline topology.

## License

MIT
