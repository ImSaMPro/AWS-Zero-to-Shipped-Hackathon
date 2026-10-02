# ReliefRoute — 30-Day Cost Analysis (run then delete)

**Scope:** Stack deployed in **us-east-1**, left running **30 days**, then torn down with `sam delete`.
**Architecture priced:** 1 Lambda (arm64, 256 MB, Function URL) · Amazon Bedrock **Nova Lite** (`amazon.nova-lite-v1:0`) · 1 DynamoDB on-demand table · CloudWatch Logs (7-day retention). No API Gateway, S3, CloudFront, Cognito, RDS, VPC, or NAT.

**Prices used (us-east-1, verified Oct 2026):**

| Resource | Price | Free tier (per month) |
|---|---|---|
| Lambda requests | $0.20 / 1M | 1,000,000 requests |
| Lambda compute (arm64) | $0.0000133334 / GB-second | 400,000 GB-seconds |
| Bedrock Nova Lite — input | $0.06 / 1M tokens | none (pay per token) |
| Bedrock Nova Lite — output | $0.24 / 1M tokens | none |
| DynamoDB on-demand write | $1.25 / 1M WRU | — |
| DynamoDB on-demand read | $0.25 / 1M RRU | — |
| DynamoDB storage | $0.25 / GB-month | 25 GB |
| CloudWatch Logs ingest | $0.50 / GB | 5 GB |
| CloudWatch Logs storage | $0.03 / GB-month | — (minimal at 7-day retention) |
| Lambda Function URL | free | — |
| Data transfer out | $0.09 / GB (after free) | 100 GB |

> **Free-tier caveat.** The Lambda compute/request free tier (1M req + 400K GB-s) is **perpetual** (always-free, not 12-month). DynamoDB 25 GB storage and CloudWatch 5 GB are also always-free. So for a demo-scale app, nearly everything lands inside free tier and the **only line that bills from token one is Bedrock**.

---

## Request model (per user action)

