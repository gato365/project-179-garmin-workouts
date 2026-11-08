# project-179-garmin-workouts

Plan a week of training in one YAML file, turn it into dated workouts on the
Garmin Forerunner, and score how closely the week was followed. The data side
of this (calories, activities, strength sets, the EMANS OS app) lives in
`project-105-emans-data`; this repo only decides what the watch should ask for
and records whether it happened.

```
plans/2026-11-08.yaml      the week: which routine, which day, what time, what changes
library/workouts.yaml      the routines themselves (exercises, sets, reps, rests, weights)
library/garmin_aliases.yaml  display name -> Garmin CATEGORY/EXERCISE where they differ
plan_workouts.py           show | validate | build | adherence | cleanup
garmin_workouts.py         the lower-level Connect manager (list, clone, send, schedule, ...)
history/                   one JSON per plan: workout ids created, then the adherence score
images/                    pictures and clips of me talking through the philosophy (below)
```

## Weekly loop

```bash
pip3 install -r requirements.txt            # garminconnect 0.2.8 + PyYAML (Python 3.9 ok)

cp plans/2026-11-08.yaml plans/2026-11-15.yaml   # next week: edit dates, times, changes
python3 plan_workouts.py show     plans/2026-11-15.yaml
python3 plan_workouts.py validate plans/2026-11-15.yaml      # every exercise is in Garmin's catalog
python3 plan_workouts.py build    plans/2026-11-15.yaml --dry-run
python3 plan_workouts.py build    plans/2026-11-15.yaml      # create + schedule on each date
python3 plan_workouts.py build    plans/2026-11-15.yaml --send --force   # also push to the watch

# the Sunday after:
python3 plan_workouts.py adherence plans/2026-11-15.yaml     # planned vs recorded, written to history/
python3 plan_workouts.py cleanup   plans/2026-11-15.yaml --yes  # optional: clear them out of Connect
```

Login uses the saved token at `~/.garminconnect` (shared with the importer in
project-105). Nothing in this repo holds credentials. `--dry-run` never logs in.

### What `build` produces

- **Name with the date**: `2026-11-09 Mon 06:00 Arms & Shoulders`. Each week's
  workouts are distinct objects, so the Connect library and `history/` are
  searchable by day instead of filling with identical "Phase 1" copies.
- **Description = the plan**: the week's description, the routine's `change`
  note (how and why this week differs), the rest policy, the planned start
  time, the focus note, and the exact weights.
- **Cycle, not straight sets**: `order: circuit` makes each block a Garmin
  repeat group of `A, rest, B, rest, C, rest` × sets, so the watch walks
  A → B → C → A → B → C and the set number is the round number. `order:
  straight` gives one repeat group per exercise instead.
- **Intentional rest**: `rest_between_sets` is programmed into every rest step.
  A routine (or a single session) can override it, e.g. 20 s for the midday
  circuit.
- **Weights in pounds**, sent to Garmin in kilograms. Library defaults, then
  `routines.<name>.weights`, then a session's own `weights`, last one wins.
- **Stretch**: `stretch: "05:00"` appends a timed stretch step.
- Swim sessions and cancelled sessions are kept in the plan and the history but
  never become Garmin workouts.

### What `adherence` measures

For every planned session it finds the watch activity of the right type on that
day closest to the planned time (preferring the activity that ran the scheduled
workout), then reports `on time` (within 15 min, `--tolerance`), `late N min`,
`early N min`, `missed`, or `cancelled`. Unplanned sessions the watch recorded
are listed too. The result goes to `history/<plan>_adherence.json`, so the
history of plans **and** of how they were followed accumulates in git.

Garmin schedules by **date only**; the planned clock time lives in the name,
the description and the history, and `adherence` is where it gets checked.

## Philosophy

### Workout modifications

I want to

- change the names of the workouts to have dates for usage
- know how I plan to change it, in the description
- specify the time between sets in terms of focus, but then also be aware in
  intentional rest
- store the history of my workouts and the adherence to them

### Importance of timing

Over the years I have noticed the importance of time and when I do not feel like
doing it. There are two types:

