records = []
def save(record):
    records.append({'name': record['name']})
    return records[-1]
