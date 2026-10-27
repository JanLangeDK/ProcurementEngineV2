# Procurement Engine V2

The procurement solution the **Agent Architecture Studio** generated from four business processes, built as the
studio's architecture document prescribes: one durable workflow platform running one workflow per story,
deterministic services and catalog tools for most steps, three bounded agents only where judgement is needed, and
every human approval kept. Everything runs on mocks; nothing here talks to a real SAP, mailbox or case system.

`docs/architecture.md` is the specification (sections 1–10) and the build plan (section 11) exported from the studio.
The first Procurement Engine answered the same four stories with one agent per colleague; this one answers them with
one workflow each and shows the difference.

## What it does

| Story | Workflow | Who decides what |
|---|---|---|
| A · a component requirement becomes a purchase order | `production_purchase` | services validate, check coverage, pick approved suppliers and the cheapest contract price; the **Supplier reply interpretation agent** judges lead times and problems; the **Procurement Manager** approves above 25,000 EUR; the workflow writes the PO to SAP-sim |
| B · an invoice with a price deviation (INV7731) | `price_deviation` | the matcher (code) finds DKK 52.00 vs 48.00 against the policy tolerance; the invoice is blocked; the **Credit note request agent** drafts; the **AP Specialist** approves the message; the **interpretation agent** reads the reply; re-match in code; parked; the **AP Team Lead** releases |
| C · a supplier asks when it gets paid | `supplier_query` | services map the supplier's invoice number to ours and compute the due date and payment run in code; read-only on SAP; the answer says it is an expectation, not a promise |
| D · general invoice processing | `invoice_processing` | receive, extract, PO and contract in parallel, compare and validate in code; the **Discrepancy analysis agent** explains exceptions; approval or supplier clarification; posting only when valid or approved |

## Run it

```bash
uv sync --group dev
cp .env.example .env           # optional: PEV2_LLM_* for Azure AI Foundry; without it the fixture provider runs the agents
uv run pev2 ui                 # http://localhost:7860 — the test window
uv run pev2 api                # http://localhost:8000/docs — the HTTP API
uv run pev2 demo               # reset and play the four stories unattended
uv run pytest                  # 40+ tests: foundation, rules, stories, API
uv run pev2 evals run          # the agents' eval sets with the fixture provider
```

### The test window

- **Simulator**: fire the events that start workflows (an invoice arrives, a payment question, a component requirement)
  and the events workflows wait for (a supplier reply, an order confirmation); scheduler tick; reset; simulated outages.
- **Instances**: every workflow instance with its status and the step it is at; select one for its full audit trail.
- **Approvals**: pick who you are; approve or reject what is waiting for you (segregation of duties is enforced).
- **Reviews**: parked instances (modelled exceptions, failed gates, low agent confidence, outages) — retry or abandon,
  or ask the investigator for a read-only proposal.
- **Discoveries**: what the investigator found missing, grouped and counted; export the gap report for the studio.
- **Cases & mail**, **SAP-sim**, **Audit**: what the simulated systems hold.
- **Policy**: edit the invoice-handling tolerance and fire INV7731 again — the same code behaves differently.

## Uncovered cases: the investigator and the loopback

The four workflows cover what was modelled. A case nobody modelled, say a supplier mail "we changed our bank
account, update it before the next payment run", goes to the **uncovered-case workflow**: the **Investigator agent**
establishes the facts with read-only tools, states a hypothesis and the risk, proposes actions, and lists what the
platform is missing to handle the case. Three things are guaranteed in code, not in a prompt:

- the investigator holds only read tools; anything else it asks for is refused by the registry and recorded;
- a proposal never executes: a gate marks each proposed action as supported or not, a case is opened for the owner,
  and a person decides in the Reviews tab;
- every missing tool, workflow, policy, data or permission lands in **Discoveries**, grouped and counted, and the
  **gap report** is the brief the Agent Architecture Studio turns into the next scenario. Ten parked cases with the
  same gap become one new modelled path.

The same investigator can be asked about any parked instance ("Ask the investigator" in Reviews) without changing it.

## How it is built (section 11 of the architecture document)

```
src/pev2/
  engine/runtime.py     the durable workflow runtime: replay-based steps, waits, approvals, reviews, gates, scheduler tick
  workflows/            one module per workflow of §4 (steps named as in the document) + unknown_case
  investigation.py      the investigator's guardrails, the discoveries log and the gap report (the loopback)
  services/             deterministic services of §3, rules as functions (matching, payments, purchasing, enquiries, invoices)
  gates/                validation gates of §5
  tools/                the catalog of §3: registry (allow-lists, idempotency, retries, authority matrix) and mock adapters
  agents/ gateway/      the three agents of §2 and the model gateway (fixture or OpenAI-compatible incl. Azure)
  observability/        structured audit events with the process instance id
  ui/app.py api.py      the Gradio window and the HTTP API
  demo.py evals.py      pev2 demo / smoke / evals
fixtures/sap/           the simulated SAP tables (synthetic, Danish Crown flavoured, from the first engine)
fixtures/brain/         policies, playbooks, supplier briefs the services read
tests/ evals/           rules → tests; agents → eval sets
```

Deviations from §11.2, with reasons: the durable workflow engine is a small replay-based runtime over SQLite
(`engine/runtime.py`) rather than Temporal or Dapr, and the agent harness is the OpenAI SDK behind a gateway rather
than Microsoft Agent Framework — both because the brief was "no real backend, mock everything" and one laptop.
The step contract (activities, waits, approvals, parallel groups) is the same, so a real engine can replace it.

## Honest limits

- Mocks only. SAP-sim is a few SQLite tables; the knowledge base is keyword search over markdown.
- The fixture provider stands in for the model with deterministic rules; set `PEV2_LLM_PROVIDER=openai_compatible`
  to run the three agents on a real model (Azure AI Foundry works with the v1 endpoint).
- One process, one SQLite file: fine for testing, not for production.
