"""The conversation store: groups, and the project package each conversation owns.

The property under test throughout is that a conversation and its model are ONE
artefact. A transcript that says "I set the carbon price to 80" is a lie if
reopening the conversation shows a different model, so the package has to travel
with the chat and survive a restart.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from bifrost.conversations.store import DEFAULT_GROUP, ConversationStore


@pytest.fixture()
def store(tmp_path: Path) -> ConversationStore:
    return ConversationStore(root=tmp_path / "conversations")


def test_a_new_conversation_has_no_project(store: ConversationStore) -> None:
    c = store.create(title="Peak week")
    assert c.title == "Peak week"
    assert c.group == DEFAULT_GROUP
    assert store.has_project(c.id) is False
    assert store.read_project(c.id) is None


def test_a_project_package_round_trips_byte_for_byte(store: ConversationStore) -> None:
    """The package is opaque to Bifrost — it must come back exactly as stored.

    Bifrost does not parse it: it is Ragnarok's format, and the whole point is
    that Ragnarok can reopen it. Any mangling here would only surface much later,
    as a failed import.
    """
    c = store.create()
    blob = b"PK\x03\x04" + bytes(range(256)) * 40
    store.save_project(c.id, blob, version=7)
    assert store.read_project(c.id) == blob
    assert store.get(c.id).project_version == 7
    assert store.has_project(c.id) is True


def test_saving_a_project_leaves_no_partial_file(store: ConversationStore) -> None:
    """It is written to a .part and moved, so a crash cannot truncate the one
    artefact whose loss costs the user their model."""
    c = store.create()
    store.save_project(c.id, b"PK\x03\x04payload")
    files = {p.name for p in (store.root / c.id).iterdir()}
    assert "project.zip" in files
    assert not any(name.endswith(".part") for name in files)


def test_events_persist_and_are_counted(store: ConversationStore) -> None:
    c = store.create()
    events = [{"kind": "user", "text": "solve it"}, {"kind": "assistant", "text": "done"}]
    store.save_events(c.id, events)
    assert store.events(c.id) == events
    assert store.get(c.id).event_count == 2


def test_grouping_puts_recent_work_first_and_unfiled_last(store: ConversationStore) -> None:
    """Group order follows activity, except the unfiled pile which always sinks —
    a heap of untitled chats should not outrank named work."""
    store.create(title="loose")                        # DEFAULT_GROUP
    old = store.create(title="old", group="Korea 2030")
    store.save_events(old.id, [{"kind": "user"}])
    new = store.create(title="new", group="Offshore")
    store.save_events(new.id, [{"kind": "user"}])

    groups = [g["group"] for g in store.grouped()]
    assert groups[-1] == DEFAULT_GROUP
    assert set(groups) == {"Korea 2030", "Offshore", DEFAULT_GROUP}
    assert groups.index("Offshore") < groups.index("Korea 2030")


def test_moving_between_groups_and_renaming(store: ConversationStore) -> None:
    c = store.create(title="draft")
    store.update(c.id, title="Offshore pipeline", group="Korea 2030")
    again = store.get(c.id)
    assert (again.title, again.group) == ("Offshore pipeline", "Korea 2030")


def test_a_blank_group_falls_back_rather_than_vanishing(store: ConversationStore) -> None:
    """An empty group string would create an unnamed bucket the rail cannot label."""
    c = store.create(title="x", group="   ")
    assert store.get(c.id).group == DEFAULT_GROUP
    store.update(c.id, group="")
    assert store.get(c.id).group == DEFAULT_GROUP


def test_the_store_survives_a_corrupt_conversation(store: ConversationStore) -> None:
    """One unreadable directory must not take the whole rail down."""
    good = store.create(title="fine")
    broken = store.root / "broken"
    broken.mkdir(parents=True)
    (broken / "meta.json").write_text("{ not json", encoding="utf-8")
    listed = [c.id for c in store.list()]
    assert listed == [good.id]


def test_delete_takes_the_package_with_it(store: ConversationStore) -> None:
    c = store.create()
    store.save_project(c.id, b"PK\x03\x04")
    assert store.delete(c.id) is True
    assert store.get(c.id) is None
    assert store.delete(c.id) is False  # idempotent, not an error


def test_unsafe_ids_are_refused(store: ConversationStore) -> None:
    """Ids reach the filesystem, so traversal has to be impossible by construction."""
    for bad in ("../escape", "a/b", "", "x" * 65):
        with pytest.raises(ValueError):
            store.project_path(bad)


def test_a_store_reopened_from_disk_sees_everything(tmp_path: Path) -> None:
    """Restart safety: the rail is rebuilt from disk, not from memory."""
    root = tmp_path / "conversations"
    first = ConversationStore(root=root)
    c = first.create(title="Persisted", group="Korea 2030")
    first.save_project(c.id, b"PK\x03\x04blob", version=3)
    first.save_events(c.id, [{"kind": "user", "text": "hi"}])

    second = ConversationStore(root=root)
    restored = second.get(c.id)
    assert restored is not None
    assert (restored.title, restored.group, restored.project_version) == ("Persisted", "Korea 2030", 3)
    assert second.read_project(c.id) == b"PK\x03\x04blob"
    assert second.events(c.id) == [{"kind": "user", "text": "hi"}]


def test_the_active_conversation_is_remembered_across_restarts(tmp_path: Path) -> None:
    """Returning to Bifrost must resume, not restart.

    Without this the user comes back to an empty shell beside a session that still
    holds their model, with nothing explaining what is loaded.
    """
    root = tmp_path / "conversations"
    first = ConversationStore(root=root)
    c = first.create(title="Peak week")
    assert first.get_active() is None
    first.set_active(c.id)

    assert ConversationStore(root=root).get_active() == c.id


def test_a_deleted_active_conversation_does_not_resurrect(store: ConversationStore) -> None:
    """A dangling active id would send the shell to a conversation that is gone."""
    c = store.create()
    store.set_active(c.id)
    store.delete(c.id)
    assert store.get_active() is None


def test_assumptions_are_recorded_and_travel_with_the_conversation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A number the agent supplied itself must be traceable afterwards.

    The failure this prevents is real: on a live run the agent chose a 5% discount
    rate, said in prose that it was "a harmless placeholder", and that value then sat
    inside a solved model with nothing marking it as invented.
    """
    from bifrost import assumptions
    from bifrost.conversations import store as store_module

    local = ConversationStore(root=tmp_path / "conversations")
    monkeypatch.setattr(store_module, "STORE", local)
    monkeypatch.setattr(assumptions, "STORE", local)

    c = local.create(title="Peak week")
    assert assumptions.load(c.id) == []

    entry = assumptions.record(
        c.id, subject="discount rate", value="0.05", unit="/yr",
        basis="assumed", source="no capex in this run, so it is inert",
        affects="annuitised capital cost",
    )
    assert entry["basis"] == "assumed"
    assert entry["id"]

    # Superseding appends rather than replaces — how the model got here is part of
    # the record, not just where it ended up.
    assumptions.record(c.id, subject="discount rate", value="0.07", basis="user")
    register = assumptions.load(c.id)
    assert [a["value"] for a in register] == ["0.05", "0.07"]
    assert [a["basis"] for a in register] == ["assumed", "user"]

    # It lives in the conversation's own directory, so it travels with the project.
    assert (local._dir(c.id) / "assumptions.json").exists()


