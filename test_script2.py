import os, base64
os.environ['ADMIN_PASSWORD']='vertep-test-admin-secret'
hdr='Basic ' + base64.b64encode(b'admin:vertep-test-admin-secret').decode()
from fastapi.testclient import TestClient
from core.app import app
client=TestClient(app, headers={"Authorization":hdr})
resp=client.post('/api/system/state', json={'reason':'restore failed'})
print('status', resp.status_code)
print('body', resp.text)
