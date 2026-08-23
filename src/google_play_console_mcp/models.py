"""Pydantic models for Google Play Console MCP Server."""

from __future__ import annotations

from datetime import date, datetime  # noqa: TC003 - Pydantic needs these at runtime
from typing import Any

from pydantic import BaseModel, Field


class Release(BaseModel):
    """Represents an app release on a track."""

    package_name: str = Field(..., description="App package name")
    track: str = Field(..., description="Release track")
    status: str = Field(..., description="Release status")
    version_codes: list[int] = Field(default_factory=list, description="Version codes in release")
    version_name: str | None = Field(None, description="Version name")
    rollout_percentage: float = Field(100.0, description="Rollout percentage (0-100)")
    release_notes: dict[str, str] = Field(
        default_factory=dict, description="Release notes by language"
    )


class TrackInfo(BaseModel):
    """Information about a release track."""

    track: str = Field(..., description="Track name")
    releases: list[Release] = Field(default_factory=list, description="Releases on this track")


class DeploymentResult(BaseModel):
    """Result of a deployment operation."""

    success: bool = Field(..., description="Whether deployment succeeded")
    edit_id: str | None = Field(None, description="Edit ID for the operation")
    package_name: str = Field(..., description="App package name")
    track: str = Field(..., description="Target track")
    version_code: int | None = Field(None, description="Deployed version code")
    message: str = Field(..., description="Status message")
    error: str | None = Field(None, description="Error details if failed")


class AppDetails(BaseModel):
    """Detailed app information."""

    package_name: str = Field(..., description="App package name")
    title: str | None = Field(None, description="App title")
    short_description: str | None = Field(None, description="Short description")
    full_description: str | None = Field(None, description="Full description")
    default_language: str | None = Field(None, description="Default language")
    developer_name: str | None = Field(None, description="Developer name")
    developer_email: str | None = Field(None, description="Developer email")
    developer_website: str | None = Field(None, description="Developer website")


class Review(BaseModel):
    """User review."""

    review_id: str = Field(..., description="Review ID")
    author_name: str = Field(..., description="Author name")
    star_rating: int = Field(..., description="Star rating (1-5)")
    comment: str = Field(..., description="Review comment")
    language: str = Field(..., description="Review language")
    device: str | None = Field(None, description="Device name")
    android_version: str | None = Field(None, description="Android OS version")
    app_version_code: int | None = Field(None, description="App version code")
    app_version_name: str | None = Field(None, description="App version name")
    last_modified: datetime | None = Field(None, description="Last modification time")
    developer_reply: str | None = Field(None, description="Developer reply if present")
    developer_reply_time: datetime | None = Field(None, description="Reply time")


class ReviewReplyResult(BaseModel):
    """Result of replying to a review."""

    success: bool = Field(..., description="Whether reply succeeded")
    review_id: str = Field(..., description="Review ID")
    message: str = Field(..., description="Status message")
    error: str | None = Field(None, description="Error details if failed")


class SubscriptionProduct(BaseModel):
    """Subscription product definition."""

    product_id: str = Field(..., description="Subscription product ID")
    package_name: str = Field(..., description="App package name")
    status: str | None = Field(None, description="Subscription status")
    base_plans: list[dict[str, Any]] = Field(
        default_factory=list, description="Base plan definitions"
    )


class SubscriptionPurchase(BaseModel):
    """Subscription purchase status."""

    package_name: str = Field(..., description="App package name")
    subscription_id: str = Field(..., description="Subscription product ID")
    purchase_token: str = Field(..., description="Purchase token")
    order_id: str | None = Field(None, description="Order ID")
    start_time: datetime | None = Field(None, description="Subscription start time")
    expiry_time: datetime | None = Field(None, description="Subscription expiry time")
    auto_renewing: bool = Field(False, description="Whether auto-renewing")
    cancel_reason: int | None = Field(None, description="Cancellation reason code")
    payment_state: int | None = Field(None, description="Payment state code")
    price_currency: str | None = Field(None, description="Price currency code")
    price_amount_micros: int | None = Field(None, description="Price amount in micros")


class VoidedPurchase(BaseModel):
    """Voided purchase record."""

    package_name: str = Field(..., description="App package name")
    purchase_token: str = Field(..., description="Original purchase token")
    order_id: str | None = Field(None, description="Order ID")
    voided_time: datetime | None = Field(None, description="Time of voiding")
    voided_reason: int | None = Field(None, description="Reason for voiding")
    voided_source: int | None = Field(None, description="Source of voiding")


