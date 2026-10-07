"""
========================================
_bucket_fields.py — what a bucket's fields may hold
========================================

The caps every stored field is cut to, the enums of the v2 write-layer fields, the
clamps that bring a number into range (and say so: OB-W001 / OB-W002), and the
normalisers that turn whatever a caller passed into the form that reaches the
frontmatter. Reading a file back runs through the same place: `_sanitize_text`,
`_sanitize_float_field` and `_normalize_metadata_value` bound what a hand-edited or
imported YAML may carry.

Nothing here touches disk. BucketManager (core/bucket_manager.py) inherits FieldsMixin;
create() and update() (core/_bucket_write.py) call these before anything is written,
so a bad value is refused before it reaches a file.
========================================
"""

import math
import re
from datetime import date, datetime
from typing import Any, Optional

from utils import PROV_MAX_LINES, PROV_RELS, PROV_TARGET_MAX, parse_bool
from ._sources import normalize_sources

# The unified error system: clamping an out-of-range value reports OB-W001/OB-W002
# (rule.md §11)
try:
    from errors import push_warning as _ob_push_warning  # type: ignore
except Exception:
    try:
        from .errors import push_warning as _ob_push_warning  # type: ignore
    except Exception:
        def _ob_push_warning(*_a, **_kw):  # type: ignore
            return None


def _clamp_importance(v, source: str) -> int:
    """importance out of range -> clamp into [1,10], and raise an OB-W001 notice."""
    try:
        iv = int(v)
    except (TypeError, ValueError, OverflowError):
        _ob_push_warning("OB-W001", f"importance={v!r} 无法解析，回退为 5（{source}）")
        return 5
    if iv < 1 or iv > 10:
        clamped = max(1, min(10, iv))
        _ob_push_warning("OB-W001", f"importance={iv} 超出 [1,10]，已修正为 {clamped}（{source}）")
        return clamped
    return iv


def _clamp_unit(v, field: str, source: str) -> float:
    """valence/arousal out of range -> clamp into [0.0,1.0], and raise an OB-W002 notice."""
    try:
        fv = float(v)
    except (TypeError, ValueError):
        _ob_push_warning("OB-W002", f"{field}={v!r} 无法解析，回退为 0.5（{source}）")
        return 0.5
    if not math.isfinite(fv):
        _ob_push_warning("OB-W002", f"{field}={v!r} 不是有限数，回退为 0.5（{source}）")
        return 0.5
    if fv < 0.0 or fv > 1.0:
        clamped = max(0.0, min(1.0, fv))
        _ob_push_warning("OB-W002", f"{field}={fv} 超出 [0.0,1.0]，已修正为 {clamped}（{source}）")
        return clamped
    return fv


# ============================================================
# Field caps and defaults
# ------------------------------------------------------------
# rule.md §①: no bare magic numbers. The field truncation caps are gathered here; the
# search-scoring numbers are in core/bucket_manager.py ("search scoring").
# After changing any of these, run tests/regression to verify the scoring behaviour.
# ============================================================

