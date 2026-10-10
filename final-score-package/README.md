# ARC-Bench Codex Agent（智能体）

The host generates ROOT child modules in source dependency order, with a fresh coding session for each module.
Missing source dependencies and real source dependency cycles stop generation. A valid atomic DAG
may fold into a coarse module cycle; that cycle is a warning and source order breaks ties.
It synchronizes dependencies by package/lock content, builds the frontend, checks production
startup, saves a lightweight source checkpoint（源码检查点）, and creates a real module Git
commit（版本提交）before advancing. Module commits are labelled runnable; acceptance pending.

## Delivery and acceptance are separate（交付与验收分离）

Delivery is decided only by whether real deliverable source exists: a verified checkpoint, a real
change to product source, or a prepared/restored application input. Never by a model claim, a test
result, or whether the host's own checks could run. `delivery_outcome` returns
`ready`/`partial`/`unavailable`; `main()` exits with `delivery.exit_code`, not with a value derived
from acceptance. The generation status (`complete`/`partial`/`blocked`) records generation only and
is never overwritten by an acceptance exception.

| Situation | Delivered | Exit | Recorded |
| --- | --- | ---: | --- |
| Candidate generated, all checks pass | candidate | 0 | generation complete; acceptance passed |
| Candidate generated, checks fail or cannot run | candidate | 0 | acceptance failed/unverified; raw error kept |
| Planning auth/quota denial | candidate | 0 | later model calls stop; valid observations kept |
| Repair batch interrupted or broke startup | source before that batch, including earlier valid batches | 0 | interruption recorded; final frozen observations decide acceptance |
| Code changed after final acceptance | current candidate | 0 | old pass marked stale, not re-run |
| Final report/commit/reporting failure | current candidate | 0 | reporting or commit warning |
| Generation stopped with a saved checkpoint | latest checkpointed candidate | 0 | generation partial/blocked |
| No real source at all, only the empty scaffold | not claimed | 1 | real generation/output failure |

## Acceptance order and call budget（验收顺序与调用预算）

Independent planning reads the original requirements and reference images; the program supplies the
atomic ID list. Before coding, deterministic preparation validates the original source IDs,
dependencies and trusted recovery authority without a model turn. Full original requirements,
ROOT constraints and shared prerequisites remain available to implementation. Source-only checks
cannot mechanically decide every natural-language meaning.
Each module's independent observation script is now planned in a source-only workspace immediately
before that module's coding turn, rather than planning every module before the first coding turn.
After coding, build, production startup and checkpointing, the host runs that frozen script early.
The early result is provisional（临时证据）and its business verdict remains unverified; later modules
may provide missing prerequisites. The next coding prompt receives this evidence, and final acceptance
re-executes every trustworthy selected script against the completed application.
Identity and frozen file hashes prevent reuse across source or agent versions. Failed early plans
remain unverified and are not retried after coding; prepared scripts survive later model quota failures.

One source-isolated script-correction turn（脚本纠错调用）is allowed per full run for a definite
discovery syntax error, a missing `test`/`expect` reference in the test file or the native unfinished-step
guard marker. Explicit browser-origin
errors, locator failures and mismatched assertions never authorize this correction. Only a canonical
missing import prefix, appended closing delimiters for `Unexpected end of input`, or `await ` inserted
before original line-start `test.step` calls after the runtime guard marker can be selected. Removing
only those permitted additions must recover every original byte, including multiline expectations
and actions, in order. This verifies the returned correction patch; it does not statically search the
original JavaScript for missing awaits. Other
syntax fixes remain unverified. The original
`frozen-v1.spec.js` and original error stay intact; a successful correction creates `frozen-v2.spec.js`,
binds both hashes and the selected version to host authority, invalidates old acceptance and runs the
selected script again. This narrow byte-preservation check cannot establish complete semantic
equivalence. The correction workspace provides context isolation（上下文隔离）, not filesystem access
isolation. The host snapshots existing product source and binds frozen files and authority before the
turn; an out-of-workspace mutation is rejected and restoration is verified, with the correction
allowance consumed. Failure reporting or temporary cleanup cannot replace the original SDK error.
If application or frozen-file restoration cannot be verified, a separate host integrity STOP（主机
完整性停止）blocks every later model request and leaves acceptance unverified. The original exception
keeps its actual provider status; an ordinary failure is not relabelled as authentication denial.
The failed output and source snapshot are retained, with the snapshot path in STOP evidence.
Correction resource blocks and integrity STOP are bound to a new checkpoint of the last trustworthy
source, preserving the failed working output. Resuming that STOP lineage cannot automatically spend
another model turn; a separately authorized fresh run is independent. If STOP checkpoint export
fails, the original error still wins, current requests stop and the snapshot remains, while durability
is explicitly unverified rather than claimed as saved.
Before the correction request, the existing budget checkpoint also records
`script_correction_pending=true`（纠错尚未完成）. A restored trusted pending checkpoint stops further
model requests even if later STOP export failed or the process was interrupted. The marker clears
only when a safe terminal receipt and its corresponding checkpoint commit succeed: selected v2 for
a successful correction, or a fully verified restoration after a non-resource failure. Consumed
allowance remains one. A resource block or unknown restoration remains blocked; there is no manual
clear option for this lineage.
Corrupted selected bytes remain unverified;
there is no fallback to v1. Selected versions and consumed repair allowances accompany checkpoints.

