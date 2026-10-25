def add_item(value, items=None):
    if items is None:
        items = []
    items.append(value)
    return items
