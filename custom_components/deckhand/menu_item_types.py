"""Which Home Assistant entities a menu item type can act on.

Mirrors ``ITEM_TYPE_ENTITY_DOMAINS`` and ``entity_domain_error`` in Helm
(``helm/apps/menus/item_types.py``, helm#438 / helm#450). Helm checks every
item it writes against that table, including the contextual items
``add_menu_item`` asks for over ``menu_request``. That plane has no reply, so
when Helm refuses an item the refusal is a WARNING in Helm's log and the Home
Assistant user sees the service succeed with no item on the dial (helm#459).
Checking the same rule here, before publishing, turns it into an error Home
Assistant shows.

Keep the table identical to Helm's. tests/test_menu_item_entity_domain.py
reads Helm's copy when the helm checkout sits beside this repo and fails if
the two differ.

One difference, by necessity: Helm lets an item that already holds an
out-of-domain entity keep it (``held=``), so a row written before the rule
stays editable. HACS cannot see Helm's rows, so it refuses those too. Such an
item is broken on the dial anyway (helm#430: an art picker on a remote lists
no sources and every pick fails in HA), so saying so is the useful answer.
"""

from __future__ import annotations

# item_type -> the Home Assistant domains its entity may be in. A type not
# listed takes any entity.
ITEM_TYPE_ENTITY_DOMAINS: dict[str, tuple[str, ...]] = {
    # helm#430: a pick on the art picker is media_player.select_source on the
    # item's entity, and the picker lists that entity's source_list.
    "ha_art": ("media_player",),
}


def disallowed_entity_domains(item_type, entity_id) -> tuple[str, ...] | None:
    """The domains ``entity_id`` would have to be in for an ``item_type`` item.

    None when the pair is fine: the type takes any entity, no entity is given
    (a FrameCast-driven gallery names none), or the entity is in one of the
    type's domains. Normalises exactly as Helm does — ``str()`` then strip,
    and a ``<domain>.`` prefix match — so the two sides agree on every input.
    """
    domains = ITEM_TYPE_ENTITY_DOMAINS.get(item_type)
    entity_id = str(entity_id or "").strip()
    if not domains or not entity_id:
        return None
    if any(entity_id.startswith(f"{domain}.") for domain in domains):
        return None
    return domains
