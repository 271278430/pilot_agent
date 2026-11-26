---
name: build-model-application
description: Implement the user's approved Web application and its model interaction without altering protected acceptance fixtures.
---
Read status, README.md and model_client.py. Write app.py and index.html to meet each approved requirement. Use stdlib HTTP service --port, /health, /, /chat. Preserve trusted model metadata from model_client.generate. Render answers via textContent; sandboxed UI uses the specified parent postMessage bridge. Do not fetch host APIs or external scripts. Run source_test and examine failures before exporting candidate. The code is authored from the user's goal, not a preset bug fix. Protected tests/client and model files are never editable.
