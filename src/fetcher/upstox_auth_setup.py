"""
One-time OAuth2 setup script for Upstox.

Run this once to complete the authorization flow:
    python -m src.fetcher.upstox_auth_setup

After running, your token will be cached in data/upstox_token.json and
auto-refreshed by the main fetcher.
"""
import http.server
import threading
import webbrowser
from urllib.parse import parse_qs, urlparse

from src.config_loader import get_config
from src.fetcher.upstox import UpstoxAuth


def run_auth_setup():
    cfg = get_config()
    auth = UpstoxAuth(cfg)
    auth_code_holder = {}

    class CallbackHandler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            parsed = urlparse(self.path)
            params = parse_qs(parsed.query)
            auth_code_holder["code"] = params.get("code", [None])[0]
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"<h2>Authorization successful! You can close this tab.</h2>")

        def log_message(self, format, *args):
            pass  # Suppress server logs

    # Start a local server to capture the callback
    server = http.server.HTTPServer(("localhost", 8080), CallbackHandler)
    t = threading.Thread(target=server.handle_request)
    t.start()

    login_url = auth.get_login_url()
    print("\n" + "=" * 60)
    print("UPSTOX OAUTH2 SETUP")
    print("=" * 60)
    print(f"\nOpening browser to:\n{login_url}\n")
    print("If browser doesn't open, copy the URL above and open it manually.")
    print("After login, you'll be redirected to localhost:8080.\n")

    webbrowser.open(login_url)
    t.join(timeout=120)

    code = auth_code_holder.get("code")
    if not code:
        print("ERROR: No authorization code received. Please try again.")
        return

    print(f"Got auth code: {code[:10]}...")
    token_data = auth.exchange_code(code)
    print("\n✅ Token obtained and saved to data/upstox_token.json")
    print(f"   Access token (first 20 chars): {token_data['access_token'][:20]}...")
    print("\nYou're all set! The monitor will now use this token automatically.")
    print("Note: Upstox tokens expire daily. Run this script again if needed.\n")


if __name__ == "__main__":
    run_auth_setup()
