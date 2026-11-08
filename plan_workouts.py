#!/usr/bin/env python3
"""
plan_workouts.py - turn a weekly YAML plan into dated Garmin Connect workouts,
keep a history of what was created, and score adherence afterwards.

    python3 plan_workouts.py show      plans/2026-11-08.yaml      # the week as a table
    python3 plan_workouts.py validate  plans/2026-11-08.yaml      # every exercise resolves in Garmin's catalog
    python3 plan_workouts.py build     plans/2026-11-08.yaml --dry-run   # print every payload, create nothing
    python3 plan_workouts.py build     plans/2026-11-08.yaml             # create + schedule each session on its date
    python3 plan_workouts.py build     plans/2026-11-08.yaml --send      # ...and push them to the watch too
    python3 plan_workouts.py adherence plans/2026-11-08.yaml      # planned vs. what the watch recorded
    python3 plan_workouts.py cleanup   plans/2026-11-08.yaml --yes # delete this week's workouts from Connect

HOW A SESSION BECOMES A WORKOUT
  * name        "<date> <Dow> <HH:MM> <routine>"  - dated, so the library never
                fills with identical "Phase 1" copies and the history is searchable.
  * description the plan's `description`, the routine's `change` note (how and
                why this week differs), the rest policy, and the weights used.
  * steps       every block of the library routine becomes a Garmin repeat group.
                order: circuit  -> A, rest, B, rest, C, rest  x sets   (A->B->C->A...)
                order: straight -> (A, rest) x sets, then (B, rest) x sets, ...
  * rest        `rest_between_sets` is the INTENTIONAL rest you commit to on the
                watch; it replaces every rest the library has inside a round. A
                library exercise with no `rest` is a superset (next movement
                immediately), and the rest after the last exercise - between
                rounds - keeps the library value unless `rest_after_round` is set.
                The plan's `focus_note` is the other half - starting each set on
                focus, not fatigue - and is written into the description.
  * weight      library `weight_lbs`, overridden by plan `routines.<name>.weights`,
                overridden by the session's own `weights`. Pounds in, kilograms
                to Garmin (that is what its editor stores).

Garmin only schedules by DATE; the planned time lives in the name, the
description and history/, and `adherence` compares it to the actual start.

Credentials are never in this repo: the saved token at ~/.garminconnect is
used (see garmin_workouts.py). `--dry-run` needs no login at all.
"""

import argparse
import json
import os
import re
import sys
from datetime import date, datetime, timedelta

try:
    import yaml
except ImportError:                                           # pragma: no cover
    sys.exit("pip3 install pyyaml")

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import garmin_workouts as gw                                  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
LIBRARY = os.path.join(HERE, "library", "workouts.yaml")
ALIASES = os.path.join(HERE, "library", "garmin_aliases.yaml")
HISTORY = os.path.join(HERE, "history")
DOW = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
STRENGTH_KEYS = {"strength_training"}
SWIM_KEYS = {"open_water_swimming", "lap_swimming", "swimming"}
DEFAULT_REST = 30


# ------------------------------------------------------------------ helpers
def seconds(v, default=None):
    """'01:30' -> 90, '45' -> 45, 45 -> 45, None -> default."""
    if v is None or v == "":
        return default
    if isinstance(v, (int, float)):
        return int(v)
    s = str(v).strip()
    if ":" in s:
        parts = [int(p) for p in s.split(":")]
        while len(parts) < 3:
            parts.insert(0, 0)
        h, m, sec = parts
        return h * 3600 + m * 60 + sec
    return int(s)


def hhmm(v):
    """'6 AM' | '6:45 AM' | '12:10 PM' | '16:45' | '06:00' -> 'HH:MM'."""
    s = str(v).strip().upper().replace(".", "")
    m = re.match(r"^(\d{1,2})(?::(\d{2}))?\s*(AM|PM)?$", s)
    if not m:
        raise ValueError(f"cannot read time {v!r}; use '6 AM', '12:10 PM' or '16:45'")
    h, mi, ap = int(m.group(1)), int(m.group(2) or 0), m.group(3)
    if ap == "PM" and h != 12:
        h += 12
    if ap == "AM" and h == 12:
        h = 0
    return f"{h:02d}:{mi:02d}"


