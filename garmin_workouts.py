#!/usr/bin/env python3
"""
Garmin Connect workout manager: list, inspect, clone/modify, create, send to the
watch, schedule, and delete strength workouts from the terminal.

    python3 garmin/garmin_workouts.py list
    python3 garmin/garmin_workouts.py show 1198044790
    python3 garmin/garmin_workouts.py dump 1198044790 -o chest_back.json

    # exact duplicate under a new name (Garmin's own copy endpoint)
    python3 garmin/garmin_workouts.py copy 1198044790 --name "Chest & Back - Phase 2"

    # clone an existing workout and change it on the way (dry-run prints the payload)
    python3 garmin/garmin_workouts.py clone 1198044790 --name "Chest & Back - 3x12 - 65lb bench" \\
        --rounds 3 --reps 12 --rest 60 \\
        --replace DUMBBELL_BENCH_PRESS=BENCH_PRESS/BARBELL_BENCH_PRESS \\
        --weight BARBELL_BENCH_PRESS=65 --weight DUMBBELL_FLYE=12.5 --dry-run
    python3 garmin/garmin_workouts.py clone 1198044790 --name "..." --rounds 3 --send

    # build one from scratch: CATEGORY/EXERCISE_NAME:REPS[@LB]  (or :60s for time)
    python3 garmin/garmin_workouts.py new --name "Legs - squats + calf raises" --rounds 3 --rest 60 \\
        --exercise SQUAT/DUMBBELL_SQUAT:10@25 --exercise CALF_RAISE/STANDING_CALF_RAISE:10@25

    # watch visibility
    python3 garmin/garmin_workouts.py devices
    python3 garmin/garmin_workouts.py send 1198044790             # push to the Forerunner
    python3 garmin/garmin_workouts.py schedule 1198044790 2026-10-30
    python3 garmin/garmin_workouts.py scheduled --month 2026-10
    python3 garmin/garmin_workouts.py unschedule <scheduleId>
    python3 garmin/garmin_workouts.py delete 1234567890 --yes      # removes from the Connect library

    # look up Garmin's exercise names (needed for --replace / --exercise)
    python3 garmin/garmin_workouts.py exercises calf
    python3 garmin/garmin_workouts.py exercises "bench press"

WHAT THE WATCH SHOWS (Forerunner 265). A workout in the Garmin Connect library is
NOT on the watch by itself. It reaches the watch in one of two ways:
  * `send`      -> it lands in the watch's Training > Workouts list and STAYS there
                   until deleted ON THE WATCH (deleting it in Connect does not remove
                   it; Garmin has no API for removing a pushed workout).
  * `schedule`  -> it shows up on the watch's calendar / "today's workout" for that
                   date only; `unschedule` takes it off the calendar again.
So the cleanest way to keep the watch list short: push only the workouts you
actually use, delete stale ones on the watch (hold UP > Training > Workouts >
workout > Delete), and schedule the rest day by day instead of pushing them.

CREDENTIALS: never in this file. Uses the saved token at ~/.garminconnect, falling
back to GARMIN_EMAIL / GARMIN_PASSWORD for a one-time login (same as
garmin_import.py). Weights are typed in POUNDS and sent to Garmin in kilograms,
which is what its workout editor stores; the watch displays them in your unit
setting. Every write goes through an undocumented Garmin endpoint, so this can
break without notice; `--dry-run` always shows exactly what would be sent.
"""

import argparse
import copy
import json
import os
import re
import sys
import urllib.request
from datetime import date, datetime

try:
    from garminconnect import Garmin
except ImportError:                                           # pragma: no cover
    sys.exit("pip3 install garminconnect")

TOKENSTORE = os.path.expanduser("~/.garminconnect")
WS = "/workout-service"
DEVICE_MSG = "/device-service/devicemessage/messages"
DEVICES = "/device-service/deviceregistration/devices"
LAST_USED = "/device-service/deviceservice/mylastused"
CATALOG_URL = "https://connect.garmin.com/web-data/exercises/Exercises.json"
KG_PER_LB = 0.45359237