class InAppProduct(BaseModel):
    """In-app product definition."""

    sku: str = Field(..., description="Product SKU")
    package_name: str = Field(..., description="App package name")
    product_type: str = Field(..., description="Product type (managed_product or subscription)")
    status: str | None = Field(None, description="Product status")
    default_language: str | None = Field(None, description="Default language")
    title: str | None = Field(None, description="Product title")
    description: str | None = Field(None, description="Product description")
    default_price: dict[str, Any] | None = Field(None, description="Default price information")


class InAppProductActionResult(BaseModel):
    """Result of a delete/batch-delete action on in-app products."""

    success: bool = Field(..., description="Whether the action succeeded")
    package_name: str = Field(..., description="App package name")
    sku: str | None = Field(None, description="Product SKU (None for batch operations)")
    message: str = Field(..., description="Status message")
    error: str | None = Field(None, description="Error details if failed")


class Listing(BaseModel):
    """Store listing for a specific language."""

    language: str = Field(..., description="Language code (e.g., en-US)")
    title: str | None = Field(None, description="App title")
    full_description: str | None = Field(None, description="Full description")
    short_description: str | None = Field(None, description="Short description")
    video: str | None = Field(None, description="YouTube video URL")


class ListingUpdateResult(BaseModel):
    """Result of updating a store listing."""

    success: bool = Field(..., description="Whether update succeeded")
    package_name: str = Field(..., description="App package name")
    language: str = Field(..., description="Language code")
    message: str = Field(..., description="Status message")
    error: str | None = Field(None, description="Error details if failed")


class TesterInfo(BaseModel):
    """Information about testers for a track."""

    track: str = Field(..., description="Track name")
    google_groups: list[str] = Field(
        default_factory=list, description="List of Google Group email addresses"
    )


class OrderLineItem(BaseModel):
    """A single line item within an order."""

    product_id: str | None = Field(None, description="Purchased product ID or in-app SKU")
    product_title: str | None = Field(None, description="Developer-specified product name")


class Order(BaseModel):
    """Order/transaction information (Android Publisher v3 Order resource).

    Product IDs live in ``line_items`` and the order status is the string enum
    ``state``; the v3 Order resource has no top-level product ID or numeric
    purchase state.
    """

    order_id: str = Field(..., description="Order ID")
    package_name: str = Field(..., description="App package name")
    state: str | None = Field(
        None,
        description="Order state (e.g. PENDING, PROCESSED, CANCELED, PARTIALLY_REFUNDED, REFUNDED)",
    )
    line_items: list[OrderLineItem] = Field(
        default_factory=list, description="Line items making up the order"
    )
    product_ids: list[str] = Field(
        default_factory=list, description="Product IDs across all line items"
    )
    purchase_token: str | None = Field(None, description="Purchase token")
    create_time: datetime | None = Field(None, description="Time the order was created")


class ExpansionFile(BaseModel):
    """APK expansion file information."""

    version_code: int = Field(..., description="Version code")
    expansion_file_type: str = Field(..., description="Expansion file type (main or patch)")
    file_size: int | None = Field(None, description="File size in bytes")
    references_version: int | None = Field(None, description="Referenced version code")


class Apk(BaseModel):
    """An APK belonging to an edit."""

    package_name: str = Field(..., description="App package name")
    version_code: int = Field(..., description="Version code from the manifest")
    sha1: str | None = Field(None, description="SHA1 hash of the APK payload")
    sha256: str | None = Field(None, description="SHA256 hash of the APK payload")


class Bundle(BaseModel):
    """An Android App Bundle (.aab) belonging to an edit."""

    package_name: str = Field(..., description="App package name")
    version_code: int = Field(..., description="Version code from the base module manifest")
    sha1: str | None = Field(None, description="SHA1 hash of the upload payload")
    sha256: str | None = Field(None, description="SHA256 hash of the upload payload")


class DeobfuscationFile(BaseModel):
    """A deobfuscation (mapping/symbol) file uploaded for an APK version."""

    package_name: str = Field(..., description="App package name")
    version_code: int = Field(..., description="APK version code the file applies to")
    symbol_type: str | None = Field(
        None, description="Deobfuscation file type (proguard or nativeCode)"
    )


class AppImage(BaseModel):
    """A store-listing image belonging to an edit (edits.images resource)."""

    package_name: str = Field(..., description="App package name")
    language: str = Field(..., description="Language localization code (BCP-47 tag)")
    image_type: str = Field(
        ..., description="Image type (e.g. phoneScreenshots, icon, featureGraphic)"
    )
    image_id: str | None = Field(None, description="Unique image identifier")
    url: str | None = Field(None, description="URL where the image is served")
    sha1: str | None = Field(None, description="SHA1 hash of the image content")
    sha256: str | None = Field(None, description="SHA256 hash of the image content")


