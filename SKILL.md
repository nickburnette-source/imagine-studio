# Rally clip graph + Imagine shot briefs

A rally skin is seven assets in one world. Grok writes the beats. Imagine
animates them. Do **not** mention apps, invites, UI chrome, phones, or
“skins” in Imagine prompts.

## Graph

```
Preview.png          look-lock still
Rally.mp4            START Preview  →  one send beat  →  cuts to Idle
Message.mp4          START Preview  →  one incoming beat  →  cuts to Idle
Idle.mp4             START Idle rest → END Idle rest  →  loops
                     last frame is reused as the start of the reactions
Accept.mp4           START Idle last frame  →  one yes beat  →  cuts to Idle
Decline.mp4          START Idle last frame  →  one abort beat  →  cuts to Idle
Reschedule.mp4       START Idle last frame  →  one hold beat  →  cuts to Idle
```

## Imagine (image-to-video)

The **still is the picture**. The **prompt is only what changes**.

- One action per clip. One camera (or locked). Order events in time.
- Do not re-describe subject, lighting, or composition — the frame has them.
- Do not add people, objects, weather, or particles that are not already
  in the still. Dust/debris only if the still shows loose dirt or sand.
- Atmosphere words (“dust explodes”, “fog rolls in”) spawn fake volumes.
  Prefer motion of things that exist: rotors, lights, a visor, a hand.
- Write `Sound:` as a sound designer: named materials and spaces
  (“turbine spool through the open cabin, rotor whoosh, headset click”).
- Short in-world speech is fine. Baked captions and logos are not.

## Roles (meaning vs shot)

Translate squad meaning into a shot the locked still can actually play.

| Clip | Squad meaning | Shot (from the still) |
|------|----------------|------------------------|
| Preview | recognition | full-frame still of the subject |
| Rally | we’re going | spool-up / ready-to-go using what’s in frame |
| Message | something’s coming in | attention: a light, a glance, a radio click |
| Idle | waiting | tiny looping life, camera locked |
| Accept | I’m in | commit using what’s in frame (hand, rotor, lights) |
| Decline | can’t make it | abort / stand-down in frame (radio, visor, hold) |
| Reschedule | later | wait-one in frame (idle RPM, a fist hold, weather *if it’s already there*) |

Creative liberties = *which* in-frame motion carries the meaning.
Not a new location, not a new person, not matter from nowhere.