STRENGTH = {"sportTypeId": 5, "sportTypeKey": "strength_training"}
NO_TARGET = {"workoutTargetTypeId": 1, "workoutTargetTypeKey": "no.target"}
KG_UNIT = {"unitId": 8, "unitKey": "kilogram", "factor": 1000.0}
END_REPS = {"conditionTypeId": 10, "conditionTypeKey": "reps"}
END_TIME = {"conditionTypeId": 2, "conditionTypeKey": "time"}
END_ITER = {"conditionTypeId": 7, "conditionTypeKey": "iterations"}

# Server-owned fields that must not be echoed back when creating a workout.
STRIP_KEYS = {"stepId", "workoutId", "ownerId", "createdDate", "updatedDate", "uploadTimestamp",
              "author", "workoutSourceId", "atpPlanId", "trainingPlanId", "consumer",
              "consumerName", "consumerImageURL", "consumerWebsiteURL", "shared",
              "sharedWithUsers", "workoutThumbnailUrl", "workoutNameI18nKey",
              "descriptionI18nKey", "workoutProvider", "providerExerciseSourceId"}


# ------------------------------------------------------------------ client
def client():
    try:
        g = Garmin()
        g.login(TOKENSTORE)
        return g
    except Exception as e:
        print(f"No saved token ({e}); trying a fresh login...", file=sys.stderr)
    email, pw = os.environ.get("GARMIN_EMAIL"), os.environ.get("GARMIN_PASSWORD")
    if not (email and pw):
        sys.exit("No token at ~/.garminconnect and GARMIN_EMAIL / GARMIN_PASSWORD not set.")
    g = Garmin(email=email, password=pw)
    g.login()
    try:
        g.garth.dump(TOKENSTORE)
    except Exception:
        pass
    return g


def api(g, method, path, **kw):
    """garth handles auth + the connectapi subdomain; this just picks the verb."""
    return g.garth.connectapi(path, method=method, **kw)


# ------------------------------------------------------------- step helpers
def is_repeat(st):
    return st.get("type") == "RepeatGroupDTO"


def is_exercise(st):
    return (not is_repeat(st)) and (st.get("stepType") or {}).get("stepTypeKey") == "interval"


def is_rest(st):
    return (not is_repeat(st)) and (st.get("stepType") or {}).get("stepTypeKey") == "rest"


def walk(steps, depth=0):
    for st in steps:
        yield depth, st
        if is_repeat(st):
            yield from walk(st.get("workoutSteps") or [], depth + 1)


def norm(s):
    return re.sub(r"[^A-Z0-9]+", "_", str(s or "").upper()).strip("_")


def step_label(st):
    return st.get("exerciseName") or st.get("category") or "?"


def matches(st, token):
    """token matches a step by exerciseName, category or description (case/space
    insensitive); 'all' matches every exercise step."""
    if not is_exercise(st):
        return False
    t = norm(token)
    if t == "ALL":
        return True
    cands = {norm(st.get("exerciseName")), norm(st.get("category")), norm(st.get("description"))}
    cands.discard("")
    return t in cands or any(t and t in c for c in cands if c)


def fmt_step(st):
    if is_repeat(st):
        return f"REPEAT x{st.get('numberOfIterations')}"
    kind = (st.get("stepType") or {}).get("stepTypeKey")
    end = (st.get("endCondition") or {}).get("conditionTypeKey")
    val = st.get("endConditionValue")
    val = int(val) if isinstance(val, float) and val.is_integer() else val
    s = f"{kind:8s} "
    if kind == "interval":
        s += f"{st.get('category') or '-'}/{st.get('exerciseName') or '-'}"
        if st.get("description"):
            s += f"  ({st['description']})"
    s += f"  {end}={val}" + ("s" if end == "time" else "")
    if st.get("weightValue") is not None:
        unit = (st.get("weightUnit") or {}).get("unitKey") or "kilogram"
        kg = float(st["weightValue"])
        lb = kg / KG_PER_LB if unit == "kilogram" else kg
        s += f"  @ {lb:.1f} lb"
    return s


