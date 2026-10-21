#!/usr/bin/env python3
"""Migrate open Vikunja tasks, with their projects, into Kaneo.

    python3 vikunja2kaneo.py export          # Vikunja -> ./vikunja-export/*.csv|json
    python3 vikunja2kaneo.py import          # dry run: print the plan, write nothing
    python3 vikunja2kaneo.py import --apply  # ask once, then write into Kaneo

Credentials come from the environment (or the matching flags):

    VIKUNJA_URL, VIKUNJA_TOKEN    export
    KANEO_URL,   KANEO_TOKEN      import

Stdlib only, single file, no dependencies.
"""

import argparse
import csv
import html
import json
import os
import re
import sys
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

if sys.version_info < (3, 8):
    raise SystemExit("vikunja2kaneo needs Python 3.8 or newer")

# Kaneo's backlog is a task status ("planned"), not a board column. Vikunja's
# Backlog column maps to it, so those tasks land in the project's Backlog view
# instead of inventing a board column that would sit there pretending to be one.
PLANNED = "planned"

# Vikunja column name -> Kaneo column slug, where the two spell it differently.
# Anything else keeps its own name, slugified: a Vikunja column with no
# equivalent becomes a new Kaneo column rather than being folded away silently.
ALIASES = {"doing": "in-progress", "testing": "in-review"}

DEFAULT_STATUS = "to-do"

# Vikunja priority (0-5) -> Kaneo priority. 5 ("DO NOW") folds into urgent.
PRIORITY = {
    0: "no-priority",
    1: "low",
    2: "medium",
    3: "high",
    4: "urgent",
    5: "urgent",
}

ICON = "Layout"
CSV_FIELDS = [
    "project",
    "task_id",
    "identifier",
    "title",
    "description",
    "status",
    "priority",
    "due_date",
    "created",
    "updated",
]


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------