def test_an_unknown_basis_falls_back_to_assumed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The safe default is the one that flags the number for a human."""
    from bifrost import assumptions
    from bifrost.conversations import store as store_module

    local = ConversationStore(root=tmp_path / "conversations")
    monkeypatch.setattr(store_module, "STORE", local)
    monkeypatch.setattr(assumptions, "STORE", local)
    c = local.create()
    assert assumptions.record(c.id, subject="x", value="1", basis="vibes")["basis"] == "assumed"


def test_attachments_live_with_the_conversation_that_needs_them(tmp_path, monkeypatch):
    """A handed-over wind profile is part of the study, so it is stored with it.

    Uploads were keyed by SESSION, which put every conversation's files in one
    shared folder outside the thing that gets saved, moved or re-imported. The
    transcript would still cite the path, so a conversation re-opened elsewhere
    pointed at a file that was not there — and the agent's documented fallback for
    a missing profile is to synthesise one.
    """
    import io

    from bifrost.agent import attachments
    from bifrost.config import CONFIG

    monkeypatch.delenv("RAGNAROK_AGENT_UPLOAD_DIR", raising=False)
    saved = attachments.save("conv-abc", "wind.csv", io.BytesIO(b"snapshot,cf\n2030,0.4\n"))

    stored = Path(saved["path"])
    assert stored.exists()
    # Inside THIS conversation's directory, not a shared session folder.
    assert stored.parent.parent == CONFIG.conversations_dir / "conv-abc"

    # Readable by the conversation that owns it...
    assert attachments.read_preview("conv-abc", saved["path"])["totalLines"] == 2
    # ...and not by any other, even given the full path.
    assert "no attachment" in attachments.read_preview("conv-xyz", saved["path"])["error"]


# ── chart payloads live beside the transcript, not in it ─────────────────────────


def _chart(chart_id: str = "c1", points: int = 5000) -> dict:
    return {
        "kind": "chart.show", "chartId": chart_id, "title": "Dispatch", "chartKind": "line", "unit": "MW",
        "x": [f"t{i}" for i in range(points)],
        "series": [{"name": "gas", "values": [float(i) for i in range(points)]}],
    }


def test_a_chart_is_stored_as_a_stub_and_its_points_in_their_own_file(store: ConversationStore) -> None:
    c = store.create()
    store.save_events(c.id, [{"kind": "user", "text": "plot"}, _chart()])

    on_disk = (store.root / c.id / "events.json").read_text("utf-8")
    assert "t4999" not in on_disk and len(on_disk) < 1000, "events.json must stay light"
    stub = store.events(c.id)[1]
    assert stub["lazy"] is True and "x" not in stub and "series" not in stub
    assert (stub["title"], stub["chartKind"], stub["unit"], stub["chartId"]) == ("Dispatch", "line", "MW", "c1")

    payload = store.chart(c.id, "c1")
    assert payload is not None and len(payload["x"]) == 5000
    assert payload["series"][0]["values"][-1] == 4999.0
    assert store.get(c.id).event_count == 2


def test_resaving_a_stub_keeps_it_a_stub_and_never_rewrites_the_points(store: ConversationStore) -> None:
    """The browser sends back what it holds. A stub must stay a stub, and a hydrated
    chart (points + lazy flag) must not overwrite the stored points."""
    c = store.create()
    store.save_events(c.id, [_chart()])
    chart_file = store.root / c.id / "charts" / "c1.json"
    stamp = chart_file.stat().st_mtime_ns

    stub = store.events(c.id)
    store.save_events(c.id, stub + [{"kind": "user", "text": "more"}])
    hydrated = {**stub[0], **store.chart(c.id, "c1")}
    store.save_events(c.id, [hydrated, {"kind": "user", "text": "more"}])

    assert chart_file.stat().st_mtime_ns == stamp
    assert all("x" not in e for e in store.events(c.id))
    assert not list((store.root / c.id).rglob("*.part"))


def test_a_legacy_transcript_with_inline_charts_is_still_readable_and_gets_split(store: ConversationStore) -> None:
    """events.json as written before this change: charts inline. Reading it must work
    unchanged, and the first read moves the points out so every later open is light."""
    c = store.create()
    legacy = [{"kind": "user", "text": "plot"}, _chart("old1", 100)]
    (store.root / c.id / "events.json").write_text(json.dumps(legacy), "utf-8")

    # Even before a rewrite, the chart can be fetched from the inline copy.
    assert len(store.chart(c.id, "old1")["x"]) == 100

    events = store.events(c.id)
    assert events[1]["lazy"] is True and "x" not in events[1]
    assert len(store.chart(c.id, "old1")["x"]) == 100
    assert "t99" not in (store.root / c.id / "events.json").read_text("utf-8")
    assert store.events(c.id) == events  # idempotent


def test_events_full_puts_the_points_back_for_export(store: ConversationStore) -> None:
    c = store.create()
    original = _chart(points=50)
    store.save_events(c.id, [{"kind": "user", "text": "plot"}, original])
    full = store.events_full(c.id)
    assert full[1] == original  # self-contained: no `lazy`, points inline


def test_chart_ids_cannot_traverse_and_unknown_ids_are_none(store: ConversationStore) -> None:
    c = store.create()
    store.save_events(c.id, [_chart()])
    assert store.chart(c.id, "../meta") is None
    assert store.chart(c.id, "nope") is None


def test_a_chart_without_an_id_stays_inline_rather_than_losing_its_points(store: ConversationStore) -> None:
    c = store.create()
    anonymous = {k: v for k, v in _chart(points=3).items() if k != "chartId"}
    store.save_events(c.id, [anonymous])
    assert store.events(c.id)[0]["x"] == ["t0", "t1", "t2"]