# --- Default metadata values (kept in step with dehydrator/import_memory) ---
_DEFAULT_VALENCE = 0.5
_DEFAULT_AROUSAL = 0.3
_DEFAULT_IMPORTANCE = 5
_PINNED_IMPORTANCE = 10           # the importance a pinned/protected bucket is locked to
_DEFAULT_DOMAIN_NAME = "未分类"     # the placeholder used when no domain was supplied
_EDITABLE_BUCKET_TYPES = frozenset(
    {"dynamic", "permanent", "feel", "i", "self"}
)
# The write-layer fields of v2 (part 1 of the plan document). Absent is the default reading of each:
# no direction_of_fit = thetic (recording what is), no evidential = not marked,
# no internally_generated = it happened out there. So only a marked value is stored.
DIRECTIONS_OF_FIT = frozenset({"thetic", "telic"})
EVIDENTIALS = frozenset({"inference", "assumption"})
# RFC 5545 RRULE, the subset this version reads: yearly, on the date in `when`.
RECURRENCES = frozenset({"FREQ=YEARLY"})
# A hold (core/_holds.py) is a short telic entry hung on a standing one: exception_of =
# the id it is hung on, hold = how far it reaches. The two come together or not at all.
HOLD_LEVELS = frozenset({"defer", "avoid"})
# cue = "when I meet this, remember that": {condition, phrasings}. Writing one declares
# the entry is waiting on something, so a cue without a condition is no cue. phrasings
# are the ways it might be said, filled in later by the backfill; [] until then.
# card_of = the name this MIND entry is the card of (the names table's key, normalised by
# the tool that writes it). Only a MIND entry is a card; which MIND room, and one live card
# per name, are the write tools' checks (tools/grow/_cards.check_card).
# sources = the pieces of the host's material this memory was formed from, one record each
# (core/_sources.py: identity, revision, fingerprint, span, use). Whether a source may be
# used is the registry's question, asked by the write tools before they get here.
# looks_like_promise = the backfill read a promise in a sentence the main model did not mark
# telic. Telic stays the main model's switch; the mark is what the read side turns into a
# question. Stored only as true.
V2_FIELDS = ("direction_of_fit", "bound", "evidential", "internally_generated",
             "recurrence", "backfilled", "cue", "exception_of", "hold", "review_after",
             "card_of", "sources", "looks_like_promise")
_CUE_CONDITION_MAX = 200
_CUE_PHRASINGS_MAX_ITEMS = 16
_CUE_PHRASING_MAX = 200
_HOLD_TARGET_MAX = 64
_REVIEW_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

# --- Field truncation lengths, so the frontmatter cannot bloat ---
_SOURCE_TOOL_MAX = 32
_GROW_BATCH_ID_MAX = 64
_WHY_REMEMBERED_MAX = 500
# prov (where this came from) is not truncated: a cut target is half an id pointing at
# nothing. PROV_MAX_LINES lines of at most PROV_TARGET_MAX characters (utils), and
# anything past either is refused by _normalize_prov.
# --- Truncation lengths for three later fields ---
# room    = the room. The caller decides it and tools/_rooms.py validates it; the store
#           only stores it and has no opinion on its meaning.
# summary = the one-sentence summary. It is the foundation of "distant things get only
#           their summary", and the dehydrator backfills it in the background.
# when    = when the event happened (an ISO date string). recall's time gate reads it;
#           left empty it means the moment of storing.
_ROOM_MAX = 64
_SUMMARY_MAX = 200
_WHEN_MAX = 32
_DEFAULT_MAX_BUCKET_BYTES = 50 * 1024
_MAX_TAGS = 64
_MAX_TAG_CHARS = 128
_MAX_DOMAINS = 16
_MAX_DOMAIN_CHARS = 128
# --- subjects: the third of the three kinds of label ---
# Not many names appear in one memory, people and things together; the cap exists to stop a
# model having a fit and splitting an entire passage into names.
_MAX_SUBJECTS = 16
_MAX_BOUND = 8
_MAX_SUBJECT_CHARS = 64

# --- meaning / media: hold's experience-anchoring extension ---
# meaning is stored as list[str]: the same memory may be touched again at different
# moments, and each hold passes in the new entry, which is appended rather than
# overwriting what is there (see merge_or_create in tools/_common.py).
_MEANING_ITEM_MAX = 2000        # length cap on one meaning entry
_MEANING_LIST_MAX_ITEMS = 50    # how many meanings one bucket may accumulate
_MEDIA_MAX_ITEMS = 20           # how many media references one memory may carry
_MEDIA_PATH_MAX = 500
_MEDIA_TITLE_MAX = 200
_MEDIA_TYPE_MAX = 32
_MEDIA_NOTE_MAX = 500

