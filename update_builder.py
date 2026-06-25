import re

with open("app/agents/builder.py", "r") as f:
    content = f.read()

content = content.replace("save_session_to_memory", "save_memory_note")

old_instruction_tail = r'"You have access to a long-term semantic memory store containing past Linux packaging sessions, build quirks, dependency workarounds, and resolution steps. \\n\\n"[\s\S]*?"2\. When you successfully resolve a nuanced packaging issue, ensure the relevant quirks, errors, and final working configurations are saved clearly to your memory so you can recall them in future sessions\."'

new_instruction_tail = r'"When analyzing a build failure, prioritize using `search_memory` first to see if you have solved this quirk before.\n" \n            "When you successfully fix a build error, immediately use `save_memory_note` to record the exact error and the fix for future reference."'

content = re.sub(old_instruction_tail, new_instruction_tail, content)

with open("app/agents/builder.py", "w") as f:
    f.write(content)