class ImageDeleteResult(BaseModel):
    """Result of deleting one or all store-listing images."""

    success: bool = Field(..., description="Whether the delete action succeeded")
    package_name: str = Field(..., description="App package name")
    language: str = Field(..., description="Language localization code (BCP-47 tag)")
    image_type: str = Field(..., description="Image type the deletion applied to")
    deleted_count: int = Field(0, description="Number of images deleted")
    message: str = Field(..., description="Status message")
    error: str | None = Field(None, description="Error details if failed")


class BatchDeploymentResult(BaseModel):
    """Result of batch deployment to multiple tracks."""

    success: bool = Field(..., description="Whether all deployments succeeded")
    results: list[DeploymentResult] = Field(
        default_factory=list, description="Individual deployment results"
    )
    successful_count: int = Field(0, description="Number of successful deployments")
    failed_count: int = Field(0, description="Number of failed deployments")
    message: str = Field(..., description="Overall status message")


class ProductPurchase(BaseModel):
    """Status of an in-app product purchase."""

    package_name: str = Field(..., description="App package name")
    product_id: str = Field(..., description="In-app product SKU")
    purchase_token: str = Field(..., description="Purchase token")
    order_id: str | None = Field(None, description="Order ID")
    purchase_state: int | None = Field(
        None, description="Purchase state (0=purchased, 1=canceled, 2=pending)"
    )
    consumption_state: int | None = Field(
        None, description="Consumption state (0=yet to be consumed, 1=consumed)"
    )
    acknowledgement_state: int | None = Field(
        None, description="Acknowledgement state (0=not acknowledged, 1=acknowledged)"
    )
    purchase_time: datetime | None = Field(None, description="Purchase time")
    purchase_type: int | None = Field(
        None, description="Purchase type (0=test, 1=promo, 2=rewarded)"
    )
    quantity: int | None = Field(None, description="Quantity purchased")
    region_code: str | None = Field(None, description="Billing region code")
    developer_payload: str | None = Field(None, description="Developer-supplied payload")


class ProductPurchaseActionResult(BaseModel):
    """Result of an acknowledge/consume action on an in-app product purchase."""

    success: bool = Field(..., description="Whether the action succeeded")
    package_name: str = Field(..., description="App package name")
    product_id: str = Field(..., description="In-app product SKU")
    purchase_token: str = Field(..., description="Purchase token")
    action: str = Field(..., description="Action performed (acknowledge or consume)")
    message: str = Field(..., description="Status message")
    error: str | None = Field(None, description="Error details if failed")


class ValidationResult(BaseModel):
    """Validation result details."""

    field: str = Field(..., description="Field that failed validation")
    message: str = Field(..., description="Error message")
    value: Any | None = Field(None, description="Invalid value")


class OrderRefundResult(BaseModel):
    """Result of refunding an order."""

    success: bool = Field(..., description="Whether the refund succeeded")
    package_name: str = Field(..., description="App package name")
    order_id: str = Field(..., description="Order ID")
    revoked: bool = Field(..., description="Whether the entitlement was also revoked")
    message: str = Field(..., description="Status message")
    error: str | None = Field(None, description="Error details if failed")


class SubscriptionActionResult(BaseModel):
    """Result of a cancel/defer/revoke action on a subscription purchase."""

    success: bool = Field(..., description="Whether the action succeeded")
    package_name: str = Field(..., description="App package name")
    purchase_token: str = Field(..., description="Purchase token")
    action: str = Field(..., description="Action performed (cancel, defer, or revoke)")
    message: str = Field(..., description="Status message")
    details: dict[str, Any] | None = Field(
        None, description="Extra result data (e.g. defer expiry)"
    )
    error: str | None = Field(None, description="Error details if failed")


class SubscriptionCatalogResult(BaseModel):
    """Result of a delete action on a subscription catalog product."""

    success: bool = Field(..., description="Whether the action succeeded")
    package_name: str = Field(..., description="App package name")
    product_id: str | None = Field(
        None, description="Subscription product ID (None for batch operations)"
    )
    message: str = Field(..., description="Status message")
    error: str | None = Field(None, description="Error details if failed")


class SubscriptionOffer(BaseModel):
    """Subscription offer definition (basePlans.offers resource)."""

    package_name: str = Field(..., description="App package name")
    product_id: str = Field(..., description="Parent subscription product ID")
    base_plan_id: str = Field(..., description="Parent base plan ID")
    offer_id: str = Field(..., description="Subscription offer ID")
    state: str | None = Field(None, description="Offer state (e.g. DRAFT, ACTIVE, INACTIVE)")
    offer_tags: list[str] = Field(default_factory=list, description="Offer tag strings")
    phases: list[dict[str, Any]] = Field(
        default_factory=list, description="Offer phase definitions"
    )
    regions_version: str | None = Field(None, description="Regions catalog version")