def print_workout(w):
    print(f"[{w.get('workoutId')}] {w.get('workoutName')}   sport={w.get('sportType', {}).get('sportTypeKey')}"
          f"   updated={str(w.get('updatedDate') or '')[:10]}")
    if w.get("description"):
        print(f"  {w['description']}")
    for seg in w.get("workoutSegments") or []:
        for depth, st in walk(seg.get("workoutSteps") or []):
            print("  " * (depth + 1) + fmt_step(st))


def exercise_step(category, name=None, reps=None, seconds=None, weight_lb=None, description=None):
    if not (reps or seconds):
        raise ValueError("an exercise needs reps or seconds")
    return {
        "type": "ExecutableStepDTO",
        "stepType": {"stepTypeId": 3, "stepTypeKey": "interval"},
        "endCondition": dict(END_REPS if reps else END_TIME),
        "endConditionValue": float(reps or seconds),
        "targetType": dict(NO_TARGET),
        "category": category,
        "exerciseName": name or None,
        "description": description,
        "weightValue": round(weight_lb * KG_PER_LB, 3) if weight_lb else None,
        "weightUnit": dict(KG_UNIT) if weight_lb else None,
    }


def rest_step(seconds):
    return {
        "type": "ExecutableStepDTO",
        "stepType": {"stepTypeId": 5, "stepTypeKey": "rest"},
        "endCondition": dict(END_TIME),
        "endConditionValue": float(seconds),
        "targetType": dict(NO_TARGET),
    }


def repeat_group(n, steps):
    return {
        "type": "RepeatGroupDTO",
        "stepType": {"stepTypeId": 6, "stepTypeKey": "repeat"},
        "numberOfIterations": int(n),
        "endConditionValue": float(n),
        "endCondition": dict(END_ITER),
        "skipLastRestStep": False,
        "smartRepeat": False,
        "workoutSteps": steps,
    }


def new_workout(name, steps, description=None):
    return {
        "sportType": dict(STRENGTH),
        "subSportType": None,
        "workoutName": name,
        "description": description,
        "estimatedDurationInSecs": 0,
        "workoutSegments": [{"segmentOrder": 1, "sportType": dict(STRENGTH), "workoutSteps": steps}],
    }


def strip_ids(obj):
    if isinstance(obj, dict):
        return {k: strip_ids(v) for k, v in obj.items() if k not in STRIP_KEYS}
    if isinstance(obj, list):
        return [strip_ids(v) for v in obj]
    return obj


def renumber(payload):
    """stepOrder is sequential through the whole segment (repeat group first,
    then its children); childStepId ties children to their repeat group."""
    for seg in payload["workoutSegments"]:
        order = 0
        group = 0

        def visit(steps, child):
            nonlocal order, group
            for st in steps:
                order += 1
                st["stepOrder"] = order
                if is_repeat(st):
                    group += 1
                    st["childStepId"] = group
                    visit(st.get("workoutSteps") or [], group)
                else:
                    st["childStepId"] = child
        visit(seg["workoutSteps"], None)
    return payload


def set_weight(st, lb):
    st["weightValue"] = round(float(lb) * KG_PER_LB, 3) if lb else None
    st["weightUnit"] = dict(KG_UNIT) if lb else None


# ----------------------------------------------------------- exercise names
_catalog = None


def catalog():
    """Garmin's public exercise catalog: {CATEGORY: {EXERCISE_NAME, ...}}."""
    global _catalog
    if _catalog is None:
        # Garmin's CDN answers 403 to the default urllib agent; any browser UA works.
        req = urllib.request.Request(CATALOG_URL, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=30) as r:
            data = json.load(r)
        _catalog = {cat: set((v.get("exercises") or {}).keys())
                    for cat, v in (data.get("categories") or {}).items()}
    return _catalog


