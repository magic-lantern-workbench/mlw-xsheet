import json
import jsonschema
from jsonschema import validate

# Load schema
with open("xdts.schema.json", "r", encoding="utf-8") as f:
    schema = json.load(f)

# Load XDTS JSON file
with open("timesheet.xdts.json", "r", encoding="utf-8") as f:
    xdts_data = json.load(f)

# Validate
try:
    validate(instance=xdts_data, schema=schema)
    print("✅ XDTS file is valid.")
except jsonschema.exceptions.ValidationError as e:
    print(f"❌ XDTS file is invalid: {e.message}")
