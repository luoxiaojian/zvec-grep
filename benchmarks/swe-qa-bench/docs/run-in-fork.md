# Run the Rust SWE-QA benchmark in your fork

The [SWE-QA workflow](../../../.github/workflows/swe-qa-bench.yml) compares OpenCode without and with the **same selected Rust zvec-grep build**. It never benchmarks the TypeScript package checked out with the harness. `candidate_ref` may be a Rust branch, tag, or commit; it defaults to `main`. The workflow records the resolved commit and verifies the npm tarball hash before each task. The benchmark is manual-only and runs only for a fork's `admin` or `maintain` members, including job reruns.

1. Fork `zvec-ai/zvec-grep` and clone your fork. Enable GitHub Actions in the fork. Put this workflow on the fork's **default branch** first: GitHub's manual dispatch control requires the workflow file to exist there. You may then use `--ref` to choose a different harness branch. Set `candidate_ref` independently to choose the Rust source to build. The chosen ref must exist in the fork and contain `rust/`.
2. Obtain a Bailian API key for a workspace that can invoke the selected `glm-5.2` or `qwen3.8-max` model. In your fork's **Settings → Secrets and variables → Actions**, add the repository secret `GLM_API_KEY`. Both model choices use this legacy secret name for execution and self-judging. Do not commit the key or pass it on a command line. With [GitHub CLI](https://cli.github.com/manual/gh_secret_set), run `gh secret set GLM_API_KEY --repo OWNER/zvec-grep` and enter the value interactively.
3. In the same Actions settings, add the repository **variable** `SWE_QA_MODEL_BASE_URL`, for example `https://YOUR-WORKSPACE.cn-beijing.maas.aliyuncs.com/compatible-mode/v1`. Use the OpenAI-compatible URL for **your** Bailian workspace and the region of your API key. The variable is applied to both execution and judging. If omitted, the workflow retains the project maintainer's checked-in endpoint, which may not accept your key.
4. The default `embedding=local` uses `local/potion-code-16m-v2` and needs no embedding credential. For `embedding=remote`, add the repository secret `QWEN_EMBEDDING_API_KEY` and repository variable `QWEN_EMBEDDING_ENDPOINT`; the workflow uses `qwen/qwen3.7-text-embedding`, as Retrieval-only does. Source content is sent to that endpoint in remote mode.
5. Open **Actions → SWE-QA Bench → Run workflow**, choose the harness branch, then set `candidate_ref`, `model`, `embedding`, and `scope`. Start with `repro-3` (3 tasks × 2 profiles × 5 trials = 30 trials). `smoke` has 5 tasks / 50 trials; `all-full` has 20 tasks / 200 trials. Failed trials can be retried twice, causing additional model calls. Both execution and judging use the selected model. The default model is `glm-5.2`.

Equivalent CLI dispatch:

```sh
gh workflow run swe-qa-bench.yml --repo OWNER/zvec-grep \
  --ref main \
  -f candidate_ref=main \
  -f model=glm-5.2 \
  -f embedding=local \
  -f scope=repro-3
```

Use your fork's Rust branch name or full commit SHA for `candidate_ref` when evaluating a change. `--ref` selects the **benchmark harness**; `candidate_ref` selects the **Rust package**. The workflow does not run from a pull request or push, and it does not use upstream CI resources. A GitHub user with only `write` access may see the dispatch button, but the workflow rejects the run before checkout or model-secret use.

For recovery after a harness fix, `task_slugs` can select a comma-separated subset
of the locked scope, such as `-f scope=all-full -f task_slugs=sqlfluff-2,sympy-38`.
Each selected task still runs all five trials in both profiles. This creates a new
run with its own candidate/harness identity and a report for only those tasks.
Keep the original run's artifacts and provenance; do not relabel them as results
from the new harness or present the subset report as a complete 20-task evaluation.
Rerunning failed jobs on the original run uses its original commit and cannot pick
up a collector fix committed later.

The versioned [`ci-config.json`](../ci-config.json) sets the trial count, failure retry limit, and model behind each embedding runtime. Change that file on the harness branch to change the fixed protocol; the workflow input selects one of those versioned runtimes, while environment variables carry endpoints and credentials.

The run Summary shows the selected Rust commit and the aggregate of completed task reports. Download the `swe-qa-aggregate-report-*` artifact for `report.md`, `report.json`, and `candidate-manifest.json` (commit and tarball hash). Candidate packages, reports and paired Harbor evidence, including failed attempts under `.retry-history/`, are retained for 90 days. If tasks fail, they are listed as missing and excluded from every Aggregate metric; the workflow still has a failed conclusion.

Each OpenCode trial also records `agent/session-trace/`: the submitted instruction,
the selected root session and all descendant session/message/part rows, referenced
OpenCode overflow files, and a manifest with completeness checks and SHA256 hashes.
The raw tool parts preserve input arguments and the output visible to the agent.
This is separate from `session-usage.json`, which contains accounting rather than
the full transcript. Failed attempts retain available partial evidence and cannot
pass the complete-trace gate. Artifact publication still requires the secret scan.

The paired artifact includes a `search-traces/` reading index and per-trial Markdown
transcripts. It labels explicit shell `rg`, built-in `grep`, and zg MCP calls
separately. Both profiles have pinned ripgrep available; the zvec-grep profile can
still use standard tools. Compare complete paths for the same question rather than
assuming that the profiles are rg-only and zg-only or that calls match one-to-one.

Download every artifact into a new directory for the run:

```sh
gh run download RUN_ID --repo OWNER/zvec-grep --dir /absolute/path/to/evaluations/RUN_ID
```

Keep the original downloaded evidence alongside any derived reports. To regenerate
the reading index for a task, point `--runs-dir` at its downloaded `swe-qa-runs/TASK`
directory and use a separate output directory:

```sh
uv run --project benchmarks/swe-qa-bench python -m zg_bench.reports.search_traces \
  --runs-dir /absolute/path/to/swe-qa-runs/TASK \
  --output-dir /absolute/path/to/reading-index
```

References: [GitHub manual workflows](https://docs.github.com/en/actions/how-tos/manage-workflow-runs/manually-run-a-workflow), [Actions secrets](https://docs.github.com/en/actions/how-tos/write-workflows/choose-what-workflows-do/use-secrets), [Bailian API keys](https://help.aliyun.com/zh/model-studio/get-api-key), [Bailian regions and endpoints](https://help.aliyun.com/zh/model-studio/regions/).