def parse_exercise_token(tok):
    """'CATEGORY/NAME:REPS[@LB]' | 'CATEGORY/NAME:60s' | 'NAME:10' (category looked up)."""
    m = re.match(r"^([A-Za-z0-9_ /-]+?)(?::(\d+)(s?))?(?:@([\d.]+))?$", tok.strip())
    if not m:
        sys.exit(f"Cannot parse exercise spec {tok!r}; expected CATEGORY/NAME:REPS[@LB]")
    ex, n, secs, lb = m.groups()
    cat, name = (ex.split("/", 1) + [None])[:2] if "/" in ex else (None, ex)
    cat = norm(cat) if cat else None
    name = norm(name) if name else None
    if cat is None and name:
        hits = [c for c, names in catalog().items() if name in names]
        if len(hits) > 1:
            # PUSH_UP lives in both PUSH_UP and SUSPENSION: prefer the category
            # that is the exercise's own name, then one the name mentions.
            pref = [c for c in hits if c == name] or [c for c in hits if c in name and c != "BANDED_EXERCISES"]
            if len(pref) == 1:
                hits = pref
        if len(hits) != 1:
            sys.exit(f"{name}: {'not in' if not hits else 'ambiguous in'} Garmin's catalog "
                     f"({', '.join(hits) or 'try: exercises ' + name.lower()}); give CATEGORY/NAME")
        cat = hits[0]
    return cat, name, (int(n) if n and not secs else None), (int(n) if secs else None), (float(lb) if lb else None)


# ---------------------------------------------------------------- commands
def cmd_list(g, a):
    rows = api(g, "GET", f"{WS}/workouts", params={"start": 0, "limit": a.limit, "myWorkoutsOnly": "true"})
    print(f"{'workoutId':>12}  {'sport':18s} {'updated':10s}  name")
    for w in rows:
        print(f"{w['workoutId']:>12}  {w.get('sportType', {}).get('sportTypeKey', ''):18s} "
              f"{str(w.get('updatedDate') or w.get('createdDate') or '')[:10]:10s}  {w.get('workoutName')}")


def cmd_show(g, a):
    print_workout(api(g, "GET", f"{WS}/workout/{a.workout_id}"))


def cmd_dump(g, a):
    w = api(g, "GET", f"{WS}/workout/{a.workout_id}")
    txt = json.dumps(w, indent=1)
    if a.out:
        open(a.out, "w").write(txt)
        print(f"wrote {a.out}")
    else:
        print(txt)


def cmd_copy(g, a):
    r = api(g, "POST", f"{WS}/workout/copy/{a.workout_id}", data=a.name.encode("utf-8"),
            headers={"Content-Type": "text/plain"})
    print(f"copied -> workoutId {r.get('workoutId') if isinstance(r, dict) else r}  {a.name!r}")
    _after_create(g, a, r)


