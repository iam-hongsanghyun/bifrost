"""The conversation store: groups, and the project package each conversation owns.

The property under test throughout is that a conversation and its model are ONE
artefact. A transcript that says "I set the carbon price to 80" is a lie if
reopening the conversation shows a different model, so the package has to travel
with the chat and survive a restart.
"""

from __future__ import annotations

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
