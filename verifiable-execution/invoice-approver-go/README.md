# invoice-approver: Verifiable Execution You Can Tamper With Yourself

A small invoice-approval agent that runs as a durable, **verifiable** Dapr
Workflow on [Diagrid Catalyst](https://www.diagrid.io/catalyst). Every step it
takes is signed as it happens, so the run leaves behind a record you can hand to
an auditor and prove was not altered.

This repo is built to be poked at. You run the agent, bring your own database as
the workflow store, edit a row in that database directly, and watch Catalyst
catch the change - in the console and from the command line. If you can make the
tamper show up on your own machine, you understand exactly what the signing
guarantees.

- **The agent** (`main.go`): `get_invoice -> check_vendor -> check_policy -> decide -> release_payment | hold_payment`. One node per business step, so each one lands in the signed history as its own activity. The checks are plain Go; the model writes the decision rationale but never gates the money - the router reads the check results directly.
- **The tamper script** (`tamper_history.py`): edits the signed history straight in your Postgres store, flipping a flagged vendor to cleared.

---

## What You'll See

By the end you'll have produced these.

**Before - the run, verified.** Every step signed as it happened:

![Verified execution - every step signed and green](docs/images/verified.png)

**After - one row edited in the database.** The green badge is gone, replaced by a red banner naming the block that broke:

![Tampered execution - red Workflow history tampered banner, 15 of 18 events signed](docs/images/tampered.png)

The tamper localizes to the exact steps and the exact value that changed:

![Event history - the full run, with red shields on the two events that changed](docs/images/tampered-history.png)

![check_vendor node - History integrity Tampered, output vendor_check CLEARED](docs/images/tampered-node.png)

---

## Prerequisites

```bash
diagrid version                 # Diagrid CLI - https://docs.diagrid.io/references/catalyst/catalyst-cli-intro/
go version                      # Go 1.26.4+
export OPENAI_API_KEY="sk-..."  # the model writes the decision text
```

You'll also need a Postgres database you control. Any Postgres works; this guide
uses a free [Supabase](https://supabase.com) project because it's the quickest
to stand up.

---

## 1. Create a Project With History Signing On

Signing is set when the project is created and can't be added later, so it goes
on the create command. We're bringing our own workflow store, so we do **not**
ask for the managed one.

```bash
diagrid login

diagrid project create invoice-audit-byodb \
  --enable-workflow-history-signing \
  --deploy-managed-kv --deploy-managed-pubsub \
  --wait --use
```

---

## 2. Point Catalyst at Your Own Database

This is the "bring your own store" part. Catalyst will write the signed workflow
history into **your** Postgres, which is what lets you tamper with it later.

### Get the right connection string from Supabase

In your Supabase project: **Project Settings -> Database -> Connection string ->
Session pooler**. Use the **session pooler** host, not `db.<ref>.supabase.co`.

> **Why the pooler matters.** The direct `db.<ref>.supabase.co` host is
> IPv6-only, and the Dapr sidecar can't dial it from Catalyst - you'll get
> `dial tcp [2600:...]:5432: network is unreachable`. The session pooler is
> IPv4, so it just works. The pooler host looks like
> `aws-0-<region>.pooler.supabase.com` and the user becomes
> `postgres.<project-ref>`.

### Create the state store component

The component must set `actorStateStore=true` - workflows run on the actor
runtime, so without it Catalyst rejects the store with *"state store is not
configured to use the actor runtime."*

```bash
diagrid component create wf-store \
  --type state.postgresql \
  --metadata connectionString="host=aws-0-<region>.pooler.supabase.com port=5432 user=postgres.<project-ref> password=$SUPABASE_PWD dbname=postgres sslmode=require" \
  --metadata actorStateStore=true \
  --project invoice-audit-byodb --wait
```

Set the password in your shell first so it never lands in your history:

```bash
read -s SUPABASE_PWD && export SUPABASE_PWD
```

> Editing it later? `diagrid component update wf-store --metadata connectionString="..." --project invoice-audit-byodb` - note there's no `--type` flag on `update`.

---

## 3. Run the Agent

Because your `wf-store` already provides the workflow store, skip the managed
one on the run, or you'll hit *"managed workflows store cannot be enabled
because workflows API is already enabled by wf-store."*

```bash
cd invoice-approver-go
go mod tidy
diagrid agent create invoice-approver --project invoice-audit-byodb --wait

# INV-2026-0312 is the flagged vendor over the limit - the run we'll tamper with.
# GOAI_RUN pins the instance ID so you know exactly what to open and export.
GOAI_RUN=inv-0312-run1 INVOICE_ID=INV-2026-0312 \
  diagrid dev run -f catalyst.yaml --project invoice-audit-byodb --skip-managed-workflow
```

You'll see the node trace print and the run end HELD:

```
>>> get_invoice: INV-2026-0312 vendor="Acme Consulting LLC" amount=18750.00 USD
>>> check_vendor: "Acme Consulting LLC" -> FLAGGED
>>> check_policy: 18750.00 vs limit 10000.00 -> FAILED
>>> hold_payment: INV-2026-0312 held for manual review
Checks   : vendor=FLAGGED policy=FAILED
Outcome  : HELD (receipt none)
```

Open the [Catalyst console](https://catalyst.diagrid.io) -> **invoice-approver**
-> the execution. You should see the **History verified** badge and every step
signed. Screenshot this - it's your "before."

---

## 4. Tamper With the Database Directly

Now edit the signed history in your own Postgres, behind Catalyst's back. Dapr's
Postgres store keeps one row per key in a table called `state`; the workflow
history rows have keys like `invoice-approver||inv-0312-run1||history-000007`,
and each value is a base64-encoded protobuf event. One of them contains
`FLAGGED` - the vendor-check result.

`tamper_history.py` finds that row and rewrites `FLAGGED` to `CLEARED` (same
length, so the protobuf stays valid), as if the flagged vendor had been cleared.

```bash
# same password you used for the component
read -s SUPABASE_PWD && export SUPABASE_PWD

# tell the script your pooler host + user (or edit the defaults in the file)
export SUPABASE_HOST="aws-0-<region>.pooler.supabase.com"
export SUPABASE_USER="postgres.<project-ref>"

# dry run first - shows the row it will change, writes nothing
uv run --with 'psycopg[binary]' python tamper_history.py

# do it for real
uv run --with 'psycopg[binary]' python tamper_history.py --apply
```

Output:

```
target row: invoice-approver||inv-0312-run1||history-000007
  contains 'FLAGGED' -> rewriting to 'CLEARED'

tampered invoice-approver||inv-0312-run1||history-000007 in the database.
now reload the execution in the Catalyst console to see it flagged.
```

---

## 5. Watch Catalyst Catch It

Catalyst re-verifies a workflow's history every time it's loaded, so the tamper
surfaces the moment the execution is opened again - it doesn't re-scan every row
of the list on every page load.

**In the console:** reopen the same execution. The green badge is gone, replaced
by a red **"Workflow history tampered"** banner, the count now reads **15 of 18
events signed**, **Block 2** is red, and the affected steps carry red shields.
Drill into `check_vendor` and you'll see the doctored output `"vendor_check":
"CLEARED"` sitting right next to the attestation that was signed for it - flagged
rather than trusted. Screenshot this - it's your "after."

![check_vendor event - the CLEARED output beside its attestation payload and signature](docs/images/tampered-attestation.png)

**In the runtime:** re-run the same instance and the agent runtime refuses to
start on the tampered history:

```
error: durable: schedule workflow: failed to start workflow:
  workflow history signature verification failed for 'inv-0312-run1':
  signature 2: events digest mismatch for range [6, 9)
```

![Agent runtime refusing to start on the tampered instance](docs/images/runtime-refused.png)

Block 2 covers events 6 through 8, and the vendor-check result is event 7 - so
the failure doesn't just say *something* changed, it points at exactly what.

---

## Bonus: Tamper With an Exported Receipt

You don't need the database to see this. Export the signed archive - the receipt
you'd hand an auditor - and it verifies offline against Diagrid's region CA, with
no access to Catalyst:

```bash
diagrid workflow archive export inv-0312-run1 \
  -p invoice-audit-byodb -a invoice-approver --out receipt.json
diagrid workflow archive trust-anchor -p invoice-audit-byodb --out trust.pem

diagrid workflow archive verify receipt.json --trust-anchor trust.pem
# Archive verified  (events: 18, signatures: 6)
```

Now change one value in `receipt.json` (find `"FLAGGED"`, make it `"CLEARED"`)
and verify again:

```bash
diagrid workflow archive verify receipt-tampered.json --trust-anchor trust.pem
# Archive verification failed: signature chain verification failed:
#   signature 2: events digest mismatch for range [6, 9)
```

Same verdict, same located block - and the person checking never needed to know
what the record was supposed to say. They only needed the receipt and the CA.

---

## Clean Up

```bash
diagrid project delete invoice-audit-byodb
```

Then pause or delete the Supabase project once your screenshots are captured.

---

## How the Money Is Gated

Worth calling out, because it's the point auditors care about: the LLM writes the
decision *rationale*, but it does not decide whether to pay. The router in
`route()` reads the deterministic `vendor_check` and `policy_check` results and
only releases payment when both passed. The model can phrase things however it
likes; it can't move money on its own. The signed history proves which path
actually ran.