def apply_mods(payload, a):
    """Shared by clone: rounds / reps / rest / replace / weight / reps-for / drop / append."""
    steps_all = [st for seg in payload["workoutSegments"] for _, st in walk(seg["workoutSteps"])]
    if a.rounds:
        for st in steps_all:
            if is_repeat(st):
                st["numberOfIterations"] = int(a.rounds)
                st["endConditionValue"] = float(a.rounds)
    if a.reps:
        for st in steps_all:
            if is_exercise(st) and (st.get("endCondition") or {}).get("conditionTypeKey") == "reps":
                st["endConditionValue"] = float(a.reps)
    if a.rest is not None:
        for st in steps_all:
            if is_rest(st):
                st["endConditionValue"] = float(a.rest)
    for spec in a.replace or []:
        old, new = spec.split("=", 1)
        cat, name, *_ = parse_exercise_token(new)
        hit = 0
        for st in steps_all:
            if matches(st, old):
                st["category"], st["exerciseName"] = cat, name
                hit += 1
        if not hit:
            sys.exit(f"--replace {spec}: no step matches {old!r}")
    for spec in a.reps_for or []:
        ex, n = spec.split("=", 1)
        hit = 0
        for st in steps_all:
            if matches(st, ex):
                st["endCondition"], st["endConditionValue"] = dict(END_REPS), float(n)
                hit += 1
        if not hit:
            sys.exit(f"--reps-for {spec}: no step matches {ex!r}")
    for spec in a.weight or []:
        ex, lb = spec.split("=", 1)
        hit = 0
        for st in steps_all:
            if matches(st, ex):
                set_weight(st, float(lb))
                hit += 1
        if not hit:
            sys.exit(f"--weight {spec}: no step matches {ex!r}")
    for ex in a.drop or []:
        hit = 0
        for seg in payload["workoutSegments"]:
            for container in [seg] + [st for _, st in walk(seg["workoutSteps"]) if is_repeat(st)]:
                steps = container.get("workoutSteps") or []
                out, skip_rest = [], False
                for st in steps:
                    if skip_rest and is_rest(st):
                        skip_rest = False
                        continue
                    skip_rest = False
                    if matches(st, ex):
                        hit += 1
                        skip_rest = True          # also drop the rest that follows it
                        continue
                    out.append(st)
                container["workoutSteps"] = out
        if not hit:
            sys.exit(f"--drop {ex}: no step matches")
    for tok in a.append or []:
        cat, name, reps, secs, lb = parse_exercise_token(tok)
        seg = payload["workoutSegments"][-1]
        groups = [st for _, st in walk(seg["workoutSteps"]) if is_repeat(st)]
        target = groups[-1]["workoutSteps"] if groups else seg["workoutSteps"]
        rests = [st["endConditionValue"] for st in target if is_rest(st)]
        target.append(exercise_step(cat, name, reps, secs, lb))
        target.append(rest_step(a.rest if a.rest is not None else (rests[-1] if rests else 45)))
    if a.description is not None:
        payload["description"] = a.description
    return payload


def cmd_clone(g, a):
    src = api(g, "GET", f"{WS}/workout/{a.workout_id}")
    payload = strip_ids(copy.deepcopy(src))
    payload["workoutName"] = a.name
    payload = renumber(apply_mods(payload, a))
    _create(g, a, payload, f"cloned from {src.get('workoutName')!r} [{a.workout_id}]")


def cmd_new(g, a):
    steps = []
    for tok in a.exercise:
        cat, name, reps, secs, lb = parse_exercise_token(tok)
        steps.append(exercise_step(cat, name, reps, secs, lb))
        steps.append(rest_step(a.rest))
    if a.last_rest is not None:
        steps[-1] = rest_step(a.last_rest)
    payload = renumber(new_workout(a.name, [repeat_group(a.rounds, steps)], a.description))
    _create(g, a, payload, "built from --exercise specs")


def _create(g, a, payload, how):
    print(f"# {how}")
    print_workout(payload)
    if a.dry_run:
        print("\n--dry-run: payload that WOULD be POSTed to /workout-service/workout:")
        print(json.dumps(payload, indent=1))
        return
    r = api(g, "POST", f"{WS}/workout", json=payload)
    wid = r.get("workoutId") if isinstance(r, dict) else None
    print(f"\ncreated -> workoutId {wid}")
    if wid is None:
        print(json.dumps(r, indent=1)[:2000])
        return
    _after_create(g, a, r)


def _after_create(g, a, created):
    wid = created.get("workoutId") if isinstance(created, dict) else None
    if not wid:
        return
    if getattr(a, "send", False):
        push(g, wid, getattr(a, "device", None))
    if getattr(a, "schedule", None):
        schedule(g, wid, a.schedule)


