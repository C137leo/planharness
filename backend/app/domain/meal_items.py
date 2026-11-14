"""Merge equivalent food rows, never infer food identity from user language."""
import copy

AMOUNTS = ("grams", "kcal", "protein", "fat", "carb")
METADATA = ("preparation", "cooking_method", "cooked_state", "brand", "unit", "meta")


def same_food(left, right):
    if not left.get("name") or left.get("name") != right.get("name"):
        return False
    for key in ("ingredient_id", "ingredient_key"):
        if left.get(key) and right.get(key) and left[key] != right[key]:
            return False
    if any((left.get(key) is None) != (right.get(key) is None) for key in AMOUNTS if key != "grams"):
        return False
    return all(left.get(key) == right.get(key) for key in METADATA)


def merge_food_items(items, *, target=None):
    """Keep the first stable ID and sum observed amounts, not catalogue estimates.

    Different preparation/brand/catalogue identities remain separate. This is
    called only when adding/replacing food; historical records are not rewritten.
    """
    result = []
    for value in items:
        item = copy.deepcopy(value)
        if target is not None and not same_food(item, target):
            result.append(item)
            continue
        existing = next((other for other in result if same_food(other, item)), None)
        if existing is None:
            result.append(item)
            continue
        for key in AMOUNTS:
            if existing.get(key) is not None and item.get(key) is not None:
                existing[key] = round(float(existing[key]) + float(item[key]), 4)
            elif key in existing or key in item:
                # Unknown nutrients must not be silently replaced by a partial sum.
                existing[key] = None
        for key in ("ingredient_id", "ingredient_key"):
            if not existing.get(key) and item.get(key):
                existing[key] = item[key]
    return result
