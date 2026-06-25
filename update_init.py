import re

with open("app/tools/__init__.py", "r") as f:
    content = f.read()

content = content.replace("save_session_to_memory", "save_memory_note")

with open("app/tools/__init__.py", "w") as f:
    f.write(content)