def load_yaml(path):
    with open(path) as f:
        return yaml.safe_load(f) or {}


def load_library():
    lib = load_yaml(LIBRARY)
    return {w["name"]: w for w in lib.get("workouts") or []}


def load_aliases():
    return {gw.norm(k): v for k, v in (load_yaml(ALIASES).get("aliases") or {}).items()}


def find_routine(library, name):
    """Exact name, else the unique library workout whose name contains it."""
    if name in library:
        return library[name]
    key = gw.norm(name)
    hits = [w for n, w in library.items() if key in gw.norm(n)]
    if len(hits) == 1:
        return hits[0]
    sys.exit(f"routine {name!r}: {'not in' if not hits else 'ambiguous in'} library/workouts.yaml "
             f"({', '.join(w['name'] for w in hits) or 'see `show`'})")


_PREFER_AVOID = {"BANDED_EXERCISES", "SUSPENSION", "SANDBAG", "PLYO"}


def resolve(name, aliases):
    """Display name -> (CATEGORY, EXERCISE_NAME) in Garmin's catalog."""
    key = gw.norm(name)
    if key in aliases:
        cat, ex = aliases[key].split("/", 1)
        return gw.norm(cat), gw.norm(ex)
    cat_map = gw.catalog()
    for cand in (key, key[:-1] if key.endswith("S") else None, key[:-2] if key.endswith("ES") else None):
        if not cand:
            continue
        hits = [c for c, names in cat_map.items() if cand in names]
        if len(hits) > 1:
            pref = [c for c in hits if c == cand] or [c for c in hits if c not in _PREFER_AVOID]
            if len(pref) >= 1:
                hits = pref[:1]
        if len(hits) == 1:
            return hits[0], cand
    toks = [t for t in key.split("_") if len(t) > 3]
    near = sorted({f"{c}/{e}" for c, s in cat_map.items() for e in s if toks and all(t in e for t in toks[:2])})[:8]
    sys.exit(f"exercise {name!r} is not in Garmin's catalog. Add it to library/garmin_aliases.yaml"
             f"{' - near: ' + ', '.join(near) if near else ''}")


# ------------------------------------------------------------- plan reading
def week_dates(plan):
    start = plan.get("week_start")
    if isinstance(start, str):
        start = date.fromisoformat(start)
    if start.weekday() != 6:
        print(f"note: week_start {start} is a {DOW[start.weekday()]}, not a Sunday", file=sys.stderr)
    return start


def sessions(plan):
    """Flatten plan.days[].sessions[] into dicts with date/time/routine/type."""
    start = week_dates(plan)
    out = []
    for i, day in enumerate(plan.get("days") or []):
        d = day.get("date")
        d = date.fromisoformat(d) if isinstance(d, str) else (d or start + timedelta(days=i))
        for j, s in enumerate(day.get("sessions") or []):
            s = dict(s)
            s["date"] = d
            s["dow"] = DOW[d.weekday()]
            s["time"] = hhmm(s["time"]) if s.get("time") else None
            s["type"] = (s.get("type") or ("swim" if "swim" in str(s.get("routine", "")).lower() else "strength")).lower()
            s["cancelled"] = bool(s.get("cancelled"))
            s["slot"] = s.get("slot") or ["morning", "midday", "night"][min(j, 2)]
            out.append(s)
    return out


def routine_cfg(plan, s):
    """Merged overrides: plan.routines[<routine>] under the session's own keys."""
    base = dict((plan.get("routines") or {}).get(s.get("routine"), {}) or {})
    for k in ("reps", "sets", "rest", "rest_after_round", "order", "change", "weights", "stretch", "library"):
        if s.get(k) is not None:
            if k == "weights":
                base["weights"] = {**(base.get("weights") or {}), **(s["weights"] or {})}
            else:
                base[k] = s[k]
    return base