def pick_device(g, device_id=None):
    if device_id:
        return int(device_id), str(device_id)
    try:
        lu = api(g, "GET", LAST_USED) or {}
        if lu.get("userDeviceId"):
            return int(lu["userDeviceId"]), lu.get("lastUsedDeviceName") or "last used device"
    except Exception:
        pass
    devs = api(g, "GET", DEVICES) or []
    watches = [d for d in devs if "scale" not in (d.get("productDisplayName") or "").lower()]
    if len(watches) != 1:
        sys.exit("Could not pick a device automatically; pass --device <deviceId> (see `devices`).")
    return int(watches[0]["deviceId"]), watches[0].get("productDisplayName")


def push(g, workout_id, device_id=None):
    did, dname = pick_device(g, device_id)
    name = api(g, "GET", f"{WS}/workout/{workout_id}").get("workoutName")
    payload = [{
        "deviceId": did,
        "messageUrl": f"workout-service/workout/FIT/{workout_id}",
        "messageType": "workouts",
        "groupName": None,
        "messageName": name,
        "priority": 1,
        "fileType": "FIT",
        "metaDataId": int(workout_id),
    }]
    r = api(g, "POST", DEVICE_MSG, json=payload)
    print(f"queued {name!r} for {dname} (deviceId {did}); it arrives on the next watch sync.")
    if r:
        print(json.dumps(r, indent=1)[:600])


def schedule(g, workout_id, day):
    date.fromisoformat(day)
    r = api(g, "POST", f"{WS}/schedule/{workout_id}", json={"date": day})
    sid = r.get("workoutScheduleId") if isinstance(r, dict) else None
    print(f"scheduled workout {workout_id} on {day}  (scheduleId {sid})")


def cmd_send(g, a):
    push(g, a.workout_id, a.device)


def cmd_schedule(g, a):
    schedule(g, a.workout_id, a.date)


def cmd_unschedule(g, a):
    api(g, "DELETE", f"{WS}/schedule/{a.schedule_id}")
    print(f"unscheduled {a.schedule_id}")


def cmd_scheduled(g, a):
    y, m = (a.month or date.today().strftime("%Y-%m")).split("-")
    cal = api(g, "GET", f"/calendar-service/year/{int(y)}/month/{int(m) - 1}") or {}
    items = [it for it in cal.get("calendarItems") or [] if it.get("itemType") == "workout"]
    if not items:
        print(f"no scheduled workouts in {y}-{m}")
    for it in sorted(items, key=lambda i: i.get("date") or ""):
        print(f"{it.get('date')}  scheduleId={it.get('id')}  workoutId={it.get('workoutId')}  "
              f"{it.get('title')}  {'(done)' if it.get('completionTarget') or it.get('workoutUuid') and it.get('activityId') else ''}")


def cmd_delete(g, a):
    w = api(g, "GET", f"{WS}/workout/{a.workout_id}")
    if not a.yes:
        sys.exit(f"Refusing to delete [{a.workout_id}] {w.get('workoutName')!r} without --yes")
    api(g, "DELETE", f"{WS}/workout/{a.workout_id}")
    print(f"deleted [{a.workout_id}] {w.get('workoutName')!r} from the Connect library "
          f"(a copy already pushed to the watch stays there until deleted on the watch).")


def cmd_devices(g, a):
    for d in api(g, "GET", DEVICES) or []:
        print(f"{d.get('deviceId'):>12}  {d.get('productDisplayName')}  "
              f"{'(primary)' if d.get('primary') else ''}")
    try:
        lu = api(g, "GET", LAST_USED) or {}
        print(f"last used: {lu.get('lastUsedDeviceName')} ({lu.get('userDeviceId')})")
    except Exception:
        pass


def cmd_queue(g, a):
    print(json.dumps(api(g, "GET", DEVICE_MSG), indent=1)[:4000])


def cmd_exercises(g, a):
    q = norm(a.query)
    n = 0
    for cat, names in sorted(catalog().items()):
        for name in sorted(names):
            if q in name or q in cat:
                print(f"{cat}/{name}")
                n += 1
    if not n:
        print(f"nothing in Garmin's catalog matches {a.query!r}")