- **GET /** (serve SPA): ~1 Lambda invoke, ~50 ms, no Bedrock, 1 tiny DynamoDB read at most.
- **POST /plan**: 1 Lambda invoke (~2.5 s incl. Bedrock call), **1 Bedrock Nova Lite call**, ~2 DynamoDB writes.
- **POST /confirm**: 1 Lambda invoke (~150 ms), 1 DynamoDB write.
- **GET /metrics**: 1 Lambda invoke (~200 ms), a few DynamoDB reads.

**Token estimate per `/plan` call** (the only metered cost): prompt (system + need-reports + inventory) ≈ **1,500 input tokens**; structured JSON plan + rationale ≈ **700 output tokens**. Conservative — real demo prompts are smaller.

**Per-plan Bedrock cost:**
- input: 1,500 × $0.06 / 1,000,000 = **$0.00009**
- output: 700 × $0.24 / 1,000,000 = **$0.000168**
- **≈ $0.000258 per plan** (about **1 plan = 0.026 cents**; ~3,900 plans per $1).

---

## Scenario A — Realistic demo/judging usage (recommended planning number)

Assumes you, judges, and a handful of testers use it. **~500 plan generations** + ~5,000 page views + ~500 confirms over 30 days.

| Line item | 30-day usage | Inside free tier? | Billed cost |
|---|---|---|---|
| Lambda requests | ~11,000 total | ✅ (<1M) | **$0.00** |
| Lambda compute (arm64) | 500 plans × 2.5 s × 0.25 GB = 312 GB-s + overhead ≈ 400 GB-s | ✅ (<400K) | **$0.00** |
| Bedrock Nova Lite | 500 plans × $0.000258 | ❌ (always billed) | **$0.13** |
| DynamoDB writes | ~2,000 WRU | ✅ effectively $0 | **~$0.00** |
| DynamoDB reads | ~10,000 RRU | ✅ effectively $0 | **~$0.00** |
| DynamoDB storage | <1 MB | ✅ (<25 GB) | **$0.00** |
| CloudWatch Logs | <50 MB ingest | ✅ (<5 GB) | **$0.00** |
| Data transfer out | <1 GB | ✅ (<100 GB) | **$0.00** |
| **30-DAY TOTAL** | | | **≈ $0.13** |

---

## Scenario B — Heavy usage (stress / public traffic)

**10,000 plan generations** + 100,000 page views + 10,000 confirms over 30 days.

| Line item | 30-day usage | Billed cost |
|---|---|---|
| Lambda requests | ~130,000 | $0.00 (<1M free) |
| Lambda compute | 10,000 × 2.5 s × 0.25 GB = 6,250 GB-s + overhead ≈ 7,000 GB-s | $0.00 (<400K free) |
| Bedrock Nova Lite | 10,000 × $0.000258 | **$2.58** |
| DynamoDB writes | ~40,000 WRU | ~$0.05 |
| DynamoDB reads | ~200,000 RRU | ~$0.05 |
| DynamoDB storage | ~10 MB | $0.00 |
| CloudWatch Logs | ~0.8 GB ingest | $0.00 (<5 GB free) |
| Data transfer | ~3 GB | $0.00 (<100 GB free) |
| **30-DAY TOTAL** | | **≈ $2.73** |

---

## Scenario C — Idle (deployed but barely touched)

Stack sits deployed; ~20 plans + light clicks over 30 days.

| Line item | Billed cost |
|---|---|
| Everything (Lambda, DynamoDB, CloudWatch) | $0.00 (free tier) |
| Bedrock | 20 × $0.000258 = **$0.005** |
| **30-DAY TOTAL** | **≈ $0.01 (effectively $0)** |

**Key fact:** this is a **serverless, scale-to-zero** stack. There is **no hourly/standing charge** — no EC2, no NAT gateway, no provisioned capacity, no idle RDS. If nobody calls it, it costs **$0**. You pay only for actual Bedrock tokens consumed.

---

## 30-day bottom line

| Scenario | 30-day cost |
|---|---|
| **Idle** | ~$0.01 |
| **Realistic demo (plan around this)** | **~$0.13** |
| **Heavy / stress** | ~$2.73 |

**Plan for ≈ $0.15, worst-case a few dollars.** The dominant (really, the *only* material) cost is Bedrock Nova Lite tokens; everything else stays inside the always-free tier at demo scale.

### What could blow this up (and the guards already in the plan)
- **Runaway `/plan` calls** (abuse of the public Function URL). Guard: in-handler per-IP rate limit + a daily plan cap; optionally a **Bedrock budget alarm**.
- **Verbose prompts** (stuffing huge inventories). Guard: cap input size server-side; the JSON schema keeps output small.
- **Using Nova 2 Lite instead of Nova Lite v1.** Nova 2 Lite is **$0.30/$2.50 per 1M** (5×/10× pricier) — stick with `amazon.nova-lite-v1:0` (the one your CLI confirmed) unless you need Nova 2's reasoning.

### Recommended $0 safety nets (optional, free to set)
1. **AWS Budgets** — a $5 monthly budget with an email alert at 50%/80%/100% (first 2 budgets are free).
2. **Billing alarm** in CloudWatch at $1.
3. **Bedrock model-invocation logging off** (or 7-day retention) to keep CloudWatch free-tier.

---

## Teardown on day 30 (stops all billing)

```bash
sam delete --stack-name reliefroute --region us-east-1
```

This deletes the Lambda, the DynamoDB table (and its data), the log group, and the Function URL — **every billable resource in the stack**. After deletion, ongoing cost is **$0**. (No S3 bucket to empty, no CloudFront distribution to disable, no detached EBS/EIP left behind — the minimal footprint makes teardown clean and complete in one command.)

**Post-teardown verification (optional):**
```bash
aws cloudformation describe-stacks --stack-name reliefroute --region us-east-1
# Expect: "Stack with id reliefroute does not exist" = fully gone.
```

> The SAM deploy bucket (`--resolve-s3` creates a small `aws-sam-cli-managed-*` bucket for build artifacts) is **not** removed by `sam delete` and holds a few MB. It costs fractions of a cent/month; delete it manually if you want a truly zero footprint.