# --- invalidation: marks that a basis of this memory changed under it (W3C PROV) ---
# A list of records {kind, of, by, at, confirmed_at?}, appended by regrow(mode="overturn")
# on every descendant of the overturned version, and by the confirm gesture (core/
# _invalidation.py) for a source revision or a panel edit looked at and kept, and by the
# panel's 内容错了 on a MIND entry (a `disputed` record with the owner's `note`). The memory
# keeps surfacing; the mark is shown wherever it is read by id. `confirmed_at` (a day) =
# looked at on that day and kept as it is: the record no longer counts as open. Capped so
# a basis overturned again and again cannot grow the frontmatter without bound: the newest
# stay. `of` / `by` hold a memory id or a source's string form (core/_sources.STRING_MAX).
_INVALIDATION_MAX_ITEMS = 32
_INVALIDATION_KIND_MAX = 32
_INVALIDATION_ID_MAX = 128
_INVALIDATION_AT_MAX = 32
_INVALIDATION_CHANGE_MAX = 200
_INVALIDATION_NOTE_MAX = 500     # core/_invalidation.NOTE_MAX

_METADATA_TEXT_LIMITS = {
    "status": 32,
    "type": 32,
    # Provenance stamps for name/summary: a single short enum-ish word ("fallback").
    "name_source": 32,
    "summary_source": 32,
    "resolution_reason": 500,
    "resolved_by": 128,
    "related_bucket": 128,
    # Closing a want records who closed it. The field name is deliberately kept separate
    # from the resolved_by above, which older data uses for a bucket_id or a
    # source label, while this one holds a person's name.
    "closed_by": 50,
    "author": 120,
    "user_name": 120,
    "title": 120,
    "why_remembered": _WHY_REMEMBERED_MAX,
    "source_tool": _SOURCE_TOOL_MAX,
    "grow_batch_id": _GROW_BATCH_ID_MAX,
    "last_merged_by": _SOURCE_TOOL_MAX,
    "_pre_anchor_source_tool": _SOURCE_TOOL_MAX,
}

_MAX_METADATA_DEPTH = 16
_MAX_METADATA_NODES = 10_000


def _clamp01(value, default: float) -> float:
    """Clamp any input into [0.0, 1.0]; on failure return `default`.

    This exists for the ``max(0.0, min(1.0, float(x)))`` boilerplate scattered through the
    body (model_valence, weight, bucket_type_defaults.weight and so on).
    The philosophical valence/arousal go through _clamp_unit instead, which pushes an
    OB-W002.
    This helper clamps silently, and suits the case where the caller already guarantees the
    range and this is at most a belt-and-braces check.
    """
    try:
        numeric = float(value)
    except (TypeError, ValueError):
        return default
    if not math.isfinite(numeric):
        return default
    return max(0.0, min(1.0, numeric))


