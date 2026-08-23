"""Dated snapshot of the Data Safety question-ID vocabulary. Warnings only.

Google adds and renames these ids without notice and does not publish the
catalogue, so this list is *always* somewhat behind. It exists to say "this one
is new, look at it" — nothing here can stop a write, and nothing here should
ever be treated as the set of valid ids.

Composite ids (``PSL_DATA_USAGE_RESPONSES:PSL_NAME:DATA_USAGE_COLLECTION_PURPOSE``)
are checked segment by segment, so a new data type under a known family is still
surfaced instead of being hidden by its familiar prefix.

Refreshing it: export a fresh CSV from Play Console, run
``preflight_data_safety_csv`` over it, and add whatever ``unknown_question_ids``
reports to ``KNOWN_ID_TOKENS``, bumping ``SNAPSHOT_DATE`` in the same commit.
"""

from __future__ import annotations

#: When these tokens were last confirmed against real exports.
SNAPSHOT_DATE = "2026-08-24"
#: Where they came from, so a maintainer can reproduce the list. Built from the
#: union of six Data Safety CSVs exported by real published apps between 2022
#: and 2026, plus a blank export template, rather than from documentation --
#: the Help Center does not publish the vocabulary.
SNAPSHOT_SOURCE = (
    "union of six real Play Console exports (2022-2026) and the "
    "fastlane-plugin-google_data_safety (MIT) blank template; see "
    "LICENSES/owenbean400-fastlane-plugin-google_data_safety-MIT.txt"
)
#: True while the snapshot is known to cover only part of the vocabulary. It
#: softens the warning text from "unknown" to "not in this partial snapshot".
SNAPSHOT_PARTIAL = True

#: Every colon-separated segment seen across those exports.
KNOWN_ID_TOKENS: frozenset[str] = frozenset(
    {
        "DATA_USAGE_COLLECTION_PURPOSE",
        "DATA_USAGE_SHARING_PURPOSE",
        "DATA_USAGE_USER_CONTROL",
        "PSL_ACCOUNT_DELETION_URL",
        "PSL_ACCOUNT_MANAGEMENT",
        "PSL_ACM_NONE",
        "PSL_ACM_OAUTH",
        "PSL_ACM_OTHER",
        "PSL_ACM_SPECIFY",
        "PSL_ACM_USER_ID_OTHER_AUTH",
        "PSL_ACM_USER_ID_PASSWORD",
        "PSL_ACM_USER_ID_PASSWORD_OTHER_AUTH",
        "PSL_ADDRESS",
        "PSL_ADVERTISING",
        "PSL_ANALYTICS",
        "PSL_APPROX_LOCATION",
        "PSL_APPS_ON_DEVICE",
        "PSL_APP_FUNCTIONALITY",
        "PSL_AUDIO",
        "PSL_CALENDAR",
        "PSL_CONTACTS",
        "PSL_CRASH_LOGS",
        "PSL_CREDIT_DEBIT_BANK_ACCOUNT_NUMBER",
        "PSL_CREDIT_SCORE",
        "PSL_DATA_COLLECTION_COLLECTS_PERSONAL_DATA",
        "PSL_DATA_COLLECTION_COMPLIES_FAMILY_POLICY",
        "PSL_DATA_COLLECTION_ENCRYPTED_IN_TRANSIT",
        "PSL_DATA_COLLECTION_USER_REQUEST_DELETE",
        "PSL_DATA_DELETION_URL",
        "PSL_DATA_TYPES_APP_ACTIVITY",
        "PSL_DATA_TYPES_APP_PERFORMANCE",
        "PSL_DATA_TYPES_AUDIO",
        "PSL_DATA_TYPES_CALENDAR",
        "PSL_DATA_TYPES_CONTACTS",
        "PSL_DATA_TYPES_EMAIL_AND_TEXT",
        "PSL_DATA_TYPES_FILES_AND_DOCS",
        "PSL_DATA_TYPES_FINANCIAL",
        "PSL_DATA_TYPES_HEALTH_AND_FITNESS",
        "PSL_DATA_TYPES_IDENTIFIERS",
        "PSL_DATA_TYPES_LOCATION",
        "PSL_DATA_TYPES_PERSONAL",
        "PSL_DATA_TYPES_PHOTOS_AND_VIDEOS",
        "PSL_DATA_TYPES_SEARCH_AND_BROWSING",
        "PSL_DATA_USAGE_COLLECTION_AND_SHARING",
        "PSL_DATA_USAGE_EPHEMERAL",
        "PSL_DATA_USAGE_ONLY_COLLECTED",
        "PSL_DATA_USAGE_ONLY_SHARED",
        "PSL_DATA_USAGE_RESPONSES",
        "PSL_DATA_USAGE_USER_CONTROL_OPTIONAL",
        "PSL_DATA_USAGE_USER_CONTROL_REQUIRED",
        "PSL_DEVELOPER_COMMUNICATIONS",
        "PSL_DEVICE_ID",
        "PSL_EMAIL",
        "PSL_EMAILS",
        "PSL_FILES_AND_DOCS",
        "PSL_FITNESS",
        "PSL_FRAUD_PREVENTION_SECURITY",
        "PSL_HAS_OUTSIDE_APP_ACCOUNTS",
        "PSL_HEALTH",
        "PSL_INDEPENDENTLY_VALIDATED",
        "PSL_IN_APP_SEARCH_HISTORY",
        "PSL_LOGIN_THROUGH_EMPLOYMENT_OR_ENTERPRISE_ACCOUNT",
        "PSL_LOGIN_WITH_OUTSIDE_APP_ID",
        "PSL_MUSIC",
        "PSL_NAME",
        "PSL_OTHER",
        "PSL_OTHER_APP_ACTIVITY",
        "PSL_OTHER_AUDIO",
        "PSL_OTHER_MESSAGES",
        "PSL_OTHER_PERFORMANCE",
        "PSL_OTHER_PERSONAL",
        "PSL_OUTSIDE_APP_ACCOUNT_TYPES",
        "PSL_OUTSIDE_APP_ACCOUNT_TYPE_OTHER",
        "PSL_OUTSIDE_APP_ACCOUNT_TYPE_SPECIFY",
        "PSL_PERFORMANCE_DIAGNOSTICS",
        "PSL_PERSONALIZATION",
        "PSL_PHONE",
        "PSL_PHOTOS",
        "PSL_POLITICAL_RELIGIOUS",
        "PSL_PRECISE_LOCATION",
        "PSL_PURCHASE_HISTORY",
        "PSL_RACE_ETHNICITY",
        "PSL_SEXUAL_ORIENTATION_GENDER_IDENTITY",
        "PSL_SMS_CALL_LOG",
        "PSL_SUPPORTED_ACCOUNT_CREATION_METHODS",
        "PSL_SUPPORT_DATA_DELETION_BY_USER",
        "PSL_UPI_BADGE_OPT_IN",
        "PSL_USER_ACCOUNT",
        "PSL_USER_GENERATED_CONTENT",
        "PSL_USER_INTERACTION",
        "PSL_VIDEOS",
        "PSL_WEB_BROWSING_HISTORY",
    }
)
