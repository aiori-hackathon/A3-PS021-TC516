# Orientation video script: Threadline (PS-021, team A3-PS021-TC516)

**Length:** planned ≈ 8 min 15 s; **hard limit: under 10 minutes** (aim to finish by 9:30). **Format:** each member appears on camera for their introduction, then speaks over a screen recording of the dashboard.
Speaker lines are suggestions; say them in your own words. Placeholders in *[brackets]* must be replaced before recording.

## Team

| Member | Name | Role | Owns in the code |
|---|---|---|---|
| Yugraj Mangate | Yugraj Mangate | Backend, API, SQLite persistence, and project coordination | `server.py`, `threadline/service.py`, `threadline/store.py` |
| Neha Niphade | Neha Niphade | Agent adapters, correlation, recovery, testing, dashboard, and documentation | `threadline/`, `tests/`, `static/`, `docs/` |

Every member's segment covers four points: **(1) their role, (2) their understanding of PS-021, (3) the proposed Threadline solution from their angle, (4) their part of the failure-and-recovery demo.**

## Timing budget

| Segment | Speaker | Planned |
|---|---|---|
| 1. Role, PS-021, solution, baseline demo | Member 1 | 2:15 |
| 2. Role, PS-021, solution, failure & recovery demo | Member 2 | 3:15 |
| 3. Joint idempotency & evidence demo | Neha | 1:45 |
| Close | Yugraj | 0:30 |
| **Total** | | **≈ 8:00** (≈ 2:00 spare for transitions) |

Rehearse once with a stopwatch. If a run-through exceeds 9:30, shorten the "Solution from my angle" parts first; never cut a demo step.

## Before you hit record

```powershell
cd C:\AIORI
python -m unittest discover -s tests -t .     # must end with "OK" (56 tests)
python server.py --fresh                      # clean demo database; leave this window running
```

* Open http://localhost:8080 in a full-screen browser window (zoom 100–110%).
* *Recent experiments* must be empty. If it isn't, click **Reset demo data** twice.
* Leave **Animate replay** ticked. Close notifications and hide the bookmarks bar.
* After recording, stop the server with **Ctrl+C** in its window.

---

## Segment 1: Yugraj Mangate, Backend, API, and SQLite persistence (≈ 2:15)

**On camera: role + PS-021 (~40 s)**
> "Hi, I'm Yugraj Mangate. I built the backend, the API and the SQLite persistence of Threadline.
>
> My understanding of PS-021: a modern AI assistant is not one program. A user types in a web chat, a planner agent decides what to do, and a tool agent calls real systems. Each runs as a separate service with its own protocol. Messages between them can time out, be retried, arrive twice, arrive late, arrive out of order, or get lost. If that happens during a hand-off, the conversation breaks: context is lost, or the user gets the same answer twice. PS-021 asks us to keep the dialog correlated and recoverable as it moves across adapters."

**Solution from my angle (~40 s), screen: README architecture diagram or the dashboard header**
> "Our proposed solution, Threadline, makes one key decision: no adapter owns the conversation; an append-only event log does. Every message passes through one gateway, which validates it and appends it to a SQLite log. Database triggers make that table physically append-only: no update, no delete. Even duplicates and late messages are stored, because the log is our evidence. It is all standard-library Python with a small JSON API: create an experiment, recover, deliver delayed messages, resume, and inspect."

**Demo part: baseline (~55 s), screen: dashboard**
1. Click **Happy path** → **Run experiment**.
   > "This is a clean conversation: the user writes in web-chat, the planner answers, hands off to the tool agent, and the result comes back. The event inspector at the bottom is the raw log in arrival order."
2. Click **2 · Replay & recover**.
   > "Recovery reads the log back. Status *consistent*: nothing had to be fixed. The timeline shows the conversation crossing three adapters, and the experiment results show the counts and how long recovery took."

## Segment 2: Neha Niphade, Agent adapters, correlation, and recovery (≈ 3:15)

**On camera: role + PS-021 (~35 s)**
> "Hi, I'm Neha Niphade. I worked on the agent adapters, the correlation model and the recovery engine.
>
> For me, the core of PS-021 is identity: if you can't tell which message is which, and what it answers, you can't recover anything. Timestamps and adapter names are not enough, because clocks differ and retries come from different places."