def session_name(plan, s):
    fmt = plan.get("name_format") or "{date} {dow} {time} {routine}"
    return fmt.format(date=s["date"].isoformat(), dow=s["dow"], time=s["time"] or "",
                      routine=s["routine"], slot=s["slot"]).strip()


def build_payload(plan, s, library, aliases):
    cfg = routine_cfg(plan, s)
    routine = find_routine(library, cfg.get("library") or s["routine"])
    rest_default = seconds(cfg.get("rest"), seconds(plan.get("rest_between_sets"), None))
    order = (cfg.get("order") or plan.get("order") or "circuit").lower()
    weights = {gw.norm(k): v for k, v in (cfg.get("weights") or {}).items()}
    used_weights = []
    steps = []

    for block in routine.get("blocks") or []:
        n_sets = int(cfg.get("sets") or block.get("sets") or 3)
        exs = block.get("exercises") or []
        built = []
        for k, e in enumerate(exs):
            cat, ex = resolve(e["name"], aliases)
            reps = cfg.get("reps") or e.get("reps")
            secs = seconds(e.get("duration")) if e.get("duration") else None
            if secs:
                reps = None
            w = weights.get(gw.norm(e["name"]), weights.get(gw.norm(e.get("display_name") or ""), e.get("weight_lbs")))
            if isinstance(w, str) and w.lower() in ("bodyweight", "bw", "none"):
                w = None
            if e.get("weight") == "bodyweight" and gw.norm(e["name"]) not in weights:
                w = None
            w = float(w) if w not in (None, "") else None
            if w:
                used_weights.append(f"{e.get('display_name') or e['name']} {w:g} lb")
            last = k == len(exs) - 1
            # Rest rules: a library exercise with NO `rest` is a superset - the next
            # movement follows immediately and no rest step is written. Where the
            # library has a rest, the plan's intentional rest replaces it inside a
            # round; the rest after the LAST exercise (between rounds) keeps the
            # library value unless `rest_after_round` says otherwise.
            lib_rest = seconds(e.get("rest"), None)
            if lib_rest is None:
                rest = None
            elif last:
                rest = seconds(cfg.get("rest_after_round"), lib_rest)
            else:
                rest = rest_default if rest_default is not None else lib_rest
            if last and cfg.get("rest_after_round") is not None:
                rest = seconds(cfg["rest_after_round"])
            built.append((gw.exercise_step(cat, ex, reps, secs, w, description=e.get("display_name") or e.get("note")), rest))

        if order == "straight":
            for st, rest in built:
                grp = gw.repeat_group(n_sets, [st, gw.rest_step(rest if rest is not None else (rest_default or DEFAULT_REST))])
                grp["skipLastRestStep"] = True
                steps.append(grp)
        else:                                   # circuit: A -> B -> C -> A ...
            inner = []
            for st, rest in built:
                inner.append(st)
                if rest is not None:
                    inner.append(gw.rest_step(rest))
            grp = gw.repeat_group(n_sets, inner)
            grp["skipLastRestStep"] = bool(block.get("skip_last_rest", True))
            steps.append(grp)

    stretch = cfg.get("stretch") or plan.get("stretch")
    if stretch:
        cat, ex = resolve("Stretch", aliases)
        steps.append(gw.exercise_step(cat, ex, None, seconds(stretch), None, description="Stretch"))

    desc = []
    if plan.get("description"):
        desc.append(plan["description"].strip())
    if cfg.get("change"):
        desc.append("Change: " + cfg["change"].strip())
    rest_txt = f"{rest_default}s" if rest_default is not None else "library rests"
    desc.append(f"Rest {rest_txt} between sets ({order}). Planned start {s['time'] or '-'}.")
    if plan.get("focus_note"):
        desc.append(plan["focus_note"].strip())
    if used_weights:
        desc.append("Weights: " + "; ".join(dict.fromkeys(used_weights)))
    description = "\n".join(desc)[:1024]

    payload = gw.new_workout(session_name(plan, s), steps, description)
    return gw.renumber(payload), routine["name"]


