import os
os.environ['ADMIN_PASSWORD']='vertep-test-admin-secret'
from fastapi.testclient import TestClient
from core.app import app
client=TestClient(app)
resp=client.post('/api/system/state', json={'reason':'restore failed'})
print('status', resp.status_code)
print('body', resp.text)