class OneTimeProduct(BaseModel):
    """One-time product definition (monetization.oneTimeProducts resource)."""

    product_id: str = Field(..., description="One-time product ID")
    package_name: str = Field(..., description="App package name")
    listings: list[dict[str, Any]] = Field(
        default_factory=list, description="Store listing definitions"
    )
    purchase_options: list[dict[str, Any]] = Field(
        default_factory=list, description="Purchase option definitions"
    )
    offer_tags: list[dict[str, Any]] = Field(
        default_factory=list, description="Offer tag definitions"
    )
    restricted_payment_countries: dict[str, Any] | None = Field(
        None, description="Restricted payment countries configuration"
    )


class OneTimeProductActionResult(BaseModel):
    """Result of a delete/batch-delete action on a one-time product catalog resource."""

    success: bool = Field(..., description="Whether the action succeeded")
    package_name: str = Field(..., description="App package name")
    product_id: str | None = Field(
        None, description="One-time product ID (None for batch operations)"
    )
    message: str = Field(..., description="Status message")
    error: str | None = Field(None, description="Error details if failed")


class OneTimeProductOffer(BaseModel):
    """One-time product offer definition (purchaseOptions.offers resource)."""

    package_name: str = Field(..., description="App package name")
    product_id: str = Field(..., description="Parent one-time product ID")
    purchase_option_id: str = Field(..., description="Parent purchase option ID")
    offer_id: str = Field(..., description="One-time product offer ID")
    state: str | None = Field(None, description="Offer state (e.g. DRAFT, ACTIVE, INACTIVE)")
    offer_tags: list[str] = Field(default_factory=list, description="Offer tag strings")
    regions_version: str | None = Field(None, description="Regions catalog version")


class ProductPurchaseV2(BaseModel):
    """Status of an in-app product purchase (Purchases.productsv2)."""

    package_name: str = Field(..., description="App package name")
    purchase_token: str = Field(..., description="Purchase token")
    order_id: str | None = Field(None, description="Order ID")
    acknowledgement_state: str | None = Field(None, description="Acknowledgement state (enum)")
    purchase_completion_time: str | None = Field(
        None, description="Purchase completion time (RFC3339)"
    )
    region_code: str | None = Field(None, description="Billing region code")
    product_line_items: list[dict[str, Any]] = Field(
        default_factory=list, description="Purchased product line items"
    )
    obfuscated_external_account_id: str | None = Field(
        None, description="Obfuscated external account ID"
    )
    obfuscated_external_profile_id: str | None = Field(
        None, description="Obfuscated external profile ID"
    )
    test_purchase: bool = Field(False, description="Whether this is a test purchase")


class ExternalTransaction(BaseModel):
    """External (alternative billing) transaction (externaltransactions resource)."""

    package_name: str = Field(..., description="App package name")
    external_transaction_id: str = Field(..., description="External transaction ID")
    transaction_state: str | None = Field(None, description="Current transaction state")
    create_time: str | None = Field(None, description="Time the transaction was created (RFC3339)")
    current_pre_tax_amount: dict[str, Any] | None = Field(
        None, description="Current transaction amount before tax (Price)"
    )
    original_pre_tax_amount: dict[str, Any] | None = Field(
        None, description="Original transaction amount before tax (Price)"
    )
    test_purchase: bool = Field(False, description="Whether this is a test purchase")


class DeviceTierConfig(BaseModel):
    """Device tier config (applications.deviceTierConfigs resource)."""

    package_name: str = Field(..., description="App package name")
    device_tier_config_id: str | None = Field(None, description="Device tier config ID")
    device_groups: list[dict[str, Any]] = Field(
        default_factory=list, description="Device group definitions"
    )
    device_tier_set: dict[str, Any] | None = Field(
        None, description="Set of device tiers for the app"
    )
    user_country_sets: list[dict[str, Any]] = Field(
        default_factory=list, description="User country set definitions"
    )


class User(BaseModel):
    """A Play Console user with account access (users resource)."""

    developer_id: str = Field(..., description="Developer account ID")
    email: str | None = Field(None, description="User's email address")
    access_state: str | None = Field(
        None, description="Current access state (e.g. INVITED, ACCESS_GRANTED)"
    )
    expiration_time: str | None = Field(
        None, description="Time the user's access expires (RFC3339)"
    )
    developer_account_permissions: list[str] = Field(
        default_factory=list, description="Account-wide permissions granted to the user"
    )


class Grant(BaseModel):
    """An app-level access grant for a user (grants resource)."""

    developer_id: str = Field(..., description="Developer account ID")
    email: str = Field(..., description="Email of the user the grant belongs to")
    package_name: str | None = Field(None, description="App package name the grant applies to")
    app_level_permissions: list[str] = Field(
        default_factory=list, description="App-level permissions granted to the user"
    )


