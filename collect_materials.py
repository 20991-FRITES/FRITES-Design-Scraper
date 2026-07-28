import json

def collect_materials(node, materials):
    if isinstance(node, dict):
        material = node.get("material")
        if material:
            materials.add(material.strip())

        for value in node.values():
            collect_materials(value, materials)

    elif isinstance(node, list):
        for item in node:
            collect_materials(item, materials)


with open("structure.json", "r", encoding="utf-8") as f:
    data = json.load(f)

materials = set()
collect_materials(data, materials)

for material in sorted(materials):
    print(material)