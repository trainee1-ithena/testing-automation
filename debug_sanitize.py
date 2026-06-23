import json, re, sys
sys.path.insert(0, 'testGen')

matrix = json.loads(open('testGen/scenario_matrix_customer_create_ticket.json', encoding='utf-8-sig').read())
inv = matrix['inventory_text']

a_items = []
for line in inv.splitlines():
    m = re.match(
        r'(A\d+)\.\s+(\S+)\s+\|.*type:\s*(\S+).*\|.*required:\s*(\S+).*\|.*constraints:\s*([^|]+).*\|.*auto_filled:\s*(\S+)',
        line, re.IGNORECASE
    )
    if m:
        a_items.append({
            'name': m.group(2),
            'type': m.group(3).lower().rstrip(','),
            'required': m.group(4).lower().rstrip(','),
            'auto_filled': m.group(6).lower().rstrip(','),
        })

print('A-items:')
for a in a_items:
    print(' ', a)

required_manual = [
    a['name'] for a in a_items
    if a['required'] in ('yes',) and (
        a['auto_filled'] == 'no' or a['type'] in ('text', 'textarea')
    )
]
print('required_manual:', required_manual)

field_defaults = {}
for a in a_items:
    if a['name'] not in field_defaults and a['required'] == 'yes' and (
        a['auto_filled'] == 'no' or a['type'] in ('text', 'textarea')
    ):
        if a['type'] in ('text', 'textarea'):
            field_defaults[a['name']] = 'Test ' + a['name'].replace('_', ' ').title()
print('field_defaults:', field_defaults)

tc1 = next(s for s in matrix['scenarios'] if s['id'] == 'TC001')
print('TC001 cat:', tc1['category'], 'inputs:', tc1.get('inputs'))