class AccessResult(BaseModel):
    """Result of a user/grant write that returns an empty response (delete)."""

    success: bool = Field(..., description="Whether the operation succeeded")
    message: str = Field(..., description="Status message")
    error: str | None = Field(None, description="Error details if failed")


class DataSafetyResult(BaseModel):
    """Result of updating an app's data safety labels (applications.dataSafety)."""

    success: bool = Field(..., description="Whether the update succeeded")
    package_name: str = Field(..., description="App package name")
    message: str = Field(..., description="Status message")
    error: str | None = Field(None, description="Error details if failed")


class AppRecovery(BaseModel):
    """App recovery action (applications.appRecoveries resource)."""

    package_name: str = Field(..., description="App package name")
    app_recovery_id: str | None = Field(None, description="App recovery action ID")
    status: str | None = Field(None, description="Recovery action status")
    targeting: dict[str, Any] | None = Field(
        None, description="Targeting criteria for the recovery action"
    )
    create_time: str | None = Field(None, description="Time the recovery action was created")


class AppRecoveryResult(BaseModel):
    """Result of a deploy/cancel/add-targeting action on an app recovery action."""

    success: bool = Field(..., description="Whether the action succeeded")
    package_name: str = Field(..., description="App package name")
    app_recovery_id: str | None = Field(None, description="App recovery action ID")
    message: str = Field(..., description="Status message")
    error: str | None = Field(None, description="Error details if failed")


class GeneratedApksDownload(BaseModel):
    """A single downloadable generated APK (generatedapks resource)."""

    package_name: str = Field(..., description="App package name")
    version_code: int = Field(..., description="Bundle version code the APK was generated from")
    download_id: str = Field(..., description="Download ID identifying the generated APK")
    apk_type: str = Field(
        ...,
        description=(
            "Kind of generated APK: split, standalone, universal, asset_pack_slice, or recovery"
        ),
    )


class SystemApkVariant(BaseModel):
    """A system APK variant (systemapks.variants resource)."""

    package_name: str = Field(..., description="App package name")
    version_code: int = Field(..., description="Version code of the App Bundle")
    variant_id: int | None = Field(None, description="ID of the system APK variant")
    device_spec: dict[str, Any] | None = Field(
        None, description="Device spec used to generate the APK"
    )
    options: dict[str, Any] | None = Field(None, description="Options applied to the generated APK")


class DownloadResult(BaseModel):
    """Result of downloading a file to a local path."""

    success: bool = Field(..., description="Whether the download succeeded")
    destination_path: str = Field(..., description="Local path the file was written to")
    message: str = Field(..., description="Status message")
    error: str | None = Field(None, description="Error details if failed")


class InternalAppSharingArtifact(BaseModel):
    """An uploaded internal app sharing artifact (internalappsharingartifacts)."""

    package_name: str = Field(..., description="App package name")
    download_url: str | None = Field(None, description="Download URL for the uploaded artifact")
    certificate_fingerprint: str | None = Field(
        None, description="SHA-256 fingerprint of the signing certificate"
    )
    sha256: str | None = Field(None, description="SHA-256 hash of the artifact")


# =============================================================================
# Play Developer Reporting API v1beta1
# =============================================================================


class ReportingApp(BaseModel):
    """An app visible to the caller through the Reporting API (apps.search)."""

    name: str = Field(..., description="Resource name, e.g. apps/com.example")
    package_name: str = Field(..., description="App package name")
    display_name: str | None = Field(None, description="App display name in Play Console")


class MetricSetFreshness(BaseModel):
    """How current a metric set's data is, for one aggregation period.

    Play vitals data lags reality. Read this before drawing a conclusion from a
    query: a ``latest_end_time`` two days old means the most recent two days are
    simply absent, not healthy.
    """

    aggregation_period: str | None = Field(
        None, description="Aggregation period this freshness applies to (HOURLY, DAILY, FULL_RANGE)"
    )
    latest_end_time: datetime | None = Field(
        None, description="Latest timestamp for which data is available"
    )


class MetricSetInfo(BaseModel):
    """Metadata for a metric set: what can be queried, and how fresh it is."""

    name: str = Field(..., description="Metric set resource name")
    metric_set: str = Field(..., description="Short metric set key, e.g. crash_rate")
    package_name: str = Field(..., description="App package name")
    freshness: list[MetricSetFreshness] = Field(
        default_factory=list, description="Freshness per aggregation period"
    )


class MetricRow(BaseModel):
    """One row of a metric-set query result."""

    start_time: datetime | None = Field(None, description="Start of the aggregation bucket")
    dimensions: dict[str, Any] = Field(
        default_factory=dict, description="Dimension values for this row, keyed by dimension name"
    )
    metrics: dict[str, Any] = Field(
        default_factory=dict, description="Metric values for this row, keyed by metric name"
    )


