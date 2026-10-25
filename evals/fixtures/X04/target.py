from storage import save
def parse_user(data):
    return {'name': data['name']}
def render_user(data):
    record = save(parse_user(data))
    return record['name']
