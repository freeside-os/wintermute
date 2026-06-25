import re

with open("app/agents/triage.py", "r") as f:
    content = f.read()

old_instruction_tail = r'"You have access to a long-term semantic memory store containing past Linux packaging sessions, build quirks, dependency workarounds, and resolution steps. \\n\\n"[\s\S]*?"2\. When you successfully resolve a nuanced packaging issue, ensure the relevant quirks, errors, and final working configurations are saved clearly to your memory so you can recall them in future sessions\."'

new_instruction_tail = r'"Use `search_memory` to check past triage history or general system queries if needed."'

content = re.sub(old_instruction_tail, new_instruction_tail, content)

with open("app/agents/triage.py", "w") as f:
    f.write(content)