def history_path(plan_path):
    return os.path.join(HISTORY, os.path.splitext(os.path.basename(plan_path))[0] + ".json")


def read_history(plan_path):
    p = history_path(plan_path)
    return json.load(open(p)) if os.path.exists(p) else None


# ---------------------------------------------------------------- commands
def cmd_show(a):
    plan = load_yaml(a.plan)
    print(f"Plan {plan.get('week_start')}  rest={plan.get('rest_between_sets')}  order={plan.get('order', 'circuit')}")
    if plan.get("description"):
        print("  " + plan["description"].strip().replace("\n", "\n  "))
    print(f"\n{'date':10} {'dow':3} {'time':5} {'slot':7} {'type':8} routine")
    for s in sessions(plan):
        flag = "  (cancelled)" if s["cancelled"] else ""
        print(f"{s['date']} {s['dow']} {s['time'] or '--:--':5} {s['slot']:7} {s['type']:8} {s['routine']}{flag}")


def cmd_validate(a):
    plan = load_yaml(a.plan)
    library, aliases = load_library(), load_aliases()
    bad = 0
    seen = set()
    for s in sessions(plan):
        if s["type"] != "strength" or s["cancelled"]:
            continue
        cfg = routine_cfg(plan, s)
        routine = find_routine(library, cfg.get("library") or s["routine"])
        for b in routine.get("blocks") or []:
            for e in b.get("exercises") or []:
                if e["name"] in seen:
                    continue
                seen.add(e["name"])
                try:
                    cat, ex = resolve(e["name"], aliases)
                    print(f"ok   {e['name']:45} -> {cat}/{ex}")
                except SystemExit as err:
                    bad += 1
                    print(f"FAIL {e['name']:45} {err}")
    print(f"\n{len(seen)} exercise(s), {bad} unresolved.")
    sys.exit(1 if bad else 0)


def cmd_build(a):
    plan = load_yaml(a.plan)
    library, aliases = load_library(), load_aliases()
    sess = sessions(plan)
    g = None if a.dry_run else gw.client()
    hist = read_history(a.plan) or {"plan": os.path.relpath(a.plan, HERE), "week_start": str(week_dates(plan)),
                                     "sessions": []}
    done = {(h["date"], h["time"], h["routine"]) for h in hist["sessions"] if h.get("workout_id")}
    created = 0
    for s in sess:
        key = (s["date"].isoformat(), s["time"], s["routine"])
        rec = {"date": key[0], "dow": s["dow"], "time": s["time"], "slot": s["slot"], "type": s["type"],
               "routine": s["routine"], "cancelled": s["cancelled"]}
        if s["cancelled"] or s["type"] != "strength":
            print(f"-- {key[0]} {s['time'] or '--:--'} {s['routine']}: {'cancelled' if s['cancelled'] else s['type']} - no Garmin workout")
            if not a.dry_run and key not in done:
                hist["sessions"].append(rec)
            continue
        if key in done and not a.force:
            print(f"-- {key[0]} {s['time']} {s['routine']}: already built (history); use --force to rebuild")
            continue
        payload, lib_name = build_payload(plan, s, library, aliases)
        print(f"\n## {payload['workoutName']}   (library: {lib_name})")
        gw.print_workout(payload)
        if a.dry_run:
            if a.json:
                print(json.dumps(payload, indent=1))
            continue
        r = gw.api(g, "POST", f"{gw.WS}/workout", json=payload)
        wid = r.get("workoutId") if isinstance(r, dict) else None
        if not wid:
            print("  !! create failed:", json.dumps(r)[:500])
            continue
        rec.update(workout_name=payload["workoutName"], workout_id=wid, library=lib_name,
                   created_at=datetime.now().isoformat(timespec="seconds"))
        print(f"  created -> workoutId {wid}")
        if plan.get("schedule", True) and not a.no_schedule:
            try:
                sr = gw.api(g, "POST", f"{gw.WS}/schedule/{wid}", json={"date": key[0]})
                rec["schedule_id"] = sr.get("workoutScheduleId") if isinstance(sr, dict) else None
                print(f"  scheduled on {key[0]} (scheduleId {rec['schedule_id']})")
            except Exception as e:                            # pragma: no cover
                print(f"  !! schedule failed: {e}")
        if a.send:
            gw.push(g, wid, a.device)
            rec["sent"] = True
        hist["sessions"] = [h for h in hist["sessions"]
                            if (h["date"], h.get("time"), h["routine"]) != key] + [rec]
        created += 1
    if a.dry_run:
        print(f"\n--dry-run: {sum(1 for s in sess if s['type'] == 'strength' and not s['cancelled'])} workout(s) would be created; nothing sent to Garmin.")
        return
    hist["sessions"].sort(key=lambda h: (h["date"], h.get("time") or ""))
    hist["built_at"] = datetime.now().isoformat(timespec="seconds")
    os.makedirs(HISTORY, exist_ok=True)
    json.dump(hist, open(history_path(a.plan), "w"), indent=1)
    print(f"\n{created} workout(s) created. History -> {os.path.relpath(history_path(a.plan), HERE)}")