# -------------------------------------------------------------------- main
def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sp = ap.add_subparsers(dest="cmd", required=True)

    p = sp.add_parser("list", help="workouts in your Connect library");  p.add_argument("--limit", type=int, default=100); p.set_defaults(f=cmd_list)
    p = sp.add_parser("show", help="print a workout's steps");            p.add_argument("workout_id"); p.set_defaults(f=cmd_show)
    p = sp.add_parser("dump", help="raw workout JSON");                   p.add_argument("workout_id"); p.add_argument("-o", "--out"); p.set_defaults(f=cmd_dump)

    def create_opts(p):
        p.add_argument("--name", required=True)
        p.add_argument("--description")
        p.add_argument("--dry-run", action="store_true", help="print the payload, create nothing")
        p.add_argument("--send", action="store_true", help="push to the watch after creating")
        p.add_argument("--schedule", metavar="YYYY-MM-DD", help="also put it on the calendar")
        p.add_argument("--device", help="deviceId for --send (default: last used watch)")

    p = sp.add_parser("copy", help="exact duplicate via Garmin's copy endpoint"); p.add_argument("workout_id"); create_opts(p); p.set_defaults(f=cmd_copy)

    p = sp.add_parser("clone", help="duplicate an existing workout with changes")
    p.add_argument("workout_id"); create_opts(p)
    p.add_argument("--rounds", type=int, help="iterations for every repeat group")
    p.add_argument("--reps", type=int, help="reps for every rep-based exercise step")
    p.add_argument("--rest", type=int, help="seconds for every rest step")
    p.add_argument("--replace", action="append", metavar="OLD=CATEGORY/NEW", help="swap an exercise")
    p.add_argument("--weight", action="append", metavar="EXERCISE=LB", help="set weight (lb); EXERCISE may be 'all'")
    p.add_argument("--reps-for", action="append", metavar="EXERCISE=N", help="reps for one exercise")
    p.add_argument("--drop", action="append", metavar="EXERCISE", help="remove an exercise (and its rest)")
    p.add_argument("--append", action="append", metavar="CATEGORY/NAME:REPS[@LB]", help="add an exercise at the end")
    p.set_defaults(f=cmd_clone)

    p = sp.add_parser("new", help="build a strength workout from scratch")
    create_opts(p)
    p.add_argument("--exercise", action="append", required=True, metavar="CATEGORY/NAME:REPS[@LB]")
    p.add_argument("--rounds", type=int, default=3)
    p.add_argument("--rest", type=int, default=45, help="seconds between exercises")
    p.add_argument("--last-rest", type=int, help="seconds after the last exercise of a round")
    p.set_defaults(f=cmd_new)

    p = sp.add_parser("send", help="push a workout to the watch");        p.add_argument("workout_id"); p.add_argument("--device"); p.set_defaults(f=cmd_send)
    p = sp.add_parser("schedule", help="put a workout on the calendar");  p.add_argument("workout_id"); p.add_argument("date"); p.set_defaults(f=cmd_schedule)
    p = sp.add_parser("unschedule", help="remove a calendar entry");      p.add_argument("schedule_id"); p.set_defaults(f=cmd_unschedule)
    p = sp.add_parser("scheduled", help="calendar entries for a month");  p.add_argument("--month", metavar="YYYY-MM"); p.set_defaults(f=cmd_scheduled)
    p = sp.add_parser("delete", help="delete from the Connect library");  p.add_argument("workout_id"); p.add_argument("--yes", action="store_true"); p.set_defaults(f=cmd_delete)
    p = sp.add_parser("devices", help="registered devices");              p.set_defaults(f=cmd_devices)
    p = sp.add_parser("queue", help="pending device messages (pushed workouts)"); p.set_defaults(f=cmd_queue)
    p = sp.add_parser("exercises", help="search Garmin's exercise catalog"); p.add_argument("query"); p.set_defaults(f=cmd_exercises)

    a = ap.parse_args()
    g = None if a.cmd == "exercises" else client()
    a.f(g, a)


if __name__ == "__main__":
    main()
