"""One-time helper: link your AliExpress account to your app and get the
access token the pipeline needs (the ALI_ACCESS_TOKEN GitHub secret).

Just run it and follow the prompts - it asks for everything it needs:

  python get_aliexpress_token.py

What it does, in order:
1. Asks for your App Key, App Secret, and your app's Callback URL
   (all three are on your app's page on open.aliexpress.com).
2. Prints the authorization address - open it in your browser, log in to
   the AliExpress account and approve.
3. The browser then lands on your callback page. Copy the FULL address
   from the address bar and paste it back into this script. Do this
   promptly - the code inside that address expires within minutes.
4. The script exchanges the code immediately and prints the ACCESS TOKEN.
   Save that as the ALI_ACCESS_TOKEN GitHub secret (Settings -> Secrets
   and variables -> Actions). The token is the value this script labels
   ALI_ACCESS_TOKEN at the end - NOT the short 3_xxxxxx_... code from the
   browser address bar.

NEVER type real keys into this file itself - the file is in the public
repo, so anything written here gets published.
"""
import os
import re
import sys
import time

import requests

from aliexpress_client import sign_params

TOKEN_ENDPOINT = "https://api-sg.aliexpress.com/rest/auth/token/create"
API_PATH = "/auth/token/create"


def ask(label, current=""):
    if current:
        return current
    try:
        return input(f"{label}: ").strip()
    except EOFError:
        return ""


def extract_code(pasted):
    """Accepts either the bare code or the whole callback URL."""
    pasted = pasted.strip()
    match = re.search(r"[?&]code=([^&\s]+)", pasted)
    if match:
        return match.group(1)
    return pasted


def exchange(app_key, app_secret, code):
    params = {
        "app_key": app_key,
        "timestamp": str(int(time.time() * 1000)),
        "sign_method": "sha256",
        "code": code,
    }
    params["sign"] = sign_params(params, app_secret, api_path=API_PATH)
    resp = requests.post(TOKEN_ENDPOINT, data=params, timeout=30)
    print(f"\nHTTP {resp.status_code}")
    print(resp.text[:1000])
    try:
        return resp.json()
    except ValueError:
        return {}


def main():
    app_key = ask("Paste your App Key", os.getenv("ALI_APP_KEY") or "")
    app_secret = ask("Paste your App Secret", os.getenv("ALI_APP_SECRET") or "")
    if not app_key or not app_secret:
        print("Both the App Key and App Secret are needed - run again.")
        sys.exit(1)

    # A code (or full callback URL) can also be passed directly:
    #   python get_aliexpress_token.py CODE_OR_URL
    if len(sys.argv) > 1:
        code = extract_code(sys.argv[1])
    else:
        callback = ask("Paste your app's Callback URL (from the app console)")
        if callback:
            print("\nOpen this address in your browser, log in and approve:\n")
            print(f"https://api-sg.aliexpress.com/oauth/authorize?response_type=code"
                  f"&force_auth=true&client_id={app_key}&redirect_uri={callback}\n")
        print("After approving, the browser lands on the callback page.")
        code = extract_code(ask("Copy the FULL address from the address bar and paste it here"))

    if not code:
        print("No code found - run again and paste the callback address straight away.")
        sys.exit(1)

    body = exchange(app_key, app_secret, code)
    access_token = body.get("access_token")
    refresh_token = body.get("refresh_token")
    if access_token:
        print("\n=== SUCCESS ===")
        print(f"ALI_ACCESS_TOKEN (save as GitHub secret): {access_token}")
        if refresh_token:
            print(f"refresh_token (keep somewhere safe):     {refresh_token}")
        if body.get("expire_time"):
            print(f"expires at (epoch ms): {body['expire_time']}")
    else:
        print("\nNo access_token in the response. If the error mentions the code, it expired "
              "(they last only minutes) or was already used once - every code works exactly one "
              "time. Do the browser approval again to get a FRESH code and paste the new "
              "callback address promptly.")


if __name__ == "__main__":
    main()
