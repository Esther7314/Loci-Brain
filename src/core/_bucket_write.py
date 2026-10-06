"""
========================================
_bucket_write.py — how a write happens
========================================

create() and update(), and the two that rewrite a body through update's locked path
(replace_text_fields, update_content_fragment).

The order is the same for both:
  1. normalise what was passed (core/_bucket_fields.py); a bad value is refused here,
     before anything touches disk
  2. under the bucket's lease (`_bucket_turn`): read what is on disk, decide, and commit
     the Markdown atomically (core/_bucket_files.py)
  3. update the parsed listing in place (core/_bucket_cache.py)
  4. queue the derived vector, embed the newest meaning, append the ledger event
Markdown is the source of truth: a vector that fails is retried in the background and
never rolls the text back.

BucketManager (core/bucket_manager.py) inherits WriteMixin.
========================================
"""

import logging
import os
import re
import tempfile
import uuid
from typing import Any, Optional

import frontmatter

from utils import (
    LEGACY_FROM_FIELD,
    PROV_FIELD,
    busy_retry,
    generate_bucket_id,
    sanitize_name,
    safe_path,
    parse_bool,
)
from ._rooms import is_event_room
from ._sources import note_written
from ._bucket_fields import (
    V2_FIELDS,
    _DEFAULT_DOMAIN_NAME,
    _DEFAULT_VALENCE,
    _EDITABLE_BUCKET_TYPES,
    _GROW_BATCH_ID_MAX,
    _MAX_DOMAIN_CHARS,
    _MAX_DOMAINS,
    _MAX_SUBJECT_CHARS,
    _MAX_SUBJECTS,
    _MAX_TAG_CHARS,
    _MAX_TAGS,
    _MEANING_LIST_MAX_ITEMS,
    _MEDIA_MAX_ITEMS,
    _METADATA_TEXT_LIMITS,
    _PINNED_IMPORTANCE,
    _ROOM_MAX,
    _SOURCE_TOOL_MAX,
    _SUMMARY_MAX,
    _WHEN_MAX,
    _WHY_REMEMBERED_MAX,
    _clamp01,
    _clamp_importance,
    _clamp_unit,
)

logger = logging.getLogger("loci_brain.bucket")