The temporary runner copy wraps Playwright `test.step`（测试步骤）at runtime. Any step promise still
pending when the test ends produces `ARC_OBSERVATION_UNFINISHED_STEP` and makes that atom unverified.
The correction eligibility comes from an executed failed, non-application parsed test with the guard
configuration error. Aggregate text alone, or an application error using the same marker string,
cannot authorize correction or spend its allowance.
Frozen script bytes are untouched; the evidence retains the actual runner copy and guard. Awaited and
nested awaited steps retain their normal behavior and errors. This protects unfinished steps; it does
not detect every missing `await`（等待）, especially a step that has already settled, or decide whether
an expected value is faithful to the requirements. The guard is appended to preserve original line
numbers and strict-mode directives; a top-level alias captured before wrapping can bypass it.
An executed failure with both a structured HTTP-page error location and the first stack frame in a
document event handler is classified as an application failure. A URL mentioned in a message alone,
test-origin error or `page.evaluate`（页面脚本求值）error cannot establish that origin. Only executed
assertion failures or these clearly sourced application failures in valid runs are
then collected; script, planning, skipped and configuration errors stay unverified and never trigger
an application fix. Those failures permit
one bounded application repair pass（修复阶段）for the whole run. Failures are grouped stably by
module, with at most eight atoms per batch. Script correction consumes one of the existing eight
normal repair turns, leaving at most seven product batches when used. Without correction, at most
eight product batches remain. Each batch uses a fresh
session, a compact file handoff and its own source snapshot（源码快照）outside the workspace with a
path→hash manifest. Host build/startup validation retains a successful batch. An interrupted batch
restores that batch's snapshot in the actual delivery directory, preserving earlier valid batches.
An exact remote compact/remote compaction（远程压缩）error permits at most one extra fresh-session
recovery in the entire pass after saving the failed draft and validating the restored runtime.
A second compaction error or other repair failure stops further repair calls. Quota（额度）or
authentication（认证）denial stops all later model requests. The normal-turn counters and single extra
compaction recovery allowance persist across resume; a rewritten working receipt cannot reset the
checkpoint authority. A failed snapshot permits no model mutation. Batch metadata records attempts, failures,
recovery and unprocessed atoms; `kept` means retained source, never full functional passage.
A kept repair that changed deliverable source
gets exactly one frozen replay; unchanged source is not re-observed.

For M ROOT modules the normal budget is M coding turns + M
observation-planning turns + at most 8 normal repair turns shared by script correction and product
repair, and 1 extra compaction recovery turn (no product repair when nothing really failed), plus
the existing separately bounded transport and technical recovery turns when needed.
Results are bound to the final application digest and report passed,
failed and unverified separately. A successful exit, source checkpoint or Git commit is not full
requirement acceptance, and none of it is a platform score.

The host binds shared interface declarations to real definition and consumer file hashes in
.arc/engineering/state.json. Coding and repair receive current source excerpts and stale flags;
changed files require reading their real definitions and consumers again. Frozen observation and handoff metadata accompany checkpoints. Repair context includes prior passed, failed and unverified atoms;
acceptance retains before/after evidence and records improvements and regressions without a new
score-based delivery veto.