def _request(method, url, token, body=None, tries=8):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        url,
        data=data,
        method=method,
        headers={
            "Authorization": "Bearer " + token,
            "Content-Type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return json.load(r)
    except urllib.error.HTTPError as e:
        # Kaneo allows 100 requests per window; a full migration spans several.
        if e.code == 429 and tries > 0:
            wait = int(e.headers.get("Retry-After") or 30) + 1
            print(f"  [rate limit] sleeping {wait}s", file=sys.stderr, flush=True)
            time.sleep(wait)
            return _request(method, url, token, body, tries - 1)
        detail = e.read().decode(errors="replace")[:300]
        raise SystemExit(f"{method} {url} -> HTTP {e.code}: {detail}")


def html_to_md(s):
    """Vikunja stores descriptions as HTML; Kaneo renders markdown."""
    if not s:
        return ""
    s = re.sub(
        r'(?is)<a[^>]*href="([^"]*)"[^>]*>(.*?)</a>',
        lambda m: f"[{m.group(2)}]({m.group(1)})",
        s,
    )
    s = re.sub(r"(?i)<br\s*/?>", "\n", s)
    s = re.sub(r"(?i)<li[^>]*>", "- ", s)
    s = re.sub(r"(?i)</(p|div|li|h[1-6])>", "\n", s)
    s = re.sub(r"<[^>]+>", "", s)
    s = html.unescape(s)
    s = re.sub(r"[ \t]+", " ", s)
    return re.sub(r"\n{3,}", "\n\n", s).strip()


def clean_date(d):
    """Vikunja uses the zero time for 'unset'."""
    if not d or d.startswith("0001-01-01"):
        return ""
    return d


def slugify(s, limit=50):
    s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode()
    s = re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")
    return s[:limit].strip("-") or "project"


def status_for(bucket):
    """Kaneo status for a task, given the name of the Vikunja column it sits in."""
    name = (bucket or "").strip()
    if not name:
        return DEFAULT_STATUS
    key = name.lower()
    if key == "backlog":
        return PLANNED
    return ALIASES.get(key, slugify(name))


def priority_for(value):
    return PRIORITY.get(int(value or 0), "no-priority")


def _rename_pair(value):
    if "=" not in value:
        raise argparse.ArgumentTypeError("expected OLD=NEW, e.g. Inbox=Personale")
    return tuple(value.split("=", 1))


def needed_columns(group, order_hint):
    """Ordered [(slug, column name)] of board columns these tasks need.

    "planned" is a status, not a column, so it never appears here. Order follows
    the source board when the export recorded it, else first appearance.
    """
    seen = {}
    for t in group:
        slug = status_for(t["_bucket"])
        if slug != PLANNED and slug not in seen:
            seen[slug] = t["_bucket"].strip()
    if order_hint:
        rank = {title: i for i, title in enumerate(order_hint)}
        return sorted(seen.items(), key=lambda kv: rank.get(kv[1], len(rank)))
    return list(seen.items())


# --------------------------------------------------------------------------
# export: Vikunja
# --------------------------------------------------------------------------


def vikunja(url, token, path, **query):
    full = url.rstrip("/") + path
    if query:
        full += "?" + urllib.parse.urlencode(query)
    return _request("GET", full, token)


def bucket_order(url, token, project_id):
    """Column titles left to right, so the Kaneo board can be laid out the same."""
    for view in vikunja(url, token, f"/api/v1/projects/{project_id}/views"):
        if view.get("view_kind") == "kanban":
            buckets = vikunja(
                url, token, f"/api/v1/projects/{project_id}/views/{view['id']}/buckets"
            )
            return [b["title"] for b in sorted(buckets, key=lambda b: b["position"])]
    return []


def cmd_export(args):
    projects = {
        p["id"]: p["title"]
        for p in vikunja(args.vikunja_url, args.vikunja_token, "/api/v1/projects")
    }

    tasks, page = [], 1
    while True:
        batch = vikunja(
            args.vikunja_url,
            args.vikunja_token,
            "/api/v1/tasks",
            filter="done = false",
            per_page=50,
            page=page,
            expand="buckets",
        )
        tasks.extend(batch)
        if len(batch) < 50:
            break
        page += 1

    order = {
        pid: bucket_order(args.vikunja_url, args.vikunja_token, pid)
        for pid in {t["project_id"] for t in tasks}
    }

    out = Path(args.dir)
    out.mkdir(parents=True, exist_ok=True)
    (out / "open_tasks.json").write_text(
        json.dumps(
            {"projects": projects, "bucket_order": order, "tasks": tasks},
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )

    with open(out / "open_tasks.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=CSV_FIELDS)
        w.writeheader()
        for t in sorted(tasks, key=lambda t: (t["project_id"], t["id"])):
            buckets = t.get("buckets") or []
            w.writerow(
                {
                    "project": projects.get(t["project_id"], f"#{t['project_id']}"),
                    "task_id": t["id"],
                    "identifier": t.get("identifier") or "",
                    "title": t["title"],
                    "description": html_to_md(t.get("description")),
                    "status": buckets[0]["title"] if buckets else "",
                    "priority": priority_for(t.get("priority")),
                    "due_date": clean_date(t.get("due_date")),
                    "created": clean_date(t.get("created")),
                    "updated": clean_date(t.get("updated")),
                }
            )

    per_project = {}
    for t in tasks:
        name = projects.get(t["project_id"], t["project_id"])
        per_project[name] = per_project.get(name, 0) + 1
    print(f"{len(tasks)} open tasks across {len(per_project)} projects -> {out}")
    for name, n in sorted(per_project.items(), key=lambda kv: -kv[1]):
        print(f"  {n:3d}  {name}")
    print(
        f"\n{len(projects) - len(per_project)} project(s) had no open tasks and are not in the export."
    )


# --------------------------------------------------------------------------
# import: Kaneo
# --------------------------------------------------------------------------


def kaneo(args, method, path, body=None):
    return _request(method, args.kaneo_url.rstrip("/") + path, args.kaneo_token, body)


def column_order(cols, needed):
    """Every column, the ones these tasks need first, in source order."""
    wanted = [slug for slug, _ in needed]
    by_slug = {c["slug"]: c for c in cols}
    rest = [
        c for c in sorted(cols, key=lambda c: c["position"]) if c["slug"] not in wanted
    ]
    return [by_slug[s] for s in wanted if s in by_slug] + rest


def ensure_columns(args, project_id, needed):
    """Create the board columns these tasks need, then order them like the source.

    Creates nothing the tasks will not use, and stops rather than writing tasks
    into a column that does not exist.
    """
    if not needed:
        return
    cols = kaneo(args, "GET", f"/api/column/{project_id}")
    have = {c["slug"] for c in cols}
    missing = [(slug, title) for slug, title in needed if slug not in have]
    for _slug, title in missing:
        kaneo(args, "POST", f"/api/column/{project_id}", {"name": title})
    if missing:
        cols = kaneo(args, "GET", f"/api/column/{project_id}")
        have = {c["slug"] for c in cols}

    absent = [slug for slug, _ in needed if slug not in have]
    if absent:
        raise SystemExit(
            f"{project_id}: column(s) missing after creation: {', '.join(absent)}"
        )

    # Send every column: a partial reorder leaves the omitted ones with colliding
    # positions.
    desired = column_order(cols, needed)
    current = sorted(cols, key=lambda c: c["position"])
    if [c["id"] for c in desired] != [c["id"] for c in current]:
        kaneo(
            args,
            "PUT",
            f"/api/column/reorder/{project_id}",
            {
                "columns": [
                    {"id": c["id"], "position": i} for i, c in enumerate(desired)
                ]
            },
        )


def build_description(task):
    body = html_to_md(task.get("description") or "")
    origin = f"_Migrated from Vikunja #{task['id']} (column: {task.get('_bucket') or 'n/a'})_"
    return (body + "\n\n" if body else "") + origin


def confirm(summary, assume_yes):
    if assume_yes:
        return
    if not sys.stdin.isatty():
        raise SystemExit("refusing to write without confirmation; re-run with --yes")
    print(summary)
    if input("\nProceed? [y/N] ").strip().lower() not in ("y", "yes"):
        raise SystemExit("aborted, nothing written")


def cmd_import(args):
    data = Path(args.dir)
    payload = json.loads((data / "open_tasks.json").read_text(encoding="utf-8"))
    projects = {
        int(k): v for k, v in payload["projects"].items()
    }  # JSON keys are strings
    order_hint = {int(k): v for k, v in (payload.get("bucket_order") or {}).items()}

    tasks = []
    for t in payload["tasks"]:
        buckets = t.get("buckets") or []
        t["_bucket"] = buckets[0]["title"] if buckets else ""
        tasks.append(t)

    map_path = data / "kaneo_map.json"
    mapping = (
        json.loads(map_path.read_text())
        if map_path.exists()
        else {"workspace": None, "projects": {}, "tasks": {}}
    )

    renames = dict(args.rename or [])

    ws, existing = "DRY-RUN", {}
    if args.apply:
        workspaces = kaneo(args, "GET", "/api/workspace")
        if not workspaces:
            raise SystemExit("no workspace visible to this token")
        ws = mapping.get("workspace") or workspaces[0]["id"]
        mapping["workspace"] = ws
        existing = {
            p["name"]: p["id"]
            for p in kaneo(args, "GET", f"/api/project?workspaceId={ws}")
        }

    by_project = {}
    for t in tasks:
        by_project.setdefault(t["project_id"], []).append(t)

    groups = []
    for vid, vname in sorted(
        projects.items(), key=lambda kv: -len(by_project.get(kv[0], []))
    ):
        group = by_project.get(vid, [])
        if not group:
            continue
        name = renames.get(vname, vname)
        groups.append(
            {
                "vid": vid,
                "name": name,
                "pid": mapping["projects"].get(str(vid)) or existing.get(name),
                "needed": needed_columns(group, order_hint.get(vid)),
                "pending": [t for t in group if str(t["id"]) not in mapping["tasks"]],
            }
        )

    total = sum(len(g["pending"]) for g in groups)
    new = [g["name"] for g in groups if not g["pid"]]
    summary = [f"Kaneo: {args.kaneo_url}"]
    for g in groups:
        cols = ", ".join(slug for slug, _ in g["needed"]) or "-"
        tag = "" if g["pid"] else "  [new project]"
        summary.append(
            f"  {g['name'][:42]:<42} {len(g['pending']):>3} tasks  columns: {cols}{tag}"
        )
    summary.append(
        f"\n{total} tasks, {len(new)} new project(s), {len(groups)} project(s) touched"
    )
    summary = "\n".join(summary)

    if not args.apply:
        print(summary)
        print("\ndry run, nothing written. Re-run with --apply.")
        return

    confirm(summary, args.yes)

    created = skipped = 0
    for g in groups:
        pid = g["pid"]
        if not pid:
            pid = kaneo(
                args,
                "POST",
                "/api/project",
                {
                    "name": g["name"],
                    "workspaceId": ws,
                    "icon": ICON,
                    "slug": slugify(g["name"]),
                },
            )["id"]
            mapping["projects"][str(g["vid"])] = pid
            map_path.write_text(json.dumps(mapping, indent=2), encoding="utf-8")
        ensure_columns(args, pid, g["needed"])
        print(f"\n{g['name']}  ->  {pid}   ({len(g['pending'])} tasks)")

        for t in g["pending"]:
            status = status_for(t["_bucket"])
            due = clean_date(t.get("due_date"))
            print(f"  [{status:11s}] {t['title'][:66]}")
            body = {
                "title": t["title"],
                "description": build_description(t),
                "priority": priority_for(t.get("priority")),
                "status": status,
            }
            if due:
                body["dueDate"] = due
            mapping["tasks"][str(t["id"])] = kaneo(
                args, "POST", f"/api/task/{pid}", body
            )["id"]
            map_path.write_text(json.dumps(mapping, indent=2), encoding="utf-8")
            created += 1
        skipped += len(by_project.get(g["vid"], [])) - len(g["pending"])

    print(
        f"\ncreated: {created} tasks in {len(groups)} projects; already migrated, skipped: {skipped}"
    )


# --------------------------------------------------------------------------


def selftest():
    assert slugify("KIDS - Infrastruttura AWS") == "kids-infrastruttura-aws"
    assert (
        slugify("CKA - Certified Kubernetes Administrator")
        == "cka-certified-kubernetes-administrator"
    )
    assert slugify("   ") == "project"

    # Vikunja's Backlog is Kaneo's "planned" status, never a board column.
    assert status_for("Backlog") == PLANNED
    assert status_for("To-Do") == "to-do"
    assert status_for("Doing") == "in-progress"
    assert status_for("Testing") == "in-review"
    assert status_for("Waiting on client") == "waiting-on-client"
    assert status_for("") == "to-do" and status_for(None) == "to-do"

    assert priority_for(5) == priority_for(4) == "urgent"
    assert priority_for(0) == "no-priority" and priority_for(None) == "no-priority"
    assert clean_date("0001-01-01T00:00:00Z") == ""
    assert clean_date("2027-01-04T11:00:00Z") == "2027-01-04T11:00:00Z"
    assert (
        html_to_md('<p>see <a href="https://x.it">here</a></p>')
        == "see [here](https://x.it)"
    )
    assert html_to_md("<ul><li>a</li><li>b</li></ul>") == "- a\n- b"
    assert html_to_md("a &amp; b") == "a & b" and html_to_md(None) == ""
    assert (
        build_description({"id": 5, "_bucket": "To-Do"})
        == "_Migrated from Vikunja #5 (column: To-Do)_"
    )

    group = [
        {"_bucket": "Backlog"},
        {"_bucket": "To-Do"},
        {"_bucket": "Doing"},
        {"_bucket": "Waiting on client"},
    ]
    # planned is not a column, order follows the source board, unknown names survive
    assert needed_columns(
        group, ["Backlog", "To-Do", "Doing", "Waiting on client"]
    ) == [
        ("to-do", "To-Do"),
        ("in-progress", "Doing"),
        ("waiting-on-client", "Waiting on client"),
    ]
    assert needed_columns([{"_bucket": "Backlog"}], []) == []
    assert needed_columns(group, [])[0] == ("to-do", "To-Do")

    cols = [
        {"id": "a", "slug": "to-do", "position": 0},
        {"id": "b", "slug": "in-progress", "position": 1},
        {"id": "c", "slug": "in-review", "position": 2},
        {"id": "d", "slug": "done", "position": 3},
    ]
    # every column comes back, not only the ones the tasks need
    assert [c["id"] for c in column_order(cols, [("to-do", "To-Do")])] == [
        "a",
        "b",
        "c",
        "d",
    ]
    assert [
        c["id"] for c in column_order(cols, [("done", "Done"), ("to-do", "To-Do")])
    ] == [
        "d",
        "a",
        "b",
        "c",
    ]
    print("selftest ok")


def main():
    p = argparse.ArgumentParser(description="Migrate open Vikunja tasks into Kaneo.")
    sub = p.add_subparsers(dest="cmd", required=True)

    e = sub.add_parser("export", help="read open tasks out of Vikunja")
    e.add_argument("--vikunja-url", default=os.environ.get("VIKUNJA_URL"))
    e.add_argument("--vikunja-token", default=os.environ.get("VIKUNJA_TOKEN"))
    e.add_argument("--dir", default="vikunja-export", help="output directory")
    e.set_defaults(func=cmd_export)

    i = sub.add_parser("import", help="write them into Kaneo (dry run unless --apply)")
    i.add_argument("--kaneo-url", default=os.environ.get("KANEO_URL"))
    i.add_argument("--kaneo-token", default=os.environ.get("KANEO_TOKEN"))
    i.add_argument(
        "--dir", default="vikunja-export", help="directory written by export"
    )
    i.add_argument(
        "--apply", action="store_true", help="actually create projects and tasks"
    )
    i.add_argument("--yes", action="store_true", help="skip the confirmation prompt")
    i.add_argument(
        "--rename",
        action="append",
        type=_rename_pair,
        metavar="OLD=NEW",
        help="rename a project on the way in (repeatable)",
    )
    i.set_defaults(func=cmd_import)

    sub.add_parser("selftest", help="run the built-in assertions").set_defaults(
        func=lambda a: selftest()
    )

    args = p.parse_args()
    if args.cmd == "export" and not (args.vikunja_url and args.vikunja_token):
        raise SystemExit("VIKUNJA_URL and VIKUNJA_TOKEN are required (env or flags)")
    if args.cmd == "import" and not (args.kaneo_url and args.kaneo_token):
        raise SystemExit("KANEO_URL and KANEO_TOKEN are required (env or flags)")
    args.func(args)


if __name__ == "__main__":
    main()