def _atomic_create_text(path: str, text: str) -> None:
    """Publish a complete text file atomically, refusing an existing target.

    ``atomic_write_text`` intentionally replaces its destination, which is the
    right behavior for updates but unsafe for creation races.  Build the full
    file beside the destination and publish it with a hard link: link creation
    is atomic and fails with ``FileExistsError`` instead of overwriting.
    """

    target = os.path.abspath(path)
    parent = os.path.dirname(target)
    os.makedirs(parent, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(
        prefix=f".{os.path.basename(target)}.create.",
        dir=parent,
    )
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.link(temporary, target)
    finally:
        try:
            os.unlink(temporary)
        except OSError:
            pass


class WriteMixin:
    """create(), update() and the derived indexes after a write."""

    # ---------------------------------------------------------
    # Create a new bucket
    # Write content and metadata into a .md file
    # ---------------------------------------------------------
    async def create(
        self,
        content: str,
        tags: Optional[list[str]] = None,
        importance: int = 5,
        domain: Optional[list[str]] = None,
        valence: float = 0.5,
        arousal: float = 0.3,
        bucket_type: str = "dynamic",
        name: Optional[str] = None,
        pinned: bool = False,
        protected: bool = False,
        why_remembered: str = "",
        # Where this came from: [{rel, target}] (utils, `prov`). The caller has already
        # picked each rel from its route and checked what has to exist.
        prov: Optional[list[dict]] = None,
        weight: Optional[float] = None,
        source_tool: str = "",
        grow_batch_id: str = "",
        bucket_id_override: str = "",
        allow_embedding_fallback: bool = False,
        meaning: str = "",
        media: Any = None,
        test_data: bool = False,
        room: str = "",
        summary: str = "",
        when: str = "",
        subjects: Optional[list[str]] = None,
        direction_of_fit: str = "",
        bound: Optional[list[str]] = None,
        evidential: str = "",
        internally_generated: bool = False,
        recurrence: str = "",
        backfilled: Optional[list[str]] = None,
        cue: Any = None,
        exception_of: str = "",
        hold: str = "",
        review_after: str = "",
        card_of: str = "",
        sources: Any = None,
        looks_like_promise: bool = False,
    ) -> str:
        """
        Create a new memory bucket, return bucket ID.

        pinned/protected=True: bucket won't be merged, decayed, or have importance changed.
        Importance is locked to 10 for pinned/protected buckets.

        Provenance:
        - source_tool: "hold" | "grow" — which tool created it. feel goes through the hold
          branch, so a feel bucket has source_tool="hold" and is told apart by bucket_type.
        - grow_batch_id: every bucket split out of one grow call shares a batch_id, so the
          dashboard can group them by batch.
        - bucket_id_override: a readable id supplied by the caller (feel uses
          ``feel_202605011423_V085``). On a collision with an existing bucket a
          second-resolution suffix is appended automatically.
          Empty -> fall back to ``generate_bucket_id()`` (12 hex characters).
        """
        # ``allow_embedding_fallback`` is retained for API compatibility.
        # All memory types now write first; embedding is a derived index.

        # F-04: strip dangerous control characters and bidi overrides out of content, tags and name
        content = self._sanitize_text(content)
        self._validate_bucket_content(content)
        if name:
            name = self._sanitize_text(name)

        # Candidate selection is finalized immediately before the no-overwrite
        # write while holding that exact ID's normal bucket turn.  The value
        # here is provisional so metadata normalization can include a useful
        # source label without doing an unsafe check-then-write.
        preferred_bucket_id = (
            sanitize_name(bucket_id_override) or generate_bucket_id()
            if bucket_id_override
            else generate_bucket_id()
        )
        bucket_name = self._create_name(name)
        domain, tags = self._create_labels(bucket_type, domain, tags)
        linked_content = content  # wikilink injection disabled; LLM adds [[]] via prompt

        # --- Build the YAML frontmatter metadata, one step at a time. A value that is
        #     refused raises here, before anything reaches disk. ---
        metadata = self._create_metadata(
            preferred_bucket_id, bucket_name, bucket_type, tags, domain,
            valence=valence, arousal=arousal, importance=importance, pinned=pinned,
            protected=protected, test_data=test_data, source_tool=source_tool)
        self._create_provenance(
            metadata, source_tool=source_tool, grow_batch_id=grow_batch_id,
            why_remembered=why_remembered, prov=prov)
        self._create_descriptions(
            metadata, room=room, subjects=subjects, summary=summary, when=when,
            meaning=meaning)
        self._create_v2_fields(
            metadata, direction_of_fit=direction_of_fit, bound=bound, evidential=evidential,
            internally_generated=internally_generated, recurrence=recurrence,
            backfilled=backfilled, cue=cue, exception_of=exception_of, hold=hold,
            review_after=review_after, card_of=card_of,
            sources=sources, looks_like_promise=looks_like_promise)
        self._create_weight_and_defaults(metadata, bucket_type, weight, why_remembered)
        self._create_first_of_kind(metadata, tags)

        # --- Choose the directory, then pick the final id and publish the file under
        #     that id's lease ---
        target_dir, primary_domain = self._create_target_dir(bucket_type, pinned, domain)
        bucket_id, candidate_path = await self._create_publish(
            preferred_bucket_id, bucket_id_override, bucket_name, target_dir, metadata,
            linked_content, media)

        # Markdown becomes the visible source of truth before any network or
        # derived-index await: the new entry joins the parsed listing in place.
        self._cache_upsert(bucket_id, candidate_path)
        # The file is on disk: a keyed write (core/_sources.run_once) claims this id.
        note_written(bucket_id)

        logger.info(
            f"Created bucket / 创建记忆桶: {bucket_id} ({bucket_name}) → {primary_domain}/"
            + (" [PINNED]" if pinned else "") + (" [PROTECTED]" if protected else "")
        )

        # Markdown is committed before any derived-index work. The managed
        # server enqueues and returns immediately; standalone mode tries once.
        await self._index_after_write(bucket_id, linked_content)
        # meaning gets an embedding of its own rather than being concatenated into content
        # and embedded together. Concatenating would let a long content dominate the vector
        # and dilute the signal of a one-sentence meaning; stored separately, retrieval takes
        # the higher of the two similarities and a single sentence of feeling can be hit on
        # its own.
        # Best effort: a failure only logs a warning and does not affect the fact that the
        # bucket is already on disk.
        await self._sync_meaning_embedding(bucket_id, metadata.get("meaning") or [])
        self._record_ledger_event(
            "TraceCreated",
            bucket_id,
            str(metadata.get("type") or bucket_type),
            linked_content,
            metadata,
        )

        return bucket_id

    # ---------------------------------------------------------
    # create()'s steps, in the order create() runs them
    # ---------------------------------------------------------
    def _create_name(self, name: Optional[str]) -> str:
        """The bucket's name, which is also the stem of its file name."""
        # The bucket name is "YYYY-MM-DD HH-MM-SS [title generated by the LLM]", or just
        # the timestamp when there is no title.
        # Hyphens are used instead of colons, so that a later sanitize_name pass cannot
        # strip the colons and wreck readability.
        # The name and filename use the **local** time: something stored after midnight
        # carrying yesterday afternoon's name grates every time it is read. Only this naming
        # layer changed; created/last_active remain suffix-less UTC (the three conventions
        # in tools/_when — changing those would shift the whole history by eight hours).
        try:
            from ._when import now as _local_now
            _ts = _local_now().strftime("%Y-%m-%d %H-%M-%S")
        except Exception:
            _ts = self._datetime_now().strftime("%Y-%m-%d %H-%M-%S")
        _clean = sanitize_name(name) if name else ""
        bucket_name = (f"{_ts} {_clean}" if (_clean and _clean != "unnamed") else _ts)[:80]
        return bucket_name

    def _create_labels(
        self,
        bucket_type: str,
        domain: Optional[list[str]],
        tags: Optional[list[str]],
    ) -> tuple[list[str], list[str]]:
        """domain and tags in their stored form."""
        # feel buckets are allowed to have empty domain; others default to ["未分类"]
        if bucket_type == "feel":
            domain = domain if domain is not None else []
        else:
            domain = domain or [_DEFAULT_DOMAIN_NAME]
        domain = self._normalize_metadata_list(
            domain,
            max_items=_MAX_DOMAINS,
            max_chars=_MAX_DOMAIN_CHARS,
        )
        if bucket_type != "feel" and not domain:
            domain = [_DEFAULT_DOMAIN_NAME]
        tags = self._normalize_metadata_list(
            tags,
            max_items=_MAX_TAGS,
            max_chars=_MAX_TAG_CHARS,
        )
        return domain, tags

    def _create_metadata(
        self,
        bucket_id: str,
        bucket_name: str,
        bucket_type: str,
        tags: list[str],
        domain: list[str],
        *,
        valence: float,
        arousal: float,
        importance: int,
        pinned: bool,
        protected: bool,
        test_data: bool,
        source_tool: str,
    ) -> dict:
        """The frontmatter every bucket starts with: id, name, labels, the clamped
        coordinates and importance, the timestamps, and the pinned / protected / test
        marks. `bucket_id` is the provisional id; _create_publish sets the final one."""
        # --- Pinned/protected buckets: lock importance to 10 ---
        if pinned or protected:
            importance = _PINNED_IMPORTANCE

        # --- Build the YAML frontmatter metadata ---
        # Out-of-range values are not clamped silently: they raise an OB-W001/OB-W002 that
        # travels to the end of the MCP return value
        metadata = {
            "id": bucket_id,
            "name": bucket_name,
            "tags": tags,
            "domain": domain,
            "valence": _clamp_unit(valence, "valence", f"create:{bucket_id}"),
            "arousal": _clamp_unit(arousal, "arousal", f"create:{bucket_id}"),
            "importance": _clamp_importance(importance, f"create:{bucket_id}"),
            "type": bucket_type,
            "created": self._now_iso(),
            "last_active": self._now_iso(),
            "activation_count": 0,
        }
        if test_data:
            metadata["provenance"] = {
                "kind": "test",
                "created_by": str(source_tool or "developer")[:_SOURCE_TOOL_MAX],
                "erasable": True,
            }
        if pinned:
            metadata["pinned"] = True
        if protected:
            metadata["protected"] = True
        if bucket_type == "permanent" or pinned:
            metadata["type"] = "permanent"
        return metadata

    def _create_provenance(
        self,
        metadata: dict,
        *,
        source_tool: str,
        grow_batch_id: str,
        why_remembered: str,
        prov: Optional[list[dict]],
    ) -> None:
        """Which tool wrote it, the grow batch, why it is worth remembering, and where it
        came from (`prov`, refused whole with ValueError)."""
        # --- The source tool and the grow batch ---
        # An empty source_tool means the caller did not declare one (older callers), and
        # nothing is written into the frontmatter.
        # Only the grow path passes grow_batch_id; hold and feel never carry that field.
        if source_tool:
            metadata["source_tool"] = str(source_tool).strip()[:_SOURCE_TOOL_MAX]
        if grow_batch_id:
            metadata["grow_batch_id"] = str(grow_batch_id).strip()[:_GROW_BATCH_ID_MAX]

        # --- Let a memory carry "why this is worth remembering" ---
        # A free-text field, written by the model or by hand. It takes no part in scoring;
        # it only appears in display and search.
        # An empty string means no reason was given, and the dashboard simply omits the row.
        if why_remembered:
            metadata["why_remembered"] = str(why_remembered).strip()[:_WHY_REMEMBERED_MAX]
        # --- Where this came from: `prov` (refused whole, before anything is written) ---
        prov_lines = self._normalize_prov(prov)
        if prov_lines:
            metadata[PROV_FIELD] = prov_lines

    def _create_descriptions(
        self,
        metadata: dict,
        *,
        room: str,
        subjects: Optional[list[str]],
        summary: str,
        when: str,
        meaning: str,
    ) -> None:
        """room, subjects, summary, when, and the first meaning."""
        # --- room / summary / when ---
        # The semantic validation of `room` lives in tools/_rooms.py and the caller decides
        # it; the store only stores it and has no opinion on its meaning.
        # `summary` is backfilled in the background by the dehydrator (it is on update's
        # whitelist too).
        # `when` is when the event happened; left empty it means the moment of storing, so
        # `created` can simply be read and nothing redundant is written.
        if room:
            metadata["room"] = self._sanitize_text(str(room)).strip()[:_ROOM_MAX]
        # --- subjects: the **third kind** of label, namely who ---
        # 🔴 It is a field of its own and must never be mixed in: mixed into tags it would
        #    break the guarantee that a tag appears literally in the body; mixed into
        #    aliases it would enter BM25 scoring, and who a memory is about should not move
        #    relevance.
        # The model extracts it and it is normalised through the alias table, so no extra
        # field has to be written by hand.
        if subjects:
            metadata["subjects"] = self._normalize_metadata_list(
                subjects, max_items=_MAX_SUBJECTS, max_chars=_MAX_SUBJECT_CHARS)
        if summary:
            metadata["summary"] = self._sanitize_text(str(summary)).strip()[:_SUMMARY_MAX]
        if when:
            metadata["when"] = str(when).strip()[:_WHEN_MAX]
        # --- meaning / media: why I myself think this memory is worth being recalled ---
        # meaning is a list[str]: on creation it holds one entry (the sentence this hold
        # passed in), and every later hold or trace appends to the same list. media is an
        # opaque list of external references.
        # Both are optional, neither depends on the other, and neither takes part in scoring.
        meaning_item = self._normalize_meaning_item(meaning)
        if meaning_item:
            metadata["meaning"] = [meaning_item]

    def _create_v2_fields(self, metadata: dict, **given) -> None:
        """The v2 write-layer fields that are not at their default; ValueError on a value
        outside its enum, half a hold, or a card on an event."""
        # --- v2 write-layer fields (validated here too: a bad value never reaches disk) ---
        metadata.update({k: v for k, v in self._v2_fields(**given).items() if v is not None})
        if bool(metadata.get("exception_of")) != bool(metadata.get("hold")):
            raise ValueError("exception_of and hold come together: a hold names what it "
                             "is hung on and how far it reaches")
        if metadata.get("card_of") and is_event_room(metadata.get("room")):
            raise ValueError("card_of is for MIND entries: an event is not the card of a name")

    def _create_weight_and_defaults(
        self,
        metadata: dict,
        bucket_type: str,
        weight: Optional[float],
        why_remembered: str,
    ) -> None:
        """The weight of a promise, then config.bucket_type_defaults where the caller
        passed nothing."""
        # --- "weight of the promise", 0.0-1.0, which is not importance ---
        # importance = how important this thing is; weight = how heavily it presses on me.
        # It belongs to what is wanted (telic).
        if metadata.get("direction_of_fit") == "telic" and weight is not None:
            metadata["weight"] = _clamp01(weight, _DEFAULT_VALENCE)
        # --- bucket_type_defaults: per-type default values ---
        # config.bucket_type_defaults may hold {<type>: {weight: 1.0, dont_surface: false}, ...}
        # and is applied only where the caller passed no explicit value.
        # An older config without that section is skipped silently.
        try:
            type_defaults = (self.config.get("bucket_type_defaults") or {}).get(bucket_type, {})
            if type_defaults:
                if "weight" in type_defaults and "weight" not in metadata and weight is None:
                    metadata["weight"] = _clamp01(type_defaults["weight"], _DEFAULT_VALENCE)
                if "dont_surface" in type_defaults and "dont_surface" not in metadata:
                    if parse_bool(type_defaults["dont_surface"], default=False):
                        metadata["dont_surface"] = True
                if "why_remembered" in type_defaults and not why_remembered:
                    metadata["why_remembered"] = str(type_defaults["why_remembered"]).strip()[:_WHY_REMEMBERED_MAX]
        except Exception as e:
            logger.warning(f"bucket_type_defaults apply failed / 类型默认值应用失败: {e}")
        # --- The deliberate-forgetting switch, defaulting to False. A new bucket does not
        #     write it into the frontmatter, to save space ---
        # It only appears there after an update(dont_surface=True).

    def _create_first_of_kind(self, metadata: dict, tags: list[str]) -> None:
        """Mark the bucket first_of_kind when none of its tags is in the store yet."""
        # --- first_of_kind, decided automatically ---
        # The rule: this bucket's tags share nothing at all with the tags already in the
        # store -> this is a "first time".
        # Only buckets that carry tags are judged; a bucket with no tags is never marked.
        if tags:
            try:
                existing_tags = self._collect_all_tags()
                if existing_tags is not None and not (set(tags) & existing_tags):
                    metadata["first_of_kind"] = True
            except Exception as e:
                # A failure here must not block the main write path
                logger.warning(f"first_of_kind check failed / 首次标记检测失败: {e}")

    def _create_target_dir(
        self,
        bucket_type: str,
        pinned: bool,
        domain: list[str],
    ) -> tuple[str, str]:
        """The directory the new file goes in (created if missing), and the primary
        domain that names it."""
        # --- Choose directory by type + primary domain ---
        if bucket_type == "permanent" or pinned:
            type_dir = self.permanent_dir
        elif bucket_type == "feel":
            type_dir = self.feel_dir
        else:
            type_dir = self.dynamic_dir
        if bucket_type == "feel":
            primary_domain = "沉淀物"  # feel subfolder name
        else:
            primary_domain = self._primary_domain(domain)
        target_dir = os.path.join(type_dir, primary_domain)
        os.makedirs(target_dir, exist_ok=True)
        return target_dir, primary_domain

    async def _create_publish(
        self,
        preferred_bucket_id: str,
        bucket_id_override: str,
        bucket_name: str,
        target_dir: str,
        metadata: dict,
        linked_content: str,
        media: Any,
    ) -> tuple[str, str]:
        """Pick the final id and publish the Markdown. Each candidate id is tried under its
        own bucket lease (the same one update / delete take); the file is created with a
        no-overwrite write, so a collision of any kind moves on to the next candidate.
        Returns (bucket_id, file path)."""
        bucket_id = preferred_bucket_id

        def _candidate_ids():
            yield preferred_bucket_id
            if bucket_id_override:
                yield f"{preferred_bucket_id}_{self._datetime_now().strftime('%S')}"
                for _attempt in range(5):
                    yield f"{preferred_bucket_id}_{uuid.uuid4().hex[:2]}"
            while True:
                yield generate_bucket_id()

        # Finalize the ID, persist any ID-keyed media and publish the Markdown
        # while holding the same turn used by update/migrate/delete.  The
        # second existence check closes the former create-vs-migrate TOCTOU.
        collision_count = 0
        for candidate_id in _candidate_ids():
            async with self._bucket_turn(candidate_id):
                # scan_if_missing=False: this call **expects to find nothing** (the id was
                # just generated), and taking the fallback scan would mean walking the whole
                # store for nothing on every single grow.
                # A genuine collision still has two more layers below: os.path.exists, and a
                # no-overwrite write.
                if self._find_bucket_file(candidate_id, scan_if_missing=False):
                    collision_count += 1
                    continue

                if bucket_name and bucket_name != candidate_id:
                    filename = f"{bucket_name}_{candidate_id}.md"
                else:
                    filename = f"{candidate_id}.md"
                candidate_path = safe_path(target_dir, filename)
                if os.path.exists(candidate_path):
                    collision_count += 1
                    continue

                bucket_id = candidate_id
                metadata["id"] = bucket_id
                metadata.pop("media", None)
                persisted_media = await self.media_store.persist(bucket_id, media)
                normalized_media = self._normalize_media(persisted_media)
                if normalized_media:
                    metadata["media"] = normalized_media

                post = frontmatter.Post(  # type: ignore[arg-type]
                    linked_content,
                    **metadata,
                )
                try:
                    # Publish the file and its ID lookup entry under the same
                    # path-index guard.  The collision check above can leave a
                    # complete, ready index that does not yet contain this new
                    # ID; without this hand-off an outbox worker can observe
                    # the committed Markdown as "missing" and discard its
                    # embedding task while create() awaits meaning indexing.
                    with self._bucket_path_index_guard:
                        _atomic_create_text(
                            candidate_path, frontmatter.dumps(post)
                        )
                        self._bucket_path_index[bucket_id] = candidate_path
                except FileExistsError:
                    # An unmanaged writer may not honor the bucket turn.  The
                    # no-overwrite publish still protects its file; choose a
                    # fresh ID instead of replacing it.
                    collision_count += 1
                    continue
                except OSError as e:
                    logger.error(
                        "Failed to write bucket file / 写入桶文件失败: "
                        "%s: %s",
                        candidate_path,
                        e,
                    )
                    raise
                break

        if collision_count > 6:
            logger.warning(
                "bucket_id_override %r repeatedly conflicted; used random id %s",
                preferred_bucket_id,
                bucket_id,
            )

        return bucket_id, candidate_path

    # ---------------------------------------------------------
    # Wikilink injection — DISABLED
    # Now handled by LLM prompts (the model adds [[]] around proper nouns)
    # ---------------------------------------------------------
    # def _apply_wikilinks(self, content, tags, domain, name): ...
    # def _collect_wikilink_keywords(self, content, tags, domain, name): ...
    # def _normalize_keywords(self, keywords): ...
    # def _extract_auto_keywords(self, content): ...

    # ---------------------------------------------------------
    # Update bucket
    # Supports: content, tags, importance, valence, arousal, name, resolved
    # ---------------------------------------------------------
    async def update(
        self,
        bucket_id: str,
        *,
        allow_embedding_fallback: bool = False,
        bump_active: bool = False,
        **kwargs,
    ) -> bool:
        """
        Update bucket content or metadata fields.

        bump_active=False (the default): a pure metadata or content edit — trace,
        anchor, a background auto-resolve, an import — which does **not** refresh
        last_active and does not touch activation_count.
        bump_active=True: treat this write as a genuine activation (hold/grow merging into a
        neighbouring bucket, say), refreshing last_active and incrementing
        activation_count, with the same meaning as touch().
        revise=fn: fn(metadata as on disk, read under the lock) -> keywords to write
        (see _update_locked); for appends and fill-only-if-blank writes.
        """
        async with self._bucket_turn(bucket_id):
            committed = await self._update_locked(
                bucket_id,
                allow_embedding_fallback=allow_embedding_fallback,
                bump_active=bump_active,
                **kwargs,
            )
        # What this call wrote, for a keyed write's claim (core/_sources.run_once).
        if committed:
            note_written(bucket_id)
        return committed

    async def _update_locked(
        self,
        bucket_id: str,
        *,
        allow_embedding_fallback: bool = False,
        bump_active: bool = False,
        **kwargs,
    ) -> bool:
        """update()'s body, run while the caller holds the bucket's lease (update,
        replace_text_fields, update_content_fragment). False writes nothing.

        The steps, in order: apply `revise` to what is on disk; normalise the arguments
        (_update_normalize_args); read the file; settle pin / type / importance
        (_update_settle_pin_and_type); write the typed fields (_update_apply_typed_fields),
        the v2 fields and the pass-through list (_update_apply_pass_through); check the
        hold and card pairs; bump the activation when asked; commit, moving the file when
        its type or domain moved it (_update_commit); then the listing, the vectors and
        the ledger."""
        revise = kwargs.pop("revise", None)
        file_path = self._find_bucket_file(bucket_id)
        if not file_path:
            return False

        # `revise`: a function of the entry's metadata as it is on disk now, under this
        # bucket's lock, returning the keywords to write (None writes nothing and reports
        # failure, {} writes nothing). Every write that depends on what is already there —
        # appending to a list, filling only a blank — is decided here, so a write landing
        # between someone's read and their write cannot be overwritten by a stale copy.
        if revise is not None:
            current = self._load_bucket(file_path)
            if current is None:
                return False
            more = revise(dict(current.get("metadata") or {}))
            if more is None:
                return False
            kwargs.update(more)
            if not kwargs:
                return True

        if not await self._update_normalize_args(bucket_id, kwargs):
            return False

        try:
            post = busy_retry(lambda: frontmatter.load(file_path))
        except Exception as e:
            logger.warning(f"Failed to load bucket for update / 加载桶失败: {file_path}: {e}")
            return False

        current_type = str(post.get("type") or "dynamic").strip().lower()
        if not self._update_settle_pin_and_type(bucket_id, post, kwargs, current_type):
            return False

        self._update_apply_typed_fields(bucket_id, post, kwargs)
        # --- v2 write-layer fields: stored form; None (= the default) removes the field ---
        _v2_given = {k: kwargs[k] for k in V2_FIELDS if k in kwargs}
        if _v2_given:
            try:
                kwargs.update(self._v2_fields(**_v2_given))
            except ValueError as exc:
                logger.warning(f"update() refused {bucket_id}: {exc}")
                return False
        if not await self._update_apply_pass_through(bucket_id, post, kwargs):
            return False
        # A hold is both halves or neither: half of one is a pointer that holds nothing,
        # or a level hung on nothing.
        if bool(post.get("exception_of")) != bool(post.get("hold")):
            logger.warning(f"update() refused {bucket_id}: exception_of and hold come together")
            return False
        if post.get("card_of") and is_event_room(post.get("room")):
            logger.warning(f"update() refused {bucket_id}: card_of is for MIND entries")
            return False

        # --- Activation time and activation count ---
        # last_active means "the last genuine activation or recall" and nothing else, and it
        # is the input to decay's recency scoring.
        # A metadata edit — trace, anchor, a background auto-resolve — **does not
        # count as activity**: refreshing it unconditionally here would reset the forgetting
        # clock, and would also leave activation_count and last_active permanently out of
        # step (the count static while the timestamp keeps getting newer). Only a genuine new
        # event write treats the memory as activated again, which bump_active=True triggers
        # explicitly (hold/grow merging into a neighbouring bucket, say), refreshing
        # last_active and incrementing activation_count with the same meaning as touch().
        if bump_active:
            post["last_active"] = self._now_iso()
            post["activation_count"] = int(post.get("activation_count") or 0) + 1

        committed_path = self._update_commit(bucket_id, file_path, post, current_type)
        if committed_path is None:
            return False

        if bump_active:
            self._cache_bump(
                bucket_id,
                last_active=post["last_active"],
                activation_count=post["activation_count"],
                file_path=committed_path,
            )
        # The parsed listing takes the new form now, before any await below.
        self._cache_upsert(bucket_id, committed_path, file_path)

        logger.info(f"Updated bucket / 更新记忆桶: {bucket_id}")

        # Content is already committed. Queue the derived vector without
        # turning provider failure into a false "memory write failed" result.
        if "content" in kwargs:
            await self._index_after_write(bucket_id, post.content or "")
        # meaning has an embedding of its own, so a change to content and a change to
        # meaning each trigger their own regeneration.
        if "meaning" in kwargs or "meaning_append" in kwargs:
            await self._sync_meaning_embedding(bucket_id, post.get("meaning") or [])
        self._record_ledger_event(
            "TraceUpdated",
            bucket_id,
            str(post.get("type") or "dynamic"),
            post.content or "",
            dict(post.metadata),
            {"changed_fields": sorted(str(k) for k in kwargs.keys())},
        )

        return True

    # ---------------------------------------------------------
    # _update_locked()'s steps, in the order it runs them
    # ---------------------------------------------------------
    async def _update_normalize_args(self, bucket_id: str, kwargs: dict) -> bool:
        """Bring each passed value into its stored form, in place. Content that is too
        large raises ValueError; a bad `prov` line refuses the whole update (False).
        Media is persisted here (MediaStore), before the file is read."""
        # Normalize public/migration inputs at the storage boundary.  A quoted
        # YAML value such as "false" must never be persisted as true merely
        # because Python considers non-empty strings truthy.
        for field in (
            "resolved",
            "pinned",
            "digested",
            "dont_surface",
            "first_of_kind",
            "anchor",
        ):
            if field in kwargs:
                kwargs[field] = parse_bool(kwargs[field])

        if "content" in kwargs:
            kwargs["content"] = self._sanitize_text(kwargs["content"])
            self._validate_bucket_content(kwargs["content"])
        if "tags" in kwargs:
            kwargs["tags"] = self._normalize_metadata_list(
                kwargs["tags"],
                max_items=_MAX_TAGS,
                max_chars=_MAX_TAG_CHARS,
            )
        if "aliases" in kwargs:
            # The expansion words: they feed the bm25 index only and never appear on the tag
            # row a human reads
            kwargs["aliases"] = self._normalize_metadata_list(
                kwargs["aliases"],
                max_items=_MAX_TAGS,
                max_chars=_MAX_TAG_CHARS,
            )
        if "subjects" in kwargs:
            # subjects, the third kind of label. It enters neither tags nor aliases; see create().
            kwargs["subjects"] = self._normalize_metadata_list(
                kwargs["subjects"],
                max_items=_MAX_SUBJECTS,
                max_chars=_MAX_SUBJECT_CHARS,
            )
        if "domain" in kwargs:
            kwargs["domain"] = self._normalize_metadata_list(
                kwargs["domain"],
                max_items=_MAX_DOMAINS,
                max_chars=_MAX_DOMAIN_CHARS,
            ) or [_DEFAULT_DOMAIN_NAME]
        if "media" in kwargs:
            # media is a wholesale overwrite (trace's media_replace). An empty list clears the field.
            kwargs["media"] = self._normalize_media(
                await self.media_store.persist(bucket_id, kwargs["media"])
            )
        if "media_append" in kwargs:
            # media_append appends (trace's media_append, and every hold call).
            kwargs["media_append"] = self._normalize_media(
                await self.media_store.persist(bucket_id, kwargs["media_append"])
            )
        if "meaning" in kwargs:
            # meaning is a wholesale overwrite (trace's meaning_replace, for corrections and cleanup).
            kwargs["meaning"] = self._normalize_meaning_list(kwargs["meaning"])
        if "meaning_append" in kwargs:
            # meaning_append appends one new meaning (trace's meaning_append, and every hold call).
            kwargs["meaning_append"] = self._normalize_meaning_item(kwargs["meaning_append"])
        if "invalidation" in kwargs:
            # The whole list is written each time; the caller appends to what it read.
            kwargs["invalidation"] = self._normalize_invalidation(kwargs["invalidation"])
        if PROV_FIELD in kwargs:
            # The whole list is written each time, like invalidation. A bad line refuses
            # the whole update rather than reaching disk.
            try:
                kwargs[PROV_FIELD] = self._normalize_prov(kwargs[PROV_FIELD])
            except ValueError as exc:
                logger.warning(f"update() refused {bucket_id}: {exc}")
                return False
        return True

    def _update_settle_pin_and_type(
        self,
        bucket_id: str,
        post,
        kwargs: dict,
        current_type: str,
    ) -> bool:
        """Decide the final pinned / type / importance before anything is written; False
        refuses the update: a terminal (archived / deleted) bucket, a type that is not
        editable, a type that contradicts the pin, or a protected bucket leaving
        permanent. Fills kwargs["type"] / kwargs["importance"] where the pin decides them."""
        # Work out the final pin/type state before mutating the post.  Type is
        # also a physical-storage decision, so unsupported values must fail
        # here instead of being written into frontmatter and reported as a
        # successful edit.
        was_pinned = parse_bool(post.get("pinned", False), default=False)
        is_protected = parse_bool(post.get("protected", False), default=False)
        if (
            current_type == "archived"
            or post.get("deleted_at")
            or parse_bool(post.get("tombstone"), default=False)
        ):
            # Archive is a terminal lifecycle transition.  Allowing an
            # ordinary update here is unsafe even when ``type`` was omitted:
            # pinned=True forces type=permanent below and could otherwise
            # resurrect an archived file into the active tree.
            logger.warning(
                "update() rejected mutation on terminal bucket=%s",
                bucket_id,
            )
            return False
        will_be_pinned = parse_bool(
            kwargs.get("pinned", was_pinned), default=was_pinned
        )

        requested_type: str | None = None
        if "type" in kwargs:
            requested_type = str(kwargs["type"] or "").strip().lower()
            if requested_type not in _EDITABLE_BUCKET_TYPES:
                logger.warning(
                    "update() rejected unsupported type=%r for bucket=%s",
                    requested_type,
                    bucket_id,
                )
                return False
        forced_type: str | None = None
        if will_be_pinned:
            forced_type = "permanent"
        elif "pinned" in kwargs and was_pinned and not is_protected:
            # A true pinned bucket demotes when explicitly unpinned.  Explicit
            # permanent memories (was_pinned=False) remain permanent.
            forced_type = "dynamic"

        if forced_type is not None:
            if requested_type is not None and requested_type != forced_type:
                logger.warning(
                    "update() rejected incompatible pinned/type transition "
                    "bucket=%s pinned=%s type=%s",
                    bucket_id,
                    will_be_pinned,
                    requested_type,
                )
                return False
            kwargs["type"] = forced_type
            requested_type = forced_type

        if (
            requested_type is not None
            and requested_type != current_type
            and is_protected
            and requested_type != "permanent"
        ):
            logger.warning(
                "update() rejected protected bucket type transition "
                "bucket=%s type=%s",
                bucket_id,
                requested_type,
            )
            return False

        # pinned/protected buckets lock importance at 10.  An atomic
        # pinned=False + importance=N transition is allowed, because the final
        # state is no longer pinned; this is needed for quota-safe unpinning.
        if will_be_pinned or is_protected:
            kwargs.pop("importance", None)
        elif forced_type == "dynamic" and "importance" not in kwargs:
            # 🔴 **Unpinning drops importance back down with it** (bug ④).
            # The old behaviour: after unpinning, importance stayed at 10 — and a pinned
            # bucket does not occupy the importance>=9 pool, so the moment it is unpinned it
            # falls into that pool at full marks and fills it up (the pool caps at 24).
            # Meanwhile the importance parameter was withdrawn entirely: **there is no
            # entry point anywhere that can bring it back down**, so unpinning became a
            # one-way door. The "quota-safe unpinning" path mentioned in the comment above
            # has been there all along; it simply had no caller able to reach it any more —
            # so this branch walks it on their behalf.
            # Landing on 8 is not an arbitrary pick: it is the number the system already
            # degrades to when the hard cap is hit (_HIGH_IMP_DEGRADE_TO), and two paths
            # arriving at the same place is what stops them fighting each other.
            kwargs["importance"] = 8
        return True

    def _update_apply_typed_fields(self, bucket_id: str, post, kwargs: dict) -> None:
        """The fields with a conversion of their own: body, labels, the clamped numbers,
        name, the pin, and the media / meaning lists (replace or append)."""
        # --- Update only fields that were passed in ---
        if "content" in kwargs:
            post.content = kwargs["content"]  # wikilink injection disabled; LLM adds [[]] via prompt
        if "tags" in kwargs:
            post["tags"] = kwargs["tags"]
        if "aliases" in kwargs:
            if kwargs["aliases"]:
                post["aliases"] = kwargs["aliases"]
            else:
                post.metadata.pop("aliases", None)
        if "importance" in kwargs:
            post["importance"] = _clamp_importance(kwargs["importance"], f"update:{bucket_id}")
        if "domain" in kwargs:
            post["domain"] = kwargs["domain"]
        if "valence" in kwargs:
            post["valence"] = _clamp_unit(kwargs["valence"], "valence", f"update:{bucket_id}")
        if "arousal" in kwargs:
            post["arousal"] = _clamp_unit(kwargs["arousal"], "arousal", f"update:{bucket_id}")
        if "name" in kwargs:
            post["name"] = sanitize_name(kwargs["name"])
        if "resolved" in kwargs:
            post["resolved"] = kwargs["resolved"]
        if "pinned" in kwargs:
            post["pinned"] = kwargs["pinned"]
            if kwargs["pinned"]:
                post["importance"] = _PINNED_IMPORTANCE  # pinned → lock importance to 10
                post.metadata.pop("anchor", None)  # pinned and anchor are mutually exclusive: pinning it as a core rule clears the coordinate-system mark
        if "digested" in kwargs:
            post["digested"] = kwargs["digested"]
        if "model_valence" in kwargs:
            post["model_valence"] = _clamp01(kwargs["model_valence"], _DEFAULT_VALENCE)
        if "media" in kwargs:
            # A wholesale overwrite (trace's media_replace); an empty list clears the field.
            if kwargs["media"]:
                post["media"] = kwargs["media"]
            else:
                post.metadata.pop("media", None)
        if "media_append" in kwargs and kwargs["media_append"]:
            # Appends, deduplicating any earlier reference with the same path (trace's
            # media_append, and every hold call).
            existing_media = post.get("media") or []
            existing_paths = {m.get("path") for m in existing_media if isinstance(m, dict)}
            appended = existing_media + [
                m for m in kwargs["media_append"] if m.get("path") not in existing_paths
            ]
            post["media"] = appended[:_MEDIA_MAX_ITEMS]
        if "meaning" in kwargs:
            # A wholesale overwrite (trace's meaning_replace, for corrections and cleanup);
            # an empty list clears the field.
            if kwargs["meaning"]:
                post["meaning"] = kwargs["meaning"]
            else:
                post.metadata.pop("meaning", None)
        if "meaning_append" in kwargs and kwargs["meaning_append"]:
            # Appends one new meaning without overwriting what is there (trace's
            # meaning_append, and every hold call).
            existing_meaning = post.get("meaning") or []
            if isinstance(existing_meaning, str):
                existing_meaning = [existing_meaning]
            post["meaning"] = (list(existing_meaning) + [kwargs["meaning_append"]])[:_MEANING_LIST_MAX_ITEMS]

    async def _update_apply_pass_through(self, bucket_id: str, post, kwargs: dict) -> bool:
        """Every other field update() accepts, written as given (None removes it), with
        the few that are cut, clamped or checked on the way. False refuses the update:
        anchor=True past ANCHOR_LIMIT."""
        # --- Pass-through fields ---
        # These fields have no validation or conversion logic: whatever is given is written.
        # A new field only has to be added to this tuple.
        for k in ("status", "type", "resolution_reason", "resolved_by",
                  "related_bucket", "author", "user_name", "title",
                  # Everything below passes through unconverted, weight included.
                  # weight only means anything on something wanted; its type is not checked in this
                  # loop, and server.py above guarantees the range it passes in.
                  "why_remembered", "dont_surface", "first_of_kind",
                  "weight",
                  # prov: where this came from, normalised in _update_normalize_args.
                  # Writing it retires the older `from` string on the same entry, so the
                  # two can never disagree; an empty list removes the field.
                  PROV_FIELD,
                  # subjects. The dehydrator's backfill comes through this path.
                  "subjects",
                  # anchor: a bool that takes no part in scoring, hard-capped at 24.
                  # The cap is checked in the anchor branch below (counting only on a
                  # False->True transition). set_anchor() remains the preferred entry point;
                  # update() is only here as a fallback for bulk migration scripts.
                  "anchor",
                  # Provenance fields:
                  # source_tool / grow_batch_id are normally fixed at create() time, and the
                  # pass-through here serves migration scripts backfilling older buckets.
                  # last_merged_by is written by _common.merge_or_create after a merge and
                  # says whether hold or grow triggered the last merge.
                  # _pre_anchor_source_tool holds the original source_tool saved when
                  # anchoring, restored automatically on release; None deletes the field.
                  "source_tool", "grow_batch_id", "last_merged_by", "_pre_anchor_source_tool",
                  # room (semantic validation in tools/_rooms.py), summary (the
                  # background-backfilled one-sentence summary), when (when the event
                  # happened).
                  "room", "summary", "when",
                  # regrow's version chain. supersedes = who I replaced; superseded_by = who
                  # replaced me (any value means "an old version": recall and surfacing skip
                  # it, while a direct lookup by id still sees it).
                  "supersedes", "superseded_by",
                  # The two ends of fold / gist.
                  # cover = who this gist covers (a list[str], always a definite list of ids
                  #   — a time range is only a convenient way to write the input, and what
                  #   lands on disk must already be resolved);
                  # covered_by = who covers me (any value means "stops surfacing on its own",
                  #   while search, direct id lookup and drilling down are unaffected).
                  # 🔴 Writing both ends is **deliberate**: the surfacing pools rescan the
                  #   whole store every round, and querying the single field covered_by is
                  #   enough to filter, with no reverse index to maintain.
                  "cover", "covered_by",
                  # The three forgetting stages alive/faded/sunk.
                  # alive/faded are written here by the decay loop (pure metadata); sunk goes
                  # through sink_bucket(), because it has to move the original text and a
                  # plain update() would recompute the vector when content changes.
                  "decay_stage",
                  # Closing a want records who closed it, plus a timestamp of when the user
                  # was last asked about it. 🔴 The hole this filled: both fields were already
                  # assembled into the updates dict in trace_core.py, and closed_by even had
                  # a length cap in _METADATA_TEXT_LIMITS, but neither had ever been listed on
                  # this pass-through whitelist — and update() writes only whitelisted keys,
                  # so a field missing from here is **silently discarded**. The result was
                  # that the close button and the "asked about it" stamp appeared to succeed
                  # while not one character reached disk.
                  "closed_by", "last_asked",
                  # When this entry was last dreamt about. Weaving a dream records this and
                  # nothing else, and ingredient selection discounts by it — "do not keep
                  # having the same dream". **weight is not touched.**
                  # 🔴 Writing that field walked **straight back into the trap the comment
                  #    above describes**: it was added only as `update(last_dreamt=...)` in
                  #    `_dream.py` and never put on this list, so it was silently dropped, the
                  #    smoke test reported "last_dreamt=None", and that looked like a bug
                  #    somewhere else entirely.
                  #    **The very thing one comment warned about happened again within the
                  #    hour** — which says that "remember to add it to the list" does not work
                  #    as a method.
                  #    -> What cures it is not remembering harder, it is the assertion in
                  #      smoke_dream that last_dreamt really was written: that one goes red
                  #      when the list entry is missing.
                  "last_dreamt",
                  # Where `name` / `summary` came from. Present with the value
                  # "fallback" = that field is not the model's answer, it is a slice of
                  # the caller's own body standing in for one that never arrived; absent
                  # = the ordinary case. Written and cleared by grow's background
                  # backfill, and the only reason they are persisted at all is so a
                  # re-tagging pass can still **find** those buckets — a fallback fills
                  # the field in, which makes every "this one is unfinished" check stop
                  # matching it.
                  # 📌 Same trap as the two fields above: a field added at one end and
                  #    never listed here is silently dropped by update(), and the symptom
                  #    looks like a bug somewhere else entirely. What catches it is not
                  #    remembering, it is an assertion that the stamp really reached disk.
                  "name_source", "summary_source",
                  # invalidation: the records saying a basis of this memory changed under
                  # it (regrow's overturn writes them on every descendant). Normalised
                  # in _update_normalize_args; an empty list removes the field.
                  "invalidation",
                  # v2 write-layer fields, already normalised by _update_locked before this step.
                  *V2_FIELDS):
            if k in kwargs:
                if k == "weight" and kwargs[k] is not None:
                    post[k] = _clamp01(kwargs[k], _DEFAULT_VALENCE)
                elif k == "invalidation":
                    if kwargs[k]:
                        post[k] = kwargs[k]
                    else:
                        post.metadata.pop(k, None)
                elif k == PROV_FIELD:
                    post.metadata.pop(LEGACY_FROM_FIELD, None)
                    if kwargs[k]:
                        post[k] = kwargs[k]
                    else:
                        post.metadata.pop(k, None)
                elif k == "room":
                    post[k] = self._sanitize_text(str(kwargs[k])).strip()[:_ROOM_MAX]
                elif k == "summary":
                    post[k] = self._sanitize_text(str(kwargs[k])).strip()[:_SUMMARY_MAX]
                elif k == "when":
                    post[k] = str(kwargs[k]).strip()[:_WHEN_MAX]
                elif k == "dont_surface":
                    post[k] = kwargs[k]
                elif k == "first_of_kind":
                    post[k] = kwargs[k]
                elif k == "anchor":
                    # anchor is a bool; a False deletes the field outright to keep the
                    # frontmatter clean.
                    # The pass-through path is held to ANCHOR_LIMIT too: otherwise a bulk
                    # script or the front end calling update(anchor=True) directly could push
                    # the anchor total past the cap of 24. Only a False->True transition
                    # counts; setting anchor again on a bucket that already has it does not.
                    if kwargs[k]:
                        already_anchor = parse_bool(
                            post.get("anchor", False), default=False
                        )
                        if not already_anchor:
                            # FIX (RED-02): count_anchors is async and must be awaited, or
                            # `coroutine >= int` raises TypeError and the whole cap check
                            # stops working.
                            current = await self.count_anchors()
                            if current >= self.ANCHOR_LIMIT:
                                logger.warning(
                                    f"update() 拒绝 anchor=True：已达上限 "
                                    f"{self.ANCHOR_LIMIT}（当前 {current}）。bucket={bucket_id}"
                                )
                                return False
                        post["anchor"] = True
                    else:
                        post.metadata.pop("anchor", None)
                else:
                    if kwargs[k] is None:
                        # None explicitly deletes that frontmatter field (used when an anchor
                        # release clears its temporary field)
                        post.metadata.pop(k, None)
                    elif k in _METADATA_TEXT_LIMITS:
                        post[k] = self._sanitize_text(str(kwargs[k])).strip()[
                            :_METADATA_TEXT_LIMITS[k]
                        ]
                    else:
                        post[k] = kwargs[k]
        return True

    def _update_commit(
        self,
        bucket_id: str,
        file_path: str,
        post,
        current_type: str,
    ) -> Optional[str]:
        """Write the post to where its type and domain put it (moving the file when that
        changed). The committed path, or None when the commit failed."""
        final_type = str(post.get("type") or current_type).strip().lower()
        target_path = file_path
        if final_type in _EDITABLE_BUCKET_TYPES:
            target_path = self._bucket_target_path(
                file_path,
                final_type,
                post.get("domain") or [_DEFAULT_DOMAIN_NAME],
                str(post.get("status") or "active"),
            )

        try:
            committed_path = self._commit_bucket_update(
                file_path,
                target_path,
                frontmatter.dumps(post),
                bucket_id=bucket_id,
            )
        except (OSError, ValueError) as e:
            logger.error(
                "Failed to commit bucket update / 提交桶更新失败: "
                "%s -> %s: %s",
                file_path,
                target_path,
                e,
            )
            return None
        return committed_path

    async def replace_text_fields(self, old: str, new: str) -> dict[str, int]:
        """Replace a display term through managed per-bucket transactions.

        This is used when the configured human name changes.  It deliberately
        does not bump ``last_active``: a display-name migration is not a memory
        activation.  Each bucket is re-read while holding the normal
        cross-process bucket lock and then committed through ``_update_locked``
        so atomic writes, derived-index updates, ledger/projection events and
        concurrent edits retain the same guarantees as every other mutation.
        """

        if not old or not new or old == new:
            return {"buckets_changed": 0, "replacements": 0}

        pattern = re.compile(re.escape(old))
        bucket_ids: list[str] = []
        seen: set[str] = set()
        directories = list(self._active_dirs) + [self.archive_dir]
        for _root, _filename, file_path in self._iter_md_files(directories):
            bucket = self._load_bucket(file_path)
            bucket_id = str((bucket or {}).get("id") or "").strip()
            if bucket_id and bucket_id not in seen:
                seen.add(bucket_id)
                bucket_ids.append(bucket_id)

        changed = 0
        total = 0
        for bucket_id in bucket_ids:
            async with self._bucket_turn(bucket_id):
                file_path = self._find_bucket_file(bucket_id)
                if not file_path:
                    continue
                try:
                    post = frontmatter.load(file_path)
                except Exception as exc:
                    logger.warning(
                        "Failed to load bucket for text replacement %s: %s",
                        bucket_id,
                        exc,
                    )
                    continue

                replacements = 0
                updates: dict[str, str] = {}
                # A callable replacement is literal.  Passing ``new`` directly
                # would interpret user-controlled ``\\1``/``\\g<name>`` syntax
                # as regular-expression group references.
                content, count = pattern.subn(lambda _match: new, post.content or "")
                if count:
                    updates["content"] = content
                    replacements += count
                for field in ("name", "why_remembered", "user_name"):
                    value = post.get(field)
                    if not isinstance(value, str) or not value:
                        continue
                    replaced, count = pattern.subn(lambda _match: new, value)
                    if count:
                        updates[field] = replaced
                        replacements += count

                if not updates:
                    continue
                try:
                    committed = await self._update_locked(bucket_id, **updates)
                except (OSError, ValueError) as exc:
                    logger.warning(
                        "Text replacement rejected for bucket %s: %s",
                        bucket_id,
                        exc,
                    )
                    continue
                if committed:
                    changed += 1
                    total += replacements

        return {"buckets_changed": changed, "replacements": total}

    async def update_content_fragment(
        self,
        bucket_id: str,
        *,
        old_str: str,
        new_str: str,
        **kwargs,
    ) -> dict[str, Any]:
        """Atomically replace one unique literal fragment in a bucket body.

        The match and the write deliberately happen under the same per-bucket
        cross-process lock.  Computing the replacement from an earlier
        ``get()`` result would let a concurrent trace/update be overwritten by
        a stale full-body snapshot.

        ``new_str`` may be empty (delete the matched fragment).  Zero matches
        and multiple matches are both non-mutating results so callers never
        have to guess which occurrence was intended.
        """
        old_text = str(old_str)
        replacement = str(new_str)
        if not old_text:
            return {"ok": False, "error": "empty_old_str", "matches": 0}
        if "content" in kwargs:
            return {"ok": False, "error": "content_conflict", "matches": 0}

        async with self._bucket_turn(bucket_id):
            file_path = self._find_bucket_file(bucket_id)
            if not file_path:
                return {"ok": False, "error": "not_found", "matches": 0}
            try:
                post = frontmatter.load(file_path)
            except Exception as exc:
                logger.warning(
                    "Failed to load bucket for content patch %s: %s",
                    bucket_id,
                    exc,
                )
                return {"ok": False, "error": "read_failed", "matches": 0}

            current_content = str(post.content or "")
            # ``str.count`` ignores overlapping occurrences ("aa" in "aaa"),
            # which could silently patch the first of two valid match starts.
            # Only 0/1/many matters, so stop at the second start rather than
            # scanning every pathological overlapping match.
            first_match = current_content.find(old_text)
            if first_match < 0:
                return {
                    "ok": False,
                    "error": "old_str_not_found",
                    "matches": 0,
                }
            second_match = current_content.find(old_text, first_match + 1)
            if second_match >= 0:
                return {
                    "ok": False,
                    "error": "old_str_ambiguous",
                    "matches": 2,
                }

            updated_content = self._sanitize_text(
                current_content.replace(old_text, replacement, 1)
            )
            if updated_content == current_content:
                return {"ok": False, "error": "unchanged", "matches": 1}
            if not updated_content.strip():
                return {
                    "ok": False,
                    "error": "invalid_content",
                    "matches": 1,
                    "message": "替换后正文不能为空；如需移除整个桶，请使用归档。",
                }

            updates = dict(kwargs)
            updates["content"] = updated_content
            try:
                committed = await self._update_locked(bucket_id, **updates)
            except ValueError as exc:
                return {
                    "ok": False,
                    "error": "invalid_content",
                    "matches": 1,
                    "message": str(exc),
                }
            if committed:
                note_written(bucket_id)
            return {
                "ok": bool(committed),
                "error": "" if committed else "update_failed",
                "matches": 1,
            }

    # ---------------------------------------------------------
    # Internal: keep embedding index in sync with markdown storage
    # ---------------------------------------------------------
    async def _sync_embedding(self, bucket_id: str, content: str) -> bool:
        """Best-effort inline indexing for runtimes without a queue worker."""
        if not self.embedding_engine or not getattr(self.embedding_engine, "enabled", False):
            return False
        if not content or not content.strip():
            return True
        return bool(
            await self.embedding_engine.generate_and_store(bucket_id, content)
        )

    async def _sync_meaning_embedding(self, bucket_id: str, meaning_list: list[str]) -> None:
        """Best-effort: embed the most recent meaning entry, separate from content.

        It takes the last entry in the list: the most recent feeling is usually the closest
        to the current context. There is no dedicated outbox or retry queue — a failed
        meaning vector does not affect the fact that the memory itself is already on disk,
        and the next hold or trace that appends a new meaning tries again.
        """
        if not meaning_list:
            return
        engine = self.embedding_engine
        if not engine or not getattr(engine, "enabled", False):
            return
        store_meaning = getattr(engine, "generate_and_store_meaning", None)
        if not callable(store_meaning):
            return
        try:
            await store_meaning(bucket_id, meaning_list[-1])
        except Exception as exc:
            logger.warning(f"meaning embedding failed for {bucket_id}: {exc}")

    async def _index_after_write(self, bucket_id: str, content: str) -> None:
        """Queue derived indexing after Markdown is safely on disk.

        The server runtime starts a durable outbox worker, so this returns
        without waiting for network I/O.  Standalone/stdio users still get one
        inline attempt; failures remain queued for a later managed startup.
        """
        outbox = self.embedding_outbox
        queued = False
        if outbox is not None:
            try:
                queued = bool(outbox.enqueue(bucket_id, content))
            except Exception as exc:
                logger.error(
                    "Failed to persist embedding outbox item for %s: %s",
                    bucket_id,
                    exc,
                )
            if queued and getattr(outbox, "running", False):
                return

        try:
            indexed = await self._sync_embedding(bucket_id, content)
        except Exception as exc:
            indexed = False
            logger.warning(
                "Inline embedding attempt failed; memory remains queued / "
                "同步向量尝试失败，记忆已保留待后台重试: %s: %s",
                bucket_id,
                exc,
            )
        if indexed and outbox is not None:
            try:
                outbox.discard(bucket_id)
            except Exception:
                logger.warning("Failed to acknowledge embedding outbox item: %s", bucket_id)
        elif not indexed:
            logger.warning(
                "Memory saved without vector; pending retry / 记忆已落盘，向量待重试: %s",
                bucket_id,
            )