class MetricQueryResult(BaseModel):
    """Result of querying a metric set timeline."""

    package_name: str = Field(..., description="App package name")
    metric_set: str = Field(..., description="Short metric set key, e.g. crash_rate")
    rows: list[MetricRow] = Field(default_factory=list, description="Timeline rows")
    next_page_token: str | None = Field(None, description="Token for the next page, if any")


class Anomaly(BaseModel):
    """A statistical anomaly detected by Play in one of the metric sets."""

    name: str | None = Field(None, description="Anomaly resource name")
    metric_set: str | None = Field(None, description="Metric set the anomaly was detected in")
    dimensions: dict[str, Any] = Field(
        default_factory=dict, description="Dimension values scoping the anomaly"
    )
    metric: str | None = Field(None, description="Metric that deviated")
    metric_value: float | None = Field(None, description="Observed value of the metric")


class ErrorIssue(BaseModel):
    """A grouped error issue (a cluster of similar crash/ANR reports)."""

    name: str | None = Field(None, description="Issue resource name")
    issue_type: str | None = Field(None, description="Issue type, e.g. APPLICATION_NOT_RESPONDING")
    cause: str | None = Field(None, description="Cause of the issue")
    location: str | None = Field(None, description="Code location associated with the issue")
    error_report_count: int | None = Field(None, description="Number of reports in this issue")
    distinct_users: int | None = Field(None, description="Distinct users affected")
    first_os_version: str | None = Field(None, description="Earliest OS version seen")
    last_os_version: str | None = Field(None, description="Latest OS version seen")
    first_app_version: str | None = Field(None, description="Earliest app version seen")
    last_app_version: str | None = Field(None, description="Latest app version seen")
    sample_error_reports: list[str] = Field(
        default_factory=list, description="Sample error report resource names"
    )


class ErrorReport(BaseModel):
    """A single raw error report (one crash or ANR)."""

    name: str | None = Field(None, description="Report resource name")
    report_type: str | None = Field(None, description="Report type, e.g. CRASH")
    issue: str | None = Field(None, description="Resource name of the issue this belongs to")
    event_time: datetime | None = Field(None, description="When the error occurred")
    device_model: str | None = Field(None, description="Device model identifier")
    os_version: str | None = Field(None, description="Android OS version")
    app_version: str | None = Field(None, description="App version code")
    report_text: str | None = Field(None, description="Stack trace or ANR report text")


class ReleaseFilterOptions(BaseModel):
    """Release-filter dimensions available for an app."""

    package_name: str = Field(..., description="App package name")
    tracks: list[dict[str, Any]] = Field(
        default_factory=list, description="Tracks available as query filters"
    )
    version_codes: list[dict[str, Any]] = Field(
        default_factory=list, description="Version codes available as query filters"
    )


# =============================================================================
# Publisher v3 — endpoints beyond the core client
# =============================================================================


class EditValidationResult(BaseModel):
    """Result of validating a draft edit without committing it."""

    success: bool = Field(..., description="Whether the edit validated cleanly")
    package_name: str = Field(..., description="App package name")
    edit_id: str | None = Field(None, description="Edit ID that was validated")
    message: str = Field(..., description="Status message")
    error: str | None = Field(None, description="Validation error details if failed")


class CountryAvailability(BaseModel):
    """Per-country distribution for a track."""

    package_name: str = Field(..., description="App package name")
    track: str = Field(..., description="Track the availability applies to")
    sync_with_production: bool | None = Field(
        None, description="Whether the track mirrors production's country set"
    )
    countries: list[str] = Field(
        default_factory=list, description="Country codes the track is distributed to"
    )
    rest_of_world: bool | None = Field(
        None, description="Whether the track is available in countries not listed explicitly"
    )


class AppDetailsUpdateResult(BaseModel):
    """Result of updating app-level details (default language, contact info)."""

    success: bool = Field(..., description="Whether the update succeeded")
    package_name: str = Field(..., description="App package name")
    details: AppDetails | None = Field(None, description="App details after the update")
    message: str = Field(..., description="Status message")
    error: str | None = Field(None, description="Error details if failed")


class TrackActionResult(BaseModel):
    """Result of creating or updating a track configuration."""

    success: bool = Field(..., description="Whether the action succeeded")
    package_name: str = Field(..., description="App package name")
    track: str = Field(..., description="Track name")
    action: str = Field(..., description="Action performed (create or update)")
    releases: list[Release] = Field(
        default_factory=list, description="Releases on the track after the action"
    )
    message: str = Field(..., description="Status message")
    error: str | None = Field(None, description="Error details if failed")