class FieldsMixin:
    """The normalisers and sanitisers of BucketManager: pure, no disk."""

    def _max_bucket_bytes(self) -> int:
        raw = (self.config.get("limits") or {}).get(
            "max_bucket_bytes", _DEFAULT_MAX_BUCKET_BYTES
        )
        try:
            value = int(raw)
        except (TypeError, ValueError, OverflowError):
            return _DEFAULT_MAX_BUCKET_BYTES
        return value if value >= 0 else _DEFAULT_MAX_BUCKET_BYTES

    def _validate_bucket_content(self, content: str) -> None:
        cap = self._max_bucket_bytes()
        if cap <= 0:
            return
        size = len(content.encode("utf-8"))
        if size > cap:
            raise ValueError(
                f"内容过大（{size / 1024:.1f} KB > 上限 {cap / 1024:.0f} KB）。"
                "请拆分后存入，或调整 config.limits.max_bucket_bytes。"
            )

    def _v2_fields(self, **given) -> dict:
        """The v2 write-layer fields in their stored form; raises ValueError on a value
        outside the enums. A field at its default comes back as None, which update()
        reads as "remove the field" and create() drops."""
        out: dict = {}
        if "direction_of_fit" in given:
            v = str(given["direction_of_fit"] or "").strip()
            if v and v not in DIRECTIONS_OF_FIT:
                raise ValueError(f"direction_of_fit must be thetic or telic, got {v!r}")
            out["direction_of_fit"] = "telic" if v == "telic" else None
        if "bound" in given:
            names = self._normalize_metadata_list(
                given["bound"] or [], max_items=_MAX_BOUND, max_chars=_MAX_SUBJECT_CHARS)
            out["bound"] = names or None
        if "evidential" in given:
            v = str(given["evidential"] or "").strip()
            if v and v not in EVIDENTIALS:
                raise ValueError(f"evidential must be inference or assumption, got {v!r}")
            out["evidential"] = v or None
        if "internally_generated" in given:
            out["internally_generated"] = (
                True if parse_bool(given["internally_generated"], default=False) else None)
        if "recurrence" in given:
            v = str(given["recurrence"] or "").strip().upper()
            if v and v not in RECURRENCES:
                raise ValueError(f"recurrence: only FREQ=YEARLY is read, got {v!r}")
            out["recurrence"] = v or None
        if "backfilled" in given:
            fields = self._normalize_metadata_list(
                given["backfilled"] or [], max_items=16, max_chars=32)
            out["backfilled"] = fields or None
        if "cue" in given:
            out["cue"] = self._normalize_cue(given["cue"])
        if "exception_of" in given:
            v = self._sanitize_text(str(given["exception_of"] or "")).strip()
            if v and (len(v) > _HOLD_TARGET_MAX or re.search(r"\s", v)):
                raise ValueError(f"exception_of must be one bucket id, got {v[:_HOLD_TARGET_MAX + 1]!r}")
            out["exception_of"] = v or None
        if "hold" in given:
            v = str(given["hold"] or "").strip()
            if v and v not in HOLD_LEVELS:
                raise ValueError(f"hold must be defer or avoid, got {v!r}")
            out["hold"] = v or None
        if "review_after" in given:
            v = str(given["review_after"] or "").strip()
            if v:
                try:
                    real_day = bool(_REVIEW_DATE_RE.match(v)) and bool(
                        datetime.strptime(v, "%Y-%m-%d"))
                except ValueError:
                    real_day = False
                if not real_day:
                    raise ValueError(f"review_after must be one YYYY-MM-DD day, got {v!r}")
            out["review_after"] = v or None
        if "card_of" in given:
            v = self._sanitize_text(str(given["card_of"] or "")).strip()
            if v and (len(v) > _MAX_SUBJECT_CHARS or "\n" in v):
                raise ValueError(f"card_of must be one name, got {v[:_MAX_SUBJECT_CHARS + 1]!r}")
            out["card_of"] = v or None
        if "sources" in given:
            out["sources"] = normalize_sources(given["sources"]) or None
        if "looks_like_promise" in given:
            out["looks_like_promise"] = (
                True if parse_bool(given["looks_like_promise"], default=False) else None)
        return out

    @classmethod
    def _normalize_cue(cls, cue) -> Optional[dict]:
        """The stored form of `cue`: {condition, phrasings}. Empty (None, "", {}) is no
        cue, which update() reads as "remove it". A plain string is the condition. A cue
        that carries anything but has no condition raises ValueError: the condition is
        what makes it a cue."""
        if cue is None or cue == "" or cue == {}:
            return None
        if isinstance(cue, str):
            cue = {"condition": cue}
        if not isinstance(cue, dict):
            raise ValueError(f"cue must be {{condition, phrasings}}, got {type(cue).__name__}")
        condition = cls._sanitize_text(str(cue.get("condition") or "")).strip()
        if not condition:
            raise ValueError("cue needs a non-empty condition")
        return {
            "condition": condition[:_CUE_CONDITION_MAX],
            "phrasings": cls._normalize_metadata_list(
                cue.get("phrasings") or [], max_items=_CUE_PHRASINGS_MAX_ITEMS,
                max_chars=_CUE_PHRASING_MAX),
        }

    @classmethod
    def _normalize_metadata_list(
        cls,
        values,
        *,
        max_items: int,
        max_chars: int,
    ) -> list[str]:
        if values is None:
            return []
        if isinstance(values, str):
            values = [values]
        elif not isinstance(values, (list, tuple, set)):
            values = [values]
        normalized: list[str] = []
        for value in values:
            text = cls._sanitize_text(str(value)).strip()[:max_chars]
            if text and text not in normalized:
                normalized.append(text)
            if len(normalized) >= max_items:
                break
        return normalized

    @classmethod
    def _normalize_meaning_item(cls, text) -> str:
        """Trim one meaning entry. This is not summarisation, only a length cap."""
        if not text:
            return ""
        return cls._sanitize_text(str(text)).strip()[:_MEANING_ITEM_MAX]

    @classmethod
    def _normalize_meaning_list(cls, values) -> list[str]:
        """For wholesale replacement: trim each entry, drop the empty ones, and cap the count.

        It deliberately does not deduplicate: the same sentence written at two different
        moments is itself information, and deduplicating would erase that gap in time.
        """
        if not values:
            return []
        if isinstance(values, str):
            values = [values]
        normalized: list[str] = []
        for v in values:
            item = cls._normalize_meaning_item(v)
            if item:
                normalized.append(item)
            if len(normalized) >= _MEANING_LIST_MAX_ITEMS:
                break
        return normalized

    @classmethod
    def _normalize_invalidation(cls, records) -> list[dict]:
        """Keep each record to its four short text fields, plus `confirmed_at` when it is
        set; a record with no `kind` says nothing and is dropped. Order is kept, the newest
        `_INVALIDATION_MAX_ITEMS` win."""
        if not records:
            return []
        if isinstance(records, dict):
            records = [records]
        if not isinstance(records, (list, tuple)):
            return []
        out: list[dict] = []
        for rec in records:
            if not isinstance(rec, dict):
                continue
            kind = cls._sanitize_text(str(rec.get("kind") or "")).strip()[:_INVALIDATION_KIND_MAX]
            if not kind:
                continue
            row = {
                "kind": kind,
                "of": cls._sanitize_text(str(rec.get("of") or "")).strip()[:_INVALIDATION_ID_MAX],
                "by": cls._sanitize_text(str(rec.get("by") or "")).strip()[:_INVALIDATION_ID_MAX],
                "at": cls._sanitize_text(str(rec.get("at") or "")).strip()[:_INVALIDATION_AT_MAX],
            }
            confirmed = cls._sanitize_text(str(rec.get("confirmed_at") or "")).strip()
            if confirmed:
                row["confirmed_at"] = confirmed[:_INVALIDATION_AT_MAX]
            # A source_gone record also names the change that wrote it (host and
            # change_id), whether the body was cleared, and the change that lifted it.
            for key in ("change", "lifted_by"):
                value = cls._sanitize_text(str(rec.get(key) or "")).strip()
                if value:
                    row[key] = value[:_INVALIDATION_CHANGE_MAX]
            if rec.get("cleared") is True:
                row["cleared"] = True
            # A disputed record carries the owner's note (core/_invalidation.DISPUTED).
            note = cls._sanitize_text(str(rec.get("note") or "")).strip()
            if note:
                row["note"] = note[:_INVALIDATION_NOTE_MAX]
            out.append(row)
        return out[-_INVALIDATION_MAX_ITEMS:]

    @classmethod
    def _normalize_prov(cls, lines) -> list[dict]:
        """The stored form of `prov`: [{rel, target}], order kept, identical lines once.
        Raises ValueError on a rel outside PROV_RELS, a target that is empty, holds
        whitespace or is longer than PROV_TARGET_MAX, or more than PROV_MAX_LINES lines.
        Nothing is cut to fit: a shortened target or a dropped line would be a chain that
        silently says less than it was given. Whether a target exists is the tools'
        question, not this one."""
        if not lines:
            return []
        if isinstance(lines, dict):
            lines = [lines]
        if not isinstance(lines, (list, tuple)):
            raise ValueError(f"prov must be a list of {{rel, target}}, got {type(lines).__name__}")
        out: list[dict] = []
        for line in lines:
            if not isinstance(line, dict):
                raise ValueError(f"prov line must be {{rel, target}}, got {type(line).__name__}")
            rel = str(line.get("rel") or "").strip()
            if rel not in PROV_RELS:
                raise ValueError(f"prov rel must be one of {', '.join(PROV_RELS)}, got {rel!r}")
            target = cls._sanitize_text(str(line.get("target") or "")).strip()
            if not target or len(target) > PROV_TARGET_MAX or re.search(r"\s", target):
                raise ValueError(f"prov target must be one id of at most {PROV_TARGET_MAX} "
                                 f"characters, got {target[:PROV_TARGET_MAX + 1]!r}")
            entry = {"rel": rel, "target": target}
            if entry not in out:
                out.append(entry)
        if len(out) > PROV_MAX_LINES:
            raise ValueError(f"prov holds at most {PROV_MAX_LINES} lines, got {len(out)}")
        return out

    @classmethod
    def _normalize_media(cls, media) -> list[dict]:
        """Validate persisted media metadata; `path` must already have been stabilised by
        MediaStore."""
        if not media:
            return []
        if not isinstance(media, list):
            media = [media]
        normalized: list[dict] = []
        for item in media:
            if not isinstance(item, dict):
                continue
            path = cls._sanitize_text(str(item.get("path") or "")).strip()[:_MEDIA_PATH_MAX]
            if not path:
                continue
            entry: dict = {"path": path}
            title = item.get("title")
            if title:
                entry["title"] = cls._sanitize_text(str(title)).strip()[:_MEDIA_TITLE_MAX]
            media_type = item.get("type")
            if media_type:
                entry["type"] = cls._sanitize_text(str(media_type)).strip()[:_MEDIA_TYPE_MAX]
            note = item.get("note")
            if note:
                entry["note"] = cls._sanitize_text(str(note)).strip()[:_MEDIA_NOTE_MAX]
            digest = str(item.get("sha256") or "").lower()
            if re.fullmatch(r"[0-9a-f]{64}", digest):
                entry["sha256"] = digest
            try:
                size = int(item.get("size"))
            except (TypeError, ValueError, OverflowError):
                size = -1
            if size >= 0:
                entry["size"] = size
            if item.get("stored") is True:
                entry["stored"] = True
            normalized.append(entry)
            if len(normalized) >= _MEDIA_MAX_ITEMS:
                break
        return normalized

    # ---------------------------------------------------------
    # Sanitising what is read and written: control characters, old float spellings,
    # and the YAML graph a file may carry
    # ---------------------------------------------------------
    @staticmethod
    def _sanitize_text(text: str) -> str:
        """F-04 fix: strip NUL, dangerous control characters, and Unicode bidi
        override/isolate characters.

        \\n (LF), \\r (CR) and \\t (Tab) are preserved.
        What is stripped:
          U+0000~U+0008, U+000B, U+000C, U+000E~U+001F, U+007F (C0/C1 control characters)
          U+202A~U+202E bidi controls (LRE / RLE / PDF / LRO / RLO)
          U+2066~U+2069 bidi isolates (LRI / RLI / FSI / PDI)
        Emoji and CJK are unaffected.
        """
        _ctrl_table = {
            c: None
            for c in list(range(0x00, 0x09))    # 0x00..0x08
            + [0x0B, 0x0C]                       # VT, FF
            + list(range(0x0E, 0x20))            # 0x0E..0x1F
            + [0x7F]                             # DEL
            + list(range(0x202A, 0x202F))        # bidi controls 0x202A..0x202E
            + list(range(0x2066, 0x206A))        # bidi isolates 0x2066..0x2069
        }
        return str(text).translate(_ctrl_table)

    @staticmethod
    def _sanitize_float_field(value, default: float) -> float:
        """Extract a float from whatever format it arrives in (older ones such as `'V0.9'`,
        `'[我的视角:V0.3]'` and plain 0.9 are all accepted)."""
        if isinstance(value, (int, float)):
            numeric = float(value)
            if not math.isfinite(numeric):
                return default
            return max(0.0, min(1.0, numeric))
        try:
            nums = re.findall(r'[-+]?\d*\.?\d+', str(value))
            if not nums:
                return default
            numeric = float(nums[0])
            if not math.isfinite(numeric):
                return default
            return max(0.0, min(1.0, numeric))
        except Exception:
            return default

    @classmethod
    def _normalize_metadata_value(
        cls,
        value,
        *,
        _depth: int = 0,
        _seen: set[int] | None = None,
        _budget: list[int] | None = None,
    ):
        """Return bounded, alias-free JSON-safe YAML metadata.

        SafeLoader blocks object construction but still permits recursive and
        exponentially shared aliases.  Reject repeated containers and cap the
        expansion before rebuilding untrusted frontmatter into ordinary lists.
        """
        if _depth > _MAX_METADATA_DEPTH:
            raise ValueError("bucket metadata exceeds nesting-depth limit")
        if _seen is None:
            _seen = set()
        if _budget is None:
            _budget = [_MAX_METADATA_NODES]
        _budget[0] -= 1
        if _budget[0] < 0:
            raise ValueError("bucket metadata exceeds node limit")
        if isinstance(value, datetime):
            return value.isoformat()
        if isinstance(value, date):
            return value.isoformat()
        if value is None or isinstance(value, (str, bool, int)):
            return value
        if isinstance(value, float):
            # RFC 8259/JSON has no NaN or infinity.  Normalize YAML's .nan and
            # .inf scalars to null; known numeric fields below then apply their
            # documented defaults instead of poisoning dashboard responses.
            return value if math.isfinite(value) else None
        if isinstance(value, (bytes, bytearray, memoryview, set, frozenset)):
            raise ValueError(
                f"bucket metadata contains non-JSON-safe value: {type(value).__name__}"
            )
        if isinstance(value, dict):
            identity = id(value)
            if identity in _seen:
                raise ValueError("bucket metadata contains recursive/shared aliases")
            _seen.add(identity)
            normalized: dict[str, Any] = {}
            for key, item in value.items():
                if isinstance(key, datetime):
                    normalized_key = key.isoformat()
                elif isinstance(key, date):
                    normalized_key = key.isoformat()
                elif key is None or isinstance(key, (str, bool, int)):
                    normalized_key = str(key)
                elif isinstance(key, float) and math.isfinite(key):
                    normalized_key = str(key)
                else:
                    raise ValueError(
                        "bucket metadata contains a non-JSON mapping key"
                    )
                if normalized_key in normalized:
                    raise ValueError(
                        "bucket metadata contains colliding normalized keys"
                    )
                normalized[normalized_key] = cls._normalize_metadata_value(
                    item,
                    _depth=_depth + 1,
                    _seen=_seen,
                    _budget=_budget,
                )
            return normalized
        if isinstance(value, (list, tuple)):
            identity = id(value)
            if identity in _seen:
                raise ValueError("bucket metadata contains recursive/shared aliases")
            _seen.add(identity)
            return [
                cls._normalize_metadata_value(
                    v,
                    _depth=_depth + 1,
                    _seen=_seen,
                    _budget=_budget,
                )
                for v in value
            ]
        raise ValueError(
            f"bucket metadata contains unsupported scalar: {type(value).__name__}"
        )
