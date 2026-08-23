"""The edit lifecycle, an escape hatch, and locale coverage.

Every mutation inherited from upstream is a **pre-baked one-shot**: it opens an
edit, does one thing, commits, and closes. That is safe and it is why the tool
surface reads cleanly, but it means an agent cannot compose a transaction —
"patch the listing, upload a screenshot, set the track, validate, then commit"
is five commits and five review submissions instead of one.

This module exposes the lifecycle so that transaction is expressible, and makes
it safe to expose by giving ``commit_edit`` a preview built from **what is
actually staged in the edit**. The gate elsewhere previews arguments the caller
passed; here the interesting content was staged by earlier calls, so the preview
reads it back from Play instead.

Also here:

``android_publisher_invoke``
    A generic call into any Android Publisher v3 method by dotted path. Insurance
    for surfaces this server has not wrapped, and for endpoints Google ships
    after this release. Idea from ``TylerThompson/google-play-console-mcp``.
``list_locale_coverage`` / ``clone_listing_to_locale``
    Which locales have a store listing, and copying one to another. From
    ``hemati/google-play-store-mcp`` — whose version opens an edit for the
    read and never deletes it, leaking one per call. This one cleans up.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import structlog
from googleapiclient.errors import HttpError

from google_play_console_mcp.errors import ValidationError
from google_play_console_mcp.models import (
    EditDescription,
    EditSession,
    ListingUpdateResult,
    LocaleCoverage,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

logger = structlog.get_logger(__name__)

#: HTTP methods that only read. Used to decide whether a generic invoke needs
#: the confirmation gate — the same signal ``_execute`` uses to decide whether a
#: request is safe to retry.
READ_ONLY_HTTP_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})

#: Resource paths reachable through ``android_publisher_invoke``. An allowlist
#: rather than free attribute traversal: ``getattr`` down a caller-supplied path
#: on a live client is how a "call any method" tool becomes a way to reach
#: something that is not an API method at all.
INVOKABLE_ROOTS = frozenset(
    {
        "edits",
        "inappproducts",
        "monetization",
        "orders",
        "purchases",
        "reviews",
        "systemapks",
        "users",
        "grants",
        "generatedapks",
        "applications",
        "apprecovery",
        "externaltransactions",
        "internalappsharingartifacts",
    }
)

#: Listing fields copied between locales. Deliberately not the images: cloning
#: those means downloading and re-uploading every asset, which is a different
#: operation with different failure modes and belongs behind its own preview.
_LISTING_FIELDS = ("title", "shortDescription", "fullDescription", "video")


class EditsMixin:
    """Edit lifecycle, generic invocation and locale tools.

    Mixed into :class:`google_play_console_mcp.client.PlayStoreClient`; relies on
    ``_get_service``, ``_execute``, ``_create_edit``, ``_commit_edit``,
    ``_delete_edit`` and ``_logger``.
    """

    _logger: Any

    def _get_service(self) -> Any:  # pragma: no cover - provided by host
        raise NotImplementedError

    def _execute(self, request: Any) -> Any:  # pragma: no cover - provided by host
        raise NotImplementedError

    def _create_edit(self, package_name: str) -> str:  # pragma: no cover
        raise NotImplementedError

    def _delete_edit(self, package_name: str, edit_id: str) -> None:  # pragma: no cover
        raise NotImplementedError

    # =========================================================================
    # Lifecycle
    # =========================================================================

    def open_edit(self, package_name: str) -> EditSession:
        """Open a draft edit and return its id.

        Nothing is visible to users until the edit is committed, so opening one
        is harmless. Edits expire after about seven days; an abandoned one costs
        nothing but is worth deleting.
        """
        edit_id = self._create_edit(package_name)
        return EditSession(
            package_name=package_name,
            edit_id=edit_id,
            message=(
                "Edit opened. Stage changes with the edit-aware tools, then commit_edit. "
                "Nothing reaches users until you do."
            ),
        )

    def describe_edit(self, package_name: str, edit_id: str) -> EditDescription:
        """Read back everything staged in an edit.

        This is what makes committing safe to expose. The changes were staged by
        earlier calls, so the only honest preview is one read from Play rather
        than assembled from the current call's arguments.

        Each section is read independently and a failure is recorded rather than
        raised: an edit with no listings staged returns 404 for listings, which
        is information, not an error.
        """
        service = self._get_service()
        description = EditDescription(package_name=package_name, edit_id=edit_id)

        def _try(label: str, call: Any) -> Any:
            try:
                return self._execute(call)
            except HttpError as exc:
                description.unreadable.append(f"{label}: {exc.reason}")
                return None

        tracks = _try(
            "tracks",
            service.edits().tracks().list(packageName=package_name, editId=edit_id),
        )
        for track in (tracks or {}).get("tracks", []):
            for release in track.get("releases", []):
                description.track_releases.append(
                    {
                        "track": track.get("track"),
                        "status": release.get("status"),
                        "versionCodes": release.get("versionCodes", []),
                        "userFraction": release.get("userFraction"),
                        "name": release.get("name"),
                    }
                )

        listings = _try(
            "listings",
            service.edits().listings().list(packageName=package_name, editId=edit_id),
        )
        description.listing_languages = sorted(
            item.get("language", "") for item in (listings or {}).get("listings", [])
        )

        details = _try(
            "details", service.edits().details().get(packageName=package_name, editId=edit_id)
        )
        if details:
            description.details = {
                "defaultLanguage": details.get("defaultLanguage"),
                "contactEmail": details.get("contactEmail"),
                "contactWebsite": details.get("contactWebsite"),
            }

        apks = _try("apks", service.edits().apks().list(packageName=package_name, editId=edit_id))
        description.apk_version_codes = [
            int(a["versionCode"]) for a in (apks or {}).get("apks", []) if "versionCode" in a
        ]

        bundles = _try(
            "bundles", service.edits().bundles().list(packageName=package_name, editId=edit_id)
        )
        description.bundle_version_codes = [
            int(b["versionCode"]) for b in (bundles or {}).get("bundles", []) if "versionCode" in b
        ]

        description.is_empty = not any(
            (
                description.track_releases,
                description.listing_languages,
                description.apk_version_codes,
                description.bundle_version_codes,
            )
        )
        return description

    def validate_open_edit(self, package_name: str, edit_id: str) -> dict[str, Any]:
        """Dry-run an open edit without committing or discarding it."""
        service = self._get_service()
        try:
            self._execute(service.edits().validate(packageName=package_name, editId=edit_id))
        except HttpError as exc:
            return {"valid": False, "edit_id": edit_id, "error": str(exc.reason)}
        return {"valid": True, "edit_id": edit_id, "message": "Edit validates; safe to commit."}

    def commit_open_edit(
        self,
        package_name: str,
        edit_id: str,
        changes_not_sent_for_review: bool = False,
    ) -> dict[str, Any]:
        """Commit an edit, publishing everything staged in it."""
        service = self._get_service()
        try:
            result = self._execute(
                service.edits().commit(
                    packageName=package_name,
                    editId=edit_id,
                    changesNotSentForReview=changes_not_sent_for_review,
                )
            )
        except HttpError as exc:
            return {"success": False, "edit_id": edit_id, "error": str(exc.reason)}
        return {
            "success": True,
            "edit_id": edit_id,
            "committed": result.get("id", edit_id),
            "message": "Edit committed.",
        }

    def discard_edit(self, package_name: str, edit_id: str) -> dict[str, Any]:
        """Discard an edit without committing. Nothing staged in it takes effect."""
        self._delete_edit(package_name, edit_id)
        return {
            "success": True,
            "edit_id": edit_id,
            "message": "Edit discarded; nothing staged in it was published.",
        }

    # =========================================================================
    # Generic invocation
    # =========================================================================

    def resolve_publisher_request(
        self, method_path: str, params: dict[str, Any] | None = None
    ) -> tuple[Callable[[], Any], str]:
        """Resolve a dotted API path into a ready-to-run call and its HTTP verb.

        Resolution is separate from execution on purpose: the verb decides
        whether the call needs the confirmation gate, and a read must not be
        made to round-trip for approval just to find that out.

        Args:
            method_path: e.g. "edits.tracks.list" or "purchases.voidedpurchases.list".
            params: Keyword arguments for that method, named as the API names them.

        Returns:
            A zero-argument callable that performs the request, and the HTTP
            method the API uses for it.

        Raises:
            ValidationError: if the path is not an allowlisted, real API method.
        """
        parts = [p for p in method_path.split(".") if p]
        min_parts = 2
        if len(parts) < min_parts:
            raise ValidationError(
                f"{method_path!r} is not a method path.",
                remedy="Use resource.method, e.g. 'edits.tracks.list'.",
            )
        if parts[0] not in INVOKABLE_ROOTS:
            raise ValidationError(
                f"{parts[0]!r} is not an invokable Android Publisher resource.",
                remedy=f"Start the path with one of: {', '.join(sorted(INVOKABLE_ROOTS))}.",
            )

        node = self._get_service()
        for step in parts[:-1]:
            factory = getattr(node, step, None)
            if factory is None or not callable(factory):
                raise ValidationError(
                    f"No API resource {step!r} in {method_path!r}.",
                    remedy=(
                        "Check the path against the androidpublisher v3 discovery document. "
                        "Resource names are lowercase and unspaced, e.g. 'countryavailability'."
                    ),
                )
            node = factory()

        method = getattr(node, parts[-1], None)
        if method is None or not callable(method):
            raise ValidationError(
                f"No API method {parts[-1]!r} on {'.'.join(parts[:-1])!r}.",
                remedy="Check the method name against the discovery document.",
            )

        request = method(**(params or {}))
        http_method = (getattr(request, "method", "") or "").upper()
        if not http_method or not hasattr(request, "execute"):
            # A dotted path whose last segment is a sub-resource rather than a
            # method (e.g. "edits.tracks") resolves to a resource object, not a
            # request. Letting that through would produce an empty HTTP verb,
            # which the caller would classify as a write and then fail on.
            raise ValidationError(
                f"{method_path!r} names a resource, not a method.",
                remedy=(
                    "Append the operation, e.g. 'edits.tracks.list' rather than "
                    "'edits.tracks'."
                ),
            )
        return (lambda: self._execute(request)), http_method

    # =========================================================================
    # Locales
    # =========================================================================

    def list_locale_coverage(
        self, package_name: str, target_locales: Sequence[str] | None = None
    ) -> LocaleCoverage:
        """Which locales have a store listing, and which targets are missing one.

        Opens a throwaway edit to read, then **deletes it**. Reading listings
        requires an edit, and leaving it open leaks one per call.
        """
        service = self._get_service()
        edit_id = self._create_edit(package_name)
        try:
            result = self._execute(
                service.edits().listings().list(packageName=package_name, editId=edit_id)
            )
        finally:
            self._delete_edit(package_name, edit_id)

        present = sorted({item.get("language", "") for item in result.get("listings", [])})
        coverage = LocaleCoverage(
            package_name=package_name, present_locales=present, count=len(present)
        )
        if target_locales:
            wanted = list(target_locales)
            coverage.target_locales = wanted
            coverage.missing_locales = sorted(set(wanted) - set(present))
            coverage.extra_locales = sorted(set(present) - set(wanted))
        return coverage

    def clone_listing_to_locale(
        self,
        package_name: str,
        source_language: str,
        target_language: str,
        overwrite: bool = False,
    ) -> ListingUpdateResult:
        """Copy a store listing's text from one locale to another.

        Copies text only — title, descriptions and video. Images are not cloned:
        that means downloading and re-uploading every asset, which is a
        different operation with different failure modes.

        A locale that already has a listing is left alone unless ``overwrite``
        is set, so this cannot silently replace translated copy with the source
        language.
        """
        service = self._get_service()
        edit_id = self._create_edit(package_name)

        try:
            source = self._execute(
                service.edits()
                .listings()
                .get(packageName=package_name, editId=edit_id, language=source_language)
            )

            if not overwrite:
                try:
                    existing = self._execute(
                        service.edits()
                        .listings()
                        .get(packageName=package_name, editId=edit_id, language=target_language)
                    )
                except HttpError:
                    existing = None
                if existing:
                    self._delete_edit(package_name, edit_id)
                    return ListingUpdateResult(
                        success=False,
                        package_name=package_name,
                        language=target_language,
                        message=(
                            f"{target_language} already has a listing. Pass overwrite=True to "
                            f"replace it — this would otherwise overwrite translated copy with "
                            f"{source_language} text."
                        ),
                        error="listing_exists",
                    )

            body = {field: source.get(field, "") for field in _LISTING_FIELDS}
            self._execute(
                service.edits()
                .listings()
                .update(
                    packageName=package_name,
                    editId=edit_id,
                    language=target_language,
                    body=body,
                )
            )
            self._execute(service.edits().commit(packageName=package_name, editId=edit_id))
        except HttpError as exc:
            self._delete_edit(package_name, edit_id)
            return ListingUpdateResult(
                success=False,
                package_name=package_name,
                language=target_language,
                message=f"Failed to clone {source_language} to {target_language}",
                error=str(exc.reason),
            )

        return ListingUpdateResult(
            success=True,
            package_name=package_name,
            language=target_language,
            message=f"Copied the {source_language} listing text to {target_language}",
        )
