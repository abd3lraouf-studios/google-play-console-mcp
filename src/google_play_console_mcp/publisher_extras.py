"""Android Publisher v3 endpoints beyond the core client.

These live in their own module rather than in ``client.py`` so the core client
stays focused and this surface stays cheap to maintain on its own. It is mixed
into :class:`google_play_console_mcp.client.PlayStoreClient` and reuses that class's
edit lifecycle helpers, retry/backoff and transport lock.

What is here and why it matters:

``validate_edit``
    Dry-run an edit before committing. Without it there is no way to find out
    that a change is rejected other than committing it.
``get_country_availability``
    Read which countries a track is distributed to.
``update_app_details``
    App-level settings — default language and the contact email, phone and
    website shown on the store listing.
``create_track`` / ``update_track``
    Custom closed-testing tracks. The core client can deploy to the four
    built-in tracks but cannot create a track.
``delete_listing`` / ``delete_all_listings``
    Remove a locale's store listing. The core client can only add.
``add_externally_hosted_apk``
    Enterprise (Play-for-Work) distribution of an APK hosted outside Play.
``patch_expansion_file`` / ``update_expansion_file``
    Re-point an OBB reference at another version code without re-uploading.
``acknowledge_subscription_purchase``
    The core client acknowledges one-time products but not
    subscriptions. An unacknowledged subscription purchase is **refunded
    automatically by Google after three days**, so this was a real billing gap.
``archive_subscription``
    Archive rather than delete, which is what Play recommends for a
    subscription that has ever had a purchase.
``convert_region_prices``
    Convert one source price into every Play region's price.
``delete_review_reply``
    Retract a developer reply.

**Not here: managed publishing.** Several servers in this space expose a
``publish_managed_release`` tool. There is no such endpoint: the Android
Publisher v3 discovery document has no ``managedPublishing`` resource, so
``service.managedPublishing()`` raises at runtime. ``edits.commit`` does take a
``changesNotSentForReview`` flag, but that governs re-review after a rejection,
not the managed-publishing gate. Sending managed changes live remains a Play
Console UI action with no API equivalent.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import structlog
from googleapiclient.errors import HttpError

from google_play_console_mcp.models import (
    AppDetails,
    AppDetailsUpdateResult,
    CountryAvailability,
    EditValidationResult,
    ExpansionFile,
    ExternallyHostedApk,
    ListingDeleteResult,
    RegionPriceConversion,
    Release,
    ReviewReplyDeleteResult,
    SubscriptionActionResult,
    TrackActionResult,
)

if TYPE_CHECKING:
    from collections.abc import Sequence

logger = structlog.get_logger(__name__)

# Expansion files are either the main OBB or a patch OBB.
EXPANSION_FILE_TYPES = frozenset({"main", "patch"})

# Release statuses accepted by a track configuration.
RELEASE_STATUSES = frozenset({"draft", "inProgress", "halted", "completed"})


def _parse_release(data: dict[str, Any]) -> Release:
    """Parse a track release body into a Release.

    ``userFraction`` is absent on a completed release (it means 100%) and is a
    0..1 float otherwise, while the model carries a 0..100 percentage.
    """
    fraction = data.get("userFraction")
    rollout = 100.0 if fraction is None else float(fraction) * 100.0
    return Release(
        package_name=data.get("packageName", ""),
        track=data.get("track", ""),
        status=data.get("status", "unknown"),
        version_codes=[int(code) for code in data.get("versionCodes", [])],
        version_name=data.get("name"),
        rollout_percentage=rollout,
        release_notes={
            note.get("language", ""): note.get("text", "") for note in data.get("releaseNotes", [])
        },
    )


class PublisherExtrasMixin:
    """Publisher v3 operations absent from the core client.

    Relies on members provided by :class:`google_play_console_mcp.client.PlayStoreClient`:
    ``_get_service``, ``_execute``, ``_create_edit``, ``_commit_edit``,
    ``_delete_edit`` and ``_logger``.
    """

    _logger: Any

    def _get_service(self) -> Any:  # pragma: no cover - provided by host
        raise NotImplementedError

    def _execute(self, request: Any) -> Any:  # pragma: no cover - provided by host
        raise NotImplementedError

    def _create_edit(self, package_name: str) -> str:  # pragma: no cover - provided by host
        raise NotImplementedError

    def _commit_edit(self, package_name: str, edit_id: str) -> None:  # pragma: no cover
        raise NotImplementedError

    def _delete_edit(self, package_name: str, edit_id: str) -> None:  # pragma: no cover
        raise NotImplementedError

    # =========================================================================
    # Edits
    # =========================================================================

    def validate_edit(self, package_name: str) -> EditValidationResult:
        """Dry-run an edit: open one, validate it, then discard it.

        Validation reports whether the edit *would* commit cleanly. Because an
        empty edit is always valid, this is most useful as a connectivity and
        permissions probe: a 403 here means the service account cannot publish,
        which is worth learning before a deploy rather than during one.

        Args:
            package_name: App package name.

        Returns:
            Validation result.
        """
        self._logger.info("Validating edit", package_name=package_name)
        service = self._get_service()
        edit_id = self._create_edit(package_name)

        try:
            self._execute(service.edits().validate(packageName=package_name, editId=edit_id))
        except HttpError as e:
            self._delete_edit(package_name, edit_id)
            return EditValidationResult(
                success=False,
                package_name=package_name,
                edit_id=edit_id,
                message="Edit validation failed",
                error=str(e.reason),
            )

        # Never commit: validation is a dry run by definition.
        self._delete_edit(package_name, edit_id)
        return EditValidationResult(
            success=True,
            package_name=package_name,
            edit_id=edit_id,
            message="Edit validated successfully and was discarded",
        )

    # =========================================================================
    # App details and country availability
    # =========================================================================

    def get_country_availability(self, package_name: str, track: str) -> CountryAvailability:
        """Read which countries a track is distributed to.

        Args:
            package_name: App package name.
            track: Track name (e.g. production, beta).

        Returns:
            Country availability for the track.
        """
        service = self._get_service()
        edit_id = self._create_edit(package_name)

        try:
            result = self._execute(
                service.edits()
                .countryavailability()
                .get(packageName=package_name, editId=edit_id, track=track)
            )
        finally:
            self._delete_edit(package_name, edit_id)

        return CountryAvailability(
            package_name=package_name,
            track=track,
            sync_with_production=result.get("syncWithProduction"),
            countries=[c.get("countryCode", "") for c in result.get("countries", [])],
            rest_of_world=result.get("restOfWorld"),
        )

    def update_app_details(
        self,
        package_name: str,
        default_language: str | None = None,
        contact_email: str | None = None,
        contact_phone: str | None = None,
        contact_website: str | None = None,
    ) -> AppDetailsUpdateResult:
        """Update app-level details.

        Only the fields supplied are changed; the rest are read from the current
        details and sent back unchanged, because the Play API's update replaces
        the whole resource.

        Args:
            package_name: App package name.
            default_language: Default listing language (e.g. en-US).
            contact_email: Developer contact email shown on the listing.
            contact_phone: Developer contact phone shown on the listing.
            contact_website: Developer website shown on the listing.

        Returns:
            Update result carrying the details after the change.
        """
        self._logger.info("Updating app details", package_name=package_name)
        service = self._get_service()
        edit_id = self._create_edit(package_name)

        try:
            current = self._execute(
                service.edits().details().get(packageName=package_name, editId=edit_id)
            )

            body = {
                "defaultLanguage": (
                    default_language
                    if default_language is not None
                    else current.get("defaultLanguage")
                ),
                "contactEmail": (
                    contact_email if contact_email is not None else current.get("contactEmail")
                ),
                "contactPhone": (
                    contact_phone if contact_phone is not None else current.get("contactPhone")
                ),
                "contactWebsite": (
                    contact_website
                    if contact_website is not None
                    else current.get("contactWebsite")
                ),
            }

            updated = self._execute(
                service.edits()
                .details()
                .update(packageName=package_name, editId=edit_id, body=body)
            )
            self._commit_edit(package_name, edit_id)
        except HttpError as e:
            self._delete_edit(package_name, edit_id)
            return AppDetailsUpdateResult(
                success=False,
                package_name=package_name,
                message="Failed to update app details",
                error=str(e.reason),
            )

        return AppDetailsUpdateResult(
            success=True,
            package_name=package_name,
            details=AppDetails(
                package_name=package_name,
                default_language=updated.get("defaultLanguage"),
                developer_email=updated.get("contactEmail"),
                developer_website=updated.get("contactWebsite"),
            ),
            message="App details updated",
        )

    # =========================================================================
    # Tracks
    # =========================================================================

    def _write_track(
        self,
        package_name: str,
        track: str,
        action: str,
        releases: Sequence[dict[str, Any]] | None,
    ) -> TrackActionResult:
        """Create or update a track configuration inside one edit."""
        service = self._get_service()
        edit_id = self._create_edit(package_name)
        body: dict[str, Any] = {"track": track, "releases": list(releases or [])}

        try:
            if action == "create":
                result = self._execute(
                    service.edits()
                    .tracks()
                    .create(packageName=package_name, editId=edit_id, body=body)
                )
            else:
                result = self._execute(
                    service.edits()
                    .tracks()
                    .patch(packageName=package_name, editId=edit_id, track=track, body=body)
                )
            self._commit_edit(package_name, edit_id)
        except HttpError as e:
            self._delete_edit(package_name, edit_id)
            return TrackActionResult(
                success=False,
                package_name=package_name,
                track=track,
                action=action,
                message=f"Failed to {action} track",
                error=str(e.reason),
            )

        return TrackActionResult(
            success=True,
            package_name=package_name,
            track=track,
            action=action,
            releases=[_parse_release(r) for r in result.get("releases", [])],
            message=f"Track {track} {action}d",
        )

    def create_track(
        self,
        package_name: str,
        track: str,
        releases: Sequence[dict[str, Any]] | None = None,
    ) -> TrackActionResult:
        """Create a custom closed-testing track.

        The four built-in tracks (internal, alpha, beta, production) always
        exist. This creates an additional named track, which is how closed
        testing groups beyond the built-ins are set up.

        Args:
            package_name: App package name.
            track: Name for the new track.
            releases: Optional initial releases for the track.

        Returns:
            Track action result.
        """
        self._logger.info("Creating track", package_name=package_name, track=track)
        return self._write_track(package_name, track, "create", releases)

    def update_track(
        self,
        package_name: str,
        track: str,
        releases: Sequence[dict[str, Any]] | None = None,
    ) -> TrackActionResult:
        """Update a track's release configuration.

        Args:
            package_name: App package name.
            track: Track to update.
            releases: Release configuration to write to the track.

        Returns:
            Track action result.
        """
        self._logger.info("Updating track", package_name=package_name, track=track)
        return self._write_track(package_name, track, "update", releases)

    # =========================================================================
    # Listings
    # =========================================================================

    def delete_listing(self, package_name: str, language: str) -> ListingDeleteResult:
        """Delete one locale's store listing.

        Args:
            package_name: App package name.
            language: Language code to delete (e.g. de-DE).

        Returns:
            Deletion result.
        """
        self._logger.info("Deleting listing", package_name=package_name, language=language)
        service = self._get_service()
        edit_id = self._create_edit(package_name)

        try:
            self._execute(
                service.edits()
                .listings()
                .delete(packageName=package_name, editId=edit_id, language=language)
            )
            self._commit_edit(package_name, edit_id)
        except HttpError as e:
            self._delete_edit(package_name, edit_id)
            return ListingDeleteResult(
                success=False,
                package_name=package_name,
                language=language,
                message="Failed to delete listing",
                error=str(e.reason),
            )

        return ListingDeleteResult(
            success=True,
            package_name=package_name,
            language=language,
            message=f"Deleted {language} listing",
        )

    def delete_all_listings(self, package_name: str) -> ListingDeleteResult:
        """Delete every localized store listing for an app.

        Destructive and not undoable through the API — the listing text for all
        locales is removed in one commit.

        Args:
            package_name: App package name.

        Returns:
            Deletion result.
        """
        self._logger.warning("Deleting all listings", package_name=package_name)
        service = self._get_service()
        edit_id = self._create_edit(package_name)

        try:
            self._execute(
                service.edits().listings().deleteall(packageName=package_name, editId=edit_id)
            )
            self._commit_edit(package_name, edit_id)
        except HttpError as e:
            self._delete_edit(package_name, edit_id)
            return ListingDeleteResult(
                success=False,
                package_name=package_name,
                deleted_all=True,
                message="Failed to delete all listings",
                error=str(e.reason),
            )

        return ListingDeleteResult(
            success=True,
            package_name=package_name,
            deleted_all=True,
            message="Deleted all localized listings",
        )

    # =========================================================================
    # APKs and expansion files
    # =========================================================================

    def add_externally_hosted_apk(
        self,
        package_name: str,
        externally_hosted_apk: dict[str, Any],
    ) -> ExternallyHostedApk:
        """Register an APK hosted outside Play (enterprise distribution only).

        Only usable by organisations enrolled in Play for Work private-channel
        distribution; a normal developer account gets a 403.

        Args:
            package_name: App package name.
            externally_hosted_apk: ExternallyHostedApk resource body — must
                include externallyHostedUrl, fileSha1Base64, fileSha256Base64,
                fileSize, packageName, versionCode and the manifest fields Play
                would otherwise read from the APK itself.

        Returns:
            The registered APK.
        """
        self._logger.info("Adding externally hosted APK", package_name=package_name)
        service = self._get_service()
        edit_id = self._create_edit(package_name)

        try:
            result = self._execute(
                service.edits()
                .apks()
                .addexternallyhosted(
                    packageName=package_name,
                    editId=edit_id,
                    body={"externallyHostedApk": externally_hosted_apk},
                )
            )
            self._commit_edit(package_name, edit_id)
        except HttpError:
            self._delete_edit(package_name, edit_id)
            raise

        apk = result.get("externallyHostedApk", {})
        return ExternallyHostedApk(
            package_name=package_name,
            version_code=apk.get("versionCode"),
            externally_hosted_url=apk.get("externallyHostedUrl"),
            file_size=int(apk["fileSize"]) if apk.get("fileSize") is not None else None,
            file_sha1_base64=apk.get("fileSha1Base64"),
            file_sha256_base64=apk.get("fileSha256Base64"),
        )

    def set_expansion_file_reference(
        self,
        package_name: str,
        version_code: int,
        expansion_file_type: str,
        references_version: int,
        *,
        patch: bool = False,
    ) -> ExpansionFile:
        """Point a version's OBB at one already uploaded against another version.

        Large apps reuse the same OBB across builds. This associates an existing
        expansion file with a new version code instead of re-uploading it.

        Args:
            package_name: App package name.
            version_code: Version code to attach the reference to.
            expansion_file_type: "main" or "patch".
            references_version: Version code whose expansion file to reference.
            patch: Use the API's patch (partial) semantics rather than update
                (full replace). Both accept the same body here.

        Returns:
            The expansion file after the change.

        Raises:
            ValueError: if the expansion file type is not main or patch.
        """
        if expansion_file_type not in EXPANSION_FILE_TYPES:
            raise ValueError(
                f"expansion_file_type must be one of {sorted(EXPANSION_FILE_TYPES)}, "
                f"got '{expansion_file_type}'"
            )

        service = self._get_service()
        edit_id = self._create_edit(package_name)
        body = {"referencesVersion": references_version}

        try:
            api = service.edits().expansionfiles()
            call = api.patch if patch else api.update
            result = self._execute(
                call(
                    packageName=package_name,
                    editId=edit_id,
                    apkVersionCode=version_code,
                    expansionFileType=expansion_file_type,
                    body=body,
                )
            )
            self._commit_edit(package_name, edit_id)
        except HttpError:
            self._delete_edit(package_name, edit_id)
            raise

        return ExpansionFile(
            version_code=version_code,
            expansion_file_type=expansion_file_type,
            file_size=result.get("fileSize"),
            references_version=result.get("referencesVersion"),
        )

    # =========================================================================
    # Purchases and monetization
    # =========================================================================

    def acknowledge_subscription_purchase(
        self,
        package_name: str,
        subscription_id: str,
        purchase_token: str,
        developer_payload: str | None = None,
    ) -> SubscriptionActionResult:
        """Acknowledge a subscription purchase.

        Google **automatically refunds and revokes a subscription purchase that
        is not acknowledged within three days**. The core toolset could
        acknowledge one-time products but not subscriptions, so this closed a
        path to silent revenue loss.

        Args:
            package_name: App package name.
            subscription_id: Subscription product ID.
            purchase_token: Purchase token from the client.
            developer_payload: Optional payload to store with the purchase.

        Returns:
            Action result.
        """
        self._logger.info(
            "Acknowledging subscription purchase",
            package_name=package_name,
            subscription_id=subscription_id,
        )
        service = self._get_service()
        body = {"developerPayload": developer_payload} if developer_payload else {}

        try:
            self._execute(
                service.purchases()
                .subscriptions()
                .acknowledge(
                    packageName=package_name,
                    subscriptionId=subscription_id,
                    token=purchase_token,
                    body=body,
                )
            )
        except HttpError as e:
            return SubscriptionActionResult(
                success=False,
                package_name=package_name,
                purchase_token=purchase_token,
                action="acknowledge",
                message="Failed to acknowledge subscription purchase",
                error=str(e.reason),
            )

        return SubscriptionActionResult(
            success=True,
            package_name=package_name,
            purchase_token=purchase_token,
            action="acknowledge",
            message="Subscription purchase acknowledged",
        )

    def archive_subscription(self, package_name: str, product_id: str) -> SubscriptionActionResult:
        """Archive a subscription.

        Play recommends archiving rather than deleting any subscription that has
        ever been purchased: deletion is rejected once purchases exist, and
        archiving preserves the record for existing subscribers.

        Args:
            package_name: App package name.
            product_id: Subscription product ID.

        Returns:
            Action result.
        """
        self._logger.info(
            "Archiving subscription", package_name=package_name, product_id=product_id
        )
        service = self._get_service()

        try:
            self._execute(
                service.monetization()
                .subscriptions()
                .archive(packageName=package_name, productId=product_id, body={})
            )
        except HttpError as e:
            return SubscriptionActionResult(
                success=False,
                package_name=package_name,
                purchase_token="",
                action="archive",
                message="Failed to archive subscription",
                error=str(e.reason),
            )

        return SubscriptionActionResult(
            success=True,
            package_name=package_name,
            purchase_token="",
            action="archive",
            message=f"Subscription {product_id} archived",
            details={"product_id": product_id},
        )

    def convert_region_prices(
        self,
        package_name: str,
        price_micros: str,
        currency_code: str,
    ) -> RegionPriceConversion:
        """Convert one source price into every Play region's price.

        Returns Play's own recommended prices rather than a naive FX conversion:
        the values are rounded to each region's local price conventions.

        Args:
            package_name: App package name.
            price_micros: Source price in micros (1 unit = 1,000,000 micros).
            currency_code: Source currency code (e.g. USD).

        Returns:
            Converted prices per region.
        """
        service = self._get_service()
        # The API takes a google.type.Money, so micros split into units + nanos.
        micros = int(price_micros)
        body: dict[str, Any] = {
            "price": {
                "currencyCode": currency_code,
                "units": str(micros // 1_000_000),
                "nanos": (micros % 1_000_000) * 1000,
            }
        }

        result = self._execute(
            service.monetization().convertRegionPrices(packageName=package_name, body=body)
        )

        return RegionPriceConversion(
            package_name=package_name,
            source_price_micros=price_micros,
            source_currency=currency_code,
            converted_regions=result.get("convertedRegionPrices", {}),
            converted_other_regions=result.get("convertedOtherRegionsPrice"),
        )

    # =========================================================================
    # Reviews
    # =========================================================================

    def delete_review_reply(self, package_name: str, review_id: str) -> ReviewReplyDeleteResult:
        """Retract a developer reply to a review.

        The Play API has **no delete-reply endpoint**. A reply is retracted by
        replying again with empty text, which clears the visible reply. The
        result records the mechanism so a caller is not misled into believing
        the reply was erased from history.

        Args:
            package_name: App package name.
            review_id: Review whose reply should be retracted.

        Returns:
            Retraction result.
        """
        self._logger.info("Retracting review reply", package_name=package_name, review_id=review_id)
        service = self._get_service()

        try:
            self._execute(
                service.reviews().reply(
                    packageName=package_name,
                    reviewId=review_id,
                    body={"replyText": ""},
                )
            )
        except HttpError as e:
            return ReviewReplyDeleteResult(
                success=False,
                package_name=package_name,
                review_id=review_id,
                message="Failed to retract review reply",
                error=str(e.reason),
            )

        return ReviewReplyDeleteResult(
            success=True,
            package_name=package_name,
            review_id=review_id,
            message="Review reply retracted by replying with empty text",
        )
