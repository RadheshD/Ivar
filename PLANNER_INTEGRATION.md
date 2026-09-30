# PLANNER_INTEGRATION.md — AI Planner → WhatsApp handoff contract

This is the wire contract between the AI Planner page (website) and the IVA WhatsApp
channel (this repository). It covers only the "Share Plan on WhatsApp" button: what URL
to build and what message to put in it. Everything else about the planner — its UI,
questionnaire and any nutrition computation — is entirely the website's concern; the
WhatsApp side does no computation of its own and only ever restates what this message
says.

## 1. The `wa.me` link

The "Share Plan on WhatsApp" button must open:

```
https://wa.me/<business-number>?text=<url-encoded message>
```

- `<business-number>` is the WhatsApp Business number IVA runs on, in international
  format with no `+`, no spaces and no leading zeros (e.g. `919876500001`). **Never
  hardcode this number in the website's code.** Read it from the website's own
  configuration/environment, the same way any other IVA contact number is configured
  there — it can change (e.g. between staging and production, or if the number is ever
  rotated) independently of a code deploy.
- `<url-encoded message>` is the message below, percent-encoded as a URL query
  parameter (standard `encodeURIComponent` / equivalent). In particular, line breaks
  must become `%0A` (or `\n` encoded), and spaces should be encoded as `%20` (not `+`,
  since this is the query string of a `wa.me` link, not a form body).

Example (line breaks shown literally for readability; encode them for the real link):

```
https://wa.me/919876500001?text=IVA%20PLAN%20v1%0Agoal%3A%20healthy_eating%0Adiet%3A%20veg%0Acalories%3A%202446%0Aprotein_g%3A%2091%0Apicks%3A%20Plant%20Protein%20Mix%3B%20Active%20Protein%3B%20Ivar%20Active%20Nutrition%20Drinks%3B%20Makhana%20Crunch
```

Tapping it opens WhatsApp with that text pre-filled in the chat with the business
number; the customer still has to tap **Send**. Nothing is sent automatically.

## 2. The message format

Before encoding, the message is plain text, newline-separated:

```
IVA PLAN v1
goal: healthy_eating
diet: veg
calories: 2446
protein_g: 91
picks: Plant Protein Mix; Active Protein; Ivar Active Nutrition Drinks; Makhana Crunch
```

Rules:

- **The first line must be exactly `IVA PLAN v1`** — that exact case, that exact
  spacing, nothing else on the line. This is how the bot recognises the message; get
  this wrong and it is treated as ordinary text, not a plan.
- Every line after that is `key: value` — the key, a colon, a space, then the value.
  One field per line.
- Only send fields you actually have a value for. A key that's missing or empty is
  simply not included as a line — don't send `calories: ` with nothing after it.
- Field order does not matter, but the example above is the recommended order.
- The whole message should stay well under 2000 characters. In practice a handful of
  fields plus up to 10 picks will never come close to that.

## 3. Allowed keys and values

Only these keys are read. Anything else is silently ignored — not an error, but not
acted on either, so don't rely on any other key reaching the bot.

| Key | Allowed values | Notes |
|---|---|---|
| `goal` | `healthy_eating` | The only value currently recognised. Send it only when it's actually this. More values will be added here once agreed with the WhatsApp side — do not invent new ones without checking first, they will just be dropped. |
| `diet` | `veg`, `egg`, or `non_veg` | Optional. If present and valid, it's saved as the customer's food preference. |
| `calories` | Integer, `800`–`6000` | Optional. Outside this range, or non-numeric, it is dropped (not clamped, not rounded — just ignored). |
| `protein_g` | Integer, `10`–`400` | Optional. Same rule: outside range or non-numeric is dropped. |
| `picks` | Product names, separated by `; ` (semicolon-space) | At most 10. Each name is matched against the live catalog on the WhatsApp side — send the plan's product names as written; do not invent IDs or SKUs, there is no such field. |

**A value the bot doesn't recognise is dropped, never guessed.** For example
`goal: lose_weight` today results in no goal being shown at all, not the closest known
goal.

## 4. Fields that must NOT be sent

Do **not** include any of the following, even if the planner computes them internally:

- BMI
- BMR
- TDEE
- Healthy weight range
- Nutrition score

These are treated as unrecognised keys on the WhatsApp side and are dropped silently —
they will never be stored, never be shown to the customer there, and never be logged.
This isn't a missing feature to request later: it's a deliberate data-minimisation
decision (the WhatsApp channel restates only the targets the customer explicitly shared
for shopping — goal, diet, calories, protein — and does no health/medical computation
or storage of its own). If a future field genuinely needs to cross this boundary, that
needs its own conversation, not just adding a new key.

## 5. What the customer sees back

For reference, so the two experiences feel consistent: the bot replies with a short
summary of only the fields it accepted (e.g. "Your plan: healthy eating, about 2446 kcal
and 91 g protein a day"), then each `picks` entry it could match in the catalog, shown
with the catalog's own current name and price — never a price or description from the
message text. Unmatched or out-of-stock picks are mentioned, not silently dropped. An
"Add all to cart" button adds everything that matched.

## 6. Privacy

Plan contents can be health-adjacent. The WhatsApp side never logs the message text or
any parsed field, and never stores plan text in its database (only a message length is
recorded for the underlying webhook message, same as for every other message). Please
apply the same care on the website side: avoid logging the full plan text in analytics
or error tracking beyond what you already need for support purposes.
