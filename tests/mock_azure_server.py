"""
Minimal mock server that mimics Azure OpenAI's chat completions response
shape, so we can test AzureOpenAIProvider's request/response handling
without real credentials or network access.
"""
import json
from http.server import BaseHTTPRequestHandler, HTTPServer


class MockHandler(BaseHTTPRequestHandler):
    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        body = json.loads(self.rfile.read(length))

        # Echo back something that references a REAL column (should pass
        # validation) mixed with one fake column (should get flagged).
        fake_reply = (
            "The Seq Scan on transactions is likely caused by a missing "
            "index on account_id. Consider: "
            "CREATE INDEX idx_transactions_account_id ON transactions(account_id); "
            "You might also check the imaginary_column field for skew."
        )

        response = {
            "choices": [
                {"message": {"role": "assistant", "content": fake_reply}}
            ]
        }
        payload = json.dumps(response).encode("utf-8")

        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, format, *args):
        pass  # silence default logging


if __name__ == "__main__":
    server = HTTPServer(("localhost", 8890), MockHandler)
    print("Mock Azure OpenAI server running on http://localhost:8890")
    server.serve_forever()
