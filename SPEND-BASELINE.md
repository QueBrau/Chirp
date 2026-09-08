# Spending baseline and planning report

Chirps' current planning target is **$125 USD per calendar month**, declared in
[`infra/spend-policy.json`](infra/spend-policy.json). This is a planning target,
not a measured bill or an enforced cap. The earlier dated estimates remain in the
audit history; this tool does not validate or replace them with observed spend.

The c371 implementation supplies offline arithmetic and a repeatable reporting
format. Actual billing reconciliation, existing-control discovery, delivered budget
alerts, anomaly monitoring and a reviewed retention proposal remain open acceptance.
No cloud access, alert creation, resource deletion or billing change occurs here.

## Generate a private report

Requires Python 3.11 or later. From the repository root:

```sh
scripts/spend-report \
  --input infra/examples/spend-synthetic.json \
  --report /private/tmp/chirps-synthetic-spend-report.json
```

The committed input is **synthetic test data**, with no account identifiers or live
billing figures. Its supplied gross is $40.005, credits are -$5.005 and adjustments
are $1, producing $36 net for August 1–7. Its explicitly assumed linear August
forecast is $159.43. These numbers describe the example only.

Use a new output path for every run. The CLI creates a file with mode `0600` and
refuses existing output paths and final symlinks. Input and policy paths must be
regular files and cannot be final symlinks; input JSON is limited to 64 KiB and
eight nesting levels. An interrupted write may leave an incomplete private file;
inspect that artifact separately and retry with a fresh path. The tool does not
delete a pathname after a write failure or promise crash-durable publication.

Success prints only `REPORT_WRITTEN` with false provider-verification and notification
flags. Failure exits 2 with `REPORT_NOT_WRITTEN` and a fixed error label. Amounts,
identifiers, file paths and exception details are not echoed. Store your real input
and reports outside the repository in an access-controlled directory; do not commit
them. The tool does not change permissions on an input file.

## Supplied data contract

The example is the executable schema reference. Required fields are `version: 1`,
`basis`, `currency: "USD"`, `period` and `amounts`. Unknown fields, duplicate JSON
keys and duplicate categories are rejected. Each report has one basis and currency;
combine multiple accounts/providers into nonoverlapping category subtotals before
input, with private reconciliation records retained separately.

| Field | Meaning and boundary |
| --- | --- |
| `basis: "reported"` | Operator-supplied reported costs. This label does not authenticate a provider export or establish completeness. |
| `basis: "estimated"` | Operator-supplied estimate for a whole calendar month, including a future month if desired. This is never labeled observed spend. |
| `period: {start, end}` | Full `YYYY-MM-DD` dates, both inclusive, beginning on day 1 and staying within one month. Reported coverage must end before today's UTC date; estimates must end on the last day of the month. |
| `amounts` | One to fourteen unique category entries, each containing `category`, nonnegative `gross` and nonpositive `credits`. Amounts are decimal strings, with at most 12 whole and 9 fractional digits; no floats, scientific notation or nonfinite values. |
| `adjustments` | Optional signed decimal amount for taxes, refunds or corrections omitted from category gross/credits. Include each item once. Omission means zero was supplied, not that the invoice has no adjustments. |
| `forecast` | Optional for reported data only, using the explicit assumptions below. Never inferred from partial coverage. |
| `denominators` | Optional same-period usage totals, with valid units and positive values. Availability rules are below. |

Categories are `api_compute`, `ws_compute`, `cloud_sql`, `redis`, `networking`,
`media_storage`, `media_egress`, `identity`, `hosting`, `email`, `builds_artifacts`,
`logs_monitoring`, `provider_fees` and `other`. They are coarse operator mappings,
not verified provider SKUs. Missing categories are not proven free. Include costs
such as storage operations/soft-delete retention in the appropriate supplied
subtotal and keep gross charges distinct from credits/free allowances.

Use completed UTC days for coverage and align source data to that cut before
supplying it. The script does not convert a provider's reporting timezone, reconcile
invoice-month boundaries or detect delayed postings. Even a whole-month report is
only a supplied component subtotal: it does not prove every SKU, tax, provider,
credit or late adjustment was included. Review credits and recurring gross
separately when assessing the sustainable cost after allowances expire.