**Solution from my angle (~45 s), screen: click any row in the Event inspector**
> "In Threadline every message carries five correlation fields: a stable conversation id, a unique message id, a sequence number, a causation id pointing to the message it answers, and a deterministic idempotency key, which is a hash of the conversation, sequence, role and cause. On the right you can see the native payload. The planner speaks HTTP with headers, the tool agent speaks JSON-RPC, the web chat a WebSocket frame. The same five fields survive all three protocols.
>
> Recovery is a pure replay of the log: validate, remove duplicates, resolve conflicts, order by sequence, detect gaps, and find where to resume. Every log entry gets a verdict, a rule and a reason."

**Demo part: failure and recovery (~1:55)**
1. Click **Chaos (combined)**; point at the five ticked faults → **Run experiment**.
   > "Now we inject five kinds of failure at once: the tool agent times out during a hand-off; messages are duplicated, both a redelivery and a planner retry with new wording; events overtake each other; one message is delayed in the network; and two agents claim the same slot."
2. Click **2 · Replay & recover** and let the animation play. Point at the Decision log.
   > "The redelivery and the retry are removed as duplicates, because they share an idempotency key, so the user never sees two replies. The split-brain claim on seq 4 loses to the tool agent, because the previous hand-off leased that slot to it. Late events are placed by sequence number, never by timestamp.
   > The result is *incomplete*: sequence 6 is missing. We don't skip it silently. The gap is shown in red, the next event is flagged because its cause is missing, and resume is blocked."
3. Click **Deliver delayed (1)** → **2 · Replay & recover**.
   > "The delayed message finally arrives, after later ones. Replaying the log again closes the gap. Status: *recovered with conflicts*, which means the conflict was resolved by a rule but stays flagged for review. Threadline also detects the open hand-off to the tool agent."
4. Click **3 · Resume from checkpoint** → **2 · Replay & recover**.
   > "The tool agent resumes from the recovered checkpoint at sequence 8, linked by causation to the last good message. The dialog is now complete."

## Segment 3: Neha Niphade, Testing, experiments, dashboard, and documentation (≈ 1:45)

**On camera: role + PS-021 (~35 s)**
> "I also handled testing, the experiments, the dashboard and the documentation.
>
> I see PS-021 as an *experimental* problem. It is not enough to claim recovery works; we have to show, repeatably, what happens under each kind of failure, and explain every decision."

**Solution from my angle + demo part (~60 s), screen: dashboard**
> "Threadline's dashboard turns the event log into evidence an operator can check."
1. Click **2 · Replay & recover** once more; point at the header of *Experiment results*.
   > "Running recovery again on the same log gives the identical context hash. Recovery is idempotent, and the dashboard shows it on every run."
2. Click the `DUPLICATE` row for seq 2 in the Event inspector, then the `REJECTED` row for seq 4.
   > "Every decision is explainable from the log: verdict, rule, and a plain-language reason, next to all the correlation metadata."
3. Click **Missing message** → **Run experiment** → **2 · Replay & recover**.
   > "When a message is truly lost, Threadline keeps reporting *incomplete* and *ended with gaps*. It never invents context."

**Evidence (~40 s), screen: a second terminal (leave the server running)**
```powershell
cd C:\AIORI
python -m threadline.bench
python -m unittest discover -s tests -t .
```
> "The benchmark runs all eight scenarios through the full workflow. Every dialog ends complete except the permanently missing message, which is correctly reported as incomplete, and every recovery is idempotent. Our 56 automated tests check the rules; for example, 25 different arrival orders always give the same context hash, and randomised timestamps change nothing."

## Close: Member 1 (≈ 0:30)

> "Limitations: the adapters are simulated in-process, and resume replays a scripted continuation instead of calling a real LLM. Next we want real WebSocket and JSON-RPC connectors, an outbox for automatic redelivery, and seeded random fault injection for statistical evaluation. Thank you. Our code and documentation are on GitHub; the link is in the video description."

*Video description (paste on upload):* `Threadline: AIORI-3, team A3-PS021-TC516, PS-021. Code: https://github.com/aiori-hackathon/A3-PS021-TC516`

---

### Recording checklist

- [ ] Yugraj Mangate and Neha Niphade names and roles are filled in above
- [ ] `python -m unittest discover -s tests -t .` ends with `OK` (56 tests) on the recording machine
- [ ] Server started with `python server.py --fresh`; *Recent experiments* is empty at the start
- [ ] Both members are visible on camera and clearly audible, and cover role, PS-021, the Threadline solution, and their demo part
- [ ] Final video is under 10 minutes (planned ≈ 8:15)
- [ ] README.md → *Team* and *Submission details* completed for Yugraj Mangate and Neha Niphade
- [x] Uploaded: https://youtu.be/4IXSzTUrEOA, linked in README.md and docs/PRESENTATION.md
- [ ] Server stopped with Ctrl+C after recording