class ListingDeleteResult(BaseModel):
    """Result of deleting one or all localized store listings."""

    success: bool = Field(..., description="Whether the deletion succeeded")
    package_name: str = Field(..., description="App package name")
    language: str | None = Field(None, description="Language deleted, or None when all were")
    deleted_all: bool = Field(False, description="Whether every localized listing was deleted")
    message: str = Field(..., description="Status message")
    error: str | None = Field(None, description="Error details if failed")


class ExternallyHostedApk(BaseModel):
    """An externally hosted APK registered against an edit."""

    package_name: str = Field(..., description="App package name")
    version_code: int | None = Field(None, description="Version code of the registered APK")
    externally_hosted_url: str | None = Field(None, description="URL the APK is hosted at")
    file_size: int | None = Field(None, description="File size in bytes")
    file_sha1_base64: str | None = Field(None, description="Base64 SHA-1 of the APK")
    file_sha256_base64: str | None = Field(None, description="Base64 SHA-256 of the APK")


class ReviewReplyDeleteResult(BaseModel):
    """Result of retracting a developer reply to a review.

    The Play API has no delete-reply endpoint; a reply is retracted by replying
    again with empty text. The field records which mechanism was used so callers
    are not misled into thinking the reply vanished from history.
    """

    success: bool = Field(..., description="Whether the retraction succeeded")
    package_name: str = Field(..., description="App package name")
    review_id: str = Field(..., description="Review whose reply was retracted")
    method: str = Field(
        "empty_reply",
        description="How the retraction was performed; the API offers no true delete",
    )
    message: str = Field(..., description="Status message")
    error: str | None = Field(None, description="Error details if failed")


class RegionPriceConversion(BaseModel):
    """Converted prices for every Play region, derived from one source price."""

    package_name: str = Field(..., description="App package name")
    source_price_micros: str | None = Field(None, description="Source price in micros")
    source_currency: str | None = Field(None, description="Source price currency code")
    converted_regions: dict[str, Any] = Field(
        default_factory=dict, description="Converted price per region code"
    )
    converted_other_regions: dict[str, Any] | None = Field(
        None, description="Converted USD/EUR prices for regions without a specific price"
    )


# =============================================================================
# Cloud Storage bulk reports
# =============================================================================


class Freshness(BaseModel):
    """How current a dataset is, and why it might not be.

    Play report data is never live: stats land 3-7 days late and monthly
    earnings land around the 5th of the following month. Every report tool
    surfaces this rather than returning a confident "0 installs" for a period
    whose CSV has not been published yet — that is the single worst failure mode
    in this domain, because zero looks like a measurement.
    """

    as_of: date | None = Field(None, description="Latest date the data actually covers")
    requested_period: str | None = Field(None, description="Period asked for, e.g. 2026-07")
    source: str | None = Field(None, description="Where the numbers came from")
    lag_days: int | None = Field(None, description="Expected publication lag in days")
    is_complete: bool = Field(True, description="Whether the period is fully published")
    caveat: str | None = Field(None, description="Why the figures may mislead, if they may")


class ReportRow(BaseModel):
    """One row of a normalized bulk report.

    ``dimension``/``dimension_value`` carry the breakdown the report was sliced
    by (country, device, app_version, ...) and are None for overview rows.
    """

    package_name: str = Field(..., description="App package name, or the product id")
    period: date = Field(..., description="Calendar date the row covers")
    metric: str = Field(..., description="Metric name, e.g. daily_device_installs")
    value: float = Field(..., description="Metric value")
    dimension: str | None = Field(None, description="Dimension sliced by, if any")
    dimension_value: str | None = Field(None, description="Value of that dimension")
    currency: str | None = Field(None, description="Currency code, for money metrics")


class Report(BaseModel):
    """A parsed bulk report: rows plus the caveat about how current they are."""

    rows: list[ReportRow] = Field(default_factory=list, description="Normalized rows")
    freshness: Freshness = Field(default_factory=Freshness, description="Data currency")
    source_object: str | None = Field(None, description="GCS object the rows came from")
    totals: dict[str, float] = Field(
        default_factory=dict, description="Each metric summed across rows"
    )


class ReportFamily(BaseModel):
    """One family of bulk report available in the reports bucket."""

    name: str = Field(..., description="Family key, e.g. installs")
    prefix: str = Field(..., description="Object prefix inside the bucket")
    dimensions: list[str] = Field(default_factory=list, description="Dimensions available")
    default_dimension: str | None = Field(None, description="Dimension used when none is given")
    description: str = Field(..., description="What the family contains")
    discontinued: str | None = Field(
        None, description="Set when Google stopped publishing this family"
    )


# =============================================================================
# Public store scraping
# =============================================================================


