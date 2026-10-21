# vikunja2kaneo

Move your **open Vikunja tasks, with their projects**, into [Kaneo](https://kaneo.app).

Two commands, one Python file, no dependencies.

```bash
export VIKUNJA_URL=https://todo.example.com VIKUNJA_TOKEN=tk_xxx
python3 vikunja2kaneo.py export

export KANEO_URL=https://kaneo.example.com KANEO_TOKEN=xxx
python3 vikunja2kaneo.py import            # dry run, prints the plan
python3 vikunja2kaneo.py import --apply    # shows the plan, asks, then writes
```

`--apply` prints what it is about to do — projects, tasks, and which board
columns each project needs — and waits for a `y`. It refuses to run in a pipe
without `--yes`, so a script can't write to your instance by accident.

`export` produces two files in `./vikunja-export/`:

| file | what it is |
|---|---|
| `open_tasks.json` | raw Vikunja payload, lossless. This is what `import` reads |
| `open_tasks.csv` | flattened, human-readable, descriptions converted to markdown |

## Requirements

- Python 3.8+ (stdlib only)
- A Vikunja API token: *Settings → API Tokens*, needs task read access
- A Kaneo API token with project and task write access. On **Kaneo cloud**
  (`cloud.kaneo.app`) the same API is served at the same paths and API keys are
  enabled, so create one in the app and use it as `KANEO_TOKEN`
- Both instances reachable over HTTP(S)

Nothing is installed and nothing is imported outside the standard library.

## What gets migrated

For every Vikunja project that has at least one open task, a Kaneo project is
created (existing projects with the same name are reused). Closed tasks are
ignored.

Per task: **title**, **description**, **priority**, **due date**, and the
kanban column. Labels, assignees, comments, attachments and subtasks are
**not** migrated: Vikunja's task list can only expand `subtasks`, `buckets`
and `comments`, so labels and assignees would each cost one extra request per
task — a few hundred tasks would spend the whole rate limit on them.

Each description ends with a provenance line, so anything can be traced back:

```
_Migrated from Vikunja #562 (column: To-Do)_
```

### Columns

| Vikunja column | Kaneo |
|---|---|
| Backlog | the `planned` status — Kaneo's Backlog view, not a column |
| To-Do | column `to-do` |
| Doing | column `in-progress` |
| Testing | column `in-review` |
| anything else | a column with the same name, created if missing |

Kaneo's **Backlog is a status, not a board column**: tasks with status
`planned` live in the project's Backlog tab and stay off the board. So Vikunja
tasks that were sitting in Backlog are migrated to `planned`, and no column is
invented for them. (A Kaneo column *named* "Backlog" is just a normal board
column — it does not replace the view.)

For the rest, the importer looks at the columns the tasks actually need, checks
what the project has, and creates only what is missing — then orders the board
the way the source board was ordered. A Vikunja column with no Kaneo equivalent
keeps its own name instead of being folded into `to-do`. Columns that would
hold no task are never created.

### Priorities

| Vikunja | Kaneo |
|---|---|
| 0 (unset) | `no-priority` |
| 1 | `low` |
| 2 | `medium` |
| 3 | `high` |
| 4 (urgent) | `urgent` |
| 5 (DO NOW) | `urgent` |

## Renaming projects on the way in

```bash
python3 vikunja2kaneo.py import --rename "Inbox=Personale" --apply
```

Repeatable. Applied only when the project is created, matched against the
Vikunja project name.

## Safe to re-run

`import` is a dry run unless you pass `--apply`. Every created project and task
id is written to `vikunja-export/kaneo_map.json` as it goes, so an interrupted
run resumes where it stopped and a second `--apply` creates nothing new.

To roll back, delete the ids listed in `kaneo_map.json`:

```bash
python3 - <<'EOF'
import json, os, urllib.request
m = json.load(open("vikunja-export/kaneo_map.json"))
url, tok = os.environ["KANEO_URL"].rstrip("/"), os.environ["KANEO_TOKEN"]
for task_id in m["tasks"].values():
    req = urllib.request.Request(f"{url}/api/task/{task_id}", method="DELETE",
                                 headers={"Authorization": "Bearer " + tok})
    urllib.request.urlopen(req)
EOF
```

Projects are left behind on purpose — deleting a project with tasks in it is
not something a migration script should do silently.

## Notes and limits

- **Rate limit.** A Kaneo API key allows 100 requests per minute. A few hundred
  tasks will hit it; the script waits for `Retry-After` and continues.
- **Descriptions** are HTML in Vikunja and markdown in Kaneo. Links, line
  breaks and list items are converted; exotic markup (tables, inline styles) is
  flattened to text.
- **Due dates** are copied verbatim, including dates already in the past.
  Vikunja's zero date (`0001-01-01`) is treated as "no due date".
- **One workspace.** Projects are created in the first workspace the token can
  see. Pass a token that only sees the workspace you want if that's ambiguous.
- **Tested against** a self-hosted Kaneo and Kaneo cloud, October 2026: 83
  tasks across 6 projects, read back from the API. It needs a Kaneo that has the
  Backlog view (the `planned` status); older releases don't.
- **Not a sync.** It's a one-way copy, run once. Running it again after adding
  tasks to Vikunja will pick up the new ones only.

## License

MIT
