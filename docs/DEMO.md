# Demo script, X post and form answers

Target length: 85 seconds. Hard limit: 90 seconds. Record at 1080p. Phone chat on the left half, Kestra UI on the right half where noted. Captions are burned in; voice-over is optional and uses the same words.

Before recording, from a clean state: `rig/reset.sh` (or deploy `kestra/demo/orders_etl.yaml` at revision 1), the forwarder enabled, Hermes gateway running with the webhook route and a Telegram delivery target. Run the flow once, off camera, only if you want a failed execution already in the list. Better: trigger it on camera in shot 2.

<!-- TODO(orchestrator): after the e2e run, replace the placeholders marked [REAL] with the measured values (times, revision numbers, incident id) from evidence/. Never state a number that is not in evidence/. -->

## Shots

| # | Time | On screen | Caption / voice-over |
|---|---|---|---|
| 1 | 0:00 to 0:06 | Kestra UI, flow `orders_etl` topology: `extract`, `transform`, `load`. Cursor on **Execute**. | "A daily pipeline. The supplier just renamed a column." |
| 2 | 0:06 to 0:14 | Execution runs. `extract` goes green, `transform` goes red. Execution page shows FAILED with the ValueError in the logs. | "It fails in transform. Normally this waits until someone wakes up." |
| 3 | 0:14 to 0:24 | Cut to the phone. Telegram message from Hermes arrives: incident id, one sentence of root cause ("the CSV column is now `total_amount`, the script reads `amount`"), the diff with one `-` line and one `+` line, `/kestra apply INC-... <hash>` with the 12-character hash already filled in [REAL: incident id and hash]. | "Hermes got the failure, read the logs and what changed, and proposes a fix. It has changed nothing yet." |
| 4 | 0:24 to 0:30 | Phone: scroll the diff slowly. Highlight the SHA-256 line. | "This diff is exactly what will be written. The approval is tied to its hash." |
| 5 | 0:30 to 0:40 | Phone: copy the printed command and send it: `/kestra apply INC-... <hash>`. The reply says the fix is being applied against base revision [REAL: revision]. | "One approval: you type the command that names the hash of the diff you read. A different diff would be refused." |
| 6 | 0:40 to 0:55 | Split screen. Phone: "applying", then "verified". Kestra UI (right): flow revision 1 becomes 2, a new execution appears, `extract` is already green and is not running, `transform` and `load` run and go green. | "It saves a new revision, then replays from the failed task. `extract` is not run again." |
| 7 | 0:55 to 1:03 | Kestra execution page of the replay: Gantt view. Hover `extract` to show the original timestamps. Phone shows the final report: `verified`, fixed revision [REAL], replay execution id [REAL]. | "Verified: the failed task now succeeds and the earlier task was reused." |
| 8 | 1:03 to 1:12 | Phone: `/kestra pauses`. Shows `refund_approval` waiting with the question, its two inputs and the ready-to-type commands. Type the printed command `/kestra resume <exec> <taskrun> approved=true reason="checked"`. Kestra UI: execution goes on and finishes. | "Kestra Pause tasks become chat questions too." |
| 9 | 1:12 to 1:20 | Hermes dashboard, **Kestra on-call** tab: incidents with status `verified`, the diff, pending pauses empty. | "A read-only dashboard tab shows every incident and pause." |
| 10 | 1:20 to 1:25 | Title card: "Kestra On-Call for Hermes Agent". Under it: "fail-closed namespaces, protected flows, one approval, guarded rollback". Repo URL. | "Open source, MIT. github.com/zkasuran/hermes-kestra-oncall" |

Total: 85 seconds.

If the cut runs long, drop shot 8 first, then shot 9. Shots 1 to 7 are the story and must stay.

### Recording notes

- Use a screen recording of the phone (or a Telegram desktop window sized like a phone) so text stays sharp.
- Show the Kestra execution list before and after in shot 6 so the new revision and the single new execution are visible.
- Do not show tokens, the `.env` file or the Kestra password. Blur the Telegram chat list.
- Do not speed up shot 5; the moment where you read the diff and then send the command with its hash is the point.
- A safety shot only if there is time left in the cut: run the same flow with the namespace removed from `namespaces`. Hermes says nothing is allowed and writes nothing. Not part of the 85 seconds.

## X post (279 characters, under 280)

```
A Kestra flow failed. Hermes read the logs, sent the diff to my phone, and after one approval fixed the flow and replayed from the failed task. The task that already succeeded did not rerun.

Kestra On-Call for @NousResearch Hermes Agent:
github.com/zkasuran/hermes-kestra-oncall
```

Attach the video. Post it from the account that owns the submission. Add the post link to the form.

## Form answers (draft)

The form questions were not available when this was written. Adjust the labels to match; keep the content.

**Project name:** Kestra On-Call for Hermes Agent (`kestra-oncall`)

**One sentence:** A Hermes plugin that puts Hermes on call for Kestra: it diagnoses a failed execution, proposes a fix, and after one human approval updates the flow and replays from the failed task.

**What it does:** When an execution in an allowed Kestra namespace fails, Hermes reads the failed task, error logs and a diff against the last good run, and proposes the smallest flow change. The plugin validates the proposal with Kestra, stores it, and shows the exact diff and hash in chat. After approval it saves a new flow revision, replays the failed execution from the failed task (earlier successful tasks are reused), follows the replay and verifies it. If the replay fails it restores the previous revision when the flow is still at the fixed one. Kestra Pause tasks become chat questions that can be resumed or killed from any Hermes surface. A read-only dashboard tab and Desktop pane list incidents and pauses.

**Why it is more than a prompt or a skill:** It registers seven tools, a `pre_tool_call` approval hook, a `/kestra` command, a CLI (`setup`, `doctor`), a webhook route, a bundled skill, a SQLite store with a step journal, and a dashboard tab with its own read-only API. The write path is idempotent and resumable: stale guard, read-back after the write, one replay adopted on retry, guarded rollback.

**Who it is for:** Anyone who runs Kestra pipelines and wants failures triaged and repaired without opening the UI at night, while keeping a human in the loop for every write.

**Safety and reliability:** Empty namespace list allows nothing. Protected flows are diagnosis only. Approval is bound to the stored proposal hash. Unattended runs (webhook, cron) cannot write, even if Hermes auto-approves. Credentials come only through Hermes secret scope and are redacted from everything shown. Limit stated plainly: Kestra has no compare-and-set on flow updates, so the check-then-write window is narrowed, not removed.

**How it was tested:** [REAL] Fill from `evidence/`: Kestra 2.0.9, Hermes version, the end-to-end scenarios and their results (healthy control ignored, lookalike namespace ignored, protected flow refused, stale proposal refused, refused write, rollback, interrupted apply resumed, second apply is a no-op, pause stale guard), run through a real Hermes gateway and webhook with a real model.

**Hermes surfaces used:** Tools, hooks (`pre_tool_call`), slash command, CLI command, bundled skill, webhook route, dashboard plugin, Desktop plugin, plugin settings and secret scope.

**Repository:** https://github.com/zkasuran/hermes-kestra-oncall

**Catalog PR:** [REAL] link to the `plugin-catalog/kestra-oncall.yaml` PR in NousResearch/hermes-agent.

**Demo video:** [REAL] link.

**X post:** [REAL] link.

**AI use disclosure:** Implementation was AI-assisted. The behaviour described above was checked against a real Kestra 2.0.9 and a real Hermes install.
