# App vitals (Play Developer Reporting API)

Nine read-only tools over the Play Developer Reporting API v1beta1 — crash rate,
ANR rate, performance and power vitals, grouped error issues, raw error reports
and anomalies.

This is a **second Google API** with its own OAuth scope. The Publisher API tools
answer "did the release ship?"; these answer "is it healthy?".

## Access

Add `https://www.googleapis.com/auth/playdeveloperreporting` to the service
account. Play Console grants reporting access **independently** of publishing
access, so a service account that deploys successfully can still be refused here
with a 403. Grant it under **Play Console → Users and permissions**; permission
changes can take up to 24 hours to propagate.

No extra configuration is needed — the same `GOOGLE_APPLICATION_CREDENTIALS` or
`GOOGLE_PLAY_STORE_CREDENTIALS` key is reused, scoped differently.

## Read freshness before you read numbers

Play vitals lag reality by hours to days. A query for "the last 7 days" that
silently returns five is the most likely way to reach a wrong conclusion from
this API — the missing days look like an improvement rather than a gap.

```
get_vitals_freshness(package_name="com.example.app")
```

Returns `latest_end_time` per metric set and aggregation period. Check it before
acting on a rate.

## Metric sets

Tools take a short `metric_set` key, not a resource name. `list_metric_sets`
returns the current list.

| Key | Play resource | Measures |
|---|---|---|
| `crash_rate` | `crashRateMetricSet` | Crash rate |
| `anr_rate` | `anrRateMetricSet` | Application Not Responding rate |
| `slow_start_rate` | `slowStartRateMetricSet` | Slow cold/warm/hot start |
| `slow_rendering_rate` | `slowRenderingRateMetricSet` | Janky frames |
| `excessive_wakeup_rate` | `excessiveWakeupRateMetricSet` | Excessive background wake-ups |
| `stuck_background_wakelock_rate` | `stuckBackgroundWakelockRateMetricSet` | Stuck background wakelocks |
| `lmk_rate` | `lmkRateMetricSet` | Low Memory Killer rate |
| `error_counts` | `errorCountMetricSet` | Grouped crash and ANR counts |

## Tools

| Tool | Purpose |
|---|---|
| `list_reporting_apps` | Enumerate every app the service account can see. The Publisher API has no "list my apps" endpoint, so this is the only way to walk a portfolio. |
| `list_metric_sets` | The metric-set keys this server accepts. |
| `get_vitals_freshness` | How current each metric set is. Call before trusting a number. |
| `get_metric_set_metadata` | Metadata for one metric set, including freshness. |
| `query_vitals` | Query a metric-set timeline with dimensions, metrics and filters. |
| `get_release_filter_options` | Tracks and version codes usable in a `filter_expression`. |
| `list_vitals_anomalies` | Statistical anomalies Play detected across the metric sets. |
| `search_error_issues` | Grouped error issues — clusters of similar crashes or ANRs. |
| `search_error_reports` | Individual crashes and ANRs, with stack traces. |

Every tool here is a read, so none is blocked by `GOOGLE_PLAY_MCP_READ_ONLY`.

## Querying a timeline

```
query_vitals(
    package_name="com.example.app",
    metric_set="crash_rate",
    metrics=["crashRate", "distinctUsers"],
    dimensions=["versionCode", "countryCode"],
    days_back=14,
    aggregation_period="DAILY",
    filter_expression="versionCode = 42",
    user_cohort="OS_PUBLIC",
)
```

- `aggregation_period`: `HOURLY`, `DAILY` or `FULL_RANGE`
- `user_cohort`: `OS_PUBLIC`, `OS_BETA` or `APP_TESTERS`
- `days_back` defines a window ending now; rows come back with `start_time`,
  a `dimensions` map and a `metrics` map
- Paging is handled internally up to `max_results`; if the API had more, the
  result carries `next_page_token`

Omitting `metrics` returns whatever the API considers the default set for that
metric set. Use `get_metric_set_metadata` to see what a set supports.

## Triaging a bad release

```
1. get_vitals_freshness      — is the data current enough to judge?
2. query_vitals              — crash_rate sliced by versionCode
3. search_error_issues       — order_by "errorReportCount desc"
4. search_error_reports      — filter to the worst issue, read the stack trace
5. halt_release / update_rollout  — Publisher API tools, act on the finding
```

## Implementation notes

- Credential resolution, retry/backoff and the transport lock are shared with
  the Publisher client — the Reporting surface is a mixin on `PlayStoreClient`,
  not a separate client.
- Timestamps arrive in two shapes: `google.type.DateTime` objects for timeline
  buckets and RFC3339 strings for freshness and event times. Both are parsed.
- Several fields arrive either as a scalar or as a small wrapper object
  (`{"versionCode": 42}`, `{"marketingName": "Pixel 8"}`) depending on the
  endpoint. They are normalised to scalars.
- `get_vitals_freshness` across all metric sets **skips** a set the service
  account cannot read rather than failing the whole batch.
