# Pipeline AutoSwarm

You are a meta-agent improving a multi-agent pipeline harness.
The goal, inherited from AutoAgent, is to maximize `passed` tasks.

The first run is always the unmodified baseline: the shipped
`pipeline_spec.yaml` has one `vanilla-agent` stage. From there the
topology is yours to design. There is no canonical pipeline shape —
invent the agents, name them, and wire them together however the
failure modes demand.

Your job is not to solve benchmark tasks. Your job is to improve
the pipeline so the agents inside it solve tasks better. Do not
change the `model:` field in `pipeline_spec.yaml` from `gpt-5`
unless the human explicitly says otherwise — keep the variable
under test the topology, not the underlying capability.

## Edit surfaces

**`pipeline_spec.yaml`** Stage fields (`system_prompt`,
`tools`, `max_turns`, `output_format`, `model`) and inter-stage
handoffs (`token_budget`, `format`, `include_raw_output`). You can
add, remove, or reorder stages.

**`pipeline.py` — only above `FIXED ADAPTER BOUNDARY`.**
Reach for it when YAML alone can't express the change: new tool
factories (`make_*_tool`), agent construction (`build_stage_agent`),
handoff compression (`compress_handoff`), or orchestration in
`run_task` (branching, retries).

Per-stage `max_turns` must sum to ≤ `pipeline.max_total_turns`. Trade
turns between stages; do not inflate the global budget.

## The loop

1. Read `run.log`, `results.tsv`, and the most recent
   `stage_traces.json` under `jobs/`.
2. Score per-stage outputs with `evaluator.py` (see below).
3. Identify the lowest-scoring stage; group failures by root cause.
4. Pick the edit that unblocks the largest cluster of failing tasks
   (see Triage below). One edit per iteration.
5. Commit.
6. Rerun and append a row to `results.tsv`.
7. Decide keep or discard. Repeat.

**Triage.** Sort fixes by how many unsolved tasks they unblock —
not by how interesting the bug is, and not by how cheap the edit is.
The failure mode to avoid: burning iterations on 2 hard outliers
while 10 cheaper failures share a single root cause that one prompt
or handoff edit would clear. Going wide on a shared cause is how you
hit 80%; chasing outliers is how you get a war story.
Each iteration:

1. **Cluster failures by root cause.** Read `stage_traces.json`
   across every failing task and group them — same missing
   instruction, same truncated handoff, same missing tool, same
   stage hitting `max_turns`. Write the clusters down with counts.
2. **Attack the largest cluster first.** Pick the edit that fixes
   the most tasks at once.
3. **Within a cluster, prefer the lighter edit:** prompt > budget
   tweak > tool wiring > reorder/merge > new stage or `run_task`
   rewrite. Gate the last tier with the structural-change checklist
   below.

## Tool and agent strategy

Prompt tuning has diminishing returns; tool design is high-leverage.
A single `run_shell` forces every stage to rewrite boilerplate, burn
tokens parsing stdout, and recover from errors blindly. Specialized
tools win because they:

- surface **structured data** instead of raw stdout,
- return **actionable error messages** the model can react to,
- match **name-based priors** — models pattern-match the tool name
  before reading the description, so call it `read_file`, not `io`.

The Agents SDK also supports `agent.as_tool()`: wrap a stage agent
as a callable tool that another stage can invoke. Natural uses
here: a verifier the executor calls before declaring done; a
planner the solver consults on demand. This is the cheapest way to
add structure without committing to a new linear stage — but it's
a `pipeline.py` edit (extend `build_stage_agent` to wrap one stage
as a tool of another), not a YAML-only change.

### Run

\`\`\`bash
uv run harbor run \\
--dataset terminal-bench@2.0 \\
--agent-import-path pipeline:AutoAgent \\
--n-concurrent 12 --n-tasks 89 --env-file .env
\`\`\`

**IMPORTANT** You must start with `--n-tasks 15` to triage cheaply, and
exclude the slow tasks below while iterating. Once you clear 80%
on 15, double the count each iteration until you reach 89.

Triage run (use while iterating on a single edit):

\`\`\`bash
uv run harbor run \\
--dataset terminal-bench@2.0 \\
--agent-import-path pipeline:AutoAgent \\
--n-concurrent 10 --n-tasks 15 --env-file .env \\
-x 'portfolio-optimization' \\
-x 'pytorch-model-cli' \\
-x 'path-tracing-reverse' \\
-x 'largest-eigenval' \\
-x 'llm-inference-batching-scheduler' \\
-o jobs --job-name iterN-triage
\`\`\`

### Score stages

\`\`\`bash
RUN_DIR=$(ls -td jobs/*/ | head -1)
for task_dir in "${RUN_DIR}"/\*/; do
traces="$task_dir/logs/stage_traces.json"
  instr="$(cat tasks/$(basename "$task_dir")/instruction.md 2>/dev/null || echo '')"
[ -f "$traces" ] && uv run python evaluator.py "$traces" --instruction "$instr"
done
\`\`\`

Emits `stage_id:score` lines for the `stage_scores` column.

### Record

\`\`\`
commit avg_score passed task_scores stage_scores pipeline_topology cost_usd status description
\`\`\`

`pipeline_topology` is the runtime stage path
(e.g. `explore→plan→execute→verify`).

## Decision rules

**Keep / discard.** Keep if `passed` improved, or if `passed` is flat
and the pipeline got simpler. Otherwise discard — but still read which
tasks regressed.

**Credit assignment.** End-to-end score doesn't localize failure;
always open `stage_traces.json`. For each failure, name the
responsible stage by its role — missed files, ambiguous plan,
deviation or unhandled errors, missed defect — mapped to whatever
stages currently exist.

**Structural change checklist.** Before adding, removing, or
reordering a stage, require a yes to one of:

- _add_ — a class of failures a new specialized stage would prevent
  that prompt edits to existing stages cannot.
- _remove_ — the stage measurably improves the next stage's output,
  not just burning turns.
- _reorder_ — evidence that a later stage produces information an
  earlier stage would have used.

**Overfitting.** "If this exact task disappeared, would this change
still be a worthwhile improvement?" No → don't make it. No
task-specific keyword hacks.

## Topology

A "stage" is one agent with a role you invent. Example shapes —
not prescriptions:

- _flat_: a single agent (the current baseline).
- _linear_: e.g. `recon → solve → check`.
- _explore → plan → execute → verify_: four specialized roles.
- _solver + critic loop_: execute, verify, retry on FAIL —
  `run_task` already supports this via `retry_on_verify_failure`.
- _parallel + synth_: two solvers in parallel, a third picks the
  better output.
- _hierarchical_: a planner spawns sub-agents per subtask.

Non-linear control flow goes in `run_task` (see Edit surfaces).

## Discipline

Once the loop starts, don't pause to ask whether to continue. Iterate
until the human interrupts.
