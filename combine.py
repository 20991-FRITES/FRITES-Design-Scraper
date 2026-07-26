import json


INPUT = (
    "structure_rev.json",
    "structure.json",
)

combined_output = {
    "url": "",
    "title": "Providers",
    "children": [],
}

for filename in INPUT:
    with open(filename, "r", encoding="utf-8") as f:
        combined_output["children"].append(json.load(f))

with open("full_structure.json", "w", encoding="utf-8") as f:
    json.dump(combined_output, f, ensure_ascii=False, indent=2)