import re

with open('tests/test_features.py', 'r', encoding='utf-8') as f:
    content = f.read()

# Fix test_distributed_video_artifact_reaches_final_assembly
content = content.replace(
    '    job_id = response.json()["job_id"]\n    task = None\n    for _ in range(100):\n        task = client.post("/api/tasks/claim", json={"node_name": "video-worker", "vram_mb": 4096,\n                           "supported_tasks": ["video"], "supported_workflows": ["*"]]).json().get("task")',
    '    job_id = response.json()["job_id"]\n    approve_script(client, job_id)\n    task = None\n    for _ in range(100):\n        task = client.post("/api/tasks/claim", json={"node_name": "video-worker", "vram_mb": 4096,\n                           "supported_tasks": ["video"], "supported_workflows": ["*"]]).json().get("task")'
)

with open('tests/test_features.py', 'w', encoding='utf-8') as f:
    f.write(content)
print('OK')
