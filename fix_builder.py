with open("app/agents/builder.py", "r") as f:
    content = f.read()

content = content.replace('"When analyzing a build failure, prioritize using `search_memory` first to see if you have solved this quirk before.\n" \n            "When you successfully fix a build error, immediately use `save_memory_note` to record the exact error and the fix for future reference."',
                          '"When analyzing a build failure, prioritize using `search_memory` first to see if you have solved this quirk before.\\n" \\\n            "When you successfully fix a build error, immediately use `save_memory_note` to record the exact error and the fix for future reference."')

with open("app/agents/builder.py", "w") as f:
    f.write(content)
