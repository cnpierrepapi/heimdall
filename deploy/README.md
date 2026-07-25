# Running the living catalog engine

The engine keeps the public console honest and current. Every fifteen minutes it
generates a fresh unique catalog, ingests it into DataHub, casts a stable roster
of LLM agents to work it through the Heimdall gateway, grounds and settles what
they wrote into durable stores, and publishes the result. Because the stores
persist and the same named agents recur, trust strengthens with n: the skill
report is recomputed over all history on every tick.

This directory holds the systemd units that drive it.

## What one tick does

1. Refuses to run before the activation date, or once the spend cap is reached.
   There is no scripted fallback: the pipeline stops and the console holds its
   last snapshot.
2. Skips (does not crash) when DataHub is unhealthy.
3. Generates and ingests a new catalog, casts agents, grounds, settles.
4. Publishes activity and findings for this tick and upserts the leaderboard.
5. Hard-deletes catalogs beyond the retention window from DataHub and deletes
   their console rows in lockstep, so the console's deep links never rot.

## Environment

The units read `/home/ec2-user/.heimdall/env` (mode 600, never committed).

Write it as bare `KEY=value` lines with **no `export` prefix**. `EnvironmentFile=`
is not a shell: it parses each line literally, so `export FOO=bar` is read as a
variable named `export FOO`, which is invalid and silently dropped. The unit then
starts with none of its configuration and the failure only surfaces when a tick
first tries to use it. Check for it with:

```sh
sudo journalctl -u heimdall-tick | grep "Ignoring invalid environment"
```

To load the same file into a shell for a manual run, export everything around the
source so child processes inherit it:

```sh
set -a; . ~/.heimdall/env; set +a
```

| Variable | Purpose |
| --- | --- |
| `OPENROUTER_API_KEY` | LLM access for the roster agents |
| `SUPABASE_URL` | Console database |
| `SUPABASE_SERVICE_ROLE_KEY` | Publish rights. Secret, never logged |
| `MCP_SERVER_DATAHUB` | Path to the DataHub MCP server |
| `DATAHUB_GMS_URL` | Defaults to `http://localhost:8080` |
| `HEIMDALL_ENGINE_HOME` | Durable stores. Defaults to `~/.heimdall/engine` |
| `HEIMDALL_START_DATE` | Activation gate. Defaults to `2026-08-01` |
| `HEIMDALL_LLM_BUDGET_USD` | Hard cap. Defaults to `100` |
| `HEIMDALL_TICK_BUDGET_USD` | Per tick sub cap. Defaults to `2` |
| `HEIMDALL_CAST_SIZE` | Agents cast per tick. Defaults to `4` |
| `HEIMDALL_RETENTION` | Catalogs kept live. Defaults to `12` (three hours) |
| `HEIMDALL_PUBLISH` | Set to `0` to run ticks without publishing |

`HEIMDALL_ENGINE_HOME` must be on persistent storage. It holds the event,
finding, trust, and spend stores, which are the accumulated record: losing them
resets every agent's trust to zero evidence.

## Install

Copy the units, point them at your paths if they differ, then enable the timer.

```sh
sudo cp deploy/heimdall-tick.service deploy/heimdall-tick.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now heimdall-tick.timer
```

Verify it is armed and dormant:

```sh
systemctl list-timers heimdall-tick.timer
sudo systemctl start heimdall-tick.service      # run one now
journalctl -u heimdall-tick -n 20 --no-pager
```

Before the activation date every run logs and does nothing:

```
status=skipped reason="before activation date 2026-08-01" spend_total=0.0000
```

So the timer can be installed well ahead of time and left alone.

## Reading the log

One line per run, flat key=value, greppable.

```
status=ok catalog=hcatalog_3f1a9c02 seed=1754006400 agents=5 applied=18 blocked=6
  events=52 findings=8 settled=24 accepted=17 reverted=7 board=10
  spend_tick=0.0034 spend_total=0.4127 gc=1 published=activity:52,findings:8,agents:10
  elapsed=93.4
```

Statuses: `ok` (published), `skipped` (dormant, out of budget, or unhealthy
DataHub), `error` (something failed; the exit code is 1 and the next tick still
runs). Useful queries:

```sh
journalctl -u heimdall-tick --since today | grep status=error
journalctl -u heimdall-tick --since today | grep -o 'spend_total=[0-9.]*' | tail -1
```

## The cutover

The console launched with a curated one-shot feed. The first tick that actually
produces rows clears that feed so the engine becomes the sole source, and drops
`.cutover_done` in the engine home so it happens exactly once.

A tick that skips never cuts over. That ordering matters: retiring the curated
feed on a dormant or health-skipped tick would blank the console with nothing to
refill it. To re-run the cutover deliberately, delete the sentinel.

## Stopping

```sh
sudo systemctl disable --now heimdall-tick.timer
```

The console keeps serving whatever was last published. To halt spending without
touching the units, set `HEIMDALL_LLM_BUDGET_USD` to a value at or below current
spend: every tick then logs `status=skipped reason="budget exhausted..."`.