## Connected flow and remaining limits（已接通流程与边界）

| Capability | Before v5 | Current behavior |
| --- | --- | --- |
| Deterministic source preparation（确定性源码准备） | Whole-source model plan | Source IDs and dependencies checked without a model turn |
| Isolated observation planning（隔离观察规划） | All module scripts planned upfront | Each script freezes immediately before its module's coding |
| Runtime observation（运行期观察） | Concentrated after generation | Early provisional evidence feeds the next coding turn; final acceptance rechecks |
| Product repair and frozen replay（产品修复与冻结复验） | Present | Kept: executed failures, bounded batches, source restoration and final replay |
| Definite script-error correction（确定脚本错误纠正） | Missing | One source-only correction, immutable v1, selected v2 and shared repair budget |
| Unfinished asynchronous steps（未完成异步步骤） | Could look like product failure or even pass | Runtime guard makes affected atoms unverified; one exact await-only script correction is allowed, and this signal never triggers product repair |
| Version, cache and resume integrity（版本、缓存及恢复一致性） | v1 only | Selected version/hash and original v1 bound together; stale passes rejected |
| General test semantics and scenario calibration（测试语义与场景校准） | Unproven | Still unproven; offline correct/fault controls validate specific behaviors only |

The ordered flow is:

1. Validate original source, dependency order and resume identity without a model turn.
2. Immediately before each unfinished module, plan and freeze its source-only observation script.
3. Code the module with prior provisional evidence and file-bound shared interfaces; build, start,
   checkpoint and commit runnable source.
4. Execute early observations in isolated runtime state. A permitted script correction uses the shared
   budget and a separate frozen version. Record provisional results and pass them to the next module.
5. At the end, execute each trustworthy selected script again. Partial/blocked runs only observe
   completed modules offline, constructing no model client; all other atoms remain unverified.
6. Collect valid executed assertion failures and clearly sourced application failures; repair small
   batches within the remaining shared budget,
   restore interrupted batches, then replay frozen checks after retained product changes.
7. Report delivered source, generation status, passed/failed/unverified atoms and evidence separately.

Offline native Playwright controls verify correct persistence, an injected persistence fault,
malformed tests, wrong expectations, unfinished steps and browser-origin errors. Wrong expectations
can still fail on a correct application; this implementation cannot automatically identify every such
case. The controls are neither all 100 task scenarios nor an official score or new model-generation
result. A new local generation or platform run needs separate authorization.

## Entrypoint and recovery（入口与恢复）

```sh
python3 main.py /path/to/requirements --output-dir /path/to/output --type web
python3 main.py /path/to/requirements --output-dir /path/to/new-output --resume-from /path/to/old-output
```

The input contains requirements.yaml with id ROOT. Preserve a complete platform-prepared app;
otherwise copy template/. A partial app is rejected. Resume requires a separate output directory
and checks input, agent/dependency, template, skill, runtime, model and gateway identities.
Old-version checkpoints are rejected; no automatic migration is provided.

A restored output first receives a real recovery-baseline commit, then is rebuilt and started.
The old directory/history stays intact. Only the contiguous completed prefix is skipped.
Later runnable source after a gap is saved and committed without skipping the failed module.
Snapshot and Git publication are not atomic: a commit failure retains the snapshot, records
commit_failed, stops generation and still delivers the restored candidate. The host does not ask
the coding model to repair Git. Checkpoint identity is still enforced, so a checkpoint from a
different requirement set, generator or model configuration can never be claimed as completed work.

Recovery metadata is in .arc/codex-recovery/. Observations and their host-owned source metadata,
frozen scripts, implementation declarations and version-bound acceptance.json live in
module-observations/. Runtime databases and observation media are excluded from source snapshots,
and the repair snapshot additionally excludes .arc and module-observations so a restore can never
delete original evidence. Final technical checks and acceptance records precede a final-candidate
commit. Even an all-completed resume validates current source and frozen inputs before reusing
acceptance. Restoring the snapshot also clears the dependency-sync marker, so the next install
follows the restored package files rather than the rejected repair.