- doing the exercise at the precise time I have planned
- beginning the set at the planned time, based on my focus and fatigue

It is important to distinguish these because they can help regarding completing
a task in focus rather than letting distractions or fatigue dictate the timing
of my actions. **This can be carried out in other areas of life as well, by
being mindful of the difference between planned timing and focus-based timing.**

In this repo the first kind is the session `time` in the plan, checked by
`adherence`; the second is the rest step the watch counts down, set by
`rest_between_sets`, and the `focus_note` written into every description.

### Equation of weight trajectory

- I should easily be able to figure out when exactly I will be my ideal weight
  based on a simple equation of calorie intake, expenditure, and my current
  weight.
  - Calories spent through exercise and daily activities should be accounted
    for in the calculation.
  - Based on my food intake according to my dietary plan, and me exceeding my
    calories, I should have a clear understanding of how it affects my weight
    trajectory and adjust my plan accordingly.
- As I learn more about my body, I should be able to adjust my workouts,
  dietary plans, and how I calculate this equation of weight trajectory to
  better suit my individual needs and optimize my progress.

The inputs already exist in `project-105-emans-data`: `eman_calories` (intake),
`eman_exercises.calories` (exercise expenditure), and daily weight in
`emans_os.activity_logs`. The equation itself is the next thing to build.

### Data management

One source of truth per fact, and corrections live beside the data rather than
over it. The weekly markdown is the record for food; the watch is the record for
activities; the plan file here is the record for intent. Where a device is wrong
(GPS under-counting pool laps, the app dropping a water entry) the fix goes into
an override table that the loader re-applies, so a refresh can never silently
undo it.

### In my own words

Pictures and clips of me walking through this live in [`images/`](images/).
Drop a file in and reference it here:

<!-- ![Why dated workouts](images/2026-11-dated-workouts.jpg) -->
<!-- ![Planned time vs focus time](images/2026-11-timing.jpg) -->
<!-- ![The weight equation on the whiteboard](images/2026-11-weight-equation.jpg) -->

## Weekly workout plan

### Week of 2026-11-08

| Day | Morning | Midday | Night |
|---|---|---|---|
| Sunday | Rest & Swim | Circuit & Stretch 12:10 PM | Circuit & Stretch 4:45 PM |
| Monday | Arms & Shoulders 6:00 AM | Circuit & Stretch 12:10 PM | Circuit & Stretch 4:45 PM |
| Tuesday | Legs & Abs 6:45 AM | Circuit & Stretch 12:10 PM | Circuit & Stretch 4:45 PM |
| Wednesday | Chest & Back 6:00 AM | Circuit & Stretch 12:10 PM | Circuit & Stretch 4:45 PM |
| Thursday | Arms & Shoulders 6:45 AM | Circuit & Stretch 12:10 PM | Circuit & Stretch 4:45 PM |
| Friday | Legs & Abs 6:00 AM | Circuit & Stretch 12:10 PM | Circuit & Stretch 4:45 PM |
| Saturday | Chest & Back (cancelled) | Circuit & Stretch 12:10 PM | Circuit & Stretch 4:45 PM |

The machine-readable version is [`plans/2026-11-08.yaml`](plans/2026-11-08.yaml).
The midday and night sessions both use the library's *Circuit Workout -
Midday*; that is an assumption marked in the plan file.

## Library notes

`library/workouts.yaml` is the Garmin Connect backup of the routines (times
`MM:SS`, weights in pounds, a rest after the last exercise is skipped on the
final round). Garmin's catalog spells a few names its own way
(`BENT_OVER_ROW_WITH_DUMBELL`, `DUMBBELL_FLYE`), and some display names are
ambiguous or plural (`Push-ups`, `Squats`, `Shoulder Press`); those are pinned
in `library/garmin_aliases.yaml`. `validate` tells you when a new name needs an
alias, and `python3 garmin_workouts.py exercises "<word>"` searches the catalog.

What the watch shows: a workout in the Connect library is not on the watch until
it is **sent** (stays until deleted on the watch) or **scheduled** (shows on that
day's calendar). `build` schedules by default; add `--send` only for the ones
you want in the watch's own Workouts list.