def _activities(g, start, end):
    """All activities between two dates (inclusive), newest API first -> sorted by start."""
    acts = g.get_activities_by_date(start.isoformat(), end.isoformat())
    out = []
    for x in acts or []:
        st = x.get("startTimeLocal")
        if not st:
            continue
        out.append({"id": x.get("activityId"), "name": x.get("activityName"),
                    "type": (x.get("activityType") or {}).get("typeKey"),
                    "start": datetime.fromisoformat(st.replace(".0", "")[:19]),
                    "duration_min": round((x.get("duration") or 0) / 60, 1),
                    "workout_id": x.get("workoutId")})
    return sorted(out, key=lambda r: r["start"])


def cmd_adherence(a):
    plan = load_yaml(a.plan)
    hist = read_history(a.plan)
    sess = sessions(plan)
    byhist = {(h["date"], h.get("time"), h["routine"]): h for h in (hist or {}).get("sessions", [])}
    g = gw.client()
    start = min(s["date"] for s in sess)
    end = max(s["date"] for s in sess)
    acts = _activities(g, start, end)
    tol = a.tolerance
    used = set()
    rows = []
    for s in sess:
        key = (s["date"].isoformat(), s["time"], s["routine"])
        h = byhist.get(key, {})
        want = STRENGTH_KEYS if s["type"] == "strength" else SWIM_KEYS if s["type"] == "swim" else None
        planned = datetime.combine(s["date"], datetime.strptime(s["time"], "%H:%M").time()) if s["time"] else None
        cands = [x for x in acts if x["start"].date() == s["date"] and x["id"] not in used
                 and (want is None or x["type"] in want)]
        pick = None
        if h.get("workout_id"):
            pick = next((x for x in cands if x.get("workout_id") == h["workout_id"]), None)
        if pick is None and cands:
            pick = min(cands, key=lambda x: abs((x["start"] - planned).total_seconds()) if planned else 0)
        if s["cancelled"]:
            status = "cancelled" + (" (but done)" if pick else "")
        elif pick is None:
            status = "missed"
        else:
            delta = round((pick["start"] - planned).total_seconds() / 60) if planned else None
            status = ("on time" if delta is not None and abs(delta) <= tol else
                      f"{'late' if delta > 0 else 'early'} {abs(delta)} min" if delta is not None else "done")
        if pick:
            used.add(pick["id"])
        rows.append({"date": key[0], "dow": s["dow"], "planned": s["time"], "routine": s["routine"],
                     "type": s["type"], "status": status,
                     "actual": pick["start"].strftime("%H:%M") if pick else None,
                     "activity_id": pick["id"] if pick else None,
                     "activity_name": pick["name"] if pick else None,
                     "duration_min": pick["duration_min"] if pick else None,
                     "workout_id": h.get("workout_id")})
    planned_n = sum(1 for r in rows if not r["status"].startswith("cancelled"))
    done_n = sum(1 for r in rows if r["actual"] and not r["status"].startswith("cancelled"))
    ontime_n = sum(1 for r in rows if r["status"] == "on time")
    print(f"{'date':10} {'dow':3} {'plan':5} {'actual':6} {'status':14} routine")
    for r in rows:
        print(f"{r['date']} {r['dow']} {r['planned'] or '--:--':5} {r['actual'] or '--:--':6} {r['status']:14} {r['routine']}")
    extra = [x for x in acts if x["id"] not in used and (x["type"] in STRENGTH_KEYS | SWIM_KEYS)]
    if extra:
        print("\nunplanned sessions the watch recorded:")
        for x in extra:
            print(f"  {x['start']:%Y-%m-%d %H:%M}  {x['type']:20} {x['duration_min']:5} min  {x['name']}")
    print(f"\ncompleted {done_n}/{planned_n} planned sessions; {ontime_n} started within {tol} min of plan.")
    out = {"plan": os.path.relpath(a.plan, HERE), "scored_at": datetime.now().isoformat(timespec="seconds"),
           "tolerance_min": tol, "completed": done_n, "planned": planned_n, "on_time": ontime_n,
           "sessions": rows,
           "unplanned": [{**x, "start": x["start"].isoformat()} for x in extra]}
    os.makedirs(HISTORY, exist_ok=True)
    p = history_path(a.plan).replace(".json", "_adherence.json")
    json.dump(out, open(p, "w"), indent=1)
    print(f"written -> {os.path.relpath(p, HERE)}")