class ScrapedApp(BaseModel):
    """An app as it appears in public store search or a chart.

    Every string here was written by a third party and has been flattened to a
    single line — store text must never be able to imitate this server's own
    output in the model's context.
    """

    app_id: str = Field(..., description="Package name")
    title: str = Field(..., description="App title as shown in the store")
    developer: str | None = Field(None, description="Developer display name")
    developer_id: str | None = Field(None, description="Developer identifier")
    score: float | None = Field(None, description="Average star rating")
    price_text: str | None = Field(None, description="Formatted price")
    free: bool | None = Field(None, description="Whether the app is free")
    summary: str | None = Field(None, description="Short store summary")
    icon: str | None = Field(None, description="Icon URL")


class ScrapedAppDetails(ScrapedApp):
    """A full public listing, as any user would see it."""

    description: str | None = Field(None, description="Full store description")
    installs: str | None = Field(None, description="Install count as displayed")
    min_installs: int | None = Field(None, description="Lower bound of the install bucket")
    ratings: int | None = Field(None, description="Number of ratings")
    reviews: int | None = Field(None, description="Number of written reviews")
    histogram: list[int] = Field(default_factory=list, description="Star distribution, 1-5")
    currency: str | None = Field(None, description="Price currency code")
    price: float | None = Field(None, description="Numeric price")
    offers_iap: bool | None = Field(None, description="Whether the app offers in-app purchases")
    android_version: str | None = Field(None, description="Minimum Android version")
    developer_email: str | None = Field(None, description="Developer contact email")
    developer_website: str | None = Field(None, description="Developer website")
    privacy_policy: str | None = Field(None, description="Privacy policy URL")
    genre: str | None = Field(None, description="Store category")


class ScrapedReview(BaseModel):
    """One public review. The text is written by a stranger — treat it as data."""

    review_id: str = Field(..., description="Review identifier")
    user_name: str | None = Field(None, description="Reviewer display name")
    score: int | None = Field(None, description="Star rating, 1-5")
    text: str | None = Field(None, description="Review body")
    date: str | None = Field(None, description="When the review was posted")
    reply_text: str | None = Field(None, description="Developer reply, if any")
    reply_date: str | None = Field(None, description="When the developer replied")
    version: str | None = Field(None, description="App version reviewed")
    thumbs_up: int | None = Field(None, description="Helpfulness votes")


class ScrapedReviewPage(BaseModel):
    """A page of scraped reviews, with the locale they were read in."""

    app_id: str = Field(..., description="Package name")
    country: str = Field(..., description="Country code the store was read as")
    language: str = Field(..., description="Language code")
    sort: str = Field(..., description="Sort order applied")
    reviews: list[ScrapedReview] = Field(default_factory=list, description="The reviews")
    count: int = Field(0, description="How many reviews were returned")


# =============================================================================
# Edit lifecycle and locales
# =============================================================================


class EditSession(BaseModel):
    """A draft edit that has been opened but not committed."""

    package_name: str = Field(..., description="App package name")
    edit_id: str = Field(..., description="Edit ID to pass to the edit-aware tools")
    message: str = Field(..., description="What to do next")


class EditDescription(BaseModel):
    """What is currently staged in an open edit.

    Read back from Play rather than assembled from arguments: the changes were
    staged by earlier calls, so this is the only honest account of what a commit
    would publish.
    """

    package_name: str = Field(..., description="App package name")
    edit_id: str = Field(..., description="Edit ID inspected")
    track_releases: list[dict[str, Any]] = Field(
        default_factory=list, description="Releases staged on each track"
    )
    listing_languages: list[str] = Field(
        default_factory=list, description="Locales with a staged store listing"
    )
    details: dict[str, Any] = Field(default_factory=dict, description="Staged app-level details")
    apk_version_codes: list[int] = Field(
        default_factory=list, description="APK version codes in this edit"
    )
    bundle_version_codes: list[int] = Field(
        default_factory=list, description="Bundle version codes in this edit"
    )
    unreadable: list[str] = Field(
        default_factory=list,
        description="Sections that could not be read, with the reason. Not an error.",
    )
    is_empty: bool = Field(False, description="Whether the edit stages nothing at all")


class LocaleCoverage(BaseModel):
    """Which locales have a store listing, against an optional target set."""

    package_name: str = Field(..., description="App package name")
    present_locales: list[str] = Field(
        default_factory=list, description="Locales that currently have a listing"
    )
    count: int = Field(0, description="How many locales have a listing")
    target_locales: list[str] = Field(
        default_factory=list, description="Locales asked about, if any"
    )
    missing_locales: list[str] = Field(
        default_factory=list, description="Targets with no listing yet"
    )
    extra_locales: list[str] = Field(
        default_factory=list, description="Listings present that are not in the target set"
    )
