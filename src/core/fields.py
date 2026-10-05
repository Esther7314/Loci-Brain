# -*- coding: utf-8 -*-
"""
core/fields.py — the one table of what an entry's frontmatter fields mean.

An entry is a Markdown file; everything Loci knows about it beyond its body is a key in
its YAML frontmatter. This table names every key the code writes or still reads, which
part of the library it belongs to, and what it means. It is the only copy: the export
package's schema note (core/export_package.py) is generated from it, and
tests/test_field_table.py fails when the store accepts a key this table does not describe
(the create() keys, update()'s pass-through list and its handled keys, V2_FIELDS).

A key not in this table is not refused anywhere — older libraries carry keys of their own
— but a package lists such keys as undescribed instead of guessing what they mean.

Exports: FIELDS · GROUPS · Field · describe · schema_document · render_schema_note ·
         undescribed
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Field:
    name: str
    group: str
    meaning: str
    legacy: bool = False     # read for older libraries; no current code path writes it


GROUPS = {
    "identity": "What the entry is and where it sits",
    "labels": "Words about the entry: titles, summaries, labels, names",
    "time": "When it happened and when it was written",
    "weight": "How much it weighs and how it fades",
    "write": "The v2 write layer (plan part 1): what kind of statement it is",
    "state": "Whether something wanted is still open, and who closed it",
    "links": "How entries stand on each other: lines, versions, covers",
    "sources": "The host's material it was formed from, and what happened to it",
    "removal": "Deletion and archiving",
    "stamps": "Which tool wrote what, for repair passes",
}

_F = Field

FIELDS: tuple[Field, ...] = (
    # ---- identity ----
    _F("id", "identity", "The entry's id, unique in the library (12 hex characters for "
       "entries written by this version; older libraries also hold other forms). Every "
       "link (prov, cover, supersedes, exception_of) names entries by this id."),
    _F("type", "identity", "Storage kind: dynamic (the default), permanent (a pinned "
       "rule), feel (a settled feeling), archived (taken off the timeline on purpose, or "
       "the version before an overwrite), i / self (private channels stored as dynamic). "
       "It decides the top folder; the first domain decides the folder below it."),
    _F("room", "identity", "One of the four rooms: EVENT/SELF (it happened to me), "
       "EVENT/WORLD (it happened out there), MIND/TRAITS (what someone is like), "
       "MIND/VIEWS (what I think about something)."),
    _F("domain", "identity", "Domain labels, a list; the first one names the folder the "
       "file lives in."),
    _F("media", "identity", "Attachments: a list of {path, title, type, note}; `path` is "
       "relative to the library folder (normally under _media/)."),

    # ---- labels ----
    _F("name", "labels", "A short title."),
    _F("summary", "labels", "The one-sentence summary (filled in by the backfill). What "
       "is left of the body once the entry has sunk."),
    _F("tags", "labels", "Scene words and user labels, a list. Tags starting and ending "
       "with __ are the system's own markers."),
    _F("aliases", "labels", "Expansion words for literal search; never shown as tags."),
    _F("subjects", "labels", "The names in the entry, people and things, normalised "
       "through the names table (aliases.yaml)."),
    _F("why_remembered", "labels", "Free text: why this was kept."),
    _F("meaning", "labels", "What it meant, a list: each touch appends one, the newest "
       "last."),
    _F("title", "labels", "A title given by an import or an older tool."),
    _F("author", "labels", "Who wrote it, as an import or an older tool recorded it."),
    _F("user_name", "labels", "The owner's name as an import recorded it."),

    # ---- time ----
    _F("created", "time", "When the entry was written. Stamps without an offset are UTC."),
    _F("when", "time", "When the event happened: an ISO date or date-time (a clock time "
       "makes it wait until that hour on its day), or a period for a gist. Empty means "
       "the moment it was written."),
    _F("last_active", "time", "The last time it was really used (written from, or "
       "touched by the owner); the input to fading. Being found by a search does not "
       "count."),
    _F("last_asked", "time", "When the owner was last asked about this open wanted "
       "thing."),
    _F("last_dreamt", "time", "When it was last used as dream material."),
    _F("review_after", "time", "The day an open-ended defer hold is asked about again "
       "(YYYY-MM-DD). A review day, not an expiry."),

    # ---- weight ----
    _F("importance", "weight", "1-10. Pinned entries are 10."),
    _F("valence", "weight", "How it felt, 0 (bad) to 1 (good)."),
    _F("arousal", "weight", "How strongly it was felt, 0 to 1."),
    _F("model_valence", "weight", "The valence the writing model gave, kept apart from the "
       "owner's."),
    _F("weight", "weight", "How heavily an open wanted thing weighs on the mind, 0 to 1."),
    _F("activation_count", "weight", "How many times it was really used."),
    _F("decay_stage", "weight", "alive / faded / sunk. A sunk entry's body is its "
       "summary; the original text is in archive/原文/<id>.txt."),
    _F("pinned", "weight", "A core rule: shown by the door, never fades."),
    _F("protected", "weight", "Never fades and is never sunk."),
    _F("anchor", "weight", "One of at most 24 coordinate entries; takes no part in "
       "scoring."),
    _F("first_of_kind", "weight", "The first entry of its kind (set when written); it "
       "does not fade."),
    _F("dont_surface", "weight", "Never comes up by itself (breath, musing, dreams); a "
       "search still finds it. On an old version it is the version chain's mark."),
    _F("digested", "weight", "An older marker that a feeling was digested."),

    # ---- write layer ----
    _F("direction_of_fit", "write", "thetic (recording what is; the default when absent) "
       "or telic (something wanted / promised)."),
    _F("bound", "write", "Who is bound by a telic entry: a list of names, normalised like "
       "subjects."),
    _F("evidential", "write", "On a MIND entry: inference (worked out) or assumption "
       "(taken for granted). Absent = not marked."),
    _F("internally_generated", "write", "true when it was imagined or dreamt rather than "
       "lived."),
    _F("recurrence", "write", "RFC 5545 RRULE subset; this version reads FREQ=YEARLY "
       "(every year on the date in `when`)."),
    _F("backfilled", "write", "The field names the backfill model filled in (the main "
       "model's own values are never overwritten)."),
    _F("cue", "write", "{condition, phrasings}: the event a telic entry waits for and the "
       "ways it might be said. Writing one says the entry waits on something."),
    _F("exception_of", "write", "A hold: the id of the standing entry this hold is hung "
       "on. Comes together with `hold`."),
    _F("hold", "write", "A hold's reach: defer (do not press) or avoid (do not touch)."),
    _F("card_of", "write", "On a MIND entry: the names-table name this entry is the "
       "card of."),
    _F("looks_like_promise", "write", "The backfill read a promise the main model did not "
       "mark telic; turned into a question, never into a telic entry."),

    # ---- state ----
    _F("status", "state", "active / resolved / abandoned, for telic entries."),
    _F("closed_by", "state", "Who closed a wanted thing (a name, or `expired` for a hold "
       "whose day passed)."),
    _F("resolved", "state", "An older closed marker (boolean)."),
    _F("resolution_reason", "state", "Why it was closed, as an older tool wrote it."),
    _F("resolved_by", "state", "What closed it, as an older tool wrote it (an id or a "
       "label)."),
    _F("related_bucket", "state", "An older single link to a related entry."),
    _F("change_log", "state", "The history of an old `plan` entry, kept as written.",
       legacy=True),

    # ---- links ----
    _F("prov", "links", "Where it came from: a list of {rel, target}, rel one of "
       "wasDerivedFrom (stands on another entry), wasRevisionOf (a new version of it), "
       "wasQuotedFrom (quotes a source by its string form), hadPrimarySource."),
    _F("from", "links", "The older comma-separated spelling of prov; read, never written.",
       legacy=True),
    _F("supersedes", "links", "The version this entry replaced."),
    _F("superseded_by", "links", "The version that replaced this one: it is an old "
       "version (never surfaces; a read by id still sees it)."),
    _F("cover", "links", "On a gist or period: the ids it covers, a resolved list."),
    _F("covered_by", "links", "The gists that cover this entry, a list: it stops surfacing "
       "on its own."),

    # ---- sources ----
    _F("sources", "sources", "The host's material it was formed from, one record each: "
       "{system, instance, container, id, through?, revision, fingerprint, "
       "fingerprint_by, span?, use, completed_from?}; completed_from (registry or "
       "host_scope) says Loci filled in the container of a line the writer named only by "
       "its bare id, and is not identity. The material itself stays with the host; the "
       "record's state (active, unreadable, withdrawn, deleted, held) is in the source "
       "registry."),
    _F("invalidation", "sources", "Marks that a basis changed under it: a list of {kind, "
       "of, by, at, confirmed_at?}, kind one of overturn, source_revised, edited, "
       "source_gone, source_held, source_restored. Open until confirmed."),

    # ---- removal ----
    _F("deleted_at", "removal", "A soft delete: when. The file sits in archive/ and is "
       "not exported."),
    _F("tombstone", "removal", "A soft delete marker (true)."),
    _F("tombstoned_at", "removal", "When the tombstone was written."),
    _F("erasure_mode", "removal", "How it was deleted (tombstone_only: the text is kept, "
       "nothing is erased)."),
    _F("archived_at", "removal", "When an older version was moved to archive/ by an "
       "overwriting import."),

    # ---- stamps ----
    _F("source_tool", "stamps", "The tool that wrote the entry (grow, hold, import, ...)."),
    _F("grow_batch_id", "stamps", "The batch a multi-item write belonged to."),
    _F("last_merged_by", "stamps", "Which tool merged into it last (older merge path)."),
    _F("_pre_anchor_source_tool", "stamps", "source_tool saved while anchored, restored "
       "on release."),
    _F("name_source", "stamps", "`fallback` when name is a slice of the body standing in "
       "for a model answer that never came."),
    _F("summary_source", "stamps", "`fallback` likewise for summary."),
    _F("provenance", "stamps", "{kind: test, created_by, erasable}: test data that may be "
       "erased."),
    _F("_retagged", "stamps", "An older re-tagging pass's mark.", legacy=True),
)

_BY_NAME = {f.name: f for f in FIELDS}


def describe(name: str) -> Field | None:
    return _BY_NAME.get(str(name))


def undescribed(keys) -> list[str]:
    """The keys this table says nothing about, sorted."""
    return sorted({str(k) for k in keys} - set(_BY_NAME))


def schema_document(library_version: int, present: dict | None = None,
                    code_version: int | None = None) -> dict:
    """The machine-readable schema note: the library version (and the version the code
    that wrote the note reads, when they differ), every field with its group and meaning,
    and — given the keys a package holds with how many entries carry each — which fields
    are present and which keys the table does not describe."""
    present = present or {}
    return {
        "library_schema_version": int(library_version),
        "code_schema_version": int(code_version if code_version is not None
                                   else library_version),
        "groups": dict(GROUPS),
        "fields": [{"name": f.name, "group": f.group, "meaning": f.meaning,
                    "legacy": f.legacy, "entries": int(present.get(f.name, 0))}
                   for f in FIELDS],
        "undescribed": [{"name": k, "entries": int(present[k])}
                        for k in undescribed(present)],
    }


def render_schema_note(doc: dict) -> str:
    """The same document as Markdown, for a person opening the package."""
    out = [f"# Loci library, schema version {doc['library_schema_version']}", "",
           "Each memory is one Markdown file under `buckets/`: YAML frontmatter, then the "
           "body. The fields below are every key the code writes or reads; `entries` is "
           "how many entries in this package carry it.", ""]
    if doc["code_schema_version"] != doc["library_schema_version"]:
        out += [f"⚠️ The library is version {doc['library_schema_version']}; the code that "
                f"exported it reads version {doc['code_schema_version']}, and the table "
                "below is that code's. Migrate the library (scripts/migrate.py) before "
                "relying on it.", ""]
    for group, title in doc["groups"].items():
        rows = [f for f in doc["fields"] if f["group"] == group]
        if not rows:
            continue
        out += [f"## {title}", "", "| field | entries | meaning |", "|---|---|---|"]
        for f in rows:
            meaning = f["meaning"] + (" (legacy: read, not written)" if f["legacy"] else "")
            out.append(f"| `{f['name']}` | {f['entries']} | {meaning} |")
        out.append("")
    if doc["undescribed"]:
        out += ["## Keys this version does not describe", "",
                "Carried as written; this version neither writes nor interprets them.", ""]
        out += [f"- `{u['name']}` ({u['entries']} entries)" for u in doc["undescribed"]]
        out.append("")
    return "\n".join(out)
