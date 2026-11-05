"""Manifest-driven resolver for trusted local visual assets.

Only logical IDs are accepted. Every candidate is resolved below its configured
root after symlink expansion, then decoded and verified by Pillow before it is
returned. This module deliberately has no HTTP or URL-fetching implementation.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping

from PIL import Image, UnidentifiedImageError


ASSET_ROOT = Path(__file__).resolve().parent.parent / "assets"
MANIFEST_PATH = ASSET_ROOT / "manifest.json"
_LOGICAL_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_.-]*$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_SHA1_RE = re.compile(r"^[0-9a-f]{40}$")


class AssetResolverError(ValueError):
    """Base error for malformed manifests and unsafe asset paths."""


class AssetValidationError(AssetResolverError):
    """Raised when an existing local candidate is not a safe PNG."""


@dataclass(frozen=True, slots=True)
class ResolvedVisualAsset:
    """Verified local asset and the manifest provenance associated with it."""

    path: Path
    source: str
    logical_id: str
    attribution: str | None = None
    source_url: str | None = None
    license: str | None = None
    original_sha1: str | None = None


@dataclass(frozen=True, slots=True)
class _Candidate:
    root: Path
    relative_path: str
    source: str
    metadata: Mapping[str, Any]


def _real_root(value: str | Path) -> Path:
    root = Path(value).expanduser().resolve(strict=False)
    if not root.is_dir():
        raise AssetResolverError(f"asset root is not a directory: {value}")
    return root


def _safe_relative(root: Path, relative_path: str | Path) -> Path:
    raw = str(relative_path)
    path = Path(raw)
    if not raw or "\x00" in raw or path.is_absolute():
        raise AssetResolverError("manifest asset paths must be relative")
    try:
        candidate = (root / path).resolve(strict=False)
        candidate.relative_to(root)
    except (OSError, ValueError) as exc:
        raise AssetResolverError("asset path escapes its configured root") from exc
    return candidate


def _normalized_hash(value: Any, pattern: re.Pattern[str], name: str) -> str | None:
    if value is None or value == "":
        return None
    result = str(value).strip().lower()
    if not pattern.fullmatch(result):
        raise AssetResolverError(f"invalid {name} in asset manifest")
    return result


def _metadata(
    value: Any, parent: Mapping[str, Any]
) -> tuple[str, dict[str, Any]] | None:
    if isinstance(value, str):
        return value, dict(parent)
    if not isinstance(value, Mapping):
        return None
    path = value.get("path") or value.get("file") or value.get("relative_path")
    if not isinstance(path, str) or not path.strip():
        return None
    result = dict(parent)
    result.update(value)
    return path, result


class AssetResolver:
    """Resolve manifest IDs in override, user, bundled, then reviewed-cache order."""

    def __init__(
        self,
        *,
        asset_root: str | Path = ASSET_ROOT,
        user_override_root: str | Path | None = None,
        cache_root: str | Path | None = None,
        manifest_path: str | Path | None = None,
    ) -> None:
        self.asset_root = _real_root(asset_root)
        self.user_root = _real_root(self.asset_root / "user")
        self.user_override_root = (
            _real_root(user_override_root) if user_override_root is not None else None
        )
        self.cache_root = _real_root(cache_root) if cache_root is not None else None
        self.manifest_path = Path(manifest_path or self.asset_root / "manifest.json")
        self._manifest = self._read_manifest(self.manifest_path)

    @staticmethod
    def _read_manifest(path: Path) -> dict[str, Any]:
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise AssetResolverError(f"unable to read asset manifest: {path}") from exc
        if not isinstance(value, Mapping) or value.get("schema_version") != 1:
            raise AssetResolverError("asset manifest schema_version must be 1")
        assets = value.get("assets")
        if not isinstance(assets, Mapping):
            raise AssetResolverError("asset manifest must contain an assets object")
        return dict(value)

    @property
    def manifest(self) -> Mapping[str, Any]:
        """Return the parsed manifest without exposing mutable resolver state."""

        return self._manifest

    @staticmethod
    def _validate_logical_id(logical_id: str) -> str:
        value = str(logical_id or "").strip().lower()
        if not _LOGICAL_ID_RE.fullmatch(value):
            raise AssetResolverError(f"invalid logical asset ID: {logical_id!r}")
        return value

    def _entry(self, logical_id: str) -> Mapping[str, Any] | None:
        value = self._manifest["assets"].get(logical_id)
        return value if isinstance(value, Mapping) else None

    def _candidates(
        self, logical_id: str, *, include_cache: bool
    ) -> Iterable[_Candidate]:
        entry = self._entry(logical_id)
        if entry is None:
            return ()
        shared = {
            key: entry[key]
            for key in (
                "attribution",
                "source_url",
                "license",
                "original_sha1",
                "sha256",
            )
            if key in entry
        }
        candidates: list[_Candidate] = []
        if self.user_override_root is not None:
            for key in ("override", "user_override", "user"):
                parsed = _metadata(entry.get(key), shared)
                if parsed is not None:
                    candidates.append(
                        _Candidate(
                            self.user_override_root,
                            parsed[0],
                            "user_override",
                            parsed[1],
                        )
                    )
                    break
        parsed = _metadata(entry.get("user"), shared)
        if parsed is not None:
            candidates.append(_Candidate(self.user_root, parsed[0], "user", parsed[1]))
        parsed = _metadata(entry.get("bundled"), shared)
        if parsed is not None:
            candidates.append(
                _Candidate(self.asset_root, parsed[0], "bundled", parsed[1])
            )
        if include_cache and self.cache_root is not None:
            cache_values = entry.get("cache")
            if not isinstance(cache_values, list):
                cache_values = [cache_values]
            for value in cache_values:
                parsed = _metadata(value, shared)
                if parsed is not None:
                    candidates.append(
                        _Candidate(self.cache_root, parsed[0], "cache", parsed[1])
                    )
        return tuple(candidates)

    def _resolve_candidates(
        self,
        logical_id: str,
        candidates: Iterable[_Candidate],
        expected_sha256: str | None,
    ) -> ResolvedVisualAsset | None:
        expected = _normalized_hash(expected_sha256, _SHA256_RE, "sha256")
        for candidate in candidates:
            try:
                path = _safe_relative(candidate.root, candidate.relative_path)
                if not path.is_file():
                    continue
                manifest_hash = _normalized_hash(
                    candidate.metadata.get("sha256"), _SHA256_RE, "sha256"
                )
                if (
                    expected is not None
                    and manifest_hash is not None
                    and expected != manifest_hash
                ):
                    continue
                actual = _sha256(path)
                if (manifest_hash or expected) and actual != (
                    expected or manifest_hash
                ):
                    continue
                _verify_png(path)
                original_sha1 = _normalized_hash(
                    candidate.metadata.get("original_sha1"), _SHA1_RE, "original_sha1"
                )
                return ResolvedVisualAsset(
                    path=path,
                    source=candidate.source,
                    logical_id=logical_id,
                    attribution=_optional_text(candidate.metadata.get("attribution")),
                    source_url=_optional_text(candidate.metadata.get("source_url")),
                    license=_optional_text(
                        candidate.metadata.get("license")
                        or candidate.metadata.get("license_id")
                    ),
                    original_sha1=original_sha1,
                )
            except (AssetResolverError, AssetValidationError, OSError):
                continue
        return None

    def resolve_local(
        self, logical_id: str, *, expected_sha256: str | None = None
    ) -> ResolvedVisualAsset | None:
        """Resolve override, user, or bundled assets without consulting cache."""

        normalized = self._validate_logical_id(logical_id)
        return self._resolve_candidates(
            normalized,
            self._candidates(normalized, include_cache=False),
            expected_sha256,
        )

    def resolve_cached(
        self, logical_id: str, *, expected_sha256: str | None = None
    ) -> ResolvedVisualAsset | None:
        """Resolve only an explicitly manifest-reviewed local cache candidate."""

        normalized = self._validate_logical_id(logical_id)
        candidates = tuple(self._candidates(normalized, include_cache=True))
        return self._resolve_candidates(
            normalized,
            (candidate for candidate in candidates if candidate.source == "cache"),
            expected_sha256,
        )

    def resolve(
        self,
        logical_id: str,
        *,
        expected_sha256: str | None = None,
        allow_cache: bool = True,
    ) -> ResolvedVisualAsset | None:
        """Resolve local assets first and optionally use a reviewed cache."""

        local = self.resolve_local(logical_id, expected_sha256=expected_sha256)
        if local is not None or not allow_cache:
            return local
        return self.resolve_cached(logical_id, expected_sha256=expected_sha256)

    def load_image(self, asset: ResolvedVisualAsset) -> Image.Image:
        """Load an already-resolved asset as a verified Pillow image."""

        _verify_png(asset.path)
        with Image.open(asset.path) as source:
            source.load()
            return source.copy()


def _optional_text(value: Any) -> str | None:
    text = str(value).strip() if value is not None else ""
    return text or None


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _verify_png(path: Path) -> None:
    if path.suffix.lower() != ".png":
        raise AssetValidationError("only local PNG assets are supported at runtime")
    try:
        with Image.open(path) as probe:
            if probe.format != "PNG" or probe.mode not in {"RGB", "RGBA"}:
                raise AssetValidationError("asset must be a PNG in RGB or RGBA mode")
            probe.verify()
        with Image.open(path) as image:
            if image.format != "PNG" or image.mode not in {"RGB", "RGBA"}:
                raise AssetValidationError("asset must be a PNG in RGB or RGBA mode")
            image.load()
    except (UnidentifiedImageError, OSError, ValueError) as exc:
        if isinstance(exc, AssetValidationError):
            raise
        raise AssetValidationError("asset is not a readable PNG") from exc


def resolve_local(
    logical_id: str,
    *,
    expected_sha256: str | None = None,
    user_override_root: str | Path | None = None,
    asset_root: str | Path = ASSET_ROOT,
    manifest_path: str | Path | None = None,
) -> ResolvedVisualAsset | None:
    """Module-level local resolver convenience API."""

    return AssetResolver(
        asset_root=asset_root,
        user_override_root=user_override_root,
        manifest_path=manifest_path,
    ).resolve_local(logical_id, expected_sha256=expected_sha256)


def resolve_cached(
    logical_id: str,
    *,
    expected_sha256: str | None = None,
    cache_root: str | Path | None = None,
    asset_root: str | Path = ASSET_ROOT,
    manifest_path: str | Path | None = None,
) -> ResolvedVisualAsset | None:
    """Module-level reviewed-cache resolver convenience API."""

    return AssetResolver(
        asset_root=asset_root,
        cache_root=cache_root,
        manifest_path=manifest_path,
    ).resolve_cached(logical_id, expected_sha256=expected_sha256)


def resolve(
    logical_id: str,
    *,
    expected_sha256: str | None = None,
    user_override_root: str | Path | None = None,
    cache_root: str | Path | None = None,
    asset_root: str | Path = ASSET_ROOT,
    manifest_path: str | Path | None = None,
    allow_cache: bool = True,
) -> ResolvedVisualAsset | None:
    """Module-level resolver convenience API; never performs external fetching."""

    return AssetResolver(
        asset_root=asset_root,
        user_override_root=user_override_root,
        cache_root=cache_root,
        manifest_path=manifest_path,
    ).resolve(logical_id, expected_sha256=expected_sha256, allow_cache=allow_cache)


__all__ = [
    "ASSET_ROOT",
    "AssetResolver",
    "AssetResolverError",
    "AssetValidationError",
    "ResolvedVisualAsset",
    "resolve",
    "resolve_cached",
    "resolve_local",
]
