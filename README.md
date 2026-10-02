# ReliefRoute

**The last-mile aid dispatcher that proves every delivery decision.**
A Zero-to-Shipped (AWS Builder Center) submission — category *Social Good*, focus track *Community*.

When a disaster or heat emergency hits, responders have supplies in one place and people in need in another. ReliefRoute ingests **need reports** + **available resources**, uses **Amazon Bedrock (Nova Lite)** to produce a **ranked, constraint-checked dispatch plan** — who gets what, in what order, via which volunteer — and attaches a plain-language **"why this plan" proof** (rationale + assumptions + confidence). A **confirmed-delivery feedback loop** then records whether each delivery actually happened and matched the plan, surfacing a live **plan-accuracy** metric.

> **sense → reason → act → prove.** Most entries stop at an answer or a dashboard. ReliefRoute closes the loop and shows its reasoning as an auditable artifact.

---

## Why it's built this way

- **Verifiable action, not just an answer** — every plan carries its rationale, assumptions, and a confidence score.
- **Closed feedback loop** — `/confirm` + `/metrics` prove the action was correct after the fact.
- **Voice-first** — field responders can dictate a need (browser-native Web Speech API — $0, no Amazon Transcribe).
- **Lowest possible AWS footprint** — one Lambda, one DynamoDB table, Bedrock. No API Gateway, S3, CloudFront, Cognito, RDS, or VPC. Scales to zero; ~$0.15 for a 30-day demo run (see `COST-ANALYSIS.md`).

---

## Architecture

```
Browser / phone (SPA served by Lambda; voice via Web Speech API; localStorage cache)
        │  HTTPS
        ▼
Lambda Function URL  (Python 3.12 · arm64 · 256MB)
   GET  /         → serves the SPA
   POST /plan     → Bedrock Nova Lite → ranked plan + "why" proof   ──► Amazon Bedrock (amazon.nova-lite-v1:0)
   POST /confirm  → record delivery outcome                          ──► DynamoDB (on-demand, TTL)
   GET  /metrics  → plan-accuracy readout
```

**Files**
| Path | Purpose |
|---|---|
| `template.yaml` | AWS SAM template — the whole stack |
| `src/app.py` | Lambda handler: routing, Bedrock plan, DynamoDB, embedded SPA |
| `src/requirements.txt` | Build deps (none beyond the runtime) |
| `PLAN.md` | Competitive analysis + why this wins |
| `COST-ANALYSIS.md` | Detailed 30-day cost breakdown |

---

## Prerequisites

1. **AWS account** with credentials configured in your terminal:
   ```powershell
   aws configure          # or: aws sso login
   aws sts get-caller-identity   # confirm you're authenticated
   ```
