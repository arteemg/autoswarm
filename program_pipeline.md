# Pipeline AutoSwarm

You are a meta-agent that improves a multi-agent pipeline harness.
Your primary edit surface is `pipeline_spec.yaml`, not `pipeline.py` directly.
The goal is the same as the original AutoAgent: maximize `passed` tasks.

## Setup

Before starting:

1. Read `README.md`, `program.md`, `pipeline.py`, and `pipeline_spec.yaml`.
2. Read a representative sample of task instructions and verifier code.
3. Build the base image and verify `pipeline.py` imports cleanly:
   ```bash
   docker build -f Dockerfile.base -t autoswarm-base .
   uv run python -c "from pipeline import AutoAgent"
   ```
4. Initialize `results.tsv` if it does not exist (use the extended schema below).

The first run is always the unmodified baseline.

## How to Run

```bash
rm -rf jobs; mkdir -p jobs && uv run harbor run -p tasks/ -n 100 \
  --agent-import-path pipeline:AutoAgent -o jobs --job-name latest > run.log 2>&1
```

## What You Can Edit in pipeline_spec.yaml

### Stage-level (high value)
- `system_prompt` for any stage
- `tools` per stage — add or remove `run_shell`
- `max_turns` per stage
- `output_format` per stage (`bullet_list` | `json` | `prose` | `structured_json`)
- `model` per stage (inherits `pipeline.model` if omitted)

### Handoff-level (medium value)
- `token_budget` between stages
- `format` of context passed
- `include_raw_output` flag

### Structural (high risk, high reward)
- Add a new stage (insert anywhere in the sequence)
- Remove a stage (merge its responsibility into an adjacent stage)
- Reorder stages

Turn budgets must stay within `pipeline.max_total_turns` in total.
If you raise one stage's `max_turns`, reduce another's.

## What You Can Edit in pipeline.py

Everything above the `FIXED ADAPTER BOUNDARY` comment:
- `PIPELINE_SPEC_PATH` — if you want to rename the spec file
- `make_shell_tool()` — add new tool types (e.g. `read_file`, `write_file`)
- `build_stage_agent()` — change agent construction
- `compress_handoff()` — improve context compression (currently naive truncation)
- `run_task()` — change orchestration logic (e.g. conditional branching, retry)

## Running the Stage Evaluator

After each run, score stage traces with:

```bash
for task_dir in jobs/latest/*/; do
  traces="$task_dir/logs/stage_traces.json"
  instr="$(cat tasks/$(basename $task_dir)/instruction.md 2>/dev/null || echo '')"
  [ -f "$traces" ] && uv run python evaluator.py "$traces" --instruction "$instr"
done
```

This produces per-stage scores (`explore:0.82,plan:0.71,...`) for `results.tsv`.

## Logging Results

```
commit	avg_score	passed	task_scores	stage_scores	pipeline_topology	cost_usd	status	description
```

- `stage_scores`: per-stage judge averages, e.g. `explore:0.82,plan:0.71,execute:0.65,verify:0.88`
- `pipeline_topology`: stage sequence at time of run, e.g. `explore→plan→execute→verify`

## Experiment Loop

1. Check the current branch and commit.
2. Read the latest `run.log` and recent task-level results.
3. Run the stage evaluator on failed tasks to get per-stage scores.
4. Identify which stage has the lowest scores or most failures.
5. Group failures by root cause and affected stage.
6. Choose one targeted improvement (stage prompt, handoff budget, or structure).
7. Edit `pipeline_spec.yaml` (or `pipeline.py` if tool-level change is needed).
8. Commit the change.
9. Rebuild and rerun the task suite.
10. Record results in `results.tsv`.
11. Decide keep or discard.

## Credit Assignment

When diagnosing failures, identify **which stage** failed:

- **explore**: Did it miss files, misread the environment, or skip relevant structure?
- **plan**: Did it produce an incomplete, wrong, or ambiguous execution plan?
- **execute**: Did it deviate from plan, hit unhandled errors, or produce wrong artifacts?
- **verify**: Did it miss an incorrect output, or fail to flag a real problem?

Stage-level failures require stage-level fixes. End-to-end score alone is
insufficient for diagnosis — always read `stage_traces.json` from the trajectory.

## Structural Edit Rules

**Before adding a stage**, ask:
> "Is there a consistent class of failures that a new specialized agent
> would prevent, and that the current agents cannot handle even with better prompts?"

**Before removing a stage**, ask:
> "Does this stage's output actually improve the next stage's performance,
> or is it just adding latency and burning turns?"

**Before reordering stages**, ask:
> "Is there evidence that an earlier stage is producing information that
> arrives too late to be useful?"

## Keep / Discard Rules

- If `passed` improved → keep.
- If `passed` stayed the same and the pipeline is simpler → keep.
- Otherwise → discard.

Discarded runs still provide signal. Read which tasks regressed and why.

## Overfitting Rule (inherited from AutoAgent)

"If this exact task disappeared, would this pipeline structure still
be a worthwhile improvement?"

If no → it is probably overfitting. Do not add task-specific hacks or
hardcoded keyword rules.

## NEVER STOP

Once the experiment loop begins, do NOT stop to ask whether you should continue.
Do NOT pause at a "good stopping point." Continue iterating until the human
explicitly interrupts you.
