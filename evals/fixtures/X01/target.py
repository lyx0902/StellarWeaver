from business import select_items
def render(items, options):
    return ','.join(select_items(items))