Arithmetic retains exact decimal sums for gross, credits, adjustments and net:
`net = gross + credits + adjustments`. A local precision of 50 digits accommodates
all allowed category totals and denominators. Only presentation fields are rounded
half up; exact net controls threshold comparisons. Negative net remains negative.

## Review flags and forecast assumptions

The source policy sets review thresholds at 50%, 80% and 100% of $125 ($62.50, $100
and $125), and a linear forecast review threshold of 100%. These are **local report
flags**, not configured notifications. Reaching a threshold includes equality.

For reported data, the comparison basis is `supplied_period_net`; an early-month
subtotal below a threshold does not prove monthly compliance. Estimates use
`supplied_month_estimate_net`. Every report explicitly leaves
`monthly_compliance_proven`, `provider_verified`, `billing_completeness_verified`
and `notifications_configured` false.

A forecast requires all three input fields:

```json
{
  "method": "linear_calendar_days",
  "assumes_complete_covered_days": true,
  "assumes_daily_net_rate_continues": true
}
```

The formula is `supplied_net * days_in_month / covered_days`, with leap years and
inclusive dates respected. Its comparison basis is `assumed_linear_monthly_net`.
This assumption also extrapolates credits and adjustments at the same daily rate;
one-off credits, fixed monthly charges or usage growth can make it misleading.
The script makes that limitation explicit and does not silently adjust the forecast.
Use a separate whole-month estimate with reviewed components when a linear rate
does not represent the planned workload. No growth curve or provider price is
invented by the tool.

## Optional usage allocations

Each denominator declares `metric`, `unit`, decimal-string `value` and its own
inclusive `period`. Supported pairs are `accepted_writes`/`count`,
`egress_bytes`/`bytes` and `socket_hours`/`hours`. Values allow at most 18 whole and
9 fractional digits; writes and bytes must be positive integers. Socket hours
may be fractional. The operator must establish consistent measurement semantics,
population and coverage before supplying these totals.

Period mismatch, incorrect unit, zero/negative values or fractional counts yield
an unavailable allocation with a fixed reason; they never produce a unit cost.
Malformed numeric strings, unknown metrics or duplicate metrics reject the input.
Valid allocations divide the supplied net subtotal by the supplied usage total
and round to eight decimal places. A displayed zero can mean a smaller positive
amount. These are allocated costs across the selected components, not a marginal
provider price or a verified efficiency metric.

DAU, MAU and retained active campus denominators are intentionally absent until
their product definitions, measurement source and matching period are agreed.
Identity-provider MAUs must not be substituted for product DAU. c373 owns the
analytics definitions; c371 can later consume reviewed aggregate measurements.

## Remaining operational acceptance

Before c371 closes, the billing owner should reconcile an actual period against
Billing reports/invoice SKUs, preserving gross/credits/adjustments and missing
components. Review existing account budgets and export locations before adding
controls. Permission to view billing, API availability, absence of existing budgets
and usable export coverage are separate facts; this offline tool proves none of
them. Google's [budget API setup](https://docs.cloud.google.com/billing/docs/how-to/budget-api)
and [export setup](https://docs.cloud.google.com/billing/docs/how-to/export-data-bigquery-setup)
describe the account/API/export prerequisites.

Reuse or configure scoped 50/80/100% and forecast alerts, define a daily-anomaly
comparison and owner, and verify recipient delivery through an approved test.
[Cloud Billing budgets](https://docs.cloud.google.com/billing/docs/how-to/budgets)
do not automatically cap spending. No billing disablement or spend-cap decision
is part of this change. Any later spend-cap proposal needs a separate review of
then-current service coverage, enforcement lag and outage tradeoffs.

Review artifact/log/storage retention from inventory, preserve serving and rollback
images and required security evidence, and propose measured savings before any
cleanup. Reconcile the baseline monthly and after material usage, pricing or
topology changes. Existing SES, networking and analytics lanes retain their scope;
their future costs and measurements can be supplied without blocking today's
offline planning workflow.

## Local validation

`backend/tests/test_c371_spend_report.py` executes arithmetic, calendar and schema
boundaries plus the real CLI, private-file permissions, input FIFO rejection,
existing-file/symlink preservation and interrupted-write behavior. These tests use
synthetic inputs and require no database or cloud access.