2. **AWS SAM CLI** — `sam --version` (install: https://docs.aws.amazon.com/serverless-application-model/latest/developerguide/install-sam-cli.html)
3. **Python 3.12** (SAM uses it to build the arm64 package).
4. **Bedrock Nova Lite** — **no manual enablement needed.** Serverless models auto-enable on first `InvokeModel`; the Lambda already has `bedrock:InvokeModel`. Confirm the model is in your account:
   ```powershell
   aws bedrock list-foundation-models --region us-east-1 --query "modelSummaries[?contains(modelId,'nova-lite')].modelId" --output table
   ```

Region is **us-east-1** throughout.

---

## Deploy — step by step

**Step 1 — build** (packages `src/` for arm64):
```powershell
cd "D:\Projects\Kiro\Zero to Shipped"
sam build
```

**Step 2 — deploy** (first time; creates a tiny managed artifact bucket):
```powershell
sam deploy --stack-name reliefroute --region us-east-1 --capabilities CAPABILITY_IAM --resolve-s3
```

**Step 3 — grab the live URL** (printed as the `AppUrl` output; or re-fetch any time):
```powershell
aws cloudformation describe-stacks --stack-name reliefroute --region us-east-1 --query "Stacks[0].Outputs[?OutputKey=='AppUrl'].OutputValue" --output text
```

**Step 4 — open the URL** in a browser. Click **Generate dispatch plan** → you'll see the ranked plan, the "why" proof, then **Confirm** → watch the loop metrics update. (First `/plan` call auto-enables Nova Lite — it may take a second longer.)

**Smoke-test from the CLI** (optional):
```powershell
$URL = aws cloudformation describe-stacks --stack-name reliefroute --region us-east-1 --query "Stacks[0].Outputs[?OutputKey=='AppUrl'].OutputValue" --output text
curl.exe -s -X POST "$URL`plan" -H "content-type: application/json" -d '{\"needs\":[{\"location\":\"Shelter A\",\"people\":120,\"needs\":\"water\",\"urgency\":\"high\"}],\"resources\":{\"supplies\":[{\"item\":\"water (20L)\",\"qty\":90}],\"volunteers\":[{\"name\":\"Asha\",\"vehicle\":\"van\"}]}}'
```

---

## Optional cost safety nets (free)

A $5 monthly budget with an email alert (first two AWS Budgets are free):
```powershell
# Replace EMAIL below. Create budget.json / notif.json first, or use the console: Billing → Budgets.
aws budgets create-budget --account-id (aws sts get-caller-identity --query Account --output text) `
  --budget "BudgetName=reliefroute,BudgetLimit={Amount=5,Unit=USD},TimeUnit=MONTHLY,BudgetType=COST"
```
(Or set it in **Billing → Budgets** in the console — simplest for the alert email wiring.)

---

## Clean up — step by step (stops all billing)

**Step 1 — delete the stack** (removes Lambda, DynamoDB table + data, log group, Function URL):
```powershell
sam delete --stack-name reliefroute --region us-east-1
```
Confirm the two prompts (delete stack / delete artifact folder) with `y`.

**Step 2 — verify it's gone:**
```powershell
aws cloudformation describe-stacks --stack-name reliefroute --region us-east-1
# Expect: "Stack with id reliefroute does not exist"  → fully removed.
```

**Step 3 — (optional) remove the SAM artifact bucket** for a true zero footprint. `sam delete` leaves the `aws-sam-cli-managed-*` bucket (a few MB, fractions of a cent/month):
```powershell
# find it
aws s3 ls | Select-String "aws-sam-cli-managed"
# then (replace BUCKET): empty + delete
aws s3 rb s3://BUCKET --force
```

After Step 1, ongoing cost is **$0** — the stack scales to zero and has no standing resources (no EC2, NAT, or provisioned capacity). See `COST-ANALYSIS.md` for the full 30-day breakdown.

---

## Submission checklist (Zero to Shipped — per official T&Cs)

**Ship gate (mandatory, pass/fail — no exceptions):**
- [ ] App is **live on AWS and reachable via a public URL** at evaluation time (the `AppUrl` output).
- [ ] **Documented proof of coding-agent connection to the AWS console** (screenshot/log of the agent authenticated to your account + the deploy it performed).
- [ ] Accessible to **both the AI scoring system and human judges** (Function URL is public — ✓).

**Submission requirements:**
- [ ] Coding agent connected to the AWS console **before/during** the submission period.
- [ ] **Category:** Social Good (one of the five).
- [ ] **Focus track:** Community (Community or Startup — Community fits a civic/relief app).
- [ ] **Published project on AWS Builder Center** with: (a) proof of coding-agent connection, (b) description of the project + development process, (c) category + focus track, (d) the live app URL.
- [ ] **Original**, not previously published.
- [ ] Documents AWS services used (Lambda, Bedrock Nova Lite, DynamoDB) + how the coding agent was used.

**Judging (4 equal pillars, 25% each — write the Builder Center post to hit all four):**
- **Technical Innovation & Originality** → the verifiable sense→reason→act→**prove** closed loop (rare in the field).
- **Implementation Quality** → one-command SAM deploy, minimal footprint, clean teardown.
- **Community/Market Impact** → last-mile relief; minutes between a need and its delivery.
- **Creativity & Storytelling** → the human relief narrative + the "why this plan" proof artifact.

**Deadline:** October 2, 2026, 11:59 p.m. PT. Prize: $5,000 AWS credits + swag per winner (5 winners; top 100 by AI score advance to the human panel).