ARCBENCH_RESUME_FROM is the resume environment default. A local archive may carry a copied
recovery-input/ directory and set it to /workspace/submission/recovery-input.
Public platform support/persistence for this extra variable has not been verified.

## Dependencies and diagnostics（依赖与诊断）

openai-codex is pinned to 0.160.0 with its matching bundled CLI（命令行运行时）.
OPENAI_API_KEY, OPENAI_BASE_URL and MODEL come from the runner. Actual competition gateway
and model responses remain unverified. VISUAL_MODEL affects identity, not image-call routing.
The existing CLI configuration explicitly sets web_search="disabled".

Dependency installation includes dev dependencies, is limited to 600 seconds and falls back
from npm ci only for an explicit lock mismatch or absent lock. Builds are limited to 180 seconds.
Model calls retain SDK behavior; no new host-wide model deadline is imposed.
Started/returned phases are recorded, without inventing a cause for time spent waiting.
When the installed SDK exposes `Thread.turn()` and `TurnHandle.interrupt()`, model turns use a
soft warning and a conservative hard limit (45 minutes for coding, 30 minutes for planning or
repair, and 15 minutes for a retry). A timeout restores at most once; retries keep the current
module subtree, atomic IDs, original requirement path and checkpoint while dropping repeated
history. Older SDKs retain the original `thread.run()` compatibility path and require the outer
runner to provide any process-level kill boundary.
The host records a non-authoritative working checkpoint after each model or repair batch; SDK
internal tool items are not individually observable through the compatibility `thread.run()` path.
Auxiliary handoff/reporting failures are recorded separately and fall back to original requirements
and actual source when validated recovery authority is safe. Untrusted existing recovery metadata
stops further requests instead of resetting correction or repair allowances. SDK constructor, entry,
login and exit failures reach the delivery tail; original errors and completed source checkpoints
are retained. Hard resource stops allow only offline observations of eligible completed modules.
An empty starter scaffold is never claimed as completed delivery.
The summary records host generation elapsed time, first coding start when reached, and per-module
observation-planning duration. Usage is null when the SDK supplies no usage for these host calls;
no token or cost estimate is invented.
Console and summary separate generated/committed modules, passed/failed chains and unverified IDs.

Interrupted generation also exports module-observations/generation.json. It lists every source
module and atomic requirement, delivered checkpoint status, redacted failures and recovery/draft
paths. A failed module's implementation.json remains a historical declaration and is explicitly
marked as not delivery evidence; its files may have been rolled back. Draft paths point to the
existing private recovery directory, which the platform may omit from the downloaded application.
Reporting errors do not withhold source or permit another request after a resource block.

The existing single transport retry now starts a fresh session only after the failed draft is
retained and the previous checkpoint is restored with matching product hashes and completed IDs.
Unavailable, rolled-back or indeterminate restoration stops the run; a first-module interruption
without a verified checkpoint stops with the original workspace intact. Authentication, permission
and quota failures stop further planning, coding and repair requests. Other transient errors are
not labeled as remote compaction without actual supporting error text.

When generation is partial or blocked and has completed modules, final acceptance runs without
constructing or logging into a model client. It executes only trustworthy, already prepared frozen
checks for completed modules; it never replans or repairs in this mode. All other source atoms are
explicitly unverified, including when an acceptance tool fails. Existing interface handoff,
source-linked scenarios and repair replay remain in place, but these host checks do not establish
complete scenario semantics, real-model recovery effectiveness or improved official scores.

```sh
python -B -m unittest discover -s tests -p test_recovery.py -v
python -B -m unittest discover -s tests -p test_sdk_compatibility.py -v
```

Run the real SDK group separately in a freshly installed, credential-free process; recovery tests
substitute the model client. Local tests do not prove real-model generation stability. Bounded
client recovery mitigates interrupted repair; it does not establish that the SDK（开发工具包）or
gateway compaction protocol defect has been fixed.
No agent script automatically starts a simulation, uploads a submission or runs platform evaluation.

See template/TEMPLATE_GUIDE.md for neutral application facilities. The seed entrypoint remains
empty and is awaited after schema initialization before listening. Generated seeds add only missing
required records and preserve successful user changes on restart; seed failure stops startup.
Preparing a test database does not provision production data.
