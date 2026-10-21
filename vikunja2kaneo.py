#!/usr/bin/env python3
"""Migrate open Vikunja tasks, with their projects, into Kaneo.

    python3 vikunja2kaneo.py export          # Vikunja -> ./vikunja-export/*.csv|json
    python3 vikunja2kaneo.py import          # dry run: print what would be created
    python3 vikunja2kaneo.py import --apply  # write into Kaneo

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

# Vikunja kanban column -> Kaneo column slug. Kaneo ships only
# to-do / in-progress / in-review / done, so Backlog is created on demand.
STATUS = {"backlog": "backlog", "to-do": "to-do", "doing": "in-progress", "testing": "in-review"}
DEFAULT_STATUS = "to-do"

# Vikunja priority (0-5) -> Kaneo priority. 5 ("DO NOW") folds into urgent.
PRIORITY = {0: "no-priority", 1: "low", 2: "medium", 3: "high", 4: "urgent", 5: "urgent"}

COLUMN_ORDER = ["backlog", "to-do", "in-progress", "in-review", "done"]
ICON = "Layout"
CSV_FIELDS = ["project", "task_id", "identifier", "title", "description",
              "status", "priority", "due_date", "created", "updated"]


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------

def _request(method, url, token, body=None, tries=8):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method, headers={
        "Authorization": "Bearer " + token,
        "Content-Type": "application/json",
    })
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
    s = re.sub(r'(?is)<a[^>]*href="([^"]*)"[^>]*>(.*?)</a>',
               lambda m: f"[{m.group(2)}]({m.group(1)})", s)
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
    return STATUS.get((bucket or "").strip().lower(), DEFAULT_STATUS)


def priority_for(value):
    return PRIORITY.get(int(value or 0), "no-priority")


# --------------------------------------------------------------------------
# export: Vikunja
# --------------------------------------------------------------------------

def vikunja(url, token, path, **query):
    full = url.rstrip("/") + path
    if query:
        full += "?" + urllib.parse.urlencode(query)
    return _request("GET", full, token)


def cmd_export(args):
    projects = {p["id"]: p["title"] for p in vikunja(args.vikunja_url, args.vikunja_token, "/api/v1/projects")}

    tasks, page = [], 1
    while True:
        batch = vikunja(args.vikunja_url, args.vikunja_token, "/api/v1/tasks",
                        filter="done = false", per_page=50, page=page, expand="buckets")
        tasks.extend(batch)
        if len(batch) < 50:
            break
        page += 1

    out = Path(args.dir)
    out.mkdir(parents=True, exist_ok=True)
    (out / "open_tasks.json").write_text(
        json.dumps({"projects": projects, "tasks": tasks}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    with open(out / "open_tasks.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=CSV_FIELDS)
        w.writeheader()
        for t in sorted(tasks, key=lambda t: (t["project_id"], t["id"])):
            buckets = t.get("buckets") or []
            w.writerow({
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
            })

    per_project = {}
    for t in tasks:
        name = projects.get(t["project_id"], t["project_id"])
        per_project[name] = per_project.get(name, 0) + 1
    print(f"{len(tasks)} open tasks across {len(per_project)} projects -> {out}")
    for name, n in sorted(per_project.items(), key=lambda kv: -kv[1]):
        print(f"  {n:3d}  {name}")
    print(f"\n{len(projects) - len(per_project)} project(s) had no open tasks and are not in the export.")


# --------------------------------------------------------------------------
# import: Kaneo
# --------------------------------------------------------------------------

def kaneo(args, method, path, body=None):
    return _request(method, args.kaneo_url.rstrip("/") + path, args.kaneo_token, body)


def ensure_columns(args, project_id):
    """Kaneo has no Backlog column: create it, then enforce COLUMN_ORDER."""
    cols = kaneo(args, "GET", f"/api/column/{project_id}")
    if not any(c["slug"] == "backlog" for c in cols):
        cols.append(kaneo(args, "POST", f"/api/column/{project_id}", {"name": "Backlog"}))
    rank = {slug: i for i, slug in enumerate(COLUMN_ORDER)}
    cols.sort(key=lambda c: rank.get(c["slug"], 99))
    kaneo(args, "PUT", f"/api/column/reorder/{project_id}",
          {"columns": [{"id": c["id"], "position": i} for i, c in enumerate(cols)]})


def build_description(task):
    body = html_to_md(task.get("description") or "")
    origin = f"_Migrated from Vikunja #{task['id']} (column: {task.get('_bucket') or 'n/a'})_"
    return (body + "\n\n" if body else "") + origin


def cmd_import(args):
    data = Path(args.dir)
    payload = json.loads((data / "open_tasks.json").read_text(encoding="utf-8"))
    projects = {int(k): v for k, v in payload["projects"].items()}  # JSON keys are strings

    tasks = []
    for t in payload["tasks"]:
        buckets = t.get("buckets") or []
        t["_bucket"] = buckets[0]["title"] if buckets else ""
        tasks.append(t)

    map_path = data / "kaneo_map.json"
    mapping = json.loads(map_path.read_text()) if map_path.exists() else \
        {"workspace": None, "projects": {}, "tasks": {}}

    renames = dict(r.split("=", 1) for r in args.rename or [])

    if args.apply:
        workspaces = kaneo(args, "GET", "/api/workspace")
        if not workspaces:
            raise SystemExit("no workspace visible to this token")
        ws = mapping.get("workspace") or workspaces[0]["id"]
        mapping["workspace"] = ws
        existing = {p["name"]: p["id"] for p in kaneo(args, "GET", f"/api/project?workspaceId={ws}")}
    else:
        ws, existing = "DRY-RUN", {}

    by_project = {}
    for t in tasks:
        by_project.setdefault(t["project_id"], []).append(t)

    created = skipped = 0
    for vid, vname in sorted(projects.items(), key=lambda kv: -len(by_project.get(kv[0], []))):
        group = by_project.get(vid, [])
        if not group:
            continue
        name = renames.get(vname, vname)
        pid = mapping["projects"].get(str(vid))
        if not pid and args.apply:
            if name in existing:
                pid = existing[name]
            else:
                pid = kaneo(args, "POST", "/api/project", {
                    "name": name, "workspaceId": ws, "icon": ICON, "slug": slugify(name),
                })["id"]
            ensure_columns(args, pid)
            mapping["projects"][str(vid)] = pid
        print(f"\n{name}  ->  {pid or '<new:' + slugify(name) + '>'}   ({len(group)} tasks)")

        for t in group:
            key = str(t["id"])
            if key in mapping["tasks"]:
                skipped += 1
                continue
            status = status_for(t["_bucket"])
            due = clean_date(t.get("due_date"))
            print(f"  [{status:11s}] {t['title'][:66]}")
            if args.apply:
                body = {
                    "title": t["title"],
                    "description": build_description(t),
                    "priority": priority_for(t.get("priority")),
                    "status": status,
                }
                if due:
                    body["dueDate"] = due
                mapping["tasks"][key] = kaneo(args, "POST", f"/api/task/{pid}", body)["id"]
                map_path.write_text(json.dumps(mapping, indent=2), encoding="utf-8")
            created += 1

    verb = "created" if args.apply else "would create"
    print(f"\n{verb}: {created} tasks in {len(mapping['projects']) or '?'} projects; "
          f"already migrated, skipped: {skipped}")
    if not args.apply:
        print("dry run, nothing written. Re-run with --apply.")


# --------------------------------------------------------------------------

def selftest():
    assert slugify("KIDS - Infrastruttura AWS") == "kids-infrastruttura-aws"
    assert slugify("CKA - Certified Kubernetes Administrator") == "cka-certified-kubernetes-administrator"
    assert slugify("   ") == "project"
    assert status_for("Backlog") == "backlog" and status_for("Doing") == "in-progress"
    assert status_for("Testing") == "in-review"
    assert status_for("") == "to-do" and status_for(None) == "to-do"
    assert priority_for(5) == priority_for(4) == "urgent" and priority_for(0) == "no-priority"
    assert priority_for(None) == "no-priority"
    assert clean_date("0001-01-01T00:00:00Z") == ""
    assert clean_date("2027-01-04T11:00:00Z") == "2027-01-04T11:00:00Z"
    assert html_to_md('<p>see <a href="https://x.it">here</a></p>') == "see [here](https://x.it)"
    assert html_to_md("<ul><li>a</li><li>b</li></ul>") == "- a\n- b"
    assert html_to_md("a &amp; b") == "a & b" and html_to_md(None) == ""
    assert build_description({"id": 5, "_bucket": "To-Do"}) == "_Migrated from Vikunja #5 (column: To-Do)_"
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
    i.add_argument("--dir", default="vikunja-export", help="directory written by export")
    i.add_argument("--apply", action="store_true", help="actually create projects and tasks")
    i.add_argument("--rename", action="append", metavar="OLD=NEW",
                   help="rename a project on the way in (repeatable)")
    i.set_defaults(func=cmd_import)

    sub.add_parser("selftest", help="run the built-in assertions").set_defaults(func=lambda a: selftest())

    args = p.parse_args()
    if args.cmd == "export" and not (args.vikunja_url and args.vikunja_token):
        raise SystemExit("VIKUNJA_URL and VIKUNJA_TOKEN are required (env or flags)")
    if args.cmd == "import" and not (args.kaneo_url and args.kaneo_token):
        raise SystemExit("KANEO_URL and KANEO_TOKEN are required (env or flags)")
    args.func(args)


if __name__ == "__main__":
    main()