def cmd_cleanup(a):
    hist = read_history(a.plan)
    if not hist:
        sys.exit("no history for this plan; nothing to delete")
    ids = [h["workout_id"] for h in hist["sessions"] if h.get("workout_id")]
    if not a.yes:
        sys.exit(f"would delete {len(ids)} workout(s) from the Connect library: {ids}  (add --yes)")
    g = gw.client()
    for h in hist["sessions"]:
        if h.get("schedule_id"):
            try:
                gw.api(g, "DELETE", f"{gw.WS}/schedule/{h['schedule_id']}")
            except Exception:
                pass
        if h.get("workout_id"):
            gw.api(g, "DELETE", f"{gw.WS}/workout/{h['workout_id']}")
            print(f"deleted [{h['workout_id']}] {h.get('workout_name')}")
            h["deleted_at"] = datetime.now().isoformat(timespec="seconds")
    json.dump(hist, open(history_path(a.plan), "w"), indent=1)
    print("history kept; workout ids marked deleted. Anything pushed to the watch stays until removed on the watch.")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sp = ap.add_subparsers(dest="cmd", required=True)
    for name, fn, helptxt in [("show", cmd_show, "print the plan as a table"),
                              ("validate", cmd_validate, "check every exercise against Garmin's catalog"),
                              ("adherence", cmd_adherence, "planned vs recorded sessions for the week"),
                              ("cleanup", cmd_cleanup, "delete the week's workouts from Connect")]:
        p = sp.add_parser(name, help=helptxt)
        p.add_argument("plan")
        if name == "adherence":
            p.add_argument("--tolerance", type=int, default=15, help="minutes counted as 'on time'")
        if name == "cleanup":
            p.add_argument("--yes", action="store_true")
        p.set_defaults(f=fn)
    p = sp.add_parser("build", help="create (and schedule) one Garmin workout per strength session")
    p.add_argument("plan")
    p.add_argument("--dry-run", action="store_true", help="print payloads, create nothing, no login")
    p.add_argument("--json", action="store_true", help="with --dry-run, also print the raw JSON payloads")
    p.add_argument("--send", action="store_true", help="push each created workout to the watch")
    p.add_argument("--device", help="deviceId for --send (default: last used watch)")
    p.add_argument("--no-schedule", action="store_true", help="create in the library only")
    p.add_argument("--force", action="store_true", help="rebuild sessions already in history")
    p.set_defaults(f=cmd_build)
    a = ap.parse_args()
    a.f(a)


if __name__ == "__main__":
    main()
